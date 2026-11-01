#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六只脚（foooooot.com）个人轨迹批量导出。

用法:
    pip install playwright
    playwright install chromium
    python tools/liuzhijiao_export.py --diagnose     # 先诊断，留证
    python tools/liuzhijiao_export.py                # 再正式下载

数据位置（2026-11 实测）:
    我的行程  /home/trip/my_trip/
    我的收藏  /home/trip/fav_trip/
    两者也都挂在 /home/trip/ 的 tab 上：#my_trip-1 / #fav_trip-1
    轨迹下载  /trip/<id>/downloads/
    结构化备份 /trip/<id>/trackjson/

流程:
  1. 弹出浏览器窗口，你自己登录（脚本不接触密码，也不保存它）
  2. 脚本翻「我的行程」和「我的收藏」，收集行程编号
  3. 逐条下载 GPX；拿不到就试直连入口，再退回 trackjson
  4. 结果落在 data/liuzhijiao/，清单见 manifest.csv

每次运行都在 logs/ 下留 run-<时间>.log，崩溃 traceback 也在里面。
--diagnose 额外存页面 HTML 和截图。

脚本只做读取和下载：不点赞、不评论、不改任何东西。
登录态存在 .cache/liuzhijiao-profile/，下次运行不用重新登录。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

BASE = "http://www.foooooot.com"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "liuzhijiao"
DEFAULT_PROFILE = ROOT / ".cache" / "liuzhijiao-profile"
LOG_DIR = ROOT / "logs"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# (地址模板, 中文名, tab 锚点前缀)。{uid} 只有兜底地址才用得上。
MY_TARGETS = [
    ("/home/trip/my_trip/", "我的行程", "#my_trip"),
    ("/home/trip/", "我的行程", "#my_trip"),
    ("/userindex/{uid}/trip/", "我的行程", None),
]
FAV_TARGETS = [
    ("/home/trip/fav_trip/", "我的收藏", "#fav_trip"),
    ("/home/trip/", "我的收藏", "#fav_trip"),
    ("/userindex/{uid}/favorite/", "我的收藏", None),
]

# 轨迹文件入口。/trip/<id>/gps/file/?format=gpx 是实测可用的主入口，
# ?fileformat= 那组是下载页上的另外两个按钮（KML / 原始航迹）。
DIRECT_ENDPOINTS = [
    "/trip/{tid}/gps/file/?format=gpx",
    "/trip/{tid}/gps/file/?format=kml",
    "/trip/{tid}/download/?fileformat=gpx",
    "/trip/{tid}/download/?fileformat=kml",
    "/trip/{tid}/download/?fileformat=orig",
]

LOGFILE: Path | None = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOGFILE is not None:
        try:
            with LOGFILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def safe_name(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return (s or "untitled")[:maxlen]


def looks_like_html(body: bytes) -> bool:
    head = body[:400].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<head" in head[:200]


def scrape_ids(page) -> list[str]:
    """先看链接，链接里没有就退回整页 HTML 里的 /trip/<id>/。"""
    ids: list[str] = []
    seen: set[str] = set()

    def take(hrefs):
        for h in hrefs or []:
            m = re.search(r"/trip/(\d+)/", h or "")
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))

    try:
        take(page.eval_on_selector_all("a[href*='/trip/']", "els => els.map(e => e.getAttribute('href'))"))
    except Exception:
        pass
    if ids:
        return ids
    try:
        take(re.findall(r"/trip/(\d+)/", page.content()))
    except Exception:
        pass
    return ids


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------

def gpx_from_points(points, title: str) -> str:
    """points: [(lat, lon, ele|None, ts|None), ...] -> GPX 1.1 文本"""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="liuzhijiao_export" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{escape(title)}</name>",
        f"    <time>{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</time>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{escape(title)}</name>",
        "    <trkseg>",
    ]
    for lat, lon, ele, ts in points:
        out.append(f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
        if ele is not None:
            out.append(f"        <ele>{ele:.1f}</ele>")
        if ts is not None:
            out.append(
                f"        <time>{datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</time>"
            )
        out.append("      </trkpt>")
    out += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(out)


def points_from_trackjson(payload):
    """六只脚 trackjson 形态是 [[时间戳, 经度, 纬度, 海拔, 速度, 里程], ...]。

    对几种常见包装都做兼容，并做一次经纬度范围自检：明显越界就当作颠倒了交换回来。
    """
    rows = payload
    if isinstance(payload, dict):
        for key in ("points", "track", "data", "track_data", "list"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list) or not rows:
        return []

    points = []
    for row in rows:
        if isinstance(row, dict):
            ts = row.get("time") or row.get("timestamp") or row.get("t")
            lng = row.get("lng") if "lng" in row else row.get("lon", row.get("longitude"))
            lat = row.get("lat", row.get("latitude"))
            ele = row.get("ele", row.get("elevation", row.get("alt")))
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            ts, lng, lat = row[0], row[1], row[2]
            ele = row[3] if len(row) > 3 else None
        else:
            continue

        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            continue

        if abs(lat) > 90 and abs(lng) <= 90:
            lat, lng = lng, lat
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        if abs(lat) < 0.01 and abs(lng) < 0.01:
            continue

        try:
            ele = float(ele) if ele not in (None, "", "-") else None
        except (TypeError, ValueError):
            ele = None
        try:
            ts = int(float(ts)) if ts not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            ts = None

        points.append((lat, lng, ele, ts))
    return points


# --------------------------------------------------------------------------
# 浏览器动作
# --------------------------------------------------------------------------

def is_logged_in(page) -> bool:
    try:
        if page.locator("a#logout").count() > 0:
            return True
        if page.locator("a[href*='logout']").count() > 0:
            return True
        body = page.inner_text("body", timeout=5000)
        return ("退出" in body) or ("我的六只脚" in body)
    except Exception:
        return False


def wait_for_login(page, headless: bool, timeout: int = 900) -> None:
    try:
        page.goto(f"{BASE}/accounts/login/", wait_until="domcontentloaded")
    except Exception as exc:
        log(f"登录页打不开：{exc}")
    page.wait_for_timeout(1500)

    if is_logged_in(page):
        log("已是登录状态（沿用了上次的会话），当前地址：" + page.url)
        return

    log("=" * 62)
    log("请在打开的浏览器窗口里登录六只脚（账号密码只输给你自己的浏览器）")
    log("登录成功后不用管，脚本会自动往下走。最多等 %d 秒。" % timeout)
    log("=" * 62)
    if headless:
        log("警告：--headless 模式下没人能操作登录，建议去掉这个参数")

    start = time.time()
    last_note = 0
    while time.time() - start < timeout:
        page.wait_for_timeout(2000)
        try:
            if is_logged_in(page):
                log("检测到登录成功，当前地址：" + page.url)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass
        waited = int(time.time() - start)
        if waited - last_note >= 60:
            last_note = waited
            log(f"  还在等登录…… 已等 {waited} 秒，当前地址：{page.url}")
    raise SystemExit("等登录超时了。重跑一次；若窗口里其实已登录，检查 .cache 里的会话是否还在。")


def find_uid(page) -> str | None:
    """uid 只是兜底地址用得着，认不出也不影响主流程。"""
    try:
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        m = re.search(r"Hi,\s*(\S+)", body)
        nick = m.group(1).strip() if m else None
        if nick:
            hrefs = page.eval_on_selector_all(
                "a[title], a[href*='/userindex/']",
                "els => els.map(e => [e.getAttribute('href'), e.getAttribute('title'), e.innerText])",
            )
            for href, title, text in hrefs or []:
                if nick in ((title or ""), (text or "")):
                    mm = re.search(r"/userindex/(\d+)/", href or "")
                    if mm:
                        return mm.group(1)
    except Exception:
        pass
    return None


def settle(page, seconds: float = 6.0, expect_ids: bool = True) -> list[str]:
    """等页面把行程卡片渲染出来：边滚边等，最多 seconds 秒。"""
    deadline = time.time() + seconds
    ids: list[str] = []
    while time.time() < deadline:
        page.wait_for_timeout(500)
        ids = scrape_ids(page)
        if ids and expect_ids:
            return ids
        if not expect_ids:
            break
    return ids


def activate_tab(page, tab: str, label: str) -> bool:
    """切 tab。用 JS 点页面上可见的那个，绕开下拉菜单里隐藏的同名链接。"""
    try:
        done = page.evaluate(
            """([tab, text]) => {
                const all = [...document.querySelectorAll('a')];
                const vis = all.filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                let el = vis.find(e => (e.getAttribute('href') || '').includes(tab));
                if (!el && text) el = vis.find(e => (e.innerText || '').trim() === text);
                if (!el) el = all.find(e => (e.getAttribute('href') || '').includes(tab));
                if (el) { el.click(); return true; }
                return false;
            }""",
            [tab, label],
        )
    except Exception:
        done = False
    if done:
        log(f"    已切到 {label}")
        page.wait_for_timeout(1800)
    else:
        log(f"    没找到 {label} 的 tab 入口")
    return bool(done)


def read_list(page, url: str, tab: str | None, label: str) -> tuple[list[str], str]:
    """打开一个列表地址，必要时切 tab，返回 (行程编号, 实际地址)。"""
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        log(f"    {url} 打不开：{exc}")
        return [], url
    page.wait_for_timeout(1000)

    if tab:
        activate_tab(page, tab, label)

    ids = settle(page, 8.0)
    if not ids:
        for _ in range(6):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(400)
        ids = scrape_ids(page)

    log(f"    {url} -> {len(ids)} 条")
    return ids, page.url


def collect_ids(page, targets, label: str, uid: str | None) -> tuple[list[str], str]:
    """按优先级试各个地址，第一个能出编号的算数；再尝试翻页。"""
    for pat, name, tab in targets:
        if "{uid}" in pat and not uid:
            continue
        url = BASE + pat.format(uid=uid or "")
        ids, landed = read_list(page, url, tab, label)
        if not ids:
            continue
        page.wait_for_timeout(600)
        ids = paginate(page, ids, tab)
        return ids, landed
    return [], ""


def paginate(page, ids: list[str], tab: str | None) -> list[str]:
    """翻页：优先找「下一页」，再试 tab 上的 -2/-3 序号锚点。"""
    seen = set(ids)
    for n in range(2, 41):
        before = len(ids)
        moved = False

        nxt = None
        for sel in ["a:has-text('下一页')", "a[rel='next']", "a:has-text('下页')"]:
            try:
                loc = page.locator(sel).first
                if not (loc.count() and loc.is_visible()):
                    continue
                href = loc.get_attribute("href") or ""
                if href.startswith("javascript") or not href.strip():
                    continue
                nxt = href
                break
            except Exception:
                continue
        if nxt:
            try:
                page.goto(urljoin(page.url, nxt), wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                for _ in range(4):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(300)
                moved = True
            except Exception:
                moved = False
        elif tab:
            try:
                moved = bool(
                    page.evaluate(
                        """([sel]) => {
                            const el = [...document.querySelectorAll('a')]
                                .find(e => (e.getAttribute('href') || '').includes(sel));
                            if (el) { el.click(); return true; }
                            return false;
                        }""",
                        [f"{tab}-{n}"],
                    )
                )
                if moved:
                    page.wait_for_timeout(1500)
            except Exception:
                moved = False

        if not moved:
            break
        for tid in scrape_ids(page):
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
        log(f"    第 {n} 页：+{len(ids) - before}，累计 {len(ids)}")
        if len(ids) == before:
            break
    return ids


def fetch(ctx, url: str):
    """走浏览器上下文的会话去取文件，手动带上 UA 与 Referer。"""
    return ctx.request.get(
        url, timeout=60000, headers={"User-Agent": UA, "Referer": BASE + "/"}
    )


def check_body(resp, url: str) -> tuple[bytes | None, str]:
    """校验响应，返回 (内容, 扩展名) 或 (None, 失败原因)。"""
    if not resp.ok:
        return None, f"HTTP {resp.status}"
    body = resp.body()
    if len(body) < 200:
        return None, f"太小 {len(body)}B"
    if looks_like_html(body):
        return None, "返回网页不是轨迹"
    low = url.lower()
    if low.endswith(".gpx"):
        return body, ".gpx"
    if low.endswith(".kml"):
        return body, ".kml"
    # /gps/file/?format=gpx 这种靠查询参数给格式
    m = re.search(r"[?&](?:format|fileformat)=([a-z0-9]+)", low)
    if m and m.group(1) in ("gpx", "kml"):
        return body, "." + m.group(1)
    ctype = (resp.headers.get("content-type") or "").lower()
    if "gpx" in ctype:
        return body, ".gpx"
    if "kml" in ctype:
        return body, ".kml"
    head = body[:600].lower()
    if b"<gpx" in head:
        return body, ".gpx"
    if b"<kml" in head:
        return body, ".kml"
    return None, f"认不出格式（content-type={ctype or '空'}）"


def download_one(page, ctx, tid: str, out_dir: Path, raw_dir: Path, force: bool) -> dict:
    row = {"id": tid, "title": "", "source": "", "file": "", "points": 0, "status": ""}

    try:
        page.goto(f"{BASE}/trip/{tid}/", wait_until="domcontentloaded")
        page.wait_for_timeout(700)
        title = (page.title() or "").split("_")[0].strip()
        row["title"] = title or f"trip-{tid}"
    except Exception as exc:
        row["title"] = f"trip-{tid}"
        log(f"  ! 标题取不到: {exc}")

    stem = f"{tid}_{safe_name(row['title'])}"

    # 1) 官方下载页
    cands: list[str] = []
    try:
        page.goto(f"{BASE}/trip/{tid}/downloads/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        page_self = page.url.split("?")[0].rstrip("/")
        picked: list[str] = []
        for h in hrefs or []:
            if not h:
                continue
            full = urljoin(page.url, h)
            low = full.lower()
            if low.split("?")[0].rstrip("/") == page_self:
                continue  # 下载页自己，不是文件
            if ("gps/file" in low or "fileformat=" in low or "format=" in low
                    or re.search(r"\.(gpx|kml|zip)(\?|$)", low) or "download" in low):
                if full not in picked:
                    picked.append(full)
        cands += picked
        log(f"  下载页给出 {len(picked)} 个候选：{[u.split('/trip/')[-1] for u in picked][:6]}")
    except Exception as exc:
        log(f"  ! 下载页打不开: {exc}")

    # 2) 约定入口
    cands += [BASE + ep.format(tid=tid) for ep in DIRECT_ENDPOINTS]

    def rank(u: str) -> int:
        low = u.lower()
        if ".gpx" in low or "format=gpx" in low:
            return 0
        if ".kml" in low or "format=kml" in low:
            return 1
        return 2

    cands.sort(key=rank)
    reasons: list[str] = []
    seen: set[str] = set()
    for url in cands[:10]:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = fetch(ctx, url)
        except Exception as exc:
            reasons.append(f"{url} -> {exc}")
            continue
        body, info = check_body(resp, url)
        if body is None:
            reasons.append(f"{url} -> {info}")
            continue
        target = out_dir / f"{stem}{info}"
        if target.exists() and not force and target.stat().st_size > 0:
            row.update(file=str(target.relative_to(ROOT)), status="skip(exists)")
            return row
        target.write_bytes(body)
        row.update(file=str(target.relative_to(ROOT)), status="ok")
        return row

    # 3) trackjson 兜底
    try:
        resp = fetch(ctx, f"{BASE}/trip/{tid}/trackjson/")
        log(f"  trackjson: HTTP {resp.status}")
        raw = resp.body()
        if resp.ok and raw[:1] in (b"[", b"{"):
            payload = json.loads(raw.decode("utf-8", "replace"))
            (raw_dir / f"{tid}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            pts = points_from_trackjson(payload)
            log(f"  trackjson 解出 {len(pts)} 个点")
            if len(pts) >= 2:
                target = out_dir / f"{stem}.gpx"
                if target.exists() and not force:
                    row.update(file=str(target.relative_to(ROOT)), status="skip(exists)")
                    return row
                target.write_text(gpx_from_points(pts, row["title"]), encoding="utf-8")
                row.update(file=str(target.relative_to(ROOT)), points=len(pts), status="ok(trackjson)")
                return row
    except Exception as exc:
        log(f"  ! trackjson 失败: {exc}")

    row["status"] = "fail"
    for r in reasons[:4]:
        log(f"    {r}")
    return row


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------

def snap(page, tag: str) -> dict:
    info: dict = {"url": page.url, "title": ""}
    try:
        info["title"] = page.title()
    except Exception:
        pass
    try:
        png = LOG_DIR / f"{tag}.png"
        page.screenshot(path=str(png))
        info["screenshot"] = png.name
    except Exception as exc:
        info["screenshot_error"] = str(exc)
    try:
        html = LOG_DIR / f"{tag}.html"
        html.write_text(page.content(), encoding="utf-8")
        info["html"] = html.name
    except Exception as exc:
        info["html_error"] = str(exc)
    return info


def diagnose(page, ctx, uid: str | None) -> None:
    log("== 诊断模式：只留证，不下载 ==")
    report: dict = {"time": datetime.now().isoformat(timespec="seconds"), "uid": uid}

    report["logged_in"] = is_logged_in(page)
    log(f"登录态：{report['logged_in']}")

    for label, targets in (("my", MY_TARGETS), ("fav", FAV_TARGETS)):
        report[label] = []
        for pat, name, tab in targets:
            entry: dict = {"pattern": pat, "tab": tab}
            if "{uid}" in pat and not uid:
                entry["skip"] = "无 uid"
                report[label].append(entry)
                continue
            url = BASE + pat.format(uid=uid or "")
            ids, landed = read_list(page, url, tab, label)
            entry.update(requested=url, landed=landed, ids=len(ids), sample=ids[:5])
            report[label].append(entry)
            if ids:
                snap(page, f"03-{label}")
                break

    path = LOG_DIR / "diagnose.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("诊断结果已写入：" + str(path))


# --------------------------------------------------------------------------

def main() -> int:
    global LOGFILE

    ap = argparse.ArgumentParser(description="导出六只脚账号里的收藏与行程轨迹")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE), help="浏览器会话目录")
    ap.add_argument("--uid", help="你的 userindex 编号（兜底地址才用得上）")
    ap.add_argument("--my-url", help="我的行程列表页完整地址")
    ap.add_argument("--fav-url", help="收藏列表页完整地址")
    ap.add_argument("--only", choices=["both", "my", "fav"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="总共最多下几条，0 表示不限")
    ap.add_argument("--delay", type=float, default=2.0, help="每条之间的间隔秒数")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    ap.add_argument("--headless", action="store_true", help="无头模式（登录仍需有头）")
    ap.add_argument("--diagnose", action="store_true", help="只诊断留证，不下载")
    ap.add_argument("--probe", action="store_true", help="--diagnose 的旧名字")
    args = ap.parse_args()
    diagnose_mode = args.diagnose or args.probe

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGFILE = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
    log(f"启动，日志：{LOGFILE}")
    log(f"Python {sys.version.split()[0]}，工作目录 {Path.cwd()}")

    out_root = Path(args.out).resolve()
    my_dir, fav_dir, raw_dir = out_root / "my", out_root / "fav", out_root / "raw"
    for d in (my_dir, fav_dir, raw_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright。先运行：pip install playwright && playwright install chromium")
        return 2

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile_dir).resolve()),
            headless=args.headless,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log("浏览器已启动")

        try:
            wait_for_login(page, args.headless)

            uid = args.uid or find_uid(page)
            log(f"uid = {uid}")

            if diagnose_mode:
                diagnose(page, ctx, uid)
                return 0

            jobs = []
            my_ids: list[str] = []
            fav_ids: list[str] = []
            if args.only in ("both", "my"):
                my_ids, _ = (
                    read_list(page, args.my_url, None, "我的行程")
                    if args.my_url
                    else collect_ids(page, MY_TARGETS, "我的行程", uid)
                )
                log(f"我的行程共 {len(my_ids)} 条")

            if args.only in ("both", "fav"):
                fav_ids, _ = (
                    read_list(page, args.fav_url, None, "我的收藏")
                    if args.fav_url
                    else collect_ids(page, FAV_TARGETS, "我的收藏", uid)
                )
                log(f"我的收藏共 {len(fav_ids)} 条")

            # 两个 tab 的卡片可能同时在 DOM 里，会重复；自己记录的行程优先
            if my_ids and fav_ids:
                overlap = set(my_ids) & set(fav_ids)
                if overlap:
                    log(f"有 {len(overlap)} 条两边都出现，按「我的行程」归档，收藏里跳过")
                    fav_ids = [t for t in fav_ids if t not in overlap]
                if set(my_ids) == set(fav_ids):
                    snap(page, "05-fav-suspect")
                    log("注意：两边完全一致，很可能「我的收藏」tab 没真正切过去。")
                    log("已存 logs/05-fav-suspect.html —— 把这个文件发我，我按真实结构修。")

            jobs = [(t, my_dir, "my") for t in my_ids] + [(t, fav_dir, "fav") for t in fav_ids]

            if not jobs:
                snap(page, "99-empty")
                log("一条都没收集到，页面已存到 logs/99-empty.*")
                return 4

            if args.limit:
                jobs = jobs[: args.limit]

            rows, ok, fail = [], 0, 0
            for i, (tid, d, src) in enumerate(jobs, 1):
                log(f"[{i}/{len(jobs)}] {src} {tid}")
                row = download_one(page, ctx, tid, d, raw_dir, args.force)
                row["source"] = src
                rows.append(row)
                if row["status"].startswith("ok") or row["status"].startswith("skip"):
                    ok += 1
                else:
                    fail += 1
                if i < len(jobs):
                    time.sleep(args.delay + random.random())

            manifest = out_root / "manifest.csv"
            with manifest.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["id", "title", "source", "file", "points", "status"])
                w.writeheader()
                w.writerows(rows)

            log("=" * 62)
            log(f"完成：成功 {ok}，失败 {fail}，清单 {manifest}")
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        if LOGFILE is not None:
            try:
                with LOGFILE.open("a", encoding="utf-8") as f:
                    f.write("\n[TRACEBACK]\n" + traceback.format_exc() + "\n")
            except Exception:
                pass
            print(f"\n崩溃了。完整 traceback 在：{LOGFILE}")
        sys.exit(1)
