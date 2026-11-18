import json
from urllib.request import urlopen


class Feed:
    def __init__(self, url, source):
        self.url = url
        self.source = source

    def collect(self):
        with urlopen(self.url, timeout=5) as response:
            payload = json.load(response)
        if payload['source'] != self.source:
            raise ValueError('feed source mismatch')
        seen = set()
        for item in payload['items']:
            if not item.get('id') or item['id'] in seen or not item.get('url'):
                raise ValueError('invalid or repeated source record')
            seen.add(item['id'])
            yield {**item, 'source': self.source}
