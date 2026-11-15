"""Bounded network/process I/O. No shell, no automatic installations, no model calls."""
from contextlib import contextmanager
import email.utils
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sc_core import DEFAULTS, DOMAINS, Failure, State, classify_text, host_matches, platform_for

EXTRA_HOSTS = ('www.googleapis.com', 'api.x.com', 'googlevideo.com', 'ytimg.com',
               'hdslb.com', 'bilivideo.com', 'bilivideo.cn', 'qpic.cn', 'qlogo.cn', 'xhscdn.com',
               'douyinpic.com', 'byteimg.com', 'twimg.com')


def load_config(path=None):
    config = dict(DEFAULTS)
    config['connectors'] = {}
    config_path = path or os.environ.get('THEREACH_CONFIG')
    if config_path:
        p = Path(config_path).expanduser().resolve()
        obj = json.loads(p.read_text())
        if not isinstance(obj, dict) or set(obj) - set(DEFAULTS) - {'state_dir', 'transcribe_command', 'vision_command', 'ocr_command'}:
            raise Failure('BAD_INPUT', '配置含未知字段，或不是 JSON 对象')
        config.update(obj)
        config['_base'] = str(p.parent)
    else:
        config['_base'] = str(Path(__file__).resolve().parents[2])
    directory = os.environ.get('THEREACH_STATE') or config.get('state_dir') or '.thereach'
    config['state_dir'] = str((Path(config['_base']) / Path(directory).expanduser()).resolve())
    for k in ('timeout', 'deadline', 'max_calls', 'max_bytes', 'max_output_chars', 'cache_ttl', 'failure_ttl', 'max_redirects', 'max_media_bytes'):
        if isinstance(config[k], bool) or not isinstance(config[k], (int, float)) or config[k] <= 0:
            raise Failure('BAD_INPUT', k + ' 必须为正数')
    for k in ('max_paid_calls', 'min_interval', 'max_images', 'max_comments'):
        if isinstance(config[k], bool) or not isinstance(config[k], (int, float)) or config[k] < 0:
            raise Failure('BAD_INPUT', k + ' 必须非负')
    for k in ('max_calls', 'max_paid_calls', 'max_bytes', 'max_output_chars', 'max_redirects', 'max_images', 'max_comments', 'max_media_bytes'):
        if not isinstance(config[k], int):
            raise Failure('BAD_INPUT', k + ' 必须为整数')
    if not isinstance(config['connectors'], dict):
        raise Failure('BAD_INPUT', 'connectors 必须为对象')
    return config


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    if '/' in name:
        p = Path(name).expanduser()
        return str(p.resolve()) if p.is_file() and os.access(str(p), os.X_OK) else None
    local = Path.home() / '.local/bin' / name
    if local.is_file() and os.access(str(local), os.X_OK):
        return str(local)
    reach = shutil.which('agent-reach') or str(Path.home() / '.local/bin/agent-reach')
    if Path(reach).is_file():
        first = Path(reach).read_text().splitlines()[0]
        if first.startswith('#!') and first[2:].startswith('/'):
            p = Path(first[2:]).parent / name
            if p.is_file() and os.access(str(p), os.X_OK):
                return str(p)
    for p in (Path.home() / '.agent-reach/tools' / name / 'bin' / name,
              Path.home() / '.agent-reach/tools' / name / 'current/bin' / name,
              Path.home() / '.agent-reach/tools/npm-global/bin' / name):
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Runtime:
    def __init__(self, config):
        self.config = config
        self.state = State(config['state_dir'])
        self.started = time.monotonic()
        self.end = self.started + config['deadline']
        self.calls, self.paid_calls, self.bytes = 0, 0, 0
        self.attempts = []
        self.owner = uuid.uuid4().hex
        self.opener = build_opener(NoRedirect())

    def remaining(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise Failure('BUDGET_EXCEEDED', '任务总时间预算耗尽')
        return min(self.config['timeout'], left)

    def charge(self, paid=False):
        self.remaining()
        if self.calls >= self.config['max_calls']:
            raise Failure('BUDGET_EXCEEDED', '调用预算耗尽')
        if paid and self.paid_calls >= self.config['max_paid_calls']:
            raise Failure('BUDGET_EXCEEDED', '付费/模型调用预算耗尽；需配置 max_paid_calls 并显式授权')
        self.calls += 1
        if paid:
            self.paid_calls += 1

    def add_bytes(self, count):
        self.bytes += count
        if self.bytes > self.config['max_bytes']:
            raise Failure('OUTPUT_LIMIT', '输入数据字节预算耗尽')

    @contextmanager
    def attempt(self, platform, backend, action):
        started = time.monotonic()
        entry = {'platform': platform, 'backend': backend, 'action': action}
        self.attempts.append(entry)
        reserved = False
        try:
            wait = self.state.reserve(platform, self.owner, self.end - time.monotonic(), self.config['min_interval'])
            if wait:
                if wait >= self.end - time.monotonic():
                    raise Failure('BUDGET_EXCEEDED', '平台限速等待将超出总预算')
                time.sleep(wait)
                wait = self.state.reserve(platform, self.owner, self.end - time.monotonic(), self.config['min_interval'])
                if wait:
                    raise Failure('BUSY', '平台时隙已被其他进程占用')
            reserved = True
            yield
            entry['status'] = 'ok'
        except Failure as e:
            self.state.block(platform, e)
            entry['status'] = e.code
            raise
        except sqlite3.OperationalError:
            entry['status'] = 'BUSY'
            raise Failure('BUSY', '本地共享状态库繁忙；稍后重试，不打开新浏览器') from None
        finally:
            if reserved:
                self.state.release(platform, self.owner)
            entry['elapsed_ms'] = round((time.monotonic() - started) * 1000)

    def command(self, argv, payload=None, paid=False, extra_env=None, timeout=None):
        if not argv or not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise Failure('BAD_INPUT', '命令必须为字符串数组')
        exe = executable(argv[0])
        if not exe:
            raise Failure('NEED_SETUP', '缺少执行工具：' + Path(argv[0]).name)
        self.charge(paid)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        additions = [str(Path(exe).parent)]
        for name in ('node', 'ffmpeg'):
            found = executable(name)
            if found:
                additions.append(str(Path(found).parent))
        env['PATH'] = os.pathsep.join(additions + [env.get('PATH', '')])
        if extra_env:
            env.update(extra_env)
        cap = max(1, self.config['max_bytes'] - self.bytes)
        with tempfile.TemporaryDirectory(prefix='sc-', dir=str(self.state.directory)) as directory:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
                if payload is not None:
                    stdin.write(json.dumps(payload, ensure_ascii=False).encode())
                stdin.seek(0)
                proc = subprocess.Popen([exe, *argv[1:]], stdin=stdin, stdout=stdout, stderr=stderr,
                                        cwd=directory, env=env, start_new_session=True)
                stop_at = min(self.end, time.monotonic() + (self.remaining() if timeout is None else timeout))
                try:
                    while proc.poll() is None:
                        if time.monotonic() > stop_at:
                            raise Failure('TIMEOUT', '子进程超时，已终止本次子进程组')
                        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > cap:
                            raise Failure('OUTPUT_LIMIT', '子进程输出超过字节预算')
                        time.sleep(0.025)
                finally:
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(cap + 1), stderr.read(cap + 1)
        self.add_bytes(len(out) + len(err))
        text = out.decode('utf-8', 'replace')
        if proc.returncode:
            try:
                data = json.loads(text)
                if isinstance(data, dict) and data.get('ok') is False and isinstance(data.get('error'), dict):
                    code = data['error'].get('code', 'UPSTREAM_BROKEN')
                    raise Failure(code, data['error'].get('message') or data['error'].get('hint', ''), data['error'].get('retry_after_seconds', 0))
            except ValueError:
                pass
            raise classify_text(err.decode('utf-8', 'replace') + text)
        return text

    def command_json(self, argv, payload=None, paid=False, timeout=None):
        try:
            data = json.loads(self.command(argv, payload, paid, timeout=timeout))
        except ValueError:
            raise Failure('INVALID_RESPONSE', '上游返回不是单个 JSON 对象') from None
        if not isinstance(data, dict):
            raise Failure('INVALID_RESPONSE', '上游 JSON 顶层不是对象')
        if data.get('ok') is False:
            e = data.get('error') or {}
            raise Failure(e.get('code', 'UPSTREAM_BROKEN'), e.get('message') or e.get('hint', ''), e.get('retry_after_seconds', 0))
        return data

    def validate_http(self, url, platform=None, media=False):
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            raise Failure('BAD_INPUT', '不安全的 HTTP URL')
        host = (p.hostname or '').lower()
        allowed = tuple(d for domains in DOMAINS.values() for d in domains) + EXTRA_HOSTS
        if not any(host_matches(host, d) for d in allowed):
            raise Failure('BAD_INPUT', '跳转或媒体 URL 不在平台域名白名单中')
        if platform and not media and not any(host_matches(host, d) for d in DOMAINS[platform]):
            raise Failure('BAD_INPUT', '短链发生跨平台跳转，停止并核对链接')
        try:
            addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        except OSError:
            raise Failure('NETWORK_ERROR', '域名解析失败') from None
        if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise Failure('BAD_INPUT', '拒绝访问非公网地址')

    def http(self, url, platform=None, headers=None, media=False):
        initial_host = urlsplit(url).hostname
        headers = dict(headers or {})
        for hop in range(self.config['max_redirects'] + 1):
            self.validate_http(url, platform, media)
            self.charge()
            # Never forward an API credential to a redirect host.
            if urlsplit(url).hostname != initial_host:
                headers = {}
            req = Request(url, headers={'User-Agent': 'thereach/0.1 (read-only)', 'Accept-Encoding': 'identity', **headers})
            try:
                response = self.opener.open(req, timeout=self.remaining())
            except HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    location = e.headers.get('Location')
                    e.close()
                    if not location or hop >= self.config['max_redirects']:
                        raise Failure('INVALID_RESPONSE', '短链接跳转超过上限或缺少 Location')
                    new = urljoin(url, location)
                    if urlsplit(url).scheme == 'https' and urlsplit(new).scheme != 'https':
                        raise Failure('BAD_INPUT', '拒绝 HTTPS 降级跳转')
                    url = new
                    continue
                code = {401: 'NEED_LOGIN', 402: 'PERMISSION_DENIED', 403: 'PERMISSION_DENIED', 404: 'NOT_FOUND', 429: 'RATE_LIMITED', 412: 'PLATFORM_BLOCKED'}.get(e.code, 'UPSTREAM_BROKEN')
                delay = 900 if e.code == 429 else 0
                retry = e.headers.get('Retry-After', '')
                if retry.isdigit():
                    delay = max(delay, int(retry))
                elif retry:
                    try:
                        delay = max(delay, int(email.utils.parsedate_to_datetime(retry).timestamp() - time.time()))
                    except (ValueError, TypeError):
                        pass
                e.close()
                raise Failure(code, 'HTTP ' + str(e.code) + '；停止或按失败分类处理', delay) from None
            except (URLError, OSError) as e:
                raise classify_text(str(e)) from None
            with response:
                content_type = response.headers.get('Content-Type', '')
                cap = min(self.config['max_bytes'] - self.bytes, self.config['max_media_bytes'] if media else self.config['max_bytes'])
                chunks, size = [], 0
                while True:
                    timeout = self.remaining()
                    try:
                        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                        if sock:
                            sock.settimeout(timeout)
                        read = getattr(response, 'read1', response.read)
                        chunk = read(min(65536, cap - size + 1))
                    except (OSError, TimeoutError) as e:
                        raise classify_text(str(e)) from None
                    if not chunk:
                        break
                    self.add_bytes(len(chunk))
                    size += len(chunk)
                    if size > cap:
                        raise Failure('OUTPUT_LIMIT', 'HTTP 响应超过字节上限')
                    chunks.append(chunk)
                return url, b''.join(chunks), content_type
        raise Failure('INVALID_RESPONSE', '重定向循环')

    def json_http(self, url, headers=None):
        _, body, _ = self.http(url, headers=headers)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise Failure('INVALID_RESPONSE', 'API 未返回 JSON') from None
        if isinstance(data, dict) and data.get('error'):
            raise Failure('PERMISSION_DENIED', 'API 返回错误；请检查权限、配额和账号')
        return data

    def metrics(self):
        return {'calls': self.calls, 'paid_calls': self.paid_calls, 'bytes': self.bytes,
                'elapsed_ms': round((time.monotonic() - self.started) * 1000), 'attempts': self.attempts,
                'budget_scope': 'HTTP requests + subprocess invocations; upstream internal requests are opaque and not individually counted'}
