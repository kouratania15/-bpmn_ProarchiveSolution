# -*- coding: utf-8 -*-
"""
Script de test automatisé — Architecture d'amendement en 5 étapes (intention →
application → diff sémantique), section 27 de SKILL.md.

Pour chacune des 34 sous-catégories d'amendement (22 d'ajout, 7 de suppression,
5 de modification), une paire (texte initial, instruction d'amendement) unique
— une création suivie d'UN SEUL amendement (pas de chaînage, chaque
sous-catégorie est testée isolément). Vérifie succès/échec de chaque étape et
consigne les IDs perdus entre versions (comparaison des .logic-core.json),
même méthode que run_versioning_tests.py dont ce script réutilise les
fonctions d'exécution/rapport pour ne pas dupliquer la logique de sous-processus.

Usage :
    (venv) PS C:\\Users\\laptop spirit\\Desktop\\bpmn-agent> python run_amendment_intent_tests.py
"""
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from run_versioning_tests import run_pipeline, extract_process_id, load_node_ids, PIPELINE_SCRIPT

OUTPUT_DIR = Path("tests_output_amendment_intent")
REPORT_FILE = Path("rapport_tests_amendment_intent.md")

# ----------------------------------------------------------------------
# 34 paires : (nom, categorie, operation_type attendu, texte_v1, instruction_amendement)
# ----------------------------------------------------------------------
TESTS = [
    # ---- 27.1 Ajout (22) ----
    ("a01_insert_sequential", "Ajout", "insert_sequential",
     "Un client dépose un colis. L'agent scanne le colis. L'agent expédie le colis.",
     "Ajoute une pesée du colis après le scan."),
    ("a02_insert_conditional_exclusive", "Ajout", "insert_conditional_exclusive",
     "Un employé soumet une note de frais. Le manager approuve la note.",
     "Si le montant dépasse 500 euros, le manager doit d'abord obtenir l'accord du directeur "
     "financier avant d'approuver."),
    ("a03_insert_conditional_inclusive", "Ajout", "insert_conditional_inclusive",
     "Un locataire signale un problème dans son logement. Le syndic envoie un technicien.",
     "Si le problème concerne la plomberie, envoie un plombier. Si le problème concerne "
     "l'électricité, envoie un électricien."),
    ("a04_insert_parallel", "Ajout", "insert_parallel",
     "Un candidat passe un entretien. Le recruteur rédige un compte-rendu.",
     "Après l'entretien, le recruteur rédige le compte-rendu et en parallèle, le service RH "
     "vérifie les références du candidat."),
    ("a05_insert_event_based_gateway", "Ajout", "insert_event_based_gateway",
     "Un client passe une commande. Le système confirme la commande.",
     "Après la commande, le système attend soit le paiement du client soit l'annulation de la "
     "commande par le client, selon ce qui arrive en premier."),
    ("a06_insert_complex_gateway", "Ajout", "insert_complex_gateway",
     "Un projet est soumis pour évaluation. Le comité approuve le projet.",
     "Le projet n'est approuvé que si au moins 3 des 5 membres du comité votent favorablement."),
    ("a07_insert_loop_backward", "Ajout", "insert_loop_backward",
     "Un auteur soumet un article. L'éditeur relit l'article. L'éditeur publie l'article.",
     "Si l'éditeur trouve des erreurs, l'auteur doit corriger l'article et le soumettre à "
     "nouveau pour relecture."),
    ("a08_add_actor_internal_lane", "Ajout", "add_actor_internal_lane",
     "Un client réserve une chambre d'hôtel. La réception confirme la réservation.",
     "Après confirmation, le service ménage doit préparer la chambre avant l'arrivée du client."),
    ("a09_add_actor_external_pool", "Ajout", "add_actor_external_pool",
     "Un client passe une commande de meubles. Le magasin prépare la commande.",
     "Une fois la commande prête, le magasin doit faire appel à un transporteur externe qui "
     "livre les meubles chez le client."),
    ("a10_add_subprocess_embedded", "Ajout", "add_subprocess_embedded",
     "Le client soumet une réclamation. L'agent traite la réclamation.",
     "Le traitement de la réclamation doit maintenant inclure trois étapes : vérifier "
     "l'historique du client, évaluer la validité de la réclamation, et proposer une "
     "compensation."),
    ("a11_add_call_activity", "Ajout", "add_call_activity",
     "Un nouvel employé est recruté. Le service RH prépare son arrivée.",
     "Avant de finaliser l'arrivée, le service RH doit appeler le processus standard de "
     "vérification des antécédents déjà utilisé pour tous les recrutements."),
    ("a12_add_subprocess_adhoc", "Ajout", "add_subprocess_adhoc",
     "Un événement d'entreprise est organisé. L'équipe événementiel prépare l'événement.",
     "La préparation inclut réserver la salle, commander le traiteur et envoyer les invitations, "
     "dans n'importe quel ordre selon les disponibilités de chacun."),
    ("a13_add_subprocess_transactional", "Ajout", "add_subprocess_transactional",
     "Un client transfère de l'argent entre deux comptes bancaires. La banque traite le transfert.",
     "Le traitement doit se faire comme une seule opération : si le débit du compte source "
     "échoue, annuler également le crédit déjà effectué sur le compte destination."),
    ("a14_add_subprocess_event", "Ajout", "add_subprocess_event",
     "Un colis est en cours de livraison. Le transporteur livre le colis.",
     "À tout moment pendant la livraison, si le client annule sa commande, le transporteur doit "
     "immédiatement retourner le colis à l'entrepôt."),
    ("a15_add_timer_event", "Ajout", "add_timer_event",
     "Un client soumet un ticket de support. L'agent répond au ticket.",
     "Si aucune réponse n'est envoyée dans les 24 heures, le ticket est automatiquement "
     "transféré au responsable."),
    ("a16_add_signal_event", "Ajout", "add_signal_event",
     "Le service qualité détecte un défaut sur un lot de production. Le service qualité corrige "
     "le défaut.",
     "Dès que le défaut est détecté, le service qualité doit diffuser une alerte à tous les "
     "services concernés (production, logistique, ventes) simultanément."),
    ("a17_add_escalation_event", "Ajout", "add_escalation_event",
     "Un client dépose une plainte. Le service client traite la plainte.",
     "Si le montant du litige dépasse 1000 euros, la plainte doit être escaladée au responsable "
     "régional."),
    ("a18_add_compensation_event", "Ajout", "add_compensation_event",
     "Un client réserve un vol et un hôtel.",
     "Si la réservation de l'hôtel échoue après que le vol a déjà été réservé, annule "
     "automatiquement la réservation du vol précédemment effectuée."),
    ("a19_add_conditional_event", "Ajout", "add_conditional_event",
     "Un fournisseur soumet une offre. L'acheteur évalue l'offre.",
     "L'évaluation ne démarre que lorsque le stock du produit concerné passe en dessous du seuil "
     "de réapprovisionnement — le système reste en attente jusqu'à ce que cette condition "
     "devienne vraie."),
    ("a20_add_terminate_event", "Ajout", "add_terminate_event",
     "Un client passe une commande. Le système traite la commande.",
     "Si le client annule sa commande à tout moment avant expédition, le processus s'arrête "
     "immédiatement dans son ensemble, quelles que soient les étapes en cours."),
    ("a21_add_multi_instance", "Ajout", "add_multi_instance",
     "Un projet est soumis pour évaluation. Le comité examine le projet.",
     "Le projet doit être évalué par chacun des trois membres du comité indépendamment et en "
     "parallèle avant d'obtenir un classement."),
    ("a22_add_data_object", "Ajout", "add_data_object",
     "Le fournisseur soumet ses données d'enregistrement. Le gestionnaire vérifie si le "
     "fournisseur est déjà enregistré.",
     "Si le fournisseur est nouveau, le gestionnaire enregistre le fournisseur en utilisant les "
     "données du fournisseur."),

    # ---- 27.2 Suppression (7) ----
    ("d01_delete_task_simple", "Suppression", "delete_task_simple",
     "Un client commande un produit. Le système envoie un email de confirmation. Le système "
     "envoie un SMS de confirmation. Le système expédie la commande.",
     "Retire l'envoi du SMS de confirmation, garde uniquement l'email."),
    ("d02_delete_gateway_branch", "Suppression", "delete_gateway_branch",
     "Un candidat postule à une offre d'emploi. Le recruteur examine le CV. Si le profil "
     "correspond parfaitement au poste, le recruteur planifie un entretien. Si le profil ne "
     "correspond pas du tout, le recruteur envoie une réponse négative. Si le profil correspond "
     "partiellement, le recruteur propose un poste alternatif au candidat.",
     "Retire la branche du poste alternatif, il ne reste que les deux issues initiales "
     "(entretien ou réponse négative)."),
    ("d03_delete_gateway_full", "Suppression", "delete_gateway_full",
     "Un employé soumet une demande d'achat. Si le montant dépasse 1000 euros, le manager "
     "approuve la demande. Sinon, la demande est automatiquement validée.",
     "Retire la condition sur le montant, toutes les demandes d'achat doivent désormais être "
     "approuvées par le manager sans exception."),
    ("d04_delete_actor", "Suppression", "delete_actor",
     "Un client réserve une chambre d'hôtel. La réception confirme la réservation. Le service "
     "ménage prépare la chambre avant l'arrivée du client.",
     "Le service ménage n'existe plus, la réception se charge elle-même de préparer la chambre."),
    ("d05_delete_subprocess", "Suppression", "delete_subprocess",
     "Le client soumet une réclamation. L'agent traite la réclamation, ce qui inclut vérifier "
     "l'historique du client, évaluer la validité de la réclamation, et proposer une "
     "compensation.",
     "Retire toute l'étape de traitement détaillé de la réclamation, l'agent se contente "
     "désormais de transmettre directement la réclamation au service juridique."),
    ("d06_delete_event", "Suppression", "delete_event",
     "Un client soumet un ticket de support. L'agent répond au ticket. Si aucune réponse n'est "
     "envoyée dans les 24 heures, le ticket est automatiquement transféré au responsable.",
     "Retire le transfert automatique après 24 heures, il n'y a plus de délai de ce type."),
    ("d07_delete_data_object", "Suppression", "delete_data_object",
     "Le fournisseur soumet ses données d'enregistrement. Le gestionnaire vérifie si le "
     "fournisseur est déjà enregistré en consultant la base de données des fournisseurs.",
     "Retire la consultation de la base de données des fournisseurs, le gestionnaire vérifie "
     "désormais de mémoire sans référence à un système externe."),

    # ---- 27.3 Modification (5) ----
    ("m01_rename_only", "Modification", "rename_only",
     "Un employé soumet une demande d'achat. Le manager approuve la demande.",
     "Renomme la tâche 'Le manager approuve la demande' en 'Le manager valide la demande "
     "d'achat'."),
    ("m02_replace_task", "Modification", "replace_task",
     "Le client passe une commande. Le système envoie un email de confirmation.",
     "Remplace l'envoi d'email par l'envoi d'une notification push."),
    ("m03_replace_gateway_type", "Modification", "replace_gateway_type",
     "Un locataire signale un problème dans son logement. Si le problème concerne la plomberie, "
     "envoie un plombier. Sinon, si le problème concerne l'électricité, envoie un électricien.",
     "Un même problème peut désormais concerner à la fois la plomberie ET l'électricité en même "
     "temps : si c'est le cas, envoie les deux techniciens."),
    ("m04_change_task_actor", "Modification", "change_task_actor",
     "Un employé soumet une demande d'achat. Le manager approuve la demande.",
     "C'est maintenant le directeur financier qui approuve la demande, plus le manager."),
    ("m05_change_gateway_condition", "Modification", "change_gateway_condition",
     "Un employé soumet une note de frais. Si le montant dépasse 500 euros, le manager transmet "
     "au directeur financier avant d'approuver. Sinon, le manager approuve directement.",
     "Change le seuil de 500 euros à 1000 euros."),
]


def run_test(index: int, total: int, name: str, category: str, expected_op: str, text_v1: str, amend: str) -> dict:
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}] {name} ({category} -> {expected_op})")
    print(f"{'=' * 70}")

    result = {
        "name": name, "category": category, "expected_op": expected_op,
        "v1_ok": False, "amend_ok": False,
        "v1_duration": 0, "amend_duration": 0,
        "lost_ids": None, "error": "",
    }

    out_v1 = OUTPUT_DIR / f"{name}_v1.bpmn"
    ok, stdout, stderr, dur = run_pipeline([
        sys.executable, str(PIPELINE_SCRIPT), text_v1,
        "--process-name", name, "--out", str(out_v1),
    ])
    result["v1_ok"], result["v1_duration"] = ok, dur
    print(f"  Creation : {'OK' if ok else 'ECHEC'} ({dur:.1f}s)")
    if not ok:
        result["error"] = f"V1: {stderr[-500:]}"
        return result

    process_id = extract_process_id(stdout)
    if not process_id:
        result["error"] = "process_id introuvable dans la sortie de creation"
        return result

    out_v2 = OUTPUT_DIR / f"{name}_v2.bpmn"
    ok, stdout, stderr, dur = run_pipeline([
        sys.executable, str(PIPELINE_SCRIPT), amend,
        "--process-id", process_id, "--out", str(out_v2),
    ])
    result["amend_ok"], result["amend_duration"] = ok, dur
    print(f"  Amendement : {'OK' if ok else 'ECHEC'} ({dur:.1f}s)")
    if not ok:
        result["error"] = f"AMEND: {stderr[-800:]}"
        return result

    ids_v1 = load_node_ids(out_v1.with_suffix(".logic-core.json"))
    ids_v2 = load_node_ids(out_v2.with_suffix(".logic-core.json"))
    if ids_v1 is not None and ids_v2 is not None:
        lost = ids_v1 - ids_v2
        result["lost_ids"] = sorted(lost) if lost else []

    return result


def write_report(results: list[dict]) -> None:
    total = len(results)
    full_ok = sum(1 for r in results if r["v1_ok"] and r["amend_ok"])

    lines = []
    lines.append(f"# Rapport de tests — Architecture d'amendement en 5 étapes — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"**Résultat global : {full_ok}/{total} sous-catégories réussies (création + amendement)**")
    lines.append("")
    lines.append("| # | Test | Catégorie | operation_type attendu | Création | Amendement | IDs perdus |")
    lines.append("|---|------|-----------|-------------------------|----------|------------|------------|")
    for i, r in enumerate(results, 1):
        v1 = "OK" if r["v1_ok"] else "ECHEC"
        am = "OK" if r["amend_ok"] else "ECHEC"
        lost = ", ".join(r["lost_ids"]) if r["lost_ids"] else ("-" if r["lost_ids"] == [] else "n/a")
        lines.append(f"| {i} | `{r['name']}` | {r['category']} | `{r['expected_op']}` | {v1} | {am} | {lost} |")

    lines.append("")
    lines.append("## Détails des échecs")
    lines.append("")
    any_failure = False
    for r in results:
        if not (r["v1_ok"] and r["amend_ok"]):
            any_failure = True
            lines.append(f"### {r['name']} — {r['category']} ({r['expected_op']})")
            lines.append("```")
            lines.append(r["error"] or "(pas de détail capturé)")
            lines.append("```")
            lines.append("")
    if not any_failure:
        lines.append("Aucun échec sur les 34 sous-catégories.")

    lines.append("")
    lines.append("## Suspicions de perte d'identifiants (à vérifier manuellement)")
    lines.append("")
    any_loss = False
    for r in results:
        if r["lost_ids"]:
            any_loss = True
            lines.append(f"- `{r['name']}` : {', '.join(r['lost_ids'])}")
    if not any_loss:
        lines.append("Aucune perte d'identifiant détectée.")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def main():
    total = len(TESTS)
    print(f"Lancement de {total} sous-catégories d'amendement (creation + 1 amendement chacune)...\n")

    results = []
    for i, (name, category, expected_op, text_v1, amend) in enumerate(TESTS, 1):
        results.append(run_test(i, total, name, category, expected_op, text_v1, amend))

    write_report(results)

    full_ok = sum(1 for r in results if r["v1_ok"] and r["amend_ok"])
    print(f"\n{'=' * 70}")
    print(f"TERMINE : {full_ok}/{total} sous-categories reussies")
    print(f"Rapport detaille : {REPORT_FILE.resolve()}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
