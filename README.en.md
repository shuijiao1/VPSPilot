# GUKO

[![Docker Image](https://img.shields.io/badge/ghcr.io-guko-blue?logo=docker)](https://github.com/shuijiao1/GUKO/pkgs/container/guko)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[中文](README.md) | **English**

**A lightweight VPS / server management Telegram Bot: server status dashboard, SSH login management, and common benchmark / diagnostic tools.**

> Open a private chat with the Bot to view server lists, status details, traffic, and resource usage. Add servers, test SSH, and run IP quality, NodeQuality, streaming unlock, NextTrace, GB5, and other common checks from Telegram.  
> Whitelist mode is enabled by default, making it suitable for self-hosting.

---

## 🎯 Features

- **Server dashboard**: View online count, CPU / memory / disk usage, traffic, realtime network speed, and system information.
- **Add servers from Telegram**: Supports single-server add, batch import, edit, delete, and SSH connectivity tests.
- **Flexible SSH authentication**: Supports inherited default keys, per-server keys, existing key paths, uploaded / pasted private keys, and password login.
- **Common test shortcuts**: Supports IP quality, NodeQuality, streaming unlock checks, NextTrace, GB5, and more.
- **IP / domain tools**: Supports IPPure official images and bgp.tools BGP route images.
- **Safe defaults**: Whitelist mode is required; GUKO focuses on common tests and does not provide a general remote command execution feature.
- **Docker-friendly deployment**: Includes Docker Compose, Makefile, and initialization script.

---

## 🚀 Quick Start

Prepare first:

1. Create a Bot via [@BotFather](https://t.me/BotFather) and get `BOT_TOKEN`.
2. Use [@userinfobot](https://t.me/userinfobot) or [@RawDataBot](https://t.me/RawDataBot) to get your numeric Telegram user ID.

Two deployment methods are available. **Docker Compose is recommended**.

### Method 1: Docker Compose (recommended, no git clone)

```bash
mkdir -p guko/keys guko/media guko/tmp
cd guko

curl -Lo docker-compose.yml https://raw.githubusercontent.com/shuijiao1/GUKO/main/docker-compose.example.yml

cat > .env <<'EOF'
BOT_TOKEN=replace-me
ALLOWED_USERS=123456789
ADMIN_USERS=123456789
DATA_DIR=/data
GUKO_INV=/data/servers.json
MEDIA_DIR=/data/media
TMP_DIR=/data/tmp
KEYS_DIR=/data/keys
GUKO_DEFAULT_USER=root
GUKO_DEFAULT_PORT=22
GUKO_DEFAULT_KEY=/data/keys/id_ed25519
ENABLE_BGP=true
ENABLE_IPPURE=true
ENABLE_IPQ=true
ENABLE_NQ=true
ENABLE_GB5=true
ENABLE_STREAM=true
ENABLE_NEXTTRACE=true
ALLOW_INSECURE_STARTUP=false
EOF

cat > servers.json <<'EOF'
{
  "defaults": {
    "ssh": {
      "user": "root",
      "port": 22,
      "key": "/data/keys/id_ed25519"
    }
  },
  "servers": []
}
EOF

nano .env
docker compose pull
docker compose up -d
docker compose logs -f
```

Only these values need to be changed in the minimal config first:

```env
BOT_TOKEN=replace-me
ALLOWED_USERS=123456789
ADMIN_USERS=123456789
```

After startup, send `/addserver` to the Bot to add your first server.

### Method 2: Source build (development)

```bash
git clone https://github.com/shuijiao1/GUKO.git
cd GUKO
cp .env.example .env
cp servers.example.json servers.json
mkdir -p keys media tmp
nano .env
docker build -f telegram-bot/Dockerfile -t guko:local .
docker run -d --name guko-bot --restart unless-stopped \
  --env-file .env \
  -v ./servers.json:/data/servers.json \
  -v ./keys:/data/keys \
  -v ./media:/data/media \
  -v ./tmp:/data/tmp \
  guko:local
docker logs -f guko-bot
```

---

## 💬 Usage

### Open dashboard

Send this to the Bot in a private chat:

```text
/start
```

The Bot will show the GUKO dashboard. Tap a server to view details.

### Add servers

Tap **➕ Add Server**, or send:

```text
/addserver
```

#### Add one server

Choose **Add single server**, then send:

```text
name IP [port] [user]
```

Examples:

```text
hk-01 203.0.113.10 22 root
jp-01 203.0.113.20:2222 debian
```

Then the Bot will ask for the login method:

- **Use default key / config**: Use `GUKO_DEFAULT_KEY` or `defaults.ssh.key` from `servers.json`.
- **Use existing key path**: Send a path such as `/data/keys/id_ed25519`.
- **Upload / paste a new private key**: Send SSH private key text, or upload a private key file. The Bot saves it to `/data/keys/`, sets permission to `600`, and tries an SSH login test.
- **Use password**: Send SSH password. The Bot saves the config and tries a login test.
- **Save only, skip test**: Only write the server record. You can add authentication later.

After adding, test with buttons or commands:

```text
/testssh hk-01
/testall
```

The server detail page also supports **Edit** and **Delete**. Delete requires confirmation and only removes local Bot configuration; it does not touch the remote machine.

#### Batch import

Choose **Batch import**. The Bot will ask:

1. Whether all servers use the same SSH port, or each line includes its own port.
2. Whether all servers use the same key, same password, per-line auth, or import without testing.

Common batch format:

```text
hk-01 203.0.113.10 root
jp-01 203.0.113.20 debian
sg-01 203.0.113.30 root
```

If choosing per-line ports:

```text
hk-01 203.0.113.10 22 root
jp-01 203.0.113.20 2222 debian
sg-01 203.0.113.30:53580 root
```

If choosing per-line auth:

```text
hk-01 203.0.113.10 22 root key:/data/keys/hk_ed25519
jp-01 203.0.113.20 2222 debian password:your-password
```

> Passwords / private keys sent through Telegram pass through Telegram cloud. Use a private Bot and restrict `ALLOWED_USERS`.

### Commands

- `/start` — Open GUKO dashboard
- `/list` — Show server list
- `/status` — Show overview status
- `/addserver` — Add / batch import servers
- `/testssh <name/IP/ID/alias>` — Test SSH for one server
- `/testall` — Batch test SSH
- `/exportconfig` — Export sanitized config
- `/info <name/IP/ID/alias>` — Show single-server details
- `/health` — Read-only health check
- `/jobs` — Show background jobs
- `/ip <IPv4 or domain>` — IPPure / BGP tools
- `/nexttrace <server> <target>` — Route tracing

---

## ⚙️ Configuration

`.env` example:

```env
BOT_TOKEN=replace-me
ALLOWED_USERS=123456789
ADMIN_USERS=123456789
DATA_DIR=/data
GUKO_INV=/data/servers.json
MEDIA_DIR=/data/media
TMP_DIR=/data/tmp
KEYS_DIR=/data/keys
GUKO_DEFAULT_USER=root
GUKO_DEFAULT_PORT=22
GUKO_DEFAULT_KEY=/data/keys/id_ed25519
ENABLE_BGP=true
ENABLE_IPPURE=true
ENABLE_IPQ=true
ENABLE_NQ=true
ENABLE_GB5=true
ENABLE_STREAM=true
ENABLE_NEXTTRACE=true
ALLOW_INSECURE_STARTUP=false
```

| Variable | Required | Default | Description |
|---|---:|---|---|
| `BOT_TOKEN` | Yes | - | Telegram Bot Token |
| `ALLOWED_USERS` | Yes | - | Allowed Telegram numeric user IDs, comma-separated |
| `ADMIN_USERS` | No | `ALLOWED_USERS` | Admin IDs; can add / delete servers and use high-risk features |
| `DATA_DIR` | No | `/data` | Container data directory |
| `GUKO_INV` | No | `/data/servers.json` | Server inventory path |
| `MEDIA_DIR` | No | `/data/media` | Image and report output directory |
| `TMP_DIR` | No | `/data/tmp` | Temporary directory |
| `KEYS_DIR` | No | `/data/keys` | SSH private key storage directory |
| `GUKO_DEFAULT_USER` | No | `root` | Default SSH user |
| `GUKO_DEFAULT_PORT` | No | `22` | Default SSH port |
| `GUKO_DEFAULT_KEY` | No | `/data/keys/id_ed25519` | Default SSH private key path |
| `ENABLE_BGP` | No | `true` | Enable BGP image feature |
| `ENABLE_IPPURE` | No | `true` | Enable IPPure image feature |
| `ENABLE_IPQ` | No | `true` | Enable IP quality feature |
| `ENABLE_NQ` | No | `true` | Enable NodeQuality feature |
| `ENABLE_GB5` | No | `true` | Enable GB5 feature |
| `ENABLE_STREAM` | No | `true` | Enable streaming unlock checks |
| `ENABLE_NEXTTRACE` | No | `true` | Enable NextTrace |
| `BGP_FETCH` | No | `/data/tools/bgp_fetch.py` | BGP image helper script path |
| `IPPURE_DOWNLOAD` | No | `/data/tools/download_ippure.js` | IPPure download script path |
| `ALLOW_INSECURE_STARTUP` | No | `false` | Skip security startup checks for development / migration |

> `BOT_TOKEN` and `ALLOWED_USERS` are required. Do not commit real `.env` files.

---

## 🛠 Operations

Persistent data lives in the installation directory:

```text
GUKO/
├── docker-compose.example.yml
├── .env
├── servers.json       # private server inventory
├── keys/              # SSH private keys
├── media/             # report images / output files
└── tmp/               # temporary files
```

Common commands:

```bash
cd <install-dir>
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

Upgrade:

```bash
cd <install-dir>
git pull
docker compose pull
docker compose up -d
```

Or use Makefile:

```bash
make up
make logs
make restart
make down
```

---

## 🧾 Batch add via config file

It is recommended to put shared defaults under `defaults.ssh`, and only override differences per server:

```json
{
  "defaults": {
    "ssh": {
      "user": "root",
      "port": 22,
      "key": "~/.ssh/id_ed25519"
    }
  },
  "servers": [
    {
      "name": "hk-01",
      "host": "203.0.113.10"
    },
    {
      "name": "jp-01",
      "host": "203.0.113.20",
      "ssh": {
        "user": "debian",
        "port": 2222,
        "key": "~/.ssh/jp_ed25519"
      }
    },
    {
      "name": "sg-password",
      "host": "203.0.113.30",
      "ssh": {
        "auth": "password",
        "password": "change-me"
      }
    }
  ]
}
```

Legacy format is still supported:

```json
{
  "name": "legacy",
  "host": "203.0.113.40",
  "user": "root",
  "port": 53580,
  "key": "/data/keys/server_key"
}
```

Test after batch import:

```bash
./guko.py list
./guko.py run hk-01 'hostname'
```

You can also export sanitized config from the Bot:

```text
/exportconfig
```

---

## 🧩 Optional tool preparation

- IP quality / NodeQuality / streaming / NextTrace / GB5 are executed on the target server through SSH. Dependencies are installed or downloaded by the upstream scripts on first run.
- BGP and IPPure images are generated locally inside the Bot container. The default Dockerfile includes Node.js, Playwright, Chromium, and rendering dependencies.
- If scripts referenced by `BGP_FETCH` or `IPPURE_DOWNLOAD` do not exist, the Bot will try to download helper scripts automatically and show a clear error on failure.
- You can disable selected buttons with environment variables: `ENABLE_BGP=false`, `ENABLE_IPPURE=false`, `ENABLE_IPQ=false`, `ENABLE_NQ=false`, `ENABLE_GB5=false`, `ENABLE_STREAM=false`, `ENABLE_NEXTTRACE=false`.

---

## 🧩 Source run (development)

```bash
git clone https://github.com/shuijiao1/GUKO.git
cd GUKO
python3 -m venv .venv
. .venv/bin/activate
pip install -r telegram-bot/requirements.txt
cp .env.example .env
cp servers.example.json servers.json
nano .env
python3 telegram-bot/bot.py
```

Syntax check:

```bash
make check
```

---

## 🔐 Privacy

- The repository does not contain any Bot Token, real user ID, server password, or private key.
- `.env`, `servers.json`, `keys/`, `media/`, and `tmp/` are ignored by Git. Do not commit real configuration.
- Whitelist mode is enabled by default. The Bot refuses to start when allowed users are not configured.
- IPPure, bgp.tools, NodeQuality, streaming checks, and similar features will access corresponding third-party services.
- Deleting a server only removes local Bot configuration. It does not delete or reinstall the remote machine.

## License

MIT

---

## ⚙️ Versioning and Releases

- Current version: `v0.1.3`
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)
- Docker images are published as `latest`, `v0.1.3`, and commit sha tags
- GitHub Releases are generated from `CHANGELOG.md`
- Maintainers can publish a new version with:

```bash
./release.sh <version> "release notes"
```
