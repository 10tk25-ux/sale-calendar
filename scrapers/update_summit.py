#!/usr/bin/env python3
"""
サミット 品種別割引カレンダー 自動月次更新スクリプト

処理フロー:
1. shufoo.net XML API で shopId=825407 のチラシ一覧を取得
2. 対象月の全月チラシを絞り込む（publishEndTime で判定）
3. 各チラシのサムネイルを Vision API に送り「品種別割引カレンダー」を特定
4. 特定したチラシの chirashi.pdf をダウンロード
5. PyMuPDF で PDF を 4x 解像度 JPEG に変換
6. summit_calendar_parser.py の処理を実行（品目抽出）
7. tokubai_calendar.html の SUMMIT_MONTHLY を置換

定期実行: 毎月1日・25日に実行推奨
"""

import sys
import os
import re
import json
import base64
from pathlib import Path
from datetime import date, timedelta, datetime
from xml.etree import ElementTree as ET

import argparse
import shutil
import time

import httpx
import anthropic

# summit_calendar_parser を import
sys.path.insert(0, str(Path(__file__).parent))
import summit_calendar_parser as parser

# ── 設定 ─────────────────────────────────────────────
SHOP_ID        = 825407
XML_API_URL    = f"https://asp.shufoo.net/api/shopDetailNewXML/{SHOP_ID}/?crosstype=portal&useUtf=true&src=jsview"
IMAGE_CACHE    = "https://ipqcache2.shufoo.net"
REPO_PATH      = Path('G:/マイドライブ/Claude/sale-calendar')
HTML_PATH      = REPO_PATH / 'index.html'
SCRAPERS_DIR   = Path(__file__).parent
IMAGE_SAVE_PATH = REPO_PATH / 'summit_monthly_calendar.jpg'
PDF_SCALE      = 4   # PDF→JPEG の拡大倍率（4x = 2480x3508px）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

# ノイズ除去ルール
EXCLUDE_PATTERNS = [
    re.compile(r'^\d+$'),                    # 数字のみ
    re.compile(r'^[月火水木金土日]$'),         # 曜日1文字
    re.compile(r'^[ぁ-んァ-ン]{1}$'),           # ひらがな・カタカナ1文字のみ
    re.compile(r'^.{1,2}・.{1,2}$'),          # X・Y 形式で両側1〜2文字（ズ・グ 等）
    re.compile(r'^ムチ$'),                     # OCRゴミ（ムチ）
    re.compile(r'キャンペーン'),
    re.compile(r'画像には'),
    re.compile(r'ます。$'),
    re.compile(r'^空白$'),
    re.compile(r'倍$'),
    re.compile(r'ジレ.*ヤギ'),
    re.compile(r'^ふりかけ・$'),
    re.compile(r'梅干'),
    re.compile(r'佃煮'),
    re.compile(r'[茨薮粒養佃]'),               # OCRゴミ文字（日海佃キ化入 等）
    re.compile(r'^増汁$'),                    # OCRゴミ
    re.compile(r'^VR\b'),                     # VR VILLG 等のロゴ読み込み
    re.compile(r'^[A-Z]{2,}\s'),              # 英大文字2文字以上+スペース始まり
]

SUMMIT_MONTHLY_PATTERN = re.compile(
    r'(// サミット 品種別割引カレンダー（月間）[^\n]*\n\s*const SUMMIT_MONTHLY\s*=\s*)\{.*?\};',
    re.DOTALL
)

# ── HTTP ──────────────────────────────────────────────

def get(url: str, _retries: int = 3) -> httpx.Response:
    for attempt in range(_retries):
        try:
            return httpx.get(url, headers=HEADERS, timeout=60, follow_redirects=True)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == _retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"    [retry {attempt+1}/{_retries}] {type(e).__name__}  ({wait}s後リトライ)", file=sys.stderr)
            time.sleep(wait)

# ── 1. XML API からチラシ一覧取得 ─────────────────────

def fetch_flyer_list_from_xml() -> list[dict]:
    """
    shopDetailNewXML API からチラシ情報を取得する。
    Returns:
      [{"content_id": str, "internal_id": str, "upload_date": "YYYY/MM/DD",
        "publish_start": datetime, "publish_end": datetime,
        "thumb_url": str, "pdf_url": str}, ...]
    """
    print(f"  XML API 取得: {XML_API_URL}", file=sys.stderr)
    resp = get(XML_API_URL)
    # Shift-JIS or UTF-8、文字化け対策
    resp.encoding = "utf-8"
    xml_text = resp.text

    root = ET.fromstring(xml_text.encode("utf-8"))
    flyers = []

    for ch in root.findall(".//chirashi"):
        internal_id = ch.findtext("id", "")
        content_id  = ch.findtext("contentId", "")
        contents_xml_url = ch.findtext("contentsXml", "")

        # upload_date を contentsXml URL から抽出
        # 例: https://asp.shufoo.net/c/2026/04/27/33685949051603/index/contents.xml
        dm = re.search(r'/c/(\d{4}/\d{2}/\d{2})/', contents_xml_url)
        upload_date = dm.group(1) if dm else ""  # "2026/04/27"

        # publishStartTime / publishEndTime
        def parse_dt(s):
            try:
                return datetime.strptime(s, "%Y/%m/%d %H:%M:%S")
            except Exception:
                return None

        pub_start = parse_dt(ch.findtext("publishStartTime", ""))
        pub_end   = parse_dt(ch.findtext("publishEndTime", ""))

        # サムネイルURL
        thumb_url = ch.findtext("thumb", "")

        # PDF URL: ipqcache2.shufoo.net/c/{date}/c/{contentId}/index/img/chirashi.pdf
        pdf_url = ""
        if upload_date and content_id:
            pdf_url = (f"{IMAGE_CACHE}/c/{upload_date}/c/{content_id}"
                       f"/index/img/chirashi.pdf")

        flyers.append({
            "content_id":    content_id,
            "internal_id":   internal_id,
            "upload_date":   upload_date,
            "publish_start": pub_start,
            "publish_end":   pub_end,
            "thumb_url":     thumb_url,
            "pdf_url":       pdf_url,
        })

    print(f"  {len(flyers)} 件のチラシを検出", file=sys.stderr)
    for f in flyers:
        pub = f["publish_end"].strftime("%Y-%m-%d") if f["publish_end"] else "不明"
        print(f"    {f['content_id']} (〜{pub})", file=sys.stderr)

    return flyers

# ── 2. 対象月の全月チラシを絞り込む ─────────────────────

def is_full_month_flyer(flyer: dict, target_year: int, target_month: int) -> bool:
    """publishEnd が target_year/target_month の末日ならフルマンスチラシ"""
    pe = flyer.get("publish_end")
    if not pe:
        return False

    # 末日判定: target_month の最終日以降
    if target_month == 12:
        last_day = date(target_year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(target_year, target_month + 1, 1) - timedelta(days=1)

    return pe.year == target_year and pe.month == target_month and pe.day == last_day.day

# ── 3. Vision API でサムネイルを識別 ──────────────────

IDENTIFY_PROMPT = """\
この画像はスーパーのチラシのサムネイルです。
「品種別割引カレンダー」または「品目別割引カレンダー」と呼ばれる、
カテゴリ別に割引品目が書かれた月間カレンダー形式のチラシですか？

このチラシの特徴:
- カレンダー（日付のマス目）が縦に並んでいる
- 各日または各週に、割引になる食品カテゴリ名（例:「肉類」「チーズ」など）が書かれている
- 色分けされた横長のブロックが多数ある

「はい」または「いいえ」のみで答えてください。
"""

def is_calendar_flyer(thumb_url: str, client: anthropic.Anthropic) -> bool:
    try:
        resp = get(thumb_url)
        if resp.status_code != 200 or len(resp.content) < 500:
            return False

        img_b64 = base64.standard_b64encode(resp.content).decode()
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            ct = "image/jpeg"

        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": ct, "data": img_b64}},
                    {"type": "text", "text": IDENTIFY_PROMPT},
                ],
            }],
        )
        answer = message.content[0].text.strip()
        return "はい" in answer or "yes" in answer.lower()

    except Exception as e:
        print(f"    [warn] サムネイル識別エラー: {e}", file=sys.stderr)
        return False

# ── 4. PDF をダウンロードして JPEG に変換 ─────────────

def download_and_convert_pdf(flyer: dict, dest_path: Path) -> bool:
    """
    chirashi.pdf をダウンロードし PyMuPDF で JPEG に変換して dest_path に保存。
    成功すれば True を返す。
    """
    try:
        import fitz
    except ImportError:
        print("    [error] PyMuPDF が未インストール。pip install pymupdf", file=sys.stderr)
        return False

    pdf_url = flyer["pdf_url"]
    print(f"    PDFダウンロード: {pdf_url}", file=sys.stderr)

    try:
        resp = get(pdf_url)
        if resp.status_code != 200 or len(resp.content) < 10000:
            print(f"    [error] PDFダウンロード失敗: HTTP {resp.status_code}, {len(resp.content)}B",
                  file=sys.stderr)
            return False

        # PDF を一時ファイルに保存
        tmp_pdf = Path("C:/Temp/summit_chirashi_tmp.pdf")
        tmp_pdf.parent.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(resp.content)
        print(f"    PDF保存: {tmp_pdf} ({len(resp.content)//1024}KB)", file=sys.stderr)

        # PyMuPDF で 1ページ目を画像化
        doc = fitz.open(str(tmp_pdf))
        page = doc[0]
        mat = fitz.Matrix(PDF_SCALE, PDF_SCALE)
        pix = page.get_pixmap(matrix=mat)
        print(f"    画像サイズ: {pix.width}x{pix.height}", file=sys.stderr)

        # 一時JPEGに保存→GoogleドライブへコピーはPowerShellで
        tmp_jpg = Path("C:/Temp/summit_chirashi_tmp.jpg")
        pix.save(str(tmp_jpg))

        # dest_path はGoogleドライブ（日本語パス）なので shutil.copy2 で
        shutil.copy2(str(tmp_jpg), str(dest_path))
        print(f"    JPEG保存完了: {dest_path} ({dest_path.stat().st_size//1024}KB)",
              file=sys.stderr)

        return True

    except Exception as e:
        print(f"    [error] PDF変換失敗: {e}", file=sys.stderr)
        return False

# ── 5. パーサー実行 ────────────────────────────────────

def run_parser(image_path: Path, start_day: date, client: anthropic.Anthropic) -> dict:
    import cv2, numpy as np

    original_image_path = parser.IMAGE_PATH
    original_start_day  = parser.START_DAY
    original_total_days = parser.TOTAL_DAYS

    if start_day.month == 12:
        next_month = date(start_day.year + 1, 1, 1)
    else:
        next_month = date(start_day.year, start_day.month + 1, 1)
    total_days = (next_month - start_day).days

    parser.IMAGE_PATH  = str(image_path)
    parser.START_DAY   = start_day
    parser.TOTAL_DAYS  = total_days

    try:
        img = parser.load_image(str(image_path))
        if img is None:
            print("    [error] 画像読み込み失敗", file=sys.stderr)
            return {}

        h, w = img.shape[:2]
        print(f"    画像サイズ: {w}x{h}", file=sys.stderr)

        boundary_lines = parser.find_day_boundary_lines(img, total_days=total_days)
        print(f"    境界線 {len(boundary_lines)} 本検出", file=sys.stderr)

        day_items: dict[int, list[str]] = {d: [] for d in range(1, total_days + 1)}

        for col in parser.COLUMN_DEFS:
            # 画像幅を超える列はスキップ
            if col['x1'] >= w:
                print(f"    {col['name']} スキップ (x={col['x1']} >= 幅{w})", file=sys.stderr)
                continue
            x2 = min(col['x2'], w)
            print(f"    {col['name']} (x={col['x1']}〜{x2}) 処理中...", file=sys.stderr)
            blocks = parser.detect_colored_blocks(img, col['x1'], x2, boundary_lines)
            print(f"      {len(blocks)} ブロック", file=sys.stderr)

            for day_start, day_end, y1, y2 in blocks:
                text = parser.extract_items(img, y1, y2, col['x1'], x2,
                                            client, col['vertical'])
                items = parser.apply_exclusions(text, parser.EXCLUDE_KEYWORDS)
                for day in range(day_start, min(day_end + 1, total_days + 1)):
                    day_items[day].extend(items)

        output = {}
        for day in range(1, total_days + 1):
            d   = start_day + timedelta(days=day - 1)
            key = d.strftime('%Y-%m-%d')
            unique_items = list(dict.fromkeys(day_items[day]))
            if unique_items:
                output[key] = unique_items

        return output

    finally:
        parser.IMAGE_PATH  = original_image_path
        parser.START_DAY   = original_start_day
        parser.TOTAL_DAYS  = original_total_days

# ── 6. クリーニング ────────────────────────────────────

def clean_data(raw: dict) -> dict:
    cleaned = {}
    for date_str, items in raw.items():
        result = []
        for item in items:
            if any(p.search(item) for p in EXCLUDE_PATTERNS):
                continue
            if item not in result:
                result.append(item)
        if result:
            cleaned[date_str] = result
    return cleaned

# ── 7. HTML パッチ ────────────────────────────────────

def patch_summit_monthly(html: str, monthly_data: dict) -> str:
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    json_str = json.dumps(monthly_data, ensure_ascii=False)
    replacement = (
        f'// サミット 品種別割引カレンダー（月間）自動更新: {now_str}\n'
        f'  const SUMMIT_MONTHLY = {json_str};'
    )

    new_html, n = SUMMIT_MONTHLY_PATTERN.subn(replacement, html)
    if n == 0:
        simple_pat = re.compile(r'const SUMMIT_MONTHLY\s*=\s*\{.*?\};', re.DOTALL)
        new_html, n = simple_pat.subn(f'const SUMMIT_MONTHLY = {json_str};', html)

    if n == 0:
        raise RuntimeError(
            "SUMMIT_MONTHLY ブロックが見つかりませんでした。"
            "HTMLの構造を確認してください。"
        )
    print(f"SUMMIT_MONTHLY を {len(monthly_data)} 日分で更新しました", file=sys.stderr)
    return new_html

# ── メイン ────────────────────────────────────────────

def main():
    arg_parser = argparse.ArgumentParser(description="サミット 品種別割引カレンダー 自動更新")
    arg_parser.add_argument("--month", metavar="YYYY-MM",
                            help="対象月を手動指定（省略時は実行日から自動判定）")
    args = arg_parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: 環境変数 ANTHROPIC_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("=" * 55, file=sys.stderr)
    print("  サミット 品種別割引カレンダー 自動更新", file=sys.stderr)
    print("=" * 55, file=sys.stderr)

    if args.month:
        try:
            target = datetime.strptime(args.month, "%Y-%m").date().replace(day=1)
        except ValueError:
            print("エラー: --month は YYYY-MM 形式で指定してください（例: 2026-06）", file=sys.stderr)
            sys.exit(1)
    else:
        today = date.today()
        if today.day >= 25:
            if today.month == 12:
                target = date(today.year + 1, 1, 1)
            else:
                target = date(today.year, today.month + 1, 1)
        else:
            target = date(today.year, today.month, 1)

    target_year  = target.year
    target_month = target.month
    print(f"\n対象月: {target_year}年{target_month}月", file=sys.stderr)

    # Step 1: XML APIからチラシ一覧取得
    print("\n[1] シュフーXML APIからチラシ一覧を取得中...", file=sys.stderr)
    flyers = fetch_flyer_list_from_xml()
    if not flyers:
        print("チラシが見つかりませんでした", file=sys.stderr)
        sys.exit(1)

    # Step 2: 対象月の全月チラシに絞り込む
    print(f"\n[2] {target_month}月の全月チラシを絞り込み中...", file=sys.stderr)
    candidates = [f for f in flyers if is_full_month_flyer(f, target_year, target_month)]

    if not candidates:
        print(f"  {target_month}月の全月チラシが見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(candidates)} 件が候補", file=sys.stderr)

    # Step 3: 全候補でカレンダー識別（break なし・全件チェック）
    print("\n[3] サムネイルで品種別割引カレンダーを識別中...", file=sys.stderr)
    calendar_flyers = []

    for f in candidates:
        print(f"  確認中: {f['content_id']}", file=sys.stderr)
        if is_calendar_flyer(f["thumb_url"], client):
            print(f"  ✓ カレンダーと判定: {f['content_id']}", file=sys.stderr)
            calendar_flyers.append(f)
        else:
            print(f"  ✗ 対象外", file=sys.stderr)

    if not calendar_flyers:
        print("\n  自動識別失敗。最初の候補を使用します。", file=sys.stderr)
        calendar_flyers = [candidates[0]]

    start_day = date(target_year, target_month, 1)

    # Step 4–5: PDF ダウンロード→JPEG変換→パーサー実行
    if IMAGE_SAVE_PATH.exists():
        backup_img = IMAGE_SAVE_PATH.with_name("summit_prev_calendar_backup.jpg")
        shutil.copy2(str(IMAGE_SAVE_PATH), str(backup_img))
        print(f"\n  前月分バックアップ: {backup_img}", file=sys.stderr)

    if len(calendar_flyers) == 1:
        print(f"\n[4] カレンダーPDFをダウンロード・変換中...", file=sys.stderr)
        calendar_flyer = calendar_flyers[0]
        if not download_and_convert_pdf(calendar_flyer, IMAGE_SAVE_PATH):
            print("画像のダウンロード/変換に失敗しました", file=sys.stderr)
            sys.exit(1)
        print(f"\n[5] カレンダー解析中 (Vision API)...", file=sys.stderr)
        raw_data = run_parser(IMAGE_SAVE_PATH, start_day, client)
    else:
        # 複数候補: 全て解析して日数最多のものを採用（誤識別対策）
        print(f"\n[4-5] 候補 {len(calendar_flyers)} 件を全て解析して最良を選択...", file=sys.stderr)
        tmp_path = Path("C:/Temp/summit_candidate_tmp.jpg")
        best_flyer = None
        best_data: dict = {}
        best_days = -1
        for f in calendar_flyers:
            print(f"  候補 {f['content_id']} を解析中...", file=sys.stderr)
            if not download_and_convert_pdf(f, tmp_path):
                print(f"    → ダウンロード失敗、スキップ", file=sys.stderr)
                continue
            data = run_parser(tmp_path, start_day, client)
            n = len(data)
            print(f"    → {n} 日分", file=sys.stderr)
            if n > best_days:
                best_days = n
                best_data = data
                best_flyer = f
        if not best_flyer:
            print("全候補の解析に失敗しました", file=sys.stderr)
            sys.exit(1)
        print(f"\n  採用: {best_flyer['content_id']} ({best_days} 日分)", file=sys.stderr)
        shutil.copy2(str(tmp_path), str(IMAGE_SAVE_PATH))
        calendar_flyer = best_flyer
        raw_data = best_data

    if not raw_data:
        print("解析結果が空でした", file=sys.stderr)
        sys.exit(1)

    print(f"  → {len(raw_data)} 日分のデータを取得", file=sys.stderr)

    # Step 6: クリーニング
    print("\n[6] クリーニング中...", file=sys.stderr)
    monthly_data = clean_data(raw_data)
    print(f"  → {len(monthly_data)} 日分（クリーニング後）", file=sys.stderr)

    json_out = SCRAPERS_DIR / f"summit_{target_year}{target_month:02d}_clean.json"
    json_out.write_text(json.dumps(monthly_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  JSON保存: {json_out}", file=sys.stderr)

    # Step 7: HTML更新（バックアップ→書き込み→失敗時復元）
    print(f"\n[7] tokubai_calendar.html を更新中...", file=sys.stderr)
    import shutil
    html = HTML_PATH.read_text(encoding="utf-8")
    backup = HTML_PATH.with_suffix('.html.bak')
    backup.write_text(html, encoding="utf-8")
    try:
        new_html = patch_summit_monthly(html, monthly_data)
        HTML_PATH.write_text(new_html, encoding="utf-8")
        backup.unlink(missing_ok=True)
        print(f"\n完了: {HTML_PATH}", file=sys.stderr)
    except Exception as e:
        print(f"[エラー] HTML更新失敗: {e}", file=sys.stderr)
        shutil.copy2(str(backup), str(HTML_PATH))
        backup.unlink(missing_ok=True)
        print("  → バックアップから復元しました", file=sys.stderr)
        sys.exit(1)

    # GitHub Pages へ自動 push
    _push_to_github()


def _push_to_github():
    """index.html の変更を GitHub Pages へ push"""
    import subprocess
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        result = subprocess.run(
            ["git", "-C", str(REPO_PATH), "add", "index.html"],
            capture_output=True, text=True
        )
        result = subprocess.run(
            ["git", "-C", str(REPO_PATH), "commit", "-m", f"自動更新: {now_str}"],
            capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            print("GitHub: 変更なし（スキップ）", file=sys.stderr)
            return
        result = subprocess.run(
            ["git", "-C", str(REPO_PATH), "push"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("GitHub Pages へ push 完了", file=sys.stderr)
        else:
            print(f"[warn] git push 失敗: {result.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] GitHub push エラー: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
