#!/bin/zsh
cd "$(dirname "$0")"
echo "期货选品监控网页启动中..."
echo "打开浏览器访问: http://127.0.0.1:8787"
python3 -m futures_monitor.web --host 0.0.0.0 --port 8787
