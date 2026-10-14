"""应用与主窗口"""
from __future__ import annotations

import signal
import sys
import threading
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__  # noqa: E402
from .binary_manager import BinaryManager  # noqa: E402
from .config import (  # noqa: E402
    APP_ICON_FILE,
    APP_ID,
    APP_NAME,
    DATA_DIR,
    PROJECT_URL,
    RESOURCE_DATA_DIR,
    Config,
)
from .dialogs import LogWindow, TunnelDialog, copy_text  # noqa: E402
from .quick_tunnel import QuickTunnelWatcher, quick_tunnel_name  # noqa: E402
from .tunnel import (  # noqa: E402
    STATE_ERROR,
    STATE_LABELS,
    STATE_RUNNING,
    TunnelConfig,
    TunnelManager,
    TunnelProcess,
)
from .versions_page import VersionsPage  # noqa: E402

CSS = """
.status-dot {
  min-width: 10px; min-height: 10px; border-radius: 999px;
  background: alpha(currentColor, 0.25);
  margin-right: 4px;
}
.status-dot.running { background: #2ec27e; box-shadow: 0 0 8px alpha(#2ec27e, 0.7); }
.status-dot.starting { background: #f5c211; box-shadow: 0 0 8px alpha(#f5c211, 0.6); }
.status-dot.error { background: #e01b24; box-shadow: 0 0 8px alpha(#e01b24, 0.6); }
.state-chip {
  padding: 2px 8px; border-radius: 999px; font-size: 0.8em; font-weight: 600;
  background: alpha(currentColor, 0.08);
}
.state-chip.running { color: #2ec27e; background: alpha(#2ec27e, 0.15); }
.state-chip.starting { color: #c68a00; background: alpha(#f5c211, 0.2); }
.state-chip.error { color: #e01b24; background: alpha(#e01b24, 0.15); }
.log-view { padding: 10px; font-size: 0.92em; }
.hero-title { font-weight: 800; font-size: 1.35em; }
"""


class TunnelRow(Adw.ActionRow):
    def __init__(self, proc: TunnelProcess, win: "MainWindow"):
        super().__init__(activatable=False, use_markup=False)
        self.proc = proc
        self.win = win
        tid = proc.config.id

        self.dot = Gtk.Box(css_classes=["status-dot"], valign=Gtk.Align.CENTER)
        self.add_prefix(self.dot)

        self.chip = Gtk.Label(css_classes=["state-chip"], valign=Gtk.Align.CENTER)
        self.toggle_btn = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["circular"])
        self.toggle_btn.connect("clicked", self._on_toggle)
        log_btn = Gtk.Button(
            icon_name="utilities-terminal-symbolic", valign=Gtk.Align.CENTER,
            css_classes=["flat"], tooltip_text="查看日志",
        )
        log_btn.connect("clicked", lambda *_: win.show_logs(tid))

        menu = Gio.Menu()
        for label, action in (
            ("编辑", "win.tunnel-edit"),
            ("复制为副本", "win.tunnel-duplicate"),
            ("复制命令行", "win.tunnel-copy"),
        ):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, GLib.Variant.new_string(tid))
            menu.append_item(item)
        danger = Gio.Menu()
        item = Gio.MenuItem.new("删除", None)
        item.set_action_and_target_value("win.tunnel-delete", GLib.Variant.new_string(tid))
        danger.append_item(item)
        menu.append_section(None, danger)
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
            css_classes=["flat"], menu_model=menu,
        )

        box = Gtk.Box(spacing=6)
        for w in (self.chip, self.toggle_btn, log_btn, menu_btn):
            box.append(w)
        self.add_suffix(box)

        self._hid = proc.connect("state-changed", lambda *_: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        cfg = self.proc.config
        # PreferencesRow 默认开启 markup；构造时显式关闭后才能原样显示用户输入。
        self.set_title(cfg.display_name)
        subtitle = f"{cfg.hostname}  →  {cfg.listen_host}:{cfg.port}"
        if cfg.autostart:
            subtitle += "  ·  自动连接"
        self.set_subtitle(subtitle)

        state = self.proc.state
        for css in ("running", "starting", "error"):
            self.dot.remove_css_class(css)
            self.chip.remove_css_class(css)
        if state in ("running", "starting", "error"):
            self.dot.add_css_class(state)
            self.chip.add_css_class(state)
        self.chip.set_label(STATE_LABELS.get(state, state))

        active = self.proc.is_active()
        self.toggle_btn.set_icon_name(
            "media-playback-stop-symbolic" if active else "media-playback-start-symbolic"
        )
        self.toggle_btn.set_tooltip_text("断开" if active else "连接")
        if active:
            self.toggle_btn.remove_css_class("suggested-action")
            self.toggle_btn.add_css_class("destructive-action")
        else:
            self.toggle_btn.remove_css_class("destructive-action")
            self.toggle_btn.add_css_class("suggested-action")

    def _on_toggle(self, *_args) -> None:
        if self.proc.is_active():
            self.win.stop_tunnel(self.proc.config.id)
        else:
            self.win.start_tunnel(self.proc.config.id)

    def dispose_row(self) -> None:
        self.proc.disconnect(self._hid)


def _window_size(raw) -> tuple[int, int]:
    """读取配置里的窗口尺寸。

    config.json 是用户可以手改的，width/height 写成字符串、null 或越界值时
    必须退回默认值：这里的异常会让 MainWindow.__init__ 中断，程序变成
    “进程在跑、窗口从未出现”，而且日志里只有一行 traceback。
    """
    if not isinstance(raw, dict):
        raw = {}

    def pick(key: str, fallback: int) -> int:
        try:
            value = int(raw.get(key, fallback))
        except (TypeError, ValueError, OverflowError):
            # OverflowError：config 里写了 Infinity（json 允许解析为 inf），
            # int(inf) 会抛 OverflowError 而非 ValueError，必须接住。
            return fallback
        return min(max(value, 640), 4096)

    return pick("width", 980), pick("height", 660)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "Application"):
        super().__init__(application=app, title=APP_NAME)
        self.config: Config = app.config
        self.bm: BinaryManager = app.bm
        self.manager: TunnelManager = app.manager
        self._rows: list[TunnelRow] = []
        self._log_windows: dict[str, LogWindow] = {}
        self._force_close = False
        self._binary_path: Optional[str] = None

        width, height = _window_size(self.config.get("window"))
        self.set_default_size(width, height)
        self.set_size_request(640, 460)

        self._build_ui()
        self._setup_actions()
        self.manager.connect("tunnels-changed", lambda *_: self._rebuild_list())
        self.manager.connect("state-changed", self._on_state_changed)
        self.connect("close-request", self._on_close_request)
        self.quick_tunnel = QuickTunnelWatcher(
            self,
            on_accept=self._quick_tunnel_accept,
            is_enabled=self._quick_tunnel_enabled,
            known_hostnames=self._quick_tunnel_hostnames,
        )

        self._rebuild_list()
        self.on_binary_changed()
        GLib.timeout_add(700, self._autostart)

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        view = Adw.ToolbarView()
        self.toasts.set_child(view)

        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="新建隧道 (Ctrl+N)")
        add_btn.set_action_name("win.new-tunnel")
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("全部启动", "win.start-all")
        menu.append("全部停止", "win.stop-all")
        menu.append("剪贴板快速连接", "win.clipboard-quick-tunnel")
        section = Gio.Menu()
        section.append("关于", "app.about")
        section.append("退出", "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="主菜单"))
        view.add_top_bar(header)
        view.set_content(self.stack)

        self.stack.add_titled_with_icon(self._build_tunnels_page(), "tunnels", "隧道", "network-workgroup-symbolic")
        self.versions_page = VersionsPage(self)
        self.stack.add_titled_with_icon(self.versions_page, "versions", "版本管理", "software-update-available-symbolic")

    def _build_tunnels_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner(title="未找到可用的 cloudflared 二进制文件，隧道无法启动", button_label="前往安装")
        self.banner.connect("button-clicked", lambda *_: self.stack.set_visible_child_name("versions"))
        outer.append(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer.append(scroller)
        clamp = Adw.Clamp(
            maximum_size=880, tightening_threshold=620,
            margin_top=20, margin_bottom=28, margin_start=18, margin_end=18,
        )
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(box)

        toolbar = Gtk.Box(spacing=8)
        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.summary = Gtk.Label(xalign=0, css_classes=["hero-title"])
        self.binary_label = Gtk.Label(
            xalign=0, css_classes=["dim-label", "caption"],
            ellipsize=Pango.EllipsizeMode.END,
        )
        label_box.append(self.summary)
        label_box.append(self.binary_label)
        start_all = Gtk.Button(label="全部启动", css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
        start_all.set_action_name("win.start-all")
        stop_all = Gtk.Button(label="全部停止", valign=Gtk.Align.CENTER)
        stop_all.set_action_name("win.stop-all")
        toolbar.append(label_box)
        toolbar.append(start_all)
        toolbar.append(stop_all)
        box.append(toolbar)

        self.list_stack = Gtk.Stack(vhomogeneous=False)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        empty = Adw.StatusPage(
            icon_name="network-workgroup-symbolic",
            title="还没有隧道",
            description="添加一个 Cloudflare Tunnel 连接，即可一键将远程 SSH / RDP / 任意 TCP 服务映射到本地端口。",
        )
        empty_btn = Gtk.Button(label="新建隧道", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        empty_btn.set_action_name("win.new-tunnel")
        empty.set_child(empty_btn)
        self.list_stack.add_named(self.listbox, "list")
        self.list_stack.add_named(empty, "empty")
        box.append(self.list_stack)
        return outer

    # ------------------------------------------------------------ 动作
    def _add_action(self, name: str, callback, param: Optional[str] = None) -> None:
        action = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
        action.connect("activate", callback)
        self.add_action(action)

    def _add_toggle_action(self, name: str, state: bool, callback) -> None:
        """开关型动作：菜单里会显示成一个勾选项。"""
        action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(bool(state)))
        action.connect("change-state", callback)
        self.add_action(action)

    def _setup_actions(self) -> None:
        self._add_action("new-tunnel", lambda *_: self._open_editor(None))
        self._add_toggle_action(
            "clipboard-quick-tunnel", self._quick_tunnel_enabled(), self._on_toggle_quick_tunnel
        )
        self._add_action("start-all", lambda *_: self.start_all())
        self._add_action("stop-all", lambda *_: self.stop_all())
        self._add_action("tunnel-edit", lambda _a, p: self._edit(p.get_string()), "s")
        self._add_action("tunnel-delete", lambda _a, p: self._confirm_delete(p.get_string()), "s")
        self._add_action("tunnel-duplicate", lambda _a, p: self.manager.duplicate(p.get_string()), "s")
        self._add_action("tunnel-copy", lambda _a, p: self._copy_command(p.get_string()), "s")
        self._add_action("tunnel-logs", lambda _a, p: self.show_logs(p.get_string()), "s")

    # ------------------------------------------------------------ 列表
    def _prune_log_windows(self) -> None:
        """关掉那些隧道已经不存在的日志窗口。

        否则删除一条正在看日志的隧道后，窗口会一直停在屏幕上：它指向的进程已经
        被移出管理器，既不会再更新也关不掉（再点日志按钮只会把同一个窗口叫回来）。
        """
        alive = {p.config.id for p in self.manager.procs}
        for tunnel_id in [t for t in self._log_windows if t not in alive]:
            win = self._log_windows.pop(tunnel_id)
            win.close()

    def _rebuild_list(self) -> None:
        self._prune_log_windows()
        for row in self._rows:
            row.dispose_row()
            try:
                self.listbox.remove(row)
            except Exception:
                # GTK4 中 remove 可能在行已 unparent 时抛异常，兜底尝试 unparent
                try:
                    row.unparent()
                except Exception:
                    pass
        self._rows.clear()
        for proc in self.manager.procs:
            row = TunnelRow(proc, self)
            self.listbox.append(row)
            self._rows.append(row)
        self.list_stack.set_visible_child_name("list" if self._rows else "empty")
        self._update_summary()

    def _update_summary(self) -> None:
        running = len(self.manager.active())
        total = len(self.manager.procs)
        self.summary.set_label(f"{running} / {total} 个隧道运行中" if total else "隧道")

    def _on_state_changed(self, _mgr, tunnel_id: str, state: str) -> None:
        self._update_summary()
        proc = self.manager.get(tunnel_id)
        if proc is None:
            return
        name = proc.config.display_name
        if state == STATE_ERROR:
            detail = proc.last_error or "请查看日志了解详情"
            toast = Adw.Toast.new(f"{name} 连接失败：{detail[:80]}")
            toast.set_button_label("日志")
            toast.set_action_name("win.tunnel-logs")
            toast.set_action_target_value(GLib.Variant.new_string(tunnel_id))
            toast.set_timeout(6)
            self.toasts.add_toast(toast)
        elif state == STATE_RUNNING:
            self.toast(f"{name} 已连接：{proc.config.listen_host}:{proc.config.port}")

    # ------------------------------------------------------------ 隧道操作
    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def current_binary(self) -> Optional[str]:
        return self.bm.resolve(self.config.get("active_version", "latest-installed"))

    def start_tunnel(self, tunnel_id: str) -> bool:
        proc = self.manager.get(tunnel_id)
        if proc is None or proc.is_active():
            return False
        # 对话框里拦过了，但手改 config.json 可能留下空 hostname 或含空格：这里再兜底，
        # 否则会拿 --hostname "" 或含空格的值去起进程，cloudflared 报错信息很迷惑。
        hostname = proc.config.hostname.strip()
        if not hostname or " " in hostname:
            self.toast(f"「{proc.config.display_name}」的隧道主机名无效，请先编辑修正")
            return False
        binary = self.current_binary()
        if not binary:
            self.toast("未找到 cloudflared，请先在“版本管理”中安装")
            self.stack.set_visible_child_name("versions")
            return False
        conflict = self.manager.port_conflict(proc.config)
        if conflict:
            self.toast(f"端口 {proc.config.port} 已被「{conflict.config.display_name}」占用")
            return False
        proc.start(binary)
        return True

    def stop_tunnel(self, tunnel_id: str) -> None:
        proc = self.manager.get(tunnel_id)
        if proc:
            proc.stop()

    def start_all(self) -> None:
        if not self.manager.procs:
            return
        # 二进制缺失时只提示一次就返回：否则循环里每个 start_tunnel 都会重复
        # 弹“未找到 cloudflared”并跳页，N 条隧道就弹 N 次。
        if not self.current_binary():
            self.toast("未找到 cloudflared，请先在“版本管理”中安装")
            self.stack.set_visible_child_name("versions")
            return
        started = 0
        already = 0
        for proc in self.manager.procs:
            if proc.is_active():
                already += 1
            elif self.start_tunnel(proc.config.id):
                started += 1
        if started == 0:
            if already == len(self.manager.procs):
                self.toast("所有隧道已在运行中")
            else:
                self.toast("没有可启动的隧道")

    def stop_all(self) -> None:
        self.manager.stop_all()

    def _autostart(self) -> bool:
        targets = [p for p in self.manager.procs if p.config.autostart and not p.is_active()]
        if not targets or not self.current_binary():
            return False
        # start_tunnel 可能被端口冲突挡下来，按实际启动数汇报
        started = 0
        for proc in targets:
            try:
                if self.start_tunnel(proc.config.id):
                    started += 1
            except Exception:
                # 单个隧道启动失败不应影响其他
                continue
        if started:
            self.toast(f"已自动连接 {started} 个隧道")
        return False

    # ------------------------------------------------------------ 剪贴板快速连接
    def _quick_tunnel_enabled(self) -> bool:
        return bool(self.config.get("clipboard_quick_tunnel", True))

    def _quick_tunnel_hostnames(self) -> set:
        """已配置过的隧道主机名：快速隧道的子域是随机且唯一的，命中就说明建过了。"""
        return {
            p.config.hostname.strip().lower()
            for p in self.manager.procs
            if p.config.hostname.strip()
        }

    def _on_toggle_quick_tunnel(self, action, value: GLib.Variant) -> None:
        action.set_state(value)
        enabled = value.get_boolean()
        if not enabled:
            self.quick_tunnel.cancel_pending()
        self.config.set("clipboard_quick_tunnel", enabled)
        self.config.save()
        self.toast(
            "已开启：窗口获得焦点时检测剪贴板中的快速隧道链接"
            if enabled
            else "已关闭剪贴板快速连接"
        )

    def _quick_tunnel_accept(self, hostname: str) -> None:
        """确认要连接剪贴板里的快速隧道：打开新建对话框，用户只需要补一个端口号。"""
        self.stack.set_visible_child_name("tunnels")
        cfg = TunnelConfig(hostname=hostname, name=quick_tunnel_name())
        saved = False

        def on_save(new_cfg: TunnelConfig) -> None:
            nonlocal saved
            saved = True
            self._on_saved(new_cfg)

        dialog = TunnelDialog(self, cfg, on_save, is_new=True, focus_port=True)

        def on_close_request(*_args) -> bool:
            if not saved:
                # 用户在预填对话框里点了“取消” / 按了 Esc：链接并没有被建出来，
                # 把它移出 watcher 的“已处理”集合，下次切回窗口还会再问一次。
                self.quick_tunnel.forget(hostname)
            return False

        dialog.connect("close-request", on_close_request)
        dialog.present()

    # ------------------------------------------------------------ 编辑 / 删除
    def _open_editor(self, cfg: Optional[TunnelConfig]) -> None:
        TunnelDialog(self, cfg, self._on_saved).present()

    def _edit(self, tunnel_id: str) -> None:
        proc = self.manager.get(tunnel_id)
        if proc:
            self._open_editor(proc.config.copy())

    def _on_saved(self, cfg: TunnelConfig) -> None:
        existing = self.manager.get(cfg.id)
        if existing:
            was_active = existing.is_active()
            self.manager.update(cfg)
            if was_active:
                self.toast("配置已保存，将在下次连接时生效")
        else:
            self.manager.add(cfg)
            self.toast(f"已添加隧道「{cfg.display_name}」")

    def _confirm_delete(self, tunnel_id: str) -> None:
        proc = self.manager.get(tunnel_id)
        if proc is None:
            return
        dlg = Adw.MessageDialog(
            transient_for=self, heading=f"删除「{proc.config.display_name}」？",
            body="如果该隧道正在运行，将会被立即断开。此操作不可撤销。",
        )
        dlg.add_response("cancel", "取消")
        dlg.add_response("delete", "删除")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "delete" and self.manager.remove(tunnel_id))
        dlg.present()

    def _copy_command(self, tunnel_id: str) -> None:
        proc = self.manager.get(tunnel_id)
        if proc:
            copy_text(self, proc.config.command_string(self.current_binary() or "cloudflared"))
            self.toast("命令行已复制到剪贴板")

    def show_logs(self, tunnel_id: str) -> None:
        win = self._log_windows.get(tunnel_id)
        if win:
            win.present()
            return
        proc = self.manager.get(tunnel_id)
        if proc is None:
            return
        win = LogWindow(self, proc)
        self._log_windows[tunnel_id] = win
        win.connect("close-request", lambda *_: self._on_log_window_closed(tunnel_id))
        win.present()

    def _on_log_window_closed(self, tunnel_id: str) -> bool:
        self._log_windows.pop(tunnel_id, None)
        return False  # 不拦截关闭

    # ------------------------------------------------------------ 二进制
    def on_binary_changed(self) -> None:
        path = self.current_binary()
        self._binary_path = path
        self.banner.set_revealed(path is None)
        if path is None:
            self.binary_label.set_label("cloudflared：未安装")
            self.versions_page.set_current(None, None)
            return
        self.binary_label.set_label(f"cloudflared：检测中… ({path})")

        def probe() -> None:
            version = self.bm.probe_version(path)
            GLib.idle_add(done, version)

        def done(version: Optional[str]) -> bool:
            try:
                # 窗口可能已在探测期间被销毁，避免访问已销毁的控件
                if self._binary_path != path:
                    return False
                # 检查窗口是否仍有效
                if not self.get_root():
                    return False
                self.binary_label.set_label(f"cloudflared {version or '(未知版本)'} · {path}")
                self.versions_page.set_current(version, path)
            except Exception:
                pass
            return False

        threading.Thread(target=probe, daemon=True).start()

    # ------------------------------------------------------------ 关闭
    def _save_window_size(self) -> None:
        try:
            w = self.get_width()
            h = self.get_height()
            # 窗口未实现或最小化时可能返回 0，过滤掉
            if w > 0 and h > 0:
                self.config.set("window", {"width": w, "height": h})
                self.config.save()
        except Exception:
            # 获取尺寸失败时不保存，避免把 0 写入配置导致下次启动窗口过小
            pass

    def _on_close_request(self, *_args) -> bool:
        self.quick_tunnel.cancel_pending()
        running = len(self.manager.active())
        if running == 0 or self._force_close:
            self._save_window_size()
            self.manager.stop_all()
            return False
        dlg = Adw.MessageDialog(
            transient_for=self, heading="退出并断开所有隧道？",
            body=f"仍有 {running} 个隧道处于连接状态，退出后将全部断开。",
        )
        dlg.add_response("cancel", "取消")
        dlg.add_response("quit", "断开并退出")
        dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_d, resp):
            if resp == "quit":
                self._force_close = True
                self.close()

        dlg.connect("response", on_response)
        dlg.present()
        return True


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.config = Config()
        self.bm = BinaryManager(DATA_DIR / "versions")
        self.manager = TunnelManager(self.config)

    def do_startup(self) -> None:
        # 让 X11 下的 WM_CLASS / Wayland 下的 app_id 与 .desktop 文件名一致，
        # 否则任务栏无法把窗口和应用（及其图标）对应起来；必须在 gtk_init 之前设置
        GLib.set_prgname(APP_ID)
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode("utf-8"))
        _display = Gdk.Display.get_default()
        if _display is not None:
            Gtk.StyleContext.add_provider_for_display(
                _display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self._register_app_icon()

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit)
        self.add_action(quit_action)
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("win.new-tunnel", ["<primary>n"])

        # SIGTERM / SIGHUP（注销、`kill`、`flatpak kill`、关掉启动它的终端）默认会让 Python
        # 立刻死掉，do_shutdown 跑不到，已经起来的 cloudflared 全部变成孤儿进程继续占着端口。
        # 转成正常退出：quit() → 主循环结束 → do_shutdown → stop_all(force=True)。
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._on_unix_signal, signum)
            except (AttributeError, ValueError):  # 非 Unix 平台 / 不支持的信号
                pass

    def _register_app_icon(self) -> None:
        # 非 Flatpak、且未安装到系统时，把 SVG 图标与 .desktop 文件放到 ~/.local/share，
        # 这样 GNOME Shell / KDE Plasma 等才能通过 app_id / WM_CLASS 找到任务栏图标
        from .desktop_integration import ensure_desktop_integration

        ensure_desktop_integration()
        # 下面让 GTK 自身也能找到图标：窗口图标、关于对话框、X11 下的 _NET_WM_ICON
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        if APP_ICON_FILE.is_file():
            data_dir = str(RESOURCE_DATA_DIR)
            if data_dir not in (theme.get_search_path() or []):
                theme.add_search_path(data_dir)
        if theme.has_icon(APP_ID):
            Gtk.Window.set_default_icon_name(APP_ID)

    def main_window(self) -> Optional["MainWindow"]:
        """主窗口（按类型查找，而不是依赖 ``props.active_window`` 恰好是它）。"""
        return next((w for w in self.get_windows() if isinstance(w, MainWindow)), None)

    def do_activate(self) -> None:
        win = self.main_window() or MainWindow(self)
        win.present()

    def _on_unix_signal(self, signum: int) -> bool:
        print(f"[app] 收到信号 {signal.Signals(signum).name}，正在退出并断开所有隧道", file=sys.stderr)
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        # 退出时强制结束所有隧道，避免 SIGTERM 后 5 秒强制杀的定时器因主循环已退出而不执行
        self.manager.stop_all(force=True)
        Adw.Application.do_shutdown(self)

    def _on_quit(self, *_args) -> None:
        win = self.main_window()
        if win:
            win.close()  # 走 close-request，仍有隧道在跑时会先确认
        else:
            self.quit()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutWindow(
            transient_for=self.main_window(),
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            developer_name="lingyicute",
            comments="通过 Cloudflare Tunnel (cloudflared access) 管理多条任意 TCP 隧道连接，并内置 cloudflared 版本管理。",
            website=PROJECT_URL,
            issue_url=PROJECT_URL + "/issues",
            license_type=Gtk.License.AGPL_3_0,
        )
        about.present()


def main(argv: Optional[list[str]] = None) -> int:
    return Application().run(argv if argv is not None else sys.argv)
