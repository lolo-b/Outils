#!/usr/bin/env python3
"""
Récupère la consommation d'eau journalière L'Eau d'Île-de-France
et l'envoie dans Domoticz (Managed Counter).

Seules les erreurs sont écrites dans les logs (stderr).

Version 2026.11.17
"""

import asyncio
import sys
from datetime import date, timedelta

import requests
from pyeauidf import EauIDFClient

# ==================== CONFIGURATION ====================
EMAIL = "laurent.bauvineau@gmail.com"
PASSWORD = "0(n=r€ff)GNk"

# Domoticz
DOMOTICZ_URL = "http://192.168.1.75:8080"          # ou avec user:pass@ip:port
DOMOTICZ_USER = None                        # ou "user" si auth activée
DOMOTICZ_PASSWORD = None                    # ou "password"
DOMOTICZ_IDX = 142                              # IDX de ton Managed Counter "Eau"

# Nombre de jours d'historique à récupérer
NB_JOURS = 15
# =======================================================

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

    try:
        resp = requests.get(
            f"{DOMOTICZ_URL}/json.htm",
            params=params,
            auth=auth,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            print(f"ERREUR Domoticz pour {date_str} : {data}", file=sys.stderr)
    except Exception as e:
        print(f"Erreur envoi Domoticz ({date_str}) : {e}", file=sys.stderr)


async def main() -> None:
    try:
        async with EauIDFClient(EMAIL, PASSWORD) as client:
            await client.login()

            end = date.today()
            start = end - timedelta(days=NB_JOURS)

            result = await client.get_daily_consumption(
                start_date=start,
                end_date=end,
            )

            if not result.records:
                return  # Pas de données : pas considéré comme une erreur

            for record in sorted(result.records, key=lambda r: r.date):
                # Date
                d = record.date.date() if hasattr(record.date, "date") else record.date
                date_str = d.isoformat()

                # Consommation du jour (litres)
                usage_liters = int(round(record.consumption_liters))

                # Index cumulé
                if record.meter_reading is None:
                    print(f"{date_str} : pas d'index compteur, ignoré", file=sys.stderr)
                    continue

                # Si la valeur est petite (< 100000), on suppose des m³ → litres
                if record.meter_reading < 100000:
                    counter_liters = int(round(record.meter_reading * 1000))
                else:
                    # Déjà en litres
                    counter_liters = int(round(record.meter_reading))

                push_to_domoticz(counter_liters, usage_liters, date_str)
    except Exception as e:
        print(f"Erreur L'Eau d'Île-de-France : {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

