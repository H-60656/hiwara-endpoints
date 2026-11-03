/**
 * hiwara-endpoints 的外部定时器（Cloudflare Worker）。
 *
 * 为什么需要它：这个仓库自己的 `schedule` 事件不可靠（实测记录见
 * ../README.md 和 .github/workflows/collect.yml 的注释）。Worker 的 cron 是
 * Cloudflare 一侧的定时器，不依赖 GitHub 的调度器，所以稳。
 *
 * 它做的事只有一件：调 GitHub 的 workflow_dispatch 接口，让 collect.yml 跑一轮。
 *
 * 需要的配置（都放在 Worker 的 secret 里，不进代码）：
 *   GITHUB_TOKEN  —— fine-grained PAT，只给 H-60656/hiwara-endpoints 这一个仓库，
 *                    权限只勾 "Actions: Read and write"。别用账号级的 classic token。
 *   TRIGGER_KEY   —— 可选。设了的话，手动触发要带 ?key=<值>，防止别人白嫖你的配额。
 */

const OWNER = 'H-60656';
const REPO = 'hiwara-endpoints';
const WORKFLOW = 'collect.yml';
const REF = 'main';

const API_VERSION = '2022-11-28';

async function dispatch(env) {
  const token = env.GITHUB_TOKEN;
  if (!token) {
    return { ok: false, status: 0, detail: 'GITHUB_TOKEN 没设置（wrangler secret put GITHUB_TOKEN）' };
  }

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': API_VERSION,
        'User-Agent': 'hiwara-endpoints-cron',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: REF }),
    });
  } catch (error) {
    return { ok: false, status: 0, detail: `请求没发出去：${String(error)}` };
  }

  // 成功是 204 无内容，不是 200
  if (res.status === 204) {
    return { ok: true, status: 204, detail: `已触发 ${WORKFLOW}（ref=${REF}）` };
  }

  const body = await res.text();
  return {
    ok: false,
    status: res.status,
    detail: `GitHub 回了 ${res.status}：${body.slice(0, 300)}`,
  };
}

export default {
  // Cloudflare cron 到点走这里
  async scheduled(event, env, ctx) {
    const result = await dispatch(env);
    console.log(
      `[cron] ${new Date(event.scheduledTime).toISOString()} ${result.ok ? 'ok' : 'FAILED'} ` +
        `status=${result.status} ${result.detail}`
    );
    // 失败时抛错，Cloudflare 的日志/tail 里能直接看见红字
    if (!result.ok) throw new Error(result.detail);
  },

  // 手动验证：浏览器/curl 打 /run 就触发一次
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname !== '/run') {
      return new Response(
        [
          'hiwara-endpoints 外部定时器',
          '',
          `定时：cron 3 * * * *（每小时第 3 分钟）→ 触发 ${OWNER}/${REPO} 的 ${WORKFLOW}`,
          '手动：GET /run 触发一次',
          '',
        ].join('\n'),
        { headers: { 'Content-Type': 'text/plain; charset=utf-8' } }
      );
    }

    if (env.TRIGGER_KEY && url.searchParams.get('key') !== env.TRIGGER_KEY) {
      return new Response('key 不对\n', { status: 403 });
    }

    const result = await dispatch(env);
    return new Response(`${JSON.stringify(result, null, 2)}\n`, {
      status: result.ok ? 200 : 502,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  },
};
