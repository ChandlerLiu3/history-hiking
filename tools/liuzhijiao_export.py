#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六只脚（foooooot.com）个人轨迹批量导出。

用法:
    pip install playwright
    playwright install chromium
    python tools/liuzhijiao_export.py

流程:
  1. 弹出浏览器窗口，你自己输入账号密码登录（脚本不接触密码，也不保存它）
  2. 脚本自动翻「我的行程」和「收藏」列表，收集行程编号
  3. 逐条打开 /trip/<id>/downloads/ 下载 GPX；拿不到就退回 trackjson 自己拼 GPX
  4. 结果落在 data/liuzhijiao/，清单见 manifest.csv

脚本只做读取和下载：不点赞、不评论、不改任何东西。

登录态存在 .cache/liuzhijiao-profile/ 里，下次运行不用重新登录。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

BASE = "http://www.foooooot.com"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "liuzhijiao"
DEFAULT_PROFILE = ROOT / ".cache" / "liuzhijiao-profile"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 收藏页的候选地址。站点没公开文档，只能逐个试，第一个真能翻出行程的算数。
FAV_CANDIDATES = [
    "/userindex/{uid}/favorite/",
    "/userindex/{uid}/favorites/",
    "/userindex/{uid}/collect/",
    "/userindex/{uid}/collection/",
    "/userindex/{uid}/star/",
    "/userindex/{uid}/like/",
    "/favorite/",
    "/favorites/",
    "/trip/favorite/",
]

MY_CANDIDATES = [
    "/userindex/{uid}/trip/",
    "/userindex/{uid}/trips/",
    "/trip/my/",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def safe_name(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return (s or "untitled")[:maxlen]


def looks_like_html(body: bytes) -> bool:
    head = body[:400].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<head" in head[:200]


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
    """六只脚 trackjson 实测形态是 [[时间戳, 经度, 纬度, 海拔, 速度, 里程], ...]。

    这里对几种常见包装都做兼容，并做一次经纬度范围自检（中国境内经度约 73-135、
    纬度约 18-54），明显颠倒时交换回来。
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

        # 经纬度颠倒自检
        if abs(lat) > 90 and abs(lng) <= 90:
            lat, lng = lng, lat
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        if abs(lat) < 0.01 and abs(lng) < 0.01:
            continue  # 空点

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
        if page.locator("a[href*='logout']").count() > 0:
            return True
        body = page.inner_text("body", timeout=5000)
        return ("退出" in body) or ("我的六只脚" in body) or ("个人中心" in body)
    except Exception:
        return False


def wait_for_login(page, headless: bool, timeout: int = 600) -> None:
    page.goto(f"{BASE}/accounts/login/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    if is_logged_in(page):
        log("已是登录状态（沿用了上次的会话）")
        return

    log("=" * 62)
    log("请在打开的浏览器窗口里登录六只脚（账号密码只输给你自己的浏览器）")
    log("登录成功后不用管，脚本会自动往下走。最多等 %d 秒。" % timeout)
    log("=" * 62)
    if headless:
        log("警告：--headless 模式下没人能操作登录，建议去掉这个参数")

    start = time.time()
    while time.time() - start < timeout:
        page.wait_for_timeout(2000)
        try:
            if is_logged_in(page):
                log("检测到登录成功")
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass
    raise SystemExit("等登录超时了。重新运行一次，或者在窗口里登录后立刻回车重试。")


def find_uid(page) -> str | None:
    """从页头的头像链接里抠出自己的 userindex 编号。"""
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    probes = [
        "a:has(img[src*='avatar'])",
        "a[href*='/userindex/']:has(img)",
    ]
    for sel in probes:
        try:
            hrefs = page.eval_on_selector_all(
                sel, "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            continue
        for href in hrefs or []:
            m = re.search(r"/userindex/(\d+)/", href or "")
            if m:
                return m.group(1)

    # 兜底：把所有 userindex 链接按出现次数排序，取头部那个
    try:
        hrefs = page.eval_on_selector_all(
            "header a[href*='/userindex/'], #header a[href*='/userindex/']",
            "els => els.map(e => e.getAttribute('href'))",
        )
        for href in hrefs or []:
            m = re.search(r"/userindex/(\d+)/", href or "")
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def collect_trip_ids(page, url: str, max_pages: int = 40, verbose: bool = True):
    """翻一个列表页，返回行程编号列表（保序去重）。

    列表页可能是懒加载，所以每页都先滚到底再取链接。
    """
    ids: list[str] = []
    seen: set[str] = set()
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        log(f"  列表页打不开：{url}（{exc}）")
        return []
    page.wait_for_timeout(1200)

    landed = urlparse(page.url)
    want = urlparse(url)
    if landed.path.rstrip("/") != want.path.rstrip("/"):
        if verbose:
            log(f"  跳过 {want.path}：被重定向到 {landed.path}")
        return []

    for page_no in range(1, max_pages + 1):
        for _ in range(8):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(250)

        try:
            hrefs = page.eval_on_selector_all(
                "a[href*='/trip/']", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []

        added = 0
        for href in hrefs or []:
            m = re.search(r"/trip/(\d+)/?$", (href or "").split("?")[0].split("#")[0])
            if not m:
                continue
            tid = m.group(1)
            if tid in seen:
                continue
            seen.add(tid)
            ids.append(tid)
            added += 1

        if verbose:
            log(f"  第 {page_no} 页：+{added}，累计 {len(ids)}")

        # 下一页
        nxt = None
        for sel in ["a:has-text('下一页')", "a[rel='next']", "a.next"]:
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
        if not nxt:
            break
        try:
            page.goto(urljoin(page.url, nxt), wait_until="domcontentloaded")
        except Exception as exc:
            log(f"  翻页失败，停止：{exc}")
            break
        page.wait_for_timeout(1500)

    return ids


def resolve_list_url(page, uid: str, templates, label: str) -> str | None:
    """先看个人主页上有没有对应入口，再挨个试候选地址，第一个真能翻出行程的算数。

    判定标准有两条：落地地址没被重定向走、页面上「行程详情」链接数量合理。
    少了是空列表，多了说明被弹回首页（首页塞满全站行程），两种都不要。
    """
    tried: list[str] = []

    if label == "收藏":
        try:
            page.goto(f"{BASE}/userindex/{uid}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            pairs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.getAttribute('href'), e.innerText])"
            )
            for href, text in pairs or []:
                full = urljoin(page.url, href or "")
                if not full.startswith(BASE):
                    continue
                if any(k in (text or "") for k in ("收藏", "喜欢")):
                    tried.append(full)
        except Exception:
            pass

    tried += [BASE + pat.format(uid=uid) for pat in templates]

    seen: set[str] = set()
    for url in tried:
        path = urlparse(url).path.rstrip("/")
        if path in seen:
            continue
        seen.add(path)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(900)
        except Exception:
            continue
        if urlparse(page.url).path.rstrip("/") != path:
            continue
        try:
            hrefs = page.eval_on_selector_all(
                "a[href*='/trip/']", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            continue
        n = len(
            {m.group(1) for h in (hrefs or []) if (m := re.search(r"/trip/(\d+)/", h or ""))}
        )
        if 0 < n <= 400:
            log(f"  命中{label}列表页 {path}（{n} 条）")
            return url
    return None


def fetch(ctx, url: str):
    """走浏览器上下文的会话去取文件，手动带上 UA 与 Referer。"""
    return ctx.request.get(
        url, timeout=60000, headers={"User-Agent": UA, "Referer": BASE + "/"}
    )


def download_one(page, ctx, tid: str, out_dir: Path, raw_dir: Path, force: bool) -> dict:
    row = {"id": tid, "title": "", "source": "", "file": "", "points": 0, "status": ""}

    # 标题
    try:
        page.goto(f"{BASE}/trip/{tid}/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        title = (page.title() or "").split("_")[0].strip()
        row["title"] = title or f"trip-{tid}"
    except Exception as exc:
        row["title"] = f"trip-{tid}"
        log(f"  ! 标题取不到: {exc}")

    stem = f"{tid}_{safe_name(row['title'])}"

    # 1) 官方下载页
    try:
        page.goto(f"{BASE}/trip/{tid}/downloads/", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        cands = []
        for h in hrefs or []:
            if not h:
                continue
            full = urljoin(page.url, h)
            if re.search(r"\.(gpx|kml|zip)(\?|$)", full, re.I) or "download" in full.lower():
                cands.append(full)

        # GPX 优先
        cands.sort(key=lambda u: (0 if ".gpx" in u.lower() else 1 if ".kml" in u.lower() else 2))
        for url in cands[:6]:
            try:
                resp = fetch(ctx, url)
                if not resp.ok:
                    continue
                body = resp.body()
                if len(body) < 200 or looks_like_html(body):
                    continue
                ext = ".gpx" if ".gpx" in url.lower() else ".kml" if ".kml" in url.lower() else ".zip"
                if ext == ".zip":
                    continue  # 压缩包先跳过，避免解压带来的一堆例外
                target = out_dir / f"{stem}{ext}"
                if target.exists() and not force and target.stat().st_size > 0:
                    row.update(file=str(target.relative_to(ROOT)), status="skip(exists)")
                    return row
                target.write_bytes(body)
                row.update(file=str(target.relative_to(ROOT)), status="ok")
                return row
            except Exception:
                continue
    except Exception as exc:
        log(f"  ! 下载页打不开: {exc}")

    # 2) trackjson 兜底
    try:
        resp = fetch(ctx, f"{BASE}/trip/{tid}/trackjson/")
        if resp.ok:
            payload = resp.json()
            (raw_dir / f"{tid}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            pts = points_from_trackjson(payload)
            if len(pts) >= 2:
                target = out_dir / f"{stem}.gpx"
                if target.exists() and not force:
                    row.update(file=str(target.relative_to(ROOT)), status="skip(exists)")
                    return row
                target.write_text(gpx_from_points(pts, row["title"]), encoding="utf-8")
                row.update(file=str(target.relative_to(ROOT)), points=len(pts), status="ok(trackjson)")
                return row
    except Exception:
        pass

    row["status"] = "fail"
    return row


def probe(page, ctx, uid: str | None) -> None:
    """诊断模式：把列表页和一条行程的下载页链接打出来，便于对症改脚本。"""
    log("== 探测模式 ==")
    log(f"首页状态: {page.goto(BASE, wait_until='domcontentloaded').status}")
    log(f"登录态: {is_logged_in(page)}")
    if not uid:
        uid = find_uid(page)
    log(f"uid: {uid}")
    if not uid:
        return

    for label, tpls in (("我的行程", MY_CANDIDATES), ("收藏", FAV_CANDIDATES)):
        log(f"-- {label} 候选 --")
        for pat in tpls:
            url = BASE + pat.format(uid=uid)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(600)
            except Exception as exc:
                log(f"  {pat}: 打不开 ({exc})")
                continue
            landed = urlparse(page.url).path
            hrefs = page.eval_on_selector_all("a[href*='/trip/']", "els => els.map(e => e.getAttribute('href'))")
            n = len({m.group(1) for h in (hrefs or []) if (m := re.search(r"/trip/(\d+)/", h or ""))})
            log(f"  {pat}: 落地 {landed}，行程链接 {n} 条")

    log("-- 随便挑一条行程看下载页 --")
    try:
        page.goto(f"{BASE}/userindex/{uid}/trip/", wait_until="domcontentloaded")
    except Exception as exc:
        log(f"  个人行程页打不开：{exc}")
        return
    page.wait_for_timeout(800)
    hrefs = page.eval_on_selector_all("a[href*='/trip/']", "els => els.map(e => e.getAttribute('href'))")
    tid = None
    for h in hrefs or []:
        m = re.search(r"/trip/(\d+)/", h or "")
        if m:
            tid = m.group(1)
            break
    log(f"样本行程: {tid}")
    if tid:
        try:
            page.goto(f"{BASE}/trip/{tid}/downloads/", wait_until="domcontentloaded")
            page.wait_for_timeout(800)
        except Exception as exc:
            log(f"  下载页打不开：{exc}")
            return
        ds = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        for h in (ds or [])[:40]:
            if h and re.search(r"\.(gpx|kml|zip)|download", h, re.I):
                log(f"  下载链接: {h}")
        r = fetch(ctx, f"{BASE}/trip/{tid}/trackjson/")
        log(f"  trackjson: HTTP {r.status}, {r.headers.get('content-type')}, {len(r.body())} bytes")
        log(f"  trackjson 预览: {r.body()[:200]!r}")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="导出六只脚账号里的收藏与行程轨迹")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE), help="浏览器会话目录")
    ap.add_argument("--uid", help="你的 userindex 编号，自动识别失败时手动指定")
    ap.add_argument("--my-url", help="我的行程列表页完整地址，覆盖自动探测")
    ap.add_argument("--fav-url", help="收藏列表页完整地址，覆盖自动探测")
    ap.add_argument("--only", choices=["both", "my", "fav"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="总共最多下几条，0 表示不限")
    ap.add_argument("--delay", type=float, default=2.0, help="每条之间的间隔秒数")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    ap.add_argument("--headless", action="store_true", help="无头模式（登录仍需有头）")
    ap.add_argument("--probe", action="store_true", help="只诊断，不下载")
    args = ap.parse_args()

    out_root = Path(args.out).resolve()
    my_dir = out_root / "my"
    fav_dir = out_root / "fav"
    raw_dir = out_root / "raw"
    for d in (my_dir, fav_dir, raw_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright。先运行：\n  pip install playwright\n  playwright install chromium", file=sys.stderr)
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

        try:
            wait_for_login(page, args.headless)

            uid = args.uid or find_uid(page)
            if not uid:
                log("没认出自己的 userindex 编号。打开你的个人主页，把地址里的数字填给 --uid 再跑一次。")
                log("例如主页是 /userindex/123456/ 就写 --uid 123456")
                return 3
            log(f"uid = {uid}")

            if args.probe:
                probe(page, ctx, uid)
                return 0

            jobs = []
            if args.only in ("both", "my"):
                url = (
                    args.my_url
                    or resolve_list_url(page, uid, MY_CANDIDATES, "我的行程")
                    or (BASE + f"/userindex/{uid}/trip/")
                )
                log(f"收集我的行程: {url}")
                ids = collect_trip_ids(page, url)
                log(f"我的行程共 {len(ids)} 条")
                jobs += [(tid, my_dir, "my") for tid in ids]

            if args.only in ("both", "fav"):
                fav_url = args.fav_url or resolve_list_url(page, uid, FAV_CANDIDATES, "收藏")
                if fav_url:
                    log(f"收集收藏: {fav_url}")
                    ids = collect_trip_ids(page, fav_url)
                    log(f"收藏共 {len(ids)} 条")
                    jobs += [(tid, fav_dir, "fav") for tid in ids]
                else:
                    log("收藏列表页没找到。看 --probe 的输出，把正确地址用 --fav-url 传进来。")

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
            if fail:
                log("失败的条目多半是私有行程或已删除，清单里标了 fail。")
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
