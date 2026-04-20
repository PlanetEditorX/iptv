#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import json
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

from quality import (
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

# 全局频道质量报表
CHANNEL_REPORT = {}

# ============================
# 图标 + EPG ID 映射
# ============================

LOGO_ID_MAP = {
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
# EPG ID 生成
# ============================

def get_epg_id(name: str):
    if name in LOGO_ID_MAP:
        return LOGO_ID_MAP[name]
    return name

def get_epg_meta(name: str, index: int):
    epg_id = get_epg_id(name)
    return {
        "id": epg_id,
        "name": epg_id,
        "chno": index,
        "lang": "zh",
        "country": "CN"
    }

# ============================
# 频道排序（自然排序）
# ============================

def channel_sort_key(name: str):
    m = re.match(r"(CCTV|CETV)(\d+)", name)
    if m:
        return (m.group(1), int(m.group(2)))
    return ("ZZZ", name)

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
# 白名单 / 黑名单
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
            if attempt == retries:
                print(f"  >>> Skip {url}\n")
                return ""

# ============================
# 名称规范化
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

def is_blacklisted(name: str, urls: list, blacklist: list) -> bool:
    for key in blacklist:
        if key in name:
            return True
        for u in urls:
            if key in u:
                return True
    return False

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

    # 解析 URL
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))

    # 需要删除的无意义参数
    drop_keys = {
        "token", "auth", "ts", "sign", "expires", "expiry",
        "e", "_t", "_ts", "uuid", "session", "sessionid",
        "v", "ver", "random", "r", "t"
    }

    # 删除无意义参数
    query = {k: v for k, v in query.items() if k.lower() not in drop_keys}

    # 参数排序（避免顺序变化导致缓存失效）
    sorted_query = urlencode(sorted(query.items()))

    # 去掉尾部斜杠
    path = parsed.path.rstrip("/")

    path = re.sub(r"\.m3u8.*$", ".m3u8", path)
    path = re.sub(r"\.flv.*$", ".flv", path)

    # 重建 URL
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

def add_channel(channels, name, url, blacklist):
    name = normalize_name(name)
    url = normalize_url(url.strip())

    if not name or not url:
        return

    # 黑名单过滤（频道名或 URL 命中黑名单）
    for key in blacklist:
        if key in name or key in url:
            return

    # 数字频道过滤
    if is_numeric_channel(name):
        return

    # URL 基础过滤
    if not is_good_url(url):
        return

    # 添加源
    if url not in channels[name]:
        channels[name].append(url)

# ============================
# 解析 TXT / M3U / JSON
# ============================

def parse_txt_like(content, channels, blacklist):
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
        add_channel(channels, name, url, blacklist)

def parse_m3u(content, channels, blacklist):
    last_name = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            if "," in line:
                last_name = line.split(",", 1)[1].strip()
        elif line and not line.startswith("#") and last_name:
            add_channel(channels, last_name, line, blacklist)
            last_name = None

def parse_tvbox_json(content, channels, blacklist):
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
                add_channel(channels, name, url, blacklist)

def detect_and_parse(content, channels, blacklist):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        parse_tvbox_json(text, channels, blacklist)
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels, blacklist)
    else:
        parse_txt_like(text, channels, blacklist)

# ============================
# 并发检测 + 排序
# ============================

def detect_and_sort_urls(name, urls, is_entertainment=False):
    # 过滤失败次数过多的
    urls = [u for u in urls if fail_count.get(u, 0) < 10]

    # 基础过滤
    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 开始检测，共 {total} 条源\n")

    results = {}
    meta = {}   # 保存分辨率等信息
    THREADS = 6

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        future_map = {exe.submit(quality_score, u): u for u in good_urls}

        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score = future.result()

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
                f"{w}x{h}  延迟{delay}s  清晰度{blur:.1f}  总分{score:.1f}",
                flush=True
            )

            results[url] = score
            meta[url] = (w, h, bitrate, delay, blur, score)

            # 超时或失败源累积 fail_count
            if score <= 0:
                fail_count[url] = fail_count.get(url, 0) + 1

    # 娱乐频道专属过滤逻辑
    if is_entertainment:
        filtered = {}

        for url, (w, h, bitrate, delay, blur, score) in meta.items():
            # 分辨率低于 720p → 丢弃
            if w < 1280 or h < 720:
                continue

            # 超时源（score=0）丢弃
            if score <= 0:
                continue

            filtered[url] = score

        # 如果全部源都是低分辨率或超时 → 删除频道
        if not filtered:
            print(f">>> {name} 全部为低分辨率或超时，删除该频道\n")
            CHANNEL_REPORT[name] = {
                "total": total,
                "usable": 0,
                "removed": True,
                "best_res": "N/A",
                "best_score": 0,
                "type": "entertainment"
            }
            return []

        results = filtered

    # 统计可用源
    usable = sum(1 for s in results.values() if s > 0)

    # 选一个最佳源（分数最高）
    best_url = None
    best_score = -1
    best_res = "N/A"
    for url, score in results.items():
        if score > best_score:
            best_score = score
            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            best_res = f"{w}x{h}" if w and h else "N/A"
            best_url = url

    print(f">>> {name} 排序完成（可用 {usable} / 总 {total}）\n")

    # 写入全局报表
    CHANNEL_REPORT[name] = {
        "total": total,
        "usable": usable,
        "removed": False,
        "best_res": best_res,
        "best_score": round(best_score, 1) if best_score >= 0 else 0,
        "type": "entertainment" if is_entertainment else "tv"
    }

    return sorted(results.keys(), key=lambda u: results[u], reverse=True)

# ============================
# TXT 输出（支持 mode）
# ============================

def build_output_txt(channels, whitelist, blacklist, mode):
    lines = []

    # 电视频道
    if mode in ("all", "cctv", "satellite"):
        lines.append("电视频道,#genre#")
        for idx, name in enumerate(sorted(channels.keys(), key=channel_sort_key), start=1):
            if name not in whitelist:
                continue

            # CCTV 模式
            if mode == "cctv" and not name.startswith("CCTV"):
                continue

            # 卫视模式
            if mode == "satellite" and name.startswith("CCTV"):
                continue

            urls = detect_and_sort_urls(name, channels[name])

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    # 娱乐频道
    if mode in ("all", "entertainment"):
        lines.append("娱乐频道,#genre#")
        for idx, name in enumerate(sorted(channels.keys()), start=1):
            if name in whitelist:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 8:
                continue
            if is_blacklisted(name, raw_urls, blacklist):
                continue
            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    return "\n".join(lines)

# ============================
# M3U 输出（支持 mode）
# ============================

def build_output_m3u(channels, whitelist, blacklist, mode):
    lines = []
    lines.append('#EXTM3U x-tvg-url="http://gh.qninq.cn/https://raw.githubusercontent.com/PlanetEditorX/iptv-api/refs/heads/master/output/epg/epg.gz"')

    # 电视频道
    if mode in ("all", "cctv", "satellite"):
        for idx, name in enumerate(sorted(channels.keys(), key=channel_sort_key), start=1):
            if name not in whitelist:
                continue

            if mode == "cctv" and not name.startswith("CCTV"):
                continue

            if mode == "satellite" and name.startswith("CCTV"):
                continue

            urls = detect_and_sort_urls(name, channels[name])
            logo = get_logo(name)
            epg = get_epg_meta(name, idx)

            for url in urls:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{epg["id"]}" tvg-name="{epg["name"]}" '
                    f'tvg-chno="{epg["chno"]}" tvg-language="{epg["lang"]}" '
                    f'tvg-country="{epg["country"]}" '
                    f'tvg-logo="{logo}" group-title="📺电视频道",{epg["name"]}'
                )
                lines.append(url)

    # 娱乐频道
    if mode in ("all", "entertainment"):
        for idx, name in enumerate(sorted(channels.keys()), start=1):
            if name in whitelist:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 8:
                continue
            if is_blacklisted(name, raw_urls, blacklist):
                continue
            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)
            logo = get_logo(name)
            epg = get_epg_meta(name, idx)

            for url in urls:
                lines.append(
                    f'#EXTINF:-1 tvg-id="{epg["id"]}" tvg-name="{epg["name"]}" '
                    f'tvg-chno="{epg["chno"]}" tvg-language="{epg["lang"]}" '
                    f'tvg-country="{epg["country"]}" '
                    f'tvg-logo="{logo}" group-title="📡娱乐频道",{name}'
                )
                lines.append(url)

    return "\n".join(lines)

# ============================
# 自动生成 README.md（HTML 质量报表）
# ============================

def build_readme():
    readme_path = ROOT / "README.md"

    tv_rows = []
    ent_rows = []

    total_channels = len(CHANNEL_REPORT)
    removed_channels = sum(1 for x in CHANNEL_REPORT.values() if x["removed"])
    kept_channels = total_channels - removed_channels
    total_usable = sum(x["usable"] for x in CHANNEL_REPORT.values())

    for name, info in sorted(CHANNEL_REPORT.items(), key=lambda x: (x[1]["removed"], x[0])):
        # 高分频道加 ⭐（得分 >= 2000）
        star = " ⭐" if info["best_score"] >= 2000 and not info["removed"] else ""

        row = (
            f"<tr>"
            f"<td>{name}{star}</td>"
            f"<td>{info['usable']} / {info['total']}</td>"
            f"<td>{info['best_res']}</td>"
            f"<td>{info['best_score']}</td>"
            f"<td>{'<span style=\"color:red\">已删除</span>' if info['removed'] else '<span style=\"color:green\">保留</span>'}</td>"
            f"</tr>"
        )

        if info["type"] == "entertainment":
            ent_rows.append(row)
        else:
            tv_rows.append(row)

    html = []
    html.append("# IPTV 质量报表\n")
    html.append("> 本报表由构建脚本自动生成，展示最近一次检测结果。\n\n")

    # ======= 总统计 =======
    html.append("## 📊 总览统计\n")
    html.append(f"- **总频道数：** {total_channels}\n")
    html.append(f"- **保留频道数：** {kept_channels}\n")
    html.append(f"- **已删除频道数：** {removed_channels}\n")
    html.append(f"- **总可用源数：** {total_usable}\n\n")

    # ======= 电视频道 =======
    html.append("## 📺 电视频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")
    html.extend(tv_rows or ['<tr><td colspan="5">无数据</td></tr>'])
    html.append("</table>\n")

    # ======= 娱乐频道 =======
    html.append("## 📡 娱乐频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")
    html.extend(ent_rows or ['<tr><td colspan="5">无数据</td></tr>'])
    html.append("</table>\n")

    readme_path.write_text("\n".join(html), encoding="utf-8")
    print("[done] wrote README.md quality report")

# ============================
# 主流程
# ============================

def main():
    mode = "all"
    if len(sys.argv) >= 2:
        mode = sys.argv[1].lower()

    print(f"[mode] 当前构建模式：{mode}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    channels = defaultdict(list)
    whitelist = load_channel_whitelist()
    blacklist = load_blacklist()
    live_sources = load_live_urls()

    for url, label in live_sources:
        try:
            content = fetch_text(url)
            detect_and_parse(content, channels, blacklist)
        except Exception as e:
            print(f"[error] {url} -> {e}")

    out_txt = build_output_txt(channels, whitelist, blacklist, mode)
    (OUTPUT_DIR / f"channels_{mode}.txt").write_text(out_txt, encoding="utf-8")

    out_m3u = build_output_m3u(channels, whitelist, blacklist, mode)
    (OUTPUT_DIR / f"channels_{mode}.m3u").write_text(out_m3u, encoding="utf-8")

    print(f"[done] wrote channels_{mode}.txt + channels_{mode}.m3u")

    build_readme()

    save_all()

if __name__ == "__main__":
    main()
