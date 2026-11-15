"""Shared contract, canonical URLs, private state, and deterministic extraction."""
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLATFORMS = ('douyin', 'xiaohongshu', 'bilibili', 'wechat', 'youtube', 'x')
DOMAINS = {
    'douyin': ('douyin.com', 'iesdouyin.com'),
    'xiaohongshu': ('xiaohongshu.com', 'xhslink.com'),
    'bilibili': ('bilibili.com', 'b23.tv'),
    'wechat': ('mp.weixin.qq.com',),
    'youtube': ('youtube.com', 'youtu.be'),
    'x': ('x.com', 'twitter.com', 't.co'),
}
TERMINAL = {'PLATFORM_BLOCKED', 'RATE_LIMITED', 'NEED_LOGIN', 'PERMISSION_DENIED', 'BUSY'}
STOP = TERMINAL | {'BAD_INPUT', 'NOT_FOUND', 'BUDGET_EXCEEDED', 'OUTPUT_LIMIT', 'LOCAL_PERMISSION'}
DEFAULTS = {
    'timeout': 25, 'deadline': 100, 'max_calls': 12, 'max_paid_calls': 0,
    'max_bytes': 80_000_000, 'max_output_chars': 100_000, 'cache_ttl': 3600,
    'failure_ttl': 120, 'min_interval': 10, 'max_redirects': 5,
    'max_images': 3, 'max_comments': 20, 'max_media_bytes': 64_000_000,
    'agent_reach': 'agent-reach', 'china_read': 'china-read',
    'youtube_api': False, 'x_api': False, 'connectors': {},
}


class Failure(Exception):
    def __init__(self, code, message='', retry_after=0):
        self.code, self.message = code, str(message)
        self.retry_after = retry_after or (900 if code == 'RATE_LIMITED' else 0)
        super().__init__(self.message)

    def as_dict(self):
        return {'code': self.code, 'message': redact(self.message)[:400],
                'retry_after_seconds': self.retry_after,
                'human_action_required': self.code in TERMINAL - {'RATE_LIMITED', 'BUSY'}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def redact(value):
    s = str(value)
    s = re.sub(r'(?i)((?:xsec_token|access_token|auth_token|api_key|key|cookie|authorization|token)=)[^&\s"<>]+', r'\1[redacted]', s)
    s = re.sub(r'(?i)Bearer\s+[^\s"<>]+', 'Bearer [redacted]', s)
    return s


def host_matches(host, suffix):
    return host == suffix or host.endswith('.' + suffix)


def platform_for(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = (p.hostname or '').lower()
        for name, domains in DOMAINS.items():
            if any(host_matches(host, d) for d in domains):
                return name
    except ValueError:
        pass
    raise Failure('BAD_INPUT', '只接受六个平台的 HTTP(S) 链接；禁止凭据、任意端口和未知域名')


def input_url(text):
    urls = re.findall(r'https?://[^\s<>"\u3000]+', text)
    urls = list(dict.fromkeys(html.unescape(u.rstrip(".,;!?)\u3002\uff0c\uff1b\uff01\uff09\u3011'\u300d")) for u in urls))
    if len(urls) != 1:
        raise Failure('BAD_INPUT', '请提供恰好一个分享链接（可包含分享文案）')
    platform_for(urls[0])
    return urls[0]


def canonical(url):
    """Identity URL is separate from access URL: never drop access tokens before fetch."""
    name = platform_for(url)
    p = urlsplit(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    path = p.path.rstrip('/') or '/'
    if name == 'youtube':
        vid = path.strip('/') if p.hostname == 'youtu.be' else q.get('v')
        if not vid and path.startswith(('/shorts/', '/embed/', '/live/')):
            vid = path.split('/')[2]
        if vid:
            return 'https://www.youtube.com/watch?' + urlencode({'v': vid})
    if name == 'x':
        m = re.search(r'/status/(\d+)', path)
        if m:
            return 'https://x.com/i/status/' + m.group(1)
    if name == 'bilibili':
        m = re.search(r'/(BV[A-Za-z0-9]+|av\d+)', path)
        if m:
            return 'https://www.bilibili.com/video/' + m.group(1) + ('?p=' + q['p'] if 'p' in q else '')
    if name == 'xiaohongshu':
        m = re.search(r'/(?:explore|discovery/item|search_result)/([A-Za-z0-9_-]+)', path)
        if m:
            return 'https://www.xiaohongshu.com/explore/' + m.group(1)
    if name == 'douyin':
        vid = q.get('modal_id') or q.get('aweme_id')
        m = re.search(r'/(video|note)/(\d+)', path)
        if m:
            return 'https://www.douyin.com/' + m.group(1) + '/' + m.group(2)
        if vid:
            return 'https://www.douyin.com/video/' + vid
    keep = {k: v for k, v in q.items() if k in ('__biz', 'mid', 'idx', 'sn')}
    return urlunsplit(('https', p.netloc.lower(), path, urlencode(keep), ''))


def short_link(url):
    p = urlsplit(url)
    return p.hostname in ('b23.tv', 'youtu.be', 't.co', 'xhslink.com', 'v.douyin.com') or (p.hostname or '').endswith('.xhslink.com')


def clean(text):
    return '\n'.join(x.strip() for x in html.unescape(str(text or '')).splitlines() if x.strip())


def item(url, title='', text='', author='', published_at='', scope='body', images=None):
    identity = canonical(url)
    return {'id': digest(identity)[:20], 'platform': platform_for(url), 'url': identity,
            'title': clean(title), 'author': author or '', 'published_at': published_at or '',
            'text': clean(text), 'text_scope': scope, 'images': images or [],
            'subtitles': [], 'transcript': '', 'comments': [], 'ocr': [], 'vision': [],
            'coverage': {'text': 'available' if clean(text) else 'missing',
                         'subtitles': 'not_requested', 'transcript': 'not_requested',
                         'comments': 'not_requested', 'ocr': 'not_requested', 'vision': 'not_requested'},
            'provenance': [], 'warnings': []}


def deduplicate(items):
    """URL and substantial exact-text dedup; preserve provenance of syndicated copies."""
    out, urls, texts = [], {}, {}
    for entry in items:
        u = canonical(entry['url'])
        normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', entry.get('text', '')))
        h = digest(normalized) if len(normalized) >= 100 else None
        existing = urls.get(u) or (texts.get(h) if h else None)
        if existing is not None:
            if u != existing['url']:
                existing.setdefault('duplicates', []).append({'url': u, 'platform': entry['platform']})
            for p in entry.get('provenance', []):
                if p not in existing['provenance']:
                    existing['provenance'].append(p)
            continue
        out.append(entry)
        urls[u] = entry
        if h:
            texts[h] = entry
    return out


class Page(HTMLParser):
    """Deliberately accept known article containers, never whole-page navigation text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.active, self.parts, self.images = [], 0, [], []
        self.title, self.meta, self.jsonld = [], {}, []
        self.script = None
        self.title_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('meta', 'img', 'br', 'hr', 'input', 'source', 'link', 'wbr', 'area', 'base', 'embed', 'param', 'track'):
            if tag == 'meta':
                self.meta[a.get('property') or a.get('name')] = a.get('content', '')
            if self.active and tag == 'img':
                src = a.get('data-src') or a.get('src', '')
                if src.startswith('https://'):
                    self.images.append(src)
            if self.active and tag in ('br', 'hr'):
                self.parts.append('\n')
            return
        self.stack.append(tag)
        if a.get('id') in ('js_content', 'article_content') or tag == 'article':
            if not self.active:
                self.active = len(self.stack)
        if tag == 'title':
            self.title_depth = len(self.stack)
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.script = ''
        if self.active and tag in ('p', 'div', 'section', 'h1', 'h2', 'li'):
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self.stack:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.jsonld.append(json.loads(self.script))
            except ValueError:
                pass
            self.script = None
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.active and depth <= self.active:
            self.active = 0
        if self.title_depth and depth <= self.title_depth:
            self.title_depth = 0
        self.stack = self.stack[:depth - 1]

    def handle_data(self, data):
        if self.script is not None:
            self.script += data
        if self.title_depth:
            self.title.append(data)
        if self.active and not any(t in ('script', 'style', 'noscript') for t in self.stack):
            self.parts.append(data)


def parse_page(url, source):
    page = Page()
    page.feed(source)
    text = clean(''.join(page.parts))
    title = page.meta.get('og:title') or ''.join(page.title)
    images = list(dict.fromkeys(page.images))
    scope = 'body'
    if not text:
        def walk(v):
            if isinstance(v, list):
                for x in v:
                    yield from walk(x)
            if isinstance(v, dict):
                yield v
                yield from walk(v.get('@graph', []))
        for obj in walk(page.jsonld):
            body = obj.get('articleBody') or obj.get('text')
            desc = obj.get('description')
            if isinstance(body, str) and body.strip():
                text, title = body, obj.get('headline') or title
                break
            if isinstance(desc, str) and obj.get('@type') in ('VideoObject', 'SocialMediaPosting'):
                text, title, scope = desc, obj.get('name') or obj.get('headline') or title, 'description'
    if not text:
        visible_source = re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', '', source, flags=re.I | re.S)
        visible = clean(re.sub('<[^>]+>', ' ', visible_source))
        challenge = classify_text(visible[:12000])
        if challenge.code in TERMINAL:
            raise challenge
        raise Failure('EMPTY_CONTENT', '只取得页面壳/元信息，未取得正文；不算成功')
    return item(url, title, text, scope=scope, images=images)


def classify_text(message):
    s = str(message).lower()
    if 'traceback (most recent call last)' in s:
        s = s.strip().splitlines()[-1]
    if any(x in s for x in ('operation not permitted', 'permissionerror', 'read-only file system')):
        return Failure('LOCAL_PERMISSION', '执行环境或本地文件权限阻止了工具；这不是平台验证码或登录限制')
    if any(x in s for x in ('captcha', '验证码', '安全验证', '环境异常', '访问过于频繁', 'verify you are human', 'not a bot', '验证后继续')):
        return Failure('PLATFORM_BLOCKED', '平台要求人工验证；停止此平台后续请求')
    if any(x in s for x in ('429', 'too many requests', 'rate limit', 'rate_limited')):
        return Failure('RATE_LIMITED', '平台限流；不切换后端', 900)
    if any(x in s for x in ('sign in', 'login required', 'need_login', '请先登录', '需要登录')):
        return Failure('NEED_LOGIN', '请用户在平台官方页面或现有工具中自行登录')
    if any(x in s for x in ('403', 'forbidden', 'permission denied', 'access denied')):
        return Failure('PERMISSION_DENIED', '访问权限不足；不切换后端')
    if any(x in s for x in ('timed out', 'timeout')):
        return Failure('TIMEOUT', '上游调用超时')
    if any(x in s for x in ('name resolution', 'nodename nor servname', 'connection', 'network', 'ssl', 'certificate')):
        return Failure('NETWORK_ERROR', '网络或 TLS 连接失败，不据此推断需要登录')
    if any(x in s for x in ('not found', 'has been removed', 'video unavailable', '404')):
        return Failure('NOT_FOUND', '内容不存在或当前不可用')
    return Failure('UPSTREAM_BROKEN', '上游未成功返回可用数据；请检查工具健康状态')


class State:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.directory / 'state.sqlite3'), timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT);
            CREATE TABLE IF NOT EXISTS gates (platform TEXT PRIMARY KEY, code TEXT, until_time REAL);
            CREATE TABLE IF NOT EXISTS rates (platform TEXT PRIMARY KEY, last_time REAL, lease_until REAL, owner TEXT);
            CREATE TABLE IF NOT EXISTS probes (key TEXT PRIMARY KEY, expires REAL, evidence TEXT);
            CREATE TABLE IF NOT EXISTS checks (platform TEXT, action TEXT, checked_at REAL, evidence TEXT,
                PRIMARY KEY (platform, action));
        ''')
        self.db.commit()
        os.chmod(self.directory / 'state.sqlite3', 0o600)

    def close(self):
        self.db.close()

    def __del__(self):
        db = getattr(self, 'db', None)
        if db is not None:
            db.close()

    def cached(self, key):
        row = self.db.execute('SELECT value FROM cache WHERE key=? AND expires>?', (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, value, ttl):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (key, time.time() + ttl, json.dumps(value, ensure_ascii=False)))

    def gate(self, platform):
        row = self.db.execute('SELECT code, until_time FROM gates WHERE platform=?', (platform,)).fetchone()
        if row and (row[1] == 0 or row[1] > time.time()):
            raise Failure(row[0], '该平台此前已停止；人工处理后用 resume 显式恢复。缓存结果仍可读。', max(0, int(row[1] - time.time())) if row[1] else 0)

    def block(self, platform, failure):
        if failure.code in TERMINAL:
            until = time.time() + max(60, failure.retry_after) if failure.code == 'RATE_LIMITED' else (time.time() + 60 if failure.code == 'BUSY' else 0)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?)', (platform, failure.code, until))

    def resume(self, platform):
        row = self.db.execute('SELECT code, until_time FROM gates WHERE platform=?', (platform,)).fetchone()
        if row and row[0] in ('RATE_LIMITED', 'BUSY') and row[1] > time.time():
            raise Failure(row[0], '冷却期未结束，不能提前恢复', int(row[1] - time.time()))
        with self.db:
            self.db.execute('DELETE FROM gates WHERE platform=?', (platform,))

    def reserve(self, platform, owner, lease_seconds, interval):
        self.gate(platform)
        now = time.time()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT last_time,lease_until,owner FROM rates WHERE platform=?', (platform,)).fetchone()
            if row and row[1] > now and row[2] != owner:
                raise Failure('BUSY', '另一个调用正在使用同一平台；稍后再试')
            wait = max(0, (row[0] + interval - now) if row else 0)
            if wait > 0:
                self.db.rollback()
                return wait
            self.db.execute('INSERT OR REPLACE INTO rates VALUES (?,?,?,?)', (platform, now, now + lease_seconds + 5, owner))
            self.db.commit()
            return 0
        except Exception:
            self.db.rollback()
            raise

    def release(self, platform, owner):
        with self.db:
            self.db.execute('UPDATE rates SET lease_until=0 WHERE platform=? AND owner=?', (platform, owner))

    def probe_valid(self, key):
        return self.db.execute('SELECT 1 FROM probes WHERE key=? AND expires>?', (key, time.time())).fetchone() is not None

    def record_probe(self, key, evidence):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO probes VALUES (?,?,?)', (key, time.time() + 7 * 86400, json.dumps(evidence, ensure_ascii=False)))

    def record_check(self, platform, action, evidence):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checks VALUES (?,?,?,?)',
                (platform, action, time.time(), json.dumps(evidence, ensure_ascii=False)))

    def inspection(self, platform):
        gate = self.db.execute('SELECT code,until_time FROM gates WHERE platform=?', (platform,)).fetchone()
        checks = {action: {'checked_at': at, **json.loads(evidence)} for action, at, evidence in
                  self.db.execute('SELECT action,checked_at,evidence FROM checks WHERE platform=?', (platform,))}
        active = gate and (gate[1] == 0 or gate[1] > time.time())
        return {'gate': {'code': gate[0], 'until_time': gate[1]} if active else None, 'checks': checks}
