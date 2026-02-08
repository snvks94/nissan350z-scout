from utils import safe_send, seen, hash_id, ocena, MAX_EUR, kurs_eur
import requests
from bs4 import BeautifulSoup

def parse_listing(url):
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    title = soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else "Nissan 350Z"
    price_elem = soup.select_one("span[data-testid='price']")
    price_eur = None
    if price_elem:
        try:
            price_eur = float(price_elem.get_text(strip=True).replace("€","").replace(".","").split()[0])
        except:
            price_eur = None
    year_elem = soup.select_one("li[data-testid='first-registration']")
    year = year_elem.get_text(strip=True) if year_elem else "?"
    loc_elem = soup.select_one("li[data-testid='seller-location']")
    loc = loc_elem.get_text(strip=True) if loc_elem else "?"
    return title, price_eur, year, loc

def run(seen_set):
    try:
        r = requests.get(f"https://suchen.mobile.de/fahrzeuge/search.html?vc=Car&mk=18700&ms=20&sb=rel&vc=Car&fc=EUR&pr=%3A{MAX_EUR}", timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='/pl/samochod/']"):
            full_link = a.get("href")
            if not full_link.startswith("http"): full_link = "https://www.mobile.de" + full_link
            uid = hash_id(full_link)
            if uid in seen_set: continue
            seen_set.add(uid)
            title, price_eur, year, loc = parse_listing(full_link)
            cena_pln = round(price_eur * kurs_eur) if price_eur else "?"
            opinia = ocena(price_eur if price_eur else MAX_EUR, title)
            safe_send(f"🇩🇪 {title}\nRocznik: {year}\nCena: {price_eur} € (~{cena_pln} zł)\nLokalizacja: {loc}\nOcena: {opinia}\n{full_link}")
    except Exception as e:
        print(f"Mobile.de error: {e}")
