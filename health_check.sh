#!/bin/bash
# health_check.sh — AI Builder Bot
# 薄封装：设好本 bot 的差异点后，交给同级 shared/health_check_base.sh 执行。
# 完整流程与踩坑记录见那个文件，别把逻辑抄回来。

DIR="$(cd "$(dirname "$0")" && pwd)"

BOT_NAME="AI Builder Bot"

# 终态 WARN 的匹配模式。⚠️ 必须与 builder_report.py 里 write_log("WARN", ...) 的实际措辞
# 一致——对不上就会把"正常的没东西可播"误判成缺跑，白派一次无头补跑。
NO_NEWS_PATTERN="没有可用的新推文"
NO_NEWS_MSG="今天上游 feed 里没有新推文，未出稿（非故障）"

# 本 bot 只有一个素材源（follow-builders 的公共 feed），没有"分源零产"的概念，
# 故不设 STALE_KEY/ZERO_KEY，公共层会跳过第 5 节。
# 上游断更由 fetch 的 FEED_STALE 标记负责，不走体检。

# 第 4 节：推文数异常偏低，或 feed 滞后过久
content_check() {
    local tweets lag
    tweets=$(jsonl_field tweets 99)
    if [ -n "$tweets" ] && [ "$tweets" -lt 5 ] 2>/dev/null; then
        notify WARN "本次仅 ${tweets} 条推文，请检查上游 follow-builders feed"
        echo "[health_check] WARN: tweets=${tweets}，推文数量异常偏低"
    fi
    lag=$(jsonl_field feed_lag_h 0)
    if [ -n "$lag" ] && [ "${lag%%.*}" -gt 36 ] 2>/dev/null; then
        notify WARN "上游 feed 已滞后 ${lag} 小时，GitHub Actions 可能断了"
        echo "[health_check] WARN: feed_lag_h=${lag}"
    fi
}

source "$DIR/../shared/health_check_base.sh"
