import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.agent_interface.catalog import ROOT
from tools.agent_interface.service import Service


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memory = load('memory_v3', '.claude/skills/program-memory/scripts/research_store.py')
recovery = load('recovery', '.claude/skills/campaign/scripts/recovery.py')
sys.modules['recovery'] = recovery
campaign = load('campaign_v3', '.claude/skills/campaign/scripts/campaign_runner.py')


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self):
        store = memory.ResearchStore(self.root / 'memory.sqlite')
        self.addCleanup(store.db.close)
        return store

    def test_migration_preserves_ids_links_backup_and_restore(self):
        path = self.root / 'memory.sqlite'
        memory.legacy.init_memory('p', str(path))
        original = memory.legacy.record_fact('p', 'program_fact', 'original', db_path=str(path))
        correction = memory.legacy.record_correction(original['fact_id'], 'corrected', 'new observation', str(path), program='p')
        with sqlite3.connect(path) as db:
            before = db.execute('SELECT * FROM facts ORDER BY fact_id').fetchall()
        store = self.store()
        self.assertEqual(before, [tuple(row) for row in store.db.execute('SELECT * FROM facts ORDER BY fact_id')])
        self.assertEqual(original['fact_id'], store.get('p', correction['fact_id'])['legacy']['correction_of'])
        backups = list(self.root.glob('*.pre-v2-*.sqlite'))
        self.assertEqual(1, len(backups))
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(before, db.execute('SELECT * FROM facts ORDER BY fact_id').fetchall())
        snapshot = self.root / 'snapshot.sqlite'
        memory.backup(path, snapshot)
        restored = memory.ResearchStore(snapshot)
        self.addCleanup(restored.db.close)
        self.assertEqual(store.query('p', history=original['fact_id']), restored.query('p', history=original['fact_id']))
        self.assertEqual(2, store.db.execute('SELECT max(version) FROM memory_schema').fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute('DELETE FROM research_events')

    def test_revision_conflicts_and_program_isolation(self):
        first, second = self.store(), self.store()
        fact = first.append('p', {'kind': 'observation', 'content': 'Tenant A owns object'}, {'actor': 'test'})
        updated = second.correct('p', fact['id'], 1, 'Object owned by tenant B', 'new evidence', {'actor': 'second'})
        self.assertEqual(2, updated['revision'])
        with self.assertRaises(memory.RevisionConflict):
            first.correct('p', fact['id'], 1, 'stale', 'stale writer', {})
        with self.assertRaises(memory.RevisionConflict):
            first.correct('other', fact['id'], 2, 'cross-program', 'invalid', {})
        self.assertEqual([], first.query('other', history=fact['id']))
        with self.assertRaises(ValueError):
            first.append('other', {'kind': 'hypothesis', 'content': 'claim', 'sources': [fact['id']]}, {})
        self.assertEqual(2, len(first.query('p', history=fact['id'])))

    def test_secrets_every_field_and_export_rescan(self):
        store = self.store()
        secret = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'
        for field in ('content', 'expires_at', 'sources', 'details', 'artifacts'):
            record = {'kind': 'observation', 'content': 'safe', field: secret}
            with self.subTest(field=field), self.assertRaises(ValueError):
                store.append('p', record, {})
        record = store.append('p', {'kind': 'observation', 'content': 'safe', 'sensitivity': 'report-safe'}, {})
        for value in ({'actor': secret}, {'metadata': {'nested': secret}}):
            with self.assertRaises(ValueError):
                store.append('p', {'kind': 'observation', 'content': 'safe'}, value)
        with self.assertRaises(ValueError):
            store.correct('p', record['id'], 1, 'safe', secret, {})
        high = store.append('p', {'kind': 'decision', 'content': 'private decision', 'sensitivity': 'high-sensitivity'}, {})
        self.assertNotIn(high['id'], json.dumps(store.summary('p')))
        # A malicious direct projection edit must not bypass export scanning.
        poisoned = {**record, 'content': secret}
        with store.db:
            store.db.execute('UPDATE research_entities SET data=? WHERE id=?', (json.dumps(poisoned), record['id']))
        self.assertEqual([], store.export('p'))

    def test_provider_switch_recovers_research_and_negative_results(self):
        database = self.root / 'agent.sqlite'
        service = Service(database, 'uid:test')
        self.addCleanup(service.db.close)
        service.register('p', 'c', 'a' * 64)
        first = service.open_session(dict(program_id='p', campaign_id='c', provider='codex', client_version='test'))['session']
        def call(operation, params):
            return service.handle(dict(schema_version='1.0', operation=operation, params=params))
        observed = call('research.append', {'session_id': first['session_id'], 'record': {'kind': 'negative_result', 'content': 'Tenant isolation held', 'details': {'surface': '/objects', 'method': 'two fixture accounts', 'conditions': 'v1'}, 'expires_at': '2020-01-01T00:00:00+00:00'}})
        second = service.open_session(dict(program_id='p', campaign_id='c', provider='opencode', client_version='test', predecessor_session_id=first['session_id']))['session']
        handoff = service.handoff(second)
        self.assertIn(observed['id'], handoff['research']['source_ids'])
        self.assertIn(observed['id'], handoff['research']['expired_review'])
        self.assertEqual('agent-unreviewed', handoff['research']['records'][0]['trust'])
        self.assertFalse(handoff['research']['scope_authority'])

    def test_snapshot_and_no_unknown_retry(self):
        state = recovery.Recovery(self.root)
        self.addCleanup(state.db.close)
        original = {'outdir': '/private/run', 'program': 'p'}
        state.bind(original, 'fixture.test', 'p', 'scope', 'policy')
        self.assertEqual(original, state.bind({'outdir': '/wrong/global'}, 'fixture.test', 'p', 'scope', 'policy', resume=True))
        with self.assertRaises(ValueError):
            state.bind(original, 'other.test', 'p', 'scope', 'policy', resume=True)
        self.assertTrue(state.claim('workflow', 'digest'))
        competing = recovery.Recovery(self.root)
        self.addCleanup(competing.db.close)
        with self.assertRaises(ValueError):
            competing.claim('workflow', 'digest')
        state.heartbeat('workflow')
        with state.db:
            state.db.execute('UPDATE runs SET expires=0')
        with self.assertRaises(ValueError):
            competing.claim('workflow', 'digest')
        self.assertEqual('interrupted', state.db.execute('SELECT status FROM runs').fetchone()[0])
        competing.reconcile('workflow', True)
        self.assertFalse(state.claim('workflow', 'digest'))
        with self.assertRaises(ValueError):
            state.claim('workflow', 'changed-digest')

    def test_campaign_exception_preserves_failure_and_no_global_context(self):
        args = campaign.build_parser().parse_args(['hunt', '--target', 'fixture.test', '--program', 'p', '--no-init', '--dry-run'])
        runner = campaign.Campaign(args)
        context = {'outdir': '/unused', 'program': 'p'}
        with patch.object(campaign, 'CAMPAIGN_ROOT', self.root), patch.object(campaign, 'load_context', return_value=context), patch.object(runner, 'phase_backbone', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                runner.hunt()
        status = json.loads((runner.dir / 'status.json').read_text())
        self.assertEqual('failed', status['phase'])
        self.assertTrue((runner.dir / 'context.snapshot.json').exists())
        self.assertNotEqual('/unused', json.loads((runner.dir / 'context.snapshot.json').read_text())['context']['outdir'])


if __name__ == '__main__':
    unittest.main()

class Phase3AcceptanceTests(unittest.TestCase):
    setUp = Phase3Tests.setUp
    store = Phase3Tests.store

    def test_sensitivity_inheritance_source_revision_and_correction(self):
        store = self.store()
        source = store.append('p', {'kind': 'observation', 'content': 'Restricted observation', 'sensitivity': 'high-sensitivity'}, {})
        derived = store.append('p', {'kind': 'hypothesis', 'content': 'Derived inference', 'sensitivity': 'report-safe', 'sources': [source['id']]}, {})
        self.assertEqual('high-sensitivity', derived['sensitivity'])
        self.assertEqual({source['id']: 1}, derived['source_revisions'])
        updated = store.correct('p', derived['id'], 1, 'Updated inference', 'Additional context', {})
        self.assertEqual('high-sensitivity', updated['sensitivity'])
        self.assertEqual([], store.export('p'))
        self.assertEqual([], store.query('p', history=derived['id']))

    def test_failed_migration_rolls_back_and_keeps_backup(self):
        path = self.root / 'memory.sqlite'
        memory.legacy.init_memory('p', str(path))
        memory.legacy.record_fact('p', 'program_fact', 'original', db_path=str(path))
        with patch.object(memory.ResearchStore, '_write', side_effect=RuntimeError('migration failure')):
            with self.assertRaises(RuntimeError):
                self.store()
        with sqlite3.connect(path) as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM facts').fetchone()[0])
            self.assertNotIn('research_events', {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertTrue(list(self.root.glob('*.pre-v2-*.sqlite')))
        self.assertEqual(1, len(self.store().query('p')))

    def test_legacy_security_overrides_rejected(self):
        path = self.root / 'legacy.sqlite'
        memory.legacy.init_memory('p', str(path))
        for kwargs in ({'reviewed': True}, {'allow_secrets': True}):
            with self.assertRaises(ValueError):
                memory.legacy.record_fact('p', 'program_fact', 'safe', db_path=str(path), **kwargs)
        with self.assertRaises(ValueError):
            memory.legacy.search_facts('p', db_path=str(path), cross_program=True)
        with self.assertRaises(ValueError):
            memory.legacy.get_fact('anything', str(path))

    def test_campaign_lock_and_unique_ids(self):
        args = campaign.build_parser().parse_args(['hunt', '--target', 'fixture.test', '--dry-run'])
        first, second, competing = (campaign.Campaign(args) for _ in range(3))
        with patch.object(campaign, 'CAMPAIGN_ROOT', self.root):
            first.setup_dir('fixture.test')
            second.setup_dir('fixture.test')
            try:
                self.assertNotEqual(first.campaign_id, second.campaign_id)
                with self.assertRaises(ValueError):
                    competing.setup_dir('fixture.test', resume_id=first.campaign_id)
            finally:
                for runner in (first, second):
                    runner.log_fh.close()
                    runner.results_fh.close()
                    runner.recovery.db.close()
                    runner.lock_fh.close()

    def test_completed_campaign_workflow_survives_resume_and_partial_log(self):
        args = campaign.build_parser().parse_args(['hunt', '--target', 'fixture.test', '--dry-run'])
        args.dry_run = False
        first = campaign.Campaign(args)
        with patch.object(campaign, 'CAMPAIGN_ROOT', self.root):
            first.setup_dir('fixture.test')
            with patch.object(first, 'run_cmd', return_value=0) as execute:
                first.run_workflow('standard-catalog', 'search')
                self.assertEqual(1, execute.call_count)
            first.results_fh.write('{partial-crash')
            first.results_fh.close()
            first.log_fh.close()
            first.lock_fh.close()
            first.recovery.db.close()
            resumed = campaign.Campaign(args)
            resumed.setup_dir('fixture.test', resume_id=first.campaign_id)
            try:
                with patch.object(resumed, 'run_cmd', side_effect=AssertionError('Successful task must not repeat')):
                    resumed.run_workflow('standard-catalog', 'search')
                self.assertIn(('standard-catalog', 'search'), resumed.completed)
            finally:
                resumed.results_fh.close()
                resumed.log_fh.close()
                resumed.lock_fh.close()
                resumed.recovery.db.close()

    def test_interface_artifact_ownership_integrity_and_expired_worker(self):
        from tools.agent_interface.catalog import catalog
        from tools.agent_interface.contracts import InterfaceError
        service = Service(self.root / 'interface.sqlite', 'uid:test')
        self.addCleanup(service.db.close)
        context = service.register('p', 'c', 'a' * 64)
        session = service.open_session(dict(program_id='p', campaign_id='c', provider='codex', client_version='test'))['session']
        entry = next(e for e in catalog()['workflows'] if e['id'] == 'standard-catalog/search')
        task = dict(schema_version='1.0', task_id='t', idempotency_key='k', program_id='p', campaign_id='c',
                    session_id=session['session_id'], skill='standard-catalog', workflow='search', inputs={},
                    expected_context_revision=1, scope_digest=context['scope_digest'], policy_digest=context['policy_digest'], workflow_digest=entry['workflow_digest'])
        result = service.submit(task)
        artifact = result['artifacts'][0]
        record = {'kind': 'evidence', 'content': 'Local standards result', 'artifacts': [{'id': artifact['id'], 'sha256': artifact['sha256']}]}
        def append(sid):
            return service.handle(dict(schema_version='1.0', operation='research.append', params={'session_id': sid, 'record': record}))
        self.assertEqual('evidence', append(session['session_id'])['kind'])
        service.register('q', 'c', 'b' * 64)
        other = service.open_session(dict(program_id='q', campaign_id='c', provider='opencode', client_version='test'))['session']
        with self.assertRaises(InterfaceError):
            append(other['session_id'])
        with service.db:
            service.db.execute('UPDATE artifacts SET data=? WHERE id=?', (b'tampered', artifact['id']))
        with self.assertRaises(InterfaceError):
            append(session['session_id'])
        result.update(status='running', ended_at=None)
        with service.db:
            service.save_result(result)
            service.db.execute('INSERT OR REPLACE INTO task_leases VALUES(?,?)', ('t', 0))
        recovered = service.handle(dict(schema_version='1.0', operation='task.status', params={'session_id': session['session_id'], 'task_id': 't'}))
        self.assertEqual('interrupted', recovered['status'])
        self.assertEqual(recovered, service.submit(task))

class Phase3ContextTests(unittest.TestCase):
    setUp = Phase3Tests.setUp
    store = Phase3Tests.store

    def test_context_preserves_counterevidence_and_detects_stale_sources(self):
        store = self.store()
        fact = store.append('p', {'kind': 'observation', 'content': 'Fixture ownership boundary'}, {})
        hypothesis = store.append('p', {'kind': 'hypothesis', 'content': 'Boundary may differ', 'sources': [fact['id']]}, {})
        negative = store.append('p', {'kind': 'negative_result', 'content': 'Boundary held in fixture', 'sources': [hypothesis['id']]}, {})
        for i in range(55):
            store.append('p', {'kind': 'observation', 'content': f'Unrelated fixture {i}'}, {})
        pack = store.context_pack('p', hypothesis['id'], max_records=2)
        self.assertEqual([hypothesis['id'], negative['id']], [r['id'] for r in pack['records']])
        self.assertEqual(55, pack['omitted']['unrelated'])
        self.assertEqual(1, pack['omitted']['record_limit'])
        self.assertEqual(pack, store.context_pack('p', hypothesis['id'], max_records=2))
        store.correct('p', fact['id'], 1, 'Corrected boundary', 'fixture update', {})
        stale = store.context_pack('p', hypothesis['id'])['stale_sources']
        self.assertEqual([{'entity_id': hypothesis['id'], 'source_id': fact['id'], 'expected_revision': 1, 'current_revision': 2}], stale)
        with self.assertRaises(ValueError):
            store.context_pack('other', hypothesis['id'])

    def test_context_byte_bounds_and_restricted_records(self):
        store = self.store()
        focus = store.append('p', {'kind': 'hypothesis', 'content': 'Fixture focus'}, {})
        large = store.append('p', {'kind': 'investigation', 'content': 'x' * 4000, 'sources': [focus['id']]}, {})
        restricted = store.append('p', {'kind': 'observation', 'content': 'Hidden fixture', 'sensitivity': 'high-sensitivity', 'sources': [focus['id']]}, {})
        pack = store.context_pack('p', focus['id'], max_bytes=2048)
        self.assertLessEqual(len(json.dumps(pack, sort_keys=True).encode()), 2048)
        self.assertEqual(1, pack['omitted']['byte_limit'])
        self.assertNotIn(large['id'], json.dumps(pack))
        self.assertNotIn(restricted['id'], json.dumps(pack))
        with self.assertRaises(ValueError):
            store.context_pack('p', large['id'], max_bytes=2048)
        with self.assertRaises(ValueError):
            store.context_pack('p', restricted['id'])

    def test_lesson_candidates_require_outcomes_and_cannot_self_promote(self):
        store = self.store()
        outcome = store.append('p', {'kind': 'investigation', 'content': 'Local fixture outcome', 'sensitivity': 'program-private'}, {})
        candidate = {'kind': 'technique_candidate', 'content': 'Check ownership at each transition', 'sensitivity': 'report-safe',
                     'sources': [outcome['id']], 'details': {'applicability': 'Multi-step ownership checks', 'supporting_outcomes': [outcome['id']], 'counterexamples': []}}
        saved = store.append('p', candidate, {})
        self.assertEqual('agent-unreviewed', saved['trust'])
        self.assertEqual('program-private', saved['sensitivity'])
        self.assertEqual({outcome['id']: 1}, saved['source_revisions'])
        self.assertNotIn(saved['id'], json.dumps(store.export('p')))
        for record in ({**candidate, 'trust': 'reviewed'}, {**candidate, 'details': {**candidate['details'], 'approved': True}},
                       {**candidate, 'sources': []}, {**candidate, 'details': {}}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                store.append('p', record, {})
        with self.assertRaises(ValueError):
            store.append('other', candidate, {})

    def test_context_transport_provider_switch_and_stale_session(self):
        from tools.agent_interface.contracts import InterfaceError
        service = Service(self.root / 'interface.sqlite', 'uid:test')
        self.addCleanup(service.db.close)
        service.register('p', 'c', 'a' * 64)
        session = service.open_session(dict(program_id='p', campaign_id='c', provider='codex', client_version='test'))['session']
        def call(op, sid, **params):
            return service.handle(dict(schema_version='1.0', operation=op, params={'session_id': sid, **params}))
        outcome = call('research.append', session['session_id'], record={'kind': 'investigation', 'content': 'Fixture result'})
        candidate = call('research.append', session['session_id'], record={'kind': 'technique_candidate', 'content': 'Fixture lesson', 'sources': [outcome['id']],
                         'details': {'applicability': 'Local fixture', 'supporting_outcomes': [outcome['id']], 'counterexamples': []}})
        second = service.open_session(dict(program_id='p', campaign_id='c', provider='opencode', client_version='test', predecessor_session_id=session['session_id']))['session']
        self.assertEqual(call('research.context', session['session_id'], entity_id=candidate['id']), call('research.context', second['session_id'], entity_id=candidate['id']))
        with self.assertRaises(InterfaceError):
            call('research.context', second['session_id'], entity_id=candidate['id'], max_records=True)
        service.register('p', 'c', 'b' * 64)
        with self.assertRaises(InterfaceError):
            call('research.context', second['session_id'], entity_id=candidate['id'])
