#!/usr/bin/env bash
set -euo pipefail
cd /opt/astrbot/laya-decision
case "${1:-predict}" in
predict)
  docker compose exec -T laya-server python - predict < smoke_remote.py | tee gpu-prediction-smoke.jsonl
  ;;
seed)
  docker compose exec -T laya-server python -c "from services.laya_service.jobs import Jobs; assert not any(j['state'] in ('running','queued') for j in Jobs('/state/jobs').list()), 'existing jobs must finish before isolated smoke'"
  docker compose stop laya-trainer
  docker compose exec -T laya-server python - seed < smoke_remote.py | tee synthetic-job.json
  docker compose run -d --name chat-dynamics-laya-smoke-worker --no-deps -e LAYA_DATASETS=/models/smoke-datasets laya-trainer
  ;;
status)
  docker compose exec -T laya-server python - status < smoke_remote.py
  docker logs --tail 30 chat-dynamics-laya-smoke-worker
  ;;
finish)
  docker stop chat-dynamics-laya-smoke-worker
  docker compose up -d laya-trainer
  docker compose exec -T laya-server python - predict < smoke_remote.py | tee gpu-prediction-after-training.jsonl
  docker compose exec -T laya-server python - status < smoke_remote.py | tee synthetic-training-result.jsonl
  docker compose exec -T laya-server python - guards < smoke_remote.py | tee promotion-guard-smoke.json
  docker compose restart laya-server
  for attempt in $(seq 1 30); do
    if docker compose exec -T laya-server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/readyz',timeout=3)" >/dev/null 2>&1; then
      docker compose exec -T laya-server python - predict < smoke_remote.py | tee gpu-prediction-after-restart.jsonl
      exit 0
    fi
    sleep 2
  done
  exit 1
  ;;
*) exit 2 ;;
esac
