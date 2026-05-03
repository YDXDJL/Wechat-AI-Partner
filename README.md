# Micro Agent

一个支持多 LLM 提供商、MCP 工具、微信集成、技能系统和定时任务的微型 AI Agent 框架。

## 项目需求

1. **多提供商支持** — 同时支持 Claude (Anthropic) 和 OpenAI 格式的 API，可随时切换
2. **MCP 工具集成** — 通过 Model Context Protocol 连接外部工具服务器
3. **微信集成** — 直接对接微信 iLink Bot API，支持多账号、独立聊天记录
4. **技能/人设系统** — 兼容 Claude Code 的 SKILL.md 格式，可将任意角色蒸馏为 AI 人格
5. **定时任务** — 基于 cron 表达式的定时执行
6. **持久化存储** — 聊天记录、模型配置、账号凭证均持久化到本地

## 功能概览

| 功能 | 说明 |
|------|------|
| 双提供商 | Claude (Anthropic) + OpenAI 兼容格式，`/new-model` 新增，`/model-list` 扫描，`/model <name|number>` 切换 |
| MCP 工具 | stdio 传输，JSON-RPC 2.0，自动工具发现 |
| 内置工具 | Read / Write / Edit / Bash / CurrentTime / Weather / WebSearch / WebFetch |
| 微信集成 | 长轮询收发消息，打字指示器，文件发送，context_token 管理 |
| 多账号 | 扫码登录，独立聊天记录，账号-技能绑定 |
| 技能系统 | SKILL.md 格式，YAML 前置元数据，角色人设注入 |
| 图片理解 | 微信图片缓存后等待下一句要求，本地图片可用 `/image` 发送 |
| 表情包 | 启动时读取 `sticker/sticker_introduction.md`，按角色语气和场景动态匹配 |
| 定时任务 | APScheduler CronTrigger，每个任务独立 Agent 实例 |
| 聊天历史 | JSON 持久化，按账号隔离存储 |

## 架构

```
main.py              CLI 入口，主循环
  |
  +-- config.py        配置加载 (AgentConfig, MCPServerConfig, SchedulerJobConfig)
  +-- models.py        模型配置管理 (ModelConfig)
  +-- model_configurator.py  独立模型配置工具
  +-- wizard.py        首次模型配置向导（兼容旧流程）
  +-- agent.py         核心 Agent (双提供商，tool loop，skill 注入，meta-commentary 过滤)
  +-- mcp_client.py    MCP 服务器连接 + 工具调度
  +-- builtin_tools.py 内置工具实现 (文件、命令、时间、天气、网页搜索/抓取)
  +-- skill_manager.py SKILL.md 加载，角色 prompt 构建
  +-- sticker_utils.py 表情包读取与匹配
  +-- image_utils.py   图片附件与多模态内容构造
  +-- scheduler.py     APScheduler 定时任务
  +-- history.py       JSON 聊天持久化 (按账号隔离)
  +-- wechat.py        微信 API 客户端 (长轮询)
  +-- wechat_accounts.py  多账号管理，扫码登录，技能绑定
```

## 安装

```bash
pip install anthropic openai mcp apscheduler PyYAML
pip install wechat_clawbot  # 微信集成
```

## 快速开始

```bash
cd Agent
python main.py
```

首次启动如果没有任何模型配置，会自动进入模型设置向导。启动后也可以在终端中使用 `/new-model` 新增模型。

### 配置文件

- `config.json` — Agent 配置 (system_prompt, max_tokens, thinking)
- `mcp_servers.json` — MCP 服务器列表
- `scheduler_jobs.json` — 定时任务
- `models/` — 模型配置目录，每个 JSON 文件一个配置

## 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助，并列出当前可用技能命令 |
| `/quit` / `/exit` | 退出 |
| `/reset` | 清空对话历史 |
| `/clear` | 清空当前上下文 |
| `/history` | 查看历史信息 |
| `/new-model` | 在终端中新建模型配置 |
| `/model-list` | 扫描并列出所有模型配置 |
| `/model <name|number>` | 切换模型 |
| `/wechat` | 开关微信模式 |
| `/wechat switch` | 扫码切换到新微信账号，保存为下次默认账号，并清空该账号上下文 |
| `/image <path> [prompt]` | 发送本地图片给 Agent；微信模式下会路由到最近微信联系人 |
| `/account` | 查看当前微信账号 |
| `/skills` | 列出可用技能 |
| `/skills show <skill>` | 查看技能详情、别名和已加载资源 |
| `/skills search <query>` | 按名称、别名、描述搜索技能 |
| `/skills reload` | 重新扫描并加载技能 |
| `/<skill> [args]` | 开启该技能的微信人格会话 |
| `/bind <skill>` | 绑定技能到当前账号 |
| `/unbind` | 解绑技能 |
| `/schedule add <id> "<cron>" "<prompt>"` | 添加定时任务 |
| `/schedule remove <id>` | 删除定时任务 |
| `/schedule list` | 列出所有定时任务 |

## 技能系统

兼容 Claude Code 的 SKILL.md 格式。将 SKILL.md 文件放入 `skills/` 或 `.claude/skills/` 目录即可自动加载。

技能以“目录”为单位加载：除了 `SKILL.md`，同目录及子目录中的 `persona.md`、`memory.md`、`examples.md`、`meta.json`、`.txt`、`.md`、`.json` 等文本资料也会自动作为附加资料注入 prompt。这样角色设定、长期记忆、示例对话可以拆文件维护，不必全部塞进一个 `SKILL.md`。

### SKILL.md 格式

```markdown
---
name: xia-yizhou
description: 夏以昼 — 外冷内热的前男友人设
argument-hint: [对话内容]
user-invocable: true
allowed-tools: Read, Write, Bash
---

## 人设设定

夏以昼，男，25岁。性格外冷内热...

## 说话风格

- 简洁直接
- 偶尔毒舌但隐含关心
- 喜欢用省略号
```

### 账号-技能绑定

每个微信账号可以绑定一个技能，启动时自动加载：

```bash
/skills              # 查看技能、别名和资源数量
/skills show xia-yizhou
/skills search Xavier
/bind xia-yizhou     # 支持技能名、目录名、meta.json slug/english_name 等别名
/unbind              # 解绑
```

绑定后该账号的所有微信消息都会使用角色人设回复，技能 prompt 持久化到 Agent 状态中。

直接输入 `/<skill>` 也会自动开启微信模式并激活该技能：

- 如果最近微信联系人已有上下文，只切换人格并等待对方下一条消息
- 如果最近微信联系人没有上下文，会主动发送一条符合身份的开场消息
- 开启后，本地普通输入会继续通过微信发送给最近联系人

## 微信集成

### 启动流程

1. 启动时自动使用上一次的微信账号；如果没有记录，则使用第一个已保存账号
2. 输入 `/wechat` 开启微信模式
3. 微信消息自动进入 Agent 处理
4. 需要切换账号时输入 `/wechat switch` 扫码登录，新账号会保存为下次默认账号，并清空该账号上下文

### 消息处理流程

```
微信消息 → 长轮询获取 → 去重 (message_id/seq)
  → 格式化为 channel 消息
  → 按 sender_id 选择独立 Agent/聊天历史
  → Agent 处理 (可能触发 tool use)
  → 发送回复 (带 context_token)
```

### 微信命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示微信可用命令和当前可用技能命令 |
| `/model-list` | 列出模型 |
| `/model <name|number>` | 切换模型 |
| `/new-model` | 微信侧不可新增模型，会提示回到终端操作 |
| `/reset` | 重置对话 |
| `/clear` | 清空当前联系人上下文 |
| `/skills` | 列出可用技能 |
| `/<skill> [args]` | 开启该技能的人格会话 |

## MCP 工具

在 `mcp_servers.json` 中配置 MCP 服务器：

```json
{
  "servers": [
    {
      "name": "my-server",
      "command": "python",
      "args": ["-m", "my_mcp_server"]
    }
  ]
}
```

内置工具始终可用，无需配置。当前包括文件读写编辑、命令执行、当前时间、天气、网页搜索和网页抓取。

## 定时任务

```bash
# 每天早上 9 点发送问候
/schedule add morning "0 9 * * *" "发送一条早安问候"

# 每小时检查一次
/schedule add check "0 * * * *" "检查是否有新消息需要回复"

/schedule list     # 查看所有任务
/schedule remove morning  # 删除任务
```

## 依赖

- `anthropic>=0.40.0`
- `openai>=1.0.0`
- `mcp>=1.27.0`
- `apscheduler>=3.10.0`
- `PyYAML>=6.0.0`
- `wechat_clawbot` (微信集成)
- `PyPDF2` (可选，PDF 读取)

## 上传 GitHub 前准备

以下内容不建议上传到公开仓库：

- `models/*.json` 和 `models/.selected`：包含模型名称和 API Key。建议只提交 `models/example.json`。
- `history/`：聊天上下文和用户隐私数据。
- `media/`：微信图片、下载媒体和运行缓存。
- `__pycache__/`、`*.pyc`：Python 缓存。
- `scheduler_jobs.json`：如果包含私人定时任务 prompt，建议不要上传真实文件。
- `config.json`、`mcp_servers.json`：如果写入了私人路径、私有 MCP server 或敏感系统 prompt，建议改成示例配置。
- `skills/.claude/skills/create-ex/.git/`：嵌套 Git 元数据，不需要上传。

推荐做法是提交源码、文档、`requirements.txt`、示例配置和不含隐私的 skill/sticker 资源；运行产生的账号、历史、媒体、模型密钥都留在本地。
