# post_picture_book.py
# - 楽天ブックスAPIで「著者」から取得（タイトル検索なし）
# - 絵本以外を除外
# - 重複なし：過去DEDUP_DAYS日以内に出した(ISBN or タイトル+著者)はスキップ
# - openBD で説明文を補強（ISBNがあるとき）
# - 本文 <=140字・最大2文、改行の次行にアフィリンク
# - X投稿（refresh_tokenローテ時は GITHUB_OUTPUT に new_refresh_token）

from __future__ import annotations
import os, re, json, random, base64, pathlib
from typing import Dict, Any, List
from datetime import datetime, timedelta, timezone
import requests

USER_AGENT = "ehon-no-mori-bot/6.0 (+https://github.com/)"
MAX_BODY = 140

# ========= 絵本の有名著者（必要に応じて増やす） =========
PREFERRED_AUTHORS = [
    # 日本
    "五味太郎","せなけいこ","かがくいひろし","ヨシタケシンスケ","林明子","なかやみわ",
    "佐々木マキ","西巻茅子","長新太","わかやまけん","わたなべしげお","馬場のぼる",
    "いわむらかずお","谷川俊太郎","安野光雅","荒井良二","いもとようこ","田島征三",
    "田中清代","村上康成","三浦太郎","浜田桂子","こいでやすこ","中川李枝子","山脇百合子",
    "中川ひろたか","工藤ノリコ","tupera tupera","スズキコージ","柴田ケイコ","長谷川義史",
    "高畠純","きたやまようこ","きむらゆういち","きくちちき","ミロコマチコ","島田ゆか",
    # 海外
    "エリック・カール","レオ・レオニ","ディック・ブルーナ","モーリス・センダック",
    "ドクター・スース","マーガレット・ワイズ・ブラウン","レイモンド・ブリッグズ",
    "アン・グットマン","ゲオルグ・ハレンスレーベン","オードリー・ウッド","ドン・ウッド",
    "ジュリア・ドナルドソン","アクセル・シェフラー",
]

# 絵本以外を弾く語
NG_WORDS = [
    "文庫","新書","児童文学","小説","ノベル","ラノベ",
    "コミック","漫画","マンガ","ムック",
    "青い鳥文庫","つばさ文庫","みらい文庫","ポケット文庫",
]
OK_HINTS = ["絵本","読み聞かせ","よみきかせ","幼児","赤ちゃん","0歳","1歳","2歳","3歳","4歳","5歳","6歳"]

# 楽天ジャンル（環境変数で上書き可）
GENRE_PICTURE  = os.getenv("RAKUTEN_GENRE_PICTURE", "001020004")  # 絵本
GENRE_CHILDREN = "001004"  # 児童書（著者検索の取りこぼし救済）

# 重複管理
HISTORY_PATH = os.getenv("POST_HISTORY_PATH", "data/posted_history.json")
DEDUP_DAYS = int(os.getenv("DEDUP_DAYS", "120"))  # 3回/日なら90-120推奨

def log(*args): print(*args, flush=True)
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"環境変数が未設定です: {name}")
    return v
def safe_get(d: Dict[str, Any], key: str, default: str = "") -> str:
    v = d.get(key, default)
    return "" if v is None else str(v).strip()

# ---------- 履歴（重複制御） ----------
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()

def load_history() -> List[Dict[str, str]]:
    p = pathlib.Path(HISTORY_PATH)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_history(hist: List[Dict[str, str]]) -> None:
    p = pathlib.Path(HISTORY_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

def is_dup(title: str, author: str, isbn: str, hist: List[Dict[str, str]]) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    key_t, key_a, key_i = _norm(title), _norm(author), _norm(isbn or "")
    for h in hist:
        ts = h.get("ts")
        try:
            ts = datetime.fromisoformat(ts) if ts else cutoff
        except Exception:
            ts = cutoff
        if ts < cutoff:
            continue
        hi = _norm(h.get("isbn",""))
        if key_i and hi and key_i == hi:
            return True
        if _norm(h.get("title","")) == key_t and _norm(h.get("author","")) == key_a:
            return True
    return False

def remember_post(title: str, author: str, url: str, isbn: str) -> None:
    hist = load_history()
    hist.append({
        "title": title, "author": author, "url": url, "isbn": isbn,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # 期限切れ掃除（履歴肥大を防ぐ）
    cutoff2 = datetime.now(timezone.utc) - timedelta(days=max(DEDUP_DAYS, 90))
    cleaned = []
    for h in hist:
        try:
            ts = datetime.fromisoformat(h.get("ts","1970-01-01"))
        except Exception:
            ts = cutoff2
        if ts >= cutoff2:
            cleaned.append(h)
    save_history(cleaned)

# ---------- openBD（ISBNで説明を補強） ----------
def enrich_caption_with_openbd(caption: str, isbn: str) -> str:
    isbn = isbn.replace("-", "").strip()
    if not isbn:
        return caption
    try:
        r = requests.get(f"https://api.openbd.jp/v1/get?isbn={isbn}", timeout=10)
        if r.status_code != 200:
            return caption
        arr = r.json() or []
        if not arr or not arr[0]:
            return caption
        data = arr[0]
        # ONIX: CollateralDetail.TextContent の TextType 03/02/01 を優先
        tc = (data.get("onix", {})
                  .get("CollateralDetail", {})
                  .get("TextContent") or [])
        preferred = {"03","02","01"}
        texts = []
        for t in tc:
            if isinstance(t, dict) and str(t.get("TextType")) in preferred:
                texts.append(t.get("Text") or t.get("text") or "")
        extra = next((x for x in texts if x and x.strip()), "")
        if extra:
            text = re.sub(r"\s+", " ", extra.strip())
            # 既存captionが貧弱なら置換、そこそこあるなら差し替え（シンプルに置換）
            caption = text
        return caption.strip()
    except Exception:
        return caption

# ---------- 楽天API ----------
def is_picture_book(it: Dict[str, Any]) -> bool:
    blob = " ".join([
        it.get("title") or "",
        it.get("itemCaption") or "",
        it.get("seriesName") or "",
        it.get("label") or "",
        it.get("size") or "",
    ])
    if any(kw in blob for kw in NG_WORDS): return False
    if any(h in blob for h in OK_HINTS):   return True
    return True  # 著者ホワイトリスト前提で通す

def rakuten_search_by_author(s: requests.Session, author: str) -> List[Dict[str, Any]]:
    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"
    base = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "affiliateId":   require_env("RAKUTEN_AFFILIATE_ID"),
        "format": "json", "formatVersion": 2,
        "hits": 30, "availability": 1,
        "sort": "reviewCount",
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount,seriesName,label,size,isbn",
    }
    items: List[Dict[str, Any]] = []

    # a) 絵本ジャンル author=
    r1 = s.get(URL, params=dict(base, booksGenreId=GENRE_PICTURE, author=author), timeout=25)
    if r1.status_code == 200:
        items += [it.get("Item") or it for it in r1.json().get("Items", [])]

    # b) 児童書大分類 author=
    if not items:
        r2 = s.get(URL, params=dict(base, booksGenreId=GENRE_CHILDREN, author=author), timeout=25)
        if r2.status_code == 200:
            items += [it.get("Item") or it for it in r2.json().get("Items", [])]

    # c) キーワード author（表記ゆれ救済）
    if not items:
        r3 = s.get(URL, params=dict(base, booksGenreId=GENRE_PICTURE, keyword=author), timeout=25)
        if r3.status_code == 200:
            items += [it.get("Item") or it for it in r3.json().get("Items", [])]
    if not items:
        r4 = s.get(URL, params=dict(base, booksGenreId=GENRE_CHILDREN, keyword=author), timeout=25)
        if r4.status_code == 200:
            items += [it.get("Item") or it for it in r4.json().get("Items", [])]

    # フィルタ＆重複除去
    items = [it for it in items if (it.get("itemCaption") or "").strip()]
    items = [it for it in items if is_picture_book(it)]
    seen, uniq = set(), []
    for it in items:
        key = (safe_get(it,"title"), safe_get(it,"author"))
        if key in seen: continue
        seen.add(key); uniq.append(it)
    return uniq

def fetch_book() -> Dict[str, str]:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    hist = load_history()

    # 著者をシャッフルして、未投稿のものを優先的に選定
    for author in random.sample(PREFERRED_AUTHORS, k=len(PREFERRED_AUTHORS)):
        items = rakuten_search_by_author(s, author)
        if not items:
            continue
        # reviewCount順の先頭から重複を除いて選ぶ
        for it in items:
            title  = safe_get(it, "title")
            a      = safe_get(it, "author")
            isbn   = safe_get(it, "isbn")
            if is_dup(title, a, isbn, hist):
                continue
            caption = re.sub(r"\s+", " ", safe_get(it, "itemCaption"))
            caption = enrich_caption_with_openbd(caption, isbn)
            link = (it.get("affiliateUrl") or it.get("itemUrl") or "").strip()
            return {
                "title":  title,
                "author": a,
                "caption": caption,
                "url": link,
                "ra": safe_get(it, "reviewAverage"),
                "rc": safe_get(it, "reviewCount"),
                "isbn": isbn,
            }

    raise RuntimeError("楽天API: 著者検索で重複回避した結果、候補が尽きました。著者を増やす/DEDUP_DAYSを短くする。")

# ---------- OpenAI（140字・最大2文） ----------
def build_post(book: Dict[str, str]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    SYSTEM = (
        "あなたは書店員。日本語でX向けの“短文”紹介文を作る。"
        "制約: 本文は必ず140字以内、文は最大2文。"
        "絵文字は0〜1個、ハッシュタグは最大2つ。"
        "誠実で温かく。誰向け/どのシーンかを1フレーズ入れる。"
        "URLは本文に入れない。出力は本文のみ。"
    )
    USER = (
        f"書名:{book['title']}\n"
        f"著者:{book['author']}\n"
        f"紹介の種:{book['caption']}\n"
        f"平均レビュー:{book['ra']} / 件数:{book['rc']}\n"
        "条件どおり短く端的に。"
    )

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":SYSTEM},{"role":"user","content":USER}],
        temperature=0.7, max_tokens=160,
    )
    body = re.sub(r"\s+", " ", (r.choices[0].message.content or "").strip())
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY-1].rstrip() + "…"
    return f"{body}\n{book['url']}" if book.get("url") else body

# ---------- X投稿 ----------
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
        headers={"Authorization": f"Basic {basic}", "Content-Type":"application/x-www-form-urlencoded"},
        data={"grant_type":"refresh_token","refresh_token":tw_refresh_token,
              "client_id":tw_client_id,"scope":"tweet.read tweet.write users.read offline.access"},
        timeout=25,
    )
    if r.status_code != 200:
        log("X TOKEN ERROR:", r.status_code, r.text[:800]); r.raise_for_status()
    p = r.json()
    access_token = p["access_token"]
    new_refresh  = p.get("refresh_token")

    if new_refresh:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                print(f"new_refresh_token={new_refresh}", file=f)
            log("new_refresh_token written to GITHUB_OUTPUT")

    r2 = s.post("https://api.twitter.com/2/tweets",
                json={"text": text},
                headers={"Authorization": f"Bearer {access_token}","Content-Type":"application/json"},
                timeout=25)
    if r2.status_code >= 300:
        log("X POST ERROR:", r2.status_code, r2.text[:800]); r2.raise_for_status()
    return r2.json()

def main():
    for n in ["RAKUTEN_APP_ID","RAKUTEN_AFFILIATE_ID","OPENAI_API_KEY",
              "TW_CLIENT_ID","TW_CLIENT_SECRET","TW_REFRESH_TOKEN"]:
        require_env(n)

    book = fetch_book()
    text = build_post(book)

    log("POST PREVIEW:\n", text)
    res = post_to_x(text)
    if res:
        # ツイート成功後に履歴へ記録（失敗時に無駄ブロックしない）
        remember_post(book["title"], book["author"], book["url"], book.get("isbn",""))
        try:
            log("POSTED:", json.dumps(res.get("data", res), ensure_ascii=False))
        except Exception:
            log("POSTED raw:", res)

if __name__ == "__main__":
    main()
