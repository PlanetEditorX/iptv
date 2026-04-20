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

from quality_raw import (
    quality_score,
    cache,
    save_all
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

WHITELIST = set()
BLACKLIST = []

URL_SOURCE = {}
CHANNEL_REPORT = {}

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
                return ""

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

def add_channel(channels, name, url, source_url=None):
    name = normalize_name(name)
    url = url.strip()

    if not name or not url:
        return

    if name not in WHITELIST:
        for key in BLACKLIST:
            if key in name or key in url:
                return

    if not is_good_url(url):
        return

    if url not in channels[name]:
        channels[name].append(url)
        if source_url:
            URL_SOURCE[url] = source_url

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

def detect_and_parse(content, channels, source_url=None):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        try:
            data = json.loads(text)
            lives = data.get("lives") or []
            for live in lives:
                for ch in live.get("channels", []):
                    name = ch.get("name")
                    urls = ch.get("urls") or []
                    for url in urls:
                        add_channel(channels, name, url, source_url)
        except:
            pass
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels, source_url)
    else:
        parse_txt_like(text, channels, source_url)

def detect_and_sort_urls(name, urls):
    urls = list(set(urls))
    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 检测 {total} 条源\n")

    results = {}
    THREADS = 4

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        future_map = {exe.submit(quality_score, u): u for u in good_urls}

        for idx, future in enumerate(as_completed(future_map), start=1):
            time.sleep(random.uniform(0.1, 0.5))
            url = future_map[future]
            score, cached = future.result()
            results[url] = score

    return sorted(results.keys(), key=lambda u: results[u], reverse=True)

def build_output_txt(channels, mode):
    lines = []
    lines.append("电视频道,#genre#")

    for name in sorted(channels.keys()):
        urls = detect_and_sort_urls(name, channels[name])
        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

    return "\n".join(lines)

def build_output_m3u(channels, mode):
    lines = []
    lines.append("#EXTM3U")

    for name in sorted(channels.keys()):
        urls = detect_and_sort_urls(name, channels[name])
        for url in urls:
            lines.append(f"#EXTINF:-1,{name}")
            lines.append(url)

    return "\n".join(lines)

def main(mode):
    OUTPUT_DIR.mkdir(exist_ok=True)

    global WHITELIST, BLACKLIST
    WHITELIST = load_channel_whitelist()
    BLACKLIST = load_blacklist()

    live_sources = load_live_urls()
    channels = defaultdict(list)

    for src, label in live_sources:
        content = fetch_text(src)
        detect_and_parse(content, channels, source_url=src)

    txt = build_output_txt(channels, mode)
    m3u = build_output_m3u(channels, mode)

    (OUTPUT_DIR / f"channels_{mode}.txt").write_text(txt, encoding="utf-8")
    (OUTPUT_DIR / f"channels_{mode}.m3u").write_text(m3u, encoding="utf-8")

    save_all(mode)

if __name__ == "__main__":
    mode = sys.argv[1]
    main(mode)
