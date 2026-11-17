#!/usr/bin/python3
"""
AI Builder 日报

每天把 X 上一线 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文整理成一份
播报，**每条话题都带原推直链**，由 AI 定时任务写稿后推送到飞书并归档至 Notion。本脚本只负责取材与推送，
不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch    ：优先通过 twscrape 直连抓取自建 X List（配置 X_LIST_ID 后彻底脱离上游定时依赖）；
             未配置 X_LIST_ID 时临时回退 follow-builders 公共 feed。落盘 logs/last_context.txt
- send     ：读取写好的稿子文件 → 清洗 HTML → 推送飞书 + 同步 Notion + 写日志
- test-list：只读测试 X List 与 Cookie 连通性，不写 run.log 或 last_context.txt

写稿规范在同目录 prompt.md，由 Antigravity Sidecar 定时任务读取。
"""

import argparse
import asyncio
import html
import json
import os
import plistlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (already_ran_today, load_sent_urls, url_key,
                       fetch_rss, parse_entry_date,
                       make_logger, make_pending_saver, proxy_ok,
                       emit_fetch_output, send_x_linked_report)

import requests

BASE = Path(__file__).parent
LOG_FILE = BASE / "logs" / "run.log"
JSONL_FILE = BASE / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE = BASE / "pending_messages.json"
DRAFT_FILE = BASE / "logs" / "report_draft.txt"
FETCH_META = BASE / "logs" / "fetch_meta.json"
LAST_CONTEXT = BASE / "logs" / "last_context.txt"
SENT_URLS = BASE / "logs" / "sent_urls.json"
TWSCRAPE_DB = BASE / "logs" / "twscrape_accounts.db"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.shirley.ai-builder-bot.plist"

FEED_URL = os.getenv(
    "BUILDER_FEED_URL",
    "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json")
FEED_BLOGS_URL = os.getenv(
    "BUILDER_BLOGS_URL",
    "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-blogs.json")
FEED_STALE_H = int(os.getenv("BUILDER_FEED_STALE_H", "36"))
PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", "4"))
WINDOW_H = int(os.getenv("BUILDER_WINDOW_H", "24"))
LIST_SCAN_LIMIT = int(os.getenv("BUILDER_LIST_SCAN_LIMIT", "200"))
INCLUDE_REPLIES = os.getenv("BUILDER_INCLUDE_REPLIES", "0") == "1"

# 回退 follow-builders 时剔除的 8 个非一线个人账号
EXCLUDED_HANDLES = {
    "claudeai", "googlelabs",
    "mattturck", "adityaag", "nikunj",
    "zarazhangrui", "petergyang", "danshipper",
}

# 回退 follow-builders 时附带的个人技术博客直连 RSS（48h 窗口）
BUILDER_BLOG_FEEDS = [
    ("Simon Willison (@simonw)", "https://simonwillison.net/atom/everything/", 4),
    ("Lilian Weng (@lilianweng)", "https://lilianweng.github.io/index.xml", 2),
    ("Hamel Husain (@HamelHusain)", "https://hamel.dev/index.xml", 2),
    ("Armin Ronacher (@mitsuhiko)", "https://lucumr.pocoo.org/feed.atom", 2),
    ("Eugene Yan (@eugeneyan)", "https://eugeneyan.com/rss/", 2),
]

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

_SESSION = requests.Session()
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _proxy_ok() -> bool:
    """代理预检 + 端口自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, _SESSION)
    if ok and _PROXY:
        os.environ["HTTP_PROXY"] = _PROXY
        os.environ["HTTPS_PROXY"] = _PROXY
    return ok


def _load_plist_env_if_needed() -> None:
    """直接运行 test-list 时从 LaunchAgent plist 补全环境变量（不覆盖已存在变量）。"""
    global _PROXY, FEISHU_WEBHOOK, WINDOW_H, LIST_SCAN_LIMIT, INCLUDE_REPLIES
    if not PLIST_PATH.exists():
        return
    try:
        with PLIST_PATH.open("rb") as fh:
            data = plistlib.load(fh)
        env_map = data.get("EnvironmentVariables") or {}
        for k, v in env_map.items():
            if isinstance(k, str) and v is not None and k not in os.environ:
                os.environ[k] = str(v)
        _PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or _PROXY
        FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", FEISHU_WEBHOOK)
        WINDOW_H = int(os.getenv("BUILDER_WINDOW_H", str(WINDOW_H)))
        LIST_SCAN_LIMIT = int(os.getenv("BUILDER_LIST_SCAN_LIMIT", str(LIST_SCAN_LIMIT)))
        INCLUDE_REPLIES = os.getenv("BUILDER_INCLUDE_REPLIES", "0") == "1"
    except Exception as exc:
        print(f"[WARN] 读取 plist 环境变量失败: {exc}", file=sys.stderr)


def _iso(s: str):
    """ISO8601 时间字符串转 aware datetime；解析失败返回 None。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _plain_text(fragment: str, limit: int = 0) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] if limit else text


def _parse_list_id(raw: str) -> int | None:
    """支持纯数字 List ID 或完整 URL（https://x.com/i/lists/<id>）。"""
    val = (raw or "").strip()
    if not val:
        return None
    if val.isdigit():
        return int(val)
    m = re.search(r"/lists/(\d+)", val)
    return int(m.group(1)) if m else None


def _cookie_dict_from_env() -> dict[str, str]:
    """从 X_COOKIES 或 X_AUTH_TOKEN / X_CT0 解析 Cookie。"""
    raw = (os.getenv("X_COOKIES") or "").strip()
    parsed: dict[str, str] = {}
    if raw:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                parsed[k.strip()] = v.strip()
    auth_token = (os.getenv("X_AUTH_TOKEN") or "").strip()
    ct0 = (os.getenv("X_CT0") or "").strip()
    if auth_token:
        parsed["auth_token"] = auth_token
    if ct0:
        parsed["ct0"] = ct0
    return parsed


def _format_tweet_text(t) -> str:
    """展开 t.co 链接，并补充 X Article 标题与引用推文正文。"""
    text = (t.rawContent or "").strip()
    for lk in getattr(t, "links", None) or []:
        tco = getattr(lk, "tcourl", "") or ""
        dest = getattr(lk, "url", "") or ""
        if tco and dest and tco in text:
            text = text.replace(tco, dest)

    article = getattr(t, "article", None)
    if article and getattr(article, "title", ""):
        text = f"[X Article: {article.title}] {text}".strip()

    qt = getattr(t, "quotedTweet", None)
    if qt is not None:
        q_user = getattr(getattr(qt, "user", None), "username", "") or "unknown"
        q_url = getattr(qt, "url", "") or ""
        q_text = (getattr(qt, "rawContent", "") or "").replace("\n", " ").strip()[:240]
        if q_text or q_url:
            text = f"{text}\n[引用 @{q_user} ({q_url})]: {q_text}".strip()
    return text


async def _safe_get_account(pool, account_name: str):
    try:
        return await pool.get(account_name)
    except ValueError:
        return None


async def _ensure_twscrape_account(pool, account_name: str, cookie_map: dict[str, str]) -> None:
    """将环境变量 Cookie 同步进 SQLite 账号池，或复用已有活跃账号。"""
    existing = await _safe_get_account(pool, account_name)
    has_env = bool(cookie_map.get("auth_token") and cookie_map.get("ct0"))
    if has_env:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
        stored = getattr(existing, "cookies", {}) or {}
        if (
            existing is None
            or not existing.active
            or stored.get("auth_token") != cookie_map["auth_token"]
            or stored.get("ct0") != cookie_map["ct0"]
        ):
            await pool.add_account_cookies(account_name, cookie_str)
        return

    accounts = await pool.accounts_info()
    if any(a.get("active") for a in accounts):
        return
    raise RuntimeError(
        "未检测到可用 X Cookie：请在 ~/Library/LaunchAgents/com.shirley.ai-builder-bot.plist "
        "配置 X_AUTH_TOKEN 与 X_CT0（或 X_COOKIES）"
    )


async def _fetch_x_list_async(list_id: int, sent_before: set) -> tuple[list, dict]:
    """通过 twscrape 抓取自建 X List 过去 WINDOW_H 小时内的推文。"""
    from twscrape import API, AccountsPool

    account_name = (os.getenv("X_ACCOUNT_NAME") or "builder_alt").strip() or "builder_alt"
    cookie_map = _cookie_dict_from_env()

    pool = AccountsPool(db_file=str(TWSCRAPE_DB), raise_when_no_account=True)
    await _ensure_twscrape_account(pool, account_name, cookie_map)
    api = API(pool=pool, proxy=_PROXY)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_H)

    by_author: dict[str, list[dict]] = {}
    seen_in_run: set[str] = set()
    dropped_rt = dropped_reply = dropped_old = dropped_dup = 0
    old_streak = 0
    scanned = 0

    async for t in api.list_timeline(list_id, limit=LIST_SCAN_LIMIT):
        scanned += 1
        t_date = t.date if t.date.tzinfo else t.date.replace(tzinfo=timezone.utc)
        if t_date < cutoff:
            dropped_old += 1
            old_streak += 1
            if old_streak >= 8:
                break
            continue
        old_streak = 0

        if t.retweetedTweet is not None or t.retweetedTweetId is not None:
            dropped_rt += 1
            continue

        user = t.user
        handle = (getattr(user, "username", "") or "").strip()
        if not handle:
            continue

        # 默认跳过对他人推文的零散回复，保留主动发推与本人自串（self-thread）
        if not INCLUDE_REPLIES and t.inReplyToTweetId is not None:
            reply_user = (
                getattr(getattr(t, "inReplyToUser", None), "username", None)
                or getattr(t, "inReplyToScreenName", None)
                or ""
            )
            if reply_user.lower() != handle.lower():
                dropped_reply += 1
                continue

        url = t.url or f"https://x.com/{handle}/status/{t.id_str}"
        uk = url_key(url)
        if uk in sent_before or uk in seen_in_run:
            dropped_dup += 1
            continue
        seen_in_run.add(uk)

        by_author.setdefault(handle, []).append({
            "handle": handle,
            "name": (getattr(user, "displayname", "") or handle).strip(),
            "bio": (getattr(user, "rawDescription", "") or "").replace("\n", " ").strip()[:80],
            "url": url,
            "text": _format_tweet_text(t),
            "createdAt": t_date.astimezone(timezone.utc).isoformat(),
            "likes": t.likeCount or 0,
            "retweets": t.retweetCount or 0,
            "replies": t.replyCount or 0,
            "isQuote": bool(t.isQuoteStatus or t.quotedTweet is not None),
        })

    # 检查账号是否因 401/风控被 twscrape 标记失效
    acc = await _safe_get_account(pool, account_name)
    if acc is not None and not acc.active:
        err = acc.error_msg or "Cookie 可能已过期或账号受限"
        raise RuntimeError(f"X 账号 {account_name} 已失效（{err}），请更新 X_AUTH_TOKEN / X_CT0")

    rows: list[dict] = []
    dropped_cap = 0
    for handle_rows in by_author.values():
        handle_rows.sort(key=lambda x: -x["likes"])
        rows.extend(handle_rows[:PER_BUILDER_CAP])
        if len(handle_rows) > PER_BUILDER_CAP:
            dropped_cap += len(handle_rows) - PER_BUILDER_CAP

    rows.sort(key=lambda x: -x["likes"])
    stats = {
        "list_id": list_id,
        "scanned": scanned,
        "builders": len(by_author),
        "dropped_rt": dropped_rt,
        "dropped_reply": dropped_reply,
        "dropped_old": dropped_old,
        "dropped_dup": dropped_dup,
        "dropped_cap": dropped_cap,
        "fetched_at": now,
    }
    return rows, stats


def _format_context(
    rows: list[dict],
    header_lines: list[str],
    blog_rows: list[dict] | None = None,
) -> str:
    """统一格式化输出给写稿模型的素材上下文。"""
    times = [_iso(x["createdAt"]) for x in rows]
    times = [t for t in times if t]
    span = (
        f"{min(times).strftime('%Y-%m-%d %H:%M')} ~ "
        f"{max(times).strftime('%Y-%m-%d %H:%M')} UTC"
    ) if times else "无"

    lines = list(header_lines)
    lines.insert(1, f"【推文实际时间区间】{span}")
    lines += [
        "",
        "推文的 赞/转/回 是 X 上的真实互动量，可以照实写。",
        "",
    ]

    for i, x in enumerate(rows, 1):
        when = _iso(x["createdAt"])
        when_s = when.strftime("%m-%d %H:%M UTC") if when else x["createdAt"]
        lines.append(f"--- {i}. @{x['handle']}（{x['name']}｜{x['bio']}）")
        lines.append(
            f"[时间] {when_s}"
            f"　[赞] {x['likes']}　[转] {x['retweets']}　[回] {x['replies']}"
            + ("　[引用推]" if x["isQuote"] else "")
        )
        lines.append(f"[原推] {x['url']}")
        lines.append(f"[正文] {x['text']}")
        lines.append("")

    if blog_rows:
        lines.append("=== BUILDER_BLOGS_BEGIN ===")
        lines.append(f"【Builder 深度长文 / 官方工程博客（48h 内新发，共 {len(blog_rows)} 篇）】")
        lines.append("（注：长文可作为独立条目或补充印证写进日报，引用时附上对应链接；全篇仍须保证至少带一条 x.com 原推直链）")
        lines.append("")
        for j, b in enumerate(blog_rows, 1):
            lines.append(f"--- [Blog {j}] {b['author']}：《{b['title']}》")
            lines.append(f"[时间] {b['date']}　[链接] {b['url']}")
            if b["summary"]:
                lines.append(f"[摘要] {b['summary']}")
            lines.append("")
        lines.append("=== BUILDER_BLOGS_END ===")

    return "\n".join(lines)


def _fetch_builder_blogs(sent_before: set) -> list:
    """回退模式专用：follow-builders feed-blogs.json + Builder 个人博客直连 RSS（48h 窗口）。"""
    blog_items = []
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    try:
        r = _SESSION.get(FEED_BLOGS_URL, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for item in data.get("blogs", []):
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or item.get("link") or ""
                title = (item.get("title") or "").strip()
                if not url or not title:
                    continue
                uk = url_key(url)
                if uk in sent_before or uk in seen:
                    continue
                seen.add(uk)
                src = item.get("source") or item.get("author") or "Official AI Blog"
                summary = _plain_text(item.get("summary") or item.get("description") or item.get("content") or "", 320)
                blog_items.append({
                    "author": src,
                    "title": title,
                    "url": url,
                    "date": (item.get("publishedAt") or item.get("date") or "")[:16],
                    "summary": summary,
                })
    except Exception as exc:
        print(f"[WARN] feed-blogs.json 抓取异常: {exc}", file=sys.stderr)

    for author_label, feed_url, limit in BUILDER_BLOG_FEEDS:
        try:
            entries = fetch_rss(feed_url, limit=limit)
            for e in entries:
                dt = parse_entry_date(e)
                if dt and dt < cutoff:
                    continue
                url = getattr(e, "link", "") or getattr(e, "id", "")
                title = (getattr(e, "title", "") or "").strip()
                if not url or not title:
                    continue
                uk = url_key(url)
                if uk in sent_before or uk in seen:
                    continue
                seen.add(uk)
                summary = _plain_text(getattr(e, "summary", "") or "", 320)
                blog_items.append({
                    "author": author_label,
                    "title": title,
                    "url": url,
                    "date": dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "",
                    "summary": summary,
                })
        except Exception as exc:
            print(f"[WARN] Builder 博客 {feed_url} 抓取异常: {exc}", file=sys.stderr)

    return blog_items


def _gather_follow_builders_fallback(sent_before: set) -> tuple[str, dict, float]:
    """未配置 X_LIST_ID 时的临时回退通道（follow-builders + 个人博客 RSS）。"""
    r = _SESSION.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = r.json()

    generated = _iso(feed.get("generatedAt", "")) or datetime.now(timezone.utc)
    lag_h = round((datetime.now(timezone.utc) - generated).total_seconds() / 3600, 1)
    lookback = feed.get("lookbackHours", 24)

    rows, dropped_dup, dropped_cap, dropped_excluded = [], 0, 0, 0
    existing_urls = set()
    kept_builders = 0

    for acct in feed.get("x", []):
        handle = acct.get("handle", "")
        if handle.lower().lstrip("@") in EXCLUDED_HANDLES:
            dropped_excluded += 1
            continue
        kept_builders += 1
        name = acct.get("name", "")
        bio = (acct.get("bio", "") or "")[:80]
        kept_this = 0
        tweets = sorted(acct.get("tweets", []),
                        key=lambda t: -(t.get("likes", 0) or 0))
        for t in tweets:
            url = t.get("url", "")
            if not url:
                continue
            uk = url_key(url)
            if uk in sent_before or uk in existing_urls:
                dropped_dup += 1
                continue
            if kept_this >= PER_BUILDER_CAP:
                dropped_cap += 1
                continue
            kept_this += 1
            existing_urls.add(uk)
            rows.append({
                "handle": handle, "name": name, "bio": bio, "url": url,
                "text": (t.get("text", "") or "").strip(),
                "createdAt": t.get("createdAt", ""),
                "likes": t.get("likes", 0) or 0,
                "retweets": t.get("retweets", 0) or 0,
                "replies": t.get("replies", 0) or 0,
                "isQuote": bool(t.get("isQuote")),
            })

    rows.sort(key=lambda x: -x["likes"])
    blog_rows = _fetch_builder_blogs(sent_before)

    header = [
        f"【feed 生成时间】{generated.strftime('%Y-%m-%d %H:%M')} UTC"
        f"（距现在 {lag_h} 小时，上游回看窗口 {lookback}h｜未配置 X_LIST_ID，临时回退 follow-builders）",
        f"【本次可用推文】{len(rows)} 条（主源 {kept_builders} 位 builder，已剔除 {dropped_excluded} 个机构/VC/教程号；"
        f"博客命中 {len(blog_rows)} 篇；"
        f"跨天已播过滤掉 {dropped_dup} 条，单人上限滤掉 {dropped_cap} 条）",
    ]
    context = _format_context(rows, header, blog_rows)
    metrics = {
        "source_mode": "follow_builders_fallback",
        "tweets": len(rows),
        "builders": kept_builders,
        "blogs": len(blog_rows),
        "dropped_excluded": dropped_excluded,
        "dropped_dup": dropped_dup,
        "dropped_cap": dropped_cap,
        "feed_lag_h": lag_h,
        "feed_generated_at": generated.isoformat(),
    }
    return context, metrics, lag_h


def gather() -> tuple[str, dict, float]:
    """取材主入口：配置了 X_LIST_ID 时独占使用 twscrape X List；否则回退 follow-builders。"""
    sent_before = load_sent_urls(SENT_URLS)
    list_id = _parse_list_id(os.getenv("X_LIST_ID", ""))
    if list_id is None:
        print("[INFO] 未检测到 X_LIST_ID，临时使用 follow-builders 回退源", file=sys.stderr)
        return _gather_follow_builders_fallback(sent_before)

    rows, stats = asyncio.run(_fetch_x_list_async(list_id, sent_before))
    fetched_at = stats["fetched_at"]
    header = [
        f"【抓取模式】X List 直连（List ID: {list_id}，回看窗口 {WINDOW_H}h，抓取时间 {fetched_at.strftime('%Y-%m-%d %H:%M')} UTC）",
        f"【本次可用推文】{len(rows)} 条（活跃 {stats['builders']} 位 builder｜"
        f"共扫描 {stats['scanned']} 条，过滤转推 {stats['dropped_rt']}、回复 {stats['dropped_reply']}、"
        f"超窗 {stats['dropped_old']}、跨天已播 {stats['dropped_dup']}、单人上限 {stats['dropped_cap']}）",
    ]
    context = _format_context(rows, header, blog_rows=None)
    metrics = {
        "source_mode": "x_list",
        "list_id": list_id,
        "tweets": len(rows),
        "builders": stats["builders"],
        "blogs": 0,
        "dropped_rt": stats["dropped_rt"],
        "dropped_reply": stats["dropped_reply"],
        "dropped_old": stats["dropped_old"],
        "dropped_dup": stats["dropped_dup"],
        "dropped_cap": stats["dropped_cap"],
        "feed_lag_h": 0.0,
        "feed_generated_at": fetched_at.isoformat(),
    }
    return context, metrics, 0.0


# ===== 模式 1：fetch =====

def run_fetch() -> int:
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    try:
        context, metrics, lag_h = gather()
    except Exception as e:
        print("=== FEED_ERROR ===")
        print(f"拉取素材失败：{e}")
        write_log("WARN", f"builder 素材拉取失败，未发送：{e}")
        return 0

    if metrics["tweets"] == 0:
        print("=== NO_TWEETS ===")
        extra_blog_note = (
            f"（另有 {metrics.get('blogs', 0)} 篇博客新文，但缺少 x.com 原推直链，按出稿硬门禁未发送）"
            if metrics.get("blogs", 0) > 0
            else "（窗口内暂无符合条件的新推文）"
        )
        write_log("WARN", f"没有可用的新推文{extra_blog_note}，未发送")
        return 0

    is_x_list = metrics.get("source_mode") == "x_list"
    lag_note = f"X List 直连" if is_x_list else f"feed 滞后 {lag_h}h"
    FETCH_META.write_text(json.dumps(
        {"log_summary": f"builder {metrics['builders']} 位 / 推文 {metrics['tweets']} 条（{lag_note}）",
         "fetch_date": datetime.now().strftime("%Y-%m-%d"),
         "metrics": metrics}, ensure_ascii=False), encoding="utf-8")

    out = ["=== FETCH_OK ===",
           f"今天日期：{datetime.now().strftime('%Y-%m-%d')}"]
    if not is_x_list and lag_h > FEED_STALE_H:
        out += ["=== FEED_STALE ===",
                f"上游 feed 已滞后 {lag_h} 小时（阈值 {FEED_STALE_H}h），"
                f"多半是 follow-builders 的 GitHub Actions 断了。"
                f"照常出稿，但务必在稿子里写明这批推文的实际日期。",
                "=== FEED_STALE_END ==="]
    out += ["=== CONTEXT_BEGIN ===", context, "=== CONTEXT_END ==="]
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


# ===== 模式 2：test-list =====

def run_test_list() -> int:
    """只读测试 X List 与 Cookie 连通性，不写日志也不改去重记录。"""
    _load_plist_env_if_needed()
    list_id = _parse_list_id(os.getenv("X_LIST_ID", ""))
    if list_id is None:
        print("ERROR: 未检测到有效的 X_LIST_ID（请在环境变量或 LaunchAgent plist 中设置）", file=sys.stderr)
        return 2
    if not _proxy_ok():
        print(f"ERROR: 代理不可用：{_PROXY}", file=sys.stderr)
        return 2

    rows, stats = asyncio.run(_fetch_x_list_async(list_id, set()))
    print(
        f"✓ X List {list_id} 测试成功：扫描 {stats['scanned']} 条，"
        f"近 {WINDOW_H}h 命中 {stats['builders']} 位 builder 共 {len(rows)} 条可用推文"
        f"（过滤转推 {stats['dropped_rt']}、回复 {stats['dropped_reply']}、超窗 {stats['dropped_old']}）"
    )
    for idx, r in enumerate(rows[:5], 1):
        snippet = r["text"].replace("\n", " ")[:110]
        print(f"  {idx}. @{r['handle']} ({r['likes']}赞) {r['url']} -> {snippet}")
    return 0


# ===== 模式 3：send =====

def run_send(draft_path: Path) -> int:
    return send_x_linked_report(
        draft_path,
        log_file=LOG_FILE, cache_file=CACHE_FILE, fetch_meta=FETCH_META,
        sent_urls=SENT_URLS, webhook=FEISHU_WEBHOOK,
        write_log=write_log, save_pending=save_pending,
        link_label="原推")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Builder 日报")
    parser.add_argument("--mode", choices=["fetch", "send", "test-list"], required=True)
    parser.add_argument("--draft", default=str(DRAFT_FILE))
    args = parser.parse_args()
    if args.mode == "fetch":
        sys.exit(run_fetch())
    if args.mode == "test-list":
        sys.exit(run_test_list())
    sys.exit(run_send(Path(args.draft)))
