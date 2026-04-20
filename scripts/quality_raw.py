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

CACHE_FILE = Path(__file__).parent / "cache.json"

cache_lock = threading.Lock()
cache = {}

RAW_RESULTS = {}

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

cache = load_json(CACHE_FILE)


def run_silent(cmd, timeout=5):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout
    )


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


def measure_first_frame_delay(url, timeout=5):
    try:
        cmd = ["ffmpeg", "-v", "quiet", "-i", url, "-vframes", "1", "-f", "null", "-"]
        run_silent(cmd, timeout=timeout)
        return 1.0
    except:
        return 999


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


def quality_score(url):
    # 缓存命中
    with cache_lock:
        if url in cache:
            return cache[url]["score"], True

    ok, w, h, bitrate = probe_stream(url)
    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (not ok) or (w == 0) or (h == 0)

    if failed:
        score = 0
    else:
        score = (w * h) / 1000 + bitrate / 10000 + blur - delay * 10

    # 写 cache
    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "bitrate": bitrate,
            "delay": delay,
            "blur": blur,
            "score": score
        }

    # 上报原始观测
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


def save_all(job_name=None):
    save_json(CACHE_FILE, cache)

    if job_name:
        raw_file = Path(__file__).parent.parent / "sources" / f"raw_results_{job_name}.json"
        save_json(raw_file, RAW_RESULTS)
