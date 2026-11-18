import sqlite3
from urllib.parse import urlparse


class Frontier:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS targets(
            source TEXT, source_id TEXT, object_id TEXT, url TEXT, due INTEGER,
            PRIMARY KEY(source,source_id))''')

    def schedule(self, item, object_id, now):
        parsed = urlparse(item['url'])
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('unsupported source URL')
        due = now + (3600 if not item.get('withdrawn') else 86400)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?)',
                            (item['source'], item['id'], object_id, item['url'], due))

    def rows(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM targets ORDER BY source,source_id')]
