# hiwara-endpoints 外部定时器（Cloudflare Worker）

这个仓库自己的 `schedule` 事件不可靠 —— 实测记录见根目录 `README.md` 的
「刷新节奏」一节：建仓头 12 小时跑 0 次，推空提交救回来两轮，之后又全停，
再推一次也没恢复；后来又零零散散自己跑了几轮，时间点也没规律。
`push` 和 `workflow_dispatch` 始终正常。

所以真正的定时换到这里：**Cloudflare Worker 的 cron 打 GitHub 的 `workflow_dispatch`**。
Cloudflare 的定时器是它自己一侧的，不看 GitHub 的调度器脸色。

```
Cloudflare cron（每小时 :03）
        │
        ▼
Worker: POST /repos/H-60656/hiwara-endpoints/actions/workflows/collect.yml/dispatches
        │
        ▼
collect.yml 跑一轮（约 50 秒）→ 刷新 endpoints.json → 提交
```

## 为什么要单独一个 PAT

Worker 需要一个 GitHub token 才有权触发 workflow。**别用账号级的 classic token**，
去建一个 fine-grained PAT：

1. <https://github.com/settings/personal-access-tokens/new>
2. `Repository access` → **Only select repositories** → `hiwara-endpoints`
3. `Permissions` → `Repository permissions` → **Actions: Read and write**
   （其他全部留 `No access`）
4. 有效期按需，最长一年

这样即使 token 泄漏，也只能触发这一个仓库的 workflow，读不到代码也删不了东西。

## 部署

需要 Node 18+（本机 `node --version` 已是 v25）。

```bash
cd cron/cloudflare
npm install

# 先把 token 写进 secret（不会进仓库）
npx wrangler secret put GITHUB_TOKEN
# 粘贴上一步的 fine-grained PAT

# 可选：给手动触发加个口令，防止别人打你的 /run 白嫖配额
npx wrangler secret put TRIGGER_KEY

npx wrangler deploy
```

`wrangler deploy` 会打印 Worker 的 URL，形如
`https://hiwara-endpoints-cron.<你的子域>.workers.dev`。

首次部署时会要求 `wrangler login`（浏览器授权一次），免费账号即可。

## 验证

```bash
# 手动触发一轮，看返回
curl "https://hiwara-endpoints-cron.<子域>.workers.dev/run?key=<TRIGGER_KEY>"
# → {"ok":true,"status":204,"detail":"已触发 collect.yml（ref=main）"}

# 看 Worker 的实时日志（cron 到点也会打在这）
npx wrangler tail
```

再去 <https://github.com/H-60656/hiwara-endpoints/actions> 确认多了一条
`workflow_dispatch` 的 run。

## 部署后一定会踩的两个坑（实测）

### 坑一：本地访问不了 `*.workers.dev`，但 Worker 其实是好的

症状：`curl` 报 `无法连接到远程服务器`，浏览器打不开
`https://hiwara-endpoints-cron.<子域>.workers.dev`，超时 20 秒。

原因不是 Worker 没部署成功，而是**本地网络在 TLS 握手的 SNI 层就把
`*.workers.dev` 掐了**。用同一个 Cloudflare 边缘 IP 只换 SNI 就能看出来：

| 目标 IP | 握手里的 SNI | 结果 |
|---|---|---|
| `104.20.28.232` | `api.iwara.tv` | ✅ TLSv1.3，0.5 s |
| `104.20.28.232` | `hiwara-endpoints-cron.<子域>.workers.dev` | ❌ `ConnectionResetError` |
| `104.21.41.200` | `example.com` | ✅ TLSv1.3，0.42 s |
| `104.21.41.200` | `hiwara-endpoints-cron.<子域>.workers.dev` | ❌ `ConnectionResetError` |
| `104.21.41.200` | `workers.dev` | ❌ `ConnectionResetError` |

换成 TLS 1.2 / 1.3、关掉或改 ALPN、跳过证书校验，**结果全一样** —— 判据只有
SNI 里那个域名。顺带一提，本地 DNS 对 `*.workers.dev` 也给的是污染地址
（`199.16.156.38`、`108.160.167.158` 之类），真值要用 DoT 查
（`104.21.41.200` / `172.67.192.74`），但**钉了真 IP 照样连不上**，
所以这不是 DNS 的问题。

**这不影响功能。** cron 是 Cloudflare 那边到点自己执行的，也是 Cloudflare
那边去调 `api.github.com`，跟你本机能不能打开 workers.dev 一点关系都没有。
只有**手动**验 `/run` 时才需要绕一下：

```bash
# 走本机代理（端口按自己的改）
curl -sS -k -m 25 --proxy http://127.0.0.1:7897 \
  "https://hiwara-endpoints-cron.<子域>.workers.dev/run?key=<TRIGGER_KEY>"
```

能拿到 `{"ok":true,"status":204,...}` 就说明 Worker 完全正常。

### 坑二：`/run` 回 502，`Resource not accessible by personal access token`

```json
{
  "ok": false,
  "status": 403,
  "detail": "GitHub 回了 403：{\"message\":\"Resource not accessible by personal access token\",\n\"documentation_url\":\"https://docs.github.com/rest/actions/workflows#create-a-workflow-dispatch-event\"}"
}
```

这不是 Worker 写错了，是 **fine-grained PAT 少了 `Actions: Read and write`**。
Worker 只是如实把 GitHub 的 403 转成了 502。

修法：回 <https://github.com/settings/personal-access-tokens> 打开那枚 token →
`Repository permissions` → **Actions 改成 `Read and write`** → `Save`。

**改完不用重新 `wrangler deploy`，也不用重新 `secret put`** —— secret 里的
token 字符串没变，权限是 GitHub 服务端每次请求时重新算的。改完直接再打一次
`/run` 即可。

> 用 classic token（`gho_…` / `ghp_…`）也能通，只要勾了 `workflow` scope。
> 但那样 token 对**整个账号**有权，泄漏面大，所以还是推荐 fine-grained。

## 免费额度

Cloudflare Workers Free：

| 项目 | 额度 | 我们的用量 |
|---|---|---|
| 请求数 | 100,000 次/天 | 24 次/天（每小时一次） |
| CPU 时间 | 10 ms/次 | 一次 `fetch` 出去就返回，远低于 |
| Cron 触发 | 免费档支持 | 1 条 `3 * * * *` |

占用约 **0.024%**，不用管。

## 本地调试

```bash
cd cron/cloudflare
cat > .dev.vars <<'EOF'
GITHUB_TOKEN=ghp_xxx
TRIGGER_KEY=test
EOF
npx wrangler dev
# 另开一个窗口
curl "http://127.0.0.1:8787/run?key=test"
```

`.dev.vars` 已在 `.gitignore` 里，不会进仓库。

## 文件

| 文件 | 作用 |
|---|---|
| `src/index.js` | Worker 本体：`scheduled` 走 cron，`fetch` 走手动 `/run` |
| `wrangler.toml` | Worker 名、入口、cron 表达式、可观测性 |
| `package.json` | 只依赖 `wrangler`（devDependency） |

## 不想用 Cloudflare 的话

同样的思路换任何「能定时发 HTTP 请求」的免费服务都行，最省事的是
**任意一台常开的机器加一条 crontab**：

```cron
3 * * * * curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/H-60656/hiwara-endpoints/actions/workflows/collect.yml/dispatches \
  -d '{"ref":"main"}'
```

Worker 只是把这个 curl 挪到了云上，不用自己养机器。
