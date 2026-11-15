"""Read visible content using the already installed Reach-owned Browser Bridge.

Run with Agent Reach's Python. No cookie export, custom signing, or new browser.
"""
import argparse
import json
from pathlib import Path
import sys

from sc_core import Failure, canonical, classify_text, item, parse_page, platform_for


EXTRACT_JS = r"""(() => {
  const visible = (document.body?.innerText || '').slice(0, 12000);
  const article = document.getElementById('js_content');
  if (article?.innerText.trim()) return {url: location.href, html: article.outerHTML,
    title: document.getElementById('activity-name')?.innerText.trim() || document.title,
    author: document.getElementById('js_name')?.innerText.trim() || ''};
  const id = location.pathname.match(/\/(?:video|note)\/(\d+)/)?.[1];
  const comments = [...document.querySelectorAll('[data-e2e="comment-list"] [data-e2e="comment-item"]')].slice(0, 20).map(el => {
    const info = el.querySelector('.comment-item-info-wrap');
    return {text: info?.nextElementSibling?.innerText.trim() || '',
      author: info?.querySelector('[data-click-from="title"]')?.innerText.trim() || '',
      comment_id: el.querySelector('[id^="tooltip_"]')?.id.replace('tooltip_', '') || ''};
  }).filter(x => x.text);
  const roots = [window._ROUTER_DATA, window.__INITIAL_STATE__, window.__NEXT_DATA__, window.SSR_RENDER_DATA];
  for (const selector of ['#RENDER_DATA', '#__NEXT_DATA__']) {
    const raw = document.querySelector(selector)?.textContent;
    if (raw) { try { roots.push(JSON.parse(raw)); } catch {
      try { roots.push(JSON.parse(decodeURIComponent(raw))); } catch {} }
    }
  }
  const seen = new Set(); let count = 0;
  const walk = (v, depth = 0) => {
    if (!v || typeof v !== 'object' || seen.has(v) || depth > 18 || ++count > 30000) return null;
    seen.add(v);
    if (String(v.aweme_id || v.awemeId || '') === id && typeof v.desc === 'string' && v.desc.trim()) return v;
    for (const child of Object.values(v)) { const found = walk(child, depth + 1); if (found) return found; }
    return null;
  };
  if (id) for (const root of roots) {
    const found = walk(root);
    if (found) return {url: location.href, text: found.desc, author: found.author?.nickname || '',
      images: (found.images || []).flatMap(x => x.url_list || []).filter(x => typeof x === 'string').slice(0, 20), comments};
  }
  const info = id && [...document.querySelectorAll('[data-e2e="detail-video-info"]')]
    .find(el => el.getAttribute('data-e2e-aweme-id') === id);
  const desc = info?.querySelector('h1') || (id && document.querySelector('[data-e2e="video-desc"]'));
  if (desc?.innerText.trim()) return {url: location.href, text: desc.innerText,
    title: desc.innerText, author: document.querySelector('[data-e2e="user-info"] img[alt]')?.alt || '', images: [], comments};
  return {url: location.href, visible, title: document.title};
})()"""


def bilibili_rows(rows, target, action, limit):
    if not isinstance(rows, list):
        raise Failure('INVALID_RESPONSE', 'B站适配器未返回列表')
    if action == 'subtitle':
        text = '\n'.join(x.get('content', '') for x in rows if isinstance(x, dict))
        if not text.strip():
            raise Failure('EMPTY_CONTENT', '没有取得实际字幕文本')
        return [item(target, text=text, scope='subtitle')]
    result = []
    for row in rows[:limit]:
        entry = item(target, text=row.get('text', ''), author=row.get('author', ''),
                     published_at=row.get('time', ''), scope='comment')
        entry['comment_id'] = str(row.get('rpid', ''))
        result.append(entry)
    return result


def browser_failure(exc, target, action):
    """Interpret known subtitle hints without exposing arbitrary upstream output."""
    failure = exc if isinstance(exc, Failure) else classify_text(str(exc))
    code = getattr(exc, 'code', failure.code)
    # OpenCLI puts EMPTY_RESULT's explanation in hint, not in message.
    # Agent Reach currently maps this to UPSTREAM_BROKEN; inspect both fields.
    hint = str(getattr(exc, 'hint', '') or '')
    if (platform_for(target) == 'bilibili' and action == 'subtitle'
            and code in ('UPSTREAM_BROKEN', 'EMPTY_RESULT')
            and any(text in str(exc) + '\n' + hint for text in (
                '此视频没有发现外挂或智能字幕', '字幕文件中没有字幕片段'))):
        return Failure('NOT_FOUND', '当前会话未发现该视频可用的字幕轨或字幕片段；不代表没有画面内嵌字幕。可使用已配置并获授权的音频转写服务')
    return Failure(code, failure.message, failure.retry_after)


def read(target, timeout, action='read', limit=20):
    platform = platform_for(target)
    if platform == 'bilibili' and action in ('subtitle', 'comments'):
        from agent_reach.runtime import browser, tools
        from agent_reach.runtime.browser_binding import query_slot
        from agent_reach.backends.opencli import opencli_call
        from urllib.parse import parse_qs, urlsplit
        tools.extend_path()
        with query_slot(platform):
            browser.ensure_running()
            args = [action, target]
            if action == 'comments':
                args += ['--limit', str(limit)]
            else:
                part = parse_qs(urlsplit(target).query).get('p', ['1'])[0]
                if not part.isdigit() or int(part) < 1:
                    raise Failure('BAD_INPUT', 'B站分P必须是正整数')
                args += ['--page', part]
            rows = opencli_call(platform, args, timeout=timeout,
                login_hint='请用户在专用浏览器的B站官方页面本人登录')
        return bilibili_rows(rows, target, action, limit)
    if platform not in ('douyin', 'wechat') or (action != 'read' and not (platform == 'douyin' and action == 'comments')):
        raise Failure('BAD_INPUT', '浏览器补充动作不受支持')
    from agent_reach.runtime import browser, bridge, tools
    from agent_reach.runtime.browser_binding import query_slot
    from agent_reach.runtime.run import run_cmd
    tools.extend_path()
    with query_slot(platform):
        browser.ensure_running()
        profile = browser.bound_context()
        if not profile or not bridge.require_compatible():
            raise Failure('NEED_SETUP', '现有专用浏览器 Bridge 未连接')
        argv, _ = bridge.launch_spec()
        module = Path(argv[-1]).parent / 'browser/page.js'
        if not module.is_file():
            raise Failure('NEED_SETUP', '现有 OpenCLI 未提供 Page 模块')
        # One navigation and one bounded DOM observation, no synthesized API calls.
        script = ('import {Page} from ' + json.dumps(module.as_uri()) + ';'
            'const page=new Page("thereach-' + platform + '",60,process.argv[1],"background");'
            'await page.goto(process.argv[2]);'
            'let result; const end=Date.now()+8000; do {'
            'result=await page.evaluate(' + json.dumps(EXTRACT_JS) + ');'
            'if(result?.session && result?.data) result=result.data;'
            'if(result?.html || (result?.text && (' + json.dumps(action) + '!=="comments" || result?.comments?.length)) || /验证码|安全验证|环境异常|请先登录|登录后查看/.test(result?.visible || "")) break;'
            'await new Promise(r=>setTimeout(r,500)); } while(Date.now()<end);'
            'console.log(JSON.stringify(result));')
        browser.touch()
        data = json.loads(run_cmd([argv[0], '--input-type=module', '-e', script, profile, canonical(target)], timeout=timeout))
        browser.save_session()
    if (data.get('text') or data.get('html')) and canonical(data.get('url', '')) != canonical(target):
        raise Failure('INVALID_RESPONSE', '浏览器返回正文的页面与请求目标不一致')
    if data.get('html'):
        entry = parse_page(target, data['html'])
        entry['title'] = data.get('title', '')
        entry['author'] = data.get('author', '')
        return [entry]
    if data.get('text'):
        if action == 'comments':
            rows = []
            for comment in data.get('comments', [])[:limit]:
                entry = item(target, text=comment['text'], author=comment.get('author'), scope='comment')
                entry['comment_id'] = comment.get('comment_id')
                rows.append(entry)
            return rows
        return [item(target, title=data.get('title', ''), text=data['text'], author=data.get('author'), scope='description', images=data.get('images'))]
    failure = classify_text(data.get('visible', ''))
    if failure.code == 'UPSTREAM_BROKEN':
        failure = Failure('EMPTY_CONTENT', '专用浏览器未渲染可确认的目标正文')
    raise failure


def audio_info(target, timeout):
    if platform_for(target) != 'bilibili':
        raise Failure('BAD_INPUT', '浏览器音轨目前只支持 B站')
    from agent_reach.runtime import browser, bridge, tools
    from agent_reach.runtime.browser_binding import query_slot
    from agent_reach.runtime.run import run_cmd
    tools.extend_path()
    with query_slot('bilibili'):
        browser.ensure_running()
        profile = browser.bound_context()
        if not profile or not bridge.require_compatible():
            raise Failure('NEED_SETUP', '现有专用浏览器 Bridge 未连接')
        argv, _ = bridge.launch_spec()
        root = Path(argv[-1]).parent
        page = root / 'browser/page.js'
        utils = root.parent.parent / 'clis/bilibili/utils.js'
        if not page.is_file() or not utils.is_file():
            raise Failure('NEED_SETUP', '现有 OpenCLI 缺少 Page/WBI 工具模块')
        browser.touch()
        try:
            output = run_cmd([argv[0], str(Path(__file__).with_name('bilibili_audio.mjs')),
                page.as_uri(), utils.as_uri(), profile, canonical(target)], timeout=timeout)
        except Exception as exc:
            # run_cmd attaches the structured JSON envelope to failed commands.
            output = getattr(exc, 'stdout', '')
            if not output:
                raise
        data = json.loads(output)
        browser.save_session()
        return data


def open_login(target):
    platform_for(target)
    from agent_reach.runtime import browser
    from agent_reach.runtime.browser_binding import exclusive_browser
    with exclusive_browser():
        browser.start(target)
    return {'ok': True, 'opened': target, 'status': 'waiting_for_human',
            'next': '用户本人处理官方页面；完成后 resume 指定平台并 check 原动作。不自动解除停止状态。'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('target')
    p.add_argument('--timeout', type=float, default=25)
    p.add_argument('--action', choices=('read', 'subtitle', 'comments', 'audio', 'login'), default='read')
    p.add_argument('--limit', type=int, default=20)
    a = p.parse_args()
    try:
        if a.action == 'login':
            data = open_login(a.target)
        elif a.action == 'audio':
            data = audio_info(a.target, a.timeout)
        else:
            data = {'ok': True, 'items': read(a.target, a.timeout, a.action, a.limit)}
        print(json.dumps(data, ensure_ascii=False))
        return 0 if data.get('ok') else 2
    except Exception as exc:
        # Never echo upstream JS/HTML/session data in diagnostics.
        failure = browser_failure(exc, a.target, a.action)
        print(json.dumps({'ok': False, 'error': failure.as_dict()}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
