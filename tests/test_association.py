import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from syndication.collect import synchronize
from syndication.discovery import Feed
from syndication.frontier import Frontier
from syndication.identity import IdentityBook
from syndication.resolution import Resolver
from syndication.site import serve


BODY_A = ('华东地区发行授权公告：经审核，授予示例文化有限公司在上海市、江苏省、'
          '浙江省范围内开展出版物批发业务的许可，有效期至二零二七年十二月三十一日。')
BODY_B = ('西部发行许可公告：经审核，授予另一家示例发行有限公司在四川省、重庆市'
          '范围内开展出版物零售业务的许可，有效期至二零二八年六月三十日。')
BODY_C = ('华南地区发行授权公告：经审核，授予第三家示例文化有限公司在广东省、'
          '福建省范围内开展出版物批发业务的许可，有效期至二零二七年九月三十日。')

TEMPLATE = ('根据《出版管理条例》及其实施细则的有关规定，现就本地区出版物发行'
            '许可事项公告如下：经审核，准予申请人开展相关业务，许可期限以证书载明'
            '为准。请申请人于公告之日起三十日内办理后续手续。')


def item(source, source_id, revision=1, **extra):
    base = {'source': source, 'id': source_id, 'revision': revision,
            'url': f'http://{source}.example/{source_id}'}
    base.update(extra)
    return base


class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.book = IdentityBook(Path(self.directory.name) / 'identity.sqlite3')

    def test_rewritten_title_is_linked_by_content(self):
        original = self.book.attach(item(
            'publisher', 'n1', title='华东发行授权公告', body=BODY_A,
            published='2026-03-01'))
        syndicated = self.book.attach(item(
            'partner', 'p1', title='上海文化企业获出版物批发许可', body=BODY_A,
            published='2026-03-02'))
        self.assertNotEqual(original, syndicated)
        report = self.book.reconcile()
        self.assertEqual(self.book.resolve_object(original),
                         self.book.resolve_object(syndicated))
        links = [link for link in report['links'] if link['active']]
        self.assertEqual(links[0]['decision'], 'strong')
        self.assertTrue(any(reason['kind'] == 'content_similarity'
                            for reason in links[0]['evidence']['reasons']))

    def test_shared_template_is_not_merged(self):
        first = self.book.attach(item('publisher', 'n1', title='发行许可公告', body=TEMPLATE,
                                      issuer='示例发行公司', published='2026-01-01'))
        second = self.book.attach(item('publisher', 'n2', title='发行许可公告', body=TEMPLATE,
                                       issuer='另一家发行公司', published='2026-01-01'))
        third = self.book.attach(item('partner', 'p1', title='转载公告', body=TEMPLATE,
                                      issuer='第三家公司', published='2026-05-01'))
        report = self.book.reconcile()
        self.assertEqual(self.book.resolve_object(first), first)
        self.assertEqual(self.book.resolve_object(second), second)
        self.assertEqual(self.book.resolve_object(third), third)
        self.assertFalse([link for link in report['links'] if link['active']])

    def test_same_title_alone_is_not_merged(self):
        first = self.book.attach(item('publisher', 'n1', title='授权公告', body=BODY_A))
        second = self.book.attach(item('partner', 'p1', title='授权公告', body=BODY_C))
        report = self.book.reconcile()
        self.assertNotEqual(self.book.resolve_object(first),
                            self.book.resolve_object(second))
        self.assertFalse([link for link in report['links'] if link['active']])

    def test_canonical_reference_is_hard_evidence(self):
        target = item('publisher', 'n1', title='授权公告', body=BODY_A)
        syndicated = item('partner', 'p1', title='完全改写后的标题', body='简短导语',
                          canonical=target['url'])
        first = self.book.attach(target)
        second = self.book.attach(syndicated)
        self.book.reconcile()
        self.assertEqual(self.book.resolve_object(first),
                         self.book.resolve_object(second))

    def test_shared_homepage_is_not_canonical_evidence(self):
        first = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                      canonical='http://publisher.example/'))
        second = self.book.attach(item('partner', 'p1', title='乙', body=BODY_C,
                                       canonical='http://publisher.example/'))
        self.book.reconcile()
        self.assertNotEqual(self.book.resolve_object(first),
                            self.book.resolve_object(second))

    def test_manual_confirm_overrides_low_similarity_and_survives_rerun(self):
        first = self.book.attach(item('publisher', 'n1', title='甲公告', body=BODY_A))
        second = self.book.attach(item('partner', 'p1', title='乙公告', body='站点摘要'))
        report = self.book.reconcile()
        self.assertNotEqual(self.book.resolve_object(first),
                            self.book.resolve_object(second))
        self.book.confirm_records(('publisher', 'n1'), ('partner', 'p1'), note='编辑核对')
        self.assertEqual(self.book.resolve_object(first),
                         self.book.resolve_object(second))
        for _ in range(3):
            report = self.book.reconcile()
            self.assertEqual(self.book.resolve_object(first),
                             self.book.resolve_object(second))
        link = [link for link in report['links'] if link['active']][0]
        self.assertEqual(link['decision'], 'human')
        self.assertTrue(any(reason['kind'] == 'human_confirmation'
                            for reason in link['evidence']['reasons']))

    def test_revoke_and_split_keep_records_and_targets(self):
        frontier = Frontier(Path(self.directory.name) / 'frontier.sqlite3')
        first = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                      published='2026-03-01'))
        second = self.book.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                       published='2026-03-02'))
        self.book.reconcile()
        self.assertEqual(self.book.resolve_object(first), self.book.resolve_object(second))
        self.book.revoke_records(('publisher', 'n1'), ('partner', 'p1'), note='误合并')
        for _ in range(3):
            report = self.book.reconcile()
            self.assertNotEqual(self.book.resolve_object(first),
                                self.book.resolve_object(second))
        self.assertEqual(len(self.book.rows()), 2)
        self.assertEqual({row['source_id'] for row in self.book.rows()}, {'n1', 'p1'})
        self.assertTrue(report['exclusions'])

    def test_detach_one_member_from_cluster(self):
        urls = ['http://publisher.example/n1']
        first = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                      published='2026-03-01'))
        second = self.book.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                       published='2026-03-02'))
        third = self.book.attach(item('partner', 'p2', title='丙', body=BODY_A,
                                      published='2026-03-02'))
        self.book.reconcile()
        self.book.detach(first, note='原发不是同一份')
        report = self.book.reconcile()
        self.assertNotEqual(self.book.resolve_object(first),
                            self.book.resolve_object(second))
        self.assertEqual(self.book.resolve_object(second),
                         self.book.resolve_object(third))
        self.assertEqual(len(report['retired']), 0)

    def test_three_way_split(self):
        ids = [
            self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                  published='2026-03-01')),
            self.book.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                  published='2026-03-02')),
            self.book.attach(item('partner', 'p2', title='丙', body=BODY_A,
                                  published='2026-03-02')),
        ]
        self.book.reconcile()
        self.assertEqual(len({self.book.resolve_object(value) for value in ids}), 1)
        self.book.split([[ids[0]], ids[1:]], note='只有两家转载同源')
        self.assertNotEqual(self.book.resolve_object(ids[0]),
                            self.book.resolve_object(ids[1]))
        self.assertEqual(self.book.resolve_object(ids[1]),
                         self.book.resolve_object(ids[2]))
        for _ in range(3):
            self.book.reconcile()
        self.assertNotEqual(self.book.resolve_object(ids[0]),
                            self.book.resolve_object(ids[1]))
        self.assertEqual(self.book.resolve_object(ids[1]),
                         self.book.resolve_object(ids[2]))
        # Splitting a detached singleton is a no-op, not an under-covered split.
        self.book.split([[ids[0]]])

    def test_rerun_is_idempotent(self):
        first = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                      published='2026-03-01'))
        second = self.book.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                       published='2026-03-02'))
        self.book.reconcile()
        for _ in range(3):
            report = self.book.reconcile()
        self.assertEqual(len(self.book.rows()), 2)
        self.assertEqual(len([row for row in report['links'] if row['active']]), 1)
        self.assertEqual(self.book.resolve_object(first), self.book.resolve_object(second))

    def test_source_identifier_reuse_keeps_both_records(self):
        old = self.book.attach(item('publisher', 'n1', revision=4, title='甲', body=BODY_A,
                                    url='http://publisher.example/n1'))
        new = self.book.attach(item('publisher', 'n1', revision=1, title='乙', body=BODY_C,
                                    url='http://publisher.example/n1-2027'))
        self.assertNotEqual(old, new)
        self.assertEqual(len(self.book.history()), 1)
        self.assertEqual(self.book.history()[0]['object_id'], old)
        current = [row for row in self.book.rows() if row['source_id'] == 'n1'][0]
        self.assertEqual(current['object_id'], new)
        self.assertEqual(current['current']['title'], '乙')

    def test_withdrawal_keeps_two_facts(self):
        original = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                         published='2026-03-01'))
        partner = self.book.attach(item('partner', 'p1', title='转载标题', body=BODY_A,
                                        published='2026-03-02'))
        self.book.reconcile()
        self.book.set_withdrawn('publisher', 'n1', True, reason='http_410')
        report = self.book.reconcile()
        rows = {(row['source'], row['source_id']): row for row in self.book.rows()}
        self.assertTrue(rows[('publisher', 'n1')]['withdrawn'])
        self.assertFalse(rows[('partner', 'p1')]['withdrawn'])
        self.assertTrue(rows[('partner', 'p1')]['effective'])
        obj = next(entry for entry in report['objects']
                   if entry['object_id'] == self.book.resolve_object(original))
        self.assertEqual([record['source'] for record in obj['effective_records']],
                         ['partner'])
        conflict = next(conflict for conflict in obj['conflicts']
                        if conflict['kind'] == 'withdrawn_mismatch')
        self.assertEqual(conflict['active_copies'][0]['source'], 'partner')
        self.assertEqual(self.book.resolve_object(original),
                         self.book.resolve_object(partner))

    def test_withdrawn_record_not_effective_as_current_content(self):
        original = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A))
        self.book.set_withdrawn('publisher', 'n1', True)
        report = self.book.reconcile()
        self.assertFalse(self.book.rows()[0]['effective'])
        self.assertEqual(self.book.rows()[0]['withdrawal_reason'], 'http')
        self.assertTrue(report['objects'][0]['conflicts'])

    def test_explain_reports_next_fetch_and_conflicts(self):
        frontier = Frontier(Path(self.directory.name) / 'frontier.sqlite3')
        first = self.book.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                      published='2026-03-01'), now=10)
        second = self.book.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                       published='2026-03-02'), now=10)
        self.book.reconcile()
        self.book.set_withdrawn('publisher', 'n1', True)
        for record in self.book.rows():
            frontier.schedule({'source': record['source'], 'id': record['source_id'],
                               'url': record['current']['url'],
                               'withdrawn': record['withdrawn']},
                              record['object_id'], 100, record)
        report = self.book.explain(frontier=frontier)
        fetches = {row['source']: row for row in report['next_fetches']}
        self.assertEqual(fetches['publisher']['due'], 100 + 86400)
        self.assertEqual(fetches['partner']['due'], 100 + 3600)
        self.assertEqual(fetches['publisher']['record_status'], 'withdrawn')
        self.assertEqual(fetches['publisher']['association_id'],
                         fetches['partner']['association_id'])


class RedirectTests(unittest.TestCase):
    def test_confirmed_migration_follows_new_address(self):
        with serve() as (url, state):
            identity = IdentityBook(':memory:')
            frontier = Frontier(':memory:')
            resolver = Resolver(':memory:')
            state['items'].append({'id': 'notice-3', 'revision': 1, 'title': '迁移公告',
                                   'body': BODY_C, 'path': '/notice/3'})
            state['redirects']['/notice/3'] = '/notice/3-new'
            state['items'].append({'id': 'notice-3-new', 'revision': 2,
                                   'title': '迁移公告', 'body': BODY_C,
                                   'path': '/notice/3-new'})
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 frontier, 100, resolver)
            record = next(row for row in report['sources']
                          if row['source_id'] == 'notice-3')
            self.assertEqual(record['fetch_url'], url + '/notice/3-new')
            target = next(row for row in report['next_fetches']
                          if row['source_id'] == 'notice-3')
            self.assertEqual(target['url'], url + '/notice/3-new')
            states = {row['from_url']: row for row in report['resolutions']}
            self.assertEqual(states[url + '/notice/3']['state'], 'resolved')
            self.assertEqual(states[url + '/notice/3']['final_url'],
                             url + '/notice/3-new')

    def test_channel_redirect_does_not_merge_notices(self):
        with serve() as (url, state):
            identity = IdentityBook(':memory:')
            resolver = Resolver(':memory:')
            state['redirects']['/notice/1'] = '/channel'
            state['redirects']['/notice/2'] = '/channel'
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 Frontier(':memory:'), 100, resolver)
            states = {row['from_url']: row for row in report['resolutions']}
            self.assertEqual(states[url + '/notice/1']['state'], 'index')
            self.assertEqual(states[url + '/notice/2']['state'], 'index')
            objects = {row['source_id']: row['association_id'] for row in report['sources']}
            self.assertNotEqual(objects['notice-1'], objects['notice-2'])
            self.assertEqual(IdentityBook(':memory:').resolve_object('x'), 'x')

    def test_human_confirmed_migration_used_when_probe_unavailable(self):
        with serve() as (url, state):
            identity = IdentityBook(':memory:')
            resolver = Resolver(':memory:')
            first = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                Frontier(':memory:'), 100, resolver)
            resolver.record_human('publisher', 'notice-1', url + '/moved/1',
                                  'support confirmed move')
            frontier = Frontier(':memory:')
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 frontier, 200, resolver)
            record = next(row for row in identity.rows()
                          if row['source_id'] == 'notice-1')
            self.assertEqual(record['fetch_url'], url + '/moved/1')
            self.assertEqual(record['urls'][-1], url + '/moved/1')
            target = next(row for row in report['next_fetches']
                          if row['source_id'] == 'notice-1')
            self.assertEqual(target['url'], url + '/moved/1')


class EndToEndTests(unittest.TestCase):
    def test_redirect_into_existing_notice_links_sources(self):
        with serve() as (url, state):
            identity = IdentityBook(':memory:')
            resolver = Resolver(':memory:')
            state['items'].append({'id': 'notice-3', 'revision': 1, 'title': '转载',
                                   'body': BODY_C, 'path': '/notice/3',
                                   'canonical': url + '/notice/3-target'})
            state['items'].append({'id': 'notice-3-target', 'revision': 1,
                                   'title': '原发', 'body': BODY_C,
                                   'path': '/notice/3-target'})
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 Frontier(':memory:'), 100, resolver)
            associations = {row['source_id']: row['association_id']
                            for row in report['sources']}
            self.assertEqual(associations['notice-3'],
                             associations['notice-3-target'])
            own = {row['source_id']: row['object_id'] for row in report['sources']}
            self.assertNotEqual(own['notice-3'], own['notice-3-target'])


if __name__ == '__main__':
    unittest.main()
