"""Source-record identity and cross-source association.

A source record is always stored under its own immutable ``object_id``.  Two
records belonging to the same published notice are joined through an explicit
association graph instead of being collapsed or deleted.  That keeps every
partner's wording, revision history and fetch target intact, and lets a human
split a mistaken association without losing evidence.

Automatic association is deliberately conservative: a canonical reference
declared by a record is hard evidence; a combination of very similar body text
plus at least one independent signal is strong evidence.  Weaker matches are
only proposals.  The same title, the same template or the same bare address is
never sufficient.
"""

import json
import sqlite3
import uuid

from .resolution import address_key
from .similarity import body_text, canonical_urls, compare, normalize

def _pair(left, right):
    return '\x1f'.join(sorted((left, right)))


def _core_payload(item):
    """Payload without fields tracked separately from the published content.

    ``withdrawn`` is a status that can change without a revision, and ``url`` is
    an address that may migrate while the notice itself stays the same.
    """
    return json.dumps({key: value for key, value in item.items()
                       if key not in ('withdrawn', 'url')},
                      sort_keys=True, ensure_ascii=False)


def _payload(item):
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


class IdentityBook:
    def __init__(self, path, resolver=None):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.resolver = resolver
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS source_records(
                source TEXT, source_id TEXT, object_id TEXT, revision INTEGER,
                current TEXT, urls TEXT, incarnation INTEGER, fetch_url TEXT,
                withdrawn INTEGER DEFAULT 0, withdrawal_reason TEXT,
                first_seen INTEGER DEFAULT 0, last_seen INTEGER DEFAULT 0,
                PRIMARY KEY(source,source_id));
            CREATE TABLE IF NOT EXISTS observations(
                source TEXT, source_id TEXT, revision INTEGER, payload TEXT,
                incarnation INTEGER DEFAULT 1,
                PRIMARY KEY(source,source_id,incarnation,revision));
            CREATE TABLE IF NOT EXISTS retired_records(
                source TEXT, source_id TEXT, object_id TEXT, revision INTEGER,
                current TEXT, urls TEXT, incarnation INTEGER, retired_at INTEGER,
                reason TEXT, PRIMARY KEY(source,source_id,incarnation));
            CREATE TABLE IF NOT EXISTS objects(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT UNIQUE,
                created INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS object_links(
                pair TEXT PRIMARY KEY, left_object TEXT, right_object TEXT,
                decision TEXT, score REAL, evidence TEXT, active INTEGER DEFAULT 1,
                created INTEGER DEFAULT 0, updated INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS association_proposals(
                pair TEXT PRIMARY KEY, left_object TEXT, right_object TEXT,
                decision TEXT, score REAL, evidence TEXT, blocked TEXT,
                created INTEGER DEFAULT 0, updated INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS split_exclusions(
                pair TEXT PRIMARY KEY, left_object TEXT, right_object TEXT,
                detail TEXT, created INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS association_events(
                event INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT,
                detail TEXT, created INTEGER DEFAULT 0);
        ''')
        self._migrate()

    def _migrate(self):
        """Allow opening databases created by the previous schema."""
        self._add_missing('source_records', {
            'urls': "TEXT", 'incarnation': 'INTEGER DEFAULT 1',
            'fetch_url': 'TEXT', 'withdrawn': 'INTEGER DEFAULT 0',
            'withdrawal_reason': 'TEXT', 'first_seen': 'INTEGER DEFAULT 0',
            'last_seen': 'INTEGER DEFAULT 0'})
        self._add_missing('observations', {'incarnation': 'INTEGER DEFAULT 1'})
        self._backfill_addresses()

    def _backfill_addresses(self):
        """Old rows have no address column; recover it from the stored payload."""
        for row in self.db.execute(
                "SELECT source, source_id, current, urls, fetch_url FROM source_records "
                "WHERE urls IS NULL OR urls='' OR urls='[]'").fetchall():
            url = (json.loads(row['current']) or {}).get('url')
            if not url:
                continue
            self.db.execute(
                'UPDATE source_records SET urls=?, fetch_url=COALESCE(fetch_url, ?) '
                'WHERE source=? AND source_id=?',
                (json.dumps([url]), url, row['source'], row['source_id']))
        self.db.commit()

    def _add_missing(self, table, columns):
        existing = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
        for name, declaration in columns.items():
            if name not in existing:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        self.db.commit()

    # -- records -----------------------------------------------------------
    def attach(self, item, now=0):
        """Store one feed observation and return the record's own object id."""
        source, source_id = item['source'], item['id']
        url = item.get('url', '')
        payload = _payload(item)
        core = _core_payload(item)
        with self.db:
            old = self.db.execute(
                'SELECT * FROM source_records WHERE source=? AND source_id=?',
                (source, source_id)).fetchone()
            if old is None:
                object_id = self._new_object(now)
                self.db.execute(
                    'INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (source, source_id, object_id, item['revision'], payload,
                     json.dumps([url]), 1, url, int(bool(item.get('withdrawn'))),
                     'feed' if item.get('withdrawn') else None, now, now))
                self.db.execute(
                    'INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                    (source, source_id, item['revision'], payload, 1))
                return object_id

            previous = self.db.execute(
                'SELECT payload FROM observations WHERE source=? AND source_id=? '
                'AND incarnation=? AND revision=?',
                (source, source_id, old['incarnation'], item['revision'])).fetchone()
            if previous and previous['payload'] != payload:
                if self._is_reuse(old, item):
                    return self._replace_incarnation(old, item, now)
                raise ValueError('conflicting source revision')

            if item['revision'] < old['revision']:
                if self._is_reuse(old, item):
                    return self._replace_incarnation(old, item, now)
                self.db.execute(
                    'INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                    (source, source_id, item['revision'], payload, old['incarnation']))
                self._merge_addresses(old, url)
                return old['object_id']

            if item['revision'] == old['revision']:
                if _core_payload(json.loads(old['current'])) != core:
                    if not self._is_reuse(old, item):
                        raise ValueError('conflicting source revision')
                    return self._replace_incarnation(old, item, now)
                self._merge_addresses(old, url)
                self._apply_status(old, item)
                return old['object_id']

            if self._is_reuse(old, item):
                return self._replace_incarnation(old, item, now)

            urls = self._merged_urls(old, url)
            fetch_url = old['fetch_url'] or url
            self.db.execute(
                'UPDATE source_records SET revision=?, current=?, urls=?, fetch_url=?, '
                'last_seen=? WHERE source=? AND source_id=?',
                (item['revision'], payload, json.dumps(urls), fetch_url, now,
                 source, source_id))
            self.db.execute(
                'INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)',
                (source, source_id, item['revision'], payload, old['incarnation']))
            self._apply_status(old, item)
            return old['object_id']

    def _new_object(self, now):
        object_id = str(uuid.uuid4())
        self.db.execute('INSERT INTO objects(object_id, created) VALUES(?,?)',
                        (object_id, now))
        return object_id

    def _merged_urls(self, old, url):
        urls = json.loads(old['urls'] or '[]')
        if url and url not in urls:
            urls.append(url)
        return urls

    def _merge_addresses(self, old, url):
        urls = self._merged_urls(old, url)
        if json.dumps(urls) != (old['urls'] or '[]'):
            self.db.execute(
                'UPDATE source_records SET urls=? WHERE source=? AND source_id=?',
                (json.dumps(urls), old['source'], old['source_id']))

    def _apply_status(self, old, item):
        if 'withdrawn' not in item:
            return
        withdrawn = int(bool(item['withdrawn']))
        self.db.execute(
            'UPDATE source_records SET withdrawn=?, withdrawal_reason=? '
            'WHERE source=? AND source_id=?',
            (withdrawn, 'feed' if withdrawn else None,
             old['source'], old['source_id']))

    def _is_reuse(self, old, item):
        """True when a source reuses an identifier for an unrelated notice.

        A lower revision is not enough on its own, because feeds replay history
        and partner sites reuse a shared template.  Reuse requires either a
        contradicting issuer/date signal or a body that is plainly not the same
        notice, and never applies to a known address of the existing record.
        """
        current = json.loads(old['current'])
        old_urls = json.loads(old['urls'] or '[]')
        new_url = item.get('url', '')
        if new_url and new_url in old_urls:
            return False
        if self.resolver is not None and new_url:
            expanded = self.resolver.expand_keys({address_key(url) for url in old_urls})
            if address_key(new_url) in expanded:
                return False
        evidence = compare(current, item, old_urls, [new_url])
        if evidence['decision'] == 'hard':
            return False
        if evidence['contradiction']:
            return True
        if evidence['body_similarity'] >= 0.6:
            return False
        if evidence['title_similarity'] >= 0.9:
            return False
        return bool(evidence['body_similarity'] or old_urls)

    def _replace_incarnation(self, old, item, now):
        self.db.execute(
            'INSERT OR REPLACE INTO retired_records VALUES(?,?,?,?,?,?,?,?,?)',
            (old['source'], old['source_id'], old['object_id'], old['revision'],
             old['current'], old['urls'], old['incarnation'], now,
             'identifier_reuse'))
        object_id = self._new_object(now)
        incarnation = old['incarnation'] + 1
        url = item.get('url', '')
        payload = _payload(item)
        self.db.execute(
            'INSERT OR REPLACE INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (old['source'], old['source_id'], object_id, item['revision'], payload,
             json.dumps([url]), incarnation, url,
             int(bool(item.get('withdrawn'))),
             'feed' if item.get('withdrawn') else None, now, now))
        self.db.execute(
            'INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)',
            (old['source'], old['source_id'], item['revision'], payload, incarnation))
        self._event('identifier_reuse', {
            'source': old['source'], 'source_id': old['source_id'],
            'retired_object_id': old['object_id'], 'object_id': object_id})
        return object_id

    # -- addresses, withdrawal --------------------------------------------
    def add_address(self, source, source_id, url):
        if not url:
            return
        row = self.db.execute(
            'SELECT * FROM source_records WHERE source=? AND source_id=?',
            (source, source_id)).fetchone()
        if row is None:
            return
        with self.db:
            self._merge_addresses(row, url)

    def confirm_migration(self, source, source_id, url, now=0):
        """Record a confirmed new address and make later fetches follow it."""
        if not url:
            return
        row = self.db.execute(
            'SELECT * FROM source_records WHERE source=? AND source_id=?',
            (source, source_id)).fetchone()
        if row is None:
            return
        if url in json.loads(row['urls'] or '[]') and row['fetch_url'] == url:
            return
        self.add_address(source, source_id, url)
        self.set_fetch_url(source, source_id, url)
        with self.db:
            self._event('address_migration', {'source': source, 'source_id': source_id,
                                              'url': url}, now)

    def set_fetch_url(self, source, source_id, url):
        if not url:
            return
        with self.db:
            self.db.execute(
                'UPDATE source_records SET fetch_url=? WHERE source=? AND source_id=?',
                (url, source, source_id))

    def set_withdrawn(self, source, source_id, withdrawn, reason='http'):
        with self.db:
            self.db.execute(
                'UPDATE source_records SET withdrawn=?, withdrawal_reason=? '
                'WHERE source=? AND source_id=?',
                (int(bool(withdrawn)), reason if withdrawn else None,
                 source, source_id))

    # -- graph -------------------------------------------------------------
    def _event(self, action, detail, now=0):
        self.db.execute('INSERT INTO association_events(action,detail,created) VALUES(?,?,?)',
                        (action, json.dumps(detail, sort_keys=True, ensure_ascii=False), now))

    def _object_exists(self, object_id):
        return self.db.execute('SELECT 1 FROM objects WHERE object_id=?',
                               (object_id,)).fetchone() is not None

    def _components(self):
        objects = [row['object_id'] for row in
                   self.db.execute('SELECT object_id FROM objects ORDER BY seq')]
        parent = {object_id: object_id for object_id in objects}

        def find(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        for row in self.db.execute('SELECT * FROM object_links WHERE active=1'):
            left, right = row['left_object'], row['right_object']
            if left in parent and right in parent:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root
        groups = {}
        for object_id in objects:
            groups.setdefault(find(object_id), []).append(object_id)
        order = {row['object_id']: row['seq'] for row in
                 self.db.execute('SELECT object_id, seq FROM objects')}
        result = {}
        components = {}
        for members in groups.values():
            representative = min(members, key=lambda value: order.get(value, 0))
            components[representative] = members
            for member in members:
                result[member] = representative
        return result, components

    def resolve_object(self, object_id):
        return self._components()[0].get(object_id, object_id)

    def _exclusions(self):
        return {row['pair']: dict(row) for row in
                self.db.execute('SELECT * FROM split_exclusions')}

    def _blocked(self, left, right):
        """Return the exclusion that forbids merging these two components."""
        mapping, groups = self._components()
        left_rep, right_rep = mapping[left], mapping[right]
        combined = set(groups.get(left_rep, [left])) | set(groups.get(right_rep, [right]))
        for pair, row in self._exclusions().items():
            if row['left_object'] in combined and row['right_object'] in combined:
                return row
        return None

    def _auto_link(self, left, right, decision, evidence, now=0):
        mapping = self._components()[0]
        if mapping.get(left) == mapping.get(right):
            return True
        blocked = self._blocked(left, right)
        if blocked is not None:
            self._propose(left, right, decision, evidence, now,
                          blocked='human_split', blocked_pair=blocked['pair'])
            return False
        pair = _pair(left, right)
        score = evidence.get('score', 0.0)
        with self.db:
            self.db.execute(
                'INSERT INTO object_links VALUES(?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(pair) DO UPDATE SET decision=excluded.decision, '
                'score=excluded.score, evidence=excluded.evidence, active=1, '
                'updated=excluded.updated',
                (pair, left, right, decision, score,
                 json.dumps(evidence, ensure_ascii=False), 1, now, now))
            self.db.execute('DELETE FROM association_proposals WHERE pair=?', (pair,))
            self._event('auto_link', {'pair': pair, 'decision': decision,
                                      'evidence': evidence}, now)
        return True

    def _propose(self, left, right, decision, evidence, now=0, blocked=None,
                 blocked_pair=None):
        pair = _pair(left, right)
        if self.db.execute('SELECT 1 FROM object_links WHERE pair=? AND active=1',
                           (pair,)).fetchone():
            return
        detail = {'decision': decision, 'score': evidence.get('score', 0.0),
                  'blocked': blocked, 'blocked_pair': blocked_pair,
                  'evidence': evidence}
        with self.db:
            self.db.execute(
                'INSERT INTO association_proposals VALUES(?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(pair) DO UPDATE SET decision=excluded.decision, '
                'score=excluded.score, evidence=excluded.evidence, '
                'blocked=excluded.blocked, updated=excluded.updated',
                (pair, left, right, decision, evidence.get('score', 0.0),
                 json.dumps(evidence, ensure_ascii=False), blocked, now, now))
            self._event('proposal', {'pair': pair, 'decision': decision,
                                     'blocked': blocked}, now)

    def confirm(self, left, right, note='', now=0):
        """Human confirmation wins over an earlier split of the same pair."""
        if left == right:
            return left
        for object_id in (left, right):
            if not self._object_exists(object_id):
                raise ValueError('unknown object')
        mapping, groups = self._components()
        combined = set(groups.get(mapping[left], [left])) | set(groups.get(mapping[right], [right]))
        with self.db:
            for exclusion_pair, row in list(self._exclusions().items()):
                if (row['left_object'] in combined and row['right_object'] in combined):
                    # The human states these records belong together; no stale
                    # exclusion inside the joined group may contradict that.
                    self.db.execute('DELETE FROM split_exclusions WHERE pair=?',
                                    (exclusion_pair,))
                    self._event('exclusion_cleared', {'pair': exclusion_pair,
                                                      'by': 'human'}, now)
            self._connect([left, right], note, now)
            self._event('human_confirm', {'pair': _pair(left, right), 'note': note},
                        now)
        return self.resolve_object(left)

    def revoke(self, left, right, note='', now=0):
        """Undo an association between two objects; auto association may not undo it.

        The human ruling must hold even when the cluster was formed indirectly.
        The group reachable from ``left`` without passing through ``right`` is
        kept together, every other member moves to the other side, and all
        cross-pairs are excluded from automatic re-merge.
        """
        if left == right:
            return left
        mapping, components = self._components()
        if mapping.get(left) != mapping.get(right):
            with self.db:
                pair = _pair(left, right)
                self.db.execute(
                    'UPDATE object_links SET active=0, decision=?, updated=? WHERE pair=?',
                    ('revoked', now, pair))
                self.db.execute('DELETE FROM association_proposals WHERE pair=?', (pair,))
                self.db.execute(
                    'INSERT OR REPLACE INTO split_exclusions VALUES(?,?,?,?,?)',
                    (pair, left, right,
                     json.dumps({'kind': 'human_revoke', 'note': note},
                                ensure_ascii=False), now))
                self._event('human_revoke', {'pair': pair, 'note': note}, now)
            return self.resolve_object(left)
        members = components[mapping[left]]
        kept = self._reachable(left, blocked={right}, members=members)
        with self.db:
            for member in members:
                for other in members:
                    if member == other:
                        continue
                    if (member in kept) == (other in kept):
                        continue
                    self._exclude(member, other, note, now, kind='human_revoke')
            self._connect(list(kept), note, now)
            self._connect([member for member in members if member not in kept], note, now)
            self._event('human_revoke', {'left': left, 'right': right,
                                         'groups': [sorted(kept),
                                                    sorted(set(members) - kept)],
                                         'note': note}, now)
        return self.resolve_object(left)

    def _reachable(self, start, blocked, members):
        """Objects reachable from ``start`` over active links, avoiding ``blocked``."""
        allowed = set(members) - set(blocked)
        edges = {member: set() for member in allowed}
        for row in self.db.execute('SELECT * FROM object_links WHERE active=1'):
            left, right = row['left_object'], row['right_object']
            if left in edges and right in edges:
                edges[left].add(right)
                edges[right].add(left)
        seen = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            for neighbour in edges.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    pending.append(neighbour)
        return seen

    def _exclude(self, left, right, note, now=0, kind='human_split'):
        if left == right:
            return
        pair = _pair(left, right)
        self.db.execute(
            'UPDATE object_links SET active=0, decision=?, updated=? WHERE pair=?',
            ('revoked' if kind == 'human_revoke' else 'split', now, pair))
        self.db.execute('DELETE FROM association_proposals WHERE pair=?', (pair,))
        self.db.execute(
            'INSERT OR REPLACE INTO split_exclusions VALUES(?,?,?,?,?)',
            (pair, left, right,
             json.dumps({'kind': kind, 'note': note}, ensure_ascii=False), now))

    def _connect(self, members, note, now=0):
        """Keep a human-defined group connected through explicit links."""
        members = list(dict.fromkeys(members))
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                pair = _pair(left, right)
                evidence = {'reasons': [{'kind': 'human_confirmation', 'note': note}],
                            'score': 1.0}
                self.db.execute(
                    'INSERT INTO object_links VALUES(?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(pair) DO UPDATE SET decision=?, score=1.0, '
                    'evidence=?, active=1, updated=?',
                    (pair, left, right, 'human', 1.0,
                     json.dumps(evidence, ensure_ascii=False), 1, now, now,
                     'human', json.dumps(evidence, ensure_ascii=False), now))
                self.db.execute('DELETE FROM association_proposals WHERE pair=?', (pair,))

    def detach(self, object_id, note='', now=0):
        """Remove one object from its association, excluding it from re-merge."""
        if not self._object_exists(object_id):
            raise ValueError('unknown object')
        mapping, groups = self._components()
        members = groups.get(mapping[object_id], [object_id])
        if len(members) < 2:
            return object_id
        remaining = [member for member in members if member != object_id]
        with self.db:
            for partner in remaining:
                pair = _pair(object_id, partner)
                self.db.execute(
                    'UPDATE object_links SET active=0, decision=?, updated=? WHERE pair=?',
                    ('split', now, pair))
                self.db.execute('DELETE FROM association_proposals WHERE pair=?', (pair,))
                self.db.execute(
                    'INSERT OR REPLACE INTO split_exclusions VALUES(?,?,?,?,?)',
                    (pair, object_id, partner,
                     json.dumps({'kind': 'human_split', 'note': note},
                                ensure_ascii=False), now))
            self._connect(remaining, note, now)
            self._event('human_split', {'object_id': object_id,
                                        'partners': remaining, 'note': note}, now)
        return object_id

    def split(self, groups, note='', now=0):
        """Human split; groups must cover every member of the touched objects."""
        groups = [list(group) for group in groups]
        mapping, components = self._components()
        touched = set()
        for group in groups:
            for object_id in group:
                if not self._object_exists(object_id):
                    raise ValueError('unknown object')
                touched.add(mapping[object_id])
        covered = {member for group in groups for object_id in group
                   for member in components.get(mapping[object_id], [])}
        expected = {member for representative in touched
                    for member in components.get(representative, [])}
        if covered != expected:
            raise ValueError('split must cover every associated object')
        group_of = {}
        for index, group in enumerate(groups):
            for object_id in group:
                group_of[object_id] = index
        with self.db:
            for pair, row in list(self._exclusions().items()):
                both_known = (row['left_object'] in group_of
                              and row['right_object'] in group_of)
                same_group = (both_known and group_of[row['left_object']]
                              == group_of[row['right_object']])
                if same_group:
                    # The human now keeps these together; a stale exclusion must
                    # not block a later legitimate merge.
                    self.db.execute('DELETE FROM split_exclusions WHERE pair=?', (pair,))
            for left_index, left_group in enumerate(groups):
                for right_group in groups[left_index + 1:]:
                    for left in left_group:
                        for right in right_group:
                            pair = _pair(left, right)
                            self.db.execute(
                                'UPDATE object_links SET active=0, decision=?, updated=? '
                                'WHERE pair=?', ('split', now, pair))
                            self.db.execute(
                                'DELETE FROM association_proposals WHERE pair=?', (pair,))
                            self.db.execute(
                                'INSERT OR REPLACE INTO split_exclusions VALUES(?,?,?,?,?)',
                                (pair, left, right,
                                 json.dumps({'kind': 'human_split', 'note': note},
                                            ensure_ascii=False), now))
            for group in groups:
                self._connect(group, note, now)
            self._event('human_split', {'groups': groups, 'note': note}, now)
        return {object_id: self.resolve_object(object_id) for object_id in group_of}

    def _object_for(self, identity):
        if isinstance(identity, str) and self._object_exists(identity):
            return identity
        if isinstance(identity, dict):
            identity = (identity['source'], identity['source_id'])
        if isinstance(identity, (tuple, list)) and len(identity) == 2:
            row = self.db.execute(
                'SELECT object_id FROM source_records WHERE source=? AND source_id=?',
                tuple(identity)).fetchone()
            if row:
                return row['object_id']
        raise ValueError('unknown source record')

    def confirm_records(self, left, right, note='', now=0):
        return self.confirm(self._object_for(left), self._object_for(right), note, now)

    def revoke_records(self, left, right, note='', now=0):
        return self.revoke(self._object_for(left), self._object_for(right), note, now)

    def detach_record(self, identity, note='', now=0):
        return self.detach(self._object_for(identity), note, now)

    # -- reconciliation ----------------------------------------------------
    def record_views(self, resolver=None):
        resolver = resolver or self.resolver
        rows = self.rows()
        views = []
        for row in rows:
            urls = list(row.get('urls') or [])
            current_url = (row.get('current') or {}).get('url')
            if current_url and current_url not in urls:
                urls.append(current_url)
            keys = set()
            if resolver is not None:
                keys = resolver.expand_keys({address_key(url) for url in urls})
            else:
                keys = {address_key(url) for url in urls}
            views.append({**row, 'urls': urls, 'keys': keys,
                          'identity': (row['source'], row['source_id'])})
        return views

    def reconcile(self, resolver=None, now=0):
        """Recompute associations.  Safe and idempotent across collection runs."""
        resolver = resolver or self.resolver
        if resolver is not None:
            for aliases in resolver.human_aliases().values():
                for alias in aliases:
                    self.confirm_migration(alias['source'], alias['source_id'],
                                           alias['url'], now)
        views = self.record_views(resolver)
        owners = {}
        body_owners = {}
        for view in views:
            for key in view['keys']:
                owners.setdefault(key, set()).add(view['identity'])
            body_key = normalize(body_text(view['current']))
            if body_key:
                body_owners.setdefault(body_key, set()).add(view['identity'])
        for index, left in enumerate(views):
            for right in views[index + 1:]:
                canonical_left = self._usable_canonical(left, right, owners)
                canonical_right = self._usable_canonical(right, left, owners)
                body_key = normalize(body_text(left['current']))
                shared_body = (body_key
                               and body_key == normalize(body_text(right['current']))
                               and len(body_owners.get(body_key, ())) > 2)
                evidence = compare(
                    left['current'], right['current'], left['keys'], right['keys'],
                    canonical_left=canonical_left, canonical_right=canonical_right,
                    shared_body=shared_body)
                decision = evidence['decision']
                if left['source'] == right['source'] and decision != 'hard':
                    # Within one source, only an explicit canonical reference may
                    # join records; identifier reuse is handled by incarnation.
                    continue
                if decision in ('hard', 'strong'):
                    self._auto_link(left['object_id'], right['object_id'],
                                    decision, evidence, now)
                elif decision in ('provisional', 'proposed'):
                    self._propose(left['object_id'], right['object_id'], decision,
                                  evidence, now)
        return self.explain(resolver)

    @staticmethod
    @staticmethod
    def _usable_canonical(view, other, owners):
        result = []
        for url in canonical_urls(view['current']):
            key = address_key(url)
            if key not in other['keys']:
                continue
            if owners.get(key) != {other['identity']}:
                # A shared channel/homepage address identifies no single notice.
                continue
            if key in view['keys']:
                # The reference must point at the other record, not at self.
                continue
            result.append(key)
        return result

    # -- reporting ---------------------------------------------------------
    def rows(self):
        mapping = self._components()[0]
        result = []
        for row in self.db.execute('SELECT * FROM source_records ORDER BY source,source_id'):
            record = dict(row)
            record['current'] = json.loads(record['current'])
            record['urls'] = json.loads(record['urls'] or '[]')
            record['withdrawn'] = bool(record['withdrawn'])
            record['effective'] = not record['withdrawn']
            record['association_id'] = mapping.get(record['object_id'], record['object_id'])
            result.append(record)
        return result

    def history(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM retired_records ORDER BY source,source_id,incarnation')]

    def links(self):
        result = []
        for row in self.db.execute('SELECT * FROM object_links ORDER BY created,pair'):
            record = dict(row)
            record['evidence'] = json.loads(record['evidence'] or '{}')
            result.append(record)
        return result

    def proposals(self):
        result = []
        for row in self.db.execute('SELECT * FROM association_proposals ORDER BY pair'):
            record = dict(row)
            record['evidence'] = json.loads(record['evidence'] or '{}')
            result.append(record)
        return result

    def events(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM association_events ORDER BY event')]

    def explain(self, resolver=None, frontier=None):
        resolver = resolver or self.resolver
        views = {view['object_id']: view for view in self.record_views(resolver)}
        mapping, components = self._components()
        links = self.links()
        proposals = self.proposals()
        retired = self.history()
        failures = resolver.failures() if resolver is not None else {}
        objects = []
        conflicts = []
        for representative, members in sorted(components.items()):
            member_views = [views[member] for member in members if member in views]
            active_links = [link for link in links
                            if link['active'] and link['left_object'] in members
                            and link['right_object'] in members]
            relevant_proposals = [proposal for proposal in proposals
                                  if proposal['left_object'] in members
                                  or proposal['right_object'] in members]
            withdrawn = [view for view in member_views if view['withdrawn']]
            effective = [view for view in member_views if not view['withdrawn']]
            object_conflicts = []
            if withdrawn and effective:
                object_conflicts.append({
                    'kind': 'withdrawn_mismatch',
                    'withdrawn_records': [
                        {'source': view['source'], 'source_id': view['source_id'],
                         'reason': view['withdrawal_reason']} for view in withdrawn],
                    'active_copies': [
                        {'source': view['source'], 'source_id': view['source_id']}
                        for view in effective],
                    'detail': 'original withdrawn; syndicated copies remain as published',
                })
            if withdrawn and not effective:
                object_conflicts.append({
                    'kind': 'withdrawn_all',
                    'withdrawn_records': [
                        {'source': view['source'], 'source_id': view['source_id']}
                        for view in withdrawn]})
            for proposal in relevant_proposals:
                object_conflicts.append({
                    'kind': 'unconfirmed_match',
                    'decision': proposal['decision'],
                    'records': [proposal['left_object'], proposal['right_object']],
                    'blocked': proposal['blocked'],
                    'evidence': proposal['evidence']})
            for view in member_views:
                for url in view['urls']:
                    failure = failures.get(address_key(url))
                    if failure:
                        object_conflicts.append({
                            'kind': 'unresolved_redirect',
                            'source': view['source'], 'source_id': view['source_id'],
                            'from_url': failure['from_url'],
                            'final_url': failure['final_url'],
                            'state': failure['state'],
                            'detail': 'redirect target is not a specific notice; not used for association'})
            for record in retired:
                if record['object_id'] in members:
                    object_conflicts.append({
                        'kind': 'identifier_reuse',
                        'source': record['source'], 'source_id': record['source_id'],
                        'retired_object_id': record['object_id'],
                        'retired_revision': record['revision'],
                        'detail': 'identifier reused by a later unrelated notice'})
            objects.append({
                'object_id': representative,
                'members': [{
                    'source': view['source'], 'source_id': view['source_id'],
                    'object_id': view['object_id'], 'revision': view['revision'],
                    'withdrawn': view['withdrawn'],
                    'withdrawal_reason': view['withdrawal_reason'],
                    'effective': not view['withdrawn'],
                    'urls': view['urls'], 'fetch_url': view['fetch_url'],
                    'current': view['current']} for view in member_views],
                'effective_records': [
                    {'source': view['source'], 'source_id': view['source_id']}
                    for view in effective],
                'links': active_links,
                'proposals': relevant_proposals,
                'conflicts': object_conflicts,
            })
            conflicts.extend([{**conflict, 'object_id': representative}
                              for conflict in object_conflicts])
        exclusions = [dict(row) for row in self.db.execute(
            'SELECT * FROM split_exclusions ORDER BY created')]
        result = {
            'objects': objects,
            'links': links,
            'proposals': proposals,
            'exclusions': exclusions,
            'retired': retired,
            'conflicts': conflicts,
        }
        if frontier is not None:
            status = {(view['source'], view['source_id']): view for view in views.values()}
            retired_objects = {record['object_id'] for record in retired}
            result['next_fetches'] = []
            for row in frontier.rows():
                view = status.get((row['source'], row['source_id']))
                target_object = row['object_id']
                owns_target = bool(view and view['object_id'] == target_object)
                if target_object in retired_objects:
                    record_status = 'retired'
                elif owns_target:
                    record_status = 'withdrawn' if view['withdrawn'] else 'active'
                elif row['retired']:
                    record_status = 'retired'
                else:
                    record_status = 'unknown'
                result['next_fetches'].append({
                    **row,
                    'object_id': target_object,
                    'association_id': mapping.get(target_object, target_object),
                    'record_status': record_status,
                    'resolved_object_id': (target_object if owns_target else None),
                })
        return result
