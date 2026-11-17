# AI Builder Bot

Daily digest of first-hand tweets from a curated X List of AI researchers, founders, and engineers, delivered to Feishu and archived to Notion.

## Workflow

```text
Curated X List (via twscrape SearchTimeline `list:<id>` + cookie auth)
  │
  ▼
1. Fetch & Filter (claude_report.sh fetch -> .venv Python 3.12)
  ├─ Pull past 24h non-retweet / non-reply posts from X_LIST_ID via twscrape
  ├─ Expand t.co links, attach X Article titles & quoted tweet context
  └─ Deduplicate against 7-day sent history (sent_urls.json) & apply per-author caps
  │
  ▼
2. Draft Report (Scheduled Agent)
  ├─ Read logs/last_context.txt and prompt.md
  ├─ Group into 4–6 builder-centric topics with inline hyperlinks & real like counts
  └─ Write logs/report_draft.txt
  │
  ▼
3. Validate & Deliver (claude_report.sh send)
  ├─ Enforce hard gate: require valid x.com/<user>/status/<id> original tweet links
  ├─ Convert to Feishu card Markdown & auto-paginate if needed
  └─ Push to Feishu webhook, archive to Notion, and store sent tweet URLs
```

## Setup (Zero-Cost X List via `twscrape`)

1. Create an X List containing your curated AI builders and grab its `X_LIST_ID` (from `https://x.com/i/lists/<id>`).
2. Log into an X alt account in a browser and copy `auth_token` and `ct0` cookies from DevTools (`Application -> Cookies -> https://x.com`).
3. Copy `com.shirley.ai-builder-bot.plist.example` to `~/Library/LaunchAgents/com.shirley.ai-builder-bot.plist` and fill in `FEISHU_WEBHOOK`, `X_LIST_ID`, `X_AUTH_TOKEN`, and `X_CT0`.
4. Verify connectivity (read-only):

```bash
.venv/bin/python builder_report.py --mode test-list
```

## Operation

1. **Fetch Materials**: `bash claude_report.sh fetch`
2. **Send to Feishu**: `bash claude_report.sh send`
