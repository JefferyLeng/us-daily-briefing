#!/usr/bin/env python3
"""阿里云函数计算入口 — 定时触发研报/简报推送"""

import json
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger()

CODE_DIR = os.path.dirname(os.path.abspath(__file__))


def handler(event, context):
    # Build config.json from environment variables
    cfg = {}
    cfg_path = os.path.join(CODE_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    for env_key, cfg_key in [
        ("FEISHU_WEBHOOK_URL", "feishu_webhook_url"),
        ("DEEPSEEK_API_KEY", "deepseek_api_key"),
        ("SOCIALDATA_API_KEY", "socialdata_api_key"),
    ]:
        val = os.environ.get(env_key, "")
        if val:
            cfg[cfg_key] = val

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    script = os.environ.get("TARGET_SCRIPT", "")
    if not script:
        log.error("TARGET_SCRIPT 环境变量未设置")
        return "error: TARGET_SCRIPT not set"

    script_path = os.path.join(CODE_DIR, script)
    if not os.path.exists(script_path):
        log.error("脚本不存在: %s", script_path)
        return f"error: script not found: {script}"

    log.info("运行脚本: %s", script)
    result = subprocess.run(
        [sys.executable, script_path, "--force"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=CODE_DIR,
    )

    if result.stdout:
        log.info(result.stdout[-1500:])
    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            log.warning(line[-200:])

    if result.returncode != 0:
        return f"error: exit code {result.returncode}"

    return "ok"
