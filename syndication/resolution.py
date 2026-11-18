"""Persistent address resolution.

A source may move a notice to a new address and redirect the old one.  Only a
resolved redirect whose final address is a specific record is a confirmed
migration.  Redirects that end on a channel index (or on another redirect
loop) are recorded as unresolved and are never used as association evidence.
"""

import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

MAX_HOPS = 8


def _validate(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('unsupported source URL')
    return url


def address_key(url):
    parsed = urlparse(url)
    path = parsed.path or '/'
    if path != '/' and path.endswith('/'):
        path = path[:-1]
    return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}'


CHANNEL_SEGMENTS = frozenset({
    'channel', 'channels', 'index', 'home', 'homepage', 'list', 'category',
    'categories', 'news', 'feed', 'feeds', 'archive', 'archives', 'default',
})


def _is_index(url, known_index_urls):
    """A channel index identifies no single notice.

    Only unambiguous shapes count: the site root, a trailing slash, a declared
    channel address, or a generic channel segment.  An opaque path such as
    ``/1`` is left for convergence detection to judge.
    """
    parsed = urlparse(url)
    path = parsed.path or '/'
    if path == '/' or path.endswith('/'):
        return True
    if url in known_index_urls:
        return True
    segments = [segment for segment in path.split('/') if segment]
    if segments and segments[-1].lower() in CHANNEL_SEGMENTS:
        return True
    return False


class Resolver:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS resolutions(
                from_url TEXT PRIMARY KEY, final_url TEXT, state TEXT,
                chain TEXT, confirmed_by TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS source_aliases(
                source TEXT, source_id TEXT, url TEXT, note TEXT,
                PRIMARY KEY(source,source_id,url));
        ''')

    # -- observation -------------------------------------------------------
    def resolve(self, url, known_index_urls=()):
        """Follow redirects outside the database; never guesses a target."""
        current = _validate(url)
        chain = [current]
        for _ in range(MAX_HOPS):
            final = self._once(current)
            if final is None:
                return {'from_url': url, 'final_url': chain[-1], 'state': 'unresolved',
                        'chain': chain, 'reason': 'too many redirects'}
            if final == current:
                if len(chain) == 1:
                    return {'from_url': url, 'final_url': current, 'state': 'direct',
                            'chain': chain}
                if _is_index(current, known_index_urls):
                    return {'from_url': url, 'final_url': current, 'state': 'index',
                            'chain': chain}
                return {'from_url': url, 'final_url': current, 'state': 'resolved',
                        'chain': chain}
            if _is_index(final, known_index_urls):
                chain.append(final)
                return {'from_url': url, 'final_url': final, 'state': 'index',
                        'chain': chain}
            chain.append(final)
            current = final
        return {'from_url': url, 'final_url': chain[-1], 'state': 'unresolved',
                'chain': chain, 'reason': 'too many redirects'}

    @staticmethod
    def _once(url):
        request = Request(url, method='GET', headers={'User-Agent': 'syndication-map/1'})
        try:
            with urlopen(request, timeout=5) as response:
                final = response.geturl()
                response.read(0)
                return final
        except HTTPError as error:
            return getattr(error, 'url', None) or url
        except URLError:
            return None

    # -- persistence -------------------------------------------------------
    def record(self, resolution, confirmed_by='redirect'):
        final = resolution.get('final_url')
        state = resolution.get('state', 'unresolved')
        if state == 'index':
            confirmed_by = 'index_redirect'
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO resolutions VALUES(?,?,?,?,?,?)',
                (resolution['from_url'], final, state,
                 ' -> '.join(resolution.get('chain') or []), confirmed_by,
                 resolution.get('reason')))

    def confirmed(self):
        return {address_key(row['final_url']): dict(row)
                for row in self.db.execute(
                    "SELECT * FROM resolutions WHERE state='resolved'")}

    def failures(self):
        return {address_key(row['from_url']): dict(row)
                for row in self.db.execute(
                    "SELECT * FROM resolutions WHERE state IN ('index','unresolved')")}

    def all(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM resolutions ORDER BY from_url')]

    def latest_for(self, url):
        row = self.db.execute('SELECT * FROM resolutions WHERE from_url=?', (url,)).fetchone()
        return dict(row) if row else None

    def record_human(self, source, source_id, url, note):
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO source_aliases VALUES(?,?,?,?)',
                (source, source_id, url, note))
        return {'source': source, 'source_id': source_id, 'url': url,
                'state': 'resolved', 'confirmed_by': 'human', 'note': note}

    def human_aliases(self):
        """Human-confirmed address declarations, independent of record storage."""
        result = {}
        for row in self.db.execute('SELECT * FROM source_aliases'):
            result.setdefault(address_key(row['url']), []).append(
                {'source': row['source'], 'source_id': row['source_id'],
                 'url': row['url'], 'note': row['note']})
        return result

    def expand_keys(self, keys):
        """Expand address keys through confirmed, non-index redirects."""
        index = {address_key(row['from_url']): row for row in
                 self.db.execute("SELECT * FROM resolutions WHERE state='resolved'")}
        expanded = set(keys)
        changed = True
        while changed:
            changed = False
            for source_key, row in index.items():
                if row['final_url'] and address_key(row['final_url']) in expanded and source_key not in expanded:
                    expanded.add(source_key)
                    changed = True
        return expanded

    def current_url(self, url):
        """Latest fetch location for an address, following confirmed hops."""
        index = {address_key(row['from_url']): row for row in
                 self.db.execute("SELECT * FROM resolutions WHERE state='resolved'")}
        current = url
        seen = {address_key(url)}
        while address_key(current) in index:
            final = index[address_key(current)]['final_url']
            if not final or address_key(final) in seen:
                break
            current = final
            seen.add(address_key(current))
        return current
