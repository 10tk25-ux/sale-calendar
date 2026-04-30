# 特売カレンダー 引き継ぎメモ
作成: 2026-04-30

---

## プロジェクト概要

`G:\マイドライブ\Claude\tokubai_calendar.html`
- サミット（品種別割引カレンダー）・オーケー・日進の特売情報を週次表示するHTMLカレンダー
- Pythonスクレイパーで自動更新、スケジュールタスクで定期実行

---

## ファイル構成

```
G:\マイドライブ\Claude\
├── tokubai_calendar.html          # メインHTML（SALES・SUMMIT_MONTHLY を内包）
├── serve.py                       # 簡易HTTPサーバー（port 3000）
└── scrapers\
    ├── update_tokubai.py          # オーケー＋日進 SALES 更新スクリプト（2日おき実行）
    ├── update_summit.py           # サミット SUMMIT_MONTHLY 更新スクリプト（月1日・25日実行）
    ├── ok_scraper.py              # オーケー tokubai スクレイパー（STORE_ID=258480）
    ├── nissin_scraper.py          # 日進 WordPressブログ スクレイパー
    ├── summit_calendar_parser.py  # サミット PDF→Vision API パーサー
    ├── sales_latest.json          # update_tokubai.py の最終出力（デバッグ用）
    ├── summit_202605_clean.json   # 5月サミットデータ（クリーニング済み）
    └── HANDOFF.md                 # このファイル
```

---

## HTMLの内部データ構造

```javascript
// 週次特売（オーケー・日進）
const SALES = [
  { store:"オーケー", from:"YYYY-MM-DD", to:"YYYY-MM-DD",
    name:"商品名", price:数値, unit:"単位", priority:1|2, prominence:1〜5 },
  ...
];

// サミット 品種別割引カレンダー（月間）
const SUMMIT_MONTHLY = {
  "2026-05-01": ["カレー・シチュー", "味噌・インスタント味噌汁", ...],
  "2026-05-02": ["えび", "ひき肉", ...],
  ...
};
```

**カレンダーの描画ロジック（重要）：**
- `SALES` の `from !== to` → 上段グリーンバナー（期間セール）
- `SALES` の `from === to` → 下段「本日限り」カード（日替わり）
- `SUMMIT_MONTHLY` → 中段サミットバンド（月間データ）

---

## 現在のデータ状態（2026-04-30時点）

| データ | 状態 |
|--------|------|
| SUMMIT_MONTHLY | 5月分（5/1〜5/31）入り済み ✅ |
| SALES（オーケー） | 4/20〜4/26の旧チラシ（期限切れ）。新チラシ待ち |
| SALES（日進） | 同上。サイトに現在セール記事なし |

→ 5月に入り、tokubaiに新チラシが上がれば自動更新で反映される。

---

## スケジュールタスク

| タスク名 | cron | 内容 |
|----------|------|------|
| `update-tokubai-sales` | `0 6 */2 * *` | 2日おき6時 / update_tokubai.py |
| `update-summit-calendar` | `0 7 1,25 * *` | 毎月1日・25日7時 / update_summit.py |

※ 25日実行は翌月カレンダーの先取りのため

---

## スクレイパー詳細

### オーケー (ok_scraper.py)
- URL: `https://tokubai.co.jp/オーケー/258480`（札の辻店）
- `__NEXT_DATA__` JSON から leafletId 抽出 → 画像URL → Vision API (claude-opus-4-5)
- max_tokens: 4096（本セッションで修正済み）
- JSON修復関数 `_safe_json_loads()` 追加済み
- period 取得失敗時は今日〜+6日をフォールバックとして使用

### 日進 (nissin_scraper.py)
- URL: `https://www.nissin-world-delicatessen.jp/news/sale/`
- WordPressブログの記事ページから画像取得 → Vision API
- max_tokens: 4096（本セッションで修正済み）
- JSON修復関数 `_safe_json_loads()` 追加済み

### サミット (update_summit.py)
- シュフー XML API: `https://asp.shufoo.net/api/shopDetailNewXML/825407/`
- publishEnd が対象月の末日のチラシを候補として抽出
- claude-haiku-4-5 でサムネイル識別（品種別割引カレンダーか判定）
- chirashi.pdf をダウンロード → PyMuPDF 4x変換 → Vision API
- **過去の誤識別事例**: `5661346637201`（通常チラシ）を誤ってカレンダーと判定したことあり

---

## EXCLUDE_PATTERNS（update_summit.py）

5月データのクリーニングで確定したノイズパターン：

```python
EXCLUDE_PATTERNS = [
    re.compile(r'^\d+$'),
    re.compile(r'^[月火水木金土日]$'),
    re.compile(r'^[ぁ-んァ-ン]{1}$'),    # 1文字のみ（えび・パンは残す）
    re.compile(r'^.{1,2}・.{1,2}$'),     # ズ・グ 等の短縮ゴミ
    re.compile(r'^ムチ$'),
    re.compile(r'キャンペーン'),
    re.compile(r'画像には'),
    re.compile(r'ます。$'),
    re.compile(r'^空白$'),
    re.compile(r'倍$'),
    re.compile(r'ジレ.*ヤギ'),
    re.compile(r'^ふりかけ・$'),
    re.compile(r'梅干'),
    re.compile(r'佃煮'),
    re.compile(r'[茨薮粒養佃]'),          # OCRゴミ漢字
    re.compile(r'^増汁$'),
    re.compile(r'^VR\b'),
    re.compile(r'^[A-Z]{2,}\s'),
]
```

---

## 未解決の懸念事項

1. **サミット自動識別の精度**: haiku の Vision API がたまに通常チラシをカレンダーと誤認する。
   改善案: 複数候補を全てパースして、日数が最多のものを採用する方式に変更する。

2. **カレンダー表示の挙動**: 本セッションでpreviewツール使用中に
   - 500px以下に縮小するとモバイルビューに切り替わる（日付単体表示になる）
   - `body.style.zoom` + `overflow:hidden` での3日表示は機能したが不安定
   → HTML側にモバイル/デスクトップ切替ブレークポイントあり（要確認）

3. **4/30 サミット「—」**: SUMMIT_MONTHLY は5/1〜5/31のため4/30はデータなし。
   4月データは既に上書き済み。許容範囲か要確認。

---

## サーバー起動方法

```
.claude/launch.json の「特売カレンダー」を使用
→ serve.py が port 3000 で起動
→ http://localhost:3000/tokubai_calendar.html
```

## 手動実行方法

```powershell
$env:ANTHROPIC_API_KEY = $env:ANTHROPIC_ORG_ID
cd "G:\マイドライブ\Claude\scrapers"

# オーケー＋日進 更新
python update_tokubai.py

# サミット 更新（通常は自動）
python update_summit.py
```

---

## summit_calendar_parser.py の列定義（参考）

- PDF は PyMuPDF で 4倍（2480×3508px）に変換
- 列定義 COLUMN_DEFS は x1/x2 座標で区切られた縦列
- bookW=620×4=2480px で全列が収まる設計
