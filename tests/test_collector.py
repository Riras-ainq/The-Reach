import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'thereach/scripts'))
from sc_core import DEFAULTS, Failure, item
from sc_runtime import Runtime
from sc_collector import Collector
from collect import bounded, markdown, public_result, status_for, platform_status, main
import contextlib
import io


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = dict(DEFAULTS, state_dir=self.directory.name, min_interval=0)
        self.r = Runtime(self.config)
        self.c = Collector(self.r)
        self.url = 'https://www.youtube.com/watch?v=abcdefghijk'

    def tearDown(self):
        self.directory.cleanup()

    def test_captcha_no_fallback_and_persistent_gate(self):
        called = []
        def blocked(*args):
            called.append('blocked')
            raise Failure('PLATFORM_BLOCKED', 'captcha')
        def fallback(*args):
            called.append('fallback')
            return [item(self.url, text='should never happen')]
        with patch.object(self.c.p, 'routes', return_value=[('one', blocked), ('two', fallback)]):
            with self.assertRaises(Failure):
                self.c.operation('youtube', 'read', self.url)
        self.assertEqual(called, ['blocked'])
        other = Collector(Runtime(self.config), refresh=True)
        with patch.object(other.p, 'routes', return_value=[('two', fallback)]):
            with self.assertRaises(Failure):
                other.operation('youtube', 'read', self.url)
        self.assertEqual(called, ['blocked'])

    def test_network_failure_can_degrade_once(self):
        def broken(*args):
            raise Failure('NETWORK_ERROR', 'offline')
        def fallback(*args):
            return [item(self.url, text='真实测试正文')]
        with patch.object(self.c.p, 'routes', return_value=[('one', broken), ('two', fallback)]):
            values = self.c.operation('youtube', 'read', self.url)
        self.assertEqual(values[0]['text'], '真实测试正文')
        self.assertEqual([x['status'] for x in self.r.attempts], ['NETWORK_ERROR', 'ok'])

    def test_success_cached_across_invocations(self):
        def backend(*args):
            return [item(self.url, text='fixture only')]
        with patch.object(self.c.p, 'routes', return_value=[('fixture', backend)]):
            self.c.operation('youtube', 'read', self.url)
        other = Collector(Runtime(self.config))
        with patch.object(other.p, 'routes', side_effect=AssertionError('cache missed')):
            self.assertTrue(other.operation('youtube', 'read', self.url)[0]['from_cache'])

    def test_failed_cache_stops_invalid_retries(self):
        def broken(*args):
            raise Failure('UPSTREAM_BROKEN', 'offline')
        with patch.object(self.c.p, 'routes', return_value=[('fixture', broken)]):
            with self.assertRaises(Failure):
                self.c.operation('youtube', 'read', self.url)
        with patch.object(self.c.p, 'routes', side_effect=AssertionError('invalid retry')):
            with self.assertRaises(Failure):
                self.c.operation('youtube', 'read', self.url)

    def test_subtitle_login_stops_comments_but_preserves_description(self):
        def operation(platform, action, target, limit=5):
            if action == 'read':
                return [item(self.url, text='视频描述', scope='description')]
            if action == 'subtitle':
                raise Failure('NEED_LOGIN', 'login')
            raise AssertionError('must not request comments after login restriction')
        with patch.object(self.c, 'operation', side_effect=operation):
            value = self.c.extract(self.url, subtitles=True, comments=True)[0]
        self.assertEqual(value['text'], '视频描述')
        self.assertEqual(value['coverage']['comments'], 'stopped')
        self.assertEqual(status_for([value]), 'partial')

    def test_search_platform_failure_keeps_other_results(self):
        def operation(platform, action, target, limit=5):
            if platform == 'x':
                raise Failure('NEED_LOGIN', 'login')
            return [item(self.url, title='fixture hit', scope='search_hit')]
        with patch.object(self.c, 'operation', side_effect=operation):
            rows, outcomes = self.c.search('coffee', ['x', 'youtube'], limit=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(outcomes['x']['error']['code'], 'NEED_LOGIN')

    def test_unverified_connector_never_routed(self):
        self.config['connectors'] = {'wechat': {'command': ['/bin/false'], 'actions': ['read'], 'paid': False}}
        self.assertNotIn('verified-connector', [name for name, _ in self.c.p.routes('wechat', 'read')])

    def test_empty_title_not_fulltext_and_output_truncation(self):
        row = item(self.url, title='title only', scope='description')
        self.assertEqual(status_for([row]), 'partial')
        row['text'] = 'a' * 50000
        value = bounded({'items': [row], 'status': 'complete'}, 2500)
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False)), 2500)
        self.assertTrue(value['output_truncated'])

    def test_private_access_token_not_exported(self):
        row = item('https://www.xiaohongshu.com/explore/abc', text='test')
        row['_access_url'] = row['url'] + '?xsec_token=secret'
        value = public_result(row)
        self.assertNotIn('secret', json.dumps(value))
        self.assertIn('来源', markdown({'items': [row], 'status': 'partial'}))

    def test_status_uses_real_action_evidence_and_no_network(self):
        row = item(self.url, text='Fixture description', scope='description')
        with patch.object(self.c.p, 'routes', return_value=[('fixture', lambda *a: [row])]):
            self.c.operation('youtube', 'read', self.url)
        self.r.state.block('x', Failure('NEED_LOGIN', 'fixture'))
        with patch.object(self.r, 'command', side_effect=AssertionError('status must be offline')):
            result = platform_status(self.c)
        evidence = result['youtube']['checks']['read']
        self.assertEqual(evidence['status'], 'content_returned')
        self.assertEqual(evidence['scope'], ['description'])
        self.assertEqual(result['x']['gate']['code'], 'NEED_LOGIN')
        self.assertEqual(result['xiaohongshu']['checks'], {})

    def test_check_refreshes_cache_but_never_resumes_gate(self):
        row = item(self.url, text='Fixture description')
        argv = ['--state-dir', self.directory.name, 'check', 'youtube', 'read', self.url]
        with patch('sc_providers.Providers.routes', return_value=[('fixture', lambda *a: [row])]):
            self.c.operation('youtube', 'read', self.url)
        self.r.state.block('youtube', Failure('NEED_LOGIN', 'fixture'))
        with patch('sc_providers.Providers.routes', side_effect=AssertionError('must remain blocked')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(argv), 2)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'NEED_LOGIN')

    def test_check_does_not_accept_wrong_platform_url(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--state-dir', self.directory.name, 'check', 'x', 'read', self.url])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'BAD_INPUT')

    def test_login_opens_official_page_without_resuming(self):
        self.r.state.block('bilibili', Failure('NEED_LOGIN', 'fixture'))
        with patch('sc_providers.Providers.browser_command', return_value={'ok': True, 'status': 'waiting_for_human'}) as opened:
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--state-dir', self.directory.name, 'login', 'bilibili'])
        self.assertEqual(code, 0)
        self.assertEqual(opened.call_args.args[:2], ('login', 'https://passport.bilibili.com/login'))
        self.assertEqual(self.r.state.inspection('bilibili')['gate']['code'], 'NEED_LOGIN')

    def test_legacy_enrichment_uses_resolved_bv_identity(self):
        original = 'https://www.bilibili.com/video/av80433022'
        resolved = 'https://www.bilibili.com/video/BV1GJ411x7h7'
        targets = []
        def operation(platform, action, target, limit=5):
            targets.append((action, target))
            return [item(resolved, text='fixture')]
        with patch.object(self.c, 'operation', side_effect=operation):
            self.c.extract(original, subtitles=True)
        self.assertEqual(targets, [('read', original), ('subtitle', resolved)])

    def test_comment_limit_is_enforced_even_when_upstream_returns_more(self):
        rows = [item(self.url, text='fixture ' + str(i), scope='comment') for i in range(8)]
        with patch.object(self.c.p, 'routes', return_value=[('fixture', lambda *a: rows)]):
            result = self.c.operation('youtube', 'comments', self.url, 3)
        self.assertEqual(len(result), 3)
        self.assertEqual(self.r.state.inspection('youtube')['checks']['comments']['count'], 3)


if __name__ == '__main__':
    unittest.main()
