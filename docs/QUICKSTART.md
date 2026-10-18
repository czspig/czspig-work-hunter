# 猪猪求职助手 上手指南

## 1. 准备环境

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 核心运行时 |
| Node.js | 22+ | 本地 Browser Runtime / CDP 代理 |
| Google Chrome | 最新稳定版 | 连接已登录的 BOSS 直聘 |
| AI API Key | — | 默认 DeepSeek，也支持 Anthropic、豆包或其他 OpenAI 兼容接口 |

采集岗位存在账号受限的风险。请仅用于个人求职，并保持低频。

## 2. 安装

前端产物 `dist` 不随仓库提交，需要先构建前端，再安装 Python 包：

```bash
git clone https://github.com/czspig/czspig-work-hunter.git
cd czspig-work-hunter
npm --prefix src/bosshunter/web/frontend ci
npm --prefix src/bosshunter/web/frontend run build
pip install -e .
```

## 3. 开启 Chrome 远程调试

用独立的用户目录启动 Chrome（Chrome 136+ 在默认用户目录下会忽略远程调试参数）：

```bash
# macOS（先用 Cmd+Q 完全退出已有的 Chrome）
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.czspig-chrome"

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.czspig-chrome"
```

```powershell
# Windows（PowerShell；Chrome 装在别处时调整路径）
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 "--user-data-dir=$env:LOCALAPPDATA\CzspigChrome"
```

在这个新打开的 Chrome 窗口里登录 BOSS 直聘，任务运行期间不要关闭它。

## 4. 完成本地配置

```bash
bosshunter web
```

在 `http://127.0.0.1:8686` 中：

1. 上传你自己的简历（`.md`、`.docx` 或带文字层的 `.pdf`）。
2. 填写搜索关键词、目标城市、期望薪资、一票否决词和评分阈值。
3. 在「AI 设置」中选择服务商（默认 DeepSeek），填写 API Key 并点「测试连接」。
4. 保存。

API Key 只在本地面板输入，会保存在本机的 `config.yaml`（已被 `.gitignore` 忽略），不要贴到聊天、Issue 或提交里。

## 5. 检查连接并运行

```bash
bosshunter ai-status
bosshunter connect
bosshunter run
```

`run` 会依次完成：检查 Chrome → 采集岗位 → AI 评分。也可以在工作台点「采集并评分」。

## 6. 挑岗位、投递

1. 工作台的「今日待处理」会列出达到评分阈值的岗位，并附上评分理由。
2. 打开岗位链接，**自己在 BOSS 直聘沟通、投递**。
3. 回到工作台，把投递过的岗位「标记已投递」，不合适的点「放弃」。
4. 需要时在岗位详情点「AI 生成定制简历」，下载后手动发给 HR。

## 安全边界

- 不会在招聘平台上发送、回复或自动投递任何内容。
- 遇到验证码、频率限制、登录墙或页面结构变化时会安全停止，不尝试绕过。
- 采集额度和风险暂停可以在配置页的「采集安全设置」里调整；建议保持默认值或调得更保守。
