# AI Builder Bot

Daily digest of first-hand tweets and engineering blog posts from leading AI researchers, founders, and builders, delivered to Feishu.

## Workflow

```text
Follow-Builders Feed (X + Blogs) + Curated Builder Whitelist + Personal RSS
  │
  ▼
1. Fetch & Filter (builder_report.py --mode fetch)
  ├─ Pull daily feed generated at 14:17 CST (covering the past 24 hours)
  ├─ Filter out institutional/VC/tutorial noise accounts (EXCLUDED_HANDLES)
  ├─ Supplement with 15 whitelisted builders from AINews & 5 personal engineering blogs
  └─ Deduplicate against 7-day sent history (sent_urls.json) & apply per-author caps
  │
  ▼
2. Draft Report (Scheduled Agent at 15:00 CST)
  ├─ Read logs/last_context.txt and prompt.md
  ├─ Group into 4–6 builder-centric topics focusing on concrete technical insights
  └─ Include real engagement metrics (likes) and original x.com post links
  │
  ▼
3. Validate & Deliver (builder_report.py --mode send)
  ├─ Enforce hard gate: require valid x.com/<user>/status/<id> original tweet links
  ├─ Convert to Feishu card Markdown & auto-paginate if needed
  └─ Push to Feishu webhook, archive to Notion, and store sent tweet URLs
```

## Operation

1. **Fetch Materials**: Run `bash claude_report.sh fetch` to pull the latest builder tweets and blog posts into `logs/last_context.txt`.
2. **Generate Draft**: The agent reads `logs/last_context.txt` and `prompt.md`, writing the report to `logs/report_draft.txt`.
3. **Send to Feishu**: Run `bash claude_report.sh send` to verify links and push the report to Feishu.
