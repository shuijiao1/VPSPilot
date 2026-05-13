# 安全说明

JiaoOps 是自部署服务器管理 Bot。它能保存 SSH 凭据，并可能执行远程命令，请按下面规则部署。

## 必须自建 Bot

- 不要使用别人提供的公共 Bot。
- 请在 Telegram BotFather 创建自己的 Bot，并把 token 写入 `.env` 的 `BOT_TOKEN`。
- 不要把 Bot Token 提交到 GitHub、截图或发到群聊。

## 必须白名单

- `ALLOWED_USERS` 不能为空；为空时程序会拒绝启动。
- 非白名单用户只能看到“无权限”和自己的 Telegram ID。
- 建议只允许自己的 Telegram 用户 ID。
- 不建议把 Bot 加入群聊；如果一定要加群，也不要把群成员都加入白名单。

## 管理员权限

- `ADMIN_USERS` 为空时默认等于 `ALLOWED_USERS`。
- 管理员可以添加/修改服务器、使用高危功能。
- 普通白名单用户只能查看和运行允许的只读功能。

## SSH 密钥和密码

- 推荐使用 SSH 密钥，不推荐密码。
- 通过 Telegram 发送私钥/密码会经过 Telegram 云端；如果介意，请手动把密钥放到服务器 `./keys` 目录，然后在配置里写路径。
- Bot 保存上传的私钥时会放到 `/data/keys/` 并设置 `0600` 权限。
- Bot 不会在回复里回显私钥或密码。

## 远程命令

- `/run` 默认关闭。
- 只有设置 `ENABLE_REMOTE_RUN=true` 且用户在 `ADMIN_USERS` 内时才可使用。
- 这等价于远程 SSH 执行命令，风险很高，请谨慎开启。

## 开源/备份

不要提交或公开：

- `.env`
- `servers.json`
- `keys/`
- `docker-compose.yml` 中的真实环境变量
- 任何 Bot Token、SSH 密码、私钥、真实服务器 IP 清单

仓库只应提交：

- `.env.example`
- `servers.example.json`
- `docker-compose.example.yml`
