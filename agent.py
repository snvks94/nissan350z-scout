import requests
import hashlib
import os
import re

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_EUR = 11000
EUR_TO_PLN = 4.3

HEADERS = {"User-Agent": "Mozilla/5.0"}

BAD_WORDS = [
    "swap", "projekt", "brak", "uszkodz",
    "na części", "bez dokument", "drift"
]

SEEN_FILE = "seen.txt"

# ------------------ Helpers ------------------

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
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": False
    })

def is_bad(title):
    low = title.lower()
    return any(word in low for word in BAD_WORDS)

def hash_id(text):
    return hashlib.md5(text.encode()).hexdigest()

def ocena_oferty(title, price_eur, country):
    score = 0
    if is_bad(title):
        return "⚠️ RYZYKO"
    if price_eur <= MAX_EUR:
        score += 1
    if country == "PL":
        score += 1
    if price_eur <= MAX_EUR * 0.9:
        score += 1
    if score >= 2:
        return "✅ OKAZJA"
    else:
        return "ℹ️ DO SPRAWDZENIA"

# ------------------ Seen ------------------
seen = load_seen()

# ------------------ POLAND ------------------

def olx():
    url = "https://www.olx.pl/auta/q-nissan-350z/rss/"
    r = requests.get(url, headers=HEADERS)
    if "<item>" not in r.text:
        return
    items = r.text.split("<item>")[1:]
    for it in items:
        title = re.search("<title>(.*?)</title>", it).group(1)
        link = re.search("<link>(.*?)</link>", it).group(1)
        price_eur = MAX_EUR  # brak ceny w RSS → używamy MAX_EUR
        country = "🇵🇱"
        uid = hash_id(title + link)
        if uid in seen: continue
        seen.add(uid)
        ocen = ocena_oferty(title, price_eur, "PL")
        send(f"🚨 {title}\n{country} OLX\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {link}")

def otomoto():
    url = "https://www.otomoto.pl/osobowe/nissan/350z/"
    r = requests.get(url, headers=HEADERS)
    if "350Z" not in r.text: return
    price_eur = MAX_EUR
    country = "🇵🇱"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "PL")
    send(f"ℹ️ Nissan 350Z\n{country} Otomoto\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

# ------------------ GERMANY / EU ------------------

def mobile_de():
    url = f"https://suchen.mobile.de/fahrzeuge/search.html?dam=0&isSearchRequest=true&makeModelVariant1.makeId=18700&makeModelVariant1.modelId=20&maxPrice={MAX_EUR}"
    r = requests.get(url, headers=HEADERS)
    if "350Z" not in r.text: return
    price_eur = MAX_EUR
    country = "🇩🇪"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "DE")
    send(f"🇩🇪 Nissan 350Z\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

def autoscout():
    url = f"https://www.autoscout24.com/lst/nissan/350-z?price_to={MAX_EUR}"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text: return
    price_eur = MAX_EUR
    country = "🇪🇺"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "EU")
    send(f"🇪🇺 Nissan 350Z\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

# ------------------ CZECH / BALTICS ------------------

def sauto():
    url = "https://www.sauto.cz/osobni/detail/nissan/350z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text: return
    price_eur = MAX_EUR
    country = "🇨🇿"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "CZ")
    send(f"🇨🇿 Nissan 350Z\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

def autoplius():
    url = "https://en.autoplius.lt/ads/cars/nissan/350z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text: return
    price_eur = MAX_EUR
    country = "🇱🇹"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "LT")
    send(f"🇱🇹 Nissan 350Z\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

def auto24():
    url = "https://www.auto24.ee/kasutatud/nimekiri.php?mark=73&mudel=350Z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text: return
    price_eur = MAX_EUR
    country = "🇪🇪"
    uid = hash_id(r.text[:500])
    if uid in seen: return
    seen.add(uid)
    ocen = ocena_oferty("Nissan 350Z", price_eur, "EE")
    send(f"🇪🇪 Nissan 350Z\nCena: {price_eur} € (~{int(price_eur*EUR_TO_PLN)} zł)\nOcena: {ocen}\nLink: {url}")

# ------------------ RUN ------------------
olx()
otomoto()
mobile_de()
autoscout()
sauto()
autoplius()
auto24()

save_seen(seen)
