"""带时间戳的短码控制台日志。"""

from datetime import datetime

from . import log_codes as C


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(value):
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_fmt(v) for v in value) + "]"
    return str(value)


def log(code, /, **fields):
    """打印一行: [时间] 错误码 key=value ..."""
    parts = []
    for key, val in fields.items():
        if val is None:
            continue
        parts.append(f"{key}={_fmt(val)}")
    suffix = (" " + " ".join(parts)) if parts else ""
    print(f"\n[{_ts()}] {code}{suffix}")


def log_debug(enabled, code, /, **fields):
    if enabled:
        log(code, **fields)


def log_status(text):
    """主流程状态行（无业务码，仅时间戳）。"""
    log(C.ST000, msg=text)


def hint(code):
    """返回错误码说明（供文档/调试，不打印）。"""
    return C.LOG_HINTS.get(code, "")
