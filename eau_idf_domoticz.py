#!/usr/bin/env python3
"""
Récupère la consommation d'eau journalière L'Eau d'Île-de-France
et l'envoie dans Domoticz (Managed Counter).
"""

import asyncio
import requests
import sys
from datetime import date, timedelta
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
            print(f"  → ERREUR Domoticz pour {date_str} : {data}", file=sys.stderr)
        else:
            print(f"  → {date_str} : index={counter_liters} L, conso={usage_liters} L : OK")
    except Exception as e:
        print(f"  → Erreur envoi Domoticz ({date_str}) : {e}", file=sys.stderr)
        
async def main() -> None:
    print("Connexion à L'Eau d'Île-de-France...")
    async with EauIDFClient(EMAIL, PASSWORD) as client:
        await client.login()
        print("Connexion réussie.")

        end = date.today()
        start = end - timedelta(days=NB_JOURS)

        result = await client.get_daily_consumption(
            start_date=start,
            end_date=end,
        )

        if not result.records:
            print("Aucune donnée de consommation trouvée.")
            return

        print(f"\n{len(result.records)} jour(s) récupéré(s). Envoi vers Domoticz (idx={DOMOTICZ_IDX})...\n")

        for record in sorted(result.records, key=lambda r: r.date):
            # Date
            d = record.date.date() if hasattr(record.date, "date") else record.date
            date_str = d.isoformat()

            # Consommation du jour (litres)
            usage_liters = int(round(record.consumption_liters))

            # Index cumulé : pyeauidf renvoie meter_reading en m³ → conversion en litres
            # (comme dans ton CSV fournisseur qui est en litres)
            if record.meter_reading is not None:
                # Si la valeur est petite (< 100000), on suppose des m³
                if record.meter_reading < 100000:
                    counter_liters = int(round(record.meter_reading * 1000))
                else:
                    # Déjà en litres
                    counter_liters = int(round(record.meter_reading))
            else:
                print(f"  → {date_str} : pas d'index compteur, ignoré")
                continue

            estimated = " (estimé)" if getattr(record, "is_estimated", False) else ""
            print(f"{date_str} : {usage_liters} L{estimated}")
            push_to_domoticz(counter_liters, usage_liters, date_str)

        print("\nTerminé.")

if __name__ == "__main__":
    asyncio.run(main())