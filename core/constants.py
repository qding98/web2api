"""全局常量：浏览器路径、CDP 端口等（新架构专用）。"""

import os
import shutil
import sys
from pathlib import Path


def _first_existing_path(candidates: list[str]) -> str | None:
    """从候选路径中返回第一个存在的浏览器可执行文件；输入为平台候选列表，输出为路径或 None。"""
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            return str(expanded)
    return None


def _first_on_path(names: list[str]) -> str | None:
    """从 PATH 中查找第一个可执行浏览器名称；输入为命令名列表，输出为绝对路径或 None。"""
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def _default_chromium_bin() -> str:
    """按当前系统推导默认浏览器路径；输入来自环境变量和常见安装路径，输出供 BrowserManager 使用。"""
    env_value = os.environ.get("WEB2API_CHROMIUM_BIN") or os.environ.get("CHROMIUM_BIN")
    if env_value and Path(env_value).expanduser().exists():
        return str(Path(env_value).expanduser())

    if sys.platform.startswith("win"):
        found = _first_existing_path(
            [
                r"C:\Program Files\fingerprint-chromium\chrome.exe",
                r"C:\Program Files (x86)\fingerprint-chromium\chrome.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        )
        return found or "chrome.exe"

    if sys.platform == "darwin":
        found = _first_existing_path(
            [
                "/Applications/fingerprint-chromium.app/Contents/MacOS/Chromium",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        )
        return found or "/Applications/Chromium.app/Contents/MacOS/Chromium"

    found = _first_existing_path(
        [
            "/opt/fingerprint-chromium/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
    )
    return found or _first_on_path(["chromium", "chromium-browser", "google-chrome", "chrome"]) or "/usr/bin/chromium"


# 与现有 multi_web2api 保持一致，便于同机运行时分端口
CHROMIUM_BIN = _default_chromium_bin()
REMOTE_DEBUGGING_PORT = 9223  # 默认端口，单浏览器兼容
# 多浏览器并存时的端口池（按 ProxyKey 各占一端口，仅当 refcount=0 时关闭并回收端口）
CDP_PORT_RANGE = list(range(9223, 9243))  # 9223..9232，最多 20 个并发浏览器
CDP_ENDPOINT = "http://127.0.0.1:9223"
TIMEZONE = "America/Chicago"
USER_DATA_DIR_PREFIX = "fp-data"  # user_data_dir = home / fp-data / fingerprint_id


def user_data_dir(fingerprint_id: str) -> Path:
    """按指纹 ID 拼接 user-data-dir，不依赖 profile_id。"""
    return Path.home() / USER_DATA_DIR_PREFIX / fingerprint_id
