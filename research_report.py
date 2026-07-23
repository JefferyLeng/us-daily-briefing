#!/usr/bin/env python3
"""研报速递 - 四源合一（东方财富 + 慧博投研 + 艾瑞咨询 + TrendForce）+ DeepSeek总结 + 飞书推送"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

os.environ.setdefault("no_proxy", "*")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

# 去重文件：优先 SCRIPT_DIR，FC 等只读环境回退到 /tmp
def _resolve_sent_path(filename):
    primary = SCRIPT_DIR / filename
    try:
        primary.touch(exist_ok=True)
        primary.unlink(missing_ok=True)
        return primary
    except (PermissionError, OSError):
        tmp = Path("/tmp") / filename
        log.warning("SCRIPT_DIR 不可写，去重文件使用: %s", tmp)
        return tmp


SENT_EM_PATH = _resolve_sent_path("sent_reports.json")
SENT_HIBOR_PATH = _resolve_sent_path("sent_hibor.json")
SENT_IRESEARCH_PATH = _resolve_sent_path("sent_iresearch.json")
SENT_TRENDFORCE_PATH = _resolve_sent_path("sent_trendforce.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# ---- 东方财富 ----
REPORT_API = "https://reportapi.eastmoney.com/report/list"
DETAIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}
DEFAULT_PREFERRED_INSTITUTIONS = [
    "中信证券", "中金公司", "国泰君安", "华泰证券", "海通证券",
    "招商证券", "广发证券", "申万宏源", "东方证券", "天风证券",
    "国盛证券", "长江证券", "兴业证券", "平安证券", "中信建投",
]
HIGH_RATINGS = {"买入", "强推", "强力买入"}
MEDIUM_RATINGS = {"增持", "推荐", "跑赢行业", "优于大市"}
PREFERRED_TYPES = {"宏观", "行业", "策略"}
RATING_CHANGE_MAP = {1: "上调", 2: "维持", 3: "下调", 4: "首次覆盖"}
REPORT_TYPE_MAP = {
    1: "宏观", 2: "个股", 3: "行业", 4: "策略", 5: "其他",
    6: "固收", 7: "基金", 9: "期货", 10: "外汇", 11: "债券", 16: "期权",
}

# ---- 慧博投研 ----
# 列表页用桌面站精选研报页（每日精选），摘要抓取仍用移动站 wap_detail.aspx
HIBOR_BASE_URL = "https://www.hibor.com.cn"
HIBOR_LIST_URL = HIBOR_BASE_URL + "/elitelist.html"   # 全部研报 tab
HIBOR_LIST_PAGE_URL = HIBOR_BASE_URL + "/elitelist_{page}_0.html"
HIBOR_WAP_DETAIL_URL = "https://m.hibor.com.cn/wap_detail.aspx"
HIBOR_HEADERS = {
    # 注意：WAF 通过正则匹配 UA。Chrome/120.0.0.0 会被拦截，Chrome/120.0 反而通过
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
HIBOR_TYPE_MAP = {"宏观经济": "宏观", "投资策略": "策略", "行业分析": "行业", "公司调研": "个股"}
HIBOR_RATING_MAP = {
    "买入": "买入", "强推": "强推", "强力买入": "强力买入",
    "增持": "增持", "推荐": "推荐", "跑赢行业": "跑赢行业",
    "优于大市": "优于大市", "强于大市": "优于大市", "领先大市": "优于大市",
    "看好": "推荐",
}

# ---- 艾瑞咨询 ----
IRESEARCH_URL = "https://report.iresearch.cn/"

# ---- TrendForce 集邦咨询 ----
# 全球站 RSS 2.0，20 条 item，含标题/链接/日期/作者/描述
# 中文站 cn.trendforce.com SSL 被拦，故用全球站英文 RSS，由 DeepSeek 翻译成中文
TRENDFORCE_RSS_URL = "https://www.trendforce.com/news/feed/"
TRENDFORCE_SOURCE_NAME = "TrendForce 集邦咨询"

# ---- DeepSeek ----
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
SYSTEM_PROMPT = """你是一个专业的金融研报分析师。请根据以下研报信息，生成简洁的要点总结。

要求：
1. 用2-4个要点概括核心观点
2. 每个要点不超过50字
3. 重点突出：投资逻辑、关键数据、风险提示
4. 保持客观，使用第三人称
5. 如果信息不足，根据已有内容合理总结，不要编造数据
6. 直接输出要点，每行一个，以 "- " 开头，不要加标题和额外说明"""

IRESEARCH_SYSTEM_PROMPT = """你是一个专业的行业研究分析师。请根据以下行业研究报告信息，生成简洁的要点总结。

要求：
1. 用2-4个要点概括报告核心观点
2. 每个要点不超过50字
3. 重点突出：市场规模、增长趋势、关键数据、行业机会
4. 保持客观，使用第三人称
5. 直接输出要点，每行一个，以 "- " 开头，不要加标题和额外说明"""

TRENDFORCE_SYSTEM_PROMPT = """你是一个专业的科技产业分析师。请根据以下英文资讯，生成简洁的中文要点总结。

要求：
1. 用2-4个要点概括核心观点（请输出中文）
2. 每个要点不超过50字
3. 重点突出：技术趋势、价格变动、产能/出货、产业链影响
4. 保持客观，使用第三人称
5. 直接输出要点，每行一个，以 "- " 开头，不要加标题和额外说明"""


# ============================================================
# 配置
# ============================================================

def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 去重跟踪（通用）
# ============================================================

def _load_sent(path, retention_days=7):
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = (datetime.now(CST) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        return {k for k, v in data.get("entries", {}).items() if v >= cutoff}
    except Exception:
        return set()


def _save_sent(path, ids, retention_days=7):
    now_str = datetime.now(CST).strftime("%Y-%m-%d")
    entries = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        entries = old.get("entries", {})
    except Exception:
        pass
    for rid in ids:
        entries[rid] = now_str
    cutoff = (datetime.now(CST) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    entries = {k: v for k, v in entries.items() if v >= cutoff}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_updated": now_str, "entries": entries}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("去重文件写入失败 %s: %s（推送已成功，下次可能重复）", path, e)


def load_em_sent():
    return _load_sent(SENT_EM_PATH, 7)

def save_em_sent(ids):
    _save_sent(SENT_EM_PATH, ids, 7)

def load_hibor_sent():
    return _load_sent(SENT_HIBOR_PATH, 7)

def save_hibor_sent(ids):
    _save_sent(SENT_HIBOR_PATH, ids, 7)

def load_iresearch_sent():
    return _load_sent(SENT_IRESEARCH_PATH, 14)

def save_iresearch_sent(ids):
    _save_sent(SENT_IRESEARCH_PATH, ids, 14)


def load_trendforce_sent():
    return _load_sent(SENT_TRENDFORCE_PATH, 7)

def save_trendforce_sent(ids):
    _save_sent(SENT_TRENDFORCE_PATH, ids, 7)


# ============================================================
# 东方财富：数据获取
# ============================================================

def fetch_em_reports(date_str, max_retries=3):
    begin_date = date_str   # 仅当天，避免重复推送历史
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/report/",
    }
    all_reports = []
    seen_codes = set()
    for q_type in [1, 3, 4, 2]:
        params = {
            "industryCode": "*", "pageSize": 100, "industry": "*",
            "rating": "", "ratingChange": "",
            "beginTime": begin_date, "endTime": date_str,
            "pageNo": 1, "fields": "", "qType": q_type,
            "orgCode": "", "code": "*", "rcode": "", "cb": "",
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
                log.info("[东财] qType=%d(%s) 返回 %d 篇", q_type, type_label, len(reports))
                for r in reports:
                    code = r.get("infoCode", "")
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        all_reports.append(r)
                break
            except Exception as e:
                log.warning("[东财] 获取失败 qType=%d (%d/%d): %s", q_type, attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(3)
    log.info("[东财] 合计 %d 篇（去重后）", len(all_reports))
    return all_reports


def parse_em_report(r):
    info_code = r.get("infoCode", "")
    rating_change_raw = r.get("ratingChange", "")
    rating_change = RATING_CHANGE_MAP.get(rating_change_raw, "") if isinstance(rating_change_raw, int) else str(rating_change_raw).strip()
    report_type_raw = r.get("reportType", "")
    report_type = REPORT_TYPE_MAP.get(report_type_raw, "其他") if isinstance(report_type_raw, int) else (str(report_type_raw).strip() or "其他")
    return {
        "id": info_code,
        "title": r.get("title", "").strip(),
        "org": r.get("orgSName", "").strip(),
        "researcher": r.get("researcher", "").strip(),
        "publishDate": r.get("publishDate", ""),
        "rating": r.get("emRatingName", "").strip(),
        "ratingChange": rating_change,
        "reportType": report_type,
        "stockName": r.get("stockName", "").strip(),
        "stockCode": r.get("stockCode", "").strip(),
        "encodeUrl": r.get("encodeUrl", ""),
        "pdfUrl": f"https://data.eastmoney.com/report/zw_macresearch.jshtml?infocode={info_code}" if info_code else "",
        "content": "",
        "source": "eastmoney",
    }


def fetch_em_abstract(info_code):
    if not info_code:
        return ""
    try:
        resp = requests.get(
            "https://data.eastmoney.com/report/zw_macresearch.jshtml",
            params={"infocode": info_code},
            headers=DETAIL_HEADERS,
            timeout=15,
        )
        m = re.search(r'id="ctx-content"[^>]*>(.*?)</div>\s*(?:<div|<script)', resp.text, re.DOTALL)
        if m:
            clean = re.sub(r'<[^>]+>', '', m.group(1))
            clean = re.sub(r'\s+', ' ', clean).strip()
            return clean[:1500]
    except Exception as e:
        log.warning("[东财] 摘要失败 %s: %s", info_code, e)
    return ""


# ============================================================
# 慧博投研：数据获取
# ============================================================

def _hibor_http_get(url, params=None, max_retries=3, delay=1.0):
    """桌面站 www.hibor.com.cn 启用了 TLS 指纹检测，requests 会被 302 到 tip.html。
    优先用 curl 子进程抓取（指纹像 Chrome），失败时回退到 requests。"""
    full_url = url
    if params:
        from urllib.parse import urlencode
        full_url = f"{url}?{urlencode(params)}"

    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(
                [
                    "curl", "-sL", "--max-time", "20",
                    "-A", HIBOR_HEADERS["User-Agent"],
                    "-H", f"Accept-Language: {HIBOR_HEADERS['Accept-Language']}",
                    full_url,
                ],
                capture_output=True, timeout=25,
            )
            html = result.stdout.decode("utf-8", errors="replace")
            # tip.html 是反爬提示页，长度通常 < 1KB 且不含 trContent
            if len(html) > 3000 or "trContent" in html:
                return html
            log.warning("[慧博] curl 返回可疑页 (%d 字节, attempt=%d)", len(html), attempt)
        except Exception as e:
            log.warning("[慧博] curl %s 失败 (%d/%d): %s", full_url, attempt, max_retries, e)

        # 回退到 requests（移动站可用）
        try:
            resp = requests.get(url, params=params, headers=HIBOR_HEADERS, timeout=15)
            resp.encoding = resp.apparent_encoding
            html = resp.text
            if len(html) > 3000 or "trContent" in html:
                return html
        except Exception as e:
            log.warning("[慧博] requests %s 失败 (%d/%d): %s", full_url, attempt, max_retries, e)

        if attempt < max_retries:
            time.sleep(delay)
    return ""


def fetch_hibor_reports(hibor_config, target_date=None):
    """从 https://www.hibor.com.cn/elitelist.html 抓取"全部研报" tab（每日精选）。
    target_date: 'YYYY-MM-DD'，仅保留当天发布的研报；None 则不过滤。"""
    max_pages = hibor_config.get("max_pages", 1)
    delay = hibor_config.get("request_delay", 1.0)

    row_re = re.compile(
        r'<tr class="trContent">\s*'
        r'<td[^>]*>.*?</td>\s*'
        r'<td[^>]*>\s*<a\s+href="(/data/([a-f0-9]+)\.html)"\s+title="([^"]+)"[^>]*>[^<]*</a>\s*</td>\s*'
        r'<td[^>]*>\s*([^<]*)</td>\s*'      # 类别
        r'<td[^>]*>\s*([^<]*)</td>\s*'      # 研究员
        r'<td[^>]*>\s*([^<]*)</td>\s*'      # 页数
        r'<td[^>]*>\s*([^<]*)</td>\s*'      # 日期
        r'</tr>',
        re.DOTALL,
    )

    all_reports = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        url = HIBOR_LIST_URL if page == 1 else HIBOR_LIST_PAGE_URL.format(page=page)
        html = _hibor_http_get(url, max_retries=3, delay=delay)
        if not html:
            continue

        rows = row_re.findall(html)
        for link, rid, full_title, cat_raw, author_raw, pages_raw, date_raw in rows:
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)

            title = full_title.strip()
            cat_name = cat_raw.strip()
            author = author_raw.strip()
            pub_date = date_raw.strip()

            # 仅保留 target_date 当天发布的研报（pub_date 格式 'YYYY-MM-DD HH:MM:SS'）
            if target_date and not pub_date.startswith(target_date):
                continue

            org = ""
            org_match = re.match(r'^([^-]+?)-(.*?)-\d{6}$', title)
            if org_match:
                org = org_match.group(1).strip()

            report_type = HIBOR_TYPE_MAP.get(cat_name, cat_name or "其他")
            detail_url = f"{HIBOR_BASE_URL}{link}"

            all_reports.append({
                "id": rid,
                "title": title,
                "org": org,
                "researcher": author,
                "publishDate": pub_date,
                "rating": "",
                "ratingChange": "",
                "reportType": report_type,
                "stockName": "",
                "stockCode": "",
                "detailUrl": detail_url,
                "content": "",
                "source": "hibor",
            })

        log.info("[慧博] elitelist page=%d: %d 篇", page, len(rows))
        if page < max_pages:
            time.sleep(delay)

    log.info("[慧博] 合计 %d 篇（去重后）", len(all_reports))
    return all_reports


def fetch_hibor_abstract(rid, delay=2.5):
    if not rid:
        return ""
    try:
        resp = requests.get(
            HIBOR_WAP_DETAIL_URL,
            params={"id": rid},
            headers=HIBOR_HEADERS,
            timeout=15,
        )
        resp.encoding = resp.apparent_encoding
        html = resp.text

        m = re.search(r'<meta\s+name="description"\s+content="(.*?)"', html)
        if m:
            clean = re.sub(r'<[^>]+>', '', m.group(1))
            clean = re.sub(r'\s+', ' ', clean).strip()
            if len(clean) > 30:
                return clean[:1500]

        m2 = re.search(r'<div class="doc-content">(.*?)</div>\s*<div class="open-app', html, re.DOTALL)
        if m2:
            clean = re.sub(r'<[^>]+>', '', m2.group(1))
            clean = re.sub(r'\s+', ' ', clean).strip()
            return clean[:1500]
    except Exception as e:
        log.warning("[慧博] 摘要失败 %s: %s", rid, e)
    return ""


# ============================================================
# 艾瑞咨询：数据获取
# ============================================================

def fetch_iresearch_reports():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(IRESEARCH_URL, headers=headers, timeout=15)
        resp.encoding = resp.apparent_encoding
        html = resp.text
    except Exception as e:
        log.error("[艾瑞] 获取页面失败: %s", e)
        return []

    idx = html.find("ulroot3")
    if idx < 0:
        log.warning("[艾瑞] 未找到报告列表区域")
        return []

    block = html[idx:idx + 20000]
    items = re.findall(r'<li[^>]*id="f[^"]*"[^>]*>(.*?)</li>', block, re.DOTALL)
    log.info("[艾瑞] 解析到 %d 个条目", len(items))

    reports = []
    for item in items:
        link_match = re.search(
            r'href="(https://report\.iresearch\.cn/report/[^"]*)"[^>]*target="_blank">(.*?)</a>',
            item, re.DOTALL,
        )
        if not link_match:
            continue

        link = link_match.group(1)
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
            "org": "艾瑞咨询",
            "researcher": "",
            "publishDate": date,
            "rating": "",
            "ratingChange": "",
            "reportType": "行业",
            "stockName": "",
            "stockCode": "",
            "detailUrl": link,
            "description": desc,
            "content": desc,
            "source": "iresearch",
        })

    return reports


def filter_iresearch_recent(reports, days=7):
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = []
    for r in reports:
        if not r["publishDate"]:
            continue
        try:
            rdate = datetime.strptime(r["publishDate"], "%Y-%m-%d")
            if rdate.strftime("%Y-%m-%d") >= cutoff:
                recent.append(r)
        except ValueError:
            pass
    return recent


# ---- TrendForce：RSS 抓取 ----

def fetch_trendforce_reports():
    """从 TrendForce 全球站 RSS 抓取最新英文资讯。

    RSS 2.0 标准格式，已实测 20 条 item 含 title/link/pubDate/description。
    中文站 cn.trendforce.com SSL 被拦，故走全球站英文 RSS。
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    try:
        resp = requests.get(
            TRENDFORCE_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
        )
        root = ET.fromstring(resp.text)
    except Exception as e:
        log.error("[TrendForce] RSS 抓取失败: %s", e)
        return []

    items = root.findall(".//item")
    log.info("[TrendForce] RSS 解析到 %d 条", len(items))

    reports = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        desc_raw = (item.findtext("description") or "").strip()

        if not title or not link:
            continue

        # 日期解析 RFC822 → %Y-%m-%d
        pub_date = ""
        if pub_date_raw:
            try:
                dt = parsedate_to_datetime(pub_date_raw)
                pub_date = dt.astimezone(CST).strftime("%Y-%m-%d")
            except Exception:
                pass

        # 去掉 description 里的 HTML 标签
        desc = re.sub(r"<[^>]+>", "", desc_raw).strip()

        # id 取 link 末尾 slug；为空则退回 link 本身
        rid = link.rstrip("/").split("/")[-1] or link

        # 标题清理：去掉 [News] / [Insights] / [Press] 前缀
        clean_title = re.sub(r"^\[(News|Insights|Press)\]\s*", "", title)

        reports.append({
            "id": rid,
            "title": clean_title,
            "org": TRENDFORCE_SOURCE_NAME,
            "researcher": "",
            "publishDate": pub_date,
            "rating": "",
            "ratingChange": "",
            "reportType": "行业",
            "stockName": "",
            "stockCode": "",
            "detailUrl": link,
            "description": desc,
            "content": desc,
            "source": "trendforce",
        })

    return reports


# ============================================================
# 评分 & 筛选
# ============================================================

def score_report(report, preferred, source_bonus=0):
    score = 0

    if report.get("org") in preferred:
        score += 30

    rating = report.get("rating", "")
    if rating in HIGH_RATINGS:
        score += 20
    elif rating in MEDIUM_RATINGS:
        score += 15

    rc = report.get("ratingChange", "")
    if rc == "首次覆盖":
        score += 15
    elif rc == "上调":
        score += 12
    elif rc == "维持":
        score += 5
    elif rc:
        score += 3

    if report.get("reportType") in PREFERRED_TYPES:
        score += 25

    content_len = len(report.get("content", ""))
    if content_len > 50:
        score += 15
    elif content_len > 0:
        score += 8

    score += source_bonus
    return score


def _select_from(reports, sent_ids, preferred, count, source_bonus=0, max_stock=5):
    filtered = [r for r in reports if r["id"] not in sent_ids]
    for r in filtered:
        r["_score"] = score_report(r, preferred, source_bonus)
    filtered.sort(key=lambda x: x["_score"], reverse=True)

    selected = []
    stock_count = 0
    for r in filtered:
        if len(selected) >= count:
            break
        if r.get("reportType") == "个股":
            if stock_count >= max_stock:
                continue
            stock_count += 1
        selected.append(r)

    for r in selected:
        r.pop("_score", None)
    return selected


def select_reports(em_reports, hb_reports, ir_reports, tf_reports,
                   preferred, em_sent, hb_sent, ir_sent, tf_sent,
                   em_count, hb_count, ir_count, tf_count,
                   hibor_bonus=20):
    em_sel = _select_from(em_reports, em_sent, preferred, em_count, source_bonus=0)
    hb_sel = _select_from(hb_reports, hb_sent, preferred, hb_count, source_bonus=hibor_bonus)
    ir_sel = _select_from(ir_reports, ir_sent, preferred, ir_count, source_bonus=0)
    tf_sel = _select_from(tf_reports, tf_sent, preferred, tf_count, source_bonus=0)
    return em_sel, hb_sel, ir_sel, tf_sel


# ============================================================
# AI 总结
# ============================================================

def summarize_report(report, api_key, system_prompt=None, max_retries=2):
    prompt = system_prompt or SYSTEM_PROMPT
    user_parts = [f"标题：{report['title']}"]
    if report.get("org"):
        user_parts.append(f"机构：{report['org']}")
    if report.get("researcher"):
        user_parts.append(f"研究员：{report['researcher']}")
    if report.get("rating"):
        user_parts.append(f"评级：{report['rating']}")
    if report.get("ratingChange"):
        user_parts.append(f"评级变动：{report['ratingChange']}")
    if report.get("stockName"):
        code = f"({report['stockCode']})" if report.get("stockCode") else ""
        user_parts.append(f"相关股票：{report['stockName']}{code}")
    if report.get("content"):
        user_parts.append(f"摘要：{report['content'][:1500]}")

    user_message = "\n".join(user_parts)

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_API,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-v4-pro",
                    "messages": [
                        {"role": "system", "content": prompt},
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
            log.warning("AI总结失败 (%d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(3)

    if report.get("content"):
        return report["content"][:300]
    return "（信息不足，暂无总结）"


# ============================================================
# 飞书卡片构建
# ============================================================

def _build_em_block(index, r):
    lines = [f"**{index}. {r['title']}**"]
    meta = [p for p in [r.get("org"), r.get("researcher")] if p]
    if meta:
        lines.append(" | ".join(meta))
    rating_parts = []
    if r.get("reportType") == "个股":
        if r["rating"]:
            s = r["rating"]
            if r.get("ratingChange"):
                s += f"({r['ratingChange']})"
            rating_parts.append(s)
        if r.get("stockName"):
            code = f"({r['stockCode']})" if r.get("stockCode") else ""
            rating_parts.append(f"{r['stockName']}{code}")
    elif r.get("stockName"):
        code = f"({r['stockCode']})" if r.get("stockCode") else ""
        rating_parts.append(f"{r['stockName']}{code}")
    if rating_parts:
        lines.append(" | ".join(rating_parts))
    if r.get("ai_summary"):
        lines.append(f"\n**核心提炼：**\n{r['ai_summary']}")
    if r.get("pdfUrl"):
        lines.append(f"\n[查看原文]({r['pdfUrl']})")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def _build_hibor_block(index, r):
    lines = [f"**{index}. {r['title']}**"]
    meta = [p for p in [r.get("org"), r.get("researcher")] if p]
    if meta:
        lines.append(" | ".join(meta))
    rating_parts = []
    if r.get("rating"):
        s = r["rating"]
        if r.get("ratingChange"):
            s += f"({r['ratingChange']})"
        rating_parts.append(s)
    if rating_parts:
        lines.append(" | ".join(rating_parts))
    if r.get("ai_summary"):
        lines.append(f"\n**核心提炼：**\n{r['ai_summary']}")
    if r.get("detailUrl"):
        lines.append(f"\n[查看原文]({r['detailUrl']})")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def _build_iresearch_block(index, r):
    lines = [f"**{index}. {r['title']}**"]
    if r.get("publishDate"):
        lines.append(f"发布日期: {r['publishDate']}")
    if r.get("ai_summary"):
        lines.append(f"\n**核心提炼：**\n{r['ai_summary']}")
    if r.get("detailUrl"):
        lines.append(f"\n[查看原文]({r['detailUrl']})")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def _build_trendforce_block(index, r):
    # TrendForce 为英文资讯，AI 已译成中文要点；标题保留英文原文
    lines = [f"**{index}. {r['title']}**"]
    if r.get("publishDate"):
        lines.append(f"发布日期: {r['publishDate']} | 来源: TrendForce")
    if r.get("ai_summary"):
        lines.append(f"\n**核心提炼：**\n{r['ai_summary']}")
    if r.get("detailUrl"):
        lines.append(f"\n[查看原文]({r['detailUrl']})")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def build_feishu_card(em_reports, hb_reports, ir_reports, tf_reports, date_str):
    header_title = f"研报速递 | {date_str}"
    elements = []
    total = len(em_reports) + len(hb_reports) + len(ir_reports) + len(tf_reports)

    if total == 0:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**今日暂无新研报**"}})
        elements.append({"tag": "hr"})
        now_str = datetime.now(CST).strftime("%H:%M")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"生成时间: {now_str} | 数据来源: ColdTech"}})
        return {"msg_type": "interactive", "card": {
            "header": {"title": {"tag": "plain_text", "content": header_title}, "template": "orange"},
            "elements": elements,
        }}

    # 总览
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**本期精选{total}篇研报**"}})

    # ---- 东方财富专区 ----
    if em_reports:
        orgs = list({r["org"] for r in em_reports if r.get("org")})
        org_hint = "、".join(orgs[:5])
        if len(orgs) > 5:
            org_hint += f"等{len(orgs)}家机构"
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**东方财富研报 ({len(em_reports)}篇)**\n来自{org_hint}"}})
        elements.append({"tag": "hr"})
        for i, r in enumerate(em_reports, 1):
            elements.append(_build_em_block(i, r))
            elements.append({"tag": "hr"})

    # ---- 慧博投研专区 ----
    if hb_reports:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**慧博投研专区 ({len(hb_reports)}篇)**"}})
        elements.append({"tag": "hr"})
        for i, r in enumerate(hb_reports, 1):
            elements.append(_build_hibor_block(i, r))
            elements.append({"tag": "hr"})

    # ---- 艾瑞行业研究专区 ----
    if ir_reports:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**艾瑞行业研究 ({len(ir_reports)}篇)**"}})
        elements.append({"tag": "hr"})
        for i, r in enumerate(ir_reports, 1):
            elements.append(_build_iresearch_block(i, r))
            elements.append({"tag": "hr"})

    # ---- TrendForce 科技产业资讯专区 ----
    if tf_reports:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**TrendForce 集邦咨询 ({len(tf_reports)}篇)**\n半导体/存储/AI 硬件等英文资讯，已译成中文要点"}})
        elements.append({"tag": "hr"})
        for i, r in enumerate(tf_reports, 1):
            elements.append(_build_trendforce_block(i, r))
            elements.append({"tag": "hr"})

    # 页脚
    now_str = datetime.now(CST).strftime("%H:%M")
    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"生成时间: {now_str} | 数据来源: ColdTech + 慧博投研 + 艾瑞咨询 + TrendForce"}})

    card = {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text", "content": header_title}, "template": "orange"},
        "elements": elements,
    }}

    # 28KB 超限裁剪
    card_json = json.dumps(card, ensure_ascii=False)
    if len(card_json.encode("utf-8")) > 28000:
        while len(card_json.encode("utf-8")) > 28000 and len(elements) > 4:
            elements.pop(-3)
            card_json = json.dumps(card, ensure_ascii=False)

    return card


# ============================================================
# 飞书推送
# ============================================================

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
            log.warning("飞书返回错误 (%d/%d): %s", attempt, max_retries, result)
        except Exception as e:
            log.warning("飞书推送异常 (%d/%d): %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(5)
    log.error("飞书推送失败，已达最大重试次数")
    return False


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="研报速递 - 四源合一飞书推送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略非交易日）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")
    api_key = config.get("deepseek_api_key", "")
    settings = config.get("report_settings", {})
    hibor_config = config.get("hibor_settings", {})
    iresearch_config = config.get("iresearch_settings", {})
    trendforce_config = config.get("trendforce_settings", {})
    preferred = settings.get("preferred_institutions", DEFAULT_PREFERRED_INSTITUTIONS)

    em_count = settings.get("eastmoney_count", 10)
    hb_count = settings.get("hibor_count", 10)
    ir_count = settings.get("iresearch_count", 10)
    tf_count = settings.get("trendforce_count", trendforce_config.get("count", 5))
    hibor_bonus = hibor_config.get("source_bonus", 20)

    date_str = datetime.now(CST).strftime("%Y-%m-%d")

    if not args.force and datetime.now(CST).weekday() >= 5:
        log.info("周末，跳过。使用 --force 强制运行。")
        return

    log.info("研报速递启动 | 日期: %s | 东财%d + 慧博%d + 艾瑞%d + TrendForce%d",
             date_str, em_count, hb_count, ir_count, tf_count)

    # ---- Phase 1: 抓取三源 ----
    log.info("=== 抓取东方财富研报 ===")
    raw_em = fetch_em_reports(date_str)
    parsed_em = [parse_em_report(r) for r in raw_em]

    parsed_hb = []
    if hibor_config.get("enabled", True):
        log.info("=== 抓取慧博投研 ===")
        parsed_hb = fetch_hibor_reports(hibor_config, target_date=date_str)
        log.info("[慧博] 当天(%s) %d 篇", date_str, len(parsed_hb))

    parsed_ir = []
    if iresearch_config.get("enabled", True):
        log.info("=== 抓取艾瑞咨询 ===")
        raw_ir = fetch_iresearch_reports()
        parsed_ir = filter_iresearch_recent(raw_ir, days=1)
        log.info("[艾瑞] 当天 %d 篇", len(parsed_ir))

    parsed_tf = []
    if trendforce_config.get("enabled", True):
        log.info("=== 抓取 TrendForce ===")
        raw_tf = fetch_trendforce_reports()
        # TrendForce RSS 更新不规律（实测曾冻结 3 周），不做日期过滤，
        # 直接交给 sent_trendforce.json 去重：未读的最新 N 篇入选。
        # RSS 默认按时间倒序，配合 _select_from 稳定排序即"最新未读优先"。
        parsed_tf = raw_tf
        log.info("[TrendForce] RSS 共 %d 篇（去重后筛选）", len(parsed_tf))

    # ---- Phase 2: 去重 & 筛选 ----
    em_sent = load_em_sent()
    hb_sent = load_hibor_sent()
    ir_sent = load_iresearch_sent()
    tf_sent = load_trendforce_sent()

    em_sel, hb_sel, ir_sel, tf_sel = select_reports(
        parsed_em, parsed_hb, parsed_ir, parsed_tf,
        preferred, em_sent, hb_sent, ir_sent, tf_sent,
        em_count, hb_count, ir_count, tf_count,
        hibor_bonus=hibor_bonus,
    )

    if not em_sel and not hb_sel and not ir_sel and not tf_sel:
        log.info("当天(%s)无新研报，跳过推送", date_str)
        if args.dry_run:
            print(json.dumps(build_feishu_card([], [], [], [], date_str), ensure_ascii=False, indent=2))
        return

    log.info("精选: 东财 %d + 慧博 %d + 艾瑞 %d + TrendForce %d = %d 篇",
             len(em_sel), len(hb_sel), len(ir_sel), len(tf_sel),
             len(em_sel) + len(hb_sel) + len(ir_sel) + len(tf_sel))

    # ---- Phase 3: 抓取摘要 ----
    for r in em_sel:
        r["content"] = fetch_em_abstract(r["id"])

    hibor_delay = hibor_config.get("request_delay", 2.5)
    for r in hb_sel:
        r["content"] = fetch_hibor_abstract(r["id"], delay=0)
        time.sleep(hibor_delay)

    # 艾研报告已有 description 作为 content，无需额外抓取

    # ---- Phase 4: AI 总结 ----
    ai_available = bool(api_key)
    if not ai_available:
        log.warning("DeepSeek API Key 未配置，将使用原始摘要")

    for r in em_sel + hb_sel:
        if ai_available:
            r["ai_summary"] = summarize_report(r, api_key, SYSTEM_PROMPT)
        elif r.get("content"):
            r["ai_summary"] = r["content"][:300]
        else:
            r["ai_summary"] = ""

    for r in ir_sel:
        if ai_available:
            r["ai_summary"] = summarize_report(r, api_key, IRESEARCH_SYSTEM_PROMPT)
        elif r.get("content"):
            r["ai_summary"] = r["content"][:300]
        else:
            r["ai_summary"] = ""

    # TrendForce 英文资讯：用专门 prompt 让 DeepSeek 译成中文要点
    for r in tf_sel:
        if ai_available:
            r["ai_summary"] = summarize_report(r, api_key, TRENDFORCE_SYSTEM_PROMPT)
        elif r.get("content"):
            r["ai_summary"] = r["content"][:300]
        else:
            r["ai_summary"] = ""

    # ---- Phase 5: 构建卡片 & 推送 ----
    card = build_feishu_card(em_sel, hb_sel, ir_sel, tf_sel, date_str)

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
        new_em = {r["id"] for r in em_sel if r.get("id")}
        em_sent.update(new_em)
        save_em_sent(em_sent)

        new_hb = {r["id"] for r in hb_sel if r.get("id")}
        hb_sent.update(new_hb)
        save_hibor_sent(hb_sent)

        new_ir = {r["id"] for r in ir_sel if r.get("id")}
        ir_sent.update(new_ir)
        save_iresearch_sent(ir_sent)

        new_tf = {r["id"] for r in tf_sel if r.get("id")}
        tf_sent.update(new_tf)
        save_trendforce_sent(tf_sent)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
