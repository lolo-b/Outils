#!/usr/bin/env python3
"""
Import de l'historique de consommation d'eau (fichier fournisseur) vers Domoticz.

Le fichier CSV attendu a le format (séparateur ';') :
Date de relevé;Index relevé (litres);Consommation du jour (litres);Index Mesuré/Estimé
13/05/2026 12:00;2043608;71;Mesuré

Ce script pousse chaque ligne vers un device Domoticz de type
"Compteur géré" (Managed Counter) via l'API JSON, en utilisant le format :
svalue = INDEX_CUMULE;CONSO_DU_JOUR;YYYY-MM-DD
ce qui permet à Domoticz de ranger la donnée dans l'historique
(vues semaine/mois/année), même pour des dates passées.

Utilisation :
    python3 import_eau_domoticz.py

Configurez les variables ci-dessous avant de lancer le script.
"""

import csv
import sys
from datetime import datetime

import requests

# --------------------------------------------------------------------------
# CONFIGURATION — à adapter à votre installation
# --------------------------------------------------------------------------

# URL de base de votre Domoticz (sans slash final)
DOMOTICZ_URL = "http://192.168.1.75:8080"

# Identifiants si l'authentification Domoticz est activée (sinon laisser None)
DOMOTICZ_USER = None
DOMOTICZ_PASSWORD = None

# IDX du device "Compteur géré" créé dans Domoticz (Configuration > Appareils)
DOMOTICZ_IDX = 142

# Chemin vers le fichier CSV fourni par votre fournisseur d'eau
CSV_FILE = "historique_jours_litres.csv"

# --------------------------------------------------------------------------


def push_to_domoticz(counter_liters: int, usage_liters: int, date_str: str) -> None:
    """Envoie une entrée (index cumulé, conso du jour, date) à Domoticz."""
    svalue = f"{counter_liters};{usage_liters};{date_str}"
    params = {
        "type": "command",
        "param": "udevice",
        "idx": DOMOTICZ_IDX,
        "nvalue": 0,
        "svalue": svalue,
    }
    auth = (DOMOTICZ_USER, DOMOTICZ_PASSWORD) if DOMOTICZ_USER else None

    resp = requests.get(f"{DOMOTICZ_URL}/json.htm", params=params, auth=auth, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "OK":
        print(f"  -> ERREUR Domoticz pour {date_str} : {data}", file=sys.stderr)
    else:
        print(f"  -> {date_str} : index={counter_liters} L, conso={usage_liters} L : OK")


def main() -> None:
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    print(f"{len(rows)} jours trouvés dans {CSV_FILE}. Envoi vers Domoticz (idx={DOMOTICZ_IDX})...\n")

    for row in rows:
        raw_date = row["Date de relevé"].strip()
        # Le fichier contient une heure (ex: "12:00"), on ne garde que la date
        dt = datetime.strptime(raw_date, "%d/%m/%Y %H:%M")
        date_str = dt.strftime("%Y-%m-%d")

        counter = int(row["Index relevé (litres)"])
        usage = int(row["Consommation du jour (litres)"])

        push_to_domoticz(counter, usage, date_str)

    print("\nTerminé.")


if __name__ == "__main__":
    main()
