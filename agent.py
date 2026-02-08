import os
import requests
import hashlib
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_EUR = 11000

SEEN_FILE = "seen.txt"
HEADERS = {"User-Agent": "Mozilla/5.0"}

BAD_WORDS = [
    "swap","projekt","brak","uszkodz","na części","bez dokument","drift"
]

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(f.read().splitlines())
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen))

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg,"disable_web_page_preview": False})

def is_bad(text):
    txt = text.lower()
    return any(w in txt for w in BAD_WORDS)

def hash_id(text):
    return hashlib.md5(text.encode()).hexdigest()

# Pobierz kurs EUR -> PLN z NBP
def get_kurs_eur_pln():
    try:
        r = requests.get("https://api.nbp.pl/api/exchangerates/rates/A/EUR/?format=json")
        j = r.json()
        return float(j["rates"][-1]["mid"])
    except:
        return None

kurs_eur = get_kurs_eur_pln()

# Ocena oferty
def ocena(cena_eur, title):
    if is_bad(title):
        return "⚠️ RYZYKO"
    if cena_eur <= MAX_EUR * 0.9:
        return "✅ OKAZJA"
    if cena_eur <= MAX_EUR:
        return "ℹ️ DO SPRAWDZENIA"
    return "❌ POZA BUDŻETEM"

seen = load_seen()

# ------------------------- AUTO SCOUT24 -------------------------

def parse_autoscout24_listing(listing_url):
    r = requests.get(listing_url, headers=HEADERS)
    page = BeautifulSoup(r.text, "lxml")

    # Tytuł
    title = page.find("h1")
    title = title.get_text(strip=True) if title else "Nissan 350Z"

    # Cena
    price_elem = page.select_one("[data-test='price']")
    price_eur = None
    if price_elem:
        txt = price_elem.get_text().replace("€","").replace(",","").strip()
        try:
            price_eur = float(txt.split()[0])
        except:
            price_eur = None

    # Rocznik
    year = None
    yr_elem = page.select_one("[data-test='first-registration']")
    if yr_elem:
        year = yr_elem.get_text(strip=True)

    # Lokalizacja
    loc_elem = page.select_one("[data-test='seller-location']")
    location = loc_elem.get_text(strip=True) if loc_elem else "?"

    return title, price_eur, year, location

def autoscout24():
    search_url = f"https://www.autoscout24.com/lst/nissan/350-z?price_to={MAX_EUR}"
    r = requests.get(search_url, headers=HEADERS)
    soup = BeautifulSoup(r.text, "lxml")

    # linki do pojedynczych ofert
    for a in soup.select("a[data-test='listing-title']"):
        full_link = "https://www.autoscout24.com" + a.get("href")
        uid = hash_id(full_link)
        if uid in seen: 
            continue
        seen.add(uid)

        title, price_eur, year, location = parse_autoscout24_listing(full_link)
        cena_pln = round(price_eur * kurs_eur) if price_eur and kurs_eur else None
        opinia = ocena(price_eur if price_eur else MAX_EUR, title)

        send(f"🇪🇺 {title}\nRocznik: {year}\nCena: {price_eur} € (~{cena_pln} zł)\nLokalizacja: {location}\nOcena: {opinia}\n{full_link}")

# ------------------------- MOBILE.DE -------------------------

def parse_mobilede_listing(url):
    r = requests.get(url, headers=HEADERS)
    page = BeautifulSoup(r.text, "lxml")

    title = page.select_one("h1")
    title = title.get_text(strip=True) if title else "Nissan 350Z"

    price_e = page.select_one("span[data-testid='price']")
    price_eur = None
    if price_e:
        txt = price_e.get_text().replace("€","").replace(".","").strip()
        try:
            price_eur = float(txt.split()[0])
        except:
            pass

    year = None
    yr = page.select_one("li[data-testid='first-registration']")
    if yr:
        year = yr.get_text(strip=True)

    loc = None
    loc_el = page.select_one("li[data-testid='seller-location']")
    if loc_el:
        loc = loc_el.get_text(strip=True)
    return title, price_eur, year, loc

def mobile_de():
    search_url = f"https://suchen.mobile.de/fahrzeuge/search.html?vc=Car&mk=18700&ms=20&sb=rel&vc=Car&fc=EUR&pr=%3A{MAX_EUR}"
    r = requests.get(search_url, headers=HEADERS)
    soup = BeautifulSoup(r.text, "lxml")

    for a in soup.select("a[href*='/pl/samochod/']"):
        full_link = a["href"]
        if not full_link.startswith("http"):
            full_link = "https://www.mobile.de" + full_link
        uid = hash_id(full_link)
        if uid in seen:
            continue
        seen.add(uid)

        title, price_eur, year, location = parse_mobilede_listing(full_link)
        cena_pln = round(price_eur * kurs_eur) if price_eur and kurs_eur else None
        opinia = ocena(price_eur if price_eur else MAX_EUR, title)

        send(f"🇩🇪 {title}\nRocznik: {year}\nCena: {price_eur} € (~{cena_pln} zł)\nLokalizacja: {location}\nOcena: {opinia}\n{full_link}")

# ------------------------- POLAND via RSS -------------------------

def olx_rss():
    rss = "https://www.olx.pl/auta/q-nissan-350z/rss/"
    r = requests.get(rss, headers=HEADERS)
    soup = BeautifulSoup(r.content, "xml")
    for item in soup.find_all("item"):
        link = item.find("link").text
        uid = hash_id(link)
        if uid in seen:
            continue
        seen.add(uid)

        # nie parsujemy PL HTML, bo dynamiczne → tylko tytuł i link
        title = item.find("title").text
        opinia = ocena(MAX_EUR, title)
        send(f"🇵🇱 {title}\nCena (przybliżona EUR): ≤{MAX_EUR}\nOcena: {opinia}\n{link}")

def otomoto_rss():
    rss = "https://www.otomoto.pl/rss?search%5Bfilter_float_price%3Ato%5D=11000&search%5Bquery%5D=nissan+350z"
    r = requests.get(rss, headers=HEADERS)
    soup = BeautifulSoup(r.content, "xml")
    for item in soup.find_all("item"):
        link = item.find("link").text
        uid = hash_id(link)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.find("title").text
        opinia = ocena(MAX_EUR, title)
        send(f"🇵🇱 {title}\nCena (przybliżona EUR): ≤{MAX_EUR}\nOcena: {opinia}\n{link}")

# ------------------ RUN ------------------

olx_rss()
otomoto_rss()
autoscout24()
mobile_de()

save_seen(seen)
