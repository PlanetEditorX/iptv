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
STATE_DIR = ROOT / "sources/state"

CACHE_FILE = STATE_DIR / "cache.json"
STREAM_FAIL_FILE  = STATE_DIR / "stream_fail.json"

# ============================
# 全局缓存 + 失败计数
# ============================

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
# 质量检测（核心）
# ============================

def quality_score(url):
    # 缓存命中
    with cache_lock:
        if url in cache:
            return cache[url]["score"], True

    # 检测
    ok, w, h, bitrate = probe_stream(url)
    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (not ok) or (w == 0) or (h == 0)

    # 更新失败计数
    with fail_lock:
        if failed:
            stream_fail[url] = stream_fail.get(url, 0) + 1
        else:
            stream_fail[url] = 0

    # 评分
    if failed:
        score = 0
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

    return score, False

# ============================
# 保存（cache + stream_fail）
# ============================

def save_all():
    save_json(CACHE_FILE, cache)
    save_json(STREAM_FAIL_FILE, stream_fail)
