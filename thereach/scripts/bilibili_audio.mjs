// Reuses the installed OpenCLI Page and WBI helper; no cookie export.
const [pageModule, utilsModule, profile, target] = process.argv.slice(2);
const { Page } = await import(pageModule);
const { wbiSign } = await import(utilsModule);
const page = new Page('thereach-bilibili-audio', 60, profile, 'background');
const rawEvaluate = page.evaluate.bind(page);
page.evaluate = async (...args) => {
  const result = await rawEvaluate(...args);
  return result?.session && result?.data ? result.data : result;
};
const fail = (code, message) => { throw Object.assign(new Error(message), {code}); };
function check(payload) {
  const status = payload?.http_status;
  if (status === 412 || payload?.code === -352) fail('PLATFORM_BLOCKED', 'B站要求人工验证；停止音轨获取');
  if (status === 429 || payload?.code === -509) fail('RATE_LIMITED', 'B站限流；停止音轨获取');
  if (status === 401 || payload?.code === -101) fail('NEED_LOGIN', 'B站音轨获取需要本人登录');
  if (status === 403 || payload?.code === -403) fail('PERMISSION_DENIED', 'B站音轨访问权限不足');
  if (!payload || payload.code !== 0) fail('UPSTREAM_BROKEN', 'B站未返回可用的播放信息');
  return payload.data;
}
async function get(path, params, signed = false) {
  if (signed) params = await wbiSign(page, params);
  const url = 'https://api.bilibili.com' + path + '?' + new URLSearchParams(params);
  const response = await page.evaluate(`async () => {
    const response = await fetch(${JSON.stringify(url)}, {credentials: 'include'});
    if (!response.ok) return {http_status: response.status};
    try { return await response.json(); } catch { return {invalid_json: true}; }
  }`);
  return check(response);
}
try {
  const url = new URL(target);
  const bvid = url.pathname.match(/\/video\/(BV[0-9A-Za-z]{10})/)?.[1];
  const part = url.searchParams.get('p') || '1';
  if (!bvid || !/^[1-9][0-9]*$/.test(part)) fail('BAD_INPUT', '需要有效 B站 BV 视频及正整数分P');
  await page.goto(target);
  const view = await get('/x/web-interface/view', {bvid});
  if (view?.bvid !== bvid) fail('INVALID_RESPONSE', '视频身份不匹配');
  if (view?.rights?.pay || view?.rights?.ugc_pay || view?.is_upower_exclusive)
    fail('PERMISSION_DENIED', '付费或专属视频不自动获取音轨，以免处理试看内容');
  const selected = view?.pages?.filter(p => p.page === Number(part));
  if (selected?.length !== 1 || !selected[0].cid || !(selected[0].duration > 0))
    fail('BAD_INPUT', '分P不存在或缺少音轨身份与时长');
  const {cid, duration} = selected[0];
  const play = await get('/x/player/wbi/playurl', {bvid, cid, fnval: 16}, true);
  if (play.timelength && play.timelength / 1000 < duration - 5)
    fail('PERMISSION_DENIED', '播放时长短于视频时长，可能只取得试看内容');
  const audio = (play?.dash?.audio || []).filter(a => a.baseUrl || a.base_url);
  audio.sort((a, b) => (a.bandwidth || Infinity) - (b.bandwidth || Infinity));
  if (!audio.length) fail('NOT_FOUND', '该视频未返回独立 DASH 音轨；不自动下载完整视频');
  const track = audio[0];
  console.log(JSON.stringify({ok: true, audio: {
    url: track.baseUrl || track.base_url, bandwidth: track.bandwidth || 0,
    mime_type: track.mimeType || track.mime_type || 'audio/mp4',
    bvid, cid, part: Number(part), duration_seconds: duration,
    source_url: target, title: view.title || ''
  }}));
} catch (error) {
  // Never print signed media URLs, browser profile, scripts or HTML on failure.
  const known = ['PLATFORM_BLOCKED','RATE_LIMITED','NEED_LOGIN','PERMISSION_DENIED',
    'UPSTREAM_BROKEN','BAD_INPUT','INVALID_RESPONSE','NOT_FOUND'];
  console.log(JSON.stringify({ok: false, error: {
    code: known.includes(error.code) ? error.code : 'UPSTREAM_BROKEN',
    message: known.includes(error.code) ? error.message : '浏览器音轨请求失败；未返回可用播放信息'
  }}));
  process.exitCode = 2;
}
