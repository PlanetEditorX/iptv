#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"


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
# 读取频道白名单
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

    # CCTV 系列
    m = re.match(r"CCTV[- ]?0?(\d+)", name.upper())
    if m:
        return f"CCTV{m.group(1)}"

    # CETV 系列
    m = re.match(r"CETV[- ]?0?(\d+)", name.upper())
    if m:
        return f"CETV{m.group(1)}"

    # 去掉乱码
    name = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", name)

    return name


# ============================
# URL 过滤规则
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
# 黑名单匹配（仅娱乐频道）
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
# 纯数字频道过滤
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
# 解析 txt 格式
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
# 解析 m3u 格式
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
        parse_m3u(text)
    else:
        parse_txt_like(text, channels)


# ============================
# 自然排序（CCTV1 < CCTV2 < CCTV10）
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
# 输出酷9可用 txt（含娱乐频道 + 黑名单 + 数字过滤）
# ============================
def build_output_txt(channels, whitelist, blacklist):
    lines = []

    # 电视频道
    lines.append("电视频道,#genre#")
    for name in sorted(channels.keys(), key=channel_sort_key):
        if name not in whitelist:
            continue
        urls = [u for u in channels[name] if is_good_url(u)]
        if not urls:
            continue
        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

    # 娱乐频道
    lines.append("娱乐频道,#genre#")
    for name in sorted(channels.keys()):
        if name in whitelist:
            continue

        urls = [u for u in channels[name] if is_good_url(u)]

        # 黑名单过滤
        if is_blacklisted(name, urls, blacklist):
            continue

        # 纯数字频道过滤
        if is_numeric_channel(name):
            continue

        # 源数量必须 ≥ 2
        if len(urls) < 2:
            continue

        for url in urls:
            lines.append(f"{name},{url}")
        lines.append("")

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

    out_txt = build_output_txt(channels, whitelist, blacklist)
    out_file = OUTPUT_DIR / "ku9_live.txt"
    out_file.write_text(out_txt, encoding="utf-8")

    print(f"[done] wrote {out_file}")


if __name__ == "__main__":
    main()
