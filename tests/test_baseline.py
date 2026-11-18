import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from syndication.collect import synchronize
from syndication.discovery import Feed
from syndication.frontier import Frontier
from syndication.identity import IdentityBook
from syndication.site import serve


class SyndicationTests(unittest.TestCase):
    def test_feed_identity_and_next_fetch(self):
        with serve() as (url, state):
            book = IdentityBook(':memory:')
            frontier = Frontier(':memory:')
            report = synchronize(Feed(url + '/feed', 'publisher'), book, frontier, 100)
            self.assertEqual(len(report['sources']), 2)
            self.assertEqual(report['next_fetches'][0]['due'], 3700)
            identities = [item['object_id'] for item in report['sources']]
            again = synchronize(Feed(url + '/feed', 'publisher'), book, frontier, 200)
            self.assertEqual([item['object_id'] for item in again['sources']], identities)

    def test_same_title_does_not_merge_different_source_records(self):
        book = IdentityBook(':memory:')
        item = {'source': 'a', 'id': '1', 'revision': 1, 'title': '公告', 'url': 'http://local/1'}
        first = book.attach(item)
        second = book.attach({**item, 'source': 'b'})
        self.assertNotEqual(first, second)

    def test_revision_conflict_and_older_data(self):
        book = IdentityBook(':memory:')
        item = {'source': 'a', 'id': '1', 'revision': 2, 'title': '新', 'url': 'http://local/1'}
        book.attach(item)
        book.attach({**item, 'revision': 1, 'title': '旧'})
        with self.assertRaises(ValueError):
            book.attach({**item, 'title': '冲突'})
        self.assertEqual(book.rows()[0]['current']['title'], '新')

    def test_redirect_and_withdrawal_source_protocol(self):
        with serve() as (url, state):
            state['redirects']['/old'] = '/notice/1'
            with urlopen(url + '/old') as response:
                self.assertEqual(response.geturl(), url + '/notice/1')
            state['withdrawn'].add('/notice/1')
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '/notice/1')
            self.assertEqual(error.exception.code, 410)

    def test_current_source_identity_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'identities.db'
            item = {'source': 'a', 'id': '1', 'revision': 1, 'url': 'http://local/1'}
            first = IdentityBook(filename).attach(item)
            self.assertEqual(IdentityBook(filename).attach(item), first)

    def test_frontier_rejects_non_http_source(self):
        with self.assertRaises(ValueError):
            Frontier(':memory:').schedule({'source': 'a', 'id': '1', 'url': 'file:///local'}, 'object', 0)
