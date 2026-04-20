#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import json
import tempfile
import threading
from pathlib import Path
from PIL import Image
import numpy as np
import cv2

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
CACHE_FILE = Path(__file__).parent / "cache.json"

# ============================
# 全局缓存 + 原始观测
# ============================

cache_lock = threading.Lock()
cache = {}
RAW_RESULTS = {}

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

# 加载缓存
cache = load_json(CACHE_FILE)

# ============================
# 静默运行子进程
# ============================

def run_silent(cmd, timeout=5):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout
    )

# ============================
# ffprobe：分辨率 + 码率
# ============================

def probe_stream(url, timeout=5):
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate",
            url
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return True, stream.get("width", 0), stream.get("height", 0), int(stream.get("bit_rate", 0))
    except:
        return False, 0, 0, 0

# ============================
# ffmpeg：首帧延迟
# ============================

def measure_first_frame_delay(url, timeout=5):
    try:
        cmd = ["ffmpeg", "-v", "quiet", "-i", url, "-vframes", "1", "-f", "null", "-"]
        run_silent(cmd, timeout=timeout)
        return 1.0
    except:
        return 999

# ============================
# ffmpeg：截图 + 清晰度（Laplacian）
# ============================

def snapshot_blur_score(url, timeout=5):
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        cmd = ["ffmpeg", "-v", "quiet", "-y", "-i", url, "-vframes", "1", tmp]
        run_silent(cmd, timeout=timeout)

        img = Image.open(tmp).convert("L")
        arr = np.array(img)
        return cv2.Laplacian(arr, cv2.CV_64F).var()
    except:
        return 0

# ============================
# 质量检测（核心）
# ============================

def quality_score(url):
    # 1. 缓存命中
    with cache_lock:
        if url in cache:
            return cache[url]["score"], True

    # 2. ffprobe / ffmpeg 检测
    ok, w, h, bitrate = probe_stream(url)
    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (not ok) or (w == 0) or (h == 0)

    # 3. 评分（与原版一致）
    if failed:
        score = 0
    else:
        score = (w * h) / 1000 + bitrate / 10000 + blur - delay * 10

    # 4. 写入缓存
    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "bitrate": bitrate,
            "delay": delay,
            "blur": blur,
            "score": score
        }

    # 5. 上报原始观测（CI 合并时使用）
    RAW_RESULTS[url] = {
        "ok": not failed,
        "score": score,
        "width": w,
        "height": h,
        "bitrate": bitrate,
        "delay": delay,
        "blur": blur
    }

    return score, False

# ============================
# 保存（cache + raw_results）
# ============================

def save_all(job_name=None):
    # 保存缓存
    save_json(CACHE_FILE, cache)

    # 保存原始观测（CI 合并时使用）
    if job_name:
        raw_file = SOURCES_DIR / f"raw_results_{job_name}.json"
        save_json(raw_file, RAW_RESULTS)
