# Windows 电脑部署教程

本教程介绍如何在 Windows 电脑上运行 `douyin-auto-fire`。

Windows 部署适合：

- 想先在自己的电脑上测试项目；
- 不想使用 GitHub Actions；
- 希望直接扫码登录生成本地登录状态；
- 希望配合 Windows 任务计划程序每天自动运行。

> Windows 电脑需要在任务执行时保持开机并联网。如果电脑关机，任务无法正常运行。

---

## 1. 安装 Python

项目建议使用 Python 3.11 或更高版本。

下载地址：

**https://www.python.org/downloads/**

安装时建议勾选：

```text
Add Python to PATH
```

安装完成后打开 PowerShell：

```powershell
python --version
```

如果系统同时安装了多个 Python，也可以执行：

```powershell
py --version
```

---

## 2. 安装 Git

如果电脑还没有 Git，可以安装：

**https://git-scm.com/download/win**

安装后检查：

```powershell
git --version
```

如果不想安装 Git，也可以直接在 GitHub 页面点击 **Code → Download ZIP** 下载项目并解压。

---

## 3. 下载项目

使用 Git：

```powershell
git clone https://github.com/unmev/douyin-auto-fire.git
cd douyin-auto-fire
```

建议把项目放在一个固定路径，例如：

```text
C:\douyin-auto-fire
```

如果以后要使用 Windows 任务计划程序，尽量不要频繁移动这个目录。

---

## 4. 创建 Python 虚拟环境

在项目目录打开 PowerShell：

```powershell
py -3.11 -m venv .venv
```

如果电脑只有一个 Python，也可以使用：

```powershell
python -m venv .venv
```

后续教程直接调用虚拟环境中的 Python，不需要手动执行激活脚本。

---

## 5. 安装项目依赖

执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## 6. 安装 Chromium

执行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Playwright 会自动下载项目运行需要的 Chromium 浏览器。

---

## 7. 配置发送内容

复制示例配置：

```powershell
Copy-Item config.example.json config.json
```

然后打开：

```powershell
notepad config.json
```

也可以直接使用在线配置生成器：

**https://douyin-config.pages.dev/**

将生成的 JSON 完整保存到：

```text
config.json
```

第一次建议只配置：

```text
1 个好友 + 1 条文字消息
```

例如：

```json
{
  "friends": ["好友昵称"],
  "messages": [
    {"type": "text", "value": "续火花 ✨"}
  ],
  "send_interval_seconds": {
    "min": 3,
    "max": 8
  },
  "prevent_duplicates": false
}
```

---

# 8. 登录抖音

Windows 推荐直接使用项目自带的扫码登录脚本。

执行：

```powershell
.\.venv\Scripts\python.exe scripts\login.py
```

程序会自动打开 Chromium。

按照浏览器中的提示完成抖音扫码登录。

登录完成并看到抖音首页后，回到 PowerShell，根据提示按一次 **Enter**。

程序会生成：

```text
storage-state.json
```

这个文件保存当前登录状态。

> ⚠️ `storage-state.json` 相当于账号登录凭证，不要发送给其他人，也不要上传到公开仓库。

项目的 `.gitignore` 已经忽略 `storage-state.json`。

---

## 9. 第一次运行 Dry Run

先不要真实发送消息。

执行：

```powershell
.\.venv\Scripts\python.exe run.py --dry-run
```

Dry Run 会检查：

- 登录状态是否有效；
- 是否能够正常进入抖音私信页；
- 是否能够找到目标好友；
- 配置是否正确。

但不会真正发送消息。

如果运行成功，再进入下一步。

---

## 10. 测试真实发送

执行：

```powershell
.\.venv\Scripts\python.exe run.py
```

这次会真正向配置中的好友发送消息。

第一次建议仍然只保留一个测试好友。

确认：

- 好友没有发错；
- 文字内容正确；
- 程序可以正常结束。

确认无误后再增加其他好友或消息。

---

## 11. 切换为无头模式

第一次扫码登录和排查问题时，可以让浏览器正常显示。

日常自动运行建议使用 Headless 模式。

在项目根目录新建：

```text
.env
```

可以执行：

```powershell
notepad .env
```

写入：

```env
HEADLESS=true
```

如需固定出口代理，可在 `.env` 中继续加入：

```env
DOUYIN_PROXY_SERVER=http://proxy.example.com:3128
DOUYIN_PROXY_USERNAME=proxy-user
DOUYIN_PROXY_PASSWORD=proxy-password
```

HTTP 支持可选用户名和密码，并可通过 CONNECT 访问 HTTPS 页面；SOCKS5 仅支持无认证。未配置 `DOUYIN_PROXY_SERVER` 时仍然使用原有直连逻辑。

保存以后再次运行：

```powershell
.\.venv\Scripts\python.exe run.py --dry-run
```

这一次 Chromium 会在后台运行，不会弹出浏览器窗口。

---

## 12. 使用 Cookie 登录（可选）

如果不想运行扫码登录脚本，也可以和 GitHub Actions 一样使用 Cookie。

先在正常浏览器中登录抖音，再使用 Cookie-Editor 导出完整 JSON。

详细步骤：

👉 [GitHub Actions 教程中的 Cookie 获取步骤](github-actions.md#3-获取抖音-cookie)

然后打开 `.env`：

```powershell
notepad .env
```

将 Cookie 压缩为单行 JSON 后写入：

```env
DOUYIN_COOKIE=[{"name":"xxx","value":"xxx","domain":".douyin.com","path":"/"}]
HEADLESS=true
```

如果项目目录里同时存在有效的 `storage-state.json`，程序会优先使用 `storage-state.json`。

如果想完全改用 Cookie，可以先删除旧的：

```powershell
Remove-Item storage-state.json
```

> Cookie 和 `storage-state.json` 都属于登录凭证，不要提交到 GitHub。

---

## 13. 配置钉钉通知（可选）

如果希望每次运行后收到钉钉通知，在 `.env` 中继续加入：

```env
DINGTALK_WEBHOOK=你的钉钉Webhook
DINGTALK_SECRET=你的钉钉Secret
```

两个参数必须同时填写。

如果需要 Telegram 通知，可以在 `.env` 中加入：

```env
TELEGRAM_BOT_TOKEN=123456:你的BotToken
TELEGRAM_CHAT_ID=-1001234567890
```

这两个参数必须同时填写。钉钉和 Telegram 可以同时启用，通知失败不会影响抖音任务结果。

例如：

```env
HEADLESS=true
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxxx
DINGTALK_SECRET=SECxxxx
```

---

# 14. 配置 Windows 每天自动运行

如果希望电脑每天自动运行，可以使用 Windows 自带的 **任务计划程序**。

按 `Win + R`，输入：

```text
taskschd.msc
```

打开任务计划程序。

点击：

```text
创建基本任务
```

任务名称可以填写：

```text
Douyin Auto Fire
```

触发器选择：

```text
每天
```

然后设置希望运行的时间。

---

## 15. 配置任务操作

操作选择：

```text
启动程序
```

假设项目路径是：

```text
C:\douyin-auto-fire
```

### 程序或脚本

填写：

```text
C:\douyin-auto-fire\.venv\Scripts\python.exe
```

### 添加参数

填写：

```text
run.py
```

### 起始于

填写：

```text
C:\douyin-auto-fire
```

> **“起始于”一定要填写项目目录。** 否则程序可能找不到 `config.json`、`.env` 或 `storage-state.json`。

保存任务即可。

---

## 16. 测试任务计划程序

找到刚刚创建的任务：

```text
Douyin Auto Fire
```

右键选择：

```text
运行
```

然后检查项目目录：

```text
artifacts\run.log
```

也可以在 PowerShell 中执行：

```powershell
Get-Content .\artifacts\run.log -Tail 100
```

如果日志正常，说明任务计划程序配置成功。

---

## 17. 查看运行结果

项目运行后的诊断文件位于：

```text
artifacts\
```

可能包含：

```text
run.log
result.json
history.json
screenshots\
traces\
```

查看最近日志：

```powershell
Get-Content .\artifacts\run.log -Tail 100
```

如果需要持续查看日志：

```powershell
Get-Content .\artifacts\run.log -Wait
```

> 截图和日志可能包含账号或聊天相关信息，不建议直接公开。

---

## 18. 登录失效怎么办？

如果日志提示：

```text
登录状态已失效
安全验证
需要重新登录
```

最简单的方法是重新运行：

```powershell
.\.venv\Scripts\python.exe scripts\login.py
```

重新扫码登录。

新的 `storage-state.json` 会覆盖旧文件。

然后先测试：

```powershell
.\.venv\Scripts\python.exe run.py --dry-run
```

Dry Run 成功后即可继续自动运行。

---

## 19. 更新项目

如果是 Git 克隆的项目：

```powershell
cd C:\douyin-auto-fire
git pull
```

然后更新 Python 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果 Playwright 版本有变化，也可以重新执行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

自己的这些文件不会被正常的 `git pull` 提交到仓库：

```text
.env
config.json
storage-state.json
artifacts\
```

---

## 常用命令

```powershell
# 扫码登录
.\.venv\Scripts\python.exe scripts\login.py

# Dry Run，只检查不发送
.\.venv\Scripts\python.exe run.py --dry-run

# 真实发送
.\.venv\Scripts\python.exe run.py

# 查看最近 100 行日志
Get-Content .\artifacts\run.log -Tail 100

# 更新项目
git pull
```

---

## Windows 部署推荐流程

```text
安装 Python / Git
        ↓
下载项目
        ↓
创建 .venv
        ↓
安装依赖和 Chromium
        ↓
创建 config.json
        ↓
运行 scripts/login.py 扫码登录
        ↓
运行 --dry-run
        ↓
测试真实发送
        ↓
.env 设置 HEADLESS=true
        ↓
配置 Windows 任务计划程序
        ↓
每天自动运行
```

---

## 其他部署方式

- 👉 [GitHub Actions 部署](github-actions.md)
- 👉 [云服务器部署](server.md)
- 👉 [返回项目主页](../README.md)
