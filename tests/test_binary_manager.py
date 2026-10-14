"""Offline binary selection and installation tests (real files, mocked HTTP)."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cftm.binary_manager import BinaryManager, version_key


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class Response(io.BytesIO):
    def __init__(self, data, length=None, on_eof=None):
        super().__init__(data)
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.on_eof = on_eof

    def read(self, size=-1):
        data = super().read(size)
        if not data and self.on_eof:
            self.on_eof()
        return data


class BinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bm = BinaryManager(Path(self.tmp.name) / 'versions')
        self.bm.system, self.bm.arch, self.bm.asset = 'linux', 'amd64', 'cloudflared-linux-amd64'
        self.tag = '2026.1.0'

    def install_old(self, content=b'old'):
        final = self.bm.path_for(self.tag)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(content)
        final.chmod(0o755)
        return final

    def download(self, response, **kwargs):
        done = []
        with patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', return_value=response), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.download(self.tag, lambda *_: None, lambda *a: done.append(a), **kwargs)
        self.assertEqual(len(done), 1)
        self.assertFalse(self.bm.is_downloading(self.tag))
        self.assertIsNone(self.bm.download_stats(self.tag))
        self.assertFalse(list(self.bm.versions_dir.rglob('*.part')))
        self.assertFalse(list(self.bm.versions_dir.rglob('*.extracting')))
        self.assertFalse(list(self.bm.versions_dir.glob('.install-*')))
        return done[0][1]

    def test_stable_beats_numbered_prerelease(self):
        tags = ['2026.1.0-rc.2', '2026.1.0', '2026.1.0-beta.10', '2026.1.0-rc.10']
        self.assertEqual(sorted(tags, key=version_key),
                         ['2026.1.0-beta.10', '2026.1.0-rc.2', '2026.1.0-rc.10', '2026.1.0'])
        self.assertEqual(version_key('v2026.1.0+build.9'), version_key('2026.1.0'))
        self.assertLess(version_key('2026.1.0-1'), version_key('2026.1.0'))
        self.assertLess(version_key('2026.1.0'), version_key('2026.2.0-rc.1'))

    def test_foreign_binary_and_directory_are_not_installed(self):
        final = self.bm.path_for(self.tag)
        final.parent.mkdir()
        final.with_suffix('.exe').write_bytes(b'PE')
        self.assertFalse(self.bm.is_installed(self.tag))
        final.mkdir()
        self.assertEqual(self.bm.installed(), [])
        with patch.object(self.bm, 'system_binary', return_value='/system/cloudflared'):
            self.assertEqual(self.bm.resolve('latest-installed'), '/system/cloudflared')

    def test_non_executable_is_not_installed(self):
        final = self.install_old()
        final.chmod(0o644)
        self.assertFalse(self.bm.is_installed(self.tag))

    def test_success_with_digest_and_size(self):
        data = b'new binary'
        self.assertIsNone(self.download(Response(data), size=len(data),
                                       digest='sha256:' + hashlib.sha256(data).hexdigest()))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), data)
        self.assertEqual(self.bm.installed(), [self.tag])

    def test_integrity_failures_preserve_old(self):
        final = self.install_old()
        for response, kwargs in [(Response(b'new', 20), {}), (Response(b''), {}),
                                 (Response(b'new'), {'size': 20}),
                                 (Response(b'new'), {'digest': 'sha256:' + '0' * 64})]:
            with self.subTest(kwargs=kwargs):
                self.assertIsNotNone(self.download(response, **kwargs))
                self.assertEqual(final.read_bytes(), b'old')

    def test_cancel_during_eof_does_not_publish(self):
        final = self.install_old()
        error = self.download(Response(b'new', on_eof=lambda: self.bm.cancel(self.tag)))
        self.assertEqual(error, '已取消')
        self.assertEqual(final.read_bytes(), b'old')

    def test_chmod_failure_preserves_old(self):
        final = self.install_old()
        with patch.object(Path, 'chmod', side_effect=OSError('permission denied')):
            self.assertIsNotNone(self.download(Response(b'new')))
        self.assertEqual(final.read_bytes(), b'old')

    def archive(self, name='cloudflared', kind=tarfile.REGTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = 'elsewhere' if kind != tarfile.REGTYPE else ''
            member.size = 3 if kind == tarfile.REGTYPE else 0
            tf.addfile(member, io.BytesIO(b'new') if member.size else None)
        self.bm.system, self.bm.asset = 'darwin', 'cloudflared-darwin-amd64.tgz'
        return Response(data.getvalue())

    def test_tgz_success(self):
        self.assertIsNone(self.download(self.archive('./cloudflared')))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), b'new')

    def test_tgz_rejects_suffix_match_and_links(self):
        final = self.install_old()
        for name, kind in [('not-cloudflared', tarfile.REGTYPE),
                           ('cloudflared', tarfile.SYMTYPE), ('cloudflared', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                final = self.install_old()
                self.assertIsNotNone(self.download(self.archive(name, kind)))
                self.assertEqual(final.read_bytes(), b'old')

    def test_symlink_version_directory_is_not_written(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        self.bm.path_for(self.tag).parent.symlink_to(outside, target_is_directory=True)
        self.assertIsNotNone(self.download(Response(b'new')))
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_tag_never_opens_network(self):
        self.tag = '../outside'
        with patch('cftm.binary_manager.urllib.request.urlopen') as request, \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            done = []
            self.bm.download(self.tag, lambda *_: None, lambda *a: done.append(a))
        request.assert_not_called()
        self.assertEqual(len(done), 1)
        self.assertIsNotNone(done[0][1])

    def test_cancel_during_extraction_preserves_old(self):
        final = self.install_old()
        response = self.archive()
        original = tarfile.TarFile.extractfile

        def extract(tf, member):
            self.bm.cancel(self.tag)
            return original(tf, member)

        with patch.object(tarfile.TarFile, 'extractfile', extract):
            self.assertEqual(self.download(response), '已取消')
        self.assertEqual(final.read_bytes(), b'old')

    def test_done_callback_can_register_retry(self):
        queued = []

        class QueuedThread(InlineThread):
            def start(inner):
                queued.append(inner.target)

        done = []

        def on_done(tag, error):
            done.append(error)
            if len(done) == 1:
                self.bm.download(tag, lambda *_: None, on_done)

        with patch('cftm.binary_manager.threading.Thread', QueuedThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', side_effect=[Response(b''), Response(b'ok')]), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.download(self.tag, lambda *_: None, on_done)
            queued.pop(0)()
            self.assertTrue(self.bm.is_downloading(self.tag))
            queued.pop(0)()
        self.assertEqual(len(done), 2)
        self.assertIsNone(done[-1])
        self.assertFalse(self.bm.is_downloading(self.tag))
