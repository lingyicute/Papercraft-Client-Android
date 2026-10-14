"""应用配置的加载与持久化"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_ID = "uk._92li.cftm.CloudflaredTunnelManager"
APP_NAME = "Cloudflared 隧道管理器"
PROJECT_URL = "https://github.com/lingyicute/Cloudflared-Helper-Desktop"

def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent

def _find_data_dir() -> Path:
    """查找 data 目录，兼容 PyInstaller、源码运行、Flatpak 等多种布局。"""
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "data")
    # 源码运行：repo_root / data
    candidates.append(Path(__file__).resolve().parent.parent / "data")
    # Flatpak / 系统安装： /app/share/cftm/data, /app/share/cloudflared-tunnel-manager/data, /usr/share/...
    candidates.append(Path("/app/share/cftm/data"))
    candidates.append(Path("/app/share/cloudflared-tunnel-manager/data"))
    candidates.append(Path(__file__).resolve().parent / "data")
    for cand in candidates:
        if cand.is_dir():
            return cand
    # 回退：即使不存在也返回首选路径，保持旧行为，调用方再通过 is_file() 判断
    return _resource_root() / "data"

RESOURCE_DATA_DIR = _find_data_dir()
APP_ICON_FILE = RESOURCE_DATA_DIR / f"{APP_ID}.svg"
# 兜底：如果首选路径下图标不存在，尝试在常见系统图标路径中查找（Flatpak 已安装到 hicolor 时）
if not APP_ICON_FILE.is_file():
    for alt in (
        Path("/app/share/icons/hicolor/scalable/apps") / f"{APP_ID}.svg",
        Path("/usr/share/icons/hicolor/scalable/apps") / f"{APP_ID}.svg",
    ):
        if alt.is_file():
            APP_ICON_FILE = alt
            RESOURCE_DATA_DIR = alt.parent
            break

def _xdg(env: str, default: str) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else Path.home() / default


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "cloudflared-tunnel-manager"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "cloudflared-tunnel-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    # "latest-installed" | "system" | 具体版本号如 "2025.1.0"
    "active_version": "latest-installed",
    "tunnels": [],
    "window": {"width": 980, "height": 660},
    "clipboard_quick_tunnel": True,
}


class Config:
    """极简的 JSON 配置对象。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"[config] 读取配置失败，使用默认值: {exc}")

    def save(self) -> None:
        tmp: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 同目录原子替换；独占创建随机临时文件，避免固定 .tmp 的符号链接和并发覆盖。
            # NamedTemporaryFile 默认权限 0600，额外参数可能含 Access 凭据，不应公开读取。
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as fh:
                tmp = Path(fh.name)
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self.path)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            print(f"[config] 保存配置失败: {exc}")
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
