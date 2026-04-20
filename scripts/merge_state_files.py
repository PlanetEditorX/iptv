#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ============================
# 全局路径
# ============================

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
            all_raw.setdefault(url, []).append(info)

    return all_raw

# ============================
# 统一判刑逻辑
# ============================

def recompute_fail(all_raw):
    """
    规则：
    - 只要某 URL 在任意 job 中 ok=True → fail_count=0
    - 否则 fail_count += 1
    - 连续失败 >=10 → 进入 FAILED_SOURCES
    """
    old_fail = load_json(FAIL_COUNT_FILE)
    fail_count = dict(old_fail)

    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst).strftime("%Y-%m-%d")

    failed_sources = load_json(FAILED_SOURCES_FILE)
    source_fail = load_json(SOURCE_FAIL_FILE)

    for url, obs_list in all_raw.items():

        # 只要有一次成功 → 清零
        if any(o["ok"] for o in obs_list):
            fail_count[url] = 0
            continue

        # 全部失败 → 累加
        fail_count[url] = fail_count.get(url, 0) + 1

        # 连续失败 10 次 → 记录
        if fail_count[url] >= 10:
            if url not in failed_sources:
                remove_date = (datetime.now(cst) + timedelta(days=30)).strftime("%Y-%m-%d")
                failed_sources[url] = {
                    "fail_time": today,
                    "remove_time": remove_date
                }

    return fail_count, source_fail, failed_sources

# ============================
# live_urls.txt 清理
# ============================

def rebuild_live_urls(failed_sources):
    if not LIVE_URLS_FILE.exists():
        return

    new_lines = []
    with LIVE_URLS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            url = line.strip().split("$")[0]
            if url not in failed_sources:
                new_lines.append(line.strip())

    LIVE_URLS_FILE.write_text("\n".join(new_lines), encoding="utf-8")

# ============================
# README 生成
# ============================

def build_readme(fail_count, failed_sources):
    html = []
    html.append("# IPTV 质量报表（CI 合并版）\n")

    cst = timezone(timedelta(hours=8))
    build_time = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
    html.append(f"⏱ **构建时间：{build_time} (CST)**\n\n")

    # 失效上游源
    if failed_sources:
        html.append("## ❌ 失效上游源（连续 10 次失败）\n")
        for url, info in failed_sources.items():
            html.append(f"- `{url}`")
            html.append(f"  - 失效时间：{info['fail_time']}")
            html.append(f"  - 删除时间：{info['remove_time']}\n")
        html.append("\n")

    # 统计
    total_urls = len(fail_count)
    failed_10 = sum(1 for v in fail_count.values() if v >= 10)

    html.append("## 📊 统计\n")
    html.append(f"- **总 URL 数：** {total_urls}")
    html.append(f"- **连续失败 ≥10 的 URL：** {failed_10}\n")

    README_FILE.write_text("\n".join(html), encoding="utf-8")

# ============================
# 主流程
# ============================

def main():
    print("=== 合并 raw_results ===")
    all_raw = merge_raw()

    print("=== 统一判刑 ===")
    fail_count, source_fail, failed_sources = recompute_fail(all_raw)

    print("=== 写入 fail_count / source_fail / failed_sources ===")
    save_json(FAIL_COUNT_FILE, fail_count)
    save_json(SOURCE_FAIL_FILE, source_fail)
    save_json(FAILED_SOURCES_FILE, failed_sources)

    print("=== 清理 live_urls.txt ===")
    rebuild_live_urls(failed_sources)

    print("=== 生成 README.md ===")
    build_readme(fail_count, failed_sources)

    print("=== merge_state_files.py 完成 ===")

if __name__ == "__main__":
    main()
