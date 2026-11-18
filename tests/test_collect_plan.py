import tempfile
import unittest
from pathlib import Path

from syndication.collect import synchronize
from syndication.discovery import Feed
from syndication.frontier import Frontier
from syndication.identity import IdentityBook
from syndication.resolution import Resolver
from syndication.similarity import compare
from syndication.site import serve


BODY_A = ('华东地区发行授权公告：经审核，授予示例文化有限公司在上海市、江苏省、'
          '浙江省范围内开展出版物批发业务的许可，有效期至二零二七年十二月三十一日。')
BODY_B = ('西部发行许可公告：经审核，授予另一家示例发行有限公司在四川省、重庆市'
          '范围内开展出版物零售业务的许可，有效期至二零二八年六月三十日。')


def item(source, source_id, revision=1, **extra):
    base = {'source': source, 'id': source_id, 'revision': revision,
            'url': f'http://{source}.example/{source_id}'}
    base.update(extra)
    return base


class CollectPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def test_two_sources_are_linked_and_plan_reports_one_object(self):
        with serve() as (url, state):
            state['items'] = [
                {'id': 'n1', 'revision': 1, 'title': '华东发行授权公告',
                 'body': BODY_A, 'path': '/n1', 'canonical': url + '/n1'},
                {'id': 'p1', 'revision': 1, 'title': '上海企业获批发许可',
                 'body': BODY_A, 'path': '/p1', 'canonical': url + '/n1'},
            ]
            identity = IdentityBook(':memory:')
            frontier = Frontier(':memory:')
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 frontier, 100)
            associations = {row['source_id']: row['association_id']
                            for row in report['sources']}
            self.assertEqual(associations['n1'], associations['p1'])
            self.assertEqual(len(report['objects']), 1)
            self.assertEqual(len(report['next_fetches']), 2)
            self.assertTrue(all(row['record_status'] == 'active'
                                for row in report['next_fetches']))

    def test_original_withdrawal_keeps_partner_effective(self):
        with serve() as (url, state):
            state['items'] = [
                {'id': 'n1', 'revision': 1, 'title': '授权公告', 'body': BODY_A,
                 'path': '/n1', 'canonical': url + '/n1'},
                {'id': 'p1', 'revision': 1, 'title': '转载标题', 'body': BODY_A,
                 'path': '/p1', 'canonical': url + '/n1'},
            ]
            identity = IdentityBook(':memory:')
            frontier = Frontier(':memory:')
            synchronize(Feed(url + '/feed', 'publisher'), identity, frontier, 100)
            state['withdrawn'].add('/n1')
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 frontier, 200)
            records = {row['source_id']: row for row in report['sources']}
            self.assertTrue(records['n1']['withdrawn'])
            self.assertFalse(records['p1']['withdrawn'])
            self.assertEqual(records['n1']['association_id'],
                             records['p1']['association_id'])
            conflict = next(conflict for conflict in report['conflicts']
                            if conflict['kind'] == 'withdrawn_mismatch')
            self.assertEqual(conflict['active_copies'][0]['source_id'], 'p1')
            fetches = {row['source_id']: row for row in report['next_fetches']}
            self.assertEqual(fetches['n1']['due'], 200 + 86400)
            self.assertEqual(fetches['p1']['due'], 200 + 3600)

    def test_confirmed_migration_updates_plan_but_channel_landing_does_not(self):
        with serve() as (url, state):
            state['items'] = [
                {'id': 'n1', 'revision': 1, 'title': '迁移公告', 'body': BODY_A,
                 'path': '/n1'},
                {'id': 'n2', 'revision': 1, 'title': '另一公告', 'body': BODY_B,
                 'path': '/n2'},
            ]
            state['redirects']['/n1'] = '/n1-new'
            state['redirects']['/n2'] = '/channel'
            state['items'].append({'id': 'n1-new', 'revision': 2, 'title': '迁移公告',
                                   'body': BODY_A, 'path': '/n1-new'})
            identity = IdentityBook(':memory:')
            resolver = Resolver(':memory:')
            report = synchronize(Feed(url + '/feed', 'publisher'), identity,
                                 Frontier(':memory:'), 100, resolver)
            fetches = {row['source_id']: row for row in report['next_fetches']}
            self.assertEqual(fetches['n1']['url'], url + '/n1-new')
            self.assertEqual(fetches['n2']['url'], url + '/n2')
            migrated = next(row for row in report['resolutions']
                            if row['from_url'] == url + '/n1')
            landing = next(row for row in report['resolutions']
                           if row['from_url'] == url + '/n2')
            self.assertEqual(migrated['state'], 'resolved')
            self.assertEqual(landing['state'], 'index')
            self.assertNotEqual(fetches['n1']['association_id'],
                                fetches['n2']['association_id'])

    def test_reused_identifier_keeps_retired_target_and_address(self):
        identity = IdentityBook(':memory:')
        frontier = Frontier(':memory:')
        first = identity.attach(item('publisher', 'n1', revision=5, title='旧公告',
                                     body=BODY_A, url='http://publisher.example/n1'),
                                now=10)
        frontier.schedule({'source': 'publisher', 'id': 'n1',
                           'url': 'http://publisher.example/n1'}, first, 20)
        fresh = identity.attach(item('publisher', 'n1', revision=1, title='新公告',
                                     body=BODY_B,
                                     url='http://publisher.example/2027/n1'), now=30)
        frontier.schedule({'source': 'publisher', 'id': 'n1',
                           'url': 'http://publisher.example/2027/n1'}, fresh, 30)
        frontier.retire_stale({('publisher', 'n1')})
        targets = frontier.rows()
        self.assertEqual({row['object_id'] for row in targets}, {first, fresh})
        retired = next(row for row in targets if row['object_id'] == first)
        self.assertEqual(retired['url'], 'http://publisher.example/n1')
        self.assertFalse(retired['retired'])
        report = identity.explain(None, frontier)
        statuses = {row['object_id']: row['record_status']
                    for row in report['next_fetches']}
        self.assertEqual(statuses[first], 'retired')
        self.assertEqual(statuses[fresh], 'active')
        self.assertEqual(len(identity.history()), 1)

    def test_human_ruling_is_not_undone_by_later_rounds(self):
        identity = IdentityBook(':memory:')
        frontier = Frontier(':memory:')
        left = identity.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                                    published='2026-03-01'))
        right = identity.attach(item('partner', 'p1', title='乙', body=BODY_A,
                                     published='2026-03-02'))
        identity.reconcile()
        identity.revoke(left, right, note='并非同一份')
        for now in range(1, 4):
            identity.attach(item('partner', 'p1', revision=now + 1, title='乙新',
                                 body=BODY_A, published='2026-03-02'), now=now)
            report = identity.reconcile()
        self.assertNotEqual(identity.resolve_object(left),
                            identity.resolve_object(right))
        self.assertEqual(len(identity.rows()), 2)
        self.assertTrue(any(row['decision'] == 'revoked'
                            for row in identity.links()))

    def test_proposals_explain_why_match_is_unconfirmed(self):
        identity = IdentityBook(':memory:')
        identity.attach(item('publisher', 'n1', title='甲', body=BODY_A,
                             issuer='示例公司', published='2026-03-01'))
        identity.attach(item('partner', 'p1', title='乙', body=BODY_A,
                             issuer='另一家公司', published='2026-03-02'))
        report = identity.reconcile()
        self.assertFalse([row for row in report['links'] if row['active']])
        proposal = report['proposals'][0]
        self.assertIn(proposal['decision'], ('proposed', 'provisional'))
        self.assertTrue(proposal['evidence']['reasons'])
        self.assertFalse(proposal['evidence']['issuer_match'])

    def test_template_and_title_alone_never_join(self):
        template = ('根据《出版管理条例》及其实施细则的有关规定，现就本地区出版物发行'
                    '许可事项公告如下：经审核，准予申请人开展相关业务，许可期限以证书'
                    '载明为准。请申请人于公告之日起三十日内办理后续手续。')
        identity = IdentityBook(':memory:')
        first = identity.attach(item('publisher', 'n1', title='发行许可公告',
                                     body=template, issuer='甲公司',
                                     published='2026-01-01'))
        second = identity.attach(item('publisher', 'n2', title='发行许可公告',
                                      body=template, issuer='乙公司',
                                      published='2026-01-01'))
        third = identity.attach(item('partner', 'p1', title='转载公告',
                                     body=template, issuer='丙公司',
                                     published='2026-05-01'))
        report = identity.reconcile()
        self.assertEqual(len({identity.resolve_object(value)
                              for value in (first, second, third)}), 3)
        self.assertFalse([row for row in report['links'] if row['active']])


class SimilarityTests(unittest.TestCase):
    def test_same_title_with_different_bodies_is_not_evidence(self):
        left = {'title': '授权公告', 'body': BODY_A}
        right = {'title': '授权公告', 'body': BODY_B}
        evidence = compare(left, right)
        self.assertEqual(evidence['decision'], 'none')

    def test_template_body_without_corroboration_is_not_auto_joined(self):
        body = '经审核准予申请人开展相关业务许可期限以证书载明为准'
        evidence = compare({'title': '甲', 'body': body},
                           {'title': '乙', 'body': body}, shared_body=True)
        self.assertNotEqual(evidence['decision'], 'strong')

    def test_canonical_reference_outweighs_different_wording(self):
        evidence = compare({'title': '改写标题', 'body': '短摘要'},
                           {'title': '原标题', 'body': BODY_A},
                           canonical_left={'http://publisher.example/n1'})
        self.assertEqual(evidence['decision'], 'hard')
        self.assertEqual(evidence['reasons'][0]['kind'], 'canonical_reference')

    def test_conflicting_issuer_blocks_identical_body(self):
        body = '完全相同的正文内容用于测试发行许可公告模板复用场景'
        evidence = compare({'title': '甲', 'body': body, 'issuer': '甲公司',
                            'published': '2026-01-01'},
                           {'title': '甲', 'body': body, 'issuer': '乙公司',
                            'published': '2026-01-01'})
        self.assertNotEqual(evidence['decision'], 'strong')
        self.assertTrue(evidence['contradiction'])


if __name__ == '__main__':
    unittest.main()
