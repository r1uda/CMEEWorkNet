# CMEE事务组

固定成员使用的事务工作台，提供手机和电脑布局。正式服务地址：https://cmee-work.top 。本仓库用于协作开发；提交代码后需单独部署，正式网站才会更新。

## 本机运行

需要 Python 3.11 或更高版本，无第三方 Python 依赖、无前端打包步骤。

```powershell
python -m app.server --demo
```

打开 http://127.0.0.1:8765 ，选择演示身份。演示模式使用独立示例数据库，不发送真实邮件、不调用 AI。

## 功能

- 邮箱验证码登录、固定成员与独立权限。
- 值班排班、Excel 导入、个人换班和公开求换班。
- 事务登记、草稿恢复、编号编辑、一二节与七八节预填日常卫生。
- 正常值班每次计 2 小时，无事务也计时；值班外事项单独记录，不计工时。
- 管理员成员统计、历史事务 Excel 导入导出、修改历史与审计。
- 事务交接、留言、待办进度、可见通知自动已读。
- 换班与交接邮件通知、班后 10 分钟漏登记提醒、次日 9 点汇总。
- DeepSeek 工作助手，按登录身份与权限查询记录、提出需确认的操作。
- 公开换班回复支持选择班次交换、班次待定、仅代班；确认后更新排班。
- Quill 安卓应用介绍与安装包下载入口。

## 配置

复制 `.env.example` 为 `.env`，仅在本机或服务器填写服务凭证。正式初始化可使用：

```powershell
python -m app.manage init --admin "固定成员完整姓名" --email "本人真实邮箱"
python -m app.server
```

正式运行需要 SMTP、HTTPS 及相应的服务配置。默认数据库为 `data/workbench.sqlite3`；演示数据库为 `data/demo.sqlite3`。请勿将演示库覆盖到正式服务器。新增服务环境使用自己的配置，不复制正式密钥或成员数据。

本仓库不包含数据库、凭证、真实成员邮箱、原始 Excel、部署账号记录或 APK。Quill APK 可单独放在 `data/quill/quill-0.1.5-android-arm64.apk`；缺失时下载接口返回 404，其余页面可运行。

## 检查

```powershell
python -m unittest discover -s tests
node --check app/static/app.js
node tests/frontend_smoke.cjs
```

Python 用例使用临时数据库和模拟邮件/模型。Node 只用于可选前端检查。每次修改运行相关检查，界面修改还应在本机浏览器检查手机与电脑布局。

## 协作与部署

详见 [协作开发](docs/协作开发.md)。当前数据库结构版本为 schema 11；迁移脚本位于 `app/migrations/`。私有仓库包含固定成员姓名及工作职责，请保持私有。

Linux 服务模板在 `deploy/`，Docker 启动模板为 `compose.yaml`。部署前备份数据库和代码、检查迁移并核对服务状态；`deploy/install.sh` 仅供首次安装。GitHub 当前没有自动部署工作流，合并代码不会自动变更正式网站。
