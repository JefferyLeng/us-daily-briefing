#!/usr/bin/env python3
"""美股每日早报 - 飞书自动推送"""

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

MAJOR_INDICES = {
    "^DJI": "道琼斯",
    "^IXIC": "纳斯达克",
    "^GSPC": "标普500",
    "^SOX": "费城半导体",
}

# 扩展指数：随网页版报告一起加入（纳指100 / VIX / 金龙指数）
EXTENDED_INDICES = {
    "^NDX": "纳斯达克100",
    "^VIX": "VIX恐慌指数",
    "^HXC": "中国金龙指数",   # Yahoo 常无数据，回退用 PGJ（Invesco 中国金龙 ETF）
}
# 金龙指数回退链：^HXC 无数据时用 PGJ ETF 代替
FALLBACK_TICKERS = {"^HXC": ("PGJ", "中国金龙指数")}

# 网页版报告托管地址（GitHub Pages，随 gh-pages 分支部署）
PAGES_BASE_URL = "https://jefferyleng.github.io/us-daily-briefing/"

SECTOR_ETFS = {
    "XLK": "科技",
    "XLF": "金融",
    "XLE": "能源",
    "XLV": "医疗保健",
    "XLY": "可选消费",
    "XLP": "必需消费",
    "XLI": "工业",
    "XLB": "材料",
    "XLU": "公用事业",
    "XLRE": "房地产",
    "XLC": "通信",
}

# 风向标股票
BELLWETHERS = {
    "AAPL": "苹果", "MSFT": "微软", "AMZN": "亚马逊", "GOOGL": "谷歌",
    "META": "Meta", "NVDA": "英伟达", "TSLA": "特斯拉", "LITE": "Lumentum",
    "MRVL": "迈威尔", "GLW": "康宁", "AVGO": "博通", "SPCX": "SpaceX",
    "AMD": "超微半导体", "SKHY": "海力士",
}

# 存储概念股
STORAGE_STOCKS = {
    "MU": "美光科技", "WDC": "西部数据", "STX": "希捷",
    "PSTG": "Pure Storage", "NTAP": "NetApp", "SIMO": "Silicon Motion",
    "SMCI": "超微电脑", "SKHY": "海力士",
}

# CPO 概念股（光电共封装）
CPO_STOCKS = {
    "LITE": "Lumentum", "FN": "Fabrinet", "COHR": "相干公司",
    "MRVL": "迈威尔科技", "AVGO": "博通", "GLW": "康宁",
    "AAOI": "Applied Optoelectronics", "ACMR": "ACM Research",
}

# 云计算厂商
CLOUD_STOCKS = {
    "MSFT": "微软Azure", "AMZN": "亚马逊AWS", "GOOGL": "谷歌云",
    "ORCL": "甲骨文", "CRWV": "CoreWeave", "NBIS": "Nebius",
    "NOW": "ServiceNow", "SNOW": "Snowflake",
    "NET": "Cloudflare", "DDOG": "Datadog",
}

DEFAULT_CHINESE_ADRS = {
    "BABA": "阿里巴巴", "JD": "京东", "PDD": "拼多多",
    "BIDU": "百度", "NIO": "蔚来", "LI": "理想汽车",
    "XPEV": "小鹏汽车", "BILI": "哔哩哔哩", "TME": "腾讯音乐",
    "FUTU": "富途", "TIGR": "老虎证券", "NTES": "网易",
    "BZ": "Boss直聘", "ZTO": "中通快递", "VIPS": "唯品会",
    "IQ": "爱奇艺", "TAL": "好未来", "EDU": "新东方",
}

# 英文板块 → 中文
SECTOR_CN = {
    "Technology": "科技", "Healthcare": "医疗保健",
    "Financial Services": "金融服务", "Consumer Cyclical": "可选消费",
    "Consumer Defensive": "必需消费", "Energy": "能源",
    "Industrials": "工业", "Basic Materials": "基础材料",
    "Communication Services": "通信服务", "Utilities": "公用事业",
    "Real Estate": "房地产", "Financial": "金融",
    "Health Care": "医疗保健", "Consumer Discretionary": "可选消费",
    "Consumer Staples": "必需消费", "Materials": "材料",
}

# 热门美股中文名映射
STOCK_CN = {
    # 科技巨头
    "AAPL": "苹果", "MSFT": "微软", "GOOGL": "谷歌", "GOOG": "谷歌",
    "AMZN": "亚马逊", "NVDA": "英伟达", "META": "Meta Platforms", "TSLA": "特斯拉",
    "SPCX": "SpaceX",
    "BRK-B": "伯克希尔", "BRK.B": "伯克希尔",
    # 金融
    "JPM": "摩根大通", "BAC": "美国银行", "WFC": "富国银行", "GS": "高盛",
    "MS": "摩根士丹利", "C": "花旗", "BLK": "贝莱德", "SCHW": "嘉信理财",
    "AXP": "美国运通", "V": "Visa", "MA": "万事达", "PYPL": "PayPal",
    "COIN": "Coinbase", "HOOD": "Robinhood", "SOFI": "SoFi",
    # 医疗保健
    "UNH": "联合健康", "JNJ": "强生", "PFE": "辉瑞", "ABBV": "艾伯维",
    "MRK": "默克", "LLY": "礼来", "TMO": "赛默飞", "ABT": "雅培",
    "MRNA": "Moderna", "GILD": "吉利德", "AMGN": "安进", "REGN": "再生元",
    "VRTX": "福泰制药", "BIIB": "百健", "DHR": "丹纳赫", "ISRG": "直觉外科",
    "LEGN": "传奇生物", "AZN": "阿斯利康", "NVO": "诺和诺德",
    "SNPS": "新思科技", "CDNS": "楷登电子",
    "CELC": "Celcuity", "PRAX": "Praxis Precision", "KOD": "Kodiak Sciences",
    "KYMR": "Kymera Therapeutics", "ABVX": "Abivax", "VKTX": "Viking Therapeutics",
    "DNA": "Ginkgo Bioworks", "RXRX": "Recursion Pharma",
    # 半导体
    "INTC": "英特尔", "AMD": "超微半导体", "AVGO": "博通", "QCOM": "高通",
    "TXN": "德州仪器", "MU": "美光", "AMAT": "应用材料", "LRCX": "泛林半导体",
    "KLAC": "科磊", "MRVL": "迈威尔科技", "ON": "安森美", "MCHP": "微芯科技",
    "NXPI": "恩智浦", "STM": "意法半导体", "WOLF": "Wolfspeed", "COHR": "相干公司",
    "AEHR": "Aehr Test Systems", "CAMT": "Camtek", "PENG": "Penguin Solutions",
    "ARM": "ARM Holdings", "ASML": "ASML", "SNPS": "新思科技",
    "MPWR": "Monolithic Power", "SWKS": "Skyworks",
    "TER": "泰瑞达", "ENTG": "Entegris", "UCTT": "Ultra Clean",
    "LSCC": "Lattice Semiconductor", "RMBS": "Rambus",
    # 软件/云
    "ADBE": "Adobe", "CRM": "Salesforce", "ORCL": "甲骨文", "IBM": "IBM",
    "NOW": "ServiceNow", "INTU": "Intuit", "SNOW": "Snowflake", "CRWV": "CoreWeave", "NBIS": "Nebius",
    "PLTR": "Palantir", "CRWD": "CrowdStrike", "DDOG": "Datadog",
    "NET": "Cloudflare", "MDB": "MongoDB", "ZS": "Zscaler",
    "PANW": "Palo Alto Networks", "FTNT": "Fortinet",
    "OKTA": "Okta", "TTD": "The Trade Desk", "PATH": "UiPath",
    "FIG": "Figma", "SHOP": "Shopify", "SQ": "Block",
    "RNG": "RingCentral", "HUBS": "HubSpot", "DOCU": "DocuSign",
    "TEAM": "Atlassian", "WDAY": "Workday", "VEEV": "Veeva Systems",
    # 消费
    "AMZN": "亚马逊", "TSLA": "特斯拉", "HD": "家得宝", "NKE": "耐克",
    "MCD": "麦当劳", "SBUX": "星巴克", "DIS": "迪士尼", "NFLX": "奈飞",
    "KO": "可口可乐", "PEP": "百事", "WMT": "沃尔玛", "COST": "好市多",
    "TGT": "Target", "CVS": "CVS健康", "ABNB": "爱彼迎", "UBER": "Uber",
    "LYFT": "Lyft", "RIVN": "Rivian", "LCID": "Lucid Motors",
    "RBLX": "Roblox", "SNAP": "Snap", "ROKU": "Roku", "BABA": "阿里巴巴",
    "PINS": "Pinterest", "ETSY": "Etsy", "CHWY": "Chewy",
    "LULU": "Lululemon", "TJX": "TJX", "LOW": "Lowe's",
    # 能源
    "CVX": "雪佛龙", "XOM": "埃克森美孚", "COP": "康菲石油",
    "SLB": "斯伦贝谢", "EOG": "EOG能源", "OXY": "西方石油",
    "MPC": "Marathon Petroleum", "VLO": "Valero能源",
    "FANG": "Diamondback Energy", "TE": "T1 Energy",
    "ENPH": "Enphase Energy", "SEDG": "SolarEdge", "RUN": "Sunrun",
    # 工业
    "BA": "波音", "CAT": "卡特彼勒", "GE": "通用电气", "HON": "霍尼韦尔",
    "UNP": "联合太平洋", "RTX": "雷神技术", "LMT": "洛克希德马丁",
    "DE": "迪尔", "MMM": "3M", "UPS": "UPS", "FDX": "联邦快递",
    "EMR": "艾默生电气", "ETN": "伊顿", "PH": "Parker Hannifin",
    # 通信
    "TMUS": "T-Mobile", "T": "AT&T", "VZ": "Verizon",
    "WBD": "华纳兄弟探索", "PARA": "派拉蒙", "CMCSA": "康卡斯特",
    # 硬件/设备
    "HPE": "惠普企业", "DELL": "戴尔", "HPQ": "惠普",
    "APH": "安费诺", "GLW": "康宁",
    # 其他热门
    "MSTR": "MicroStrategy", "AI": "C3.ai",
    "IONQ": "IonQ", "RKLB": "Rocket Lab", "OPEN": "Opendoor",
    "HIMS": "Hims & Hers", "BRAI": "Braiin", "XMTR": "Xometry",
    "AFRM": "Affirm", "UPST": "Upstart", "RBLX": "Roblox",
    "S": "SentinelOne", "CRCT": "Cricut",
    "NNOX": "Nano Dimension", "JOBY": "Joby Aviation",
    "LUNR": "Intuitive Machines", "ASTS": "AST SpaceMobile",
    "RKLB": "Rocket Lab", "RDW": "Redwire",
    "KC": "Kingsoft Cloud", "DIDI": "滴滴",
    "GRRR": "Gorilla Technology", "SERV": "Serve Robotics",
    "MARA": "Marathon Digital", "RIOT": "Riot Platforms",
    "CLSK": "CleanSpark", "BTBT": "Bit Digital",
    "SMCI": "超微电脑", "DELL": "戴尔",
    "BKKT": "Bakkt Holdings", "HOLO": "MicroCloud Hologram",
    "OPTT": "Ocean Power Tech", "ARVL": "Arrival",
    "SOUN": "SoundHound AI", "BBAI": "BigBear.ai",
    "LTRN": " Lantern Pharma", "PTON": "Peloton",
    "ZIM": "以星航运", "SITM": "SiTime",
    "ALRM": "Alarm.com", "AVTE": "Aviat Networks",
}

# ---- 配置 ----

def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        log.info("请复制 config.json 模板并填入飞书 Webhook 地址")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

# ---- 数据获取 ----

def fetch_major_indices():
    """获取三大指数 + 半导体指数 + 扩展指数（纳指100/VIX/金龙）收盘数据。

    用 period="ytd" 一次下载，同时算出日涨跌（收盘 vs 前收）和
    年初至今涨跌（收盘 vs 年内首个收盘）。
    """
    all_indices = {**MAJOR_INDICES, **EXTENDED_INDICES}
    # 回退标的（如 ^HXC 的 PGJ）一并下载，主代码无数据时启用
    fallback_map = dict(FALLBACK_TICKERS)
    tickers = list(all_indices.keys()) + [fb for fb, _ in fallback_map.values()]
    data = yf.download(tickers, period="ytd", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker, name in all_indices.items():
        try:
            close = data["Close"][ticker].dropna()
            used = ticker
            if len(close) < 2 and ticker in fallback_map:
                # 主代码无数据 → 尝试回退标的
                fb_ticker, fb_name = fallback_map[ticker]
                fb_close = data["Close"][fb_ticker].dropna()
                if len(fb_close) >= 2:
                    close, used = fb_close, fb_ticker
                    name = fb_name
                    log.info("[指数] %s 无数据，回退用 %s", ticker, fb_ticker)
            if len(close) < 2:
                log.warning("[指数] %s(%s) 数据不足(%d条)，跳过", name, ticker, len(close))
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            ytd_pct = (last - close.iloc[0]) / close.iloc[0] * 100
            results.append({
                "name": name,
                "ticker": used,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
            })
        except Exception as e:
            log.warning("获取指数 %s 失败: %s", name, e)
    return results


def fetch_sector_performance():
    """获取 11 个板块 ETF 涨跌幅"""
    tickers = list(SECTOR_ETFS.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker in tickers:
        name = SECTOR_ETFS[ticker]
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            results.append({
                "name": name,
                "ticker": ticker,
                "change_pct": round(change_pct, 2),
            })
        except Exception as e:
            log.warning("获取板块 %s 失败: %s", name, e)

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results


def fetch_top_gainers_losers(size=10):
    """获取当日涨幅/跌幅最大的个股"""
    gainers = []
    losers = []

    try:
        result = yf.screen("day_gainers", count=size)
        quotes = result.get("quotes", [])
        for q in quotes[:size]:
            gainers.append(_parse_screener_quote(q))
    except Exception as e:
        log.warning("获取涨幅个股失败: %s", e)

    try:
        result = yf.screen("day_losers", count=size)
        quotes = result.get("quotes", [])
        for q in quotes[:size]:
            losers.append(_parse_screener_quote(q))
    except Exception as e:
        log.warning("获取跌幅个股失败: %s", e)

    return gainers, losers


def _parse_screener_quote(q):
    """解析 Screener 返回的个股数据"""
    symbol = q.get("symbol", "")
    display = q.get("displayName") or q.get("shortName") or symbol
    price = q.get("regularMarketPrice", 0)
    change_pct = q.get("regularMarketChangePercent", 0)
    volume = q.get("regularMarketVolume", 0)
    market_cap = q.get("marketCap", 0)

    return {
        "symbol": symbol,
        "cn_name": STOCK_CN.get(symbol, ""),
        "display_name": display,
        "sector": "",
        "price": round(float(price), 2) if price else 0,
        "change_pct": round(float(change_pct), 2) if change_pct else 0,
        "volume": int(volume) if volume else 0,
        "market_cap": int(market_cap) if market_cap else 0,
    }


def _enrich_sector_info(stocks):
    """批量获取个股板块信息"""
    if not stocks:
        return
    symbols = [s["symbol"] for s in stocks]
    for symbol in symbols:
        try:
            info = yf.Ticker(symbol).info
            sector_en = info.get("sector", "")
            sector_cn = SECTOR_CN.get(sector_en, sector_en)
            for s in stocks:
                if s["symbol"] == symbol:
                    s["sector"] = sector_cn
                    break
        except Exception as e:
            log.warning("获取 %s 板块信息失败: %s", symbol, e)


def fetch_chinese_adrs(adr_map):
    """获取中概股行情"""
    tickers = list(adr_map.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker in tickers:
        name = adr_map.get(ticker, ticker)
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                continue
            last = close.iloc[-1]
            prev = close.iloc[-2]
            change_pct = (last - prev) / prev * 100
            results.append({
                "name": name,
                "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception as e:
            log.warning("获取中概股 %s 失败: %s", ticker, e)

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results


def fetch_stock_group(name_map):
    """通用：获取一组股票的当日涨跌

    批量 download 拉收盘价,涨跌幅 = (今日收盘 - 昨日收盘) / 昨日收盘。
    用 close.iloc[-2] 作为"昨日收盘"——这是 yfinance 最可靠的口径,
    fast_info.previous_close 对新股(代码切换/上市初期)返回值不可靠。
    """
    tickers = list(name_map.keys())
    data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    if data.empty:
        return None

    results = []
    for ticker in tickers:
        name = name_map.get(ticker, ticker)
        try:
            close = data["Close"][ticker].dropna()
            if len(close) < 2:
                log.warning("%s(%s): 数据不足(%d条),跳过", name, ticker, len(close))
                continue
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            change_pct = (last - prev) / prev * 100
            results.append({
                "name": name,
                "ticker": ticker,
                "close": round(last, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception as e:
            log.warning("获取 %s(%s) 失败: %s", name, ticker, e)

    return results

# ---- 格式化 ----

def _fmt_pct(pct):
    """格式化百分比，美股惯例：绿涨红跌"""
    if pct > 0:
        return f"+{pct:.2f}% ▲"
    elif pct < 0:
        return f"{pct:.2f}% ▼"
    return f"{pct:.2f}%"


def _fmt_volume(vol):
    """格式化成交量"""
    if vol >= 1e9:
        return f"{vol/1e9:.1f}B"
    elif vol >= 1e6:
        return f"{vol/1e6:.1f}M"
    elif vol >= 1e3:
        return f"{vol/1e3:.1f}K"
    return str(vol)


def _fmt_cap(cap):
    """格式化市值"""
    if cap >= 1e12:
        return f"{cap/1e12:.1f}T"
    elif cap >= 1e9:
        return f"{cap/1e9:.1f}B"
    elif cap >= 1e6:
        return f"{cap/1e6:.1f}M"
    return str(cap)


def _fmt_stock_line(s):
    """格式化单只股票行：中文名(代码) $价格 涨跌幅"""
    return f"{s['name']}({s['ticker']})  ${s['close']:.2f}  {_fmt_pct(s['change_pct'])}"


def build_feishu_card(indices, sectors, gainers, losers, adrs, bellwethers, storage, cpo, cloud):
    """构建飞书交互式卡片消息"""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    elements = []

    # 大盘指数（含扩展指数与年初至今）
    if indices:
        lines = ["**📊 指数行情**\n"]
        extended_names = set(EXTENDED_INDICES.values())
        for idx in indices:
            line = f"{idx['name']}  {idx['close']:>10,.2f}  {_fmt_pct(idx['change_pct'])}"
            ytd = idx.get("ytd_pct")
            if ytd is not None:
                line += f"  年初{_fmt_pct(ytd)}"
            if idx["name"] in extended_names:
                line = "· " + line   # 扩展指数缩进区分
            lines.append(line)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**📊 指数行情** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 板块表现
    if sectors:
        lines = ["**🏭 板块表现（按涨跌幅排序）**\n"]
        for s in sectors:
            lines.append(f"{s['name']}({s['ticker']})  {_fmt_pct(s['change_pct'])}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🏭 板块表现** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 涨幅个股
    if gainers:
        lines = ["**🚀 涨幅 Top 20**\n"]
        for i, s in enumerate(gainers, 1):
            cn = f" {s['cn_name']}" if s.get("cn_name") else ""
            sector = f" [{s['sector']}]" if s.get("sector") else ""
            lines.append(
                f"{i}. {s['display_name']}({s['symbol']}){cn}{sector}  "
                f"${s['price']:.2f}  {_fmt_pct(s['change_pct'])}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🚀 涨幅排行** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 跌幅个股
    if losers:
        lines = ["**📉 跌幅 Top 20**\n"]
        for i, s in enumerate(losers, 1):
            cn = f" {s['cn_name']}" if s.get("cn_name") else ""
            sector = f" [{s['sector']}]" if s.get("sector") else ""
            lines.append(
                f"{i}. {s['display_name']}({s['symbol']}){cn}{sector}  "
                f"${s['price']:.2f}  {_fmt_pct(s['change_pct'])}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**📉 跌幅排行** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 中概股
    if adrs:
        lines = ["**🇨🇳 中概股行情**\n"]
        for s in adrs:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🇨🇳 中概股行情** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 风向标
    if bellwethers:
        lines = ["**🧭 风向标**\n"]
        for s in bellwethers:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🧭 风向标** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 存储概念股
    if storage:
        lines = ["**💾 存储概念股**\n"]
        for s in storage:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**💾 存储概念股** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # CPO 概念股
    if cpo:
        lines = ["**🔗 CPO概念股（光电共封装）**\n"]
        for s in cpo:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**🔗 CPO概念股** — 数据暂不可用"}})

    elements.append({"tag": "hr"})

    # 云计算厂商
    if cloud:
        lines = ["**☁️ 云计算厂商**\n"]
        for s in cloud:
            lines.append(_fmt_stock_line(s))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**☁️ 云计算厂商** — 数据暂不可用"}})

    # 页脚
    elements.append({"tag": "hr"})
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"生成时间: {now_str}\n[🌐 完整报告（网页版）]({PAGES_BASE_URL})"},
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"美股每日早报 | {today}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    return card

# ---- HTML 报告 ----

def _esc(s):
    """HTML 转义"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _h_pct(pct):
    """网页版涨跌幅：红涨绿跌（A 股配色），返回带 class 的 span"""
    if pct is None:
        return "<span class='flat'>--</span>"
    cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "")
    return f"<span class='{cls}'>{pct:+.2f}% {arrow}</span>"


def _h_table(rows, headers):
    """rows: list[list[str(HTML)]]; headers: list[str]"""
    h = "<div class='tbl-wrap'><table><thead><tr>"
    for hd in headers:
        h += f"<th>{_esc(hd)}</th>"
    h += "</tr></thead><tbody>"
    for row in rows:
        h += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    h += "</tbody></table></div>"
    return h


def build_html_report(indices, sectors, gainers, losers, adrs,
                      bellwethers, storage, cpo, cloud):
    """生成单文件 HTML 网页版早报（红涨绿跌，内联 CSS，移动端自适应）"""
    bj_now = datetime.now(timezone(timedelta(hours=8)))
    us_date = (bj_now - timedelta(days=1)).strftime("%Y-%m-%d")   # 报告覆盖的美股交易日
    date_str = bj_now.strftime("%Y-%m-%d")
    gen_time = bj_now.strftime("%Y-%m-%d %H:%M")

    css = """
    :root { --up:#e02020; --down:#0a8f4e; --flat:#666; --line:#e8e8ec;
            --bg:#f5f6f8; --card:#fff; --txt:#222; --sub:#777; }
    * { box-sizing:border-box; margin:0; padding:0; }
    body { font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
           background:var(--bg); color:var(--txt); line-height:1.6; padding:16px; }
    .wrap { max-width:860px; margin:0 auto; }
    header { text-align:center; padding:20px 12px 8px; }
    header h1 { font-size:22px; }
    header .sub { color:var(--sub); font-size:13px; margin-top:4px; }
    .idx-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
                 gap:10px; margin:16px 0; }
    .idx-card { background:var(--card); border:1px solid var(--line); border-radius:10px;
                padding:14px 12px; text-align:center; }
    .idx-card .nm { font-size:13px; color:var(--sub); }
    .idx-card .px { font-size:20px; font-weight:700; margin:2px 0; }
    .idx-card .meta { font-size:12px; }
    section { background:var(--card); border:1px solid var(--line); border-radius:10px;
              padding:16px; margin:14px 0; }
    section h2 { font-size:16px; margin-bottom:10px; border-left:4px solid #3b6fe0;
                 padding-left:8px; }
    .tbl-wrap { overflow-x:auto; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { padding:7px 8px; text-align:left; border-bottom:1px solid var(--line);
             white-space:nowrap; }
    th { color:var(--sub); font-weight:600; background:#fafafa; }
    tr:last-child td { border-bottom:none; }
    td.num, th.num { text-align:right; }
    .up { color:var(--up); font-weight:600; }
    .down { color:var(--down); font-weight:600; }
    .flat { color:var(--flat); }
    .tag { display:inline-block; background:#eef2fb; color:#3b6fe0; border-radius:4px;
           padding:0 6px; font-size:12px; }
    footer { text-align:center; color:var(--sub); font-size:12px; padding:16px 0 30px; }
    footer a { color:#3b6fe0; text-decoration:none; }
    @media (max-width:480px) { body{padding:8px} .idx-card .px{font-size:17px} }
    """

    h = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>美股早报 {date_str}</title><style>{css}</style></head><body><div class='wrap'>"]

    # 头部
    h.append(f"<header><h1>📈 每日美股早报</h1>"
             f"<div class='sub'>美股 {us_date}（美东）收盘 · 北京时间 {date_str} 推送</div></header>")

    # 指数大卡片（四大指数）
    majors = [i for i in (indices or []) if i["ticker"] in MAJOR_INDICES]
    if majors:
        h.append("<div class='idx-cards'>")
        for i in majors:
            ytd_html = _h_pct(i.get("ytd_pct"))
            h.append(f"<div class='idx-card'><div class='nm'>{_esc(i['name'])}</div>"
                     f"<div class='px'>{i['close']:,.2f}</div>"
                     f"<div class='meta'>{_h_pct(i['change_pct'])} · 年初{ytd_html}</div></div>")
        h.append("</div>")

    # 一、指数详情（含扩展指数）
    if indices:
        rows = []
        for i in indices:
            rows.append([_esc(i["name"]),
                         f"<span class='tag'>{_esc(i['ticker'])}</span>",
                         f"{i['close']:,.2f}",
                         _h_pct(i["change_pct"]),
                         _h_pct(i.get("ytd_pct"))])
        h.append("<section><h2>一、指数详情</h2>"
                 + _h_table(rows, ["指数", "代码", "收盘", "日涨跌", "年初至今"])
                 + "</section>")

    # 二、板块表现
    if sectors:
        rows = [[_esc(s["name"]), f"<span class='tag'>{_esc(s['ticker'])}</span>",
                 _h_pct(s["change_pct"])] for s in sectors]
        h.append("<section><h2>二、板块表现（GICS 行业 ETF）</h2>"
                 + _h_table(rows, ["板块", "代码", "日涨跌"]) + "</section>")

    # 三/四、涨跌幅 Top 20
    for title, items in (("三、涨幅 Top 20", gainers), ("四、跌幅 Top 20", losers)):
        if items:
            rows = []
            for n, s in enumerate(items, 1):
                nm = _esc(s.get("cn_name") or s["display_name"])
                rows.append([str(n), nm, f"<span class='tag'>{_esc(s['symbol'])}</span>",
                             _esc(s.get("sector") or ""), f"{s['price']:.2f}",
                             _h_pct(s["change_pct"])])
            h.append(f"<section><h2>{title}</h2>"
                     + _h_table(rows, ["#", "公司", "代码", "板块", "价格($)", "涨跌幅"])
                     + "</section>")

    # 五~九、股票组（中概/风向标/存储/CPO/云计算）
    groups = [
        ("五、中概股行情", adrs), ("六、风向标", bellwethers),
        ("七、存储概念股", storage), ("八、CPO 概念股（光电共封装）", cpo),
        ("九、云计算厂商", cloud),
    ]
    for title, items in groups:
        if items:
            rows = [[_esc(s["name"]), f"<span class='tag'>{_esc(s['ticker'])}</span>",
                     f"{s['close']:.2f}", _h_pct(s["change_pct"])] for s in items]
            h.append(f"<section><h2>{title}</h2>"
                     + _h_table(rows, ["名称", "代码", "价格($)", "涨跌幅"]) + "</section>")

    # 页脚
    h.append(f"<footer>生成时间 {gen_time} · 数据来源 Yahoo Finance · "
             f"<a href='{PAGES_BASE_URL}'>历史报告存档</a><br>"
             f"本报告仅供参考，不构成投资建议</footer>")

    h.append("</div></body></html>")
    return "".join(h)

def send_to_feishu(card_data, webhook_url, max_retries=3):
    """发送卡片到飞书 Webhook，带重试"""
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
            else:
                log.warning("飞书返回错误 (attempt %d/%d): %s", attempt, max_retries, result)
        except Exception as e:
            log.warning("飞书推送异常 (attempt %d/%d): %s", attempt, max_retries, e)

        if attempt < max_retries:
            time.sleep(5)

    log.error("飞书推送失败，已达最大重试次数")
    return False

# ---- 主流程 ----

def main():
    parser = argparse.ArgumentParser(description="美股每日早报 - 飞书推送 + 网页版报告")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略周末）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--html-dir", default=None,
                        help="HTML 报告输出目录（默认脚本目录下 reports/，传空串禁用）")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")

    # 美股交易日对应北京时间周二至周六
    if not args.force and datetime.now(timezone(timedelta(hours=8))).weekday() == 0:
        log.info("北京时间周一，美股无交易（周末），跳过。使用 --force 强制运行。")
        return

    # 合并中概股配置
    adr_map = config.get("chinese_adrs", DEFAULT_CHINESE_ADRS)
    if isinstance(adr_map, list):
        adr_map = {t: t for t in adr_map}

    # 获取数据（每个模块独立 try，互不影响）
    log.info("开始获取美股数据...")

    indices = None
    try:
        indices = fetch_major_indices()
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
        if gainers or losers:
            log.info("补充板块信息...")
            _enrich_sector_info(gainers + losers)
    except Exception as e:
        log.error("获取涨跌幅排行失败: %s", e)

    adrs = None
    try:
        adrs = fetch_chinese_adrs(adr_map)
        log.info("中概股数据: %d 条", len(adrs) if adrs else 0)
    except Exception as e:
        log.error("获取中概股数据失败: %s", e)

    bellwethers = None
    try:
        bellwethers = fetch_stock_group(BELLWETHERS)
        log.info("风向标数据: %d 条", len(bellwethers) if bellwethers else 0)
    except Exception as e:
        log.error("获取风向标数据失败: %s", e)

    storage = None
    try:
        storage = fetch_stock_group(STORAGE_STOCKS)
        log.info("存储概念股数据: %d 条", len(storage) if storage else 0)
    except Exception as e:
        log.error("获取存储概念股数据失败: %s", e)

    cpo = None
    try:
        cpo = fetch_stock_group(CPO_STOCKS)
        log.info("CPO概念股数据: %d 条", len(cpo) if cpo else 0)
    except Exception as e:
        log.error("获取CPO概念股数据失败: %s", e)

    cloud = None
    try:
        cloud = fetch_stock_group(CLOUD_STOCKS)
        log.info("云计算厂商数据: %d 条", len(cloud) if cloud else 0)
    except Exception as e:
        log.error("获取云计算厂商数据失败: %s", e)

    # 构建卡片
    card = build_feishu_card(indices, sectors, gainers, losers, adrs, bellwethers, storage, cpo, cloud)

    # 生成网页版报告（日期页 + index 覆盖为最新）
    html_dir_arg = args.html_dir
    html_dir = None if html_dir_arg == "" else (
        Path(html_dir_arg) if html_dir_arg else SCRIPT_DIR / "reports")
    if html_dir:
        try:
            html_dir.mkdir(parents=True, exist_ok=True)
            html = build_html_report(indices, sectors, gainers, losers, adrs,
                                     bellwethers, storage, cpo, cloud)
            date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            dated = html_dir / f"{date_str}.html"
            index_page = html_dir / "index.html"
            dated.write_text(html, encoding="utf-8")
            index_page.write_text(html, encoding="utf-8")
            log.info("网页版报告已生成: %s (index.html 同步更新)", dated)
        except Exception as e:
            log.warning("网页版报告生成失败（不影响飞书推送）: %s", e)

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - 飞书卡片内容预览")
        print("=" * 60)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    # 发送
    if not webhook_url or "在此粘贴" in webhook_url:
        log.error("飞书 Webhook URL 未配置，请在 config.json 中设置")
        sys.exit(1)

    success = send_to_feishu(card, webhook_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
