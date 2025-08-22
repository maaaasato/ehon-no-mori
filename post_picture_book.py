# post_picture_book.py
# - 楽天ブックスAPIで絵本を1冊取得
# - OpenAIでX向け紹介文を生成（短文）
# - OAuth2 Refresh -> AccessでXに投稿
# - 返ってきた refresh_token があれば GitHub Actions に渡す（GITHUB_OUTPUT）
#
# 必要な環境変数:
# RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID, OPENAI_API_KEY
# TW_CLIENT_ID, TW_CLIENT_SECRET, TW_REFRESH_TOKEN
#
# 2025-08 版（絵本厳選＋短文化）

from __future__ import annotations

import os
import re
import json
import random
from typing import Dict, Any, List
import requests

# -------- 環境変数 --------
RAKUTEN_APP_ID       = os.getenv("RAKUTEN_APP_ID")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")

TW_CLIENT_ID         = os.getenv("TW_CLIENT_ID")
TW_CLIENT_SECRET     = os.getenv("TW_CLIENT_SECRET")
TW_REFRESH_TOKEN     = os.getenv("TW_REFRESH_TOKEN")

USER_AGENT = "ehon-no-mori-bot/1.0 (+https://github.com/)"

# -------- ユーティリティ --------
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

# -------- 楽天API（絵本のみ取得）--------
# 楽天の絵本ジャンルID：001004001（※以前の 001020004 は別カテゴリなので不可）
GENRE_PICTURE = os.getenv("RAKUTEN_GENRE_PICTURE", "001004001")

# まぎれ込む児童小説や文庫・漫画を弾く
NG_WORDS = [
    "文庫", "新書", "ノベル", "小説", "児童文学",
    "コミック", "漫画", "マンガ", "ライトノベル",
    "ポケット文庫", "ポプラポケット文庫"
]

def is_picture_book(it: Dict[str, Any]) -> bool:
    t = (it.get("title") or "")
    c = (it.get("itemCaption") or "")
    if any(w in t or w in c for w in NG_WORDS):
        return False
    return True

def fetch_book() -> Dict[str, str]:
    """
    楽天ブックスAPIから《絵本ジャンルのみ》で itemCaption 付きのアイテムを1件返す。
    ランダムページ×複数回で偏りを避ける。
    """
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"

    base_params = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json",
        "formatVersion": 2,
        "hits": 30,
        "availability": 1,                  # 在庫あり
        "booksGenreId": GENRE_PICTURE,      # ← 絵本に固定
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount",
        "sort": "reviewCount",              # レビュー数順
    }

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def list_items(page: int) -> List[Dict[str, Any]]:
        r = s.get(URL, params=dict(base_params, page=page), timeout=25)
        if r.status_code != 200:
            log("Rakuten API error:", r.status_code, r.text[:500])
        r.raise_for_status()
        data = r.json()
        items = [it.get("Item") or it for it in data.get("Items", [])]
        # キャプション必須＋NGワード除外
        items = [it for it in items if (it.get("itemCaption") or "").strip()]
        items = [it for it in items if is_picture_book(it)]
        return items

    # ランダムページで8回まで試す
    for _ in range(8):
        page = random.randint(1, 60)
        items = list_items(page)
        if items:
            it = random.choice(items)
            caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
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

# -------- OpenAI で本文生成（短文・リンクが見える長さ）--------
def build_post(book: Dict[str, str]) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    SYSTEM = (
        "あなたは書店員。日本語でX向けの“短文”紹介文を作る。"
        "制約: 本文は必ず140字以内（できれば120字）。文は最大2文。"
        "絵文字は0〜1個、ハッシュタグは最大2つ。"
        "URLは投稿側で最後に別行で付けるため、本文には入れない。"
        "セールス臭は抑え、誠実で温かく。"
        "読点や冗語を削り、体言止めや箇条書き風で簡潔に。"
        "必ず誰向け/どのシーンかを1フレーズ添える。"
        "出力は本文のみ。"
    )
    USER = (
        f"書名:{book['title']}\n"
        f"著者:{book['author']}\n"
        f"紹介文の種:{book['caption']}\n"
        f"平均レビュー:{book['ra']}\n"
        f"レビュー件数:{book['rc']}\n"
        "出力は本文のみ。"
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": USER}],
        temperature=0.7,
        max_tokens=220,
    )

    body = resp.choices[0].message.content.strip()
    body = re.sub(r"\s+", " ", body)

    # ← ここを 140 に変更（リンクが確実に見える長さ）
    MAX_BODY = 140
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1].rstrip() + "…"

    if book["url"]:
        return f"{body}\n{book['url']}"
    return body

# -------- X（Twitter）投稿 --------
def post_to_x(text: str) -> Dict[str, Any] | None:
    import base64

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

    # 新しい refresh_token が返ってきたら GITHUB_OUTPUT へ
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
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        timeout=25,
    )
    if r2.status_code >= 300:
        log("X POST ERROR:", r2.status_code, r2.text[:800])
        r2.raise_for_status()

    return r2.json()

# -------- main --------
def main() -> None:
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
