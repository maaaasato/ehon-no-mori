from __future__ import annotations
import os, re, json, random, base64
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

USER_AGENT = "ehon-no-mori-bot/3.2-debug (+https://github.com/)"
MAX_BODY = 140
GENRE_PICTURE = os.getenv("RAKUTEN_GENRE_PICTURE", "001020004")  # 楽天: 絵本
DEBUG = os.getenv("DEBUG", "1") != "0"

EHONNAVI_BLOCK = [
    "その他・一般書", "一般書", "問題集", "資格", "検定", "参考書", "実用書",
    "ライトノベル", "ノベル", "小説", "ビジネス", "自己啓発",
]

NG_WORDS_RAKUTEN = [
    "文庫", "新書", "児童文学", "小説", "ノベル", "ラノベ",
    "コミック", "漫画", "マンガ", "ムック",
    "青い鳥文庫", "つばさ文庫", "みらい文庫", "ポケット文庫",
]

def log(*args): print(*args, flush=True)
def require_env(name: str) -> str:
    v = os.getenv(name);  assert v, f"環境変数が未設定です: {name}";  return v
def safe_get(d: Dict[str, Any], key: str, default: str = "") -> str:
    v = d.get(key, default);  return "" if v is None else str(v).strip()

def save_tmp(path: str, content: bytes | str):
    try:
        mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
        with open(path, mode) as f:
            f.write(content)
        if DEBUG: log(f"[DBG] saved: {path}")
    except Exception as e:
        log(f"[WARN] save_tmp failed: {path}: {e}")

def decode_html_bytes(b: bytes, apparent: Optional[str]) -> str:
    tried = []
    if apparent: tried.append(apparent)
    tried += ["utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp", "iso2022_jp"]
    for enc in tried:
        try: return b.decode(enc)
        except Exception: pass
    return b.decode("utf-8", errors="ignore")

# ---------- Ehonnavi ----------
def parse_ehonnavi(content_bytes: bytes, apparent: Optional[str]) -> Optional[Dict[str, str]]:
    html = decode_html_bytes(content_bytes, apparent)
    soup = BeautifulSoup(html, "lxml")

    text = soup.get_text(" ", strip=True)[:4000]
    if any(w in text for w in EHONNAVI_BLOCK):
        if DEBUG: log("[EHONNAVI] blocked by category keyword")
        return None

    title = ""
    og = soup.find("meta", {"property": "og:title"})
    if og and og.get("content"): title = og["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        if DEBUG: log("[EHONNAVI] title not found")
        return None

    title = title.split("｜絵本ナビ", 1)[0].strip()
    title = re.sub(r"[（(].*?[)）]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    if not title or title == "絵本ナビ" or len(title) < 2:
        if DEBUG: log(f"[EHONNAVI] invalid title after cleanup: '{title}'")
        return None
    return {"title": title, "html": html}

def random_ehonnavi_title(session: requests.Session) -> Optional[Dict[str, str]]:
    # 固定IDで検証したい時
    force = os.getenv("EHONNAVI_ID_FORCE")
    if force:
        ids = [int(force)]
    else:
        rng = os.getenv("EHONNAVI_ID_RANGE", "1-400000")
        try: lo, hi = [int(x) for x in rng.split("-")]
        except Exception: lo, hi = 1, 400000
        tries = int(os.getenv("EHONNAVI_TRIES", "20"))
        ids = [random.randint(lo, hi) for _ in range(tries)]

    BASE = "https://www.ehonnavi.net/ehon00.asp?no="
    for no in ids:
        url = f"{BASE}{no}"
        try:
            r = session.get(url, timeout=20)
            if DEBUG: log(f"[EHONNAVI] try {url} -> {r.status_code}, apparent={r.apparent_encoding}")
        except Exception as e:
            if DEBUG: log(f"[EHONNAVI] request error {url}: {e}")
            continue
        if r.status_code != 200:
            continue
        picked = parse_ehonnavi(r.content, r.apparent_encoding)
        if picked:
            if DEBUG: log(f"[EHONNAVI] picked title: {picked['title']}")
            # デバッグ用にHTML保存
            save_tmp("/tmp/ehonnavi_pick.html", picked["html"])
            return {"title": picked["title"], "navi_url": url}
    return None

# ---------- Rakuten ----------
def is_picture_book_rakuten(it: Dict[str, Any]) -> bool:
    blob = " ".join([
        it.get("title") or "",
        it.get("itemCaption") or "",
        it.get("seriesName") or "",
        it.get("label") or "",
        it.get("size") or "",
    ])
    return not any(kw in blob for kw in NG_WORDS_RAKUTEN])

def best_match(items: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
    def score(it):
        sim = SequenceMatcher(None, safe_get(it, "title"), target).ratio()
        rc = float(it.get("reviewCount") or 0)
        return sim * 0.8 + (min(rc, 1000) / 1000.0) * 0.2
    return sorted(items, key=score, reverse=True)[0] if items else None

def fetch_book() -> Dict[str, str]:
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json", "Accept-Language": "ja,en;q=0.8"})

    picked = random_ehonnavi_title(s)

    def search_rakuten_by_title(title_kw: str) -> List[Dict[str, Any]]:
        base = {
            "applicationId": require_env("RAKUTEN_APP_ID"),
            "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
            "format": "json", "formatVersion": 2,
            "hits": 30, "availability": 1,
            "booksGenreId": GENRE_PICTURE,
            "sort": "reviewCount",
            "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
        }
        items_all: List[Dict[str, Any]] = []

        # title=
        p1 = dict(base, title=title_kw)
        r1 = s.get(URL, params=p1, timeout=25)
        if DEBUG: log(f"[RAKUTEN] title search '{title_kw}' -> {r1.status_code}")
        if r1.status_code == 200:
            save_tmp("/tmp/rakuten_search_title.json", r1.text)
            data = r1.json()
            items_all += [it.get("Item") or it for it in data.get("Items", [])]

        # keyword=（表記ゆれ）
        if not items_all:
            p2 = dict(base, keyword=title_kw)
            r2 = s.get(URL, params=p2, timeout=25)
            if DEBUG: log(f"[RAKUTEN] keyword search '{title_kw}' -> {r2.status_code}")
            if r2.status_code == 200:
                save_tmp("/tmp/rakuten_search_keyword.json", r2.text)
                data2 = r2.json()
                items_all += [it.get("Item") or it for it in data2.get("Items", [])]

        raw = len(items_all)
        items_all = [it for it in items_all if (it.get("itemCaption") or "").strip()]
        items_all = [it for it in items_all if is_picture_book_rakuten(it)]
        if DEBUG:
            sample = [safe_get(it, "title") for it in items_all[:5]]
            log(f"[RAKUTEN] hits raw={raw}, after_filter={len(items_all)}; sample={sample}")
        return items_all

    if picked:
        log("EhonNavi picked:", picked["title"], picked["navi_url"])
        items = search_rakuten_by_title(picked["title"])
        if items:
            it = best_match(items, picked["title"])
            caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
            link = (it.get("affiliateUrl") or it.get("itemUrl") or "").strip()
            return {"title": safe_get(it, "title"), "author": safe_get(it, "author"),
                    "caption": caption, "url": link,
                    "ra": safe_get(it, "reviewAverage"), "rc": safe_get(it, "reviewCount")}
        else:
            log("Rakuten search by EhonNavi title failed; will fallback.")

    # Fallback: Rakuten random pages (絵本ジャンル)
    base_fb = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json", "formatVersion": 2,
        "hits": 30, "availability": 1,
        "booksGenreId": GENRE_PICTURE, "sort": "reviewCount",
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size",
    }
    for _ in range(12):
        page = random.randint(1, 80)
        r = s.get(URL, params=dict(base_fb, page=page), timeout=25)
        if DEBUG: log(f"[RAKUTEN] fallback page={page} -> {r.status_code}")
        if r.status_code != 200:
            continue
        data = r.json()
        items = [it.get("Item") or it for it in data.get("Items", [])]
        items = [it for it in items if (it.get("itemCaption") or "").strip()]
        items = [it for it in items if is_picture_book_rakuten(it)]
        if DEBUG: log(f"[RAKUTEN] fallback items after_filter={len(items)}")
        if items:
            it = random.choice(items)
            caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
            link = (it.get("affiliateUrl") or it.get("itemUrl") or "").strip()
            return {"title": safe_get(it, "title"), "author": safe_get(it, "author"),
                    "caption": caption, "url": link,
                    "ra": safe_get(it, "reviewAverage"), "rc": safe_get(it, "reviewCount")}
    raise RuntimeError("楽天API: 絵本が見つかりません（EhonNavi→Rakutenともに失敗）")

# ---------- OpenAI ----------
def build_post(book: Dict[str, str]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    SYSTEM = ("あなたは書店員。日本語でX向けの“短文”紹介文を作る。"
              "制約: 本文は必ず140字以内、文は最大2文。絵文字は0〜1個、ハッシュタグは最大2つ。"
              "セールス臭を抑え、誰向け/どのシーンかを1フレーズ入れる。"
              "URLは本文に入れない。出力は本文のみ。")
    USER = (f"書名:{book['title']}\n著者:{book['author']}\n紹介の種:{book['caption']}\n"
            f"平均レビュー:{book['ra']} / 件数:{book['rc']}\n短く端的に。")
    r = client.chat.completions.create(
        model="gpt-4o-mini", messages=[{"role":"system","content":SYSTEM},{"role":"user","content":USER}],
        temperature=0.7, max_tokens=160,
    )
    body = re.sub(r"\s+", " ", (r.choices[0].message.content or "").strip())
    if len(body) > MAX_BODY: body = body[:MAX_BODY-1].rstrip() + "…"
    return f"{body}\n{book['url']}" if book.get("url") else body

# ---------- X ----------
def post_to_x(text: str) -> Dict[str, Any] | None:
    tw_client_id = require_env("TW_CLIENT_ID"); tw_client_secret = require_env("TW_CLIENT_SECRET")
    tw_refresh_token = require_env("TW_REFRESH_TOKEN")
    basic = base64.b64encode(f"{tw_client_id}:{tw_client_secret}".encode()).decode()
    s = requests.Session(); s.headers.update({"User-Agent": USER_AGENT})
    token_url = "https://api.twitter.com/2/oauth2/token"
    r = s.post(token_url, headers={"Authorization": f"Basic {basic}","Content-Type":"application/x-www-form-urlencoded"},
               data={"grant_type":"refresh_token","refresh_token":tw_refresh_token,"client_id":tw_client_id,
                     "scope":"tweet.read tweet.write users.read offline.access"}, timeout=25)
    if r.status_code != 200: log("X TOKEN ERROR:", r.status_code, r.text[:800]); r.raise_for_status()
    p = r.json(); access_token = p["access_token"]; new_refresh = p.get("refresh_token")
    if new_refresh:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f: print(f"new_refresh_token={new_refresh}", file=f)
            log("new_refresh_token written to GITHUB_OUTPUT")
    r2 = s.post("https://api.twitter.com/2/tweets", json={"text": text},
                headers={"Authorization": f"Bearer {access_token}","Content-Type":"application/json"}, timeout=25)
    if r2.status_code >= 300: log("X POST ERROR:", r2.status_code, r2.text[:800]); r2.raise_for_status()
    return r2.json()

def main():
    for n in ["RAKUTEN_APP_ID","RAKUTEN_AFFILIATE_ID","OPENAI_API_KEY","TW_CLIENT_ID","TW_CLIENT_SECRET","TW_REFRESH_TOKEN"]:
        require_env(n)
    book = fetch_book()
    text = build_post(book)
    log("POST PREVIEW:\n", text)
    res = post_to_x(text)
    if res:
        try: log("POSTED:", json.dumps(res.get("data", res), ensure_ascii=False))
        except Exception: log("POSTED raw:", res)

if __name__ == "__main__": main()
