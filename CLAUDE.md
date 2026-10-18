# CLAUDE.md

猪猪求职助手：本地、只读的 AI 岗位筛选助手。Python 包目录仍叫 `bosshunter`，CLI 命令也是 `bosshunter`。

## 产品边界（不要越过）

- 只采集 BOSS 直聘岗位，并对岗位做 AI 评分；不在招聘平台上发送、回复或投递任何内容。
- 不加回 AI 招呼语、自动发送、HR 回复监听，也不加入其他招聘平台。
- 不绕过验证码、频率限制或登录墙，不调高默认采集频率。
- API Key 只通过本地面板或环境变量提供，不写进代码，也不要求用户在聊天中提供。

## 常用命令

```bash
npm --prefix src/bosshunter/web/frontend ci
npm --prefix src/bosshunter/web/frontend run build   # tsc + vite
npm --prefix src/bosshunter/web/frontend test        # vitest
python -m pytest -q                                   # 后端测试（pythonpath=src）
```
