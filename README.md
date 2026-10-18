<p align="center">
  <img src="assets/mascot.webp" alt="猪猪求职助手吉祥物" width="180">
</p>

<h1 align="center">猪猪求职助手 v0.1.0</h1>

<p align="center">
  在你自己电脑上运行、只读的 AI 岗位筛选助手：收集 BOSS 直聘岗位，对照你的简历打分，挑出值得投的。
</p>

<p align="center">
  🏠 本地运行 · 👀 只读采集 · 🐷 你来投递 · 🤖 默认 DeepSeek
</p>

**猪猪求职助手**帮你省掉「刷岗位、看 JD、判断匹不匹配」的重复劳动。**它不会在招聘平台上替你发出任何消息**：投不投、怎么打招呼，都由你自己在 BOSS 直聘完成。

> [!WARNING]
> 采集岗位仍需要脚本浏览招聘网站，存在账号受限的风险。本项目仅供个人学习和求职使用；请遵守平台规则，保持低频，并自行承担风险。本项目与任何招聘平台无隶属、合作或背书关系。

## 能做什么

| 功能 | 说明 |
|---|---|
| 岗位采集 | 只读采集 BOSS 直聘岗位，内置每日页面额度、风险暂停和断点续采 |
| 筛选打分 | 关键词初筛、一票否决词、黑名单公司、外包识别，再由 AI 结合 JD 打分并说明理由 |
| 工作台 | 岗位池、筛选、导出 Excel、回收站；投递后手动标记「已投递」 |
| 定制简历 | 在岗位详情里点一下，按这个岗位的 JD 生成定制简历，下载后自己发送 |

**不做的事**：AI 招呼语、自动发送、监听 HR 回复，以及智联 / 前程无忧 / 猎聘采集。这些功能已从原项目中移除。

## 快速开始

需要 Python 3.10+、Node.js 22+、最新版 Google Chrome，以及一个 AI API Key（默认 DeepSeek）。

```bash
git clone https://github.com/czspig/czspig-work-hunter.git
cd czspig-work-hunter
npm --prefix src/bosshunter/web/frontend ci
npm --prefix src/bosshunter/web/frontend run build
pip install -e .
bosshunter web
```

打开 `http://127.0.0.1:8686`，上传你的简历，设置关键词和城市，并在「AI 设置」里填写 API Key。**API Key 只在本地面板里填写**，不要写进代码或提交到仓库。

然后用远程调试模式打开 Chrome，并在这个窗口里登录 BOSS 直聘：

```bash
bosshunter ai-status   # 检查 AI 连接
bosshunter connect     # 检查 Chrome 连接
bosshunter run         # 采集 + 评分
```

完整步骤见 [上手指南](docs/QUICKSTART.md)，全部命令见 [CLI 命令](docs/CLI.md)。

## 致谢与许可证

本项目基于 [升级打怪开源社区](https://github.com/shengjidaguai-china) 的 [BossHunter](https://github.com/shengjidaguai-china/BossHunter) 二次开发，感谢原作者和所有贡献者。

源码沿用 [PolyForm Noncommercial License 1.0.0](LICENSE)，**仅限非商业用途**。它是 source-available（源码可用）软件，不是 OSI 定义下的开源软件。
