"""cloudflared 二进制文件的版本管理。

功能：
- 自动检测系统 / 架构，匹配官方发布的资源文件名
- 从 GitHub Releases 获取版本列表
- 下载、安装（自动 chmod +x）、删除指定版本
- 在“最新已安装 / 系统 PATH / 指定版本”之间解析当前应使用的二进制
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import tempfile
import time
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

GITHUB_API = "https://api.github.com/repos/cloudflare/cloudflared/releases?per_page=30"
DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/download"
USER_AGENT = "cloudflared-tunnel-manager/1.0 (+https://github.com/lingyicute/Cloudflared-Helper-Desktop)"


class DownloadCancelled(Exception):
    pass


def detect_platform() -> tuple[str, str]:
    """返回 (system, arch)，arch 使用 cloudflared 发布时的命名。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("armv") or machine == "arm":
        arch = "arm"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    else:
        arch = machine
    return system, arch


def asset_name(system: str, arch: str) -> str:
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    if system == "windows":
        return f"cloudflared-windows-{arch}.exe"
    return f"cloudflared-linux-{arch}"


def version_key(tag: str) -> tuple:
    """先比较主版本，再比较预发布标识；构建元数据不影响优先级。"""
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
        tag if isinstance(tag, str) else "",
    )
    if match is None:
        return (0, 0, 0), -1, ()
    core = tuple(int(match[i]) for i in (1, 2, 3))
    prerelease = match[4]
    # 数字标识按数值排序，且低于非数字标识（SemVer）；稳定版高于所有同号预发布。
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    ) if prerelease is not None else ()
    return core, int(prerelease is None), identifiers


def _is_safe_tag(tag: object) -> bool:
    """版本号只能是单级目录名，拒绝 /, \\, .. 等路径穿越。"""
    if not isinstance(tag, str) or not tag.strip():
        return False
    if tag in (".", ".."):
        return False
    if "/" in tag or "\\" in tag or ".." in tag or "\x00" in tag:
        return False
    # 额外拒绝以 '.' 开头的隐藏目录，避免 .hidden 这类
    if tag.startswith("."):
        return False
    return True

def _parse_sha256_digest(digest: object) -> Optional[str]:
    """把 GitHub API 的 ``sha256:<hex>`` 解析成小写 hex；格式不认识就返回 None（跳过校验）。"""
    if not isinstance(digest, str):
        return None
    algo, _, hexdigest = digest.strip().partition(":")
    hexdigest = hexdigest.strip().lower()
    if algo.strip().lower() != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
        return None
    return hexdigest


def format_size(num: float) -> str:
    """把字节数格式化为人类可读的字符串，如「12.3 MB」。"""
    num = float(num)
    for unit in ("B", "KB", "MB"):
        if num < 1024:
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def format_speed(bytes_per_sec: float) -> str:
    """把下载速度格式化为「x.x MB/s」。"""
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(seconds: float) -> str:
    """把剩余秒数格式化为「剩余 1 分 05 秒」这样的文本。"""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"剩余 {seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"剩余 {minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"剩余 {hours} 小时 {minutes:02d} 分"

class BinaryManager:
    def __init__(self, versions_dir: Path):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.system, self.arch = detect_platform()
        self.asset = asset_name(self.system, self.arch)
        self._active_downloads: dict[str, threading.Event] = {}  # tag -> cancel event
        self._download_stats: dict[str, dict] = {}  # tag -> 下载统计（字节数 / 速度 / 剩余时间）
        self._clean_stale_parts()

    def _clean_stale_parts(self) -> None:
        """清掉上次异常退出时残留的 .part / .extracting 文件，避免越积越大。"""
        try:
            # 清理所有可能的二进制名对应的残留文件，兼容跨平台切换或旧版本残留
            exe_names = {"cloudflared", "cloudflared.exe"}
            exe_names.add(self.exe_name)
            for suffix in (".part", ".extracting"):
                for exe in exe_names:
                    for part in self.versions_dir.glob(f"*/{exe}{suffix}"):
                        try:
                            if not part.parent.is_symlink() and part.is_file():
                                part.unlink()
                        except OSError:
                            pass
            # 顺手收掉因此变空的版本目录
            for child in self.versions_dir.iterdir():
                try:
                    if child.is_dir() and not any(child.iterdir()):
                        child.rmdir()
                except OSError:
                    pass
        except OSError:
            pass

    # ------------------------------------------------------------------ 路径
    @property
    def exe_name(self) -> str:
        return "cloudflared.exe" if self.system == "windows" else "cloudflared"

    def path_for(self, tag: str) -> Path:
        if not _is_safe_tag(tag):
            raise ValueError("非法的版本号")
        return self.versions_dir / tag / self.exe_name

    def installed(self) -> list[str]:
        """只列出当前平台可执行的普通文件，不将目录或异平台 .exe 视为已安装。"""
        children = self.versions_dir.iterdir() if self.versions_dir.exists() else []
        return sorted(
            (child.name for child in children if self.is_installed(child.name)),
            key=version_key, reverse=True,
        )

    def is_installed(self, tag: str) -> bool:
        if not _is_safe_tag(tag):
            return False
        path = self.path_for(tag)
        return (
            not path.parent.is_symlink() and not path.is_symlink() and path.is_file()
            and (self.system == "windows" or os.access(path, os.X_OK))
        )

    def is_downloading(self, tag: str) -> bool:
        return tag in self._active_downloads

    def download_stats(self, tag: str) -> Optional[dict]:
        """正在进行的下载统计：{"got", "total", "speed", "eta"}；没有则返回 None。"""
        # 返回拷贝，避免后台线程与主线程同时读写同一 dict 导致的不一致
        data = self._download_stats.get(tag)
        return dict(data) if data else None

    def system_binary(self) -> Optional[str]:
        return shutil.which("cloudflared")

    def resolve(self, active: object) -> Optional[str]:
        """根据配置解析出应使用的二进制路径；找不到返回 None。"""
        # config.json 手改坏时 active 可能是数字/null：非字符串一律按自动处理，
        # 否则 path_for(Path / int) 会抛 TypeError 导致 UI 刷新崩溃。
        if not isinstance(active, str):
            active = "latest-installed"
        if active == "system":
            return self.system_binary()
        if active == "latest-installed":
            inst = self.installed()
            if inst:
                return str(self.path_for(inst[0]))
            return self.system_binary()
        if self.is_installed(active):
            return str(self.path_for(active))
        # 回退：指定的版本已被删除或非法
        inst = self.installed()
        return str(self.path_for(inst[0])) if inst else self.system_binary()

    # ------------------------------------------------------------------ 探测
    @staticmethod
    def probe_version(path: str) -> Optional[str]:
        """运行 `cloudflared --version` 并返回精简的版本字符串。"""
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=8
            )
            text = (out.stdout or out.stderr).strip()
            match = re.search(r"version\s+(\S+)", text, re.IGNORECASE)
            return match.group(1) if match else (text.splitlines()[0] if text else None)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 远程
    def fetch_releases(self, callback: Callable[[Optional[list], Optional[str]], None]) -> None:
        """异步获取版本列表，回调在 GTK 主线程执行：callback(releases, error)。"""

        def work() -> None:
            try:
                req = urllib.request.Request(
                    GITHUB_API,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.load(resp)
                # 触发限流等错误时 GitHub 返回的是 {"message": ...} 而非列表，
                # 直接遍历 dict 会报 "'str' object has no attribute 'get'" 让人摸不着头脑。
                if isinstance(data, dict):
                    raise RuntimeError(data.get("message") or "GitHub API 返回异常")
                if not isinstance(data, list):
                    raise RuntimeError("GitHub API 返回格式异常")
                releases = []
                for rel in data:
                    if not isinstance(rel, dict):
                        continue
                    assets = rel.get("assets", [])
                    if not isinstance(assets, list):
                        assets = []
                    tag = rel.get("tag_name", "")
                    if not isinstance(tag, str) or not tag:
                        continue
                    # 顺手记下本平台资源的 sha256（GitHub 的 "digest" 字段）和大小，
                    # 下载完成后据此校验：api.github.com 和实际下发文件的 CDN 不是同一台主机，
                    # 多一道独立来源的完整性检查，也能拦住静默截断的半截文件。
                    asset = next(
                        (a for a in assets if isinstance(a, dict) and a.get("name") == self.asset), None
                    )
                    digest = asset.get("digest") if asset else None
                    size = asset.get("size") if asset else None
                    releases.append(
                        {
                            "tag": tag,
                            "published": (rel.get("published_at") or "")[:10],
                            "prerelease": bool(rel.get("prerelease")),
                            "has_asset": asset is not None,
                            "digest": digest if isinstance(digest, str) else None,
                            "size": size if isinstance(size, int) and size > 0 else None,
                        }
                    )
                GLib.idle_add(callback, releases, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(callback, None, str(exc) or exc.__class__.__name__)

        threading.Thread(target=work, daemon=True).start()

    def download(
        self,
        tag: str,
        progress_cb: Callable[[str, float], None],
        done_cb: Callable[[str, Optional[str]], None],
        *,
        digest: Optional[str] = None,
        size: Optional[int] = None,
    ) -> None:
        """异步下载并安装某个版本。progress 为 0~1，未知大小时为 -1。

        :param digest: GitHub Releases API 给出的资源摘要（形如 ``sha256:<hex>``）。
            提供时下载完成后校验，不符即视为失败并丢弃文件。
        :param size: API 给出的资源大小（字节），用于在服务器不返回 Content-Length 时核对完整性。
        """
        if not _is_safe_tag(tag):
            GLib.idle_add(done_cb, tag, "非法的版本号")
            return
        if tag in self._active_downloads:
            return
        cancelled = threading.Event()
        self._active_downloads[tag] = cancelled
        self._download_stats.pop(tag, None)
        expected_sha256 = _parse_sha256_digest(digest)

        def check_cancelled() -> None:
            if cancelled.is_set():
                raise DownloadCancelled()

        def finish(error: Optional[str]) -> None:
            """注销下载状态，再通知 UI。

            顺序很重要：如果先通知 UI、再在 finally 里注销，主线程有机会在两步之间
            跑掉 ``_sync_release_rows``，此时 ``is_downloading()`` 仍为真，那一行会被
            跳过刷新，进度条永远停在最后一次进度上。
            """
            self._active_downloads.pop(tag, None)
            self._download_stats.pop(tag, None)
            GLib.idle_add(done_cb, tag, error)

        def work() -> None:
            dest_dir = self.versions_dir / tag
            final = self.path_for(tag)
            error = None
            try:
                if dest_dir.is_symlink():
                    raise RuntimeError("版本目录不能是符号链接")
                # 下载与解压都放在独占、私有且同文件系统的目录，避免跟随遗留 .part 链接。
                with tempfile.TemporaryDirectory(prefix=".install-", dir=self.versions_dir) as staging:
                    tmp = Path(staging) / (self.exe_name + ".part")
                    extracting = Path(staging) / (self.exe_name + ".extracting")
                    check_cancelled()
                    url = f"{DOWNLOAD_BASE}/{quote(tag, safe='')}/{self.asset}"
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
                        try:
                            total = int(resp.headers.get("Content-Length") or 0)
                        except (TypeError, ValueError):
                            total = 0
                        got = 0
                        hasher = hashlib.sha256()
                        started = time.monotonic()
                        last_emit = 0.0  # 上次向 UI 汇报进度的时刻
                        win_t, win_got = started, 0  # 速度采样窗口的起点
                        speed = 0.0
                        while True:
                            check_cancelled()
                            chunk = resp.read(1 << 16)
                            if not chunk:
                                break
                            fh.write(chunk)
                            hasher.update(chunk)
                            got += len(chunk)
                            now = time.monotonic()
                            if now - win_t >= 1.0:
                                # 每约 1 秒重新采样一次瞬时速度，避免数字剧烈抖动
                                speed = (got - win_got) / (now - win_t)
                                win_t, win_got = now, got
                            elif win_got == 0 and now > started:
                                speed = got / (now - started)  # 第一秒内先用平均速度
                            eta = (total - got) / speed if total and speed > 0 else -1.0
                            self._download_stats[tag] = {"got": got, "total": total, "speed": speed, "eta": eta}
                            # 最多每 100 ms 刷新一次 UI，避免大量 idle 回调拖慢主线程
                            if now - last_emit >= 0.1:
                                last_emit = now
                                GLib.idle_add(progress_cb, tag, (got / total) if total else -1.0)
                        check_cancelled()
                        # 连接被中途切断时 read() 也只是返回空串，必须自己核对字节数，
                        # 否则半截文件会被当成可用的 cloudflared 安装进去。
                        if total and got != total:
                            raise RuntimeError(f"下载不完整（{got}/{total} 字节），请重试")
                        if got == 0:
                            raise RuntimeError("下载的文件为空，请重试")
                        if size and got != size:
                            raise RuntimeError(f"文件大小与发布信息不符（{got}/{size} 字节），已丢弃")
                        if expected_sha256 and hasher.hexdigest() != expected_sha256:
                            raise RuntimeError("SHA-256 校验失败：文件与 GitHub 发布信息不符，已丢弃")
                        elapsed = max(time.monotonic() - started, 1e-6)
                        self._download_stats[tag] = {"got": got, "total": total, "speed": got / elapsed, "eta": 0.0}
                        GLib.idle_add(progress_cb, tag, 1.0 if total else -1.0)

                    staged = tmp
                    if self.asset.endswith(".tgz"):
                        with tarfile.open(tmp, "r:gz") as tf:
                            # 精确匹配 basename，不能把 not-cloudflared 当作程序。
                            member = next((m for m in tf if Path(m.name).name == "cloudflared"), None)
                            if member is None or not member.isfile():
                                raise RuntimeError(f"{self.asset} 里没有普通文件 cloudflared")
                            src = tf.extractfile(member)
                            if src is None:
                                raise RuntimeError(f"{self.asset} 里的 cloudflared 无法读取")
                            with src, open(extracting, "wb") as dst:
                                while True:
                                    check_cancelled()
                                    chunk = src.read(1 << 16)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                        staged = extracting
                    if staged.stat().st_size == 0:
                        raise RuntimeError("解压得到的 cloudflared 为空")
                    # chmod 也属于准备阶段：失败时不能破坏已经安装的旧版本。
                    if self.system != "windows":
                        staged.chmod(0o755)
                    check_cancelled()
                    if dest_dir.is_symlink():
                        raise RuntimeError("版本目录不能是符号链接")
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    staged.replace(final)
            except DownloadCancelled:
                error = "已取消"
            except Exception as exc:  # noqa: BLE001
                error = str(exc) or exc.__class__.__name__
            finally:
                # 只注销一次；通知后再 pop 会误删 done_cb 中发起的同版本重试。
                finish(error)

        threading.Thread(target=work, daemon=True).start()

    def cancel(self, tag: str) -> None:
        event = self._active_downloads.get(tag)
        if event is not None:
            event.set()

    def remove(self, tag: str) -> None:
        # 防路径穿越：tag 只能是单级目录名，否则 rmtree 会删到外面去。
        if not _is_safe_tag(tag):
            return
        shutil.rmtree(self.versions_dir / tag, ignore_errors=True)
