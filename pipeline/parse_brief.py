#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
晨报重点新闻解析 · 每日
从 Obsidian 仓库读取最新的 Morning-Brief（投资 + AI 两桶），
解析结构化条目（标题/来源/评分/核心事实/市场影响/值得关注/链接），
输出 {"artifact": {...}} JSON 到 stdout（Blueprint code Automation 约定）。
"""
import json, re, sys, datetime
from pathlib import Path

OBSIDIAN_JSON = Path.home() / "Library/Application Support/obsidian/obsidian.json"
BRIEF_DIRS = {
    "invest": "01-Projects/Invert-Bot/Morning-Brief",
    "ai": "01-Projects/AI-Narrative-Lab/Morning-Brief",
}
BRIEF_GLOB = {
    "invest": "*morning-brief-Invest news.md",
    "ai": "*morning-brief-AI news.md",
}
TOP_N = 8  # 每桶取评分最高的前 N 条

def find_vault():
    cfg = json.loads(OBSIDIAN_JSON.read_text(encoding="utf-8"))
    vaults = cfg.get("vaults", {})
    # 优先当前打开的库
    for v in vaults.values():
        if v.get("open"):
            return Path(v["path"])
    for v in vaults.values():
        return Path(v["path"])
    raise RuntimeError("no obsidian vault found")

HEAD_RE = re.compile(r"^###\s*【第(\d+)条】(.+?)\s*\|\s*(.+?)\s*\|\s*⭐(\d+)/20\s*$")
FIELD_RE = re.compile(r"^\[(核心事实|市场影响|值得关注)\]\s*(.*)$")
TAG_RE = re.compile(r"^`(.+?)`")
LINK_RE = re.compile(r"^🔗\s*(\S+)")
SCORE_DETAIL_RE = re.compile(r"^📊\s*评分明细：(.+)$")

SENTENCE_END = tuple("。！？…””」』）)》")

def looks_truncated(text):
    """生成端按字数硬切的摘要：结尾没有句读，视为截断。"""
    if not text:
        return False
    return not text.rstrip().endswith(SENTENCE_END)

def parse_brief(md_text):
    articles = []
    cur = None
    for line in md_text.splitlines():
        line = line.rstrip()
        m = HEAD_RE.match(line)
        if m:
            if cur:
                articles.append(cur)
            cur = {"rank": int(m.group(1)), "title": m.group(2).strip(),
                   "source": m.group(3).strip(), "score": int(m.group(4)),
                   "tag": "", "oneLine": "", "core": "", "impact": "", "watch": "",
                   "url": "", "scoreDetail": "", "truncated": False}
            continue
        if cur is None:
            continue
        m = FIELD_RE.match(line)
        if m:
            key = {"核心事实": "core", "市场影响": "impact", "值得关注": "watch"}[m.group(1)]
            cur[key] = m.group(2).strip()
            continue
        m = TAG_RE.match(line)
        if m and not cur["tag"]:
            cur["tag"] = m.group(1).strip()
            continue
        m = LINK_RE.match(line)
        if m:
            cur["url"] = m.group(1).strip()
            continue
        m = SCORE_DETAIL_RE.match(line)
        if m:
            cur["scoreDetail"] = m.group(1).strip()
            continue
        # 标签行之后的第一行普通文字 = one_line 导语
        if cur["tag"] and not cur["oneLine"] and line.strip() and not line.startswith(("#", "-", "`")):
            cur["oneLine"] = line.strip()
    if cur:
        articles.append(cur)
    for a in articles:
        a["truncated"] = any(looks_truncated(a[k]) for k in ("core", "impact", "watch"))
    return articles

def load_bucket(vault, key):
    d = vault / BRIEF_DIRS[key]
    if not d.exists():
        return {"error": f"目录不存在: {d}", "articles": []}
    files = sorted(d.glob(BRIEF_GLOB[key]), reverse=True)
    if not files:
        return {"error": "未找到晨报文件", "articles": []}
    latest = files[0]
    arts = parse_brief(latest.read_text(encoding="utf-8"))
    arts_sorted = sorted(arts, key=lambda a: -a["score"])
    # 新鲜度：文件名日期 vs 今天
    stale_days = None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", latest.name)
    if m:
        try:
            fdate = datetime.date.fromisoformat(m.group(1))
            stale_days = (datetime.date.today() - fdate).days
        except ValueError:
            pass
    return {
        "file": latest.name,
        "date": m.group(1) if m else "",
        "staleDays": stale_days,
        "total": len(arts),
        "articles": arts_sorted[:TOP_N],
    }

def build_artifact():
    out = {"asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "briefs": {}}
    try:
        vault = find_vault()
        out["vault"] = vault.name
        for key in BRIEF_DIRS:
            out["briefs"][key] = load_bucket(vault, key)
    except Exception as e:
        out["error"] = str(e)
    return out

def _export(artifact):
    try:
        d = str(Path(__file__).resolve().parent)
        if d not in sys.path:
            sys.path.insert(0, d)
        from export_web import export_web
        export_web(artifact, "brief")
    except Exception:
        pass

def main():
    # 本地 CLI 测试用：打印 Blueprint 包装 JSON
    a = build_artifact()
    _export(a)
    print(json.dumps({"artifact": a}, ensure_ascii=False))

def run(ctx):
    # Blueprint 托管 runner 入口：返回值即 AutomationOutput
    a = build_artifact()
    _export(a)
    return {"artifact": a}

if __name__ == "__main__":
    main()
