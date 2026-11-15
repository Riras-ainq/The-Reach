import json
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'thereach/scripts'))
from sc_core import DEFAULTS, Failure
from sc_runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = dict(DEFAULTS, state_dir=self.directory.name, min_interval=0)
        self.r = Runtime(self.config)

    def tearDown(self):
        self.directory.cleanup()

    def test_subprocess_unicode_and_no_shell(self):
        value = self.r.command_json([sys.executable, '-c', 'import json,sys;print(json.dumps({"arg":sys.argv[1],"text":"中文"}))', '$(touch /tmp/sc-should-not-exist)'])
        self.assertEqual(value['text'], '中文')
        self.assertEqual(value['arg'], '$(touch /tmp/sc-should-not-exist)')

    def test_process_timeout(self):
        self.config['timeout'] = .1
        started = time.monotonic()
        with self.assertRaises(Failure) as caught:
            self.r.command([sys.executable, '-c', 'import time; time.sleep(10)'])
        self.assertEqual(caught.exception.code, 'TIMEOUT')
        self.assertLess(time.monotonic() - started, 2)

    def test_process_output_cap(self):
        self.config['max_bytes'] = 2000
        with self.assertRaises(Failure) as caught:
            self.r.command([sys.executable, '-c', 'print("x" * 10000)'])
        self.assertEqual(caught.exception.code, 'OUTPUT_LIMIT')

    def test_native_timeout_can_extend_request_but_not_total_deadline(self):
        self.config['timeout'] = .02
        self.assertEqual(self.r.command([sys.executable, '-c', 'import time;time.sleep(.05);print("ok")'], timeout=1).strip(), 'ok')
        self.r.end = time.monotonic() + .1
        with self.assertRaises(Failure) as caught:
            self.r.command([sys.executable, '-c', 'import time;time.sleep(10)'], timeout=30)
        self.assertEqual(caught.exception.code, 'TIMEOUT')

    def test_no_retry_and_call_budget(self):
        self.config['max_calls'] = 1
        self.r.command([sys.executable, '-c', 'print("ok")'])
        with self.assertRaises(Failure) as caught:
            self.r.command([sys.executable, '-c', 'print("ok")'])
        self.assertEqual(caught.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(self.r.calls, 1)

    def test_paid_budget_does_not_launch(self):
        with self.assertRaises(Failure) as caught:
            self.r.command([sys.executable, '-c', 'print("ok")'], paid=True)
        self.assertEqual(caught.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(self.r.calls, 0)

    def test_json_upstream_error_not_success(self):
        code = 'import json;print(json.dumps({"ok":False,"error":{"code":"PLATFORM_BLOCKED","message":"captcha"}}))'
        with self.assertRaises(Failure) as caught:
            self.r.command_json([sys.executable, '-c', code])
        self.assertEqual(caught.exception.code, 'PLATFORM_BLOCKED')

    def test_structured_429_without_retry_after_has_fifteen_minute_cooldown(self):
        for exit_code in (0, 1):
            code = 'import json,sys;print(json.dumps({"ok":False,"error":{"code":"RATE_LIMITED","message":"HTTP 429","retry_after_seconds":0}}));sys.exit(' + str(exit_code) + ')'
            with self.subTest(exit_code=exit_code), patch('sc_core.time.time', return_value=1000):
                with self.assertRaises(Failure) as caught:
                    with self.r.attempt('youtube', 'fixture', 'subtitle'):
                        self.r.command_json([sys.executable, '-c', code])
                self.assertEqual(caught.exception.retry_after, 900)
                self.assertEqual(self.r.state.inspection('youtube')['gate']['until_time'], 1900)
                self.r.state.db.execute('DELETE FROM gates')
                self.r.state.db.commit()

    def test_ssrf_and_cross_platform_redirect_rejected(self):
        for url in ('https://127.0.0.1/a', 'https://evil.test/a'):
            with self.assertRaises(Failure):
                self.r.validate_http(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(Failure):
                self.r.validate_http('https://www.youtube.com/watch?v=abcdefghijk')
        with self.assertRaises(Failure):
            self.r.validate_http('https://x.com/a', platform='youtube')

    def test_redirect_hops_counted_and_authorization_not_forwarded(self):
        import io
        response = io.BytesIO(b'<html>fixture</html>')
        response.headers = {'Content-Type': 'text/html'}
        error = HTTPError('https://api.x.com/2/tweets/1', 302, 'redirect', {'Location': 'https://www.youtube.com/watch?v=abcdefghijk'}, io.BytesIO(b''))
        with patch.object(self.r, 'validate_http'), patch.object(self.r.opener, 'open', side_effect=[error, response]) as call:
            final, data, _ = self.r.http('https://api.x.com/2/tweets/1', headers={'Authorization': 'Bearer test-fixture'})
        self.assertEqual(self.r.calls, 2)
        self.assertTrue(final.startswith('https://www.youtube.com/'))
        self.assertIsNone(call.call_args_list[1].args[0].get_header('Authorization'))

    def test_429_retry_after_is_respected(self):
        error = HTTPError('https://x.com/a', 429, 'limited', {'Retry-After': '1800'}, io.BytesIO(b''))
        with patch.object(self.r, 'validate_http'), patch.object(self.r.opener, 'open', side_effect=error):
            with self.assertRaises(Failure) as caught:
                self.r.http('https://x.com/a')
        self.assertEqual(caught.exception.code, 'RATE_LIMITED')
        self.assertEqual(caught.exception.retry_after, 1800)


if __name__ == '__main__':
    unittest.main()
