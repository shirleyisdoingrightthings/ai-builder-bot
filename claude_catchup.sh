#!/bin/bash
# claude_catchup.sh — AI Builder Bot
# 当天未成功出稿时的无头补跑（自动版 Run Now）
# 由 health_check.sh（MISSING 分支）或 auto_repair 最终兜底触发
# 补跑逻辑统一维护在同级 shared/headless_catchup_base.sh（路径由 $DIR 相对推导）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="AI Builder Bot"
PLIST="$HOME/Library/LaunchAgents/com.shirley.ai-builder-bot.plist"
WRITE_SPEC="严格按 prompt.md 写稿，写入 logs/report_draft.txt。硬性要求：每条话题都必须带至少一条 x.com 原推直链（一条都没有时 send 会拒发）；开头必须写明这批推文的实际日期，不能写成今天"

source "$DIR/../shared/headless_catchup_base.sh"
