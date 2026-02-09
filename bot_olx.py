import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple, List

import requests
from bs4 import BeautifulSoup

# ======================================================
# KONFIGURACJA
# ======================================================
SEARCH_URL = os.getenv(
    "OLX_SEARCH_URL",
    "https://www.olx.pl/motoryzacja/samochody/nissan/q-350z/"
)

MAX_PLN = float(os.getenv("MAX_PLN", "46000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_STORE_FILE = "sent_store.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OLX-350Z-Bot/1.3; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

# ======================================================
# REGEXY
# ======================================================
PRICE_RE = re.compile(r"(\d[\d\s\.,]{2,})\s*(zł|pln)", re.IGNORECASE)
OFFER_ID_RE = re.compile(r'"offerId"\s*:\s*(\d+)', re.IGNORECASE)
LAT_RE = re.compile(r'"latitude"\s*:\s*([0-9\.\-]+)')
LON_RE = re.compile(r'"longitude"\s*:\s*([0-9\.\-]+)')

BLACKLIST_RE = re.compile(
    r"uszkodz|rozbit|wypadk|kolizj|na\s*części|dawca|"
    r"do\s*remontu|do\s*naprawy|korozj|rdza|zajechan|katowan|torow|panewk",
    re.IGNORECASE
)

# ======================================================
# MODELE
# ======================================================
@dataclass
class OfferStub:
    url: str


@dataclass
class Offer:
    title: str
    price_pln: Optional[float]
    location: str
    latitude: Optional[float]
    longitude: Optional[float]
    canonical_url: str
    numeric_id: Optional[str]
    signature: str


# ======================================================
# POMOCNICZE
# ======================================================
def canonicalize_url(url: str) -> str:
    u = url.split("#")[0].split("?")[0].rstrip("/")
    return u[:-5] if u.endswith(".html") else u


def sha_sig(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:20]


def make_signature(o: Offer) -> str:
    base = f"{o.title}|{o.price_pln}|{o.location}|{o.canonical_url}"
    return sha_sig(base.lower())


def load_sent() -> Set[str]:
    if not os.path.exists(SENT_STORE_FILE):
        return set()
    with open(SENT_STORE_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_sent(sent: Set[str]):
    with open(SENT_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(sent), f, indent=2, ensure_ascii=False)


# ======================================================
# TELEGRAM
# ======================================================
def telegram_send_message(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20
    ).raise_for_status()


def telegram_send_location(lat: float, lon: float):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendLocation",
        json={"chat_id": TELEGRAM_CHAT_ID, "latitude": lat, "longitude": lon},
        timeout=20
    ).raise_for_status()


def format_message(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{int(o.price_pln):,} PLN".replace(",", " ")
    return (
        f"🚗 {o.title}\n"
        f"💰 Cena: {price}\n"
        f"📍 Lokalizacja: {o.location}\n"
        f"📱 Otwórz w OLX:\n{o.canonical_url}"
    )


# ======================================================
# PARSOWANIE
# ======================================================
def parse_price(text: str) -> Optional[float]:
    m = PRICE_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace(".", "").replace(",", "."))


def extract_location(text: str) -> str:
    for line in text.split("\n"):
        if " - " in line and any(m in line.lower() for m in ["dzisiaj", "wczoraj", "odświeżono"]):
            return line.split(" - ")[0]
    return "—"


def extract_coords(html: str) -> Tuple[Optional[float], Optional[float]]:
    lat = LAT_RE.search(html)
    lon = LON_RE.search(html)
    if lat and lon:
        return float(lat.group(1)), float(lon.group(1))
    return None, None


# ======================================================
# OLX
# ======================================================
def fetch_list() -> List[OfferStub]:
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    seen, stubs = set(), []
    for a in soup.select('a[href*="/d/oferta/"]'):
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else f"https://www.olx.pl{href}"
        canon = canonicalize_url(url)
        if canon in seen:
            continue
        seen.add(canon)
        stubs.append(OfferStub(url=url))
    return stubs


def fetch_details(stub: OfferStub) -> Optional[Offer]:
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    r = requests.get(stub.url, headers=HEADERS, timeout=25)
    if r.status_code >= 400:
        return None

    soup = BeautifulSoup(r.text, "lxml")
    html = r.text
    text = soup.get_text("\n", strip=True)

    title = soup.find("h1").get_text(strip=True)
    price = parse_price(text)
    location = extract_location(text)
    lat, lon = extract_coords(html)

    if BLACKLIST_RE.search(text):
        return None

    if price is None or price > MAX_PLN:
        return None

    offer = Offer(
        title=title,
        price_pln=price,
        location=location,
        latitude=lat,
        longitude=lon,
        canonical_url=canonicalize_url(stub.url),
        numeric_id=OFFER_ID_RE.search(html).group(1) if OFFER_ID_RE.search(html) else None,
        signature=""
    )
    offer.signature = make_signature(offer)
    return offer


# ======================================================
# MAIN
# ======================================================
def main():
    sent = load_sent()
    stubs = fetch_list()

    for stub in stubs[:MAX_DETAIL_PAGES_PER_RUN]:
        offer = fetch_details(stub)
        if not offer:
            continue

        if offer.signature in sent:
            continue

        # 📍 WYŚLIJ MAPKĘ, JEŚLI MAMY GPS
        if offer.latitude is not None and offer.longitude is not None:
            telegram_send_location(offer.latitude, offer.longitude)
            time.sleep(1)

        telegram_send_message(format_message(offer))
        sent.add(offer.signature)
        time.sleep(2)

    save_sent(sent)


if __name__ == "__main__":
    main()
