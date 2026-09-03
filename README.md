# AI Builder Bot

每天把 X 上二十几位 AI builder（研究员 / 创始人 / 产品 / 工程）本人发的推文整理成一份
中文播报，每条带原推直链，推送到飞书。

## 它在「AI 日报」里的位置

2026-09-03 起飞书群里只有三个机器人。本 bot 的稿子推送到 AI 产业日报的同一个 webhook，
群里显示为「AI 产业日报」，和另外两块一起构成 AI 板块：

| 块 | 播什么 | 覆盖方式 |
|---|---|---|
| AI 产业日报 | 今天 AI 圈发生了哪些事 | 广度层，一天十来条 |
| X 热点播报 | 当天最大那件事在 X 上被怎么吵的 | 深度层，一天一到两个话题 |
| AI Builder（本 bot） | 这二十几个人本人说了什么 | 人物层，原推与原话 |

同一件事三块都碰得到不算重复。以 2026-09-01 的 Fable 5.1 为例：产业日报播发布和基准
分数，X 热点播第三方评测与限流吐槽，本 bot 播 @bcherny 本人出来解释安全策略的干预降了 85%。本 bot 的每一条都必须落在「某个具体的人说了什么」上，不写成产业新闻的缩写版。

## 数据来源

素材是 [zarazhangrui/follow-builders](https://github.com/zarazhangrui/follow-builders)
仓库里由 GitHub Actions 每天生成的静态 feed：

```
https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json
```

零 API key、零依赖，就是一个 raw.githubusercontent.com 上的 JSON（拉取需走代理）。
名单在上游的 `config/default-sources.json`，共 26 个账号，当天谁发推谁进稿
（2026-09-02 那期是 16 位 / 38 条）。大头是 Anthropic 和 OpenAI 一线做事的人，
外加 Vercel / Box / Replit / Meta 的高管和几个 VC。

> 名单我们改不了：自己生成 feed 需要 `X_BEARER_TOKEN`（付费 X API），本 bot 只消费
> 上游跑好的结果。要加人得自备 X API key 另起一套。

## 三条改动前必读的约束

1. feed 永远比我们早一天。上游 CI 是北京时间 14:17 生成当天的 feed，而 routine 10:00
   就跑，所以拿到的永远是前一天那份，覆盖前天下午到昨天下午。稿子开头必须写清这批
   推文发生在哪一天，不能写成「今天」。fetch 会把 `generatedAt`、推文时间区间、滞后
   小时数打进 context，滞后超 36h 另给 `=== FEED_STALE ===` 标记。想拿当天的 feed，
   唯一办法是把 routine 挪到 14:30 之后。
2. likes/retweets/replies 是真实互动量，可以直说。这点和 x-hotspot-bot 正好相反——
   那边的「被引用次数」是复盘引用次数，是讨论中心度的代理指标，写成点赞就是骗人。
   两边 prompt 都写死了各自口径，别互相抄。
3. 去重键是推文 URL，跨天档案在 `logs/sent_urls.json`。上游 CI 万一某天没跑成，
   raw 上还是昨天那份 feed，不去重就会把同一批推文再播一遍。

## 用法

```bash
bash claude_report.sh fetch   # 取材，把 context 打到 stdout（供 Claude 写稿，零 API）
bash claude_report.sh send    # 读 logs/report_draft.txt 并推送飞书
```

写稿规范见 [`prompt.md`](./prompt.md)，由 10:00 的 Claude 定时任务读取。
send 有一道硬闸门：稿件里没有任何 `x.com/<user>/status/<id>` 原推直链就直接 `[FAIL]` 拒发——
一份只有转述、没有原话的 builder 播报没有存在意义。

## fetch 的 stdout 标记

| 标记 | 含义 |
|---|---|
| `=== FETCH_OK ===` | 取材成功 |
| `=== NO_TWEETS ===` | feed 里没有新推文（多半上游 CI 未更新），当天不出稿 |
| `=== FEED_ERROR ===` | feed 拉不到，当天不出稿 |
| `=== FEED_STALE ===` | 滞后超阈值，照常出稿但要写明实际日期 |
| `=== SKIP_ALREADY_RAN ===` | 今天已推送过 |
| `=== SKIP_PROXY ===` | 本地代理不可用 |

## 环境变量

从 `~/Library/LaunchAgents/com.shirley.ai-builder-bot.plist` 读取（plist 含密钥，不入库）：

| 变量 | 用途 |
|---|---|
| `FEISHU_WEBHOOK` | 填 AI 产业日报那个群机器人的地址（三块共用） |
| `FEISHU_ALERT_WEBHOOK` | 运维告警走的监测机器人，不进日报群 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 拉 raw.githubusercontent.com 必须 |
| `BUILDER_FEED_URL` | 覆盖 feed 地址，一般不用 |
| `BUILDER_FEED_STALE_H` | 滞后告警阈值，默认 36 |
| `BUILDER_PER_ACCOUNT_CAP` | 每人最多进几条，默认 4，防话痨账号刷屏 |

## 运维

- 体检：launchd `com.shirley.ai-builder-bot-health`，每天 11:00
- 自愈 / 无头补跑：`auto_repair.sh` / `claude_catchup.sh`，逻辑在 `../shared/` 里
- 共享层：`../shared/bot_utils.py`，改那里所有 bot 同时生效

换机与排障见 [TROUBLESHOOTING.md](https://github.com/shirleyisdoingrightthings/bot-ops/blob/main/TROUBLESHOOTING.md)。
