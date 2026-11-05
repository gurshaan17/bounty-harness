"""Versioned research events and projections in the existing facts database."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import uuid
import re
from contextlib import closing
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('legacy_memory', Path(__file__).with_name('memory_store.py'))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
KINDS = {'program', 'asset', 'component', 'technology', 'role', 'functionality', 'observation',
         'hypothesis', 'investigation', 'negative_result', 'coverage', 'evidence', 'question', 'queue_task', 'decision'}


class RevisionConflict(ValueError):
    pass


def scan(value, depth=0):
    if depth > 12:
        raise ValueError('Research nesting limit exceeded')
    if isinstance(value, str):
        if len(value) > 8192 or legacy._check_secrets(value) or re.search(r'(?i)(?:authorization|cookie|set-cookie|token|api[_-]?key)\s*[:=]\s*\S+', value):
            raise ValueError('Research contains a secret or oversized string')
    elif isinstance(value, dict):
        for key, item in value.items():
            scan(key, depth + 1)
            scan(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            scan(item, depth + 1)


def backup(source, destination):
    if Path(destination).exists() or Path(destination).is_symlink():
        raise ValueError('Backup destination already exists')
    if not Path(source).is_file() or Path(source).is_symlink():
        raise ValueError('Invalid backup source')
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
    Path(destination).chmod(0o600)


class ResearchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Memory symlinks are unsupported')
        if self.path.exists():
            with closing(sqlite3.connect(path)) as existing:
                tables = {r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                version = existing.execute('SELECT max(version) FROM memory_schema').fetchone()[0] if 'memory_schema' in tables else 0
                if (version or 0) > 2:
                    raise ValueError('Unsupported memory schema')
            if (version or 0) < 2:
                backup(path, self.path.with_name(self.path.name + '.pre-v2-' + uuid.uuid4().hex + '.sqlite'))
        legacy.init_memory('migration', str(path))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER PRIMARY KEY)')
            version = self.db.execute('SELECT max(version) FROM memory_schema').fetchone()[0] or 0
            if version > 2:
                raise ValueError('Unsupported memory schema')
            if version == 0:
                self.db.execute('CREATE TABLE research_events(id TEXT PRIMARY KEY, program TEXT, entity TEXT, revision INTEGER, data TEXT, UNIQUE(program,entity,revision))')
                self.db.execute('CREATE TABLE research_entities(program TEXT, id TEXT, revision INTEGER, data TEXT, PRIMARY KEY(program,id))')
                # Keep legacy rows/IDs/correction links intact; import as unreviewed provenance.
                for row in self.db.execute('SELECT * FROM facts').fetchall():
                    fact = dict(row)
                    record = {'id': fact['fact_id'], 'kind': 'negative_result' if fact['category'] == 'tested_clean' else 'observation',
                              'content': fact['content'], 'sensitivity': fact['sensitivity'], 'confidence': fact['confidence'],
                              'trust': 'legacy-unreviewed', 'sources': [], 'legacy': fact, 'revision': 1}
                    self._write(fact['program'], record, {'actor': 'migration'}, 'import', '')
                self.db.execute('INSERT INTO memory_schema VALUES(1)')
            if version < 2:
                for table in ('research_events', 'facts'):
                    for operation in ('UPDATE', 'DELETE'):
                        self.db.execute(f"CREATE TRIGGER IF NOT EXISTS protect_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable migrated history'); END")
                self.db.execute("CREATE TRIGGER IF NOT EXISTS protect_facts_INSERT BEFORE INSERT ON facts BEGIN SELECT RAISE(ABORT, 'use migrated research API'); END")
                self.db.execute('INSERT INTO memory_schema VALUES(2)')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def _write(self, program, record, provenance, action, reason):
        event = {'id': str(uuid.uuid4()), 'program_id': program, 'entity_id': record['id'],
                 'revision': record['revision'], 'source_ids': record.get('sources', []),
                 'artifact_refs': record.get('artifacts', []), 'confidence': record.get('confidence', 'medium'),
                 'sensitivity': record['sensitivity'], 'trust': record['trust'], 'type': action, 'reason': reason,
                 'timestamp': datetime.now(timezone.utc).isoformat(), 'provenance': provenance, 'record': record}
        self.db.execute('INSERT INTO research_events VALUES(?,?,?,?,?)',
                        (event['id'], program, record['id'], record['revision'], json.dumps(event)))
        self.db.execute('INSERT OR REPLACE INTO research_entities VALUES(?,?,?,?)',
                        (program, record['id'], record['revision'], json.dumps(record)))
        return record

    def append(self, program, record, provenance):
        scan(program)
        scan(record)
        scan(provenance)
        if set(record) - {'kind', 'content', 'sensitivity', 'confidence', 'sources', 'expires_at', 'details', 'artifacts', 'basis', 'external_refs'}:
            raise ValueError('Unknown research field')
        if record.get('kind') not in KINDS or not isinstance(record.get('content'), str) or not record['content'].strip():
            raise ValueError('Kind and nonempty content required')
        if record.get('sensitivity', 'program-private') not in legacy.VALID_SENSITIVITY:
            raise ValueError('Invalid sensitivity')
        if record.get('confidence', 'medium') not in legacy.VALID_CONFIDENCE:
            raise ValueError('Invalid confidence')
        if record.get('basis', 'inference') not in ('observation', 'inference'):
            raise ValueError('Human conclusions require operator review authority')
        refs = record.get('external_refs', [])
        if not isinstance(refs, list) or len(refs) > 32:
            raise ValueError('Invalid external references')
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {'system', 'id'} or ref['system'] not in ('asset-graph', 'traffic-corpus', 'coverage') or not isinstance(ref['id'], str):
                raise ValueError('External references must name a supported domain store and opaque ID')
        expiry = record.get('expires_at')
        if expiry is not None:
            if not isinstance(expiry, str) or datetime.fromisoformat(expiry).tzinfo is None:
                raise ValueError('Expiry must include timezone')
        details = record.get('details', {})
        if not isinstance(details, dict) or any(not isinstance(k, str) or not isinstance(v, (str, list, dict, bool, int)) for k, v in details.items()):
            raise ValueError('Invalid entity details')
        artifacts = record.get('artifacts', [])
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            raise ValueError('Invalid artifact references')
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {'id', 'sha256'} or not isinstance(artifact['id'], str) or not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
                raise ValueError('Invalid artifact reference')
        sources = record.get('sources', [])
        if not isinstance(sources, list) or len(sources) > 32:
            raise ValueError('Invalid sources')
        source_revisions = {}
        sensitivity = record.get("sensitivity", "program-private")
        rank = {"report-safe": 0, "program-private": 1, "high-sensitivity": 2}
        for source in sources:
            if not isinstance(source, str) or not self.get(program, source):
                raise ValueError('Source must reference an existing entity in this program')
            origin = self.get(program, source)
            source_revisions[source] = origin['revision']
            if rank[origin['sensitivity']] > rank[sensitivity]:
                sensitivity = origin['sensitivity']
        record = {**record, 'id': str(uuid.uuid4()), 'revision': 1, 'trust': 'agent-unreviewed',
                  'sensitivity': sensitivity, 'source_revisions': source_revisions, 'confidence': record.get('confidence', 'medium'),
                  'basis': record.get('basis', 'inference'), 'sources': sources}
        with self.db:
            return self._write(program, record, provenance, 'append', '')

    def get(self, program, entity):
        row = self.db.execute('SELECT data FROM research_entities WHERE program=? AND id=?', (program, entity)).fetchone()
        return json.loads(row[0]) if row else None

    def correct(self, program, entity, revision, content, reason, provenance):
        scan({'content': content, 'reason': reason, 'provenance': provenance})
        if type(revision) is not int or not isinstance(content, str) or not content.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError('Revision, content and correction reason required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.get(program, entity)
            if not old or old['revision'] != revision:
                raise RevisionConflict('Missing entity or stale revision')
            if old.get('trust') == 'legacy-unreviewed':
                # Retain original metadata in history, not in the corrected projection.
                old = {k: v for k, v in old.items() if k != 'legacy'}
            record = {**old, 'revision': revision + 1, 'content': content, 'supersedes_revision': revision,
                      'trust': 'agent-unreviewed'}
            result = self._write(program, record, provenance, 'correct', reason)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def query(self, program, query='', kind=None, history=None):
        if not isinstance(query, str) or (kind is not None and kind not in KINDS):
            raise ValueError('Invalid query or kind')
        if history:
            rows = self.db.execute('SELECT data FROM research_events WHERE program=? AND entity=? ORDER BY revision', (program, history))
        else:
            rows = self.db.execute('SELECT data FROM research_entities WHERE program=? ORDER BY id', (program,))
        results = []
        for row in rows:
            record = json.loads(row[0])
            try:
                scan(record)
            except ValueError:
                continue  # Legacy secrets never reach a retrieval/export surface.
            if record.get('sensitivity') == 'high-sensitivity' or record.get('record', {}).get('sensitivity') == 'high-sensitivity':
                continue
            if query.lower() in json.dumps(record).lower() and (not kind or record.get('kind') == kind):
                results.append(record)
            if len(results) == 50:
                break
        return results

    def summary(self, program):
        records = self.query(program)
        stamp = datetime.now(timezone.utc)
        expired = [r['id'] for r in records if r.get('expires_at') and datetime.fromisoformat(r['expires_at']) <= stamp]
        return {'available': True, 'trust': 'derived', 'records': records[:20],
                'source_ids': [r['id'] for r in records[:20]], 'expired_review': expired,
                'truncated_to': 20, 'scope_authority': False}

    def contradictions(self, program):
        records = self.query(program)
        pairs = []
        for index, first in enumerate(records):
            for second in records[index + 1:]:
                if first['kind'] == second['kind'] and bool(re.search(r'\b(no|not|never|absent)\b', first['content'])) != bool(re.search(r'\b(no|not|never|absent)\b', second['content'])):
                    pairs.append({'source_ids': [first['id'], second['id']], 'status': 'needs-review'})
        return pairs[:50]

    def export(self, program):
        return [record for record in self.query(program) if record['sensitivity'] == 'report-safe']


if __name__ == '__main__':
    import argparse
    argparse.ArgumentParser(description=__doc__, epilog='Use research_admin.py for operator migration/backup; bin/bb-agent for research operations.').parse_args()
