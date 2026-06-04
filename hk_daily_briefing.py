#!/usr/bin/env python3
"""港股每日早报 - 飞书自动推送（上午/下午收盘各一次）"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

os.environ.setdefault("no_proxy", "*")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---- 常量 ----

# 港股主要指数
HK_INDICES = {
    "^HSI": "恒生指数",
    "513180.SS": "恒生科技ETF",
}

# 板块代表性股票（每组计算平均涨跌幅）
HK_SECTORS = {
    "金融": {
        "0005.HK": "汇丰控股", "1299.HK": "友邦保险", "0388.HK": "港交所",
        "1398.HK": "工商银行", "3988.HK": "中国银行", "0939.HK": "建设银行",
        "2388.HK": "中银香港", "2628.HK": "中国人寿",
    },
    "地产": {
        "0001.HK": "长和", "0016.HK": "新鸿基", "0017.HK": "新世界发展",
        "1109.HK": "华润置地", "0688.HK": "中国海外发展", "1113.HK": "长实集团",
    },
    "消费": {
        "0027.HK": "银河娱乐", "1928.HK": "金沙中国", "0291.HK": "华润啤酒",
        "0175.HK": "吉利汽车", "2020.HK": "安踏体育", "9633.HK": "农夫山泉",
    },
    "科技": {
        "0700.HK": "腾讯控股", "9999.HK": "网易-S", "3690.HK": "美团-W",
        "1024.HK": "快手-W", "2518.HK": "汽车之家-S",
    },
    "能源资源": {
        "0883.HK": "中海油", "0857.HK": "中石油", "0267.HK": "中信股份",
        "3993.HK": "洛阳钼业", "6030.HK": "中信证券",
    },
    "医疗保健": {
        "2269.HK": "药明生物", "1177.HK": "中国生物制药", "1093.HK": "石药集团",
        "6160.HK": "百济神州", "2359.HK": "药明康德",
    },
    "电信": {
        "0728.HK": "中国电信", "0762.HK": "中国联通", "0941.HK": "中国移动",
    },
    "公用事业": {
        "0002.HK": "中电控股", "0006.HK": "电能实业", "0003.HK": "中华煤气",
    },
    "工业制造": {
        "1211.HK": "比亚迪", "2313.HK": "申洲国际", "0285.HK": "比亚迪电子",
        "6690.HK": "海尔智家",
    },
}

# 恒生科技成分股
HANGSENG_TECH = {
    "0700.HK": "腾讯控股", "9999.HK": "网易-S", "3690.HK": "美团-W",
    "9988.HK": "阿里巴巴-W", "9618.HK": "京东集团-SW", "9888.HK": "百度集团-SW",
    "1024.HK": "快手-W", "9868.HK": "小鹏汽车-W", "9866.HK": "蔚来-SW",
    "2015.HK": "理想汽车-W", "9626.HK": "哔哩哔哩-W", "1810.HK": "小米集团-W",
    "0981.HK": "中芯国际", "9961.HK": "携程集团-S", "0772.HK": "阅文集团",
    "0268.HK": "金山软件", "0285.HK": "比亚迪电子", "1211.HK": "比亚迪",
    "6690.HK": "海尔智家", "1347.HK": "华虹半导体",
    "0241.HK": "阿里健康",
}

# 用于筛选涨幅跌幅的宽基股票池
HK_BROAD_LIST = {}
for sector_stocks in HK_SECTORS.values():
    HK_BROAD_LIST.update(sector_stocks)
HK_BROAD_LIST.update(HANGSENG_TECH)
# 补充更多热门股
HK_BROAD_LIST.update({
    "0001.HK": "长和", "0012.HK": "恒基兆业",
    "0016.HK": "新鸿基", "0023.HK": "东亚银行", "0066.HK": "港铁公司",
    "0175.HK": "吉利汽车", "0267.HK": "中信股份", "0288.HK": "万洲国际",
    "0386.HK": "中国石化", "0669.HK": "创科实业", "0823.HK": "领展房产",
    "0868.HK": "信义玻璃", "0941.HK": "中国移动", "0960.HK": "龙湖集团",
    "0968.HK": "信达生物", "1038.HK": "长江基建", "1044.HK": "恒安国际",
    "1177.HK": "中国生物制药", "1288.HK": "农业银行", "1766.HK": "中国财险",
    "1876.HK": "百威亚太", "1997.HK": "九龙仓置业", "2007.HK": "碧桂园",
    "2020.HK": "安踏体育", "2269.HK": "药明生物", "2313.HK": "申洲国际",
    "2382.HK": "舜宇光学科技", "2388.HK": "中银香港", "2628.HK": "中国人寿",
    "2688.HK": "新奥能源", "3328.HK": "交通银行", "3968.HK": "招商银行",
    "3988.HK": "中国银行", "6098.HK": "碧桂服", "6160.HK": "百济神州",
    "6862.HK": "海底捞", "9618.HK": "京东集团-SW", "9633.HK": "农夫山泉",
    "9698.HK": "万国数据-SW", "9901.HK": "新东方-S", "9969.HK": "诺诚健华",
    "9999.HK": "网易-S",
})

# ---- 配置 ----

def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

# ---- 工具 ----

def _fmt_pct(pct):
    if pct > 0:
        return f"+{pct:.2f}% ▲"
    elif pct < 0:
        return f"{pct:.2f}% ▼"
    return f"{pct:.2f}%"

# ---- 数据获取 ----

def fetch_indices():
    """获取港股主要指数"""
    tickers = list(HK_INDICES.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None
    results = []
    for ticker in tickers:
        name = HK_INDICES[ticker]
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            results.append({
                "name": name, "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception as e:
            log.warning("获取指数 %s 失败: %s", name, e)
    return results


def fetch_sector_performance():
    """获取板块表现（按代表性股票平均涨跌幅）"""
    # 收集所有股票代码
    all_tickers = set()
    for stocks in HK_SECTORS.values():
        all_tickers.update(stocks.keys())
    all_tickers = list(all_tickers)

    data = yf.download(all_tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for sector_name, stocks in HK_SECTORS.items():
        changes = []
        for ticker in stocks:
            try:
                close = data["Close"][ticker].dropna()
                if len(close) < 2:
                    continue
                last = close.iloc[-1]
                prev = close.iloc[-2]
                changes.append((last - prev) / prev * 100)
            except Exception:
                pass
        if changes:
            avg_change = sum(changes) / len(changes)
            results.append({
                "name": sector_name,
                "count": len(changes),
                "change_pct": round(avg_change, 2),
            })

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results


def fetch_top_gainers_losers(size=20):
    """从宽基股票池中筛选涨跌幅 Top N"""
    tickers = list(HK_BROAD_LIST.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return [], []

    stock_changes = []
    for ticker in tickers:
        name = HK_BROAD_LIST.get(ticker, ticker)
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            stock_changes.append({
                "name": name, "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception:
            pass

    stock_changes.sort(key=lambda x: x["change_pct"], reverse=True)
    gainers = stock_changes[:size]
    losers = sorted(stock_changes, key=lambda x: x["change_pct"])[:size]
    return gainers, losers


def fetch_hangsend_tech():
    """获取恒生科技成分股表现"""
    tickers = list(HANGSENG_TECH.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker in tickers:
        name = HANGSENG_TECH.get(ticker, ticker)
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            results.append({
                "name": name, "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception as e:
            log.warning("获取 %s 失败: %s", ticker, e)

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results

# ---- 格式化 ----

def _fmt_stock_line(s):
    return f"{s['name']}({s['ticker'].replace('.HK','')})  {s['close']:,.2f}  {_fmt_pct(s['change_pct'])}"


def build_feishu_card(session, indices, sectors, gainers, losers, hstech):
    """构建飞书卡片"""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    session_label = "上午收盘" if session == "morning" else "下午收盘"
    elements = []

    # 指数
    if indices:
        lines = [f"**📊 港股主要指数 ({session_label})**\n"]
        for idx in indices:
            lines.append(f"{idx['name']}  {idx['close']:>10,.2f}  {_fmt_pct(idx['change_pct'])}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📊 港股主要指数 ({session_label})** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 板块
    if sectors:
        lines = ["**🏭 板块表现（按平均涨跌幅排序）**\n"]
        for s in sectors:
            lines.append(f"{s['name']}({s['count']}只)  {_fmt_pct(s['change_pct'])}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🏭 板块表现** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 涨幅
    if gainers:
        lines = ["**🚀 涨幅 Top 20**\n"]
        for i, s in enumerate(gainers, 1):
            lines.append(f"{i}. {_fmt_stock_line(s)}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🚀 涨幅排行** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 跌幅
    if losers:
        lines = ["**📉 跌幅 Top 20**\n"]
        for i, s in enumerate(losers, 1):
            lines.append(f"{i}. {_fmt_stock_line(s)}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**📉 跌幅排行** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 恒生科技
    if hstech:
        lines = ["**🔬 恒生科技成分股**\n"]
        for s in hstech:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🔬 恒生科技成分股** — 数据暂不可用"}})

    # 页脚
    elements.append({"tag": "hr"})
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"生成时间: {now_str}"},
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"港股简报 | {today} {session_label}"},
                "template": "green",
            },
            "elements": elements,
        },
    }

# ---- 推送 ----

def send_to_feishu(card_data, webhook_url, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(card_data, ensure_ascii=False).encode("utf-8"),
                timeout=30,
            )
            result = resp.json()
            if resp.status_code == 200 and result.get("code") == 0:
                log.info("飞书推送成功")
                return True
            log.warning("飞书返回错误 (attempt %d/%d): %s", attempt, max_retries, result)
        except Exception as e:
            log.warning("飞书推送异常 (attempt %d/%d): %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(5)
    log.error("飞书推送失败，已达最大重试次数")
    return False

# ---- 主流程 ----

def main():
    parser = argparse.ArgumentParser(description="港股每日简报 - 飞书推送")
    parser.add_argument("--session", choices=["morning", "afternoon"], default=None,
                        help="指定推送时段：morning(上午收盘) 或 afternoon(下午收盘)")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略周末）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")

    # 自动判断时段
    if args.session:
        session = args.session
    else:
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        session = "morning" if now_bj.hour < 14 else "afternoon"

    # 周末检查
    if not args.force and datetime.now(timezone(timedelta(hours=8))).weekday() >= 5:
        log.info("今天不是交易日（周末），跳过。使用 --force 强制运行。")
        return

    log.info("开始获取港股数据... (时段: %s)", session)

    indices = None
    try:
        indices = fetch_indices()
        log.info("指数数据: %d 条", len(indices) if indices else 0)
    except Exception as e:
        log.error("获取指数数据失败: %s", e)

    sectors = None
    try:
        sectors = fetch_sector_performance()
        log.info("板块数据: %d 条", len(sectors) if sectors else 0)
    except Exception as e:
        log.error("获取板块数据失败: %s", e)

    gainers, losers = [], []
    try:
        gainers, losers = fetch_top_gainers_losers(size=20)
        log.info("涨幅: %d, 跌幅: %d", len(gainers), len(losers))
    except Exception as e:
        log.error("获取涨跌幅排行失败: %s", e)

    hstech = None
    try:
        hstech = fetch_hangsend_tech()
        log.info("恒生科技数据: %d 条", len(hstech) if hstech else 0)
    except Exception as e:
        log.error("获取恒生科技数据失败: %s", e)

    card = build_feishu_card(session, indices, sectors, gainers, losers, hstech)

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - 飞书卡片内容预览")
        print("=" * 60)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not webhook_url or "在此粘贴" in webhook_url:
        log.error("飞书 Webhook URL 未配置，请在 config.json 中设置")
        sys.exit(1)

    success = send_to_feishu(card, webhook_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
