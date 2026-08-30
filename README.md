# 市场情绪监控看板（Web 版）

美股日盘/周盘情绪监控看板的静态网页版，托管在 GitHub Pages。

- **数据来源**：与本机 Kimi Work 看板共用同一套管线——情绪数据来自 FRED / Yahoo Finance（每个交易日 6:12 自动更新），晨报来自 Obsidian Morning-Brief（每日 9:07 自动更新）
- **自动同步**：本机自动化每次刷新数据后，会把最新 JSON 提交推送到本仓库，网页随之更新
- **结构**：`index.html` 为单文件页面，`data/sentiment.json` 与 `data/brief.json` 为数据快照

本地预览：在本目录运行 `python3 -m http.server 8000` 后访问 http://localhost:8000
