#!/usr/bin/python3
"""
AI Builder 日报

每天把 X 上一线 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文与技术长文整理成一份
播报，**每条话题都带原推直链**，由 AI 定时任务写稿后推送到飞书并归档至 Notion。本脚本只负责取材与推送，
不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch：拉 follow-builders 公共 feed → 把 context 打到 stdout（并落盘 logs/last_context.txt）
- send ：读取写好的稿子文件 → 清洗 HTML → 推送飞书 + 同步 Notion + 写日志

素材来源：zarazhangrui/follow-builders 仓库里由 GitHub Actions 每天生成的静态 feed。
**零 API key、零依赖**，就是一个 raw.githubusercontent.com 上的 JSON。

━━━ 三条改动前必读的约束 ━━━

1. **feed 的生成时间比我们早不了多少，务必照实交代。**
   上游 CI 是 `17 6 * * *` UTC，即北京时间 14:17 生成当天的 feed。而 routine 已于 2026-09-04 改到
   北京时间 15:00 跑，正常情况下拿到的就是**当天 14:17** 刚生成的那份（滞后约 45 分钟），
   覆盖昨天下午到今天下午这 24 小时。
   所以稿子开头必须写清这批推文发生在哪一天，不能写成「今天」。
   fetch 会把 feed 的 generatedAt、推文实际时间区间、滞后小时数一并打进 context；
   滞后超过 FEED_STALE_H 还会额外给一个 === FEED_STALE === 标记。
   想让它拿到当天的 feed，唯一的办法是把 routine 挪到北京时间 14:30 之后。

2. **likes/retweets/replies 是真实互动量，可以直说。**
   这点和 x-hotspot-bot 正好相反——那边的「被引用次数」是 AINews 复盘里的引用
   次数，是讨论中心度的代理指标，写成「点赞」就是骗人。这边是 X 上的真实计数，
   写「获 8686 赞」是准确的。两个 bot 的 prompt 都写死了各自的口径，别互相抄。

3. **去重键是推文 URL**，跨天档案在 logs/sent_urls.json。
   上游 CI 万一某天没跑成，raw 上还是昨天那份 feed，不去重就会把同一批推文
   再播一遍。这道闸门是必须的，不是锦上添花。

写稿规范在同目录 prompt.md，由 Antigravity Sidecar 定时任务读取。
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (already_ran_today, load_sent_urls, url_key,
                       fetch_rss, parse_entry_date, X_POST_RE,
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

FEED_URL = os.getenv(
    "BUILDER_FEED_URL",
    "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json")
FEED_BLOGS_URL = os.getenv(
    "BUILDER_BLOGS_URL",
    "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-blogs.json")
# feed 比这个小时数还旧就打 FEED_STALE 标记。上游 14:17 生成、我们 15:00 取，
# 正常滞后约 1h；阈值定在 36h 说明上游 CI 真的断了一整天。
FEED_STALE_H = int(os.getenv("BUILDER_FEED_STALE_H", "36"))
# 每位 builder 最多进多少条，避免话痨账号刷屏挤掉其他人
PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", "4"))

# 从上游 follow-builders 名单中剔除的 8 个非一线个人 Builder 账号：
# - 类别 B（官方机构宣传号）：claudeai, googlelabs
# - 类别 C 前三位（纯 VC 投资人）：mattturck, adityaag, nikunj
# - 类别 D 三位（维护者/教程博主）：zarazhangrui, petergyang, danshipper
EXCLUDED_HANDLES = {
    "claudeai", "googlelabs",
    "mattturck", "adityaag", "nikunj",
    "zarazhangrui", "petergyang", "danshipper",
}

# 渠道 ②：从 AINews (latent.space/feed) 544 个 X 账号池中定向捞取的扩充 Builder 白名单
EXTRA_BUILDERS = {
    "noamshazeer": ("Noam Shazeer", "Character.AI 创始人 / Google Gemini 联创，Transformer 作者"),
    "polynoamial": ("Noam Brown", "OpenAI 多步推理研究科学家"),
    "drjimfan": ("Jim Fan", "NVIDIA 具身智能 GEAR Lab 负责人"),
    "ylecun": ("Yann LeCun", "Meta 首席 AI 科学家"),
    "sholtodouglas": ("Sholto Douglas", "Anthropic 研究员，专注 Scaling 与强化学习"),
    "trentonbricken": ("Trenton Bricken", "Anthropic 机械可解释性研究员"),
    "soumithchintala": ("Soumith Chintala", "PyTorch 联创"),
    "simonw": ("Simon Willison", "Datasette 创始人 / 独立 AI 工具构建者"),
    "mitchellh": ("Mitchell Hashimoto", "Ghostty / HashiCorp 创始人"),
    "hamelhusain": ("Hamel Husain", "Parlance Labs 创始人 / AI Evals 专家"),
    "eugeneyan": ("Eugene Yan", "Amazon 资深应用科学家"),
    "clementdelangue": ("Clément Delangue", "Hugging Face CEO"),
    "hwchase17": ("Harrison Chase", "LangChain 创始人"),
    "jerryjliu0": ("Jerry Liu", "LlamaIndex 创始人"),
    "aravindsrinivas": ("Aravind Srinivas", "Perplexity CEO"),
}

# 渠道 ③：一线 AI Builder 个人技术博客直连 RSS（48h 窗口）
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


def _proxy_ok() -> bool:
    """代理预检 + 端口自愈，实现见 shared/bot_utils.proxy_ok。
    raw.githubusercontent.com 在国内直连基本不通，这一步是硬前置。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, _SESSION)
    if ok and _PROXY:
        os.environ["HTTP_PROXY"] = _PROXY
        os.environ["HTTPS_PROXY"] = _PROXY
    return ok


def _iso(s: str):
    """feed 里的时间都是 ISO8601 带 Z，转成 aware datetime；解析不了返回 None。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_TAG_RE = re.compile(r"(?s)<[^>]+>")
_ANCHOR_RE = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>')


def _plain_text(fragment: str, limit: int = 0) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] if limit else text


def _fetch_extra_builder_tweets(sent_before: set, existing_urls: set) -> list:
    """渠道 ②：从 latent.space/feed 的 [AINews] 期刊中提取 EXTRA_BUILDERS 白名单原推及上下文。"""
    extra_rows = []
    try:
        entries = fetch_rss("https://www.latent.space/feed", limit=10)
        entries = [e for e in entries
                   if "[ainews]" in (getattr(e, "title", "") or "").lower()]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for e in entries[:2]:
            dt = parse_entry_date(e)
            if dt and dt < cutoff:
                continue
            body = ""
            for item in (getattr(e, "content", None) or []):
                val = item.get("value") if isinstance(item, dict) else getattr(item, "value", "")
                if val and len(val) > 2000:
                    body = val
                    break
            if not body:
                body = getattr(e, "summary", "") or ""
            if not body:
                continue

            # 定位白名单原推并截取周围上下文
            hits = {}
            order = []
            for m in X_POST_RE.finditer(body):
                h_raw, pid = m.group(1), m.group(2)
                h_low = h_raw.lower()
                if h_low not in EXTRA_BUILDERS:
                    continue
                url = f"https://x.com/{h_raw}/status/{pid}"
                uk = url_key(url)
                if uk in sent_before or uk in existing_urls:
                    continue
                if pid not in hits:
                    hits[pid] = {
                        "handle": h_raw,
                        "url": url,
                        "idx": len(order),
                        "createdAt": dt.isoformat() if dt else "",
                    }
                    order.append(pid)

            if not hits:
                continue

            ref_links = []

            def _mark_anchor(m):
                href, inner = (m.group(1) or "").strip(), m.group(2) or ""
                hit = X_POST_RE.search(href)
                if hit and hit.group(2) in hits:
                    return f"{inner} ⟦EB{hits[hit.group(2)]['idx']}⟧ "
                label = _plain_text(inner)
                if label and href.startswith(("http://", "https://")):
                    low = href.lower().split("?", 1)[0]
                    if not (any(h in low for h in ("substackcdn.com", "pbs.twimg.com")) and low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
                        clean_href = f"https://x.com/{hit.group(1)}/status/{hit.group(2)}" if hit else href
                        idx = len(ref_links)
                        ref_links.append((label, clean_href))
                        return f"{inner} ⟦EL{idx}⟧ "
                return inner

            marked = _ANCHOR_RE.sub(_mark_anchor, body)
            plain = _plain_text(marked)
            for pid in order:
                info = hits[pid]
                token = f"⟦EB{info['idx']}⟧"
                pos = plain.find(token)
                if pos >= 0:
                    left = max(0, pos - 200)
                    raw_snip = plain[left:pos] + plain[pos + len(token):pos + len(token) + 80]
                    seen_r, refs = {url_key(info["url"])}, []
                    for rm in re.finditer(r"⟦EL(\d+)⟧", raw_snip):
                        rlbl, rurl = ref_links[int(rm.group(1))]
                        ruk = url_key(rurl)
                        if ruk not in seen_r:
                            seen_r.add(ruk)
                            refs.append((rlbl, rurl))
                    snippet = re.sub(r"⟦E[BL]\d+⟧", "", raw_snip)
                    snippet = re.sub(r"^[^⟦]{0,12}⟧", "", snippet)
                    snippet = re.sub(r"⟦[^⟧]*$", "", snippet).strip()
                else:
                    snippet, refs = "", []
                if len(snippet) < 25:
                    continue
                name, bio = EXTRA_BUILDERS[info["handle"].lower()]
                existing_urls.add(url_key(info["url"]))
                extra_rows.append({
                    "handle": info["handle"],
                    "name": name,
                    "bio": bio,
                    "url": info["url"],
                    "ref_links": refs,
                    "text": f"[AINews 摘录上下文] {snippet}",
                    "createdAt": info["createdAt"],
                    "likes": 0,
                    "retweets": 0,
                    "replies": 0,
                    "isQuote": False,
                    "from_ainews": True,
                })
    except Exception as exc:
        print(f"[WARN] EXTRA_BUILDERS 渠道抓取异常（不阻断主流程）: {exc}", file=sys.stderr)
    return extra_rows


def _fetch_builder_blogs(sent_before: set) -> list:
    """渠道 ①（follow-builders feed-blogs.json）+ 渠道 ③（Builder 个人博客直连 RSS，48h 窗口）。"""
    blog_items = []
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    # 渠道 ①：follow-builders 的 feed-blogs.json
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

    # 渠道 ③：Builder 个人博客直连 RSS
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


# ===== 取材 =====

def gather() -> tuple:
    """拉 feed → 剔除非个人噪音号 → 补充三路扩充源 → 跨天去重 → 按互动量排序，返回 (context, metrics, lag_h)。"""
    r = _SESSION.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = r.json()

    generated = _iso(feed.get("generatedAt", "")) or datetime.now(timezone.utc)
    lag_h = round((datetime.now(timezone.utc) - generated).total_seconds() / 3600, 1)
    lookback = feed.get("lookbackHours", 24)

    sent_before = load_sent_urls(SENT_URLS)
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
        # 同一个人内部先按互动量排，超额时砍掉的是他自己最冷的那几条
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
                "from_ainews": False,
            })

    # 全局按点赞排序：让写稿方先看到讨论最热的，好定详略
    rows.sort(key=lambda x: -x["likes"])

    # 渠道 ②：补充 EXTRA_BUILDERS 原推
    extra_rows = _fetch_extra_builder_tweets(sent_before, existing_urls)
    rows.extend(extra_rows)

    # 渠道 ① + ③：补充官方 Engineering 博客与个人技术博客（48h 内新文）
    blog_rows = _fetch_builder_blogs(sent_before)

    times = [_iso(x["createdAt"]) for x in rows]
    times = [t for t in times if t]
    span = (f"{min(times).strftime('%Y-%m-%d %H:%M')} ~ "
            f"{max(times).strftime('%Y-%m-%d %H:%M')} UTC") if times else "无"

    lines = [
        f"【feed 生成时间】{generated.strftime('%Y-%m-%d %H:%M')} UTC"
        f"（距现在 {lag_h} 小时，上游回看窗口 {lookback}h）",
        f"【推文实际时间区间】{span}",
        f"【本次可用推文】{len(rows)} 条（主源 {kept_builders} 位 builder，已剔除 {dropped_excluded} 个机构/VC/教程号；"
        f"扩充渠道额外命中 {len(extra_rows)} 条原推 + {len(blog_rows)} 篇博客；"
        f"跨天已播过滤掉 {dropped_dup} 条，单人上限滤掉 {dropped_cap} 条）",
        "",
        "⚠️ 主源推文的 赞/转/回 是 X 上的真实互动量，可以照实写；来自 [AINews 摘录上下文] 的扩充推文未附互动量数字，不要编造赞数。",
        "",
    ]
    for i, x in enumerate(rows, 1):
        when = _iso(x["createdAt"])
        when_s = when.strftime("%m-%d %H:%M UTC") if when else x["createdAt"]
        lines.append(f"--- {i}. @{x['handle']}（{x['name']}｜{x['bio']}）")
        if x.get("from_ainews"):
            lines.append(f"[时间] {when_s}　[来源] AINews 扩充白名单（无互动量统计）")
        else:
            lines.append(f"[时间] {when_s}"
                         f"　[赞] {x['likes']}　[转] {x['retweets']}　[回] {x['replies']}"
                         + ("　[引用推]" if x["isQuote"] else ""))
        lines.append(f"[原推] {x['url']}")
        if x.get("ref_links"):
            ref_s = " | ".join(f"{lbl} -> {u}" for lbl, u in x["ref_links"])
            lines.append(f"[引用链接] {ref_s}")
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

    metrics = {
        "tweets": len(rows),
        "builders": kept_builders,
        "extra_tweets": len(extra_rows),
        "blogs": len(blog_rows),
        "dropped_excluded": dropped_excluded,
        "dropped_dup": dropped_dup,
        "dropped_cap": dropped_cap,
        "feed_lag_h": lag_h,
        "feed_generated_at": generated.isoformat(),
    }
    return "\n".join(lines), metrics, lag_h


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
        # 上游 feed 拉不到不算本 bot 的故障，但要让 routine 知道并跳过
        print("=== FEED_ERROR ===")
        print(f"拉取 {FEED_URL} 失败：{e}")
        write_log("WARN", f"builder feed 拉取失败，未发送：{e}")
        return 0

    if metrics["tweets"] == 0:
        print("=== NO_TWEETS ===")
        extra_blog_note = (
            f"（另有 {metrics.get('blogs', 0)} 篇博客新文，但缺少 x.com 原推直链，按出稿硬门禁未发送）"
            if metrics.get("blogs", 0) > 0
            else "（多半是上游 CI 未更新）"
        )
        write_log("WARN", f"feed 里没有可用的新推文{extra_blog_note}，未发送")
        return 0

    FETCH_META.write_text(json.dumps(
        {"log_summary": f"builder {metrics['builders']} 位 / 推文 {metrics['tweets']} 条"
                        f"（feed 滞后 {lag_h}h）",
         "metrics": metrics}, ensure_ascii=False), encoding="utf-8")

    out = ["=== FETCH_OK ===",
           f"今天日期：{datetime.now().strftime('%Y-%m-%d')}"]
    if lag_h > FEED_STALE_H:
        out += ["=== FEED_STALE ===",
                f"上游 feed 已滞后 {lag_h} 小时（阈值 {FEED_STALE_H}h），"
                f"多半是 follow-builders 的 GitHub Actions 断了。"
                f"照常出稿，但务必在稿子里写明这批推文的实际日期。",
                "=== FEED_STALE_END ==="]
    out += ["=== CONTEXT_BEGIN ===", context, "=== CONTEXT_END ==="]
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


# ===== 模式 2：send =====

def run_send(draft_path: Path) -> int:
    # 发送流程与 x-hotspot-bot 完全一致，统一在 bot_utils.send_x_linked_report，
    # 其中含出稿硬闸门：稿件必须带 x.com 原推直链，否则拒发。
    # 注意闸门此前在本文件里是子串匹配 `"x.com/" in u`，比 x-hotspot 松，
    # 会把 x.com/home 这类导航页当原推放行；合并后统一走 X_POST_RE。
    return send_x_linked_report(
        draft_path,
        log_file=LOG_FILE, cache_file=CACHE_FILE, fetch_meta=FETCH_META,
        sent_urls=SENT_URLS, webhook=FEISHU_WEBHOOK,
        write_log=write_log, save_pending=save_pending,
        link_label="原推")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Builder 日报")
    parser.add_argument("--mode", choices=["fetch", "send"], required=True)
    parser.add_argument("--draft", default=str(DRAFT_FILE))
    args = parser.parse_args()
    sys.exit(run_fetch() if args.mode == "fetch" else run_send(Path(args.draft)))
