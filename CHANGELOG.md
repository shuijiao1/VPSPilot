# Changelog

## [0.1.12] - 2026-06-02

- 新增 VLESS 与 Snell 管理入口；VLESS 安装可选择纯 VLESS 或 Vision + Reality。

## [0.1.11] - 2026-06-02

- 回退 Check.Place 报告渲染为原 Python/Pillow 终端网格方案，移除实验性的浏览器截图渲染。

## [0.1.10] - 2026-06-01

- 移除任务队列模式，点击测试后直接启动后台任务；同一服务器同类任务运行中时直接提示。

## [0.1.9] - 2026-05-28

- 新增 SS-Rust 与 AnyTLS 管理入口，支持安装/更新检测与查看配置。

## [0.1.8] - 2026-05-22

- 移除老用户 Compose project 改名迁移脚本，避免把一次性改名流程误认为日常升级步骤。
- 保留新版 Compose 的 `name: guko`，新部署会直接显示为 `guko`；老部署正常更新镜像不受影响。

## [0.1.7] - 2026-05-22

- 增加老用户 Compose project name 迁移脚本，稳定处理从目录名 project 迁移到 `guko` 的容器名冲突。
- README 增加老部署升级说明，明确保留宿主机挂载数据。

## [0.1.6] - 2026-05-22

- Docker Compose 示例显式设置项目名为 `guko`，避免部署目录名影响管理面板显示。
- 更新 README 部署说明，说明 Compose project name 会固定显示为 `guko`。

## [0.1.5] - 2026-05-20

- 修正文档中的安全表述，补充 Issue 模板，并完善 Release 附件。

## [0.1.4] - 2026-05-20

- 加入基础 CI、防泄密检查、Actions Node24 兼容设置和本地项目健康检查。

## [0.1.3] - 2026-05-20

- 补齐中英双语 README、统一部署说明，并加入本地 release helper。

All notable changes to this project are documented here.

## [0.1.2] - 2026-05-19

- 修复 Release workflow YAML，确保 tag 发布会自动用 CHANGELOG 生成 Release notes。

## [0.1.1] - 2026-05-19

- 维护版本发布流程：新增 CHANGELOG 与 Release Drafter；Docker 发布保留 latest、版本号和 sha 标签。
