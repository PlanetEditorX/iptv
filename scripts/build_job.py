#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import json
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import time
import random
import urllib.parse
from datetime import datetime, timedelta

from quality_raw import (
    quality_score,
    cache,
    save_all
)

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
STATE_DIR = SOURCES_DIR / "state"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

UPSTREAM_FAIL_FILE = STATE_DIR / "upstream_fail.json"
UPSTREAM_BLOCKLIST_FILE = STATE_DIR / "upstream_blocklist.json"

# ============================
# 全局变量
# ============================

WHITELIST = set()
BLACKLIST = []

URL_SOURCE = {}              # URL → 上游源
SOURCE_TOTAL = defaultdict(int)
SOURCE_FAIL = defaultdict(int)

UPSTREAM_FAIL = defaultdict(int)
UPSTREAM_BLOCKLIST = {}

FILTERED_LOG = defaultdict(list)

# 自动创建必要目录（本地调试用）
SOURCES_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================
# 工具函数
# ============================

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return default
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def is_numeric_channel(name: str) -> bool:
    n = name.strip()
    n = re.sub(r"[台频道]+$", "", n)
    return n.isdigit()

# ============================
# 名称规范化
# ============================

def normalize_name(name: str) -> str:
    raw = name.strip().upper()

    m = re.match(r"CCTV[- ]?0?(\d+)", raw)
    if m:
        return f"CCTV{m.group(1)}"

    m2 = re.match(r"(.*?卫视)", raw)
    if m2:
        return m2.group(1)

    cleaned = re.sub(r"(4K|8K|HD|FHD|UHD|超清|高清|标清|频道|综合)$", "", raw)
    cleaned = re.sub(r"\d+[MPKp]+$", "", cleaned)
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", cleaned)

    return cleaned

# ============================
# URL 过滤 / 归一化
# ============================

def is_good_url(u: str) -> bool:
    u = u.strip()
    if not u.startswith("http"):
        return False
    if u.endswith("$"):
        return False
    bad = ["udp/", "rtp/", "://239.", "://224."]
    return not any(k in u for k in bad)

def is_local_source(url: str) -> bool:
    u = url.lower()
    return (
        u.startswith("rtp://")
        or u.startswith("udp://")
        or "://239." in u
        or "://224." in u
        or "/rtp/" in u
    )

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

def normalize_url(url: str) -> str:
    if not url.startswith("http"):
        return url

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))

    drop_keys = {
        "token", "auth", "ts", "sign", "expires", "expiry",
        "e", "_t", "_ts", "uuid", "session", "sessionid",
        "v", "ver", "random", "r", "t"
    }

    query = {k: v for k, v in query.items() if k.lower() not in drop_keys}
    sorted_query = urlencode(sorted(query.items()))

    path = parsed.path.rstrip("/")
    path = re.sub(r"\.m3u8.*$", ".m3u8", path)
    path = re.sub(r"\.flv.*$", ".flv", path)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        sorted_query,
        parsed.fragment
    ))

# ============================
# 添加频道源
# ============================

def add_channel(channels, name, url, source_url=None):
    name = normalize_name(name)
    url = normalize_url(url.strip())

    if not name or not url:
        return

    if name not in WHITELIST:
        for key in BLACKLIST:
            if key in name:
                FILTERED_LOG[name].append({
                    "url": url,
                    "keyword": key,
                    "source": source_url
                })
                return

    # 本地源也要加入 channels
    if not is_good_url(url) and not is_local_source(url):
        return

    if source_url == "local_spider":
        URL_SOURCE[url] = "local_spider"
    elif url not in URL_SOURCE:
        URL_SOURCE[url] = source_url

    if url not in channels[name]:
        channels[name].append(url)

# ============================
# 解析 TXT / M3U / JSON
# ============================

def parse_txt_like(content, channels, source_url=None):
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ",http" in line:
            name, url = line.split(",", 1)
        elif "#" in line and "http" in line:
            name, url = line.split("#", 1)
        else:
            continue
        add_channel(channels, name, url, source_url)

def parse_m3u(content, channels, source_url=None):
    last_name = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            if "," in line:
                last_name = line.split(",", 1)[1].strip()
        elif line and not line.startswith("#") and last_name:
            add_channel(channels, last_name, line, source_url)
            last_name = None

def parse_tvbox_json(content, channels, source_url=None):
    try:
        data = json.loads(content)
    except:
        return
    lives = data.get("lives") or []
    for live in lives:
        for ch in live.get("channels", []):
            name = ch.get("name")
            urls = ch.get("urls") or []
            for url in urls:
                add_channel(channels, name, url, source_url)

def detect_and_parse(content, channels, source_url=None):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        parse_tvbox_json(text, channels, source_url)
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels, source_url)
    else:
        parse_txt_like(text, channels, source_url)

# ============================
# 并发测速 + 上游源统计
# ============================

def detect_and_sort_urls(name, urls, is_entertainment=False):
    urls = list(set(urls))
    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 开始检测，共 {total} 条源\n", flush=True)

    results = {}
    THREADS = 4

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        future_map = {}

        for u in good_urls:

            if is_local_source(u):
                results[u] = 100.0
                print(f"[{name}] 本地源 → 默认 100 分 | {u}", flush=True)

                src = URL_SOURCE.get(u)
                if src:
                    SOURCE_TOTAL[src] += 1
                continue

            future_map[exe.submit(quality_score, u)] = u

        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score, cached = future.result()

            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            bitrate = info.get("bitrate", 0)
            delay = info.get("delay", 0)
            blur = info.get("blur", 0)
            err = info.get("error", "")

            print(
                f"[{name}] {idx}/{total} "
                f"{'缓存' if cached else '检测'} → "
                f"{w}x{h} | {bitrate/1_000_000:.2f}Mbps | 延迟 {delay}s | 清晰度 {blur:.1f} | 得分 {score:.1f}",
                flush=True
            )

            src = URL_SOURCE.get(url)
            if src:
                SOURCE_TOTAL[src] += 1
                if score <= 0:
                    SOURCE_FAIL[src] += 1

            if err in ("timeout", "connection refused", "network unreachable", "dns error"):
                continue

            if score <= 0:
                continue

            results[url] = score

    if is_entertainment:
        filtered = {}
        for url, score in results.items():
            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            if w >= 1280 and h >= 720:
                filtered[url] = score
        results = filtered

    print(f"[{name}] 检测完成，可用 {len(results)} / {total}\n", flush=True)

    return sorted(results.keys(), key=lambda u: results[u], reverse=True)

# ============================
# TXT 输出
# ============================

def channel_sort_key(name: str):
    m = re.match(r"(CCTV|CETV)(\d+)", name)
    if m:
        return (m.group(1), int(m.group(2)))
    return ("ZZZ", name)

def build_output_txt(channels, mode):
    lines = []

    if mode in ("all", "cctv", "satellite"):
        lines.append("电视频道,#genre#")
        for name in sorted(channels.keys(), key=channel_sort_key):

            if name not in WHITELIST:
                continue

            if mode == "cctv" and not name.startswith("CCTV"):
                continue

            if mode == "satellite" and name.startswith("CCTV"):
                continue

            raw_urls = channels[name]

            # 分离本地源和远程源
            local_urls = [u for u in raw_urls if is_local_source(u)]
            remote_urls = [u for u in raw_urls if not is_local_source(u)]

            # 远程源按质量排序
            sorted_remote = detect_and_sort_urls(name, remote_urls)

            urls = sorted_remote + local_urls

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    if mode in ("all", "entertainment"):
        lines.append("媒体频道,#genre#")
        for name in sorted(channels.keys()):

            if name in WHITELIST:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 3:
                continue

            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    return "\n".join(lines)

# ============================
# M3U 输出
# ============================

LOGO_BASES = [
    "https://www.xn--rgv465a.top/tvlogo/",
    "https://live.fanmingming.cn/tv/",
    "https://gitee.com/cquptxiong/live/raw/main/tv/"
]

def build_output_m3u(channels, mode):
    lines = []
    lines.append("#EXTM3U")

    def get_logo(name):
        if name.startswith("CCTV"):
            num = name.replace("CCTV", "")
            filename_main = f"CCTV-{num}.png"
            filename_alt  = f"CCTV{num}.png"
        else:
            filename_main = f"{name}.png"
            filename_alt  = f"{name}.png"

        filename_main = urllib.parse.quote(filename_main)
        filename_alt  = urllib.parse.quote(filename_alt)

        for base in LOGO_BASES:
            url = base + filename_main
            try:
                if requests.head(url, timeout=1.5).status_code == 200:
                    return url
            except:
                pass

        for base in LOGO_BASES:
            url = base + filename_alt
            try:
                if requests.head(url, timeout=1.5).status_code == 200:
                    return url
            except:
                pass

        return None

    def get_group(name):
        if name.startswith("CCTV") or "卫视" in name:
            return "📺 电视频道"
        return "🎬 媒体频道"

    if mode in ("all", "cctv", "satellite"):
        for name in sorted(channels.keys(), key=channel_sort_key):

            if name not in WHITELIST:
                continue

            if mode == "cctv" and not name.startswith("CCTV"):
                continue

            if mode == "satellite" and name.startswith("CCTV"):
                continue

            raw_urls = channels[name]

            # 分离本地源和远程源
            local_urls = [u for u in raw_urls if is_local_source(u)]
            remote_urls = [u for u in raw_urls if not is_local_source(u)]

            # 远程源按质量排序
            sorted_remote = detect_and_sort_urls(name, remote_urls)

            # 本地源追加在后面
            urls = sorted_remote + local_urls

            tvg_id = name
            logo = get_logo(name)
            group = get_group(name)

            # urls 已经按 score 排序（detect_and_sort_urls 返回的）
            for idx, url in enumerate(urls, start=1):
                norm_url = normalize_url(url)
                info = cache.get(norm_url, {})

                score = info.get("score", 0)
                w = info.get("width", 0)
                h = info.get("height", 0)
                bitrate = info.get("bitrate", 0)
                delay = info.get("delay", 0)
                blur = info.get("blur", 0)

                res = f"{w}x{h}" if w and h else "N/A"

                # 自动标注最佳源
                best_flag = "yes" if idx == 1 else "no"

                # 自动标注排名
                rank = idx

                # 本地标识
                local_flag = "yes" if is_local_source(url) else "no"

                lines.append(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
                    f'tvg-logo="{logo}" group-title="{group}" '
                    f'score="{score:.1f}" resolution="{res}" '
                    f'bitrate="{bitrate}" delay="{delay}" blur="{blur:.2f}" '
                    f'best="{best_flag}" rank="{rank}" local="{local_flag}",{name}'
                )
                lines.append(url)

    if mode in ("all", "entertainment"):
        for name in sorted(channels.keys()):

            if name in WHITELIST:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 4:
                continue

            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)

            tvg_id = name
            logo = get_logo(name)
            group = get_group(name)

            for url in urls:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
                    f'tvg-logo="{logo}" group-title="{group}",{name}'
                )
                lines.append(url)

    return "\n".join(lines)

# ============================
# 上游源失败统计
# ============================

def update_upstream_fail(live_sources):
    global UPSTREAM_FAIL, UPSTREAM_BLOCKLIST

    today = time.strftime("%Y-%m-%d")
    updated = []

    for src, label in live_sources:
        total = SOURCE_TOTAL.get(src, 0)
        failed = SOURCE_FAIL.get(src, 0)

        if total == 0:
            failed = 1
            total = 1

        if failed == total:
            UPSTREAM_FAIL[src] += 1
            print(f"[source] {src} 全部失败（连续 {UPSTREAM_FAIL[src]} 次）")

            if UPSTREAM_FAIL[src] >= 10:
                remove_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                UPSTREAM_BLOCKLIST[src] = {
                    "fail_time": today,
                    "remove_time": remove_date
                }
                print(f"[source] {src} 连续 10 次失败 → 已永久删除")
                continue

            updated.append((src, label))
        else:
            UPSTREAM_FAIL[src] = 0
            updated.append((src, label))

    with LIVE_URLS_FILE.open("w", encoding="utf-8") as f:
        for src, label in updated:
            if label:
                f.write(f"{src}${label}\n")
            else:
                f.write(f"{src}\n")

    save_json(UPSTREAM_BLOCKLIST_FILE, UPSTREAM_BLOCKLIST)
    save_json(UPSTREAM_FAIL_FILE, UPSTREAM_FAIL)

# ============================
# 主流程
# ============================

def load_live_urls():
    items = []
    with LIVE_URLS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "$" in line:
                url, name = line.split("$", 1)
            else:
                url, name = line, ""
            items.append((url.strip(), name.strip()))
    return items

def load_channel_whitelist():
    whitelist = set()
    if CHANNEL_LIST_FILE.exists():
        with CHANNEL_LIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    whitelist.add(normalize_name(name))
    return whitelist

def load_blacklist():
    bl = []
    if BLACKLIST_FILE.exists():
        with BLACKLIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:
                    bl.append(key)
    return bl

def fetch_text(url, timeout=8, retries=3):
    print(f"[fetch] {url}")
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return r.text
        except:
            if attempt == retries:
                print(f"  >>> Skip {url}")
                return ""

def main(mode):
    # 强制创建 output 目录，避免 GitHub Actions 报错
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    global WHITELIST, BLACKLIST
    WHITELIST = load_channel_whitelist()
    BLACKLIST = load_blacklist()

    # 加载上游源
    live_sources = load_live_urls()
    channels = defaultdict(list)

    # 解析所有上游源
    for src, label in live_sources:
        print(f"[fetch] {src}")
        content = fetch_text(src)
        detect_and_parse(content, channels, source_url=src)

    # 解析本地 spider 源
    local_file = SOURCES_DIR / "local_spider.m3u"
    if local_file.exists():
        print("[local spider] 加载本地 spider 源")
        content = local_file.read_text(encoding="utf-8")
        detect_and_parse(content, channels, source_url="local_spider")

    # ============================
    # 输出 TXT / M3U（永远生成文件）
    # ============================
    txt = build_output_txt(channels, mode)
    m3u = build_output_m3u(channels, mode)

    txt_path = OUTPUT_DIR / f"channels_{mode}.txt"
    m3u_path = OUTPUT_DIR / f"channels_{mode}.m3u"

    # 即使为空也写入，避免 mv output/* 报错
    txt_path.write_text(txt or "", encoding="utf-8")
    m3u_path.write_text(m3u or "", encoding="utf-8")

    # 保存测速缓存
    save_all(mode)

    # 保存黑名单过滤日志
    filtered_file = OUTPUT_DIR / f"filtered_{mode}.json"
    filtered_file.write_text(
        json.dumps(FILTERED_LOG, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # ============================
    # 上游源失败统计（核心逻辑）
    # ============================
    update_upstream_fail(live_sources)

    print("\n[done] 构建完成\n")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "cctv"
    main(mode)
