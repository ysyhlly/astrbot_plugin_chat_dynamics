#!/usr/bin/env bash
set -euo pipefail
source_dir=/mnt/c/Users/ysyhly/Codex/laya-decision
cd /opt/astrbot/laya-decision
cp "$source_dir/turn-smoke.json" ./turn-smoke.json
cp "$source_dir/turn_diagnostics.py" ./turn_diagnostics.py
docker cp ./turn-smoke.json chat-dynamics-laya-laya-server-1:/tmp/turn-smoke.json
docker compose exec -T laya-server python - < turn_diagnostics.py || true
cp "$source_dir/services/laya_service/backend.py" services/laya_service/backend.py
cp "$source_dir/services/laya_service/app.py" services/laya_service/app.py
docker compose build laya-server
docker compose up -d laya-server laya-trainer
for attempt in $(seq 1 30); do
  if docker compose exec -T laya-server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/readyz',timeout=3)" >/dev/null 2>&1; then
    docker cp ./turn-smoke.json chat-dynamics-laya-laya-server-1:/tmp/turn-smoke.json
    docker compose exec -T laya-server python - < turn_diagnostics.py | tee turn-contract-verification.jsonl
    exit 0
  fi
  sleep 2
done
exit 1
