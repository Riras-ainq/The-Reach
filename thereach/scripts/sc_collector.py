"""Routing, component cache, stop conditions, and cross-platform aggregation."""
from pathlib import Path
import tempfile

from sc_core import (Failure, STOP, TERMINAL, canonical, deduplicate, digest, input_url,
                     platform_for, short_link)
from sc_providers import Providers


class Collector:
    def __init__(self, runtime, allow_paid=False, refresh=False):
        self.r, self.c = runtime, runtime.config
        self.p = Providers(runtime, allow_paid)
        self.allow_paid, self.refresh = allow_paid, refresh
        self.cache_hits = 0

    def operation(self, platform, action, target, limit=5):
        key = digest(['v2', platform, action, target, limit, self.c.get('youtube_api'),
                      self.c.get('x_api'), self.c['connectors']])
        cached = None if self.refresh else self.r.state.cached(key)
        if cached is not None:
            if 'failure' in cached:
                e = cached['failure']
                raise Failure(e['code'], '短期失败缓存：' + e['message'], e.get('retry_after_seconds', 0))
            self.cache_hits += 1
            for entry in cached['items']:
                entry['from_cache'] = True
            return cached['items']
        self.r.state.gate(platform)
        routes = self.p.routes(platform, action, target)
        if not routes:
            raise Failure('UNSUPPORTED', '该平台没有已验证的 ' + action + ' 路线；需授权连接器或用户提供素材')
        failures = []
        for name, fn in routes:
            try:
                with self.r.attempt(platform, name, action):
                    rows = fn(platform, action, target, limit)
                    if action in ('search', 'comments'):
                        rows = rows[:limit]
                    if not rows and action not in ('search', 'comments'):
                        raise Failure('EMPTY_CONTENT', '上游未返回内容')
                    for entry in rows:
                        entry['provenance'].append({'backend': name, 'action': action})
                    self.r.state.record_check(platform, action, {'status': 'content_returned' if any(x.get('text') for x in rows) else 'empty_text',
                        'backend': name, 'count': len(rows), 'text_chars': sum(len(x.get('text', '')) for x in rows),
                        'scope': sorted(set(x.get('text_scope', '') for x in rows)),
                        'urls': [canonical(x['url']) for x in rows[:3]]})
                    if failures:
                        for entry in rows:
                            entry['warnings'].append('经降级取得；先前失败：' + ', '.join(failures))
                    self.r.state.save(key, {'items': rows}, self.c['cache_ttl'])
                    return rows
            except Failure as e:
                failures.append(e.code)
                self.r.state.record_check(platform, action, {'status': 'failed', 'backend': name, 'error': e.as_dict()})
                if e.code in STOP:
                    raise
        failure = Failure(failures[-1] if failures else 'UNSUPPORTED', '所有允许路线失败：' + ', '.join(failures))
        self.r.state.save(key, {'failure': failure.as_dict()}, self.c['failure_ttl'])
        raise failure

    def resolve(self, url):
        if not short_link(url):
            return url
        platform = platform_for(url)
        key = digest(['resolve-v1', url])
        cached = self.r.state.cached(key)
        if cached:
            self.cache_hits += 1
            return cached['url']
        with self.r.attempt(platform, 'http-redirect', 'resolve'):
            final, _, _ = self.r.http(url, platform=platform)
            self.r.state.save(key, {'url': final}, self.c['cache_ttl'])
            return final

    def extract(self, text, subtitles=False, comments=False, transcribe=False, ocr=False, vision=False):
        url = self.resolve(input_url(text))
        platform = platform_for(url)
        rows = self.operation(platform, 'read', url)
        if not rows:
            raise Failure('EMPTY_CONTENT', '没有取得内容')
        # Threads can include replies: select matching root and handle replies separately.
        root = next((x for x in rows if x['url'] == canonical(url)), rows[0])
        requested = [('subtitles', subtitles, 'subtitle'), ('comments', comments, 'comments')]
        stopped = False
        for component, enabled, action in requested:
            if not enabled:
                continue
            if stopped:
                root['coverage'][component] = 'stopped'
                continue
            try:
                values = self.operation(platform, action, root.get('_access_url', root['url']), self.c['max_comments'] if component == 'comments' else 5)
                if component == 'subtitles':
                    root[component] = [{'text': x['text'], 'scope': 'returned_track',
                                       'provenance': x['provenance']} for x in values if x['text']]
                else:
                    if platform == 'x':
                        values = [x for x in values if x['url'] != canonical(url)]
                    root[component] = [{'id': x.get('comment_id') or digest([x['url'], x['text'], x['author'], x['published_at']])[:20],
                                        'id_source': 'platform' if x.get('comment_id') else 'derived', 'text': x['text'], 'author': x['author'],
                                        'url': x['url'], 'parent_id': x.get('parent_id'), 'published_at': x['published_at']} for x in values[:self.c['max_comments']]]
                    root['warnings'].append('评论仅覆盖实际返回的有限样本；不保证完整评论或回复树')
                root['coverage'][component] = 'available' if root[component] else 'empty'
            except Failure as e:
                root['coverage'][component] = e.code
                root['warnings'].append(e.as_dict())
                # Missing subtitle tracks are a content condition; ASR/comments may still work.
                stopped = e.code in STOP - {'NOT_FOUND'}
        if transcribe:
            if stopped:
                root['coverage']['transcript'] = 'stopped'
            elif root['subtitles']:
                root['coverage']['transcript'] = 'skipped_subtitles_available'
            else:
                try:
                    root['transcript'] = self.transcribe(url, platform)
                    root['coverage']['transcript'] = 'available'
                except Failure as e:
                    root['coverage']['transcript'] = e.code
                    root['warnings'].append(e.as_dict())
                    stopped = e.code in STOP
        for component, enabled in (('ocr', ocr), ('vision', vision)):
            if enabled:
                if stopped:
                    root['coverage'][component] = 'stopped'
                else:
                    try:
                        self.analyze_images(root, component)
                    except Failure as e:
                        root['coverage'][component] = e.code
                        root['warnings'].append(e.as_dict())
                        stopped = e.code in STOP
        return [root]

    def transcribe(self, target, platform):
        spec = self.c.get('transcribe_command')
        with self.r.attempt(platform, 'configured-asr' if spec else 'agent-reach', 'transcribe'):
            if not self.allow_paid:
                raise Failure('NEED_SETUP', '转录默认可能上传素材并消耗模型额度；需 --allow-paid')
            if spec:
                data = self.r.command_json(spec, {'action': 'transcribe', 'target': target,
                    'timeout_seconds': self.r.remaining(), 'read_only': True}, paid=True)
                value = data.get('text')
            elif platform == 'youtube':
                data = self.r.command_json([self.c['agent_reach'], 'youtube', 'transcribe', target,
                    '--timeout', str(max(1, int(self.r.remaining())))], paid=True)
                value = '\n'.join(x.get('text', '') for x in data.get('items', []))
            else:
                raise Failure('UNSUPPORTED', '当前平台未配置获授权的转录服务；可用 media 处理已有本地音视频')
            if not isinstance(value, str) or not value.strip():
                raise Failure('EMPTY_CONTENT', '转录服务未返回文本')
            return value.strip()

    def local_analyze(self, path, component):
        path = Path(path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > self.c['max_media_bytes']:
            raise Failure('BAD_INPUT', '本地文件不存在或超过 max_media_bytes')
        self.r.add_bytes(path.stat().st_size)
        cache_key = digest(['media-v1', component, digest(path.read_bytes().hex()), self.c.get(component + '_command')])
        cached = None if self.refresh else self.r.state.cached(cache_key)
        if cached:
            self.cache_hits += 1
            return cached
        spec = self.c.get(component + '_command')
        if component in ('ocr', 'vision') and not spec:
            swift = Path(__file__).with_name('vision_ocr.swift')
            argv = ['/usr/bin/swift', '-module-cache-path', str(self.r.state.directory / 'swift-cache'), str(swift), str(path), component]
            data = self.r.command_json(argv)
        else:
            if not spec:
                raise Failure('NEED_SETUP', '未配置 ' + component + '_command')
            if not self.allow_paid:
                raise Failure('NEED_SETUP', '外部识别服务需 --allow-paid 与付费预算；可能上传素材')
            data = self.r.command_json(spec, {'action': component, 'path': str(path),
                'timeout_seconds': self.r.remaining(), 'read_only': True}, paid=True)
        if not isinstance(data.get('text'), str) or not data['text'].strip():
            raise Failure('EMPTY_CONTENT', '识别器没有返回文本')
        result = {'text': data['text'], 'scope': 'ocr' if component == 'ocr' else ('image_classification' if not spec else 'model_interpretation'),
                  'engine': data.get('engine') or ('apple-vision' if not spec else 'configured-command')}
        if data.get('labels'):
            result['labels'] = data['labels']
        self.r.state.save(cache_key, result, self.c['cache_ttl'])
        return result

    def analyze_images(self, root, component):
        urls = root['images'][:self.c['max_images']]
        if not urls:
            root['coverage'][component] = 'no_images_returned'
            return
        platform = root['platform']
        root['coverage'][component] = 'available'
        for url in urls:
            with self.r.attempt(platform, 'media-http', component):
                _, data, kind = self.r.http(url, media=True)
                if not kind.startswith('image/'):
                    raise Failure('INVALID_RESPONSE', '媒体响应不是图片')
                with tempfile.TemporaryDirectory(dir=str(self.r.state.directory)) as directory:
                    path = Path(directory) / 'image'
                    path.write_bytes(data)
                    result = self.local_analyze(path, component)
                root[component].append({'url': url, **result})
        if len(root['images']) > len(urls):
            root['warnings'].append('图片处理达到 max_images 上限，未处理全部图片')

    def search(self, query, platforms, limit=3, enrich=0, **flags):
        if not query.strip() or query.startswith('-') or len(query) > 500:
            raise Failure('BAD_INPUT', '关键词不能为空、不能以 - 开头，最长 500 字符')
        results, outcomes = [], {}
        for platform in platforms:
            try:
                rows = self.operation(platform, 'search', query, limit)
                indexed = platform == 'wechat' or any(p['backend'] == 'web-index' for row in rows for p in row['provenance'])
                outcomes[platform] = {'status': 'ok', 'count': len(rows), 'scope': 'indexed_search' if indexed else 'returned_search'}
                results.extend(rows[:limit])
            except Failure as e:
                outcomes[platform] = {'status': 'failed', 'error': e.as_dict()}
                if e.code in ('BUDGET_EXCEEDED', 'OUTPUT_LIMIT'):
                    for pending in platforms[platforms.index(platform) + 1:]:
                        outcomes[pending] = {'status': 'not_attempted', 'error': e.as_dict()}
                    break
        results = deduplicate(results)
        for entry in results[:enrich]:
            try:
                detail = self.extract(entry.get('_access_url', entry['url']), **flags)[0]
                entry.update(detail)
            except Failure as e:
                entry['warnings'].append(e.as_dict())
        return deduplicate(results), outcomes

    def probe(self, platform, action, target, expected_url, expected_text):
        if len(expected_text.strip()) < 12:
            raise Failure('BAD_INPUT', '验权需至少 12 字符的已知原文，不接受通用成功提示')
        if platform_for(expected_url) != platform:
            raise Failure('BAD_INPUT', '证据 URL 与平台不匹配')
        with self.r.attempt(platform, 'connector-probe', action):
            rows = self.p.connector(platform, action, target, 3, probing=True)
            match = next((x for x in rows if canonical(x['url']) == canonical(expected_url) and expected_text in x['text']), None)
            if not match:
                raise Failure('UNVERIFIED_ACCESS', '未从指定样本取得匹配原文；不启用该动作')
            evidence = {'url': canonical(expected_url), 'action': action, 'sample_text_sha256': digest(expected_text),
                        'scope': 'single_sample_only', 'expires_in_days': 7}
            self.r.state.record_probe(self.p.connector_key(platform, action), evidence)
            return evidence

    def metrics(self):
        return {**self.r.metrics(), 'cache_hits': self.cache_hits}


def public_result(value):
    """Private cache may preserve access URLs; exports never contain internal fields."""
    if isinstance(value, dict):
        return {k: public_result(v) for k, v in value.items() if not k.startswith('_')}
    if isinstance(value, list):
        return [public_result(x) for x in value]
    return value
