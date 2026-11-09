"""Fault-injection tests for the boot script's embedded config merge, with no Hermes/network."""
import contextlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'deploy/railway/bh-start.sh').read_text().split("<<'PYCONFIG'", 1)[1].split("\n", 1)[1].split('\nPYCONFIG\n', 1)[0]
SEED_SOURCE = (ROOT / 'deploy/railway/bh-start.sh').read_text().split("<<'PYSEED'", 1)[1].split("\n", 1)[1].split('\nPYSEED\n', 1)[0]


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'

    def run_merge(self):
        err = io.StringIO()
        with patch.object(sys, 'argv', ['merge', str(self.path)]), contextlib.redirect_stderr(err):
            namespace = {'__name__': '__test__'}
            exec(compile(SOURCE, 'bh-start-config', 'exec'), namespace)
            namespace['merge'](self.path)
        return err.getvalue()

    def seed(self, data):
        self.path.write_bytes(data)
        self.path.chmod(0o600)

    def assert_original(self, data):
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_already_enabled_is_byte_identical(self):
        data = b'# exact bytes\nplugins: {enabled: [borrowed-hands, other]}\n'
        self.seed(data)
        self.assertIn('already enabled, untouched', self.run_merge())
        self.assert_original(data)

    def test_valid_merge_preserves_comments_settings_and_private_mode(self):
        self.seed(b'# keep me\nmodel: {default: my-model}\nplugins: {enabled: [other]}\n')
        self.run_merge()
        text = self.path.read_text()
        d = YAML().load(text)
        self.assertIn('# keep me', text)
        self.assertEqual(d['model'], {'default': 'my-model'})
        self.assertEqual(d['plugins']['enabled'], ['other', 'borrowed-hands'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_missing_sections_are_added(self):
        self.seed(b'model: {default: my-model}\n')
        self.run_merge()
        self.assertEqual(YAML().load(self.path)['plugins']['enabled'], ['borrowed-hands'])

    def test_symlinked_config_refused_before_parsing_target(self):
        target = Path(self.tmp.name) / 'outside.yaml'
        data = b'token: FAKE-ROOT-YAML-CANARY\n'
        target.write_bytes(data)
        target.chmod(0o600)
        self.path.symlink_to(target)
        with patch.object(YAML, 'load', side_effect=AssertionError('must not parse target')):
            with self.assertRaises(OSError):
                self.run_merge()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['config.yaml', 'outside.yaml'])

    def test_seed_refuses_a_config_link_planted_before_publication(self):
        source = Path(self.tmp.name) / 'default.yaml'
        source.write_bytes(b'plugins: {enabled: [borrowed-hands]}\n')
        target = Path(self.tmp.name) / 'outside.yaml'
        data = b'token: FAKE-ROOT-YAML-CANARY\n'
        target.write_bytes(data)
        target.chmod(0o600)
        real_fsync = os.fsync

        def plant_before_publication(fd):
            self.path.symlink_to(target)
            return real_fsync(fd)

        with patch.object(sys, 'argv', ['seed', str(source), str(self.path)]):
            with patch('os.fsync', side_effect=plant_before_publication):
                with self.assertRaises(FileExistsError):
                    exec(compile(SEED_SOURCE, 'bh-start-seed', 'exec'), {'__name__': '__test__'})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ['config.yaml', 'default.yaml', 'outside.yaml'])

    def test_malformed_policies_refused_without_changes(self):
        cases = [b'', b'# deliberately empty\n', b'null\n', b'[]\n',
                 b'plugins: borrowed-hands\n', b'plugins: null\n',
                 b'plugins: {enabled: borrowed-hands}\n',
                 b'plugins: {enabled: not-borrowed-hands-please}\n',
                 b'plugins: {enabled: null}\n', b'plugins: {enabled: {borrowed-hands: yes}}\n',
                 b'plugins: {enabled: [borrowed-hands, 3]}\n', b'plugins: [unclosed\n']
        for data in cases:
            with self.subTest(data=data):
                self.seed(data)
                with self.assertRaises(Exception):
                    self.run_merge()
                self.assert_original(data)

    def test_dump_failure_preserves_original(self):
        data = b'# user config\nmodel: {default: my-model}\n'
        self.seed(data)
        def broken_dump(yaml, obj, stream):
            stream.write('partial serialization')
            raise OSError('injected serialization failure')
        with patch.object(YAML, 'dump', broken_dump), self.assertRaises(OSError):
            self.run_merge()
        self.assert_original(data)

    def test_write_failure_preserves_original(self):
        data = b'# user config\nmodel: {default: my-model}\n'
        self.seed(data)
        real_fdopen = os.fdopen
        @contextlib.contextmanager
        def broken_fdopen(*args, **kwargs):
            with real_fdopen(*args, **kwargs) as f:
                class Writer:
                    def write(self, text):
                        f.write('partial write')
                        raise OSError('injected write failure')
                yield Writer()
        with patch('os.fdopen', broken_fdopen), self.assertRaises(OSError):
            self.run_merge()
        self.assert_original(data)

    def test_fsync_failure_preserves_original(self):
        data = b'# user config\nmodel: {default: my-model}\n'
        self.seed(data)
        with patch('os.fsync', side_effect=OSError('injected fsync failure')), self.assertRaises(OSError):
            self.run_merge()
        self.assert_original(data)

    def test_replace_failure_preserves_original(self):
        data = b'# user config\nmodel: {default: my-model}\n'
        self.seed(data)
        with patch('os.replace', side_effect=OSError('injected replace failure')), self.assertRaises(OSError):
            self.run_merge()
        self.assert_original(data)


if __name__ == '__main__':
    unittest.main()
