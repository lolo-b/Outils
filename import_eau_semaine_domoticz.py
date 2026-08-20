#!/usr/bin/env python3
"""
Import d'un historique HEBDOMADAIRE de consommation d'eau vers Domoticz.

Le fichier CSV attendu a le format (séparateur ';') :
Semaine;Index relevé fin de semaine (litres);Consommation de la semaine (litres);Index Mesuré/Estimé
12/05-18/05;1944654;1517;Mesuré

La colonne "Semaine" ne contient pas l'année : ce script la déduit en
partant de START_YEAR (année de la toute première ligne) et en
incrémentant automatiquement dès qu'un passage à l'année suivante est
détecté (ex: décembre -> janvier).

Comme la source ne fournit qu'un total par semaine (pas de détail
journalier), toute la consommation de la semaine est enregistrée sur
son dernier jour (date de fin de semaine). Les totaux semaine/mois/année
dans Domoticz seront corrects ; seule la vue "jour" affichera un pic
ponctuel plutôt qu'une répartition lissée sur 7 jours.

Aucune dépendance externe requise (uniquement la bibliothèque standard Python).

Utilisation :
    python3 import_eau_semaine_domoticz.py

Configurez les variables ci-dessous avant de lancer le script.
"""

import base64
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

# --------------------------------------------------------------------------
# CONFIGURATION — à adapter à votre installation
# --------------------------------------------------------------------------

# URL de base de votre Domoticz (sans slash final)
DOMOTICZ_URL = "http://192.168.1.75:8080"

# Identifiants si l'authentification Domoticz est activée (sinon laisser None)
DOMOTICZ_USER = None
DOMOTICZ_PASSWORD = None

# IDX du device "Compteur géré" créé dans Domoticz (Configuration > Appareils)
# -> le même IDX que pour l'import journalier, si vous alimentez le même compteur
DOMOTICZ_IDX = 142

# Chemin vers le fichier CSV hebdomadaire fourni par votre fournisseur d'eau
CSV_FILE = "historique_semaine_litres.csv"

# Année de la toute première semaine du fichier (déduite manuellement,
# car la colonne "Semaine" ne l'indique pas)
START_YEAR = 2025

# --------------------------------------------------------------------------


def push_to_domoticz(counter_liters: int, usage_liters: int, date_str: str) -> None:
    """Envoie une entrée (index cumulé, conso, date) à Domoticz."""
    svalue = f"{counter_liters};{usage_liters};{date_str}"
    params = {
        "type": "command",
        "param": "udevice",
        "idx": DOMOTICZ_IDX,
        "nvalue": 0,
        "svalue": svalue,
    }
    url = f"{DOMOTICZ_URL}/json.htm?{urllib.parse.urlencode(params)}"

    request = urllib.request.Request(url)
    if DOMOTICZ_USER:
        credentials = f"{DOMOTICZ_USER}:{DOMOTICZ_PASSWORD}".encode("utf-8")
        token = base64.b64encode(credentials).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"  -> ERREUR réseau pour {date_str} : {exc}", file=sys.stderr)
        return

    if data.get("status") != "OK":
        print(f"  -> ERREUR Domoticz pour {date_str} : {data}", file=sys.stderr)
    else:
        print(f"  -> {date_str} : index={counter_liters} L, conso semaine={usage_liters} L : OK")


def resolve_week_end_dates(rows):
    """Déduit la date de fin de chaque semaine, en gérant le passage d'année."""
    current_year = START_YEAR
    prev_end = None
    resolved = []

    for row in rows:
        _start_str, end_str = row["Semaine"].strip().split("-")
        end_day, end_month = (int(part) for part in end_str.split("/"))

        candidate = date(current_year, end_month, end_day)
        if prev_end and candidate < prev_end:
            # On a franchi le nouvel an (ex: passage de décembre à janvier)
            current_year += 1
            candidate = date(current_year, end_month, end_day)

        prev_end = candidate
        resolved.append((candidate, row))

    return resolved


def main() -> None:
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    resolved = resolve_week_end_dates(rows)

    print(f"{len(resolved)} semaines trouvées dans {CSV_FILE}. Envoi vers Domoticz (idx={DOMOTICZ_IDX})...\n")

    for end_date, row in resolved:
        counter = int(row["Index relevé fin de semaine (litres)"])
        usage = int(row["Consommation de la semaine (litres)"])
        date_str = end_date.strftime("%Y-%m-%d")

        push_to_domoticz(counter, usage, date_str)

    print("\nTerminé.")


if __name__ == "__main__":
    main()
