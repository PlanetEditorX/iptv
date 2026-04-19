import subprocess
import json
import tempfile
import threading
from pathlib import Path
from PIL import Image
import numpy as np
import cv2

CACHE_FILE = Path(__file__).parent / "cache.json"
FAIL_FILE = Path(__file__).parent / "fail_count.json"

cache_lock = threading.Lock()
fail_lock = threading.Lock()

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
fail_count = load_json(FAIL_FILE)


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
            return cache[url]["score"]

    # 检测
    ok, w, h, bitrate = probe_stream(url)
    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (not ok) or (w == 0) or (h == 0)

    # 更新失败计数
    with fail_lock:
        if failed:
            fail_count[url] = fail_count.get(url, 0) + 1
        else:
            fail_count[url] = 0

    # 评分
    if failed:
        score = -999999
    else:
        score = (w * h) / 1000 + bitrate / 10000 + blur - delay * 10

    # 写入缓存
    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "bitrate": bitrate,
            "delay": delay,
            "blur": blur,
            "score": score
        }

    return score


def save_all():
    save_json(CACHE_FILE, cache)
    save_json(FAIL_FILE, fail_count)
