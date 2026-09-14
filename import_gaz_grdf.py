#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import consommation gaz journalière GRDF (Gazpar) → Domoticz
Utilise PyGazpar + Managed Counter
version 1.0 - 2026-09-14
"""

import pygazpar
import requests
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time

# ==================== CONFIGURATION À MODIFIER ====================

# Identifiants GRDF (monespace.grdf.fr)
GRDF_USERNAME = "laurent.bauvineau@free.fr"
GRDF_PASSWORD = "8TXyz/b&?8dJ$("

# PCE (identifiant du compteur). Laisse None pour le détecter automatiquement
PCE = None          # Exemple : "GI12345678901234" ou None

# Domoticz
DOMOTICZ_URL  = "http://192.168.1.75:8080"   # ← change l'IP
DOMOTICZ_USER = ""                           # laisse vide si pas d'auth
DOMOTICZ_PASS = ""

IDX_M3  = 143       # ← IDX du Managed Counter "Gas" (m³)
IDX_KWH = 144       # ← IDX du Managed Counter "Energy" (kWh)

# Combien de jours récupérer (max ~1095)
NB_DAYS = 100

# Fichier qui mémorise la dernière date importée
STATE_FILE = Path(__file__).parent / "last_imported_date.txt"

# Multiplicateurs (Managed Counter attend souvent des valeurs *1000)
MULTIPLIER_M3  = 1000   # on envoie en litres
MULTIPLIER_KWH = 1000   # on envoie en Wh

# ==================================================================

def send_to_domoticz(idx: int, counter: float, usage: float, date_str: str) -> bool:
    """Envoie une valeur historique à un Managed Counter"""
    svalue = f"{int(round(counter))};{int(round(usage))};{date_str}"
    params = {
        "type": "command",
        "param": "udevice",
        "idx": idx,
        "nvalue": 0,
        "svalue": svalue
    }
    try:
        r = requests.get(
            f"{DOMOTICZ_URL}/json.htm",
            params=params,
            auth=(DOMOTICZ_USER, DOMOTICZ_PASS) if DOMOTICZ_USER else None,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            print(f"  ❌ Erreur Domoticz idx={idx} ({date_str}) : {data}")
            return False
        return True
    except Exception as e:
        print(f"  ❌ Exception Domoticz idx={idx} ({date_str}) : {e}")
        return False

def load_last_date() -> str | None:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip()
    return None

def save_last_date(date_str: str):
    STATE_FILE.write_text(date_str)

def main():

    # Connexion GRDF
    try:
        client = pygazpar.Client(
            pygazpar.JsonWebDataSource(
                username=GRDF_USERNAME,
                password=GRDF_PASSWORD
            )
        )
    except Exception as e:
        print(f"❌ Impossible de se connecter à GRDF : {e}")
        sys.exit(1)

    # Récupération du PCE
    pce = PCE
    if not pce:
        try:
            pces = client.get_pce_identifiers()
            if not pces:
                print("❌ Aucun PCE trouvé sur le compte")
                sys.exit(1)
            pce = pces[0]
            print(f"PCE détecté automatiquement : {pce}")
            if len(pces) > 1:
                print(f"  (autres PCE disponibles : {pces[1:]})")
        except Exception as e:
            print(f"❌ Erreur récupération PCE : {e}")
            sys.exit(1)
    else:
        print(f"PCE utilisé : {pce}")

    # Téléchargement des données
    print(f"Téléchargement des {NB_DAYS} derniers jours...")
    try:
        data = client.load_since(
            pce_identifier=pce,
            last_n_days=NB_DAYS,
            frequencies=[pygazpar.Frequency.DAILY]
        )
    except Exception as e:
        print(f"❌ Erreur téléchargement données : {e}")
        sys.exit(1)

    daily = data.get("daily", [])
    if not daily:
        print("Aucune donnée journalière reçue")
        sys.exit(0)

    # Tri par date croissante
    daily.sort(key=lambda x: datetime.strptime(x["time_period"], "%d/%m/%Y"))

    last_imported = load_last_date()
    # print(f"Dernière date déjà importée : {last_imported or 'aucune (premier import)'}")
    # print(f"Nombre de jours reçus de GRDF : {len(daily)}")

    to_import = []
    for day in daily:
        # Conversion date
        dt = datetime.strptime(day["time_period"], "%d/%m/%Y")
        date_iso = dt.strftime("%Y-%m-%d")

        if last_imported is None or date_iso > last_imported:
            to_import.append({
                "date": date_iso,
                "index_m3": float(day.get("end_index_m3") or 0),
                "conso_m3": float(day.get("volume_m3") or 0),
                "conso_kwh": float(day.get("energy_kwh") or 0),
                "coeff": float(day.get("converter_factor_kwh/m3") or 0)
            })

    if not to_import:
        print("✅ Aucune nouvelle journée à importer.")
        return

    # print(f"→ {len(to_import)} nouvelle(s) journée(s) à importer\n")

    success = 0
    for row in to_import:
        counter_m3 = row["index_m3"] * MULTIPLIER_M3
        usage_m3   = row["conso_m3"] * MULTIPLIER_M3

        # Pour le kWh on n'a pas d'index cumulatif fiable → on met -1
        counter_kwh = -1
        usage_kwh   = row["conso_kwh"] * MULTIPLIER_KWH

        # print(f"  {row['date']}  →  {row['conso_m3']:.2f} m³  /  {row['conso_kwh']:.0f} kWh  (coeff {row['coeff']:.2f})")

        ok1 = send_to_domoticz(IDX_M3,  counter_m3,  usage_m3,  row["date"])
        ok2 = send_to_domoticz(IDX_KWH, counter_kwh, usage_kwh, row["date"])

        if ok1 and ok2:
            success += 1
            save_last_date(row["date"])
            time.sleep(0.3)  # petite pause pour ne pas saturer Domoticz
        else:
            print("  → arrêt suite à une erreur")
            break

    # print(f"\nTerminé : {success}/{len(to_import)} journée(s) importée(s) avec succès.")
    if success:
        #print(f"Dernière date enregistrée : {to_import[success-1]['date']}")
        pass
if __name__ == "__main__":
    main()