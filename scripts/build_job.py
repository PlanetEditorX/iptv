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
STATE_DIR = SOURCES_DIR / "state"     # 统一状态目录
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

# ============================
# 全局白名单 / 黑名单
# ============================

WHITELIST = set()
BLACKLIST = []

# URL → 上游源映射
URL_SOURCE = {}

# ============================
# 黑名单调试记录
# ============================

FILTERED_LOG = defaultdict(list)

# ============================
# 名称规范化
# ============================

def normalize_name(name: str) -> str:
    raw = name.strip().upper()

    # ============================
    # 1. CCTV 系列强力归一化
    # ============================
    m = re.match(r"CCTV[- ]?0?(\d+)", raw)
    if m:
        num = m.group(1)
        return f"CCTV{num}"

    # ============================
    # 2. 卫视归一化
    # ============================

    # 通用卫视
    m2 = re.match(r"(.*?卫视)", raw)
    if m2:
        return m2.group(1)

    # ============================
    # 3. 去掉画质/后缀（HD/高清/超清/频道/综合）
    # ============================
    cleaned = re.sub(
        r"(4K|8K|HD|FHD|UHD|超清|高清|标清|频道|综合)$",
        "",
        raw
    )

    # 去掉尾部数字+单位（如 4M1080、8M、4M）
    cleaned = re.sub(r"\d+[MPKp]+$", "", cleaned)

    # 去掉非中文英文数字
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", cleaned)

    return cleaned

# ============================
# URL 过滤
# ============================

def is_good_url(u: str) -> bool:
    u = u.strip()
    if not u.startswith("http"):
        return False
    if u.endswith("$"):
        return False
    bad_keywords = ["udp/", "rtp/", "://239.", "://224."]
    if any(k in u for k in bad_keywords):
        return False
    return True

def is_numeric_channel(name: str) -> bool:
    n = name.strip()
    n = re.sub(r"[台频道]+$", "", n)
    return n.isdigit()

# ============================
# URL 归一化
# ============================

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
# 添加频道源（增强黑名单调试）
# ============================

def add_channel(channels, name, url, source_url=None):
    name = normalize_name(name)
    url = normalize_url(url.strip())

    if not name or not url:
        return

    # 黑名单只对非白名单频道生效
    if name not in WHITELIST:
        for key in BLACKLIST:
            if key in name:
                FILTERED_LOG[name].append({
                    "url": url,
                    "keyword": key,
                    "source": source_url
                })
                return

    if is_numeric_channel(name):
        return

    if not is_good_url(url):
        return

    # 1. local_spider 永远优先
    if source_url == "local_spider":
        URL_SOURCE[url] = "local_spider"

    # 2. 如果 URL 从未出现过，记录来源
    elif url not in URL_SOURCE:
        URL_SOURCE[url] = source_url

    # 3. 添加到频道列表
    if url not in channels[name]:
        channels[name].append(url)

# ============================
# 解析 TXT / M3U / JSON
# ============================

def parse_txt_like(content, channels, source_url=None):
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
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
# 自动识别本地源
# ============================

def is_local_source(url: str) -> bool:
    u = url.lower()
    return (
        u.startswith("rtp://")
        or u.startswith("udp://")
        or "://239." in u
        or "://224." in u
        or "/rtp/" in u
    )

# ============================
# 并发检测 + 排序
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

            # 本地源：不测速，默认 100 分
            if is_local_source(u):
                results[u] = 100.0
                print(f"[{name}] 本地源 → 默认 100 分 | {u}", flush=True)
                continue

            # 远程源：正常测速
            future_map[exe.submit(quality_score, u)] = u

        # 处理远程源测速结果
        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score, cached = future.result()

            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            bitrate = info.get("bitrate", 0)
            delay = info.get("delay", 0)
            blur = info.get("blur", 0)

            if bitrate and bitrate > 0:
                mbps_text = f"{bitrate / 1_000_000:.2f}Mbps"
            else:
                mbps_text = "N/A"

            print(
                f"[{name}] {idx}/{total} "
                f"{'缓存' if cached else '检测'} → "
                f"{w}x{h} | {mbps_text} | 延迟 {delay}s | 清晰度 {blur:.1f} | 得分 {score:.1f}",
                flush=True
            )

            results[url] = score

    # 媒体频道过滤
    if is_entertainment:
        filtered = {}
        for url, score in results.items():
            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            if w >= 1280 and h >= 720 and score > 0:
                filtered[url] = score
        results = filtered

    print(f"[{name}] 检测完成，可用 {sum(1 for s in results.values() if s > 0)} / {total}\n", flush=True)

    # 排序：本地源 100 分永远排前面
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

            urls = detect_and_sort_urls(name, channels[name])

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    if mode in ("all", "entertainment"):
        lines.append("媒体频道,#genre#")
        for name in sorted(channels.keys()):

            if name in WHITELIST:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 4:
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
        # 主源文件名（带横杠）
        if name.startswith("CCTV"):
            num = name.replace("CCTV", "")
            filename_main = f"CCTV-{num}.png"   # 主源格式
            filename_alt  = f"CCTV{num}.png"    # fanmingming + Gitee 格式
        else:
            filename_main = f"{name}.png"
            filename_alt  = f"{name}.png"

        # URL encode（处理中文）
        filename_main = urllib.parse.quote(filename_main)
        filename_alt  = urllib.parse.quote(filename_alt)

        # 1. 主源
        url1 = LOGO_BASES[0] + filename_main
        try:
            if requests.head(url1, timeout=1.5).status_code == 200:
                return url1
        except:
            pass

        # 2. fanmingming
        url2 = LOGO_BASES[1] + filename_alt
        try:
            if requests.head(url2, timeout=1.5).status_code == 200:
                return url2
        except:
            pass

        # 3. Gitee
        url3 = LOGO_BASES[2] + filename_alt
        try:
            if requests.head(url3, timeout=1.5).status_code == 200:
                return url3
        except:
            pass

        # 全部失败
        return None

    def get_group(name):
        if name.startswith("CCTV") or "卫视" in name:
            return "📺 电视频道"
        return "🎬 媒体频道"

    # ============================
    # CCTV + 卫视
    # ============================
    if mode in ("all", "cctv", "satellite"):
        for name in sorted(channels.keys(), key=channel_sort_key):

            if name not in WHITELIST:
                continue

            if mode == "cctv" and not name.startswith("CCTV"):
                continue

            if mode == "satellite" and name.startswith("CCTV"):
                continue

            urls = detect_and_sort_urls(name, channels[name])

            tvg_id = name
            logo = get_logo(name)
            group = get_group(name)

            for url in urls:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
                    f'tvg-logo="{logo}" group-title="{group}",{name}'
                )
                lines.append(url)

    # ============================
    # 媒体频道
    # ============================
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
    OUTPUT_DIR.mkdir(exist_ok=True)

    global WHITELIST, BLACKLIST
    WHITELIST = load_channel_whitelist()
    BLACKLIST = load_blacklist()

    live_sources = load_live_urls()
    channels = defaultdict(list)

    # 解析所有上游源
    for src, label in live_sources:
        content = fetch_text(src)
        detect_and_parse(content, channels, source_url=src)

    # 解析本地 spider 源
    local_file = SOURCES_DIR / "local_spider.m3u"
    if local_file.exists():
        print("[local spider] 加载本地 spider 源")
        content = local_file.read_text(encoding="utf-8")
        detect_and_parse(content, channels, source_url="local_spider")

    # 输出
    txt = build_output_txt(channels, mode)
    m3u = build_output_m3u(channels, mode)

    (OUTPUT_DIR / f"channels_{mode}.txt").write_text(txt, encoding="utf-8")
    (OUTPUT_DIR / f"channels_{mode}.m3u").write_text(m3u, encoding="utf-8")

    # 保存 raw_results
    save_all(mode)

    # 保存黑名单过滤日志
    filtered_file = OUTPUT_DIR / f"filtered_{mode}.json"
    filtered_file.write_text(json.dumps(FILTERED_LOG, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    mode = sys.argv[1]
    main(mode)
