#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import json
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from quality import (
    probe_stream,
    measure_first_frame_delay,
    snapshot_blur_score,
    quality_score,
    save_cache,
    cache
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

# ============================
# 图标映射
# ============================
LOGO_ID_MAP = {
    "CCTV1": "cctv1",
    "CCTV2": "cctv2",
    "CCTV3": "cctv3",
    "CCTV4": "cctv4",
    "CCTV5": "cctv5",
    "CCTV6": "cctv6",
    "CCTV7": "cctv7",
    "CCTV8": "cctv8",
    "CCTV9": "cctv9",
    "CCTV10": "cctv10",
    "CCTV11": "cctv11",
    "CCTV12": "cctv12",
    "CCTV13": "cctv13",
    "CCTV14": "cctv14",
    "CCTV15": "cctv15",
    "CCTV16": "cctv16",
    "CCTV17": "cctv17",

    "湖南卫视": "hunantv",
    "浙江卫视": "zhejiangtv",
    "东方卫视": "dongfangtv",
    "江苏卫视": "jiangsutv",
    "北京卫视": "bjtv",
    "广东卫视": "gdws",
    "深圳卫视": "sztv",
}

LOGO_BASE = "https://raw.githubusercontent.com/fanmingming/live/main/tv/"

def get_logo(name: str):
    key = LOGO_ID_MAP.get(name)
    if not key:
        return None
    return f"{LOGO_BASE}{key}.png"


# ============================
# 读取上游 LIVE_URLS
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


# ============================
# 读取白名单
# ============================
def load_channel_whitelist():
    whitelist = set()
    if CHANNEL_LIST_FILE.exists():
        with CHANNEL_LIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    whitelist.add(normalize_name(name))
    return whitelist


# ============================
# 读取黑名单
# ============================
def load_blacklist():
    bl = []
    if BLACKLIST_FILE.exists():
        with BLACKLIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:
                    bl.append(key)
    return bl


# ============================
# 下载上游内容
# ============================
def fetch_text(url, timeout=8):
    print(f"[fetch] {url}")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


# ============================
# 频道名规范化
# ============================
def normalize_name(name: str) -> str:
    name = name.strip()

    m = re.match(r"CCTV[- ]?0?(\d+)", name.upper())
    if m:
        return f"CCTV{m.group(1)}"

    m = re.match(r"CETV[- ]?0?(\d+)", name.upper())
    if m:
        return f"CETV{m.group(1)}"

    name = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", name)
    return name


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


# ============================
# 黑名单
# ============================
def is_blacklisted(name: str, urls: list, blacklist: list) -> bool:
    for key in blacklist:
        if key in name:
            return True
        for u in urls:
            if key in u:
                return True
    return False


# ============================
# 数字频道过滤
# ============================
def is_numeric_channel(name: str) -> bool:
    n = name.strip()
    n = re.sub(r"[台频道]+$", "", n)
    return n.isdigit()


# ============================
# 添加频道源
# ============================
def add_channel(channels, name, url):
    name = normalize_name(name)
    url = url.strip()
    if not name or not url:
        return
    if url not in channels[name]:
        channels[name].append(url)


# ============================
# 解析 txt
# ============================
def parse_txt_like(content, channels):
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
        add_channel(channels, name, url)


# ============================
# 解析 m3u
# ============================
def parse_m3u(content, channels):
    last_name = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            if "," in line:
                last_name = line.split(",", 1)[1].strip()
        elif line and not line.startswith("#") and last_name:
            add_channel(channels, last_name, line)
            last_name = None


# ============================
# 解析 TVBox JSON
# ============================
def parse_tvbox_json(content, channels):
    try:
        data = json.loads(content)
    except Exception:
        return
    lives = data.get("lives") or []
    for live in lives:
        for ch in live.get("channels", []):
            name = ch.get("name")
            urls = ch.get("urls") or []
            for url in urls:
                add_channel(channels, name, url)


# ============================
# 自动识别格式
# ============================
def detect_and_parse(content, channels):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        parse_tvbox_json(text, channels)
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels)
    else:
        parse_txt_like(text, channels)


# ============================
# 自然排序
# ============================
def channel_sort_key(name: str):
    m = re.match(r"(CCTV|CETV)(\d+)$", name.upper())
    if m:
        prefix = m.group(1)
        num = int(m.group(2))
        order_prefix = {"CCTV": 0, "CETV": 1}.get(prefix, 2)
        return (order_prefix, num, "")
    return (3, 0, name)


# ============================
# 并发 + 缓存 + 详细进度 B
# ============================
def detect_and_sort_urls(name, urls):
    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 开始检测，共 {total} 条源\n")

    results = {}
    THREADS = 6

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        future_map = {exe.submit(quality_score, u): u for u in good_urls}

        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score = future.result()

            if url in cache:
                w = cache[url]["width"]
                h = cache[url]["height"]
                bitrate = cache[url]["bitrate"]
                delay = cache[url]["delay"]
                blur = cache[url]["blur"]
                cached = True
            else:
                w = h = bitrate = delay = blur = 0
                cached = False

            print(
                f"[{name}] {idx}/{total}  "
                f"{'缓存' if cached else '检测'}  "
                f"{w}x{h}  {bitrate}kbps  延迟{delay}s  清晰度{blur:.1f}  总分{score:.1f}",
                flush=True
            )

            results[url] = score

    print(f">>> {name} 排序完成\n")

    return sorted(results.keys(), key=lambda u: results[u], reverse=True)


# ============================
# TXT 输出
# ============================
def build_output_txt(channels, whitelist, blacklist):
    lines = []

    lines.append("电视频道,#genre#")
    for name in sorted(channels.keys(), key=channel_sort_key):
        if name not in whitelist:
            continue

        urls = detect_and_sort_urls(name, channels[name])

        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

    lines.append("娱乐频道,#genre#")
    for name in sorted(channels.keys()):
        if name in whitelist:
            continue

        urls = detect_and_sort_urls(name, channels[name])

        if is_blacklisted(name, urls, blacklist):
            continue

        if is_numeric_channel(name):
            continue

        if len(urls) < 8:
            continue

        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

    return "\n".join(lines)


# ============================
# M3U 输出
# ============================
def build_output_m3u(channels, whitelist, blacklist):
    lines = []
    lines.append("#EXTM3U")

    # 电视频道
    for name in sorted(channels.keys(), key=channel_sort_key):
        if name not in whitelist:
            continue

        urls = detect_and_sort_urls(name, channels[name])
        logo = get_logo(name)

        for url in urls:
            if logo:
                lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-logo="{logo}" group-title="电视频道",{name}')
            else:
                lines.append(f'#EXTINF:-1 tvg-id="{name}" group-title="电视频道",{name}')
            lines.append(url)

    # 娱乐频道
    for name in sorted(channels.keys()):
        if name in whitelist:
            continue

        urls = detect_and_sort_urls(name, channels[name])

        if is_blacklisted(name, urls, blacklist):
            continue

        if is_numeric_channel(name):
            continue

        if len(urls) < 8:
            continue

        logo = get_logo(name)

        for url in urls:
            if logo:
                lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-logo="{logo}" group-title="娱乐频道",{name}')
            else:
                lines.append(f'#EXTINF:-1 tvg-id="{name}" group-title="娱乐频道",{name}')
            lines.append(url)

    return "\n".join(lines)


# ============================
# 主流程
# ============================
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    channels = defaultdict(list)
    whitelist = load_channel_whitelist()
    blacklist = load_blacklist()
    live_sources = load_live_urls()

    for url, label in live_sources:
        try:
            content = fetch_text(url)
            detect_and_parse(content, channels)
        except Exception as e:
            print(f"[error] {url} -> {e}")

    # TXT
    out_txt = build_output_txt(channels, whitelist, blacklist)
    (OUTPUT_DIR / "ku9_live.txt").write_text(out_txt, encoding="utf-8")

    # M3U
    out_m3u = build_output_m3u(channels, whitelist, blacklist)
    (OUTPUT_DIR / "ku9_live.m3u").write_text(out_m3u, encoding="utf-8")

    print("[done] wrote ku9_live.txt + ku9_live.m3u")


if __name__ == "__main__":
    main()
    save_cache(cache)