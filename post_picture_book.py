import os, re, random, requests

RAKUTEN_APP_ID       = os.getenv("RAKUTEN_APP_ID")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")

TW_CLIENT_ID         = os.getenv("TW_CLIENT_ID")
TW_CLIENT_SECRET     = os.getenv("TW_CLIENT_SECRET")
TW_REFRESH_TOKEN     = os.getenv("TW_REFRESH_TOKEN")

def fetch_book():
    import random, re, requests

    URL = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404"

    # まずは“確実に通る”最小構成（keyword ベース）
    base_params = {
        "applicationId": RAKUTEN_APP_ID,
        "affiliateId":   RAKUTEN_AFFILIATE_ID,
        "format": "json",
        "formatVersion": 2,
        "hits": 20,
        "availability": 1,           # 在庫あり
        "sort": "reviewCount",
        "elements": "title,author,itemCaption,affiliateUrl,itemUrl,reviewAverage,reviewCount"
    }
    keywords = ["絵本", "児童書 絵本", "読み聞かせ", "赤ちゃん 絵本", "寝る前 絵本"]

    # 1回目：keyword で叩く
    params = dict(base_params, keyword=random.choice(keywords))
    r = requests.get(URL, params=params, timeout=25)
    if r.status_code != 200:
        print("Rakuten API 1st call failed:", r.status_code, r.text)
    try:
        r.raise_for_status()
        data = r.json()
        items = [it for it in data.get("Items", []) if it.get("itemCaption")]
    except Exception:
        items = []

    # 2回目リトライ：genre 指定（outOfStockFlag は入れない）
    if not items:
        params2 = dict(base_params, booksGenreId="001004001")  # 絵本
        r2 = requests.get(URL, params=params2, timeout=25)
        if r2.status_code != 200:
            print("Rakuten API 2nd call failed:", r2.status_code, r2.text)
        r2.raise_for_status()
        data = r2.json()
        items = [it for it in data.get("Items", []) if it.get("itemCaption")]

    if not items:
        raise RuntimeError("楽天API: itemCaption付きが0件（keyword/genre両方失敗）")

    it = random.choice(items)
    caption = re.sub(r"\s+", " ", (it.get("itemCaption") or "").strip())
    link = it.get("affiliateUrl") or it.get("itemUrl")
    return {
        "title": (it.get("title") or "").strip(),
        "author": (it.get("author") or "").strip(),
        "caption": caption,
        "url": link,
        "ra": it.get("reviewAverage") or "",
        "rc": it.get("reviewCount") or ""
    }
    
def build_post(book):
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    SYSTEM = ("あなたは書店員。日本語でX向け紹介文を作る。本文は230字以内、"
              "絵文字1つまで、ハッシュタグ2つまで。温かく誠実に。"
              "誰向け/どのシーンかを1フレーズ添える。URLは最後に別行で付ける前提。")

    USER = (f"書名:{book['title']}\n著者:{book['author']}\n"
            f"紹介文の種:{book['caption']}\n平均レビュー:{book['ra']}\n"
            f"レビュー件数:{book['rc']}")

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        temperature=0.7,
        max_tokens=220,
    )

    body = resp.choices[0].message.content.strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > 230:
        body = body[:229].rstrip() + "…"
    return f"{body}\n{book['url']}"

def post_to_x(text):
    import base64, requests
    basic = base64.b64encode(f"{TW_CLIENT_ID}:{TW_CLIENT_SECRET}".encode()).decode()

    # refresh -> access ここで scope を必ず付ける！
    r = requests.post(
        "https://api.twitter.com/2/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": TW_REFRESH_TOKEN,
            "client_id": TW_CLIENT_ID,
            # 👇 これを追加
            "scope": "tweet.read tweet.write users.read offline.access",
        },
        timeout=25,
    )
    if r.status_code != 200:
        print("X TOKEN ERROR:", r.status_code, r.text)
        r.raise_for_status()
    access_token = r.json()["access_token"]

    r2 = requests.post(
        "https://api.twitter.com/2/tweets",
        json={"text": text},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=25,
    )
    if r2.status_code >= 300:
        print("X POST ERROR:", r2.status_code, r2.text)
        r2.raise_for_status()
    return r2.json().get("data")



def main():
    b = fetch_book()
    text = build_post(b)
    print("POST PREVIEW:\n", text)
    res = post_to_x(text)
    print("POSTED:", res.get("data"))

if __name__ == "__main__":
    main()
