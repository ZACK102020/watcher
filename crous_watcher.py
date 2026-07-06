#!/usr/bin/env python3
"""
CROUS Watcher — surveille trouverunlogement.lescrous.fr et envoie un email
dès qu'une NOUVELLE annonce apparaît dans les villes qui t'intéressent.

Usage:
    python3 crous_watcher.py            # lance un check unique (à utiliser avec cron)
    python3 crous_watcher.py --loop     # tourne en continu, check toutes les N minutes

Config: voir config.json (à copier depuis config.example.json et à remplir)
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://trouverunlogement.lescrous.fr/tools/45/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "crous_watcher.log"

# Codes postaux (2 premiers chiffres) des 8 départements d'Île-de-France
IDF_DEPARTMENTS = {"75", "77", "78", "91", "92", "93", "94", "95"}


def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # si le disque est en lecture seule (ex: certains environnements CI), on ignore


def load_config():
    if not CONFIG_PATH.exists():
        log(f"⚠️  Fichier config.json introuvable ({CONFIG_PATH}).")
        log("   Copie config.example.json vers config.json et remplis-le.")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    # Permet de surcharger les identifiants email via variables d'environnement
    # (pratique pour GitHub Actions Secrets, pour ne jamais committer de mot de passe)
    env_map = {
        "CROUS_SMTP_SENDER": "sender",
        "CROUS_SMTP_PASSWORD": "password",
        "CROUS_SMTP_RECEIVER": "receiver",
    }
    for env_var, key in env_map.items():
        val = os.environ.get(env_var)
        if val:
            config.setdefault("email", {})[key] = val

    return config


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_total_pages(soup):
    """Cherche 'page X sur N' dans le titre ou le lien 'Dernière page'."""
    last_page_link = soup.find("a", string=re.compile("Dernière page"))
    if last_page_link and last_page_link.get("href"):
        m = re.search(r"page=(\d+)", last_page_link["href"])
        if m:
            return int(m.group(1))
    # fallback: chercher dans le <title> ou <h1> un motif "page X sur N"
    text = soup.get_text()
    m = re.search(r"page\s+\d+\s+sur\s+(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 1


def get_total_count(soup):
    """Extrait le nombre total de logements affiché ('X logements trouvés en
    France'). Beaucoup plus précis que le nombre de pages pour le check léger :
    ça bouge dès qu'UN SEUL logement est ajouté ou retiré, contrairement au
    nombre de pages qui ne change qu'après ~24 logements d'écart."""
    text = soup.get_text()
    m = re.search(r"([\d\s]+)\s*logements?\s+trouvés", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1).replace(" ", "").replace("\xa0", ""))
        except ValueError:
            return None
    return None


def parse_listings(soup):
    """Extrait chaque logement à partir des liens /accommodations/{id}."""
    listings = {}
    links = soup.find_all("a", href=re.compile(r"/tools/45/accommodations/\d+"))
    for link in links:
        href = link["href"]
        m = re.search(r"/accommodations/(\d+)", href)
        if not m:
            continue
        acc_id = m.group(1)
        if acc_id in listings:
            continue  # certains liens apparaissent 2x (image + titre)

        name = link.get_text(strip=True)
        if not name:
            continue  # c'était probablement le lien-image, pas le lien-titre

        # Remonte au bloc parent contenant les infos de CE logement uniquement.
        # On s'arrête dès que le bloc contiendrait un 2e logement (pour ne pas
        # mélanger les infos de plusieurs annonces).
        block = link
        for _ in range(8):
            if not block.parent:
                break
            candidate = block.parent
            ids_in_candidate = {
                re.search(r"/accommodations/(\d+)", a["href"]).group(1)
                for a in candidate.find_all("a", href=re.compile(r"/tools/45/accommodations/\d+"))
            }
            if len(ids_in_candidate) > 1:
                break  # candidate englobe déjà un autre logement, on n'y monte pas
            block = candidate
        block_text = block.get_text(" ", strip=True)

        price_match = re.search(
            r"(?:de\s+)?(\d[\d,\.]*)\s*(?:à\s*(\d[\d,\.]*)\s*)?€", block_text
        )
        if price_match:
            low, high = price_match.group(1), price_match.group(2)
            price = f"de {low} à {high} €" if high else f"{low} €"
        else:
            price = "?"

        # L'adresse est le texte juste après le nom, avant le prochain marqueur connu
        address = ""
        idx = block_text.find(name)
        if idx != -1:
            rest = block_text[idx + len(name):]
            stop_markers = ["Logement très demandé", "Dernières places", "m²", "Individuel", "Colocation"]
            cut = len(rest)
            for marker in stop_markers:
                pos = rest.find(marker)
                if pos != -1:
                    cut = min(cut, pos)
            address = rest[:cut].strip(" -")

        tags = []
        if "Dernières places disponibles" in block_text:
            tags.append("Dernières places disponibles !")
        if "Logement très demandé" in block_text:
            tags.append("Logement très demandé !")

        listings[acc_id] = {
            "id": acc_id,
            "name": name,
            "address": address,
            "price": price,
            "tags": tags,
            "url": f"https://trouverunlogement.lescrous.fr{href}" if href.startswith("/") else href,
        }
    return listings


def fetch_with_retry(session, url, params=None, max_retries=6, timeout=25):
    """GET avec retries + backoff exponentiel. Lève une exception si tout échoue.
    max_retries=6 (au lieu de 4) car le site CROUS renvoie parfois un 404 passager
    (probable protection anti-bot ou instabilité serveur) qui se résout tout seul
    en attendant un peu plus longtemps."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = "utf-8"  # le site est en UTF-8 ; requests devine parfois mal (mojibake sur les accents sinon)
            return resp
        except (requests.RequestException,) as e:
            last_error = e
            wait = min(2 ** attempt, 60)  # 2s, 4s, 8s, 16s, 32s, 60s (plafonné)
            log(f"⚠️  Requête échouée (tentative {attempt}/{max_retries}) — {e}. Nouvelle tentative dans {wait}s.")
            time.sleep(wait)
    raise RuntimeError(f"Échec définitif après {max_retries} tentatives sur {url}: {last_error}")


def fetch_all_listings(max_pages_safety=150):
    session = requests.Session()
    session.headers.update(HEADERS)

    first_resp = fetch_with_retry(session, BASE_URL)
    soup = BeautifulSoup(first_resp.text, "html.parser")
    total_pages = min(get_total_pages(soup), max_pages_safety)

    all_listings = {}
    all_listings.update(parse_listings(soup))
    log(f"Page 1/{total_pages} — {len(all_listings)} logements cumulés")

    for page in range(2, total_pages + 1):
        resp = fetch_with_retry(session, BASE_URL, params={"page": page})
        page_soup = BeautifulSoup(resp.text, "html.parser")
        all_listings.update(parse_listings(page_soup))
        if page % 10 == 0 or page == total_pages:
            log(f"Page {page}/{total_pages} — {len(all_listings)} logements cumulés")
        time.sleep(0.4)  # pour rester poli avec le serveur

    return all_listings


def matches_filters(listing, departments, villes, prix_max):
    address = listing["address"]
    address_lower = address.lower()
    name_lower = listing["name"].lower()

    if departments:
        postal_match = re.search(r"\b(\d{5})\b", address)
        if not postal_match:
            return False  # pas de code postal détecté -> on ne peut pas confirmer la région, on exclut
        dept = postal_match.group(1)[:2]
        if dept not in departments:
            return False

    if villes:
        if not any(v.lower() in address_lower or v.lower() in name_lower for v in villes):
            return False

    if prix_max:
        prices = re.findall(r"[\d,\.]+", listing["price"])
        if prices:
            try:
                min_price = float(prices[0].replace(",", "."))
                if min_price > prix_max:
                    return False
            except ValueError:
                pass

    return True


def send_email(config, new_listings):
    email_cfg = config["email"]

    subject = f"🏠 {len(new_listings)} nouvelle(s) annonce(s) CROUS disponible(s) !"
    lines = [f"Nouvelles annonces détectées ({len(new_listings)}) :\n"]
    for listing in new_listings:
        tags = " — " + ", ".join(listing["tags"]) if listing["tags"] else ""
        lines.append(
            f"• {listing['name']} — {listing['address']} — {listing['price']}{tags}\n"
            f"  {listing['url']}\n"
        )
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["receiver"]

    with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["sender"], email_cfg["password"])
        server.sendmail(email_cfg["sender"], [email_cfg["receiver"]], msg.as_string())

    log(f"✅ Email envoyé à {email_cfg['receiver']} ({len(new_listings)} annonces)")


def send_alert_email(config, subject, body):
    """Email d'alerte technique — utilisé quand le scraper échoue ou détecte une anomalie,
    pour ne jamais rater une annonce à cause d'un bug silencieux."""
    email_cfg = config["email"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"⚠️ CROUS Watcher — {subject}"
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["receiver"]
    with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["sender"], email_cfg["password"])
        server.sendmail(email_cfg["sender"], [email_cfg["receiver"]], msg.as_string())
    log(f"📧 Email d'alerte envoyé : {subject}")


def run_once(config):
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    last_total_matching = state.get("last_total_matching")

    log("Récupération des annonces CROUS (toutes pages)...")
    try:
        all_listings = fetch_all_listings()
    except Exception as e:
        log(f"❌ Scraping impossible : {e}")
        try:
            send_alert_email(
                config,
                "Le scraping a échoué",
                f"Le script n'a pas réussi à récupérer les annonces après plusieurs tentatives.\n"
                f"Erreur : {e}\n\nVérifie manuellement le site : {BASE_URL}",
            )
        except Exception as mail_err:
            log(f"❌ Impossible d'envoyer l'email d'alerte non plus : {mail_err}")
        return  # on ne touche pas au state, on retentera au prochain run

    log(f"Total: {len(all_listings)} logements sur le site.")

    departments = config.get("departments", [])
    villes = config.get("villes", [])
    prix_max = config.get("prix_max")

    matching = {
        acc_id: listing
        for acc_id, listing in all_listings.items()
        if matches_filters(listing, departments, villes, prix_max)
    }
    log(f"{len(matching)} logements correspondent à tes filtres (départements={departments}, villes={villes}).")

    # --- Garde-fou anti "échec silencieux" ---
    # Si le nombre de logements matchés s'effondre brutalement par rapport au run
    # précédent (ex: le site a changé de structure et le parsing casse), on alerte
    # au lieu de mettre à jour le state comme si de rien n'était.
    if last_total_matching and last_total_matching >= 5 and len(matching) < last_total_matching * 0.3:
        log(f"⚠️ Anomalie: {len(matching)} logements trouvés vs {last_total_matching} au dernier run (chute >70%).")
        try:
            send_alert_email(
                config,
                "Anomalie détectée (chute brutale du nombre d'annonces)",
                f"Dernier run : {last_total_matching} logements correspondants.\n"
                f"Ce run : {len(matching)} logements correspondants.\n\n"
                f"Le site a peut-être changé de structure (le parsing casse silencieusement), "
                f"ou c'est une vraie chute de dispo. Vérifie manuellement : {BASE_URL}",
            )
        except Exception as mail_err:
            log(f"❌ Impossible d'envoyer l'email d'alerte: {mail_err}")
        return  # on ne met pas à jour seen_ids tant que ce n'est pas confirmé sain

    new_ids = set(matching.keys()) - seen_ids
    new_listings = [matching[i] for i in new_ids]

    # IMPORTANT : on ACCUMULE les IDs déjà vus (union), on ne les remplace jamais.
    # Le site CROUS ne renvoie pas ses résultats dans un ordre stable d'un scan à
    # l'autre : un même logement peut "manquer" un scan puis réapparaître, sans
    # avoir réellement disparu. Si on remplaçait seen_ids par matching.keys() à
    # chaque fois, un logement raté une fois serait réenregistré comme "nouveau"
    # au scan suivant → c'était la cause des emails avec 20-30 "nouveautés" en boucle.
    updated_seen_ids = seen_ids | set(matching.keys())

    if new_listings:
        log(f"🎉 {len(new_listings)} nouvelle(s) annonce(s) !")
        try:
            send_email(config, new_listings)
        except Exception as e:
            log(f"❌ Erreur envoi email: {e}")
            # on ne met PAS à jour seen_ids pour ces annonces si l'email a échoué,
            # comme ça elles seront re-signalées au prochain run plutôt que perdues
            state["seen_ids"] = list(seen_ids | (set(matching.keys()) - new_ids))
            state["last_total_matching"] = len(matching)
            save_state(state)
            return
    else:
        log("Rien de nouveau cette fois.")

    state["seen_ids"] = list(updated_seen_ids)
    state["last_total_matching"] = len(matching)
    state["last_success"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


def fetch_light_signature(session):
    """Check ultra-léger : récupère UNIQUEMENT la page 1 (pas les 60 pages)
    et retourne une 'signature' (total exact affiché sur le site + IDs de la
    page 1 + nombre de pages). Le total exact ('X logements trouvés en
    France') est le signal le plus fin : il bouge dès qu'UN SEUL logement
    change, contrairement au nombre de pages (~24 logements de marge)."""
    resp = fetch_with_retry(session, BASE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    total_count = get_total_count(soup)
    total_pages = get_total_pages(soup)
    page1_listings = parse_listings(soup)
    signature = {
        "total_count": total_count,
        "total_pages": total_pages,
        "page1_ids": sorted(page1_listings.keys()),
    }
    return signature


def run_light_check(config):
    """Check léger : 1 seule requête. Si rien n'a changé sur la page 1
    (nouveaux logements ni total de pages), on s'arrête là sans scraper
    les 60 pages. Permet un polling beaucoup plus fréquent sans surcharger
    le site ni risquer de se faire bloquer."""
    state = load_state()
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        signature = fetch_light_signature(session)
    except Exception as e:
        log(f"❌ Check léger impossible : {e}. On tente un check complet par sécurité.")
        run_once(config)
        return

    last_signature = state.get("light_signature")

    # Sécurité : même si rien n'a changé en apparence sur la page 1, on force un
    # scan complet de temps en temps (toutes les ~10 min) pour rattraper le cas
    # rare où un logement disparaît et un autre apparaît ailleurs que sur la
    # page 1 (compensation qui ne bougerait ni le total de pages ni la page 1).
    last_full_scan = state.get("last_success")
    force_full = True
    if last_full_scan:
        try:
            last_dt = time.mktime(time.strptime(last_full_scan, "%Y-%m-%d %H:%M:%S"))
            force_full = (time.time() - last_dt) > 10 * 60
        except ValueError:
            force_full = True

    if signature == last_signature and not force_full:
        log("Check léger : rien de changé sur la page 1, pas besoin de scanner en entier.")
        return

    if signature != last_signature:
        log("🔎 Check léger : changement détecté sur la page 1 → scan complet.")
    else:
        log("🔎 Scan complet périodique (sécurité, 30 min écoulées) même si rien détecté en léger.")

    run_once(config)

    # on met à jour la signature après le scan complet (qui vient de re-sauver le state)
    state = load_state()
    state["light_signature"] = signature
    save_state(state)


def main():
    config = load_config()
    loop_mode = "--loop" in sys.argv
    full_scan_mode = "--full" in sys.argv
    interval_min = config.get("check_interval_minutes", 15)

    if not loop_mode:
        if full_scan_mode:
            run_once(config)
        else:
            run_light_check(config)
        return

    log(f"Mode boucle activé — check toutes les {interval_min} minutes. Ctrl+C pour arrêter.")
    while True:
        try:
            if full_scan_mode:
                run_once(config)
            else:
                run_light_check(config)
        except Exception as e:
            log(f"❌ Erreur inattendue pendant le check: {e}")
        log(f"⏳ Prochain check dans {interval_min} min...\n")
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    main()
