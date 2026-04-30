#!/usr/bin/env python3
"""
日進ワールドデリカテッセン チラシスクレイパー
セール記事を自動取得し、Claude Vision API で商品情報を抽出する
"""

import re
import json
import base64
import os
import sys
import time
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
import anthropic

# ── 設定 ─────────────────────────────────────────────
BASE_URL      = "https://www.nissin-world-delicatessen.jp"
NEWS_URL      = f"{BASE_URL}/news/sale/"   # セール専用カテゴリページ
STORE_NAME    = "日進"
MAX_AGE_DAYS  = 60   # 何日前までの記事を対象にするか

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tokubai-calendar/1.0)"}

EXTRACT_PROMPT = """\
このスーパーのチラシ画像から商品情報を抽出してください。

【含める】
- 肉類（精肉・加工肉・ハム・ソーセージ・焼き鳥・冷凍肉など）
- 野菜・きのこ
- 卵
- 豆腐・乳製品（チーズ・バター・牛乳など）
- 菓子・スナック
- 調味料・ソース・ドレッシング・油・酢
- 飲料（ジュース・お茶・コーヒーなど）
- 冷凍食品（袋・パッケージ入り品）

【除外する（絶対に含めないこと）】
- フルーツ・果物
- 酒類（ビール・ワイン・日本酒・焼酎・ウイスキーなど）
- 惣菜・弁当（フライドチキン・から揚げ・コロッケ・ポテトサラダ・春巻き〈できあい〉・
  ハンバーグ弁当・おかず・サラダ〈できあい〉など、店内調理済み・できあい品）
- パン・ベーカリー商品
- 生鮮魚介類（刺身・切り身・干物・海産物など）

ルール:
- 商品名から産地（●●産・国産・国内産など）を除去してください
- priority: 2 = 肉・野菜・卵・豆腐・乳製品、1 = 菓子・調味料・飲料・冷凍食品
- prominence: チラシ上の価格表示の大きさ 1〜5（大きいほど高い）
- セール期間が画像に含まれていれば抽出（YYYY-MM-DD形式）
- category: 商品カテゴリを以下から1つ選択
  "肉類" / "野菜" / "冷凍食品" / "乳製品・卵" / "調味料" / "飲料" / "菓子" / "その他"

JSONのみ返してください（説明文不要）:
{
  "period": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
  "items": [
    {"name": "商品名", "price": 数値, "unit": "単位文字列", "priority": 1か2, "prominence": 1〜5,
     "category": "カテゴリ名"}
  ]
}
period が読み取れない場合は null にしてください。
"""

# ── ユーティリティ ─────────────────────────────────────

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


def parse_period_from_text(text: str) -> dict | None:
    """テキストから 'M/D〜M/D' や 'M.D▶M.D' などの日付範囲を抽出"""
    year = datetime.now().year
    patterns = [
        r"(\d{1,2})[/\.](\d{1,2})[^\d]{1,10}?[▶〜～\-]+[^\d]{0,5}?(\d{1,2})[/\.]?(\d{1,2})",
        r"(\d{1,2})月(\d{1,2})日.*?(\d{1,2})月(\d{1,2})日",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                m1, d1, m2, d2 = (int(m.group(x)) for x in range(1, 5))
                if 1 <= m1 <= 12 and 1 <= d1 <= 31 and 1 <= m2 <= 12 and 1 <= d2 <= 31:
                    return {
                        "from": f"{year}-{m1:02d}-{d1:02d}",
                        "to":   f"{year}-{m2:02d}-{d2:02d}",
                    }
            except ValueError:
                continue
    return None


def strip_dimension_suffix(url: str) -> str:
    """WordPress サムネイル URL のサイズ指定を除去してオリジナルを取得"""
    return re.sub(r"-\d+x\d+(\.\w+)$", r"\1", url)

# ── スクレイピング ─────────────────────────────────────

def fetch_sale_article_urls() -> list[dict]:
    """セール専用ページから記事 URL を取得し、直近 MAX_AGE_DAYS 日以内に絞る"""
    from datetime import timedelta
    resp = get(NEWS_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    seen, articles = set(), []
    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)

    for a in soup.find_all("a", href=re.compile(r"/news/sale/\d+/")):
        href = a["href"]
        if not href.startswith("http"):
            href = BASE_URL + href
        if href in seen:
            continue
        seen.add(href)

        parent = a.find_parent(["li", "article", "div"])
        raw_text = parent.get_text(" ", strip=True) if parent else a.get_text(strip=True)

        # 投稿日を "YYYY-M-D" / "YYYY-MM-DD" 形式で探す
        date_m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw_text)
        posted = None
        if date_m:
            try:
                posted = datetime(int(date_m.group(1)),
                                  int(date_m.group(2)),
                                  int(date_m.group(3)))
            except ValueError:
                pass

        # 古い記事はスキップ
        if posted and posted < cutoff:
            continue

        articles.append({
            "url":    href,
            "title":  raw_text[:80],
            "posted": posted.strftime("%Y-%m-%d") if posted else "不明",
        })

    return articles


def fetch_article_info(url: str) -> dict:
    """記事ページから画像 URL・セール期間・本文テキストを取得"""
    resp = get(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    # 本文エリアを特定
    body = (
        soup.find("div", class_=re.compile(r"entry[-_]?(content|body|text)", re.I))
        or soup.find("div", class_=re.compile(r"post[-_]?(content|body)", re.I))
        or soup.find("main")
        or soup.body
    )

    images = []
    if body:
        for img in body.find_all("img"):
            src = img.get("src") or img.get("data-src", "")
            if "wp-content/uploads" in src:
                src = strip_dimension_suffix(src)
                if src not in images:
                    images.append(src)

    full_text = soup.get_text(" ")
    period = parse_period_from_text(full_text)

    return {"images": images, "period": period}

# ── Vision API 解析 ────────────────────────────────────

def analyze_image(image_url: str, client: anthropic.Anthropic) -> dict | None:
    """画像を Claude Vision API に送って商品情報を抽出"""
    try:
        resp = get(image_url)
        image_b64 = base64.standard_b64encode(resp.content).decode()

        # メディアタイプを判定
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            ct = "image/jpeg"

        prompt_with_year = f"現在は{datetime.now().year}年です。年が明示されていない日付はこの年として解釈してください。\n\n" + EXTRACT_PROMPT
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": ct, "data": image_b64}},
                    {"type": "text", "text": prompt_with_year},
                ],
            }],
        )

        raw = message.content[0].text.strip()
        # JSON ブロックを抽出（```json ... ``` にも対応）
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return _safe_json_loads(m.group())

    except Exception as e:
        print(f"    [warn] 画像解析エラー: {e}", file=sys.stderr)

    return None


def _safe_json_loads(s: str) -> dict | None:
    """JSONパース。失敗時は末尾補完・trailing comma除去を試みる"""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r',\s*([}\]])', r'\1', s)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    depth_c, depth_s = 0, 0
    for ch in fixed:
        if ch == '{': depth_c += 1
        elif ch == '}': depth_c -= 1
        elif ch == '[': depth_s += 1
        elif ch == ']': depth_s -= 1
    trimmed = re.sub(r',\s*[^,}\]]*$', '', fixed)
    trimmed += ']' * max(depth_s, 0) + '}' * max(depth_c, 0)
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError as e:
        print(f"    [warn] JSON修復失敗: {e}", file=sys.stderr)
        return None

# ── エントリ生成 ──────────────────────────────────────

def build_entries(article_period: dict | None, image_results: list[dict]) -> list[dict]:
    """SALES 配列用エントリを生成"""
    entries = []
    # 画像から取得した period を優先、なければ記事テキストから取得した period を使用
    period = next(
        (r["period"] for r in image_results if r and r.get("period")),
        article_period,
    )
    if not period:
        return []

    seen_names = set()
    for result in image_results:
        if not result:
            continue
        for item in result.get("items", []):
            name = item.get("name", "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            entries.append({
                "store":      STORE_NAME,
                "from":       period["from"],
                "to":         period["to"],
                "name":       name,
                "price":      item.get("price", 0),
                "unit":       item.get("unit", "個"),
                "priority":   item.get("priority", 1),
                "prominence": item.get("prominence", 2),
                "category":   item.get("category", "その他"),
            })
    return entries

# ── メイン ────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: 環境変数 ANTHROPIC_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    all_entries: list[dict] = []

    print("=" * 50)
    print("  日進ワールドデリカテッセン スクレイパー")
    print("=" * 50)

    # Step 1: セール記事 URL を取得
    print(f"\n[1] セール記事を検索中... (直近 {MAX_AGE_DAYS} 日以内)")
    articles = fetch_sale_article_urls()
    if not articles:
        print("  対象記事が見つかりませんでした")
        return
    for a in articles:
        print(f"  [{a['posted']}] {a['title'][:50]}")
    print(f"\n  合計 {len(articles)} 件\n")

    # Step 2: 各記事を処理
    for i, article in enumerate(articles, 1):
        print(f"[{i}] {article['url']}")

        info = fetch_article_info(article["url"])
        period_str = f"{info['period']['from']} 〜 {info['period']['to']}" if info["period"] else "取得できず"
        print(f"    期間: {period_str}")
        print(f"    画像: {len(info['images'])} 枚")

        image_results = []
        for j, img_url in enumerate(info["images"], 1):
            print(f"    画像 {j} を解析中... ", end="", flush=True)
            result = analyze_image(img_url, client)
            if result:
                n = len(result.get("items", []))
                print(f"{n} 品抽出")
            else:
                print("抽出失敗")
            image_results.append(result)

        entries = build_entries(info["period"], image_results)
        all_entries.extend(entries)
        print(f"    → {len(entries)} エントリを追加\n")

    # Step 3: 結果を出力
    print("=" * 50)
    print(f"  合計 {len(all_entries)} 品")
    print("=" * 50)

    output_path = os.path.join(os.path.dirname(__file__), "nissin_sales.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_entries, f, ensure_ascii=False, indent=2)
    print(f"\n保存先: {output_path}")
    print("\n--- SALES 配列 (HTML 貼り付け用) ---")
    for e in all_entries:
        print(
            f'  {{ store:"{e["store"]}", from:"{e["from"]}", to:"{e["to"]}", '
            f'name:"{e["name"]}", price:{e["price"]}, unit:"{e["unit"]}", '
            f'priority:{e["priority"]}, prominence:{e["prominence"]} }},'
        )


if __name__ == "__main__":
    main()
