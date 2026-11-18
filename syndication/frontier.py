"""Next-fetch scheduling for every source record, including retired ones.

Each source record keeps its own target, keyed by its own object id.  That way a
reused source identifier produces a new target without overwriting the target
of the retired record, so the earlier collection basis is never dropped.

The fetch URL follows a redirect only when that redirect was confirmed to land
on a specific notice; a redirect to a channel index never rewrites a target
along with every other notice.
"""

import json
import sqlite3
from urllib.parse import urlparse

SCHEMA = '''CREATE TABLE IF NOT EXISTS targets(
    source TEXT, source_id TEXT, object_id TEXT, url TEXT, due INTEGER,
    urls TEXT, withdrawn INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
    retired INTEGER DEFAULT 0, PRIMARY KEY(source,source_id,object_id))'''


class Frontier:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Rebuild the old (source, source_id) primary key without losing rows."""
        columns = {row['name']: row for row in self.db.execute('PRAGMA table_info(targets)')}
        primary = [name for name, row in columns.items() if row['pk']]
        if primary and primary != ['source', 'source_id', 'object_id']:
            self.db.executescript('''
                ALTER TABLE targets RENAME TO targets_old;
                ''' + SCHEMA + ''';
                INSERT OR IGNORE INTO targets(source,source_id,object_id,url,due,urls,
                                              withdrawn,updated,retired)
                SELECT source, source_id, object_id, url, due, '[]', 0, 0, 0
                FROM targets_old;
                DROP TABLE targets_old;
            ''')
            columns = {row['name']: row for row in
                       self.db.execute('PRAGMA table_info(targets)')}
        for name, declaration in {'urls': 'TEXT',
                                  'withdrawn': 'INTEGER DEFAULT 0',
                                  'updated': 'INTEGER DEFAULT 0',
                                  'retired': 'INTEGER DEFAULT 0'}.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE targets ADD COLUMN {name} {declaration}')

    def schedule(self, item, object_id, now, record=None):
        record = record or {}
        url = item['url']
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('unsupported source URL')
        withdrawn = bool(item.get('withdrawn', record.get('withdrawn')))
        due = now + (86400 if withdrawn else 3600)
        urls = []
        for candidate in [*(record.get('urls') or []), url]:
            if candidate and candidate not in urls:
                urls.append(candidate)
        existing = self.db.execute(
            'SELECT * FROM targets WHERE source=? AND source_id=? AND object_id=?',
            (item['source'], item['id'], object_id)).fetchone()
        if existing:
            for candidate in json.loads(existing['urls'] or '[]'):
                if candidate not in urls:
                    urls.append(candidate)
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?,?,?,?,?)',
                (item['source'], item['id'], object_id, url, due,
                 json.dumps(urls), int(withdrawn), now, 0))

    def retire_stale(self, current):
        """Mark targets whose record incarnation is no longer current."""
        keep = {(source, source_id) for source, source_id in current}
        with self.db:
            for row in self.db.execute('SELECT * FROM targets WHERE retired=0').fetchall():
                if (row['source'], row['source_id']) not in keep:
                    self.db.execute(
                        'UPDATE targets SET retired=1 WHERE source=? AND source_id=? '
                        'AND object_id=?',
                        (row['source'], row['source_id'], row['object_id']))

    def rows(self):
        result = []
        for row in self.db.execute(
                'SELECT * FROM targets ORDER BY source,source_id,retired,object_id'):
            record = dict(row)
            record['urls'] = json.loads(record.get('urls') or '[]')
            record['retired'] = bool(record['retired'])
            result.append(record)
        return result

    def apply_resolver(self, resolver):
        """Point targets at confirmed final addresses; keep original history."""
        changed = []
        with self.db:
            for row in self.db.execute('SELECT * FROM targets').fetchall():
                resolved = resolver.current_url(row['url'])
                if resolved and resolved != row['url']:
                    self.db.execute(
                        'UPDATE targets SET url=? WHERE source=? AND source_id=? '
                        'AND object_id=?',
                        (resolved, row['source'], row['source_id'], row['object_id']))
                    changed.append({'source': row['source'], 'source_id': row['source_id'],
                                    'from_url': row['url'], 'url': resolved,
                                    'object_id': row['object_id']})
        return changed
