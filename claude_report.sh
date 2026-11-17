#!/bin/bash
# claude_report.sh — 供定时任务调用的薄封装（委托 shared/report_base.sh）
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.shirley.ai-builder-bot.plist"
SCRIPT="builder_report.py"
USAGE_MSG="用法: claude_report.sh {fetch|send|test-list|archive-only} [--draft path]"
if [ -x "$DIR/.venv/bin/python" ]; then
    export BOT_PYTHON="$DIR/.venv/bin/python"
fi
source "$DIR/../shared/report_base.sh"
