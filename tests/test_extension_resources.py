"""Native discovery reads the default root directories, independently of manifest paths."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from extension_resources import RESOURCE_TREES, check_resources, sync_resources
from package_agent import build_package, digest, verify_archive
from qa_project import package_info


class ResourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for tree, name in [('agents', 'qa-analyst.md'), ('skills', 'qa-one/SKILL.md'),
                           ('commands', 'qa/doctor.md')]:
            path = self.root / '.gigacode' / tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'Fixture for {tree}\n')

    def test_apply_creates_complete_default_trees_and_repeated_check_is_read_only(self):
        self.assertEqual(3, len(sync_resources(self.root, apply=True)))
        before = {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob('*') if p.is_file()}
        check_resources(self.root)
        self.assertEqual([], sync_resources(self.root, apply=True))
        after = {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_or_different_exports_are_not_accepted(self):
        with self.assertRaisesRegex(ValueError, 'missing extension resource directory'):
            check_resources(self.root)
        sync_resources(self.root, apply=True)
        for mode in ('different', 'missing', 'extra'):
            with self.subTest(mode=mode):
                target = self.root / 'skills/qa-one/SKILL.md'
                if mode == 'different':
                    target.write_text('Stale native skill')
                elif mode == 'missing':
                    target.unlink()
                else:
                    (self.root / 'agents/retired.md').write_text('Stale role')
                with self.assertRaisesRegex(ValueError, 'native extension resources differ'):
                    check_resources(self.root)
                sync_resources(self.root, apply=True)
                check_resources(self.root)
        self.assertFalse((self.root / 'agents/retired.md').exists())

    def test_nested_skill_assets_are_copied_with_unchanged_relative_paths(self):
        source = self.root / '.gigacode/skills/qa-one/references/guide.md'
        source.parent.mkdir()
        source.write_text('Reference\n')
        sync_resources(self.root, apply=True)
        self.assertEqual(source.read_bytes(), (self.root / 'skills/qa-one/references/guide.md').read_bytes())

    def test_build_rejects_stale_exports_before_publishing_archive(self):
        (self.root / 'VERSION').write_text((ROOT / 'VERSION').read_text())
        sync_resources(self.root, apply=True)
        (self.root / '.gigacode/agents/qa-analyst.md').write_text('Changed role without export')
        output = self.root / 'release'
        with self.assertRaisesRegex(ValueError, 'native extension resources differ'):
            build_package(self.root, output, '2026-10-15')
        self.assertFalse(output.exists())

    def test_symlink_in_any_tree_rejects_regeneration_before_first_write(self):
        sync_resources(self.root, apply=True)
        original = (self.root / 'agents/qa-analyst.md').read_bytes()
        (self.root / '.gigacode/agents/qa-analyst.md').write_text('New role')
        external = self.root / 'outside'
        external.mkdir()
        for relative in ('skills/link', '.gigacode/skills/link', 'commands'):
            with self.subTest(relative=relative):
                path = self.root / relative
                if path.is_dir():
                    shutil.rmtree(path)
                path.symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'symlinked extension resource'):
                    sync_resources(self.root, apply=True)
                self.assertEqual(original, (self.root / 'agents/qa-analyst.md').read_bytes())
                self.assertEqual([], list(external.iterdir()))
                path.unlink()


class NativeDiscoveryTests(unittest.TestCase):
    def test_archive_cannot_hide_or_change_native_resources_with_a_matching_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = build_package(ROOT, directory / 'release', '2026-10-15')
            with zipfile.ZipFile(archive) as bundle:
                original = {name: bundle.read(name) for name in bundle.namelist()}
            for mode in ('missing', 'changed'):
                with self.subTest(mode=mode):
                    entries = dict(original)
                    name = 'qa-agent/agents/qa-analyst.md'
                    if mode == 'missing':
                        del entries[name]
                    else:
                        entries[name] = b'Stale exported role\n'
                    manifest = json.loads(entries['qa-agent/PACKAGE_MANIFEST.json'])
                    manifest['files'] = [dict(path=path.removeprefix('qa-agent/'), size=len(data), sha256=digest(data))
                                         for path, data in entries.items() if not path.endswith('/PACKAGE_MANIFEST.json')]
                    entries['qa-agent/PACKAGE_MANIFEST.json'] = json.dumps(manifest).encode()
                    broken = directory / (mode + '.zip')
                    with zipfile.ZipFile(broken, 'w') as bundle:
                        for path, data in entries.items():
                            bundle.writestr(path, data)
                    with self.assertRaisesRegex(ValueError, 'Native extension resources differ'):
                        verify_archive(broken)

    def test_release_archive_has_discoverable_root_resources_without_manifest_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = build_package(ROOT, directory / 'release', '2026-10-15')
            manifest = verify_archive(archive)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(directory / 'unpacked')
            package = directory / 'unpacked/qa-agent'
            # Model the reported loader: only default directories; ignore path fields.
            self.assertEqual(14, len(list((package / 'agents').glob('*.md'))))
            self.assertEqual(6, len(list((package / 'skills').glob('*/SKILL.md'))))
            commands = {p.relative_to(package / 'commands').with_suffix('').as_posix().replace('/', ':')
                        for p in (package / 'commands').rglob('*.md')}
            self.assertEqual({'qa', 'qa:init', 'qa:doctor', 'qa:tasks', 'qa:resume'}, commands)
            config = json.loads((package / 'gigacode-extension.json').read_text())
            # Keep these metadata fields for the trusted 2.3.11 updater, which requires them.
            for tree in RESOURCE_TREES:
                self.assertEqual('.gigacode/' + tree, config.pop(tree))
            (package / 'gigacode-extension.json').write_text(json.dumps(config))
            self.assertEqual(14, package_info(package)['agent_count'])
            self.assertEqual(manifest['version'], package_info(package)['package_version'])
            shutil.rmtree(package / 'agents')
            with self.assertRaisesRegex(ValueError, 'missing extension resource directory'):
                package_info(package)


if __name__ == '__main__':
    unittest.main()
