# post_picture_book.py
# - 絵本ナビのランダムページから題名を取得
# - その題名で楽天ブックスAPI(絵本ジャンル)を検索して商品情報を取得
# - OpenAIで短文本文（<=140字・最大2文）を生成
# - Xに投稿（refresh_tokenローテーション時はGITHUB_OUTPUTに出力）
#
# 必須env:
#   RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID
#   OPENAI_API_KEY
#   TW_CLIENT_ID, TW_CLIENT_SECRET, TW_REFRESH_TOKEN
#
# 任意env:
#   EHONNAVI_ID_RANGE: "1-400000" のようにランダムID範囲を指定（不明なら既定のまま）
#   EHONNAVI_TRIES: 何回までランダム探索するか（既定: 15）

from __future__ import annotations
import os, re, json, random, base64
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

USER_AGENT = "ehon-no-mori-bot/3.0 (+https://github.com/)"
MAX_BODY = 140
GENRE_PICTURE = os.getenv("RAKUTEN_GENRE_PICTURE", "001020004")  # 絵本

# 文庫/新書/漫画など除外
NG_WORDS = [
    "文庫", "新書", "児童文学", "小説", "ノベル", "ラノベ",
    "コミック", "漫画", "マンガ", "ムック",
    "青い鳥文庫", "つばさ文庫", "みらい文庫", "ポケット文庫",
]

def log(*args): print(*args, flush=True)

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v: raise RuntimeError(f"環境変数が未設定です: {name}")
    return v

def safe_get(d: Dict[str, Any], key: str, default: str = "") -> str:
    v = d.get(key, default)
    if v is None: return ""
    return str(v).strip()

# ---------------- 絵本ナビから題名を拾う ----------------

def parse_title_from_ehonnavi_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    # 1) og:title があれば最優先
    og = soup.find("meta", {"property": "og:title"})
    title = og.get("content").strip() if og and og.get("content") else ""
    # 2) <title> フォールバック
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        return None
    # "｜絵本ナビ" 以降をカット
    title = title.split("｜絵本ナビ", 1)[0].strip()
    # 余計な空白や【 】（版型など）を軽く掃除
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"[（(].*?[)）]", "", title).strip()
    return title or None

def random_ehonnavi_title(session: requests.Session) -> Optional[Dict[str, str]]:
    rng = os.getenv("EHONNAVI_ID_RANGE", "1-400000")
    try:
        lo, hi = [int(x) for x in rng.split("-")]
    except Exception:
        lo, hi = 1, 400000
    tries = int(os.getenv("EHONNAVI_TRIES", "15"))

    BASE = "https://www.ehonnavi.net/ehon00.asp?no="

    for _ in range(tries):
        no = random.randint(lo, hi)
        url = f"{BASE}{no}"
        try:
            r = session.get(url, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:  # 404/500等はスキップ
            continue
        # 文字コードを推定しておく
        if not r.encoding:
            r.encoding = r.apparent_encoding or "utf-8"
        title = parse_title_from_ehonnavi_html(r.text)
        if title and len(title) >= 2 and title != "絵本ナビ":
            return {"title": title, "navi_url": url}
    return None

# --------------- 楽天APIで題名検索（絵本だけ） ---------------

def is_picture_book(it: Dict[str, Any]) -> bool:
    t = (it.get("title") or "")
    c = (it.get("itemCaption") or "")
    series = (it.get("seriesName") or "")
    label  = (it.get("label") or "")
    size   = (it.get("size") or "")
    blob = " ".join([t, c, series, label, size])
    return not any(kw in blob for kw in NG_WORDS)

def best_match(items: List[Dict[str, Any]], target_title: str) -> Optional[Dict[str, Any]]:
    # 題名の近さ + レビュー数の強さを適当に合成
    def score(it):
        t = safe_get(it, "title")
        sim = SequenceMatcher(None, t, target_title).ratio()
        rc = float(it.get("reviewCount") or 0)
        return sim * 0.8 + (min(rc, 1000) / 1000.0) * 0.2
    if not items: return None
    return sorted(items, key=score, reverse=True)[0]

def fetch_book() -> Dict[str, str]:
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})

    # 1) 絵本ナビから題名を得る（失敗したら後段で楽天ランダムへフォールバック）
    picked = random_ehonnavi_title(s)

    # 2) 楽天で検索
    def search_rakuten_by_title(title_kw: str) -> List[Dict[str, Any]]:
        base_params = {
            "applicationId": require_env("RAKUTEN_APP_ID"),
            "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            "availability": 1,
            "booksGenreId": GENRE_PICTURE,  # 絵本
            "sort": "reviewCount",          # 許可値（降順）
            "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
        }
        items_all: List[Dict[str, Any]] = []

        # a) titleパラメータで厳しめ検索
        params_title = dict(base_params, title=title_kw)
        r = s.get(URL, params=params_title, timeout=25)
        if r.status_code == 200:
            data = r.json()
            items = [it.get("Item") or it for it in data.get("Items", [])]
            items_all.extend(items)

        # b) ダメなら keyword でも検索（表記ゆれ対策）
        if not items_all:
            params_kw = dict(base_params, keyword=title_kw)
            r2 = s.get(URL, params=params_kw, timeout=25)
            if r2.status_code == 200:
                data2 = r2.json()
                items2 = [it.get("Item") or it for it in data2.get("Items", [])]
                items_all.extend(items2)

        # 共通フィルタ
        items_all = [it for it in items_all if (it.get("itemCaption") or "").strip()]
        items_all = [it for it in items_all if is_picture_book(it)]
        return items_all

    if picked:
        title = picked["title"]
        log("EhonNavi picked:", title, picked["navi_url"])
        items = search_rakuten_by_title(title)
        if items:
            it = best_match(items, title)
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
        else:
            log("Rakuten search by EhonNavi title failed; will fallback.")

    # 3) フォールバック：楽天の絵本ジャンルをランダムに探索
    base_params_fb = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json",
        "formatVersion": 2,
        "hits": 30,
        "availability": 1,
        "booksGenreId": GENRE_PICTURE,
        "sort": "reviewCount",
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
    }

    def pick_page(page: int) -> List[Dict[str, Any]]:
        r = s.get(URL, params=dict(base_params_fb, page=page), timeout=25)
        if r.status_code != 200:
            log("Rakuten API error:", r.status_code, r.text[:300])
        r.raise_for_status()
        data = r.json()
        items = [it.get("Item") or it for it in data.get("Items", [])]
        items = [it for it in items if (it.get("itemCaption") or "").strip()]
        items = [it for it in items if is_picture_book(it)]
        return items

    for _ in range(10):
        page = random.randint(1, 60)
        items = pick_page(page)
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

    raise RuntimeError("楽天API: 絵本が見つかりません（EhonNavi→Rakutenともに失敗）")

# ---------------- OpenAI で本文生成 ----------------

def build_post(book: Dict[str, str]) -> str:
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
    return f"{body}\n{book['url']}" if book.get("url") else body

# ---------------- X に投稿 ----------------

def post_to_x(text: str) -> Dict[str, Any] | None:
    tw_client_id     = require_env("TW_CLIENT_ID")
    tw_client_secret = require_env("TW_CLIENT_SECRET")
    tw_refresh_token = require_env("TW_REFRESH_TOKEN")

    basic = base64.b64encode(f"{tw_client_id}:{tw_client_secret}".encode()).decode()
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    token_url = "https://api.twitter.com/2/oauth2/token"
    r = s.post(
        token_url,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": tw_refresh_token,
              "client_id": tw_client_id, "scope": "tweet.read tweet.write users.read offline.access"},
        timeout=25,
    )
    if r.status_code != 200:
        log("X TOKEN ERROR:", r.status_code, r.text[:800]); r.raise_for_status()
    payload = r.json()
    access_token = payload["access_token"]
    new_refresh  = payload.get("refresh_token")

    if new_refresh:
        try:
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a", encoding="utf-8") as f:
                    print(f"new_refresh_token={new_refresh}", file=f)
                log("new_refresh_token written to GITHUB_OUTPUT")
        except Exception as e:
            log("WARN: failed to write new_refresh_token:", e)

    tweet_url = "https://api.twitter.com/2/tweets"
    r2 = s.post(tweet_url, json={"text": text},
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                timeout=25)
    if r2.status_code >= 300:
        log("X POST ERROR:", r2.status_code, r2.text[:800]); r2.raise_for_status()
    return r2.json()

# ---------------- main ----------------

def main() -> None:
    for n in ["RAKUTEN_APP_ID","RAKUTEN_AFFILIATE_ID","OPENAI_API_KEY",
              "TW_CLIENT_ID","TW_CLIENT_SECRET","TW_REFRESH_TOKEN"]:
        require_env(n)

    book = fetch_book()
    text = build_post(book)
    log("POST PREVIEW:\n", text)
    res = post_to_x(text)
    if res:
        try: log("POSTED:", json.dumps(res.get("data", res), ensure_ascii=False))
        except Exception: log("POSTED raw:", res)

if __name__ == "__main__":
    main()
