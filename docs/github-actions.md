# GitHub Actions 使用教程

本教程介绍如何使用 **GitHub Actions** 运行 `douyin-auto-fire`。

使用这种方式不需要自己准备服务器，也不需要电脑每天开机。配置完成后，GitHub Actions 会按照设定时间自动运行任务。

> 建议第一次只配置 **1 个抖音账号 + 1 个好友 + 1 条文字消息**。确认正常运行后，再添加其他好友、原生表情、随机消息或多账号。

---

## 1. Fork 项目

打开项目仓库：

**https://github.com/unmev/douyin-auto-fire**

点击右上角 **Fork**，将项目 Fork 到自己的 GitHub 账号。

![Fork 项目](https://img.908988.xyz/file/教程/douyin-auto-fire/DKPd0GVi.webp)

Fork 完成后，后面的所有操作都在 **你自己 Fork 出来的仓库** 中进行。

---

## 2. 启用 GitHub Actions

进入自己 Fork 后的仓库，点击顶部的 **Actions**。

如果 GitHub 提示 Fork 仓库的 Workflow 被禁用，点击启用工作流。

启用以后应该可以看到：

```text
Send Douyin Messages
```

这就是项目每天自动运行使用的工作流。

---

## 3. 获取抖音 Cookie

程序需要 Cookie 才能保持抖音登录状态。

### 3.1 登录抖音网页版

使用电脑浏览器打开：

**https://www.douyin.com/**

登录自己的抖音账号，并确认能够正常进入私信页面。

### 3.2 安装 Cookie-Editor

推荐使用浏览器扩展 **Cookie-Editor**：

**https://chromewebstore.google.com/detail/hlkenndednhfkekhgcdicdfddnkalmdm**

安装完成后，回到已经登录抖音的页面并打开 Cookie-Editor。

![打开 Cookie-Editor](https://img.908988.xyz/file/教程/douyin-auto-fire/STZqIxDn.webp)

### 3.3 导出 Cookie

点击 Cookie-Editor 的导出功能，导出格式选择 **JSON**。

![导出 Cookie](https://img.908988.xyz/file/教程/douyin-auto-fire/1rilVYmK.webp)

然后复制完整的 JSON 内容。

![复制 Cookie JSON](https://img.908988.xyz/file/教程/douyin-auto-fire/QKQHfndn.webp)

正确格式大致如下：

```json
[
  {
    "name": "xxx",
    "value": "xxx",
    "domain": ".douyin.com",
    "path": "/"
  }
]
```

请注意：

- 必须复制完整的 `[ ... ]` JSON 数组。
- 不要使用 `name=value; name=value;` 形式。
- 不要删除 Cookie 中的字段。
- 不要把 Cookie 提交到 GitHub 仓库。

> ⚠️ Cookie 相当于账号登录凭证，请不要发送给其他人，也不要公开到 Issue、日志或截图中。

---

## 4. 生成发送配置

除了 Cookie，程序还需要知道给谁发送、发送什么内容以及消息发送间隔。

如果不想自己写 JSON，可以直接使用配置生成器：

**https://douyin-config.pages.dev/**

生成完成后复制网站生成的完整 JSON。

一个最简单的配置例如：

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

第一次使用建议先只配置：

```text
1 个好友 + 1 条文字消息
```

先把最基础的流程跑通，再增加其他功能。

---

## 5. 添加 GitHub Secrets

进入自己 Fork 的仓库，依次打开：

```text
Settings
↓
Secrets and variables
↓
Actions
↓
New repository secret
```

![进入 Secrets](https://img.908988.xyz/file/教程/douyin-auto-fire/aiPBHuxJ.webp)

![创建 Secret](https://img.908988.xyz/file/教程/douyin-auto-fire/BKtXckyQ.webp)

第一次使用至少需要添加下面两个 Secret：

| Secret | 内容 | 必须 |
| --- | --- | --- |
| `DOUYIN_COOKIE` | Cookie-Editor 导出的完整 Cookie JSON | ✅ |
| `DOUYIN_CONFIG` | 配置生成器生成的完整配置 JSON | ✅ |

### 5.1 添加 `DOUYIN_COOKIE`

点击 **New repository secret**。

Name 填：

```text
DOUYIN_COOKIE
```

Secret 粘贴刚刚导出的完整 Cookie JSON，然后保存。

### 5.2 添加 `DOUYIN_CONFIG`

再次点击 **New repository secret**。

Name 填：

```text
DOUYIN_CONFIG
```

Secret 粘贴刚刚生成的完整配置 JSON，然后保存。

配置完成后至少应该存在：

```text
DOUYIN_COOKIE
DOUYIN_CONFIG
```

GitHub 保存 Secret 后不会再次显示具体内容，这是正常现象。

### 5.3 配置固定出口代理（可选）

GitHub 托管 Runner 的公网出口 IP 会变化。如果希望每次任务都从同一个公网 IP 访问抖音，需要购买或准备一个提供**静态出口 IP**的代理，并将代理信息保存为 GitHub Secrets。

支持范围：

| 代理类型 | 无认证 | 用户名/密码认证 |
| --- | --- | --- |
| HTTP（可通过 CONNECT 访问 HTTPS 页面） | ✅ | ✅ |
| SOCKS5 | ✅ | ❌ Playwright Chromium 不支持 |

推荐使用 HTTP 固定代理。在仓库的 Actions Secrets 中添加：

| Secret | 内容 | 必须 |
| --- | --- | --- |
| `DOUYIN_PROXY_SERVER` | 代理地址，例如 `http://proxy.example.com:3128` | 使用代理时必须 |
| `DOUYIN_PROXY_USERNAME` | 代理用户名 | 认证代理必须 |
| `DOUYIN_PROXY_PASSWORD` | 代理密码 | 认证代理必须 |

HTTP 认证代理示例：

```text
DOUYIN_PROXY_SERVER=http://proxy.example.com:3128
DOUYIN_PROXY_USERNAME=proxy-user
DOUYIN_PROXY_PASSWORD=proxy-password
```

无认证 SOCKS5 示例：

```text
DOUYIN_PROXY_SERVER=socks5://proxy.example.com:1080
```

注意：

- 用户名和密码必须同时配置；
- 不要把用户名或密码写进 `DOUYIN_PROXY_SERVER`，应使用独立 Secret；
- 带认证的 SOCKS5 会在浏览器启动前直接报错，请改用代理服务商提供的 HTTP 接入地址；
- 配置代理后，如果代理不可用或认证失败，任务会失败，不会回退到 GitHub Runner 直连；
- 项目只能保证浏览器使用指定代理，出口 IP 是否长期固定由代理服务商保证；
- 未配置 `DOUYIN_PROXY_SERVER` 时，程序保持原有直连逻辑。

---

## 6. 第一次运行：Dry Run

配置完成后，不建议第一次就直接真实发送。

项目提供了 **Dry Run** 模式，用来检查：

- Cookie 是否有效；
- 是否能够正常登录抖音；
- 是否能够找到目标好友；
- 配置是否正确。

Dry Run **不会真正发送消息**。

进入：

```text
Actions
↓
Send Douyin Messages
↓
Run workflow
```

第一次运行时，将 `dry_run` 开启（即 `true`），然后点击 **Run workflow**。

![运行 GitHub Actions](https://img.908988.xyz/file/教程/douyin-auto-fire/NLFF8g94.webp)

如果最后显示绿色的 `✓`，说明本次运行成功。

如果失败，点击本次 Workflow Run，进入：

```text
send
↓
Run
```

查看具体错误日志。不要只看最下面的 `Process completed with exit code 1`，真正的报错通常在它前面。

---

## 7. 测试真实发送

Dry Run 成功后，再手动运行一次工作流。

这一次关闭 `dry_run`，也就是：

```text
dry_run = false
```

然后运行。

这一次程序会真正向好友发送消息。

第一次真实发送仍建议只保留 **1 个测试好友**，确认好友、消息和发送结果都正确以后，再增加其他好友。

---

## 8. 使用外部 Cron 自动运行

GitHub Actions 自带的 `schedule` 定时任务有时可能出现延迟。可以使用免费的外部定时服务 **[cron-job.org](https://cron-job.org/)**，每天定时调用 GitHub API 来启动本项目。

这种方式仍然：

- 不需要服务器；
- 不需要电脑保持开机；
- 程序仍然运行在 GitHub Actions；
- cron-job.org 只负责到时间后触发工作流。

整体流程：

```text
cron-job.org
      ↓
GitHub API
      ↓
workflow_dispatch
      ↓
GitHub Actions 运行发送任务
```

### 8.1 确认工作流支持外部触发

打开 `.github/workflows/send.yml`，确保 `on:` 中存在：

```yaml
on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: Only verify login and friends without sending
        type: boolean
        default: false
```

本项目已经默认支持，一般不需要修改。

如果 `send.yml` 中还启用了 `schedule`，建议将它注释或删除，只保留 `workflow_dispatch`。否则 GitHub 自带定时和外部 Cron 可能同时触发，导致一天运行两次。

> 当前项目中的 `schedule` 已经默认注释，直接配置外部 Cron 即可。

### 8.2 创建 GitHub Token

cron-job.org 调用 GitHub API 时需要 GitHub Token。

进入 GitHub：

```text
头像
↓
Settings
↓
Developer settings
↓
Personal access tokens
↓
Fine-grained tokens
↓
Generate new token
```

创建时按下面设置：

1. `Token name`：可以填写 `cron-job`；
2. `Expiration`：选择合适的有效期，并记住到期后需要重新创建；
3. `Repository access`：选择 `Only select repositories`；
4. 只勾选自己 Fork 的 `douyin-auto-fire` 仓库；
5. 在 `Repository permissions` 中找到 `Actions`，设置为 `Read and write`。

创建完成后会得到类似：

```text
github_pat_xxxxxxxxxxxxxxxxx
```

请立即复制并妥善保存，GitHub 不会再次完整显示它。

> ⚠️ Token 相当于 GitHub 登录凭证，不要提交到仓库、README、Issue、日志或公开截图中。

### 8.3 创建 cron-job.org 任务

打开 **[cron-job.org](https://cron-job.org/)**，注册并登录账号，然后进入：

```text
Dashboard
↓
Cronjobs
↓
CREATE CRONJOB
```

创建一个新的定时任务。

### 8.4 填写 GitHub API 地址

在 `URL` 中填写：

```text
https://api.github.com/repos/你的GitHub用户名/douyin-auto-fire/actions/workflows/send.yml/dispatches
```

例如原项目仓库对应的地址是：

```text
https://api.github.com/repos/unmev/douyin-auto-fire/actions/workflows/send.yml/dispatches
```

如果使用的是自己 Fork 的仓库，必须将 `unmev` 换成你自己的 GitHub 用户名。

### 8.5 设置请求方式

`Request Method` 选择：

```text
POST
```

不要使用 `GET`。

### 8.6 添加 Request Headers

在 `Request headers` 中依次添加下面四项：

| Name | Value |
| --- | --- |
| `Authorization` | `Bearer 你的GitHubToken` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

`Authorization` 示例：

```text
Bearer github_pat_xxxxxxxxxxxxxxxxx
```

注意 `Bearer` 后面有一个空格，不要写成 `Bearer:`。

### 8.7 填写 Request Body

第一次测试时，建议先使用 Dry Run。在 `Request body` 中填写：

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": "true"
  }
}
```

其中：

- `ref` 表示运行的分支，默认是 `main`；
- `dry_run = true` 表示只检查登录和好友，不会真正发送消息。

确认外部触发正常后，再将 Body 改为：

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": "false"
  }
}
```

`dry_run = false` 表示正式执行发送任务。

### 8.8 设置每天运行时间

在 cron-job.org 中选择每天运行，并设置需要的时间。

例如希望每天北京时间 `08:30` 运行：

```text
Schedule：每天 08:30
Time zone：Asia/Shanghai
```

选择 `Asia/Shanghai` 后可以直接填写北京时间，不需要换算成 UTC。

### 8.9 先执行一次测试

全部填写完成后，不要直接等到第二天。先保存任务，然后使用 cron-job.org 的立即执行或测试功能运行一次。

测试时保持：

```json
"dry_run": "true"
```

随后打开自己 Fork 的 GitHub 仓库：

```text
Actions
↓
Send Douyin Messages
```

如果出现一条新的 Workflow Run，说明 cron-job.org 已经成功触发 GitHub Actions。检查 Dry Run 日志没有问题后，再把 Request Body 中的 `dry_run` 改为 `false`。

GitHub API 成功接收触发请求时通常返回状态码：

```text
204 No Content
```

响应内容为空是正常现象。

### 8.10 最终配置参考

```text
URL：
https://api.github.com/repos/你的GitHub用户名/douyin-auto-fire/actions/workflows/send.yml/dispatches

Method：
POST

Headers：
Authorization: Bearer 你的GitHubToken
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json

Time zone：
Asia/Shanghai
```

正式运行使用的 Body：

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": "false"
  }
}
```

### 8.11 常见错误

#### 返回 401

一般表示 Token 无效。检查：

- Token 是否复制完整；
- Token 是否已经过期；
- `Authorization` 是否为 `Bearer + 空格 + Token`。

#### 返回 403

一般表示 Token 权限不足。检查：

- `Repository access` 是否包含自己 Fork 的仓库；
- `Repository permissions` → `Actions` 是否为 `Read and write`；
- 是否误用了其他账号创建的 Token。

#### 返回 404

检查：

- URL 中的 GitHub 用户名和仓库名是否正确；
- Workflow 文件名是否为 `send.yml`；
- `ref` 是否为仓库中真实存在的分支；
- Token 是否有权访问该仓库。

#### 返回 422

一般表示请求内容不符合要求。检查：

- `send.yml` 是否包含 `workflow_dispatch`；
- Request Body 是否为有效 JSON；
- Body 中的 `ref` 和 `inputs.dry_run` 是否正确。

#### Cron 显示成功，但 Actions 没有运行

先确认 cron-job.org 的执行记录返回 `204`，然后检查 `.github/workflows/send.yml` 是否仍然包含 `workflow_dispatch`。

#### 一天运行了两次

检查 `send.yml` 是否还启用了 `schedule`，以及 cron-job.org 中是否创建了两个相同任务。外部 Cron 和 GitHub 自带定时只保留一种即可。

### 8.12 这套方案实际做了什么

cron-job.org 本身不会运行 Python、登录抖音或发送消息。它只相当于每天到时间后，自动帮你执行一次：

```text
Actions
↓
Send Douyin Messages
↓
Run workflow
```

真正运行程序的仍然是 GitHub Actions，因此不需要自己的服务器，也不需要电脑保持开机。

---

## 9. Cookie 失效怎么办？

Cookie 并不是永久有效。

如果 Actions 日志提示登录失效、需要重新登录、安全验证或 Cookie 无效：

1. 使用浏览器重新登录抖音网页版；
2. 用 Cookie-Editor 重新导出 Cookie JSON；
3. 打开仓库 `Settings`；
4. 进入 `Secrets and variables` → `Actions`；
5. 更新 `DOUYIN_COOKIE`；
6. 保存后手动执行一次 `dry_run = true`。

Dry Run 成功后即可继续正常使用。

---

## 10. 通知渠道（可选）

钉钉和 Telegram 可以单独使用，也可以同时配置。任一通知渠道发送失败都不会改变抖音任务本身的成功或失败结果。

### 10.1 钉钉通知

如果希望通过钉钉接收任务结果，可以额外添加：

| Secret | 内容 |
| --- | --- |
| `DINGTALK_WEBHOOK` | 钉钉机器人 Webhook |
| `DINGTALK_SECRET` | 钉钉机器人 Secret |

这两个 Secret 必须同时配置。

如果不需要钉钉通知，两个都不要添加即可，不影响项目正常运行。

### 10.2 Telegram 通知

先通过 Telegram 的 `@BotFather` 创建机器人并获取 Bot Token，然后取得接收通知的用户、群组或频道 Chat ID。在 Actions Secrets 中添加：

| Secret | 内容 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather 提供的机器人 Token |
| `TELEGRAM_CHAT_ID` | 接收通知的 Chat ID；群组或频道通常是负数 |

这两个 Secret 必须同时配置。未配置时程序不会调用 Telegram API，也不会影响现有逻辑。

Telegram 通知包含：

- 任务成功或存在失败；
- 正式运行或 Dry Run；
- 多账号模式下的账号 ID；
- 成功好友和发送数量；
- 失败好友和经过长度限制、安全处理的错误信息。

结果较多时消息会自动分段发送。通知不会包含 Cookie、Storage State、代理凭据、Bot Token、截图或完整异常堆栈。

---

## 11. 多账号（可选）

项目当前最多支持 **5 个抖音账号**。

第一次使用不建议直接配置多账号。先确保单账号模式下的：

```text
DOUYIN_COOKIE
DOUYIN_CONFIG
```

能够正常运行。

之后可以按照账号添加：

```text
DOUYIN_COOKIE_ACCOUNT1
DOUYIN_CONFIG_ACCOUNT1

DOUYIN_COOKIE_ACCOUNT2
DOUYIN_CONFIG_ACCOUNT2

DOUYIN_COOKIE_ACCOUNT3
DOUYIN_CONFIG_ACCOUNT3
```

以此类推，最多到 `ACCOUNT5`。

每个账号的 Cookie 和 Config 必须成对配置，不能只添加其中一个。

### 老用户增加第二个账号

如果以前一直使用：

```text
DOUYIN_COOKIE
DOUYIN_CONFIG
```

不需要删除原来的配置。

可以直接增加：

```text
DOUYIN_COOKIE_ACCOUNT2
DOUYIN_CONFIG_ACCOUNT2
```

原来的 `DOUYIN_COOKIE` / `DOUYIN_CONFIG` 会继续作为第一个账号使用。

---

## 12. 运行失败后的诊断文件

如果 GitHub Actions 运行失败，项目会自动上传诊断文件，可能包括：

```text
run.log
result.json
screenshots/
traces/
```

进入失败的 Workflow 页面，在页面底部找到 **Artifacts** 即可下载。

失败诊断 Artifact 默认保留 **3 天**。

这些文件可以帮助判断：

- Cookie 是否失效；
- 是否出现安全验证；
- 好友是否没有找到；
- 页面结构是否变化；
- Playwright 在哪一步失败。

> ⚠️ 截图和日志可能包含聊天内容或账号相关信息，请不要直接公开上传。

### 8.13 跨运行状态归档

正式运行会先从当前分支最近一次运行保存的状态 Artifact 恢复：

```text
history.json
account-state.json
```

状态归档只包含这两类 JSON，不包含 Cookie、Storage State、Token、配置、日志、截图或 trace，默认保留 7 天。Dry Run 不恢复也不写入跨运行状态；如果归档缺失、损坏、分支不匹配或校验失败，正式运行会直接停止，以避免状态丢失导致重复发送。

当前工作流不会自动绕过这个安全门。首次建立状态归档时，只能在确认“今天还没有向配置好友发送过消息”后，手动执行一次工作流并显式开启 `bootstrap_state`（同时关闭 `dry_run`）；如果已经存在归档，bootstrap 会被拒绝。bootstrap 成功后先确认状态 Artifact 已生成，再恢复普通正式运行。不要在同一天已经发送过消息后用 bootstrap 初始化空 history，也不要反复点击正式运行。

每次正式运行在发送前会保存一个不含敏感数据的不可变发送 marker；如果运行中断或状态 Artifact 上传失败，后续运行会发现未配对 marker 并安全停止，而不是回退到旧 history。公开仓库的 Artifact 对仓库读者可见，虽然不含登录凭证，仍可能包含好友身份和发送历史元数据，请据此评估仓库可见范围。

---

## 第一次使用推荐流程

```text
Fork 项目
    ↓
启用 Actions
    ↓
登录抖音
    ↓
导出 Cookie
    ↓
生成发送配置
    ↓
添加 DOUYIN_COOKIE
    ↓
添加 DOUYIN_CONFIG
    ↓
开启 Dry Run
    ↓
确认运行成功
    ↓
关闭 Dry Run
    ↓
测试真实发送
    ↓
确认成功
    ↓
等待每天自动运行
```

第一次不要同时配置多账号、多个好友、原生表情、随机消息和钉钉通知。

先把最基础的流程跑通，这样即使出现问题，也更容易判断是哪一步出了问题。

---

## 返回项目主页

👉 [返回 douyin-auto-fire](../README.md)
