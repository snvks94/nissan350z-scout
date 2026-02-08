import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# ---------------------------------
# Konfiguracja
# ---------------------------------
DEFAULT_SEARCH_URL = "https://www.olx.pl/motoryzacja/samochody/nissan/q-350z/"
SEARCH_URL = os.getenv("OLX_SEARCH_URL", DEFAULT_SEARCH_URL)

MAX_EUR = 11000.0
NBP_EUR_URL = "https://api.nbp.pl/api/exchangerates/rates/a/eur?format=json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEEN_FILE = "seen.json"
DEBUG_DIR = "debug"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OLX-Telegram-Bot/1.0; +https://github.com/)",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}

# “Jak człowiek”: opóźnienie przed czytaniem oferty (wejściem w szczegóły)
DETAIL_DELAY_RANGE: Tuple[int, int] = (10, 20)

# Bezpiecznik na czas działania workflow
MAX_DETAIL_PAGES_PER_RUN = int(os.getenv("MAX_DETAIL_PAGES_PER_RUN", "20"))

PRICE_RE = re.compile(r"([\d\s]+)(?:[,.](\d{1,2}))?")

# Blacklista: uszkodzone / do remontu / zajechane / dawca / na części itd.
BLACKLIST_PATTERNS = [
    r"\buszkodz\w*",          # uszkodzony/uszkodzona/uszkodzone
    r"\brozbit\w*",
    r"\bwypadk\w*",           # “po wypadku”, “wypadkowy”
    r"\bdawca\b",
    r"\bna\s*części\b",
    r"\bczęści\b",
    r"\bniesprawn\w*",
    r"\bspalon\w*",
    r"\bdo\s*remontu\b",
    r"\bremont\s*silnika\b",
    r"\bsilnik\s*do\s*remontu\b",
    r"\bskrzynia\s*do\s*remontu\b",
    r"\bdo\s*naprawy\b",
    r"\bwymaga\s*napraw\w*\b",
    r"\bkorozj\w*",           # korozja / skorodowany
    r"\brdza\w*",
    r"\bzajechan\w*",         # “zajechany”
    r"\bkatowan\w*",          # “katowany”
    r"\btorow\w*",            # “torowy” (często po ostrym użytkowaniu)
    r"\bstuk\w*\b",           # “stuki”, “stuka”
    r"\bpanewk\w*",           # panewki
    r"\bbrak\s*(przeglądu|oc)\b",
    r"\bbez\s*(przeglądu|oc)\b",
]

BLACKLIST_RE = re.compile("|".join(f"(?:{p})" for p in BLACKLIST_PATTERNS), re.IGNORECASE)


@dataclass
class OfferStub:
    url: str
    offer_id: str


@dataclass
class Offer:
    title: str
    price_pln: Optional[float]
    location: str
    url: str
    offer_id: str


def ensure_debug_dir() -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)


def load_seen() -> Set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: Set[str]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)


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
        return None

    whole = m.group(1).replace(" ", "")
    frac = m.group(2) or "00"
    try:
        return float(f"{int(whole)}.{int(frac):02d}")
    except Exception:
        return None


def is_blacklisted(text: str) -> bool:
    if not text:
        return False
    return bool(BLACKLIST_RE.search(text))


def normalize_offer_id(url: str) -> str:
    # OLX często ma końcówkę .html i parametry
    u = url.split("?")[0].rstrip("/")
    last = u.split("/")[-1]
    last = last.replace(".html", "")
    return last or u


def fetch_list_stubs() -> List[OfferStub]:
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    html = resp.text

    soup = BeautifulSoup(html, "lxml")

    anchors = soup.select('a[href*="/d/oferta/"]')
    stubs: List[OfferStub] = []
    seen_urls = set()

    for a in anchors:
        href = a.get("href") or ""
        if "/d/oferta/" not in href:
            continue
        url = href if href.startswith("http") else f"https://www.olx.pl{href}"
        url = url.split("#")[0]
        if url in seen_urls:
            continue
        seen_urls.add(url)

        offer_id = normalize_offer_id(url)
        stubs.append(OfferStub(url=url, offer_id=offer_id))

    if not stubs:
        ensure_debug_dir()
        with open(os.path.join(DEBUG_DIR, "list_debug.html"), "w", encoding="utf-8") as f:
            f.write(html)

    return stubs


def fetch_offer_details(stub: OfferStub) -> Optional[Offer]:
    # “Czytanie jak człowiek”
    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))

    r = requests.get(stub.url, headers=HEADERS, timeout=25)
    if r.status_code >= 400:
        return None

    html = r.text
    soup = BeautifulSoup(html, "lxml")

    # Tytuł: zwykle h1
    h1 = soup.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "").strip()

    # Cena: próbujemy znaleźć fragment z “zł”
    price_pln = None
    price_candidates = soup.find_all(string=re.compile(r"\bzł\b", re.IGNORECASE))
    for s in price_candidates[:30]:
        p = parse_price_pln(str(s))
        if p is not None:
            price_pln = p
            break

    # Lokalizacja: szukamy “data - miasto” albo elementów z breadcrumb / address
    location = "—"
    # Heurystyka: często w tekście jest “Miasto - data”
    text = soup.get_text("\n", strip=True)
    for line in text.split("\n"):
        if " - " in line and any(m in line.lower() for m in ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
                                                             "lipca", "sierpnia", "września", "października", "listopada",
                                                             "grudnia", "dzisiaj", "wczoraj", "odświeżono"]):
            location = line.split(" - ")[0].strip()
            break

    # Opis: przydaje się do blacklisty
    # Weźmy duży kawał tekstu (bez JS), wystarczy do filtrów słów kluczowych
    combined_for_blacklist = f"{title}\n{text}"

    # Jeśli tytuł nie istnieje — zapisz debug
    if not title:
        ensure_debug_dir()
        with open(os.path.join(DEBUG_DIR, f"offer_{stub.offer_id}.html"), "w", encoding="utf-8") as f:
            f.write(html)

    return Offer(
        title=title or "—",
        price_pln=price_pln,
        location=location or "—",
        url=stub.url,
        offer_id=stub.offer_id,
    ), combined_for_blacklist


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
    if o.price_pln is None:
        price = "—"
    else:
        # 46 000 zamiast 46,000.00
        price = f"{o.price_pln:,.0f} PLN".replace(",", " ")
    return (
        f"🚗 {o.title}\n"
        f"💰 {price}\n"
        f"📍 {o.location}\n"
        f"🔗 {o.url}"
    )


def main():
    eur_rate = get_eur_pln_rate()
    max_pln = MAX_EUR * eur_rate

    seen = load_seen()
    stubs = fetch_list_stubs()

    sent = 0
    checked_details = 0

    for stub in stubs:
        if stub.offer_id in seen:
            continue

        if checked_details >= MAX_DETAIL_PAGES_PER_RUN:
            break

        result = fetch_offer_details(stub)
        checked_details += 1
        seen.add(stub.offer_id)  # oznaczamy jako “już widziane” niezależnie od wyniku

        if not result:
            continue

        offer, blacklist_text = result

        # Blacklista (tytuł + treść)
        if is_blacklisted(blacklist_text):
            continue

        # Musi mieć cenę i mieścić się w limicie
        if offer.price_pln is None:
            continue

        if offer.price_pln <= max_pln:
            telegram_send(format_msg(offer))
            sent += 1
            # mała przerwa między wysyłkami (nie mylić z czytaniem ofert)
            time.sleep(1.5)

    save_seen(seen)
    print(
        f"EUR/PLN={eur_rate:.4f} | limit={max_pln:.2f} PLN | "
        f"stuby={len(stubs)} | przeczytane_oferty={checked_details} | wysłano={sent}"
    )


if __name__ == "__main__":
    main()
