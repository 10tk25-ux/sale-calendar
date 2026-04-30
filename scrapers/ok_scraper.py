#!/usr/bin/env python3
"""
オーケーストア チラシスクレイパー（tokubai 経由）
tokubai の店舗ページからチラシ画像を取得し、Claude Vision API で商品情報を抽出する

★ STORE_ID の調べ方:
  https://tokubai.co.jp/%E3%82%AA%E3%83%BC%E3%82%B1%E3%83%BC/
  ↑ で最寄り店舗を開くとURL末尾の数字が STORE_ID
"""

import re
import json
import base64
import os
import sys
import time
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup
import anthropic

# ── 設定 ─────────────────────────────────────────────
# ★ ご利用の店舗に合わせて STORE_ID を変更してください
STORE_ID   = 258480          # オーケー 札の辻店
STORE_SLUG = "%E3%82%AA%E3%83%BC%E3%82%B1%E3%83%BC"   # "オーケー" URLエンコード
STORE_NAME = "オーケー"
TOKUBAI_BASE = "https://tokubai.co.jp"
IMAGE_BASE   = "https://image.tokubai.co.jp/images/bargain_office_leaflets/o=true"

# 期間が何日以上なら「月間チラシ」とみなしてスキップするか
MONTHLY_THRESHOLD_DAYS = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

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
- 日用品・生活用品（洗剤・虫よけ・化粧品・スポンジ・入浴剤・トイレ用品など）

ルール:
- 商品名から産地（●●産・国産・国内産など）を除去してください
- priority: 2 = 肉・野菜・卵・豆腐・乳製品、1 = 菓子・調味料・飲料・冷凍食品
- prominence: チラシ上の価格表示の大きさ 1〜5（大きいほど高い）
- セール期間が画像に含まれていれば抽出（YYYY-MM-DD形式）
- チラシ内に「◯◯日限り」「◯日〜◯日」などの日付別コーナーがある場合、
  その日付をそのコーナーの商品の period_override として使ってください
- category: 商品カテゴリを以下から1つ選択
  "肉類" / "野菜" / "冷凍食品" / "乳製品・卵" / "調味料" / "飲料" / "菓子" / "その他"

JSONのみ返してください（説明文不要）:
{
  "period": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
  "items": [
    {"name": "商品名", "price": 数値, "unit": "単位文字列", "priority": 1か2, "prominence": 1〜5,
     "category": "カテゴリ名",
     "period_override": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}
  ]
}
period が読み取れない場合は null にしてください。
period_override は日付別コーナーの商品のみ設定し、通常商品は省略してください。
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


def parse_date_range(text: str) -> dict | None:
    """YYYY-MM-DD〜YYYY-MM-DD や M/D〜M/D などを解析"""
    year = datetime.now().year

    # ISO 形式
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}).*?(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return {
            "from": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
            "to":   f"{m.group(4)}-{m.group(5)}-{m.group(6)}",
        }

    # M/D〜M/D 形式
    m = re.search(r"(\d{1,2})[/月](\d{1,2})[日]?\s*[〜～~\-]+\s*(\d{1,2})[/月](\d{1,2})", text)
    if m:
        m1, d1, m2, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if 1 <= m1 <= 12 and 1 <= d1 <= 31 and 1 <= m2 <= 12 and 1 <= d2 <= 31:
            return {
                "from": f"{year}-{m1:02d}-{d1:02d}",
                "to":   f"{year}-{m2:02d}-{d2:02d}",
            }
    return None


def is_monthly(period: dict) -> bool:
    """期間が長すぎる（月間チラシ）かどうか判定"""
    try:
        d_from = datetime.strptime(period["from"], "%Y-%m-%d")
        d_to   = datetime.strptime(period["to"],   "%Y-%m-%d")
        return (d_to - d_from).days >= MONTHLY_THRESHOLD_DAYS
    except Exception:
        return False

# ── tokubai スクレイピング ──────────────────────────────

def fetch_leaflets(store_id: int, store_slug: str) -> list[dict]:
    """
    店舗ページから leaflet 一覧を取得する。
    tokubai は Next.js 製で __NEXT_DATA__ JSON に全データが含まれる。
    """
    url = f"{TOKUBAI_BASE}/{store_slug}/{store_id}"
    print(f"  店舗ページ取得: {url}", file=sys.stderr)
    resp = get(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    leaflets = []
    seen_ids = set()

    # ── 方法1: __NEXT_DATA__ (JSON) から抽出 ──
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag:
        try:
            data = json.loads(next_data_tag.string)
            text = json.dumps(data, ensure_ascii=False)

            for m in re.finditer(r'"leafletId"\s*:\s*(\d+)', text):
                leaflet_id = int(m.group(1))
                if leaflet_id in seen_ids:
                    continue
                seen_ids.add(leaflet_id)

                # 前後500文字に拡大して日付を探す
                fragment = text[max(0, m.start()-500):m.end()+500]
                period = parse_date_range(fragment)
                leaflets.append({"id": leaflet_id, "period": period})
        except Exception as e:
            print(f"    [warn] __NEXT_DATA__ 解析エラー: {e}", file=sys.stderr)

    # ── 方法2: HTML の <a href> から /leaflets/ID を探す ──
    if not leaflets:
        pat2 = re.compile(r"/leaflets/(\d+)")
        for a in soup.find_all("a", href=pat2):
            m2 = pat2.search(a["href"])
            if not m2:
                continue
            leaflet_id = int(m2.group(1))
            if leaflet_id in seen_ids:
                continue
            seen_ids.add(leaflet_id)
            parent = a.find_parent(["li", "div", "article"]) or a
            period = parse_date_range(parent.get_text(" ", strip=True))
            leaflets.append({"id": leaflet_id, "period": period})

    # ── 方法3: <script> 内の JSON 文字列から直接 regex ──
    if not leaflets:
        for script in soup.find_all("script"):
            if not script.string:
                continue
            for m in re.finditer(r'"id"\s*:\s*(\d{8,9})', script.string):
                leaflet_id = int(m.group(1))
                if leaflet_id in seen_ids:
                    continue
                seen_ids.add(leaflet_id)
                fragment = script.string[max(0, m.start()-300):m.end()+300]
                period = parse_date_range(fragment)
                leaflets.append({"id": leaflet_id, "period": period})

    print(f"  {len(leaflets)} 件のチラシを検出", file=sys.stderr)
    return leaflets


def fetch_leaflet_images(leaflet_id: int, store_id: int, store_slug: str) -> list[str]:
    """leaflet ページから o=true 画像 URL 一覧を取得する"""
    url = f"{TOKUBAI_BASE}/{store_slug}/{store_id}/leaflets/{leaflet_id}"
    print(f"    leaflet ページ取得: {url}", file=sys.stderr)
    try:
        resp = get(url)
    except Exception as e:
        print(f"    [warn] ページ取得失敗: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    image_ids = []
    seen = set()
    full_text = str(soup)

    for m in re.finditer(r"bargain_office_leaflets/o=true/(\d+\.jpg)", full_text):
        img_id = m.group(1)
        if img_id not in seen:
            seen.add(img_id)
            image_ids.append(img_id)

    if not image_ids:
        for m in re.finditer(r"bargain_office_leaflets/[^/\"']+/(\d+\.jpg)", full_text):
            img_id = m.group(1)
            if img_id not in seen:
                seen.add(img_id)
                image_ids.append(img_id)

    urls = [f"{IMAGE_BASE}/{img_id}" for img_id in image_ids]
    urls = [u.split("?")[0] for u in urls]
    print(f"    画像 {len(urls)} 枚", file=sys.stderr)
    return urls

# ── Vision API 解析 ────────────────────────────────────

def analyze_image(image_url: str, client: anthropic.Anthropic) -> dict | None:
    try:
        resp = get(image_url)
        if resp.status_code != 200:
            print(f"    [warn] 画像取得失敗: HTTP {resp.status_code}", file=sys.stderr)
            return None

        image_b64 = base64.standard_b64encode(resp.content).decode()
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
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return _safe_json_loads(m.group())

    except Exception as e:
        print(f"    [warn] 画像解析エラー: {e}", file=sys.stderr)

    return None


def _safe_json_loads(s: str) -> dict | None:
    """JSONパース。失敗時は末尾補完・trailing comma除去を試みる"""
    # 1. そのままパース
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 2. trailing comma 除去
    fixed = re.sub(r',\s*([}\]])', r'\1', s)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # 3. 切り捨てられた末尾を補完
    depth_c, depth_s = 0, 0
    for ch in fixed:
        if ch == '{': depth_c += 1
        elif ch == '}': depth_c -= 1
        elif ch == '[': depth_s += 1
        elif ch == ']': depth_s -= 1
    # 最後の不完全な文字列/配列要素を削除して閉じる
    # 最後の完全なカンマ区切り位置まで切り戻す
    trimmed = re.sub(r',\s*[^,}\]]*$', '', fixed)
    trimmed += ']' * max(depth_s, 0) + '}' * max(depth_c, 0)
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError as e:
        print(f"    [warn] JSON修復失敗: {e}", file=sys.stderr)
        return None

# ── エントリ生成 ──────────────────────────────────────

def strip_origin(name: str) -> str:
    return re.sub(r'\S*産\s*', '', name).strip()


def build_entries(leaflet_period: dict | None, image_results: list[dict]) -> list[dict]:
    resolved_period = next(
        (r["period"] for r in image_results if r and r.get("period")),
        leaflet_period,
    )
    if not resolved_period:
        # フォールバック: 今日〜6日後（期間不明時）
        today = datetime.now()
        resolved_period = {
            "from": today.strftime("%Y-%m-%d"),
            "to":   (today + timedelta(days=6)).strftime("%Y-%m-%d"),
        }
        print(f"    [info] period 不明のためフォールバック: {resolved_period}", file=sys.stderr)

    entries = []
    seen_names = set()

    for result in image_results:
        if not result:
            continue
        for item in result.get("items", []):
            name = item.get("name", "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            period = item.get("period_override") or resolved_period

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

def run(store_id: int = STORE_ID, client: anthropic.Anthropic = None) -> list[dict]:
    """
    スクレイプ実行。entries リストを返す。
    client が None の場合は ANTHROPIC_API_KEY 環境変数から作成する。
    """
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("環境変数 ANTHROPIC_API_KEY が設定されていません")
        client = anthropic.Anthropic(api_key=api_key)

    print(f"\n{'='*55}", file=sys.stderr)
    print(f"  {STORE_NAME} スクレイパー (tokubai 経由)", file=sys.stderr)
    print(f"{'='*55}", file=sys.stderr)

    print(f"\n[1] チラシ一覧を取得中... (店舗ID: {store_id})", file=sys.stderr)
    leaflets = fetch_leaflets(store_id, STORE_SLUG)

    if not leaflets:
        print("  チラシが見つかりませんでした", file=sys.stderr)
        return []

    # 月間チラシをスキップ
    weekly = []
    for lf in leaflets:
        if lf["period"] and is_monthly(lf["period"]):
            print(f"  スキップ (月間): id={lf['id']} {lf['period']}", file=sys.stderr)
        else:
            weekly.append(lf)
            p = lf["period"] or "期間不明"
            print(f"  対象: id={lf['id']} {p}", file=sys.stderr)

    if not weekly:
        print("  週次チラシが見つかりませんでした", file=sys.stderr)
        return []

    print(f"\n[2] 画像URLを収集中 ({len(weekly)} チラシ)...", file=sys.stderr)
    all_image_urls: list[str] = []
    seen_img_ids: set[str] = set()
    representative_period: dict | None = None

    for lf in weekly:
        img_urls = fetch_leaflet_images(lf["id"], store_id, STORE_SLUG)
        for url in img_urls:
            img_id = url.split("/")[-1]
            if img_id not in seen_img_ids:
                seen_img_ids.add(img_id)
                all_image_urls.append(url)

        if lf["period"]:
            if representative_period is None:
                representative_period = lf["period"]
            else:
                try:
                    existing_len = (
                        datetime.strptime(representative_period["to"], "%Y-%m-%d") -
                        datetime.strptime(representative_period["from"], "%Y-%m-%d")
                    ).days
                    new_len = (
                        datetime.strptime(lf["period"]["to"], "%Y-%m-%d") -
                        datetime.strptime(lf["period"]["from"], "%Y-%m-%d")
                    ).days
                    if new_len < existing_len:
                        representative_period = lf["period"]
                except Exception:
                    pass

    print(f"  ユニーク画像 {len(all_image_urls)} 枚\n", file=sys.stderr)

    print("[3] 画像を解析中...", file=sys.stderr)
    image_results = []
    THIS_YEAR = datetime.now().year
    for j, img_url in enumerate(all_image_urls, 1):
        img_id = img_url.split("/")[-1]
        print(f"    [{j}/{len(all_image_urls)}] {img_id} を解析中... ", end="", flush=True, file=sys.stderr)
        result = analyze_image(img_url, client)
        if result:
            n = len(result.get("items", []))
            print(f"{n} 品抽出", file=sys.stderr)
        else:
            print("抽出失敗", file=sys.stderr)
        image_results.append(result)

    entries = build_entries(representative_period, image_results)

    # クリーニング（今年のデータ、price/unit あり）
    clean_entries = []
    for e in entries:
        if not e["price"] or not e["unit"]:
            continue
        if not e["from"].startswith(str(THIS_YEAR)):
            continue
        e["name"] = strip_origin(e["name"])
        if e["name"]:
            clean_entries.append(e)

    print(f"\n  → {len(clean_entries)} エントリ（クリーニング後）", file=sys.stderr)
    return clean_entries


def main():
    entries = run()
    output_path = os.path.join(os.path.dirname(__file__), "ok_sales.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"保存先: {output_path}", file=sys.stderr)
    print(json.dumps(entries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
