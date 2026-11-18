import json
import sqlite3
import uuid


class IdentityBook:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS source_records(
                source TEXT, source_id TEXT, object_id TEXT, revision INTEGER,
                current TEXT, PRIMARY KEY(source,source_id));
            CREATE TABLE IF NOT EXISTS observations(
                source TEXT, source_id TEXT, revision INTEGER, payload TEXT,
                PRIMARY KEY(source,source_id,revision));
        ''')

    def attach(self, item):
        payload = json.dumps(item, sort_keys=True, ensure_ascii=False)
        with self.db:
            old = self.db.execute('SELECT * FROM source_records WHERE source=? AND source_id=?',
                                  (item['source'], item['id'])).fetchone()
            identity = old['object_id'] if old else str(uuid.uuid4())
            previous = self.db.execute('SELECT payload FROM observations WHERE source=? AND source_id=? AND revision=?',
                                       (item['source'], item['id'], item['revision'])).fetchone()
            if previous and previous[0] != payload:
                raise ValueError('conflicting source revision')
            self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',
                            (item['source'], item['id'], item['revision'], payload))
            if old is None or item['revision'] > old['revision']:
                self.db.execute('INSERT OR REPLACE INTO source_records VALUES(?,?,?,?,?)',
                                (item['source'], item['id'], identity, item['revision'], payload))
        return identity

    def rows(self):
        return [{**dict(row), 'current': json.loads(row['current'])}
                for row in self.db.execute('SELECT * FROM source_records ORDER BY source,source_id')]
