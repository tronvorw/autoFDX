"""
在线自更新：下载 GitHub 指定 tag 的源码 zipball，解压后由独立进程在主程序退出后覆盖项目目录并重启。

为何不能「运行中直接覆盖自己」：
- Windows 会锁定正在执行的 .py / .pyc / 部分被加载的 DLL；
- 正确做法是：子进程 WaitForSingleObject(旧 PID) → 再 shutil 覆盖 → 再启动新进程。

本模块供 UI 线程调用：下载与解压可在后台线程执行；拉起 apply_update 须在主线程或确认路径有效后立刻退出主程序。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .update_check import github_request_headers

# 优先：github.com 归档 zip，不经过 api.github.com，可避免与 REST 相同的匿名 IP 限流。
def _github_archive_zip_url(owner_repo: str, tag: str) -> str:
    or_ = owner_repo.strip().strip("/")
    parts = or_.split("/", 1)
    if len(parts) != 2:
        return ""
    own, rep = parts[0], parts[1]
    t = quote(tag.strip(), safe="")
    return f"https://github.com/{own}/{rep}/archive/refs/tags/{t}.zip"


# 回退：GitHub zipball API（可能与检查更新共享匿名限额，故作次选）
def _zipball_url(owner_repo: str, tag: str) -> str:
    or_ = owner_repo.strip().strip("/")
    t = quote(tag.strip(), safe="")
    return f"https://api.github.com/repos/{or_}/zipball/{t}"


def staging_root(project_root: Path) -> Path:
    """临时目录：位于项目下，便于 apply 脚本用绝对路径找到。"""
    return (project_root / ".update_staging").resolve()


def clear_staging(project_root: Path) -> None:
    """每次更新前清空 staging，避免混入旧文件。"""
    root = staging_root(project_root)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)


def download_zipball(owner_repo: str, tag: str, dest_zip: Path, timeout_sec: float = 300.0) -> str | None:
    """
    下载 tag 对应源码 zip 到 dest_zip。
    优先使用 github.com/archive 直链，失败再尝试 api.github.com zipball。
    失败返回错误说明字符串；成功返回 None。
    """
    urls = [_github_archive_zip_url(owner_repo, tag), _zipball_url(owner_repo, tag)]
    last_err: Exception | None = None
    for url in urls:
        if not url:
            continue
        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        req = Request(url, headers=github_request_headers())
        try:
            with urlopen(req, timeout=timeout_sec) as resp, open(dest_zip, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            return None
        except Exception as e:
            last_err = e
            try:
                if dest_zip.is_file():
                    dest_zip.unlink()
            except OSError:
                pass
    return f"下载更新包失败：{last_err}"


def extract_zipball(zip_path: Path, extract_to: Path) -> tuple[Path | None, str | None]:
    """
    解压 zip；GitHub zipball 根下仅一层目录 `owner-repo-sha/`。
    返回 (该目录的 Path, 错误信息)。
    """
    extract_to.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_to)
    except zipfile.BadZipFile:
        return None, "更新包不是有效 zip"
    except OSError as e:
        return None, f"解压失败：{e}"

    top = [p for p in extract_to.iterdir() if p.is_dir()]
    if len(top) != 1:
        return None, "解压目录结构异常（预期仅一层根文件夹）"
    return top[0], None


def prepare_tag_source_folder(owner_repo: str, tag: str, project_root: Path) -> tuple[Path | None, str | None]:
    """
    下载并解压指定 tag，返回发布根目录（解压后的唯一子文件夹）。
    失败时 (None, error_message)。
    """
    root = project_root.resolve()
    clear_staging(root)
    base = staging_root(root)
    zip_path = base / "release.zip"
    err = download_zipball(owner_repo, tag, zip_path)
    if err:
        return None, err
    inner, err2 = extract_zipball(zip_path, base / "extract")
    if err2:
        return None, err2
    return inner, None


def spawn_post_exit_apply(
    *,
    source_inner: Path,
    project_root: Path,
    wait_pid: int,
    python_exe: str | None = None,
    restart_script: Path | None = None,
) -> tuple[bool, str]:
    """
    启动 tools/apply_update.py：在 wait_pid 退出后覆盖文件并重启。

    返回 (是否已成功提交子进程, 说明)。子进程启动失败时第二项为错误原因。
    """
    root = project_root.resolve()
    apply_py = root / "tools" / "apply_update.py"
    if not apply_py.is_file():
        return False, f"缺少 {apply_py}，无法自动替换文件"

    entry = restart_script or (root / "fallen_doll.py")
    if not entry.is_file():
        return False, f"找不到入口脚本 {entry}"

    py = python_exe or sys.executable
    cmd = [
        py,
        str(apply_py),
        "--wait-pid",
        str(wait_pid),
        "--source",
        str(source_inner.resolve()),
        "--dest",
        str(root),
        "--restart",
        str(entry.resolve()),
        "--python",
        py,
    ]

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(root),
            close_fds=False if os.name == "nt" else True,
            creationflags=flags,
        )
    except OSError as e:
        return False, f"无法启动更新进程：{e}"
    return True, ""
