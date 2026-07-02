#!/usr/bin/env python3
"""
tokubai_calendar.html の SALES セクションを自動更新する。

処理フロー:
1. ok_scraper.py でオーケーのセール情報を取得
2. nissin_scraper.py で日進のセール情報を取得
3. 取得したエントリを SALES = [...] 形式に変換
4. tokubai_calendar.html の SALES 定数を置換する

定期実行: 2日おきを推奨
"""

import sys
import os
import re
import json
from pathlib import Path
from datetime import datetime

# スクレイパーを import
sys.path.insert(0, str(Path(__file__).parent))
import ok_scraper
import nissin_scraper
import anthropic

REPO_PATH   = Path(__file__).parent.parent
HTML_PATH   = REPO_PATH / 'index.html'

# ── カテゴリ順 ─────────────────────────────────────────

CATEGORY_ORDER = {
    "肉類":     0,
    "野菜":     1,
    "調味料":   2,
    "冷凍食品": 3,
    "乳製品・卵": 4,
    "飲料":     5,
    "菓子":     6,
    "その他":   7,
}

def _cat_rank(e: dict) -> int:
    return CATEGORY_ORDER.get(e.get("category", "その他"), 7)

# ── 商品名除外パターン ─────────────────────────────────
# ここに追加すればオーケー・日進の両方に適用される

SALES_EXCLUDE_PATTERNS = [
    re.compile(r'梅'),    # 南高梅・梅干し類
    re.compile(r'柏餅'),  # 柏餅類
    re.compile(r'黒豆'),  # 黒豆類
    re.compile(r'レーズン'), # レーズン（果物入り）
]

def _is_excluded(entry: dict) -> bool:
    return any(p.search(entry["name"]) for p in SALES_EXCLUDE_PATTERNS)

# ── カテゴリ補正パターン ───────────────────────────────
# AI が誤分類しやすい肉加工品を「肉類」に強制上書き

MEAT_PATTERNS = [
    re.compile(r'ウインナー|ウィンナー'),
    re.compile(r'ソーセージ'),
    re.compile(r'ベーコン'),
    re.compile(r'ハム'),
    re.compile(r'焼き鳥|焼鳥'),
    re.compile(r'サラミ'),
    re.compile(r'スパム'),
]

def _fix_category(entry: dict) -> dict:
    if entry.get("category") != "肉類":
        if any(p.search(entry["name"]) for p in MEAT_PATTERNS):
            entry = {**entry, "category": "肉類"}
    return entry

# ── SALES ブロック置換 ─────────────────────────────────

SALES_PATTERN = re.compile(
    r'(const SALES\s*=\s*\[).*?(\];)',
    re.DOTALL
)

def entries_to_js(entries: list[dict]) -> str:
    """エントリリストをカテゴリ順 JavaScript 配列リテラルに変換"""
    lines = []
    # カテゴリ別にグループ化
    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        cat = e.get("category", "その他")
        by_cat.setdefault(cat, []).append(e)

    for cat in sorted(by_cat.keys(), key=lambda c: CATEGORY_ORDER.get(c, 7)):
        lines.append(f'    // ── {cat} ──')
        for item in by_cat[cat]:
            name_js = (item['name'] or '').replace('"', '\\"')
            unit_js = (item['unit'] or '個').replace('"', '\\"')
            cat_js  = (cat or 'その他').replace('"', '\\"')
            lines.append(
                f'    {{ store:"{item["store"]}", from:"{item["from"]}", to:"{item["to"]}", '
                f'name:"{name_js}", price:{item["price"]}, unit:"{unit_js}", '
                f'priority:{item["priority"]}, prominence:{item["prominence"]}, category:"{cat_js}" }},'
            )

    return "\n".join(lines)


def patch_sales(html: str, entries: list[dict]) -> str:
    """html 内の SALES = [...] を新しいエントリで置換"""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    js_body = entries_to_js(entries)
    replacement = (
        f'const SALES = [\n'
        f'    // 自動更新: {now_str}\n'
        f'{js_body}\n'
        f'  ];'
    )

    new_html, n = SALES_PATTERN.subn(replacement, html)
    if n == 0:
        print("警告: SALES ブロックが見つかりませんでした", file=sys.stderr)
    else:
        print(f"SALES ブロックを {len(entries)} エントリで更新しました", file=sys.stderr)
    return new_html


# ── メイン ────────────────────────────────────────────

def _load_api_key() -> str:
    """環境変数 → .env ファイルの順で ANTHROPIC_API_KEY を取得"""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    os.environ["ANTHROPIC_API_KEY"] = key
                    return key
    return ""


def main():
    api_key = _load_api_key()
    if not api_key:
        print("エラー: ANTHROPIC_API_KEY が環境変数にも .env にも見つかりません", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    all_entries: list[dict] = []

    print("=" * 55, file=sys.stderr)
    print("  特売カレンダー SALES 自動更新", file=sys.stderr)
    print("=" * 55, file=sys.stderr)

    # ── オーケー ──
    print("\n[オーケー] スクレイプ開始", file=sys.stderr)
    ok_failed = False
    try:
        ok_entries = ok_scraper.run(client=client)
        all_entries.extend(ok_entries)
        print(f"  → {len(ok_entries)} エントリ取得", file=sys.stderr)
    except Exception as e:
        ok_failed = True
        print(f"  [エラー] オーケースクレイプ失敗: {e}", file=sys.stderr)

    # ── 日進 ──
    print("\n[日進] スクレイプ開始", file=sys.stderr)
    nissin_failed = False
    try:
        nissin_entries = _run_nissin(client)
        all_entries.extend(nissin_entries)
        print(f"  → {len(nissin_entries)} エントリ取得", file=sys.stderr)
    except Exception as e:
        nissin_failed = True
        print(f"  [エラー] 日進スクレイプ失敗: {e}", file=sys.stderr)

    print(f"\n合計 {len(all_entries)} エントリ", file=sys.stderr)

    if ok_failed and nissin_failed:
        print("全スクレイパーが失敗したため HTML を更新しません", file=sys.stderr)
        sys.exit(1)

    if not all_entries:
        print("エントリが0件のため HTML を更新しません", file=sys.stderr)
        sys.exit(0)

    # 除外パターンでフィルタリング
    before = len(all_entries)
    all_entries = [e for e in all_entries if not _is_excluded(e)]
    excluded = before - len(all_entries)
    if excluded:
        print(f"除外パターンで {excluded} エントリを除去しました", file=sys.stderr)

    # カテゴリ補正（肉加工品を肉類に統一）
    all_entries = [_fix_category(e) for e in all_entries]

    # カテゴリ順にソート
    all_entries.sort(key=_cat_rank)

    # ── HTML を更新（バックアップ→書き込み→失敗時復元） ──
    import shutil
    html = HTML_PATH.read_text(encoding="utf-8")
    backup = HTML_PATH.with_suffix('.html.bak')
    backup.write_text(html, encoding="utf-8")
    try:
        new_html = patch_sales(html, all_entries)
        HTML_PATH.write_text(new_html, encoding="utf-8")
        backup.unlink(missing_ok=True)
        print(f"\n更新完了: {HTML_PATH}", file=sys.stderr)
    except Exception as e:
        print(f"[エラー] HTML書き込み失敗: {e}", file=sys.stderr)
        shutil.copy2(str(backup), str(HTML_PATH))
        backup.unlink(missing_ok=True)
        print("  → バックアップから復元しました", file=sys.stderr)
        sys.exit(1)

    # JSON にも保存（デバッグ用）
    out = Path(__file__).parent / "sales_latest.json"
    out.write_text(json.dumps(all_entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON保存: {out}", file=sys.stderr)

    # GitHub Pages へ自動 push
    _push_to_github()


def _push_to_github():
    """index.html の変更を GitHub Pages へ push"""
    import subprocess
    log_file = Path(__file__).parent / "git_push.log"

    def _log(msg: str):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] {msg}\n"
        print(msg, file=sys.stderr)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)

    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

        r_add = subprocess.run(
            ["git", "-C", str(REPO_PATH), "add", "index.html"],
            capture_output=True, text=True
        )
        _log(f"git add rc={r_add.returncode} out={r_add.stdout.strip()!r} err={r_add.stderr.strip()!r}")

        r_commit = subprocess.run(
            ["git", "-C", str(REPO_PATH), "commit", "-m", f"自動更新: {now_str}"],
            capture_output=True, text=True
        )
        combined = r_commit.stdout + r_commit.stderr
        _log(f"git commit rc={r_commit.returncode} out={r_commit.stdout.strip()!r} err={r_commit.stderr.strip()!r}")

        if "nothing to commit" in combined or r_commit.returncode != 0:
            _log("GitHub: 変更なし（スキップ）またはコミット失敗")
            return

        r_push = subprocess.run(
            ["git", "-C", str(REPO_PATH), "push"],
            capture_output=True, text=True
        )
        _log(f"git push rc={r_push.returncode} out={r_push.stdout.strip()!r} err={r_push.stderr.strip()!r}")
        if r_push.returncode == 0:
            _log("GitHub Pages へ push 完了")
        else:
            _log(f"[warn] git push 失敗")
    except Exception as e:
        _log(f"[warn] GitHub push エラー: {e}")


def _run_nissin(client: anthropic.Anthropic) -> list[dict]:
    """nissin_scraper の処理を client を再利用して実行"""
    articles = nissin_scraper.fetch_sale_article_urls()
    if not articles:
        return []

    all_entries = []
    THIS_YEAR = datetime.now().year

    for article in articles:
        info = nissin_scraper.fetch_article_info(article["url"])
        image_results = []
        for img_url in info["images"]:
            result = nissin_scraper.analyze_image(img_url, client)
            image_results.append(result)

        entries = nissin_scraper.build_entries(info["period"], image_results)
        for e in entries:
            if not e["from"].startswith(str(THIS_YEAR)):
                continue
            all_entries.append(e)

    return all_entries


if __name__ == "__main__":
    main()
