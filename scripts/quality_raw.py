import math
import time
import threading
import subprocess
import json
import tempfile
from pathlib import Path
from PIL import Image
import numpy as np
import cv2

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "sources/state"
CACHE_FILE = STATE_DIR / "cache.json"
STREAM_FAIL_FILE = STATE_DIR / "stream_fail.json"

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
# 全局缓存 + 原始观测
# ============================

cache_lock = threading.Lock()
cache = load_json(CACHE_FILE)
RAW_RESULTS = {}
EXPIRE_SECONDS = 24 * 3600

# 直播源失败计数
stream_fail = load_json(STREAM_FAIL_FILE)

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
# ffmpeg：截图 + 清晰度
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
# 静态画面检测
# ============================

def is_black_or_solid_color(arr, threshold=5):
    return np.std(arr) < threshold

def detect_logo(img, region_ratio=0.2):
    h, w = img.shape
    rh = int(h * region_ratio)
    rw = int(w * region_ratio)
    roi = img[0:rh, w-rw:w]

    mean_val = np.mean(roi)
    std_val = np.std(roi)
    edges = cv2.Canny(roi, 80, 150)
    edge_ratio = np.sum(edges > 0) / edges.size

    return mean_val > 80 and std_val > 20 and edge_ratio > 0.02

def is_static_stream(url, timeout=5, checks=3, interval=1):
    static_count = 0
    for _ in range(checks):
        if _check_static_once(url, timeout):
            static_count += 1
        time.sleep(interval)
    return static_count == checks

def _check_static_once(url, timeout=5):
    try:
        tmp1 = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        tmp2 = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name

        run_silent(["ffmpeg", "-v", "quiet", "-y", "-i", url, "-vframes", "1", tmp1], timeout=timeout)
        time.sleep(1)
        run_silent(["ffmpeg", "-v", "quiet", "-y", "-i", url, "-vframes", "1", tmp2], timeout=timeout)

        img1 = np.array(Image.open(tmp1).convert("L"))
        img2 = np.array(Image.open(tmp2).convert("L"))

        if is_black_or_solid_color(img1) and is_black_or_solid_color(img2):
            return True

        if detect_logo(img1) or detect_logo(img2):
            return False

        diff = cv2.absdiff(img1, img2)
        change_ratio = np.sum(diff > 10) / diff.size
        return change_ratio < 0.005
    except:
        return False

# ============================
# raw_score → 0~100
# ============================

def map_to_0_100(raw_score):
    if raw_score <= -100:
        return 0.0
    x = raw_score / 25.0
    y = math.tanh(x)
    return (y + 1) * 50

# ============================
# 质量检测（核心）
# ============================

def quality_score(url):
    now = time.time()

    # 缓存命中
    with cache_lock:
        if url in cache:
            ts = cache[url].get("ts", 0)
            if now - ts < EXPIRE_SECONDS:
                return cache[url]["score"], True

    ok, w, h, bitrate = probe_stream(url)
    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (not ok) or (w == 0) or (h == 0)

    if not failed:
        try:
            if is_static_stream(url):
                failed = True
        except:
            pass

    if failed:
        raw_score = -100
    else:
        resolution_score = (w * h) / 50000
        blur_score = min(blur / 20, 20)
        delay_penalty = min(delay, 5) * 15
        raw_score = resolution_score + blur_score - delay_penalty

    final_score = map_to_0_100(raw_score)

    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "bitrate": bitrate,
            "delay": delay,
            "blur": blur,
            "raw_score": raw_score,
            "score": final_score,
            "ts": now
        }

    RAW_RESULTS[url] = {
        "ok": not failed,
        "raw_score": raw_score,
        "score": final_score,
        "width": w,
        "height": h,
        "bitrate": bitrate,
        "delay": delay,
        "blur": blur
    }

    # 直播源失败计数
    if final_score > 0:
        stream_fail[url] = 0
    else:
        stream_fail[url] = stream_fail.get(url, 0) + 1

    return final_score, False

# ============================
# 保存
# ============================

def cleanup_cache():
    now = time.time()
    new_cache = {}

    for url, info in cache.items():
        ts = info.get("ts", 0)
        score = info.get("score", 0)

        if score <= 0:
            if now - ts < 3600:
                new_cache[url] = info
            continue

        if now - ts < EXPIRE_SECONDS:
            new_cache[url] = info

    return new_cache

def save_all(job_name=None):
    global cache

    cache = cleanup_cache()
    save_json(CACHE_FILE, cache)

    if job_name:
        raw_file = STATE_DIR / f"raw_results_{job_name}.json"
        save_json(raw_file, RAW_RESULTS)

    save_json(STREAM_FAIL_FILE, stream_fail)
