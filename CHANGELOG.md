# 更新日志

## v0.1.0

基于 BossHunter v2.4.0 改造的首个版本。

- 定位改为只读的 AI 岗位筛选助手：保留 BOSS 直聘采集、初筛、AI 评分、工作台和导出。
- 移除 AI 招呼语、自动发送、发送节流、HR 回复监听、跟进、Agent API 和面试准备导出。
- 移除智联招聘、前程无忧、猎聘采集，以及 Windows / macOS 一键启动脚本。
- 定制简历改为在岗位详情中手动生成（`POST /api/jobs/<id>/tailored-resume`）。
- BOSS 岗位可以手动标记「已投递」。
- AI 默认使用 DeepSeek（`deepseek-v4-pro`），其他服务商仍可在配置页切换。
- 换上猪猪求职助手的名称、logo、吉祥物和粉色主题。
