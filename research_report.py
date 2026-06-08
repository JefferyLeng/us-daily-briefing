#!/usr/bin/env python3
"""研报速递 - 东方财富研报抓取 + 智谱AI总结 + 飞书推送"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

os.environ.setdefault("no_proxy", "*")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SENT_PATH = SCRIPT_DIR / "sent_reports.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

SESSION_LABEL = "每日精选"

REPORT_API = "https://reportapi.eastmoney.com/report/list"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """你是一个专业的金融研报分析师。请根据以下券商研报信息，生成简洁的要点总结。

要求：
1. 用2-4个要点概括研报核心观点
2. 每个要点不超过50字
3. 重点突出：投资逻辑、关键数据、风险提示
4. 保持客观，使用第三人称
5. 如果信息不足，根据已有内容合理总结，不要编造数据
6. 直接输出要点，每行一个，以 "- " 开头，不要加标题和额外说明"""

DEFAULT_PREFERRED_INSTITUTIONS = [
    "中信证券", "中金公司", "国泰君安", "华泰证券", "海通证券",
    "招商证券", "广发证券", "申万宏源", "东方证券", "天风证券",
    "国盛证券", "长江证券", "兴业证券", "平安证券", "中信建投",
]

HIGH_RATINGS = {"买入", "强推", "强力买入"}
MEDIUM_RATINGS = {"增持", "推荐", "跑赢行业", "优于大市"}

PREFERRED_TYPES = {"宏观", "行业", "策略"}

# ratingChange 数字映射
RATING_CHANGE_MAP = {
    1: "上调",
    2: "维持",
    3: "下调",
    4: "首次覆盖",
}

# reportType 数字映射
REPORT_TYPE_MAP = {
    1: "宏观",
    2: "个股",
    3: "行业",
    4: "策略",
    5: "其他",
    6: "固收",
    7: "基金",
    9: "期货",
    10: "外汇",
    11: "债券",
    16: "期权",
}

DETAIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}


# ---- 配置 ----

def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- 去重跟踪 ----

def load_sent_ids():
    if not SENT_PATH.exists():
        return set()
    try:
        with open(SENT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = (datetime.now(CST) - timedelta(days=7)).strftime("%Y-%m-%d")
        entries = data.get("entries", {})
        return {k for k, v in entries.items() if v >= cutoff}
    except Exception:
        return set()


def save_sent_ids(ids):
    now_str = datetime.now(CST).strftime("%Y-%m-%d")
    entries = {}
    try:
        with open(SENT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        entries = old.get("entries", {})
    except Exception:
        pass

    for rid in ids:
        entries[rid] = now_str

    cutoff = (datetime.now(CST) - timedelta(days=7)).strftime("%Y-%m-%d")
    entries = {k: v for k, v in entries.items() if v >= cutoff}

    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_updated": now_str, "entries": entries}, f, ensure_ascii=False, indent=2)


# ---- 数据获取：东方财富研报 ----

def fetch_reports(date_str, max_retries=3):
    begin_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/report/",
    }

    all_reports = []
    seen_codes = set()

    for q_type in [1, 3, 4, 2]:
        params = {
            "industryCode": "*",
            "pageSize": 100,
            "industry": "*",
            "rating": "",
            "ratingChange": "",
            "beginTime": begin_date,
            "endTime": date_str,
            "pageNo": 1,
            "fields": "",
            "qType": q_type,
            "orgCode": "",
            "code": "*",
            "rcode": "",
            "cb": "",
            "_": str(int(time.time() * 1000)),
        }
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(REPORT_API, params=params, headers=headers, timeout=15)
                data = resp.json()
                reports = data.get("data", [])
                if isinstance(reports, dict):
                    reports = reports.get("list", [])
                type_label = REPORT_TYPE_MAP.get(q_type, str(q_type))
                log.info("qType=%d(%s) 返回 %d 篇", q_type, type_label, len(reports))
                for r in reports:
                    code = r.get("infoCode", "")
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        all_reports.append(r)
                break
            except Exception as e:
                log.warning("获取研报失败 qType=%d (attempt %d/%d): %s", q_type, attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(3)

    log.info("合计获取 %d 篇研报（去重后）", len(all_reports))
    return all_reports


def parse_report(r):
    info_code = r.get("infoCode", "")
    title = r.get("title", "").strip()
    org = r.get("orgSName", "").strip()
    researcher = r.get("researcher", "").strip()
    publish_date = r.get("publishDate", "")
    rating = r.get("emRatingName", "").strip()

    rating_change_raw = r.get("ratingChange", "")
    if isinstance(rating_change_raw, int):
        rating_change = RATING_CHANGE_MAP.get(rating_change_raw, "")
    else:
        rating_change = str(rating_change_raw).strip()

    report_type_raw = r.get("reportType", "")
    if isinstance(report_type_raw, int):
        report_type = REPORT_TYPE_MAP.get(report_type_raw, "其他")
    else:
        report_type = str(report_type_raw).strip() or "其他"

    stock_name = r.get("stockName", "").strip()
    stock_code = r.get("stockCode", "").strip()
    encode_url = r.get("encodeUrl", "")

    pdf_url = f"https://data.eastmoney.com/report/zw_macresearch.jshtml?infocode={info_code}" if info_code else ""

    return {
        "infoCode": info_code,
        "title": title,
        "org": org,
        "researcher": researcher,
        "publishDate": publish_date,
        "rating": rating,
        "ratingChange": rating_change,
        "reportType": report_type,
        "stockName": stock_name,
        "stockCode": stock_code,
        "encodeUrl": encode_url,
        "pdfUrl": pdf_url,
        "content": "",
    }


def fetch_abstract(info_code):
    if not info_code:
        return ""
    try:
        resp = requests.get(
            "https://data.eastmoney.com/report/zw_macresearch.jshtml",
            params={"infocode": info_code},
            headers=DETAIL_HEADERS,
            timeout=15,
        )
        html = resp.text
        m = re.search(r'id="ctx-content"[^>]*>(.*?)</div>\s*(?:<div|<script)', html, re.DOTALL)
        if m:
            raw = m.group(1)
            clean = re.sub(r'<[^>]+>', '', raw)
            clean = re.sub(r'\s+', ' ', clean).strip()
            return clean[:1500]
    except Exception as e:
        log.warning("获取摘要失败 %s: %s", info_code, e)
    return ""


# ---- 评分筛选 ----

def score_report(report, preferred):
    score = 0

    if report["org"] in preferred:
        score += 30

    if report["rating"] in HIGH_RATINGS:
        score += 20
    elif report["rating"] in MEDIUM_RATINGS:
        score += 15

    if report["ratingChange"] == "首次覆盖":
        score += 15
    elif report["ratingChange"] == "上调":
        score += 12
    elif report["ratingChange"] == "维持":
        score += 5
    elif report["ratingChange"]:
        score += 3

    if report.get("reportType") in PREFERRED_TYPES:
        score += 25

    if len(report["content"]) > 50:
        score += 15
    elif len(report["content"]) > 0:
        score += 8

    return score


def score_and_select(reports, preferred, sent_ids, count=20, max_stock=5):
    filtered = [r for r in reports if r["infoCode"] not in sent_ids]
    if not filtered:
        return []

    for r in filtered:
        r["_score"] = score_report(r, preferred)

    filtered.sort(key=lambda x: x["_score"], reverse=True)

    selected = []
    stock_count = 0
    for r in filtered:
        if len(selected) >= count:
            break
        rt = r.get("reportType", "其他")
        if rt == "个股":
            if stock_count >= max_stock:
                continue
            stock_count += 1
        selected.append(r)

    for r in selected:
        r.pop("_score", None)

    return selected


# ---- AI总结：智谱GLM ----

def summarize_report(report, api_key, max_retries=2):
    user_parts = [f"标题：{report['title']}"]
    if report["org"]:
        user_parts.append(f"机构：{report['org']}")
    if report["researcher"]:
        user_parts.append(f"研究员：{report['researcher']}")
    if report["rating"]:
        user_parts.append(f"评级：{report['rating']}")
    if report["ratingChange"]:
        user_parts.append(f"评级变动：{report['ratingChange']}")
    if report["stockName"]:
        code_part = f"({report['stockCode']})" if report["stockCode"] else ""
        user_parts.append(f"相关股票：{report['stockName']}{code_part}")
    if report["content"]:
        user_parts.append(f"摘要：{report['content'][:1500]}")

    user_message = "\n".join(user_parts)

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_API,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-v4-pro",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    "thinking": {"type": "disabled"},
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
                timeout=30,
            )
            result = resp.json()
            if "error" in result:
                raise ValueError(result["error"].get("message", str(result["error"])))
            content = result["choices"][0]["message"]["content"].strip()
            if content:
                log.info("AI总结完成: %s...", content[:30])
                return content
            raise ValueError("AI返回空内容")
        except Exception as e:
            log.warning("AI总结失败 (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(3)

    if report["content"]:
        return report["content"][:300]
    return "（信息不足，暂无总结）"


# ---- 飞书卡片构建 ----

def build_feishu_card(reports, date_str):
    header_title = f"研报速递 | {date_str}"

    elements = []

    if not reports:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**今日暂无新研报**"},
        })
        elements.append({"tag": "hr"})
        now_str = datetime.now(CST).strftime("%H:%M")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"生成时间: {now_str} | 数据来源: ColdTech"},
        })
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": header_title},
                    "template": "orange",
                },
                "elements": elements,
            },
        }

    orgs = list({r["org"] for r in reports if r["org"]})
    org_hint = "、".join(orgs[:5])
    if len(orgs) > 5:
        org_hint += f"等{len(orgs)}家机构"
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**本期精选{len(reports)}篇研报**，来自{org_hint}"},
    })
    elements.append({"tag": "hr"})

    for i, r in enumerate(reports, 1):
        lines = [f"**{i}. {r['title']}**"]

        meta_parts = []
        if r["org"]:
            meta_parts.append(r["org"])
        if r["researcher"]:
            meta_parts.append(r["researcher"])
        if meta_parts:
            lines.append(" | ".join(meta_parts))

        rating_parts = []
        if r.get("reportType") == "个股":
            if r["rating"]:
                rating_str = r["rating"]
                if r["ratingChange"]:
                    rating_str += f"({r['ratingChange']})"
                rating_parts.append(rating_str)
            if r["stockName"]:
                code = f"({r['stockCode']})" if r["stockCode"] else ""
                rating_parts.append(f"{r['stockName']}{code}")
        elif r["stockName"]:
            code = f"({r['stockCode']})" if r["stockCode"] else ""
            rating_parts.append(f"{r['stockName']}{code}")
        if rating_parts:
            lines.append(" | ".join(rating_parts))

        summary = r.get("ai_summary", "")
        if summary:
            lines.append(f"\n**核心提炼：**\n{summary}")

        if r["pdfUrl"]:
            lines.append(f"\n[查看原文]({r['pdfUrl']})")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    now_str = datetime.now(CST).strftime("%H:%M")
    footer = "生成时间: {now} | 数据来源: ColdTech".format(now=now_str)
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": footer},
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": "orange",
            },
            "elements": elements,
        },
    }

    card_json = json.dumps(card, ensure_ascii=False)
    if len(card_json.encode("utf-8")) > 28000:
        while len(card_json.encode("utf-8")) > 28000 and len(elements) > 4:
            elements.pop(-3)
            card_json = json.dumps(card, ensure_ascii=False)

    return card


# ---- 飞书推送 ----

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
    parser = argparse.ArgumentParser(description="研报速递 - 飞书推送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略非交易日）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")
    api_key = config.get("deepseek_api_key", "")
    settings = config.get("report_settings", {})
    preferred = settings.get("preferred_institutions", DEFAULT_PREFERRED_INSTITUTIONS)
    count = settings.get("reports_per_session", 20)

    date_str = datetime.now(CST).strftime("%Y-%m-%d")

    if not args.force and datetime.now(CST).weekday() >= 5:
        log.info("周末，跳过。使用 --force 强制运行。")
        return

    log.info("研报速递启动 | 日期: %s", date_str)

    raw_reports = fetch_reports(date_str)
    if not raw_reports:
        log.warning("未获取到研报数据")
        card = build_feishu_card([], date_str)
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return
        if webhook_url:
            send_to_feishu(card, webhook_url)
        return

    parsed = [parse_report(r) for r in raw_reports]

    sent_ids = load_sent_ids()

    for r in parsed:
        r["_score"] = score_report(r, preferred)

    selected = score_and_select(parsed, preferred, sent_ids, count)

    if not selected:
        log.info("没有新的未推送研报")
        card = build_feishu_card([], date_str)
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return
        if webhook_url:
            send_to_feishu(card, webhook_url)
        return

    log.info("精选 %d 篇研报，抓取摘要中...", len(selected))

    for r in selected:
        r["content"] = fetch_abstract(r["infoCode"])
        log.info("  - %s: %s", r["org"], r["title"][:30])

    ai_available = bool(api_key)
    if not ai_available:
        log.warning("DeepSeek API Key 未配置，将使用原始摘要")

    for r in selected:
        if ai_available:
            r["ai_summary"] = summarize_report(r, api_key)
        elif r["content"]:
            r["ai_summary"] = r["content"][:300]
        else:
            r["ai_summary"] = ""

    card = build_feishu_card(selected, date_str)

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - 飞书卡片内容预览")
        print("=" * 60)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not webhook_url:
        log.error("飞书 Webhook URL 未配置")
        sys.exit(1)

    success = send_to_feishu(card, webhook_url)
    if success:
        new_ids = {r["infoCode"] for r in selected if r["infoCode"]}
        sent_ids.update(new_ids)
        save_sent_ids(sent_ids)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
