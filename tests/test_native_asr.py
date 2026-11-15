"""Offline tests of native ASR boundaries; never downloads or starts speech models."""
import contextlib
import io
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
from sc_native_asr import validate_result
from collect import main, markdown


def asr_result():
    return {'ok': True, 'text': '测试转写', 'segments': [{'start': 0.5, 'end': 2, 'text': '测试转写'}],
            'duration_seconds': 3, 'engine': 'apple-speech-analyzer', 'scope': 'asr',
            'locale': 'zh_CN', 'uploads': False, 'model_downloaded': False}


class NativeASRTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = dict(DEFAULTS, state_dir=self.directory.name, min_interval=0)
        self.r = Runtime(self.config)
        self.c = Collector(self.r)
        self.url = 'https://www.bilibili.com/video/BV1LpNd6YEta?p=2'
        self.info = {'url': 'https://cdn.bilivideo.cn/audio.m4a?expires=private-fixture',
                     'bvid': 'BV1LpNd6YEta', 'cid': 123, 'part': 2, 'duration_seconds': 3,
                     'source_url': self.url, 'bandwidth': 10}

    def tearDown(self):
        self.r.state.close()
        self.directory.cleanup()

    def test_missing_system_resources_stops_before_media_download(self):
        with patch.object(self.c.native, 'check', return_value={'ready': False}), \
                patch.object(self.c.p, 'browser_command', side_effect=AssertionError('no download')):
            with self.assertRaises(Failure) as caught:
                self.c.transcribe(self.url, 'bilibili')
        self.assertEqual(caught.exception.code, 'NEED_SETUP')
        self.assertEqual(self.r.paid_calls, 0)

    def test_audio_cache_keeps_identity_but_not_signed_url(self):
        with patch.object(self.c.p, 'browser_command', return_value={'audio': self.info}) as browser, \
                patch.object(self.r, 'http', return_value=('', b'fixture audio', 'audio/mp4')) as download:
            path, metadata = self.c.native.bilibili_audio(self.url)
            again, cached = self.c.native.bilibili_audio(self.url)
        self.assertEqual(path, again)
        self.assertEqual(metadata['part'], 2)
        self.assertTrue(cached['audio_cache_hit'])
        self.assertEqual(browser.call_count, 1)
        self.assertEqual(download.call_count, 1)
        self.assertNotIn('private-fixture', json.dumps(metadata))
        # Corruption is detected; a later call must fetch the track again.
        path.write_bytes(b'corrupted')
        with patch.object(self.c.p, 'browser_command', side_effect=Failure('NETWORK_ERROR', 'fixture')):
            with self.assertRaises(Failure):
                self.c.native.bilibili_audio(self.url)

    def test_audio_gates_urls_size_and_identity(self):
        variants = [dict(self.info, url='https://evil.test/audio'),
                    dict(self.info, source_url=self.url.replace('p=2', 'p=1')),
                    dict(self.info, bandwidth=DEFAULTS['max_media_bytes'] * 100)]
        for info in variants:
            with self.subTest(info=info), patch.object(self.c.p, 'browser_command', return_value={'audio': info}), \
                    patch.object(self.r, 'http', side_effect=AssertionError('must reject before download')):
                with self.assertRaises(Failure):
                    self.c.native.bilibili_audio(self.url)
        self.r.state.block('bilibili', Failure('NEED_LOGIN', 'fixture login'))
        with patch.object(self.c.p, 'browser_command', side_effect=AssertionError('gate must stop browser')):
            with self.assertRaises(Failure) as caught:
                self.c.native.bilibili_audio(self.url)
        self.assertEqual(caught.exception.code, 'NEED_LOGIN')

    def test_native_result_cached_and_duration_checked(self):
        path = Path(self.directory.name) / 'audio.m4a'
        path.write_bytes(b'fixture')
        meta = {'audio_sha256': 'fixture', 'duration_seconds': 3}
        with patch.object(self.c.native, 'check', return_value={'ready': True}), \
                patch.object(self.c.native, 'bilibili_audio', return_value=(path, meta)), \
                patch.object(self.c.native, 'apple', return_value=asr_result()) as apple:
            result = self.c.native.transcribe(self.url, 'bilibili')
            cached = self.c.native.transcribe(self.url, 'bilibili')
        self.assertEqual(result['text'], '测试转写')
        self.assertTrue(cached['from_cache'])
        self.assertEqual(apple.call_count, 1)
        self.c.native.refresh = True
        with patch.object(self.c.native, 'check', return_value={'ready': True}), \
                patch.object(self.c.native, 'bilibili_audio', return_value=(path, dict(meta, duration_seconds=30))), \
                patch.object(self.c.native, 'apple', return_value=asr_result()):
            with self.assertRaises(Failure) as caught:
                self.c.native.transcribe(self.url, 'bilibili')
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')

    def test_invalid_or_nonmonotonic_timestamps_rejected(self):
        for segments in ([{'start': -1, 'end': 2, 'text': 'fixture'}],
                         [{'start': 1, 'end': 99, 'text': 'fixture'}],
                         [{'start': float('nan'), 'end': 2, 'text': 'fixture'}],
                         [{'start': 2, 'end': 3, 'text': '一'}, {'start': 0, 'end': 1, 'text': '二'}]):
            with self.subTest(segments=segments), self.assertRaises(Failure):
                validate_result(dict(asr_result(), segments=segments))

    def test_extract_and_local_cli_keep_asr_timestamps_and_provenance(self):
        def operation(platform, action, *args):
            if action == 'read':
                return [item(self.url, text='简介')]
            raise Failure('NOT_FOUND', 'fixture no subtitle')
        with patch.object(self.c, 'operation', side_effect=operation), \
                patch.object(self.c.native, 'transcribe', return_value=asr_result()):
            row = self.c.extract(self.url, transcribe=True)[0]
        self.assertEqual(row['transcript'], '测试转写')
        self.assertEqual(row['transcript_segments'][0]['start'], .5)
        self.assertIn('[00:00:00]', markdown({'items': [row]}))
        path = Path(self.directory.name) / 'fixture.wav'
        path.write_bytes(b'fixture')
        output = Path(self.directory.name) / 'result'
        with patch('sc_native_asr.NativeASR.transcribe', return_value=asr_result()), \
                contextlib.redirect_stdout(io.StringIO()):
            status = main(['--state-dir', self.directory.name, 'media', str(path), '--mode', 'transcribe', '--output', str(output)])
        self.assertEqual(status, 0)
        data = json.loads(output.with_suffix('.json').read_text())
        self.assertEqual(data['media_result']['segments'][0]['text'], '测试转写')
        self.assertIn('[00:00:00]', output.with_suffix('.md').read_text())

    def test_subtitles_win_without_downloading_or_transcribing(self):
        def operation(platform, action, *args):
            return [item(self.url, text='原始字幕' if action == 'subtitle' else '简介')]
        with patch.object(self.c, 'operation', side_effect=operation), \
                patch.object(self.c.native, 'transcribe', side_effect=AssertionError('subtitles must win')):
            row = self.c.extract(self.url, transcribe=True)[0]
        self.assertEqual(row['subtitles'][0]['text'], '原始字幕')
        self.assertEqual(row['coverage']['transcript'], 'skipped_subtitles_available')

    def test_missing_subtitle_is_short_cached_without_blocking_platform(self):
        def no_track(*args):
            raise Failure('NOT_FOUND', 'fixture no subtitle')
        with patch.object(self.c.p, 'routes', return_value=[('fixture', no_track)]):
            with self.assertRaises(Failure):
                self.c.operation('bilibili', 'subtitle', self.url)
        with patch.object(self.c.p, 'routes', side_effect=AssertionError('must reuse failure cache')):
            with self.assertRaises(Failure):
                self.c.operation('bilibili', 'subtitle', self.url)
        self.assertIsNone(self.r.state.inspection('bilibili')['gate'])


if __name__ == '__main__':
    unittest.main()
