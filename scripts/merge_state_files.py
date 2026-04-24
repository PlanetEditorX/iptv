#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import re

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
STATE_DIR = SOURCES_DIR / "state"     # 统一状态目录
OUTPUT_DIR = ROOT / "output"

RAW_FILES = [
    STATE_DIR / "raw_results_cctv.json",
    STATE_DIR / "raw_results_satellite.json",
    STATE_DIR / "raw_results_entertainment.json",
]

STREAM_FAIL_FILE = STATE_DIR / "stream_fail.json"
UPSTREAM_BLOCKLIST_FILE = STATE_DIR / "upstream_blocklist.json"
LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
README_FILE = ROOT / "README.md"

# ============================
# JSON 工具
# ============================

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ============================
# 合并 raw_results
# ============================

def merge_raw():
    all_raw = {}
    for f in RAW_FILES:
        if not f.exists():
            continue
        data = load_json(f)
        for url, info in data.items():
            all_raw[url] = info
    return all_raw

# ============================
# 解析 channels_all.txt → 构建频道 → URL 列表
# ============================

def load_channels():
    txt_file = OUTPUT_DIR / "channels_all.txt"
    channels = {}

    if not txt_file.exists():
        return channels

    for line in txt_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        name, url = line.split(",", 1)
        channels.setdefault(name, []).append(url)

    return channels

# ============================
# 频道排序（自然排序）
# ============================

def channel_sort_key(name: str):
    m = re.match(r"(CCTV|CETV)(\d+)", name)
    if m:
        return (m.group(1), int(m.group(2)))
    return ("ZZZ", name)

# ============================
# 频道类型划分
# ============================

def get_channel_type(name: str) -> str:
    # 央视
    if name.startswith("CCTV"):
        return "tv"
    # 卫视（湖南卫视、东方卫视、北京卫视…）
    if name.endswith("卫视"):
        return "tv"
    # 其它全部归为媒体频道
    return "entertainment"

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
# 构建频道质量报表（核心）
# ============================

def build_channel_report(channels, raw):
    report = {}

    for name, urls in channels.items():
        if name == '电视频道':
            continue
        total = 0
        usable = 0
        best_score = -1
        best_res = "N/A"

        for url in urls:

            # README 不统计本地源
            if is_local_source(url):
                continue

            info = raw.get(url)
            if not info:
                continue

            total += 1
            score = info["score"]

            if score > 0:
                usable += 1

            if score > best_score:
                best_score = score
                w = info["width"]
                h = info["height"]
                best_res = f"{w}x{h}" if w and h else "N/A"

        removed = usable == 0

        report[name] = {
            "total": total,
            "usable": usable,
            "removed": removed,
            "best_res": best_res,
            "best_score": round(best_score, 1) if best_score >= 0 else 0,
            "type": get_channel_type(name),
        }

    return report

# ============================
# 判刑逻辑（stream_fail / upstream_blocklist）
# ============================

def recompute_fail(raw):
    # 失败直播源
    stream_fail = load_json(STREAM_FAIL_FILE)
    # 上游源黑名单
    upstream_blocklist = load_json(UPSTREAM_BLOCKLIST_FILE)

    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst).strftime("%Y-%m-%d")

    for url, info in raw.items():
        # 当测试通过失败次数归零
        if info["score"] > 0:
            stream_fail[url] = 0
        else:
            # 测试失败后次数+1
            stream_fail[url] = stream_fail.get(url, 0) + 1


    return stream_fail, upstream_blocklist

# ============================
# live_urls 清理
# ============================

def rebuild_live_urls(upstream_blocklist):
    if not LIVE_URLS_FILE.exists():
        return

    new_lines = []
    for line in LIVE_URLS_FILE.read_text(encoding="utf-8").splitlines():
        url = line.split("$")[0]
        if url not in upstream_blocklist:
            new_lines.append(line)

    LIVE_URLS_FILE.write_text("\n".join(new_lines), encoding="utf-8")

# ============================
# README（频道级报表）
# ============================

def build_readme(report, upstream_blocklist):
    html = []
    html.append("# IPTV 质量报表\n")

    cst = timezone(timedelta(hours=8))
    build_time = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
    html.append(f"⏱ **构建时间：{build_time} (CST)**\n\n")

    # 总览统计
    total_channels = len(report)
    removed_channels = sum(1 for x in report.values() if x["removed"])
    kept_channels = total_channels - removed_channels
    total_usable = sum(x["usable"] for x in report.values())

    html.append("## 📊 总览统计\n")
    html.append(f"- **总频道数：** {total_channels}")
    html.append(f"- **保留频道数：** {kept_channels}")
    html.append(f"- **已删除频道数：** {removed_channels}")
    html.append(f"- **总可用源数：** {total_usable}\n\n")

    # ============================
    # 电视频道（CCTV + 卫视，自然排序）
    # ============================

    html.append("## 📺 电视频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

    tv_items = [(name, info) for name, info in report.items() if info["type"] == "tv"]

    for name, info in sorted(tv_items, key=lambda x: (x[1]["removed"], channel_sort_key(x[0]))):
        status = '<span style="color:red">已删除</span>' if info["removed"] else '<span style="color:green">保留</span>'
        html.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{info['usable']} / {info['total']}</td>"
            f"<td>{info['best_res']}</td>"
            f"<td>{info['best_score']}</td>"
            f"<td>{status}</td>"
            f"</tr>"
        )

    # 失效上游源
    if upstream_blocklist:
        html.append("## ❌ 失效上游源（连续 10 次失败）\n")
        for url, info in upstream_blocklist.items():
            html.append(f"- `{url}`")
            html.append(f"  - 失效时间：{info['fail_time']}")
            html.append(f"  - 删除时间：{info['remove_time']}\n")
        html.append("\n")

    html.append("</table>\n")

    # ============================
    # 媒体频道
    # ============================

    html.append("## 📡 媒体频道\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源/总源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

    ent_items = [(name, info) for name, info in report.items() if info["type"] == "entertainment"]

    for name, info in sorted(ent_items, key=lambda x: (x[1]["removed"], x[0])):
        status = '<span style="color:red">已删除</span>' if info["removed"] else '<span style="color:green">保留</span>'
        html.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{info['usable']} / {info['total']}</td>"
            f"<td>{info['best_res']}</td>"
            f"<td>{info['best_score']}</td>"
            f"<td>{status}</td>"
            f"</tr>"
        )

    html.append("</table>\n")

    README_FILE.write_text("\n".join(html), encoding="utf-8")

# ============================
# 主流程
# ============================

def main():
    print("=== 合并 raw_results ===")
    raw = merge_raw()

    print("=== 加载频道 ===")
    channels = load_channels()

    print("=== 构建频道报表 ===")
    report = build_channel_report(channels, raw)

    print("=== 判刑 ===")
    stream_fail, upstream_blocklist = recompute_fail(raw)

    save_json(STREAM_FAIL_FILE, stream_fail)
    save_json(UPSTREAM_BLOCKLIST_FILE, upstream_blocklist)

    print("=== 清理 live_urls ===")
    rebuild_live_urls(upstream_blocklist)

    print("=== 生成 README ===")
    build_readme(report, upstream_blocklist)

    print("=== merge_state_files 完成 ===")

if __name__ == "__main__":
    main()
