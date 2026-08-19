#!/usr/bin/env python3
"""A股每日全景复盘（一期·数据版）- 指数/情绪/板块/涨停池/资金流
数据源 akshare（东财），飞书摘要卡片 + GitHub Pages 网页全景报告。
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

os.environ.setdefault("no_proxy", "*")

import requests

try:
    import akshare as ak
except ImportError:
    print("缺少 akshare: pip install akshare")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ashare")

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

PAGES_BASE_URL = "https://jefferyleng.github.io/us-daily-briefing/ashare/"

# ---- 常量 ----

# 展示用指数（东财代码）
ASHARE_INDICES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "899050": "北证50",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
    "000922": "中证红利",
}
# 深证综指（399106）仅用于两市成交额全口径统计，不进指数表
AMOUNT_AUX_INDEX = ("399106", "深证综指")

# 新浪源代码前缀（东财被断连时的回退源）
SINA_INDEX_CODES = {
    "000001": "sh000001", "399001": "sz399001", "399006": "sz399006",
    "000688": "sh000688", "899050": "bj899050", "000300": "sh000300",
    "000905": "sh000905", "000852": "sh000852", "000016": "sh000016",
    "000922": "sh000922", "399106": "sz399106",
}


def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _retry(fn, times=3, delay=2.0):
    """akshare 接口偶发 RemoteDisconnected，带退避重试"""
    import time as _t
    for i in range(1, times + 1):
        try:
            return fn()
        except Exception as e:
            if i == times:
                raise
            log.info("[重试] %s 第%d次失败: %s，%.1fs后重试",
                     getattr(fn, "__name__", "api"), i, e, delay)
            _t.sleep(delay)
            delay *= 1.5


def _num(v):
    """宽松数值解析：'8.06%' / '1234' / 1234.0 → float；失败返回 None"""
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ============================================================
# 数据采集（每个模块独立容错，失败不拖垮整份报告）
# ============================================================

def fetch_indices(date_compact):
    """指数收盘：东财优先（一次含涨跌/YTD/成交额），整体失败回退新浪。

    返回 (list[dict], trading_today)。东财路径下上证无当日K线视为休市。
    """
    year = date_compact[:4]

    # ---- 主源：东财 ----
    results = []
    em_failed = 0
    trading_today = False
    for code, name in ASHARE_INDICES.items():
        try:
            df = _retry(lambda: ak.index_zh_a_hist(
                symbol=code, period="daily",
                start_date=f"{year}0101", end_date=date_compact))
            if df is None or df.empty or "收盘" not in df.columns:
                log.warning("[指数] %s 无数据，跳过", name)
                continue
            last = df.iloc[-1]
            if str(last["日期"]).replace("-", "") != date_compact:
                # 该指数当日无K线（节假日/未更新），除上证外跳过
                if code == "000001":
                    return [], False
                log.warning("[指数] %s 当日无K线，跳过", name)
                continue
            if code == "000001":
                trading_today = True
            prev = float(df.iloc[-2]["收盘"]) if len(df) >= 2 else None
            close = float(last["收盘"])
            ytd_base = float(df.iloc[0]["收盘"])
            results.append({
                "name": name,
                "code": code,
                "close": round(close, 2),
                "change_pct": round((close - prev) / prev * 100, 2) if prev else None,
                "ytd_pct": round((close - ytd_base) / ytd_base * 100, 2),
                "amount": float(last["成交额"]) if "成交额" in df.columns else 0.0,
            })
        except Exception as e:
            em_failed += 1
            log.warning("[指数] %s(%s) 东财失败: %s", name, code, e)

    if results or em_failed < len(ASHARE_INDICES):
        return results, trading_today

    # ---- 回退：新浪（spot 提供成交额 + daily 算涨跌/YTD）----
    log.warning("[指数] 东财全挂，回退新浪源")
    try:
        spot = _retry(ak.stock_zh_index_spot_sina)
        spot_amount = {str(r["代码"]): _num(r.get("成交额")) or 0.0
                       for _, r in spot.iterrows()}
    except Exception as e:
        log.warning("[指数] 新浪spot失败: %s", e)
        spot_amount = {}
    for code, name in ASHARE_INDICES.items():
        sina_code = SINA_INDEX_CODES.get(code)
        if not sina_code:
            continue
        try:
            df = _retry(lambda: ak.stock_zh_index_daily(symbol=sina_code))
            # 新浪 date 列为 datetime.date，需与 date 对象比较
            year_start = datetime(int(year), 1, 1).date()
            df = df[df["date"] >= year_start].reset_index(drop=True)
            if df.empty or len(df) < 2:
                continue
            close = float(df.iloc[-1]["close"])
            prev = float(df.iloc[-2]["close"])
            ytd_base = float(df.iloc[0]["close"])
            results.append({
                "name": name,
                "code": code,
                "close": round(close, 2),
                "change_pct": round((close - prev) / prev * 100, 2),
                "ytd_pct": round((close - ytd_base) / ytd_base * 100, 2),
                "amount": spot_amount.get(sina_code, 0.0),
            })
        except Exception as e:
            log.warning("[指数] %s 新浪失败: %s", name, e)
    # 新浪无日期校验，weekday 由外部保证
    return results, True


def fetch_total_amount(date_compact):
    """两市成交额（上证综指 + 深证综指，全口径），含昨日值算环比。

    东财失败回退新浪 spot（仅当日值，无环比）。
    """
    total, prev_total = 0.0, 0.0
    ok = False
    for code, name in (("000001", "上证指数"), AMOUNT_AUX_INDEX):
        try:
            df = _retry(lambda: ak.index_zh_a_hist(
                symbol=code, period="daily",
                start_date="20260101", end_date=date_compact))
            if df is None or df.empty or "成交额" not in df.columns:
                continue
            last = df.iloc[-1]
            if str(last["日期"]).replace("-", "") != date_compact:
                continue
            total += float(last["成交额"])
            if len(df) >= 2:
                prev_total += float(df.iloc[-2]["成交额"])
            ok = True
        except Exception as e:
            log.warning("[成交额] %s 东财失败: %s", name, e)
    if ok:
        return total, prev_total

    # 回退：新浪 spot（当日成交额，无昨日环比）
    try:
        spot = _retry(ak.stock_zh_index_spot_sina)
        amt = {str(r["代码"]): _num(r.get("成交额")) or 0.0 for _, r in spot.iterrows()}
        total = amt.get("sh000001", 0.0) + amt.get("sz399106", 0.0)
        log.info("[成交额] 使用新浪回退")
    except Exception as e:
        log.warning("[成交额] 新浪回退也失败: %s", e)
    return total, 0.0


def fetch_market_activity():
    """市场情绪（乐咕赚钱效应）：涨跌家数/涨停/跌停/活跃度。"""
    try:
        df = _retry(ak.stock_market_activity_legu)
        data = {str(r["item"]): r["value"] for _, r in df.iterrows()}

        def _int(key):
            v = _num(data.get(key))
            return int(v) if v is not None else 0

        return {
            "up": _int("上涨"),
            "down": _int("下跌"),
            "limit_up": _int("涨停"),
            "limit_down": _int("跌停"),
            "flat": _int("平盘"),
            "activity": _num(data.get("活跃度")) or 0.0,
        }
    except Exception as e:
        log.warning("[市场情绪] 获取失败: %s", e)
        return None


def fetch_zt_pool(date_compact):
    """涨停池 + 连板天梯。返回 dict 或 None。"""
    try:
        df = ak.stock_zt_pool_em(date=date_compact)
        if df is None or df.empty:
            return None
        stocks = []
        for _, r in df.iterrows():
            stocks.append({
                "code": str(r.get("代码", "")),
                "name": str(r.get("名称", "")),
                "seal_amount": float(r.get("封板资金", 0) or 0),
                "break_count": int(r.get("炸板次数", 0) or 0),
                "stat": str(r.get("涨停统计", "")),       # 如 "2/2"
                "days": int(r.get("连板数", 1) or 1),
                "industry": str(r.get("所属行业", "")),
            })
        # 连板天梯：按连板数分组
        tiers = {}
        for s in stocks:
            tiers.setdefault(s["days"], []).append(s)
        tier_summary = {d: len(v) for d, v in sorted(tiers.items(), reverse=True)}
        return {
            "total": len(stocks),
            "stocks": stocks,
            "tiers": tiers,
            "tier_summary": tier_summary,
            "max_days": max((s["days"] for s in stocks), default=0),
        }
    except Exception as e:
        log.warning("[涨停池] 获取失败: %s", e)
        return None


def fetch_zb_pool(date_compact):
    """炸板池（数量 + 炸板率）。"""
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_compact)
        return 0 if df is None else len(df)
    except Exception as e:
        log.warning("[炸板池] 获取失败: %s", e)
        return None


def fetch_dt_pool(date_compact):
    """跌停池数量。"""
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_compact)
        return 0 if df is None else len(df)
    except Exception as e:
        log.warning("[跌停池] 获取失败: %s", e)
        return None


def fetch_industry_ranking(top_n=10):
    """行业板块涨跌 TOP。东财失败回退同花顺。"""
    try:
        df = _retry(ak.stock_board_industry_name_em)
        if df is None or df.empty or "涨跌幅" not in df.columns:
            raise ValueError("东财行业板块数据为空")
        df = df.sort_values("涨跌幅", ascending=False)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r["板块名称"]),
                "change_pct": float(r["涨跌幅"]),
                "leader": str(r.get("领涨股票", "")),
            })
        return {"top": rows[:top_n], "bottom": list(reversed(rows[-top_n:]))}
    except Exception as e:
        log.warning("[行业板块] 东财失败(%s)，回退同花顺", e)
    # ---- 回退：同花顺行业概览 ----
    try:
        df = _retry(ak.stock_board_industry_summary_ths)
        if df is None or df.empty or "涨跌幅" not in df.columns:
            return None
        df = df.sort_values("涨跌幅", ascending=False)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r["板块"]),
                "change_pct": _num(r["涨跌幅"]) or 0.0,
                "leader": str(r.get("领涨股", "")),
            })
        return {"top": rows[:top_n], "bottom": list(reversed(rows[-top_n:]))}
    except Exception as e:
        log.warning("[行业板块] 同花顺也失败: %s", e)
        return None


def fetch_concept_ranking(top_n=10):
    """概念板块涨幅 TOP。"""
    try:
        df = _retry(ak.stock_board_concept_name_em)
        if df is None or df.empty or "涨跌幅" not in df.columns:
            return None
        df = df.sort_values("涨跌幅", ascending=False)
        return [{
            "name": str(r["板块名称"]),
            "change_pct": float(r["涨跌幅"]),
        } for _, r in df.head(top_n).iterrows()]
    except Exception as e:
        log.warning("[概念板块] 获取失败: %s", e)
        return None


def fetch_fund_flow(top_n=5):
    """行业主力资金净流入/流出 TOP。东财失败回退同花顺。"""
    try:
        df = _retry(lambda: ak.stock_sector_fund_flow_rank(
            indicator="今日", sector_type="行业资金流"))
        if df is None or df.empty:
            raise ValueError("东财资金流数据为空")
        col = "今日主力净流入-净额"
        if col not in df.columns:
            raise ValueError(f"缺少列 {col}")
        df = df.sort_values(col, ascending=False)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r["名称"]),
                "net": float(r[col]),
                "change_pct": float(r.get("今日涨跌幅", 0) or 0),
            })
        return {"in": rows[:top_n], "out": list(reversed(rows[-top_n:]))}
    except Exception as e:
        log.warning("[资金流] 东财失败(%s)，回退同花顺", e)
    # ---- 回退：同花顺行业资金流（净额单位为亿元，×1e8 换算成元）----
    try:
        df = _retry(lambda: ak.stock_fund_flow_industry(symbol="即时"))
        if df is None or df.empty or "净额" not in df.columns:
            return None
        df = df.sort_values("净额", ascending=False)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r["行业"]),
                "net": float(r["净额"]) * 1e8,
                "change_pct": _num(r.get("行业-涨跌幅")) or 0.0,
            })
        return {"in": rows[:top_n], "out": list(reversed(rows[-top_n:]))}
    except Exception as e:
        log.warning("[资金流] 同花顺也失败: %s", e)
        return None


# ============================================================
# 格式化 & 飞书卡片
# ============================================================

def _ball(pct):
    if pct is None:
        return "⚪"
    if pct > 0:
        return "🔴"
    if pct < 0:
        return "🟢"
    return "⚪"


def _yi(v):
    """元 → 亿元"""
    return v / 1e8


def _fmt_yi(v, signed=False):
    s = f"{_yi(abs(v)):.0f}亿"
    if signed:
        return f"+{s}" if v > 0 else f"-{s}"
    return s


def build_feishu_card(indices, activity, zt, zb_count, dt_count,
                      total_amount, prev_total_amount,
                      industry, concept, fund_flow, date_str):
    elements = []

    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"**收盘 {date_str}**"}})

    # 一、指数收盘
    if indices:
        parts = [f"{_ball(i['change_pct'])}{i['name']} {i['close']:,.2f} "
                 f"{i['change_pct']:+.2f}%" for i in indices if i["change_pct"] is not None]
        lines = ["**一、指数收盘**\n"]
        for k in range(0, len(parts), 3):
            lines.append("  ".join(parts[k:k + 3]))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    elements.append({"tag": "hr"})

    # 二、市场情绪
    if activity:
        zb_rate = ""
        if zt and zb_count is not None:
            total_board = zt["total"] + zb_count
            if total_board > 0:
                zb_rate = f" · 炸板率{zb_count / total_board * 100:.0f}%"
        dt_txt = f" · 跌停{dt_count}" if dt_count is not None else ""
        amt = f" · 成交{_yi(total_amount) / 10000:.2f}万亿" if total_amount else ""
        amt_delta = ""
        if total_amount and prev_total_amount:
            d = (total_amount - prev_total_amount) / prev_total_amount * 100
            amt_delta = f"（环比{d:+.1f}%）"
        red = activity["up"] + activity["down"]
        red_rate = f"红盘率{activity['up'] / red * 100:.0f}%" if red else ""
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**二、市场情绪：涨{activity['up']} 跌{activity['down']} · "
            f"涨停{activity['limit_up']}{dt_txt}{zb_rate}**\n"
            f"{red_rate}{amt}{amt_delta}"}})

    elements.append({"tag": "hr"})

    # 三、板块
    if industry and industry["top"]:
        t, b = industry["top"][0], industry["bottom"][0]
        concept_txt = ""
        if concept:
            concept_txt = f"\n概念领涨：{concept[0]['name']} {concept[0]['change_pct']:+.2f}%"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**三、板块：领涨 {t['name']} {t['change_pct']:+.2f}% ｜ "
            f"领跌 {b['name']} {b['change_pct']:+.2f}%**{concept_txt}"}})

    elements.append({"tag": "hr"})

    # 四、连板天梯
    if zt and zt["total"] > 0:
        max_tier = zt["tiers"].get(zt["max_days"], [])
        max_names = "、".join(s["name"] for s in max_tier[:3])
        higher = sum(c for d, c in zt["tier_summary"].items() if d >= 3)
        tier_txt = f" · 3板以上{higher}家" if higher else ""
        first_boards = zt["tier_summary"].get(1, 0)
        lines = [f"**四、连板天梯：最高{zt['max_days']}板 {max_names}{tier_txt} · 首板{first_boards}家**\n"]
        # 列出 2 板以上名单（紧凑）
        parts = []
        for d in sorted(zt["tiers"], reverse=True):
            if d < 2:
                break
            for s in zt["tiers"][d]:
                parts.append(f"🔵{d}板 {s['name']}")
        for k in range(0, len(parts), 4):
            lines.append("  ".join(parts[k:k + 4]))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    elements.append({"tag": "hr"})

    # 五、主力资金
    if fund_flow:
        ins = "、".join(f"{r['name']}{_fmt_yi(r['net'], True)}" for r in fund_flow["in"][:3])
        outs = "、".join(f"{r['name']}{_fmt_yi(r['net'], True)}" for r in fund_flow["out"][:3])
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**五、主力资金（行业）**\n流入：{ins}\n流出：{outs}"}})

    # 页脚
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"生成时间: {now_str}\n[📱 查看完整报告]({PAGES_BASE_URL})"}})

    return {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text",
                             "content": f"📉 A股每日复盘 · {date_str}"},
                   "template": "red"},
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
            import time
            time.sleep(5)
    log.error("飞书推送失败")
    return False


# ============================================================
# HTML 报告
# ============================================================

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _h_pct(pct):
    if pct is None:
        return "<span class='flat'>--</span>"
    cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "")
    return f"<span class='{cls}'>{pct:+.2f}% {arrow}</span>"


def _h_table(rows, headers):
    h = "<div class='tbl-wrap'><table><thead><tr>"
    for hd in headers:
        h += f"<th>{_esc(hd)}</th>"
    h += "</tr></thead><tbody>"
    for row in rows:
        h += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    h += "</tbody></table></div>"
    return h


def _h_bars(rows):
    """CSS 水平柱图：rows = [(name, value)]，正负双向。"""
    if not rows:
        return ""
    max_abs = max(abs(v) for _, v in rows) or 1.0
    h = "<div class='bars'>"
    for name, v in rows:
        cls = "pos" if v > 0 else "neg"
        w = abs(v) / max_abs * 100
        val = f"+{_yi(v):.0f}亿" if v > 0 else f"{_yi(v):.0f}亿"
        h += (f"<div class='bar-row'><span class='bar-name'>{_esc(name)}</span>"
              f"<div class='bar-track'><div class='bar-fill {cls}' style='width:{w:.1f}%'></div></div>"
              f"<span class='bar-val'>{val}</span></div>")
    h += "</div>"
    return h


HTML_CSS = """
:root { --up:#e02020; --down:#0a8f4e; --flat:#666; --line:#e8e8ec;
        --bg:#f5f6f8; --card:#fff; --txt:#222; --sub:#777; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
       background:var(--bg); color:var(--txt); line-height:1.6; padding:16px; }
.wrap { max-width:860px; margin:0 auto; }
header { text-align:center; padding:20px 12px 8px; }
header h1 { font-size:22px; }
header .sub { color:var(--sub); font-size:13px; margin-top:4px; }
.stat-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
              gap:10px; margin:16px 0; }
.stat-card { background:var(--card); border:1px solid var(--line); border-radius:10px;
             padding:12px 10px; text-align:center; }
.stat-card .nm { font-size:12px; color:var(--sub); }
.stat-card .px { font-size:19px; font-weight:700; margin:2px 0; }
.stat-card .meta { font-size:12px; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin:14px 0; }
section h2 { font-size:16px; margin-bottom:10px; border-left:4px solid #d64545;
             padding-left:8px; }
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:7px 8px; text-align:left; border-bottom:1px solid var(--line);
         white-space:nowrap; }
th { color:var(--sub); font-weight:600; background:#fafafa; }
tr:last-child td { border-bottom:none; }
.up { color:var(--up); font-weight:600; }
.down { color:var(--down); font-weight:600; }
.flat { color:var(--flat); }
.tag { display:inline-block; background:#fbeeec; color:#d64545; border-radius:4px;
       padding:0 6px; font-size:12px; }
.bars { margin-top:8px; }
.bar-row { display:flex; align-items:center; gap:8px; margin:5px 0; font-size:13px; }
.bar-name { width:88px; text-align:right; flex-shrink:0; overflow:hidden;
            text-overflow:ellipsis; white-space:nowrap; }
.bar-track { flex:1; background:#f0f0f4; border-radius:3px; height:14px; overflow:hidden; }
.bar-fill { height:100%; border-radius:3px; }
.bar-fill.pos { background:linear-gradient(90deg,#ff8a80,#e02020); }
.bar-fill.neg { background:linear-gradient(90deg,#0a8f4e,#66c296); margin-left:auto; }
.bar-val { width:70px; flex-shrink:0; font-size:12px; color:var(--sub); }
footer { text-align:center; color:var(--sub); font-size:12px; padding:16px 0 30px; }
footer a { color:#d64545; text-decoration:none; }
@media (max-width:480px) { body{padding:8px} .stat-card .px{font-size:16px} }
"""


def build_html_report(indices, activity, zt, zb_count, dt_count,
                      total_amount, prev_total_amount,
                      industry, concept, fund_flow, date_str):
    gen_time = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    h = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>A股每日复盘 {date_str}</title><style>{HTML_CSS}</style></head><body><div class='wrap'>",
         f"<header><h1>📉 A股每日全景复盘</h1>"
         f"<div class='sub'>{date_str}（北京时间）收盘 · 生成于 {gen_time}</div></header>"]

    # 情绪卡片
    if activity:
        red = activity["up"] + activity["down"]
        red_rate = activity["up"] / red * 100 if red else 0
        cards = [
            ("上涨", f"{activity['up']}", f"红盘率{red_rate:.0f}%"),
            ("下跌", f"{activity['down']}", ""),
            ("涨停", f"{activity['limit_up']}", f"炸板{zb_count if zb_count is not None else '--'}"
             + (f" 炸板率{zb_count / (activity['limit_up'] + zb_count) * 100:.0f}%"
                if zb_count is not None and (activity['limit_up'] + zb_count) > 0 else "")),
            ("跌停", f"{dt_count if dt_count is not None else '--'}", ""),
        ]
        if total_amount:
            d = ((total_amount - prev_total_amount) / prev_total_amount * 100
                 if prev_total_amount else None)
            cards.append(("两市成交", f"{_yi(total_amount) / 10000:.2f}万亿",
                          f"环比{d:+.1f}%" if d is not None else ""))
        h.append("<div class='stat-cards'>")
        for nm, px, meta in cards:
            h.append(f"<div class='stat-card'><div class='nm'>{_esc(nm)}</div>"
                     f"<div class='px'>{_esc(px)}</div>"
                     f"<div class='meta'>{_esc(meta)}</div></div>")
        h.append("</div>")

    # 章节动态编号（某数据源缺失时不跳号）
    seq = iter(range(1, 10))
    def _no():
        return f"{'一二三四五六七八九十'[next(seq) - 1]}、"

    # 指数收盘
    if indices:
        rows = []
        for i in indices:
            rows.append([_esc(i["name"]), f"<span class='tag'>{_esc(i['code'])}</span>",
                         f"{i['close']:,.2f}", _h_pct(i["change_pct"]),
                         _h_pct(i.get("ytd_pct")),
                         f"{_yi(i['amount']):,.0f}亿" if i["amount"] else "--"])
        h.append(f"<section><h2>{_no()}指数收盘</h2>"
                 + _h_table(rows, ["指数", "代码", "收盘", "日涨跌", "年初至今", "成交额"])
                 + "</section>")

    # 行业板块
    if industry:
        rows = []
        for r in industry["top"]:
            rows.append([_esc(r["name"]), _h_pct(r["change_pct"]), _esc(r["leader"])])
        h.append(f"<section><h2>{_no()}行业板块 · 领涨 TOP 10</h2>"
                 + _h_table(rows, ["板块", "涨跌幅", "领涨股"]) + "</section>")
        rows = []
        for r in industry["bottom"]:
            rows.append([_esc(r["name"]), _h_pct(r["change_pct"]), _esc(r["leader"])])
        h.append(f"<section><h2>{_no()}行业板块 · 领跌 TOP 10</h2>"
                 + _h_table(rows, ["板块", "涨跌幅", "领涨股"]) + "</section>")

    # 概念板块
    if concept:
        rows = [[_esc(r["name"]), _h_pct(r["change_pct"])] for r in concept]
        h.append(f"<section><h2>{_no()}概念板块 · 领涨 TOP 10</h2>"
                 + _h_table(rows, ["概念", "涨跌幅"]) + "</section>")

    # 涨停池与连板天梯
    if zt and zt["total"] > 0:
        tier_txt = " ｜ ".join(f"{d}板×{c}家" for d, c in zt["tier_summary"].items())
        rows = []
        for s in sorted(zt["stocks"], key=lambda x: -x["days"]):
            if s["days"] < 2:
                continue
            rows.append([f"<span class='tag'>{s['days']}板</span>", _esc(s["name"]),
                         _esc(s["code"]), _esc(s["stat"]),
                         f"{_yi(s['seal_amount']):.1f}亿" if s["seal_amount"] else "--",
                         str(s["break_count"]), _esc(s["industry"])])
        body = (f"<p style='font-size:13px;color:#777;margin-bottom:8px'>"
                f"涨停 {zt['total']} 家 · {tier_txt}"
                + (f" · 炸板 {zb_count} 家" if zb_count is not None else "")
                + (f" · 跌停 {dt_count} 家" if dt_count is not None else "") + "</p>")
        if rows:
            body += _h_table(rows, ["梯队", "名称", "代码", "涨停统计", "封板资金", "炸板次数", "行业"])
        else:
            body += "<p style='font-size:13px'>今日无 2 板及以上连板股</p>"
        h.append(f"<section><h2>{_no()}涨停池 · 连板天梯</h2>" + body + "</section>")

    # 主力资金（行业）
    if fund_flow:
        rows = []
        for r in fund_flow["in"]:
            rows.append([_esc(r["name"]), _h_pct(r["change_pct"]),
                         f"<span class='up'>+{_yi(r['net']):.0f}亿</span>"])
        for r in fund_flow["out"]:
            rows.append([_esc(r["name"]), _h_pct(r["change_pct"]),
                         f"<span class='down'>{_yi(r['net']):.0f}亿</span>"])
        bar_rows = [(r["name"], r["net"]) for r in fund_flow["in"]] + \
                   [(r["name"], r["net"]) for r in fund_flow["out"]]
        h.append(f"<section><h2>{_no()}主力资金 · 行业净流入/流出 TOP 5</h2>"
                 + _h_bars(bar_rows)
                 + "<div style='height:12px'></div>"
                 + _h_table(rows, ["行业", "今日涨跌幅", "主力净流入"]) + "</section>")

    h.append(f"<footer>生成时间 {gen_time} · 数据来源 东方财富（via akshare） · "
             f"<a href='{PAGES_BASE_URL}'>历史复盘存档</a><br>"
             f"本报告仅供参考，不构成投资建议</footer>")
    h.append("</div></body></html>")
    return "".join(h)


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="A股每日全景复盘 - 飞书推送 + 网页报告")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略休市判断）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--html-dir", default=None,
                        help="HTML 输出目录（默认脚本目录下 ashare_reports/，传空串禁用）")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")

    now = datetime.now(CST)
    date_str = now.strftime("%Y-%m-%d")
    date_compact = now.strftime("%Y%m%d")

    if not args.force and now.weekday() >= 5:
        log.info("周末休市，跳过。使用 --force 强制运行。")
        return

    log.info("A股每日复盘启动 | 日期: %s", date_str)

    # ---- 采集 ----
    indices, trading_today = fetch_indices(date_compact)
    if not trading_today and not args.force:
        log.info("今日(%s)无上证K线，判定休市，跳过推送", date_str)
        return

    total_amount, prev_total_amount = fetch_total_amount(date_compact)
    activity = fetch_market_activity()
    zt = fetch_zt_pool(date_compact)
    zb_count = fetch_zb_pool(date_compact)
    dt_count = fetch_dt_pool(date_compact)
    industry = fetch_industry_ranking()
    concept = fetch_concept_ranking()
    fund_flow = fetch_fund_flow()

    log.info("采集完成: 指数%d 情绪%s 涨停%s 炸板%s 跌停%s 行业%s 概念%s 资金%s",
             len(indices), "✓" if activity else "✗",
             zt["total"] if zt else "✗", zb_count, dt_count,
             "✓" if industry else "✗", "✓" if concept else "✗",
             "✓" if fund_flow else "✗")

    # ---- 卡片 & HTML ----
    card = build_feishu_card(indices, activity, zt, zb_count, dt_count,
                             total_amount, prev_total_amount,
                             industry, concept, fund_flow, date_str)

    html_dir_arg = args.html_dir
    html_dir = None if html_dir_arg == "" else (
        Path(html_dir_arg) if html_dir_arg else SCRIPT_DIR / "ashare_reports")
    if html_dir:
        try:
            html_dir.mkdir(parents=True, exist_ok=True)
            html = build_html_report(indices, activity, zt, zb_count, dt_count,
                                     total_amount, prev_total_amount,
                                     industry, concept, fund_flow, date_str)
            (html_dir / f"{date_str}.html").write_text(html, encoding="utf-8")
            (html_dir / "index.html").write_text(html, encoding="utf-8")
            log.info("网页版报告已生成: %s/index.html", html_dir)
        except Exception as e:
            log.warning("网页版报告生成失败（不影响飞书推送）: %s", e)

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - 飞书卡片内容预览")
        print("=" * 60)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not webhook_url or "在此粘贴" in webhook_url:
        log.error("飞书 Webhook URL 未配置")
        sys.exit(1)

    success = send_to_feishu(card, webhook_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
