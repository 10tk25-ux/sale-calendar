#!/usr/bin/env python3
"""
サミット 品種別割引カレンダー 自動解析スクリプト

【ロジック】
1. 日付列（x=1380-1440）の白い横線（2px幅）を検出 → 正確な境界y座標を取得
2. 30本の白線 = 各日の上端、+1本（最終日の下端）で計31本の境界線
3. 各列でカラーブロックを検出 → ブロックのy範囲を境界線と照合して日付範囲を確定
4. 各ブロックをVision APIに送り品目テキストを抽出
5. 除外品目を適用してJSON出力
"""

import cv2
import numpy as np
from PIL import Image
import anthropic
import base64
import json
import sys
import os
import io
from datetime import date, timedelta

# ─── 設定 ───────────────────────────────────────────────────────────────────

IMAGE_PATH  = 'G:/マイドライブ/Claude/summit_monthly_calendar.jpg'
TOTAL_DAYS  = 30
START_DAY   = date(2026, 4, 1)

# 日付区切り線を検出するための列範囲（日付数字列）
DATE_COL_X1 = 1380
DATE_COL_X2 = 1440

# 除外品目キーワード
EXCLUDE_KEYWORDS = [
    '花', 'ペットフード', 'ペット用品',
    '生理用品', '吸水ケア', '紙おむつ',
    '鍋', 'フライパン', '包丁', 'まな板',
    '文房具',
    '梅干',
    '佃煮',
]

# 列定義（固定ピクセル座標 - セル中点スキャンで実測）
# 日付列（茶色の縦柱）: x=1310-1470
# ギャップ: x=1480, 1740, 2030, 2320, 2410
COLUMN_DEFS = [
    {'name': 'A列', 'x1': 1490, 'x2': 1730, 'vertical': False},
    {'name': 'B列', 'x1': 1750, 'x2': 2020, 'vertical': False},
    {'name': 'C列', 'x1': 2040, 'x2': 2310, 'vertical': True },
    {'name': 'D列', 'x1': 2330, 'x2': 2400, 'vertical': True },
]

# デバッグ: 検出結果を画像で保存するか
DEBUG_SAVE = True
DEBUG_DIR  = 'G:/マイドライブ/Claude/scrapers/debug_calendar'


# ─── 画像読み込み（日本語パス対応） ─────────────────────────────────────────

def load_image(path: str) -> np.ndarray:
    """PIL経由で読み込むことでWindows日本語パスに対応"""
    pil_img = Image.open(path)
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


# ─── ① 横線検出（白線走査方式） ──────────────────────────────────────────────

def find_day_boundary_lines(img: np.ndarray,
                            date_col_x1: int = DATE_COL_X1,
                            date_col_x2: int = DATE_COL_X2,
                            total_days: int = TOTAL_DAYS) -> list[int]:
    """
    日付数字列（date_col_x1〜date_col_x2）の白い横線を走査して
    各日の上端y座標を検出する。

    カレンダーの日付セルは濃い背景色で、セル間の区切り線は白（2px幅）。
    dark_pct < 0.1 の行を「白線」として検出する。

    Returns: boundary_lines (total_days+1本)
             [day1_top, day2_top, ..., day30_top, day30_bottom]
    """
    col  = img[:, date_col_x1:date_col_x2]
    gray = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY)
    h    = gray.shape[0]

    dark_pct = np.mean(gray < 100, axis=1)

    # カレンダー本体（y > 380）の白線を検出
    white_rows = np.where((dark_pct < 0.1) & (np.arange(h) > 380))[0]

    # 連続行をグループ化して1本の線にまとめる
    if len(white_rows) == 0:
        print('警告: 白線が検出できません。等間隔フォールバックを使用', file=sys.stderr)
        top  = 411
        step = 97
        return [top + i * step for i in range(total_days + 1)]

    groups = [[white_rows[0]]]
    for i in range(1, len(white_rows)):
        if white_rows[i] - white_rows[i-1] <= 3:
            groups[-1].append(white_rows[i])
        else:
            groups.append([white_rows[i]])

    white_lines = [int(np.mean(g)) for g in groups]

    if len(white_lines) < total_days:
        print(f'警告: 白線 {len(white_lines)} 本のみ検出（期待: {total_days}）', file=sys.stderr)

    # total_days本 = 各日の上端
    tops = white_lines[:total_days]

    # 最終日の下端を追加
    if len(tops) >= 2:
        last_step = tops[-1] - tops[-2]
    else:
        last_step = 97
    boundary_lines = tops + [tops[-1] + last_step]

    return boundary_lines


def y_to_day(y: int, boundary_lines: list[int]) -> int:
    """
    y座標を日付（1〜total_days）に変換する。
    boundary_lines[i] <= y < boundary_lines[i+1] のとき day = i+1
    """
    for i in range(len(boundary_lines) - 1):
        if boundary_lines[i] <= y < boundary_lines[i + 1]:
            return i + 1
    return len(boundary_lines) - 1  # 最終日


# ─── ② ブロック検出（境界線ベース） ─────────────────────────────────────────

def detect_colored_blocks(img: np.ndarray, x1: int, x2: int,
                           boundary_lines: list[int],
                           sep_th: int = 145,
                           blk_th: int = 150) -> list[tuple]:
    """
    境界線（boundary_lines）を使ってカラーブロックを検出する。

    各日セルの中点輝度 < blk_th → そのセルにブロックあり
    境界線位置の輝度 >= sep_th → その境界は「区切り」（別ブロック）

    Returns: list of (day_start, day_end, y1, y2)
    """
    col  = img[:, x1:x2]
    gray = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY)
    bl   = boundary_lines
    n    = len(bl) - 1  # 日数

    # 各日セルにブロックが存在するか（中点輝度で判定）
    cell_has_block = []
    for i in range(n):
        y_mid   = (bl[i] + bl[i+1]) // 2
        margin  = min(10, (bl[i+1] - bl[i]) // 4)
        s       = gray[max(0, y_mid - margin):y_mid + margin, :]
        cell_has_block.append(float(np.mean(s)) < blk_th)

    # 各内部境界線が「区切り線（白）」か（境界輝度で判定）
    is_sep = []
    for i in range(1, len(bl) - 1):
        s = gray[max(0, bl[i] - 1):bl[i] + 2, :]
        is_sep.append(float(np.mean(s)) >= sep_th)

    # ランレングスで連続セルを1ブロックにまとめる
    blocks  = []
    in_blk  = False
    bstart  = 0
    for i in range(n):
        if cell_has_block[i]:
            if not in_blk:
                in_blk = True; bstart = i
            elif i > 0 and is_sep[i - 1]:
                blocks.append((bstart + 1, i, bl[bstart], bl[i]))
                bstart = i
        else:
            if in_blk:
                in_blk = False
                blocks.append((bstart + 1, i, bl[bstart], bl[i]))
    if in_blk:
        blocks.append((bstart + 1, n, bl[bstart], bl[n]))

    return blocks


# ─── ③ Vision API テキスト抽出 ──────────────────────────────────────────────

def extract_items(img: np.ndarray, y1: int, y2: int,
                  x1: int, x2: int,
                  client: anthropic.Anthropic,
                  is_vertical: bool = False) -> str:
    """
    ブロック領域をクロップしてVision APIに送り、品目テキストを返す。
    """
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return ''

    pil_img = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

    buf = io.BytesIO()
    pil_img.save(buf, format='JPEG', quality=95)
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode('utf-8')

    vertical_note = '縦書きです。右から左または上から下に読んでください。' if is_vertical else ''

    resp = client.messages.create(
        model='claude-opus-4-5',
        max_tokens=400,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'image',
                 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}},
                {'type': 'text',
                 'text': (
                     f'この画像に書かれているスーパーの割引品目を読んでください。{vertical_note}\n'
                     '大きな中黒（・）が大項目の区切りです。\n'
                     '各大項目を「/」で区切って出力してください。\n'
                     '例：しらす・ちりめん / カレー・シチュー / 缶詰\n'
                     '余分な説明不要。見えたものだけ。テキストがなければ空白を返す。'
                 )}
            ]
        }]
    )
    if not resp.content:
        return ''
    return resp.content[0].text.strip()


# ─── ④ 除外フィルター ────────────────────────────────────────────────────────

def apply_exclusions(text: str, keywords: list[str]) -> list[str]:
    """品目テキストから除外キーワードを含む項目を除去して返す"""
    if not text:
        return []
    items = [i.strip() for i in text.split('/') if i.strip()]
    return [item for item in items
            if not any(kw in item for kw in keywords)]


# ─── メイン ─────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print('エラー: ANTHROPIC_API_KEY が未設定', file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    # 画像読み込み（日本語パス対応）
    img = load_image(IMAGE_PATH)
    if img is None:
        print(f'エラー: {IMAGE_PATH} を読み込めません', file=sys.stderr)
        sys.exit(1)
    h, w = img.shape[:2]
    print(f'画像サイズ: {w}x{h}', file=sys.stderr)

    # デバッグ出力フォルダ
    if DEBUG_SAVE:
        os.makedirs(DEBUG_DIR, exist_ok=True)

    # ① 白線走査で正確な日付境界を取得
    print('日付境界線を検出中（白線走査）...', file=sys.stderr)
    boundary_lines = find_day_boundary_lines(img, DATE_COL_X1, DATE_COL_X2, TOTAL_DAYS)
    print(f'  境界線 {len(boundary_lines)} 本: y={boundary_lines[:6]}...', file=sys.stderr)
    print(f'  step例: {boundary_lines[1]-boundary_lines[0]}〜{boundary_lines[-1]-boundary_lines[-2]}px',
          file=sys.stderr)

    # デバッグ: 横線を画像に描画して保存（PIL使用、日本語パス対応）
    if DEBUG_SAVE:
        dbg = img.copy()
        for i, y in enumerate(boundary_lines[:-1]):
            cv2.line(dbg, (1310, y), (2420, y), (180, 180, 180), 1)
            cv2.putText(dbg, str(i + 1), (1315, y + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 200), 1)
        Image.fromarray(cv2.cvtColor(dbg, cv2.COLOR_BGR2RGB)).save(
            f'{DEBUG_DIR}/lines_detected.jpg')
        print(f'  → {DEBUG_DIR}/lines_detected.jpg に保存', file=sys.stderr)

    # ③ 各列でブロック検出 → テキスト抽出（COLUMN_DEFSは固定x座標）
    day_items: dict[int, list[str]] = {d: [] for d in range(1, TOTAL_DAYS + 1)}

    for col in COLUMN_DEFS:
        print(f'\n{col["name"]} (x={col["x1"]}~{col["x2"]}) を処理中...', file=sys.stderr)

        blocks = detect_colored_blocks(img, col['x1'], col['x2'], boundary_lines)
        print(f'  {len(blocks)} ブロック検出', file=sys.stderr)

        for idx, (day_start, day_end, y1, y2) in enumerate(blocks):
            print(f'  [{idx+1}] {day_start}日〜{day_end}日 (y={y1}〜{y2})  テキスト抽出中...',
                  file=sys.stderr)

            text  = extract_items(img, y1, y2, col['x1'], col['x2'],
                                  client, col['vertical'])
            items = apply_exclusions(text, EXCLUDE_KEYWORDS)

            print(f'       -> {items}', file=sys.stderr)

            # デバッグ: ブロック画像をPILで保存（日本語パス対応）
            if DEBUG_SAVE:
                crop = img[y1:y2, col['x1']:col['x2']]
                fname = f'{DEBUG_DIR}/{col["name"][:3]}_{day_start:02d}-{day_end:02d}_{idx}.jpg'
                Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).save(fname)

            # 日付範囲の各日に品目を追加
            for day in range(day_start, min(day_end + 1, TOTAL_DAYS + 1)):
                day_items[day].extend(items)

    # ④ 重複除去してJSON出力
    output = {}
    for day in range(1, TOTAL_DAYS + 1):
        d   = START_DAY + timedelta(days=day - 1)
        key = d.strftime('%Y-%m-%d')
        unique_items = list(dict.fromkeys(day_items[day]))  # 順序保持で重複除去
        if unique_items:
            output[key] = unique_items

    # UTF-8で直接ファイルに書く（PowerShellのstdoutエンコーディング問題を回避）
    out_path = os.path.join(os.path.dirname(IMAGE_PATH),
                            'scrapers', 'summit_april2026_raw2.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f'Saved: {out_path}', file=sys.stderr)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
