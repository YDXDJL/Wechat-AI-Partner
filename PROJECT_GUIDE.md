# Micro Agent 项目详细解读与运行指南

本文档面向“第一次接手这个项目的人”，目标是把项目是什么、依赖什么、怎么配置、怎么启动、怎么使用全部讲清楚。

## 1. 项目是什么

Micro Agent 是一个本地运行的小型 Agent 框架。它把以下能力组合在一起：

- 多模型接入：支持 Anthropic/Claude 风格接口，也支持 OpenAI 风格接口。
- 小米 MiMo Token Plan：当前项目可通过 Anthropic 兼容接口使用 `mimo-v2.5`。
- MCP 和内置工具：模型可以使用文件、命令、时间、天气、网页搜索、网页抓取等工具。
- 微信集成：可以连接微信机器人账号，接收微信消息并回复。
- 多账号管理：微信账号可扫码登录、保存、切换；不同账号上下文隔离。
- Skill 人设系统：读取 `skills/` 里的 `SKILL.md`，把角色、人设、记忆注入模型。
- 图片理解：支持微信图片和本地图片路径；微信图片会先缓存，等待下一句文字要求后再一起发给模型。
- 聊天历史：按账号和微信联系人保存 JSON 历史，后续可继续上下文。
- 定时任务：通过 cron 表达式让 Agent 定时运行任务。

## 2. 目录结构

核心文件如下：

```text
Agent/
  main.py                 主入口，负责 CLI、微信模式、命令分发
  agent.py                核心 Agent，负责模型调用、tool loop、历史保存、skill 注入
  builtin_tools.py        内置工具：Read/Write/Edit/Bash/CurrentTime/WebSearch/WebFetch/Weather
  mcp_client.py           MCP 客户端，连接外部 MCP server，隐藏微信发送工具
  wechat.py               微信客户端，负责轮询消息、发消息、下载图片
  wechat_accounts.py      微信账号管理，扫码登录、默认账号、skill 绑定
  image_utils.py          图片检测、base64 构造、图片摘要
  skill_manager.py        Skill 加载、别名解析、skill prompt 构造
  history.py              聊天历史 JSON 读写
  models.py               模型配置读写
  model_configurator.py   独立模型配置工具，可扫描、新增、编辑、复制模型配置
  wizard.py               首次模型配置向导（兼容旧流程）
  scheduler.py            定时任务
  config.py               配置加载
  README.md               原项目说明
  requirements.txt        Python 依赖
  config.json             Agent 基础配置
  mcp_servers.json        MCP server 配置
  scheduler_jobs.json     定时任务配置
  models/                 模型配置目录
  skills/                 本地 skill 目录
  history/                对话历史目录
  media/wechat/           微信图片等媒体保存目录，运行后生成
```

## 3. 环境准备

推荐使用 Python 3.11+。当前机器上你用的是 Windows + PowerShell，项目目录是：

```powershell
cd C:\Users\Lisc\Desktop\Agent
```

安装基础依赖：

```powershell
pip install -r requirements.txt
```

微信集成还需要 `wechat_clawbot`。如果本地还没有，需要安装：

```powershell
pip install wechat_clawbot
```

如果要读 PDF，可选安装：

```powershell
pip install PyPDF2
```

## 4. 模型配置

模型配置保存在 `models/` 目录下，每个模型一个 JSON 文件。

当前 MiMo 配置示例在：

```text
models/mimo_v2.5.json
```

典型字段：

```json
{
  "display_name": "MiMo V2.5",
  "provider": "claude",
  "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
  "model": "mimo-v2.5",
  "api_key": "...",
  "provider_name": "Xiaomi MiMo"
}
```

关键点：

- `provider: "claude"` 表示使用 Anthropic/Claude 兼容调用方式。
- `base_url` 指向小米 Token Plan 的 Anthropic 兼容地址。
- `model` 使用 `mimo-v2.5`。
- 当前选中的模型名保存在 `models/.selected`。

如果没有模型配置，启动时会进入 `wizard.py` 交互式配置流程。

启动后推荐使用主程序内置命令管理模型：

```text
/new-model
/model-list
/model <name|number>
```

- `/new-model`：在终端中分步新增模型配置，完成后自动保存并切换到新模型。
- `/model-list`：重新扫描 `models/` 目录，列出所有可选模型和当前选中项。
- `/model <name|number>`：按配置名或列表序号切换模型，例如 `/model 1`。

微信侧支持 `/model-list` 和 `/model <name|number>`，但不支持真正新增模型。新增模型必须在终端中执行，避免 API Key 出现在微信聊天里。

## 5. 基础启动方式

在项目目录运行：

```powershell
python main.py
```

启动时会做这些事：

1. 读取 `config.json`。
2. 读取 `mcp_servers.json` 并连接 MCP server。
3. 注册内置工具。
4. 加载默认微信账号。
5. 扫描并加载 `skills/`。
6. 读取当前模型配置。
7. 启动定时任务调度器。
8. 进入主循环，等待本地输入或微信消息。

成功启动后会看到类似：

```text
MCP: ...
WeChat: Connected default account (...)
Skills: ...
Model: ...
Micro Agent ready. Type '/help' for commands, '/quit' to exit.
```

## 6. 本地命令使用

启动后，在终端里可以输入命令。

常用命令：

```text
/help
/quit
/exit
/reset
/clear
/history
/new-model
/model-list
/model <name|number>
/wechat
/wechat switch
/account
/skills
/skills show <skill>
/skills search <query>
/skills reload
/<skill-name> [args]
/bind <skill>
/unbind
/image <path> [prompt]
/schedule add <id> "<cron>" "<prompt>"
/schedule remove <id>
/schedule list
```

说明：

- `/help`：显示命令和 skill 列表。
- `/new-model`：在终端中新增模型配置。
- `/model-list`：列出模型配置。
- `/model <name|number>`：切换模型。
- `/wechat`：开启或关闭微信模式。
- `/wechat switch`：扫码切换微信账号，并清空该账号上下文。
- `/<skill-name>`：激活某个 skill，并自动进入微信模式。
- `/bind <skill>`：把 skill 绑定到当前微信账号。
- `/image <path> [prompt]`：把本地图片发给 Agent 分析。

普通文本不是命令，会直接发送给当前 Agent。

如果微信模式已经开启，并且有最近微信联系人，终端普通文本会路由到该微信联系人对应的 `chat_agent`，回复也会发回微信。

## 7. 微信使用流程

### 7.1 首次或切换账号

运行：

```text
/wechat switch
```

程序会打开或打印二维码链接。用微信扫码后，账号会保存到本地。保存位置由 `wechat_clawbot` 管理，项目自身通过 `wechat_accounts.py` 读取。

切换账号后：

- 新账号会成为下次默认账号。
- 当前账号的上下文会清空。
- sender agent 缓存会重建。
- pending 图片缓存会清空。

### 7.2 开启微信模式

运行：

```text
/wechat
```

开启后，`wechat.py` 会开始后台轮询微信消息。

收到微信文字后：

1. `wechat.py` 把消息放入队列。
2. `main.py` 取出消息。
3. 按 `sender_id` 找到对应的独立 `Agent`。
4. 包装成 `<channel source="wechat">...</channel>`。
5. 交给模型。
6. 回复通过 `send_wechat_reply()` 发回微信。

### 7.3 微信回复拆分

微信回复统一走：

```python
send_wechat_reply()
```

它内部调用：

```python
split_wechat_reply()
```

拆分规则：

- 如果模型输出多行，每一行会作为一条微信气泡。
- 如果是短句，直接一条发出。
- 如果是长句，会按中文/英文句末标点尝试拆分。
- 默认最多拆成 4 条。

这个功能是微信回复出口，不应绕过。

## 8. 图片功能怎么用

### 8.1 微信图片

当前逻辑是“先收图，后等要求”：

1. 用户在微信发送纯图片。
2. 程序下载图片到本地 `media/wechat/<account>/<sender>/`。
3. 图片不会立刻发给模型。
4. 图片会进入 `pending_wechat_images[sender_id]` 缓存。
5. 用户下一句文字要求到达后，程序把“上一张/多张图片 + 当前文字要求”一起发给模型。

示例：

```text
用户：发送一张截图
程序：不回复，只缓存图片
用户：这张图里是什么？
程序：把图片和“这张图里是什么？”一起发给模型
```

如果连续发送多张纯图片，它们会累积到同一个 sender 的 pending 缓存里，下一句文字会一次性带给模型。

### 8.2 本地图片

终端命令：

```powershell
/image "C:\path\to\image.png" 这张图是什么？
```

如果当前在微信模式并且有最近联系人：

- 本地图片会作为该微信联系人的下一条输入。
- 回复会发回微信。

如果不在微信模式：

- 图片会发给普通终端 Agent。
- 回复显示在终端。

### 8.3 图片历史

请求模型时会发送真实 base64 图片。

保存历史前会把 base64 替换成摘要：

```text
【图片】用户发送了一张图片
来源: wechat
路径: ...
格式: image/png
大小: ... bytes
消息ID: ...
```

这样后续上下文能知道用户发过图片、图片在哪里、格式和大小是什么，但不会保存巨大的 base64。

## 9. Skill 人设系统

Skill 放在：

```text
skills/
```

一个 skill 通常是一个目录，目录里有：

```text
SKILL.md
persona.md
memory.md
examples.md
meta.json
```

`skill_manager.py` 会递归扫描：

```text
skills/
.claude/skills/
```

只要目录里有 `SKILL.md`，就会加载为一个 skill。

### 9.1 SKILL.md 基本格式

```markdown
---
name: xia-yizhou
description: 夏以昼人设
argument-hint: [可选参数]
user-invocable: true
allowed-tools: Read, Bash
---

这里写角色、人设、说话方式、规则。
```

### 9.2 Skill 别名

Skill 名称来源：

- `SKILL.md` frontmatter 里的 `name`
- skill 目录名
- `meta.json` 里的 `slug`
- `meta.json` 里的 `english_name`
- frontmatter 里的 alias/aliases

所以可以通过不同名字激活同一个 skill。

### 9.3 激活 skill

终端输入：

```text
/ex-xia-yizhou
```

或：

```text
/bind ex-xia-yizhou
```

区别：

- `/<skill>`：立即激活，并进入微信模式。
- `/bind <skill>`：把 skill 绑定到当前微信账号，后续自动加载。
- `/unbind`：取消绑定。

Skill 激活后，会写入 `Agent.extra_instructions` 和 persistent skill 状态。每个微信 sender 的独立 Agent 都会应用绑定 skill。

## 10. 现实信息和联网工具

模型可见的内置工具包括：

```text
CurrentTime
Weather
WebSearch
WebFetch
Read
Write
Edit
Bash
```

工具逻辑：

- `CurrentTime`：获取当前时间，默认 `Asia/Shanghai`。
- `Weather`：查询城市当前天气。
- `WebSearch`：搜索网页，返回标题、链接、摘要。
- `WebFetch`：抓取网页正文。

重要设计：

- 程序不再用关键词硬触发搜索。
- 模型先判断自己的知识和上下文是否足够。
- 不够时，模型自己调用工具。
- 工具结果回来后，模型仍然按当前 skill 人设回复。

微信发送工具 `wechat_reply/wechat_typing/wechat_send_file` 被隐藏，不暴露给模型，避免模型自己发微信造成重复或“前面内容没放出来”。

## 11. 聊天历史

历史保存在：

```text
history/
```

普通终端历史：

```text
history/conversation.json
```

微信账号历史：

```text
history/<account_id>/conversation.json
```

微信联系人独立历史：

```text
history/<account_id>/<sender_id>/conversation.json
```

每次 `Agent.run()` 完成后会保存历史。

历史保存前会做清理：

- 移除 thinking block。
- 移除旧版本残留的微信发送工具调用。
- 把旧 `wechat_reply` 工具内容转换成普通 assistant 文本。
- 把图片 base64 替换成 `【图片】` 摘要。

## 12. 定时任务

添加任务：

```text
/schedule add morning "0 9 * * *" "早上好，给我一条提醒"
```

查看任务：

```text
/schedule list
```

删除任务：

```text
/schedule remove morning
```

定时任务通过 `scheduler.py` 使用 APScheduler。每个任务会创建独立 Agent 实例运行，不直接复用当前聊天中的 Agent。

## 13. 常见运行方式

### 13.1 只在终端聊天

```powershell
cd C:\Users\Lisc\Desktop\Agent
python main.py
```

然后直接输入：

```text
你好
```

### 13.2 使用微信聊天

```powershell
python main.py
/wechat
```

然后在微信里给机器人发消息。

### 13.3 切换微信账号

```text
/wechat switch
```

扫码后，新账号保存为默认账号，上下文清空。

### 13.4 激活人设并微信聊天

```text
/ex-xia-yizhou
```

如果已有最近微信联系人：

- 有上下文：等待对方下一句。
- 无上下文：主动发一条符合人设的开场消息。

### 13.5 发微信图片并追问

```text
微信：发送图片
微信：这张图里是什么？
```

第一步只缓存图片，第二步才调用模型。

### 13.6 本地图片分析

```powershell
/image "C:\Users\Lisc\Pictures\test.png" 帮我看看这张图
```

## 14. 排查问题

### 14.1 微信没有回复

检查：

- 是否已经 `/wechat` 开启微信模式。
- 是否已经连接默认账号。
- 终端是否有 `[WeChat: ...]` 入站日志。
- 是否发送的是纯图片。纯图片会缓存，不会立刻回复，需要再发一句文字要求。

### 14.2 回复重复

当前发送逻辑已经避免模型直接调用 `wechat_reply`，微信回复只走 `send_wechat_reply()`。如果仍重复，优先检查：

- 是否开了多个 `python main.py` 进程。
- 微信轮询是否被多个实例同时启动。

### 14.3 搜索或天气没生效

模型会自行决定是否调用工具。可以明确说：

```text
你先上网查一下，再告诉我
```

如果工具卡住，`mcp_client.py` 里有 MCP 超时保护，`builtin_tools.py` 里的 HTTP 请求也有超时。

### 14.4 图片没有被模型看见

检查：

- 图片是不是纯图片消息。如果是，必须再发一句文字要求。
- 终端是否出现 `Buffered ... image(s)`。
- `media/wechat/...` 下是否保存了图片文件。
- 历史里是否出现 `【图片】` 摘要。

## 15. 上传 GitHub 前清理

这个项目有不少“本地运行态数据”，不应该直接上传到公开仓库。

建议保留并上传：

- 核心源码：`main.py`、`agent.py`、`builtin_tools.py`、`mcp_client.py`、`wechat.py`、`wechat_accounts.py`、`skill_manager.py`、`image_utils.py`、`sticker_utils.py`、`scheduler.py`、`history.py`、`models.py`、`model_configurator.py`、`config.py`、`wizard.py`。
- 文档：`README.md`、`PROJECT_GUIDE.md`、`IMPLEMENTATION_LOGIC.md`、`PROMPTS.md`。
- 依赖：`requirements.txt`。
- 示例配置：建议用 `config.example.json`、`mcp_servers.example.json`、`models/example.json` 形式上传，不上传真实密钥。
- 非隐私资源：确认不含个人信息的 `skills/` 和 `sticker/` 可以上传。

建议排除或清理：

- `__pycache__/`、`*.pyc`：Python 缓存，完全不需要。
- `history/`：聊天记录、联系人上下文、测试上下文，包含隐私。
- `media/`：微信图片和媒体缓存，包含隐私且体积会变大。
- `models/*.json`、`models/.selected`：模型配置通常包含 API Key。
- `scheduler_jobs.json`：如果写了私人主动聊天任务或 prompt，不要上传真实版本。
- `config.json`、`mcp_servers.json`：如果包含私有路径、私有 server 或敏感 prompt，改成 example 后再上传。
- `skills/.claude/skills/create-ex/.git/`：嵌套 Git 仓库元数据，对运行无用。
- `history/__llm_tool_test__/`、`history/__skill_weather_test__/`：测试残留，可直接删除。

如果只是准备公开仓库，最稳妥的结构是：源码 + 文档 + 示例配置 + 不含隐私的 skill/sticker 资源；所有账号、聊天、媒体、密钥都留在本地。
