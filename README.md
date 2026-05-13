# 饺管家 / JiaoOps

一个轻量 VPS / 服务器管理 Telegram Bot：服务器列表、状态面板、SSH 远程执行、常用测试脚本入口。

## 快速部署

```bash
git clone <你的仓库地址> JiaoOps
cd JiaoOps
./install.sh
# 编辑 .env：填 BOT_TOKEN、ALLOWED_USERS / ADMIN_USERS
docker compose -f docker-compose.example.yml up -d --build

# 或者用 Makefile
make init
make up
```

部署步骤：

1. 去 Telegram BotFather 创建自己的 Bot，拿到 `BOT_TOKEN`
2. 给 Bot 发 `/start`，从“无权限”提示里看到自己的 Telegram ID
3. 把 ID 写入 `.env` 的 `ALLOWED_USERS` 和 `ADMIN_USERS`
4. 启动容器
5. 在 Bot 里发送 `/addserver` 添加服务器

> JiaoOps 必须自部署，必须使用自己的 Telegram Bot，必须配置白名单。详见 `SECURITY.md`。

## 文件

- `servers.json`：服务器清单，私有部署时自己维护
- `servers.example.json`：可公开的示例配置
- `auth.py`：统一 SSH 鉴权解析，支持默认值继承、每台机器独立端口、独立密钥、密码登录
- `jiaoops.py`：命令行巡检/远程命令工具
- `telegram-bot/bot.py`：Telegram Bot 主程序
- `SECURITY.md`：安全部署说明
- `Makefile` / `install.sh`：初始化、启动、检查辅助脚本

## 用法

```bash
./server-manager/jiaoops.py list
./server-manager/jiaoops.py health
./server-manager/jiaoops.py run <name-or-ip> 'systemctl status nginx --no-pager'
```

## Telegram Bot 添加服务器

打开 Bot 后点 **➕ 添加服务器**，或者发送：

```text
/addserver
```

### 单台添加

选择 **添加单台**，按提示发送：

```text
名称 IP [端口] [用户]
```

示例：

```text
hk-01 203.0.113.10 22 root
jp-01 203.0.113.20:2222 debian
```

然后 Bot 会询问登录方式：

- **沿用默认密钥/配置**：使用 `JIAOOPS_DEFAULT_KEY` 或 `servers.json` 里的 `defaults.ssh.key`，适合新服务器继续用以前那把密钥。
- **使用已有密钥路径**：发送 `/data/keys/id_ed25519` 这类路径，不需要重新上传密钥。
- **上传/粘贴新私钥**：发送 SSH 私钥文本，或直接上传私钥文件；Bot 会保存到 `/data/keys/`，权限设为 `600`，并尝试 SSH 登录测试。
- **使用密码**：发送 SSH 密码；Bot 会保存配置并尝试登录测试。
- **先只保存，不测试登录**：只写入服务器清单，后续再补认证。

添加后可用按钮或命令测试：

```text
/testssh hk-01
/testall
```

服务器详情页也支持 **编辑** 和 **删除**；删除需要二次确认，只会删除本地配置，不会操作远端机器。

### 批量导入

选择 **批量导入** 后，Bot 会先问：

1. 是否全部使用同一个 SSH 端口，还是每台自己写端口
2. 是否全部使用同一把密钥、同一个密码、每台自己写认证，还是先只导入不测试

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

> 注意：Telegram 里发送密码/私钥会经过 Telegram 云端。自用没问题；公开项目建议提醒用户优先用私有 Bot，并限制 `ALLOWED_USERS`。

## 直接写配置文件批量添加

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

### 环境变量默认值

如果 `servers.json` 没写默认 SSH 信息，会使用：

- `JIAOOPS_DEFAULT_USER`，默认 `root`
- `JIAOOPS_DEFAULT_PORT`，默认 `22`
- `JIAOOPS_DEFAULT_KEY`，默认 `/data/keys/id_ed25519`

密码登录依赖 `sshpass`，Dockerfile 已包含。

批量添加后可以测试：

```bash
./server-manager/jiaoops.py list
./server-manager/jiaoops.py run hk-01 'hostname'
```

Bot 内还可以导出脱敏配置：

```text
/exportconfig
```

## 可选工具自动准备

- IP质量 / NodeQuality / 流媒体 / NextTrace / GB5 都是在目标服务器通过 SSH 执行对应公开脚本；首次运行会按脚本自身逻辑安装或下载依赖。
- BGP 图和 IPPure 图是在 Bot 容器本地生成；默认 Dockerfile 已包含 Node.js、Playwright、Chromium 和渲染依赖。
- 如果 `BGP_FETCH` 或 `IPPURE_DOWNLOAD` 指向的脚本不存在，Bot 会尝试自动下载工具脚本；失败时会给出明确错误。
- 可以通过环境变量关闭某些按钮：`ENABLE_BGP=false`、`ENABLE_IPPURE=false`、`ENABLE_IPQ=false`、`ENABLE_NQ=false`、`ENABLE_GB5=false`、`ENABLE_STREAM=false`、`ENABLE_NEXTTRACE=false`。

> 开源时不要提交真实 `servers.json`、Bot Token、密码或私钥。提交 `servers.example.json` 即可。

## 原则

- 只读巡检可直接跑
- 更新、改配置、重启服务前先验证
- 删除数据、重装、停服务、防火墙改动前先确认
