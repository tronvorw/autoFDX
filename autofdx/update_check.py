"""
GitHub 在线版本检查（标准库 urllib，无额外 HTTP 依赖）。

说明：
- **Release / Beta 通道**：优先读 `GET /repos/{owner}/{repo}/releases`（与你在 GitHub 上创建 Release
  时填写的「更新说明」一致）；Release 通道仅 `prerelease=false`；Beta 通道包含预发布。
- **回退**：若当前通道没有可用 Release，则回退到 `GET .../tags` 纯 tag 列表（此时无发行说明，
  UI 会提示你到仓库查看）。
- 版本排序：在 tag 名上做语义化比较（支持 v 前缀、-beta.N 等）。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from .config import UPDATE_MANIFEST_BRANCHES
from .config import UPDATE_MANIFEST_FILENAME
from .config import UPDATE_MANIFEST_OVERRIDE_URL

# GitHub 要求请求携带 User-Agent，否则可能 403 或限流异常。
_DEFAULT_UA = "autoFDX-update-check (+https://github.com/tronvorw/autoFDX)"


def github_request_headers() -> dict[str, str]:
    """
    访问 GitHub REST API 的统一请求头。

    - 未带 Token 时，同一公网 IP 约 60 次/小时，超限常见表现为 HTTP 403。
    - 可在系统环境变量中设置 **GITHUB_TOKEN** 或 **AUTOFDX_GITHUB_TOKEN**（只读 classic PAT
      或 fine-grained 即可），限额会显著提高。
    """
    h: dict[str, str] = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/vnd.github+json",
        # 显式 API 版本，减少服务端对旧客户端的异常拒绝（见 GitHub REST 文档）。
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("AUTOFDX_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _normalize_version_string(raw: str) -> str:
    """去掉首尾空白、可选 v 前缀，并截断 build 元数据（+ 之后）。"""
    s = (raw or "").strip().lstrip("vV")
    if "+" in s:
        s = s.split("+", 1)[0]
    return s


def _parse_version_key(name: str) -> tuple:
    """
    将 tag / 版本号转为可排序元组；更大表示更新。

    结构大致为：
    (major, minor, patch, release_flag, prerelease_tuple)
    - release_flag：1 表示无预发布后缀的「正式」语义；0 表示仍带 -beta 等段
    - prerelease_tuple：预发布排序用 (类型权重, 序号)；正式版为 ()

    解析失败时返回 (-1,) 以便调用方过滤。
    """
    s = _normalize_version_string(name)
    if not s:
        return (-1,)

    # 主版本与可选预发布：1.2.3 / 1.2 / 1 / 1.0.0-beta.2 / 1.0.0-beta
    m = re.match(
        r"^(\d+)(?:\.(\d+)(?:\.(\d+))?)?(?:-([a-zA-Z][a-zA-Z0-9]*)(?:\.(\d+))?)?$",
        s,
    )
    if not m:
        # 退而求其次：只取开头的数字段，避免完全无法比较
        m2 = re.match(r"^(\d+)(?:\.(\d+)(?:\.(\d+))?)?", s)
        if not m2:
            return (-1,)
        major, minor, patch = int(m2.group(1)), int(m2.group(2) or 0), int(m2.group(3) or 0)
        return (major, minor, patch, 0, (-99, 0))

    major, minor, patch = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    pre_label, pre_num = m.group(4), m.group(5)
    if not pre_label:
        # 无 -xxx 后缀：视为正式版，排序上高于同主/次/修订的预发布
        return (major, minor, patch, 1, ())

    num = int(pre_num) if pre_num is not None else 0
    label = pre_label.lower()
    # 预发布类型权重：rc > beta > alpha > dev（可按需扩展）
    rank = {
        "dev": -1,
        "alpha": 0,
        "a": 0,
        "beta": 1,
        "b": 1,
        "preview": 1,
        "rc": 2,
        "cr": 2,
    }.get(label, 0)
    return (major, minor, patch, 0, (rank, num))


def is_stable_semver_tag(tag: str) -> bool:
    """
    是否视为「正式版」tag：无 -beta / -rc 等预发布后缀（与 _parse_version_key 中 release_flag 一致）。
    Release 通道在仅 tag、无 GitHub Release 时，只考虑此类 tag。
    """
    k = _parse_version_key(tag)
    if k == (-1,):
        return False
    return k[3] == 1


def _max_tag_name(tags_json: list[dict[str, Any]], stable_only: bool = False) -> str | None:
    """从 API 返回的 tag 列表中选出语义上最新的 name；stable_only 时跳过预发布语义 tag。"""
    best_name = None
    best_key = None
    for item in tags_json:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = _parse_version_key(name)
        if key == (-1,):
            continue
        if stable_only and key[3] != 1:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best_name = name
    return best_name


def _parse_github_api_message(body: str) -> str:
    """解析 GitHub REST 错误 JSON 中的 message，便于弹窗展示真实原因。"""
    try:
        j = json.loads(body)
        m = j.get("message")
        if isinstance(m, str) and m.strip():
            return m.strip()
    except Exception:
        pass
    return ""


def _http_error_hint(code: int, body_snippet: str) -> str:
    """根据 HTTP 状态与响应正文生成面向用户的补充说明（中文）。"""
    low = (body_snippet or "").lower()
    if code in (401, 403):
        if "rate limit" in low or code == 403:
            return (
                "\n\n说明：GitHub **匿名 REST API** 按「出口公网 IP」计次（约 60 次/小时），"
                "与您本机这一小时的点击次数无必然关系——同一运营商 NAT、公司网络、云代理等会共享该额度。\n"
                "可选：① 设置环境变量 GITHUB_TOKEN / AUTOFDX_GITHUB_TOKEN；"
                "② 在仓库维护根目录 version_manifest.json（从 raw 读取，不走 REST API，见项目内示例文件）。"
            )
        return "\n\n请检查 Token 是否有效、是否具备访问该仓库的权限。"
    return ""


def _fetch_json_list(url: str, timeout_sec: float) -> tuple[list[Any] | None, str | None]:
    """GET JSON 数组；失败返回 (None, error)。"""
    req = urllib.request.Request(url, headers=github_request_headers())
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1200]
        except Exception:
            pass
        if e.code == 404:
            return None, "仓库不存在或无权访问"
        api_msg = _parse_github_api_message(err_body)
        hint = _http_error_hint(e.code, err_body)
        line = f"GitHub API 错误 HTTP {e.code}"
        if api_msg:
            line = f"{line}\n{api_msg}"
        return None, line + hint
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, f"网络错误：{reason}"
    except json.JSONDecodeError:
        return None, "GitHub 返回非 JSON（可能被限流或代理篡改）"
    except Exception as e:
        return None, f"请求失败：{e}"

    if not isinstance(data, list):
        return None, "GitHub 返回格式异常（预期为列表）"
    return data, None


def fetch_releases_list(owner_repo: str, timeout_sec: float = 18.0) -> tuple[list[dict[str, Any]], str | None]:
    """
    拉取仓库 Releases（含预发布）；按 API 默认顺序（通常较新在前），后续用 semver 再筛最大值。
    """
    owner_repo = (owner_repo or "").strip().strip("/")
    if not owner_repo or "/" not in owner_repo:
        return [], "未配置有效的 GitHub 仓库（格式 owner/repo）"
    url = f"https://api.github.com/repos/{owner_repo}/releases?per_page=100"
    raw, err = _fetch_json_list(url, timeout_sec)
    if err:
        return [], err
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
    return out, None


def pick_latest_release_for_channel(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    """
    在当前通道下选取 semver 最大的 Release。
    - release：仅 prerelease == False
    - beta：含 prerelease
    """
    if channel not in ("release", "beta"):
        channel = "release"
    best: dict[str, Any] | None = None
    best_key: tuple | None = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        pre = bool(r.get("prerelease", False))
        if channel == "release" and pre:
            continue
        tag = r.get("tag_name")
        if not isinstance(tag, str) or not tag.strip():
            continue
        key = _parse_version_key(tag)
        if key == (-1,):
            continue
        if best_key is None or key > best_key:
            best_key = key
            best = r
    return best


def _collect_manifest_urls(owner_repo: str) -> list[str]:
    """
    检查更新时依次尝试的清单 URL（均不走 api.github.com REST）。
    顺序：配置/环境变量自定义 → raw.githubusercontent → jsDelivr（CDN，常与 REST 限流解耦）。
    """
    owner_repo = (owner_repo or "").strip().strip("/")
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    add(UPDATE_MANIFEST_OVERRIDE_URL or "")
    add((os.environ.get("AUTOFDX_UPDATE_MANIFEST_URL") or "").strip())

    if owner_repo and "/" in owner_repo:
        # 查询参数仅用于绕过 raw/jsDelivr 对 main 上清单的短时旧缓存，避免「仓库已 beta.3 但检查仍读到 beta.2」。
        bust = int(time.time())
        for br in UPDATE_MANIFEST_BRANCHES:
            q = f"?_={bust}"
            add(
                f"https://raw.githubusercontent.com/{owner_repo}/{br}/{UPDATE_MANIFEST_FILENAME}{q}"
            )
            add(f"https://cdn.jsdelivr.net/gh/{owner_repo}@{br}/{UPDATE_MANIFEST_FILENAME}{q}")
    return out


def _candidate_from_manifest_obj(obj: dict[str, Any], channel: str) -> dict[str, Any] | None:
    """从已解析的 manifest 字典中提取当前通道的候选版本；无效则 None。"""
    if channel not in ("release", "beta"):
        channel = "release"
    entry = obj.get(channel)
    if not isinstance(entry, dict):
        latest = obj.get("latest")
        if isinstance(latest, dict):
            entry = latest
    if not isinstance(entry, dict):
        return None
    tag = str(entry.get("tag") or "").strip()
    if not tag:
        return None
    notes = str(entry.get("notes") or "").strip()
    title = str(entry.get("title") or "").strip()
    k = _parse_version_key(tag)
    is_pre = k != (-1,) and k[3] == 0
    return {
        "tag_name": tag,
        "notes": notes,
        "is_prerelease": is_pre,
        "html_url": "",
        "release_title": title,
        "from_release": False,
        "skipped_releases_due_to_error": False,
        "from_manifest": True,
    }


def _fetch_raw_text(url: str, timeout_sec: float) -> tuple[str | None, int | None]:
    """
    GET 返回 UTF-8 文本。成功 (text, None)；失败 (None, http_code)，非 HTTP 错误为 -1。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return None, int(e.code)
    except Exception:
        return None, -1


def fetch_update_candidate_from_manifest(
    owner_repo: str,
    channel: str,
    timeout_sec: float = 15.0,
) -> dict[str, Any] | None:
    """
    从 HTTPS 清单拉取版本信息（raw / jsDelivr / 自建 URL），不调用 api.github.com REST。

    期望 JSON 结构示例：
    {
      "release": { "tag": "v1.0.0", "notes": "…", "title": "可选标题" },
      "beta":    { "tag": "v1.0.0-beta.2", "notes": "…" }
    }
    若仅有 "latest" 键且为对象，则 release / beta 在缺省时都可回退到 latest。
    """
    if channel not in ("release", "beta"):
        channel = "release"

    for url in _collect_manifest_urls(owner_repo):
        text, _code = _fetch_raw_text(url, timeout_sec)
        if text is None:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        cand = _candidate_from_manifest_obj(obj, channel)
        if cand:
            return cand
    return None


def fetch_latest_update_candidate(
    owner_repo: str,
    channel: str,
    timeout_sec: float = 20.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    综合 Release（优先，含更新说明）与 tag 回退，得到当前通道下的「远程最新」描述。

    成功时返回字典，键：
    - tag_name: str
    - notes: str（Release 正文；无 Release 时为空，由 UI 显示占位说明）
    - is_prerelease: bool
    - html_url: str（Release 页；无则空）
    - release_title: str（Release 标题；无则空）
    - from_release: bool（是否来自 GitHub Release 而非纯 tag）
    """
    owner_repo = (owner_repo or "").strip().strip("/")
    if not owner_repo or "/" not in owner_repo:
        return None, "未配置有效的 GitHub 仓库（格式 owner/repo）"
    if channel not in ("release", "beta"):
        channel = "release"

    # 优先清单（raw / jsDelivr / 自建 URL）：不消耗 GitHub REST 配额，共享 IP 限流时仍可检查更新。
    mc = fetch_update_candidate_from_manifest(owner_repo, channel, timeout_sec)
    if mc:
        return mc, None

    # Releases 接口更易触发限流；失败时不阻断整次检查，回退到仅 tag（无发行说明）。
    releases, rerr = fetch_releases_list(owner_repo, timeout_sec=timeout_sec)
    chosen = None
    if not rerr and releases:
        chosen = pick_latest_release_for_channel(releases, channel)
    if chosen:
        tag_name = str(chosen.get("tag_name", "")).strip()
        if not tag_name:
            chosen = None
        else:
            return {
                "tag_name": tag_name,
                "notes": str(chosen.get("body") or "").strip(),
                "is_prerelease": bool(chosen.get("prerelease", False)),
                "html_url": str(chosen.get("html_url") or "").strip(),
                "release_title": str(chosen.get("name") or "").strip(),
                "from_release": True,
                "skipped_releases_due_to_error": False,
                "from_manifest": False,
            }, None

    # 无可用 Release：回退 tag 列表（作者只打 tag、未发 Release 时）
    stable_only = channel == "release"
    url = f"https://api.github.com/repos/{owner_repo}/tags?per_page=100"
    data, terr = _fetch_json_list(url, timeout_sec)
    latest = None
    if not terr and data:
        latest = _max_tag_name(data, stable_only=stable_only)

    if latest:
        k = _parse_version_key(latest)
        is_pre = k != (-1,) and k[3] == 0
        return {
            "tag_name": latest,
            "notes": "",
            "is_prerelease": is_pre,
            "html_url": "",
            "release_title": "",
            "from_release": False,
            "skipped_releases_due_to_error": bool(rerr),
            "from_manifest": False,
        }, None

    if terr:
        return None, terr

    hint = (
        "Release 通道下暂无正式版 tag（若只有 Beta tag，请开启 Beta 通道），"
        "且未拉取到有效清单（请推送 version_manifest.json，或配置 UPDATE_MANIFEST_OVERRIDE_URL / 环境变量 AUTOFDX_UPDATE_MANIFEST_URL）。"
        if stable_only
        else "暂无可用远程版本：REST 与 tag 均不可用，且清单拉取失败；请确认清单已发布或使用 Token。"
    )
    return None, hint


def fetch_latest_tag_name(
    owner_repo: str, timeout_sec: float = 12.0, stable_only: bool = False
) -> tuple[str | None, str | None]:
    """
    查询远程最新 tag 名。

    返回 (latest_tag_name, error_message)：
    - error_message 非空表示配置无效、网络或 API 错误；
    - 成功时 latest_tag_name 为字符串（与 GitHub 上 tag 名一致，通常带 v 前缀）。
    """
    owner_repo = (owner_repo or "").strip().strip("/")
    if not owner_repo or "/" not in owner_repo:
        return None, "未配置有效的 GitHub 仓库（格式 owner/repo）"

    url = f"https://api.github.com/repos/{owner_repo}/tags?per_page=100"
    data, err = _fetch_json_list(url, timeout_sec)
    if err:
        return None, f"{err}：{owner_repo}"

    latest = _max_tag_name(data, stable_only=stable_only)
    if not latest:
        msg = (
            "仓库暂无符合 Release 通道的正式版 tag（仅含预发布 tag 时请使用 Beta 通道）。"
            if stable_only
            else "仓库暂无可用版本 tag（需为可解析的语义化名称）。"
        )
        return None, msg
    return latest, None


def github_tree_url_for_tag(owner_repo: str, tag_name: str) -> str:
    """浏览器打开该 tag 对应源码树（不依赖是否创建 GitHub Release）。"""
    or_ = owner_repo.strip().strip("/")
    t = tag_name.strip()
    return f"https://github.com/{or_}/tree/{t}"


def is_remote_newer(current_version: str, remote_tag: str) -> bool:
    """当前版本是否严格早于远程 tag。"""
    a, b = _parse_version_key(current_version), _parse_version_key(remote_tag)
    if a == (-1,) or b == (-1,):
        return False
    return b > a
