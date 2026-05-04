# Micro Agent 完整实现逻辑说明

本文档面向“要维护或继续开发这个项目的人”，按代码运行链路解释每个模块如何配合。

## 1. 总体执行入口

项目从 `main.py` 的 `main()` 开始。

核心流程：

```text
main()
  -> 读取配置
  -> 连接 MCP
  -> 加载微信账号
  -> 加载 skills
  -> 加载模型配置
  -> 创建 Agent
  -> 启动 scheduler
  -> 进入 while True 主循环
       -> 处理微信消息
       -> 处理本地终端输入
```

`main.py` 是协调层，不直接调用模型 API。真正调用模型的是 `agent.py` 里的 `Agent.run()`。

## 2. 配置加载逻辑

### 2.1 config.json

由 `config.py` 的 `load_agent_config()` 读取。

字段：

```json
{
  "system_prompt": "...",
  "max_tokens": 16000,
  "thinking": true,
  "max_tool_rounds": 20
}
```

含义：

- `system_prompt`：基础系统提示。
- `max_tokens`：模型最大输出 token。
- `thinking`：是否开启 Claude/MiMo adaptive thinking。
- `max_tool_rounds`：最多允许多少轮工具调用。

### 2.2 models/

`models.py` 负责模型配置。

启动时：

1. 读取 `models/.selected`。
2. 根据 selected 名称加载对应 JSON。
3. 如果没有 selected，就从 `models/` 中取第一个。
4. 如果没有任何模型配置，运行 `wizard.py`。
5. 启动后可通过 `main.py` 内置命令 `/new-model`、`/model-list`、`/model <name|number>` 管理模型。

模型配置会生成 `ModelConfig`：

```python
ModelConfig(
    display_name=...,
    provider="claude" or "openai",
    base_url=...,
    model=...,
    api_key=...,
    provider_name=...,
)
```

运行时模型命令：

```text
/new-model
  -> 创建 ModelSetupSession
  -> 在 NonBlockingInput 主循环里逐行收集 provider/base_url/model/api_key/display_name
  -> 保存到 models/<safe_name>.json
  -> 写入 models/.selected
  -> 调用 switch_runtime_model()

/model-list
  -> list_models(MODELS_DIR)
  -> format_model_list_text()

/model <name|number>
  -> resolve_model_selector()
  -> switch_runtime_model()
```

`switch_runtime_model()` 会更新当前 `model_config`、切换终端 agent 的模型、保存 `.selected`，并清空 `sender_agents`。这样后续新建的微信联系人 Agent 会使用新模型，避免旧缓存继续拿旧模型配置。

### 2.3 mcp_servers.json

`config.py` 的 `load_mcp_servers()` 读取 MCP server 列表。

`mcp_client.py` 负责连接：

```python
mcp_manager.connect_all_sync(mcp_configs)
```

当前项目还内置了工具，即使没有外部 MCP server，也能使用：

```text
Read / Write / Edit / Bash / CurrentTime / WebSearch / WebFetch / Weather
```

### 2.4 scheduler_jobs.json

`load_scheduler_jobs()` 读取定时任务配置，`scheduler.py` 负责运行。

## 3. Agent 创建逻辑

`main.py` 里有两个关键函数：

```python
history_scope(sender_id=None)
make_agent(sender_id=None)
```

### 3.1 history_scope

决定历史保存位置：

- 没有微信账号：普通终端历史。
- 有微信账号：账号级历史。
- 有微信 sender：账号 + sender 独立历史。

逻辑：

```text
active_account_id + sender_id -> history/<account_name>/<sender>/conversation.json
active_account_id only        -> history/<account_name>/conversation.json
none                          -> history/conversation.json
```

### 3.2 make_agent

创建 `Agent`：

```python
Agent(agent_config, model_config, mcp_manager, BASE_DIR, account_id=history_scope(sender_id))
```

然后调用：

```python
apply_bound_skill(target)
```

如果当前微信账号绑定了 skill，创建出来的 Agent 会自动应用该 skill。

### 3.3 sender_agents

微信每个联系人一个独立 Agent：

```python
sender_agents: dict[str, Agent] = {}
```

通过：

```python
get_sender_agent(sender_id)
```

获取或创建。

这样不同微信联系人之间上下文互不污染。

## 4. Agent.run 完整逻辑

`Agent.run(user_input)` 是核心。

流程：

```text
Agent.run(user_input)
  -> self.messages.append(user message)
  -> 获取工具列表
  -> 根据 provider 选择 _run_claude 或 _run_openai
  -> 如果 persistent skill 存在，清理 meta-commentary
  -> 清理图片 base64 和 thinking
  -> save_history()
  -> 返回最终文本
```

### 4.1 工具列表

如果 skill 限制了 allowed tools：

```python
self.mcp.get_claude_tools_filtered(self._allowed_tools)
```

否则：

```python
self.mcp.get_claude_tools()
```

`mcp_client.py` 会隐藏微信发送工具：

```text
wechat_reply
wechat_typing
wechat_send_file
```

原因：模型不能自己发微信，否则会绕过外层统一发送逻辑，导致重复发送或最终文本错乱。

### 4.2 Claude/MiMo 调用

`_run_claude()` 会循环调用模型：

```text
发送 messages + system + tools
  -> 模型返回
  -> 如果 end_turn，结束
  -> 如果 tool_use，调用工具
  -> 把 tool_result 作为 user message 追加
  -> 再调用模型
```

最多循环 `max_tool_rounds` 次。

### 4.3 OpenAI 调用

`_run_openai()` 会把内部消息格式转换为 OpenAI Chat Completions 格式。

特殊处理：

- Claude `tool_use` 转 OpenAI `tool_calls`。
- Claude `tool_result` 转 OpenAI `tool` message。
- Claude 图片 block 转 OpenAI `image_url` data URL。

### 4.4 System prompt 组成

`_get_system_prompt()` 逻辑：

```text
如果有 skill extra_instructions:
  skill prompt + Reality-check/tool-use instructions
否则:
  config system_prompt + Reality-check/tool-use instructions
```

Reality-check 规则要求模型：

1. 先判断自己知识和上下文是否够。
2. 够就直接回答。
3. 不够再调用时间、天气、搜索、网页抓取等工具。
4. 工具后仍保持当前 skill 人设。

## 5. 历史读写逻辑

`history.py` 负责：

```python
load_history()
save_history()
clear_history()
get_history_info()
```

`clear_history()` 只会把 `conversation.json` 写成空消息列表，不删除目录或文件。这样 `/clear` 后当前微信联系人仍然可以继续被定位。

### 5.1 保存路径

由账号显示名生成的 `history_key` 决定。一般情况下目录名就是用户给账号起的名字；如果多个账号重名，会追加短 account id，避免混用历史。

```python
history/<account_name>/conversation.json
```

微信联系人场景下，`account_id` 实际会被设置成：

```text
<account_name>/<sender_id>
```

所以保存到：

```text
history/<account_name>/<sender_id>/conversation.json
```

### 5.2 保存前清理

`Agent._sanitize_image_blocks_for_history()` 负责：

- 去掉 thinking block。
- 把图片 base64 替换成 `【图片】` 摘要。
- 避免保存大体积图片数据。

`Agent._normalize_legacy_history()` 负责加载旧历史时清理：

- 删除旧的 `wechat_typing`。
- 将旧的 `wechat_reply` 工具内容恢复成 assistant 文本。
- 删除旧的 `sent` tool_result。
- 删除 thinking。
- 删除旧模式造成的重复短 assistant 文本。

## 6. MCP 与内置工具逻辑

### 6.1 mcp_client.py

`MCPManager` 同时管理：

- 外部 MCP server tools。
- 内置 tools。

启动时：

```python
_register_builtin_tools()
```

外部 MCP 通过 stdio 连接。

工具调用：

```python
call_tool(name, arguments)
```

如果是内置工具，直接调用 `builtin_tools.execute_builtin_tool()`。

如果是外部工具，走 MCP session：

```python
session.call_tool(original_name, arguments=arguments)
```

MCP 调用有超时保护，避免主循环被工具卡住。

### 6.2 builtin_tools.py

内置工具：

```text
Read
Write
Edit
Bash
CurrentTime
WebSearch
WebFetch
Weather
```

实现逻辑：

- `Read`：读取文本、PDF；图片只返回文件说明。
- `Write`：覆盖写文件。
- `Edit`：按 exact string 替换。
- `Bash`：执行 shell 命令。
- `CurrentTime`：用 `zoneinfo` 获取当前时间。
- `WebSearch`：请求 DuckDuckGo HTML 搜索页并解析结果。
- `WebFetch`：抓取网页并用 HTMLParser 抽取正文。
- `Weather`：请求 `wttr.in/<location>?format=j1` 获取天气。

## 7. 微信账号逻辑

### 7.1 启动默认账号

`main.py` 调用：

```python
load_default_wechat_account(account_manager)
```

`wechat_accounts.py` 的 `get_default_account()` 会：

1. 读取上次选择的 account id。
2. 如果有效，加载该账号。
3. 如果没有，读取第一个已保存账号。
4. 如果没有任何账号，提示使用 `/wechat-new`。

### 7.2 查看账号和上下文

`/wechat-list` 和 `/wechat-account` 会调用 `account_manager.list_accounts()`，并读取：

```text
history/<account_name>/conversation.json
history/<account_name>/<sender_id>/conversation.json
```

输出每个已保存微信账号的：

- 账号显示名
- account id
- 绑定 skill
- 总消息数

账号显示名、当前账号、账号凭据和 skill 绑定现在统一保存在项目 `history` 目录内：

```text
history/openclaw-state/openclaw-weixin/
```

同一个文件里还会保存稳定的 `history_key`，防止重命名或删除重名账号后历史目录突然变化。

启动时 `wechat_accounts.py` 会设置：

```text
OPENCLAW_STATE_DIR=history/openclaw-state
```

因此 `wechat_clawbot` 也会从项目内的 `history/openclaw-state/openclaw-weixin` 读写账号凭据。首次启动会把旧的 `~/.openclaw/openclaw-weixin` 内容复制进来。

账号总览文档自动生成在：

```text
history/WECHAT_ACCOUNTS_OVERVIEW.md
```

### 7.3 扫码切换账号

`/wechat-new` 调用：

```python
switch_wechat_account()
```

流程：

```text
如果正在轮询，先停止
调用 account_manager.qr_login()
扫码成功后保存账号
设置 active_account_id
重建 WeChatClient
rebuild_agents(clear_context=False)
必要时恢复微信轮询
```

扫码成功后会提示用户输入账号显示名；空输入则使用 account id 作为显示名。

### 7.4 切换默认微信机器人账号

`/wechat-switch` 不扫码，按账号名、account id 或列表序号切换当前默认微信机器人账号。

```text
/wechat-switch
  -> 列出已有微信机器人账号

/wechat-switch <name|account_id|number>
  -> 解析账号
  -> account_manager.set_last_account_id(account_id)
  -> 重建 WeChatClient 并连接该账号
  -> 清空运行态 sender 缓存和 pending 图片
```

`last_wechat_sender` 仍用于内部记录最近联系人，但不再作为 `/wechat-switch` 的手动切换目标。

### 7.5 删除微信机器人账号

`/wechat-delete [name|account_id|number]` 删除指定账号；不传参数时删除当前默认账号。

执行内容：

```text
删除 wechat_clawbot 保存的账号凭据
删除 account-aliases.json 中的账号名和 history_key
删除 skill-bindings.json 中的绑定 skill
删除 history/<account_name> 本地历史目录
如果删的是当前账号，自动尝试切换到下一个可用账号
```

### 7.6 skill 绑定

绑定信息保存在：

```text
history/openclaw-state/openclaw-weixin/skill-bindings.json
```

`/bind <skill>`：

```text
解析 skill
设置当前 agent persistent skill
设置所有 sender agent persistent skill
account_manager.bind_skill(active_account_id, skill.name)
```

启动或创建新 sender agent 时，`apply_bound_skill()` 会自动恢复。

## 8. 微信消息接收逻辑

`wechat.py` 的 `WeChatClient` 负责接收。

### 8.1 连接

```python
wechat_client.connect(account_data)
```

把账号 token/base_url 写入 `WeixinApiOptions`。

### 8.2 开始轮询

```python
wechat_client.start_polling()
```

创建后台 event loop 和 daemon thread，运行：

```python
_poll_loop()
```

### 8.3 轮询过程

`_poll_loop()`：

```text
get_updates()
  -> 遍历消息
  -> 只处理 MessageType.USER
  -> 用 message_id/seq 去重
  -> body_from_item_list() 抽文本
  -> _download_images() 下载图片
  -> 更新 context_token
  -> 构造 WeChatMessage
  -> 放入队列
```

`main.py` 主循环再通过：

```python
wechat_client.get_message(block=False)
```

取出消息。

## 9. 微信图片逻辑

### 9.1 下载图片

`wechat.py` 的 `_download_images()` 遍历 `msg.item_list`。

如果 item 是图片：

```python
download_media_from_item()
```

下载并解密后，保存到：

```text
media/wechat/<account_id>/<sender_id>/
```

然后生成：

```python
ImageAttachment(
    path=...,
    mime_type=...,
    size=...,
    source="wechat",
    message_id=...
)
```

### 9.2 纯图片先缓存

`main.py` 主循环中：

```python
if msg.images and not msg.content.strip():
    pending_wechat_images.setdefault(msg.sender_id, []).extend(msg.images)
    continue
```

这意味着：

- 不调用模型。
- 不回复微信。
- 只等下一句文字要求。

### 9.3 下一句文字合并图片

下一条文字消息到达后：

```python
images_for_turn = []
if pending_wechat_images.get(msg.sender_id):
    images_for_turn.extend(pending_wechat_images.pop(msg.sender_id))
if msg.images:
    images_for_turn.extend(msg.images)
```

然后调用：

```python
build_wechat_channel_message(..., images_for_turn)
```

这会返回多模态 content block：

```text
text block
【图片】摘要 text block
image block(base64)
```

### 9.4 历史里的图片

模型调用时使用真实图片。

保存历史时，真实图片 block 被替换成摘要。

如果前面已经有 `【图片】` 摘要，就不再额外生成重复的 omitted 占位。

## 10. 微信消息处理逻辑

`main.py` 主循环中，微信消息处理大致如下：

```text
while wechat_mode:
  msg = wechat_client.get_message()
  if no msg: break

  last_wechat_sender = msg.sender_id
  chat_agent = get_sender_agent(msg.sender_id)

  if image download failed:
      send error reply
      continue

  if pure image:
      buffer image
      continue

  if command:
      handle command
      continue

  merge pending images
  channel_msg = build_wechat_channel_message(...)
  start_typing()
  response = chat_agent.run(channel_msg)
  stop_typing()
  send_wechat_reply()
```

关键点：

- 微信联系人按 `sender_id` 使用独立 Agent。
- skill 人设不会因为图片或工具调用丢失。
- 微信回复只走 `send_wechat_reply()`。
- 模型不可见微信发送工具，避免重复发送。

## 11. build_wechat_channel_message 逻辑

函数：

```python
build_wechat_channel_message(sender, sender_id, content, images=None)
```

输出：

### 11.1 没有图片

返回字符串：

```xml
<channel source="wechat" sender="..." sender_id="...">用户文本</channel>
<delivery>微信回复拆分说明</delivery>
```

### 11.2 有图片

返回多模态 list：

```python
[
  {"type": "text", "text": "<channel ...>...</channel>..."},
  {"type": "text", "text": "【图片】用户发送了一张图片..."},
  {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
]
```

这样模型同时看到：

- 微信 sender 信息。
- 用户文字要求。
- 当前回复拆分规则。
- 图片摘要。
- 真实图片内容。

## 12. 微信回复发送逻辑

模型返回文本后，`main.py` 调用：

```python
send_wechat_reply(wechat_client, sender_id, response)
```

### 12.1 拆句

```python
split_wechat_reply(text, max_parts=4, soft_limit=42)
```

规则：

1. 空文本返回空列表。
2. 如果模型显式输出多行，按行拆，最多 4 条。
3. 如果文本长度小于等于 42，发一条。
4. 否则按句号、问号、感叹号、省略号等标点拆。
5. 超过 4 条时，前 3 条独立，剩余合并为第 4 条。

### 12.2 发送

每条之间会 sleep 一小段时间，模拟真人连续发送：

```python
time.sleep(min(1.2, max(0.35, len(part) / 35)))
```

真正发送：

```python
wechat_client.send_reply(sender_id, part)
```

`wechat.py` 的 `send_reply()` 有短时间重复发送检测。

## 13. 本地输入处理逻辑

终端输入由 `NonBlockingInput` 后台线程读取，主循环每 0.5 秒取一次。

如果是 slash command：

```text
/help
/new-model
/model-list
/model <name|number>
/wechat
/bind
...
```

走命令分支。

如果不是命令：

- 若微信模式开启且有 `last_wechat_sender`：路由到微信联系人 Agent，并把回复发微信。
- 否则：发送给普通终端 Agent，回复打印到终端。

如果终端普通文本路由到微信联系人，且该联系人有 pending 图片，也会把 pending 图片一起带给模型。

## 14. 本地 /image 逻辑

`/image <path> [prompt]` 调用：

```python
run_local_image_command()
```

流程：

```text
parse_image_command()
load_image_attachment()
if wechat_mode and last_wechat_sender:
    合并该 sender pending 微信图片
    再追加本地图片
    作为微信联系人输入
else:
    作为普通终端 Agent 输入
```

本地图片和微信图片共用 `image_utils.py` 的多模态构造逻辑。

## 15. Skill 加载和执行逻辑

### 15.1 加载

`SkillManager(BASE_DIR)` 初始化时扫描：

```text
skills/
.claude/skills/
```

只要目录里有 `SKILL.md`，就加载。

额外资源：

```text
*.md
*.txt
*.json
```

会按优先级注入：

```text
persona.md
memory.md
examples.md
meta.json
其他
```

### 15.2 Prompt 构造

`get_skill_prompt()` 返回完整 system prompt。

内容包括：

- 当前角色是谁。
- 禁止输出旁白、解释、meta-commentary。
- 微信多条短消息输出规则。
- SKILL.md 主体。
- 附加资源内容。

`Agent._get_system_prompt()` 会在 skill prompt 后追加工具使用规则。

### 15.3 激活微信 skill

`activate_skill_for_wechat()`：

```text
确保有 active_account_id
确保微信模式开启
读取 skill prompt
设置主 agent persistent skill
设置所有 sender agent persistent skill
绑定到账号
寻找目标 sender
如果 sender 无上下文，主动发开场
如果已有上下文，等待用户下一条消息
```

## 16. 账号切换和上下文清理

`/wechat-new` 后：

```python
rebuild_agents(clear_context=False)
```

执行：

```text
重建主 agent
清空 sender_agents
清空 pending_wechat_images
last_wechat_sender = None
```

这保证换微信账号后不会继承旧账号的运行态联系人和图片缓存；已保存历史上下文不会被删除。

## 17. 一条微信文字消息的完整生命周期

示例：用户发“今天广州天气怎么样？”

```text
微信用户发消息
  -> wechat.py get_updates 拉取消息
  -> body_from_item_list 提取文本
  -> WeChatMessage 入队
  -> main.py 从队列取出
  -> get_sender_agent(sender_id)
  -> build_wechat_channel_message()
  -> Agent.run()
  -> _run_claude()
  -> 模型判断需要实时天气
  -> tool_use Weather
  -> builtin_tools._weather()
  -> tool_result 回到 messages
  -> 模型按 skill 人设组织回复
  -> Agent 清理 thinking/图片
  -> save_history()
  -> send_wechat_reply()
  -> split_wechat_reply()
  -> wechat_client.send_reply()
```

## 18. 一张微信图片的完整生命周期

示例：用户先发图片，再发“这是什么？”

```text
用户发图片
  -> wechat.py 识别 image item
  -> download_media_from_item 下载解密
  -> 保存到 media/wechat/...
  -> 构造 ImageAttachment
  -> WeChatMessage 入队
  -> main.py 发现纯图片
  -> pending_wechat_images[sender_id].append(image)
  -> 不回复

用户发“这是什么？”
  -> main.py 取出文字消息
  -> images_for_turn 取出 pending 图片
  -> build_wechat_channel_message(text + images)
  -> build_multimodal_content()
  -> Agent.run()
  -> 模型看图并回答
  -> 历史保存为 【图片】 摘要
  -> send_wechat_reply()
```

## 19. 一个现实搜索问题的完整生命周期

示例：用户发“帮我搜几家广州好吃的日料店”

```text
用户消息进入 Agent
  -> system prompt 要求模型先判断上下文是否足够
  -> 如果上下文不足，模型发起 WebSearch
  -> WebSearch 返回搜索结果
  -> 模型可继续 WebFetch 打开网页
  -> WebFetch 返回正文
  -> 模型总结结果
  -> 回复保持当前 skill 人设
  -> 微信统一拆句发送
```

注意：程序不再通过关键词提前搜索，是否调用工具由模型决定。

## 20. 开发注意事项

### 20.1 不要绕过 send_wechat_reply

所有微信回复必须走：

```python
send_wechat_reply()
```

否则会破坏：

- 拆句。
- 重复发送防护。
- 终端日志打印。

### 20.2 不要把 wechat_reply 暴露给模型

模型如果能直接调用微信发送工具，会出现：

- 模型自己发一段。
- 外层主循环又发最终文本。
- 历史中出现重复或错乱。

所以 `mcp_client.py` 隐藏 `wechat_` 前缀工具。

### 20.3 图片不要保存 base64 到历史

base64 会迅速撑爆上下文和历史文件。正确做法：

- 当前轮请求发送真实图片。
- 保存历史时只保留 `【图片】` 摘要。

### 20.4 Skill prompt 优先，但工具规则仍要追加

Skill 负责“身份和说话方式”。

工具规则负责“什么时候查现实信息”。

所以 `_get_system_prompt()` 不能简单只返回 skill prompt，必须追加 `_TOOL_USE_INSTRUCTIONS`。

### 20.5 微信图片纯图不应立即回答

用户发纯图片时常常还没说需求。当前设计是等下一句文字。

不要把纯图直接送模型，否则模型会猜用户意图，体验不好。

## 21. 推荐测试清单

改动后建议测试：

```text
python -m py_compile main.py agent.py wechat.py image_utils.py builtin_tools.py mcp_client.py
```

功能测试：

- 终端普通聊天。
- `/new-model` 能新增模型配置。
- `/model-list` 能扫描并列出模型。
- `/model <name|number>` 能切换模型，后续微信 sender agent 使用新模型。
- `/wechat` 开启微信。
- 微信文字只回复一次。
- 微信复杂回复可拆成多条。
- `/ex-xia-yizhou` 激活 skill 后微信回复保持人设。
- 微信发纯图片不立即回复。
- 微信发图后再发“这是什么”，模型能看图。
- 历史里图片为 `【图片】` 摘要。
- 问当前时间，模型调用 `CurrentTime`。
- 问天气，模型调用 `Weather`。
- 问餐厅/新闻，模型按需调用 `WebSearch/WebFetch`。
- `/wechat-new` 后运行态 sender 缓存和 pending 图片清空；已保存历史上下文不删除。

## 22. GitHub 上传前清理逻辑

项目目录里有三类内容：

```text
源码/文档：应该上传
示例配置/资源：确认不含隐私后可以上传
运行态数据/密钥/缓存：不应该上传
```

建议上传：

- `*.py` 核心源码。
- `README.md`、`PROJECT_GUIDE.md`、`IMPLEMENTATION_LOGIC.md`、`PROMPTS.md`。
- `requirements.txt`。
- 不含个人隐私的 `skills/`、`sticker/`。
- 示例配置文件，例如 `config.example.json`、`mcp_servers.example.json`、`models/example.json`。

建议排除：

- `__pycache__/`、`*.pyc`：自动生成缓存。
- `history/`：聊天上下文、联系人记录、测试历史。
- `media/`：微信图片和下载媒体。
- `models/*.json`、`models/.selected`：通常包含 API Key 或真实模型账号信息。
- `scheduler_jobs.json`：可能包含私人 prompt。
- `config.json`、`mcp_servers.json`：如果包含真实私有配置，改成 example 后再提交。
- `skills/.claude/skills/create-ex/.git/`：嵌套 Git 元数据，无运行价值。

当前目录中最明确不需要上传的是：

```text
__pycache__/
history/__llm_tool_test__/
history/__skill_weather_test__/
skills/.claude/skills/create-ex/.git/
media/
```

`history/` 和 `media/` 是否删除取决于是否还要保留本地上下文；但它们不应该进 GitHub。

