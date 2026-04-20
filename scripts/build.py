#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import json
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
from datetime import datetime, timezone, timedelta

from quality import (
    quality_score,
    cache,
    fail_count,
    save_all
)

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

# ============================
# 全局白名单 / 黑名单（不再传参）
# ============================

WHITELIST = set()
BLACKLIST = []

# ============================
# 上游源失效记录（写入 README）
# ============================

FAILED_SOURCES = {}  # {url: {"fail_time": "...", "remove_time": "..."}}
FAILED_SOURCES_FILE = SOURCES_DIR / "failed_sources.json"

def load_failed_sources():
    if FAILED_SOURCES_FILE.exists():
        try:
            return json.loads(FAILED_SOURCES_FILE.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_failed_sources(data):
    FAILED_SOURCES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

FAILED_SOURCES = load_failed_sources()

# URL → 上游源映射
URL_SOURCE = {}

# 上游源本轮是否有可用源
SOURCE_OK = {}

# 上游源连续失败计数
SOURCE_FAIL_FILE = SOURCES_DIR / "source_fail.json"

def load_source_fail():
    if SOURCE_FAIL_FILE.exists():
        try:
            return json.loads(SOURCE_FAIL_FILE.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_source_fail(data):
    SOURCE_FAIL_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

SOURCE_FAIL = load_source_fail()

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
# 白名单 / 黑名单加载（全局）
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
# 添加频道源（使用全局 WHITELIST / BLACKLIST）
# ============================

def add_channel(channels, name, url, source_url=None):
    name = normalize_name(name)
    url = normalize_url(url.strip())

    if not name or not url:
        return

    # 黑名单只对非白名单频道生效
    if name not in WHITELIST:
        for key in BLACKLIST:
            if key in name or key in url:
                return

    if is_numeric_channel(name):
        return

    if not is_good_url(url):
        return

    if url not in channels[name]:
        channels[name].append(url)
        if source_url:
            URL_SOURCE[url] = source_url

# ============================
# 解析 TXT / M3U / JSON（已移除 blacklist 参数）
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

# ============================
# 自动识别格式
# ============================

def detect_and_parse(content, channels, source_url=None):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        parse_tvbox_json(text, channels, source_url)
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels, source_url)
    else:
        parse_txt_like(text, channels, source_url)
# ============================
# 并发检测 + 排序（URL 永久封禁）
# ============================

def detect_and_sort_urls(name, urls, is_entertainment=False):
    # 永久封禁：fail_count >= 10 的 URL 不再使用
    urls = [u for u in list(set(urls)) if fail_count.get(u, 0) < 10]

    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 开始检测，共 {total} 条源\n")

    results = {}
    meta = {}
    THREADS = 6

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        # 这里 future.result() 会返回 (score, from_cache)
        future_map = {exe.submit(quality_score, u): u for u in good_urls}

        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score, cached_before = future.result()   # ⭐ 正确接收两个值

            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            bitrate = info.get("bitrate", 0)
            mbps = bitrate / 1_000_000
            delay = info.get("delay", 0)
            blur = info.get("blur", 0)

            print(
                f"[{name}] {idx}/{total}  "
                f"{'缓存' if cached_before else '检测'}  "
                f"{w}x{h}  {mbps:.2f}Mbps  延迟{delay}s  清晰度{blur:.1f}  总分{score:.1f}",
                flush=True
            )

            results[url] = score
            meta[url] = (w, h, bitrate, delay, blur, score)

            # 永久封禁逻辑
            if score <= 0:
                fail_count[url] = fail_count.get(url, 0) + 1
            else:
                src = URL_SOURCE.get(url)
                if src:
                    SOURCE_OK[src] = True

    # 娱乐频道过滤
    if is_entertainment:
        filtered = {}
        for url, (w, h, bitrate, delay, blur, score) in meta.items():
            if w < 1280 or h < 720:
                continue
            if score <= 0:
                continue
            filtered[url] = score

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

    # 统计
    usable = sum(1 for s in results.values() if s > 0)

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

    print(f">>> {name} 排序完成（可用 {usable} / 总共 {total}）\n")

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
# TXT 输出
# ============================

def build_output_txt(channels, mode):
    lines = []

    if mode in ("all", "cctv", "satellite"):
        lines.append("电视频道,#genre#")
        for idx, name in enumerate(sorted(channels.keys(), key=channel_sort_key), start=1):
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
        lines.append("娱乐频道,#genre#")
        for idx, name in enumerate(sorted(channels.keys()), start=1):
            if name in WHITELIST:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 8:
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

def build_output_m3u(channels, mode):
    lines = []
    lines.append('#EXTM3U x-tvg-url="http://gh.qninq.cn/https://raw.githubusercontent.com/PlanetEditorX/iptv-api/refs/heads/master/output/epg/epg.gz"')

    if mode in ("all", "cctv", "satellite"):
        for idx, name in enumerate(sorted(channels.keys(), key=channel_sort_key), start=1):
            if name not in WHITELIST:
                continue

            if mode == "cctv" and not name.startswith("CCTV"):
                continue
            if mode == "satellite" and name.startswith("CCTV"):
                continue

            urls = detect_and_sort_urls(name, channels[name])

            logo = get_logo(name)
            epg_id = get_epg_id(name)

            for url in urls:
                lines.append(f'#EXTINF:-1 tvg-id="{epg_id}" tvg-logo="{logo}",{name}')
                lines.append(url)

    if mode in ("all", "entertainment"):
        for idx, name in enumerate(sorted(channels.keys()), start=1):
            if name in WHITELIST:
                continue

            raw_urls = channels[name]

            if len(raw_urls) < 8:
                continue
            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)

            for url in urls:
                lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-logo="",{name}')
                lines.append(url)

    return "\n".join(lines)

# ============================
# README 生成（含失效上游源）
# ============================

def build_readme():
    readme_path = ROOT / "README.md"

    html = []
    html.append("# IPTV 质量报表\n")

    # 构建时间（CST）
    cst = timezone(timedelta(hours=8))
    build_time = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
    html.append(f"⏱ **构建时间：{build_time} (CST)**\n\n")

    # ============================
    # 清理过期失效上游源（超过 30 天）
    # ============================
    global FAILED_SOURCES
    now = datetime.now(cst)

    cleaned = {}
    for url, info in FAILED_SOURCES.items():
        remove_time = datetime.strptime(info["remove_time"], "%Y-%m-%d")
        if now.date() <= remove_time.date():
            cleaned[url] = info

    FAILED_SOURCES = cleaned
    save_failed_sources(FAILED_SOURCES)

    # ============================
    # 输出失效上游源
    # ============================
    if FAILED_SOURCES:
        html.append("## ❌ 失效上游源（自动管理）\n")
        for url, info in FAILED_SOURCES.items():
            html.append(f"- **URL：** `{url}`")
            html.append(f"  - 失效时间：{info['fail_time']}")
            html.append(f"  - 彻底删除时间：{info['remove_time']}\n")
        html.append("\n")

    # ============================
    # 总览统计
    # ============================
    total_channels = len(CHANNEL_REPORT)
    removed_channels = sum(1 for x in CHANNEL_REPORT.values() if x["removed"])
    kept_channels = total_channels - removed_channels
    total_usable = sum(x["usable"] for x in CHANNEL_REPORT.values())

    html.append("## 📊 总览统计\n")
    html.append(f"- **总频道数：** {total_channels}")
    html.append(f"- **保留频道数：** {kept_channels}")
    html.append(f"- **已删除频道数：** {removed_channels}")
    html.append(f"- **总可用源数：** {total_usable}\n\n")

    # ============================
    # 电视频道表格
    # ============================

    html.append("## 📺 电视频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

    for name, info in sorted(CHANNEL_REPORT.items(), key=lambda x: (x[1]["removed"], x[0])):
        if info["type"] != "tv":
            continue

        star = " ⭐" if info["best_score"] >= 2000 and not info["removed"] else ""
        status = '<span style="color:red">已删除</span>' if info["removed"] else '<span style="color:green">保留</span>'

        html.append(
            f"<tr>"
            f"<td>{name}{star}</td>"
            f"<td>{info['usable']} / {info['total']}</td>"
            f"<td>{info['best_res']}</td>"
            f"<td>{info['best_score']}</td>"
            f"<td>{status}</td>"
            f"</tr>"
        )

    html.append("</table>\n")

    # ============================
    # 娱乐频道表格
    # ============================

    html.append("## 📡 娱乐频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

    for name, info in sorted(CHANNEL_REPORT.items(), key=lambda x: (x[1]["removed"], x[0])):
        if info["type"] != "entertainment":
            continue

        star = " ⭐" if info["best_score"] >= 2000 and not info["removed"] else ""
        status = '<span style="color:red">已删除</span>' if info["removed"] else '<span style="color:green">保留</span>'

        html.append(
            f"<tr>"
            f"<td>{name}{star}</td>"
            f"<td>{info['usable']} / {info['total']}</td>"
            f"<td>{info['best_res']}</td>"
            f"<td>{info['best_score']}</td>"
            f"<td>{status}</td>"
            f"</tr>"
        )

    html.append("</table>\n")

    readme_path.write_text("\n".join(html), encoding="utf-8")
    print("[done] wrote README.md quality report")

# ============================
# 主流程 main()
# ============================

def main(mode):
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 加载上游源
    live_sources = load_live_urls()

    # 加载白名单 / 黑名单（全局）
    global WHITELIST, BLACKLIST
    WHITELIST = load_channel_whitelist()
    BLACKLIST = load_blacklist()

    channels = defaultdict(list)

    # 初始化上游源状态
    for src, label in live_sources:
        SOURCE_OK[src] = False

    # 解析所有上游源（只解析，不做成功/失败判断）
    for src, label in live_sources:
        try:
            content = fetch_text(src)
            detect_and_parse(content, channels, source_url=src)
        except Exception as e:
            print(f"[error] {src} -> {e}")

    # ============================
    # 先输出 TXT / M3U（这里会触发 detect_and_sort_urls，
    # 在 detect_and_sort_urls 里会根据 score>0 设置 SOURCE_OK[src] = True）
    # ============================

    txt = build_output_txt(channels, mode)
    m3u = build_output_m3u(channels, mode)

    (OUTPUT_DIR / f"channels_{mode}.txt").write_text(txt, encoding="utf-8")
    (OUTPUT_DIR / f"channels_{mode}.m3u").write_text(m3u, encoding="utf-8")

    # ============================
    # 检测跑完之后，再做上游源连续失败统计
    # ============================

    updated_live_urls = []
    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst).strftime("%Y-%m-%d")

    for src, label in live_sources:
        ok = SOURCE_OK.get(src, False)

        if ok:
            SOURCE_FAIL[src] = 0

            if src in FAILED_SOURCES:
                print(f"[source] {src} 恢复正常 → 从失败列表移除")
                del FAILED_SOURCES[src]

            updated_live_urls.append((src, label))
            continue

        SOURCE_FAIL[src] = SOURCE_FAIL.get(src, 0) + 1
        fail_times = SOURCE_FAIL[src]

        print(f"[source] {src} 全部失败（连续 {fail_times} 次）")

        if fail_times < 10:
            updated_live_urls.append((src, label))
            continue

        if src not in FAILED_SOURCES:
            remove_date = (datetime.now(cst) + timedelta(days=30)).strftime("%Y-%m-%d")
            FAILED_SOURCES[src] = {
                "fail_time": today,
                "remove_time": remove_date
            }
            print(f"[source] {src} 连续 10 次失败 → 已永久删除（记录到 FAILED_SOURCES）")

        # 不写入 updated_live_urls → 从 live_urls.txt 删除

    # 写回 live_urls.txt
    with LIVE_URLS_FILE.open("w", encoding="utf-8") as f:
        for src, label in updated_live_urls:
            if label:
                f.write(f"{src}${label}\n")
            else:
                f.write(f"{src}\n")

    save_failed_sources(FAILED_SOURCES)
    save_source_fail(SOURCE_FAIL)

    # 保存质量缓存
    save_all()

    # 生成 README
    build_readme()

# ============================
# 入口
# ============================

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    main(mode)
