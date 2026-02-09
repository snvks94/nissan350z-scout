import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple, List, Any

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
DEBUG_DIR = "debug"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OLX-350Z-Bot/1.4; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

REQUEST_TIMEOUT = 25
MAX_RETRIES = 2

# ======================================================
# REGEXY / FILTRY
# ======================================================
PRICE_ANY_RE = re.compile(r"(\d[\d\s\.,]{2,})\s*(zł|pln)\b", re.IGNORECASE)
PLAIN_NUMBER_RE = re.compile(r"(\d[\d\s\.,]{2,})")
OFFER_ID_RE = re.compile(r'"offerId"\s*:\s*(\d+)', re.IGNORECASE)

# Lokalizacja z mapki
LAT_RE = re.compile(r'"latitude"\s*:\s*([0-9\.\-]+)')
LON_RE = re.compile(r'"longitude"\s*:\s*([0-9\.\-]+)')

BLACKLIST_PATTERNS = [
    r"\buszkodz\w*", r"\brozbit\w*", r"\bwypadk\w*", r"\bpo\s*wypadku\b", r"\bpo\s*kolizji\b",
    r"\bna\s*części\b", r"\bdawca\b", r"\bniesprawn\w*", r"\bdo\s*remontu\b", r"\bdo\s*naprawy\b",
    r"\bwymaga\s*napraw\w*\b", r"\bskrzynia\s*do\s*remontu\b", r"\bsilnik\s*do\s*remontu\b",
    r"\bkorozj\w*", r"\brdza\w*", r"\bzajechan\w*", r"\bkatowan\w*", r"\btorow\w*", r"\bpanewk\w*",
]
BLACKLIST_RE = re.compile("|".join(f"(?:{p})" for p in BLACKLIST_PATTERNS), re.IGNORECASE)

# Negacje: "bez rdzy", "brak korozji" itp.
NEGATION_CLEAN_RE = re.compile(r"\b(bez|brak)\s+(rdz\w*|korozj\w*|uszkodz\w*|wypadk\w*)", re.IGNORECASE)

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
    url: str
    canonical_url: str
    numeric_id: Optional[str]
    signature: str
    url_sig: str


# ======================================================
# HELPERS
# ======================================================
def ensure_debug_dir() -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)


def canonicalize_url(url: str) -> str:
    u = url.split("#")[0].split("?")[0].rstrip("/")
    if u.endswith(".html"):
        u = u[:-5]
    return u


def url_signature(url: str) -> str:
    u = canonicalize_url(url)
    idx = u.find("/d/oferta/")
    return u[idx:] if idx >= 0 else u


def sha_sig(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:20]


def make_signature(title: str, price_pln: Optional[float], location: str, canonical_url: str) -> str:
    t = (title or "").strip().lower()
    loc = (location or "").strip().lower()
    price = "" if price_pln is None else str(int(round(price_pln)))
    base = f"{t}|{price}|{loc}|{canonical_url}"
    return sha_sig(base)


def load_sent_store() -> Dict[str, Set[str]]:
    store: Dict[str, Set[str]] = {"ids": set(), "urls": set(), "url_sigs": set(), "sigs": set()}
    if not os.path.exists(SENT_STORE_FILE):
        return store
    try:
        with open(SENT_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in store.keys():
            if k in data and isinstance(data[k], list):
                store[k] = set(str(x) for x in data[k] if str(x))
    except Exception:
        pass
    return store


def save_sent_store(store: Dict[str, Set[str]]) -> None:
    data = {k: sorted(list(v)) for k, v in store.items()}
    with open(SENT_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_already_sent(store: Dict[str, Set[str]], numeric_id: Optional[str], canonical_url: str, url_sig: str, sig: str) -> bool:
    if numeric_id and numeric_id in store["ids"]:
        return True
    if canonical_url in store["urls"]:
        return True
    if url_sig in store["url_sigs"]:
        return True
    if sig in store["sigs"]:
        return True
    return False


def mark_as_sent(store: Dict[str, Set[str]], numeric_id: Optional[str], canonical_url: str, url_sig: str, sig: str) -> None:
    if numeric_id:
        store["ids"].add(numeric_id)
    store["urls"].add(canonical_url)
    store["url_sigs"].add(url_sig)
    store["sigs"].add(sig)


# ======================================================
# PARSOWANIE CENY
# ======================================================
def to_number_pl(text_num: str) -> Optional[float]:
    if not text_num:
        return None
    t = text_num.strip().replace(" ", "").replace("\u00A0", "")
    t = t.replace(".", "").replace(",", ".")
    t = re.sub(r"[^0-9.]", "", t)
    if not t:
        return None
    try:
        return float(t)
    except Exception:
        return None


def extract_price_from_ldjson(soup: BeautifulSoup) -> Optional[float]:
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for sc in scripts[:20]:
        raw = sc.string
        if not raw:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        candidates: List[Any] = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            offers = obj.get("offers")
            if isinstance(offers, dict):
                p = offers.get("price")
                if isinstance(p, (int, float)):
                    return float(p)
                if isinstance(p, str):
                    n = to_number_pl(p)
                    if n is not None:
                        return n
            p = obj.get("price")
            if isinstance(p, (int, float)):
                return float(p)
            if isinstance(p, str):
                n = to_number_pl(p)
                if n is not None:
                    return n
    return None


def extract_price_from_json_in_html(html: str) -> Optional[float]:
    for rx in [
        r'"price"\s*:\s*\{\s*"value"\s*:\s*([0-9]{3,})',
        r'"price"\s*:\s*([0-9]{3,})',
        r'"amount"\s*:\s*([0-9]{3,})',
    ]:
        m = re.search(rx, html, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def extract_price_from_text(soup: BeautifulSoup) -> Optional[float]:
    text = soup.get_text("\n", strip=True)
    m = PRICE_ANY_RE.search(text)
    if m:
        return to_number_pl(m.group(1))
    for line in text.split("\n"):
        if "zł" in line.lower() or "pln" in line.lower():
            m2 = PLAIN_NUMBER_RE.search(line)
            if m2:
                n = to_number_pl(m2.group(1))
                if n is not None and n >= 1000:
                    return n
    return None


# ======================================================
# LOKALIZACJA + GPS
# ======================================================
def extract_location_text(soup: BeautifulSoup) -> str:
    """
    Fallback tekstowy (miasto - data) + dodatkowo próba znalezienia sekcji "Lokalizacja".
    Nie jest krytyczne — jak nie znajdziemy, wracamy "—".
    """
    text = soup.get_text("\n", strip=True)
    # heurystyka "Miasto - data"
    for line in text.split("\n"):
        if " - " in line:
            low = line.lower()
            if any(k in low for k in [
                "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
                "lipca", "sierpnia", "września", "października", "listopada",
                "grudnia", "dzisiaj", "wczoraj", "odświeżono"
            ]):
                return line.split(" - ")[0].strip()

    # próba: jeżeli w tekście jest nagłówek "Lokalizacja", to następna linia często jest "Miasto, Woj."
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, l in enumerate(lines):
        if l.lower() == "lokalizacja" and i + 1 < len(lines):
            cand = lines[i + 1]
            if 2 < len(cand) < 80:
                return cand

    return "—"


def extract_coords(html: str) -> Tuple[Optional[float], Optional[float]]:
    m1 = LAT_RE.search(html)
    m2 = LON_RE.search(html)
    if m1 and m2:
        try:
            return float(m1.group(1)), float(m2.group(1))
        except Exception:
            return None, None
    return None, None


# ======================================================
# BLACKLIST
# ======================================================
def is_blacklisted(text: str) -> bool:
    if not text:
        return False
    t = NEGATION_CLEAN_RE.sub("", text)
    t = re.sub(r"\bbezwypadk\w*", "", t, flags=re.IGNORECASE)
    return bool(BLACKLIST_RE.search(t))


# ======================================================
# TELEGRAM
# ======================================================
def telegram_send_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def format_msg(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{o.price_pln:,.0f} PLN".replace(",", " ")
    # link mobilny: canonical
    return (
        f"🚗 {o.title}\n"
        f"💰 Cena: {price}\n"
        f"📍 Lokalizacja: {o.location}\n"
        f"📱 Otwórz w OLX:\n{o.canonical_url}"
    )


# ======================================================
# HTTP helpers
# ======================================================
def safe_get(url: str) -> Optional[str]:
    """
    Pobiera URL z retry. Zwraca tekst HTML albo None.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                return None
            return resp.text
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                return None
            time.sleep(1.5 + attempt)


def looks_like_blocked_or_consent(html: str) -> bool:
    """
    Proste heurystyki: czasem OLX pokazuje stronę zgody/captcha.
    """
    low = html.lower()
    return any(x in low for x in [
        "captcha", "verify you are human", "zgadzam się", "cookies", "consent", "przeglądarka"
    ])


# ======================================================
# OLX
# ======================================================
def fetch_list_stubs() -> List[OfferStub]:
    html = safe_get(SEARCH_URL)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    stubs: List[OfferStub] = []
    seen = set()

    for a in soup.select('a[href*="/d/oferta/"]'):
        href = a.get("href") or ""
        if "/d/oferta/" not in href:
            continue
        url = href if href.startswith("http") else f"https://www.olx.pl{href}"
        c = canonicalize_url(url)
        if c in seen:
            continue
        seen.add(c)
        stubs.append(OfferStub(url=url))

    return stubs


def extract_title(soup: BeautifulSoup, html: str) -> Optional[str]:
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t

    # fallback: <title>
    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        if t:
            # często w <title> jest "Nissan 350Z ... - OLX.pl"
            return re.sub(r"\s*-\s*OLX.*$", "", t).strip()

    # fallback: meta og:title
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og.get("content").strip()

    # ostateczny fallback: spróbuj wyciągnąć z JSON-LD (name)
    m = re.search(r'"name"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1).strip()

    return None


def fetch_details(stub: OfferStub) -> Optional[Offer]:
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    html = safe_get(stub.url)
    if not html:
        return None

    # jeśli to strona zgody/antybot — pomijamy (i nie crashujemy)
    if looks_like_blocked_or_consent(html):
        return None

    soup = BeautifulSoup(html, "lxml")

    title = extract_title(soup, html)
    if not title:
        # nie mamy tytułu => strona nie jest ofertą w oczekiwanym formacie
        return None

    # location + GPS
    location = extract_location_text(soup)
    lat, lon = extract_coords(html)

    # cena: JSON-LD -> JSON w HTML -> tekst
    price_pln = extract_price_from_ldjson(soup)
    if price_pln is None:
        price_pln = extract_price_from_json_in_html(html)
    if price_pln is None:
        price_pln = extract_price_from_text(soup)

    # id
    m = OFFER_ID_RE.search(html)
    numeric_id = m.group(1) if m else None

    canonical_url = canonicalize_url(stub.url)
    url_sig = url_signature(stub.url)
    sig = make_signature(title, price_pln, location, canonical_url)

    return Offer(
        title=title,
        price_pln=price_pln,
        location=location,
        latitude=lat,
        longitude=lon,
        url=stub.url,
        canonical_url=canonical_url,
        numeric_id=numeric_id,
        signature=sig,
        url_sig=url_sig,
    )


# ======================================================
# MAIN
# ======================================================
def main():
    store = load_sent_store()
    stubs = fetch_list_stubs()

    reasons = {"already_sent": 0, "blacklisted": 0, "no_price": 0, "over_budget": 0, "sent": 0, "bad_page": 0}
    checked = 0

    for stub in stubs:
        if checked >= MAX_DETAIL_PAGES_PER_RUN:
            break

        offer = fetch_details(stub)
        checked += 1

        if not offer:
            reasons["bad_page"] += 1
            continue

        # dedupe
        if is_already_sent(store, offer.numeric_id, offer.canonical_url, offer.url_sig, offer.signature):
            reasons["already_sent"] += 1
            continue

        # blacklist
        # (łączymy tytuł + resztę tekstu, żeby mieć kontekst)
        if is_blacklisted(offer.title):
            reasons["blacklisted"] += 1
            continue

        if offer.price_pln is None:
            reasons["no_price"] += 1
            continue

        if offer.price_pln > MAX_PLN:
            reasons["over_budget"] += 1
            continue

        telegram_send_message(format_msg(offer))
        mark_as_sent(store, offer.numeric_id, offer.canonical_url, offer.url_sig, offer.signature)
        reasons["sent"] += 1

        time.sleep(1.5)

    save_sent_store(store)
    print(f"limit={MAX_PLN:.0f} PLN | stuby={len(stubs)} | przeczytane={checked} | REASONS={reasons}")


if __name__ == "__main__":
    main()
