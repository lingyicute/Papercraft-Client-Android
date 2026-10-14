"""隧道配置模型与 cloudflared 子进程管理"""
from __future__ import annotations

import shlex
import signal
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"

STATE_LABELS = {
    STATE_STOPPED: "已停止",
    STATE_STARTING: "正在连接…",
    STATE_RUNNING: "已连接",
    STATE_ERROR: "连接失败",
}

@dataclass
class TunnelConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    hostname: str = ""
    port: int = 21128
    listen_host: str = "127.0.0.1"
    autostart: bool = False
    extra_args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TunnelConfig":
        # config.json 可能被手改坏：tunnels 里混入字符串/null/数字时 data 不是 dict，
        # 此时必须退回默认对象而不是抛异常（否则整个应用启动即崩溃、没有窗口）。
        if not isinstance(data, dict):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}  # type: ignore[attr-defined]
        cfg = cls(**known)
        # 手工编辑 / 从别处拷来的 config.json 里 id 可能缺失、为 null 或重复；
        # 非法 id 会让 UI 构造菜单时抛 TypeError（导致启动后没有窗口），
        # 重复 id 会让 get() 永远只命中第一条（编辑/删除作用到别的隧道）。
        if not isinstance(cfg.id, str) or not cfg.id.strip():
            cfg.id = uuid.uuid4().hex[:10]
        try:
            cfg.port = int(cfg.port)
        except (TypeError, ValueError, OverflowError):
            cfg.port = 21128
        if not 1 <= cfg.port <= 65535:
            cfg.port = 21128
        for str_field in ("name", "hostname", "listen_host", "extra_args"):
            value = getattr(cfg, str_field)
            if not isinstance(value, str):
                setattr(cfg, str_field, "" if value is None else str(value))
        cfg.autostart = bool(cfg.autostart)
        # 额外清理：hostname 和 listen_host 去除首尾空白，避免后续校验遗漏
        cfg.hostname = cfg.hostname.strip()
        cfg.listen_host = cfg.listen_host.strip()
        return cfg

    def copy(self) -> "TunnelConfig":
        return TunnelConfig.from_dict(self.to_dict())

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.hostname or "未命名隧道"

    # cloudflared 的 access tcp/ssh/rdp/smb 是同一个子命令（上游把后三者声明为 tcp 的
    # 别名，四个子命令共用一个 Action），--url 的 scheme 也只影响缺省值不影响行为，
    # 所以这里固定用 tcp，不必再把“协议类型”存进配置。
    def build_argv(self, binary: str) -> list[str]:
        if not self.hostname or " " in self.hostname:
            raise ValueError("无效的隧道主机名")
        argv = [
            binary,
            "access",
            "tcp",
            "--hostname",
            self.hostname,
            "--url",
            f"{self.listen_host or '127.0.0.1'}:{self.port}",
        ]
        if isinstance(self.extra_args, str) and self.extra_args.strip():
            # extra_args 里引号不配对时 shlex.split 会抛 ValueError，调用方必须捕获
            # （start() 会转为 ERROR 状态，对话框会提前拦截）。
            argv += shlex.split(self.extra_args)
        return argv

    def command_string(self, binary: str = "cloudflared") -> str:
        try:
            argv = self.build_argv(binary)
        except ValueError:
            # 复制命令行时不应崩溃：退化为把整段 extra_args 当作一个参数引用起来，
            # 用户粘贴后能看到问题所在。
            argv = [
                binary, "access", "tcp",
                "--hostname", self.hostname,
                "--url", f"{self.listen_host or '127.0.0.1'}:{self.port}",
                self.extra_args,
            ]
        return " ".join(shlex.quote(a) for a in argv)


class TunnelProcess(GObject.Object):
    """单个隧道对应的 cloudflared 子进程。"""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "log-line": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, config: TunnelConfig):
        super().__init__()
        self.config = config
        self.state = STATE_STOPPED
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._log: deque[str] = deque(maxlen=3000)
        self._proc: Optional[Gio.Subprocess] = None
        self._stream: Optional[Gio.DataInputStream] = None
        self._stopping = False
        # 进程已退出、但管道里可能还压着没读完的输出时，记下退出码，等 EOF 再收尾
        self._pending_exit: Optional[int] = None
        self._eof = False
        # 启动瞬间配置的快照：运行中编辑会替换 self.config（保存的新值），
        # 但实际监听的仍是旧端口；端口冲突检测必须用快照，否则会漏报/误报。
        self._running_snapshot: Optional[TunnelConfig] = None

    def running_config(self) -> TunnelConfig:
        """当前实际生效的配置：运行中用启动快照，否则用保存的配置。"""
        if self.is_active() and self._running_snapshot is not None:
            return self._running_snapshot
        return self.config

    # ------------------------------------------------------------ 属性
    def is_active(self) -> bool:
        return self.state in (STATE_STARTING, STATE_RUNNING)

    def log_lines(self) -> list[str]:
        return list(self._log)

    def clear_log(self) -> None:
        self._log.clear()

    def uptime(self) -> float:
        return time.monotonic() - self.started_at if self.started_at and self.is_active() else 0.0

    # ------------------------------------------------------------ 内部
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("state-changed", state)

    def _append(self, line: str) -> None:
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        self._log.append(stamped)
        self.emit("log-line", stamped)

    # ------------------------------------------------------------ 控制
    def start(self, binary: str) -> None:
        if self.is_active():
            return
        try:
            argv = self.config.build_argv(binary)
        except ValueError as exc:
            # extra_args 引号不配对等情况：不要把异常抛给 GTK 信号分发（用户无感知），
            # 而是转为 ERROR 状态并写日志，UI 会弹出 toast + 日志按钮。
            self.last_error = f"额外参数解析失败: {exc}"
            self._append(f"[启动失败] {self.last_error}")
            self._set_state(STATE_ERROR)
            return
        self.last_error = None
        self._stopping = False
        self._pending_exit = None
        self._eof = False
        self._running_snapshot = self.config.copy()
        self._set_state(STATE_STARTING)
        self._append("$ " + " ".join(shlex.quote(a) for a in argv))
        try:
            launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            )
            proc = launcher.spawnv(argv)
        except GLib.Error as exc:
            self.last_error = exc.message
            self._append(f"[启动失败] {exc.message}")
            self._running_snapshot = None
            self._set_state(STATE_ERROR)
            return

        self._proc = proc
        self.started_at = time.monotonic()
        self._stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        self._read_next(self._stream)
        proc.wait_async(None, self._on_exit)
        GLib.timeout_add(2500, self._promote_running, proc)

    def stop(self, force: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        self._append("[正在断开…]")
        try:
            if force:
                proc.force_exit()
            else:
                proc.send_signal(signal.SIGTERM)
        except Exception:  # noqa: BLE001  (非 Unix 平台)
            proc.force_exit()
        if not force:
            GLib.timeout_add_seconds(5, self._force_kill, proc)

    # ------------------------------------------------------------ 回调
    def _force_kill(self, proc: Gio.Subprocess) -> bool:
        if self._proc is proc:
            self._append("[进程未响应，强制结束]")
            proc.force_exit()
        return False

    def _promote_running(self, proc: Gio.Subprocess) -> bool:
        # 用户在这 2.5 秒里点了断开时不要再报“已连接”，否则会弹出一条反直觉的提示
        if self._proc is proc and self.state == STATE_STARTING and not self._stopping:
            self._set_state(STATE_RUNNING)
        return False

    def _read_next(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line, stream)

    def _on_line(self, stream: Gio.DataInputStream, result, _data) -> None:
        # 用字节版 read_line_finish 自己解码，而不是 read_line_finish_utf8：
        # 后者遇到任何一行非法 UTF-8 就抛 GLib.Error（G_CONVERT_ERROR_ILLEGAL_SEQUENCE），
        # 原来的代码把它当成 EOF 处理，之后再也不读管道 —— 日志窗口从那一行起永远不更新，
        # 管道（64 KiB）写满后 cloudflared 的日志写入也会被阻塞住。
        try:
            raw, _length = stream.read_line_finish(result)
        except GLib.Error:
            raw = None  # 真正的 I/O 错误（管道已关闭等）才视为 EOF
        if stream is not self._stream:
            return
        if raw is None:
            # EOF：进程退出时最后几行日志（往往就是失败原因）已经读完，可以收尾了
            self._eof = True
            if self._pending_exit is not None:
                self._finish_exit()
            return
        text = bytes(raw).decode("utf-8", errors="replace").rstrip("\r\n")
        if text:
            self._append(text)
            low = text.lower()
            if "start websocket listener" in low or "listening on" in low:
                self._set_state(STATE_RUNNING)
            # 更精确的错误检测：避免把包含 "error" 子串的普通信息误判
            if " err " in f" {low} " or "level=error" in low or low.lstrip().startswith("error"):
                self.last_error = text
        self._read_next(stream)

    def _on_exit(self, proc: Gio.Subprocess, result) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        code = proc.get_exit_status() if proc.get_if_exited() else -1
        self.started_at = None
        if self._stream is not None and not self._eof:
            # 管道里可能还压着没读完的输出，等 _on_line 读到 EOF 再改状态；
            # 万一写入端被子进程的后代长期持有，兜底定时器负责收尾。
            self._pending_exit = code
            GLib.timeout_add(2000, self._finish_exit_fallback, proc)
        else:
            self._finish_exit(code)

    def _finish_exit_fallback(self, proc: Gio.Subprocess) -> bool:
        """兜底定时器：只对“它自己那一次”进程生效。

        不带 proc 直接挂 ``_finish_exit`` 的话，用户在 2 秒内断开又重连、新进程也很快
        退出时，旧定时器会把新进程的 ``_pending_exit`` 提前消费掉，新进程最后几行日志丢失。
        """
        if self._proc is proc and self._pending_exit is not None:
            self._finish_exit()
        return False

    def _finish_exit(self, code: Optional[int] = None) -> bool:
        """进程退出后的统一收尾：EOF 回调与兜底定时器谁先到谁负责，只生效一次。"""
        if code is None:
            if self._pending_exit is None:
                return False
            code, self._pending_exit = self._pending_exit, None
        else:
            self._pending_exit = None
        self._proc = None
        self._stream = None
        self._running_snapshot = None
        self._append(f"[进程已退出，状态码 {code}]")
        if self._stopping or code == 0:
            self._set_state(STATE_STOPPED)
        else:
            self._set_state(STATE_ERROR)
        self._stopping = False
        return False


class TunnelManager(GObject.Object):
    """保存全部隧道，负责持久化与批量操作。"""

    __gsignals__ = {
        "tunnels-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.procs: list[TunnelProcess] = []
        raw_tunnels = config.get("tunnels", []) or []
        # 手改 config.json 可能把 tunnels 写成 dict/字符串/null：非 list 直接忽略，
        # 否则遍历 dict 的 key（字符串）会让 from_dict 崩溃、应用无窗口。
        if not isinstance(raw_tunnels, list):
            raw_tunnels = []
        for item in raw_tunnels:
            if not isinstance(item, dict):
                continue
            self._wrap(TunnelConfig.from_dict(item))

    def _wrap(self, cfg: TunnelConfig) -> TunnelProcess:
        self._ensure_unique_id(cfg)
        proc = TunnelProcess(cfg)
        proc.connect("state-changed", lambda p, st: self.emit("state-changed", p.config.id, st))
        self.procs.append(proc)
        return proc

    def _ensure_unique_id(self, cfg: TunnelConfig) -> TunnelConfig:
        """保证 id 是非空且唯一的字符串。

        手工编辑（或从别处拷贝）config.json 时很容易出现 id 缺失 / 为 null / 两条重复，
        那会让 get() 永远只命中第一条——在 UI 上删除/编辑其中一行，实际操作的是另一条。
        """
        taken = {p.config.id for p in self.procs}
        if not isinstance(cfg.id, str) or not cfg.id.strip() or cfg.id in taken:
            cfg.id = uuid.uuid4().hex[:10]
        return cfg

    def save(self) -> None:
        self.config.set("tunnels", [p.config.to_dict() for p in self.procs])
        self.config.save()

    # ------------------------------------------------------------ CRUD
    def get(self, tunnel_id: str) -> Optional[TunnelProcess]:
        return next((p for p in self.procs if p.config.id == tunnel_id), None)

    def add(self, cfg: TunnelConfig) -> TunnelProcess:
        proc = self._wrap(cfg)
        self.save()
        self.emit("tunnels-changed")
        return proc

    def update(self, cfg: TunnelConfig) -> None:
        proc = self.get(cfg.id)
        if proc is None:
            self.add(cfg)
            return
        proc.config = cfg
        self.save()
        self.emit("tunnels-changed")

    def remove(self, tunnel_id: str) -> None:
        proc = self.get(tunnel_id)
        if proc is None:
            return
        proc.stop()
        self.procs.remove(proc)
        self.save()
        self.emit("tunnels-changed")

    def duplicate(self, tunnel_id: str) -> Optional[TunnelProcess]:
        proc = self.get(tunnel_id)
        if proc is None:
            return None
        cfg = proc.config.copy()
        cfg.id = uuid.uuid4().hex[:10]
        cfg.name = f"{proc.config.display_name} (副本)"
        cfg.autostart = False
        return self.add(cfg)

    # ------------------------------------------------------------ 批量
    def active(self) -> list[TunnelProcess]:
        return [p for p in self.procs if p.is_active()]

    def port_conflict(self, cfg: TunnelConfig) -> Optional[TunnelProcess]:
        def _norm_host(host: object) -> str:
            h = host if isinstance(host, str) else ""
            h = (h or "127.0.0.1").strip().lower() or "127.0.0.1"
            # localhost / ::1 通常解析到 127.0.0.1，视为同一地址，便于冲突检测
            if h in ("localhost", "::1"):
                h = "127.0.0.1"
            return h

        want_host, want_port = _norm_host(cfg.listen_host), cfg.port
        for p in self.active():
            if p.config.id == cfg.id:
                continue
            # 用启动快照而非当前保存值：运行中编辑改了端口后，实际占用的仍是旧端口
            running = p.running_config()
            if running.port != want_port:
                continue
            have_host = _norm_host(running.listen_host)
            # 0.0.0.0 / :: 绑定所有地址，与任何具体地址都冲突
            any_addrs = ("0.0.0.0", "::", "0:0:0:0:0:0:0:0")
            if have_host in any_addrs or want_host in any_addrs or have_host == want_host:
                return p
        return None

    def stop_all(self, force: bool = False) -> None:
        for p in self.procs:
            p.stop(force=force)
