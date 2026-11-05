"""Campaign-owned snapshots and transactional execution leases; no network logic."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Recovery:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.db = sqlite3.connect(self.directory / 'execution.sqlite', timeout=10)
        (self.directory / 'execution.sqlite').chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS runs (task TEXT PRIMARY KEY, digest TEXT, status TEXT, owner TEXT, heartbeat REAL, expires REAL, exit_code INTEGER)')
        self.owner = uuid.uuid4().hex

    def bind(self, context, target, program, scope_digest, policy_digest, resume=False):
        snapshot = self.directory / 'context.snapshot.json'
        identity = {'target': target, 'program': program, 'scope_digest': scope_digest, 'policy_digest': policy_digest}
        if snapshot.exists():
            saved = json.loads(snapshot.read_text())
            if saved['identity'] != identity:
                raise ValueError('Campaign target/program/scope/policy changed; reconcile before resuming')
            return saved['context']
        if resume:
            raise ValueError('Legacy campaign has no context snapshot; explicit migration is required')
        atomic_json(snapshot, {'identity': identity, 'context': context})
        return context

    def claim(self, task, digest, ttl=30):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM runs WHERE task=?', (task,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError('Workflow changed since original execution')
                if row['status'] == 'running' and row['expires'] < time.time():
                    self.db.execute("UPDATE runs SET status='interrupted' WHERE task=?", (task,))
                    self.db.commit()
                    raise ValueError('Remote outcome unknown; reconcile interrupted task before continuing')
                self.db.commit()
                if row['status'] == 'succeeded':
                    return False
                raise ValueError('Task already claimed, failed, or interrupted; automatic retry prohibited')
            stamp = time.time()
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,NULL)', (task, digest, 'running', self.owner, stamp, stamp + ttl))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def heartbeat(self, task, ttl=30):
        with self.db:
            changed = self.db.execute("UPDATE runs SET heartbeat=?,expires=? WHERE task=? AND owner=? AND status='running'",
                                      (time.time(), time.time() + ttl, task, self.owner)).rowcount
            if not changed:
                raise ValueError('Execution lease lost')

    def finish(self, task, code):
        with self.db:
            changed = self.db.execute("UPDATE runs SET status=?,exit_code=? WHERE task=? AND owner=? AND status='running'",
                                      ('succeeded' if code == 0 else 'failed', code, task, self.owner)).rowcount
            if not changed:
                raise ValueError('Execution lease lost')

    def reconcile(self, task, succeeded):
        with self.db:
            row = self.db.execute('SELECT * FROM runs WHERE task=?', (task,)).fetchone()
            if not row or (row['status'] == 'running' and row['expires'] >= time.time()):
                raise ValueError('Missing task or live lease')
            self.db.execute('UPDATE runs SET status=?,exit_code=? WHERE task=?',
                            ('succeeded' if succeeded else 'failed', 0 if succeeded else 1, task))
            atomic_json(self.directory / ('reconciliation-' + uuid.uuid4().hex + '.json'),
                        {'task': task, 'succeeded': succeeded, 'actor_uid': os.getuid(), 'at': time.time()})


if __name__ == '__main__':
    import argparse
    argparse.ArgumentParser(description=__doc__, epilog='Invoked by campaign_runner.py; use its reconcile command for operator recovery.').parse_args()
