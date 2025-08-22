# post_picture_book.py
# - 楽天ブックスAPIから「絵本」だけを取得（文庫/新書/漫画などは除外）
# - OpenAIで短い本文（<=140字・最大2文）を生成
# - X(Twitter)へ投稿（refresh_token → access_token）
# - refresh_token がローテーションされたら GITHUB_OUTPUT に書き出す
#
# 必須env:
#   RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID
#   OPENAI_API_KEY
#   TW_CLIENT_ID, TW_CLIENT_SECRET, TW_REFRESH_TOKEN
#
# 2025-03 改訂版（アフィリンク固定・ジャンル厳格化）

from __future__ import annotations
import os, re, json, random, base64
from typing import Dict, Any, List
import requests

# ------------------------------------------------------------
# 定数/ユーティリティ
# ------------------------------------------------------------
USER_AGENT = "ehon-no-mori-bot/2.1 (+https://github.com/)"
MAX_BODY = 140
GENRE_PICTURE = os.getenv("RAKUTEN_GENRE_PICTURE", "001004001")  # 絵本ジャンルID

def log(*args: Any) -> None:
    print(*args, flush=True)

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"環境変数が未設定です: {name}")
    return v

def safe_get(d: Dict[str, Any], key: str, default: str = "") -> str:
    v = d.get(key, default)
    if v is None:
        return ""
    return str(v).strip()

# ------------------------------------------------------------
# 絵本フィルタ（文庫/新書/コミック等を除外）
# ------------------------------------------------------------
NG_WORDS = [
    "文庫", "新書", "児童文学", "小説", "ノベル", "ラノベ",
    "コミック", "漫画", "マンガ", "ムック",
    "青い鳥文庫", "つばさ文庫", "みらい文庫", "ポケット文庫",
]

def is_picture_book(it: Dict[str, Any]) -> bool:
    # 楽天の分類ミス対策としてテキスト側も確認
    t = (it.get("title") or "")
    c = (it.get("itemCaption") or "")
    series = (it.get("seriesName") or "")
    label  = (it.get("label") or "")
    size   = (it.get("size") or "")
    blob = " ".join([t, c, series, label, size])
    return not any(kw in blob for kw in NG_WORDS)

# ------------------------------------------------------------
# 楽天API：絵本のみ取得（アフィリンク固定）
# ------------------------------------------------------------
def fetch_book() -> Dict[str, str]:
    """
    楽天ブックスAPIから《絵本ジャンルのみ》で itemCaption 付きのアイテムを1件返す。
    """
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"
    base_params = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json",
        "formatVersion": 2,
        "hits": 30,
        "availability": 1,                  # 在庫あり
        "booksGenreId": GENRE_PICTURE,      # 絵本に固定
        "sort": "-reviewCount",             # レビュー多い順（降順）
        # フィルタ用に項目拡張
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
    }

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def pick_page(page: int) -> List[Dict[str, Any]]:
        r = s.get(URL, params=dict(base_params, page=page), timeout=25)
        if r.status_code != 200:
            log("Rakuten API error:", r.status_code, r.text[:500])
        r.raise_for_status()
        data = r.json()
        items = [it.get("Item") or it for it in data.get("Items", [])]
        # キャプション必須＋絵本フィルタ
        items = [it for it in items if (it.get("itemCaption") or "").strip()]
        items = [it for it in items if is_picture_book(it)]
        return items

    # ランダムページを複数試行（偏り防止）
    for _ in range(10):
        page = random.randint(1, 60)
        items = pick_page(page)
        if items:
            it = random.choice(items)
            caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
            # ★アフィリンク固定（なければ直URL）
            link = (it.get("affiliateUrl") or it.get("itemUrl") or "").strip()
            return {
                "title":  safe_get(it, "title"),
                "author": safe_get(it, "author"),
                "caption": caption,
                "url": link,
                "ra": safe_get(it, "reviewAverage"),
                "rc": safe_get(it, "reviewCount"),
            }

    raise RuntimeError("楽天API: 絵本ジャンルから取得できませんでした")

# ------------------------------------------------------------
# OpenAI で本文生成（140字・最大2文）
# ------------------------------------------------------------
def build_post(book: Dict[str, str]) -> str:
    """
    X向け紹介文を生成（140字以内・最大2文・URLは本文に含めない）
    """
    from openai import OpenAI
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    SYSTEM = (
        "あなたは書店員。日本語でX向けの“短文”紹介文を作る。"
        "制約: 本文は必ず140字以内、文は最大2文。"
        "絵文字は0〜1個、ハッシュタグは最大2つ。"
        "セールス臭は抑え、誠実で温かく。"
        "誰向け/どのシーンかを1フレーズ入れる。"
        "URLは投稿側で別行に付けるため、本文には含めない。"
        "出力は本文のみ。"
    )
    USER = (
        f"書名:{book['title']}\n"
        f"著者:{book['author']}\n"
        f"紹介の種:{book['caption']}\n"
        f"平均レビュー:{book['ra']} / 件数:{book['rc']}\n"
        "条件どおり短く端的に。"
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": USER}],
        temperature=0.7,
        max_tokens=160,
    )

    body = (resp.choices[0].message.content or "").strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1].rstrip() + "…"

    # 改行の次行にURLを出す（リンクの見え方を安定させる）
    if book["url"]:
        return f"{body}\n{book['url']}"
    return body

# ------------------------------------------------------------
# X（Twitter）投稿（refresh→access）
# ------------------------------------------------------------
def post_to_x(text: str) -> Dict[str, Any] | None:
    tw_client_id     = require_env("TW_CLIENT_ID")
    tw_client_secret = require_env("TW_CLIENT_SECRET")
    tw_refresh_token = require_env("TW_REFRESH_TOKEN")

    basic = base64.b64encode(f"{tw_client_id}:{tw_client_secret}".encode()).decode()

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    # refresh -> access
    token_url = "https://api.twitter.com/2/oauth2/token"
    r = s.post(
        token_url,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": tw_refresh_token,
            "client_id": tw_client_id,
            "scope": "tweet.read tweet.write users.read offline.access",
        },
        timeout=25,
    )
    if r.status_code != 200:
        log("X TOKEN ERROR:", r.status_code, r.text[:800])
        r.raise_for_status()

    token_payload = r.json()
    access_token = token_payload["access_token"]
    new_refresh  = token_payload.get("refresh_token")

    # 新しい refresh_token が返ってきたら GITHUB_OUTPUT に書く
    if new_refresh:
        try:
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a", encoding="utf-8") as f:
                    print(f"new_refresh_token={new_refresh}", file=f)
                log("new_refresh_token written to GITHUB_OUTPUT")
            else:
                log("GITHUB_OUTPUT not found; skip writing new_refresh_token")
        except Exception as e:
            log("WARN: failed to write new_refresh_token to GITHUB_OUTPUT:", e)

    # 投稿
    tweet_url = "https://api.twitter.com/2/tweets"
    r2 = s.post(
        tweet_url,
        json={"text": text},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=25,
    )
    if r2.status_code >= 300:
        log("X POST ERROR:", r2.status_code, r2.text[:800])
        r2.raise_for_status()

    return r2.json()

# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def main() -> None:
    # 必須envの存在確認（早期に落として気づく）
    for n in [
        "RAKUTEN_APP_ID",
        "RAKUTEN_AFFILIATE_ID",
        "OPENAI_API_KEY",
        "TW_CLIENT_ID",
        "TW_CLIENT_SECRET",
        "TW_REFRESH_TOKEN",
    ]:
        require_env(n)

    book = fetch_book()
    text = build_post(book)

    log("POST PREVIEW:\n", text)
    res = post_to_x(text)

    if res:
        try:
            log("POSTED:", json.dumps(res.get("data", res), ensure_ascii=False))
        except Exception:
            log("POSTED raw:", res)

if __name__ == "__main__":
    main()
