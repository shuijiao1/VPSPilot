# GUKO

[![Docker Image](https://img.shields.io/badge/ghcr.io-guko-blue?logo=docker)](https://github.com/shuijiao1/GUKO/pkgs/container/guko)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**中文** | [English](README.en.md)

**轻量 VPS / 服务器管理 Telegram Bot：服务器状态面板、SSH 登录管理、常用测试脚本入口**

> 私聊打开 Bot 就能查看服务器列表、状态详情、流量与资源占用；支持在 Telegram 内添加服务器、测试 SSH、运行 IP 质量 / NodeQuality / 流媒体 / NextTrace / GB5 等常用检测。  
> 默认白名单模式，适合自托管。

---

## 🎯 核心特性

- **服务器状态面板**：展示在线数量、CPU / 内存 / 硬盘、流量、实时网速、系统信息等。
- **Telegram 内添加服务器**：支持单台添加、批量导入、编辑、删除和 SSH 连通性测试。
- **灵活 SSH 鉴权**：支持默认密钥继承、每台独立密钥、已有密钥路径、上传 / 粘贴私钥、密码登录。
- **常用测试入口**：支持 IP 质量、NodeQuality、流媒体解锁、NextTrace、GB5 等任务。
- **IP / 域名工具**：支持 IPPure 官方图片与 bgp.tools BGP 路由图。
- **适合 Docker 部署**：提供 Docker Compose、Makefile 和初始化脚本。

---

## 🚀 快速开始

先准备：

1. 到 [@BotFather](https://t.me/BotFather) 创建 Bot，拿到 `BOT_TOKEN`。
2. 用 [@userinfobot](https://t.me/userinfobot) 或 [@RawDataBot](https://t.me/RawDataBot) 获取你的 Telegram 数字用户 ID。

提供 2 种部署方式，**推荐 Docker Compose**。

### 方式一：Docker Compose（推荐，无需 git clone）

```bash
mkdir -p guko/keys guko/media guko/results guko/tmp
cd guko

curl -Lo docker-compose.yml https://github.com/shuijiao1/GUKO/releases/latest/download/docker-compose.example.yml

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

`docker-compose.yml` 已显式设置 `name: guko`，因此在 Docker / DockUP 等管理面板里会显示为 `guko`，不会因为部署目录不同变成随机目录名。

最小配置里只需要先改：

```env
BOT_TOKEN=replace-me
ALLOWED_USERS=123456789
ADMIN_USERS=123456789
```

启动后在 Bot 里发送 `/addserver` 添加第一台服务器。


### 方式二：源码构建（开发用）

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

## 💬 使用方式

### 打开面板

私聊 Bot 发送：

```text
/start
```

Bot 会显示 GUKO 总览面板，可以点服务器查看详情。

### 添加服务器

点击 **➕ 添加服务器**，或者发送：

```text
/addserver
```

#### 单台添加

选择 **添加单台**，按提示发送：

```text
名称 IP [端口] [用户]
```

示例：

```text
hk-01 203.0.113.10 22 root
jp-01 203.0.113.20:2222 debian
```

然后 Bot 会询问登录方式，支持密钥路径、上传 / 粘贴私钥、密码或先保存后测试。

添加后可用按钮或命令测试：

```text
/testssh hk-01
/testall
```

服务器详情页也支持 **编辑** 和 **删除**；删除需要二次确认，只会删除本地配置，不会操作远端机器。

#### 批量导入

选择 **批量导入** 后，Bot 会先问：

1. 是否全部使用同一个 SSH 端口，还是每台自己写端口。
2. 是否全部使用同一把密钥、同一个密码、每台自己写认证，还是先只导入不测试。

常用批量格式：

```text
hk-01 203.0.113.10 root
jp-01 203.0.113.20 debian
sg-01 203.0.113.30 root
```

如果选择“每台自己写端口”：

```text
hk-01 203.0.113.10 22 root
jp-01 203.0.113.20 2222 debian
sg-01 203.0.113.30:53580 root
```

如果选择“每台自己写认证”：

```text
hk-01 203.0.113.10 22 root key:/data/keys/hk_ed25519
jp-01 203.0.113.20 2222 debian password:your-password
```

> Telegram 里发送密码 / 私钥会经过 Telegram 云端。建议使用私有 Bot，并限制 `ALLOWED_USERS`。

### 命令

- `/start` — 打开 GUKO 面板
- `/list` — 查看服务器列表
- `/status` — 查看总览状态
- `/addserver` — 添加 / 批量导入服务器
- `/testssh <名字/IP/ID/别名>` — 测试单台服务器 SSH
- `/testall` — 批量测试 SSH
- `/exportconfig` — 导出脱敏配置
- `/info <名字/IP/ID/别名>` — 查看单台详情
- `/health` — 只读巡检
- `/jobs` — 查看后台任务
- `/ip <IPv4 或域名>` — IPPure / BGP 工具
- `/nexttrace <服务器> <目标>` — 路由追踪

---

## ⚙️ 配置说明

`.env` 示例：

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

| 变量 | 是否必填 | 默认值 | 说明 |
|---|---:|---|---|
| `BOT_TOKEN` | 是 | - | Telegram Bot Token |
| `ALLOWED_USERS` | 是 | - | 允许使用 Bot 的 Telegram 数字 ID，多个用英文逗号分隔 |
| `ADMIN_USERS` | 否 | `ALLOWED_USERS` | 管理员 ID，能添加 / 删除服务器、执行高危功能 |
| `DATA_DIR` | 否 | `/data` | 容器内数据目录 |
| `GUKO_INV` | 否 | `/data/servers.json` | 服务器清单路径 |
| `MEDIA_DIR` | 否 | `/data/media` | 图片和报告输出目录 |
| `TMP_DIR` | 否 | `/data/tmp` | 临时文件目录 |
| `KEYS_DIR` | 否 | `/data/keys` | SSH 私钥保存目录 |
| `GUKO_DEFAULT_USER` | 否 | `root` | 默认 SSH 用户 |
| `GUKO_DEFAULT_PORT` | 否 | `22` | 默认 SSH 端口 |
| `GUKO_DEFAULT_KEY` | 否 | `/data/keys/id_ed25519` | 默认 SSH 私钥路径 |
| `ENABLE_BGP` | 否 | `true` | 是否启用 BGP 图功能 |
| `ENABLE_IPPURE` | 否 | `true` | 是否启用 IPPure 图功能 |
| `ENABLE_IPQ` | 否 | `true` | 是否启用 IP 质量功能 |
| `ENABLE_NQ` | 否 | `true` | 是否启用 NodeQuality 功能 |
| `ENABLE_GB5` | 否 | `true` | 是否启用 GB5 功能 |
| `ENABLE_STREAM` | 否 | `true` | 是否启用流媒体检测 |
| `ENABLE_NEXTTRACE` | 否 | `true` | 是否启用 NextTrace |
| `BGP_FETCH` | 否 | `/data/tools/bgp_fetch.py` | BGP 图片工具脚本路径 |
| `IPPURE_DOWNLOAD` | 否 | `/data/tools/download_ippure.js` | IPPure 下载脚本路径 |
| `ALLOW_INSECURE_STARTUP` | 否 | `false` | 开发 / 迁移时跳过安全启动检查 |

> `BOT_TOKEN` 和 `ALLOWED_USERS` 必须填写；不要把真实 `.env` 提交到仓库。

---

## 🛠 运维

所有持久化数据在安装目录下：

```text
GUKO/
├── docker-compose.example.yml
├── .env
├── servers.json       # 私有服务器清单
├── keys/              # SSH 私钥
├── media/             # 报告图片 / 输出文件
└── tmp/               # 临时文件
```

常用命令：

```bash
cd <安装目录>
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

升级：

```bash
cd <安装目录>
git pull
docker compose pull
docker compose up -d
```

也可以使用 Makefile：

```bash
make up
make logs
make restart
make down
```

---

## 🧾 直接写配置文件批量添加

推荐在 `defaults.ssh` 里写公共默认值，每台服务器只覆盖不同的部分：

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

兼容旧格式，下面这种仍然可用：

```json
{
  "name": "legacy",
  "host": "203.0.113.40",
  "user": "root",
  "port": 53580,
  "key": "/data/keys/server_key"
}
```

批量添加后可以测试：

```bash
./guko.py list
./guko.py run hk-01 'hostname'
```

Bot 内还可以导出脱敏配置：

```text
/exportconfig
```

---

## 🧩 可选工具

GUKO 支持按需启用 IP 质量、NodeQuality、流媒体、NextTrace、GB5、BGP 图和 IPPure 图等功能。相关按钮可以通过环境变量关闭。

---

---

## 🧩 源码运行（开发用）

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

语法检查：

```bash
make check
```

---

## 🔐 隐私说明

- 仓库不包含任何 Bot Token、真实用户 ID、服务器密码或私钥。
- `.env`、`servers.json`、`keys/`、`media/`、`tmp/` 已加入 `.gitignore`，不要提交真实配置。
- 默认白名单模式，未配置允许用户时会拒绝启动。
- 使用 IPPure、bgp.tools、NodeQuality、流媒体检测等功能时，会访问对应第三方服务。
- 删除服务器只会删除 Bot 本地配置，不会删除或重装远端机器。

## License

MIT
