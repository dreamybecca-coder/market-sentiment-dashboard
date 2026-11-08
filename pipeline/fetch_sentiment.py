#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场情绪监控 V2 · 每日数据层
设计原则（V2 优化方案）：
  - 日盘 5 主轴：HY OAS、VIX/VIX3M、S5TH、PCR、RSP/SPY
  - 组合专属独立警报：10Y 实际利率（DFII10）、净流动性（WALCL-TGA-RRP）
  - 每个指标：当前值 + 1D/1W 变化 + 滚动分位 + 阈值
  - 状态灯（绿/黄/红）+ 越阈警报带 + 一句话判断（结论先行）
  - 信号日志：越阈落库，事后 21/63 日结算（logs/signal-log.jsonl）
  - 恐惧端谈加仓、贪婪端只谈暂停；中性区输出"无信号，坚持原计划"
数据源：FRED 主干（不带浏览器 UA）；Yahoo 增强（429 自动降级，宁陈旧不假值）。
人工维护：curated.json（PCR、S5TH、估值、资金面、恐贪外部读数）。
输出：run(ctx) 返回 {"artifact": {...}}（Blueprint 托管 runner）；CLI 打印同一结构。
"""
import json, subprocess, sys, io, csv, os, re, time, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CURATED_PATH = BASE / "curated.json"
LOG_DIR = BASE / "logs"
SIGNAL_LOG = LOG_DIR / "signal-log.jsonl"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

# ---------------------------------------------------------------- 基础工具

def curl(url, timeout=40, retries=3, use_ua=True):
    last = None
    cmd = ["curl", "-sS", "--http1.1", "--compressed", "--max-time", str(timeout)]
    if use_ua:
        cmd += ["-H", f"User-Agent: {UA}"]
    cmd.append(url)
    for i in range(retries):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        last = f"curl {r.returncode}: {r.stderr[:120]} empty={not r.stdout.strip()}"
        time.sleep(3 + i * 4)
    raise RuntimeError(last)

def fred_series(series_id, max_points=None):
    # 注意：FRED 拦截浏览器 UA，必须不带 UA
    txt = curl(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", use_ua=False)
    rows = []
    for row in csv.DictReader(io.StringIO(txt)):
        v = row.get(series_id, ".")
        if v not in (".", "", None):
            try:
                rows.append((row["observation_date"], float(v)))
            except ValueError:
                pass
    if max_points:
        rows = rows[-max_points:]
    return rows

def yahoo_closes(symbol, range_="3mo", deadline=None):
    time.sleep(2.5)  # 符号间限速，避免 429
    hosts = ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]
    d = None
    last = None
    for attempt in range(4):
        if deadline and time.time() > deadline:
            raise RuntimeError("yahoo time budget exhausted")
        host = hosts[attempt % len(hosts)]  # 429 时轮换 query1/query2
        url = f"https://{host}/v8/finance/chart/{symbol}?range={range_}&interval=1d"
        txt = curl(url, timeout=15, retries=1)
        if txt.lstrip().startswith("Too Many Requests"):
            last = "429 Too Many Requests"
            time.sleep(5 + attempt * 4)
            continue
        try:
            d = json.loads(txt)
            break
        except json.JSONDecodeError as e:
            last = str(e)
            time.sleep(3)
    if d is None:
        raise RuntimeError(last or "yahoo parse failed")
    res = d["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    return [c for c in closes if c is not None]

def cnbc_closes(symbol, period="3M", timeout=15):
    """CNBC 图表接口：SOX/RUT 等美股指数日线（Yahoo 429 时代的替代源）。"""
    url = f"https://ts-api.cnbc.com/harmony/app/charts/{period}.json?symbol={symbol}"
    d = json.loads(curl(url, timeout=timeout, retries=2))
    bars = (d.get("barData") or {}).get("priceBars") or []
    rows = []
    for b in bars:
        t = str(b.get("tradeTime", ""))
        if len(t) >= 8 and b.get("close") is not None:
            rows.append((f"{t[0:4]}-{t[4:6]}-{t[6:8]}", float(b["close"])))
    if not rows:
        raise RuntimeError("cnbc empty")
    return rows

def em_closes(secid, days=250, timeout=15):
    """东方财富日K：A股指数/板块（交易日当天为盘中最新值，比 FRED 更快）。"""
    end = datetime.date.today().strftime("%Y%m%d")
    beg = (datetime.date.today() - datetime.timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg={beg}&end={end}")
    d = json.loads(curl(url, timeout=timeout, retries=2))
    ks = ((d.get("data") or {}).get("klines")) or []
    rows = [(k.split(",")[0], float(k.split(",")[1])) for k in ks]
    if not rows:
        raise RuntimeError("eastmoney empty")
    return rows

def pct_rank(vals, current):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    below = sum(1 for v in vals if v <= current)
    return round(100.0 * below / len(vals), 1)

def pct_change(series_vals, days):
    """series_vals: 时间升序的数值列表。返回最近 days 个观测的百分比变化。"""
    if len(series_vals) <= days:
        return None
    prev = series_vals[-1 - days]
    if not prev:
        return None
    return round((series_vals[-1] / prev - 1) * 100, 2)

def bp_change(series_vals, days):
    if len(series_vals) <= days:
        return None
    return round((series_vals[-1] - series_vals[-1 - days]) * 100, 1)  # 单位%→bp

def lerp(x, x0, y0, x1, y1):
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)

# ---------------------------------------------------------------- 打分（0=极度恐惧 100=极度贪婪）

def score_momentum(pct):
    if pct <= -10: return 5
    if pct <= -5:  return lerp(pct, -10, 5, -5, 20)
    if pct <= 2:   return lerp(pct, -5, 20, 2, 45)
    if pct <= 8:   return lerp(pct, 2, 45, 8, 70)
    return min(95, lerp(pct, 8, 70, 14, 95))

def score_vix(v):
    if v <= 12: return 92
    if v <= 15: return lerp(v, 12, 92, 15, 75)
    if v <= 20: return lerp(v, 15, 75, 20, 55)
    if v <= 25: return lerp(v, 20, 55, 25, 35)
    if v <= 30: return lerp(v, 25, 35, 30, 18)
    return 5

def score_pcr(r):
    if r >= 1.2: return 10
    if r >= 0.9: return lerp(r, 1.2, 10, 0.9, 30)
    if r >= 0.7: return lerp(r, 0.9, 30, 0.7, 50)
    if r >= 0.5: return lerp(r, 0.7, 50, 0.5, 75)
    return 95

def score_breadth(p):
    if p <= 20: return 5
    if p <= 35: return lerp(p, 20, 5, 35, 20)
    if p <= 55: return lerp(p, 35, 20, 55, 45)
    if p <= 75: return lerp(p, 55, 45, 75, 70)
    return 92

def score_hy(sp):
    if sp <= 2.5: return 90
    if sp <= 3.5: return lerp(sp, 2.5, 90, 3.5, 65)
    if sp <= 4.5: return lerp(sp, 3.5, 65, 4.5, 40)
    if sp <= 6.0: return lerp(sp, 4.5, 40, 6.0, 18)
    return 5

def score_safehaven(diff):
    if diff >= 5: return 92
    if diff >= 1.5: return lerp(diff, 1.5, 70, 5, 92)
    if diff >= -1.5: return lerp(diff, -1.5, 30, 1.5, 70)
    if diff >= -5: return lerp(diff, -5, 10, -1.5, 30)
    return 8

def score_vixratio(r):
    # VIX/VIX3M：<0.85 深度 contango（自满）→ 贪婪；>1.0 倒挂（恐慌）→ 恐惧
    if r >= 1.05: return 5
    if r >= 0.95: return lerp(r, 1.05, 5, 0.95, 30)
    if r >= 0.85: return lerp(r, 0.95, 30, 0.85, 60)
    if r >= 0.75: return lerp(r, 0.85, 60, 0.75, 85)
    return 92

def zone_of(score):
    if score <= 20: return "极度恐惧"
    if score <= 40: return "恐惧"
    if score <= 60: return "中性"
    if score <= 80: return "贪婪"
    return "极度贪婪"

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None

# ---------------------------------------------------------------- 信号日志

def load_signal_log():
    if not SIGNAL_LOG.exists():
        return []
    entries = []
    for line in SIGNAL_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries

def update_signal_log(active_alerts, spx_rows):
    """active_alerts: [{key, level, text}]；spx_rows: [(date, close)] 升序。
    新警报落库；老警报按交易日结算 21/63 日 SPX 收益。"""
    LOG_DIR.mkdir(exist_ok=True)
    entries = load_signal_log()
    today = datetime.date.today().isoformat()
    rows = spx_rows or []
    dates = [d for d, _ in rows]
    spx_now = rows[-1][1] if rows else None

    open_keys = {e["key"] for e in entries if e.get("status") == "open"}
    for a in active_alerts:
        if a["key"] not in open_keys:
            entries.append({
                "key": a["key"], "level": a["level"], "text": a["text"],
                "date": today, "spxAtSignal": spx_now, "status": "open",
            })

    # 按交易日结算：找信号日（或其后首个交易日）在序列中的位置，第 21/63 个交易日后结算
    for e in entries:
        if e.get("status") != "open":
            continue
        s0 = e.get("spxAtSignal")
        if not s0 or not e.get("date"):
            continue
        idx = next((i for i, d in enumerate(dates) if d >= e["date"]), None)
        if idx is None:
            continue
        for horizon, key in ((21, "ret21d"), (63, "ret63d")):
            if key not in e and idx + horizon < len(rows):
                e[key] = round((rows[idx + horizon][1] / s0 - 1) * 100, 2)
        if "ret63d" in e:
            e["status"] = "settled"

    SIGNAL_LOG.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                          encoding="utf-8")
    return entries

def signal_stats(entries):
    settled = [e for e in entries if "ret21d" in e]
    out = {"total": len(entries), "settled21d": len(settled)}
    if settled:
        wins = sum(1 for e in settled if e["ret21d"] > 0)
        out["winRate21d"] = round(100.0 * wins / len(settled), 0)
        out["avgRet21d"] = round(sum(e["ret21d"] for e in settled) / len(settled), 2)
    return out

# ---------------------------------------------------------------- 事件条（FOMC/OpEx/月末再平衡）

FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
FOMC_2027 = ["2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
             "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]

def third_friday(year, month):
    d = datetime.date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += datetime.timedelta(days=1)

def upcoming_events(days=14):
    today = datetime.date.today()
    end = today + datetime.timedelta(days=days)
    evts = []
    for ds in FOMC_2026 + FOMC_2027:
        d = datetime.date.fromisoformat(ds)
        if today <= d <= end:
            evts.append({"date": ds, "label": "FOMC 决议", "kind": "fomc"})
    for moff in range(0, 3):
        y, m = today.year, today.month + moff
        while m > 12:
            y, m = y + 1, m - 12
        d = third_friday(y, m)
        if today <= d <= end:
            evts.append({"date": d.isoformat(), "label": "期权到期（OpEx）", "kind": "opex"})
    # 月末再平衡窗口（最后 3 个自然日）
    if today.day >= 26:
        evts.append({"date": today.isoformat(), "label": "月末养老金再平衡窗口", "kind": "rebalance"})
    evts.sort(key=lambda e: e["date"])
    return evts

# ---------------------------------------------------------------- 主流程

def build_artifact():
    errors = []
    curated = {}
    if CURATED_PATH.exists():
        curated = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    data = {}

    # ---------- FRED 主干 ----------
    fetches = [
        ("vix",  "VIXCLS", 260),
        ("spx",  "SP500", 320),
        ("ndxfb","NASDAQCOM", 10),
        ("hy",   "BAMLH0A0HYM2", 1250),
        ("us10y","DGS10", 80),
        ("dfii10","DFII10", 60),
        ("dfii30","DFII30", 60),
        ("t10y3m","T10Y3M", 500),
        ("icsa", "ICSA", 300),
        ("walcl","WALCL", 320),
        ("tga",  "WTREGEN", 800),
        ("rrp",  "RRPONTSYD", 800),
        ("sofr", "SOFR", 260),
        ("effr", "EFFR", 260),
        ("ig",   "BAMLC0A0CM", 1250),
        ("dxy",  "DTWEXBGS", 320),
    ]
    for key, sid, n in fetches:
        try:
            rows = fred_series(sid, max_points=n)
            data[key] = rows
        except Exception as e:
            errors.append(f"{sid}: {e}")

    # ---------- Yahoo 增强（全局 100 秒预算，超时剩余符号直接降级） ----------
    y = {}
    yahoo_deadline = time.time() + 100
    for key, sym, rng in [
        ("vix3m", "%5EVXV", "3mo"),
        ("vixy",  "%5EVIX", "3mo"),
        ("ndx",   "%5ENDX", "5d"),
        ("spy",   "SPY", "3mo"),
        ("tlt",   "TLT", "3mo"),
        ("rsp",   "RSP", "3mo"),
    ]:
        if time.time() > yahoo_deadline:
            errors.append(f"Yahoo {sym}（预算耗尽，已降级）")
            continue
        try:
            y[key] = yahoo_closes(sym, rng, deadline=yahoo_deadline)
        except Exception as e:
            errors.append(f"Yahoo {sym}（已降级）: {str(e)[:80]}")

    # ---------- P1 板块温度计 + A股（CNBC/东方财富，失败自动降级，不进光谱） ----------
    ext = {}
    for key, fn, arg in [
        ("sox", cnbc_closes, ".SOX"),
        ("rut", cnbc_closes, ".RUT"),
        ("kc50", em_closes, "1.000688"),
        ("cyb",  em_closes, "0.399006"),
        ("sh",   em_closes, "1.000001"),
        ("cnsemi", em_closes, "90.BK1036"),
        ("ndx_em", em_closes, "100.NDX"),
    ]:
        try:
            ext[key] = fn(arg)
        except Exception as e:
            errors.append(f"{key}（已降级）: {str(e)[:60]}")

    # ---------- P2 新增数据源：两融（东财数据中心）+ 黄金现货（东财外盘期货） ----------
    def em_rzrq(days=30):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
               f"&columns=DIM_DATE,RZYE,RZJME&sortColumns=DIM_DATE&sortTypes=-1&pageSize={days}&pageNumber=1"
               "&source=WEB&client=WEB")
        d = json.loads(curl(url, timeout=15, retries=2))
        rows = [((r["DIM_DATE"] or "")[:10], float(r["RZYE"]) / 1e8, float(r["RZJME"]) / 1e8)
                for r in ((d.get("result") or {}).get("data") or []) if r.get("RZYE")]
        rows.reverse()  # 时间正序；(日期, 融资余额亿, 当日净买入亿)
        if not rows:
            raise RuntimeError("rzrq empty")
        return rows

    def em_gold():
        url = "https://push2.eastmoney.com/api/qt/stock/get?secid=101.GC00Y&fields=f43,f60,f170"
        d = json.loads(curl(url, timeout=12, retries=2)).get("data") or {}
        if d.get("f43") in (None, "-"):
            raise RuntimeError("gold empty")
        return float(d["f43"]) / 10.0, (float(d["f170"]) / 100.0 if d.get("f170") not in (None, "-") else None)

    def em_gold_hist(days=130):
        end = datetime.date.today().strftime("%Y%m%d")
        beg = (datetime.date.today() - datetime.timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               f"secid=101.GC00Y&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg={beg}&end={end}")
        d = json.loads(curl(url, timeout=15, retries=2))
        ks = ((d.get("data") or {}).get("klines")) or []
        rows = [(k.split(",")[0], float(k.split(",")[1])) for k in ks]
        if not rows:
            raise RuntimeError("gold hist empty")
        return rows

    try:
        ext["rzrq"] = em_rzrq()
    except Exception as e:
        errors.append(f"两融（已降级）: {str(e)[:60]}")
    try:
        ext["gold"] = em_gold()
    except Exception as e:
        errors.append(f"黄金（已降级）: {str(e)[:60]}")
    try:
        ext["gold_hist"] = em_gold_hist()
    except Exception as e:
        errors.append(f"黄金历史（已降级）: {str(e)[:60]}")

    # ---------- 组装分项 ----------
    components = []
    indices = []

    # 1) HY OAS（信用，领先 2-4 周）
    hy = None
    if "hy" in data:
        rows = data["hy"]
        vals = [v for _, v in rows]
        hy = {
            "value": vals[-1], "date": rows[-1][0],
            "hist1y": vals[-260:], "hist5y": vals,
            "d1": round(vals[-1] - vals[-2], 2) if len(vals) > 1 else None,
            "chg20bp": bp_change(vals, 20), "chg30bp": bp_change(vals, 30),
            "p1y": pct_rank(vals[-260:], vals[-1]), "p5y": pct_rank(vals, vals[-1]),
        }
        s = score_hy(hy["value"])
        components.append({
            "key": "credit", "name": "高收益债利差 OAS", "group": "信用",
            "value": f'{hy["value"]:.2f}%', "score": round(s), "zone": zone_of(s),
            "delta1d": f'{hy["d1"]:+.2f}' if hy["d1"] is not None else None,
            "delta1w": f'{hy["chg20bp"]:+.0f}bp/20日' if hy["chg20bp"] is not None else None,
            "percentile1y": hy["p1y"], "percentile5y": hy["p5y"],
            "threshold": "≥3.75% 警戒 · ≥5% 或 30日+50bp 降风险",
            "weight": 20, "confidence": 1.0, "source": "FRED（自动）",
            "note": "领先股市 2–4 周；利差走阔=风险累积",
            "what": "信用评级差的公司发债，要比国债多付多少利息。差越小＝投资者越不怕违约（贪婪）；差越大＝市场在恐慌性定价违约（恐惧）。",
            "meaning": (f'现在 {hy["value"]:.2f}%，比近 5 年 {100 - hy["p5y"]:.0f}% 的时间都低——市场几乎没为违约风险定价，非常乐观。'
                        + (f'近 30 天 {hy["chg30bp"]:+.0f}bp，' if hy["chg30bp"] is not None else '')
                        + '低位利差本身不是卖出信号，但从低位快速走阔（30 天 +50bp）通常领先股市调整 2–4 周，是这套系统里最看重的先行警报。'),
        })

    # 2) VIX（波动率锚）
    if "vix" in data:
        rows = data["vix"]
        vals = [v for _, v in rows]
        v = vals[-1]
        s = score_vix(v)
        components.append({
            "key": "volatility", "name": "VIX 波动率", "group": "波动率",
            "value": f"{v:.1f}", "score": round(s), "zone": zone_of(s),
            "delta1d": f"{pct_change(vals,1):+.1f}%" if pct_change(vals,1) is not None else None,
            "delta1w": f"{pct_change(vals,5):+.1f}%/周" if pct_change(vals,5) is not None else None,
            "percentile1y": pct_rank(vals[-260:], v), "percentile5y": None,
            "threshold": "分位>90% 恐慌 · <10% 自满",
            "weight": 15, "confidence": 1.0, "source": "FRED VIXCLS（自动）",
            "note": "0DTE 时代需与 PCR 交叉验证",
            "what": "市场愿意为期权「保险」付的价格。数字越高＝越多人抢着买保护（恐慌）；越低＝没人买保护（自满）。",
            "meaning": (f'现在 {v:.1f}，处于近 1 年 {pct_rank(vals[-260:], v):.0f}% 分位'
                        + ('——几乎是一年最便宜，市场很自满。低位不是卖出信号，但意味着保险很便宜，想对冲的话成本低。' if pct_rank(vals[-260:], v) < 10
                           else '——恐慌区。历史上 VIX 处于这种高位时，分批买入的胜率反而最高。' if pct_rank(vals[-260:], v) > 90
                           else '——正常区间，没有极端情绪。')),
        })
        chg = pct_change(vals, 1)
        indices.append({"symbol": "VIX", "name": "波动率指数", "price": round(v, 2),
                        "changePct": chg, "date": rows[-1][0]})

    # 3) VIX/VIX3M 期限结构
    vix_ratio = None
    if y.get("vix3m") and y.get("vixy"):
        r = y["vixy"][-1] / y["vix3m"][-1]
        prev_r = y["vixy"][-2] / y["vix3m"][-2] if len(y["vixy"]) > 1 else None
        vix_ratio = {"value": round(r, 3), "prev": round(prev_r, 3) if prev_r else None}
        s = score_vixratio(r)
        components.append({
            "key": "vixterm", "name": "VIX/VIX3M 期限结构", "group": "波动率",
            "value": f"{r:.2f}", "score": round(s), "zone": zone_of(s),
            "delta1d": f"{(r-prev_r):+.2f}" if prev_r else None, "delta1w": None,
            "percentile1y": None, "percentile5y": None,
            "threshold": ">1.00 倒挂应激 · 回落至<1 买点区",
            "weight": 15, "confidence": 0.9, "source": "Yahoo（自动）",
            "note": "倒挂解除后 5 日历史胜率 88%（2010 年来）",
            "what": "VIX（1 个月恐慌）÷ VIX3M（3 个月恐慌）。正常 <1（远期更贵）；>1 叫倒挂，说明市场在为「眼前的急事」应激。",
            "meaning": (f'现在 {r:.2f}。'
                        + ('倒挂中——市场认为眼前的风险比三个月后大，通常对应具体事件（加息/选举/危机）。' if r >= 1.0
                           else '刚从倒挂回落到 1 以下——2010 年来这种情况后 5 日胜率 88%，是历史检验过的买点区。' if prev_r and prev_r >= 1.0
                           else '正常结构，市场没有在为近期事件应激。')),
        })

    # 4) PCR（人工周更，滚动分位口径待 P1 重建）
    curated_stale = None
    if curated.get("updatedAt"):
        try:
            curated_stale = (datetime.date.today() - datetime.date.fromisoformat(curated["updatedAt"])).days
        except ValueError:
            pass
    pcr = curated.get("putCall")
    if pcr:
        r = pcr["value"]
        s = score_pcr(r)
        components.append({
            "key": "putcall", "name": "PCR 看跌/看涨期权比", "group": "波动率",
            "value": f"{r:.2f}", "score": round(s), "zone": zone_of(s),
            "delta1d": None, "delta1w": None, "percentile1y": None, "percentile5y": None,
            "threshold": "滚动分位>90% 恐慌 · <10% 自满（周更）",
            "weight": 10, "confidence": 0.7, "source": "人工维护（周更）",
            "note": pcr.get("note", ""), "staleDays": curated_stale,
            "what": "每成交 100 张看涨期权，对应多少张看跌期权。>1＝恐慌性买保护（反向看往往是机会）；<0.7＝投机过热。",
            "meaning": (f'现在 {r:.2f}——'
                        + ('看跌交易拥挤，恐慌明显，历史上属于反向布局区。' if r > 1.0
                           else '看涨交易拥挤，投机偏热，要警惕追高风险。' if r < 0.7
                           else '中性偏乐观：没有恐慌，也没有疯狂投机。')),
        })

    # 5) S5TH 广度（人工周更）
    br = curated.get("breadthS5TH")
    s5th = br["value"] if br else None
    if br:
        p = br["value"]
        s = score_breadth(p)
        components.append({
            "key": "breadth", "name": "S5TH 广度（%>200日线）", "group": "广度",
            "value": f"{p:.1f}%", "score": round(s), "zone": zone_of(s),
            "delta1d": None, "delta1w": None, "percentile1y": None, "percentile5y": None,
            "threshold": "<25% 恐慌反向区 · >90% 修复=牛市确认",
            "weight": 15, "confidence": 0.7, "source": "人工维护（周更）",
            "note": br.get("note", ""), "staleDays": curated_stale,
            "what": "标普 500 的 500 家公司里，股价站在 200 天均线上方的占比。>70%＝普涨健康牛；<25%＝恐慌出清区。",
            "meaning": (f'现在 {p:.0f}%——'
                        + ('刚过一半：涨的是少数大票，不是全面牛市，也不是弱势，广度一般。' if 40 <= p < 70
                           else '普涨格局，上涨有广泛的群众基础。' if p >= 70
                           else '大部分股票在中期均线下方，市场处于出清/恐慌区，历史反向布局区。')),
        })

    # 6) RSP/SPY 相对强弱
    rsp_rel = None
    if y.get("rsp") and y.get("spy") and len(y["rsp"]) > 21 and len(y["spy"]) > 21:
        r20 = (y["rsp"][-1] / y["rsp"][-21] - 1) * 100
        s20 = (y["spy"][-1] / y["spy"][-21] - 1) * 100
        rsp_rel = round(r20 - s20, 2)
        healthy = rsp_rel > -2
        s = 65 if healthy else (40 if rsp_rel > -4 else 20)
        components.append({
            "key": "rspspy", "name": "RSP/SPY 等权相对强弱", "group": "广度",
            "value": f"{rsp_rel:+.1f}%/20日", "score": s, "zone": zone_of(s),
            "delta1d": None, "delta1w": None, "percentile1y": None, "percentile5y": None,
            "threshold": "<-2% 且指数新高=集中度警报",
            "weight": 10, "confidence": 0.9, "source": "Yahoo（自动）",
            "note": "指数上涨是否只剩头部（顺势指标）",
            "what": "等权重指数（500 家算数平均）vs 市值加权指数（巨头说了算）的 20 日涨幅差。负数＝涨幅集中在少数大盘股。",
            "meaning": (f'20 日相对 {rsp_rel:+.1f}%——'
                        + ('上涨有广度，不只是巨头在拉指数，结构健康。' if healthy
                           else '指数上涨主要靠少数巨头撑着，中小盘跟不上——集中度偏高，结构偏脆弱。')),
        })

    # 7) SPX 动量（保留 V1）
    spx_rows = data.get("spx")
    spx_now = None
    spx_vs200 = None
    spx_near_high = None
    if spx_rows:
        vals = [v for _, v in spx_rows]
        spx_now = vals[-1]
        ma125, ma200 = sma(vals, 125), sma(vals, 200)
        if ma125:
            pct = (spx_now / ma125 - 1) * 100
            s = score_momentum(pct)
            components.append({
                "key": "momentum", "name": "SPX 动量（vs 125日线）", "group": "广度",
                "value": f"{pct:+.1f}%", "score": round(s), "zone": zone_of(s),
                "delta1d": None, "delta1w": None, "percentile1y": None, "percentile5y": None,
                "threshold": "", "weight": 10, "confidence": 1.0,
                "source": "FRED SP500（自动）", "note": "价格高于均线=动量健康（顺势）",
                "what": "标普 500 现价相对 125 天均线的距离。线上＝中期趋势向上；跌破＝趋势转弱。",
                "meaning": (f'现在 {pct:+.1f}%——'
                            + ('稳稳站在均线上方，中期上升趋势健康。' if pct > 2
                               else '贴着均线附近，趋势不明朗。' if pct > -2
                               else '跌破中期均线，趋势转弱，顺势资金会开始减仓位。')),
            })
        if ma200:
            spx_vs200 = round((spx_now / ma200 - 1) * 100, 1)
        if len(vals) > 60:
            spx_near_high = spx_now >= max(vals[-60:]) * 0.99
        chg = pct_change(vals, 1)
        indices.insert(0, {"symbol": "SPX", "name": "标普500", "price": round(spx_now, 2),
                           "changePct": chg, "vs200d": spx_vs200, "date": spx_rows[-1][0]})

    # 8) 避险需求（SPY vs TLT）
    if y.get("spy") and y.get("tlt") and len(y["spy"]) > 21 and len(y["tlt"]) > 21:
        d = round((y["spy"][-1] / y["spy"][-21] - 1) * 100 - (y["tlt"][-1] / y["tlt"][-21] - 1) * 100, 2)
        s = score_safehaven(d)
        components.append({
            "key": "safehaven", "name": "避险需求（股 vs 债 20日）", "group": "信用",
            "value": f"{d:+.1f}%", "score": round(s), "zone": zone_of(s),
            "delta1d": None, "delta1w": None, "percentile1y": None, "percentile5y": None,
            "threshold": "", "weight": 10, "confidence": 0.9, "source": "Yahoo SPY/TLT（自动）",
            "note": "股票跑赢国债=风险偏好高",
            "what": "标普 500 与长期国债（TLT）的 20 日涨幅差。股票明显跑赢＝钱愿意冒险；国债跑赢＝钱在往安全资产躲。",
            "meaning": (f'20 日股债差 {d:+.1f}%——'
                        + ('股票明显跑赢，市场风险偏好高。' if d > 1
                           else '国债明显跑赢，钱在避险，要小心。' if d < -1
                           else '股债基本打平，没有明显的避险或冒险倾向。')),
        })

    # ---------- 完整使用指南（注入各分项） ----------
    GUIDES = {
        "credit": "数据：FRED 每日自动更新（ICE 美银高收益债指数），权重全套系统最高（20%）。怎么看：利差低＝市场不怕违约＝贪婪；单看高低没意义，要看「从低位走阔的速度」。行动规则：≥3.75% 提高警惕；≥5% 或 30 天 +50bp 触发减仓评估——历史上领先股市 2–4 周。为什么最重要：垃圾债投资者是第一批要求风险补偿的人，信用市场比股市诚实。局限：对突发黑天鹅（疫情式跳空）不提前，它防的是「温水煮青蛙」。",
        "volatility": "数据：FRED VIXCLS 每日自动（前一交易日收盘）。怎么看：只看一年分位、不看绝对值——VIX 中枢会随市场结构漂移。行动规则：分位 >90%＝恐慌区，历史上是买点不是卖点；<10%＝自满区，是想对冲的人买保险最便宜的时候。局限：0DTE 期权普及后 VIX 被结构性压低，必须和 PCR、期限结构交叉验证。",
        "vixterm": "数据：Yahoo 自动（限流时降级，不参与合成）。怎么看：>1 倒挂＝市场在为眼前的具体事件应激（加息/选举/危机）。行动规则：倒挂不是卖点；倒挂解除回落到 1 以下才是买点——2010 年来之后 5 日胜率 88%。局限：依赖 Yahoo，偶发缺数。",
        "putcall": "数据：人工周更。你自己查：CBOE 官网 cboe.com → Market Statistics → Daily Market Statistics 里的「TOTAL PUT/CALL RATIO」，免费公开；或直接把数字发我。怎么看：>1＝恐慌性买保护（反向看是机会）；<0.7＝投机过热。行动规则：配合一年滚动分位，>90% 分位是反向买点。局限：反映的是期权玩家情绪，不等于全市场。",
        "breadth": "数据：人工周更。你自己查：indexindicators.com 搜「% of S&P 500 Stocks Above 200-Day Moving Average」，免费；或 barchart.com；或直接发我数字。怎么看：>70%＝普涨健康牛；55% 上下＝只有巨头在涨；<25%＝恐慌出清。行动规则：<25% 反向布局；从低位修复过 50% 是牛市确认。局限：巨头市值占比极高的时代，广度可以长期偏弱而指数照涨，所以要和 RSP/SPY 联看。",
        "rspspy": "数据：Yahoo 自动。怎么看：等权指数 20 日涨幅 − 市值加权 20 日涨幅，负数＝涨幅集中在少数巨头。行动规则：指数接近新高但这个值 <−2%，触发「集中度警报」——靠少数股票撑着的上涨结构脆弱。",
        "momentum": "数据：FRED SP500 每日自动。怎么看：现价在 125 日均线上方＝中期趋势向上。行动规则：它是顺势确认指标，不是反向指标——趋势在就别逆势做空；跌破均线说明顺势资金会开始减仓。",
        "safehaven": "数据：Yahoo 自动（SPY vs TLT）。怎么看：股票 20 日涨幅 − 国债 20 日涨幅，负值＝钱在往安全资产躲。行动规则：单独不触发行动，用于验证其他信号——如果 VIX 低但国债在偷偷走强，说明聪明钱在避险。",
    }
    for c in components:
        c["guide"] = GUIDES.get(c["key"], "")

    # NDX 指数行
    ndx_rows = data.get("ndxfb")
    if y.get("ndx") and len(y["ndx"]) > 1:
        chg = round((y["ndx"][-1] / y["ndx"][-2] - 1) * 100, 2)
        indices.insert(1, {"symbol": "NDX", "name": "纳斯达克100", "price": round(y["ndx"][-1], 2),
                           "changePct": chg})
    elif ndx_rows:
        vals = [v for _, v in ndx_rows]
        chg = pct_change(vals, 1)
        indices.insert(1, {"symbol": "NDX", "name": "纳指综合（NDX 降级）",
                           "price": round(vals[-1], 2), "changePct": chg, "date": ndx_rows[-1][0]})

    # ---------- 组合专属警报：实际利率 + 净流动性 ----------
    portfolio = {}
    if "dfii10" in data:
        rows = data["dfii10"]
        vals = [v for _, v in rows]
        mc = bp_change(vals, 21)  # 约一个月交易日
        rr30 = None
        if "dfii30" in data:
            v30 = [v for _, v in data["dfii30"]]
            rr30 = {"value": round(v30[-1], 2), "monthChangeBp": bp_change(v30, 21)}
        portfolio["realRate"] = {
            "value": round(vals[-1], 2), "date": rows[-1][0],
            "monthChangeBp": mc,
            "realRate30": rr30,
            "status": "headwind" if (mc is not None and mc > 30) else
                      ("tailwind" if (mc is not None and mc < -20) else "neutral"),
            "note": ">+30bp/月=长久期组合独立逆风（可无视情绪光谱直接生效）",
            "what": "实际利率＝名义利率剔除通胀后的「真实借钱成本」。你的组合（科技股/创新药/火箭公司）利润大多在很远的未来，对实际利率最敏感。",
            "meaning": (f'10 年期现在 {vals[-1]:.2f}%——2008–2020 年长期在 0–1%，现在是十几年来的高位区，对高估值成长资产是持续的地心引力。'
                        + (f'30 年期 {rr30["value"]:.2f}%。' if rr30 else '')
                        + (f'近一个月变动 {mc:+.0f}bp，属于平稳（±30bp 以内）。' if mc is not None and abs(mc) <= 30
                           else f'近一个月变动 {mc:+.0f}bp，单边快速上行，估值压缩通道打开，需要行动。' if mc is not None and mc > 30
                           else f'近一个月变动 {mc:+.0f}bp，快速下行，对长久期资产是顺风。' if mc is not None else '')),
            "guide": "完整逻辑：你的持仓（SpaceX/Rocket Lab/苹果/Google/创新药/量子计算）现金流几乎都在远期，DCF 估值对实际利率最敏感——这就是为什么它有权无视情绪光谱直接亮红灯。怎么用：① 看绝对水位——2% 以上就是成长股估值的高压区；② 看月度变动——±30bp 以内算平稳，>+30bp/月是估值压缩通道（2022 年纳指 -33% 就是这条通道）；③ 10Y 和 30Y 分开看——30Y 还叠加财政赤字和养老金久期需求，30Y 单独快速上行通常意味着发债供给冲击。局限：它是慢变量，不告诉你哪天跌，只告诉你「这段时间估值天花板在被压低」。",
        }
    if all(k in data for k in ("walcl", "tga", "rrp")):
        w, t, r = data["walcl"], data["tga"], data["rrp"]
        t_map, r_map = dict(t), dict(r)
        t_dates = sorted(t_map); r_dates = sorted(r_map)
        def last_le(dates, m, d):
            c = [x for x in dates if x <= d]
            return m[c[-1]] if c else None
        nl = []
        for d, wv in w:
            tv, rv = last_le(t_dates, t_map, d), last_le(r_dates, r_map, d)
            if tv is not None and rv is not None:
                nl.append((d, wv / 1000.0 - tv / 1000.0 - rv))  # WALCL/WTREGEN 百万→十亿；RRP 已是十亿
        if len(nl) > 30:
            nl_vals = [v for _, v in nl]
            mom4 = nl_vals[-1] - nl_vals[-5]
            chgs = [nl_vals[i] - nl_vals[i - 4] for i in range(4, len(nl_vals))]
            recent = chgs[-104:]
            mu = sum(recent) / len(recent)
            var = sum((x - mu) ** 2 for x in recent) / len(recent)
            sd = var ** 0.5 or 1
            z = round((mom4 - mu) / sd, 2)
            portfolio["netLiquidity"] = {
                "value": round(nl_vals[-1], 0), "date": nl[-1][0],
                "mom4wBn": round(mom4, 0), "z4w": z,
                "status": "tailwind" if z > 1 else ("headwind" if z < -1 else "neutral"),
                "note": "WALCL−TGA−RRP；4 周动量 z>+1 顺风 / z<−1 逆风",
                "what": "美国金融体系里真正可动用的钱：美联储投放的钱（资产负债表）− 趴在财政部账户的钱（TGA）− 货币基金停泊在美联储的钱（逆回购）。",
                "meaning": (f'现在约 {nl_vals[-1] / 1000:.2f} 万亿美元。绝对值参考意义不大，关键看 4 周变化的速度和方向：'
                            + f'近 4 周 {"增加" if mom4 > 0 else "减少"}了约 {abs(mom4 * 10):.0f} 亿美元（{mom4:+.0f} Bn），'
                            + ('z=+' + str(z) + '，放水速度明显快于常态，宏观水位顺风。' if z > 1
                               else 'z=' + str(z) + '，收水速度明显快于常态，宏观水位逆风。' if z < -1
                               else f'z={z}，在正常波动范围内，水位平稳。')),
                "guide": "完整逻辑：三个部分分别是——美联储资产负债表（放出去的总水量）、财政部 TGA 账户（发债收上来还没花掉的钱，是抽水）、隔夜逆回购（货币基金停泊在美联储吃利息的钱，也是抽水）。怎么用：别看绝对值，看 4 周变化速度的 z 分数——z>+1 放水快于常态（顺风），z<−1 收水快于常态（逆风）。典型剧本：财政部大发债（TGA 上升）+ 逆回购见底后，净流动性就会趋势性收紧。局限：它解释的是「水多水少」，不解释水流向哪——水多时利好所有风险资产，水少时先跌的往往是估值最高的（又绕回你的组合）。",
            }

    # ---------- 宏观领先指标（6–18 个月慢变量，不进情绪光谱） ----------
    macro = {}
    if "t10y3m" in data:
        rows = data["t10y3m"]
        vals = [v for _, v in rows]
        cur = vals[-1]
        chg3m = round(cur - vals[-63], 2) if len(vals) > 63 else None
        macro["curve"] = {
            "value": round(cur, 2), "date": rows[-1][0], "chg3m": chg3m,
            "status": "headwind" if cur < 0 else "neutral",
            "what": "10 年期国债收益率 − 3 个月国债收益率。正常为正（钱借越久利息越高）。倒挂（<0）＝市场在押注未来被迫降息，是历史上最可靠的衰退先行指标，领先 12–18 个月。",
            "meaning": (f'现在 {cur:+.2f}%，'
                        + ('倒挂中。注意历史规律：倒挂初期股市往往还在涨，真正的近端危险信号是倒挂之后快速回正（熊陡）。' if cur < 0
                           else '未倒挂。')
                        + (f'近 3 个月变动 {chg3m:+.2f} 个百分点——快速陡峭化，需留意是否属于"倒挂后回正"的衰退临近模式。'
                           if chg3m is not None and chg3m > 0.5 else '')),
            "threshold": "<0 倒挂警戒 · 倒挂后快速回正＝衰退临近的近端信号",
            "guide": "完整判断逻辑（四步走）：① 倒挂（<0）＝衰退先行信号，1960 年来每次衰退前都倒挂，仅一次假阳性，领先 12–18 个月；② 但倒挂≠马上跌——历史上倒挂期内标普平均还能涨约 12%，因为衰退没来、盈利还撑得住，所以倒挂时别清仓；③ 真正的近端危险信号是倒挂后「快速回正」（熊陡）——那通常意味着联储开始被迫降息救火，衰退 3–6 个月内到；④ 行动规则：倒挂＝把风险预算降一档；熊陡＝进入防御姿态。当前未倒挂，正常。它回答的问题是「未来一年经济衰退的概率」，和情绪光谱（未来几周）是两个时间尺度。",
        }
    if "icsa" in data:
        rows = data["icsa"]
        vals = [v for _, v in rows]
        cur = vals[-1]
        ma4 = sum(vals[-4:]) / 4 if len(vals) >= 4 else None
        yoy = None
        if len(vals) > 56 and ma4:
            base = sum(vals[-56:-52]) / 4
            if base:
                yoy = round((ma4 / base - 1) * 100, 1)
        macro["claims"] = {
            "value": round(cur / 10000, 1), "date": rows[-1][0],
            "ma4": round(ma4 / 10000, 1) if ma4 else None, "yoy4w": yoy,
            "status": "headwind" if (yoy is not None and yoy > 10) else "neutral",
            "what": "每周首次申请失业金的人数（每周四发布）。劳动力市场最高频的体温计——裁员拐点通常领先股市盈利和官方就业数据 2–6 个月。",
            "meaning": (f'最近一周 {cur / 10000:.1f} 万人，4 周均值 {ma4 / 10000:.1f} 万'
                        + (f'，同比 {yoy:+.0f}%——' if yoy is not None else '。')
                        + ('上升超 10%，劳动力市场明显转弱——历史上这是衰退前兆，对股票是中期逆风。' if yoy is not None and yoy > 10
                           else '同比平稳，劳动力市场没有恶化迹象。' if yoy is not None else '')),
            "threshold": "4周均值同比 >+10%＝劳动力恶化信号",
            "guide": "完整逻辑：企业裁员先于官方失业率上升，所以初请是劳动力市场最早的裂缝。怎么用：① 不看单周（噪音大），看 4 周均值的同比；② 同比 >+10% 且持续＝裁员周期启动，领先企业盈利下滑 2–6 个月，对「软着陆」叙事要降置信度；③ 反过来，同比持续为负（像现在的 -10%）＝劳动力市场仍在改善，是牛市的底层支撑之一。局限：它是经济指标不是市场指标——数据好不代表股市马上涨，它的作用是校准你对「宏观背景」的基本假设。",
        }

    # ---------- P2 货币市场压力（SOFR−EFFR 基差）：金融体系管道的爆裂预警 ----------
    money_stress = {}
    if "sofr" in data and "effr" in data:
        s_map = dict(data["sofr"]); e_map = dict(data["effr"])
        common = sorted(set(s_map) & set(e_map))
        if len(common) > 20:
            spreads = [(d, round((s_map[d] - e_map[d]) * 100, 1)) for d in common]  # 基点
            cur_bp = spreads[-1][1]
            vals = [v for _, v in spreads]
            money_stress = {
                "spreadBp": cur_bp, "date": spreads[-1][0],
                "sofr": s_map[common[-1]], "effr": e_map[common[-1]],
                "percentile1y": pct_rank(vals[-250:], cur_bp),
                "status": "red" if cur_bp >= 25 else ("yellow" if cur_bp >= 10 else "neutral"),
                "what": "SOFR（机构拿国债抵押借隔夜钱的价格）− EFFR（联邦基金有效利率）。正常贴着 0 波动；走阔＝抵押借钱市场在加价——钱紧了，是金融体系管道层面最早的压力信号。",
                "meaning": (f"现在 {cur_bp:+.0f}bp。"
                            + ("管道应激区——2019 年 9 月回购危机前就是这个读数先跳。货币市场的问题会在几天内传导到所有风险资产，视同信用红灯处理。" if cur_bp >= 25
                               else "开始偏离零轴，货币市场边际趋紧，盯住它是否继续走阔。" if cur_bp >= 10
                               else "贴着零轴，货币市场管道通畅，没有压力迹象。")),
                "threshold": "≥10bp 关注 · ≥25bp 管道应激（2019-09 回购危机前兆区）",
                "guide": "完整逻辑：风险传导的顺序是「货币市场 → 信用债 → 股票」。SOFR−EFFR 比 HY 利差更上游——2019 年 9 月回购危机时它一夜跳到 +100bp 以上，而股市几天后才反应过来。怎么用：平时不用看（贴着 0 是常态）；一旦 ≥10bp 说明银行间抵押融资成本在加价，可能是季末效应（看是否快速回落），也可能是真实紧缩；≥25bp 且持续两天以上＝管道事件，直接按红灯处理，不等其他指标确认。局限：季末/年末有机械性脉冲，单日冲高要等第二天确认。",
            }

    # ---------- P2 IG 投资级利差：信用层的另一半 ----------
    credit_ig = {}
    if "ig" in data:
        ig_rows = data["ig"]; ig_vals = [v for _, v in ig_rows]
        ig_cur = ig_vals[-1]; ig_chg30 = bp_change(ig_vals, 21)
        credit_ig = {
            "value": round(ig_cur, 2), "date": ig_rows[-1][0], "chg30bp": ig_chg30,
            "percentile1y": pct_rank(ig_vals[-250:], ig_cur),
            "status": "headwind" if (ig_chg30 is not None and ig_chg30 >= 40) else "neutral",
            "what": "投资级（BBB 以上好公司）债券相对国债的利差。HY（垃圾债）看的是「怕不怕违约」，IG 看的是「优质企业融资贵不贵」——IG 走阔通常比 HY 更早、更平滑。",
            "meaning": (f"现在 {ig_cur:.2f}%，30 日变动 {ig_chg30:+.0f}bp。"
                        + ("优质企业的融资成本在快速抬升——信用收紧开始从垃圾债蔓延到好公司，这是比 HY 更值得警惕的升级信号。" if ig_chg30 is not None and ig_chg30 >= 40
                           else "优质企业融资环境平稳，信用压力还局限在垃圾债层面，没有蔓延。")),
            "threshold": "30日 +40bp＝信用收紧蔓延到优质企业",
            "guide": "完整逻辑：信用危机的扩散路径通常是 HY 先崩、IG 后跟上；如果只有 HY 走阔而 IG 不动，说明压力是局部性的（如 AI 发债链）；IG 也开始快速走阔＝压力升级为系统性。和 HY 配对看：HY 是温度计，IG 是确认器。局限：IG 利差波动小，信号少但准。",
        }

    # ---------- 估值自动化：multpl 免费源（SPX PE-TTM + Shiller CAPE） ----------
    def multpl(url, pattern):
        try:
            html = curl(url, timeout=15)
            m = re.search(pattern, html)
            return float(m.group(1)) if m else None
        except Exception as e:
            errors.append(f"multpl 估值（已降级）: {str(e)[:60]}")
            return None
    pe_auto = multpl("https://www.multpl.com/s-p-500-pe-ratio", r"Current S&P 500 PE Ratio is ([0-9.]+)")
    shi_auto = multpl("https://www.multpl.com/shiller-pe", r"Current Shiller PE Ratio is ([0-9.]+)")

    # 估值表：人工条目打底；自动 PE/CAPE 成功时覆盖 SPX PE-TTM 行并追加 CAPE
    valuation = list(curated.get("valuation", []))
    if pe_auto:
        valuation = [v for v in valuation if v.get("name") != "SPX PE-TTM"]
        valuation.insert(0, {"name": "SPX PE-TTM ⚡", "value": f"{pe_auto:.2f}x",
            "ref": "历史均值≈16 · <20 合理 / 20–25 偏贵 / >25 很贵",
            "zone": "很贵" if pe_auto > 25 else ("偏贵" if pe_auto > 20 else "合理"), "auto": True})
    if shi_auto:
        valuation.insert(1 if pe_auto else 0, {"name": "Shiller PE (CAPE) ⚡", "value": f"{shi_auto:.2f}x",
            "ref": "历史均值≈17 · >35 历史泡沫区（1929/2000/2021）",
            "zone": "泡沫区" if shi_auto > 35 else ("偏贵" if shi_auto > 27 else "合理"), "auto": True})

    # ---------- 合成：光谱层（反向）× 确认层（顺势） ----------
    groups = {}
    for c in components:
        groups.setdefault(c["group"], []).append(c)
    group_scores = {}
    for g, items in groups.items():
        wsum = sum(c["weight"] * c["confidence"] for c in items)
        group_scores[g] = sum(c["score"] * c["weight"] * c["confidence"] for c in items) / wsum if wsum else 50

    GROUP_DEFS = {
        "信用": "企业借钱市场上，投资者胆子有多大（信用利差、股债避险）",
        "波动率": "期权市场为「保险」定什么价（VIX、期限结构、看跌看涨比）",
        "广度": "上涨是普涨还是只靠少数巨头（200日线上占比、等权强弱、动量）",
    }
    def gstate(score):
        if score >= 80: return "极度乐观，毫无防备"
        if score >= 65: return "偏乐观"
        if score >= 55: return "略偏乐观"
        if score > 45: return "中性"
        if score > 35: return "略偏谨慎"
        if score > 20: return "偏恐慌"
        return "极度恐慌，反向布局区"
    group_explains = {g: {"def": GROUP_DEFS.get(g, ""), "state": gstate(s)}
                      for g, s in group_scores.items()}
    spectrum = round(sum(group_scores.values()) / len(group_scores)) if group_scores else 50

    confirms = []
    if "momentum" in [c["key"] for c in components]:
        confirms.append(next(c["score"] for c in components if c["key"] == "momentum"))
    if rsp_rel is not None:
        confirms.append(65 if rsp_rel > -2 else 30)
    if portfolio.get("netLiquidity"):
        confirms.append(70 if portfolio["netLiquidity"]["status"] == "tailwind"
                        else 30 if portfolio["netLiquidity"]["status"] == "headwind" else 50)
    confirmation = "强" if (confirms and sum(confirms) / len(confirms) >= 60) else \
                   "弱" if (confirms and sum(confirms) / len(confirms) <= 40) else "中"

    # ---------- P1 板块温度计：指数掩盖的东西 ----------
    sectors = {}
    if ext.get("sox"):
        rows = ext["sox"]; vals = [v for _, v in rows]
        sectors["sox"] = {"name": "费城半导体 SOX", "value": round(vals[-1], 0), "date": rows[-1][0],
                          "d20": pct_change(vals, 20), "d60": pct_change(vals, 60),
                          "what": "全球半导体景气的一线温度计——你的 AI/科技敞口的真实位置在这里，不在 SPX。"}
    if ext.get("rut"):
        rows = ext["rut"]; vals = [v for _, v in rows]
        sectors["rut"] = {"name": "罗素2000 小盘股", "value": round(vals[-1], 0), "date": rows[-1][0],
                          "d20": pct_change(vals, 20), "d60": pct_change(vals, 60),
                          "what": "对利率最敏感的指数。它比 SPX 先跌，是利率冲击是否传导到实体经济的验证器。"}
    spx_d20 = None
    if spx_rows:
        spx_d20 = pct_change([v for _, v in spx_rows], 20)
    if ext.get("ndx_em") and spx_d20 is not None:
        rows = ext["ndx_em"]; vals = [v for _, v in rows]
        ndx_d20 = pct_change(vals, 20)
        if ndx_d20 is not None:
            sectors["ndxrel"] = {"name": "科技相对强弱（NDX−SPX 20日）", "value": f"{ndx_d20 - spx_d20:+.1f}%",
                                 "date": rows[-1][0], "d20": round(ndx_d20 - spx_d20, 1),
                                 "what": "纳指100 与标普500 的 20 日涨幅差。负值＝科技股跑输大盘，资金在从科技切向别处。"}

    # ---------- P1 A股：全球 AI 链条的先行指标 ----------
    cn_market = {}
    for key, name in [("kc50", "科创50"), ("cyb", "创业板指"), ("sh", "上证指数"), ("cnsemi", "半导体板块")]:
        if ext.get(key):
            rows = ext[key]; vals = [v for _, v in rows]
            cn_market[key] = {"name": name, "value": round(vals[-1], 2), "date": rows[-1][0],
                              "d1": pct_change(vals, 1), "d20": pct_change(vals, 20), "d60": pct_change(vals, 60)}
    if cn_market.get("kc50", {}).get("d60") is not None and sectors.get("sox", {}).get("d60") is not None:
        rel = round(cn_market["kc50"]["d60"] - sectors["sox"]["d60"], 1)
        cn_market["lead"] = {"kc50VsSox60d": rel,
                             "note": "科创50 与 SOX 的 60 日涨幅差。2026 年 A股半导体 7 月见顶，领先本轮美股 AI 调整约 3 个月——A股科技是全球 AI 链条的前沿哨兵。"}
    # P2：两融·融资余额——A股杠杆资金的进退（东财 T+1）
    if ext.get("rzrq"):
        rz = ext["rzrq"]
        bal = [r[1] for r in rz]
        rz_d20 = pct_change(bal, 20)
        cn_market["margin"] = {
            "name": "两融·融资余额", "value": round(bal[-1] / 1e4, 2), "unit": "万亿",
            "date": rz[-1][0], "d20": rz_d20,
            "netBuy1d": round(rz[-1][2], 0),
            "what": "全市场散户和游资借券商的钱买股票的余额。A股最诚实的情绪指标之一——这是加了杠杆的真金白银，上涨需要它进，崩塌时它被迫撤。",
            "note": "20 日变动 >+5%＝杠杆资金跑步进场（情绪热）；<-5%＝被迫去杠杆（下跌有惯性，别急着接）。T+1 数据。",
        }

    # ---------- P2 黄金模块：因果链（驱动因 → 环境合成 → 动量确认） ----------
    gold = {}
    gold_px, gold_chg = (ext.get("gold") or (None, None))
    gold_hist = ext.get("gold_hist") or []
    df_vals = [v for _, v in data["dfii10"]] if data.get("dfii10") else []
    dxy_vals = [v for _, v in data["dxy"]] if data.get("dxy") else []
    if gold_px or df_vals:
        rr_chg20 = bp_change(df_vals, 20) if len(df_vals) > 20 else None
        dxy_chg20 = pct_change(dxy_vals, 20) if len(dxy_vals) > 20 else None
        hist_vals = [v for _, v in gold_hist]
        ma20 = round(sum(hist_vals[-20:]) / 20, 1) if len(hist_vals) >= 20 else None
        px_now = gold_px or (hist_vals[-1] if hist_vals else None)
        vs_ma20 = round((px_now / ma20 - 1) * 100, 1) if (ma20 and px_now) else None

        # 驱动因打分：实际利率权重 70%（第一变量），美元 30%（2022 年后被削弱）
        rr_s = (1 if rr_chg20 < 0 else 0) if rr_chg20 is not None else None
        dxy_s = (1 if dxy_chg20 < -0.5 else (0 if dxy_chg20 > 0.5 else 0.5)) if dxy_chg20 is not None else None
        env_score = None
        if rr_s is not None and dxy_s is not None:
            env_score = round(70 * rr_s + 30 * dxy_s)
        confirmed = bool(vs_ma20 is not None and vs_ma20 > 0)
        # 结论矩阵：环境（因）× 动量（果）
        if env_score is None:
            verdict, vstatus = "数据不足", "neutral"
        elif env_score >= 65 and confirmed:
            verdict, vstatus = "多头趋势确认", "tailwind"
        elif env_score >= 65:
            verdict, vstatus = "偏多环境·趋势未确认", "neutral"
        elif env_score < 35 and not confirmed:
            verdict, vstatus = "双杀确认·远离", "headwind"
        elif env_score < 35:
            verdict, vstatus = "环境偏空·反弹是逃命", "headwind"
        else:
            verdict, vstatus = "中性拉锯", "neutral"

        gvars = []
        if rr_chg20 is not None:
            gvars.append({
                "name": "① 实际利率 · 第一驱动（权重 70%）",
                "value": f"{df_vals[-1]:.2f}%", "chg": f"20日 {rr_chg20:+.0f}bp",
                "mech": ("利率下行 → 持有黄金的机会成本下降" if rr_chg20 < 0 else "利率上行 → 持有黄金的机会成本抬升"),
                "impact": "推升金价" if rr_chg20 < 0 else "压制金价",
                "dir": "fav" if rr_chg20 < 0 else "adv",
                "note": "黄金不生息，实际利率就是「拿着它亏多少利息」。这是最强因果变量，方向看 20 日变动，别看绝对值。"})
        if dxy_chg20 is not None:
            gvars.append({
                "name": "② 美元指数 · 第二驱动（权重 30%）",
                "value": f"{dxy_vals[-1]:.1f}", "chg": f"20日 {dxy_chg20:+.1f}%",
                "mech": ("美元走弱 → 非美买家变便宜 + 避险替代减弱" if dxy_chg20 < -0.5
                         else "美元走强 → 计价压力 + 避险替代增强" if dxy_chg20 > 0.5
                         else "美元走平 → 无显著计价压力"),
                "impact": "推升金价" if dxy_chg20 < -0.5 else ("压制金价" if dxy_chg20 > 0.5 else "中性"),
                "dir": "fav" if dxy_chg20 < -0.5 else ("adv" if dxy_chg20 > 0.5 else "mid"),
                "note": "「美元涨金必跌」是 2022 年前的旧规则——央行购金打破了它。现在美元只是辅助变量，权重故意压低。"})
        gvars.append({
            "name": "③ 央行购金 · 结构底（不计分，定性）",
            "value": "季度数据", "chg": "WGC 季报",
            "mech": "央行持续买入 → 吸收实物供给 → 抬高长期底部",
            "impact": "托底（不计分）", "dir": "mid",
            "note": "本轮金价在高利率+强美元下仍创历史新高的真正原因。它不定方向，定「跌不深」。"})

        gold = {
            "price": round(gold_px, 1) if gold_px else None, "chg1d": gold_chg,
            "history": [[d, v] for d, v in gold_hist[-120:]],
            "ma20": ma20, "vsMa20": vs_ma20,
            "realRate10": {"value": round(df_vals[-1], 2), "chg20bp": rr_chg20} if df_vals else None,
            "dxyBroad": {"value": round(dxy_vals[-1], 1), "chg20": dxy_chg20} if dxy_vals else None,
            "vars": gvars,
            "envScore": env_score, "confirmed": confirmed,
            "verdict": verdict, "status": vstatus,
            "momentum": ({"value": f"{px_now:,.0f}",
                          "chg": (f"均线上方 +{vs_ma20}%" if vs_ma20 >= 0 else f"均线下方 {vs_ma20}%"),
                          "mech": "价格站上 20 日线 → 趋势资金进场" if confirmed else "价格压在 20 日线下 → 趋势资金观望",
                          "impact": "确认" if confirmed else "未确认",
                          "dir": "fav" if confirmed else "adv",
                          "note": "动量是「果」不是「因」：它不预测方向，只确认环境评分有没有兑现成趋势。"} if vs_ma20 is not None else None),
            "what": "黄金不生息，定价锚是「持有它的机会成本」= 实际利率（第一驱动，权重 70%）；美元是第二驱动（计价货币，权重 30%，2022 年后被央行购金削弱）；央行购金是结构底（不定方向、定「跌不深」）。价格动量是结果，只负责确认。",
            "meaning": (f"环境评分 {env_score}/100（因：实际利率+美元），动量{'已确认' if confirmed else '未确认'}（果：价格 vs 20日线）→ {verdict}。"
                        + {"多头趋势确认": "环境和趋势同向——顺趋势持仓的最舒服状态。",
                           "偏多环境·趋势未确认": "驱动因已转好但价格还没站上趋势线——左侧观察区，别追；动量翻正（价格收复 20 日线）就是确认信号。",
                           "双杀确认·远离": "环境和趋势同向向下——任何反弹都别当反转。",
                           "环境偏空·反弹是逃命": "价格在均线上但驱动因逆风——这种背离通常以价格补跌收场。",
                           "中性拉锯": "多空力量均衡，等实际利率给出方向。"}.get(verdict, "")),
            "guide": "怎么读这张图：上行三行是「因」（谁在驱动金价），中间评分条是「因的合成」，最下一行是「果」（趋势是否确认）。① 预判拐点看「因」——实际利率 20 日方向拐头，领先金价；② 动手看「果」——评分 ≥65 且价格收复 20 日线才是右侧买点；③ 「因」与「果」背离时信「因」，但等「果」确认再加仓。",
        }


    # ---------- P2 信用链：管道（因）→ IG（传导）→ HY（末端定价）→ 股市（果） ----------
    credit_chain = {}
    if hy and money_stress and credit_ig:
        sofr_bp = money_stress.get("spreadBp")
        ig30 = credit_ig.get("chg30bp")
        hy30 = hy.get("chg30bp")
        sofr_s = (1 if sofr_bp < 10 else (0.5 if sofr_bp < 25 else 0)) if sofr_bp is not None else None
        ig_s = (1 if ig30 < 15 else (0.5 if ig30 < 40 else 0)) if ig30 is not None else None
        hy_s = (1 if hy30 < 15 else (0.5 if hy30 < 50 else 0)) if hy30 is not None else None
        env = round(30 * sofr_s + 30 * ig_s + 40 * hy_s) if None not in (sofr_s, ig_s, hy_s) else None
        if env is None:
            cverdict, cstatus = "数据不足", "neutral"
        elif env >= 80:
            cverdict, cstatus = "信用环境平静", "tailwind"
        elif env >= 60:
            cverdict, cstatus = "局部压力：末端在抖、上游未坏", "neutral"
        elif env >= 40:
            cverdict, cstatus = "压力蔓延中", "yellow"
        else:
            cverdict, cstatus = "系统性信用事件", "red"
        crows = []
        if sofr_bp is not None:
            crows.append({
                "name": "① 货币市场管道 SOFR−EFFR · 最上游（权重 30%）",
                "value": f"{sofr_bp:+.0f}bp", "chg": money_stress.get("date", ""),
                "mech": ("贴零轴 → 机构抵押融资通畅" if sofr_bp < 10
                         else "偏离零轴 → 管道开始加价、钱边际变紧" if sofr_bp < 25
                         else "大幅跳升 → 管道应激（2019-09 回购危机前兆区）"),
                "impact": "无压力" if sofr_bp < 10 else ("趋紧" if sofr_bp < 25 else "应激"),
                "dir": "fav" if sofr_bp < 10 else ("mid" if sofr_bp < 25 else "adv"),
                "note": "风险的物理源头。它不出事则已，一出事所有资产几天内连锁。季末有机械脉冲，需次日确认。"})
        if ig30 is not None:
            crows.append({
                "name": "② IG 投资级利差 · 传导层（权重 30%）",
                "value": f"{credit_ig['value']:.2f}%", "chg": f"30日 {ig30:+.0f}bp",
                "mech": ("变动 <+15bp → 优质企业融资平稳，压力没传上来" if ig30 < 15
                         else "+15~40bp → 好公司融资开始变贵，传导启动" if ig30 < 40
                         else "≥+40bp → 压力蔓延到优质企业，升级为系统性"),
                "impact": "未蔓延" if ig30 < 15 else ("蔓延中" if ig30 < 40 else "已蔓延"),
                "dir": "fav" if ig30 < 15 else ("mid" if ig30 < 40 else "adv"),
                "note": "信用危机的扩散判据：只有 HY 抖而 IG 不动＝局部事件（如 AI 发债链）；IG 跟上＝系统性。IG 是确认器。"})
        if hy30 is not None:
            crows.append({
                "name": "③ HY 高收益债利差 · 末端定价（权重 40%）",
                "value": f"{hy['value']:.2f}%", "chg": f"30日 {hy30:+.0f}bp",
                "mech": ("变动 <+15bp → 违约风险定价平稳" if hy30 < 15
                         else "+15~50bp → 市场在重新定价违约风险" if hy30 < 50
                         else "≥+50bp → 降风险触发区（领先股市 2–4 周）"),
                "impact": "平稳" if hy30 < 15 else ("重新定价中" if hy30 < 50 else "降风险警报"),
                "dir": "fav" if hy30 < 15 else ("mid" if hy30 < 50 else "adv"),
                "note": "垃圾债投资者是第一批要求风险补偿的人。它对风险偏好最敏感、动得最早——所以权重最高，但它只告诉你「疼」，不告诉你「病根」。"})
        equity_row = None
        if spx_d20 is not None:
            transmitted = spx_d20 <= -5
            equity_row = {
                "name": "④ 股市确认 · SPX 20 日",
                "value": f"{spx_d20:+.1f}%", "chg": "信用领先股市 2–4 周",
                "mech": ("股市已大跌 → 信用压力兑现成价格" if transmitted
                         else "信用在报警、股市尚未充分下跌 → 提前量还在"),
                "impact": "已传导" if transmitted else "未传导",
                "dir": "adv" if transmitted else "mid",
                "note": "信用链的价值就在于这个「未传导」窗口——它比股市早 2–4 周。窗口内执行动作，等股市跌了你已经减完。"}
        credit_chain = {
            "envScore": env, "verdict": cverdict, "status": cstatus,
            "rows": crows, "equity": equity_row,
            "what": "信用不是单一指标，是一条传导链：货币市场管道（SOFR−EFFR，钱本身紧不紧）→ IG 优质企业利差（压力有没有蔓延到好公司）→ HY 垃圾债利差（违约风险的末端定价）→ 最后才轮到股市。每一层回答的问题不同：管道=「有没有事」，IG=「大不大」，HY=「疼不疼」，股市=「兑现没」。",
            "meaning": (f"信用环境 {env}/100：{cverdict}。"
                        + {"信用环境平静": "三层都平稳，信用维度没有需要动作的信号。",
                           "局部压力：末端在抖、上游未坏": "HY 在重新定价（30日 +70bp），但管道通畅、IG 未蔓延——压力还锁定在局部（AI 发债链），按局部风险处理：不系统性减仓，但不加仓、盯 IG 是否跟上。",
                           "压力蔓延中": "两层以上恶化——从局部事件升级为系统性风险的过程正在发生，减仓动作要提前到这一档，别等股市确认。",
                           "系统性信用事件": "三层全坏——历史上对应 2008/2020 模式，现金为王。"}.get(cverdict, "")),
            "guide": "怎么读：从上往下是传导方向，从下往上看是定价结果。背离比绝对值更重要——HY 抖而 IG/管道不动＝局部（现在就是这个状态）；IG 跟上＝系统性升级；管道跳升＝一切让路。和黄金链的区别：黄金是「定价锚」链（变量→估值），信用是「传导」链（上游堵→下游炸），所以信用的因在上游、果在股市，动量确认换成了「股市是否已跌」。",
        }

    # ---------- 警报引擎 ----------
    alerts = []
    if hy:
        if hy["value"] >= 5.0 or (hy["chg30bp"] is not None and hy["chg30bp"] >= 50):
            alerts.append({"key": "hy_red", "level": "red", "actionable": True,
                           "text": f'信用利差 **{hy["value"]:.2f}%**（30日 **{hy["chg30bp"]:+.0f}bp**）：降风险触发区'})
        elif hy["value"] >= 3.75:
            alerts.append({"key": "hy_yellow", "level": "yellow", "actionable": True,
                           "text": f'信用利差 {hy["value"]:.2f}% 升至警戒区（≥3.75%），信用领先股市 2–4 周'})
    if vix_ratio:
        if vix_ratio["value"] > 1.0:
            alerts.append({"key": "vixterm_inv", "level": "red", "actionable": True,
                           "text": f'VIX/VIX3M {vix_ratio["value"]:.2f} 倒挂：市场应激，恐慌定价中'})
        elif vix_ratio["prev"] and vix_ratio["prev"] >= 1.0 and vix_ratio["value"] < 1.0:
            alerts.append({"key": "vixterm_relief", "level": "yellow", "actionable": True,
                           "text": "VIX 倒挂解除：历史 5 日胜率 88% 的买点区（分批加仓条件区）"})
    if portfolio.get("realRate", {}).get("status") == "headwind":
        mc = portfolio["realRate"]["monthChangeBp"]
        alerts.append({"key": "realrate_headwind", "level": "red", "actionable": True,
                       "text": f"10Y 实际利率月变动 {mc:+.0f}bp：长久期组合独立逆风（估值压缩通道）"})
    if portfolio.get("netLiquidity", {}).get("status") == "headwind":
        z = portfolio["netLiquidity"]["z4w"]
        alerts.append({"key": "liq_headwind", "level": "yellow", "actionable": False,
                       "text": f"净流动性 4 周动量 z={z:+.1f}：宏观水位边际收紧"})
    if s5th is not None and s5th < 25:
        alerts.append({"key": "breadth_fear", "level": "yellow", "actionable": True,
                       "text": f"S5TH 广度 {s5th:.0f}%：超卖反向区（恐慌端信号，历史高赔率区）"})
    if spx_near_high and rsp_rel is not None and rsp_rel < -2:
        alerts.append({"key": "concentration", "level": "yellow", "actionable": False,
                       "text": f"指数近新高但 RSP/SPY 20日 {rsp_rel:+.1f}%：上涨只剩头部，结构脆弱"})
    vix_p = next((c.get("percentile1y") for c in components if c["key"] == "volatility"), None)
    if vix_p is not None and vix_p >= 90:
        alerts.append({"key": "vix_p90", "level": "yellow", "actionable": False,
                       "text": f"VIX 分位 {vix_p:.0f}%：恐慌区（恐惧端信号权重大于贪婪端）"})
    # P1：板块背离——指数平静但你的敞口在崩
    sox_d20 = (sectors.get("sox") or {}).get("d20")
    if sox_d20 is not None and spx_d20 is not None and spx_d20 > -1.5 and sox_d20 < -5:
        alerts.append({"key": "sector_div", "level": "yellow", "actionable": True,
                       "text": f"板块背离：SPX 20日 {spx_d20:+.1f}% 看似平静，但 **SOX 半导体 20日 {sox_d20:+.1f}%**——你的敞口在板块侧，指数在骗你"})
    # P1：A股先行指标——7月见顶领先美股约3个月
    kc60 = (cn_market.get("kc50") or {}).get("d60")
    if kc60 is not None and kc60 < -15:
        alerts.append({"key": "cn_ai_lead", "level": "yellow", "actionable": False,
                       "text": f"A股科创50 60日 **{kc60:+.1f}%**：A股科技 7 月已见顶，历史领先美股 AI 调整——全球 AI 链条的前沿警报"})

    # P2：货币市场管道压力（最上游，视同信用红灯）
    if money_stress.get("status") == "red":
        alerts.append({"key": "funding_stress", "level": "red", "actionable": True,
                       "text": f"SOFR−EFFR **{money_stress['spreadBp']:+.0f}bp**：货币市场管道应激——比信用利差更上游的警报，按红灯处理"})
    elif money_stress.get("status") == "yellow":
        alerts.append({"key": "funding_watch", "level": "yellow", "actionable": False,
                       "text": f"SOFR−EFFR {money_stress['spreadBp']:+.0f}bp：货币市场边际趋紧，盯是否继续走阔"})
    # P2：IG 利差走阔——信用压力蔓延到优质企业
    if credit_ig.get("status") == "headwind":
        alerts.append({"key": "ig_widen", "level": "yellow", "actionable": False,
                       "text": f"IG 利差 30日 **{credit_ig['chg30bp']:+.0f}bp**：信用收紧蔓延到优质企业，压力从局部升级为系统性"})
    # P2：两融去杠杆——A股杠杆资金被迫撤离
    mg_d20 = (cn_market.get("margin") or {}).get("d20")
    if mg_d20 is not None and mg_d20 <= -5:
        alerts.append({"key": "margin_delev", "level": "yellow", "actionable": False,
                       "text": f"两融余额 20日 **{mg_d20:+.1f}%**：杠杆资金被迫撤离，A股下跌有惯性，别急着接"})

    # 无动作标签的红灯降级为黄灯（对照稿硬规则）
    for a in alerts:
        if a["level"] == "red" and not a["actionable"]:
            a["level"] = "yellow"

    level = "red" if any(a["level"] == "red" for a in alerts) else \
            "yellow" if any(a["level"] == "yellow" for a in alerts) else "green"

    # ---------- 一句话判断（结论先行，大白话，不对称规则） ----------
    verdict = curated.get("verdict", "")
    if level == "red":
        headline = alerts[0]["text"] if alerts else "出现了需要行动的变化，先处理下面的警报。"
    elif level == "yellow":
        headline = "有指标越过了警戒线，见下方黄色警报——先看完再决定动不动。"
    elif spectrum <= 20:
        headline = "市场极度恐慌：历史上这种位置分批买，赢面最大。前提是用闲钱、分批买，别一次打满。"
    elif spectrum <= 35:
        headline = "市场偏恐慌：如果公司基本面没变，现在是挑好货、分批买的时候。"
    elif spectrum >= 85:
        headline = "市场过热：人人都在抢的时候，买到的大概率是贵货。手里的可以拿着，但别加仓，先想好什么情况下卖。"
    elif spectrum >= 65:
        headline = "市场偏热：手里的仓位继续拿着没问题；但现在新买入不划算——价格里已经装进了太多好消息，留给你的空间不大。"
    else:
        headline = "不冷不热，没有信号：照原计划执行就好。情绪系统此刻的作用是告诉你——什么都不用做。"

    # ---------- 利率×股票四象限（P0 状态机，2026-10-19 因果链验证后新增） ----------
    # 单看收益率方向会误判：收益率↓可以是宽松顺风，也可以是避险崩塌。
    # 用「10Y 收益率方向 × SPX 方向」的组合判定主导逻辑（验证见 Obsidian 专题研究 2026-10-19）。
    quadrant = None
    if data.get("us10y") and spx_rows:
        yv = [v for _, v in data["us10y"]]
        sv = [v for _, v in spx_rows]
        dY = bp_change(yv, 10)   # 10 个交易日基点变化
        dS = pct_change(sv, 10)  # 10 个交易日涨跌幅 %
        if dY is not None and dS is not None:
            Y_TH, S_TH = 5.0, 0.3  # 平坦区阈值：±5bp / ±0.3% 以内视为无方向
            y_up, y_dn = dY > Y_TH, dY < -Y_TH
            s_up, s_dn = dS > S_TH, dS < -S_TH
            if y_up and s_up:
                qk, qlabel, qtone = "growth", "增长乐观", "green"
            elif y_up and s_dn:
                qk, qlabel, qtone = "rateshock", "利率冲击", "red"
            elif y_dn and s_up:
                qk, qlabel, qtone = "easing", "宽松顺风", "green"
            elif y_dn and s_dn:
                qk, qlabel, qtone = "riskoff", "风险偏好崩塌", "red"
            else:
                qk, qlabel, qtone = "neutral", "无明确主导", "gray"
            QTEXT = {
                "growth": ("收益率上行 + 股票上涨：市场在为**更强的增长**定价，股债同向但逻辑健康。",
                           "利率上行没有压垮股市＝**增长叙事盖过了折现率压力**。这是牛市中段的典型组合。"),
                "rateshock": ("收益率上行 + 股票下跌：**折现率在压缩估值**，高估值、远期现金流资产首当其冲。",
                              "利率是主导矛盾。2026 年 9 月中–10 月初就是这一象限：10Y 冲上 5.365%（23 年新高），AI/半导体被杀估值。**此象限中不抄底**，等收益率见顶或 Fed 转向信号。"),
                "easing": ("收益率下行 + 股票上涨：**金融条件转松**，修复期的最佳组合。",
                           "利率压力真正解除且风险偏好回归。从「风险偏好崩塌」迁移到本象限＝**调整结束的高置信确认**。"),
                "riskoff": ("收益率下行 + 股票下跌：**资金在逃进美债避险**——这不是「利率压力解除」，而是**风险偏好崩塌**。",
                            "最容易误读的象限。收益率下降在这里是「果」不是「因」：股跌得越急，债被买得越凶。**此象限中别把债券走强当利好**，盯信用利差和广度是否止跌。"),
                "neutral": ("收益率与股票都在平坦区：无明确主导逻辑。",
                            "两个市场都没有方向性表态，回到信用、广度、波动率等结构指标找线索。"),
            }
            qw, qm = QTEXT[qk]
            quadrant = {
                "key": qk, "label": qlabel, "tone": qtone,
                "yieldChg10d": dY, "spxChg10d": dS,
                "us10y": round(yv[-1], 2), "ydate": data["us10y"][-1][0],
                "spxDate": spx_rows[-1][0],
                "what": "横轴＝股票方向（SPX 10 日涨跌），纵轴＝利率方向（10Y 美债收益率 10 日变动）。单一指标会骗人——收益率下降可以是好事也可以是坏事——但「利率×股票」的组合不会：四个象限各自对应一种明确的市场主导逻辑。",
                "meaning": qw,
                "guide": qm + " 本框架来自 2026-10-19 三轮机构交叉验证（避险流量 vs 配置跷跷板之争的统一解法），验证笔记见 Obsidian 专题研究。",
            }

    # ---------- 警报挂晨报事件注释（P0-3：灯为什么亮，旁边直接挂证据） ----------
    BRIEF_JSON = BASE.parent / "data" / "brief.json"  # 仓库根目录 data/（parse_brief.py 的产物）
    ALERT_KW = {
        "hy": ["CDS", "利差", "信用", "发债", "违约", "债券", "CoreWeave", "甲骨文", "Oracle",
               "credit", "Credit", "bond", "Bond", "debt", "Debt",
               "chip", "Chip", "芯片", "半导体", "算力", "利率", "rate", "Rate",
               "yield", "Yield", "Treasur", "美债", "收益率"],
    }
    if BRIEF_JSON.exists():
        try:
            bj = json.loads(BRIEF_JSON.read_text(encoding="utf-8"))
            arts = []
            for b in (bj.get("briefs") or {}).values():
                arts.extend(b.get("articles") or [])
            for al in alerts:
                kws = ALERT_KW.get(al["key"].split("_")[0])
                if not kws:
                    continue
                ctx = []
                for a in arts:
                    text = " ".join(str(a.get(f) or "") for f in ("title", "core", "oneLine", "impact"))
                    if any(k in text for k in kws):
                        ctx.append({"title": a.get("title"), "source": a.get("source"),
                                    "url": a.get("url"), "tag": a.get("tag")})
                if ctx:
                    al["context"] = ctx[:4]
        except Exception:
            pass

    # ---------- 美联储预期轨 + 赛跑叙事（curated 维护，浏览器周抓） ----------
    fedwatch = curated.get("fedWatch")
    race_frame = {
        "rateSide": (f'{quadrant["label"]}（10Y {quadrant["us10y"]}%，10日 {quadrant["yieldChg10d"]:+.0f}bp）'
                     if quadrant else None),
        "fed": fedwatch,
        "aiSide": curated.get("aiSideNote"),
    }

    # ---------- 信号日志 ----------
    log_entries = update_signal_log(
        [{"key": a["key"], "level": a["level"], "text": a["text"]} for a in alerts if a["actionable"]],
        spx_rows)
    stats = signal_stats(log_entries)

    artifact = {
        "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": {"level": level, "headline": headline, "alerts": alerts,
                   "alertCount": len(alerts)},
        "composite": {
            "score": spectrum, "zone": zone_of(spectrum),
            "confirmation": confirmation,
            "confidence": round(100 * sum(c["confidence"] for c in components) / (len(components) or 1), 0),
            "groupScores": group_scores,
            "groupExplains": group_explains,
            "guidance": headline, "verdict": verdict,
            "externalFearGreed": curated.get("externalFearGreed"),
        },
        "components": components,
        "indices": indices,
        "portfolio": portfolio,
        "macro": macro,
        "events": upcoming_events(30),
        "signalStats": stats,
        "valuation": valuation,
        "valuationUpdatedAt": (curated.get("updatedAt", "") +
                               (" · PE/CAPE 自动更新" if pe_auto else "")),
        "curatedStaleDays": curated_stale,
        "flows": curated.get("flows", []),
        "quadrant": quadrant,
        "fedWatch": fedwatch,
        "raceFrame": race_frame,
        "sectors": sectors,
        "cnMarket": cn_market,
        "moneyStress": money_stress,
        "creditIG": credit_ig,
        "creditChain": credit_chain,
        "gold": gold,
        "rates": {"us10y": {"value": data["us10y"][-1][1], "date": data["us10y"][-1][0]} if data.get("us10y") else None},
        "errors": errors,
    }
    return artifact

# ---------------------------------------------------------------- 每日快照归档（供周报复盘）

SNAP_LOG = LOG_DIR / "daily-snapshots.jsonl"

def archive_snapshot(artifact):
    """每天一行紧凑快照：光谱/状态/分项分/组合警报/SPX，供周报复盘回溯。"""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        c = artifact["composite"]
        spx = next((i for i in artifact.get("indices", []) if i.get("symbol") == "SPX"), {})
        snap = {
            "date": artifact["asOf"][:10],
            "spectrum": c.get("score"), "zone": c.get("zone"),
            "confirmation": c.get("confirmation"),
            "status": artifact["status"]["level"],
            "alerts": [a["key"] for a in artifact["status"].get("alerts", [])],
            "components": {cp["key"]: cp.get("score") for cp in artifact.get("components", [])},
            "spx": spx.get("price"),
            "realRate": (artifact.get("portfolio", {}).get("realRate") or {}).get("value"),
            "netLiqZ": (artifact.get("portfolio", {}).get("netLiquidity") or {}).get("z4w"),
            "degraded": len(artifact.get("errors", [])),
        }
        # 同日重跑则覆盖该行（保持每日一条）
        lines = []
        if SNAP_LOG.exists():
            lines = [l for l in SNAP_LOG.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith('{"date": "' + snap["date"])]
        lines.append(json.dumps(snap, ensure_ascii=False))
        SNAP_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # 归档失败不影响主产物

def _export_sentiment(a):
    try:
        d = str(Path(__file__).resolve().parent)
        if d not in sys.path:
            sys.path.insert(0, d)
        from export_web import export_web
        export_web(a, "sentiment")
    except Exception:
        pass

def run(ctx):
    a = build_artifact()
    archive_snapshot(a)
    _export_sentiment(a)
    return {"artifact": a}

def main():
    a = build_artifact()
    archive_snapshot(a)
    _export_sentiment(a)
    print(json.dumps({"artifact": a}, ensure_ascii=False))

if __name__ == "__main__":
    main()
