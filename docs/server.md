# 云服务器部署教程

本教程介绍如何将 `douyin-auto-fire` 部署到 Linux 云服务器，并使用 `systemd` 每天自动运行。

推荐系统：

- Ubuntu 22.04 / 24.04
- Debian 12 / 13

> Playwright 需要运行 Chromium。建议服务器至少准备约 2 GB 内存；低内存机器可以尝试增加 Swap。

服务器部署推荐使用 **Cookie + Headless Chromium + systemd Timer**。服务器不需要桌面环境。

---

## 1. 准备抖音 Cookie

先在自己的电脑浏览器登录抖音网页版：

**https://www.douyin.com/**

然后使用 Cookie-Editor 导出当前抖音 Cookie，格式选择 **JSON**。

获取 Cookie 的详细操作可以参考：

👉 [GitHub Actions 教程中的 Cookie 获取步骤](github-actions.md#3-获取抖音-cookie)

> ⚠️ Cookie 相当于账号登录凭证，请不要发送给其他人，也不要上传到公开仓库。

---

## 2. 安装基础环境

SSH 登录服务器后执行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
```

确认 Python 版本：

```bash
python3 --version
```

建议使用 Python 3.11 或更高版本。

---

## 3. 创建运行用户

为了避免长期使用 root 运行浏览器，创建一个专用用户：

```bash
sudo useradd --system \
  --home /opt/douyin-auto-sender \
  --shell /usr/sbin/nologin \
  douyin-sender
```

如果提示用户已经存在，可以忽略。

---

## 4. 下载项目

将项目克隆到 `/opt/douyin-auto-sender`：

```bash
sudo git clone https://github.com/unmev/douyin-auto-fire.git /opt/douyin-auto-sender
sudo chown -R douyin-sender:douyin-sender /opt/douyin-auto-sender
cd /opt/douyin-auto-sender
```

---

## 5. 创建 Python 虚拟环境

使用专用用户创建虚拟环境：

```bash
sudo -u douyin-sender -H python3 -m venv /opt/douyin-auto-sender/.venv
```

安装项目依赖：

```bash
sudo -u douyin-sender -H /opt/douyin-auto-sender/.venv/bin/python -m pip install --upgrade pip
sudo -u douyin-sender -H /opt/douyin-auto-sender/.venv/bin/pip install -r /opt/douyin-auto-sender/requirements.txt
```

---

## 6. 安装 Chromium

先安装 Chromium 所需的系统依赖：

```bash
sudo /opt/douyin-auto-sender/.venv/bin/python -m playwright install-deps chromium
```

然后使用真正执行任务的 `douyin-sender` 用户下载 Chromium：

```bash
sudo -u douyin-sender -H /opt/douyin-auto-sender/.venv/bin/python -m playwright install chromium
```

> 这一步建议不要直接用 root 下载浏览器，否则后面 systemd 使用 `douyin-sender` 用户运行时可能找不到对应的 Playwright 浏览器文件。

---

## 7. 配置发送内容

进入项目目录：

```bash
cd /opt/douyin-auto-sender
```

复制示例配置：

```bash
sudo -u douyin-sender cp config.example.json config.json
```

编辑：

```bash
sudo -u douyin-sender nano config.json
```

也可以在电脑上使用配置生成器生成 JSON：

**https://douyin-config.pages.dev/**

然后将生成的内容完整复制到 `config.json`。

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

## 8. 保存 Cookie

为了避免把 Cookie 放进 Git 仓库，推荐单独保存到 `/etc`。

创建目录：

```bash
sudo mkdir -p /etc/douyin-auto-fire
```

创建 Cookie 文件：

```bash
sudo nano /etc/douyin-auto-fire/cookie.json
```

将 Cookie-Editor 导出的完整 JSON 粘贴进去并保存。

然后设置权限：

```bash
sudo chown root:douyin-sender /etc/douyin-auto-fire/cookie.json
sudo chmod 640 /etc/douyin-auto-fire/cookie.json
```

---

## 9. 创建 `.env`

创建项目环境变量文件：

```bash
sudo -u douyin-sender nano /opt/douyin-auto-sender/.env
```

写入：

```env
DOUYIN_COOKIE=/etc/douyin-auto-fire/cookie.json
HEADLESS=true
```

如需固定出口代理，可继续加入：

```env
DOUYIN_PROXY_SERVER=http://proxy.example.com:3128
DOUYIN_PROXY_USERNAME=proxy-user
DOUYIN_PROXY_PASSWORD=proxy-password
```

HTTP 支持可选用户名和密码，并可通过 CONNECT 访问 HTTPS 页面；SOCKS5 仅支持无认证。代理失败时任务会直接失败，不会回退直连。

如果需要钉钉通知，可以继续加入：

```env
DINGTALK_WEBHOOK=你的钉钉Webhook
DINGTALK_SECRET=你的钉钉Secret
```

`DINGTALK_WEBHOOK` 和 `DINGTALK_SECRET` 必须同时填写。

保存后可以限制 `.env` 权限：

```bash
sudo chmod 600 /opt/douyin-auto-sender/.env
```

---

## 10. 第一次运行 Dry Run

不要第一次就直接发送消息。

先执行：

```bash
cd /opt/douyin-auto-sender
sudo -u douyin-sender -H .venv/bin/python run.py --dry-run
```

Dry Run 会检查：

- Cookie 是否有效；
- 是否可以进入抖音私信页；
- 是否能够找到目标好友；
- 配置是否正确。

但 **不会真实发送消息**。

如果运行成功，再进入下一步。

---

## 11. 测试真实发送

执行：

```bash
cd /opt/douyin-auto-sender
sudo -u douyin-sender -H .venv/bin/python run.py
```

这次会真实发送消息。

第一次建议仍然只保留一个测试好友，确认发送对象和内容全部正确以后再增加其他好友。

---

## 12. 配置 systemd 自动运行

项目已经自带 systemd 配置：

```text
deploy/systemd/douyin-sender.service
deploy/systemd/douyin-sender.timer
```

复制到 systemd：

```bash
sudo cp deploy/systemd/douyin-sender.service /etc/systemd/system/
sudo cp deploy/systemd/douyin-sender.timer /etc/systemd/system/
```

重新加载 systemd：

```bash
sudo systemctl daemon-reload
```

启动并设置开机自启：

```bash
sudo systemctl enable --now douyin-sender.timer
```

查看定时器：

```bash
systemctl list-timers --all | grep douyin-sender
```

---

## 13. 默认运行时间

项目自带的 Timer 默认是：

```ini
OnCalendar=*-*-* 08:00:00
```

也就是每天服务器本地时间 **08:00** 运行。

建议先确认服务器时区：

```bash
timedatectl
```

如果希望使用北京时间：

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

然后再次确认：

```bash
timedatectl
```

---

## 14. 修改每天运行时间

编辑：

```bash
sudo nano /etc/systemd/system/douyin-sender.timer
```

例如每天 **00:30**：

```ini
[Timer]
OnCalendar=*-*-* 00:30:00
Persistent=true
RandomizedDelaySec=0
```

例如每天 **20:00**：

```ini
[Timer]
OnCalendar=*-*-* 20:00:00
Persistent=true
RandomizedDelaySec=0
```

修改后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart douyin-sender.timer
```

---

## 15. 手动触发一次 systemd 任务

可以执行：

```bash
sudo systemctl start douyin-sender.service
```

查看结果：

```bash
sudo systemctl status douyin-sender.service
```

查看日志：

```bash
journalctl -u douyin-sender.service -n 100 --no-pager
```

实时查看：

```bash
journalctl -u douyin-sender.service -f
```

---

## 16. 查看程序诊断文件

项目自己的运行日志和诊断文件位于：

```text
/opt/douyin-auto-sender/artifacts/
```

例如：

```bash
ls -lah /opt/douyin-auto-sender/artifacts
```

可能包含：

```text
run.log
result.json
history.json
screenshots/
traces/
```

如果发送失败，可以优先查看：

```bash
cat /opt/douyin-auto-sender/artifacts/run.log
```

> 截图和日志可能包含账号或聊天相关信息，请不要随意公开。

---

## 17. Cookie 失效怎么办？

如果日志提示登录失效、安全验证或 Cookie 无效：

1. 在自己的电脑重新登录抖音；
2. 使用 Cookie-Editor 重新导出 JSON；
3. 在服务器重新编辑：

```bash
sudo nano /etc/douyin-auto-fire/cookie.json
```

4. 替换为新的 Cookie；
5. 再执行一次 Dry Run：

```bash
cd /opt/douyin-auto-sender
sudo -u douyin-sender -H .venv/bin/python run.py --dry-run
```

Dry Run 成功后即可继续自动运行。

---

## 18. 更新项目

以后仓库更新后，可以执行：

```bash
cd /opt/douyin-auto-sender
sudo -u douyin-sender -H git pull
sudo -u douyin-sender -H .venv/bin/pip install -r requirements.txt
```

如果 Playwright 版本发生变化，建议同时重新执行：

```bash
sudo /opt/douyin-auto-sender/.venv/bin/python -m playwright install-deps chromium
sudo -u douyin-sender -H /opt/douyin-auto-sender/.venv/bin/python -m playwright install chromium
```

如果 `deploy/systemd/` 中的服务文件也更新了，再重新复制并执行：

```bash
sudo cp deploy/systemd/douyin-sender.service /etc/systemd/system/
sudo cp deploy/systemd/douyin-sender.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart douyin-sender.timer
```

---

## 常用命令

```bash
# Dry Run，不发送消息
sudo -u douyin-sender -H /opt/douyin-auto-sender/.venv/bin/python /opt/douyin-auto-sender/run.py --dry-run

# 手动真实运行
cd /opt/douyin-auto-sender
sudo -u douyin-sender -H .venv/bin/python run.py

# 查看定时器
systemctl list-timers --all | grep douyin-sender

# 手动运行一次 systemd 任务
sudo systemctl start douyin-sender.service

# 查看 systemd 日志
journalctl -u douyin-sender.service -n 100 --no-pager

# 查看程序日志
cat /opt/douyin-auto-sender/artifacts/run.log
```

---

## 其他部署方式

- 👉 [GitHub Actions 部署](github-actions.md)
- 👉 [Windows 电脑部署](windows.md)
- 👉 [返回项目主页](../README.md)
