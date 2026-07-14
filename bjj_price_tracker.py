scripts/bjj_price_tracker.py
#!/usr/bin/env python3
"""
BJJ Price Tracker
------------------
Haalt dagelijks alle producten (alle merken, alle maten) op van de
geconfigureerde webshops via Shopify's publieke products.json feed,
vergelijkt de prijzen met de vorige run, en:
  1. Slaat een snapshot + geschiedenis op in data/
  2. Stuurt een e-mail met een overzicht van prijswijzigingen

Voeg of verwijder shops in shops_config.json.
Let op: dit werkt alleen voor shops die op Shopify draaien en hun
products.json feed niet hebben uitgeschakeld. Voor andere platforms
(WooCommerce, Lightspeed, custom) moet een aparte parser worden
toegevoegd -- zie de functie `fetch_html_shop` als startpunt.
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
CONFIG_PATH = SCRIPT_DIR / "shops_config.json"
SNAPSHOT_PATH = DATA_DIR / "latest_snapshot.json"
HISTORY_PATH = DATA_DIR / "price_history.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalPriceTracker/1.0; +personal use)"
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_shopify_shop(shop):
    """Fetch every product from a Shopify store's public products.json feed.

    Shopify paginates this endpoint 250 products at a time via ?page=N.
    We stop once a page comes back empty.
    """
    base_url = shop["base_url"].rstrip("/")
    products = []
    page = 1

    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [WARN] Kon {url} niet ophalen: {e}", file=sys.stderr)
            break

        payload = resp.json()
        batch = payload.get("products", [])
        if not batch:
            break

        for product in batch:
            title = product.get("title", "")
            vendor = product.get("vendor", "")
            handle = product.get("handle", "")
            product_url = f"{base_url}/products/{handle}"

            for variant in product.get("variants", []):
                products.append({
                    "shop": shop["name"],
                    "brand": vendor,
                    "product": title,
                    "size": variant.get("title", "One Size"),
                    "price": float(variant.get("price", 0)),
                    "in_stock": bool(variant.get("available", False)),
                    "url": product_url,
                })

        page += 1
        time.sleep(0.5)  # wees aardig voor de server

    return products


def fetch_html_shop(shop):
    """Placeholder voor niet-Shopify shops.

    Voeg hier BeautifulSoup-logica toe die specifiek is voor de HTML-structuur
    van de betreffende webshop. Inspecteer de paginabron (rechtermuisknop ->
    'Pagina bron bekijken') om de juiste CSS-selectors te vinden voor
    producttitel, merk, prijs, maat en voorraadstatus.
    """
    print(f"  [SKIP] '{shop['name']}' heeft platform 'html' -- nog niet "
          f"geïmplementeerd. Voeg een parser toe in fetch_html_shop().")
    return []


def fetch_shop(shop):
    print(f"Ophalen: {shop['name']} ({shop['base_url']}) ...")
    if shop["platform"] == "shopify":
        return fetch_shopify_shop(shop)
    elif shop["platform"] == "html":
        return fetch_html_shop(shop)
    else:
        print(f"  [WARN] Onbekend platform: {shop['platform']}", file=sys.stderr)
        return []


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def make_key(item):
    return f"{item['shop']}|{item['product']}|{item['size']}"


def compare_snapshots(old_items, new_items):
    """Retourneert lijsten van prijsdalingen, prijsstijgingen, en nieuwe producten."""
    old_by_key = {make_key(i): i for i in old_items}

    drops, increases, new_products = [], [], []

    for item in new_items:
        key = make_key(item)
        old = old_by_key.get(key)
        if old is None:
            new_products.append(item)
        elif item["price"] < old["price"]:
            drops.append({**item, "old_price": old["price"]})
        elif item["price"] > old["price"]:
            increases.append({**item, "old_price": old["price"]})

    return drops, increases, new_products


def build_email_body(drops, increases, new_products, total_products):
    lines = []
    lines.append(f"BJJ prijstracker -- dagelijks overzicht ({datetime.now().strftime('%d-%m-%Y')})")
    lines.append(f"Totaal aantal bijgehouden varianten: {total_products}\n")

    if drops:
        lines.append(f"PRIJSDALINGEN ({len(drops)}):")
        for d in sorted(drops, key=lambda x: x["price"] - x["old_price"])[:30]:
            diff = d["old_price"] - d["price"]
            lines.append(
                f"  - {d['shop']}: {d['brand']} {d['product']} ({d['size']}) "
                f"€{d['old_price']:.2f} -> €{d['price']:.2f}  (-€{diff:.2f})\n    {d['url']}"
            )
        lines.append("")

    if new_products:
        lines.append(f"NIEUWE PRODUCTEN ({len(new_products)}):")
        for n in new_products[:20]:
            lines.append(f"  - {n['shop']}: {n['brand']} {n['product']} ({n['size']}) €{n['price']:.2f}")
        lines.append("")

    if increases:
        lines.append(f"Prijsstijgingen: {len(increases)} (niet in detail getoond)")

    if not drops and not new_products:
        lines.append("Geen prijsdalingen of nieuwe producten sinds de vorige run.")

    return "\n".join(lines)


def send_email(subject, body):
    email_from = os.environ.get("EMAIL_ADDRESS")
    email_password = os.environ.get("EMAIL_PASSWORD")
    email_to = os.environ.get("EMAIL_TO", email_from)

    if not email_from or not email_password:
        print("[INFO] EMAIL_ADDRESS / EMAIL_PASSWORD niet gezet -- e-mail wordt overgeslagen.")
        return

    msg = MIMEMultipart()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(email_from, email_password)
        server.send_message(msg)

    print(f"[OK] E-mail verstuurd naar {email_to}")


def main():
    config = load_config()
    DATA_DIR.mkdir(exist_ok=True)

    all_products = []
    for shop in config["shops"]:
        items = fetch_shop(shop)
        print(f"  -> {len(items)} varianten gevonden")
        all_products.extend(items)

    if not all_products:
        print("[FOUT] Geen producten opgehaald bij geen enkele shop. Stoppen.")
        sys.exit(1)

    old_snapshot = load_json(SNAPSHOT_PATH, [])
    drops, increases, new_products = compare_snapshots(old_snapshot, all_products)

    # Sla nieuwe snapshot op
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_products, f, ensure_ascii=False, indent=2)

    # Voeg toe aan geschiedenis (voor de dashboard-grafiek)
    history = load_json(HISTORY_PATH, [])
    history.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_products": len(all_products),
        "price_drops": len(drops),
        "new_products": len(new_products),
    })
    # bewaar max 180 dagen geschiedenis
    history = history[-180:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    body = build_email_body(drops, increases, new_products, len(all_products))
    print("\n" + body)

    subject = f"BJJ prijzen: {len(drops)} daling(en), {len(new_products)} nieuw"
    send_email(subject, body)


if __name__ == "__main__":
    main()
