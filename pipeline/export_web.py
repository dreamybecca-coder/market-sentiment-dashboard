#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把最新数据快照导出到 web/ 目录并推送到 GitHub Pages（best-effort，失败不影响主流程）。"""
import json, subprocess
from pathlib import Path

# 仓库根目录（pipeline/ 的上一级）；GitHub Pages 直接发布根目录
WEB_DIR = Path(__file__).resolve().parent.parent

def export_web(artifact, kind):
    """kind: 'sentiment' | 'brief'。web/ 是独立 git 仓库（market-sentiment-dashboard）。"""
    try:
        if not (WEB_DIR / ".git").exists():
            return
        data_dir = WEB_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        target = data_dir / f"{kind}.json"
        payload = json.dumps(artifact, ensure_ascii=False, indent=1)
        if target.exists() and target.read_text(encoding="utf-8") == payload:
            return  # 无变化，不提交
        target.write_text(payload, encoding="utf-8")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(WEB_DIR), *args],
                capture_output=True, text=True, timeout=60)

        git("add", f"data/{kind}.json")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return  # 无实际变更
        git("-c", "user.name=dreamybecca-coder",
            "-c", "user.email=dreamybecca-coder@users.noreply.github.com",
            "commit", "-qm", f"data: update {kind} snapshot")
        push = git("push", "-q", "origin", "main")
        if push.returncode != 0:
            print(f"[export_web] push failed: {push.stderr[:200]}")
    except Exception as e:
        print(f"[export_web] {kind} export failed: {e}")
