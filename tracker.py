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

# =====================  LOGIKA HVATANJA DEALOVA  =====================
# Bot vise NE lovi samo imenovane setove. Sada hvata SVAKI vrijedan
# proizvod (ETB / Box / UPC / Collection) ispod praga za svoj TIP.
# Tri sloja:
#   1) HOT_SETS        -> rijetki/OOP/blue-chip setovi: visi prag (vise vrijede)
#   2) TYPE_THRESHOLDS -> genericki prag po TIPU proizvoda (hvata sve ostalo)
#   3) PRIORITY_SETS   -> samo za "PRIORITET" oznaku u poruci

# Setovi koji su OOP/blue-chip/hype -> vrijede vise, dopusti visi prag.
# (kljuc lowercase bez dijakritike  ->  prag EUR za taj set)
HOT_SETS = {
    # === ENGLESKI sealed. Prag = realna DONJA granica trzista (Cardmarket).
    # Bot javlja samo ako je cijena <= prag (tj. potencijalno ISPOD trzista).
    # Brojke su konzervativne; UVIJEK provjeri Cardmarket link u poruci. ===
    # OOP / rastuci (Sword & Shield + rani S&V) - sealed booster box ako nije naznaceno
    "lost origin": 150, "silver tempest": 150, "crown zenith": 95,
    "crow zenith": 95,
    "obsidian flames": 95, "paradox rift": 110, "paldea evolved": 110,
    "astral radiance": 150, "brilliant stars": 150, "evolving skies": 280,
    "fusion strike": 130, "celebrations": 70, "hidden fates": 95,
    "champions path": 70, "shining fates": 110,
    # S&V blue-chip / shiny / hype
    "151": 60, "prismatic": 90, "surging sparks": 75, "paldean fates": 55,
    "destined rivals": 65, "chaos rising": 80, "twilight masquerade": 70,
    "shrouded fable": 65, "stellar crown": 70, "journey together": 80,
    "temporal forces": 70,
    # buduci/novi - oprez, reprint rizik; nizak prag da ne placas hype
    "pitch black": 80, "anniversary": 90, "perfect order": 65,
    "phantasmal flames": 75, "mega zygarde": 40, "mega dream": 90,
}

# Genericki prag po TIPU (za setove KOJE NISMO imenovali = nepoznato/rizicno).
# Drzimo NISKO jer ne znamo trzisnu cijenu -> javi samo ako je jako jeftino.
TYPE_THRESHOLDS = [
    (re.compile(r"ultra.?premium|\bupc\b", re.I), 110),
    (re.compile(r"super.?premium", re.I), 70),
    (re.compile(r"booster box|booster display", re.I), 90),
    (re.compile(r"elite trainer|\betb\b", re.I), 55),
    (re.compile(r"premium collection|collection box|\bcase\b", re.I), 60),
    (re.compile(r"booster bundle", re.I), 35),
]

# Setovi za "PRIORITET" oznaku (najsigurniji/najvrjedniji za flip)
PRIORITY_SETS = ["lost origin", "crown zenith", "151", "prismatic",
                 "evolving skies", "charizard", "silver tempest",
                 "astral radiance", "brilliant stars"]

# === PROVJERENI TRZISNI RASPONI (EUR, sealed, EN) ===
# Rucno provjereno preko Cardmarket/eBay. Format: (set_kljuc, tip) -> "raspon".
# Tip: "box" = booster box, "etb" = ETB, "upc" = ultra/super premium.
# Prikazuje se u poruci SAMO ako se i set I tip poklope (inace samo CM link).
# NAPOMENA: staticno - provjeri svako par mjeseci jer trziste se mijenja.
MARKET_RANGES = {
    ("lost origin", "box"):      "180-250 €",
    ("silver tempest", "box"):   "160-210 €",
    ("astral radiance", "box"):  "170-220 €",
    ("brilliant stars", "box"):  "180-230 €",
    ("evolving skies", "box"):   "300-400 €",
    ("crown zenith", "etb"):     "125-165 €",
    ("crow zenith", "etb"):      "125-165 €",
    ("151", "upc"):              "150-200 €",
    ("151", "etb"):              "65-90 €",
    ("151", "box"):              "200-260 €",
    ("charizard", "upc"):        "250-325 €",   # S&S Charizard UPC
    ("obsidian flames", "etb"):  "85-95 €",
    ("obsidian flames", "box"):  "150-190 €",
    ("paradox rift", "box"):     "130-160 €",
    ("paldea evolved", "box"):   "120-150 €",
    ("prismatic", "etb"):        "90-130 €",
}

def market_range(title):
    """Vrati provjereni trzisni raspon ako poznajemo set+tip, inace None."""
    t = norm(title)
    # odredi tip
    if re.search(r"ultra.?premium|\bupc\b|super.?premium", t):
        typ = "upc"
    elif re.search(r"elite trainer|\betb\b", t):
        typ = "etb"
    elif re.search(r"booster box|booster display", t):
        typ = "box"
    else:
        typ = None
    if not typ:
        return None
    for (skey, stype), rng in MARKET_RANGES.items():
        if stype == typ and skey in t:
            return rng
    return None


# Tip proizvoda koji uopce promatramo. Sve drugo se ignorira.
WANTED_TYPE = re.compile(
    r"(elite trainer|\betb\b|booster box|booster bundle|booster display|"
    r"premium collection|super premium|collection box|ultra premium|\bupc\b|\bcase\b)",
    re.I,
)
# Eksplicitno izbaci sitnice (smanjuje sum).
SKIP_TYPE = re.compile(
    r"(single|sleeve|deck protector|binder|portfolio|playmat|toploader|dice|"
    r"mini tin|poster|pencil|checklane|3-pack|3 pack|sleeved booster|"
    r"battle deck|toolkit|holiday calendar|build . battle|"
    r"gem pack|akrilna zastita|akrilna zastit|acryl|protector|zastita za)",
    re.I,
)

# Izbaci NE-engleske regije (drugacije trziste, prag je za EN -> lazni pozitivci)
# i ocijenjene single karte (psa/bgs/cgc + broj karte tipa "#159").
SKIP_REGION_GRADED = re.compile(
    r"(\bkorean\b|\bjapanese\b|\bjp\b|\bkr\b|\bcn\b|\bchinese\b|"
    r"\bpsa\b|\bbgs\b|\bcgc\b|\bace\b\s*\d|#\s*\d)",
    re.I,
)

# Izbaci DRUGE TCG igre (Magic Omens i sl. prodaju mijesano) - hocemo SAMO Pokemon.
SKIP_OTHER_TCG = re.compile(
    r"(yu-?gi-?oh|yugioh|magic.{0,6}gathering|\bmtg\b|lorcana|one piece|"
    r"\bop-?\d|digimon|flesh and blood|\bfab\b|weiss|dragon ball|metazoo|"
    r"star wars|riftbound|gundam|union arena|ultra.?pro|gaming case|"
    r"dragon shield|gamegenic|ultimate guard)",
    re.I,
)
# Proizvod MORA djelovati kao Pokemon (ime/set/lik). Inace preskoci.
REQUIRE_POKEMON = re.compile(
    r"(pokemon|pok\u00e9mon|\bsv\d|scarlet|violet|sword|shield|"
    r"charizard|pikachu|eevee|mewtwo|booster|elite trainer|\betb\b|"
    r"premium collection|\bupc\b)",
    re.I,
)

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

# --- ANTI-SPAM KONTROLE ---
# Shopovi koje si vec "pocistio" (znas sve dealove) -> NE javljaj nove,
# osim ako cijena PADNE ispod vec videne. Stavi ime tocno kao gore.
# Primjer: skini # ispod da utisas Igracke Hrvatska (statican katalog).
MUTED_SHOPS = {
    "Igracke Hrvatska",
}
# Max broj NOVIH alerta po jednom shopu po ciklusu (da te jedan shop ne preplavi).
# 0 = bez limita. Preporuka 3-5.
MAX_ALERTS_PER_SHOP = 4



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


def cardmarket_link(title):
    """Sastavi Cardmarket pretragu za ovaj proizvod (da odmah provjeris pravu cijenu)."""
    import urllib.parse
    # ocisti naslov od sifri/viska da pretraga bude tocnija
    q = re.sub(r"[#].*$", "", title)
    q = re.sub(r"\b(pokemon|tcg|scarlet|violet|sv\d+|cbb\w*)\b", "", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    enc = urllib.parse.quote(q)
    return f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={enc}"


def match_target(title):
    """Vrati (labela, prag, prioritet) ako je proizvod vrijedan tip.
    Prag = visi od (HOT_SETS bonus za set, genericki prag za tip).
    Tako hvatamo i imenovane setove I sve ostale vrijedne formate."""
    t = norm(title)
    if SKIP_TYPE.search(t):
        return None
    if SKIP_REGION_GRADED.search(t):
        return None
    if SKIP_OTHER_TCG.search(t):      # Yu-Gi-Oh/MTG/One Piece/oprema -> van
        return None
    if not REQUIRE_POKEMON.search(t):  # mora djelovati kao Pokemon
        return None
    if not WANTED_TYPE.search(t):
        return None

    # 1) Tip mora biti medu zeljenima (inace ignoriraj)
    type_prag = 0
    for rx, prag in TYPE_THRESHOLDS:
        if rx.search(t):
            type_prag = max(type_prag, prag)
    if type_prag == 0:
        return None

    # 2) Ako prepoznamo KONKRETAN set -> njegov prag je MJERODAVAN
    #    (jer za njega znamo pravu trzisnu cijenu). Genericki tip se koristi
    #    SAMO kad set nije prepoznat (nepoznato = rizicno = nizak prag).
    set_prag = None
    for key, prag in HOT_SETS.items():
        if key in t:
            if set_prag is None or prag > set_prag:
                set_prag = prag

    prag = set_prag if set_prag is not None else type_prag

    # 3) Labela + prioritet
    label = title.strip()
    priority = any(p in t for p in PRIORITY_SETS)
    return (label, prag, priority)


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
    per_shop_count = {}

    for uid, title, price, available, url in all_items:
        seen.add(uid)
        m = match_target(title)
        if not m:
            continue
        label, prag, priority = m

        # "Vrijedi alertati" = na stanju I (cijena<=prag ILI cijena nepoznata=0)
        hit = available and (price == 0 or price <= prag)

        prev = state.get(uid, {})
        prev_hit = prev.get("hit", False)
        prev_price = prev.get("price", 0)

        shop = uid.split(":", 1)[0]
        # Okini ako: prelaz iz "ne-hit" u "hit", ILI je vec hit ali cijena PALA
        price_dropped = hit and prev_hit and price and prev_price and price < prev_price

        # ANTI-SPAM: utisani shop javlja SAMO na pad cijene (ne na "novo na stanju")
        muted = shop in MUTED_SHOPS
        should_alert = hit and ((not prev_hit and not muted) or price_dropped)

        # ANTI-SPAM: kapa po shopu po ciklusu
        if should_alert and MAX_ALERTS_PER_SHOP:
            if per_shop_count.get(shop, 0) >= MAX_ALERTS_PER_SHOP:
                should_alert = False

        if should_alert:
            per_shop_count[shop] = per_shop_count.get(shop, 0) + 1
            cijena_txt = f"{price:.2f} EUR" if price else "cijena na stranici"
            tag = "🔥 <b>PRIORITET</b>\n" if priority else ""
            drop = "📉 <b>PAD CIJENE!</b>\n" if price_dropped else ""
            cm = cardmarket_link(title)
            # provjereni trzisni raspon (ako poznajemo set+tip)
            rng = market_range(title)
            rng_line = ""
            if rng:
                rng_line = f"📊 <b>tržište ~{rng}</b> (provjereno)\n"
                # gruba marza: donja granica raspona - cijena
                try:
                    low = float(re.findall(r"\d+", rng)[0])
                    if price and low > price:
                        rng_line += f"💰 procjena marže: +{low - price:.0f}€ i više\n"
                except Exception:
                    pass
            alerts.append(
                f"🟢 <b>NA STANJU</b>\n"
                f"{tag}{drop}"
                f"🏪 {html.escape(shop)}\n"
                f"📦 {html.escape(title)}\n"
                f"💶 <b>{cijena_txt}</b>\n"
                f"{rng_line}"
                f"🛒 <a href=\"{html.escape(url)}\">Kupi na shopu</a>\n"
                f"📊 <a href=\"{html.escape(cm)}\">Provjeri na Cardmarketu</a>\n"
                f"<i>{'raspon je orijentacija — potvrdi na Cardmarketu' if rng else 'kupi samo ako je shop cijena ispod Cardmarketa'}</i>"
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
