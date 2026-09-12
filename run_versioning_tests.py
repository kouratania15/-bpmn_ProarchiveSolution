# -*- coding: utf-8 -*-
"""
Script de test automatisé — Fonctionnalité de versioning (amendements incrémentaux).

Pour chaque scénario : une création initiale (--process-name) suivie de DEUX
amendements successifs (--process-id), le second s'appliquant sur le résultat
du premier. Les 20 scénarios couvrent volontairement des opérations très
différentes : insertion, suppression, renommage, remplacement, transformation
de gateway, ajout d'acteur interne/externe, ajout de boucle, de sous-processus,
d'objet de données, de timer, de multi-instance, de compensation, instructions
ambiguës ou contradictoires, et amendements combinant plusieurs changements
à la fois.

En plus du succès/échec de chaque étape, le script vérifie automatiquement
qu'aucun identifiant de nœud n'a disparu d'une version à l'autre sans
justification (comparaison des fichiers .logic-core.json), ce qui permet de
repérer une régression silencieuse même quand le pipeline ne renvoie pas
d'erreur.

Usage :
    (venv) PS C:\\Users\\laptop spirit\\Desktop\\bpmn-agent> python run_versioning_tests.py

Placez ce fichier à la racine du projet, à côté du dossier "scripts".
Nécessite que MySQL soit démarré et configuré (.env) comme pour les
commandes --process-name / --process-id habituelles.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

PIPELINE_SCRIPT = Path("scripts") / "pipeline.py"
OUTPUT_DIR = Path("tests_output_versioning")
REPORT_FILE = Path("rapport_tests_versioning.md")

PROCESS_ID_RE = re.compile(r"process_id=([0-9a-fA-F-]{36})")

# ----------------------------------------------------------------------
# 20 scénarios : (nom, notion testée, texte_v1, instruction_amendement_1,
#                 instruction_amendement_2)
# ----------------------------------------------------------------------
TESTS = [
    (
        "v01_insertion_simple",
        "Insertion d'une tâche entre deux existantes",
        "Un client dépose un colis. L'agent scanne le colis. L'agent expédie le colis.",
        "Ajoute une pesée du colis après le scan.",
        "Ajoute aussi un contrôle de sécurité juste avant l'expédition.",
    ),
    (
        "v02_suppression",
        "Suppression pure d'une étape existante",
        "Un client commande un produit. Le système envoie un email de confirmation. "
        "Le système envoie un SMS de confirmation. Le système expédie la commande.",
        "Retire l'envoi du SMS de confirmation, garde uniquement l'email.",
        "Retire également l'envoi de l'email, la commande est expédiée directement sans confirmation.",
    ),
    (
        "v03_renommage",
        "Renommage d'une tâche sans changer la structure",
        "Un employé soumet une demande d'achat. Le manager approuve la demande.",
        "Renomme la tâche 'Le manager approuve la demande' en 'Le manager valide la demande d'achat'.",
        "Renomme maintenant 'Un employé soumet une demande d'achat' en 'Un employé initie une demande d'achat'.",
    ),
    (
        "v04_remplacement_tache",
        "Remplacement d'une tâche par une autre",
        "Le client passe une commande. Le système envoie un email de confirmation.",
        "Remplace l'envoi d'email par l'envoi d'une notification push.",
        "Remplace maintenant la notification push par un appel téléphonique automatisé.",
    ),
    (
        "v05_lineaire_vers_exclusif",
        "Transformation d'un flux linéaire en gateway exclusif",
        "Un employé soumet une note de frais. Le manager approuve la note de frais.",
        "Si le montant dépasse 500 euros, le manager doit transmettre la note au directeur financier avant d'approuver.",
        "Si le directeur financier rejette la note, informe l'employé du refus au lieu d'approuver.",
    ),
    (
        "v06_exclusif_vers_inclusif",
        "Gateway inclusif introduit en amendement (2 conditions non exclusives)",
        "Un locataire signale un problème dans son logement. Le syndic envoie un technicien.",
        "Si le problème concerne la plomberie, envoie un plombier. Si le problème concerne l'électricité, envoie un électricien.",
        "Si le problème concerne aussi la serrurerie, envoie également un serrurier.",
    ),
    (
        "v07_ajout_acteur_interne",
        "Ajout d'un acteur interne (nouvelle lane)",
        "Un client réserve une chambre d'hôtel. La réception confirme la réservation.",
        "Après confirmation, le service ménage doit préparer la chambre avant l'arrivée du client.",
        "Ajoute aussi le service restauration, qui prépare un panier de bienvenue en parallèle du ménage.",
    ),
    (
        "v08_ajout_acteur_externe",
        "Ajout d'un acteur externe (nouvelle pool)",
        "Un client passe une commande de meubles. Le magasin prépare la commande.",
        "Une fois la commande prête, le magasin doit faire appel à un transporteur externe qui livre les meubles chez le client et confirme la livraison au magasin.",
        "Le transporteur externe doit aussi notifier une compagnie d'assurance tierce en cas de dommage constaté à la livraison.",
    ),
    (
        "v09_ajout_boucle",
        "Ajout d'une boucle (retour en arrière)",
        "Un auteur soumet un article. L'éditeur relit l'article. L'éditeur publie l'article.",
        "Si l'éditeur trouve des erreurs, l'auteur doit corriger l'article et le soumettre à nouveau pour relecture.",
        "Limite à 3 le nombre de cycles de correction ; au-delà, l'article est rejeté définitivement.",
    ),
    (
        "v10_ajout_parallelisme",
        "Ajout de parallélisme (gateway AND)",
        "Un candidat passe un entretien. Le recruteur rédige un compte-rendu. Le recruteur prend une décision d'embauche.",
        "Après l'entretien, le recruteur rédige le compte-rendu et en parallèle, le service RH vérifie les références du candidat. La décision d'embauche n'est prise qu'une fois les deux terminés.",
        "Ajoute une troisième vérification en parallèle : le service juridique valide la conformité du contrat proposé.",
    ),
    (
        "v11_ajout_sous_processus",
        "Introduction d'un sous-processus intégré",
        "Le client soumet une réclamation. L'agent traite la réclamation.",
        "Le traitement de la réclamation doit maintenant inclure trois étapes : vérifier l'historique du client, évaluer la validité de la réclamation, et proposer une compensation.",
        "Ajoute une quatrième étape dans ce même traitement : archiver le dossier une fois la compensation proposée.",
    ),
    (
        "v12_double_condition_imbriquee",
        "Amendement combinant deux conditions imbriquées",
        "Un client demande un devis. Le commercial envoie le devis.",
        "Ajoute une validation du responsable commercial avant l'envoi du devis, mais seulement si le montant dépasse 10000 euros. Sinon, le commercial envoie directement le devis sans validation.",
        "Si le responsable commercial rejette le devis, le commercial doit le renégocier avec le client avant de le soumettre à nouveau.",
    ),
    (
        "v13_reference_inexistante",
        "Amendement référant à une étape qui n'existe pas (robustesse)",
        "Un visiteur s'inscrit à une newsletter. Le système confirme l'inscription.",
        "Ajoute une étape de validation par le service juridique après l'étape de paiement.",
        "Ajoute maintenant une vraie étape de paiement avant la confirmation d'inscription.",
    ),
    (
        "v14_instruction_contradictoire",
        "Amendement contredisant une affirmation de la version précédente",
        "Le client soumet une demande. Le système traite la demande automatiquement sans intervention humaine.",
        "Ajoute une validation manuelle par un agent avant le traitement automatique.",
        "Retire la validation manuelle ajoutée précédemment, le traitement redevient entièrement automatique.",
    ),
    (
        "v15_chaine_trois_versions",
        "Chaîne de versions qui s'appuient les unes sur les autres",
        "Un employé demande du matériel informatique. Le service IT vérifie le stock. Le service IT livre le matériel.",
        "Ajoute une validation du manager avant la vérification du stock.",
        "Si le matériel n'est pas en stock, le service IT doit le commander auprès du fournisseur.",
    ),
    (
        "v16_ajout_data_object",
        "Ajout d'un objet de données en amendement",
        "Le fournisseur soumet ses données d'enregistrement. Le gestionnaire vérifie si le fournisseur est déjà enregistré.",
        "Si le fournisseur est nouveau, le gestionnaire enregistre le fournisseur en utilisant les données du fournisseur.",
        "L'acheteur passe ensuite une commande d'achat en se basant sur le bon de commande.",
    ),
    (
        "v17_ajout_timer",
        "Ajout d'un événement timer en amendement",
        "Un client soumet un ticket de support. L'agent répond au ticket.",
        "Si aucune réponse n'est envoyée dans les 24 heures, le ticket est automatiquement transféré au responsable.",
        "Réduis le délai de transfert automatique à 4 heures pour les tickets urgents, sans changer le comportement pour les tickets normaux.",
    ),
    (
        "v18_ajout_multi_instance",
        "Introduction d'un multi-instance en amendement",
        "Un projet est soumis pour évaluation. Le comité examine le projet.",
        "Le projet doit être évalué par chacun des trois membres du comité indépendamment et en parallèle avant d'obtenir un classement.",
        "Ajoute un quatrième membre au comité, qui évalue également le projet en parallèle des trois autres.",
    ),
    (
        "v19_ajout_compensation",
        "Introduction d'un mécanisme de compensation en amendement",
        "Un client réserve un vol et un hôtel.",
        "Si la réservation de l'hôtel échoue après que le vol a déjà été réservé, annule automatiquement la réservation du vol précédemment effectuée.",
        "Ajoute la même logique de compensation pour la réservation d'une voiture de location, si elle échoue après que le vol et l'hôtel ont déjà été réservés.",
    ),
    (
        "v20_suppression_branche_entiere",
        "Suppression d'une branche entière de décision",
        "Un candidat postule à une offre d'emploi. Le recruteur examine le CV. Si le profil correspond au poste, le recruteur planifie un entretien. Sinon, le recruteur envoie une réponse négative.",
        "Ajoute une troisième branche : si le profil correspond partiellement, le recruteur propose un poste alternatif au candidat.",
        "Retire la branche du poste alternatif ajoutée précédemment, il ne reste que les deux issues initiales (entretien ou réponse négative).",
    ),
]


# ----------------------------------------------------------------------
# Exécution
# ----------------------------------------------------------------------
def run_pipeline(cmd, timeout=300):
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        duration = time.time() - start
        return result.returncode == 0, result.stdout, result.stderr, duration
    except subprocess.TimeoutExpired:
        return False, "", "Timeout dépassé", time.time() - start
    except Exception as e:
        return False, "", str(e), time.time() - start


def extract_process_id(stdout):
    m = PROCESS_ID_RE.search(stdout)
    return m.group(1) if m else None


def load_node_ids(logic_core_path):
    """Retourne l'ensemble des identifiants de nœuds d'un Logic-Core JSON, ou None si illisible."""
    try:
        data = json.loads(Path(logic_core_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    ids = set()
    for key in ("nodes", "edges"):
        for item in data.get(key, []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.add(item["id"])
    return ids


def run_test(index, total, name, notion, text_v1, amend1, amend2):
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}] {name} — {notion}")
    print(f"{'=' * 70}")

    result = {
        "name": name, "notion": notion,
        "v1_ok": False, "v2_ok": False, "v3_ok": False,
        "v1_duration": 0, "v2_duration": 0, "v3_duration": 0,
        "lost_ids_v1_v2": None, "lost_ids_v2_v3": None,
        "error": "",
    }

    # --- V1 : création initiale ---
    out_v1 = OUTPUT_DIR / f"{name}_v1.bpmn"
    ok, stdout, stderr, dur = run_pipeline([
        sys.executable, str(PIPELINE_SCRIPT), text_v1,
        "--process-name", name, "--out", str(out_v1),
    ])
    result["v1_ok"], result["v1_duration"] = ok, dur
    print(f"  V1 (création) : {'OK' if ok else 'ECHEC'} ({dur:.1f}s)")
    if not ok:
        result["error"] = f"V1: {stderr[-500:]}"
        return result

    process_id = extract_process_id(stdout)
    if not process_id:
        result["error"] = "process_id introuvable dans la sortie de V1"
        return result

    # --- V2 : premier amendement ---
    out_v2 = OUTPUT_DIR / f"{name}_v2.bpmn"
    ok, stdout, stderr, dur = run_pipeline([
        sys.executable, str(PIPELINE_SCRIPT), amend1,
        "--process-id", process_id, "--out", str(out_v2),
    ])
    result["v2_ok"], result["v2_duration"] = ok, dur
    print(f"  V2 (amendement 1) : {'OK' if ok else 'ECHEC'} ({dur:.1f}s)")
    if not ok:
        result["error"] = f"V2: {stderr[-500:]}"
        return result

    ids_v1 = load_node_ids(out_v1.with_suffix(".logic-core.json"))
    ids_v2 = load_node_ids(out_v2.with_suffix(".logic-core.json"))
    if ids_v1 is not None and ids_v2 is not None:
        lost = ids_v1 - ids_v2
        result["lost_ids_v1_v2"] = sorted(lost) if lost else []

    # --- V3 : second amendement, sur le résultat du premier ---
    out_v3 = OUTPUT_DIR / f"{name}_v3.bpmn"
    ok, stdout, stderr, dur = run_pipeline([
        sys.executable, str(PIPELINE_SCRIPT), amend2,
        "--process-id", process_id, "--out", str(out_v3),
    ])
    result["v3_ok"], result["v3_duration"] = ok, dur
    print(f"  V3 (amendement 2) : {'OK' if ok else 'ECHEC'} ({dur:.1f}s)")
    if not ok:
        result["error"] = f"V3: {stderr[-500:]}"
        return result

    ids_v3 = load_node_ids(out_v3.with_suffix(".logic-core.json"))
    if ids_v2 is not None and ids_v3 is not None:
        lost = ids_v2 - ids_v3
        result["lost_ids_v2_v3"] = sorted(lost) if lost else []

    return result


def write_report(results):
    total = len(results)
    full_ok = sum(1 for r in results if r["v1_ok"] and r["v2_ok"] and r["v3_ok"])

    lines = []
    lines.append(f"# Rapport de tests — Versioning (amendements) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"**Résultat global : {full_ok}/{total} scénarios complets (V1+V2+V3) réussis**")
    lines.append("")
    lines.append("| # | Test | Notion | V1 | V2 | V3 | IDs perdus V1→V2 | IDs perdus V2→V3 |")
    lines.append("|---|------|--------|----|----|----|--------------------|--------------------|")
    for i, r in enumerate(results, 1):
        v1 = "OK" if r["v1_ok"] else "ECHEC"
        v2 = "OK" if r["v2_ok"] else "ECHEC"
        v3 = "OK" if r["v3_ok"] else "ECHEC"
        lost12 = ", ".join(r["lost_ids_v1_v2"]) if r["lost_ids_v1_v2"] else ("-" if r["lost_ids_v1_v2"] == [] else "n/a")
        lost23 = ", ".join(r["lost_ids_v2_v3"]) if r["lost_ids_v2_v3"] else ("-" if r["lost_ids_v2_v3"] == [] else "n/a")
        lines.append(f"| {i} | `{r['name']}` | {r['notion']} | {v1} | {v2} | {v3} | {lost12} | {lost23} |")

    lines.append("")
    lines.append("## Détails des échecs")
    lines.append("")
    any_failure = False
    for r in results:
        if not (r["v1_ok"] and r["v2_ok"] and r["v3_ok"]):
            any_failure = True
            lines.append(f"### {r['name']} — {r['notion']}")
            lines.append("```")
            lines.append(r["error"] or "(pas de détail capturé)")
            lines.append("```")
            lines.append("")
    if not any_failure:
        lines.append("Aucun échec sur les 3 étapes de chaque scénario.")

    lines.append("")
    lines.append("## Suspicions de perte d'identifiants (à vérifier manuellement)")
    lines.append("")
    any_loss = False
    for r in results:
        for label, lost in (("V1→V2", r["lost_ids_v1_v2"]), ("V2→V3", r["lost_ids_v2_v3"])):
            if lost:
                any_loss = True
                lines.append(f"- `{r['name']}` ({label}) : {', '.join(lost)}")
    if not any_loss:
        lines.append("Aucune perte d'identifiant détectée entre les versions comparées.")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def main():
    if not PIPELINE_SCRIPT.exists():
        print(f"Introuvable : {PIPELINE_SCRIPT}")
        print("Lancez ce script depuis la racine du projet bpmn-agent.")
        sys.exit(1)

    total = len(TESTS)
    print(f"Lancement de {total} scénarios de versioning (3 étapes chacun : creation + 2 amendements)...\n")

    results = []
    global_start = time.time()
    for i, (name, notion, text_v1, amend1, amend2) in enumerate(TESTS, 1):
        results.append(run_test(i, total, name, notion, text_v1, amend1, amend2))
    global_duration = time.time() - global_start

    write_report(results)

    full_ok = sum(1 for r in results if r["v1_ok"] and r["v2_ok"] and r["v3_ok"])
    print(f"\n{'=' * 70}")
    print(f"TERMINE : {full_ok}/{total} scenarios complets reussis en {global_duration/60:.1f} min")
    print(f"Fichiers .bpmn generes dans : {OUTPUT_DIR.resolve()}")
    print(f"Rapport detaille : {REPORT_FILE.resolve()}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
