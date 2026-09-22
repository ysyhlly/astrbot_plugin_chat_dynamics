#!/usr/bin/env bash
set -euo pipefail
source_dir=/mnt/c/Users/ysyhly/Codex/laya-decision
cd /opt/astrbot/laya-decision
cp "$source_dir"/decision_{dataset,evaluation,registry,catalog}.py core/
cp "$source_dir"/services/laya_service/*.py services/laya_service/
cp "$source_dir/Dockerfile" deploy/laya/Dockerfile
cp "$source_dir/smoke_remote.py" smoke_remote.py
docker compose build laya-server
docker compose up -d laya-server laya-trainer
for attempt in $(seq 1 40); do
  if docker compose exec -T laya-server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/readyz',timeout=3)" >/dev/null 2>&1; then
    docker compose exec -T laya-server python - predict < smoke_remote.py
    exit 0
  fi
  sleep 2
done
exit 1
