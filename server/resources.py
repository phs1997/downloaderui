import os
import shutil
from typing import Optional, Dict, Any

STORAGE_PATH = os.getenv("STORAGE_PATH", "/output")

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf"
)


def format_bytes(bytes_count: Optional[int]) -> str:
    if not bytes_count or bytes_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i, val = 0, float(bytes_count)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.2f} {units[i]}"


def format_speed(bps: Optional[int]) -> str:
    if not bps or bps <= 0:
        return "0 B/s"
    return f"{format_bytes(bps)}/s"


def format_eta(secs: Optional[int]) -> str:
    if not secs or secs <= 0:
        return "--"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    elif m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_disk_free(bytes_count: Optional[int]) -> str:
    if not bytes_count or bytes_count <= 0:
        return "0 GB"
    gb = bytes_count / (1024 ** 3)
    if gb >= 1024:
        tb = gb / 1024
        return f"{tb:.1f} TB"
    return f"{int(round(gb))} GB"


def get_disk_info() -> Optional[Dict[str, Any]]:
    path_to_check = STORAGE_PATH if os.path.exists(STORAGE_PATH) else "/"
    try:
        usage = shutil.disk_usage(path_to_check)
        return {
            "free": usage.free,
            "total": usage.total,
            "used": usage.used,
            "free_formatted": format_disk_free(usage.free),
            "total_formatted": format_bytes(usage.total),
            "used_formatted": format_bytes(usage.used),
            "percent_used": round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0.0
        }
    except Exception:
        return None
