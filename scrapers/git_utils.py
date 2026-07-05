#!/usr/bin/env python3
"""
GitHub Pages への push を堅牢に行う共通ヘルパー。

update_tokubai.py / update_summit.py から使用。

設計方針:
- push 前に必ず fetch + pull --rebase する（リモートが先行していても失敗しない）
- rebase 競合時は -X theirs で「ローカルの新しいスクレイプ結果」を優先する
  （rebase 中の theirs = 自分のコミット側。index.html は毎回全置換される
    自動生成データなので、常に最新スクレイプが勝てばよい）
- リトライあり（push の直前に他プロセスが push した場合に備える）
- 失敗を握りつぶさない: 呼び出し元が False を受けたら exit 1 して
  GitHub Actions 上で run を失敗にする → 失敗通知メールが飛ぶ
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

MAX_RETRIES = 3


def _run(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True
    )


def push_to_github(repo_path: Path, files: list[str], log_file: Path) -> bool:
    """files を add → commit → rebase → push する。成功なら True。

    変更がない場合（commit するものがない）も True を返す。
    """

    def _log(msg: str):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(msg, file=sys.stderr)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass  # ログ書き込み失敗で処理を止めない

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

    r = _run(repo_path, "add", *files)
    if r.returncode != 0:
        _log(f"[error] git add 失敗: {r.stderr.strip()}")
        return False

    r = _run(repo_path, "commit", "-m", f"自動更新: {now_str}")
    combined = r.stdout + r.stderr
    if "nothing to commit" in combined or "nothing added to commit" in combined:
        _log("変更なし（commit スキップ）")
        return True
    if r.returncode != 0:
        _log(f"[error] git commit 失敗: {combined.strip()}")
        return False
    _log(f"git commit: {r.stdout.strip()}")

    for attempt in range(1, MAX_RETRIES + 1):
        # リモートの先行コミットを取り込む（競合は最新スクレイプ優先）
        r = _run(repo_path, "pull", "--rebase", "-X", "theirs", "origin", "main")
        if r.returncode != 0:
            _log(f"[warn] pull --rebase 失敗 (試行{attempt}): {r.stderr.strip()}")
            _run(repo_path, "rebase", "--abort")
            continue

        r = _run(repo_path, "push", "origin", "main")
        if r.returncode == 0:
            _log(f"GitHub Pages へ push 完了 (試行{attempt})")
            return True
        _log(f"[warn] git push 失敗 (試行{attempt}): {r.stderr.strip()}")

    _log(f"[error] {MAX_RETRIES} 回リトライしても push できませんでした")
    return False
