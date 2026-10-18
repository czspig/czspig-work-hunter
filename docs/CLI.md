# CLI 命令

| 命令 | 说明 |
|---|---|
| `bosshunter web [-p 8686] [--no-open]` | 启动本地工作台 |
| `bosshunter ai-status` | 检查 AI 服务连接（不显示完整 Key） |
| `bosshunter connect` | 检查 Browser Runtime 与 Chrome 连接 |
| `bosshunter run` | 一键运行：采集 → AI 评分（不会自动投递） |
| `bosshunter scrape [-k 关键词] [-l 数量]` | 只采集岗位 |
| `bosshunter score [--rescore-filtered]` | 只做 AI 评分 |
| `bosshunter resume --job-id <ID>` | 为指定岗位生成定制简历 |
| `bosshunter status [--full]` | 查看岗位状态统计 |

所有命令都支持 `--config <路径>` 指定配置文件，默认读取当前目录的 `config.yaml`。
