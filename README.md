# ZG TCG Restock Bot — upute

Telegram bot koji prati HR Pokemon shopove i javi ti kad je vrijedan sealed
proizvod NA STANJU i ispod fer cijene (instant marza). Radi na GitHub Actions,
besplatno, bez servera, svakih 15 min.

---

## ŠTO PRATI
- **Shopify (products.json):** Magic Omens, Origin Cards
- **WooCommerce (Store API):** PokeBros, Dabas, Igracke Hrvatska, Carta Magica
- **Setovi + fer prag:** 151 (≤92€), Chaos Rising (≤90€), Destined Rivals (≤72€),
  Prismatic (≤95€), Surging Sparks (≤95€), Paldean Fates (≤65€),
  + pre-load: Pitch Black Night, 30th Anniversary
- Samo ETB / Booster Box / Bundle / Premium Collection. Sitnice (single, sleeve…) preskace.
- Alert SAMO na promjenu (OOS→stock ili pad cijene). Ne spama.

---

## POSTAVA (10 minuta, jednom)

### 1. Napravi GitHub repo
1. github.com → New repository → ime npr. `zgtcg-bot` → **Private** → Create.
2. Uploadaj ove fajlove (drag & drop u "Add file → Upload files"):
   - `tracker.py`
   - `requirements.txt`
   - `.github/workflows/check.yml`  ← MORA biti u toj putanji (s tockom na pocetku)

> Ako drag&drop ne pravi mapu `.github/workflows/`, napravi je kroz
> "Add file → Create new file" i u ime upisi: `.github/workflows/check.yml`

### 2. Stavi tajne (Secrets)
Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
Dodaj dva:

| Name | Value |
|------|-------|
| `TG_TOKEN` | tvoj BotFather token (onaj `8792…:AAG…`) |
| `TG_CHAT`  | tvoj Chat ID (broj od @userinfobot) |

> Token ide OVDJE, nikad u kod. Tako ga nitko ne vidi.

### 3. Upali Actions
Repo → tab **Actions** → ako pita, klikni "I understand… enable workflows".
→ lijevo odaberi **TCG Restock Check** → **Run workflow** (rucni test).
Za par sekundi vidis log. Ako je sve ok, dalje radi sam svakih 15 min.

### 4. Test da Telegram radi
Prvi `Run workflow` ce, ako bas tad nesto bude na stanju ispod praga, poslati poruku.
Da odmah provjeris vezu, mozes privremeno spustiti neki prag visoko (npr. 151 na 999)
pa ces skoro sigurno dobiti alert na prvom pokretanju — pa vrati natrag.

---

## PODESAVANJE

**Promijeniti pragove / setove:** otvori `tracker.py` → lista `TARGETS` na vrhu.
Format: `("kljucna rijec", prag_EUR, "Labela")`.

**Dodati shop (Shopify):** dopisi u `SHOPIFY_SHOPS`: `("Ime", "https://domena")`.
**Dodati shop (WooCommerce):** dopisi u `WOO_SHOPS` isto. Ako shop nema Store API,
bot ga tiho preskoci (vidis "SKIP" u logu) — nista se ne lomi.

**Cesce/rjedje:** u `.github/workflows/check.yml` promijeni `*/15` (minute).
> Napomena: GitHub cron na free planu zna kasniti par min kad su gusti — normalno.

---

## NAPOMENE
- Custom shopovi (Ozone, Sophoslab, foon, IgrackeShop) NISU ovdje jer trebaju
  headless browser (Playwright) — to je Faza 2 ako zelis.
- WooCommerce shopovi rade samo ako im je Store API otvoren; ako log stalno
  pokazuje SKIP za neki, javi pa rjesavamo kroz HTML scraping.
- Bot cita javne cijene, s pristojnim razmakom (1s/stranica, 15 min/ciklus) —
  ne preopterecuje shopove.
