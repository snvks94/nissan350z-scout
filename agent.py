import requests
import hashlib
import os
import re

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_EUR = 11000

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

BAD_WORDS = [
    "swap", "projekt", "brak", "uszkodz",
    "na części", "bez dokument", "drift"
]

SEEN_FILE = "seen.txt"


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


def is_bad(text):
    t = text.lower()
    return any(w in t for w in BAD_WORDS)


def h(x):
    return hashlib.md5(x.encode()).hexdigest()


seen = load_seen()

# ================= POLAND =================

def olx():
    url = "https://www.olx.pl/auta/q-nissan-350z/rss/"
    r = requests.get(url, headers=HEADERS)
    if "<item>" not in r.text:
        return
    items = r.text.split("<item>")[1:]
    for it in items:
        title = re.search("<title>(.*?)</title>", it).group(1)
        link = re.search("<link>(.*?)</link>", it).group(1)
        if is_bad(title):
            continue
        uid = h(title)
        if uid in seen:
            continue
        seen.add(uid)
        send(f"🚨 OLX (PL)\n{title}\n{link}")


def otomoto():
    url = "https://www.otomoto.pl/osobowe/nissan/350z/"
    r = requests.get(url, headers=HEADERS)
    if "350Z" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"ℹ️ Otomoto (PL)\n{url}")


# ================= GERMANY / EU =================

def mobile_de():
    url = f"https://suchen.mobile.de/fahrzeuge/search.html?dam=0&isSearchRequest=true&makeModelVariant1.makeId=18700&makeModelVariant1.modelId=20&maxPrice={MAX_EUR}"
    r = requests.get(url, headers=HEADERS)
    if "350Z" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"🇩🇪 mobile.de ≤ {MAX_EUR}€\n{url}")


def autoscout():
    url = f"https://www.autoscout24.com/lst/nissan/350-z?price_to={MAX_EUR}"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"🇪🇺 AutoScout24 ≤ {MAX_EUR}€\n{url}")


# ================= CZECH / BALTICS =================

def sauto():
    url = "https://www.sauto.cz/osobni/detail/nissan/350z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"🇨🇿 Sauto.cz\n{url}")


def autoplius():
    url = "https://en.autoplius.lt/ads/cars/nissan/350z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"🇱🇹 Autoplius\n{url}")


def auto24():
    url = "https://www.auto24.ee/kasutatud/nimekiri.php?mark=73&mudel=350Z"
    r = requests.get(url, headers=HEADERS)
    if "350" not in r.text:
        return
    uid = h(r.text[:500])
    if uid in seen:
        return
    seen.add(uid)
    send(f"🇪🇪 auto24.ee\n{url}")


# ================= RUN =================

olx()
otomoto()
mobile_de()
autoscout()
sauto()
autoplius()
auto24()

save_seen(seen)
