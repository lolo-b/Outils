#!/usr/bin/env python3
"""
Récupère la consommation d'eau journalière L'Eau d'Île-de-France
et l'envoie dans Domoticz (Managed Counter).
"""

import asyncio
import requests
from datetime import date, timedelta
from pyeauidf import EauIDFClient

# ==================== CONFIGURATION ====================
EMAIL = "laurent.bauvineau@gmail.com"
PASSWORD = "0(n=r€ff)GNk"

# Domoticz
DOMOTICZ_URL = "http://192.168.1.75:8080"          # ou avec user:pass@ip:port
DOMOTICZ_IDX = 142                              # IDX de ton Managed Counter "Eau"
# =======================================================

def send_to_domoticz(idx: int, value_liters: float, date_str: str = None):
    """
    Envoie une valeur au compteur Domoticz.
    Pour un Managed Counter de type water avec Divider=1000 :
    - on envoie des litres (Domoticz les convertit en m³)
    """
    svalue = f"{value_liters:.0f}"
    if date_str:
        url = f"{DOMOTICZ_URL}/json.htm?type=command&param=udevice&idx={idx}&nvalue=0&svalue={svalue}&date={date_str}"
    else:
        url = f"{DOMOTICZ_URL}/json.htm?type=command&param=udevice&idx={idx}&nvalue=0&svalue={svalue}"

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "OK":
            print(f"  → Domoticz OK : {svalue} L")
        else:
            print(f"  → Erreur Domoticz : {data}")
    except Exception as e:
        print(f"  → Erreur envoi Domoticz : {e}")

async def main():
    print("Connexion à L'Eau d'Île-de-France...")
    async with EauIDFClient(EMAIL, PASSWORD) as client:
        await client.login()
        print("Connexion réussie.")

        # Récupère les 14 derniers jours
        end = date.today()
        start = end - timedelta(days=14)

        result = await client.get_daily_consumption(
            start_date=start,
            end_date=end
        )

        if not result.records:
            print("Aucune donnée de consommation trouvée.")
            return

        print(f"\n{len(result.records)} jour(s) récupéré(s) :\n")

        for record in sorted(result.records, key=lambda r: r.date):
            d = record.date.date() if hasattr(record.date, "date") else record.date
            liters = record.consumption_liters
            estimated = " (estimé)" if getattr(record, "is_estimated", False) else ""
            print(f"{d} : {liters:.0f} L{estimated}")

            # Envoi à Domoticz avec la date (pour l'historique)
            send_to_domoticz(DOMOTICZ_IDX, liters, d.isoformat())

        # Optionnel : envoi de la dernière valeur sans date (valeur courante)
        last = max(result.records, key=lambda r: r.date)
        print(f"\nDernière valeur (actuelle) : {last.consumption_liters:.0f} L")
        send_to_domoticz(DOMOTICZ_IDX, last.consumption_liters)

if __name__ == "__main__":
    asyncio.run(main())