import contextlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'thereach/scripts'))
from sc_core import DEFAULTS, Failure, item, classify_text
from sc_runtime import Runtime, load_config
from sc_collector import Collector
from collect import main


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = dict(DEFAULTS, state_dir=str(self.root / 'state'), min_interval=0)
        self.r = Runtime(self.config)
        self.c = Collector(self.r)

    def tearDown(self):
        self.directory.cleanup()

    def connector(self, rows):
        path = self.root / 'client.py'
        path.write_text('import json,sys\np=json.load(sys.stdin)\nprint(' + repr(json.dumps({'ok': True, 'items': rows}, ensure_ascii=False)) + ')\n')
        self.config['connectors'] = {'wechat': {'command': [sys.executable, str(path)], 'actions': ['read'], 'paid': False}}
        return path

    def test_probe_content_match_required_and_config_changes_revoke(self):
        url = 'https://mp.weixin.qq.com/s/fixture-only'
        self.connector([{'url': url, 'text_scope': 'body', 'text': '这是仅用于离线测试的已知原文片段'}])
        self.assertFalse(self.c.p.has_connector('wechat', 'read'))
        with self.assertRaises(Failure):
            self.c.probe('wechat', 'read', url, url, '不匹配的原文不可以通过这个测试')
        result = self.c.probe('wechat', 'read', url, url, '这是仅用于离线测试的已知原文片段')
        self.assertEqual(result['scope'], 'single_sample_only')
        self.assertTrue(self.c.p.has_connector('wechat', 'read'))
        self.config['connectors']['wechat']['command'].append('changed')
        self.assertFalse(self.c.p.has_connector('wechat', 'read'))

    def test_generated_summary_never_passes_read_probe(self):
        url = 'https://mp.weixin.qq.com/s/fixture-only'
        self.connector([{'url': url, 'text_scope': 'summary', 'text': '这是生成的总结文本而不是原文内容'}])
        with self.assertRaises(Failure) as caught:
            self.c.probe('wechat', 'read', url, url, '这是生成的总结文本而不是原文内容')
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertFalse(self.c.p.has_connector('wechat', 'read'))

    def test_official_youtube_maps_description_and_comments(self):
        with patch.dict('os.environ', {'THEREACH_YOUTUBE_KEY': 'test-only-placeholder'}):
            with patch.object(self.r, 'json_http', return_value={'items': [{'id': 'abcdefghijk', 'snippet': {'title': 'Fixture title', 'description': 'Fixture description'}}]}) as call:
                rows = self.c.p.youtube_api('youtube', 'read', 'https://youtu.be/abcdefghijk', 2)
                self.assertEqual(rows[0]['text_scope'], 'description')
                self.assertIn('/videos?', call.call_args.args[0])
            response = {'items': [{'snippet': {'topLevelComment': {'id': 'c1', 'snippet': {'textOriginal': 'Real comment field', 'authorDisplayName': 'Tester'}}, 'totalReplyCount': 8}}]}
            with patch.object(self.r, 'json_http', return_value=response):
                rows = self.c.p.youtube_api('youtube', 'comments', 'https://youtu.be/abcdefghijk', 2)
                self.assertEqual(rows[0]['comment_id'], 'c1')
                self.assertEqual(rows[0]['reply_count'], 8)

    def test_ytdlp_comment_ids_survive_normalization(self):
        response = {'comments': [{'id': 'first', 'parent': 'root', 'text': 'first comment', 'author': 'Tester'},
                                 {'id': 'second', 'parent': 'first', 'text': 'reply', 'author': 'Other'}]}
        with patch('sc_providers.executable', return_value=sys.executable), patch.object(self.r, 'command_json', return_value=response):
            rows = self.c.p.ytdlp('youtube', 'comments', 'https://youtu.be/abcdefghijk', 5)
        self.assertEqual([x['comment_id'] for x in rows], ['first', 'second'])
        self.assertEqual(rows[1]['parent_id'], 'first')

    def test_x_thread_reply_ids_survive_action_mapping(self):
        target = 'https://x.com/jack/status/20'
        response = {'items': [{'url': 'https://x.com/person/status/21', 'text': 'fixture reply', 'raw': {'id': '21', 'in_reply_to': '20'}}]}
        with patch.object(self.r, 'command_json', return_value=response):
            rows = self.c.p.reach('x', 'comments', target, 5)
        self.assertEqual(rows[0]['text_scope'], 'comment')
        self.assertEqual(rows[0]['comment_id'], '21')
        self.assertEqual(rows[0]['parent_id'], '20')

    def test_x_root_only_thread_is_an_empty_comment_sample(self):
        target = 'https://x.com/jack/status/20'
        with patch.object(self.r, 'command_json', return_value={'items': [{'url': target, 'text': 'root only', 'raw': {'id': '20'}}]}):
            rows = self.c.p.reach('x', 'comments', target, 3)
        self.assertEqual(rows, [])

    def test_xhs_search_enrichment_preserves_signature_but_export_hides_it(self):
        signed = 'https://www.xiaohongshu.com/search_result/abc?xsec_token=fixture-only'
        def upstream(argv, *args, **kwargs):
            action, target = argv[2:4]
            if action == 'search':
                return {'items': [{'url': signed, 'title': 'fixture note'}]}
            self.assertEqual(target, signed)
            return {'items': [{'url': signed, 'text': 'fixture ' + action}]}
        with patch.object(self.r, 'command_json', side_effect=upstream) as call:
            rows, outcomes = self.c.search('fixture', ['xiaohongshu'], limit=1, enrich=1, comments=True)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(outcomes['xiaohongshu']['status'], 'ok')
        self.assertEqual(rows[0]['coverage']['comments'], 'available')
        from sc_collector import public_result
        self.assertNotIn('fixture-only', json.dumps(public_result(rows)))

    def test_xhs_unsigned_export_url_is_input_error_without_network_or_fallback(self):
        for action in ('read', 'comments'):
            with self.subTest(action=action), patch.object(self.r, 'command_json', side_effect=AssertionError('no network')):
                with self.assertRaises(Failure) as caught:
                    self.c.operation('xiaohongshu', action, 'https://www.xiaohongshu.com/explore/abc')
                self.assertEqual(caught.exception.code, 'BAD_INPUT')
                self.assertIn('xsec_token', caught.exception.message)
                self.assertIsNone(self.r.state.inspection('xiaohongshu')['gate'])

    def test_missing_subtitles_can_transcribe(self):
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        def operation(platform, action, target, limit=5):
            if action == 'read':
                return [item(url, text='fixture description')]
            raise Failure('NOT_FOUND', 'no subtitle tracks')
        with patch.object(self.c, 'operation', side_effect=operation), patch.object(self.c, 'transcribe', return_value='fixture ASR text') as asr:
            result = self.c.extract(url, subtitles=True, transcribe=True)[0]
        self.assertEqual(result['transcript'], 'fixture ASR text')
        self.assertTrue(asr.called)

    def test_local_permission_not_platform_timeout(self):
        text = 'Traceback (most recent call last):\n function(timeout=timeout)\nPermissionError: [Errno 1] Operation not permitted'
        self.assertEqual(classify_text(text).code, 'LOCAL_PERMISSION')

    def test_legacy_bilibili_url_does_not_reach_bv_only_adapter(self):
        routes = self.c.p.routes('bilibili', 'read', 'https://www.bilibili.com/video/av80433022')
        self.assertEqual(routes[0][0], 'bilibili-view')
        with patch.object(self.r, 'json_http', return_value={'code': 0, 'data': {'bvid': 'BV1GJ411x7h7', 'title': 'Fixture title', 'desc': 'Fixture description'}}):
            values = self.c.p.bilibili_legacy('bilibili', 'read', 'https://www.bilibili.com/video/av80433022?p=2', 1)
        self.assertEqual(values[0]['url'], 'https://www.bilibili.com/video/BV1GJ411x7h7?p=2')

    def test_cli_failure_exports_and_no_overwrite(self):
        prefix = self.root / 'failure'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = main(['--state-dir', str(self.root / 'state2'), 'extract', 'invalid', '--output', str(prefix)])
        self.assertEqual(status, 2)
        self.assertFalse(json.loads(Path(str(prefix) + '.json').read_text())['ok'])
        self.assertIn('BAD_INPUT', Path(str(prefix) + '.md').read_text())
        original = Path(str(prefix) + '.json').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            main(['--state-dir', str(self.root / 'state2'), 'extract', 'invalid', '--output', str(prefix)])
        self.assertEqual(Path(str(prefix) + '.json').read_bytes(), original)

    def test_config_relative_to_config_not_cwd(self):
        p = self.root / 'config.json'
        p.write_text('{"state_dir":"cache"}')
        self.assertEqual(load_config(str(p))['state_dir'], str((self.root / 'cache').resolve()))

    def test_install_links_same_source_and_collision_refused(self):
        spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install_skill.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        roots = [self.root / 'codex/skills', self.root / 'hermes/skills']
        roots[0].mkdir(parents=True)
        legacy = roots[0] / 'social-collector'
        legacy.symlink_to(self.root / 'missing/social-collector')
        preview = installer.install(roots, check=True)
        self.assertTrue(legacy.is_symlink())
        self.assertFalse((roots[0] / 'thereach').is_symlink())
        self.assertEqual(preview['entries'][0]['stale_legacy'], str(legacy))
        result = installer.install(roots)
        self.assertFalse(legacy.is_symlink())
        self.assertTrue((roots[0] / 'thereach/SKILL.md').is_file())
        self.assertEqual(installer.install(roots)['entries'][0]['status'], 'already_linked')
        self.assertTrue(result['ok'])
        self.assertEqual((roots[0] / 'thereach').resolve(), (roots[1] / 'thereach').resolve())
        collision = self.root / 'existing/skills/thereach'
        collision.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            installer.install([collision.parent])


if __name__ == '__main__':
    unittest.main()
