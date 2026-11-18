def synchronize(feed, identities, frontier, now):
    for item in feed.collect():
        object_id = identities.attach(item)
        current = next(record['current'] for record in identities.rows()
                       if record['source'] == item['source'] and record['source_id'] == item['id'])
        frontier.schedule(current, object_id, now)
    return {'sources': identities.rows(), 'next_fetches': frontier.rows()}
