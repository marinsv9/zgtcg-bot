#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZG TCG Restock Tracker
----------------------
Provjerava HR Pokemon shopove, javlja na Telegram kad je vrijedan
sealed proizvod NA STANJU i ISPOD fer praga (instant marza).

Salje alert SAMO na promjenu (OOS -> stock, ili pad cijene ispod praga),
da te ne spama isto svaki ciklus. Stanje pamti u state.json.

Pokretanje: GitHub Actions svakih 15 min (vidi .github/workflows/check.yml).
Tajne (TG_TOKEN, TG_CHAT) se citaju iz environmenta / GitHub Secrets.
"""

import os
import re
import json
import time
import html
import requests

# ----------------------------------------------------------------------------
# 1) KONFIGURACIJA
# ----------------------------------------------------------------------------

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

STATE_FILE = "state.json"
USER_AGENT = "Mozilla/5.0 (compatible; zgtcg-restock/1.0)"
REQUEST_TIMEOUT = 25

# Kljucne rijeci -> fer ulazni prag (EUR). Ako je proizvod NA STANJU i
# cijena <= prag, okida alert. Pragovi = tvoja instant-marza granica.
# Prazni/spomenuti setovi koji jos nisu izasli (Pitch Black, Anniversary)
# vec su tu pa cim shop listao proizvod, uhvatis ga.
TARGETS = [
    # set kljuc (lowercase, bez dijakritike)        prag EUR   labela
    ("151",                                          92,       "151"),
    ("chaos rising",                                 90,       "Chaos Rising"),
    ("destined rivals",                              72,       "Destined Rivals"),
    ("prismatic",                                    95,       "Prismatic Evolutions"),
    ("surging sparks",                               95,       "Surging Sparks"),
    ("paldean fates",                                65,       "Paldean Fates"),
    ("pitch black",                                  95,       "Pitch Black Night (NOVO)"),
    ("anniversary",                                  95,       "30th Anniversary (NOVO)"),
    ("celebration",                                  95,       "Anniversary/Celebration (NOVO)"),
]

# Tip proizvoda koji nas zanima (boost margina) -> blokiraj sitnice.
# Trazimo ETB / Booster Box / Bundle / Collection. Single packove preskoci.
WANTED_TYPE = re.compile(
    r"(elite trainer|booster box|booster bundle|booster display|"
    r"premium collection|super premium|collection box|ultra premium|upc|case)",
    re.I,
)
# Eksplicitno izbaci jeftine sitnice koje ne zelimo (smanjuje sum)
SKIP_TYPE = re.compile(r"(single|sleeve|deck protector|binder|portfolio|playmat|toploader|dice)", re.I)

# Shopify shopovi -> koriste /products.json (cisti JSON, najpouzdanije)
SHOPIFY_SHOPS = [
    ("Magic Omens",  "https://magicomens.com"),
    ("Origin Cards", "https://origin-cards.com"),
]

# WooCommerce shopovi -> probaj Store API (/wp-json/wc/store/v1/products).
# Ako vrati 404/prazno, shop nije WooCommerce ili je API ugasen -> preskace se.
WOO_SHOPS = [
    ("PokeBros",          "https://pokebros.com.hr"),
    ("Dabas",             "https://dabas.hr"),
    ("Igracke Hrvatska",  "https://igrackehrvatska.com"),
    ("Carta Magica",      "https://cartamagica.hr"),
    ("Pullz",             "https://pullz.shop"),
]


# ----------------------------------------------------------------------------
# 2) POMOCNE FUNKCIJE
# ----------------------------------------------------------------------------

def norm(s):
    """lowercase + makni dijakritiku za pouzdano matchanje."""
    if not s:
        return ""
    s = s.lower()
    repl = {"č": "c", "ć": "c", "ž": "z", "š": "s", "đ": "d"}
    for a, b in repl.items():
        s = s.replace(a, b)
    return s


def match_target(title):
    """Vrati (labela, prag) ako naslov odgovara nekom cilju + zeljenom tipu."""
    t = norm(title)
    if SKIP_TYPE.search(t):
        return None
    if not WANTED_TYPE.search(t):
        return None
    for key, prag, label in TARGETS:
        if key in t:
            return (label, prag)
    return None


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[WARN] TG_TOKEN/TG_CHAT nisu postavljeni - preskacem slanje.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[ERR] Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ERR] Telegram slanje: {e}")


# ----------------------------------------------------------------------------
# 3) SCRAPERI
# ----------------------------------------------------------------------------

def scrape_shopify(name, base):
    """Shopify /products.json -> lista (uid, naslov, cijena, available, url)."""
    out = []
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    page = 1
    while page <= 10:  # do 2500 proizvoda; vise nego dovoljno
        try:
            r = sess.get(f"{base}/products.json?limit=250&page={page}", timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                break
            data = r.json().get("products", [])
        except Exception as e:
            print(f"[ERR] {name} products.json: {e}")
            break
        if not data:
            break
        for p in data:
            title = p.get("title", "")
            handle = p.get("handle", "")
            url = f"{base}/products/{handle}"
            for v in p.get("variants", []):
                try:
                    price = float(v.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                available = bool(v.get("available"))
                uid = f"{name}:{p.get('id')}:{v.get('id')}"
                vtitle = title if v.get("title") in (None, "Default Title") else f"{title} ({v.get('title')})"
                out.append((uid, vtitle, price, available, url))
        page += 1
        time.sleep(1)  # pristojan razmak
    print(f"[OK] {name} (Shopify): {len(out)} varijanti")
    return out


def scrape_woocommerce(name, base):
    """WooCommerce Store API -> lista (uid, naslov, cijena, available, url).
    Tiho preskace ako API ne postoji."""
    out = []
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    page = 1
    found_api = False
    while page <= 10:
        try:
            r = sess.get(
                f"{base}/wp-json/wc/store/v1/products",
                params={"per_page": 100, "page": page, "search": "pokemon"},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        found_api = True
        for p in data:
            title = p.get("name", "")
            url = p.get("permalink", base)
            prices = p.get("prices", {}) or {}
            minor = prices.get("currency_minor_unit", 2)
            try:
                raw = prices.get("price")
                price = float(raw) / (10 ** minor) if raw is not None else 0.0
            except (TypeError, ValueError):
                price = 0.0
            available = p.get("is_in_stock", False)
            uid = f"{name}:{p.get('id')}"
            out.append((uid, title, price, available, url))
        page += 1
        time.sleep(1)
    status = f"{len(out)} proizvoda" if found_api else "Store API nedostupan (preskacem)"
    print(f"[{'OK' if found_api else 'SKIP'}] {name} (Woo): {status}")
    return out


# ----------------------------------------------------------------------------
# 4) GLAVNA PETLJA
# ----------------------------------------------------------------------------

def run():
    state = load_state()
    all_items = []

    for name, base in SHOPIFY_SHOPS:
        all_items += scrape_shopify(name, base)
    for name, base in WOO_SHOPS:
        all_items += scrape_woocommerce(name, base)

    alerts = []
    seen = set()

    for uid, title, price, available, url in all_items:
        seen.add(uid)
        m = match_target(title)
        if not m:
            continue
        label, prag = m

        # "Vrijedi alertati" = na stanju I (cijena<=prag ILI cijena nepoznata=0)
        hit = available and (price == 0 or price <= prag)

        prev = state.get(uid, {})
        prev_hit = prev.get("hit", False)
        prev_price = prev.get("price", 0)

        # Okini ako: prelaz iz "ne-hit" u "hit", ILI je vec hit ali cijena PALA
        price_dropped = hit and prev_hit and price and prev_price and price < prev_price
        if hit and (not prev_hit or price_dropped):
            shop = uid.split(":", 1)[0]
            cijena_txt = f"{price:.2f} EUR" if price else "cijena na stranici"
            alerts.append(
                f"🟢 <b>{html.escape(label)}</b> — NA STANJU\n"
                f"🏪 {html.escape(shop)}\n"
                f"📦 {html.escape(title)}\n"
                f"💶 <b>{cijena_txt}</b> (prag ≤{prag} €)\n"
                f"🔗 {html.escape(url)}"
            )

        state[uid] = {"hit": hit, "price": price, "available": available, "title": title}

    # Ocisti stavke koje vise ne postoje (da state ne raste beskonacno)
    for uid in list(state.keys()):
        if uid not in seen:
            del state[uid]

    save_state(state)

    if alerts:
        header = f"⚡ <b>RESTOCK ALERT</b> ({len(alerts)})\n\n"
        # Telegram limit ~4096 znakova po poruci -> grupiraj po 5
        chunk = []
        for a in alerts:
            chunk.append(a)
            if len(chunk) == 5:
                send_telegram(header + "\n\n".join(chunk))
                chunk = []
        if chunk:
            send_telegram(header + "\n\n".join(chunk))
        print(f"[ALERT] Poslano {len(alerts)} obavijesti.")
    else:
        print("[INFO] Nema novih hitova ovaj ciklus.")


if __name__ == "__main__":
    run()
