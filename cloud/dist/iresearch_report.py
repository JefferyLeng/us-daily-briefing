#!/usr/bin/env python3
"""行业研究报告 - 艾瑞咨询研报抓取 + DeepSeek总结 + 飞书推送"""

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
SENT_PATH = SCRIPT_DIR / "sent_iresearch.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
IRESEARCH_URL = "https://report.iresearch.cn/"

SYSTEM_PROMPT = """你是一个专业的行业研究分析师。请根据以下行业研究报告信息，生成简洁的要点总结。

要求：
1. 用2-4个要点概括报告核心观点
2. 每个要点不超过50字
3. 重点突出：市场规模、增长趋势、关键数据、行业机会
4. 保持客观，使用第三人称
5. 直接输出要点，每行一个，以 "- " 开头，不要加标题和额外说明"""


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
        cutoff = (datetime.now(CST) - timedelta(days=14)).strftime("%Y-%m-%d")
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

    cutoff = (datetime.now(CST) - timedelta(days=14)).strftime("%Y-%m-%d")
    entries = {k: v for k, v in entries.items() if v >= cutoff}

    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_updated": now_str, "entries": entries}, f, ensure_ascii=False, indent=2)


# ---- 数据获取：艾瑞咨询 ----

def fetch_iresearch_reports():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        resp = requests.get(IRESEARCH_URL, headers=headers, timeout=15)
        resp.encoding = resp.apparent_encoding
        html = resp.text
    except Exception as e:
        log.error("获取艾瑞咨询页面失败: %s", e)
        return []

    idx = html.find("ulroot3")
    if idx < 0:
        log.warning("未找到报告列表区域")
        return []

    block = html[idx:idx + 20000]
    items = re.findall(r'<li[^>]*id="f[^"]*"[^>]*>(.*?)</li>', block, re.DOTALL)
    log.info("艾瑞咨询页面解析到 %d 个报告条目", len(items))

    reports = []
    for item in items:
        link_match = re.search(
            r'href="(https://report\.iresearch\.cn/report/[^"]*)"[^>]*target="_blank">(.*?)</a>',
            item, re.DOTALL,
        )
        if not link_match:
            continue

        link = link_match.group(1)
        # Find the real title - look for the second occurrence which has the actual title
        all_titles = re.findall(r'target="_blank">(.*?)</a>', item, re.DOTALL)
        title = ""
        for t in all_titles:
            clean = re.sub(r'<[^>]+>', '', t).strip()
            if clean and clean not in ("报告", "研究", "数据"):
                title = clean
                break
        if not title:
            title = re.sub(r'<[^>]+>', '', link_match.group(2)).strip()

        desc_match = re.search(r'<p[^>]*>(.*?)</p>', item, re.DOTALL)
        desc = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip() if desc_match else ""

        date_match = re.search(r'(\d{4}/\d{1,2}/\d{1,2})', item)
        date = date_match.group(1).replace("/", "-") if date_match else ""

        rid = link.split("/")[-1].replace(".shtml", "")

        reports.append({
            "id": rid,
            "title": title,
            "link": link,
            "date": date,
            "description": desc,
        })

    return reports


def filter_recent(reports, days=7):
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = []
    for r in reports:
        if not r["date"]:
            continue
        try:
            rdate = datetime.strptime(r["date"], "%Y-%m-%d")
            if rdate.strftime("%Y-%m-%d") >= cutoff:
                recent.append(r)
        except ValueError:
            pass
    return recent


# ---- AI总结：DeepSeek ----

def summarize_report(report, api_key, max_retries=2):
    user_parts = [f"标题：{report['title']}"]
    if report.get("description"):
        user_parts.append(f"摘要：{report['description'][:1500]}")

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

    if report.get("description"):
        return report["description"][:300]
    return ""


# ---- 飞书卡片构建 ----

def build_feishu_card(reports, date_str):
    header_title = f"行业研究报告 | {date_str}"
    elements = []

    if not reports:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**今日暂无新行业报告**"},
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
                    "template": "blue",
                },
                "elements": elements,
            },
        }

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**本期精选{len(reports)}篇行业报告**，来自艾瑞咨询"},
    })
    elements.append({"tag": "hr"})

    for i, r in enumerate(reports, 1):
        lines = [f"**{i}. {r['title']}**"]

        if r.get("date"):
            lines.append(f"发布日期: {r['date']}")

        summary = r.get("ai_summary", "")
        if summary:
            lines.append(f"\n**核心提炼：**\n{summary}")

        if r.get("link"):
            lines.append(f"\n[查看原文]({r['link']})")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
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
                "template": "blue",
            },
            "elements": elements,
        },
    }


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
    parser = argparse.ArgumentParser(description="行业研究报告 - 艾瑞咨询飞书推送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略非交易日）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")
    api_key = config.get("deepseek_api_key", "")
    settings = config.get("iresearch_settings", {})
    max_reports = settings.get("max_reports", 8)

    date_str = datetime.now(CST).strftime("%Y-%m-%d")

    log.info("行业研究报告启动 | 日期: %s", date_str)

    raw_reports = fetch_iresearch_reports()
    if not raw_reports:
        log.warning("未获取到艾瑞咨询报告")
        card = build_feishu_card([], date_str)
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return
        if webhook_url:
            send_to_feishu(card, webhook_url)
        return

    recent = filter_recent(raw_reports, days=7)
    log.info("近7天报告: %d 篇", len(recent))

    sent_ids = load_sent_ids()
    new_reports = [r for r in recent if r["id"] not in sent_ids]
    log.info("去重后新报告: %d 篇", len(new_reports))

    if not new_reports:
        log.info("没有新的未推送报告")
        card = build_feishu_card([], date_str)
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return
        if webhook_url:
            send_to_feishu(card, webhook_url)
        return

    selected = new_reports[:max_reports]

    ai_available = bool(api_key)
    if not ai_available:
        log.warning("DeepSeek API Key 未配置，将使用原始摘要")

    for r in selected:
        if ai_available:
            r["ai_summary"] = summarize_report(r, api_key)
        elif r.get("description"):
            r["ai_summary"] = r["description"][:300]
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
        new_ids = {r["id"] for r in selected if r["id"]}
        sent_ids.update(new_ids)
        save_sent_ids(sent_ids)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
