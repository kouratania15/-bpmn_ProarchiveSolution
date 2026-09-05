# -*- coding: utf-8 -*-
"""
Script de test automatisé v2 pour l'agent BPMN — NOUVEAUX SCÉNARIOS.

Ces tests couvrent les MÊMES notions BPMN que le premier fichier de test, mais avec des
contextes métier différents (recrutement, hôpital, e-commerce, banque, logistique...).
Objectif : vérifier que les corrections tiennent aussi sur des formulations et des
domaines que l'agent n'a jamais vus, pas seulement sur les textes déjà utilisés pour
le déboguer (qui risquent d'être "sur-appris" par le prompt).

Usage :
    (venv) PS C:\\Users\\laptop spirit\\Desktop\\bpmn-agent> python run_all_tests_v2.py

Placez ce fichier à la racine du projet, à côté du dossier "scripts".
"""

import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

PIPELINE_SCRIPT = Path("scripts") / "pipeline.py"
OUTPUT_DIR = Path("tests_output_v2")
REPORT_FILE = Path("rapport_tests_v2.md")

# ----------------------------------------------------------------------
# Nouveaux tests : (nom_fichier, notion_testee, texte_du_processus)
# ----------------------------------------------------------------------
TESTS = [
    
    (
        "v2_test05_gateway_inclusif",
        "Gateway inclusif (OR)",
        "Un locataire signale un problème dans son appartement. Si le problème concerne la "
        "plomberie, le plombier intervient. Si le problème concerne l'électricité, l'électricien "
        "intervient. Une fois les interventions nécessaires terminées, le gestionnaire clôture le "
        "signalement."
    ),
    
    (
        "v2_test07_multi_acteurs",
        "Plusieurs acteurs / lanes",
        "Un patient consulte son médecin traitant. Le médecin prescrit une analyse de sang. Le "
        "laboratoire réalise l'analyse. Si les résultats sont anormaux, le spécialiste examine le "
        "patient. Sinon, le médecin traitant informe le patient que tout est normal."
    ),
    
    (
        "v2_test11_conditions_combinees",
        "Conditions combinées (3 branches)",
        "Une banque étudie une demande de carte de crédit. L'agent vérifie si le revenu est "
        "suffisant et si l'historique de crédit est bon. Si les deux conditions sont remplies, "
        "l'agent approuve la carte avec un plafond élevé. Si une seule condition est remplie, "
        "l'agent approuve la carte avec un plafond réduit. Si aucune condition n'est remplie, "
        "l'agent rejette la demande."
    ),
  
    
 
    
    (
        "v2_test19_escalation",
        "Événement d'escalade",
        "Un technicien traite un incident informatique. Si l'incident n'est pas résolu après 4 "
        "heures, une escalade est déclenchée vers l'expert senior, qui prend en charge le dossier "
        "en parallèle sans interrompre les tentatives du technicien. Sinon, le technicien résout "
        "l'incident seul."
    ),
    
    (
        "v2_test22_conditional_event",
        "Événement conditionnel",
        "Un client place une commande en rupture de stock. La commande reste en attente jusqu'à ce "
        "que le stock du produit soit à nouveau supérieur à zéro. Dès que cette condition devient "
        "vraie, l'entrepôt peut préparer l'expédition de la commande."
    ),
    
]


def run_test(index, total, name, notion, texte):
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{name}.bpmn"

    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}] {name}  —  {notion}")
    print(f"{'=' * 70}")

    cmd = [sys.executable, str(PIPELINE_SCRIPT), texte, "--out", str(out_path)]

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        duration = time.time() - start
        success = result.returncode == 0 and out_path.exists()
        status = "✅ OK" if success else "❌ ÉCHEC"
        print(status, f"({duration:.1f}s)")
        if not success:
            print("--- STDOUT (fin) ---")
            print("\n".join(result.stdout.splitlines()[-15:]))
            print("--- STDERR (fin) ---")
            print("\n".join(result.stderr.splitlines()[-15:]))
        return {
            "name": name,
            "notion": notion,
            "success": success,
            "duration": duration,
            "stdout_tail": "\n".join(result.stdout.splitlines()[-20:]),
            "stderr_tail": "\n".join(result.stderr.splitlines()[-20:]),
            "out_path": str(out_path) if success else None,
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        print("⏱️ TIMEOUT (>300s)")
        return {
            "name": name, "notion": notion, "success": False, "duration": duration,
            "stdout_tail": "", "stderr_tail": "Timeout dépassé (300s)", "out_path": None,
        }
    except Exception as e:
        duration = time.time() - start
        print(f"💥 EXCEPTION : {e}")
        return {
            "name": name, "notion": notion, "success": False, "duration": duration,
            "stdout_tail": "", "stderr_tail": str(e), "out_path": None,
        }


def write_report(results):
    total = len(results)
    ok = sum(1 for r in results if r["success"])
    ko = total - ok

    lines = []
    lines.append(f"# Rapport de tests BPMN v2 (nouveaux scénarios) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"**Résultat global : {ok}/{total} réussis, {ko}/{total} échoués**")
    lines.append("")
    lines.append("| # | Test | Notion testée | Statut | Durée |")
    lines.append("|---|------|----------------|--------|-------|")
    for i, r in enumerate(results, 1):
        statut = "✅ OK" if r["success"] else "❌ ÉCHEC"
        lines.append(f"| {i} | `{r['name']}` | {r['notion']} | {statut} | {r['duration']:.1f}s |")

    lines.append("")
    lines.append("## Détails des échecs")
    lines.append("")
    any_failure = False
    for r in results:
        if not r["success"]:
            any_failure = True
            lines.append(f"### {r['name']} — {r['notion']}")
            lines.append("")
            lines.append("```")
            lines.append(r["stderr_tail"] or r["stdout_tail"] or "(aucune sortie capturée)")
            lines.append("```")
            lines.append("")
    if not any_failure:
        lines.append("Aucun échec 🎉")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def main():
    if not PIPELINE_SCRIPT.exists():
        print(f"❌ Introuvable : {PIPELINE_SCRIPT}")
        print("Lancez ce script depuis la racine du projet bpmn-agent (là où se trouve le dossier 'scripts').")
        sys.exit(1)

    total = len(TESTS)
    print(f"Lancement de {total} nouveaux tests BPMN (scénarios v2)...\n")

    results = []
    global_start = time.time()
    for i, (name, notion, texte) in enumerate(TESTS, 1):
        results.append(run_test(i, total, name, notion, texte))
    global_duration = time.time() - global_start

    write_report(results)

    ok = sum(1 for r in results if r["success"])
    print(f"\n{'=' * 70}")
    print(f"TERMINÉ : {ok}/{total} tests réussis en {global_duration/60:.1f} min")
    print(f"Fichiers .bpmn générés dans : {OUTPUT_DIR.resolve()}")
    print(f"Rapport détaillé : {REPORT_FILE.resolve()}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()