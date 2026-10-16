"""Local Git/update fixture; no test methods or live network dependencies."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_auto_update as updater
from package_agent import ROOT_FILES, TREES, SKIP
from qa_project import CONTEXT, initialize
from extension_resources import sync_resources


def copy_package(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for relative in ROOT_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    for tree in TREES:
        shutil.copytree(source / tree, destination / tree, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*SKIP, '*.pyc', '*.pyo'))


class UpdateFixture:
    def set_up_update_fixture(self, *, native_ci=False):
        environment_patch = patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'gc.auto', 'GIT_CONFIG_VALUE_0': '0',
            'GIT_CONFIG_KEY_1': 'maintenance.auto', 'GIT_CONFIG_VALUE_1': 'false',
        })
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temporary)
        self.root = Path(self.temporary.name).resolve()
        self.author = self.root / 'author'
        self.remote = self.root / 'remote.git'
        self.source = self.root / 'local source with пробелами'
        self.installed = self.root / 'installed extension'
        self.project = self.root / 'product'
        self.state = self.root / 'profile'
        self.project.mkdir()
        copy_package(ROOT, self.author)
        if native_ci:
            # Native launcher integration uses a labelled local CI fixture, never GitHub/network.
            ci_fixture = self.author / 'scripts/qa_release.py'
            ci_fixture.write_text(ci_fixture.read_text() + '\n# Local CI fixture\ndef ci_passed(commit): return True\n')
        self.command('git', 'init', '-b', 'main', str(self.author))
        self.git(self.author, 'config', 'user.name', 'QA fixture')
        self.git(self.author, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.author, 'add', '.')
        self.git(self.author, 'commit', '-m', 'fixture base')
        self.command('git', 'clone', '--no-hardlinks', '--bare', str(self.author), str(self.remote))
        self.command('git', 'clone', '--no-hardlinks', str(self.remote), str(self.source))
        copy_package(self.source, self.installed)
        initialize(self.project, self.installed)
        (self.project / CONTEXT).write_text('Preserved application context')
        evidence = self.project / '.qa-agent/artifacts/result.json'
        evidence.parent.mkdir()
        evidence.write_text('Historical result')
        self.base = self.git(self.source, 'rev-parse', 'HEAD')
        self.base_version = (self.source / 'VERSION').read_text().strip()
        parts = [int(part) for part in self.base_version.split('.')]
        parts[2] += 1
        self.next_version = '.'.join(map(str, parts))
        self.calls = []

    def cleanup_temporary(self):
        # Git filesystem activity can briefly outlive receive-pack on macOS.
        for attempt in range(6):
            try:
                self.temporary.cleanup()
                return
            except OSError as error:
                if error.errno not in (39, 66) or attempt == 5:
                    raise
                time.sleep(0.05)

    def command(self, *arguments, cwd=None):
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def git(self, path, *arguments):
        return self.command('git', '-C', str(path), *arguments)

    def publish(self, *, bump=True):
        if bump:
            manifest = json.loads((self.author / 'gigacode-extension.json').read_text())
            manifest['version'] = self.next_version
            (self.author / 'gigacode-extension.json').write_text(json.dumps(manifest))
            (self.author / 'VERSION').write_text(self.next_version+'\n')
        (self.author / 'README.md').write_text('Changed package documentation\n')
        sync_resources(self.author, apply=True)
        self.git(self.author, 'add', '.')
        self.git(self.author, 'commit', '-m', 'fixture update')
        self.git(self.author, 'push', str(self.remote), 'main')
        return self.git(self.author, 'rev-parse', 'HEAD')

    def native_cli(self, arguments, **kwargs):
        if arguments[0] == 'fake-gigacode':
            self.calls.append(arguments[1:])
            if arguments[1:] == ['extensions', 'update', 'qa-agent']:
                copy_package(self.source, self.installed)
            return subprocess.CompletedProcess(arguments, 0, 'fixture CLI', '')
        return self.original_run(arguments, **kwargs)

    def apply(self, process_check=lambda: False):
        self.original_run = updater.run
        with patch('qa_auto_update.run', side_effect=self.native_cli), updater.SessionLock(self.state) as lock:
            return updater.update(self.source, self.project, self.installed, 'fake-gigacode', lock,
                                  expected_remote=str(self.remote), process_check=process_check)

    def assert_project_preserved(self):
        self.assertEqual('Preserved application context', (self.project / CONTEXT).read_text())
        self.assertEqual('Historical result', (self.project / '.qa-agent/artifacts/result.json').read_text())
