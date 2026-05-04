# GitHub 上传指南

本文档说明如何把本项目上传到 GitHub，并避免把微信账号、聊天记录、图片、API Key 等本地隐私数据传上去。

## 1. 上传前检查

先确认当前目录是项目根目录：

```powershell
cd C:\Users\Lisc\Desktop\Agent
```

查看 Git 状态：

```powershell
git status
```

重点确认这些内容不要上传：

```text
history/
media/
models/*.json
models/.selected
config.json
mcp_servers.json
scheduler_jobs.json
__pycache__/
*.pyc
```

其中 `history/` 现在包含：

```text
history/openclaw-state/openclaw-weixin/
history/WECHAT_ACCOUNTS_OVERVIEW.md
```

这里面有微信机器人账号状态和本地聊天信息，必须留在本机，不要上传。

## 2. 检查 .gitignore

确认 `.gitignore` 已经包含这些规则：

```gitignore
history/
media/
models/*.json
models/.selected
config.json
mcp_servers.json
scheduler_jobs.json
__pycache__/
*.pyc
```

检查某个文件是否会被 Git 忽略：

```powershell
git check-ignore -v history/openclaw-state/openclaw-weixin/accounts.json
git check-ignore -v models/example.json
```

如果第一条有输出，说明 `history/` 会被忽略。  
如果第二条没有输出，说明示例模型配置可以被上传。

## 3. 初始化仓库

如果还没有初始化 Git：

```powershell
git init
```

设置默认分支名：

```powershell
git branch -M main
```

如果已经初始化过，可以跳过这一步。

## 4. 创建 GitHub 空仓库

在 GitHub 网页创建一个空仓库，例如：

```text
https://github.com/YDXDJL/Wechat-AI-Partner
```

创建时建议：

- 不要勾选自动生成 README。
- 不要勾选 .gitignore。
- 不要勾选 License，除非你已经决定开源协议。

这样本地仓库推送时不会产生冲突。

## 5. 绑定 remote

使用你的仓库地址：

```powershell
git remote add origin https://github.com/YDXDJL/Wechat-AI-Partner.git
```

如果已经绑定过 remote，需要先查看：

```powershell
git remote -v
```

如果地址不对，可以改成：

```powershell
git remote set-url origin https://github.com/YDXDJL/Wechat-AI-Partner.git
```

## 6. 查看将要上传的文件

先查看未跟踪和已修改文件：

```powershell
git status --short
```

查看将被提交的文件列表：

```powershell
git add --dry-run .
```

如果看到 `history/`、`media/`、真实模型配置、API Key 文件，先停下来检查 `.gitignore`。

## 7. 暂存文件

确认没有隐私文件后：

```powershell
git add .
```

再检查一次：

```powershell
git status --short
```

## 8. 提交

第一次提交可以写：

```powershell
git commit -m "Initial WeChat AI partner agent"
```

之后更新可以按内容写，例如：

```powershell
git commit -m "Add WeChat account management"
git commit -m "Add sticker matching and image input"
git commit -m "Update project documentation"
```

## 9. 推送到 GitHub

第一次推送：

```powershell
git push -u origin main
```

以后更新：

```powershell
git push
```

## 10. 如果 GitHub 要登录

HTTPS 推送时，GitHub 通常要求使用 Personal Access Token，而不是密码。

创建 Token：

1. 打开 GitHub。
2. 进入 `Settings`。
3. 进入 `Developer settings`。
4. 进入 `Personal access tokens`。
5. 创建一个 token。
6. 权限至少需要能 push 当前仓库。

推送时：

- Username 填 GitHub 用户名。
- Password 填 token，不是 GitHub 登录密码。

## 11. 后续更新流程

每次改完代码后：

```powershell
git status --short
git add .
git commit -m "Describe your change"
git push
```

如果只是想看改了什么：

```powershell
git diff
```

如果想看已经暂存的内容：

```powershell
git diff --cached
```

## 12. 误上传隐私文件怎么办

如果还没有 push，只需要取消暂存：

```powershell
git restore --staged <file>
```

然后把文件加入 `.gitignore`。

如果已经 push 到 GitHub：

1. 立即删除或重置泄露的 API Key、微信 token。
2. 从仓库删除文件。
3. 如果泄露很严重，需要清理 Git 历史。

普通删除命令：

```powershell
git rm --cached <file>
git commit -m "Remove private file"
git push
```

注意：`git rm --cached` 只是不再追踪文件，不会删除本地文件。

## 13. 推荐上传结构

建议上传：

```text
README.md
PROJECT_GUIDE.md
IMPLEMENTATION_LOGIC.md
PROMPTS.md
GITHUB_UPLOAD_GUIDE.md
main.py
agent.py
builtin_tools.py
mcp_client.py
wechat.py
wechat_accounts.py
skill_manager.py
image_utils.py
sticker_utils.py
scheduler.py
history.py
models.py
model_configurator.py
config.py
wizard.py
requirements.txt
skills/
sticker/
models/example.json
config.example.json
mcp_servers.example.json
```

不要上传：

```text
history/
media/
models/*.json
models/.selected
config.json
mcp_servers.json
scheduler_jobs.json
__pycache__/
```

