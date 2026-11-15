"""Bilibili audio acquisition and on-device Apple ASR. No third-party uploads."""
import hashlib
import math
from pathlib import Path
import platform
import time
from urllib.parse import urlsplit

from sc_core import Failure, canonical, digest, host_matches
from sc_runtime import executable


def file_hash(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def validate_result(data):
    text, segments = data.get('text'), data.get('segments')
    duration = data.get('duration_seconds')
    if not isinstance(text, str) or not text.strip() or not isinstance(segments, list) or not segments:
        raise Failure('EMPTY_CONTENT', 'Apple 转写未返回带时间戳的原始识别稿')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise Failure('INVALID_RESPONSE', '转写器未返回有效音频时长')
    previous = 0
    for row in segments:
        start, end = row.get('start'), row.get('end')
        if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end))
                or start < previous - .1 or not 0 <= start <= end <= duration + 1
                or not isinstance(row.get('text'), str) or not row['text'].strip()):
            raise Failure('INVALID_RESPONSE', '转写时间戳缺失、越界或顺序异常')
        previous = end
    return data


class NativeASR:
    def __init__(self, runtime, providers, refresh=False):
        self.r, self.p, self.refresh = runtime, providers, refresh
        self.cache_hits = 0
        self.script = Path(__file__).with_name('speech_asr.swift')

    def apple(self, argument):
        if platform.system() != 'Darwin' or not executable('/usr/bin/swift'):
            raise Failure('NEED_SETUP', 'Apple 本机转写需要 macOS 26+、Swift 和已安装的中文语音资源')
        # Local ASR may exceed the ordinary 25-second request timeout. It is
        # still strictly bounded by the caller's overall --deadline.
        return self.r.command_json(['/usr/bin/swift', '-module-cache-path',
            str(self.r.state.directory / 'swift-cache'), str(self.script), str(argument)],
            timeout=max(.01, self.r.end - time.monotonic()))

    def check(self):
        return self.apple('--check')

    def bilibili_audio(self, target):
        self.r.state.gate('bilibili')
        target = canonical(target)
        cache_key = digest(['bilibili-audio-v1', target])
        cached = None if self.refresh else self.r.state.cached(cache_key)
        directory = self.r.state.directory / 'audio'
        if cached:
            path = Path(cached['path'])
            if (path.is_file() and path.resolve().parent == directory.resolve()
                    and path.stat().st_size <= self.r.config['max_media_bytes']
                    and file_hash(path) == cached['sha256']):
                self.cache_hits += 1
                return path, dict(cached['metadata'], audio_cache_hit=True)
        with self.r.attempt('bilibili', 'reach-browser', 'audio'):
            info = self.p.browser_command('audio', target).get('audio') or {}
            parsed = urlsplit(info.get('url', ''))
            if parsed.scheme != 'https' or not any(host_matches(parsed.hostname or '', host) for host in ('bilivideo.com', 'bilivideo.cn')):
                raise Failure('INVALID_RESPONSE', '音轨不是受支持的 B站 HTTPS 媒体地址')
            if canonical(info.get('source_url', '')) != target or not info.get('cid'):
                raise Failure('INVALID_RESPONSE', '音轨来源与目标视频不匹配')
            duration = info.get('duration_seconds')
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                raise Failure('INVALID_RESPONSE', '音轨缺少有效视频时长')
            size = info.get('bandwidth', 0) / 8 * duration
            if size > min(self.r.config['max_media_bytes'], self.r.config['max_bytes'] - self.r.bytes):
                raise Failure('OUTPUT_LIMIT', '音轨估算大小超出媒体或总字节预算')
            _, audio, kind = self.r.http(info['url'], media=True,
                headers={'Referer': target, 'User-Agent': 'Mozilla/5.0'})
            if not (kind.startswith('audio/') or kind.startswith('video/mp4') or kind.startswith('application/octet-stream')):
                raise Failure('INVALID_RESPONSE', '音轨响应不是可识别的媒体类型')
            if not audio:
                raise Failure('EMPTY_CONTENT', '音轨下载为空')
            checksum = hashlib.sha256(audio).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (checksum + '.m4a')
            if not path.exists():
                with path.open('xb') as dest:
                    dest.write(audio)
            # The expiring signed media URL is deliberately never cached/exported.
            metadata = {k: info[k] for k in ('source_url', 'bvid', 'cid', 'part', 'duration_seconds')}
            metadata.update(audio_sha256=checksum, audio_bytes=len(audio), audio_cache_hit=False)
            self.r.state.save(cache_key, {'path': str(path), 'sha256': checksum, 'metadata': metadata},
                              self.r.config['cache_ttl'])
            return path, metadata

    def transcribe(self, target, source_platform):
        status = self.check()
        if not status.get('ready'):
            raise Failure('NEED_SETUP', 'Apple 中文语音资源尚未安装；不会自动下载模型，也不会切换云端服务')
        if source_platform == 'bilibili':
            path, metadata = self.bilibili_audio(target)
        elif source_platform == 'local':
            path = Path(target).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > self.r.config['max_media_bytes']:
                raise Failure('BAD_INPUT', '本地音频不存在或超出媒体预算')
            metadata = {'audio_sha256': file_hash(path), 'audio_bytes': path.stat().st_size,
                        'audio_cache_hit': False, 'source': 'local_file'}
        else:
            raise Failure('UNSUPPORTED', '原生 ASR 当前只接入本地文件和 B站音轨')
        cache_key = digest(['apple-asr-v1', metadata['audio_sha256'], 'zh_CN',
                            platform.mac_ver()[0], file_hash(self.script)])
        cached = None if self.refresh else self.r.state.cached(cache_key)
        if cached:
            self.cache_hits += 1
            return dict(cached, source=metadata, from_cache=True)
        with self.r.attempt('local', 'apple-speech-analyzer', 'transcribe'):
            result = validate_result(self.apple(path))
            expected = metadata.get('duration_seconds')
            if expected and abs(result['duration_seconds'] - expected) > 5:
                raise Failure('INVALID_RESPONSE', '音频实际时长与视频时长不符，拒绝把片段当作完整转写')
            result.update(source=metadata, scope='asr', from_cache=False)
            self.r.state.save(cache_key, result, self.r.config['cache_ttl'])
            return result
