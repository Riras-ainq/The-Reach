"""Offline tests of observed DOM contracts; no browser, session, or network."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'thereach/scripts'))
from reach_browser import EXTRACT_JS, bilibili_rows
from sc_runtime import executable


class BrowserTests(unittest.TestCase):
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
