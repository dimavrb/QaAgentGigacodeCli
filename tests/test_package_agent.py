import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from package_agent import ROOT_FILES, TREES, build_package, digest, verify_archive


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.output = Path(self.temp.name) / 'release'
        self.root.mkdir()
        for tree in TREES:
            (self.root / tree).mkdir(parents=True)
        for name in ROOT_FILES:
            (self.root / name).write_text('fixture\n')
        (self.root / 'VERSION').write_text('1.1\n')
        (self.root / 'gigacode-extension.json').write_text(json.dumps({'name': 'qa-agent', 'version': '1.1'}))
        (self.root / '.gigacode/settings.json').write_text(json.dumps({'mcpServers': {'playwright': {'args': ['@playwright/mcp@0.0.83']}}}))

    def build(self):
        return build_package(self.root, self.output, '2026-10-02')

    def test_manifest_checksum_and_runtime_exclusions(self):
        for name in ['.git/config', '.idea/settings', 'artifacts/browser/evidence', '.qa-agent-state/task.json', 'dist/old.zip', '.gigacode/session.json', 'scripts/__pycache__/test.pyc']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('must not ship')
        archive = self.build()
        manifest = verify_archive(archive)
        self.assertEqual('1.1', manifest['version'])
        self.assertEqual('0.0.83', manifest['mcp_version'])
        self.assertEqual(set(ROOT_FILES), {entry['path'] for entry in manifest['files']})
        self.assertEqual(f'{digest(archive.read_bytes())}  qa-agent-1.1.zip\n', archive.with_suffix('.zip.sha256').read_text())
        with self.assertRaisesRegex(ValueError, 'already exist'):
            self.build()

    def test_changed_file_fails_manifest_check(self):
        archive = self.build()
        broken = self.output / 'broken.zip'
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(broken, 'w') as target:
            for item in source.infolist():
                target.writestr(item, b'changed' if item.filename == 'qa-agent/README.md' else source.read(item.filename))
        with self.assertRaisesRegex(ValueError, 'Manifest mismatch'):
            verify_archive(broken)

    def test_unsafe_archive_path_rejected_before_reading_manifest(self):
        archive = self.output / 'unsafe.zip'
        self.output.mkdir()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('qa-agent/../outside', 'data')
        with self.assertRaisesRegex(ValueError, 'Unsafe ZIP entry'):
            verify_archive(archive)

    def test_source_symlink_and_invalid_version_are_rejected(self):
        path = self.root / 'docs/link.md'
        path.symlink_to(self.root / 'README.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.build()
        path.unlink()
        (self.root / 'VERSION').write_text('../outside')
        with self.assertRaisesRegex(ValueError, 'numeric release version'):
            self.build()

    def test_extension_version_mismatch_is_rejected_before_build(self):
        (self.root / 'gigacode-extension.json').write_text(json.dumps({'name': 'qa-agent', 'version': '9.9.9'}))
        with self.assertRaisesRegex(ValueError, 'Extension version differs'):
            self.build()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
