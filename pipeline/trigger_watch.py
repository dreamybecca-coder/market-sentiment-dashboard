#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
触发线监测器（condition predicate + 可独立运行）。
只读快数据源：web/data/sentiment.json（每日3次管道产物）+ 东方财富上证盘中值 + CNBC SPX 盘中值。
新击穿才返回 True / 输出 fires；已触发过的线在条件解除前不重复提醒（状态存 logs/trigger-state.json）。
"""
import json, subprocess, datetime, sys, os
from pathlib import Path

# 仓库根目录（pipeline/ 的上一级）；也可用环境变量 MARKET_SENTIMENT_DIR 覆盖
BASE = Path(os.environ.get("MARKET_SENTIMENT_DIR", Path(__file__).resolve().parent.parent))
SENTIMENT = BASE / "data" / "sentiment.json"
STATE = BASE / "logs" / "trigger-state.json"
FIRES = BASE / "logs" / "trigger-fire.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

def curl(url, timeout=8):
    return subprocess.run(["curl", "-s", "-m", str(timeout), "-H", f"User-Agent: {UA}", url],
                          capture_output=True, text=True).stdout

def em_spot(secid):
    d = json.loads(curl(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                        "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg=20260901&end=20500101"))
    ks = ((d.get("data") or {}).get("klines")) or []
    return [(k.split(",")[0], float(k.split(",")[1])) for k in ks]

def cnbc_spot(symbol):
    d = json.loads(curl(f"https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
                        f"?symbols={symbol}&output=json"))
    q = d["FormattedQuoteResult"]["FormattedQuote"][0]
    return float(str(q.get("last", "0")).replace(",", ""))

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}

def check_triggers():
    """返回 (fires, snapshot)。fires: [{id, text, action}] 仅含「新击穿」。"""
    d = json.loads(SENTIMENT.read_text(encoding="utf-8"))
    state = load_state()
    fires = []
    snap = {"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}

    hy = next((c for c in d.get("components", []) if c.get("key") == "credit"), {})
    hy_val = float(str(hy.get("value", "0")).replace("%", "") or 0)
    snap["hy"] = hy_val

    # T1: HY ≥ 3.75%
    t1_on = hy_val >= 3.75
    if t1_on and not state.get("T1"):
        fires.append({"id": "T1", "text": f"HY 利差升至 {hy_val:.2f}%（≥3.75% 触发线）",
                      "action": "执行第二档减仓约10%（顺序：量子计算→Rocket Lab/早期创新药→不动苹果/Google），美股仓位降至约75%"})
    state["T1"] = t1_on

    # T2: SPX 跌破 125 日线（盘中用 CNBC 现价 vs 由 sentiment 反推的均线）
    mom = next((c for c in d.get("components", []) if c.get("key") == "momentum"), {})
    mom_pct = float(str(mom.get("value", "0")).replace("%", "").replace("+", "") or 0)
    spx_last = (d.get("indices") or [{}])[0].get("price")
    sma125 = spx_last / (1 + mom_pct / 100) if spx_last and mom_pct else None
    snap["sma125"] = round(sma125, 0) if sma125 else None
    spx_now = None
    try:
        spx_now = cnbc_spot(".SPX")
    except Exception:
        pass
    snap["spx"] = spx_now or spx_last
    if sma125:
        ref = spx_now or spx_last
        t2_on = ref < sma125
        if t2_on and not state.get("T2"):
            fires.append({"id": "T2", "text": f"SPX {ref:.0f} 跌破 125 日均线（{sma125:.0f}）",
                          "action": "执行第二档减仓约10%（与 T1 不叠加；若 T1 已执行过则本条只确认不重复减仓）"})
        state["T2"] = t2_on

    # T3: 上证连续 2 个交易日收盘 < 3700（直接从 K 线序列算尾部连续天数，不依赖轮询历史）
    try:
        sh = em_spot("1.000001")
        streak = 0
        for dt, c in reversed(sh):
            if c < 3700:
                streak += 1
            else:
                break
        last_date, sh_now = sh[-1]
        snap["sh"] = sh_now
        snap["shDate"] = last_date
        snap["shStreak"] = streak
        state["T3_days"] = streak
        if streak == 0:
            state["T3"] = False
        if streak >= 2 and not state.get("T3"):
            fires.append({"id": "T3", "text": f"上证已连续 {streak} 日收于 3700 下方（{last_date} 收 {sh_now:.0f}）",
                          "action": "政策底未守住：A股减一档，科创/创业板高 beta 先减"})
            state["T3"] = True
    except Exception:
        pass

    # T4: 四象限=宽松顺风 且 恐贪<20 → 加仓窗口
    q = (d.get("quadrant") or {}).get("key")
    fg = ((d.get("composite") or {}).get("externalFearGreed") or {}).get("score")
    snap["quadrant"] = q
    snap["fg"] = fg
    t4_on = (q == "easing") and isinstance(fg, (int, float)) and fg < 20
    if t4_on and not state.get("T4"):
        fires.append({"id": "T4", "text": f"加仓窗口出现：四象限已转「宽松顺风」，恐贪仍在 {fg}（<20）",
                      "action": "把 T1/T2 减出的现金分 2–3 批打回（优先此前减掉的反向顺序：先苹果/Google 级，再高 beta）"})
    state["T4"] = t4_on

    # 红灯解除：警报列表里 hy_red 消失且不再是红灯
    level = (d.get("status") or {}).get("level")
    has_hy_red = any(a.get("key") == "hy_red" for a in (d.get("status") or {}).get("alerts", []))
    relief = (level != "red") and not has_hy_red
    if relief and not state.get("RELIEF"):
        fires.append({"id": "RELIEF", "text": "信用红灯解除：警报已消失",
                      "action": "停止减仓动作，未执行的档作废，恢复正常节奏"})
    state["RELIEF"] = relief

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return fires, snap

def main():
    fires, snap = check_triggers()
    if fires:
        FIRES.write_text(json.dumps({"at": snap["at"], "snapshot": snap, "fires": fires},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"fire": bool(fires), "fires": fires, "snapshot": snap}, ensure_ascii=False))
    return bool(fires)

# 监测窗口（Asia/Shanghai）：对齐市场节点，每天三次
# 窗口宽 50 分钟 > 轮询间隔 30 分钟，保证任何轮询相位下至少有一次落入窗口
# 06:15–07:05  美股收盘+FOMC/财报（盘后）结果落地，6:12 管道已刷新
# 15:40–16:30  A股收盘后（T3 需要收盘价判定）
# 21:55–22:45  美股开盘半小时+宏观数据（CPI/非农 20:30 北京）首轮反应
WATCH_WINDOWS = [(6, 15, 7, 5), (15, 40, 16, 30), (21, 55, 22, 45)]

def _in_window(now=None):
    now = now or datetime.datetime.now()
    mins = now.hour * 60 + now.minute
    return any(h1 * 60 + m1 <= mins <= h2 * 60 + m2 for h1, m1, h2, m2 in WATCH_WINDOWS)

def should_fire(ctx):
    try:
        if not _in_window():
            return False
        fires, _ = check_triggers()
        if fires:
            FIRES.write_text(json.dumps({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                         "fires": fires}, ensure_ascii=False, indent=1), encoding="utf-8")
        return bool(fires)
    except Exception:
        return False

def run(ctx):
    fires, snap = check_triggers()
    return {"artifact": {"fire": bool(fires), "fires": fires, "snapshot": snap}}

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
