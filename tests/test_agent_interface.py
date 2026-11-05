import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.agent_interface.catalog import ROOT, catalog
from tools.agent_interface.contracts import InterfaceError, decode, validate_named
from tools.agent_interface.service import Service


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = catalog()
        cls.entry = next(e for e in cls.catalog['workflows'] if e['id'] == 'standard-catalog/search')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'interface.sqlite3'
        self.service = Service(self.database, 'uid:test')
        self.addCleanup(self.service.db.close)
        self.context = self.service.register('example', 'local', 'a' * 64)
        self.session = self.open('codex')['session']
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('External egress forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def open(self, provider, **extra):
        return self.call('session.open', dict(program_id='example', campaign_id='local', provider=provider,
                                             client_version='test-1', **extra))

    def call(self, operation, params):
        return self.service.handle(dict(schema_version='1.0', operation=operation, params=params))

    def task(self, **extra):
        return dict(schema_version='1.0', task_id='task-1', idempotency_key='key-1', program_id='example',
                    campaign_id='local', session_id=self.session['session_id'], skill='standard-catalog',
                    workflow='search', inputs={'QUERY': 'injection'}, expected_context_revision=1,
                    scope_digest='a' * 64, policy_digest=self.context['policy_digest'],
                    workflow_digest=self.entry['workflow_digest'], **extra)

    def assert_error(self, code, function, *args):
        with self.assertRaises(InterfaceError) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_catalog_normalizes_and_fails_closed(self):
        self.assertGreater(len(self.catalog['workflows']), 100)
        self.assertEqual(['offline'], self.entry['execution_modes'])
        for entry in self.catalog['workflows']:
            self.assertEqual(len(entry['workflow_digest']), 64)
            self.assertIsInstance(entry['inputs'], dict)
            self.assertTrue(all(isinstance(out, dict) for out in entry['outputs']))
            if entry['id'] != self.entry['id']:
                self.assertEqual([], entry['execution_modes'])

    def test_offline_result_artifact_and_restart(self):
        result = self.call('task.submit', self.task())
        self.assertEqual('succeeded', result['status'])
        validate_named(result, 'result')
        artifact = result['artifacts'][0]
        data = self.service.db.execute('SELECT data FROM artifacts WHERE id=?', (artifact['id'],)).fetchone()[0]
        self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
        self.assertGreater(len(json.loads(data)), 0)
        self.assertEqual(0, result['budgets']['requests'])
        restarted = Service(self.database, 'uid:test')
        self.addCleanup(restarted.db.close)
        self.assertEqual(result, restarted.submit(self.task()))

    def test_provider_switch_preserves_task_identity(self):
        result = self.call('task.submit', self.task())
        for provider in ('opencode', 'claude-code'):
            session = self.open(provider, predecessor_session_id=self.session['session_id'])['session']
            task = self.task()
            task['session_id'] = session['session_id']
            self.assertEqual(result, self.call('task.submit', task))
            handoff = self.call('session.handoff', {'session_id': session['session_id']})
            self.assertEqual(['task-1'], handoff['source_ids'])
            self.assertEqual(self.context, handoff['context'])
        self.assertEqual(self.catalog, self.call('discover', {}))

    def test_changed_replay_conflicts(self):
        self.call('task.submit', self.task())
        task = self.task()
        task['inputs']['QUERY'] = 'changed'
        self.assert_error('context_conflict', self.call, 'task.submit', task)

    def test_stale_context_denied(self):
        self.service.register('example', 'local', 'b' * 64)
        result = self.call('task.submit', self.task())
        self.assertEqual('context_conflict', result['error']['code'])
        self.assertIsNone(result['started_at'])

    def test_stale_workflow_denied(self):
        task = self.task()
        task['workflow_digest'] = 'c' * 64
        self.assertEqual('context_conflict', self.call('task.submit', task)['error']['code'])

    def test_unknown_fields_version_ids_types(self):
        for key, value in [('command', 'touch /tmp/escape'), ('principal', 'admin'), ('env', {'PATH': '/evil'})]:
            task = self.task()
            task[key] = value
            self.assert_error('invalid_input', self.call, 'task.submit', task)
        task = self.task()
        task['schema_version'] = '2.0'
        self.assert_error('unsupported_version', self.call, 'task.submit', task)
        for key, value in [('skill', '../../tmp'), ('expected_context_revision', True)]:
            task = self.task()
            task[key] = value
            self.assert_error('invalid_input', self.call, 'task.submit', task)

    def test_typed_input_and_no_shell_interpolation(self):
        task = self.task()
        task['inputs'] = {'QUERY': '$(touch /tmp/bh-escape); `id` ${HOME}'}
        result = self.call('task.submit', task)
        self.assertEqual('succeeded', result['status'])
        data = self.service.db.execute('SELECT data FROM artifacts').fetchone()[0]
        self.assertEqual([], json.loads(data))
        for index, inputs in enumerate([{'QUERY': 1}, {'PATH': '/tmp'}, {'QUERY': 'x' * 257}, {'QUERY': ''}]):
            task = self.task()
            task.update(task_id=f'bad-{index}', idempotency_key=f'bad-{index}', inputs=inputs)
            self.assertEqual('invalid_input', self.call('task.submit', task)['error']['code'])

    def test_active_workflow_denied_even_with_grant(self):
        task = self.task(approval_ref='operator-grant')
        entry = next(e for e in self.catalog['workflows'] if e['skill'] == 'recon')
        task.update(skill=entry['skill'], workflow=entry['workflow'], workflow_digest=entry['workflow_digest'])
        with patch('tools.agent_interface.service.execute', side_effect=AssertionError('Must not execute')):
            result = self.call('task.submit', task)
        self.assertEqual('denied', result['status'])
        self.assertEqual('unsupported_capability', result['error']['code'])

    def test_program_and_principal_boundaries(self):
        self.service.register('other', 'local', 'b' * 64)
        self.assert_error('scope_denial', self.call, 'session.open', dict(program_id='other', campaign_id='local',
                          provider='codex', client_version='test', predecessor_session_id=self.session['session_id']))
        other = Service(self.database, 'uid:other')
        self.addCleanup(other.db.close)
        self.assert_error('scope_denial', other.session, self.session['session_id'])
        self.assert_error('scope_denial', other.register, 'example', 'local', 'b' * 64)
        task = self.task()
        task['program_id'] = 'other'
        self.assert_error('scope_denial', self.call, 'task.submit', task)

    def test_interruption_and_replay_no_rerun(self):
        with patch('tools.agent_interface.service.execute', side_effect=KeyboardInterrupt):
            result = self.call('task.submit', self.task())
        self.assertEqual('interrupted', result['status'])
        with patch('tools.agent_interface.service.execute', side_effect=AssertionError('No retry')):
            self.assertEqual(result, self.call('task.submit', self.task()))

    def test_safe_diagnostics(self):
        secret = 'Bearer synthetic-secret-do-not-log'
        with patch('tools.agent_interface.service.execute', side_effect=RuntimeError(secret)):
            result = self.call('task.submit', self.task())
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual('internal_failure', result['error']['code'])

    def test_cancel_terminal_and_running(self):
        result = self.call('task.submit', self.task())
        params = {'session_id': self.session['session_id'], 'task_id': 'task-1'}
        self.assertEqual(result, self.call('task.cancel', params))
        result.update(status='running', ended_at=None)
        with self.service.db:
            self.service.save_result(result)
        self.assertEqual('cancelled', self.call('task.cancel', params)['status'])
        self.assertEqual('cancelled', self.call('task.submit', self.task())['status'])

    def test_bound_json_duplicate_fields_and_unsupported(self):
        for raw in (b'{"a":1,"a":2}', b'[' * 20 + b'0' + b']' * 20, b' ' * 65537, b'{"x":NaN}'):
            self.assert_error('invalid_input', decode, raw)
        for op in ('human.confirm', 'report.submit', 'grant.issue', 'shell'):
            self.assert_error('unsupported_capability', self.call, op, {})

    def test_cli_json_and_error_exit(self):
        command = [sys.executable, str(ROOT / 'bin/bb-agent'), '--database', str(self.database)]
        result = subprocess.run(command, input=json.dumps({'schema_version': '1.0', 'operation': 'capabilities', 'params': {}}),
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(json.loads(result.stdout)['isolation_available'])
        result = subprocess.run(command, input='{"bad":1}', text=True, capture_output=True)
        self.assertEqual(2, result.returncode)
        self.assertEqual('', result.stderr)


if __name__ == '__main__':
    unittest.main()
