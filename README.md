# hiwara-endpoints

iwara CDN 边缘 IP 快照，每小时由 GitHub Actions 自动刷新（每小时第 3、33 分钟各一轮）。

给 [Hiwara](https://github.com/shanmaomaoymmm/hiwara) 的 Android 客户端当「外置种子表」用。

## 为什么需要这个

iwara 的视频字节不在 `api.iwara.tv`，而在**一堆用游戏角色命名的 CDN 主机**上：

```
herta.iwara.tv   hook.iwara.tv    naja.iwara.tv    zani.iwara.tv
aiko.iwara.tv    bailu.iwara.tv   blade.iwara.tv   bronya.iwara.tv
clara.iwara.tv   firefly.iwara.tv himeko.iwara.tv  jade.iwara.tv
lumi.iwara.tv    pela.iwara.tv    phoebe.iwara.tv  silverwolf.iwara.tv
swan.iwara.tv    topaz.iwara.tv   welt.iwara.tv    youhu.iwara.tv
yuko.iwara.tv     yukong.iwara.tv ...
```

这些名字是**动态分配**的：哪个视频落到哪台主机不固定，每台的 IP 也各不相同。所以客户端里写死一张种子 IP 表，必然覆盖不全 —— 表现就是「有的视频能播、有的播不了」。

加上国内网络对 `*.iwara.tv` 的 DNS 污染（`files` → Facebook 段、`i` → `128.242.x`），客户端想自己解析也拿不到真地址。

这里的做法：**让境外的机器去解析**。Actions 的 runner 在国外，`getaddrinfo` 直接就是干净答案。采集完发布成 JSON，客户端拉下来当动态种子表。

## 数据怎么来的

`scripts/collect.py` 每轮做四件事：

1. 拉最新 32 个视频的列表（`/videos?rating=all&sort=date`）
2. 逐个取详情拿 `fileUrl`，用 `x-version` 打 `filesq` 拿清晰度清单
3. 从清单里的 `src.view` / `src.download` 抽出所有 `*.iwara.tv` 主机名 —— 这是**发现新主机**的唯一途径
4. 逐台解析 A 记录，并且**真的做一次带 SNI 的 TLS 握手**确认这台机器活着（解析结果可能是刚下线的那台）

产物是仓库根目录的 `endpoints.json`。

## 文件格式

```json
{
  "schema": 1,
  "generated_at": "2026-11-03T03:54:56Z",
  "source": "https://github.com/H-60656/hiwara-endpoints",
  "sampled_videos": 32,
  "hosts": {
    "naja.iwara.tv":   { "ips": ["51.159.14.209"],  "seen": 3 },
    "yukong.iwara.tv": { "ips": ["151.115.90.3"],   "seen": 1 }
  },
  "api": {
    "api.iwara.tv": ["104.20.28.232", "172.66.170.170"]
  }
}
```

- `ips` 只包含**验证过活着**的地址（握手成功）
- `seen` 是本轮里这台主机被多少个视频引用（越大说明越常用，客户端可以优先试）
- `api` 是 Cloudflare 后面的 API 主机，基本不变，顺手带上免得客户端要维护两个来源

## 客户端怎么用

取值顺序（每一层失败都静默往下走，**永不阻塞播放**）：

```
1. raw.githubusercontent.com/H-60656/hiwara-endpoints/main/endpoints.json
2. ghproxy.net/https://raw.githubusercontent.com/...
3. gh-proxy.com/https://raw.githubusercontent.com/...
4. DoT 直接查 1.1.1.1:853
5. 同 /24 邻居探测
6. 内置种子表
```

前三层是同一个文件的不同取法 —— 国内 `github.com` 和 `jsdelivr` 经常不通，但 `raw.githubusercontent.com` 目前实测可达（200 / 0.46s）。多备两个代理是因为这个可达性会变。

一次 GET 只有几 KB，失败 4 秒超时放弃，后台线程做，不挡 UI。

## 刷新节奏，以及踩过的两个坑

设计意图：`collect.yml` 的 cron 是 `3 * * * *` 与 `33 * * * *`（UTC），即每小时第 3、33 分钟各跑一轮 —— 两条是为了：一条被吞时另一条还在。每轮约 50 秒。

实际状况：**这个仓库的 `schedule` 不可靠**。完整实测时间线（UTC 2026-11-03）：

| 时间 | 现象 |
|---|---|
| 03:27 建仓 → 16:25 | `schedule` **0 次**，但 push / workflow_dispatch 全正常，workflow 一直 `active` |
| 16:29 推空提交 | **立刻恢复**：16:26、16:48 两轮 schedule 都 success |
| 17:03 / 17:33 / 18:03 / 18:33 | 又全部没跑 |
| 18:29 再推空提交 | 到 19:01 仍 0 轮，没再恢复 |

**坑一：新仓库的 `schedule` 可能根本不触发，而且会反复。**
症状是「状态正常、手动能跑、定时为 0」，社区讨论 #203822 / #185828 / #159649 都是同一现象。往默认分支推空提交能碰运气救回来一阵：

```bash
git commit --allow-empty -m "refresh schedule trigger"
git push
```

（推不上去时也可以走 Git Data API：`POST /git/commits` 用同一 tree + 当前 head 作 parent，再 `PATCH /git/refs/heads/main`。）

但别指望它长期有效 —— 这个仓库推了两次，第二次就没恢复。所以真正的定时**改用外部定时器**，见下一节。

**坑二：采集期间别人往 main 推东西，会让 `git push` 被拒。**
采集本身跑 50 秒，这期间若有人（或我用 contents API）改了仓库，收尾的裸 `git push` 会撞成 `! [rejected] main -> main (fetch first)`，整个 job 变红（2026-11-03 17:21 的 run#9 就是这么失败的）。现在「提交变更」步骤会保留本轮采集结果、重新拉最新 `main`、再推，最多重试 3 次。

## 真正的定时：Cloudflare Worker 打 dispatch

GitHub 的 `schedule` 靠不住，但 `workflow_dispatch` 一直很稳。所以定时搬到 Cloudflare 的免费 Worker 上：

```
Cloudflare cron（每小时 :03）
        │
        ▼
Worker → POST /repos/H-60656/hiwara-endpoints/actions/workflows/collect.yml/dispatches
        │
        ▼
collect.yml 跑一轮 → 刷新 endpoints.json → 提交
```

代码在 [`cron/cloudflare/`](cron/cloudflare/)，部署说明也在那里的 README。三条命令：

```bash
cd cron/cloudflare
npm install
npx wrangler secret put GITHUB_TOKEN    # fine-grained PAT，只给这个仓库 Actions: Read and write
npx wrangler deploy
```

免费额度占用约 **0.024%**（每天 24 次请求 / 上限 100,000 次）。

`collect.yml` 里的 cron **保留着** —— 万一哪天 GitHub 修好了，两条路都在跑也没坏处（采集脚本本身幂等，两次结果一样时不会产生新提交）。不想让它再跑就把 `schedule:` 整段删掉。

客户端不依赖这里的准时性 —— 降级链会把「快照是几小时前的」自动兜住。

## 本地跑

```bash
python scripts/collect.py              # 采集并写 endpoints.json
python scripts/collect.py --limit 32   # 指定扫多少个视频
python scripts/collect.py --dry-run    # 只打印不写文件
python scripts/collect.py --check      # 只校验现有文件结构
```

纯标准库，没有依赖。

脚本里带了一条 DoT 兜底路径，所以**在被污染的国内网络上也能跑通**（会自动 TLS 1.3-only + ALPN h1 + DoT 解析）。这条路径上的每个参数都是实测出来的，改动前先看 `_http_json_pinned` 的注释。

## 已知限制

- **快照最多滞后一小时**。新视频如果在两轮之间换了 CDN 主机，客户端这一轮可能不知道 —— 所以客户端仍保留 DoT 与邻居探测作为兜底，不是只靠这个文件。
- **定时依赖 Cloudflare Worker**（`cron/cloudflare/`）。不部署它的话，数据只在有人 push 时刷新；GitHub 自己的 `schedule` 别指望。Worker 挂了就是快照变旧，客户端行为不受影响。
- **上游结构变了脚本会失败**（退出码非 0，job 变红）。这是故意的：宁可失败，也不要发布一份空数据让客户端以为「所有主机都没了」。
- **IP 会漂移**。跑在 `151.115.x` / `62.210.x` / `51.159.x` 这几段上的机器偶尔会换，`seen` 高说明常用，但不保证长期不变。
- 本仓库只发布 DNS 快照，不代理任何流量，不含任何账号信息或视频内容。

## 许可

脚本部分随意取用。数据是公开 DNS 记录的聚合。
