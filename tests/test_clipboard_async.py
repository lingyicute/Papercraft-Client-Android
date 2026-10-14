"""Delayed clipboard results must not outlive their focus/request generation."""
import unittest
from unittest.mock import Mock

from cftm.quick_tunnel import QuickTunnelWatcher


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = Mock()
        self.window = Mock()
        self.window.get_display.return_value.get_clipboard.return_value = self.clipboard
        self.watcher = QuickTunnelWatcher(None, lambda *_: None)
        self.watcher.window = self.window
        self.watcher._window_alive = lambda: True
        self.watcher._ask = Mock()
        self.clipboard.read_text_finish.return_value = 'https://late.trycloudflare.com'
        self.addCleanup(self.watcher.cancel_pending)

    def begin(self):
        self.watcher._read_clipboard()
        args = self.clipboard.read_text_async.call_args.args
        return lambda: args[1](self.clipboard, None, *args[2:])

    def test_result_after_focus_leave_is_ignored(self):
        complete = self.begin()
        self.watcher._on_focus_leave()
        complete()
        self.watcher._ask.assert_not_called()
        self.assertNotIn('late.trycloudflare.com', self.watcher._handled)

    def test_previous_read_cannot_win_new_focus(self):
        old = self.begin()
        self.watcher._on_focus_leave()
        new = self.begin()
        old()
        self.watcher._ask.assert_not_called()
        new()
        self.watcher._ask.assert_called_once_with('late.trycloudflare.com')

    def test_forget_invalidates_inflight_read(self):
        complete = self.begin()
        self.watcher.forget('late.trycloudflare.com')
        complete()
        self.watcher._ask.assert_not_called()

    def test_invisible_window_does_not_consume_link(self):
        self.watcher._window_alive = lambda: False
        self.watcher.consider('https://late.trycloudflare.com')
        self.assertNotIn('late.trycloudflare.com', self.watcher._handled)
