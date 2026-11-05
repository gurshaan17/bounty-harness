import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.adapters.render_adapter import Conflict, REPO_ROOT, Renderer


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('AGENTS.md', 'CLAUDE.md'):
            shutil.copyfile(REPO_ROOT / name, self.root / name)
        shutil.copytree(REPO_ROOT / 'tools/adapters', self.root / 'tools/adapters',
                        ignore=shutil.ignore_patterns('state.json', 'backups', '__pycache__'))
        shutil.copytree(REPO_ROOT / '.claude/skills/standard-catalog', self.root / '.claude/skills/standard-catalog',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.originals = {p: (self.root / p).read_bytes() for p in ('AGENTS.md', 'CLAUDE.md')}

    def run_renderer(self, target, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            Renderer(self.root).run(target, **kwargs)

    def test_preview_install_idempotency_uninstall_all_targets(self):
        for target in Renderer(self.root).manifest['targets']:
            with self.subTest(target=target):
                renderer = Renderer(self.root)
                outputs = renderer.outputs(target)
                self.run_renderer(target)
                self.assertTrue(all(not (self.root / p).exists() for p in outputs))
                self.run_renderer(target, apply=True)
                before = {p: (self.root / p).stat().st_mtime_ns for p in outputs}
                self.run_renderer(target, apply=True)
                self.assertEqual(before, {p: (self.root / p).stat().st_mtime_ns for p in outputs})
                self.run_renderer(target, apply=True, uninstall=True)
                self.assertTrue(all(not (self.root / p).exists() for p in outputs))
        for name, content in self.originals.items():
            self.assertEqual(content, (self.root / name).read_bytes())

    def test_manual_edits_conflict_for_apply_and_uninstall(self):
        self.run_renderer('generic', apply=True)
        path = self.root / 'docs/bb_toolkit_agent_guide.md'
        path.write_text(path.read_text().replace('Offline', 'Changed').replace('offline', 'changed'))
        edited = path.read_text()
        for uninstall in (False, True):
            with self.assertRaises(Conflict):
                self.run_renderer('generic', apply=True, uninstall=uninstall)
            self.assertEqual(edited, path.read_text())

    def test_unrelated_text_preserved_and_unique_backups(self):
        self.run_renderer('generic', apply=True)
        path = self.root / 'docs/bb_toolkit_agent_guide.md'
        path.write_text('My introduction\n' + path.read_text() + 'My ending\n')
        (self.root / 'AGENTS.md').write_text('Changed shared instructions')
        self.run_renderer('generic', apply=True)
        self.assertTrue(path.read_text().startswith('My introduction\n'))
        self.run_renderer('generic', apply=True, uninstall=True)
        self.assertEqual('My introduction\nMy ending\n', path.read_text())
        self.assertEqual(2, len(list((self.root / 'tools/adapters/backups').glob('*.bak'))))

    def test_unmanaged_and_config_conflicts_preflight(self):
        path = self.root / '.codex/config.toml'
        path.parent.mkdir()
        path.write_text('user = "configuration"')
        with self.assertRaises(Conflict):
            self.run_renderer('codex', apply=True)
        self.assertFalse((self.root / '.agents/skills/bounty-harness/SKILL.md').exists())

    def test_paths_and_symlinks_rejected(self):
        renderer = Renderer(self.root)
        for path in ('../outside', '/tmp/escape', 'AGENTS.md', 'CLAUDE.md', '.bb/private'):
            with self.assertRaises(Conflict):
                renderer.safe_path(path)
        (self.root / 'docs').mkdir(exist_ok=True)
        (self.root / 'docs/linked').symlink_to('/tmp')
        with self.assertRaises(Conflict):
            renderer.safe_path('docs/linked/escape.md')

    def test_catalog_and_source_changes_invalidate_generated_content(self):
        renderer = Renderer(self.root)
        before = renderer.outputs('generic')
        path = self.root / '.claude/skills/standard-catalog/skill.yaml'
        path.write_text(path.read_text().replace('  export:', '  export-new:'))
        self.assertNotEqual(before, renderer.outputs('generic'))

    def test_frontmatter_and_permissions(self):
        import tomllib
        import yaml
        renderer = Renderer(self.root)
        codex = renderer.outputs('codex')
        self.assertEqual('read-only', tomllib.loads(codex['.codex/config.toml'])['sandbox_mode'])
        self.assertTrue(codex['.agents/skills/bounty-harness/SKILL.md'].startswith('---\n'))
        for path, text in renderer.outputs('opencode').items():
            front = yaml.safe_load(text.split('---\n')[1])
            self.assertEqual('deny', front['permissions'][0]['effect'])
            if 'reviewer' in path:
                self.assertFalse(any(p['action'] == 'shell' for p in front['permissions']))
        settings = json.loads(renderer.outputs('claude-code')['.claude/settings.local.json'])
        self.assertEqual('dontAsk', settings['permissions']['defaultMode'])


if __name__ == '__main__':
    unittest.main()
