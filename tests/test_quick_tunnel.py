#!/usr/bin/env python3
"""快速隧道链接识别 / 默认命名的纯逻辑测试。

不需要显示环境，直接跑：

    python3 tests/test_quick_tunnel.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cftm.quick_tunnel import (  # noqa: E402
    QuickTunnelWatcher,
    parse_quick_tunnel_url,
    quick_tunnel_name,
)

# (剪贴板内容, 应当识别出的主机名)
SHOULD_MATCH = [
    ("foo-bar.trycloudflare.com", "foo-bar.trycloudflare.com"),
    ("  https://Foo-Bar.trycloudflare.com  ", "foo-bar.trycloudflare.com"),
    ("https://x.trycloudflare.com/", "x.trycloudflare.com"),
    ("https://x.trycloudflare.com/some/path?a=1&b=2", "x.trycloudflare.com"),
    ("http://a.trycloudflare.com", "a.trycloudflare.com"),
    ("https://x.trycloudflare.com:8443", "x.trycloudflare.com"),
    ("<https://x.trycloudflare.com>", "x.trycloudflare.com"),
    ("'https://x.trycloudflare.com'", "x.trycloudflare.com"),
    ("“https://x.trycloudflare.com”", "x.trycloudflare.com"),
    ("(https://x.trycloudflare.com)", "x.trycloudflare.com"),
    ("a1.trycloudflare.com\n", "a1.trycloudflare.com"),
    ("https://x.trycloudflare.com#section", "x.trycloudflare.com"),          # 裸锚点
    ("https://x.trycloudflare.com?a=1", "x.trycloudflare.com"),              # 裸查询串
    ("https://x.trycloudflare.com/p?a=1#f", "x.trycloudflare.com"),          # 路径+查询+锚点
    ("https://x.trycloudflare.com:8443/h?a=1#f", "x.trycloudflare.com"),     # 端口+全套后缀
]

# 这些一律不该弹窗
SHOULD_REJECT = [
    None,
    "",
    "   ",
    "hello world",
    "trycloudflare.com",
    "https://trycloudflare.com",
    "https://x.trycloudflare.com.evil.com",      # 后缀伪装：真正的域名是 evil.com
    "evil.com/x.trycloudflare.com",
    "visit https://x.trycloudflare.com now",     # 一大段文本里不搜链接
    "https://x.example.com",
    "ssh://x.trycloudflare.com",
    "https://.trycloudflare.com",
    "https://x.trycloudflare.com and https://y.trycloudflare.com",
    "file:///home/user/x.trycloudflare.com",
    "https://-x.trycloudflare.com",
    "https://x.trycloudflare.com.evil.com#frag",   # 锚点不能成为后缀伪装的绕过
]


def make_watcher(*, enabled: bool = True, known=()):
    """构造一个不依赖显示环境的 watcher（window=None），记录 _ask / on_accept。"""
    asked: list[str] = []
    accepted: list[str] = []
    watcher = QuickTunnelWatcher(
        None,
        accepted.append,
        is_enabled=(lambda: enabled),
        known_hostnames=(lambda: set(known)),
    )
    # 不走真正的弹窗 / 窗口存活检查，只记录“准备询问哪个主机名”
    watcher._window_alive = lambda: True
    watcher._ask = asked.append
    return watcher, asked, accepted


# QuickTunnelWatcher.consider() 状态机用例：(检查函数, 说明)
def watcher_checks():
    checks = []

    # M3：已配置过相同主机名的隧道 -> 直接跳过，不弹确认框
    w, asked, _ = make_watcher(known={"known-xyz.trycloudflare.com"})
    got = w.consider("https://known-xyz.trycloudflare.com")
    checks.append((got is None and not asked, "已建过相同主机名的链接不再询问"))

    # 未建过的新链接 -> 弹出询问并返回主机名
    w, asked, _ = make_watcher()
    got = w.consider("https://brand-new.trycloudflare.com/path#frag")
    checks.append((got == "brand-new.trycloudflare.com" and asked == ["brand-new.trycloudflare.com"],
                   "新链接正常询问"))

    # 同一条链接一个会话只问一次
    again = w.consider("https://brand-new.trycloudflare.com")
    checks.append((again is None and len(asked) == 1, "同一条链接不重复询问"))

    # 确认框开着时，第二条链接不能被“已处理”集合吞掉
    w._dialog = object()
    second = w.consider("https://second-one.trycloudflare.com")
    checks.append((second is None and "second-one.trycloudflare.com" not in w._handled,
                   "确认框开着时不消耗第二条链接"))
    w._dialog = None

    # M2：forget() 之后，同一条链接允许再次询问（预填对话框被取消的场景）
    w.forget("brand-new.trycloudflare.com")
    reasked = w.consider("https://brand-new.trycloudflare.com")
    checks.append((reasked == "brand-new.trycloudflare.com" and asked.count("brand-new.trycloudflare.com") == 2,
                   "forget 后同一条链接可再次询问"))

    # M2b：forget() 之后紧跟着的那次 enter 是 WM 把焦点还给主窗口，不读剪贴板；
    # 主窗口真正 leave 过一次之后，下一次 enter 才重新读
    w, asked, _ = make_watcher()
    w.forget("brand-new.trycloudflare.com")
    w._on_focus_enter()
    checks.append((w._pending_id == 0, "forget 后紧接的 enter 不读剪贴板（避免取消后立刻再问）"))
    w._on_focus_leave()
    w._on_focus_enter()
    checks.append((w._pending_id != 0, "leave 过一次之后的 enter 恢复读剪贴板"))
    w.cancel_pending()
    # 没有 forget 时 enter 应当照常读剪贴板
    w, asked, _ = make_watcher()
    w._on_focus_enter()
    checks.append((w._pending_id != 0, "普通 enter 照常读剪贴板"))
    w.cancel_pending()

    # 功能开关关闭 -> 什么都不做
    w, asked, _ = make_watcher(enabled=False)
    got = w.consider("https://disabled-abc.trycloudflare.com")
    checks.append((got is None and not asked, "开关关闭时不询问"))

    # 普通文本 -> 不询问
    w, asked, _ = make_watcher()
    got = w.consider("这只是一段普通文本，不是链接")
    checks.append((got is None and not asked, "普通文本不询问"))

    return checks


def main() -> int:
    failures = 0
    for text, want in SHOULD_MATCH:
        got = parse_quick_tunnel_url(text)
        if got != want:
            failures += 1
            print(f"FAIL  match  {text!r} -> {got!r} (期望 {want!r})")
    for text in SHOULD_REJECT:
        got = parse_quick_tunnel_url(text)
        if got is not None:
            failures += 1
            print(f"FAIL  reject {text!r} -> {got!r} (期望 None)")

    # 名称格式：yyyymmdd hh:mm:ss 快速隧道
    when = time.mktime(time.strptime("20260915 09:08:07", "%Y%m%d %H:%M:%S"))
    name = quick_tunnel_name(when)
    if name != "20260915 09:08:07 快速隧道":
        failures += 1
        print(f"FAIL  name   -> {name!r}")
    if not quick_tunnel_name().endswith(" 快速隧道"):
        failures += 1
        print("FAIL  name   -> 不传时间戳时应当用当前时间")

    watcher_results = watcher_checks()
    for ok, label in watcher_results:
        if not ok:
            failures += 1
            print(f"FAIL  watcher {label}")

    total = len(SHOULD_MATCH) + len(SHOULD_REJECT) + 2 + len(watcher_results)
    print(f"test_quick_tunnel: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
