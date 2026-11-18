"""One collection round: attach records, confirm migrations, reconcile links.

Both facts are preserved when an original publisher withdraws a notice while a
partner still republishes it: each source record keeps its own status, and the
association reports the mismatch instead of inferring a withdrawal for the
partner or treating the withdrawn original as current content.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _check(url):
    """Fetch one address and report whether the notice is still published."""
    request = Request(url, method='GET', headers={'User-Agent': 'syndication-map/1'})
    try:
        with urlopen(request, timeout=5) as response:
            response.read(0)
            return {'url': url, 'status': response.status, 'active': True}
    except HTTPError as error:
        if error.code in (404, 410):
            return {'url': url, 'status': error.code, 'active': False,
                    'withdrawn': True}
        return {'url': url, 'status': error.code, 'active': None}
    except URLError as error:
        return {'url': url, 'status': None, 'active': None,
                'error': str(error.reason)}


def _index_urls(feed):
    index = set()
    for attribute in ('index_urls', 'channel_urls', 'home_urls'):
        for url in getattr(feed, attribute, None) or ():
            index.add(url)
    for attribute in ('url', 'home', 'index'):
        url = getattr(feed, attribute, None)
        if isinstance(url, str) and url:
            index.add(url)
    return index


def _resolve_record(record, resolver, index_urls):
    observed = []
    for url in record.get('urls') or [record.get('fetch_url')]:
        if url:
            observed.append(resolver.resolve(url, index_urls))
    for outcome in observed:
        resolver.record(outcome)
    return observed


def _apply_migrations(records, observed_by_record, identities, resolver, now):
    """Apply a redirect only when its target identifies a single record.

    Several notices redirecting to the same final address is the signature of a
    channel index, not of a migration; such a target must not rewrite every
    target nor become association evidence.
    """
    from .resolution import address_key
    targets = {}
    for identity, observed in observed_by_record.items():
        for outcome in observed:
            if outcome['state'] == 'resolved':
                targets.setdefault(address_key(outcome['final_url']), set()).add(identity)
    for identity, observed in observed_by_record.items():
        confirmed = [outcome for outcome in observed if outcome['state'] == 'resolved']
        if not confirmed:
            continue
        chosen = next((outcome for outcome in confirmed
                       if outcome['from_url'] == records[identity].get('fetch_url')),
                      confirmed[0])
        final = chosen.get('final_url')
        if not final:
            continue
        if len(targets.get(address_key(final), ())) > 1:
            resolver.record({**chosen, 'state': 'index'}, confirmed_by='ambiguous_target')
            continue
        identities.confirm_migration(records[identity]['source'],
                                     records[identity]['source_id'], final, now)


def synchronize(feed, identities, frontier, now, resolver=None):
    index_urls = _index_urls(feed)
    for item in feed.collect():
        identities.attach(item, now=now)
        identities.add_address(item['source'], item['id'], item.get('url'))
    if resolver is not None:
        observed_by_record = {}
        records = {}
        for record in identities.rows():
            identity = (record['source'], record['source_id'])
            records[identity] = record
            observed_by_record[identity] = _resolve_record(record, resolver,
                                                           index_urls)
        _apply_migrations(records, observed_by_record, identities, resolver, now)
        # Human-confirmed migrations are explicit and always applied, even when
        # the network probe is unavailable.
        for aliases in resolver.human_aliases().values():
            for alias in aliases:
                identities.confirm_migration(alias['source'], alias['source_id'],
                                             alias['url'], now)
    checked = []
    for record in identities.rows():
        target = record.get('fetch_url') or (record.get('current') or {}).get('url')
        if not target:
            continue
        result = _check(target)
        checked.append({**result, 'source': record['source'],
                        'source_id': record['source_id'],
                        'object_id': record['object_id']})
        if result.get('active') is False:
            identities.set_withdrawn(record['source'], record['source_id'], True,
                                     reason='http_%s' % result.get('status'))
        elif (result.get('active') is True and record['withdrawn']
              and str(record.get('withdrawal_reason') or '').startswith('http_')):
            # A source may restore a notice; only that source's own record changes,
            # and an explicit feed withdrawal is never overridden by a page probe.
            identities.set_withdrawn(record['source'], record['source_id'], False)
    if frontier is not None:
        records = identities.rows()
        for record in records:
            url = record.get('fetch_url') or (record.get('current') or {}).get('url')
            if not url:
                continue
            frontier.schedule({'source': record['source'], 'id': record['source_id'],
                               'url': url, 'withdrawn': record['withdrawn']},
                              record['object_id'], now, record)
        frontier.retire_stale({(record['source'], record['source_id'])
                               for record in records})
        if resolver is not None:
            frontier.apply_resolver(resolver)
    report = identities.reconcile(resolver, now)
    report['sources'] = identities.rows()
    report['checked'] = checked
    if resolver is not None:
        report['resolutions'] = resolver.all()
    if frontier is not None:
        report['next_fetches'] = identities.explain(resolver, frontier)['next_fetches']
    else:
        report['next_fetches'] = []
    report['review'] = {
        'proposals': report['proposals'],
        'conflicts': report['conflicts'],
        'next_fetches': report['next_fetches'],
    }
    return report
