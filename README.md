# 市场情绪监控看板（Market Sentiment Dashboard）

> 一个**以"提前给出可执行操作信号"为目标**的美股/A股情绪监控系统。
> 不是数据展示板——每个指标都必须回答两个问题：**它领先什么？触发了该做什么？**

**在线 Demo**：https://dreamybecca-coder.github.io/market-sentiment-dashboard/

*A sentiment monitoring dashboard for US/CN equity markets, designed around leading indicators and pre-committed action triggers rather than data display. All data sources are free (FRED, Eastmoney, CNN, CBOE). Docs below are in Chinese; the pipeline code is dependency-free Python 3.*

---

## 目录

- [核心理念](#核心理念)
- [逻辑架构](#逻辑架构)
- [指标与数据源](#指标与数据源)
- [快速开始](#快速开始)
- [接入你自己的数据](#接入你自己的数据)
- [自动化运行](#自动化运行)
- [目录结构](#目录结构)
- [免责声明](#免责声明)

## 核心理念

1. **预测性优先**：宁可少而领先，不要多而滞后。入选指标必须能说清"领先什么、领先多久"。
2. **操作指向**：每个指标、每个警报都映射到具体动作（减/加/不动），否则不进看板。
3. **不大而全**：6–10 个分层指标 + 明确的领先关系 > 50 个平铺指标。
4. **触发线写死**：情绪在极端时刻会劫持决策。阈值预先写进 `playbook`，触发即执行。

## 逻辑架构

```
第一层 宏观地基（慢变量，月-季尺度）
  └─ 实际利率（10Y/30Y TIPS）、净流动性（WALCL−TGA−RRP）、收益率曲线、初请失业金
     → "估值天花板在被抬高还是压低？衰退概率多大？"

第二层 信用（中变量，周尺度，权重最高 20%）
  └─ 高收益债利差 OAS + 30日变动速度
     → 垃圾债投资者是第一批要求风险补偿的人，领先股市调整 2–4 周

第三层 情绪与波动（快变量，日尺度）
  └─ VIX 一年分位、PCR 看跌看涨比、CNN 恐贪指数
     → "现在是恐慌（反向买点）还是自满（对冲最便宜）？"

第四层 广度与动量（验证层）
  └─ S5TH（200日线上占比）、SPX vs 125日线、板块温度计（SOX/罗素2000/科创50）
     → "涨是普涨还是只有巨头？我的敞口所在板块是否已背离指数？"

裁判：利率×股票四象限（10Y 10日变动 × SPX 10日涨跌）
  → 收益率下行可以是"宽松顺风"也可以是"避险崩塌"，
    只有和股票方向联看才能区分。所有信号的最终解释框架。
```

**为什么这样组合**（一句话版）：

- 信用利差权重最高——信用市场比股市诚实，"低位利差快速走阔"是最可靠的先行警报；
- 四象限是裁判——单看利率会得出完全相反的结论（2026-10 实证教训）；
- A股科技是哨兵——2026 年科创50 领先美股 AI 调整约 3 个月，不是"另一个市场"；
- 实际利率可一票否决——长久期组合（科技/创新药）对实际利率最敏感，>+30bp/月直接亮红灯；
- 信号日志可证伪——越阈自动记录，21/63 天后用真实收益结算，累积胜率数据库。

## 指标与数据源

全部**免费**数据源，无需 API key：

| 指标 | 来源 | 频率 | 自动化 |
|------|------|------|--------|
| 高收益债利差 OAS | FRED `BAMLH0A0HYM2` | 日 | ✅ 全自动 |
| VIX | FRED `VIXCLS` | 日 | ✅ 全自动 |
| SPX 价格/动量 | FRED `SP500` | 日（滞后1-3天） | ✅ 全自动 |
| 10Y/30Y 收益率、实际利率 | FRED `DGS10/DGS30/DFII10/DFII30` | 日 | ✅ 全自动 |
| 净流动性 | FRED `WALCL/WTREGEN/RRPONTSYD` | 周 | ✅ 全自动 |
| 收益率曲线 | FRED `T10Y3M` | 日 | ✅ 全自动 |
| 初请失业金 | FRED `ICSA` | 周 | ✅ 全自动 |
| A股指数/资金流 | 东方财富公开接口 | 日（盘中） | ✅ 全自动 |
| 板块温度计（SOX/RUT 等） | Yahoo Finance | 日 | ⚠️ 限流时自动降级 |
| CNN 恐贪指数 | CNN | 日 | 🔧 浏览器抓取/人工 |
| PCR 看跌看涨比 | CBOE 官网 Daily Statistics | 日 | 🔧 人工周更 |
| S5TH 广度 | indexindicators.com | 日 | 🔧 人工周更 |
| 估值（PE/CAPE） | multpl.com 等 | 周 | ⚠️ 半自动 |

🔧 = 需要手动填进 `curated.json`（看板会标注数据新鲜度，超过阈值亮 stale 提醒）。

## 快速开始

**零依赖**：只用到 Python 3 标准库 + `curl`。

```bash
# 1. 克隆
git clone https://github.com/dreamybecca-coder/market-sentiment-dashboard.git
cd market-sentiment-dashboard

# 2. 准备人工维护字段（可先用示例值跑通）
cp pipeline/curated.example.json pipeline/curated.json

# 3. 跑数据管线（约 1-2 分钟，产出 data/sentiment.json）
python3 pipeline/fetch_sentiment.py

# 4. 本地预览
python3 -m http.server 8000
# 打开 http://localhost:8000
```

**部署到自己的 GitHub Pages**：fork 本仓库 → Settings → Pages → Source 选 `main` 分支根目录。每次跑 `fetch_sentiment.py` 会自动把新数据 commit + push（`pipeline/export_web.py`，best-effort）。

**触发线哨兵**（可选）：

```bash
# 手动跑一次：检查四条触发线是否被击穿
python3 pipeline/trigger_watch.py
# 输出 fires（新击穿）到 logs/trigger-fire.json；已触发过的线不重复提醒
```

## 接入你自己的数据

### 1. 人工维护字段：`pipeline/curated.json`

复制 `curated.example.json` 为 `curated.json`，按注释填入：

- **PCR**：CBOE 官网 → Market Statistics → Daily Market Statistics → "TOTAL PUT/CALL RATIO"
- **S5TH**：indexindicators.com 搜 "% of S&P 500 Stocks Above 200-Day MA"
- **恐贪指数**：CNN Fear & Greed（0-100）
- **估值/资金面**：按示例格式填，页面会显示更新时间与新鲜度

### 2. 你自己的触发线：`playbook.md`

复制 `pipeline/playbook.example.md` 为 `playbook.md`，改成你自己的阈值、减仓顺序和档位。**这是全系统最重要的文件**——它把"看数据"变成"执行纪律"。

### 3. 每日晨报（可选）：`pipeline/parse_brief.py`

从本机 Obsidian 仓库读取晨报 Markdown（自动发现 vault，目录在脚本顶部 `BRIEF_DIRS` 配置），产出 `data/brief.json`，看板右侧展示重点新闻并与警报关键词联动。没有 Obsidian 可跳过，看板主体不受影响。

### 4. A股资金流检查：`pipeline/cn_moneyflow_check.py`

独立小工具：拉取科创50/创业板指/上证的主力资金流（东财接口），用于验证"反弹是否有资金背书"。

## 自动化运行

macOS/Linux cron 示例（作者本人的节奏，供参考）：

```cron
# 每交易日 6:12 全量数据刷新（美股收盘后）
12 6 * * 2-6  cd /path/to/repo && /usr/bin/python3 pipeline/fetch_sentiment.py
# 每交易日 15:43 A股收盘后
43 15 * * 1-5 cd /path/to/repo && /usr/bin/python3 pipeline/fetch_sentiment.py
# 每 30 分钟触发线哨兵（轻量，只读快数据源）
*/30 * * * *  cd /path/to/repo && /usr/bin/python3 pipeline/trigger_watch.py
```

## 目录结构

```
├── index.html              # 看板页面（单文件，读取 data/*.json）
├── data/
│   ├── sentiment.json      # 情绪数据快照（fetch_sentiment.py 产物）
│   └── brief.json          # 晨报快照（parse_brief.py 产物，可选）
├── pipeline/
│   ├── fetch_sentiment.py  # 主数据管线（FRED/Yahoo/东财 → artifact JSON）
│   ├── trigger_watch.py    # 触发线哨兵（T1-T4 判定 + 状态记忆）
│   ├── parse_brief.py      # Obsidian 晨报解析（可选）
│   ├── export_web.py       # 数据快照导出 + git push（被主管线调用）
│   ├── cn_moneyflow_check.py # A股资金流向检查工具
│   ├── curated.example.json    # 人工维护字段模板
│   └── playbook.example.md     # 触发线操作手册模板
└── LICENSE                 # MIT
```

## 免责声明

本项目仅供学习与研究使用，不构成任何投资建议。数据来自公开免费源，可能存在延迟、缺失或错误；页面明确标注了每项数据的时间戳与新鲜度，请以原始数据源为准。任何基于本项目做出的投资决策，风险自担。

## License

[MIT](LICENSE) © 2026 dreamybecca-coder
