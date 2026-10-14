"""剪贴板快速隧道

窗口获得焦点时读取剪贴板，如果内容是一条 ``*.trycloudflare.com`` 快速隧道链接，
就弹窗询问是否连接；用户点“是”后直接打开新建隧道对话框，主机名与名称都已填好，
只需要补一个本地端口号。
"""
from __future__ import annotations

import re
import sys
import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

QUICK_TUNNEL_DOMAIN = "trycloudflare.com"

# 剪贴板内容必须“整体就是”这条链接（允许协议头、端口、路径 / 查询 / 锚点，以及一层
# 包裹的引号/尖括号）。刻意不做“在一大段文本里搜链接”：复制一整段聊天记录时弹出一个
# 确认框只会招人烦。结尾用 $ 收口，所以 x.trycloudflare.com.evil.com 这类后缀伪装不会命中。
_QUICK_TUNNEL_RE = re.compile(
    r"""^
    (?:https?://)?                                        # 可选协议头
    (?P<host>(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+       # 至少一个子域标签
               trycloudflare\.com)
    (?::\d+)?                                             # 可选端口
    (?:[/?#][^\s]*)?                                      # 可选路径 / 查询 / 锚点
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 从聊天窗口复制出来的链接常被包一层括号或引号
_WRAP_PAIRS = (
    ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    ("<", ">"), ("(", ")"), ("[", "]"), ("【", "】"),
)


def _unwrap(text: str) -> str:
    """剥掉一层包裹的引号 / 尖括号。"""
    for left, right in _WRAP_PAIRS:
        if len(text) > len(left) + len(right) and text.startswith(left) and text.endswith(right):
            return text[len(left):-len(right)].strip()
    return text


def parse_quick_tunnel_url(text: Optional[str]) -> Optional[str]:
    """剪贴板文本里是一条快速隧道链接时返回小写主机名，否则返回 ``None``。

    >>> parse_quick_tunnel_url("https://Foo-Bar.trycloudflare.com/")
    'foo-bar.trycloudflare.com'
    >>> parse_quick_tunnel_url("https://x.trycloudflare.com?a=1#section")
    'x.trycloudflare.com'
    >>> parse_quick_tunnel_url("https://x.trycloudflare.com#section")
    'x.trycloudflare.com'
    >>> parse_quick_tunnel_url("trycloudflare.com") is None
    True
    >>> parse_quick_tunnel_url("https://x.trycloudflare.com.evil.com") is None
    True
    """
    if not text:
        return None
    match = _QUICK_TUNNEL_RE.match(_unwrap(text.strip()))
    if match is None:
        return None
    host = match.group("host").lower()
    # 正则已经要求至少一个子域标签，这里再兜一层：光秃秃的 trycloudflare.com
    # 是官网域名，不是某条快速隧道，不该拿去建隧道。
    if not host.endswith("." + QUICK_TUNNEL_DOMAIN):
        return None
    return host


def quick_tunnel_name(when: Optional[float] = None) -> str:
    """快速隧道的默认名称：``yyyymmdd hh:mm:ss 快速隧道``。"""
    return time.strftime("%Y%m%d %H:%M:%S", time.localtime(when)) + " 快速隧道"


class QuickTunnelWatcher:
    """窗口获得焦点时检测剪贴板里的快速隧道链接。

    - ``Gtk.EventControllerFocus`` 挂在窗口上时，只有“整个窗口”拿到 / 失去键盘焦点才
      会触发 enter / leave；窗口内部控件之间切焦点不会触发，所以不会反复弹窗。
    - 读剪贴板延迟 :data:`CLIPBOARD_DELAY_MS`：Wayland 下窗口刚拿到焦点的瞬间
      compositor 可能还没把剪贴板授权给本 surface，立刻读会失败；顺带把快速
      alt-tab 抖动合并掉。
    - 同一条链接一个会话只问一次：点了“否”之后再切回窗口不会被反复打扰。
      已经配置过相同主机名隧道的链接直接跳过（快速隧道的子域随机且唯一）。
    """

    #: 获得焦点后延迟多久读剪贴板（毫秒）
    CLIPBOARD_DELAY_MS = 150

    def __init__(
        self,
        window: Gtk.Window,
        on_accept: Callable[[str], None],
        *,
        is_enabled: Optional[Callable[[], bool]] = None,
        known_hostnames: Optional[Callable[[], set]] = None,
    ):
        self.window = window
        self.on_accept = on_accept
        self._is_enabled = is_enabled if is_enabled is not None else (lambda: True)
        self._known_hostnames = known_hostnames if known_hostnames is not None else (lambda: set())
        self._handled: set[str] = set()
        self._pending_id: int = 0
        self._read_generation = 0
        self._read_cancel: Optional[Gio.Cancellable] = None
        self._dialog: Optional[Adw.MessageDialog] = None
        # forget() 之后、窗口真正失去过一次焦点之前，不要读剪贴板（见 forget 的说明）
        self._suppress_until_leave = False

        # window=None 是留给无显示环境单元测试的缝：只构造判定状态机，不接 GTK。
        self._controller: Optional[Gtk.EventControllerFocus] = None
        if window is not None:
            self._controller = Gtk.EventControllerFocus()
            self._controller.connect("enter", self._on_focus_enter)
            self._controller.connect("leave", self._on_focus_leave)
            window.add_controller(self._controller)

    def forget(self, host: str) -> None:
        """把一条链接重新标记为“未处理”，允许它再次触发询问。

        用户在预填好的新建对话框里点了“取消”（而非“保存”）时调用：链接并没有被
        真正建出来，不应被 :meth:`consider` 的“一个会话只问一次”吞掉，下次切回
        窗口还应当再问一遍。

        “下次切回窗口”指的是用户主动切回来。预填对话框是模态的，它一关闭，窗口管理器
        会立刻把焦点还给主窗口，这会触发一次 enter —— 如果把这次 enter 也当成“切回
        窗口”，剪贴板里还是那条链接，确认框就会在用户点“取消”的一瞬间再次弹出来，
        形成“是 → 取消 → 是否连接？→ 是 → 取消 → …”的死循环，只能靠点“否”逃出去。
        所以这里同时置一个标志：在主窗口真正失去过一次焦点（leave）之前，enter 不读剪贴板。
        """
        self.cancel_pending()
        self._handled.discard(host)
        self._suppress_until_leave = True

    # ------------------------------------------------------------ 焦点
    def _on_focus_enter(self, *_args) -> None:
        self.cancel_pending()
        if self._suppress_until_leave:
            # 这是对话框关闭后 WM 自动还回来的焦点，不是用户切回窗口
            return
        if not self._is_enabled():
            return
        self._pending_id = GLib.timeout_add(self.CLIPBOARD_DELAY_MS, self._read_clipboard)

    def _on_focus_leave(self, *_args) -> None:
        # 焦点还没等够延迟就又走了（alt-tab 掠过）：这次不读剪贴板
        self.cancel_pending()
        # 用户确实离开了窗口：下一次 enter 才算“切回窗口”
        self._suppress_until_leave = False

    def cancel_pending(self) -> None:
        self._read_generation += 1
        if self._read_cancel is not None:
            self._read_cancel.cancel()
            self._read_cancel = None
        if self._pending_id:
            GLib.source_remove(self._pending_id)
            self._pending_id = 0

    # ------------------------------------------------------------ 剪贴板
    def _read_clipboard(self) -> bool:
        self._pending_id = 0
        self.cancel_pending()
        if not self._is_enabled() or not self._window_alive():
            return False
        try:
            display = self.window.get_display() or Gdk.Display.get_default()
            self._read_cancel = Gio.Cancellable()
            display.get_clipboard().read_text_async(
                self._read_cancel, self._on_clipboard_text, self._read_generation,
            )
        except Exception:  # noqa: BLE001
            # 无显示环境 / 窗口正在销毁：这个功能是锦上添花，不该影响主流程
            pass
        return False

    def _on_clipboard_text(self, clipboard, result, generation: int) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            # 剪贴板里没有文本（例如复制的是图片或文件），属正常情况
            return
        except Exception:  # noqa: BLE001
            return
        # 完成已发出的异步请求，但丢弃旧焦点、取消/关闭或更新一次读取之前的结果。
        if generation != self._read_generation:
            return
        self._read_cancel = None
        self.consider(text)

    def _window_alive(self) -> bool:
        try:
            return self.window.get_visible() and self.window.get_display() is not None
        except Exception:  # noqa: BLE001  (窗口已被销毁)
            return False

    # ------------------------------------------------------------ 判定
    def consider(self, text: Optional[str]) -> Optional[str]:
        """处理一段剪贴板文本；需要询问时弹出确认框，并返回命中的主机名。

        单独留一个方法，是为了不依赖显示环境就能测试判定逻辑。
        """
        host = parse_quick_tunnel_url(text)
        if host is None or not self._is_enabled() or host in self._handled:
            return None
        if self._dialog is not None:
            # 已经有一个确认框开着：这次不要把链接消耗掉。否则它会被记进 _handled
            # 却从来没被问过，本会话里再也弹不出来了——等下次获得焦点再问。
            return None
        if not self._window_alive():
            return None
        self._handled.add(host)
        if host in self._known_hostnames():
            # 这条隧道已经建过了（快速隧道的子域是随机且唯一的），不再打扰
            return None
        self._ask(host)
        return host

    # ------------------------------------------------------------ 询问
    def _ask(self, host: str) -> None:
        if self._dialog is not None:
            # consider() 已经拦掉了这种情况，这里只是兜底：绝不叠第二个确认框
            self._dialog.present()
            return
        url = f"https://{host}"
        intro = "在剪贴板中发现了一个 Cloudflare 快速隧道链接："
        # 链接放进 body：Adw.MessageDialog 的 body 本身就是居中 + 按字符自动换行的。
        # 不用 extra_child——那块区域宽度受限（默认字体下约 288px），塞一条 40 字符的
        # 等宽链接要么被裁掉，要么逼出一串 "Trying to measure GtkLabel ..." 警告。
        # body-use-markup 从 libadwaita 1.3 起才有；没有就退回纯文本，别在弹窗时崩掉。
        use_markup = hasattr(Adw.MessageDialog, "set_body_use_markup")
        body = (
            f"{intro}\n\n<tt>{GLib.markup_escape_text(url)}</tt>" if use_markup else f"{intro}\n\n{url}"
        )
        dlg = Adw.MessageDialog(
            transient_for=self.window, modal=True,
            heading="是否要连接这个快速隧道？", body=body,
        )
        if use_markup:
            dlg.set_body_use_markup(True)
        dlg.add_response("cancel", "否")
        dlg.add_response("accept", "是")
        dlg.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("accept")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, response: self._on_response(host, response))
        self._dialog = dlg
        dlg.present()

    def _on_response(self, host: str, response: str) -> None:
        # Adw.MessageDialog 没有 closed 信号：点按钮和 Esc / 关闭按钮都会先发 response
        # （close_response 已设成 cancel）再自行关闭，所以在这里收尾就够了。
        self._dialog = None
        if response != "accept":
            return
        # 确认框此刻正在自行关闭，等下一个主循环迭代再开新建对话框，
        # 免得两个模态窗口的关闭 / 打开撞在一起。
        GLib.idle_add(self._open_editor, host)

    def _open_editor(self, host: str) -> bool:
        """确认框关闭后的下一轮主循环里打开新建对话框（返回 False 表示只执行一次）。"""
        if not self._window_alive():
            return False
        try:
            self.on_accept(host)
        except Exception as exc:  # noqa: BLE001
            # 锦上添花的功能，任何意外都不该打断主循环
            print(f"[quick-tunnel] 打开新建对话框失败: {exc}", file=sys.stderr)
        return False
