# post_picture_book.py
# - 楽天ブックスAPIで絵本を1冊取得
# - OpenAIでX向け紹介文を生成（230字に整形）
# - OAuth2 Refresh -> AccessでXに投稿
# - 返ってきた refresh_token があれば GitHub Actions に渡す（GITHUB_OUTPUT）
#
# 必要な環境変数:
# RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID, OPENAI_API_KEY
# TW_CLIENT_ID, TW_CLIENT_SECRET, TW_REFRESH_TOKEN
#
# 2025-08 版

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
    v = d.get(key)
    return (v or "").strip()

# -------- 楽天API --------
def fetch_book() -> Dict[str, str]:
    """
    楽天ブックスAPIから itemCaption 付きのアイテムを1件返す。
    1回目: keyword
    2回目: 絵本ジャンル指定
    """
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"

    base_params = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json",
        "formatVersion": 2,
        "hits": 20,
        "availability": 1,                 # 在庫あり
        "sort": "reviewCount",
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount",
    }
    keywords = ["絵本", "児童書 絵本", "読み聞かせ", "赤ちゃん 絵本", "寝る前 絵本"]

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def pick_items(params: Dict[str, Any]) -> List[Dict[str, Any]]:
        r = s.get(URL, params=params, timeout=25)
        if r.status_code != 200:
            log("Rakuten API error:", r.status_code, r.text[:500])
        r.raise_for_status()
        data = r.json()
        items = [it.get("Item") or it for it in data.get("Items", [])]  # v2は {Items:[{Item:{...}}]} のこともある
        return [it for it in items if (it.get("itemCaption") or "").strip()]

    # 1回目: keyword
    items: List[Dict[str, Any]] = []
    try:
        params1 = dict(base_params, keyword=random.choice(keywords))
        items = pick_items(params1)
    except Exception as e:
        log("Rakuten 1st call failed:", e)

    # 2回目: ジャンル（絵本）指定でリトライ
    if not items:
        params2 = dict(base_params, booksGenreId="001004001")
        items = pick_items(params2)

    if not items:
        raise RuntimeError("楽天API: itemCaption付きが0件（keyword/genre両方失敗）")

    it = random.choice(items)
    caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
    link = it.get("affiliateUrl") or it.get("itemUrl")

    return {
        "title":  safe_get(it, "title"),
        "author": safe_get(it, "author"),
        "caption": caption,
        "url": link or "",
        "ra": safe_get(it, "reviewAverage"),
        "rc": safe_get(it, "reviewCount"),
    }

# -------- OpenAI で本文生成 --------
def build_post(book: Dict[str, str]) -> str:
    """
    X向け紹介文を生成（230字以内・絵文字1つ・ハッシュタグ2つまでを“目安”としてプロンプトで誘導）
    """
    from openai import OpenAI

    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    SYSTEM = (
        "あなたは書店員。日本語でX向け紹介文を作る。"
        "本文は230字以内、絵文字は最大1つ、ハッシュタグは最大2つ。"
        "温かく誠実に。誰向け/どのシーンかを1フレーズ添える。"
        "URLは投稿側で最後に別行で付けるため、本文には含めない。"
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
    if len(body) > 230:
        body = body[:229].rstrip() + "…"

    if book["url"]:
        return f"{body}\n{book['url']}"
    return body

# -------- X（Twitter）投稿 --------
def post_to_x(text: str) -> Dict[str, Any] | None:
    """
    refresh_token を使って access_token を取得し投稿。
    返ってきた refresh_token があれば GITHUB_OUTPUT に new_refresh_token= として書き出す。
    """
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
            # refresh でも scope を明示（Xの挙動に合わせる）
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

    # 新しい refresh_token が返ってくる場合がある（ローテーション）
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
    # 入力チェック（早期に気づけるよう最初に検証）
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
        # API 仕様: {"data":{"id":"xxx","text":"..."}} が返る想定
        try:
            log("POSTED:", json.dumps(res.get("data", res), ensure_ascii=False))
        except Exception:
            log("POSTED raw:", res)

if __name__ == "__main__":
    main()
