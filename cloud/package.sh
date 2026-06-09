#!/bin/bash
# 打包阿里云函数计算部署包
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$SCRIPT_DIR/dist"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

cp "$SCRIPT_DIR/main.py" "$DIST_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$DIST_DIR/"
cp "$ROOT_DIR/iresearch_report.py" "$DIST_DIR/"
cp "$ROOT_DIR/hk_daily_briefing.py" "$DIST_DIR/"
cp "$ROOT_DIR/us_daily_briefing.py" "$DIST_DIR/"

cd "$DIST_DIR"
zip -r "$SCRIPT_DIR/deploy.zip" ./*
cd "$SCRIPT_DIR"

echo ""
echo "部署包已创建: $SCRIPT_DIR/deploy.zip"
echo "包含文件:"
unzip -l "$SCRIPT_DIR/deploy.zip"
