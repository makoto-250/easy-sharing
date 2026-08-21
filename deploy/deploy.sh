#!/bin/bash
# VPS 上での更新手順。root で実行する（ssh root@... から）。
# コードは /opt/easy-sharing に置き、アプリは非rootユーザー easyshare で動く。
set -euo pipefail

cd /opt/easy-sharing
git pull
.venv/bin/pip install -q -r requirements.txt
# git が作ったファイルを含め、コードは easyshare から読めるようにしておく
chown -R root:root /opt/easy-sharing
chmod 640 /opt/easy-sharing/.env
chown root:easyshare /opt/easy-sharing/.env
systemctl restart easy-sharing
sleep 1
systemctl --no-pager --lines=5 status easy-sharing
