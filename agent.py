import os
import requests
import hashlib
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_EUR = 11000
SEEN_FILE = "seen.txt"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BAD_WORDS = ["swap","projekt","brak","uszkodz","na części","bez dokument","drift"]

# ---------------- Helpers ----------------

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(f.read().splitlines())
    except:
        return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            f.write("\n".join(seen))
    except Exception as e:
        print(f"Save seen error: {e}")

def safe_send(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN or CHAT_ID not set, skipping Telegram send")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": False},
            timeout=10
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram send failed: {e}")

def hash_id(text):
    return hashlib.md5(text.encode()).hexdigest()

def is_bad(text):
    if not text: return False
    txt = text.lower()
    return any(w in txt for w in BAD_WORDS)

def get_kurs_eur_pln():
    try:
        r = requests.get("https://api.nbp.pl/api/exchangerates/rates/A/EUR/?format=json", timeout=10)
        r.raise_for_status()
        j = r.json()
        rate = float(j.get("rates", [{}])[-1].get("mid", 4.3))
        return rate
    except Exception as e:
        print(f"Failed to fetch EUR->PLN rate, using fallback: {e}")
        return 4.3

kurs_eur = get_kurs_eur_pln()
seen = load_seen()

def ocena(cena_eur, title):
    if not title: title = ""
    if is_bad(title):
        return "⚠️ RYZYKO"
    if cena_eur is None:
        return "❓ NIEZNANA"
    if cena_eur <= MAX_EUR * 0.9:
        return "✅ OKAZJA"
    if cena_eur <= MAX_EUR:
        return "ℹ️ DO SPRAWDZENIA"
    return "❌ POZA BUDŻETEM"

# ---------------- Safe request ----------------
def safe_request(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"Request failed: {url} -> {e}")
        return None

# ---------------- POLAND ----------------
def olx_rss():
    r = safe_request("https://www.olx.pl/auta/q-nissan-350z/rss/")
    if not r: return
    try:
        soup = BeautifulSoup(r.content, "xml")
        for item in soup.find_all("item"):
            link = item.find("link").text if item.find("link") else None
            if not link: continue
            uid = hash_id(link)
            if uid in seen: continue
            seen.add(uid)
            title = item.find("title").text if item.find("title") else "Nissan 350Z"
            opinia = ocena(MAX_EUR, title)
            safe_send(f"🇵🇱 {title}\nCena EUR ≤{MAX_EUR}\nOcena: {opinia}\n{link}")
    except Exception as e:
        print(f"OLX parse error: {e}")

def otomoto_rss():
    r = safe_request("https://www.otomoto.pl/rss?search%5Bfilter_float_price%3Ato%5D=11000&search%5Bquery%5D=nissan+350z")
    if not r: return
    try:
        soup = BeautifulSoup(r.content, "xml")
        for item in soup.find_all("item"):
            link = item.find("link").text if item.find("link") else None
            if not link: continue
            uid = hash_id(link)
            if uid in seen: continue
            seen.add(uid)
            title = item.find("title").text if item.find("title") else "Nissan 350Z"
            opinia = ocena(MAX_EUR, title)
            safe_send(f"🇵🇱 {title}\nCena EUR ≤{MAX_EUR}\nOcena: {opinia}\n{link}")
    except Exception as e:
        print(f"Otomoto parse error: {e}")

# ---------------- AUTOSCOUT24 ----------------
def parse_autoscout24_listing(url):
    r = safe_request(url)
    if not r: return "Nissan 350Z", MAX_EUR, "?", "?"
    try:
        page = BeautifulSoup(r.text, "lxml")
        title = page.find("h1").get_text(strip=True) if page.find("h1") else "Nissan 350Z"
        price_elem = page.select_one("[data-test='price']")
        price_eur = None
        if price_elem:
            try: price_eur = float(price_elem.get_text(strip=True).replace("€","").replace(",","").split()[0])
            except: price_eur = None
        year_elem = page.select_one("[data-test='first-registration']")
        year = year_elem.get_text(strip=True) if year_elem else "?"
        loc_elem = page.select_one("[data-test='seller-location']")
        loc = loc_elem.get_text(strip=True) if loc_elem else "?"
        return title, price_eur, year, loc
    except Exception as e:
        print(f"AutoScout parse error: {e}")
        return "Nissan 350Z", MAX_EUR, "?", "?"

def autoscout24():
    r = safe_request(f"https://www.autoscout24.com/lst/nissan/350-z?price_to={MAX_EUR}")
    if not r: return
    try:
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[data-test='listing-title']"):
            href = a.get("href")
            if not href: continue
            full_link = "https://www.autoscout24.com" + href
            uid = hash_id(full_link)
            if uid in seen: continue
            seen.add(uid)
            title, price_eur, year, loc = parse_autoscout24_listing(full_link)
            cena_pln = round(price_eur * kurs_eur) if price_eur else "?"
            opinia = ocena(price_eur if price_eur else MAX_EUR, title)
            safe_send(f"🇪🇺 {title}\nRocznik: {year}\nCena: {price_eur} € (~{cena_pln} zł)\nLokalizacja: {loc}\nOcena: {opinia}\n{full_link}")
    except Exception as e:
        print(f"AutoScout main error: {e}")

# ---------------- MOBILE.DE ----------------
def parse_mobilede_listing(url):
    r = safe_request(url)
    if not r: return "Nissan 350Z", MAX_EUR, "?", "?"
    try:
        page = BeautifulSoup(r.text, "lxml")
        title = page.select_one("h1").get_text(strip=True) if page.select_one("h1") else "Nissan 350Z"
        price_elem = page.select_one("span[data-testid='price']")
        price_eur = None
        if price_elem:
            try: price_eur = float(price_elem.get_text(strip=True).replace("€","").replace(".","").split()[0])
            except: price_eur = None
        year_elem = page.select_one("li[data-testid='first-registration']")
        year = year_elem.get_text(strip=True) if year_elem else "?"
        loc_elem = page.select_one("li[data-testid='seller-location']")
        loc = loc_elem.get_text(strip=True) if loc_elem else "?"
        return title, price_eur, year, loc
    except Exception as e:
        print(f"Mobile.de parse error: {e}")
        return "Nissan 350Z", MAX_EUR, "?", "?"

def mobile_de():
    r = safe_request(f"https://suchen.mobile.de/fahrzeuge/search.html?vc=Car&mk=18700&ms=20&sb=rel&vc=Car&fc=EUR&pr=%3A{MAX_EUR}")
    if not r: return
    try:
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='/pl/samochod/']"):
            full_link = a.get("href")
            if not full_link.startswith("http"): full_link = "https://www.mobile.de" + full_link
            uid = hash_id(full_link)
            if uid in seen: continue
            seen.add(uid)
            title, price_eur, year, loc = parse_mobilede_listing(full_link)
            cena_pln = round(price_eur * kurs_eur) if price_eur else "?"
            opinia = ocena(price_eur if price_eur else MAX_EUR, title)
            safe_send(f"🇩🇪 {title}\nRocznik: {year}\nCena: {price_eur} € (~{cena_pln} zł)\nLokalizacja: {loc}\nOcena: {opinia}\n{full_link}")
    except Exception as e:
        print(f"Mobile.de main error: {e}")

# ---------------- RUN ----------------
def safe_run(func, name):
    try:
        func()
    except Exception as e:
        print(f"{name} failed: {e}")

def main():
    safe_run(olx_rss, "OLX")
    safe_run(otomoto_rss, "Otomoto")
    safe_run(autoscout24, "AutoScout24")
    safe_run(mobile_de, "Mobile.de")
    save_seen(seen)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected global error: {e}")
