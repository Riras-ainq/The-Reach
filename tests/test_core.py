import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'thereach/scripts'))
from sc_core import Failure, State, canonical, deduplicate, input_url, item, parse_page, platform_for


class CoreTests(unittest.TestCase):
    def test_six_platforms(self):
        examples = {'douyin': 'https://v.douyin.com/abc/', 'xiaohongshu': 'https://xhslink.com/a/abc',
            'bilibili': 'https://b23.tv/abc', 'wechat': 'https://mp.weixin.qq.com/s/abc',
            'youtube': 'https://youtu.be/abcdefghijk', 'x': 'https://x.com/a/status/123'}
        for platform, url in examples.items():
            with self.subTest(platform=platform):
                self.assertEqual(platform_for(input_url('分享给你 ' + url + '。')), platform)

    def test_spoofed_hosts_and_credentials_rejected(self):
        for url in ('https://douyin.com.evil.test/a', 'https://douyin.com@evil.test/a',
                    'file:///etc/passwd', 'https://user:pw@x.com/a', 'https://x.com:999/a'):
            with self.subTest(url=url), self.assertRaises(Failure):
                platform_for(url)

    def test_multiple_links_rejected(self):
        with self.assertRaises(Failure):
            input_url('https://x.com/a https://x.com/b')

    def test_access_query_not_identity(self):
        self.assertEqual(canonical('https://www.xiaohongshu.com/discovery/item/abc?xsec_token=secret&utm_source=x'),
                         'https://www.xiaohongshu.com/explore/abc')
        self.assertEqual(canonical('https://youtu.be/abcdefghijk?si=track'), 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(canonical('https://mp.weixin.qq.com/s?__biz=id&mid=123&idx=1&sn=abc&scene=1'),
                         'https://mp.weixin.qq.com/s?__biz=id&mid=123&idx=1&sn=abc')

    def test_article_body_no_navigation_script(self):
        page = '<title>测试</title><nav>菜单</nav><div id="js_content"><p>真实正文</p><img data-src="https://mmbiz.qpic.cn/a"><script>steal()</script><p>第二段</p></div><footer>尾部</footer>'
        value = parse_page('https://mp.weixin.qq.com/s/abc', page)
        self.assertEqual(value['text'], '真实正文\n第二段')
        self.assertEqual(value['images'], ['https://mmbiz.qpic.cn/a'])
        self.assertNotIn('steal', value['text'])

    def test_douyin_note_identity_preserved_in_export_and_dedup(self):
        note = 'https://www.douyin.com/note/1234567890123456789'
        video = 'https://www.douyin.com/video/1234567890123456789'
        self.assertEqual(canonical(note + '?utm_source=share'), note)
        self.assertEqual(canonical(video + '?utm_source=share'), video)
        exported = item(note, text='测试图文描述')
        self.assertEqual(exported['url'], note)
        self.assertEqual(len(deduplicate([exported, item(video, text='测试视频描述')])), 2)

    def test_shell_is_not_body(self):
        with self.assertRaises(Failure) as caught:
            parse_page('https://www.douyin.com/video/123', '<title>抖音</title><nav>登录 搜索 推荐</nav>')
        self.assertEqual(caught.exception.code, 'EMPTY_CONTENT')

    def test_captcha_is_terminal(self):
        with self.assertRaises(Failure) as caught:
            parse_page('https://mp.weixin.qq.com/s/abc', '<h1>环境异常，请完成安全验证</h1>')
        self.assertEqual(caught.exception.code, 'PLATFORM_BLOCKED')

    def test_jsonld_description_not_transcript(self):
        page = '<script type="application/ld+json">' + json.dumps({'@type': 'VideoObject', 'name': 'demo', 'description': '真实简介'}) + '</script>'
        value = parse_page('https://www.douyin.com/video/123', page)
        self.assertEqual(value['text_scope'], 'description')
        self.assertFalse(value['subtitles'])

    def test_dedup_canonical_and_exact_syndication(self):
        a = item('https://youtu.be/abcdefghijk', text='一段完整的内容' * 30)
        b = item('https://www.youtube.com/watch?v=abcdefghijk&si=tracking', text=a['text'])
        c = item('https://www.bilibili.com/video/BV123', text=a['text'])
        values = deduplicate([a, b, c])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['duplicates'][0]['platform'], 'bilibili')

    def test_state_cache_expiry_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            state.save('key', {'ok': True}, 100)
            self.assertTrue(state.cached('key')['ok'])
            state.save('expired', {'ok': True}, -1)
            self.assertIsNone(state.cached('expired'))
            state.block('youtube', Failure('RATE_LIMITED', 'stop', 1000))
            with self.assertRaises(Failure):
                state.resume('youtube')
            with self.assertRaises(Failure):
                State(directory).gate('youtube')

    def test_lease_cross_process_state(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = State(directory), State(directory)
            self.assertEqual(a.reserve('x', 'one', 20, 0), 0)
            with self.assertRaises(Failure) as caught:
                b.reserve('x', 'two', 20, 0)
            self.assertEqual(caught.exception.code, 'BUSY')
            a.release('x', 'one')
            self.assertGreater(b.reserve('x', 'two', 20, 10), 0)


if __name__ == '__main__':
    unittest.main()
