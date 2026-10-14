"""Configuration persistence regressions; no display required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cftm.config import Config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'

    def test_invalid_utf8_uses_defaults(self):
        self.path.write_bytes(b'\xff')
        self.assertEqual(Config(self.path).get('tunnels'), [])

    def test_save_does_not_follow_predictable_temp_symlink(self):
        victim = self.path.parent / 'other-file'
        victim.write_text('keep me')
        self.path.with_suffix('.json.tmp').symlink_to(victim)
        cfg = Config(self.path)
        cfg.save()
        self.assertEqual(victim.read_text(), 'keep me')
        self.assertEqual(json.loads(self.path.read_text()), cfg.data)

    def test_save_is_private(self):
        Config(self.path).save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        self.path.write_text('{"active_version": "system"}')
        cfg = Config(self.path)
        cfg.set('active_version', 'latest-installed')
        with patch.object(Path, 'replace', side_effect=OSError('disk error')):
            cfg.save()
        self.assertEqual(json.loads(self.path.read_text())['active_version'], 'system')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_encoding_preserves_original(self):
        self.path.write_text('{}')
        cfg = Config(self.path)
        cfg.set('bad', '\ud800')
        cfg.save()
        self.assertEqual(self.path.read_text(), '{}')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_defaults_are_independent(self):
        first, second = Config(self.path), Config(self.path)
        first.get('tunnels').append({'name': 'one'})
        self.assertEqual(second.get('tunnels'), [])
