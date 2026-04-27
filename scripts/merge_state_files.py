import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
STATE_DIR = SOURCES_DIR / "state"
OUTPUT_DIR = ROOT / "output"

M3U_FILE = OUTPUT_DIR / "channels_all.m3u"
README_FILE = ROOT / "README.md"

STREAM_FAIL_FILE = STATE_DIR / "stream_fail.json"
UPSTREAM_BLOCKLIST_FILE = STATE_DIR / "upstream_blocklist.json"
LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"

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
# 判断本地源
# ============================

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
# 解析最终 M3U（带 score / resolution / rank / local）
# ============================

def parse_m3u():
    channels = {}
    last_name = None
    last_info = {}

    if not M3U_FILE.exists():
        print("❌ channels_all.m3u 不存在")
        return channels

    for line in M3U_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if line.startswith("#EXTINF"):
            # 频道名
            if "," in line:
                last_name = line.split(",", 1)[1].strip()

            # 提取字段
            m_score = re.search(r'score="([\d\.]+)"', line)
            m_res   = re.search(r'resolution="([^"]+)"', line)
            m_rank  = re.search(r'rank="(\d+)"', line)
            m_local = re.search(r'local="(yes|no)"', line)

            last_info = {
                "score": float(m_score.group(1)) if m_score else 0,
                "resolution": m_res.group(1) if m_res else "N/A",
                "rank": int(m_rank.group(1)) if m_rank else 0,
                "local": m_local.group(1) if m_local else "no",
            }

        elif line and not line.startswith("#") and last_name:
            channels.setdefault(last_name, []).append({
                "url": line,
                **last_info
            })
            last_name = None

    return channels

# ============================
# 频道类型
# ============================

def get_channel_type(name: str) -> str:
    if name.startswith("CCTV"):
        return "tv"
    if name.endswith("卫视"):
        return "tv"
    return "entertainment"

# ============================
# 构建频道报表（忽略本地源）
# ============================

def build_channel_report(channels):
    report = {}

    for name, items in channels.items():

        # 只统计远程源
        remote_items = [x for x in items if x["local"] == "no"]

        total = len(remote_items)
        usable = sum(1 for x in remote_items if x["score"] > 0)

        # 最佳远程源（rank=1）
        best = next((x for x in remote_items if x["rank"] == 1), None)

        best_res = best["resolution"] if best else "N/A"
        best_score = best["score"] if best else 0

        removed = usable == 0

        report[name] = {
            "total": total,
            "usable": usable,
            "removed": removed,
            "best_res": best_res,
            "best_score": best_score,
            "type": get_channel_type(name),
        }

    return report

# ============================
# stream_fail / upstream_blocklist
# ============================

def recompute_fail(channels):
    stream_fail = load_json(STREAM_FAIL_FILE)
    upstream_blocklist = load_json(UPSTREAM_BLOCKLIST_FILE)

    for name, items in channels.items():
        for x in items:
            url = x["url"]
            score = x["score"]

            # 本地源不参与失败统计
            if x["local"] == "yes":
                stream_fail[url] = 0
                continue

            if score > 0:
                stream_fail[url] = 0
            else:
                stream_fail[url] = stream_fail.get(url, 0) + 1

    return stream_fail, upstream_blocklist

# ============================
# 清理 live_urls（不删除本地源）
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
# 生成 README（基于远程源统计）
# ============================

def build_readme(report):
    html = []
    html.append("# IPTV 质量报表（仅统计远程源）\n")

    cst = timezone(timedelta(hours=8))
    build_time = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
    html.append(f"⏱ **构建时间：{build_time} (CST)**\n\n")

    total_channels = len(report)
    removed_channels = sum(1 for x in report.values() if x["removed"])
    kept_channels = total_channels - removed_channels
    total_usable = sum(x["usable"] for x in report.values())

    html.append("## 📊 总览统计\n")
    html.append(f"- **总频道数：** {total_channels}")
    html.append(f"- **保留频道数：** {kept_channels}")
    html.append(f"- **已过滤频道数：** {removed_channels}")
    html.append(f"- **总可用远程源数：** {total_usable}\n\n")

    # 电视频道
    html.append("## 📺 电视频道（远程源统计）\n\n<table>")
    html.append("<tr><th>频道</th><th>可用源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

    tv_items = [(name, info) for name, info in report.items() if info["type"] == "tv"]

    for name, info in sorted(tv_items):
        status = '<span style="color:red">过滤</span>' if info["removed"] else '<span style="color:green">保留</span>'
        html.append(
            f"<tr><td>{name}</td><td>{info['usable']}</td>"
            f"<td>{info['best_res']}</td><td>{info['best_score']}</td><td>{status}</td></tr>"
        )

    html.append("</table>\n")

    # 媒体频道
    ent_items = [(name, info) for name, info in report.items() if info["type"] == "entertainment"]

    if ent_items:  # 只有在存在媒体频道时才生成
        html.append("## 🎬 媒体频道（远程源统计）\n\n<table>")
        html.append("<tr><th>频道</th><th>可用源</th><th>最佳分辨率</th><th>最高得分</th><th>状态</th></tr>")

        for name, info in sorted(ent_items):
            status = '<span style="color:red">过滤</span>' if info["removed"] else '<span style="color:green">保留</span>'
            html.append(
                f"<tr><td>{name}</td><td>{info['total']}</td>"
                f"<td>{info['best_res']}</td><td>{info['best_score']}</td><td>{status}</td></tr>"
            )

        html.append("</table>\n")

    README_FILE.write_text("\n".join(html), encoding="utf-8")

# ============================
# 主流程
# ============================

def main():
    print("=== 解析最终 M3U ===")
    channels = parse_m3u()

    print("=== 构建频道报表（仅远程源） ===")
    report = build_channel_report(channels)

    print("=== 更新失败统计 ===")
    stream_fail, upstream_blocklist = recompute_fail(channels)
    save_json(STREAM_FAIL_FILE, stream_fail)
    save_json(UPSTREAM_BLOCKLIST_FILE, upstream_blocklist)

    print("=== 清理 live_urls ===")
    rebuild_live_urls(upstream_blocklist)

    print("=== 生成 README ===")
    build_readme(report)

    print("=== merge_state_files 完成 ===")

if __name__ == "__main__":
    main()
