#!/usr/bin/python3
"""
AI Builder 日报

每天把自建 X List 中一线 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文整理成一份
播报，**每条话题都带原推直链**，由 AI 定时任务写稿后推送到飞书并归档至 Notion。本脚本只负责取材与推送，
不含写稿用的第三方大模型 API。

四种运行模式（--mode，均零 API 成本）：
- fetch       ：通过 twscrape 拉取自建 X List 过去 24h 原创与引用推文 → 落盘 logs/last_context.txt
- send        ：读取写好的稿子文件 → 清洗 HTML → 推送飞书 + 同步 Notion + 写日志
- archive-only：读取写好的稿子文件 → 清洗 HTML → 仅归档本地与 Notion + 写日志（不推飞书）
- test-list   ：只读测试 X List 与 Cookie 连通性，不写 run.log 或 last_context.txt

写稿规范在同目录 prompt.md，由 Antigravity Sidecar 定时任务读取。
"""

import argparse
import asyncio
import json
import os
import plistlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (already_ran_today, load_sent_urls, url_key,
                       make_logger, make_pending_saver, proxy_ok,
                       emit_fetch_output, send_x_linked_report,
                       sanitize_html, extract_hrefs, X_POST_RE,
                       extract_report_date, archive_and_sync_notion,
                       record_sent_urls, fetch_article_text,
                       load_daily_ledger, record_daily_ledger,
                       format_ledger_context)

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

# 每位 builder 最多进多少条，避免高频账号刷屏挤掉其他人
PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", "4"))
WINDOW_H = int(os.getenv("BUILDER_WINDOW_H", "24"))
LIST_SCAN_LIMIT = int(os.getenv("BUILDER_LIST_SCAN_LIMIT", "200"))
INCLUDE_REPLIES = os.getenv("BUILDER_INCLUDE_REPLIES", "0") == "1"

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")


def _proxy_ok() -> bool:
    """代理预检 + 端口/TUN 自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, None)
    if ok and _PROXY:
        os.environ["HTTP_PROXY"] = _PROXY
        os.environ["HTTPS_PROXY"] = _PROXY
    return ok


def _load_plist_env_if_needed() -> None:
    """直接运行 test-list / archive-only 时从 LaunchAgent plist 补全环境变量（不覆盖已存在变量）。"""
    global _PROXY, FEISHU_WEBHOOK, PER_BUILDER_CAP, WINDOW_H, LIST_SCAN_LIMIT, INCLUDE_REPLIES
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
        PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", str(PER_BUILDER_CAP)))
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


_TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")
_X_ARTICLE_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/\d+\S*", re.IGNORECASE)
_SKIP_EXT_HOSTS = (
    "x.com", "twitter.com", "t.co", "twimg.com",
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
)
_SKIP_EXT_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".pdf", ".zip")


def _expand_links(raw_text: str, links, *, strip_x_article: bool = False) -> str:
    """展开推文中的 t.co 短链，并剔除残留媒体/引用卡片 t.co 尾巴及同源 X Article 冗余链。"""
    text = (raw_text or "").strip()
    for lk in links or []:
        tco = getattr(lk, "tcourl", "") or ""
        dest = getattr(lk, "url", "") or ""
        if tco and dest and tco in text:
            text = text.replace(tco, dest)
    text = _TCO_RE.sub("", text)
    if strip_x_article:
        text = _X_ARTICLE_URL_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(ln.rstrip() for ln in text.splitlines()).strip()


def _extract_external_urls(t) -> list[str]:
    """提取推文（及引用推文）中的独立外部技术博客/文档/项目链接（排除 x.com 与音视频图床）。"""
    candidates: list[str] = []
    link_lists = [getattr(t, "links", None) or []]
    qt = getattr(t, "quotedTweet", None)
    if qt is not None:
        link_lists.append(getattr(qt, "links", None) or [])

    seen: set[str] = set()
    for lk_list in link_lists:
        for lk in lk_list:
            u = (getattr(lk, "url", "") or "").strip()
            if not u.startswith(("http://", "https://")):
                continue
            try:
                sp = urlsplit(u)
                host = (sp.hostname or "").lower()
                path_low = (sp.path or "").lower()
            except Exception:
                continue
            if not host or any(host == h or host.endswith("." + h) for h in _SKIP_EXT_HOSTS):
                continue
            if path_low.endswith(_SKIP_EXT_SUFFIXES):
                continue
            uk = url_key(u)
            if uk and uk not in seen:
                seen.add(uk)
                candidates.append(u)
    return candidates


def _extract_article_full_text(article) -> str:
    """从 twscrape Article 对象提取完整正文段落（contentState.blocks），回退 previewText。"""
    if not article:
        return ""
    cs = getattr(article, "contentState", None)
    blocks = getattr(cs, "blocks", None) if cs else None
    if blocks:
        paras = [
            (getattr(b, "text", "") or "").strip()
            for b in blocks
            if (getattr(b, "text", "") or "").strip()
        ]
        if paras:
            return "\n".join(paras)
    return (
        getattr(article, "previewText", "")
        or getattr(article, "summaryText", "")
        or ""
    ).strip()


def _format_tweet_text(t) -> str:
    """展开 t.co 链接，并补充 X Article 标题/预览与引用推文正文（不含同源 x.com/i/article 冗余链）。"""
    article = getattr(t, "article", None)
    text = _expand_links(
        getattr(t, "rawContent", ""),
        getattr(t, "links", None),
        strip_x_article=bool(article),
    )

    if article:
        a_title = (getattr(article, "title", "") or "").strip()
        a_preview = (
            getattr(article, "previewText", "")
            or getattr(article, "summaryText", "")
            or ""
        ).strip()
        header = f"[X Article: {a_title}]" if a_title else "[X Article]"
        if a_preview and a_preview not in text:
            preview_snip = a_preview.replace("\n", " ")[:240]
            if text:
                text = f"{header} {text} | 导语：{preview_snip}".strip()
            else:
                text = f"{header} 导语：{preview_snip}".strip()
        elif a_title:
            text = f"{header} {text}".strip()

    qt = getattr(t, "quotedTweet", None)
    if qt is not None:
        q_user = getattr(getattr(qt, "user", None), "username", "") or "unknown"
        q_url = getattr(qt, "url", "") or ""
        q_text = _expand_links(
            getattr(qt, "rawContent", ""),
            getattr(qt, "links", None),
            strip_x_article=bool(getattr(qt, "article", None)),
        ).replace("\n", " ").strip()[:280]
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


async def _fetch_x_list_async(
    list_id: int,
    sent_before: set,
    *,
    ledger_x_ids: set | None = None,
    enrich_details: bool = True,
) -> tuple[list, dict]:
    """通过 twscrape 的 SearchTimeline (list:<id>) 抓取自建 X List 过去 WINDOW_H 小时推文。"""
    from twscrape import API, AccountsPool
    from twscrape.logger import set_log_level

    set_log_level("WARNING")

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

    query = f"list:{list_id} -filter:retweets" + ("" if INCLUDE_REPLIES else " -filter:replies")
    async for t in api.search(query, limit=LIST_SCAN_LIMIT):
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
        if (
            uk in sent_before
            or uk in seen_in_run
            or (ledger_x_ids and str(t.id_str) in ledger_x_ids)
        ):
            dropped_dup += 1
            continue
        seen_in_run.add(uk)

        article = getattr(t, "article", None)
        initial_full = _extract_article_full_text(article) if article else ""
        # SearchTimeline 通常只返回 preview_text（不含 contentState.blocks），需标记以便后续补拉
        has_blocks = bool(getattr(getattr(article, "contentState", None), "blocks", None)) if article else False

        by_author.setdefault(handle, []).append({
            "id": int(t.id),
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
            "has_article": article is not None,
            "article_title": (getattr(article, "title", "") or "").strip() if article else "",
            "article_full_text": initial_full[:15000] if has_blocks else "",
            "ext_urls": _extract_external_urls(t),
            "ext_excerpts": [],
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

    enriched_articles = 0
    enriched_blogs = 0
    if enrich_details and rows:
        # 1) 对含 X Article 的推文补拉 tweet_details 获取完整 contentState.blocks 正文（最高 15000 字符）
        article_rows = [r for r in rows if r.get("has_article") and not r.get("article_full_text")][:8]
        for r in article_rows:
            try:
                detail_t = await api.tweet_details(r["id"])
                full_art = _extract_article_full_text(getattr(detail_t, "article", None)) if detail_t else ""
                if full_art:
                    r["article_full_text"] = full_art[:15000]
                    if not r.get("article_title") and getattr(detail_t, "article", None):
                        r["article_title"] = (getattr(detail_t.article, "title", "") or "").strip()
                    enriched_articles += 1
            except Exception as exc:
                print(f"  [WARN] 拉取 X Article 详情失败 ({r['url']}): {exc}", file=sys.stderr)

        # 2) 对含独立外部技术博客/文档链接的推文并发抓取正文摘要（最高 4000 字符）
        ext_tasks: list[tuple[dict, str]] = []
        seen_ext: set[str] = set()
        for r in rows:
            for u in r.get("ext_urls") or []:
                uk = url_key(u)
                if uk and uk not in seen_ext:
                    seen_ext.add(uk)
                    ext_tasks.append((r, u))
                if len(ext_tasks) >= 15:
                    break
            if len(ext_tasks) >= 15:
                break

        if ext_tasks:
            with ThreadPoolExecutor(max_workers=5) as ex:
                fut_map = [
                    (r, u, ex.submit(fetch_article_text, u, 10, 4000, 180))
                    for r, u in ext_tasks
                ]
                for r, u, fut in fut_map:
                    try:
                        txt = (fut.result(timeout=12) or "").strip()
                        if txt:
                            r["ext_excerpts"].append((u, txt))
                            enriched_blogs += 1
                    except Exception:
                        pass

    stats = {
        "list_id": list_id,
        "scanned": scanned,
        "builders": len(by_author),
        "dropped_rt": dropped_rt,
        "dropped_reply": dropped_reply,
        "dropped_old": dropped_old,
        "dropped_dup": dropped_dup,
        "dropped_cap": dropped_cap,
        "enriched_articles": enriched_articles,
        "enriched_blogs": enriched_blogs,
        "fetched_at": now,
    }
    return rows, stats


def _format_context(rows: list[dict], stats: dict) -> str:
    """格式化输出给写稿模型的 X List 素材上下文。"""
    times = [_iso(x["createdAt"]) for x in rows]
    times = [t for t in times if t]
    span = (
        f"{min(times).strftime('%Y-%m-%d %H:%M')} ~ "
        f"{max(times).strftime('%Y-%m-%d %H:%M')} UTC"
    ) if times else "无"

    fetched_at = stats["fetched_at"]
    lines = [
        f"【抓取模式】X List 直连（List ID: {stats['list_id']}，回看窗口 {WINDOW_H}h，抓取时间 {fetched_at.strftime('%Y-%m-%d %H:%M')} UTC）",
        f"【推文实际时间区间】{span}",
        f"【本次可用推文】{len(rows)} 条（活跃 {stats['builders']} 位 builder｜"
        f"共扫描 {stats['scanned']} 条，过滤转推 {stats['dropped_rt']}、回复 {stats['dropped_reply']}、"
        f"超窗 {stats['dropped_old']}、已播/前序去重 {stats['dropped_dup']}、单人上限 {stats['dropped_cap']}｜"
        f"深抓 X 长文 {stats.get('enriched_articles', 0)} 篇、外链正文 {stats.get('enriched_blogs', 0)} 篇）",
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
            + ("　[X长文]" if x.get("has_article") else "")
        )
        lines.append(f"[原推] {x['url']}")
        lines.append(f"[正文] {x['text']}")
        if x.get("article_full_text"):
            a_title = x.get("article_title") or "X Article"
            lines.append(f"[X Article 全文摘录（《{a_title}》，直接归属上面的 [原推] 链接，正文勿另挂 x.com/i/article 链接）]:")
            lines.append(x["article_full_text"])
        for ext_u, ext_txt in x.get("ext_excerpts") or []:
            lines.append(f"[外链技术博客/文档链接] {ext_u}")
            lines.append(f"[外链正文摘录]:\n{ext_txt}")
        lines.append("")

    return "\n".join(lines)


def gather() -> tuple[str, dict]:
    """拉取自建 X List 过去 WINDOW_H 小时推文，返回 (context, metrics)。"""
    list_id = _parse_list_id(os.getenv("X_LIST_ID", ""))
    if list_id is None:
        raise RuntimeError(
            "未配置 X_LIST_ID：请在 ~/Library/LaunchAgents/com.shirley.ai-builder-bot.plist 中填入 X_LIST_ID"
        )

    ledger = load_daily_ledger(exclude_bot="🛠️ AI Builder")
    sent_before = load_sent_urls(SENT_URLS) | ledger["urls"]
    rows, stats = asyncio.run(
        _fetch_x_list_async(
            list_id,
            sent_before,
            ledger_x_ids=ledger["x_status_ids"],
            enrich_details=True,
        )
    )
    fetched_at = stats["fetched_at"]
    context = _format_context(rows, stats)
    ledger_block = format_ledger_context(exclude_bot="🛠️ AI Builder")
    if ledger_block:
        context = f"{ledger_block}\n\n{context}"
    metrics = {
        "source_mode": "x_list",
        "list_id": list_id,
        "tweets": len(rows),
        "builders": stats["builders"],
        "dropped_rt": stats["dropped_rt"],
        "dropped_reply": stats["dropped_reply"],
        "dropped_old": stats["dropped_old"],
        "dropped_dup": stats["dropped_dup"],
        "dropped_cap": stats["dropped_cap"],
        "enriched_articles": stats.get("enriched_articles", 0),
        "enriched_blogs": stats.get("enriched_blogs", 0),
        "feed_lag_h": 0.0,
        "feed_generated_at": fetched_at.isoformat(),
    }
    return context, metrics


# ===== 模式 1：fetch =====

def run_fetch() -> int:
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    try:
        context, metrics = gather()
    except Exception as e:
        print("=== FEED_ERROR ===")
        print(f"拉取 X List 失败：{e}")
        write_log("WARN", f"X List 拉取失败，未发送：{e}")
        return 0

    if metrics["tweets"] == 0:
        print("=== NO_TWEETS ===")
        write_log("WARN", "X List 窗口内没有可用的新推文，未发送")
        return 0

    FETCH_META.write_text(json.dumps(
        {"log_summary": f"builder {metrics['builders']} 位 / 推文 {metrics['tweets']} 条（X List 直连）",
         "fetch_date": datetime.now().strftime("%Y-%m-%d"),
         "metrics": metrics}, ensure_ascii=False), encoding="utf-8")

    out = [
        "=== FETCH_OK ===",
        f"今天日期：{datetime.now().strftime('%Y-%m-%d')}",
        "=== CONTEXT_BEGIN ===",
        context,
        "=== CONTEXT_END ===",
    ]
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

    rows, stats = asyncio.run(_fetch_x_list_async(list_id, set(), enrich_details=False))
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


# ===== 模式 4：archive-only =====

def run_archive_only(draft_path: Path) -> int:
    """只读取并清洗草稿，写入本地归档 + 同步 Notion + 记录去重，不推送飞书。"""
    if not draft_path.exists():
        print(f"ERROR: 找不到草稿文件：{draft_path}", file=sys.stderr)
        return 1
    raw = draft_path.read_text(encoding="utf-8")
    report = sanitize_html(raw).strip()
    if not report:
        print("ERROR: 草稿内容为空", file=sys.stderr)
        return 1

    hrefs = extract_hrefs(report)
    x_links = [u for u in hrefs if X_POST_RE.search(u)]
    if not x_links:
        msg = "稿件里没有任何 x.com 原帖链接（格式须为 x.com/<user>/status/<id>），拒绝归档"
        print(f"=== ARCHIVE_FAIL === {msg}", file=sys.stderr)
        write_log("FAIL", msg)
        return 1

    doc_date = extract_report_date(report) or datetime.now().strftime("%Y-%m-%d")
    meta_summary = ""
    if FETCH_META.exists():
        try:
            meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
            if meta.get("fetch_date") == doc_date:
                meta_summary = (meta.get("log_summary") or "").strip()
        except Exception:
            pass

    _proxy_ok()
    arch = archive_and_sync_notion(
        report,
        "🛠️ AI Builder",
        date_str=doc_date,
        archive_dir=LOG_FILE.parent / "archive",
        archive_filename=f"{doc_date}.txt",
    )
    arch_json = json.dumps(arch, ensure_ascii=False, default=str) if isinstance(arch, dict) else str(arch)
    print(f"ARCHIVE_RESULT: {arch_json}")

    notion_ok = False
    notion_detail = ""
    if isinstance(arch, dict):
        if "notion_ok" in arch:
            notion_ok = bool(arch.get("notion_ok"))
            notion_detail = str(arch.get("notion_url") or arch.get("notion_info") or arch.get("notion_error") or arch.get("notion_msg") or "")
        elif isinstance(arch.get("notion"), dict):
            n = arch["notion"]
            notion_ok = bool(n.get("ok"))
            notion_detail = str(n.get("url") or n.get("info") or n.get("msg") or n.get("error") or "")
        elif isinstance(arch.get("notion"), (tuple, list)) and len(arch["notion"]) >= 1:
            notion_ok = bool(arch["notion"][0])
            notion_detail = str(arch["notion"][1]) if len(arch["notion"]) > 1 else ""
        elif "ok" in arch:
            notion_ok = bool(arch.get("ok"))
            notion_detail = str(arch.get("url") or arch.get("res") or arch.get("error") or "")
        elif "notion.so/" in arch_json:
            notion_ok = True
            notion_detail = arch_json

    if not notion_ok:
        prefix = f"{meta_summary} | " if meta_summary else ""
        err_msg = f"{prefix}Notion 单独归档失败 | Notion ✗ ({notion_detail or arch_json})"
        write_log("WARN", err_msg)
        print(f"=== ARCHIVE_FAIL === {err_msg}", file=sys.stderr)
        return 1

    record_sent_urls(SENT_URLS, x_links)
    record_daily_ledger("🛠️ AI Builder", report, hrefs, date_str=doc_date)
    prefix = f"{meta_summary} | " if meta_summary else ""
    status_line = f"{prefix}Notion 单独归档成功 | Notion ✓ {notion_detail}".strip()
    write_log("OK", status_line)
    print(f"=== ARCHIVE_OK === {status_line}")
    return 0


if __name__ == "__main__":
    _load_plist_env_if_needed()
    parser = argparse.ArgumentParser(description="AI Builder 日报")
    parser.add_argument("--mode", choices=["fetch", "send", "test-list", "archive-only"], required=True)
    parser.add_argument("--draft", default=str(DRAFT_FILE))
    args = parser.parse_args()
    if args.mode == "fetch":
        sys.exit(run_fetch())
    if args.mode == "test-list":
        sys.exit(run_test_list())
    if args.mode == "archive-only":
        sys.exit(run_archive_only(Path(args.draft)))
    sys.exit(run_send(Path(args.draft)))
