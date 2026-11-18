import json
import tempfile
from pathlib import Path
from syndication.collect import synchronize
from syndication.discovery import Feed
from syndication.frontier import Frontier
from syndication.identity import IdentityBook
from syndication.site import serve


def main():
    with tempfile.TemporaryDirectory() as directory, serve() as (url, state):
        identities = IdentityBook(Path(directory) / 'identity.sqlite3')
        frontier = Frontier(Path(directory) / 'frontier.sqlite3')
        result = synchronize(Feed(url + '/feed', 'publisher'), identities, frontier, 100)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
