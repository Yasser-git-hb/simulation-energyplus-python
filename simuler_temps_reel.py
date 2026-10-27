import os
import re
import glob
import json
import urllib.request
import subprocess
from datetime import datetime

# --- 1. CONFIGURATION ---
BASE_IDF = "maison_sim.idf"
BASE_EPW = "MAR_RK_Rabat-Sale.AP.601350_TMYx.2009-2023.epw"

TEMP_EPW = "meteo_temps_reel.epw"
TEMP_IDF = "maison_temps_reel.idf"
OUTPUT_DIR = "resultats_temps_reel"

# Coordonnées de Rabat-Salé (extraites de votre station EPW)
LATITUDE = 34.05
LONGITUDE = -6.75
PAST_DAYS = 3      # 3 jours passés (pour initialiser l'inertie thermique)
FORECAST_DAYS = 3  # Aujourd'hui + 2 jours de prévisions futures

# --- 2. APPEL API OPEN-METEO ---
print("1. Récupération des données météo (Passé + Prévisions) depuis Open-Meteo...")
url = (
    f"https://api.open-meteo.com/v1/forecast?"
    f"latitude={LATITUDE}&longitude={LONGITUDE}"
    f"&hourly=temperature_2m,dew_point_2m,relative_humidity_2m,surface_pressure,"
    f"shortwave_radiation,direct_normal_irradiance,diffuse_radiation,"
    f"wind_speed_10m,wind_direction_10m,cloud_cover"
    f"&wind_speed_unit=ms"
    f"&past_days={PAST_DAYS}&forecast_days={FORECAST_DAYS}"
    f"&timezone=GMT"
)

req = urllib.request.urlopen(url)
data = json.loads(req.read().decode("utf-8"))["hourly"]

# Construction d'un dictionnaire indexé par (Mois, Jour, Heure_EPW de 1 à 24)
meteo_api = {}
dates_sim = []

for i, ts in enumerate(data["time"]):
    dt = datetime.fromisoformat(ts)
    dates_sim.append(dt)
    
    # Dans un fichier EPW, les heures vont de 1 à 24 (00:00 -> Heure 1)
    cle = (dt.month, dt.day, dt.hour + 1)
    meteo_api[cle] = {
        "year": dt.year,
        "temp": data["temperature_2m"][i],
        "dew": data["dew_point_2m"][i],
        "rh": int(round(data["relative_humidity_2m"][i])),
        "press": int(round(data["surface_pressure"][i] * 100)),  # hPa -> Pa (x100)
        "ghi": int(round(data["shortwave_radiation"][i])),
        "dni": int(round(data["direct_normal_irradiance"][i])),
        "dhi": int(round(data["diffuse_radiation"][i])),
        "wdir": int(round(data["wind_direction_10m"][i])),
        "wspd": data["wind_speed_10m"][i],
        "cloud": int(round(data["cloud_cover"][i] / 10.0))       # % -> dixièmes (/10)
    }

start_dt, end_dt = dates_sim[0], dates_sim[-1]
print(f"   Période récupérée : du {start_dt.strftime('%d/%m/%Y')} au {end_dt.strftime('%d/%m/%Y')} ({len(dates_sim)} heures)")

# --- 3. CRÉATION DU FICHIER EPW TEMPS RÉEL ---
print(f"2. Génération du fichier '{TEMP_EPW}'...")
with open(BASE_EPW, "r", encoding="utf-8", errors="ignore") as f:
    lignes_epw = f.readlines()

nouvelles_lignes = lignes_epw[:8]  # Conservation des 8 lignes d'en-tête officielles

for ligne in lignes_epw[8:]:
    cols = ligne.strip().split(",")
    if len(cols) < 35:
        nouvelles_lignes.append(ligne)
        continue
        
    mois, jour, heure = int(cols[1]), int(cols[2]), int(cols[3])
    if (mois, jour, heure) in meteo_api:
        m = meteo_api[(mois, jour, heure)]
        cols[0] = str(m["year"])
        cols[6] = f"{m['temp']:.1f}"     # Température sèche (°C)
        cols[7] = f"{m['dew']:.1f}"      # Point de rosée (°C)
        cols[8] = str(m["rh"])           # Humidité relative (%)
        cols[9] = str(m["press"])        # Pression atmosphérique (Pa)
        cols[12] = "9999"                # Infrarouge horizontal -> calculé par EnergyPlus
        cols[13] = str(m["ghi"])         # GHI (Wh/m²)
        cols[14] = str(m["dni"])         # DNI (Wh/m²)
        cols[15] = str(m["dhi"])         # DHI (Wh/m²)
        cols[16] = "999999"              # Illuminance manquante standard
        cols[17] = "999999"
        cols[18] = "999999"
        cols[19] = "9999"
        cols[20] = str(m["wdir"])        # Direction du vent (degrés)
        cols[21] = f"{m['wspd']:.1f}"    # Vitesse du vent (m/s)
        cols[22] = str(m["cloud"])       # Couverture nuageuse totale (dixièmes)
        cols[23] = str(m["cloud"])       # Couverture nuageuse opaque (dixièmes)
        nouvelles_lignes.append(",".join(cols) + "\n")
    else:
        nouvelles_lignes.append(ligne)

with open(TEMP_EPW, "w", encoding="utf-8") as f:
    f.writelines(nouvelles_lignes)

# --- 4. MISE À JOUR DU RUNPERIOD DANS LE FICHIER IDF ---
print(f"3. Configuration de la période de simulation dans '{TEMP_IDF}'...")
with open(BASE_IDF, "r", encoding="utf-8", errors="ignore") as f:
    idf_content = f.read()

nouveau_runperiod = f"""RunPeriod,
  Simulation_Temps_Reel,   !- Name
  {start_dt.month},                        !- Begin Month
  {start_dt.day},                        !- Begin Day of Month
  ,                        !- Begin Year
  {end_dt.month},                        !- End Month
  {end_dt.day},                        !- End Day of Month
  ,                        !- End Year
  ,                        !- Day of Week for Start Day
  Yes,                     !- Use Weather File Holidays and Special Days
  Yes,                     !- Use Weather File Daylight Saving Period
  No,                      !- Apply Weekend Holiday Rule
  Yes,                     !- Use Weather File Rain Indicators
  Yes;                     !- Use Weather File Snow Indicators"""

# Remplace le bloc RunPeriod existant par notre fenêtre temps réel
idf_modifie = re.sub(r"RunPeriod\s*,[^;]+;", nouveau_runperiod, idf_content, flags=re.IGNORECASE)

with open(TEMP_IDF, "w", encoding="utf-8") as f:
    f.write(idf_modifie)

# --- 5. EXÉCUTION D'ENERGYPLUS ---
chemins_ep = glob.glob(r"C:\EnergyPlus*\energyplus.exe") + glob.glob(r"C:\openstudio*\EnergyPlus\energyplus.exe")
energyplus_exe = chemins_ep[0]

print(f"4. Lancement d'EnergyPlus ({energyplus_exe})...")
commande = [
    energyplus_exe,
    "-w", TEMP_EPW,
    "-d", OUTPUT_DIR,
    "-r",
    TEMP_IDF
]

res = subprocess.run(commande, capture_output=True, text=True)

if res.returncode == 0:
    print(f"\nSuccès ! Le tableau CSV des {len(dates_sim)} heures (passé + prévisions) est prêt dans :")
    print(f" -> {os.path.join(OUTPUT_DIR, 'eplusout.csv')}")
else:
    print("Erreur lors de la simulation :", res.stderr)