# Wechat-AI-Partner

一个偏向微信陪伴聊天的小型 Agent。它可以保持角色人设持续聊天，接收微信图片并结合下一句要求理解图片，也能根据聊天氛围自动发送合适的表情包。

## 主要特点

- **持续聊天**：按微信机器人账号和联系人保存独立上下文，重启后还能接着聊。
- **表情包发送**：启动时读取 `sticker/sticker_introduction.md`，根据角色心情、对话语气和使用场景选择表情包发到微信。
- **图片理解**：微信发来纯图片时先缓存，不立即瞎猜；等用户下一句要求后，把图片和文字一起交给模型处理。
- **角色 Skill**：支持 `skills/` 下的 `SKILL.md` 人设文件，可以用 `/xxx` 激活不同角色。
- **多句微信回复**：模型可以用换行拆成多条短消息，程序会像真人聊天一样连续发出。
- **现实信息工具**：模型可按需调用当前时间、天气、网页搜索和网页抓取工具。

## 快速开始

安装依赖：

```powershell
pip install -r requirements.txt
pip install wechat_clawbot
```

准备本地配置：

```powershell
copy config.example.json config.json
copy mcp_servers.example.json mcp_servers.json
copy scheduler_jobs.example.json scheduler_jobs.json
copy models\example.json models\mimo_v2.5.json
```

然后编辑 `models\mimo_v2.5.json`，把里面的 `YOUR_API_KEY_HERE` 换成自己的模型 API Key。

启动：

```powershell
python main.py
```

## 常用命令

```text
/new-model              新增模型配置
/model-list             列出模型配置
/model <name|number>    切换模型
/wechat                 开启或关闭微信模式
/wechat-list            列出微信机器人账号
/wechat-account         /wechat-list 的别名
/wechat-new             扫码新增/切换微信账号
/wechat-switch <name|account_id|number> 按名字、账号 ID 或序号切换微信机器人账号
/wechat-delete [name|account_id|number] 删除微信机器人账号和本地历史
/skills                 查看可用角色/技能
/<skill-name>           激活角色并进入微信聊天
/image <path> [prompt]  发送本地图片给 Agent
/clear                  清空当前上下文
/help                   查看帮助
```

## 表情包

表情包放在：

```text
sticker/
```

说明文件是：

```text
sticker/sticker_introduction.md
```

你可以通过修改这个 Markdown 文件来调整每张表情包的含义、情绪和使用场景。程序启动后会自动读取它，不需要把匹配规则写死在代码里。

## 图片聊天逻辑

微信侧：

```text
用户：发送图片
程序：缓存图片，不立即回复
用户：这张图里是什么？
程序：把图片 + 这句话一起发给模型，再回复微信
```

终端侧：

```powershell
/image "C:\path\to\image.png" 帮我看看这张图
```

如果当前处在微信模式，并且已有最近联系人，本地图片也会作为该联系人的输入，回复继续发回微信。

## Skill 角色

角色放在：

```text
skills/
```

一个角色通常包含：

```text
SKILL.md
persona.md
memory.md
examples.md
meta.json
```

启动后可以用：

```text
/skills
/<skill-name>
/bind <skill-name>
/unbind
```

激活角色后，图片、表情包、多句回复和现实信息工具都会继续保持当前角色身份。

## 不要上传的本地数据

这些内容已经在 `.gitignore` 中排除：

- `history/`：聊天历史、联系人上下文、微信机器人账号状态和本地总览。
- `history/openclaw-state/openclaw-weixin/`：微信机器人账号凭据、别名和绑定信息。
- `history/WECHAT_ACCOUNTS_OVERVIEW.md`：自动生成的微信账号总览，不打印 token，但仍属于本地状态。
- `media/`：微信图片和媒体缓存。
- `models/*.json`：真实模型配置和 API Key。
- `config.json`、`mcp_servers.json`、`scheduler_jobs.json`：本地真实配置。
- `__pycache__/`：Python 缓存。

仓库里只保留示例配置，例如 `models/example.json`。真实密钥和聊天记录请留在本地。
