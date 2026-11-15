"""Offline tests of observed DOM contracts; no browser, session, or network."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'thereach/scripts'))
from reach_browser import EXTRACT_JS, bilibili_rows, browser_failure
from sc_runtime import executable


class BrowserTests(unittest.TestCase):
    def test_audio_adapter_selects_part_and_lowest_track_and_stops_on_limits(self):
        node = executable('node')
        if not node:
            self.skipTest('existing Node unavailable')
        adapter = Path(__file__).resolve().parents[1] / 'thereach/scripts/bilibili_audio.mjs'
        base_view = {'code': 0, 'data': {'bvid': 'BV1LpNd6YEta', 'title': 'fixture',
            'pages': [{'page': 1, 'cid': 11, 'duration': 10}, {'page': 2, 'cid': 22, 'duration': 30}]}}
        playback = {'code': 0, 'data': {'timelength': 30000, 'dash': {'audio': [
            {'baseUrl': 'https://cdn.bilivideo.com/high', 'bandwidth': 128000},
            {'baseUrl': 'https://cdn.bilivideo.com/low', 'bandwidth': 64000}]}}}
        cases = [(base_view, playback, None),
                 (base_view, {'code': -101}, 'NEED_LOGIN'),
                 (base_view, {'http_status': 412}, 'PLATFORM_BLOCKED'),
                 (base_view, {'http_status': 429}, 'RATE_LIMITED'),
                 (base_view, {'code': 0, 'data': {'timelength': 10000}}, 'PERMISSION_DENIED'),
                 (base_view, {'code': 0, 'data': {}}, 'NOT_FOUND'),
                 ({'code': 0, 'data': dict(base_view['data'], rights={'ugc_pay': 1})}, playback, 'PERMISSION_DENIED')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utils = root / 'utils.mjs'
            utils.write_text('export async function wbiSign(page, params) { return params; }')
            page = root / 'page.mjs'
            for view, play, error in cases:
                with self.subTest(error=error):
                    page.write_text('export class Page { async goto() {} async evaluate(code) { return code.includes("/x/web-interface/view") ? '
                        + json.dumps(view) + ' : ' + json.dumps(play) + '; }}')
                    proc = subprocess.run([node, str(adapter), page.as_uri(), utils.as_uri(), 'fixture-profile',
                        'https://www.bilibili.com/video/BV1LpNd6YEta?p=2'], capture_output=True, text=True, timeout=5)
                    data = json.loads(proc.stdout)
                    if error:
                        self.assertEqual(data['error']['code'], error)
                        self.assertEqual(proc.returncode, 2)
                    else:
                        self.assertEqual(data['audio']['cid'], 22)
                        self.assertEqual(data['audio']['url'], 'https://cdn.bilivideo.com/low')
                        self.assertEqual(proc.returncode, 0)

    def test_subtitle_empty_result_hint_is_not_tool_failure(self):
        exc = Exception('bilibili subtitle returned no data')
        exc.code = 'UPSTREAM_BROKEN'
        exc.hint = '此视频没有发现外挂或智能字幕。'
        target = 'https://www.bilibili.com/video/BV1LpNd6YEta'
        failure = browser_failure(exc, target, 'subtitle')
        self.assertEqual(failure.code, 'NOT_FOUND')
        self.assertIn('当前会话', failure.message)
        exc.hint = '字幕文件中没有字幕片段。'
        self.assertEqual(browser_failure(exc, target, 'subtitle').code, 'NOT_FOUND')
        # An empty comment result, a login requirement or an unrelated error
        # must never be converted to "no subtitles".
        self.assertEqual(browser_failure(exc, target, 'comments').code, 'UPSTREAM_BROKEN')
        exc.code = 'NEED_LOGIN'
        self.assertEqual(browser_failure(exc, target, 'subtitle').code, 'NEED_LOGIN')
        exc.code, exc.hint = 'UPSTREAM_BROKEN', 'unknown upstream error'
        self.assertEqual(browser_failure(exc, target, 'subtitle').code, 'UPSTREAM_BROKEN')

    def evaluate(self, setup):
        node = executable('node')
        if not node:
            self.skipTest('existing Node unavailable')
        source = ('globalThis.window={};globalThis.location={href:"https://www.douyin.com/video/123",pathname:"/video/123"};'
                  'globalThis.document={body:{innerText:"推荐菜单"},title:"page",getElementById:()=>null,'
                  'querySelector:()=>null,querySelectorAll:()=>[]};' + setup + ';console.log(JSON.stringify(' + EXTRACT_JS + '));')
        proc = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(proc.stdout)

    def test_douyin_observed_detail_hook_and_identity(self):
        setup = ('const container={getAttribute:()=>"123",querySelector:()=>({innerText:"实际视频描述"})};'
                 'document.querySelectorAll=s=>s.includes("detail-video-info")?[container]:[];')
        value = self.evaluate(setup)
        self.assertEqual(value['text'], '实际视频描述')
        value = self.evaluate(setup + 'container.getAttribute=()=>"456";')
        self.assertNotIn('text', value)

    def test_douyin_hydration_does_not_extract_recommended_video(self):
        value = self.evaluate('window.SSR_RENDER_DATA={app:{recommend:{aweme_id:"999",desc:"unrelated"},target:{aweme_id:"123",desc:"目标描述",author:{nickname:"作者"}}}};')
        self.assertEqual(value['text'], '目标描述')
        self.assertEqual(value['author'], '作者')

    def test_wechat_article_uses_article_title(self):
        value = self.evaluate('document.getElementById=id=>({js_content:{innerText:"真实正文",outerHTML:"<div id=js_content>真实正文</div>"},"activity-name":{innerText:"真实标题"},js_name:{innerText:"作者"}}[id]);')
        self.assertEqual(value['title'], '真实标题')
        self.assertIn('真实正文', value['html'])

    def test_bilibili_subtitle_and_comment_scopes(self):
        target = 'https://www.bilibili.com/video/BV1GJ411x7h7'
        rows = bilibili_rows([{'content': '第一行'}, {'content': '第二行'}], target, 'subtitle', 3)
        self.assertEqual(rows[0]['text'], '第一行\n第二行')
        self.assertEqual(rows[0]['text_scope'], 'subtitle')
        rows = bilibili_rows([{'rpid': '123', 'text': '评论', 'author': '测试'}], target, 'comments', 1)
        self.assertEqual(rows[0]['comment_id'], '123')
        self.assertEqual(rows[0]['text_scope'], 'comment')


if __name__ == '__main__':
    unittest.main()
