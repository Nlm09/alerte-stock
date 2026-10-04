"""Surveillance des retours en stock -> alertes Telegram.

Lit products.json, visite chaque page, détecte la disponibilité et
envoie un message Telegram quand un produit passe de "pas disponible"
à "en stock" (ou "précommande").
"""
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
RECAP = "--recap" in sys.argv

PRODUITS = Path("products.json")
ETAT = Path("state.json")
SEUIL_ERREURS = 6  # nombre de vérifications ratées avant de te prévenir

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

# Correspondance entre les valeurs "schema.org" et nos statuts
DISPO = {
    "instock": "en_stock",
    "limitedavailability": "en_stock",
    "onlineonly": "en_stock",
    "instoreonly": "magasin",
    "outofstock": "rupture",
    "soldout": "rupture",
    "discontinued": "rupture",
    "preorder": "precommande",
    "presale": "precommande",
    "backorder": "precommande",
}
VALEURS = "InStock|OutOfStock|SoldOut|PreOrder|PreSale|BackOrder|LimitedAvailability|Discontinued|OnlineOnly|InStoreOnly"

# Plan B : mots cherchés dans le texte de la page
MOTS_RUPTURE = [
    "rupture de stock", "indisponible", "épuisé", "epuisé", "sold out",
    "bientôt disponible", "plus disponible", "temporairement indisponible",
]
MOTS_STOCK = ["ajouter au panier", "ajouter à mon panier", "acheter maintenant", "add to cart"]

ALERTE = {"en_stock", "precommande"}
ICONES = {
    "en_stock": "🟢", "precommande": "🟡", "rupture": "🔴",
    "magasin": "🏬", "inconnu": "❓", "erreur": "⚠️",
}


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram non configuré (secrets manquants).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=20,
        )
        if r.status_code != 200:
            print("Erreur Telegram, code", r.status_code)
    except requests.RequestException as e:
        print("Erreur Telegram :", type(e).__name__)


def recuperer(url):
    """Retourne (html, erreur). html vaut None en cas d'échec."""
    derniere = ""
    for _ in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.text, ""
            derniere = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            derniere = type(e).__name__
        time.sleep(3)
    return None, derniere


def detecter(html):
    """Retourne (statut, méthode utilisée)."""
    # 1) Données structurées (la méthode la plus fiable)
    m = re.search(r'"availability"\s*:\s*"(?:https?:\\?/\\?/schema\.org\\?/)?(\w+)"', html, re.I)
    if not m:
        m = re.search(rf"schema\.org\\?/({VALEURS})", html, re.I)
    if m:
        statut = DISPO.get(m.group(1).lower())
        if statut:
            return statut, "données structurées"

    # 2) Balises meta
    m = re.search(r'(?:product|og):availability["\']\s+content=["\']([^"\']+)', html, re.I)
    if m:
        v = m.group(1).lower().replace(" ", "")
        if "outofstock" in v or "soldout" in v or v == "oos":
            return "rupture", "balise meta"
        if "preorder" in v:
            return "precommande", "balise meta"
        if "instock" in v:
            return "en_stock", "balise meta"

    # 3) Plan B : le texte visible de la page
    texte = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    texte = re.sub(r"<[^>]+>", " ", texte).lower()
    texte = re.sub(r"\s+", " ", texte)
    if any(mot in texte for mot in MOTS_RUPTURE):
        return "rupture", "texte"
    if any(mot in texte for mot in MOTS_STOCK):
        return "en_stock", "texte"
    return "inconnu", "aucune"


def main():
    produits = json.loads(PRODUITS.read_text(encoding="utf-8"))
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if ETAT.exists() else {}
    lignes = []

    for p in produits:
        url = p["url"]
        nom = f'{p["nom"]} ({p["site"]})'
        e = etat.get(url, {})

        html, erreur = recuperer(url)
        if html is None:
            e["erreurs"] = e.get("erreurs", 0) + 1
            if e["erreurs"] == SEUIL_ERREURS:
                telegram(
                    f"⚠️ Page illisible depuis {SEUIL_ERREURS} vérifications "
                    f"({erreur}) : {nom}\n{url}"
                )
            statut, methode = "erreur", erreur
        else:
            e["erreurs"] = 0
            statut, methode = detecter(html)
            avant = e.get("statut")
            if statut in ALERTE and avant not in ALERTE:
                titre = "EN STOCK" if statut == "en_stock" else "PRÉCOMMANDE OUVERTE"
                telegram(f"{ICONES[statut]} {titre} : {nom}\n{url}")
            e["statut"] = statut

        etat[url] = e
        lignes.append(f"{ICONES.get(statut, '❓')} {nom} : {statut} [{methode}]")
        print(lignes[-1])
        time.sleep(random.uniform(1, 3))

    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding="utf-8")

    if RECAP:
        telegram("Récapitulatif de la surveillance :\n" + "\n".join(lignes))


if __name__ == "__main__":
    main()
