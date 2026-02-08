import os
import hashlib
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MAX_EUR = 11000
seen = set()

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

def get_kurs_eur_pln():
    try:
        r = requests.get("https://api.nbp.pl/api/exchangerates/rates/A/EUR/?format=json", timeout=10)
        r.raise_for_status()
        j = r.json()
        return float(j.get("rates", [{}])[-1].get("mid", 4.3))
    except Exception as e:
        print(f"Failed to fetch EUR->PLN, using fallback: {e}")
        return 4.3

kurs_eur = get_kurs_eur_pln()

BAD_WORDS = ["swap","projekt","brak","uszkodz","na części","bez dokument","drift"]

def ocena(cena_eur, title):
    if not title: title = ""
    if any(w in title.lower() for w in BAD_WORDS):
        return "⚠️ RYZYKO"
    if cena_eur is None:
        return "❓ NIEZNANA"
    if cena_eur <= MAX_EUR * 0.9:
        return "✅ OKAZJA"
    if cena_eur <= MAX_EUR:
        return "ℹ️ DO SPRAWDZENIA"
    return "❌ POZA BUDŻETEM"

def save_seen(seen_set, filename="seen.txt"):
    try:
        with open(filename, "w") as f:
            f.write("\n".join(seen_set))
    except Exception as e:
        print(f"Error saving seen.txt: {e}")

def load_seen(filename="seen.txt"):
    s = set()
    try:
        with open(filename, "r") as f:
            s = set(f.read().splitlines())
    except:
        pass
    return s
