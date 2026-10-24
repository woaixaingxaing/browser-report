#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome 浏览报告生成器 —— 日报 / 周报 / 月报 / 年度总结，单文件 HTML，离线可视化。

数据源：
  - Chrome History SQLite（visits.visit_time / visit_duration，精确到微秒）
  - Chrome Bookmarks JSON（date_added，精确到秒）
不依赖扩展、不联网、不修改原库（先快照再读）。

用法：
  python gen_report.py                      # 日报 + 本周 + 本月 + 今年
  python gen_report.py --anchor 2026-10-24  # 以某天为基准
  python gen_report.py --year 2026
  python gen_report.py --out dist/报告.html --profile Default
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import webbrowser
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from urllib.parse import urlparse

CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 单页有效阅读上限：避免"挂机页面"把总时长拉到 27 小时/天
MAX_VISIT_SECONDS = 15 * 60
SESSION_GAP_SECONDS = 20 * 60
SAME_URL_WINDOW = 30 * 60


def default_user_data() -> str:
    """当前系统上 Chrome 的用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "Google", "Chrome", "User Data")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    for candidate in ("~/.config/google-chrome", "~/.config/chromium"):
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            return path
    return os.path.expanduser("~/.config/google-chrome")


CATEGORIES = [
    ("搜索", "#0071e3", ("google.", "bing.com", "baidu.com", "duckduckgo.com", "so.com", "sogou.com")),
    ("AI", "#5856d6", ("chatgpt.com", "chat.openai.com", "claude.ai", "gemini.google.com", "deepseek.com",
                       "kimi.moonshot.cn", "doubao.com", "tongyi.aliyun.com", "perplexity.ai", "poe.com")),
    ("开发/文档", "#34c759", ("github.com", "gitlab.com", "gitee.com", "stackoverflow.com", "stackexchange.com",
                          "developer.mozilla.org", "docs.python.org", "learn.microsoft.com", "npmjs.com",
                          "pypi.org", "openfoam.org", "cfd-online.com", "readthedocs.io", "arxiv.org",
                          "localhost", "127.0.0.1", "juejin.cn", "csdn.net", "segmentfault.com",
                          "52pojie.cn", "qoder.com", "oschina.net", "quicker.net")),
    ("视频", "#ff3b30", ("youtube.com", "bilibili.com", "youku.com", "iqiyi.com", "v.qq.com", "douyin.com",
                      "netflix.com", "mgtv.com")),
    ("社交", "#ff9500", ("weibo.com", "zhihu.com", "x.com", "twitter.com", "reddit.com", "douban.com",
                     "tieba.baidu.com", "v2ex.com", "discord.com", "t.me", "facebook.com", "instagram.com")),
    ("资讯", "#5ac8fa", ("36kr.com", "ithome.com", "sspai.com", "smzdm.com", "news.", "cnbeta.com",
                     "thepaper.cn", "huxiu.com", "ifanr.com", "techcrunch.com", "news.ycombinator.com",
                     "msn.cn", "msn.com", "ifeng.com", "sina.com.cn", "news.163.com", "news.qq.com")),
    ("购物", "#ff2d55", ("taobao.com", "tmall.com", "jd.com", "amazon.", "pinduoduo.com", "1688.com",
                     "xiaohongshu.com")),
    ("办公", "#8e8e93", ("mail.google.com", "outlook.", "feishu.cn", "larksuite.com", "notion.so",
                     "wps.cn", "docs.google.com", "drive.google.com", "yuque.com", "dingtalk.com",
                     "office.163.com", "office.com")),
    ("招聘", "#00c7be", ("zhaopin.com", "51job.com", "boss.com", "liepin.com", "gxrc.com", "sydw.",
                     "gwy.", "offcn.com", "chinagwy.org")),
    ("教育", "#ac8e68", ("edu.cn", "chsi.com.cn", "xuetangx.com", "icourse163.org", "coursera.org")),
    ("本地", "#30b0c7", ("chrome-extension://", "chrome://", "file://", "extension://", "about:")),
]


def chrome_time(us: int | str | None) -> dt.datetime | None:
    if not us:
        return None
    try:
        us = int(us)
    except (TypeError, ValueError):
        return None
    if us <= 0:
        return None
    return (CHROME_EPOCH + dt.timedelta(microseconds=us)).astimezone(LOCAL_TZ)


def human_seconds(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m:02d} 分"
    if m:
        return f"{m} 分 {s:02d} 秒"
    return f"{s} 秒"


def human_short(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def pct(cur: float, prev: float) -> str:
    if not prev:
        return "—"
    d = (cur - prev) / prev * 100
    cls = "up" if d > 0 else ("down" if d < 0 else "flat")
    sign = "+" if d > 0 else ""
    return f'<span class="delta {cls}">{sign}{d:.0f}%</span>'


def domain_of(url: str) -> str:
    if url.startswith("chrome-extension://"):
        return "扩展页面"
    if url.startswith("chrome://"):
        return "浏览器内部"
    if url.startswith("file://"):
        return "本地文件"
    try:
        p = urlparse(url)
    except ValueError:
        return "其他"
    host = (p.hostname or "").lower()
    if not host:
        return "其他"
    if p.port:
        host = f"{host}:{p.port}"
    return host


def category_of(domain: str, url: str) -> str:
    hay = (domain + " " + url).lower()
    for name, color, keys in CATEGORIES:
        for k in keys:
            if k in hay:
                return name
    return "其他"


CAT_COLOR = {name: color for name, color, _ in CATEGORIES}
CAT_COLOR.setdefault("其他", "#c7c7cc")

# 热力档位用固定访问量边界，月历和年度日历共用，颜色不会随当月最大值漂移。
# (下限, 上限)，最后一档上限为 None 表示 400 次以上。
HEAT_STEPS = ((1, 49), (50, 149), (150, 399), (400, None))
HEAT_LEGEND = "1–49 · 50–149 · 150–399 · 400+ 次/天"


def heat_level(n: int) -> int:
    if n <= 0:
        return 0
    for level, (_, hi) in enumerate(HEAT_STEPS, 1):
        if hi is None or n <= hi:
            return level
    return 4


def clean_title(title: str | None, url: str) -> str:
    t = " ".join((title or "").split())
    # 图片页会把 data: URI 当标题，直接丢弃
    if t.lower().startswith(("data:", "blob:")) or len(t) > 300:
        t = ""
    if not t:
        p = urlparse(url)
        t = (p.path or url).rstrip("/").split("/")[-1] or domain_of(url)
    return t[:120]


def is_web_url(url: str) -> bool:
    """只把 http(s) 页面渲染成可点击链接，避免 javascript: 等内容进入 href。"""
    return url.startswith(("http://", "https://"))


def site_url(domain: str) -> str:
    """站点排行里的域名转成可打开的首页；浏览器内部页不生成链接。"""
    if domain in ("扩展页面", "浏览器内部", "本地文件", "其他"):
        return ""
    if domain.startswith(("localhost", "127.0.0.1")):
        return "http://" + domain
    return "https://" + domain if "." in domain else ""


def page_link(url: str, label: str, cls: str = "page-link") -> str:
    """页面标题链接。非 http(s) 页面只显示文字，不做成 href。"""
    if is_web_url(url):
        return (
            f'<a class="{cls}" href="{escape(url)}" target="_blank" rel="noopener noreferrer" '
            f'title="打开原页面：{escape(url)}">{escape(label)}'
            f'<span class="link-go" aria-hidden="true">↗</span></a>'
        )
    return f'<span class="{cls} no-href" title="浏览器内部页面，无法直接跳转">{escape(label)}</span>'


def domain_link(domain: str) -> str:
    url = site_url(domain)
    if not url:
        return escape(domain)
    return (
        f'<a class="site-link" href="{escape(url)}" target="_blank" rel="noopener noreferrer" '
        f'title="打开 {escape(domain)}">{escape(domain)}'
        f'<span class="link-go" aria-hidden="true">↗</span></a>'
    )


# ---------------------------------------------------------------- data loading

def snapshot_history(profile_dir: str) -> tuple[str, tempfile.TemporaryDirectory]:
    src = os.path.join(profile_dir, "History")
    if not os.path.exists(src):
        raise SystemExit(f"找不到 History 数据库：{src}")
    tmp = tempfile.TemporaryDirectory(prefix="chrome-hist-")
    shutil.copy2(src, os.path.join(tmp.name, "History"))
    for suffix in ("-journal", "-wal", "-shm"):
        p = src + suffix
        if os.path.exists(p):
            try:
                shutil.copy2(p, os.path.join(tmp.name, "History" + suffix))
            except OSError:
                pass
    return os.path.join(tmp.name, "History"), tmp


def load_visits(db_path: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT v.visit_time, v.visit_duration, u.url, u.title, u.visit_count
        FROM visits v JOIN urls u ON u.id = v.url
        WHERE v.visit_time > 0
        ORDER BY v.visit_time
        """
    ).fetchall()
    con.close()
    out = []
    for vt, vd, url, title, vc in rows:
        if url.startswith("data:"):
            continue
        when = chrome_time(vt)
        if not when:
            continue
        dur = (vd or 0) / 1e6
        eff = min(dur, MAX_VISIT_SECONDS) if dur > 0 else 0.0
        dom = domain_of(url)
        out.append({
            "t": when,
            "dur": dur,
            "eff": eff,
            "url": url,
            "title": clean_title(title, url),
            "domain": dom,
            "cat": category_of(dom, url),
            "vc": vc or 0,
        })
    # 单页有效时长：不超过「记录时长」和「到下一次访问的间隔」，再套 15 分钟上限。
    # 这样挂机标签页、自动刷新页面都不会把总量放大。
    for i, v in enumerate(out):
        nxt = out[i + 1] if i + 1 < len(out) else None
        gap = (nxt["t"] - v["t"]).total_seconds() if nxt else MAX_VISIT_SECONDS
        v["eff"] = min(v["dur"], max(gap, 0.0), MAX_VISIT_SECONDS) if v["dur"] > 0 else 0.0
    return out


def load_bookmarks(profile_dir: str):
    path = os.path.join(profile_dir, "Bookmarks")
    if not os.path.exists(path):
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []

    def walk(node, path_parts):
        if node.get("type") == "url":
            out.append({
                "name": " ".join((node.get("name") or "").split()) or node.get("url", ""),
                "url": node.get("url", ""),
                "added": chrome_time(node.get("date_added")),
                "folder": " / ".join(path_parts) or "未分类",
            })
            return
        name = node.get("name") or ""
        nxt = path_parts if node.get("type") == "url" else (path_parts + [name] if name else path_parts)
        for child in node.get("children") or []:
            walk(child, nxt)

    for root in (data.get("roots") or {}).values():
        if isinstance(root, dict):
            walk(root, [])
    return out


def build_history_data(visits):
    """生成前端历史浏览用的紧凑数据。

    pages: [标题, URL, 站点, 分类, 次数, 有效秒, 最后访问]
    hist:  [Unix 秒, pages 下标, 本次有效秒]

    hist 只保留 http(s) 页面，体积约 200 KB，却足以还原逐次访问时间轴。
    页面标题取最近一次访问的标题，避免同一 URL 反复存字符串。
    """
    pages = {}
    for v in visits:
        if not is_web_url(v["url"]):
            continue
        s = pages.get(v["url"])
        if s is None:
            s = {
                "title": v["title"], "url": v["url"], "domain": v["domain"],
                "cat": v["cat"], "visits": 0, "seconds": 0.0, "last": v["t"],
            }
            pages[v["url"]] = s
        s["visits"] += 1
        s["seconds"] += v["eff"]
        if v["t"] >= s["last"]:
            s["last"] = v["t"]
            s["title"] = v["title"]
    ordered = sorted(pages.values(), key=lambda r: (-r["seconds"], -r["visits"]))
    rows = [
        [r["title"], r["url"], r["domain"], r["cat"], r["visits"],
         round(r["seconds"], 1), r["last"].strftime("%Y-%m-%d %H:%M")]
        for r in ordered
    ]
    url_index = {r[1]: i for i, r in enumerate(rows)}
    hist = [
        [int(v["t"].timestamp()), url_index[v["url"]], round(v["eff"])]
        for v in visits if is_web_url(v["url"])
    ]
    hist.sort(key=lambda x: x[0])
    return rows, hist


# ---------------------------------------------------------------- aggregation

def build_period(key, label, start, end, visits, bookmarks, prev_start, prev_end, now):
    rows = [v for v in visits if start <= v["t"] < end]
    prev_rows = [v for v in visits if prev_start <= v["t"] < prev_end]

    def totals(items):
        doms = {v["domain"] for v in items}
        pages = {v["url"] for v in items}
        secs = sum(v["eff"] for v in items)
        days = {v["t"].date() for v in items}
        return {
            "visits": len(items),
            "pages": len(pages),
            "domains": len(doms),
            "seconds": secs,
            "days": len(days),
        }

    t = totals(rows)
    p = totals(prev_rows)

    # 每日
    by_day = defaultdict(list)
    for v in rows:
        by_day[v["t"].date()].append(v)

    days = []
    day = start.date()
    while day < end.date():
        items = by_day.get(day, [])
        days.append({
            "date": day,
            "visits": len(items),
            "pages": len({v["url"] for v in items}),
            "seconds": sum(v["eff"] for v in items),
            "items": sorted(items, key=lambda x: x["t"]),
        })
        day += dt.timedelta(days=1)

    # 时段 / 星期
    hours = [0] * 24
    for v in rows:
        hours[v["t"].hour] += 1

    # 年度按月汇总
    months = []
    if key == "year":
        for m in range(1, 13):
            items = [v for v in rows if v["t"].month == m]
            months.append({"month": m, "visits": len(items), "seconds": sum(v["eff"] for v in items)})

    # 会话
    sessions = []
    cur = None
    for v in sorted(rows, key=lambda x: x["t"]):
        if cur is None or (v["t"] - cur["end"]).total_seconds() > SESSION_GAP_SECONDS:
            if cur:
                sessions.append(cur)
            cur = {"start": v["t"], "end": v["t"] + dt.timedelta(seconds=v["eff"]),
                   "items": [v], "seconds": v["eff"]}
        else:
            cur["end"] = max(cur["end"], v["t"] + dt.timedelta(seconds=v["eff"]))
            cur["items"].append(v)
            cur["seconds"] += v["eff"]
    if cur:
        sessions.append(cur)
    for s in sessions:
        s["span"] = (s["end"] - s["start"]).total_seconds()
        s["sec"] = sum(v["eff"] for v in s["items"])
        s["uniq"] = len({v["url"] for v in s["items"]})
        s["domains"] = Counter(v["domain"] for v in s["items"])

    # 站点 / 页面 / 分类
    dom_stat = defaultdict(lambda: {"visits": 0, "seconds": 0.0, "cat": "其他"})
    for v in rows:
        d = dom_stat[v["domain"]]
        d["visits"] += 1
        d["seconds"] += v["eff"]
        d["cat"] = v["cat"]
    top_domains = sorted(dom_stat.items(), key=lambda kv: (-kv[1]["seconds"], -kv[1]["visits"]))[:15]

    page_stat = defaultdict(lambda: {"visits": 0, "seconds": 0.0, "last": None, "title": "", "cat": "其他", "domain": ""})
    for v in rows:
        s = page_stat[v["url"]]
        s["visits"] += 1
        s["seconds"] += v["eff"]
        s["last"] = v["t"] if s["last"] is None else max(s["last"], v["t"])
        s["title"] = v["title"]
        s["cat"] = v["cat"]
        s["domain"] = v["domain"]
    top_pages = sorted(page_stat.items(), key=lambda kv: (-kv[1]["seconds"], -kv[1]["visits"]))[:20]

    cat_stat = defaultdict(lambda: {"visits": 0, "seconds": 0.0})
    for v in rows:
        cat_stat[v["cat"]]["visits"] += 1
        cat_stat[v["cat"]]["seconds"] += v["eff"]
    cats = sorted(cat_stat.items(), key=lambda kv: -kv[1]["seconds"])
    # 占比过小的类目并进「其他」，避免环形图出现一堆看不见的细条
    total_cat = sum(c["seconds"] for _, c in cats) or 1
    small = [(n, c) for n, c in cats if n != "其他" and c["seconds"] / total_cat < 0.015]
    if len(cats) > 7 and small:
        merged = {"visits": 0, "seconds": 0.0}
        rest = []
        for n, c in cats:
            if n != "其他" and c["seconds"] / total_cat < 0.015:
                merged["visits"] += c["visits"]
                merged["seconds"] += c["seconds"]
            elif n == "其他":
                merged["visits"] += c["visits"]
                merged["seconds"] += c["seconds"]
            else:
                rest.append((n, c))
        rest.append(("其他", merged))
        cats = sorted(rest, key=lambda kv: -kv[1]["seconds"])

    # 书签
    bm_added = sorted([b for b in bookmarks if b["added"] and start <= b["added"] < end],
                      key=lambda b: b["added"], reverse=True)
    bm_folders = Counter(b["folder"] for b in bm_added)

    # 年度日历热力（整年铺满，无记录的日子留空）
    calendar = []
    if key == "year":
        d = start.date()
        while d < end.date():
            calendar.append((d, len(by_day.get(d, []))))
            d += dt.timedelta(days=1)

    # 数据覆盖天数（历史库只保留约 90 天）
    coverage = 0
    if rows:
        lo = min(v["t"].date() for v in rows)
        hi = max(v["t"].date() for v in rows)
        coverage = (hi - lo).days + 1

    # 洞察
    busiest = max(days, key=lambda x: x["visits"]) if days else None
    longest_session = max(sessions, key=lambda s: s["sec"]) if sessions else None
    longest_page = None
    if rows:
        longest_page = max(rows, key=lambda v: v["eff"])
    night = sum(1 for v in rows if v["t"].hour < 6)
    top_cat = next((c[0] for c in cats if c[0] != "其他"), "—")
    first_visit = min((v["t"] for v in rows), default=None)
    last_visit = max((v["t"] for v in rows), default=None)
    peak_hour = max(range(24), key=lambda h: hours[h]) if rows else None

    return {
        "key": key, "label": label, "start": start, "end": end, "now": now,
        "totals": t, "prev": p, "days": days, "hours": hours, "months": months,
        "coverage": coverage,
        "sessions": sessions, "top_domains": top_domains, "top_pages": top_pages,
        "cats": cats, "bm_added": bm_added, "bm_folders": bm_folders,
        "bm_total": len(bookmarks), "calendar": calendar,
        "busiest": busiest, "longest_session": longest_session, "longest_page": longest_page,
        "night": night, "top_cat": top_cat,
        "first_visit": first_visit, "last_visit": last_visit, "peak_hour": peak_hour,
        "rows": rows,
    }


# ---------------------------------------------------------------- rendering

def render_kpis(p) -> str:
    t, pr = p["totals"], p["prev"]
    avg_day = t["seconds"] / t["days"] if t["days"] else 0
    per_day = t["visits"] / t["days"] if t["days"] else 0
    is_day = p["key"] == "day"
    if is_day:
        longest = max((s["sec"] for s in p["sessions"]), default=0)
        peak = p["peak_hour"]
        peak_n = p["hours"][peak] if peak is not None else 0
        cards = [
            ("访问页面", f'{t["visits"]:,}', f'独立页面 {t["pages"]:,}', pct(t["visits"], pr["visits"])),
            ("有效阅读", human_seconds(t["seconds"]),
             f'最长单页 {human_seconds(p["longest_page"]["eff"]) if p["longest_page"] else "—"}',
             pct(t["seconds"], pr["seconds"])),
            ("站点数", f'{t["domains"]:,}',
             f'最忙 {peak:02d}:00 · {peak_n} 次' if peak is not None else "当天无访问",
             pct(t["domains"], pr["domains"])),
            ("连续浏览", f'{len(p["sessions"])}', f'最长 {human_seconds(longest)}', ""),
        ]
    else:
        cards = [
            ("访问页面", f'{t["visits"]:,}', f'独立页面 {t["pages"]:,}', pct(t["visits"], pr["visits"])),
            ("有效阅读", human_seconds(t["seconds"]), f'日均 {human_seconds(avg_day)}', pct(t["seconds"], pr["seconds"])),
            ("活跃天数", f'{t["days"]}', f'数据覆盖 {p["coverage"]} 天', ""),
            ("站点数", f'{t["domains"]:,}', f'日均 {per_day:.0f} 次访问', pct(t["domains"], pr["domains"])),
        ]
    tail_label = "较前一日" if is_day else "较上期"
    html = ['<div class="kpis">']
    for name, val, sub, delta in cards:
        tail = f' · {tail_label} {delta}' if delta and delta != "—" else ""
        html.append(
            f'<div class="kpi"><div class="kpi-name">{name}</div>'
            f'<div class="kpi-val">{val}</div>'
            f'<div class="kpi-sub">{sub}{tail}</div></div>'
        )
    html.append("</div>")
    return "".join(html)


def render_day_bars(p) -> str:
    days = p["days"]
    if not days:
        return '<p class="empty">本期暂无数据</p>'
    mx = max((d["visits"] for d in days), default=0) or 1
    many = len(days) > 12
    last_day = days[-1]["date"].day
    bars = []
    for d in days:
        h = max(2, round(d["visits"] / mx * 100))
        is_today = d["date"] == p["now"].date()
        label = f'<span>{d["visits"]}</span>' if d["visits"] else ""
        show_x = (not many) or d["date"].day in (1, last_day) or d["date"].day % 2 == 1
        xlab = str(d["date"].day) if show_x else ""
        bars.append(
            f'<div class="bar-col{" today" if is_today else ""}" title="{d["date"]} {WEEKDAYS[d["date"].weekday()]} · '
            f'{d["visits"]} 次 · {human_seconds(d["seconds"])}">'
            f'<div class="bar" style="height:{h}%">{label}</div>'
            f'<div class="bar-x">{xlab}</div></div>'
        )
    return f'<div class="bars height-160">{"".join(bars)}</div>'


def render_month_bars(p) -> str:
    months = p["months"]
    mx = max((m["visits"] for m in months), default=0) or 1
    bars = []
    for m in months:
        h = max(2, round(m["visits"] / mx * 100)) if m["visits"] else 2
        bars.append(
            f'<div class="bar-col{" today" if m["month"] == p["now"].month else ""}" '
            f'title="{m["month"]} 月 · {m["visits"]} 次 · {human_seconds(m["seconds"])}">'
            f'<div class="bar" style="height:{h}%"><span>{m["visits"] or ""}</span></div>'
            f'<div class="bar-x">{m["month"]}</div></div>'
        )
    return f'<div class="bars height-160">{"".join(bars)}</div>'


def render_hours(p) -> str:
    hours = p["hours"]
    mx = max(hours) or 1
    bars = []
    for i, n in enumerate(hours):
        h = round(n / mx * 100) if mx else 0
        hot = " hot" if n == mx and n else ""
        bars.append(
            f'<div class="hbar-col" title="{i:02d}:00–{i:02d}:59 · {n} 次">'
            f'<div class="hbar{hot}" style="height:{max(h,1)}%"></div>'
            f'<div class="hbar-x">{i if i % 3 == 0 else ""}</div></div>'
        )
    return f'<div class="hbars">{"".join(bars)}</div>'


def render_cats(p) -> str:
    cats = p["cats"]
    total = sum(c["seconds"] for _, c in cats) or 1
    stops, legend, acc = [], [], 0.0
    for name, c in cats:
        share = c["seconds"] / total * 100
        color = CAT_COLOR.get(name, "#94a3b8")
        stops.append(f"{color} {acc:.2f}% {acc + share:.2f}%")
        acc += share
        legend.append(
            f'<li><i style="background:{color}"></i><span>{escape(name)}</span>'
            f'<b>{share:.0f}%</b><em>{human_short(c["seconds"])}</em></li>'
        )
    if not stops:
        return '<p class="empty">本期暂无数据</p>'
    return (
        f'<div class="donut-wrap"><div class="donut" style="background:conic-gradient({", ".join(stops)})">'
        f'<div class="donut-hole"><b>{len(cats)}</b><span>类内容</span></div></div>'
        f'<ul class="legend">{"".join(legend)}</ul></div>'
    )


def render_domains(p) -> str:
    rows = p["top_domains"]
    if not rows:
        return '<p class="empty">本期暂无数据</p>'
    mx = max(v["seconds"] for _, v in rows) or 1
    html = ['<table class="tbl"><thead><tr><th>站点</th><th class="num">访问</th><th class="num">有效阅读</th><th class="bar-cell">占比</th></tr></thead><tbody>']
    for dom, v in rows:
        color = CAT_COLOR.get(v["cat"], "#94a3b8")
        html.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>{domain_link(dom)}'
            f'<span class="tag">{escape(v["cat"])}</span></td>'
            f'<td class="num">{v["visits"]:,}</td><td class="num">{human_short(v["seconds"])}</td>'
            f'<td class="bar-cell"><span class="mini-bar" style="width:{v["seconds"] / mx * 100:.1f}%;background:{color}"></span></td></tr>'
        )
    html.append("</tbody></table>")
    return "".join(html)


def render_pages(p, limit=12) -> str:
    rows = p["top_pages"][:limit]
    if not rows:
        return '<p class="empty">本期暂无数据</p>'
    html = ['<table class="tbl"><thead><tr><th>页面</th><th class="num">次数</th><th class="num">停留</th><th class="num">最后访问</th></tr></thead><tbody>']
    for url, v in rows:
        when = v["last"].strftime("%m-%d %H:%M:%S") if v["last"] else ""
        color = CAT_COLOR.get(v["cat"], "#94a3b8")
        html.append(
            f'<tr><td class="page-cell"><span class="dot" style="background:{color}"></span>'
            f'{page_link(url, v["title"])}'
            f'<div class="meta">{escape(v["domain"])} · {escape(v["cat"])}</div></td>'
            f'<td class="num">{v["visits"]}</td><td class="num">{human_short(v["seconds"])}</td>'
            f'<td class="num mono">{when}</td></tr>'
        )
    html.append("</tbody></table>")
    return "".join(html)


def render_gantt(p) -> str:
    days = [d for d in p["days"] if d["items"]]
    if not days:
        return '<p class="empty">本期暂无数据</p>'
    rows = []
    for d in days:
        blocks = []
        for v in d["items"]:
            start_min = v["t"].hour * 60 + v["t"].minute + v["t"].second / 60
            width = max(v["eff"], 20) / 60  # 至少 20 秒可见
            left = start_min / 1440 * 100
            w = min(width / 1440 * 100, 100 - left)
            color = CAT_COLOR.get(v["cat"], "#94a3b8")
            tip = f'{v["t"].strftime("%H:%M:%S")} · {v["title"][:60]} · {human_short(v["eff"])}'
            if is_web_url(v["url"]):
                block = (
                    f'<a class="gb" href="{escape(v["url"])}" target="_blank" '
                    f'rel="noopener noreferrer" title="{escape(tip)}" '
                    f'style="left:{left:.4f}%;width:{max(w, 0.06):.4f}%;background:{color}"></a>'
                )
            else:
                block = (
                    f'<span class="gb" title="{escape(tip)}" '
                    f'style="left:{left:.4f}%;width:{max(w, 0.06):.4f}%;background:{color}"></span>'
                )
            blocks.append(block)
        rows.append(
            f'<div class="grow"><div class="glabel">{d["date"].strftime("%m-%d")} {WEEKDAYS[d["date"].weekday()]}'
            f'<em>{len(d["items"])}</em></div><div class="gtrack">{"".join(blocks)}</div></div>'
        )
    axis = "".join(f'<span style="left:{i / 24 * 100:.4f}%">{i}</span>' for i in range(0, 25, 3))
    return f'<div class="gantt">{"".join(rows)}<div class="gaxis">{axis}</div></div>'


def render_day_details(p) -> str:
    days = [d for d in p["days"] if d["items"]]
    if not days:
        return '<p class="empty">本期暂无数据</p>'
    items = []
    for d in sorted(days, key=lambda x: x["date"], reverse=True):
        body = []
        limit = len(d["items"]) if p["key"] == "week" else 25
        for v in sorted(d["items"], key=lambda x: x["t"])[:limit]:
            color = CAT_COLOR.get(v["cat"], "#94a3b8")
            body.append(
                f'<tr><td class="mono">{v["t"].strftime("%H:%M:%S")}</td>'
                f'<td><span class="dot" style="background:{color}"></span>{page_link(v["url"], v["title"], "page-link subtle")}'
                f'<div class="meta">{escape(v["domain"])}</div></td>'
                f'<td class="num">{human_short(v["eff"])}</td></tr>'
            )
        more = ""
        if limit < len(d["items"]):
            more = f'<p class="more">另有 {len(d["items"]) - limit} 次访问未列出（按时间序取前 {limit} 条）</p>'
        items.append(
            f'<details class="day"><summary><b>{d["date"].strftime("%Y-%m-%d")}</b> '
            f'<span class="wd">{WEEKDAYS[d["date"].weekday()]}</span>'
            f'<span class="sum">{d["visits"]} 次 · {human_short(d["seconds"])}</span></summary>'
            f'<table class="tbl tight"><tbody>{"".join(body)}</tbody></table>{more}</details>'
        )
    return f'<div class="days">{"".join(items)}</div>'


def render_visit_list(p) -> str:
    """日报用：按小时折叠的完整访问列表，最近一小时默认展开。"""
    rows = sorted(p["rows"], key=lambda v: v["t"])
    if not rows:
        return '<p class="empty">这一天没有浏览记录</p>'
    by_hour = defaultdict(list)
    for v in rows:
        by_hour[v["t"].hour].append(v)
    hours = sorted(by_hour)
    groups = []
    for hour in hours:
        items = by_hour[hour]
        secs = sum(v["eff"] for v in items)
        body = []
        for v in items:
            color = CAT_COLOR.get(v["cat"], "#94a3b8")
            body.append(
                f'<tr><td class="mono">{v["t"]:%H:%M:%S}</td>'
                f'<td><span class="dot" style="background:{color}"></span>'
                f'{page_link(v["url"], v["title"], "page-link subtle")}'
                f'<div class="meta">{escape(v["domain"])}</div></td>'
                f'<td class="num">{escape(v["cat"])}</td>'
                f'<td class="num">{human_short(v["eff"])}</td></tr>'
            )
        groups.append(
            '<details class="day">'
            f'<summary><b>{hour:02d}:00–{hour:02d}:59</b>'
            f'<span class="sum">{len(items)} 次 · {human_short(secs)}</span></summary>'
            '<table class="tbl tight visit-list"><thead><tr><th>时间</th><th>页面</th>'
            f'<th class="num">类型</th><th class="num">停留</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
            '</details>'
        )
    return f'<div class="days">{"".join(groups)}</div>'


def render_calendar(p) -> str:
    cal = p["calendar"]
    if not cal:
        return ""
    counts = {d: n for d, n in cal}
    first = min(counts)
    last = max(counts)
    start = first - dt.timedelta(days=first.weekday())
    end = last + dt.timedelta(days=6 - last.weekday())
    weeks = (end - start).days // 7 + 1

    cells = []
    d = start
    while d <= end:
        if d < first or d > last:
            cells.append('<i class="cell out"></i>')
        else:
            n = counts.get(d, 0)
            lvl = heat_level(n)
            secs = sum(x["eff"] for x in p["day_map"].get(d, []))
            cells.append(
                f'<i class="cell l{lvl}" title="{d:%Y-%m-%d} {WEEKDAYS[d.weekday()]} · '
                f'{n} 次访问 · {human_seconds(secs)}"></i>'
            )
        d += dt.timedelta(days=1)

    # 月份标签：贴在当月第一周的那一列
    month_labels = []
    for m in range(1, 13):
        first_of_month = dt.date(first.year, m, 1)
        if first_of_month < first or first_of_month > last:
            continue
        col = (first_of_month - start).days // 7
        month_labels.append(f'<span style="grid-column:{col + 1}">{m} 月</span>')

    day_labels = ['<span></span>', '<span>周一</span>', '<span></span>',
                  '<span>周三</span>', '<span></span>', '<span>周五</span>', '<span></span>']
    total = sum(counts.values())

    return (
        '<div class="cal-scroll">'
        f'<div class="cal-months" style="grid-template-columns:repeat({weeks},11px)">'
        f'{"".join(month_labels)}</div>'
        '<div class="cal-body">'
        f'<div class="cal-days">{"".join(day_labels)}</div>'
        f'<div class="cal-grid" style="grid-template-columns:repeat({weeks},11px)">'
        f'{"".join(cells)}</div>'
        '</div></div>'
        '<div class="cal-foot">'
        f'<span>全年 {total:,} 次访问，{sum(1 for n in counts.values() if n)} 天有记录</span>'
        '<span class="cal-scale" title="按每天访问次数着色">'
        '<span class="cal-scale-end">少</span>'
        '<span class="cal-step"><i class="cell l0" title="无记录"></i>0</span>'
        '<span class="cal-step"><i class="cell l1" title="1–49 次/天"></i>1–49</span>'
        '<span class="cal-step"><i class="cell l2" title="50–149 次/天"></i>50–149</span>'
        '<span class="cal-step"><i class="cell l3" title="150–399 次/天"></i>150–399</span>'
        '<span class="cal-step"><i class="cell l4" title="400 次以上/天"></i>400+</span>'
        '<span class="cal-scale-end">多</span></span>'
        '</div>'
    )


def render_insights(p) -> str:
    t = p["totals"]
    bullets = []
    if p["key"] == "day":
        if t["visits"]:
            bullets.append(f'共打开 <b>{t["visits"]:,}</b> 个页面，涉及 <b>{t["domains"]}</b> 个站点，有效阅读 <b>{human_seconds(t["seconds"])}</b>。')
            bullets.append(f'第一次访问在 <b>{p["first_visit"]:%H:%M}</b>，最后一次在 <b>{p["last_visit"]:%H:%M}</b>。')
            peak = p["peak_hour"]
            bullets.append(f'最活跃的是 <b>{peak:02d}:00–{peak:02d}:59</b>，{p["hours"][peak]} 次访问。')
            if p["top_cat"] != "—":
                bullets.append(f'内容以 <b>{escape(p["top_cat"])}</b> 为主。')
            if p["longest_session"]:
                s = p["longest_session"]
                bullets.append(f'最长一次连续浏览从 <b>{s["start"]:%H:%M:%S}</b> 开始，有效阅读 <b>{human_seconds(s["sec"])}</b>，'
                               f'看了 {s["uniq"]} 个不同页面。')
            if p["longest_page"]:
                v = p["longest_page"]
                bullets.append(f'单页停留最久的是《<b>{escape(v["title"])}</b>》，{human_seconds(v["eff"])}。')
            bullets.append(f'凌晨 0–6 点的访问占 <b>{p["night"] / t["visits"] * 100:.0f}%</b>。')
    elif t["visits"]:
        bullets.append(f'共打开 <b>{t["visits"]:,}</b> 个页面，涉及 <b>{t["domains"]}</b> 个站点，有效阅读 <b>{human_seconds(t["seconds"])}</b>。')
        if p["top_cat"] != "—":
            bullets.append(f'内容以 <b>{escape(p["top_cat"])}</b> 为主。')
        if p["busiest"]:
            b = p["busiest"]
            bullets.append(f'最忙的一天是 <b>{b["date"]:%m 月 %d 日}（{WEEKDAYS[b["date"].weekday()]}）</b>，{b["visits"]} 次访问。')
        if p["longest_session"]:
            s = p["longest_session"]
            bullets.append(f'最长一次连续浏览从 <b>{s["start"]:%m-%d %H:%M:%S}</b> 开始，有效阅读 <b>{human_seconds(s["sec"])}</b>，'
                           f'看了 {s["uniq"]} 个不同页面。')
        if p["longest_page"]:
            v = p["longest_page"]
            bullets.append(f'单页停留最久的是《<b>{escape(v["title"])}</b>》，{human_seconds(v["eff"])}。')
        bullets.append(f'凌晨 0–6 点的访问占 <b>{p["night"] / t["visits"] * 100:.0f}%</b>。')
    if p["bm_added"]:
        bullets.append(f'当日新增书签 <b>{len(p["bm_added"])}</b> 个。' if p["key"] == "day"
                       else f'本期新增书签 <b>{len(p["bm_added"])}</b> 个。')
    if not bullets:
        return ""
    return f'<ul class="insights">{"".join(f"<li>{b}</li>" for b in bullets)}</ul>'


def render_bookmarks(p) -> str:
    added = p["bm_added"]
    if not added:
        return '<p class="empty">本期没有新增书签</p>'
    folders = "".join(
        f'<span class="chip">{escape(name)} <b>{n}</b></span>' for name, n in p["bm_folders"].most_common(6)
    )
    rows = []
    for b in added[:40]:
        rows.append(
            f'<tr><td class="mono">{b["added"].strftime("%m-%d %H:%M:%S")}</td>'
            f'<td>{page_link(b["url"], b["name"])}'
            f'<div class="meta">{escape(b["folder"])}</div></td></tr>'
        )
    return (
        f'<div class="chips">{folders}</div>'
        f'<table class="tbl tight"><tbody>{"".join(rows)}</tbody></table>'
        f'<p class="more">共 {len(added)} 个新增书签，显示最近 {min(40, len(added))} 个。全部书签 {p["bm_total"]} 个。</p>'
    )


def render_period(p) -> str:
    t = p["totals"]
    if p["key"] == "year":
        p["day_map"] = defaultdict(list)
        for v in p["rows"]:
            p["day_map"][v["t"].date()].append(v)

    avg_day = t["seconds"] / t["days"] if t["days"] else 0
    last_day = p["end"] - dt.timedelta(days=1)
    if p["key"] == "day":
        eyebrow = f'日报 · {p["start"]:%Y-%m-%d} {WEEKDAYS[p["start"].weekday()]}'
        lede = (f'有效阅读 {human_seconds(t["seconds"])}，涉及 {t["domains"]} 个站点，'
                f'{len(p["sessions"])} 次连续浏览。')
    else:
        eyebrow = f'{escape(p["label"])} · {p["start"]:%Y-%m-%d} — {last_day:%Y-%m-%d}'
        lede = f'有效阅读 {human_seconds(t["seconds"])}，日均 {human_seconds(avg_day)}，涉及 {t["domains"]} 个站点。'
    hero = (
        f'<p class="eyebrow">{eyebrow}</p>'
        f'<h1>{t["visits"]:,} 个页面</h1>'
        f'<p class="lede">{lede}</p>'
        + render_kpis(p)
    )
    parts = [f'<section id="p-{p["key"]}" class="period">',
             f'<div class="band hero"><div class="band-inner">{hero}</div></div>']

    if t["visits"] == 0:
        parts.append('<div class="band alt"><div class="band-inner">'
                     '<p class="empty big">这段时间没有浏览记录</p></div></div>')
        parts.append("</section>")
        return "".join(parts)

    alt = [True]

    def band(title, sub, inner):
        head = f'<h2 class="band-title">{title}</h2>'
        if sub:
            head += f'<p class="band-sub">{sub}</p>'
        cls = "band alt" if alt[0] else "band"
        alt[0] = not alt[0]
        return f'<div class="{cls}"><div class="band-inner">{head}{inner}</div></div>'

    if p["key"] == "year":
        parts.append(band("全年活跃日历", "每格一天。浅灰表示没有记录——Chrome 默认只保留约 90 天历史。",
                          render_calendar(p)))

    parts.append(band("这一天" if p["key"] == "day" else "这一期", "", render_insights(p)))

    if p["key"] == "day":
        parts.append(band(
            "节奏",
            "每一条色块是一次页面停留，悬停可看标题与时长；下面是 24 小时的访问分布。",
            '<div class="stack">'
            '<div class="card"><h3>全天时间轴</h3>' + render_gantt(p) + '</div>'
            '<div class="card"><h3>时段分布</h3>' + render_hours(p) + '</div>'
            '</div>',
        ))
    else:
        parts.append(band(
            "节奏",
            "每天打开多少页面，一天里什么时候最活跃。",
            '<div class="grid2">'
            f'<div class="card"><h3>{"每月访问" if p["key"] == "year" else "每日访问"}</h3>'
            + (render_month_bars(p) if p["key"] == "year" else render_day_bars(p)) +
            '</div><div class="card"><h3>时段分布</h3>' + render_hours(p) + "</div></div>",
        ))

    parts.append(band(
        "去处",
        "时间花在了哪些站点，以及内容都是什么类型。",
        '<div class="grid2"><div class="card"><h3>站点 Top 15</h3>' + render_domains(p) +
        '</div><div class="card"><h3>内容类型</h3>' + render_cats(p) + "</div></div>",
    ))

    parts.append(band("读得最久", "按有效阅读时长排序的页面。",
                      '<div class="card">' + render_pages(p, 20) + "</div>"))

    if p["key"] in ("week", "day"):
        if p["key"] == "week":
            parts.append(band("会话时间轴", "每行一天，色块是一次页面停留；间隔超过 20 分钟算新会话。",
                              '<div class="card">' + render_gantt(p) + "</div>"))

        sess = sorted(p["sessions"], key=lambda s: s["sec"], reverse=True)[:8]
        rows = []
        for s in sess:
            top = "、".join(domain_link(d) for d, _ in s["domains"].most_common(2))
            end_label = (s["end"].strftime("%H:%M:%S") if p["key"] == "day" or s["end"].date() == s["start"].date()
                         else s["end"].strftime("%m-%d %H:%M:%S"))
            start_label = (s["start"].strftime("%H:%M:%S") if p["key"] == "day"
                           else s["start"].strftime("%m-%d %H:%M:%S"))
            rows.append(
                f'<tr><td class="mono">{start_label}</td>'
                f'<td class="mono">{end_label}</td>'
                f'<td class="num">{human_short(s["sec"])}</td><td class="num">{s["uniq"]}</td>'
                f'<td>{top}</td></tr>'
            )
        parts.append(band(
            "最长的几次连续浏览" if p["key"] == "week" else "连续浏览",
            "" if p["key"] == "week" else "间隔超过 20 分钟算新会话，按有效阅读排序。",
            '<div class="card"><table class="tbl"><thead><tr><th>开始</th><th>结束</th>'
            '<th class="num">有效阅读</th><th class="num">页面</th><th>主要站点</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>",
        ))

    if p["key"] == "day":
        parts.append(band("逐次明细", "按小时折叠，点击展开；所有标题都可直达原网页。",
                          '<div class="card">' + render_visit_list(p) + "</div>"))
    else:
        parts.append(band("逐日明细", "时间精确到秒，点击某一天展开。",
                          '<div class="card">' + render_day_details(p) + "</div>"))

    if p["bm_added"] or p["key"] != "day":
        parts.append(band("新增书签", "", '<div class="card">' + render_bookmarks(p) + "</div>"))
    parts.append("</section>")
    return "".join(parts)


def render_history(pages, hist) -> str:
    """Better History 风格的历史浏览页：左侧日历 + 右侧逐次访问时间轴。"""
    domain_count = len({p[2] for p in pages})
    return f"""
<section id="p-history" class="period active">
<div class="hist">
  <aside class="hist-side">
    <div class="hist-side-head">
      <p class="eyebrow">浏览历史</p>
      <p class="hist-count"><b>{len(hist):,}</b><span>次访问</span></p>
      <p class="hist-side-meta">{len(pages):,} 个页面 · {domain_count:,} 个站点</p>
    </div>
    <div class="hist-cal">
      <div class="hist-cal-head">
        <button id="hcal-prev" type="button" aria-label="上个月">‹</button>
        <b id="hcal-title">—</b>
        <button id="hcal-next" type="button" aria-label="下个月">›</button>
      </div>
      <div class="hist-cal-week"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
      <div class="hist-cal-grid" id="hcal-grid"></div>
      <div class="hist-cal-foot">
        <span id="hcal-summary">—</span>
        <span class="hist-scale" title="按每天访问次数着色：{HEAT_LEGEND}" aria-label="颜色越深访问越多：{HEAT_LEGEND}">少<i class="hcal-lv l1" title="1–49 次/天"></i><i class="hcal-lv l2" title="50–149 次/天"></i><i class="hcal-lv l3" title="150–399 次/天"></i><i class="hcal-lv l4" title="400 次以上/天"></i>多</span>
      </div>
      <p class="hist-scale-note">{HEAT_LEGEND}</p>
    </div>
    <div class="hist-side-foot">
      <button id="hist-latest" type="button" class="hist-ghost">回到最近一天</button>
      <p>数据只存在这个 HTML 文件里，不联网、不上传。</p>
    </div>
  </aside>
  <div class="hist-main">
    <header class="hist-head">
      <div class="hist-head-text">
        <p class="eyebrow" id="hist-eyebrow">正在读取</p>
        <h1 id="hist-title">浏览历史</h1>
        <p class="hist-sub" id="hist-sub">正在整理本地索引…</p>
      </div>
      <div class="hist-tools">
        <div class="hist-daynav">
          <button id="hist-prev-day" type="button" aria-label="前一天">‹</button>
          <button id="hist-today" type="button">今天</button>
          <button id="hist-next-day" type="button" aria-label="后一天">›</button>
        </div>
        <label class="hist-date" for="hist-date" title="选择日期">
          <span id="hist-date-label">选择日期</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="3"></rect><path d="M8 3v4M16 3v4M4 10h16"></path></svg>
          <input type="date" id="hist-date" aria-label="选择日期">
        </label>
        <div class="hist-set-wrap">
          <button id="settings-btn" class="hist-icon" type="button" aria-label="显示设置" aria-expanded="false" aria-controls="settings-pop">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 7h9M19 7h1M4 17h2M10 17h10"></path><circle cx="16" cy="7" r="2.2"></circle><circle cx="8" cy="17" r="2.2"></circle></svg>
          </button>
          <div class="settings-pop" id="settings-pop" hidden>
            <div class="set-head"><b>显示设置</b><button id="settings-close" type="button" aria-label="关闭">×</button></div>
            <div class="set-row"><span>主题</span><div class="set-seg" data-set="theme">
              <button type="button" data-v="auto">自动</button><button type="button" data-v="light">浅色</button><button type="button" data-v="dark">深色</button>
            </div></div>
            <div class="set-row"><span>时间</span><div class="set-seg" data-set="clock">
              <button type="button" data-v="24">24 小时</button><button type="button" data-v="12">12 小时</button>
            </div></div>
            <div class="set-row"><span>密度</span><div class="set-seg" data-set="density">
              <button type="button" data-v="comfortable">舒适</button><button type="button" data-v="compact">紧凑</button>
            </div></div>
            <label class="set-toggle"><span>显示完整网址</span><input type="checkbox" data-set="showUrl"></label>
            <label class="set-toggle"><span>显示停留时长</span><input type="checkbox" data-set="showDur"></label>
            <p class="set-note">设置保存在本机浏览器，只影响这个报告。</p>
          </div>
        </div>
      </div>
    </header>
    <div class="hist-search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>
      <input id="hist-query" type="search" placeholder="搜索标题、网址或站点…" aria-label="搜索浏览历史" autocomplete="off" spellcheck="false">
      <button id="hist-clear" class="hist-clear" type="button" hidden>清除</button>
      <kbd>/</kbd>
    </div>
    <div class="hist-filters" id="hist-filters"></div>
    <div class="hist-list" id="hist-list" aria-live="polite">
      <p class="hist-empty">正在读取本地索引…</p>
      <noscript><p class="hist-empty">需要启用 JavaScript 才能显示浏览时间轴。</p></noscript>
    </div>
    <p class="hist-more" id="hist-more"></p>
  </div>
</div>
</section>"""


def _load_css() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.css")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


CSS = _load_css()

APP_JS = r"""
(function () {
  'use strict';
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => [...(el || document).querySelectorAll(s)];
  const pad = n => String(n).padStart(2, '0');
  const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const SETTINGS_KEY = 'browser-report.settings.v1';
  const HEAT = window.HEAT_STEPS || [[1, 49], [50, 149], [150, 399], [400, null]];
  function heatLevel(n) {
    if (!n) return 0;
    for (let i = 0; i < HEAT.length; i++) {
      const hi = HEAT[i][1];
      if (hi === null || n <= hi) return i + 1;
    }
    return HEAT.length;
  }

  const btns = $$('.seg button');
  const secs = $$('.period');
  const pageRoot = $('#p-history');
  const listEl = $('#hist-list');
  const filtersEl = $('#hist-filters');
  const moreEl = $('#hist-more');
  const q = $('#hist-query');
  const clearBtn = $('#hist-clear');
  const titleEl = $('#hist-title');
  const eyebrowEl = $('#hist-eyebrow');
  const subEl = $('#hist-sub');
  const hcalGrid = $('#hcal-grid');
  const hcalTitle = $('#hcal-title');
  const hcalPrev = $('#hcal-prev');
  const hcalNext = $('#hcal-next');
  const hcalSummary = $('#hcal-summary');
  const prevDayBtn = $('#hist-prev-day');
  const nextDayBtn = $('#hist-next-day');
  const todayBtn = $('#hist-today');
  const latestBtn = $('#hist-latest');
  const dateInput = $('#hist-date');
  const dateLabel = $('#hist-date-label');
  const pop = $('#settings-pop');
  const gear = $('#settings-btn');
  const closeSettingsBtn = $('#settings-close');

  if (!listEl || !pageRoot) return;

  /* ---------------- 设置 ---------------- */
  function loadSettings() {
    const def = { theme: 'auto', clock: '24', density: 'comfortable', showUrl: true, showDur: true };
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      if (saved && typeof saved === 'object') {
        for (const k of Object.keys(def)) {
          if (saved[k] !== undefined) def[k] = saved[k];
        }
      }
    } catch (e) { /* file:// 下禁用存储时继续用默认值 */ }
    return def;
  }
  function saveSettings() {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(S.settings)); } catch (e) { /* ignore */ }
  }
  function applySettings() {
    const html = document.documentElement;
    if (S.settings.theme === 'auto') html.removeAttribute('data-theme');
    else html.dataset.theme = S.settings.theme;
    pageRoot.classList.toggle('is-compact', S.settings.density === 'compact');
    pageRoot.classList.toggle('hide-url', !S.settings.showUrl);
    pageRoot.classList.toggle('hide-dur', !S.settings.showDur);
    $$('.set-seg').forEach(seg => {
      const key = seg.dataset.set;
      $$('button', seg).forEach(b => b.classList.toggle('active', String(S.settings[key]) === b.dataset.v));
    });
    $$('#settings-pop input[data-set]').forEach(el => { el.checked = !!S.settings[el.dataset.set]; });
  }
  function setSettingsOpen(open) {
    pop.hidden = !open;
    gear.setAttribute('aria-expanded', String(open));
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function hl(text, toks) {
    const s = String(text);
    if (!toks || !toks.length) return esc(s);
    const re = new RegExp('(' + toks.map(escRe).join('|') + ')', 'ig');
    return s.split(re).map(part => {
      if (!part) return '';
      const hit = toks.some(t => part.toLowerCase() === t.toLowerCase());
      return hit ? '<mark>' + esc(part) + '</mark>' : esc(part);
    }).join('');
  }
  function fmt(sec) {
    sec = Math.round(sec);
    if (!sec) return '';
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
    if (h) return h + 'h' + pad(m) + 'm';
    if (m) return m + 'm';
    return sec + 's';
  }
  function fmtLong(sec) {
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (h) return h + ' 小时' + (m ? ' ' + m + ' 分' : '');
    if (m) return m + ' 分' + (s ? ' ' + s + ' 秒' : '');
    return s + ' 秒';
  }
  function dkey(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function parseKey(k) {
    const p = k.split('-').map(Number);
    return new Date(p[0], p[1] - 1, p[2]);
  }
  function monthStart(k) {
    const d = parseKey(k);
    return new Date(d.getFullYear(), d.getMonth(), 1);
  }
  function addDays(k, n) {
    const d = parseKey(k);
    d.setDate(d.getDate() + n);
    return dkey(d);
  }
  function clock(d) {
    if (S.settings.clock === '12') {
      const h = d.getHours();
      return (h < 12 ? '上午 ' : '下午 ') + ((h % 12) || 12) + ':' + pad(d.getMinutes());
    }
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function relativeDay(k) {
    const now = new Date();
    if (k === dkey(now)) return '今天';
    const y = new Date();
    y.setDate(y.getDate() - 1);
    if (k === dkey(y)) return '昨天';
    const d = parseKey(k);
    return (d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + ' 年 ')
      + (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
  }
  function dayHeadLabel(k) {
    const d = parseKey(k), now = new Date();
    const md = (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
    if (k === dkey(now)) return '今天 · ' + md;
    const y = new Date(); y.setDate(y.getDate() - 1);
    if (k === dkey(y)) return '昨天 · ' + md;
    return (d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + ' 年 ')
      + md + ' · ' + WD[d.getDay()];
  }
  function todayKey() { return dkey(new Date()); }

  /* ---------------- 数据 ---------------- */
  const S = { day: null, month: null, query: '', cat: '', domain: '', settings: loadSettings() };
  const HAY = PAGES.map(p => (p[0] + ' ' + p[1] + ' ' + p[2]).toLowerCase());
  const DAYS = new Map();
  for (const h of HIST) {
    const k = dkey(new Date(h[0] * 1000));
    let st = DAYS.get(k);
    if (!st) { st = { count: 0, sec: 0, pages: new Set(), items: [] }; DAYS.set(k, st); }
    st.count++;
    st.sec += h[2];
    st.pages.add(h[1]);
    st.items.push(h);
  }
  const DAY_KEYS = [...DAYS.keys()].sort();
  const firstKey = DAY_KEYS[0] || todayKey();
  const lastKey = DAY_KEYS[DAY_KEYS.length - 1] || todayKey();
  const minMonth = monthStart(firstKey);
  const maxMonth = monthStart(lastKey);
  S.day = DAYS.has(todayKey()) ? todayKey() : lastKey;
  S.month = monthStart(S.day);

  /* ---------------- 标签页 ---------------- */
  let currentTab = 'history';
  function show(key, push, scroll) {
    if (!document.getElementById('p-' + key)) key = 'history';
    currentTab = key;
    btns.forEach(b => b.classList.toggle('active', b.dataset.t === key));
    secs.forEach(s => s.classList.toggle('active', s.id === 'p-' + key));
    if (push) history.replaceState(null, '', '#' + key);
    if (scroll) window.scrollTo({ top: 0, behavior: 'smooth' });
    if (key === 'history') renderHistory();
  }
  btns.forEach(b => b.addEventListener('click', () => show(b.dataset.t, true, true)));

  /* ---------------- 日历 ---------------- */
  function renderCalendar() {
    const y = S.month.getFullYear(), m = S.month.getMonth();
    const firstWeekday = (new Date(y, m, 1).getDay() + 6) % 7;
    const daysIn = new Date(y, m + 1, 0).getDate();
    let monthCount = 0, monthDays = 0;
    const counts = [];
    for (let d = 1; d <= daysIn; d++) {
      const st = DAYS.get(y + '-' + pad(m + 1) + '-' + pad(d));
      const c = st ? st.count : 0;
      counts.push(c);
      if (st) { monthCount += c; monthDays++; }
    }
    let cells = '';
    for (let i = 0; i < firstWeekday; i++) cells += '<i class="hcal-blank"></i>';
    for (let d = 1; d <= daysIn; d++) {
      const k = y + '-' + pad(m + 1) + '-' + pad(d);
      const st = DAYS.get(k);
      const lvl = st ? heatLevel(counts[d - 1]) : 0;
      let cls = 'hcal-day';
      if (k < firstKey || k > lastKey) cls += ' out';
      else if (!st) cls += ' none';
      else cls += ' lv' + lvl;
      if (k === S.day) cls += ' sel';
      if (k === todayKey()) cls += ' today';
      const label = k + ' ' + WD[new Date(y, m, d).getDay()] + ' · '
        + (st ? st.count + ' 次访问 · ' + fmtLong(st.sec) : '无记录');
      cells += '<button type="button" class="' + cls + '" data-day="' + k + '" title="' + label + '"'
        + ' aria-label="' + label + '"' + (k < firstKey || k > lastKey ? ' disabled' : '') + '>'
        + '<span>' + d + '</span></button>';
    }
    hcalGrid.innerHTML = cells;
    hcalTitle.textContent = y + ' 年 ' + (m + 1) + ' 月';
    hcalSummary.textContent = monthDays ? '本月 ' + monthCount.toLocaleString() + ' 次 · ' + monthDays + ' 天' : '本月无记录';
    hcalPrev.disabled = S.month.getTime() <= minMonth.getTime();
    hcalNext.disabled = S.month.getTime() >= maxMonth.getTime();
  }
  function shiftMonth(n) {
    const d = new Date(S.month.getFullYear(), S.month.getMonth() + n, 1);
    if (d < minMonth || d > maxMonth) return;
    S.month = d;
    renderCalendar();
  }

  /* ---------------- 筛选 / 列表 ---------------- */
  function queryTokens() {
    return S.query.toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  }
  function baseItems() {
    if (S.query) {
      const toks = queryTokens();
      const hits = [];
      for (let i = 0; i < HIST.length; i++) {
        const h = HIST[i];
        const hay = HAY[h[1]];
        if (toks.every(t => hay.indexOf(t) !== -1)) hits.push(h);
      }
      return hits.reverse();
    }
    const st = DAYS.get(S.day);
    return st ? st.items.slice().reverse() : [];
  }
  function rowHTML(h, toks) {
    const p = PAGES[h[1]];
    const dt = new Date(h[0] * 1000);
    const color = (window.CAT_COLORS && window.CAT_COLORS[p[3]]) || '#8e8e93';
    const letter = (p[2].replace(/^www\./, '')[0] || '?').toUpperCase();
    return '<article class="hist-row">'
      + '<div class="hist-when"><b>' + clock(dt) + '</b>' + (h[2] > 0 ? '<em>' + fmt(h[2]) + '</em>' : '') + '</div>'
      + '<div class="hist-line"><i></i></div>'
      + '<a class="hist-card" href="' + esc(p[1]) + '" target="_blank" rel="noopener noreferrer" title="' + esc(p[0]) + '">'
      + '<span class="hist-fav" style="background:' + color + ';color:' + textOn(color) + '" aria-hidden="true">' + esc(letter) + '</span>'
      + '<span class="hist-info">'
      + '<span class="hist-title">' + hl(p[0], toks) + '</span>'
      + '<span class="hist-meta">' + hl(p[2], toks) + '<span class="hist-cat">' + esc(p[3]) + '</span></span>'
      + '<span class="hist-url">' + hl(p[1], toks) + '</span>'
      + '</span><span class="hist-go" aria-hidden="true">↗</span></a></article>';
  }
  function textOn(hex) {
    const c = hex.replace('#', '');
    const r = parseInt(c.slice(0, 2), 16), g = parseInt(c.slice(2, 4), 16), b = parseInt(c.slice(4, 6), 16);
    return ((r * 299 + g * 587 + b * 114) / 1000) > 160 ? '#1d1d1f' : '#ffffff';
  }
  function renderHeader(base, items, shown) {
    if (S.query) {
      const pages = new Set(items.map(h => h[1])).size;
      eyebrowEl.textContent = (S.cat || S.domain) ? '全部历史 · 已筛选' : '全部历史 · 搜索结果';
      titleEl.textContent = '搜索结果';
      let sub = '“' + S.query + '”匹配 ' + items.length.toLocaleString() + ' 次访问 · ' + pages.toLocaleString() + ' 个页面';
      if (S.cat || S.domain) sub += '（筛选后）';
      if (items.length > shown) sub += ' · 显示最近 ' + shown.toLocaleString() + ' 条';
      subEl.textContent = sub;
      return;
    }
    const d = parseKey(S.day), st = DAYS.get(S.day);
    eyebrowEl.textContent = d.getFullYear() + ' 年 ' + (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日 · ' + WD[d.getDay()];
    titleEl.textContent = relativeDay(S.day);
    if (!st) {
      subEl.textContent = '这一天没有浏览记录。Chrome 默认只保留约 90 天历史。';
      return;
    }
    let sub = st.count.toLocaleString() + ' 次访问 · ' + st.pages.size.toLocaleString() + ' 个页面 · 有效阅读 ' + fmtLong(st.sec);
    if (S.cat || S.domain) sub += ' · 当前筛选 ' + items.length.toLocaleString() + ' 次';
    subEl.textContent = sub;
  }
  function renderFilters(base) {
    if (!base.length) { filtersEl.innerHTML = ''; return; }
    const cats = new Map(), doms = new Map();
    for (const h of base) {
      const p = PAGES[h[1]];
      cats.set(p[3], (cats.get(p[3]) || 0) + 1);
      doms.set(p[2], (doms.get(p[2]) || 0) + 1);
    }
    let html = '<div class="hist-chips">';
    html += '<button type="button" class="hist-chip' + (S.cat ? '' : ' active') + '" data-cat="">全部 <b>' + base.length + '</b></button>';
    for (const [c, n] of [...cats.entries()].sort((a, b) => b[1] - a[1])) {
      html += '<button type="button" class="hist-chip' + (S.cat === c ? ' active' : '') + '" data-cat="' + esc(c) + '">' + esc(c) + ' <b>' + n + '</b></button>';
    }
    html += '</div><select class="hist-select" id="hist-domain" aria-label="按站点筛选"><option value="">全部站点</option>';
    for (const [d, n] of [...doms.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
      html += '<option value="' + esc(d) + '"' + (S.domain === d ? ' selected' : '') + '>' + esc(d) + ' · ' + n + '</option>';
    }
    html += '</select>';
    filtersEl.innerHTML = html;
    $$('#hist-filters [data-cat]').forEach(b => b.addEventListener('click', () => {
      S.cat = b.dataset.cat;
      renderHistory();
    }));
    const sel = $('#hist-domain');
    if (sel) sel.addEventListener('change', () => { S.domain = sel.value; renderHistory(); });
  }
  function renderTimeline(items) {
    if (!items.length) {
      const msg = S.query ? '没有找到相关记录' : '这一天没有浏览记录';
      const tip = (S.cat || S.domain) ? '当前筛选没有结果，试试“全部”。' : (S.query ? '换个关键词，或试试站点名、网址片段。' : 'Chrome 默认只保留约 90 天历史。');
      listEl.innerHTML = '<div class="hist-empty"><b>' + msg + '</b><span>' + tip + '</span></div>';
      return;
    }
    const toks = S.query ? queryTokens() : [];
    const groups = new Map();
    for (const h of items) {
      const k = dkey(new Date(h[0] * 1000));
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(h);
    }
    let out = '';
    for (const [k, list] of groups) {
      out += '<div class="hist-group">';
      if (S.query) out += '<div class="hist-dayhead"><b>' + dayHeadLabel(k) + '</b><span>' + list.length + ' 条匹配</span></div>';
      for (const h of list) out += rowHTML(h, toks);
      out += '</div>';
    }
    listEl.innerHTML = out;
  }
  function renderHistory() {
    renderCalendar();
    dateInput.min = firstKey;
    dateInput.max = lastKey;
    dateInput.value = S.day;
    const dd = parseKey(S.day);
    dateLabel.textContent = (dd.getMonth() + 1) + ' 月 ' + dd.getDate() + ' 日';
    prevDayBtn.disabled = S.day <= firstKey;
    nextDayBtn.disabled = S.day >= lastKey;

    const base = baseItems();
    const cats = new Set(base.map(h => PAGES[h[1]][3]));
    const doms = new Set(base.map(h => PAGES[h[1]][2]));
    if (S.cat && !cats.has(S.cat)) S.cat = '';
    if (S.domain && !doms.has(S.domain)) S.domain = '';
    const items = base.filter(h => {
      const p = PAGES[h[1]];
      return (!S.cat || p[3] === S.cat) && (!S.domain || p[2] === S.domain);
    });
    const limit = S.query ? 300 : 600;
    const shown = items.slice(0, limit);
    renderHeader(base, items, shown.length);
    renderFilters(base);
    renderTimeline(shown);
    moreEl.textContent = items.length > shown.length
      ? '共 ' + items.length.toLocaleString() + ' 条，显示最近 ' + shown.length.toLocaleString() + ' 条；缩小关键词范围可以继续往前看。'
      : '';
  }

  /* ---------------- 交互 ---------------- */
  function goDay(k, scroll) {
    if (!k || k < firstKey || k > lastKey) return;
    S.day = k;
    S.month = monthStart(k);
    S.query = '';
    S.cat = '';
    S.domain = '';
    q.value = '';
    clearBtn.hidden = true;
    renderHistory();
    if (scroll) window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  hcalPrev.addEventListener('click', () => shiftMonth(-1));
  hcalNext.addEventListener('click', () => shiftMonth(1));
  hcalGrid.addEventListener('click', e => {
    const b = e.target.closest('[data-day]');
    if (b && !b.disabled) goDay(b.dataset.day, true);
  });
  prevDayBtn.addEventListener('click', () => goDay(addDays(S.day, -1), true));
  nextDayBtn.addEventListener('click', () => goDay(addDays(S.day, 1), true));
  todayBtn.addEventListener('click', () => goDay(DAYS.has(todayKey()) ? todayKey() : lastKey, true));
  latestBtn.addEventListener('click', () => goDay(lastKey, true));
  dateInput.addEventListener('change', () => {
    if (dateInput.value) goDay(dateInput.value, true);
    else dateInput.value = S.day;
  });

  let searchTimer = 0;
  q.addEventListener('input', () => {
    clearBtn.hidden = !q.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      S.query = q.value.trim();
      S.cat = '';
      S.domain = '';
      if (S.query && currentTab !== 'history') show('history', true, false);
      renderHistory();
    }, 90);
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      q.value = '';
      S.query = '';
      S.cat = '';
      S.domain = '';
      clearBtn.hidden = true;
      q.blur();
      renderHistory();
    }
  });
  clearBtn.addEventListener('click', () => {
    q.value = '';
    S.query = '';
    S.cat = '';
    S.domain = '';
    clearBtn.hidden = true;
    q.focus();
    renderHistory();
  });
  document.addEventListener('keydown', e => {
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
      e.preventDefault();
      if (currentTab !== 'history') show('history', true, false);
      q.focus();
    } else if (e.key === 'Escape' && !pop.hidden) {
      setSettingsOpen(false);
    }
  });

  gear.addEventListener('click', e => {
    e.stopPropagation();
    setSettingsOpen(pop.hidden);
  });
  closeSettingsBtn.addEventListener('click', () => setSettingsOpen(false));
  document.addEventListener('click', e => {
    if (!pop.hidden && !pop.contains(e.target) && !gear.contains(e.target)) setSettingsOpen(false);
  });
  pop.addEventListener('click', e => {
    const b = e.target.closest('.set-seg button');
    if (!b) return;
    S.settings[b.closest('.set-seg').dataset.set] = b.dataset.v;
    applySettings();
    saveSettings();
    renderHistory();
  });
  pop.addEventListener('change', e => {
    const cb = e.target.closest('input[data-set]');
    if (!cb) return;
    S.settings[cb.dataset.set] = cb.checked;
    applySettings();
    saveSettings();
    renderHistory();
  });

  /* ---------------- 启动 ---------------- */
  applySettings();
  const initial = location.hash.slice(1);
  show(initial && document.getElementById('p-' + initial) ? initial : 'history', false, false);
})();
"""


def build_html(periods, generated_at, source_note, pages, hist) -> str:
    tab_labels = {"day": "日报", "week": "周报", "month": "月报", "year": "年报"}
    tabs = '<button data-t="history" class="active">历史</button>' + "".join(
        f'<button data-t="{p["key"]}">{tab_labels.get(p["key"], escape(p["label"]))}</button>'
        for p in periods
    )
    bodies = render_history(pages, hist) + "".join(render_period(p) for p in periods)
    def js_json(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    pages_json = js_json(pages)
    hist_json = js_json(hist)
    colors_json = js_json(CAT_COLOR)
    heat_json = js_json([[lo, hi] for lo, hi in HEAT_STEPS])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浏览记录 · {generated_at:%Y-%m-%d}</title>
<script>try{{var s=JSON.parse(localStorage.getItem('browser-report.settings.v1')||'{{}}');if(s.theme&&s.theme!=='auto')document.documentElement.dataset.theme=s.theme}}catch(e){{}}</script>
<style>{CSS}</style></head>
<body>
<header class="topbar"><div class="topbar-inner">
  <div class="brand">浏览报告<small>离线 · 生成于 {generated_at:%Y-%m-%d %H:%M}</small></div>
  <nav class="seg" aria-label="切换视图">{tabs}</nav>
</div></header>
{bodies}
<footer>{escape(source_note)}<br>历史索引 {len(hist):,} 次可跳转访问 / {len(pages):,} 个页面 · 有效阅读 = min(记录时长, 到下一次访问的间隔, {MAX_VISIT_SECONDS // 60} 分钟)；会话间隔阈值 {SESSION_GAP_SECONDS // 60} 分钟。</footer>
<script>window.PAGES={pages_json};window.HIST={hist_json};window.CAT_COLORS={colors_json};window.HEAT_STEPS={heat_json};</script>
<script>{APP_JS}</script>
</body></html>"""


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="Default", help="Chrome 配置文件目录名，如 Default、Profile 1")
    ap.add_argument("--user-data", default=default_user_data(),
                    help="Chrome User Data 目录；默认按当前系统自动选择")
    ap.add_argument("--anchor", default=None, help="基准日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(here, "dist", "browser-report.html"))
    ap.add_argument("--open", action="store_true", help="生成后打开报告")
    args = ap.parse_args()

    def stage(msg):
        print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)

    profile_dir = os.path.join(args.user_data, args.profile)
    stage(f"读取 {profile_dir}")
    db, tmp = snapshot_history(profile_dir)
    visits = load_visits(db)
    bookmarks = load_bookmarks(profile_dir)
    tmp.cleanup()
    stage(f"载入 {len(visits):,} 条访问 / {len(bookmarks)} 个书签")
    if not visits:
        raise SystemExit("历史库为空")

    now = dt.datetime.now(LOCAL_TZ)
    anchor = dt.datetime.strptime(args.anchor, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ) if args.anchor else now
    day0 = anchor.replace(hour=0, minute=0, second=0, microsecond=0)

    # 日报
    day = build_period("day", f"{day0.month} 月 {day0.day} 日", day0, day0 + dt.timedelta(days=1),
                       visits, bookmarks, day0 - dt.timedelta(days=1), day0, now)
    # 本周
    monday = day0 - dt.timedelta(days=day0.weekday())
    week = build_period("week", "本周", monday, monday + dt.timedelta(days=7),
                        visits, bookmarks, monday - dt.timedelta(days=7), monday, now)
    # 本月
    month_start = day0.replace(day=1)
    next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
    prev_month = (month_start - dt.timedelta(days=1)).replace(day=1)
    month = build_period("month", f"{month_start.month} 月", month_start, next_month,
                         visits, bookmarks, prev_month, month_start, now)
    # 年度
    year = args.year or day0.year
    y_start = dt.datetime(year, 1, 1, tzinfo=LOCAL_TZ)
    y_end = dt.datetime(year + 1, 1, 1, tzinfo=LOCAL_TZ)
    annual = build_period("year", f"{year} 年度", y_start, y_end,
                          visits, bookmarks, y_start - (y_end - y_start), y_start, now)

    periods = [day, week, month, annual]
    stage("聚合完成")
    pages, hist = build_history_data(visits)
    stage(f"历史索引 {len(hist):,} 次访问 / {len(pages):,} 个页面")
    covered = f"{visits[0]['t']:%Y-%m-%d} 至 {visits[-1]['t']:%Y-%m-%d}"
    source_note = (f"Chrome · {args.profile} · {len(visits):,} 条访问 / {len(bookmarks)} 个书签 · "
                   f"历史覆盖 {covered}（Chrome 默认只保留约 90 天）")
    html = build_html(periods, now, source_note, pages, hist)
    stage(f"HTML 生成 {len(html) / 1024 / 1024:.2f} MB")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    stage("写入完成")
    size = os.path.getsize(args.out) / 1024 / 1024
    print(f"OK  {args.out}  ({size:.2f} MB)")
    print(f"日报 {day['totals']['visits']:,} 次 · {human_seconds(day['totals']['seconds'])}")
    print(f"本周 {week['totals']['visits']:,} 次 · {human_seconds(week['totals']['seconds'])}")
    print(f"{month['label']} {month['totals']['visits']:,} 次 · {human_seconds(month['totals']['seconds'])}")
    print(f"{year} 年度 {annual['totals']['visits']:,} 次 · {human_seconds(annual['totals']['seconds'])}")
    if args.open:
        webbrowser.open(Path(args.out).resolve().as_uri())


if __name__ == "__main__":
    main()
