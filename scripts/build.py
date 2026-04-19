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
    cache,
    fail_count,
    save_all
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

# ============================
# 图标映射（完全匹配 xn--rgv465a.top）
# ============================

LOGO_ID_MAP = {
    # CCTV 系列（必须带横杠）
    "CCTV1": "CCTV-1",
    "CCTV2": "CCTV-2",
    "CCTV3": "CCTV-3",
    "CCTV4": "CCTV-4",
    "CCTV5": "CCTV-5",
    "CCTV5+": "CCTV-5+",
    "CCTV6": "CCTV-6",
    "CCTV7": "CCTV-7",
    "CCTV8": "CCTV-8",
    "CCTV9": "CCTV-9",
    "CCTV10": "CCTV-10",
    "CCTV11": "CCTV-11",
    "CCTV12": "CCTV-12",
    "CCTV13": "CCTV-13",
    "CCTV14": "CCTV-14",
    "CCTV15": "CCTV-15",
    "CCTV16": "CCTV-16",
    "CCTV17": "CCTV-17",

    # 卫视（中文名直接作为文件名）
    "湖南卫视": "湖南卫视",
    "浙江卫视": "浙江卫视",
    "东方卫视": "东方卫视",
    "江苏卫视": "江苏卫视",
    "北京卫视": "北京卫视",
    "广东卫视": "广东卫视",
    "深圳卫视": "深圳卫视",
    "湖北卫视": "湖北卫视",
    "黑龙江卫视": "黑龙江卫视",
    "安徽卫视": "安徽卫视",
    "重庆卫视": "重庆卫视",
    "东南卫视": "东南卫视",
    "甘肃卫视": "甘肃卫视",
    "广西卫视": "广西卫视",
    "贵州卫视": "贵州卫视",
    "海南卫视": "海南卫视",
    "河北卫视": "河北卫视",
    "河南卫视": "河南卫视",
    "吉林卫视": "吉林卫视",
}

LOGO_BASE = "https://www.xn--rgv465a.top/tvlogo/"

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
# 下载上游内容（带重试）
# ============================
def fetch_text(url, timeout=8, retries=3):
    print(f"[fetch] {url}")

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()

            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding

            return r.text

        except Exception as e:
            print(f"  Error: {e}  (attempt {attempt}/{retries})")

            if attempt < retries:
                continue
            else:
                print(f"  >>> Skip {url}\n")
                return ""


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

    name = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9+]+", "", name)
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
# 并发 + 缓存 + 失败计数 + 自动剔除坏源
# ============================
def detect_and_sort_urls(name, urls):
    # 自动剔除失败 >= 10 次的源
    urls = [u for u in urls if fail_count.get(u, 0) < 10]

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

            # 从缓存读取详细信息
            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            bitrate = info.get("bitrate", 0)
            delay = info.get("delay", 0)
            blur = info.get("blur", 0)

            cached = (url in cache)

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

        raw_urls = channels[name]

        # 先过滤，不够 8 条直接跳过，不检测
        if len(raw_urls) < 8:
            continue

        if is_blacklisted(name, raw_urls, blacklist):
            continue

        if is_numeric_channel(name):
            continue

        # 需要检测的才检测
        urls = detect_and_sort_urls(name, raw_urls)

        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

    return "\n".join(lines)


# ============================
# M3U 输出（含图标）
# ============================
def build_output_m3u(channels, whitelist, blacklist):
    lines = []
    lines.append('#EXTM3U x-tvg-url="http://gh.qninq.cn/https://raw.githubusercontent.com/PlanetEditorX/iptv-api/refs/heads/master/output/epg/epg.gz"')

    # 电视频道
    for name in sorted(channels.keys(), key=channel_sort_key):
        if name not in whitelist:
            continue

        urls = detect_and_sort_urls(name, channels[name])
        logo = get_logo(name)

        for url in urls:
            if logo:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{LOGO_ID_MAP.get(name, name)}" '
                    f'tvg-name="{LOGO_ID_MAP.get(name, name)}" '
                    f'tvg-logo="{logo}" group-title="📺央视频道",{LOGO_ID_MAP.get(name, name)}'
                )
            else:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{name}" group-title="📺央视频道",{name}'
                )
            lines.append(url)

    # 卫视频道 / 娱乐频道
    for name in sorted(channels.keys()):
        if name in whitelist:
            continue

        raw_urls = channels[name]

        # 先过滤，不够 8 条直接跳过，不检测
        if len(raw_urls) < 8:
            continue

        if is_blacklisted(name, raw_urls, blacklist):
            continue

        if is_numeric_channel(name):
            continue

        # 需要检测的才检测
        urls = detect_and_sort_urls(name, raw_urls)

        logo = get_logo(name)

        for url in urls:
            if logo:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" '
                    f'tvg-logo="{logo}" group-title="📡卫视频道",{name}'
                )
            else:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{name}" group-title="📡卫视频道",{name}'
                )
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

    # 解析上游源
    for url, label in live_sources:
        try:
            content = fetch_text(url)
            detect_and_parse(content, channels)
        except Exception as e:
            print(f"[error] {url} -> {e}")

    # TXT 输出
    out_txt = build_output_txt(channels, whitelist, blacklist)
    (OUTPUT_DIR / "ku9_live.txt").write_text(out_txt, encoding="utf-8")

    # M3U 输出
    out_m3u = build_output_m3u(channels, whitelist, blacklist)
    (OUTPUT_DIR / "ku9_live.m3u").write_text(out_m3u, encoding="utf-8")

    print("[done] wrote ku9_live.txt + ku9_live.m3u")

    # 保存缓存 + 失败计数
    save_all()


if __name__ == "__main__":
    main()
