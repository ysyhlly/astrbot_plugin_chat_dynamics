#!/usr/bin/env bash
set -euo pipefail
target=/opt/astrbot/laya-decision
source_dir=/mnt/c/Users/ysyhly/Codex/laya-decision
mkdir -p "$target/core" "$target/services" "$target/deploy"
cp -r "$source_dir/services/laya_service" "$target/services/"
cp -r "$source_dir/deploy/laya" "$target/deploy/"
cp "$source_dir"/decision_{dataset,evaluation,registry,catalog}.py "$target/core/"
cp "$target/deploy/laya/compose.existing-astrbot.yaml" "$target/compose.yaml"
if [ ! -f "$target/admin.env" ]; then
  umask 077
  printf 'LAYA_ADMIN_TOKEN=' > "$target/admin.env"
  openssl rand -hex 32 >> "$target/admin.env"
fi
chmod 600 "$target/admin.env"
printf '__pycache__/\n**/__pycache__/\n*.pyc\nadmin.env\n' > "$target/.dockerignore"
cd "$target"
docker compose config --quiet
docker network inspect astrbot_astrbot_network --format '{{.Name}}'
printf 'Laya deployment files prepared; administrator token exists (not printed).\n'
docker compose build laya-server
