"""Adapters around existing public CLIs and opt-in official APIs."""
import json
import datetime
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from sc_core import Failure, canonical, clean, digest, item, parse_page, platform_for
from sc_runtime import executable

CHANNELS = {'x': 'twitter', 'wechat': 'web'}
SITES = {'douyin': 'douyin.com', 'xiaohongshu': 'xiaohongshu.com', 'bilibili': 'bilibili.com',
         'wechat': 'mp.weixin.qq.com', 'youtube': 'youtube.com', 'x': 'x.com'}


def image_urls(raw):
    result = []
    def add(v):
        if isinstance(v, str) and v.startswith('https://'):
            result.append(v)
        elif isinstance(v, list):
            for x in v:
                add(x)
        elif isinstance(v, dict):
            for k in ('url', 'urlDefault', 'url_default', 'image_url', 'url_list', 'src'):
                if k in v:
                    add(v[k])
    for k in ('images', 'imageList', 'image_list', 'image_urls', 'thumbnail', 'cover', 'pic', 'thumbnails'):
        add(raw.get(k))
    return list(dict.fromkeys(result))[:20]


def normalized(rows, platform, scope, target=None):
    out = []
    if not isinstance(rows, list):
        raise Failure('INVALID_RESPONSE', '上游 items/data 不是列表')
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get('raw') if isinstance(row.get('raw'), dict) else row
        url = row.get('url') or row.get('webpage_url') or row.get('link') or target
        if not url:
            continue
        try:
            if platform_for(url) != platform:
                continue
        except Failure:
            continue
        text = row.get('text') or row.get('content') or row.get('desc') or row.get('description') or ''
        if not isinstance(text, str):
            text = ''
        author = row.get('author') or row.get('uploader') or row.get('nickname') or ''
        if isinstance(author, dict):
            author = author.get('name') or author.get('nickname') or ''
        entry = item(url, row.get('title') or row.get('name') or '', text, author,
                     row.get('published_at') or row.get('upload_date') or '',
                     scope=scope, images=image_urls(raw))
        entry['_access_url'] = url
        if scope == 'comment':
            entry['comment_id'] = row.get('comment_id') or raw.get('comment_id') or raw.get('id')
            entry['parent_id'] = raw.get('parent_id') or raw.get('parent') or raw.get('in_reply_to')
        out.append(entry)
    return out


class Providers:
    def __init__(self, runtime, allow_paid=False):
        self.r, self.c, self.allow_paid = runtime, runtime.config, allow_paid

    def connector_key(self, platform, action):
        connector = self.c['connectors'].get(platform, {})
        command = connector.get('command', [])
        identity = []
        for arg in command:
            p = Path(arg).expanduser()
            if p.is_absolute() and p.is_file():
                stat = p.stat()
                identity.append((arg, stat.st_size, stat.st_mtime_ns))
        return digest([platform, action, connector, identity])

    def has_connector(self, platform, action):
        spec = self.c['connectors'].get(platform, {})
        return action in spec.get('actions', []) and self.r.state.probe_valid(self.connector_key(platform, action))

    def connector(self, platform, action, target, limit, probing=False):
        spec = self.c['connectors'].get(platform, {})
        if action not in spec.get('actions', []) or not spec.get('command'):
            raise Failure('NEED_SETUP', '该连接器未声明所需动作')
        if not probing and not self.has_connector(platform, action):
            raise Failure('UNVERIFIED_ACCESS', '连接器动作未通过真实样本 probe，禁止自动调用')
        paid = spec.get('paid', True)
        if paid and not self.allow_paid:
            raise Failure('NEED_SETUP', '该连接器可能调用付费/模型服务，需 --allow-paid 和付费预算')
        data = self.r.command_json(spec['command'], {'schema_version': 1, 'platform': platform,
            'action': action, 'target': target, 'limit': limit, 'timeout_seconds': self.r.remaining(),
            'max_bytes': self.c['max_bytes'] - self.r.bytes, 'read_only': True}, paid=paid)
        rows = data.get('items')
        if not isinstance(rows, list):
            raise Failure('INVALID_RESPONSE', '连接器必须返回 items 列表及实际 source URL')
        allowed = {'search': {'search_hit'}, 'read': {'body', 'description'},
                   'subtitle': {'subtitle'}, 'comments': {'comment'}}[action]
        if any(not isinstance(x, dict) or x.get('text_scope') not in allowed or not x.get('url') for x in rows):
            raise Failure('INVALID_RESPONSE', '连接器的文本范围或证据 URL 不符合契约；拒收生成总结')
        result = []
        for row in rows:
            result.extend(normalized([row], platform, row['text_scope']))
        if not result and action != 'search':
            raise Failure('EMPTY_CONTENT', '连接器未返回内容证据')
        return result

    def reach(self, platform, action, target, limit):
        requested_action = action
        channel = CHANNELS.get(platform, platform)
        if platform == 'x' and action in ('read', 'comments'):
            action = 'thread'
        if platform == 'wechat':
            if action == 'search':
                target = 'site:mp.weixin.qq.com ' + target
        args = [self.c['agent_reach'], channel, action, target]
        if action in ('search', 'comments'):
            args += ['--limit', str(limit)]
        args += ['--timeout', str(max(1, int(self.r.remaining())))]
        data = self.r.command_json(args)
        scope = 'search_hit' if requested_action == 'search' else ('subtitle' if requested_action == 'subtitle' else ('comment' if requested_action == 'comments' else ('description' if platform in ('youtube', 'bilibili') else 'body')))
        out = normalized(data.get('items', []), platform, scope, target if action != 'search' else None)
        if platform == 'x' and requested_action == 'comments':
            out = [entry for entry in out if entry['url'] != canonical(target)]
        for entry in out:
            if platform == 'wechat' and action != 'search':
                entry['warnings'].append('reader_returned_text: 第三方 reader 文本，未保证全文完整性')
            if data.get('backend') == 'public-web' and action != 'search':
                entry['text_scope'] = 'public_web_text'
                entry['warnings'].append('公开网页结果；完整性需人工核对')
        if not out and requested_action not in ('search', 'comments'):
            raise Failure('EMPTY_CONTENT', 'Agent Reach 未返回可用内容')
        return out

    def public(self, platform, action, target, limit):
        if action != 'read':
            raise Failure('NEED_SETUP', '公开 HTML 路线仅提供正文读取')
        final, body, kind = self.r.http(target, platform=platform)
        if kind and 'html' not in kind:
            raise Failure('INVALID_RESPONSE', '平台未返回 HTML 页面')
        return [parse_page(final, body.decode('utf-8', 'replace'))]

    def bilibili_legacy(self, platform, action, target, limit):
        aid = re.search(r'/av(\d+)', target).group(1)
        payload = self.r.json_http('https://api.bilibili.com/x/web-interface/view?' + urlencode({'aid': aid}))
        if not isinstance(payload, dict):
            raise Failure('INVALID_RESPONSE', 'B站视频信息不是 JSON 对象')
        code = payload.get('code')
        if code == -404:
            raise Failure('NOT_FOUND', 'B站未找到该 AV 视频')
        if code in (-412, -352):
            raise Failure('PLATFORM_BLOCKED', 'B站要求人工验证')
        if code in (-101, -403):
            raise Failure('NEED_LOGIN', 'B站该视频需本人登录或访问权限')
        data = payload.get('data')
        if code != 0 or not isinstance(data, dict) or not data.get('bvid'):
            raise Failure('INVALID_RESPONSE', 'B站未返回有效的视频信息')
        suffix = '?p=' + parse_qs(urlsplit(target).query)['p'][0] if 'p' in parse_qs(urlsplit(target).query) else ''
        date = datetime.datetime.fromtimestamp(data['pubdate'], datetime.timezone.utc).isoformat() if isinstance(data.get('pubdate'), (int, float)) else ''
        return [item('https://www.bilibili.com/video/' + data['bvid'] + suffix, data.get('title'),
                     data.get('desc'), (data.get('owner') or {}).get('name'), date, 'description', image_urls(data))]

    def browser_command(self, action, target, limit=20):
        reach = executable(self.c['agent_reach'])
        if not reach:
            raise Failure('NEED_SETUP', '缺少现有 Agent Reach')
        first = Path(reach).read_text().splitlines()[0]
        interpreter = first[2:] if first.startswith('#!/') else ''
        if not interpreter or not executable(interpreter):
            raise Failure('NEED_SETUP', '无法定位已有 Agent Reach 的 Python；不会安装新依赖')
        return self.r.command_json([interpreter, str(Path(__file__).with_name('reach_browser.py')),
            target, '--timeout', str(self.r.remaining()), '--action', action, '--limit', str(limit)])

    def browser(self, platform, action, target, limit):
        data = self.browser_command(action, target, limit)
        scope = {'subtitle': 'subtitle', 'comments': 'comment'}.get(action, 'body' if platform == 'wechat' else 'description')
        return normalized(data.get('items', []), platform, scope, target)

    def china(self, platform, action, target, limit):
        data = self.r.command_json([self.c['china_read'], 'douyin', 'search', target, '--limit', str(limit)])
        rows = data.get('data', [])
        if isinstance(rows, dict):
            rows = rows.get('items') or rows.get('data') or []
        return normalized(rows, platform, 'search_hit')[:limit]

    def web_search(self, platform, action, target, limit):
        data = self.r.command_json([self.c['agent_reach'], 'web', 'search', 'site:' + SITES[platform] + ' ' + target,
                                    '--limit', str(limit), '--timeout', str(max(1, int(self.r.remaining())))])
        return normalized(data.get('items', []), platform, 'search_hit')[:limit]

    def ytdlp(self, platform, action, target, limit):
        exe = executable('yt-dlp')
        if not exe:
            raise Failure('NEED_SETUP', '未发现现有 yt-dlp；不会自动安装')
        args = [exe, '--ignore-config', '--no-playlist', '--skip-download', '--dump-single-json',
                '--socket-timeout', str(max(1, int(self.r.remaining()))), '--retries', '0',
                '--extractor-retries', '0', '--fragment-retries', '0', '--no-warnings']
        node = executable('node')
        if node:
            args += ['--js-runtimes', 'node:' + node]
        if action == 'comments':
            args += ['--write-comments', '--extractor-args', 'youtube:max_comments=' + str(limit)]
        args += ['--', target]
        data = self.r.command_json(args)
        if action == 'comments':
            rows = [{'url': target, 'text': x.get('text', ''), 'author': x.get('author', ''),
                     'raw': {'comment_id': x.get('id'), 'parent': x.get('parent')}} for x in (data.get('comments') or [])[:limit]]
            return normalized(rows, platform, 'comment')
        return normalized([data], platform, 'description', target)

    def youtube_api(self, platform, action, target, limit):
        key = os.environ.get('THEREACH_YOUTUBE_KEY')
        if not key:
            raise Failure('NEED_SETUP', '未配置 THEREACH_YOUTUBE_KEY')
        base = 'https://www.googleapis.com/youtube/v3/'
        params = {'key': key, 'part': 'snippet', 'maxResults': min(50, limit)}
        vid = parse_qs(urlsplit(canonical(target)).query).get('v', [''])[0] if action != 'search' else ''
        if action == 'search':
            endpoint = 'search'
            params.update(q=target, type='video')
        elif action == 'read':
            endpoint = 'videos'
            params.pop('maxResults')
            params['id'] = vid
        else:
            endpoint = 'commentThreads'
            params.update(videoId=vid, textFormat='plainText')
        data = self.r.json_http(base + endpoint + '?' + urlencode(params))
        out = []
        for row in data.get('items', []):
            snippet = row.get('snippet', {})
            if action == 'comments':
                top = snippet.get('topLevelComment', {})
                s = top.get('snippet', {})
                entry = item(target, text=s.get('textOriginal') or s.get('textDisplay'), author=s.get('authorDisplayName'), scope='comment')
                entry['comment_id'] = top.get('id')
                entry['reply_count'] = snippet.get('totalReplyCount', 0)
                entry['warnings'].append('仅第一批顶层评论，未展开全部回复')
            else:
                identity = row.get('id', {})
                video_id = identity.get('videoId') if isinstance(identity, dict) else identity
                if not video_id:
                    continue
                entry = item('https://www.youtube.com/watch?v=' + video_id,
                    snippet.get('title'), snippet.get('description'), snippet.get('channelTitle'),
                    snippet.get('publishedAt'), 'search_hit' if action == 'search' else 'description',
                    images=image_urls(snippet))
            out.append(entry)
        if not out and action == 'read':
            raise Failure('NOT_FOUND', 'YouTube API 没有返回该视频')
        return out

    def x_api(self, platform, action, target, limit):
        if not self.allow_paid:
            raise Failure('NEED_SETUP', 'X API 默认视为可能付费，需 --allow-paid')
        if not os.environ.get('THEREACH_X_BEARER'):
            raise Failure('NEED_SETUP', '未配置 THEREACH_X_BEARER')
        # HTTP call is counted separately; reserve one paid call without double-counting it.
        if self.r.paid_calls >= self.c['max_paid_calls']:
            raise Failure('BUDGET_EXCEEDED', 'X API 付费预算耗尽')
        self.r.paid_calls += 1
        params = {'tweet.fields': 'created_at,author_id,conversation_id', 'expansions': 'author_id', 'user.fields': 'name,username'}
        if action == 'read':
            match = re.search(r'/status/(\d+)', target)
            if not match:
                raise Failure('BAD_INPUT', 'X 读取需帖子链接')
            endpoint = 'tweets/' + match.group(1)
        else:
            endpoint = 'tweets/search/recent'
            query = target
            if action == 'comments':
                match = re.search(r'/status/(\d+)', target)
                if not match:
                    raise Failure('BAD_INPUT', 'X 评论需帖子链接')
                query = 'conversation_id:' + match.group(1) + ' is:reply'
            params.update(query=query, max_results=max(10, min(100, limit)))
        data = self.r.json_http('https://api.x.com/2/' + endpoint + '?' + urlencode(params),
                   {'Authorization': 'Bearer ' + os.environ['THEREACH_X_BEARER']})
        users = {u['id']: u for u in data.get('includes', {}).get('users', [])}
        rows = data.get('data') or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for row in rows[:limit]:
            user = users.get(row.get('author_id'), {})
            out.append(item('https://x.com/' + user.get('username', 'i') + '/status/' + row['id'],
                            text=row.get('text'), author=user.get('name'), published_at=row.get('created_at'),
                            scope='comment' if action == 'comments' else ('search_hit' if action == 'search' else 'body')))
        if data.get('errors'):
            for entry in out:
                entry['warnings'].append('X API 部分字段访问失败，结果可能不完整')
            if not out:
                raise Failure('PERMISSION_DENIED', 'X API 返回部分访问错误，无可用内容')
        return out

    def routes(self, platform, action, target=None):
        routes = []
        if self.has_connector(platform, action):
            routes.append(('verified-connector', self.connector))
        if platform == 'youtube' and self.c['youtube_api'] and action in ('read', 'search', 'comments'):
            routes.append(('youtube-api', self.youtube_api))
        if platform == 'x' and self.c['x_api'] and action in ('read', 'search', 'comments'):
            routes.append(('x-api', self.x_api))
        if action == 'read':
            if platform in ('douyin', 'wechat'):
                routes.append(('reach-browser', self.browser))
                routes.append(('public-html', self.public))
            if platform == 'douyin':
                routes.append(('yt-dlp', self.ytdlp))
            elif platform == 'bilibili' and target and re.search(r'/av\d+', target):
                # Public view metadata resolves legacy AV URLs without the BV-only CLI.
                routes.append(('bilibili-view', self.bilibili_legacy))
            else:
                routes.append(('agent-reach', self.reach))
            if platform in ('xiaohongshu', 'x'):
                routes.append(('public-html', self.public))
        elif action == 'search':
            if platform == 'douyin':
                routes.append(('china-read', self.china))
            elif platform != 'wechat':
                routes.append(('agent-reach', self.reach))
            if platform in ('douyin', 'wechat'):
                routes.append(('web-index', self.web_search))
        elif action == 'subtitle' and platform in ('youtube', 'bilibili'):
            if platform == 'bilibili':
                routes.append(('reach-browser', self.browser))
            routes.append(('agent-reach', self.reach))
        elif action == 'comments':
            if platform in ('bilibili', 'douyin'):
                routes.append(('reach-browser', self.browser))
            if platform == 'xiaohongshu':
                routes.append(('agent-reach', self.reach))
            if platform == 'x':
                routes.append(('agent-reach', self.reach))
            if platform == 'youtube':
                routes.append(('yt-dlp', self.ytdlp))
        return routes
