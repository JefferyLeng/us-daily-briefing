#!/usr/bin/env python3
"""X/Twitter 监控 - SocialData API + 飞书推送"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

os.environ.setdefault("no_proxy", "*")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SENT_PATH = SCRIPT_DIR / "sent_tweets.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

SOCIALDATA_API_BASE = "https://api.socialdata.tools/twitter"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
TRANSLATE_PROMPT = "将以下推文内容翻译成中文，保留原始格式、换行和股票代码（如 $NVDA）。直接输出翻译结果，不要加任何前缀说明。"
DEFAULT_TARGET = "aleabitoreddit"


# ---- 配置 ----

def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.error("配置文件不存在: %s", p)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- 去重跟踪 ----

def load_sent_data():
    if not SENT_PATH.exists():
        return {"user_id": None, "tweets": {}}
    try:
        with open(SENT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = (datetime.now(CST) - timedelta(days=30)).strftime("%Y-%m-%d")
        tweets = data.get("tweets", {})
        tweets = {k: v for k, v in tweets.items() if v.get("date", "") >= cutoff}
        return {"user_id": data.get("user_id"), "tweets": tweets}
    except Exception:
        return {"user_id": None, "tweets": {}}


def save_sent_data(data):
    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---- SocialData API ----

def get_user_id(username, api_key):
    try:
        resp = requests.get(
            f"{SOCIALDATA_API_BASE}/user/{username}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            user_id = data.get("id_str") or str(data.get("id", ""))
            screen_name = data.get("screen_name", username)
            log.info("用户 @%s (ID: %s)", screen_name, user_id)
            return user_id, screen_name
        log.error("获取用户资料失败: %s %s", resp.status_code, resp.text[:200])
        return None, username
    except Exception as e:
        log.error("获取用户资料异常: %s", e)
        return None, username


def fetch_tweets(user_id, api_key):
    try:
        resp = requests.get(
            f"{SOCIALDATA_API_BASE}/user/{user_id}/tweets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            tweets = data.get("tweets", [])
            log.info("获取到 %d 条推文", len(tweets))
            return tweets
        if resp.status_code == 402:
            log.error("SocialData API 余额不足")
        else:
            log.error("获取推文失败: %s %s", resp.status_code, resp.text[:200])
        return []
    except Exception as e:
        log.error("获取推文异常: %s", e)
        return []


def filter_new_tweets(tweets, sent_tweets, target_sn):
    new = []
    for t in tweets:
        tid = t.get("id_str", "")
        if not tid or tid in sent_tweets:
            continue
        if t.get("retweeted_status"):
            continue
        reply_to = t.get("in_reply_to_screen_name", "")
        if reply_to and reply_to.lower() != target_sn.lower():
            continue
        new.append(t)
    return new


# ---- 翻译：DeepSeek ----

def translate_text(text, api_key, max_retries=2):
    if not text or not api_key:
        return ""
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
                        {"role": "system", "content": TRANSLATE_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "thinking": {"type": "disabled"},
                    "temperature": 0.1,
                    "max_tokens": 1000,
                },
                timeout=30,
            )
            result = resp.json()
            if "error" in result:
                raise ValueError(result["error"].get("message", str(result["error"])))
            content = result["choices"][0]["message"]["content"].strip()
            if content:
                log.info("翻译完成: %s...", content[:30])
                return content
            raise ValueError("翻译返回空内容")
        except Exception as e:
            log.warning("翻译失败 (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(2)
    return ""


# ---- 时间格式化 ----

def format_tweet_time(tweet):
    created = tweet.get("tweet_created_at", "")
    if not created:
        return ""
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        cst_time = dt.astimezone(CST)
        return cst_time.strftime("%m-%d %H:%M")
    except Exception:
        return ""


def format_stats(tweet):
    parts = []
    views = tweet.get("views_count", 0)
    if views:
        parts.append(f"{views / 10000:.1f}万 views" if views >= 10000 else f"{views} views")
    rts = tweet.get("retweet_count", 0)
    if rts:
        parts.append(f"{rts} RT")
    likes = tweet.get("favorite_count", 0)
    if likes:
        parts.append(f"{likes} ❤")
    replies = tweet.get("reply_count", 0)
    if replies:
        parts.append(f"{replies} 回复")
    return " | ".join(parts)


# ---- 飞书卡片构建 ----

def build_feishu_card(tweet, screen_name):
    tweet_id = tweet.get("id_str", "")
    full_text = tweet.get("full_text", "")
    tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

    time_str = format_tweet_time(tweet)
    stats = format_stats(tweet)
    translation = tweet.get("translation", "")

    lines = [f"**@{screen_name} 发布了新推文**"]

    if time_str:
        lines.append(f"时间: {time_str} (北京时间)")

    lines.append("")
    lines.append(full_text)

    if translation:
        lines.append("")
        lines.append(f"**中文翻译：**\n{translation}")

    if stats:
        lines.append("")
        lines.append(stats)

    lines.append("")
    lines.append(f"[查看原推]({tweet_url})")

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "数据来源：ColdTech SocialData | 白毛推文"},
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "X/Twitter 监控"},
                "template": "green",
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
    parser = argparse.ArgumentParser(description="X/Twitter 监控 - 飞书推送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片内容，不发送")
    parser.add_argument("--init", action="store_true", help="首次运行：仅记录现有推文，不推送")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = config.get("feishu_webhook_url", "")
    sd_api_key = config.get("socialdata_api_key", "")
    ds_api_key = config.get("deepseek_api_key", "")
    settings = config.get("twitter_settings", {})
    target = settings.get("target_username", DEFAULT_TARGET)

    if not sd_api_key:
        log.error("SocialData API Key 未配置")
        sys.exit(1)

    log.info("X/Twitter 监控启动 | 目标: @%s", target)

    sent_data = load_sent_data()
    user_id = sent_data.get("user_id")
    first_run = not sent_data.get("tweets")

    if not user_id:
        user_id, screen_name = get_user_id(target, sd_api_key)
        if not user_id:
            log.error("无法获取用户 ID")
            sys.exit(1)
        sent_data["user_id"] = user_id
        save_sent_data(sent_data)
    else:
        screen_name = target

    tweets = fetch_tweets(user_id, sd_api_key)
    if not tweets:
        log.info("未获取到推文")
        return

    sent_tweets = sent_data.get("tweets", {})
    new_tweets = filter_new_tweets(tweets, sent_tweets, screen_name)

    log.info("新推文: %d 条 (总共 %d 条)", len(new_tweets), len(tweets))

    # 首次运行或 --init：仅记录，不推送
    if first_run and not args.dry_run:
        log.info("首次运行，记录 %d 条现有推文不推送", len(tweets))
        now_str = datetime.now(CST).strftime("%Y-%m-%d")
        for t in tweets:
            tid = t.get("id_str", "")
            if tid:
                sent_data["tweets"][tid] = {
                    "date": now_str,
                    "text": t.get("full_text", "")[:100],
                }
        save_sent_data(sent_data)
        return

    if not new_tweets:
        return

    new_tweets.sort(key=lambda t: t.get("tweet_created_at", ""))

    if ds_api_key:
        log.info("翻译 %d 条推文...", len(new_tweets))
        for t in new_tweets:
            text = t.get("full_text", "")
            if text:
                t["translation"] = translate_text(text, ds_api_key)
    else:
        log.warning("DeepSeek API Key 未配置，跳过翻译")

    if args.dry_run:
        for t in new_tweets:
            actual_sn = t.get("user", {}).get("screen_name", screen_name)
            card = build_feishu_card(t, actual_sn)
            print("\n" + "=" * 60)
            print("DRY RUN - 飞书卡片内容预览")
            print("=" * 60)
            print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if not webhook_url:
        log.error("飞书 Webhook URL 未配置")
        sys.exit(1)

    now_str = datetime.now(CST).strftime("%Y-%m-%d")
    for t in new_tweets:
        actual_sn = t.get("user", {}).get("screen_name", screen_name)
        card = build_feishu_card(t, actual_sn)
        success = send_to_feishu(card, webhook_url)
        if success:
            tid = t.get("id_str", "")
            if tid:
                sent_data["tweets"][tid] = {
                    "date": now_str,
                    "text": t.get("full_text", "")[:100],
                }
                save_sent_data(sent_data)
        else:
            log.error("推送失败，停止处理")
            sys.exit(1)

        if len(new_tweets) > 1:
            time.sleep(2)

    log.info("推送完成，共 %d 条新推文", len(new_tweets))


if __name__ == "__main__":
    main()
