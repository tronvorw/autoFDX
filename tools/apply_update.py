"""
自更新落地脚本：必须在主程序退出后运行，因此由主程序用 subprocess 提前拉起，仅依赖标准库。

流程概要：
1) 等待旧进程 PID 结束（Windows 用 WaitForSingleObject；其他平台轮询 os.kill(pid, 0)）。
2) 将「解压后的发布根目录」中的文件覆盖复制到项目根目录（合并覆盖，不做镜像删除）。
3) 删除 .update_staging 临时目录。
4) 用同一解释器重新启动 fallen_doll.py。

保护规则（避免覆盖用户数据）：
- 不覆盖已存在的 data/user_config.json。
- 不覆盖已存在的 assets/templates/custom_*.png（用户标定模板）。
- 跳过源中的 __pycache__、*.pyc、.update_staging。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _log(line: str, log_path: Path) -> None:
    """追加一行到日志文件，失败则忽略（尽量不阻断更新）。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _wait_process_exit(pid: int, timeout_sec: float) -> bool:
    """
    等待指定 PID 退出；若在超时内结束返回 True，超时返回 False。
    若进程已不存在，视为 True（可继续复制）。
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 仅需同步句柄以等待终止
        PROCESS_SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(PROCESS_SYNCHRONIZE, 0, pid)
        if not handle:
            return True
        ms = int(timeout_sec * 1000)
        WAIT_OBJECT_0 = 0
        ret = kernel32.WaitForSingleObject(handle, ms)
        kernel32.CloseHandle(handle)
        return ret == WAIT_OBJECT_0

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.25)
    return False


def _should_skip_source_rel(rel: Path) -> bool:
    """源树中不参与复制的路径（相对发布根）。"""
    parts = rel.parts
    if "__pycache__" in parts:
        return True
    if ".update_staging" in parts:
        return True
    if rel.name.endswith(".pyc"):
        return True
    return False


def _should_skip_overwrite_dest(rel_posix: str, dest_file: Path) -> bool:
    """目标已存在时需保留的用户文件。"""
    if rel_posix == "data/user_config.json" and dest_file.is_file():
        return True
    if (
        rel_posix.startswith("assets/templates/")
        and dest_file.name.startswith("custom_")
        and dest_file.suffix.lower() == ".png"
        and dest_file.is_file()
    ):
        return True
    return False


def overlay_copy(source_root: Path, dest_root: Path, log_path: Path) -> None:
    """
    将 source_root 下的文件合并复制到 dest_root。
    不删除目标中多余旧文件（避免误删用户自建内容）。
    """
    source_root = source_root.resolve()
    dest_root = dest_root.resolve()
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source_root)
        if _should_skip_source_rel(rel):
            continue
        rel_posix = rel.as_posix()
        dest_file = dest_root / rel
        if _should_skip_overwrite_dest(rel_posix, dest_file):
            _log(f"保留已有: {rel_posix}", log_path)
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest_file)
        _log(f"已更新: {rel_posix}", log_path)


def _restart(python_exe: str, script_path: Path, cwd: Path, log_path: Path) -> None:
    """启动新版本入口脚本；与控制台/无窗启动方式无关，由解释器决定。"""
    script_path = script_path.resolve()
    cwd = cwd.resolve()
    try:
        flags = 0
        if os.name == "nt":
            # 不额外弹出黑色 CMD（若解释器本身是 pythonw 则本来也无控制台）
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        subprocess.Popen(
            [python_exe, str(script_path)],
            cwd=str(cwd),
            close_fds=False if os.name == "nt" else True,
            creationflags=flags,
        )
        _log(f"已请求重启: {python_exe} {script_path}", log_path)
    except OSError as e:
        _log(f"重启失败: {e}", log_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="autoFDX 退出后覆盖安装并重启")
    parser.add_argument("--wait-pid", type=int, required=True, help="需等待结束的主进程 PID")
    parser.add_argument("--source", type=Path, required=True, help="解压后的发布根目录（单层子目录内）")
    parser.add_argument("--dest", type=Path, required=True, help="项目根目录")
    parser.add_argument("--restart", type=Path, required=True, help="重启时执行的脚本路径（通常为 fallen_doll.py）")
    parser.add_argument("--python", type=str, default=sys.executable, help="用于重启的解释器路径")
    parser.add_argument("--wait-seconds", type=float, default=180.0, help="等待旧进程退出的超时（秒）")
    args = parser.parse_args()

    dest: Path = args.dest.resolve()
    log_path = dest / "logs" / "update_apply.log"
    _log("======== 开始应用更新 ========", log_path)
    _log(f"wait_pid={args.wait_pid} source={args.source} dest={dest}", log_path)

    if not _wait_process_exit(args.wait_pid, args.wait_seconds):
        _log("错误：等待旧进程退出超时，已中止复制（请手动关闭程序后重试）", log_path)
        return 2

    source_root = args.source.resolve()
    if not source_root.is_dir():
        _log(f"错误：源目录不存在 {source_root}", log_path)
        return 3

    try:
        overlay_copy(source_root, dest, log_path)
    except OSError as e:
        _log(f"复制过程出错: {e}", log_path)
        return 4

    staging = dest / ".update_staging"
    if staging.is_dir():
        try:
            shutil.rmtree(staging, ignore_errors=True)
            _log("已清理 .update_staging", log_path)
        except OSError as e:
            _log(f"清理 staging 失败（可手动删）: {e}", log_path)

    _restart(args.python, args.restart, dest, log_path)
    _log("======== 应用更新流程结束 ========", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
