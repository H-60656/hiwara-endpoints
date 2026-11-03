#!/usr/bin/env python3
"""采集 iwara 各花名 CDN 主机的真实边缘 IP，产出 host -> [ip] 快照。

为什么需要这个：
    国内 DNS 对 *.iwara.tv 是污染的（files -> Facebook 段、i -> 128.242.x）。
    手机侧的 DoT 虽然能拿到真地址，但 853 端口本身会抽风（实测整轮超时过一次）。
    而 iwara 的 CDN 花名主机（herta / hook / naja / zani / aiko / lumi ...）是
    **动态分配**的，写死在代码里的种子表必然覆盖不全。

    所以思路是：让 GitHub Actions 的 runner（境外，DNS 天然干净）每隔一段时间
    扫一批视频，从清晰度清单里抽出真实主机名、解析它们的 A 记录，把结果发布成
    一个 JSON 快照。App 端拉这个快照当"外置种子表"，拉不到再退回 DoT / 邻段 / 种子。

    runner 侧只做采集，不猜任何东西 —— 它看到的 IP 就是当时真实可解析的 IP。

用法：
    python scripts/collect.py                    # 采集并写 endpoints.json
    python scripts/collect.py --limit 32         # 扫多少个视频（默认 32）
    python scripts/collect.py --check            # 只校验现有 endpoints.json 的结构
    python scripts/collect.py --dry-run          # 采集但不写文件，只打印摘要

退出码：0 = 成功；非 0 = 失败（让 Actions 变红，别静默发一份坏数据）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Windows 控制台默认 GBK，中文日志会乱码甚至抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://api.iwara.tv"
# iwara 前端里 x-version 的 salt，见 hiwara/src/core/api/video.ts:91
SALT = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 输出文件（相对仓库根）
OUT_FILE = "endpoints.json"

# 单次 HTTP 请求超时
HTTP_TIMEOUT = 30

# 花名主机名的合法形状：小写字母/数字/连字符，必须能匹配到 iwara.tv 后缀。
# 限制形状是为了不把恶意/畸形字符串写进文件。
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.iwara\.(tv|ai)$")


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- DoT 兜底解析
#
# Actions runner 在国外，系统 DNS 干净，正常用 getaddrinfo 就够。
# 但在被污染的线路上（比如开发机）跑这个脚本时，getaddrinfo 会给假地址，
# 于是加一条 DoT 兜底：直接对 1.1.1.1:853 发 DNS 查询。
#
# 注意 SNI 必须用 IP 字面量 —— 用域名会被 RST。Cloudflare 的证书带 IP SAN，
# 所以证书校验不用关。这几条都是实测结论，别"优化"掉。

DOT_SERVERS = ("1.1.1.1", "1.0.0.1")
DOT_PORT = 853
DOT_TIMEOUT = 8.0


def _dot_build_query(qname: str) -> bytes:
    tid = random.randint(0, 0xFFFF)
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname_bytes = b"".join(bytes([len(p)]) + p.encode() for p in qname.split(".")) + b"\x00"
    return header + qname_bytes + struct.pack("!HH", 1, 1)  # qtype=A, qclass=IN


def _dot_parse(data: bytes) -> list[str]:
    _tid, flags, qdcount, ancount, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if flags & 0xF:
        return []
    offset = 12
    for _ in range(qdcount):
        while offset < len(data) and data[offset]:
            offset += data[offset] + 1
        offset += 5
    out: list[str] = []
    for _ in range(ancount):
        if offset >= len(data):
            break
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset]:
                offset += data[offset] + 1
            offset += 1
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rd = data[offset:offset + rdlen]
        offset += rdlen
        if rtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in rd))
    return out


def dot_resolve(host: str) -> list[str]:
    """对 1.1.1.1:853 发一次 DoT 查询，返回 A 记录。失败返回空列表。"""
    ctx = ssl.create_default_context()
    query = _dot_build_query(host)
    framed = struct.pack("!H", len(query)) + query
    for server in DOT_SERVERS:
        try:
            with socket.create_connection((server, DOT_PORT), timeout=DOT_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=server) as tls:
                    tls.sendall(framed)
                    head = tls.recv(2)
                    if len(head) < 2:
                        continue
                    size = struct.unpack("!H", head)[0]
                    body = b""
                    while len(body) < size:
                        chunk = tls.recv(size - len(body))
                        if not chunk:
                            break
                        body += chunk
            ips = _dot_parse(body)
            if ips:
                return ips
        except Exception:
            continue
    return []


def http_json(url: str, headers: dict[str, str] | None = None) -> Any:
    """GET 一个 JSON。

    先走「DoT 解析 + 钉子连接」这条路（在污染线路和干净线路都能用），
    失败再退回普通的 urllib 请求（让 runner 用自己的干净 DNS）。
    两条都不行就抛出。

    为什么把钉子放前面：本机（以及大量国内线路）系统 DNS 对 *.iwara.tv 是污染的，
    urllib 会先卡满一个 connect 超时（实测 21 秒）才失败，每请求白等这么久。
    钉子是确定性的，能在几百毫秒内出结果。
    """
    req_headers = {"User-Agent": UA, **(headers or {})}

    pinned = _http_json_pinned(url, req_headers)
    if pinned is not None:
        return pinned

    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _neighbors(ip: str, span: int = 4) -> list[str]:
    """同 /24 段里上下各 span 个邻居。

    实测必要性：`filesq.iwara.tv` 的 DoT 答案可能指向已经下线的 `5.61.209.181`，
    而同段 `5.61.209.180` / `.182` 是活的、能正常服务。只试 DoT 那一个会全盘失败。
    """
    parts = ip.split(".")
    if len(parts) != 4:
        return []
    try:
        last = int(parts[3])
    except ValueError:
        return []
    out: list[str] = []
    for step in range(1, span + 1):
        for candidate in (last + step, last - step):
            if 0 < candidate < 255:
                out.append(f"{parts[0]}.{parts[1]}.{parts[2]}.{candidate}")
    return out


def _http_json_pinned(url: str, headers: dict[str, str]) -> Any | None:
    """自己解析 + 自己连 + 自己解 HTTP，绕开被污染的系统 DNS。

    这里每一个参数都是实测出来的，别随手"简化"：

    * **TLS 1.3-only**：Python 默认 `TLS1.2+1.3` 一律被 Cloudflare 回 403
      （挑战页 `Just a moment...`，5834 字节）。锁到 1.3 才 200。
    * **ALPN 只给 `http/1.1`**：给 `["h2","http/1.1"]` 会协商到 h2，而我们这里是
      手写的 HTTP/1.1 报文，读回来是 h2 帧，解不出来。只给 h1 服务端就按 h1 回。
    * **`Referer` + `X-Site`**：缺了同样 403。这是 iwara 后端的站点判定。
    * **`Accept-Encoding: identity`**：免掉 gzip/deflate/br 的兼容分支。
    * **要试同段邻居**：DoT 给的地址可能刚好是下线的那台（见 `_neighbors`）。
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    # 同机直接解析的答案可能是污染的，优先 DoT
    primary = dot_resolve(host)
    if not primary:
        try:
            primary = [ai[4][0] for ai in socket.getaddrinfo(host, 443, family=socket.AF_INET)]
        except Exception:
            return None

    candidates: list[str] = []
    for ip in primary:
        if ip not in candidates:
            candidates.append(ip)
    # 邻居排在后面：先信 DoT 的答案，不通再猜旁边
    for ip in primary:
        for neighbor in _neighbors(ip):
            if neighbor not in candidates:
                candidates.append(neighbor)

    ctx = ssl.create_default_context()
    # 只允许 1.3 —— 1.2 会被 Cloudflare 判定为 bot
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(["http/1.1"])

    send_headers = {
        "Host": host,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.iwara.tv/",
        "X-Site": "www.iwara.tv",
        "Accept-Encoding": "identity",
        "Connection": "close",
        **headers,
    }

    last_error: str = ""
    for ip in candidates:
        try:
            sock = socket.create_connection((ip, 443), timeout=HTTP_TIMEOUT)
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                request = (
                    f"GET {path} HTTP/1.1\r\n"
                    + "".join(f"{k}: {v}\r\n" for k, v in send_headers.items())
                    + "\r\n"
                )
                tls.sendall(request.encode("utf-8"))

                chunks: list[bytes] = []
                while True:
                    piece = tls.recv(65536)
                    if not piece:
                        break
                    chunks.append(piece)
            raw = b"".join(chunks)

            head_end = raw.find(b"\r\n\r\n")
            if head_end < 0:
                last_error = f"{ip}: 响应里找不到头部结束标记"
                continue
            head = raw[:head_end].decode("latin-1", "replace")
            body = raw[head_end + 4:]

            status = head.splitlines()[0] if head else ""
            if " 200 " not in f" {status} ":
                last_error = f"{ip}: {status}（多半是被 Cloudflare 拦了）"
                continue

            lower = head.lower()
            if "transfer-encoding: chunked" in lower:
                body = _dechunk(body)
            if "content-encoding: gzip" in lower:
                import gzip

                body = gzip.decompress(body)

            return json.loads(body.decode("utf-8"))
        except Exception as exc:
            last_error = f"{ip}: {type(exc).__name__}: {exc}"
            continue

    if last_error:
        log(f"    ! 钉子直连失败：{last_error}")
    return None


def _dechunk(body: bytes) -> bytes:
    out = b""
    while body:
        line_end = body.find(b"\r\n")
        if line_end < 0:
            break
        try:
            size = int(body[:line_end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[line_end + 2:line_end + 2 + size]
        body = body[line_end + 2 + size + 2:]
    return out


def x_version(file_url: str) -> str | None:
    """从 filesq 的 fileUrl 里算出 x-version（sha1("<id>_<expires>_<salt>")）。"""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(file_url)
    file_id = parsed.path.rstrip("/").split("/")[-1]
    expires = (parse_qs(parsed.query).get("expires") or [None])[0]
    if not file_id or not expires:
        return None
    return hashlib.sha1(f"{file_id}_{expires}_{SALT}".encode()).hexdigest()


def host_of(protocol_relative: str) -> str | None:
    """从 `//herta.iwara.tv/view?...` 这类协议相对地址里抽出主机名。"""
    if not protocol_relative:
        return None
    value = protocol_relative.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        from urllib.parse import urlparse

        host = (urlparse(value).hostname or "").lower()
    except Exception:
        return None
    if HOST_RE.match(host):
        return host
    return None


def resolve_a(host: str) -> list[str]:
    """解析 A 记录。

    先 DoT（污染线路和干净线路都给真答案），失败才退回 runner 自己的
    getaddrinfo。这样同一份脚本在 GitHub Actions（干净）和国内开发机（污染）
    上都能得到正确结果 —— 否则本机跑出来的全是 Facebook/Twitter 段的假地址。
    """
    ips = dot_resolve(host)
    if ips:
        return ips
    try:
        infos = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except Exception as exc:  # 解析不到不算致命，跳过即可
        log(f"    ! {host} 解析失败：{exc}")
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def probe_a_record(host: str, ip: str, timeout: float = 6.0) -> bool:
    """确认这个 IP 真的在服务这台主机（TLS 握手 + SNI）。

    为什么不能只靠解析：iwara 的边缘偶尔会把某台花名指向已经下线的地址
    （实测 bailu 的解析结果不通，同段邻居才活）。发一次带 SNI 的握手能快速
    筛掉死的。

    TLS 版本这里**不锁 1.3** —— 我们要的只是"这台机器活着且在服务这个 SNI"，
    不是通过 Cloudflare 的 bot 检查。1.2 也能证明这事，而且更快。
    """
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:
        return False


def resolve_and_verify(host: str) -> list[str]:
    """解析 + 验证，并且像 App 端那样把同段邻居也试一遍。

    DoT 的答案可能是那台刚下线的（实测 `bailu` 给 `62.210.95.208` 已死，
    `.211` 才活着）。所以先试解析结果，都不行再试邻居。
    """
    primary = resolve_a(host)
    alive = [ip for ip in primary if probe_a_record(host, ip)]
    if alive:
        return alive

    tried = set(primary)
    alive = []
    for ip in primary:
        for neighbor in _neighbors(ip):
            if neighbor in tried:
                continue
            tried.add(neighbor)
            if probe_a_record(host, neighbor, timeout=4.0):
                alive.append(neighbor)
    return alive


def fetch_manifest_hosts(video_id: str) -> list[str]:
    """取一个视频的清晰度清单，回传清单里出现过的所有主机名。"""
    try:
        detail = http_json(f"{API}/video/{video_id}")
    except Exception as exc:
        log(f"    ! {video_id} 详情失败：{exc}")
        return []

    file_url = ((detail or {}).get("fileUrl") or "").strip()
    if not file_url:
        return []

    version = x_version(file_url)
    if not version:
        log(f"    ! {video_id} 算不出 x-version")
        return []

    try:
        manifest = http_json(file_url, headers={"x-version": version})
    except Exception as exc:
        log(f"    ! {video_id} 清单失败：{exc}")
        return []

    hosts: list[str] = []
    if not isinstance(manifest, list):
        return hosts
    for item in manifest:
        if not isinstance(item, dict):
            continue
        src = item.get("src") or {}
        if not isinstance(src, dict):
            continue
        for key in ("view", "download"):
            host = host_of(src.get(key) or "")
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def collect(limit: int) -> dict[str, Any]:
    log(f"=== 1. 取最新 {limit} 个视频 ===")
    videos: list[dict[str, Any]] = []
    page = 0
    while len(videos) < limit:
        want = min(32, limit - len(videos))
        url = f"{API}/videos?rating=all&page={page}&limit={want}&sort=date"
        try:
            payload = http_json(url)
        except Exception as exc:
            fail(f"列表请求失败：{exc}")
        # 上游返回的是 {"count": N, "limit": N, "page": N, "results": [...]}，
        # 不是裸数组。别按数组解。
        batch = (payload or {}).get("results") if isinstance(payload, dict) else payload
        if not isinstance(batch, list) or not batch:
            break
        videos.extend(batch)
        page += 1
        if page > 20:  # 防御性上限
            break
    videos = videos[:limit]
    if not videos:
        fail("列表返回 0 条 —— 上游结构或接口变了")
    log(f"  拿到 {len(videos)} 个视频")

    log(f"=== 2. 逐个取清晰度清单，收集花名主机 ===")
    host_hits: dict[str, int] = {}
    for idx, item in enumerate(videos, 1):
        vid = (item or {}).get("id")
        if not vid:
            continue
        hosts = fetch_manifest_hosts(vid)
        log(f"  [{idx}/{len(videos)}] {vid} -> {hosts or '（无）'}")
        for host in hosts:
            host_hits[host] = host_hits.get(host, 0) + 1
        time.sleep(0.2)  # 轻一点，别把上游打出限流

    if not host_hits:
        fail("一个主机名都没扫到 —— 上游结构可能变了，检查 fetch_manifest_hosts")
    log(f"  共发现 {len(host_hits)} 台主机：{sorted(host_hits)}")

    log("=== 3. 解析 A 记录并验证可用性 ===")
    hosts_out: dict[str, Any] = {}
    for host in sorted(host_hits):
        ips = resolve_a(host)
        alive = [ip for ip in ips if probe_a_record(host, ip)]
        log(f"  {host}: 解析 {ips} -> 存活 {alive}")
        if alive:
            hosts_out[host] = {
                "ips": alive,
                "seen": host_hits[host],
            }
    if not hosts_out:
        fail("所有主机都解析/验证失败，不发坏数据")

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://github.com/H-60656/hiwara-endpoints",
        "sampled_videos": len(videos),
        "hosts": hosts_out,
        # 兜底：Cloudflare 后面的 API 主机（这两个基本不变，但顺手带上，
        # 让 App 端只认一个文件就够）
        "api": {
            "api.iwara.tv": resolve_a("api.iwara.tv"),
        },
    }


def check(path: str) -> int:
    if not os.path.isfile(path):
        fail(f"{path} 不存在")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    problems: list[str] = []
    if data.get("schema") != 1:
        problems.append(f"schema 不是 1：{data.get('schema')}")
    if not data.get("generated_at"):
        problems.append("缺 generated_at")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        problems.append("hosts 为空或不是对象")
    else:
        for host, info in hosts.items():
            if not HOST_RE.match(host):
                problems.append(f"主机名形状不对：{host}")
            ips = (info or {}).get("ips")
            if not isinstance(ips, list) or not ips:
                problems.append(f"{host} 没有 ip")
                continue
            for ip in ips:
                if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(ip)):
                    problems.append(f"{host} 的 {ip} 不是 IPv4")
    for p in problems:
        log(f"  [x] {p}")
    log(f"  主机数 = {len(hosts or {})}")
    if problems:
        return 1
    log("  [ok] 结构校验通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集 iwara CDN 边缘 IP 快照")
    parser.add_argument("--limit", type=int, default=32, help="扫多少个最新视频（默认 32）")
    parser.add_argument("--out", default=OUT_FILE, help=f"输出文件（默认 {OUT_FILE}）")
    parser.add_argument("--check", action="store_true", help="只校验现有文件结构")
    parser.add_argument("--dry-run", action="store_true", help="采集但不写文件")
    args = parser.parse_args(argv)

    if args.check:
        return check(args.out)

    started = time.time()
    data = collect(args.limit)
    elapsed = time.time() - started

    if args.dry_run:
        log("=== dry-run：不写文件 ===")
        log(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return 0

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    total_ips = sum(len(v["ips"]) for v in data["hosts"].values())
    log(f"\n完成：{args.out}")
    log(f"  主机 {len(data['hosts'])} 台 / IP {total_ips} 个 / 扫描 {data['sampled_videos']} 个视频 / 耗时 {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
