# post_picture_book.py
# - 絵本ナビ: ランダムIDでページ取得 → 題名抽出（エンコード自動判別）
# - 「その他・一般書」など一般書カテゴリはスキップ
# - 題名で楽天ブックスAPI(絵本ジャンル)を検索 → 見つからなければ楽天側ランダム
# - 本文は <=140字・最大2文、改行の次行にアフィリンク
# - X投稿（refresh_tokenのローテ時はGITHUB_OUTPUTへ new_refresh_token= を出力）
#
# 必須env: RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID, OPENAI_API_KEY,
#          TW_CLIENT_ID, TW_CLIENT_SECRET, TW_REFRESH_TOKEN
# 任意env: EHONNAVI_ID_RANGE="1-400000", EHONNAVI_TRIES="20"
# 依存: requests, beautifulsoup4, lxml, openai

from __future__ import annotations
import os, re, json, random, base64
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

USER_AGENT = "ehon-no-mori-bot/3.1 (+https://github.com/)"
MAX_BODY = 140
GENRE_PICTURE = os.getenv("RAKUTEN_GENRE_PICTURE", "001020004")  # 楽天: 絵本

# 一般書/紛れ込み除外キーワード（Ehonnavi側のページ判定に使用）
EHONNAVI_BLOCK = [
    "その他・一般書", "一般書", "問題集", "資格", "検定", "参考書", "実用書",
    "ライトノベル", "ノベル", "小説", "ビジネス", "自己啓発"
]

# 楽天側の除外（文庫/新書/コミック等）
NG_WORDS_RAKUTEN = [
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

# ------------------------------------------------------------
# 文字化け対策：バイト列を複数エンコーディングでデコード
# ------------------------------------------------------------
def decode_html_bytes(b: bytes, apparent: Optional[str]) -> str:
    tried = []
    # requests の推定を最優先
    if apparent: tried.append(apparent)
    tried += ["utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp", "iso2022_jp"]
    for enc in tried:
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="ignore")

# ------------------------------------------------------------
# 絵本ナビ: タイトル抽出＋カテゴリ判定
# ------------------------------------------------------------
def parse_ehonnavi(content_bytes: bytes, apparent: Optional[str]) -> Optional[Dict[str, str]]:
    html = decode_html_bytes(content_bytes, apparent)
    soup = BeautifulSoup(html, "lxml")

    # 「その他・一般書」などがページ内にあれば即スキップ
    page_text_sample = soup.get_text(" ", strip=True)[:5000]
    if any(w in page_text_sample for w in EHONNAVI_BLOCK):
        return None

    # 題名: og:title → <title> の順で取得
    title = ""
    og = soup.find("meta", {"property": "og:title"})
    if og and og.get("content"): title = og["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title: return None

    # 「｜絵本ナビ」以降を落とし、余計な括弧情報を削る
    title = title.split("｜絵本ナビ", 1)[0].strip()
    title = re.sub(r"[（(].*?[)）]", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    # 題名が極端に短い／サイト名そのものなら捨てる
    if not title or title in ("絵本ナビ",) or len(title) < 2:
        return None

    return {"title": title}

def random_ehonnavi_title(session: requests.Session) -> Optional[Dict[str, str]]:
    rng = os.getenv("EHONNAVI_ID_RANGE", "1-400000")
    try:
        lo, hi = [int(x) for x in rng.split("-")]
    except Exception:
        lo, hi = 1, 400000
    tries = int(os.getenv("EHONNAVI_TRIES", "20"))
    BASE = "https://www.ehonnavi.net/ehon00.asp?no="

    for _ in range(tries):
        no = random.randint(lo, hi)
        url = f"{BASE}{no}"
        try:
            r = session.get(url, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        picked = parse_ehonnavi(r.content, r.apparent_encoding)
        if picked:
            picked["navi_url"] = url
            return picked
    return None

# ------------------------------------------------------------
# 楽天API 検索（絵本ジャンル固定）
# ------------------------------------------------------------
def is_picture_book_rakuten(it: Dict[str, Any]) -> bool:
    t = (it.get("title") or "")
    c = (it.get("itemCaption") or "")
    series = (it.get("seriesName") or "")
    label  = (it.get("label") or "")
    size   = (it.get("size") or "")
    blob = " ".join([t, c, series, label, size])
    return not any(kw in blob for kw in NG_WORDS_RAKUTEN)

def best_match(items: List[Dict[str, Any]], target_title: str) -> Optional[Dict[str, Any]]:
    def score(it):
        t = safe_get(it, "title")
        sim = SequenceMatcher(None, t, target_title).ratio()
        rc = float(it.get("reviewCount") or 0)
        return sim * 0.8 + (min(rc, 1000) / 1000.0) * 0.2
    return sorted(items, key=score, reverse=True)[0] if items else None

def fetch_book() -> Dict[str, str]:
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"

    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json",
        "Accept-Language": "ja,en;q=0.8",
    })

    # 1) 絵本ナビから題名
    picked = random_ehonnavi_title(s)

    # 2) 題名で楽天検索
    def search_rakuten_by_title(title_kw: str) -> List[Dict[str, Any]]:
        base_params = {
            "applicationId": require_env("RAKUTEN_APP_ID"),
            "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            "availability": 1,
            "booksGenreId": GENRE_PICTURE,     # 絵本
            "sort": "reviewCount",             # 有効値（降順）
            "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
        }
        results: List[Dict[str, Any]] = []

        # 厳しめ: title= に当てる
        r = s.get(URL, params=dict(base_params, title=title_kw), timeout=25)
        if r.status_code == 200:
            data = r.json()
            results += [it.get("Item") or it for it in data.get("Items", [])]

        # ゆるめ: keyword= に当てる（表記ゆれ）
        if not results:
            r2 = s.get(URL, params=dict(base_params, keyword=title_kw), timeout=25)
            if r2.status_code == 200:
                data2 = r2.json()
                results += [it.get("Item") or it for it in data2.get("Items", [])]

        # 共通フィルタ
        results = [it for it in results if (it.get("itemCaption") or "").strip()]
        results = [it for it in results if is_picture_book_rakuten(it)]
        return results

    if picked:
        log("EhonNavi picked:", picked["title"], picked["navi_url"])
        items = search_rakuten_by_title(picked["title"])
        if items:
            it = best_match(items, picked["title"])
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

    # 3) フォールバック: 楽天絵本ジャンルからランダム
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
        items = [it for it in items if is_picture_book_rakuten(it)]
        return items

    for _ in range(12):
        page = random.randint(1, 80)
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

# ------------------------------------------------------------
# OpenAI で本文生成（<=140字・最大2文）
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# X投稿（refresh→access）
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# main
# ------------------------------------------------------------
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
