#!/bin/bash
# 美股每日早报 - 本地手动运行
# 双击此文件运行，或终端执行 bash us_daily_briefing.command

cd "$(dirname "$0")"

python3 -c "import yfinance" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "正在安装依赖（首次运行）..."
    pip3 install -r requirements.txt
    echo ""
fi

python3 us_daily_briefing.py "$@"
echo ""
echo "按回车键退出..."
read
