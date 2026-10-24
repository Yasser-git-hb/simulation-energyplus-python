import os
import glob
import subprocess

# 1. Vos fichiers présents dans le dossier
idf_file = "maison_sim.idf"
epw_file = "MAR_RK_Rabat-Sale.AP.601350_TMYx.2009-2023.epw"
output_dir = "resultats_csv"

# 2. Détection automatique de energyplus.exe sur votre disque C:
chemins_possibles = (
    glob.glob(r"C:\EnergyPlus*\energyplus.exe") +
    glob.glob(r"C:\openstudio*\EnergyPlus\energyplus.exe")
)

if not chemins_possibles:
    raise FileNotFoundError("Introuvable automatiquement : indiquez le chemin de energyplus.exe manuellement.")

energyplus_exe = chemins_possibles[0]
print(f"Moteur utilisé : {energyplus_exe}")

# 3. Lancement de la simulation avec l'option "-r" (qui génère le CSV)
commande = [
    energyplus_exe,
    "-w", epw_file,
    "-d", output_dir,
    "-r",              # Convertit automatiquement les résultats en fichier .csv
    idf_file
]

print("Simulation en cours...")
resultat = subprocess.run(commande, capture_output=True, text=True)

# 4. Vérification des fichiers CSV générés
if resultat.returncode == 0:
    print(f"Terminé avec succès ! Ouvrez le dossier '{output_dir}' :")
    for f in os.listdir(output_dir):
        if f.endswith(".csv"):
            print(f" -> {f}")
else:
    print("Erreur lors de la simulation :", resultat.stderr)