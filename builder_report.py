#!/usr/bin/python3
"""
AI Builder 日报

每天把 X 上十几位 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文整理成一份
播报，**每条都带原推直链**，由 Claude 写稿后推送到飞书。本脚本只负责取材与推送，
不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch：拉 follow-builders 公共 feed → 把 context 打到 stdout（供 Claude routine 写稿）
- send ：读取 Claude 写好的稿子文件 → 清洗 HTML → 推送飞书 + 写日志

素材来源：zarazhangrui/follow-builders 仓库里由 GitHub Actions 每天生成的静态 feed。
**零 API key、零依赖**，就是一个 raw.githubusercontent.com 上的 JSON。

━━━ 三条改动前必读的约束 ━━━

1. **feed 的生成时间比我们早不了多少，务必照实交代。**
   上游 CI 是 `17 6 * * *` UTC，即北京时间 14:17 生成当天的 feed。而 routine 是
   北京时间 10:00 跑，那时当天的 feed 还没出，我们拿到的永远是**前一天 14:17**
   生成的那份，覆盖的是前天下午到昨天下午这 24 小时。
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

写稿规范在同目录 prompt.md，由 Claude routine 读取。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (already_ran_today, load_sent_urls, url_key,
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
# feed 比这个小时数还旧就打 FEED_STALE 标记。上游 14:17 生成、我们 10:00 取，
# 正常滞后就有 ~20h，所以阈值定在 36h——那才说明上游 CI 真的断了一天。
FEED_STALE_H = int(os.getenv("BUILDER_FEED_STALE_H", "36"))
# 每位 builder 最多进多少条，避免话痨账号刷屏挤掉其他人
PER_BUILDER_CAP = int(os.getenv("BUILDER_PER_ACCOUNT_CAP", "4"))

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
    return ok


def _iso(s: str):
    """feed 里的时间都是 ISO8601 带 Z，转成 aware datetime；解析不了返回 None。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ===== 取材 =====

def gather() -> tuple:
    """拉 feed → 跨天去重 → 按互动量排序，返回 (context, metrics)。"""
    r = _SESSION.get(FEED_URL, timeout=30)
    r.raise_for_status()
    feed = r.json()

    generated = _iso(feed.get("generatedAt", "")) or datetime.now(timezone.utc)
    lag_h = round((datetime.now(timezone.utc) - generated).total_seconds() / 3600, 1)
    lookback = feed.get("lookbackHours", 24)

    sent_before = load_sent_urls(SENT_URLS)
    rows, dropped_dup, dropped_cap = [], 0, 0

    for acct in feed.get("x", []):
        handle = acct.get("handle", "")
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
            if url_key(url) in sent_before:
                dropped_dup += 1
                continue
            if kept_this >= PER_BUILDER_CAP:
                dropped_cap += 1
                continue
            kept_this += 1
            rows.append({
                "handle": handle, "name": name, "bio": bio, "url": url,
                "text": (t.get("text", "") or "").strip(),
                "createdAt": t.get("createdAt", ""),
                "likes": t.get("likes", 0) or 0,
                "retweets": t.get("retweets", 0) or 0,
                "replies": t.get("replies", 0) or 0,
                "isQuote": bool(t.get("isQuote")),
            })

    # 全局按点赞排序：Claude 先看到讨论最热的，写稿时好定详略
    rows.sort(key=lambda x: -x["likes"])

    times = [_iso(x["createdAt"]) for x in rows]
    times = [t for t in times if t]
    span = (f"{min(times).strftime('%Y-%m-%d %H:%M')} ~ "
            f"{max(times).strftime('%Y-%m-%d %H:%M')} UTC") if times else "无"

    lines = [
        f"【feed 生成时间】{generated.strftime('%Y-%m-%d %H:%M')} UTC"
        f"（距现在 {lag_h} 小时，上游回看窗口 {lookback}h）",
        f"【推文实际时间区间】{span}",
        f"【本次可用推文】{len(rows)} 条 / {len(feed.get('x', []))} 位 builder"
        f"（跨天已播过滤掉 {dropped_dup} 条，单人上限滤掉 {dropped_cap} 条）",
        "",
        "⚠️ 下面的 赞/转/回 是 X 上的真实互动量，可以照实写。",
        "",
    ]
    for i, x in enumerate(rows, 1):
        when = _iso(x["createdAt"])
        when_s = when.strftime("%m-%d %H:%M UTC") if when else x["createdAt"]
        lines.append(f"--- {i}. @{x['handle']}（{x['name']}｜{x['bio']}）")
        lines.append(f"[时间] {when_s}"
                     f"　[赞] {x['likes']}　[转] {x['retweets']}　[回] {x['replies']}"
                     + ("　[引用推]" if x["isQuote"] else ""))
        lines.append(f"[原推] {x['url']}")
        lines.append(f"[正文] {x['text']}")
        lines.append("")

    metrics = {
        "tweets": len(rows),
        "builders": len(feed.get("x", [])),
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
        write_log("WARN", "feed 里没有可用的新推文（多半是上游 CI 未更新），未发送")
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
