#!/bin/bash
# VPS 上での更新手順。/root/easy-sharing/deploy/deploy.sh として実行する。
set -euo pipefail

cd /root/easy-sharing
git pull
.venv/bin/pip install -q -r requirements.txt
systemctl restart easy-sharing
sleep 1
systemctl --no-pager --lines=5 status easy-sharing
