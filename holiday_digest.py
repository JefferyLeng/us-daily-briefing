#!/usr/bin/env python3
"""假期速递 - 假期最后一天（含周日晚）推送消息面汇总
1. 假期期间重要政策/消息汇总（新浪7x24翻页回溯 → 关键词过滤 → DeepSeek 提炼）
2. 假期外盘表现（恒生/恒生科技/日经/KOSPI/美股三大/欧洲/黄金/原油/离岸人民币）

触发判定：今天非交易日 且 明天是交易日（交易日历动态判定，其余日子静默退出）。
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

os.environ.setdefault("no_proxy", "*")

import requests

try:
    import akshare as ak
    import yfinance as yf
except ImportError as e:
    print("缺少依赖:", e)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("holiday")

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
DIGEST_PROMPT = """你是专业的财经编辑。以下是A股休假期间的快讯标题列表（已按政策/宏观相关性过滤）。
请提炼出对A股开盘影响最重要的5-8条消息，输出中文要点：

要求：
1. 每条不超过60字，按重要性排序
2. 合并同类事件，突出：中国政策(央行/国务院/部委)、海外央行、地缘、大宗商品异动
3. 每行一条，以 "- " 开头，不要加标题和额外说明
4. 若列表中某条与A股直接相关（如证监会/印花税/产业政策），优先置顶"""

# 政策/宏观关键词（新闻标题过滤）
POLICY_KW = re.compile(
    r"政策|央行|国务院|证监会|财政部|发改|工信|商务|住建|国资委|人社|中央|部委|监管|"
    r"关税|降准|降息|加息|美联储|联储|欧洲央行|日本央行|韩央行|印花税|汇率|人民币|"
    r"财政|货币|信贷|社融|GDP|PMI|CPI|政治局|会议|规划|补贴|免税|准入|改革|自贸|"
    r"地缘|制裁|原油|黄金|大宗", re.IGNORECASE)

# 外盘观察标的（yfinance 代码 → 中文名）
GLOBAL_MARKETS = {
    "^HSI": "恒生指数", "^HSTECH": "恒生科技", "^N225": "日经225",
    "^KS11": "韩国KOSPI", "^GSPC": "标普500", "^IXIC": "纳斯达克",
    "^DJI": "道琼斯", "^STOXX50E": "欧洲斯托克50",
    "GC=F": "COMEX黄金", "CL=F": "WTI原油", "CNH=X": "离岸人民币",
}


def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 假期判定
# ============================================================

def load_trade_calendar():
    """交易日历（新浪源，一年缓存到本地）。返回 set[date]。"""
    cache = SCRIPT_DIR / "trade_calendar.json"
    this_year = datetime.now(CST).year
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("year") == this_year:
                return {date.fromisoformat(d) for d in data.get("dates", [])}
        except Exception:
            pass
    df = ak.tool_trade_date_hist_sina()
    dates = {d if isinstance(d, date) else d.date() for d in df["trade_date"]}
    try:
        cache.write_text(json.dumps(
            {"year": this_year,
             "dates": [d.isoformat() for d in sorted(dates)
                       if d.year >= this_year]}, ensure_ascii=False))
    except Exception:
        pass
    return dates


def holiday_window(today, calendar):
    """判定今天是否假期最后一天。返回 (baseline, span_days) 或 None。

    baseline = 假期前最后一个交易日（外盘涨跌的基线日）
    """
    tomorrow = today + timedelta(days=1)
    if today in calendar:
        return None            # 今天是交易日，交给每日复盘
    if tomorrow not in calendar and tomorrow.weekday() >= 5:
        # 明天也可能只是普通周末 → 明天若在日历中才算
        return None
    if tomorrow not in calendar:
        return None            # 明天仍休市（假期中间）
    # 回溯假期起点：上一个交易日
    d = today - timedelta(days=1)
    while d not in calendar:
        d -= timedelta(days=1)
    span = (today - d).days
    return d, span


# ============================================================
# 外盘表现
# ============================================================

def fetch_global_performance(baseline_date, days):
    """假期窗口内外盘累计涨跌幅。返回 list[dict]。"""
    period_days = max(days + 15, 25)
    tickers = list(GLOBAL_MARKETS.keys())
    data = yf.download(tickers, period=f"{period_days}d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker, name in GLOBAL_MARKETS.items():
        try:
            close = data["Close"][ticker].dropna()
            if close.empty:
                continue
            dates = [ts.date() if hasattr(ts, "date") else ts for ts in close.index]
            # 基线：基线日当天或之前最近的收盘
            base_idx = None
            for i in range(len(dates) - 1, -1, -1):
                if dates[i] <= baseline_date:
                    base_idx = i
                    break
            if base_idx is None:
                continue
            base = float(close.iloc[base_idx])
            last = float(close.iloc[-1])
            chg = (last - base) / base * 100
            results.append({
                "name": name, "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(chg, 2),
                "is_fx": ticker == "CNH=X",   # 汇率显示价格而非涨跌幅
            })
        except Exception as e:
            log.warning("[外盘] %s 失败: %s", name, e)
    return results or None


# ============================================================
# 政策新闻（新浪7x24 主源 + 东财全球资讯备源，均支持回溯翻页）
# ============================================================

def _sina_news_page(page, size=50):
    url = "https://zhibo.sina.cn/api/zhibo/feed"
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
               "Referer": "https://finance.sina.com.cn/7x24/"}
    params = {"page": page, "page_size": size, "zhibo_id": "152",
              "tag_id": "0", "dire": "f", "dpc": "1"}
    r = requests.get(url, params=params, headers=headers, timeout=10)
    return r.json().get("result", {}).get("data", {}).get("feed", {}).get("list", [])


def _em_news_page(sort_end="", size=50):
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Referer": "https://kuaixun.eastmoney.com/"}
    params = {"client": "web", "biz": "web_724", "fastColumn": "102",
              "sortEnd": str(sort_end), "pageSize": size,
              "req_trace": str(uuid.uuid4())}
    r = requests.get(url, params=params, headers=headers, timeout=10)
    return r.json().get("data", {}).get("fastNewsList", [])


def fetch_policy_news(window_start, window_end, max_pages=60):
    """回溯翻页抓取假期窗口内快讯，按政策关键词过滤。

    window_start/end: datetime（统一转 naive 比较）。返回 [{time, title}] 按时间倒序。
    """
    # 快讯时间为 naive 字符串，统一去掉 tzinfo 再比较
    window_start = window_start.replace(tzinfo=None)
    window_end = window_end.replace(tzinfo=None)
    # ---- 主源：新浪 7x24 ----
    items, seen = [], set()
    try:
        for page in range(1, max_pages + 1):
            rows = _sina_news_page(page)
            if not rows:
                break
            stop = False
            for r in rows:
                t = str(r.get("create_time", ""))[:19]
                if not t:
                    continue
                try:
                    dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if dt < window_start:
                    stop = True
                    break
                if dt <= window_end:
                    key = str(r.get("id") or r.get("rich_text", ""))[:80]
                    if key not in seen:
                        seen.add(key)
                        items.append({"time": t, "title": r.get("rich_text", "")})
            if stop:
                break
            time.sleep(0.5)
        log.info("[新闻] 新浪回溯 %d 条(窗口内)", len(items))
    except Exception as e:
        log.warning("[新闻] 新浪翻页失败: %s", e)

    # ---- 备源：东财全球资讯（主源失败或量太少时）----
    if len(items) < 30:
        try:
            em_items, cursor = [], ""
            for _ in range(max_pages):
                rows = _em_news_page(cursor)
                if not rows:
                    break
                stop = False
                for r in rows:
                    t = str(r.get("showTime", ""))[:19]
                    try:
                        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if dt < window_start:
                        stop = True
                        break
                    if window_start <= dt <= window_end:
                        title = r.get("title", "")
                        if title and (title, t) not in {(i["title"], i["time"]) for i in em_items}:
                            em_items.append({"time": t, "title": title})
                    cursor = r.get("realSort", "")
                if stop:
                    break
                time.sleep(0.8)
            log.info("[新闻] 东财回溯 %d 条(窗口内)", len(em_items))
            items = em_items if len(em_items) > len(items) else items
        except Exception as e:
            log.warning("[新闻] 东财翻页失败: %s", e)

    # 关键词过滤 + 时间倒序
    hits = [i for i in items if POLICY_KW.search(i["title"])]
    hits.sort(key=lambda x: x["time"], reverse=True)
    log.info("[新闻] 政策相关 %d 条", len(hits))
    return hits


def summarize_with_deepseek(news, api_key):
    """政策新闻列表 → DeepSeek 要点。失败返回 None。"""
    if not api_key or not news:
        return None
    lines = [f"[{n['time'][5:16]}] {n['title'][:80]}" for n in news[:40]]
    user_msg = "假期快讯列表：\n" + "\n".join(lines)
    try:
        r = requests.post(
            DEEPSEEK_API,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": DIGEST_PROMPT},
                               {"role": "user", "content": user_msg}],
                  "temperature": 0.3, "max_tokens": 800},
            timeout=60,
        )
        d = r.json()
        out = (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return out or None
    except Exception as e:
        log.warning("[AI] DeepSeek 汇总失败: %s", e)
        return None


# ============================================================
# 卡片
# ============================================================

def _ball(pct):
    if pct > 0:
        return "🔴"
    if pct < 0:
        return "🟢"
    return "⚪"


def build_feishu_card(window_start, span_days, ai_summary, news, markets, date_str):
    elements = []

    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"**假期区间：{window_start.strftime('%m/%d')} - {date_str[5:].replace('-', '/')}（{span_days}天）**"}})

    # 一、政策/消息面
    lines = ["**一、假期政策与消息面**\n"]
    if ai_summary:
        lines.append(ai_summary)
    elif news:
        for n in news[:8]:
            lines.append(f"- {n['title'][:60]}")
    else:
        lines.append("- 近期快讯暂不可用")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    elements.append({"tag": "hr"})

    # 二、外盘表现
    if markets:
        lines = ["**二、假期外盘表现（期间累计）**\n"]
        idx = [m for m in markets if not m["is_fx"] and "COMEX" not in m["name"] and "WTI" not in m["name"]]
        fx = [m for m in markets if m["is_fx"]]
        cmd = [m for m in markets if "COMEX" in m["name"] or "WTI" in m["name"]]
        parts = [f"{_ball(m['change_pct'])}{m['name']} {m['change_pct']:+.2f}%" for m in idx]
        for k in range(0, len(parts), 3):
            lines.append("  ".join(parts[k:k + 3]))
        if cmd:
            lines.append("  ".join(f"{_ball(m['change_pct'])}{m['name'].replace('COMEX', '').replace('WTI', '')} "
                                   f"{m['change_pct']:+.2f}%" for m in cmd))
        if fx:
            f = fx[0]
            direction = "升值" if f["change_pct"] < 0 else "贬值"   # CNH 价格涨=人民币贬值
            lines.append(f"离岸人民币 {f['close']:.3f}（假期{direction} {abs(f['change_pct']):.2f}%）")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    elements.append({"tag": "hr"})
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"生成时间: {now_str}\n明早 8:50 美股日报 · 下午 17:00 A股复盘 照常推送"}})

    return {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text",
                             "content": f"🌙 假期速递 · 明日A股恢复交易 | {date_str}"},
                   "template": "indigo"},
        "elements": elements,
    }}


def send_to_feishu(card_data, webhook_url, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url,
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps(card_data, ensure_ascii=False).encode("utf-8"),
                                 timeout=30)
            result = resp.json()
            if resp.status_code == 200 and result.get("code") == 0:
                log.info("飞书推送成功")
                return True
            log.warning("飞书返回错误 (%d/%d): %s", attempt, max_retries, result)
        except Exception as e:
            log.warning("飞书推送异常 (%d/%d): %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(5)
    return False


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="假期速递 - 假期最后一天消息面汇总")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片，不发送")
    parser.add_argument("--force", action="store_true",
                        help="跳过假期判定强制运行（测试用，窗口=最近2天）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")
    api_key = config.get("deepseek_api_key", "")

    now = datetime.now(CST)
    today = now.date()

    # ---- 假期判定 ----
    if args.force:
        baseline = today - timedelta(days=2)
        while baseline.weekday() >= 5:
            baseline -= timedelta(days=1)
        span = 2
        log.info("[force 模式] baseline=%s", baseline)
    else:
        try:
            calendar = load_trade_calendar()
        except Exception as e:
            log.error("交易日历获取失败: %s", e)
            return
        win = holiday_window(today, calendar)
        if not win:
            log.info("今日(%s)不是假期最后一天，静默退出", today)
            return
        baseline, span = win

    date_str = today.strftime("%Y-%m-%d")
    window_start = datetime.combine(baseline, datetime.min.time()) + timedelta(hours=15)  # 基线日收盘后
    log.info("假期速递启动 | 假期: %s ~ %s (%d天)", baseline, today, span)

    # ---- 外盘 ----
    markets = None
    try:
        markets = fetch_global_performance(baseline, span)
        log.info("[外盘] %d 个标的", len(markets) if markets else 0)
    except Exception as e:
        log.error("[外盘] 获取失败: %s", e)

    # ---- 政策新闻 + AI 汇总 ----
    news = fetch_policy_news(window_start, now)
    ai_summary = summarize_with_deepseek(news, api_key)
    if ai_summary:
        log.info("[AI] 汇总完成 (%d 字)", len(ai_summary))
    elif news:
        log.info("[AI] 未生成，使用原始标题")

    # ---- 卡片 & 推送 ----
    card = build_feishu_card(window_start, span, ai_summary, news, markets, date_str)

    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not webhook_url:
        log.error("飞书 Webhook 未配置")
        sys.exit(1)
    success = send_to_feishu(card, webhook_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
