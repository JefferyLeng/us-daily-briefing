# 项目速查 (MFM/automation)

每日金融简报自动化推送系统。三个推送任务，**触发/部署链路不同，改完代码后生效方式也不同**。

## 三个推送任务

| 任务 | 主脚本 | 内容 | 飞书卡片专区 |
|---|---|---|---|
| 美股简报 | `us_daily_briefing.py` | 美股指数/板块/涨跌榜/中概股/风向标/存储&CPO概念股 | 多专区 |
| 港股简报 | `hk_daily_briefing.py` | 港股指数/涨跌榜/南向资金等（分上午/下午场） | 多专区 |
| 研报速递 | `research_report.py` | 东财10 + 慧博10 + 艾瑞10 = 30 篇研报+AI总结 | 三源专区 |

## 触发与部署链路（关键！）

### 美股 / 港股 — cron-job.org → GitHub Actions
```
cron-job.org 定时 → 调 GitHub API workflow_dispatch
                → Actions runner checkout 仓库代码运行
```
- workflow 文件：`.github/workflows/briefing.yml`（美股）、`hk-briefing.yml`（港股）
- 仅 `workflow_dispatch` 触发（schedule 已移除）
- **代码 push 到 GitHub 即生效**，无需其他操作
- 港股分上午场/下午场，由触发时间（UTC 小时）判断

### 研报速递 — 阿里云函数计算 FC
```
FC 定时触发器（北京时间 11:00 + 17:00）
       → cloud/main.py handler → subprocess 跑 research_report.py
```
- 入口：`cloud/main.py`，通过环境变量 `TARGET_SCRIPT` 指定脚本
- **代码打包在 `cloud/deploy.zip`**，FC 上传后才生效
- 改完代码必须执行：
  1. `bash cloud/package.sh` — 重建 deploy.zip
  2. 手动上传 `cloud/deploy.zip` 到阿里云 FC 控制台

## 关键配置

- `config.json`（gitignore）：飞书 webhook、DeepSeek key、SocialData key、各任务参数
- `sent_*.json`（gitignore）：去重文件，7 天/14 天滚动窗口
  - `sent_reports.json` (东财) / `sent_hibor.json` / `sent_iresearch.json` / `sent_tweets.json`
  - **FC 代码目录只读**，去重文件路径用 `_resolve_sent_path()` 自动回退到 `/tmp`
- GitHub Secrets：`FEISHU_WEBHOOK_URL`、`DEEPSEEK_API_KEY`、`SOCIALDATA_API_KEY`
- FC 环境变量：同上 + `TARGET_SCRIPT`

## 重要约束 / 历史陷阱

1. **研报只推当天内容**：东财 `beginTime=today`、慧博按 `publishDate.startswith(today)` 严格过滤、艾瑞 `days=1`。三源全空时跳过推送不发空卡片。
2. **慧博桌面站 `www.hibor.com.cn` 有 TLS 指纹反爬**：
   - Python `requests` 会被 302 到反爬提示页
   - 解决：`_hibor_http_get()` 优先用 `curl` 子进程
   - UA 必须是 `Chrome/120.0`（两段版本号），WAF 的正则会拒绝 `Chrome/120.0.0.0`（三段）
3. **慧博数据源**：`https://www.hibor.com.cn/elitelist.html` "全部研报" tab（每日精选），不再用移动站的分类页面
4. **飞书卡片 28KB 上限**：30 篇研报满载约 10-11KB，安全
5. **周末跳过研报推送**：`research_report.py` 默认周六日不跑，`--force` 强制
6. **yfinance 新版返回 DataFrame 而非 Series**：港股指数解析需用 `.iloc[0]` 或类似处理
7. **GitHub Actions 的研报 workflow `research-report.yml` 已停用 schedule**，仅留 `workflow_dispatch` 备用通道

## 风向标股票（BELLWETHERS）

定义在 `us_daily_briefing.py`，包含 AAPL/MSFT/AMZN/GOOGL/META/NVDA/TSLA/LITE/MRVL/GLW/AVGO/**SPCX**。
- SPCX = SpaceX，2026-06-12 在纳斯达克 IPO，IPO 价 $135
- 同步在 `STOCK_CN` 字典加中文名映射

## 本地测试

```bash
# 美股（带飞书推送）
python3 us_daily_briefing.py --force

# 港股（上午/下午场）
python3 hk_daily_briefing.py --force --session morning
python3 hk_daily_briefing.py --force --session afternoon

# 研报（dry-run 不推送，验证抓取/卡片）
python3 research_report.py --dry-run --force

# 研报（实际推送）
python3 research_report.py --force
```

## 改代码后部署清单

| 任务 | push GitHub | 重建 deploy.zip | 上传 FC |
|---|---|---|---|
| 美股 | ✅ 即生效 | — | — |
| 港股 | ✅ 即生效 | — | — |
| 研报 | ✅（备用通道） | ✅ 必需 | ✅ 必需 |
