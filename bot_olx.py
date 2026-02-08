import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple, Dict, Any

import requests
from bs4 import BeautifulSoup

DEFAULT_SEARCH_URL = "https://www.olx.pl/motoryzacja/samochody/nissan/q-350z/"
SEARCH_URL = os.getenv("OLX_SEARCH_URL", DEFAULT_SEARCH_URL)

MAX_EUR = 11000.0
NBP_EUR_URL = "https://api.nbp.pl/api/exchangerates/rates/a/eur?format=json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Nowy, “bulletproof” storage
SENT_STORE_FILE = "sent_store.json"

# Stary plik (z poprzedniej wersji) – jeśli istnieje, zmigrujemy
LEGACY_SENT_FILE = "sent.json"

DEBUG_DIR = "debug"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OLX-Telegram-Bot/1.0; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

PRICE_RE = re.compile(r"([\d\s]+)(?:[,.](\d{1,2}))?")
BLACKLIST_PATTERNS = [
    r"\buszkodz\w*", r"\brozbit\w*", r"\bwypadk\w*", r"\bdawca\b",
    r"\bna\s*części\b", r"\bczęści\b", r"\bniesprawn\w*", r"\bspalon\w*",
    r"\bdo\s*remontu\b", r"\bremont\s*silnika\b", r"\bsilnik\s*do\s*remontu\b",
    r"\bskrzynia\s*do\s*remontu\b", r"\bdo\s*naprawy\b", r"\bwymaga\s*napraw\w*\b",
    r"\bkorozj\w*", r"\brdza\w*", r"\bzajechan\w*", r"\bkatowan\w*", r"\btorow\w*",
    r"\bstuk\w*\b", r"\bpanewk\w*", r"\bbrak\s*(przeglądu|oc)\b", r"\bbez\s*(przeglądu|oc)\b",
]
BLACKLIST_RE = re.compile("|".join(f"(?:{p})" for p in BLACKLIST_PATTERNS), re.IGNORECASE)

# Czasem OLX ma w HTML/JSON wartości typu "offerId": 1234567890
OFFER_ID_RE = re.compile(r'"offerId"\s*:\s*(\d+)', re.IGNORECASE)
AD_ID_RE = re.compile(r'"id"\s*:\s*(\d+)', re.IGNORECASE)  # fallback


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


def ensure_debug_dir() -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)


def get_eur_pln_rate() -> float:
    r = requests.get(NBP_EUR_URL, timeout=20)
    r.raise_for_status()
    data = r.json()
    return float(data["rates"][0]["mid"])


def parse_price_pln(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.lower()
    if "zamieni" in t or "za darmo" in t:
        return None

    cleaned = t.replace("zł", "").replace("pln", "").strip()
    m = PRICE_RE.search(cleaned)
    if not m:
        # spróbuj jeszcze prostszy wariant bez groszy
        m2 = re.search(r"([\d\s]+)", cleaned)
        if not m2:
            return None
        try:
            return float(int(m2.group(1).replace(" ", "")))
        except Exception:
            return None

    whole = m.group(1).replace(" ", "")
    frac = m.group(2) or "00"
    try:
        return float(f"{int(whole)}.{int(frac):02d}")
    except Exception:
        return None


def is_blacklisted(text: str) -> bool:
    return bool(text and BLACKLIST_RE.search(text))


def canonicalize_url(url: str) -> str:
    # usuń tracking, hash
    u = url.split("#")[0].split("?")[0].rstrip("/")
    # usuń .html jeśli jest
    if u.endswith(".html"):
        u = u[:-5]
    return u


def url_signature(url: str) -> str:
    """
    Dodatkowy “pół-canonical”: sama ścieżka /d/oferta/... (bez domeny),
    żeby łapać duplikaty gdy domena / subdomena się różni.
    """
    u = canonicalize_url(url)
    # weź ścieżkę od /d/oferta/
    idx = u.find("/d/oferta/")
    if idx >= 0:
        return u[idx:]
    return u


def make_signature(title: str, price_pln: Optional[float], location: str, canonical_url: str) -> str:
    """
    Ostateczny fallback, gdy nie uda się ustalić numeric ID.
    Staramy się, żeby był stabilny mimo drobnych zmian w URL.
    """
    t = (title or "").strip().lower()
    loc = (location or "").strip().lower()
    price = "" if price_pln is None else f"{int(round(price_pln))}"
    base = f"{t}|{price}|{loc}|{canonical_url}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


def load_sent_store() -> Dict[str, Set[str]]:
    """
    Zwraca słownik z trzema setami:
      - ids: numeric IDs z OLX (string)
      - urls: canonical urls
      - sigs: fallback signatures
      - url_sigs: path signatures (opcjonalnie)
    """
    store = {"ids": set(), "urls": set(), "sigs": set(), "url_sigs": set()}

    # Migracja ze starego sent.json (lista offer_id lub podobna)
    if os.path.exists(LEGACY_SENT_FILE) and not os.path.exists(SENT_STORE_FILE):
        try:
            with open(LEGACY_SENT_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            # legacy mogło być listą “offer_id” (często z URL) – wrzucamy do sigs jako “legacy”
            for item in legacy:
                if isinstance(item, str) and item:
                    store["sigs"].add(f"legacy:{item}")
            save_sent_store(store)
        except Exception:
            pass

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


def is_already_sent(store: Dict[str, Set[str]], numeric_id: Optional[str], canonical_url: str, sig: str, u_sig: str) -> bool:
    if numeric_id and numeric_id in store["ids"]:
        return True
    if canonical_url in store["urls"]:
        return True
    if u_sig in store["url_sigs"]:
        return True
    if sig in store["sigs"]:
        return True
    return False


def mark_as_sent(store: Dict[str, Set[str]], numeric_id: Optional[str], canonical_url: str, sig: str, u_sig: str) -> None:
    if numeric_id:
        store["ids"].add(numeric_id)
    store["urls"].add(canonical_url)
    store["url_sigs"].add(u_sig)
    store["sigs"].add(sig)


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

    # fallback: czasem id jest w JSON-LD lub innej strukturze
    # (nie zawsze będzie trafne, ale jako alternatywa)
    m2 = AD_ID_RE.search(html)
    if m2:
        return m2.group(1)

    return None


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

    # Cena: znajdź pierwszą sensowną
    price_pln = None
    price_candidates = soup.find_all(string=re.compile(r"\bzł\b", re.IGNORECASE))
    for s in price_candidates[:50]:
        p = parse_price_pln(str(s))
        if p is not None:
            price_pln = p
            break

    # Lokalizacja: heurystyka “Miasto - data”
    location = "—"
    page_text = soup.get_text("\n", strip=True)
    for line in page_text.split("\n"):
        if " - " in line and any(m in line.lower() for m in [
            "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
            "lipca", "sierpnia", "września", "października", "listopada",
            "grudnia", "dzisiaj", "wczoraj", "odświeżono"
        ]):
            location = line.split(" - ")[0].strip()
            break

    numeric_id = extract_numeric_id(html)
    sig = make_signature(title, price_pln, location, canonical_url)

    if not title:
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
    )

    combined_for_blacklist = f"{offer.title}\n{page_text}"
    return offer, combined_for_blacklist


def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w zmiennych środowiskowych.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    rr = requests.post(url, json=payload, timeout=20)
    rr.raise_for_status()


def format_msg(o: Offer) -> str:
    price = "—" if o.price_pln is None else f"{o.price_pln:,.0f} PLN".replace(",", " ")
    return (
        f"🚗 {o.title}\n"
        f"💰 {price}\n"
        f"📍 {o.location}\n"
        f"🔗 {o.canonical_url}"
    )


def main():
    eur_rate = get_eur_pln_rate()
    max_pln = MAX_EUR * eur_rate

    store = load_sent_store()
    stubs = fetch_list_stubs()

    checked = 0
    sent_now = 0

    for stub in stubs:
        if checked >= MAX_DETAIL_PAGES_PER_RUN:
            break

        details = fetch_offer_details(stub)
        checked += 1
        if not details:
            continue

        offer, blacklist_text = details
        u_sig = url_signature(offer.url)

        # Dedupe “bulletproof”
        if is_already_sent(store, offer.numeric_id, offer.canonical_url, offer.signature, u_sig):
            continue

        # Filtry jakości / stanu
        if is_blacklisted(blacklist_text):
            continue

        # Filtr ceny
        if offer.price_pln is None:
            continue
        if offer.price_pln > max_pln:
            continue

        # Wysyłka -> dopiero wtedy zapis do store
        telegram_send(format_msg(offer))
        mark_as_sent(store, offer.numeric_id, offer.canonical_url, offer.signature, u_sig)
        sent_now += 1

        time.sleep(1.5)

    save_sent_store(store)
    print(
        f"EUR/PLN={eur_rate:.4f} | limit={max_pln:.2f} PLN | "
        f"stuby={len(stubs)} | przeczytane_oferty={checked} | wysłano={sent_now} | "
        f"ids={len(store['ids'])} urls={len(store['urls'])} sigs={len(store['sigs'])}"
    )


if __name__ == "__main__":
    main()
