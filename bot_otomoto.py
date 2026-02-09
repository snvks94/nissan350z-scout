import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# ======================================================
# KONFIGURACJA
# ======================================================
SEARCH_URL = os.getenv(
    "OTOMOTO_SEARCH_URL",
    # Możesz podmienić na własny URL z filtrami (np. tylko Nissan 350Z)
    "https://www.otomoto.pl/osobowe/nissan/350z"
)

MAX_PLN = float(os.getenv("MAX_PLN", "46000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_STORE_FILE = "sent_store_otomoto.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OTOMOTO-350Z-Bot/1.0; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

# ======================================================
# REGEX
# ======================================================
PRICE_RE = re.compile(r"(\d[\d\s\.,]{2,})")
LAT_RE = re.compile(r'"latitude"\s*:\s*([0-9\.\-]+)', re.IGNORECASE)
LON_RE = re.compile(r'"longitude"\s*:\s*([0-9\.\-]+)', re.IGNORECASE)
NEXT_DATA_RE = re.compile(r'__NEXT_DATA__', re.IGNORECASE)

BLACKLIST_RE = re.compile(
    r"uszkodz|rozbit|wypadk|kolizj|po\s*wypadku|po\s*kolizji|"
    r"na\s*części|dawca|niesprawn|do\s*remontu|do\s*naprawy|"
    r"korozj|rdza|zajechan|katowan|torow|panewk",
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
    signature: str


# ======================================================
# URL / SIGNATURE / STORE
# ======================================================
def canonicalize_url(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/")


def sha_sig(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:20]


def make_signature(o: Offer) -> str:
    base = f"{o.title}|{int(o.price_pln) if o.price_pln else ''}|{o.location}|{o.canonical_url}"
    return sha_sig(base.lower())


def load_sent() -> Set[str]:
    if not os.path.exists(SENT_STORE_FILE):
        return set()
    try:
        with open(SENT_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_sent(sent: Set[str]) -> None:
    with open(SENT_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f, ensure_ascii=False, indent=2)


# ======================================================
# TELEGRAM
# ======================================================
def telegram_send_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }, timeout=20)
    r.raise_for_status()


def telegram_send_location(lat: float, lon: float) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendLocation"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "latitude": lat,
        "longitude": lon,
    }, timeout=20)
    r.raise_for_status()


def format_msg(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{o.price_pln:,.0f} PLN".replace(",", " ")
    # Link “mobilny”: zwykły canonical – Telegram na telefonie zwykle otwiera w aplikacji (jeśli jest).
    return (
        f"🚗 {o.title}\n"
        f"💰 Cena: {price}\n"
        f"📍 Lokalizacja: {o.location}\n"
        f"📱 Otwórz w OTOMOTO:\n{o.canonical_url}"
    )


# ======================================================
# PARSOWANIE
# ======================================================
def to_number_pl(s: str) -> Optional[float]:
    if not s:
        return None
    t = s.replace(" ", "").replace("\u00A0", "")
    t = t.replace(".", "").replace(",", ".")
    t = re.sub(r"[^0-9.]", "", t)
    try:
        return float(t)
    except Exception:
        return None


def extract_coords_from_html(html: str) -> Tuple[Optional[float], Optional[float]]:
    m1 = LAT_RE.search(html)
    m2 = LON_RE.search(html)
    if m1 and m2:
        try:
            return float(m1.group(1)), float(m2.group(1))
        except Exception:
            return None, None
    return None, None


def find_in_obj(obj: Any, keys: List[str]) -> List[Any]:
    """Zwraca listę wartości znalezionych pod podanymi kluczami (rekurencyjnie)."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                found.append(v)
            found.extend(find_in_obj(v, keys))
    elif isinstance(obj, list):
        for it in obj:
            found.extend(find_in_obj(it, keys))
    return found


def extract_stubs_from_next_data(html: str) -> List[OfferStub]:
    soup = BeautifulSoup(html, "lxml")
    s = soup.find("script", id="__NEXT_DATA__")
    if not s or not s.string:
        return []

    try:
        data = json.loads(s.string)
    except Exception:
        return []

    # Szukamy URL-i do ofert w różnych możliwych polach (zmienia się między wersjami)
    candidates = find_in_obj(data, keys=["url", "href", "canonicalUrl", "link"])
    stubs: List[OfferStub] = []
    seen = set()

    for c in candidates:
        if not isinstance(c, str):
            continue
        if "/oferta/" not in c and "/offer/" not in c:
            continue
        url = c
        if url.startswith("/"):
            url = "https://www.otomoto.pl" + url
        url = canonicalize_url(url)
        if url in seen:
            continue
        seen.add(url)
        stubs.append(OfferStub(url=url))

    return stubs


def extract_stubs_from_html(html: str) -> List[OfferStub]:
    soup = BeautifulSoup(html, "lxml")
    stubs: List[OfferStub] = []
    seen = set()

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "/oferta/" not in href and "/offer/" not in href:
            continue
        url = href
        if url.startswith("/"):
            url = "https://www.otomoto.pl" + url
        url = canonicalize_url(url)
        if url in seen:
            continue
        seen.add(url)
        stubs.append(OfferStub(url=url))

    return stubs


def fetch_list_stubs() -> List[OfferStub]:
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    html = r.text

    # 1) JSON (bardziej stabilne)
    stubs = extract_stubs_from_next_data(html)
    if stubs:
        return stubs

    # 2) fallback HTML
    return extract_stubs_from_html(html)


def extract_details_from_next_data(html: str) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Próbuje wyciągnąć title/price/location z __NEXT_DATA__ (jeśli jest)."""
    soup = BeautifulSoup(html, "lxml")
    s = soup.find("script", id="__NEXT_DATA__")
    if not s or not s.string:
        return None, None, None

    try:
        data = json.loads(s.string)
    except Exception:
        return None, None, None

    # title
    titles = find_in_obj(data, keys=["title", "name"])
    title = next((t for t in titles if isinstance(t, str) and len(t) > 3), None)

    # price
    prices = find_in_obj(data, keys=["price", "amount", "value"])
    price_pln = None
    for p in prices:
        if isinstance(p, (int, float)) and p > 1000:
            price_pln = float(p)
            break
        if isinstance(p, str):
            n = to_number_pl(p)
            if n is not None and n > 1000:
                price_pln = n
                break

    # location
    locs = find_in_obj(data, keys=["location", "city", "region", "voivodeship"])
    # zlep pierwsze sensowne stringi
    parts = [x for x in locs if isinstance(x, str) and 2 < len(x) < 60]
    location = ", ".join(dict.fromkeys(parts[:2])) if parts else None

    return title, price_pln, location


def fetch_offer_details(stub: OfferStub) -> Optional[Offer]:
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    r = requests.get(stub.url, headers=HEADERS, timeout=25)
    if r.status_code >= 400:
        return None
    html = r.text
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)

    # 1) Spróbuj z JSON
    title, price_pln, location = extract_details_from_next_data(html)

    # 2) Fallback z HTML
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None

    if price_pln is None:
        # spróbuj znaleźć pierwszą sensowną liczbę w pobliżu waluty
        for line in text.split("\n"):
            if any(x in line.lower() for x in ["pln", "zł"]):
                m = PRICE_RE.search(line)
                if m:
                    price_pln = to_number_pl(m.group(1))
                    if price_pln:
                        break

    if not location:
        # często location jest w nagłówku/sekcji szczegółów – fallback: szukamy krótkich linii z miastem/regionem
        for line in text.split("\n"):
            if 3 <= len(line) <= 50 and any(ch.isalpha() for ch in line):
                # heurystyka: sporo ofert ma lokalizację jako osobną linię bez waluty/liczb
                if not any(x in line.lower() for x in ["pln", "zł", "km", "rok", "vin"]):
                    location = line
                    break

    # GPS (jeśli dostępny)
    lat, lon = extract_coords_from_html(html)

    canonical = canonicalize_url(stub.url)

    offer = Offer(
        title=title or "—",
        price_pln=price_pln,
        location=location or "—",
        latitude=lat,
        longitude=lon,
        canonical_url=canonical,
        signature="",  # uzupełnimy po filtrach
    )

    # Filtry (blacklista + limit)
    if BLACKLIST_RE.search(text):
        return None
    if offer.price_pln is None or offer.price_pln > MAX_PLN:
        return None

    offer.signature = make_signature(offer)
    return offer


# ======================================================
# MAIN
# ======================================================
def main():
    sent = load_sent()
    stubs = fetch_list_stubs()

    stats = {"stubs": len(stubs), "checked": 0, "sent": 0, "already_sent": 0, "filtered": 0}

    for stub in stubs[:MAX_DETAIL_PAGES_PER_RUN]:
        stats["checked"] += 1
        offer = fetch_offer_details(stub)
        if not offer:
            stats["filtered"] += 1
            continue

        if offer.signature in sent:
            stats["already_sent"] += 1
            continue

        # najpierw pinezka (jeśli mamy), potem wiadomość
        if offer.latitude is not None and offer.longitude is not None:
            telegram_send_location(offer.latitude, offer.longitude)
            time.sleep(1.0)

        telegram_send_message(format_msg(offer))
        sent.add(offer.signature)
        stats["sent"] += 1

        time.sleep(1.5)

    save_sent(sent)
    print("STATS:", stats)


if __name__ == "__main__":
    main()
