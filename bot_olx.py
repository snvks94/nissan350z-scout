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

# -----------------------------
# KONFIGURACJA
# -----------------------------
DEFAULT_SEARCH_URL = "https://www.olx.pl/motoryzacja/samochody/nissan/q-350z/"
SEARCH_URL = os.getenv("OLX_SEARCH_URL", DEFAULT_SEARCH_URL)

# ZMIANA: stały limit w PLN
MAX_PLN = float(os.getenv("MAX_PLN", "46000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Bulletproof store (trzyma tylko wysłane)
SENT_STORE_FILE = "sent_store.json"

DEBUG_DIR = "debug"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OLX-Telegram-Bot/1.1; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

# Ceny w PL: "46 000 zł", "46.000 zł", "46000 zł"
PRICE_ANY_RE = re.compile(r"(\d[\d\s\.\,]{2,})\s*(zł|pln)\b", re.IGNORECASE)
PLAIN_NUMBER_RE = re.compile(r"(\d[\d\s\.\,]{2,})")

# W HTML często można znaleźć offerId
OFFER_ID_RE = re.compile(r'"offerId"\s*:\s*(\d+)', re.IGNORECASE)

# Blacklista: uszkodzone / “zajechane”
BLACKLIST_PATTERNS = [
    r"\buszkodz\w*",
    r"\brozbit\w*",
    r"\bwypadk\w*",
    r"\bpo\s*wypadku\b",
    r"\bpo\s*kolizji\b",
    r"\bna\s*części\b",
    r"\bdawca\b",
    r"\bniesprawn\w*",
    r"\bdo\s*remontu\b",
    r"\bdo\s*naprawy\b",
    r"\bwymaga\s*napraw\w*\b",
    r"\bskrzynia\s*do\s*remontu\b",
    r"\bsilnik\s*do\s*remontu\b",
    r"\bkorozj\w*",
    r"\brdza\w*",
    r"\bzajechan\w*",
    r"\bkatowan\w*",
    r"\btorow\w*",
    r"\bpanewk\w*",
]
BLACKLIST_RE = re.compile("|".join(f"(?:{p})" for p in BLACKLIST_PATTERNS), re.IGNORECASE)

# Negacje, żeby nie wycinać "bez rdzy" itd.
NEGATION_RE = re.compile(r"\b(brak|bez|bezwypadk\w*)\b", re.IGNORECASE)


@dataclass
class OfferStub:
    url: str


@dataclass
class Offer:
    title: str
    price_pln: Optional[float]
    location: str
    url: str
    canonical_url: str
    numeric_id: Optional[str]
    signature: str
    url_sig: str


# -----------------------------
# Helpers: debug / store
# -----------------------------
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


# -----------------------------
# Parsowanie ceny (bardziej odporne)
# -----------------------------
def to_number_pl(text_num: str) -> Optional[float]:
    if not text_num:
        return None
    # usuń spacje, kropki tysięcy, zamień przecinek na kropkę
    t = text_num.strip().replace(" ", "").replace("\u00A0", "")
    # jeżeli format 46.000 -> usuń kropki
    t = t.replace(".", "")
    # jeśli są przecinki jako separator dziesiętny
    t = t.replace(",", ".")
    # zostaw tylko cyfry i kropkę
    t = re.sub(r"[^0-9.]", "", t)
    if not t:
        return None
    try:
        return float(t)
    except Exception:
        return None


def extract_price_from_ldjson(soup: BeautifulSoup) -> Optional[float]:
    """
    Próbuje znaleźć cenę w JSON-LD (application/ld+json),
    np. Offer -> price / priceSpecification -> price
    """
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

        # data może być dict lub list
        candidates: List[Any] = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue

            # Najczęstsze ścieżki
            # 1) obj["offers"]["price"]
            offers = obj.get("offers")
            if isinstance(offers, dict):
                p = offers.get("price")
                if isinstance(p, (int, float)):
                    return float(p)
                if isinstance(p, str):
                    n = to_number_pl(p)
                    if n is not None:
                        return n

                # 1a) priceSpecification
                ps = offers.get("priceSpecification")
                if isinstance(ps, dict):
                    p2 = ps.get("price")
                    if isinstance(p2, (int, float)):
                        return float(p2)
                    if isinstance(p2, str):
                        n = to_number_pl(p2)
                        if n is not None:
                            return n

            # 2) obj["price"]
            p = obj.get("price")
            if isinstance(p, (int, float)):
                return float(p)
            if isinstance(p, str):
                n = to_number_pl(p)
                if n is not None:
                    return n

    return None


def extract_price_from_json_in_html(html: str) -> Optional[float]:
    """
    Fallback: czasem w HTML/JSON jest np. "price": {"value": 46000}
    albo "price":46000
    """
    m = re.search(r'"price"\s*:\s*\{\s*"value"\s*:\s*([0-9]{3,})', html, re.IGNORECASE)
    if m:
        return float(m.group(1))

    m2 = re.search(r'"price"\s*:\s*([0-9]{3,})', html, re.IGNORECASE)
    if m2:
        return float(m2.group(1))

    # czasem: "amount": 46000
    m3 = re.search(r'"amount"\s*:\s*([0-9]{3,})', html, re.IGNORECASE)
    if m3:
        return float(m3.group(1))

    return None


def extract_price_from_text(soup: BeautifulSoup) -> Optional[float]:
    """
    Ostateczny fallback: szuka "46 000 zł" w tekście strony.
    """
    text = soup.get_text("\n", strip=True)
    m = PRICE_ANY_RE.search(text)
    if m:
        return to_number_pl(m.group(1))

    # ekstremalny fallback: weź pierwszą sensowną liczbę > 1000
    for line in text.split("\n"):
        if "zł" in line.lower() or "pln" in line.lower():
            m2 = PLAIN_NUMBER_RE.search(line)
            if m2:
                n = to_number_pl(m2.group(1))
                if n is not None and n >= 1000:
                    return n
    return None


# -----------------------------
# Blacklista z obsługą negacji (prosto, ale skuteczniej)
# -----------------------------
def is_blacklisted(text: str) -> bool:
    if not text:
        return False

    t = text.lower()

    # usuń fragmenty typu "bez rdzy", "brak korozji", "bezwypadkowy"
    # (to ogranicza fałszywe trafienia)
    t = re.sub(r"\b(bez|brak)\s+(rdz\w*|korozj\w*|uszkodz\w*|wypadk\w*)", "", t)
    t = re.sub(r"\bbezwypadk\w*", "", t)

    return bool(BLACKLIST_RE.search(t))


# -----------------------------
# Telegram
# -----------------------------
def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w zmiennych środowiskowych.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def format_msg(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{o.price_pln:,.0f} PLN".replace(",", " ")
    return (
        f"🚗 {o.title}\n"
        f"💰 {price}\n"
        f"📍 {o.location}\n"
        f"🔗 {o.canonical_url}"
    )


# -----------------------------
# OLX fetching
# -----------------------------
def fetch_list_stubs() -> List[OfferStub]:
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=25)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    anchors = soup.select('a[href*="/d/oferta/"]')

    stubs: List[OfferStub] = []
    uniq = set()

    for a in anchors:
        href = a.get("href") or ""
        if "/d/oferta/" not in href:
            continue
        url = href if href.startswith("http") else f"https://www.olx.pl{href}"
        c = canonicalize_url(url)
        if c in uniq:
            continue
        uniq.add(c)
        stubs.append(OfferStub(url=url))

    if not stubs:
        ensure_debug_dir()
        with open(os.path.join(DEBUG_DIR, "list_debug.html"), "w", encoding="utf-8") as f:
            f.write(resp.text)

    return stubs


def extract_numeric_id(html: str) -> Optional[str]:
    m = OFFER_ID_RE.search(html)
    if m:
        return m.group(1)
    return None


def extract_location(text: str) -> str:
    """
    Heurystyka: szuka "Miasto - data"
    """
    for line in text.split("\n"):
        if " - " in line:
            low = line.lower()
            if any(k in low for k in [
                "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
                "lipca", "sierpnia", "września", "października", "listopada",
                "grudnia", "dzisiaj", "wczoraj", "odświeżono"
            ]):
                return line.split(" - ")[0].strip()
    return "—"


def fetch_offer_details(stub: OfferStub) -> Optional[tuple[Offer, str]]:
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    r = requests.get(stub.url, headers=HEADERS, timeout=25)
    if r.status_code >= 400:
        return None

    html = r.text
    soup = BeautifulSoup(html, "lxml")

    canonical_url = canonicalize_url(stub.url)
    u_sig = url_signature(stub.url)

    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "").strip()

    page_text = soup.get_text("\n", strip=True)
    location = extract_location(page_text)

    # Cena: 1) JSON-LD 2) JSON w HTML 3) Tekst
    price_pln = extract_price_from_ldjson(soup)
    if price_pln is None:
        price_pln = extract_price_from_json_in_html(html)
    if price_pln is None:
        price_pln = extract_price_from_text(soup)

    numeric_id = extract_numeric_id(html)

    sig = make_signature(title, price_pln, location, canonical_url)

    # debug jeśli brakuje kluczowych danych
    if not title or price_pln is None:
        ensure_debug_dir()
        with open(os.path.join(DEBUG_DIR, f"offer_debug_{sig}.html"), "w", encoding="utf-8") as f:
            f.write(html)

    offer = Offer(
        title=title or "—",
        price_pln=price_pln,
        location=location or "—",
        url=stub.url,
        canonical_url=canonical_url,
        numeric_id=numeric_id,
        signature=sig,
        url_sig=u_sig,
    )

    combined_for_blacklist = f"{offer.title}\n{page_text}"
    return offer, combined_for_blacklist


# -----------------------------
# MAIN
# -----------------------------
def main():
    store = load_sent_store()
    stubs = fetch_list_stubs()

    reasons = {
        "already_sent": 0,
        "blacklisted": 0,
        "no_price": 0,
        "over_budget": 0,
        "sent": 0,
    }

    checked = 0

    for stub in stubs:
        if checked >= MAX_DETAIL_PAGES_PER_RUN:
            break

        details = fetch_offer_details(stub)
        checked += 1
        if not details:
            continue

        offer, blacklist_text = details

        # Bulletproof dedupe
        if is_already_sent(store, offer.numeric_id, offer.canonical_url, offer.url_sig, offer.signature):
            reasons["already_sent"] += 1
            continue

        # Blacklista
        if is_blacklisted(blacklist_text):
            reasons["blacklisted"] += 1
            continue

        # Cena
        if offer.price_pln is None:
            reasons["no_price"] += 1
            continue

        # Limit
        if offer.price_pln > MAX_PLN:
            reasons["over_budget"] += 1
            continue

        telegram_send(format_msg(offer))
        mark_as_sent(store, offer.numeric_id, offer.canonical_url, offer.url_sig, offer.signature)
        reasons["sent"] += 1

        # mała przerwa między wysyłkami
        time.sleep(1.5)

    save_sent_store(store)
    print(
        f"limit={MAX_PLN:.0f} PLN | stuby={len(stubs)} | przeczytane_oferty={checked} | "
        f"REASONS={reasons} | store_ids={len(store['ids'])} store_urls={len(store['urls'])} store_sigs={len(store['sigs'])}"
    )


if __name__ == "__main__":
    main()
