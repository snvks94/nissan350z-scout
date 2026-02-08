from utils import safe_send, seen, hash_id, ocena, MAX_EUR
import requests
from bs4 import BeautifulSoup

def run(seen_set):
    try:
        r = requests.get("https://www.otomoto.pl/rss?search%5Bfilter_float_price%3Ato%5D=11000&search%5Bquery%5D=nissan+350z", timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "xml")
        for item in soup.find_all("item"):
            link = item.find("link").text if item.find("link") else None
            if not link: continue
            uid = hash_id(link)
            if uid in seen_set: continue
            seen_set.add(uid)
            title = item.find("title").text if item.find("title") else "Nissan 350Z"
            opinia = ocena(MAX_EUR, title)
            safe_send(f"🇵🇱 {title}\nCena EUR ≤{MAX_EUR}\nOcena: {opinia}\n{link}")
    except Exception as e:
        print(f"Otomoto error: {e}")
