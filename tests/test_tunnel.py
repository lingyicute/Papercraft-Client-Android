"""Tunnel model and real Gio subprocess lifecycle regressions."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib
from cftm.config import Config
from cftm.tunnel import STATE_ERROR, STATE_RUNNING, STATE_STARTING, STATE_STOPPED, TunnelConfig, TunnelManager, TunnelProcess


def wait_until(predicate, timeout=4):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = TunnelManager(Config(Path(self.tmp.name) / 'config.json'))
        self.procs = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for proc in self.procs:
            proc.stop(force=True)
        wait_until(lambda: all(not p.is_active() for p in self.procs))

    def script(self, body):
        path = Path(self.tmp.name) / 'fake-cloudflared'
        path.write_text('#!/usr/bin/python3\n' + body)
        path.chmod(0o755)
        return str(path)

    def proc(self):
        proc = self.manager.add(TunnelConfig(hostname='tcp.example.com'))
        self.procs.append(proc)
        return proc

    def test_ipv6_listener(self):
        for host in ('::1', '[::1]', '0:0:0:0:0:0:0:1'):
            cfg = TunnelConfig(hostname='tcp.example.com', listen_host=host, port=2222)
            with self.subTest(host=host):
                self.assertEqual(cfg.build_argv('cloudflared')[-1], f'[{host.strip("[]")}]:2222')
                self.assertIn(f'[{host.strip("[]")}]:2222', cfg.command_string())

    def test_invalid_arguments_are_rejected_before_spawn(self):
        for field, value in [('hostname', 'tcp\texample.com'), ('hostname', 'tcp\x00.example.com'),
                             ('listen_host', 'http://localhost'), ('listen_host', '[::1'),
                             ('listen_host', 'localhost:8080'), ('extra_args', '--token a\x00b')]:
            cfg = TunnelConfig(hostname='tcp.example.com')
            setattr(cfg, field, value)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    cfg.build_argv('cloudflared')
                proc = TunnelProcess(cfg)
                with patch('cftm.tunnel.Gio.SubprocessLauncher.new') as launcher:
                    proc.start('cloudflared')
                launcher.assert_not_called()
                self.assertEqual(proc.state, STATE_ERROR)

    def test_deleted_process_is_still_killed_at_shutdown(self):
        proc = self.proc()
        proc.start(self.script('import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint("READY", flush=True)\ntime.sleep(60)\n'))
        self.assertTrue(wait_until(lambda: any('READY' in line for line in proc.log_lines())))
        child = proc._proc
        pid = int(child.get_identifier())
        self.manager.remove(proc.config.id)
        self.assertIsNone(self.manager.get(proc.config.id))
        self.manager.stop_all(force=True)
        self.assertTrue(wait_until(lambda: not proc.is_active(), timeout=1.5))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.manager._retiring)

    def test_retiring_process_keeps_port_reserved(self):
        proc = self.proc()
        proc.start(self.script('import time\nprint("READY", flush=True)\ntime.sleep(60)\n'))
        self.assertTrue(wait_until(lambda: any('READY' in line for line in proc.log_lines())))
        self.manager.remove(proc.config.id)
        self.assertIs(self.manager.port_conflict(TunnelConfig(hostname='other.example.com')), proc)
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertFalse(self.manager._retiring)

    def test_bracketed_ipv6_conflict(self):
        proc = self.proc()
        proc.config.listen_host = '::1'
        proc.state = STATE_RUNNING
        self.assertIs(self.manager.port_conflict(TunnelConfig(listen_host='[::1]')), proc)
        proc.state = STATE_STOPPED

    def test_invalid_utf8_does_not_stop_log_reader(self):
        proc = self.proc()
        proc.start(self.script('import os\nos.write(1, b"bad\\xff\\nlast error\\n")\nraise SystemExit(7)\n'))
        self.assertTrue(wait_until(lambda: proc.state == STATE_ERROR))
        self.assertTrue(any('bad\ufffd' in line for line in proc.log_lines()))
        self.assertTrue(any('last error' in line for line in proc.log_lines()))

    def test_stale_promotion_does_not_change_new_process(self):
        proc = self.proc()
        old, new = object(), object()
        proc._proc, proc.state = new, STATE_STARTING
        proc._promote_running(old)
        self.assertEqual(proc.state, STATE_STARTING)
        proc._proc, proc.state = None, STATE_STOPPED

    def test_listener_log_after_stop_does_not_promote(self):
        proc = self.proc()
        proc.state, proc._stopping = STATE_STARTING, True
        stream = Mock()
        stream.read_bytes_finish.return_value = GLib.Bytes.new(b'listening on 127.0.0.1:2222\n')
        proc._stream = stream
        with patch.object(proc, '_read_next'):
            proc._on_read(stream, None, None)
        self.assertEqual(proc.state, STATE_STARTING)
        proc.state, proc._stream = STATE_STOPPED, None

    def test_eof_finishes_without_busy_reading_or_fallback(self):
        proc = self.proc()
        with patch.object(proc, '_read_next', wraps=proc._read_next) as read:
            proc.start(self.script('import os\nos.write(1, b"first\\n\\nlast")\n'))
            self.assertTrue(wait_until(lambda: not proc.is_active(), timeout=1))
            self.assertLess(read.call_count, 10)
        self.assertTrue(any(line.endswith('first') for line in proc.log_lines()))
        self.assertTrue(any(line.endswith('last') for line in proc.log_lines()))

    def test_long_unterminated_log_line_is_bounded(self):
        proc = self.proc()
        proc.start(self.script('import os\nos.write(1, b"x" * (1024 * 1024) + b"\\nlast\\n")\n'))
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertLess(max(map(len, proc.log_lines())), 70000)
        self.assertTrue(any(line.endswith('last') for line in proc.log_lines()))

    def test_utf8_split_across_reads_is_preserved(self):
        proc = self.proc()
        proc.start(self.script('import os,time\nos.write(1, b"\\xe4")\ntime.sleep(0.1)\nos.write(1, b"\\xb8\\xad\\n")\n'))
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertTrue(any(line.endswith('中') for line in proc.log_lines()))

    def test_old_read_does_not_contaminate_new_process(self):
        proc = self.proc()
        stream = Mock()
        stream.read_bytes_finish.return_value = GLib.Bytes.new(b'old process\n')
        proc._stream = Mock()
        proc._line_buffer.extend(b'new process')
        proc._on_read(stream, None, None)
        self.assertEqual(proc._line_buffer, b'new process')
        self.assertEqual(proc.log_lines(), [])
        proc._stream = None

    def test_exited_process_is_not_promoted_while_draining_logs(self):
        proc = self.proc()
        proc._proc = object()
        proc.state = STATE_STARTING
        proc._pending_exit = 1
        proc._promote_running(proc._proc)
        self.assertEqual(proc.state, STATE_STARTING)
        proc._proc, proc.state = None, STATE_STOPPED
