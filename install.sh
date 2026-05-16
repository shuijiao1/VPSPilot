#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
[ -f servers.json ] || cp servers.example.json servers.json
mkdir -p keys media tmp
chmod 700 keys || true
cat <<'MSG'
GUKO initialized.

Next steps:
1. Create your own Telegram bot with BotFather.
2. Edit .env and set BOT_TOKEN, ALLOWED_USERS, ADMIN_USERS.
3. Start: docker compose up -d
4. In Telegram, send /addserver to add your first server.
MSG
