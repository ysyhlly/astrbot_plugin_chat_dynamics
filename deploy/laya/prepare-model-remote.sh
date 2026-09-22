#!/usr/bin/env bash
set -euo pipefail
target=/opt/astrbot/laya-decision
source_dir=/mnt/c/Users/ysyhly/Codex/laya-decision
cp "$source_dir/services/laya_service/jobs.py" "$target/services/laya_service/jobs.py"
cp "$source_dir/services/laya_service/training.py" "$target/services/laya_service/training.py"
cp "$source_dir"/decision_{dataset,evaluation,registry,catalog}.py "$target/core/"
cp "$source_dir/smoke_remote.py" "$target/smoke_remote.py"
cd "$target"
docker compose build laya-server
docker compose run --rm --no-deps -T laya-server python -m services.laya_service.training download-base --destination /models/base
docker compose up -d laya-server
for attempt in $(seq 1 60); do
  if docker compose exec -T laya-server python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8900/readyz',timeout=3).read().decode())"; then
    docker compose exec -T laya-server python - predict < smoke_remote.py
    exit 0
  fi
  sleep 2
done
docker compose logs --tail 80 laya-server
exit 1
