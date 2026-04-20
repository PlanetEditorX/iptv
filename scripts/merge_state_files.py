#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
SCRIPTS_DIR = ROOT / "scripts"

RAW_FILES = [
    SOURCES_DIR / "raw_results_cctv.json",
    SOURCES_DIR / "raw_results_satellite.json",
    SOURCES_DIR / "raw_results_entertainment.json",
]

FAIL_COUNT_FILE = SCRIPTS_DIR / "fail_count.json"
SOURCE_FAIL_FILE = SOURCES_DIR / "source_fail.json"
FAILED_SOURCES_FILE = SOURCES_DIR / "failed_sources.json"
LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
README_FILE = ROOT / "README.md"

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def merge_raw():
    all_raw = {}

    for f in RAW_FILES:
        if not f.exists():
            continue
        data = load_json(f)
        for url, info in data.items():
            all_raw.setdefault(url, []).append(info)

    return all_raw

def recompute_fail(all_raw):
    fail_count = {}
    source_fail = {}
    failed_sources = {}

    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst).strftime("%Y-%m-%d")

    for url, obs_list in all_raw.items():
        # 只要有一次成功，就不算失败
        if any(o["ok"] for o in obs_list):
            fail_count[url] = 0
        else:
            fail_count[url] = fail_count.get(url, 0) + 1

        # 连续失败 10 次 → 记录
        if fail_count[url] >= 10:
            failed_sources[url] = {
                "fail_time": today,
                "remove_time": (datetime.now(cst) + timedelta(days=30)).strftime("%Y-%m-%d")
            }

    return fail_count, source_fail, failed_sources

def rebuild_live_urls(failed_sources):
    lines = []
    with LIVE_URLS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            url = line.strip().split("$")[0]
            if url not in failed_sources:
                lines.append(line.strip())

    LIVE_URLS_FILE.write_text("\n".join(lines), encoding="utf-8")

def build_readme(fail_count, failed_sources):
    html = []
    html.append("# IPTV 质量报表\n")

    cst = timezone(timedelta(hours=8))
    build_time = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
    html.append(f"⏱ **构建时间：{build_time} (CST)**\n\n")

    if failed_sources:
        html.append("## ❌ 失效上游源\n")
        for url, info in failed_sources.items():
            html.append(f"- `{url}` 失效时间：{info['fail_time']} 删除时间：{info['remove_time']}")
        html.append("\n")

    README_FILE.write_text("\n".join(html), encoding="utf-8")

def main():
    all_raw = merge_raw()
    fail_count, source_fail, failed_sources = recompute_fail(all_raw)

    save_json(FAIL_COUNT_FILE, fail_count)
    save_json(SOURCE_FAIL_FILE, source_fail)
    save_json(FAILED_SOURCES_FILE, failed_sources)

    rebuild_live_urls(failed_sources)
    build_readme(fail_count, failed_sources)

if __name__ == "__main__":
    main()
