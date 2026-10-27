import os
import re
import csv
import glob
import json
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- 1. CONFIGURATION ---
BASE_IDF = "maison_sim.idf"
BASE_EPW = "MAR_RK_Rabat-Sale.AP.601350_TMYx.2009-2023.epw"

TEMP_EPW = "meteo_temps_reel.epw"
WORK_DIR = "runs_40_scenarios"
FINAL_CSV = "resultats_40_scenarios.csv"

LATITUDE = 34.051
LONGITUDE = -6.752
PAST_DAYS = 3      # 3 jours passés pour caler l'inertie thermique des murs
FORECAST_DAYS = 3  # Aujourd'hui + 2 jours de prévisions (72 heures futures)

# Mettre sur True si vous voulez uniquement les heures >= heure actuelle d'aujourd'hui
# Mettre sur False pour avoir toute la journée d'aujourd'hui (01:00 à 24:00) + les 2 jours suivants
FILTRER_APRES_HEURE_ACTUELLE = False

ANGLES = list(range(0, 360, 18))  # 20 positions : 0, 18, 36, ..., 342 degrés
ETATS_FENETRES = ["Ferme", "Ouvert"]

chemins_ep = glob.glob(r"C:\EnergyPlus*\energyplus.exe") + glob.glob(r"C:\openstudio*\EnergyPlus\energyplus.exe")
if not chemins_ep:
    raise FileNotFoundError("Impossible de trouver energyplus.exe sur C:\\")
ENERGYPLUS_EXE = chemins_ep[0]


# --- 2. MISE À JOUR AUTOMATIQUE DE LA MÉTÉO (BASÉE SUR AUJOURD'HUI) ---
def mettre_a_jour_epw_temps_reel():
    maintenant = datetime.now()
    print(f"1. Date système détectée : {maintenant.strftime('%d/%m/%Y %H:%M')}")
    print("   Récupération météo Open-Meteo (3 jours passés + Aujourd'hui et 2 jours futurs)...")

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m,dew_point_2m,relative_humidity_2m,surface_pressure,"
        f"shortwave_radiation,direct_normal_irradiance,diffuse_radiation,"
        f"wind_speed_10m,wind_direction_10m,cloud_cover"
        f"&wind_speed_unit=ms"
        f"&past_days={PAST_DAYS}&forecast_days={FORECAST_DAYS}"
        f"&timezone=auto"
    )

    req = urllib.request.urlopen(url)
    data = json.loads(req.read().decode("utf-8"))["hourly"]

    meteo_api = {}
    dates_sim = []

    for i, ts in enumerate(data["time"]):
        dt = datetime.fromisoformat(ts)
        dates_sim.append(dt)
        cle = (dt.month, dt.day, dt.hour + 1)
        meteo_api[cle] = {
            "year": dt.year,
            "temp": data["temperature_2m"][i],
            "dew": data["dew_point_2m"][i],
            "rh": int(round(data["relative_humidity_2m"][i])),
            "press": int(round(data["surface_pressure"][i] * 100)),
            "ghi": int(round(data["shortwave_radiation"][i])),
            "dni": int(round(data["direct_normal_irradiance"][i])),
            "dhi": int(round(data["diffuse_radiation"][i])),
            "wdir": int(round(data["wind_direction_10m"][i])),
            "wspd": data["wind_speed_10m"][i],
            "cloud": int(round(data["cloud_cover"][i] / 10.0))
        }

    start_dt, end_dt = dates_sim[0], dates_sim[-1]
    today_dt = start_dt + timedelta(days=PAST_DAYS)
    hier_dt = today_dt - timedelta(days=1)

    print(f"   -> Fenêtre d'initialisation (Passé) : du {start_dt.strftime('%d/%m')} au {hier_dt.strftime('%d/%m')}")
    print(f"   -> Fenêtre des scénarios (Futur)    : du {today_dt.strftime('%d/%m')} au {end_dt.strftime('%d/%m')}")

    with open(BASE_EPW, "r", encoding="utf-8", errors="ignore") as f:
        lignes_epw = f.readlines()

    nouvelles_lignes = lignes_epw[:8]
    for ligne in lignes_epw[8:]:
        cols = ligne.strip().split(",")
        if len(cols) < 35:
            nouvelles_lignes.append(ligne)
            continue
        mois, jour, heure = int(cols[1]), int(cols[2]), int(cols[3])
        if (mois, jour, heure) in meteo_api:
            m = meteo_api[(mois, jour, heure)]
            cols[0] = str(m["year"])
            cols[6] = f"{m['temp']:.1f}"
            cols[7] = f"{m['dew']:.1f}"
            cols[8] = str(m["rh"])
            cols[9] = str(m["press"])
            cols[12] = "9999"
            cols[13] = str(m["ghi"])
            cols[14] = str(m["dni"])
            cols[15] = str(m["dhi"])
            cols[16], cols[17], cols[18], cols[19] = "999999", "999999", "999999", "9999"
            cols[20] = str(m["wdir"])
            cols[21] = f"{m['wspd']:.1f}"
            cols[22] = str(m["cloud"])
            cols[23] = str(m["cloud"])
            nouvelles_lignes.append(",".join(cols) + "\n")
        else:
            nouvelles_lignes.append(ligne)

    with open(TEMP_EPW, "w", encoding="utf-8") as f:
        f.writelines(nouvelles_lignes)

    return start_dt, hier_dt, today_dt, end_dt, maintenant


# --- 3. GÉNÉRATION DU MODÈLE IDF POUR CHAQUE SCÉNARIO ---
def generer_idf_scenario(idf_base, angle, etat_fenetre, start_dt, hier_dt, end_dt):
    idf = idf_base

    # A. Désactiver les SizingPeriods (DesignDays) pour accélérer la simulation
    idf = re.sub(
        r"(SimulationControl\s*,\s*No\s*,\s*[^;]*?\s*No\s*,\s*[^;]*?\s*No\s*,\s*[^;]*?)\bYes\b",
        r"\1No",
        idf,
        flags=re.IGNORECASE
    )

    # B. Désactiver l'AirflowNetwork partiel qui faisait planter scen_02_angle_000_Ouvert
    idf = re.sub(
        r"MultizoneWithoutDistribution",
        "NoMultizoneOrDistribution",
        idf,
        flags=re.IGNORECASE
    )

    # C. Mettre à jour le RunPeriod avec les dates exactes du jour
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

    idf = re.sub(r"RunPeriod\s*,[^;]+;", nouveau_runperiod, idf, flags=re.IGNORECASE)

    # D. Injecter l'angle de rotation dans l'objet Building (North Axis)
    idf = re.sub(
        r"Building\s*,\s*Building 1\s*,\s*,",
        f"Building,\n  Building 1,\n  {angle},",
        idf,
        flags=re.IGNORECASE
    )

    # E. Si scénario "Ouvert", ouvrir les fenêtres UNIQUEMENT à partir d'aujourd'hui (le futur)
    if etat_fenetre == "Ouvert":
        angle_est = (90 + angle) % 360
        angle_sud = (180 + angle) % 360
        angle_ouest = (270 + angle) % 360

        # Planning : 0 (Fermé) jusqu'à hier soir 24:00, puis 1 (Ouvert) à partir d'aujourd'hui
        planning_futur = f"""
Schedule:Compact,
  Planning_Ouverture_Futur,               !- Name
  Fraction,                               !- Schedule Type Limits Name
  Through: {hier_dt.month}/{hier_dt.day}, !- Jours passés
  For: AllDays,
  Until: 24:00, 0.0,                      !- Fenêtres fermées dans le passé
  Through: 12/31,                         !- Aujourd'hui et jours futurs
  For: AllDays,
  Until: 24:00, 1.0;                      !- Fenêtres ouvertes dans le futur
"""
        zones_fenetres = [
            ("Vent_Z2", "Thermal Zone 2", 0.735, angle_ouest),
            ("Vent_Z3", "Thermal Zone 3", 0.735, angle_est),
            ("Vent_Z4", "Thermal Zone 4", 0.735, angle_est),
            ("Vent_Z5", "Thermal Zone 5", 0.735, angle_ouest),
            ("Vent_Z6", "Thermal Zone 6", 1.500, angle_sud),
        ]

        blocs_vent = [planning_futur]
        for nom_v, zone_v, surf_v, ang_v in zones_fenetres:
            blocs_vent.append(f"""
ZoneVentilation:WindandStackOpenArea,
  {nom_v},                                !- Name
  {zone_v},                               !- Zone or Space Name
  {surf_v},                               !- Opening Area {{m2}}
  Planning_Ouverture_Futur,               !- Opening Area Fraction Schedule Name
  ,                                       !- Opening Effectiveness {{dimensionless}}
  {ang_v},                                !- Effective Angle {{deg}}
  1.5,                                    !- Height Difference {{m}}
  ,                                       !- Discharge Coefficient for Opening
  -100.0,                                 !- Minimum Indoor Temperature {{C}}
  ,                                       !- Minimum Indoor Temperature Schedule Name
  100.0,                                  !- Maximum Indoor Temperature {{C}}
  ,                                       !- Maximum Indoor Temperature Schedule Name
  -100.0,                                 !- Delta Temperature {{deltaC}}
  ,                                       !- Delta Temperature Schedule Name
  -100.0,                                 !- Minimum Outdoor Temperature {{C}}
  ,                                       !- Minimum Outdoor Temperature Schedule Name
  100.0,                                  !- Maximum Outdoor Temperature {{C}}
  ,                                       !- Maximum Outdoor Temperature Schedule Name
  40.0;                                   !- Maximum Wind Speed {{m/s}}
""")
        idf += "\n" + "\n".join(blocs_vent)

    return idf


# --- 4. EXÉCUTION ET FILTRAGE DES LIGNES FUTURES ---
def est_dans_le_futur(date_time_str, today_dt, maintenant):
    """Filtre les lignes CSV d'EnergyPlus pour exclure les 3 jours passés."""
    # Format EnergyPlus : " 10/25  01:00:00"
    texte = date_time_str.strip()
    try:
        partie_date, partie_heure = texte.split()
        mois, jour = map(int, partie_date.split("/"))
        heure = int(partie_heure.split(":")[0])  # 1 à 24
        dt_ligne = datetime(today_dt.year, mois, jour) + timedelta(hours=heure - 1)
        if FILTRER_APRES_HEURE_ACTUELLE:
            return dt_ligne >= maintenant.replace(minute=0, second=0, microsecond=0)
        return dt_ligne.date() >= today_dt.date()
    except Exception:
        return True


def executer_un_scenario(scenario_id, angle, etat_fenetre, idf_base, start_dt, hier_dt, today_dt, end_dt, maintenant):
    nom_scen = f"scen_{scenario_id:02d}_angle_{angle:03d}_{etat_fenetre}"
    dossier_scen = os.path.join(WORK_DIR, nom_scen)
    os.makedirs(dossier_scen, exist_ok=True)

    chemin_idf = os.path.join(dossier_scen, f"{nom_scen}.idf")
    with open(chemin_idf, "w", encoding="utf-8") as f:
        f.write(generer_idf_scenario(idf_base, angle, etat_fenetre, start_dt, hier_dt, end_dt))

    commande = [
        ENERGYPLUS_EXE,
        "-w", os.path.abspath(TEMP_EPW),
        "-d", dossier_scen,
        "-r",
        chemin_idf
    ]
    res = subprocess.run(commande, capture_output=True, text=True)
    chemin_csv = os.path.join(dossier_scen, "eplusout.csv")

    if res.returncode != 0 or not os.path.exists(chemin_csv):
        err_file = os.path.join(dossier_scen, "eplusout.err")
        err_msg = open(err_file, encoding="utf-8", errors="ignore").read() if os.path.exists(err_file) else res.stderr
        raise RuntimeError(f"Échec du scénario {nom_scen} :\n{err_msg}")

    with open(chemin_csv, "r", encoding="utf-8") as f:
        lecteur = list(csv.reader(f))
        entete = lecteur[0]
        # On garde uniquement les lignes à partir d'aujourd'hui (le futur)
        lignes_futures = [
            row for row in lecteur[1:]
            if row and est_dans_le_futur(row[0], today_dt, maintenant)
        ]

    return scenario_id, angle, etat_fenetre, entete, lignes_futures


if __name__ == "__main__":
    print(f"Moteur utilisé : {ENERGYPLUS_EXE}")
    start_dt, hier_dt, today_dt, end_dt, maintenant = mettre_a_jour_epw_temps_reel()

    with open(BASE_IDF, "r", encoding="utf-8", errors="ignore") as f:
        idf_base_content = f.read()

    # S'assurer que les variables de sortie sont présentes dans maison_sim.idf
    if "Output:Variable" not in idf_base_content:
        idf_base_content += """
Output:Variable,*,Zone Mean Air Temperature,Hourly;
Output:Variable,*,Zone Air Relative Humidity,Hourly;
Output:Variable,*,Zone Mean Radiant Temperature,Hourly;
"""

    scenarios = []
    scen_id = 1
    for angle in ANGLES:
        for etat in ETATS_FENETRES:
            scenarios.append((scen_id, angle, etat))
            scen_id += 1

    print(f"\n2. Lancement des {len(scenarios)} scénarios en parallèle...")
    resultats_ordonnes = {}
    entete_colonnes = None

    # Limiter à 6 processus simultanés pour éviter les blocages disque sous Windows
    nb_workers = min(6, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=nb_workers) as executor:
        futures = {
            executor.submit(
                executer_un_scenario,
                s_id, ang, etat, idf_base_content, start_dt, hier_dt, today_dt, end_dt, maintenant
            ): (s_id, ang, etat)
            for s_id, ang, etat in scenarios
        }
        for idx, future in enumerate(as_completed(futures), 1):
            s_id, ang, etat, entete, lignes = future.result()
            entete_colonnes = entete
            resultats_ordonnes[s_id] = (ang, etat, lignes)
            print(f" [{idx:02d}/40] OK -> Angle: {ang:3d}° | Fenêtres: {etat:6s} ({len(lignes)}h futures extraites)")

    print(f"\n3. Écriture du fichier unique '{FINAL_CSV}'...")
    with open(FINAL_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(["Scenario_ID", "Angle_deg", "Fenetres"] + entete_colonnes)

        for s_id in sorted(resultats_ordonnes.keys()):
            ang, etat, lignes = resultats_ordonnes[s_id]
            for ligne in lignes:
                writer.writerow([s_id, ang, etat] + ligne)

    # Suppression automatique du dossier temporaire runs_40_scenarios
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print(f"\nTerminé avec succès ! Ouvrez '{FINAL_CSV}' : il commence directement au {today_dt.strftime('%m/%d')}.")