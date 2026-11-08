#!/usr/bin/env python3
"""A股资金流向实测：科创/创业板 周五(10/23) 与 周一(10/26)
数据源：东方财富 push2/push2his 免费接口（真实交易数据，非网页）
"""
import json, urllib.request, datetime

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Referer": "https://data.eastmoney.com/"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

# ---------- 1. 指数级主力资金流（近6日） ----------
# fflow daykline: f51日期 f52主力净额 f53小单 f54中单 f55大单 f56超大单 f57主力净占比 f58小单占比 f59中 f60大 f61超大
def fflow(secid, name):
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?lmt=6&klt=101&secid={secid}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f62,f63")
    try:
        d = get(url)
        kl = (d.get("data") or {}).get("klines") or []
        print(f"\n== {name} ({secid}) 资金流向（单位:亿元）==")
        print(f"{'日期':<12}{'主力净额':>10}{'超大单':>10}{'大单':>10}{'中单':>10}{'小单':>10}{'主力占比%':>10}")
        for k in kl:
            p = k.split(",")
            yi = lambda x: round(float(x)/1e8, 2)
            print(f"{p[0]:<12}{yi(p[1]):>10}{yi(p[5]):>10}{yi(p[4]):>10}{yi(p[3]):>10}{yi(p[2]):>10}{p[6]:>10}")
        return kl
    except Exception as e:
        print(f"{name} 资金流获取失败: {e}")
        return []

fflow("1.000688", "科创50")
fflow("0.399006", "创业板指")
fflow("1.000001", "上证指数")

# ---------- 2. 今日涨跌家数（广度） ----------
def breadth(secid, name):
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f60,f170,f104,f105,f106"
    try:
        d = (get(url).get("data") or {})
        print(f"{name}: 涨{d.get('f104')}家 / 跌{d.get('f105')}家 / 平{d.get('f106')}家, 指数{d.get('f43')/100:.2f}, 涨跌幅{d.get('f170')/100:+.2f}%")
    except Exception as e:
        print(f"{name} 广度获取失败: {e}")

print("\n== 今日（10/26 收盘）涨跌家数 ==")
for sec, nm in [("1.000001","上证"),("0.399001","深证"),("0.399006","创业板指"),("1.000688","科创50")]:
    breadth(sec, nm)

# ---------- 3. 今日板块主力资金榜（行业+概念，前15/后10） ----------
def board_flow(fs, tag):
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=400&po=1&np=1&fid=f62"
           f"&fs={fs}&fields=f12,f14,f62,f66,f72,f78,f84,f184,f3")
    try:
        d = get(url)
        rows = (d.get("data") or {}).get("diff") or []
        yi = lambda x: round((x or 0)/1e8, 2)
        print(f"\n== {tag} 主力净流入 TOP15（今日, 亿元）==")
        for r in rows[:15]:
            print(f"  {r['f14']:<12}{yi(r.get('f62')):>9}  (超大{yi(r.get('f66')):>7} 大{yi(r.get('f72')):>7} 中{yi(r.get('f78')):>7} 小{yi(r.get('f84')):>7}  涨跌{r.get('f3')}%)")
        print(f"-- {tag} 主力净流出 TOP10 --")
        for r in rows[-10:]:
            print(f"  {r['f14']:<12}{yi(r.get('f62')):>9}  (涨跌{r.get('f3')}%)")
        return rows
    except Exception as e:
        print(f"{tag} 获取失败: {e}")
        return []

board_flow("m:90+t:2", "行业板块")
board_flow("m:90+t:3", "概念板块")

# ---------- 4. 关键板块近5日资金流趋势（半导体/软件/通信设备/电池/证券） ----------
def board_hist(bk, name):
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?lmt=5&klt=101&secid=90.{bk}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57")
    try:
        d = get(url)
        kl = (d.get("data") or {}).get("klines") or []
        print(f"\n== {name} 近5日主力净额(亿) ==")
        for k in kl:
            p = k.split(",")
            print(f"  {p[0]}  主力 {round(float(p[1])/1e8,2):>8}  占比 {p[6]}%")
    except Exception as e:
        print(f"{name} 历史获取失败: {e}")

for bk, nm in [("BK1036","半导体"),("BK0737","软件开发"),("BK0448","通信设备"),("BK1033","电池"),("BK0473","证券"),("BK0816","消费电子")]:
    board_hist(bk, nm)

print("\n完成时间:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
