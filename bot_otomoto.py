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
    # W teorii to powinno zwracać 350Z, ale i tak mamy twardy filtr po tytule/parametrach
    "https://www.otomoto.pl/osobowe/nissan/350z"
)

MAX_PLN = float(os.getenv("MAX_PLN", "46000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_STORE_FILE = "sent_store_otomoto.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OTOMOTO-350Z-Bot/1.1; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

# ======================================================
# FILTRY / REGEXY
# ======================================================
PRICE_RE = re.compile(r"(\d[\d\s\.,]{2,})")
# Twardy filtr: musi wyglądać jak 350Z
MODEL_350Z_RE = re.compile(r"\b350\s*z\b|\b350z\b", re.IGNORECASE)

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
    location: str  # "Miasto, Województwo" lub "Miasto" lub "—"
    canonical_url: str
    signature: str


# ======================================================
# UTIL
# ======================================================
def canonicalize_url(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/")


def sha_sig(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:20]


def make_signature(o: Offer) -> str:
    base = f"{o.title}|{int(o.price_pln) if o.price_pln else ''}|{o.location}|{o.canonical_url}"
    return sha_sig(base.lower())


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


# ======================================================
# STORE (wysłane)
# ======================================================
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
# TELEGRAM (bez mapy)
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


def format_msg(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{o.price_pln:,.0f} PLN".replace(",", " ")
    return (
        f"🚗 {o.title}\n"
        f"💰 Cena: {price}\n"
        f"📍 Lokalizacja: {o.location}\n"
        f"📱 Otwórz w OTOMOTO:\n{o.canonical_url}"
    )


# ======================================================
# JSON helpers (Next.js)
# ======================================================
def find_in_obj(obj: Any, keys: List[str]) -> List[Any]:
    found: List[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                found.append(v)
            found.extend(find_in_obj(v, keys))
    elif isinstance(obj, list):
        for it in obj:
            found.extend(find_in_obj(it, keys))
    return found


def try_get_next_data(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")
    s = soup.find("script", id="__NEXT_DATA__")
    if not s or not s.string:
        return None
    try:
        return json.loads(s.string)
    except Exception:
        return None


# ======================================================
# LISTING -> STUBY
# ======================================================
def extract_stubs_from_next_data(next_data: dict) -> List[OfferStub]:
    candidates = find_in_obj(next_data, keys=["url", "href", "canonicalUrl", "link"])
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

    next_data = try_get_next_data(r.text)
    if next_data:
        stubs = extract_stubs_from_next_data(next_data)
        if stubs:
            return stubs

    return extract_stubs_from_html(r.text)


# ======================================================
# DETAILS -> title / price / location (miasto + województwo)
# ======================================================
def extract_details_from_next_data(next_data: dict) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[str]]:
    # title
    titles = find_in_obj(next_data, keys=["title", "name"])
    title = next((t for t in titles if isinstance(t, str) and len(t) > 3), None)

    # price
    prices = find_in_obj(next_data, keys=["price", "amount", "value"])
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

    # city / voivodeship (województwo)
    city = None
    voiv = None

    # częste klucze
    city_candidates = find_in_obj(next_data, keys=["city", "town"])
    voiv_candidates = find_in_obj(next_data, keys=["region", "voivodeship", "province", "state"])

    city = next((c for c in city_candidates if isinstance(c, str) and 2 < len(c) < 60), None)
    voiv = next((v for v in voiv_candidates if isinstance(v, str) and 2 < len(v) < 60), None)

    return title, price_pln, city, voiv


def fallback_extract_price_from_text(text: str) -> Optional[float]:
    # znajdź linie z walutą i wyciągnij liczbę
    for line in text.split("\n"):
        low = line.lower()
        if "pln" in low or "zł" in low:
            m = PRICE_RE.search(line)
            if m:
                n = to_number_pl(m.group(1))
                if n is not None and n > 1000:
                    return n
    return None


def fallback_extract_location_from_text(text: str) -> str:
    """
    Prostą heurystyką łapiemy linię wyglądającą jak "Miasto, Województwo"
    """
    for line in text.split("\n"):
        l = line.strip()
        if not (3 <= len(l) <= 60):
            continue
        low = l.lower()
        if any(x in low for x in ["pln", "zł", "km", "rok", "vin", "zarejestrowan"]):
            continue
        # jeśli wygląda jak "X, Y" - bierz
        if "," in l and any(ch.isalpha() for ch in l):
            return l
    return "—"


def build_location(city: Optional[str], voiv: Optional[str]) -> str:
    if city and voiv:
        # unikaj "Warszawa, Warszawa" itp.
        if city.strip().lower() == voiv.strip().lower():
            return city.strip()
        return f"{city.strip()}, {voiv.strip()}"
    if city:
        return city.strip()
    if voiv:
        return voiv.strip()
    return "—"


def fetch_offer_details(stub: OfferStub) -> Optional[Offer]:
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    r = requests.get(stub.url, headers=HEADERS, timeout=25)
    if r.status_code >= 400:
        return None

    html = r.text
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)

    next_data = try_get_next_data(html)

    title = None
    price_pln = None
    city = None
    voiv = None

    if next_data:
        title, price_pln, city, voiv = extract_details_from_next_data(next_data)

    # fallback title
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None

    # fallback price
    if price_pln is None:
        price_pln = fallback_extract_price_from_text(text)

    # location
    location = build_location(city, voiv)
    if location == "—":
        location = fallback_extract_location_from_text(text)

    canonical = canonicalize_url(stub.url)

    offer = Offer(
        title=title or "—",
        price_pln=price_pln,
        location=location or "—",
        canonical_url=canonical,
        signature="",
    )

    # --------------------------------------------------
    # TWARDY FILTR: tylko 350Z
    # (Nissan Note / inne modele odpadają tutaj)
    # --------------------------------------------------
    hay = (offer.title or "") + "\n" + text
    if not MODEL_350Z_RE.search(hay):
        return None

    # Filtry jakości
    if BLACKLIST_RE.search(text):
        return None

    # Filtr ceny
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

    stats = {
        "stubs": len(stubs),
        "checked": 0,
        "sent": 0,
        "already_sent": 0,
        "filtered": 0,
    }

    for stub in stubs[:MAX_DETAIL_PAGES_PER_RUN]:
        stats["checked"] += 1

        offer = fetch_offer_details(stub)
        if not offer:
            stats["filtered"] += 1
            continue

        if offer.signature in sent:
            stats["already_sent"] += 1
            continue

        telegram_send_message(format_msg(offer))
        sent.add(offer.signature)
        stats["sent"] += 1

        time.sleep(1.5)

    save_sent(sent)
    print("STATS:", stats)


if __name__ == "__main__":
    main()
