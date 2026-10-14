"""“版本管理”页面：当前二进制、已安装版本、GitHub 发布列表"""
from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .binary_manager import BinaryManager, format_eta, format_size, format_speed, version_key  # noqa: E402


class ReleaseRow(Adw.ActionRow):
    def __init__(self, page: "VersionsPage", release: dict):
        super().__init__(use_markup=False)
        self.tag = release["tag"]
        self.bm: BinaryManager = page.bm
        self.set_title(self.tag)
        parts = [release["published"] or ""]
        if release["prerelease"]:
            parts.append("预发布")
        if not release["has_asset"]:
            parts.append(f"无 {page.bm.asset} 资源")
        self.set_subtitle(" · ".join(p for p in parts if p))

        self.stack = Gtk.Stack(valign=Gtk.Align.CENTER, hhomogeneous=False)
        dl_btn = Gtk.Button(
            icon_name="folder-download-symbolic",
            tooltip_text="下载并安装此版本",
            css_classes=["flat"],
            sensitive=release["has_asset"],
        )
        dl_btn.connect("clicked", lambda *_: page.download(self.tag))

        prog_box = Gtk.Box(spacing=6)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=200, show_text=True)
        cancel_btn = Gtk.Button(icon_name="process-stop-symbolic", css_classes=["flat"], tooltip_text="取消")
        cancel_btn.connect("clicked", lambda *_: page.bm.cancel(self.tag))
        prog_box.append(self.progress)
        prog_box.append(cancel_btn)

        done_box = Gtk.Box(spacing=4)
        done_box.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
        done_box.append(Gtk.Label(label="已安装"))
        done_box.add_css_class("success")

        self.stack.add_named(dl_btn, "idle")
        self.stack.add_named(prog_box, "progress")
        self.stack.add_named(done_box, "done")
        self.add_suffix(self.stack)

    def set_installed(self, installed: bool) -> None:
        self.stack.set_visible_child_name("done" if installed else "idle")

    def set_progress(self, fraction: float) -> None:
        self.stack.set_visible_child_name("progress")
        stats = self.bm.download_stats(self.tag) or {}
        got = stats.get("got", 0)
        total = stats.get("total", 0)
        speed = stats.get("speed", 0.0)
        eta = stats.get("eta", -1.0)
        speed_text = format_speed(speed) if speed > 0 else ""
        if fraction < 0:
            # 服务器未返回 Content-Length：无法算百分比，改为显示已下载量
            self.progress.pulse()
            head = format_size(got) if got else "下载中…"
        else:
            clamped = max(0.0, min(1.0, fraction))
            self.progress.set_fraction(clamped)
            head = f"{clamped * 100:.0f}%"
        self.progress.set_text(f"{head} · {speed_text}" if speed_text else head)
        # 悬停提示：已下载 / 总大小 · 速度 · 剩余时间
        tips = []
        if got:
            tips.append(f"已下载 {format_size(got)} / {format_size(total)}" if total else f"已下载 {format_size(got)}")
        if speed_text:
            tips.append(f"速度 {speed_text}")
        if eta >= 0 and 0 <= fraction < 1:
            tips.append(format_eta(eta))
        self.progress.set_tooltip_text(" · ".join(tips) if tips else None)


class VersionsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.bm: BinaryManager = win.bm
        self.config = win.config
        self._installed_rows: list[Gtk.Widget] = []
        self._release_widgets: list[Gtk.Widget] = []
        self._release_rows: dict[str, ReleaseRow] = {}
        self._releases: dict[str, dict] = {}  # tag -> fetch_releases 返回的条目（含 digest/size）
        self._radio_leader: Optional[Gtk.CheckButton] = None
        self._suppress_select = False
        self.latest_tag: Optional[str] = None
        self._install_after_fetch = False

        # ---------- 当前使用
        self.g_current = Adw.PreferencesGroup(
            title="当前使用的 cloudflared",
            description=f"平台 {self.bm.system}/{self.bm.arch} · 资源名 {self.bm.asset}",
        )
        self.current_row = Adw.ActionRow(use_markup=False, title="检测中…")
        self.current_icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        self.current_row.add_prefix(self.current_icon)
        refresh_cur = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
            tooltip_text="重新检测",
        )
        refresh_cur.connect("clicked", lambda *_: self.win.on_binary_changed())
        self.current_row.add_suffix(refresh_cur)
        self.g_current.add(self.current_row)
        self.add(self.g_current)

        # ---------- 已安装
        self.g_installed = Adw.PreferencesGroup(
            title="版本选择", description="决定启动隧道时使用哪个 cloudflared 二进制文件。"
        )
        open_btn = Gtk.Button(
            icon_name="folder-open-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
            tooltip_text="打开安装目录",
        )
        open_btn.connect("clicked", self._open_dir)
        self.g_installed.set_header_suffix(open_btn)
        self.add(self.g_installed)

        # ---------- GitHub
        self.g_releases = Adw.PreferencesGroup(
            title="GitHub 发布版本", description="来源：cloudflare/cloudflared 官方仓库"
        )
        hbox = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.latest_btn = Gtk.Button(label="安装最新版", css_classes=["suggested-action"])
        self.latest_btn.connect("clicked", lambda *_: self.install_latest())
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], tooltip_text="刷新列表")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh_releases())
        self.spinner = Gtk.Spinner(visible=False)
        hbox.append(self.spinner)
        hbox.append(self.latest_btn)
        hbox.append(self.refresh_btn)
        self.g_releases.set_header_suffix(hbox)
        self.add(self.g_releases)
        self._set_release_placeholder("点击右侧刷新按钮获取版本列表", "network-transmit-receive-symbolic")

        self._rebuild_installed()

    # ------------------------------------------------------------ 当前
    def set_current(self, version: Optional[str], path: Optional[str]) -> None:
        if path:
            self.current_row.set_title(f"cloudflared {version or '(版本未知)'}")
            self.current_row.set_subtitle(path)
            self.current_icon.set_from_icon_name("object-select-symbolic")
            self.current_row.remove_css_class("error")
        else:
            self.current_row.set_title("未找到可用的 cloudflared")
            self.current_row.set_subtitle("请在下方安装一个版本，或将 cloudflared 放入 PATH")
            self.current_icon.set_from_icon_name("dialog-error-symbolic")
            self.current_row.add_css_class("error")

    # ------------------------------------------------------------ 已安装
    def _rebuild_installed(self) -> None:
        for row in self._installed_rows:
            self.g_installed.remove(row)
        self._installed_rows.clear()
        self._radio_leader = None

        active = self.config.get("active_version", "latest-installed")
        installed = self.bm.installed()
        sysbin = self.bm.system_binary()
        # 手改配置 / 外部删除目录后，active 可能指向不存在的版本或非法类型，
        # 此时没有任何单选被选中、界面很迷惑：静默纠正为自动模式。
        # 注意：system 只有在 PATH 中真的存在时才算有效，否则应回退到自动
        valid_keys = {"latest-installed", *installed}
        if sysbin:
            valid_keys.add("system")
        if not isinstance(active, str) or active not in valid_keys:
            active = "latest-installed"
            self.config.set("active_version", active)
            self.config.save()

        self._suppress_select = True
        self._add_choice(
            "latest-installed",
            "自动：最新已安装版本",
            f"当前解析为 {installed[0]}" if installed else "尚未安装任何版本，将回退到系统 PATH",
            active == "latest-installed",
        )
        self._add_choice(
            "system", "系统 PATH 中的 cloudflared", sysbin or "未在 PATH 中找到",
            active == "system", sensitive=bool(sysbin),
        )
        for tag in installed:
            self._add_choice(tag, tag, str(self.bm.path_for(tag)), active == tag, deletable=True)
        self._suppress_select = False

    def _add_choice(self, key: str, title: str, subtitle: str, checked: bool,
                    sensitive: bool = True, deletable: bool = False) -> None:
        row = Adw.ActionRow(use_markup=False)
        row.set_title(title)
        row.set_subtitle(subtitle)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=sensitive)
        if self._radio_leader is None:
            self._radio_leader = check
        else:
            check.set_group(self._radio_leader)
        check.set_active(checked)
        check.connect("toggled", lambda c: c.get_active() and self._select(key))
        row.add_prefix(check)
        row.set_activatable_widget(check)
        if deletable:
            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                tooltip_text="删除此版本",
            )
            del_btn.connect("clicked", lambda *_: self._confirm_delete(key))
            row.add_suffix(del_btn)
        self.g_installed.add(row)
        self._installed_rows.append(row)

    def _select(self, key: str) -> None:
        if self._suppress_select or self.config.get("active_version") == key:
            return
        self.config.set("active_version", key)
        self.config.save()
        self.win.toast("已切换 cloudflared 版本策略，新启动的隧道将使用新版本")
        self.win.on_binary_changed()

    def _confirm_delete(self, tag: str) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.win, heading=f"删除 cloudflared {tag}？",
            body="正在使用此版本运行的隧道不会被中断，但之后将无法再用它启动。",
        )
        dlg.add_response("cancel", "取消")
        dlg.add_response("delete", "删除")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_d, resp):
            if resp != "delete":
                return
            self.bm.remove(tag)
            if self.config.get("active_version") == tag:
                self.config.set("active_version", "latest-installed")
                self.config.save()
            self._rebuild_installed()
            self._sync_release_rows()
            self.win.on_binary_changed()
            self.win.toast(f"已删除 cloudflared {tag}")

        dlg.connect("response", on_response)
        dlg.present()

    def _open_dir(self, *_args) -> None:
        # 确保目录存在，Flatpak 环境下可能尚未创建
        try:
            self.bm.versions_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        uri = Gio.File.new_for_path(str(self.bm.versions_dir)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.win.toast(f"无法打开目录: {exc.message}")
            # 兜底：尝试用 xdg-open
            try:
                import subprocess
                subprocess.Popen(["xdg-open", str(self.bm.versions_dir)])
            except Exception:
                pass

    # ------------------------------------------------------------ 发布列表
    def _clear_release_widgets(self) -> None:
        for w in self._release_widgets:
            self.g_releases.remove(w)
        self._release_widgets.clear()
        self._release_rows.clear()

    def _set_release_placeholder(self, text: str, icon: str) -> None:
        self._clear_release_widgets()
        row = Adw.ActionRow(use_markup=False)
        row.set_title(text)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        self.g_releases.add(row)
        self._release_widgets.append(row)

    def refresh_releases(self) -> None:
        self.refresh_btn.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.bm.fetch_releases(self._on_releases)

    def _on_releases(self, releases: Optional[list], error: Optional[str]) -> None:
        try:
            self.refresh_btn.set_sensitive(True)
            self.spinner.stop()
            self.spinner.set_visible(False)
        except Exception:
            pass
        if error or not releases:
            try:
                self._set_release_placeholder(f"获取失败：{error or '返回为空'}", "dialog-error-symbolic")
                self.win.toast("获取 GitHub 版本列表失败")
            except Exception:
                pass
            self._install_after_fetch = False
            return
        self._clear_release_widgets()
        self._releases = {r["tag"]: r for r in releases}
        self.latest_tag = next(
            (r["tag"] for r in releases if r["has_asset"] and not r["prerelease"]), None
        )
        for rel in releases:
            row = ReleaseRow(self, rel)
            self.g_releases.add(row)
            self._release_widgets.append(row)
            self._release_rows[rel["tag"]] = row
        self._sync_release_rows()
        if self.latest_tag:
            self.g_releases.set_description(f"来源：cloudflare/cloudflared 官方仓库 · 最新稳定版 {self.latest_tag}")
            installed = self.bm.installed()
            # 只有“已安装的最新版确实旧于线上稳定版”才提示：装了更新的预发布版时
            # 不应误报“有新版本”。
            if installed and version_key(installed[0]) < version_key(self.latest_tag):
                self.win.toast(f"有新版本可用：{self.latest_tag}（已安装 {installed[0]}）")
        else:
            self.g_releases.set_description("来源：cloudflare/cloudflared 官方仓库")
        if self._install_after_fetch:
            self._install_after_fetch = False
            # 当前平台没有任何带资源的稳定版时不要再触发一次刷新，
            # 否则 install_latest → refresh → _on_releases → install_latest 无限循环。
            if self.latest_tag:
                self.install_latest()
            else:
                self.win.toast("没有找到适用于当前平台的稳定版本")

    def _sync_release_rows(self) -> None:
        for tag, row in self._release_rows.items():
            if not self.bm.is_downloading(tag):
                row.set_installed(self.bm.is_installed(tag))

    def install_latest(self) -> None:
        if self.latest_tag:
            if self.bm.is_installed(self.latest_tag):
                self.win.toast(f"最新版 {self.latest_tag} 已安装")
            elif self.bm.is_downloading(self.latest_tag):
                self.win.toast(f"正在下载 {self.latest_tag}…")
            else:
                self.download(self.latest_tag)
        else:
            # 列表正在刷新中时不要重复触发 fetch（按钮已禁用即为刷新中）
            if not self.refresh_btn.get_sensitive():
                self._install_after_fetch = True
                return
            self._install_after_fetch = True
            self.refresh_releases()

    def download(self, tag: str) -> None:
        if self.bm.is_installed(tag) or self.bm.is_downloading(tag):
            return
        row = self._release_rows.get(tag)
        if row:
            row.set_progress(0.0)
        rel = self._releases.get(tag, {})
        self.bm.download(
            tag, self._on_progress, self._on_done,
            digest=rel.get("digest"), size=rel.get("size"),
        )

    def _on_progress(self, tag: str, fraction: float) -> None:
        row = self._release_rows.get(tag)
        if row:
            row.set_progress(fraction)

    def _on_done(self, tag: str, error: Optional[str]) -> None:
        if error:
            if error == "已取消":
                self.win.toast(f"已取消安装 {tag}")
            else:
                self.win.toast(f"安装 {tag} 失败：{error}")
        else:
            self.win.toast(f"cloudflared {tag} 安装完成")
        self._rebuild_installed()
        self._sync_release_rows()
        self.win.on_binary_changed()
