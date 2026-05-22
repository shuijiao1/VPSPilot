#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="guko"
SERVICE_NAME="guko-bot"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
REMOTE_COMPOSE_URL="${REMOTE_COMPOSE_URL:-https://raw.githubusercontent.com/shuijiao1/GUKO/main/docker-compose.example.yml}"

cd "$(dirname "$0")/.."

log() { printf '[GUKO] %s\n' "$*"; }
warn() { printf '[GUKO] WARN: %s\n' "$*" >&2; }

if ! command -v docker >/dev/null 2>&1; then
  echo 'docker not found' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo 'docker compose plugin not found' >&2
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  log "$COMPOSE_FILE not found, downloading default compose file"
  curl -fsSLo "$COMPOSE_FILE" "$REMOTE_COMPOSE_URL"
elif ! grep -Eq '^name:[[:space:]]*guko[[:space:]]*$' "$COMPOSE_FILE"; then
  log "adding explicit compose project name: $PROJECT_NAME"
  tmp="$(mktemp)"
  printf 'name: %s\n' "$PROJECT_NAME" > "$tmp"
  cat "$COMPOSE_FILE" >> "$tmp"
  cat "$tmp" > "$COMPOSE_FILE"
  rm -f "$tmp"
fi

mkdir -p keys media results tmp
[ -f history.json ] || printf '{}\n' > history.json

old_project="$(docker inspect "$SERVICE_NAME" --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"
if [ -n "$old_project" ] && [ "$old_project" != "$PROJECT_NAME" ]; then
  log "old compose project detected: $old_project -> $PROJECT_NAME"
  log "stopping old container $SERVICE_NAME"
  docker stop "$SERVICE_NAME" >/dev/null || true
  log "removing old container $SERVICE_NAME; bind-mounted data files stay on host"
  docker rm "$SERVICE_NAME" >/dev/null || true
  old_network="${old_project}_default"
  if docker network inspect "$old_network" >/dev/null 2>&1; then
    docker network rm "$old_network" >/dev/null 2>&1 || warn "could not remove old network $old_network; it may still be in use"
  fi
fi

log "pulling latest image"
docker compose -p "$PROJECT_NAME" pull
log "starting $PROJECT_NAME"
docker compose -p "$PROJECT_NAME" up -d

new_project="$(docker inspect "$SERVICE_NAME" --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"
status="$(docker inspect "$SERVICE_NAME" --format '{{ .State.Status }}' 2>/dev/null || true)"
if [ "$new_project" != "$PROJECT_NAME" ] || [ "$status" != "running" ]; then
  echo "migration finished but verification failed: project=$new_project status=$status" >&2
  docker compose -p "$PROJECT_NAME" ps || true
  exit 1
fi

log "done: $SERVICE_NAME is running under compose project $PROJECT_NAME"
