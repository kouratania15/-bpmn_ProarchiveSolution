"""
Orchestrateur Principal du BPMN-Agent Enterprise (Inspiré de Stieges/bpmn-generator).

Pipeline complet :
Langage Naturel -> Mistral AI -> Logic-Core -> Soundness Validator -> (Self-Healing Loop) -> pyelk -> XML BPMN 2.0 + BPMNDI.

Utilisation en Ligne de Commande :
    # 1. Génération depuis du texte libre :
    python bpmn-agent/scripts/pipeline.py "Le client soumet un dossier..." --out process.bpmn

    # 2. Génération depuis un fichier de transcription d'entretien :
    python bpmn-agent/scripts/pipeline.py --file transcription.txt --out process.bpmn

    # 3. Mode amendement incrémental temps réel (pendant l'interview) :
    python bpmn-agent/scripts/pipeline.py "Ajouter une tâche de double validation si montant > 50000" \
        --logic-core process.logic-core.json --out process_v2.bpmn

    # 4. Mode hors-ligne / direct depuis Logic-Core JSON :
    python bpmn-agent/scripts/pipeline.py --from-json bpmn-agent/examples/sample_order_fulfillment.json --out process.bpmn
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bpmn_xml import generate_bpmn_xml
from layout import compute_layout
from llm_agent import (
    extract_logic_core,
    extract_structured_analysis,
    build_process_description,
    extract_process_description,
    generate_logic_core_from_pd,
    heal_process_description,
    self_heal_logic_core,
)
from src.config import (
    DEFAULT_MODEL,
    MAX_AMENDMENT_RETRIES,
    MAX_PROCESS_DESCRIPTION_RETRIES,
    MAX_SELF_HEALING_ATTEMPTS,
)
from src.models.action import ActionType
from src.utils.logger import (
    append_execution_trace,
    finalize_run,
    generate_run_id,
    log_event,
    print_business_summary,
    set_final_experiment_summary,
)
from src.utils.trace import print_trace
from validate import validate_logic_core, validate_process_description


def _logic_core_ids(logic_core: dict[str, Any]) -> set[str]:
    """Retourne tous les IDs persistants du Logic-Core, y compris pools et lanes."""
    ids: set[str] = set()
    process = logic_core.get("process")
    if isinstance(process, dict) and isinstance(process.get("id"), str):
        ids.add(process["id"])
    for key in ("nodes", "edges", "pools"):
        for item in logic_core.get(key, []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.add(item["id"])
            if key == "pools" and isinstance(item, dict):
                for lane in item.get("lanes", []):
                    if isinstance(lane, dict) and isinstance(lane.get("id"), str):
                        ids.add(lane["id"])
    return ids


def _illegitimate_missing_ids(existing_logic_core: dict[str, Any], new_logic_core: dict[str, Any]) -> set[str]:
    """Filtre les IDs disparus pour ne garder que ceux réellement suspects.

    Un edge dont au moins une extrémité (source/target, telle qu'elle existait
    dans le Logic-Core D'ORIGINE) a elle-même disparu du nouveau graphe est une
    perte LÉGITIME : la règle 21 'Suppression' du système impose justement de
    retirer les flux devenus invalides quand un nœud est explicitement
    supprimé (ex: "retire l'envoi du SMS" -> la tâche SMS ET ses deux
    sequenceFlow adjacents disparaissent ensemble, ce qui est correct). Un
    edge dont les DEUX extrémités existent toujours dans le nouveau graphe,
    mais qui a lui-même disparu ou a été renommé, reste suspect — c'est le
    pattern de renommage arbitraire que ce garde-fou vise à empêcher."""
    missing = _logic_core_ids(existing_logic_core) - _logic_core_ids(new_logic_core)
    if not missing:
        return missing
    new_ids = _logic_core_ids(new_logic_core)
    existing_edges_by_id = {
        e.get("id"): e for e in existing_logic_core.get("edges", [])
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    }
    illegitimate: set[str] = set()
    for mid in missing:
        edge = existing_edges_by_id.get(mid)
        if edge is None:
            illegitimate.add(mid)  # pas un edge (nœud/pool/lane/process) : toujours suspect
            continue
        if edge.get("source") in new_ids and edge.get("target") in new_ids:
            illegitimate.add(mid)
    return illegitimate


def run_pipeline(
    user_text: str | None = None,
    existing_logic_core: dict[str, Any] | None = None,
    direct_logic_core: dict[str, Any] | None = None,
    max_heal_attempts: int = MAX_SELF_HEALING_ATTEMPTS,
    verbose: bool = True,
    model: str = DEFAULT_MODEL,
) -> tuple[str, dict[str, Any]]:
    """
    Exécute le cycle complet d'extraction, validation avec self-healing, layout et génération XML.
    """
    run_id = generate_run_id()
    total_steps = 8
    business_summary: dict[str, Any] = {}
    process_desc: dict[str, Any] | None = None
    try:
        log_event(run_id, ActionType.ANALYSIS, "input", "success",
                  output={"user_text": user_text, "source_artifact": direct_logic_core is not None})
        # Trace d'entree
        append_execution_trace(run_id, {
            "step": 1,
            "component": "pipeline.run_pipeline",
            "action": "INPUT_RECEIVED",
            "payload": user_text or "Input JSON direct",
        })
        if direct_logic_core is not None:
            analysis = {"participants": [], "activities": [], "events": [], "conditions": [],
                "relations": [], "communications": [], "gaps": [],
                "structural_elements": [{"id": "structural_001", "type": "start_event",
                "confidence": "structural", "reason": "Existing Logic-Core compilation entry"}]}
            log_event(run_id, ActionType.ANALYSIS, "analysis", "success", output=analysis)
            process_desc = build_process_description(analysis, run_id=run_id)
            pd_result = validate_process_description(process_desc)
            log_event(run_id, ActionType.DEBUG, "process_description_validation",
                      "success" if pd_result.ok else "failed", output=pd_result.to_dict(), errors=pd_result.errors or None)
            if not pd_result.ok:
                raise ValueError(f"Process Description structurel invalide : {pd_result.errors}")
            logic_core = direct_logic_core
            if verbose:
                print_trace(1, total_steps, "Compilation contrôlée d'un Logic-Core existant")
        elif existing_logic_core is not None:
            if not user_text:
                raise ValueError("Aucune modification fournie pour l'amendement.")
            if verbose:
                print_trace(1, total_steps, "Amendement incrémental du Logic-Core existant")
            # L'amendement est un appel LLM unique et stochastique : une tentative
            # peut, par malchance, ignorer la consigne "conserve les IDs existants"
            # et régénérer un Logic-Core sans rapport avec l'original (observé en
            # usage réel). Plutôt que d'échouer immédiatement sur ce hasard
            # d'échantillonnage, on retente quelques fois avec un rappel explicite
            # des IDs perdus la fois précédente — même logique que le
            # circuit-breaker du self-healing, mais pour une perte d'ID plutôt
            # qu'une erreur de validation.
            amend_instruction = user_text
            missing_ids: set[str] = set()
            prev_missing_ids: set[str] | None = None
            stable_missing = False
            for amend_attempt in range(1, MAX_AMENDMENT_RETRIES + 1):
                logic_core = extract_logic_core(amend_instruction, existing_logic_core, run_id=run_id, model=model)
                missing_ids = _illegitimate_missing_ids(existing_logic_core, logic_core)
                if not missing_ids:
                    break
                # Le MÊME ensemble d'IDs disparaît deux fois de suite MALGRÉ un
                # rappel explicite les nommant un par un : ce n'est plus un hasard
                # d'échantillonnage, c'est un comportement stable — le plus souvent
                # la conséquence délibérée et légitime de la demande elle-même
                # (ex: "retire le SMS" supprime nécessairement sa tâche et ses
                # flux adjacents ; une restructuration complexe peut légitimement
                # changer la forme autour d'un nœud conservé). Insister une 3e
                # fois n'apporterait rien de plus qu'un 3e résultat identique.
                if missing_ids == prev_missing_ids:
                    stable_missing = True
                    break
                prev_missing_ids = missing_ids
                append_execution_trace(run_id, {
                    "step": 1,
                    "attempt": amend_attempt,
                    "component": "llm_agent.extract_logic_core",
                    "action": "AMENDMENT_LOST_IDS_RETRY",
                    "missing_ids": sorted(missing_ids),
                })
                if amend_attempt < MAX_AMENDMENT_RETRIES:
                    if verbose:
                        print_trace(1, total_steps,
                                     f"Amendement a supprimé des IDs existants — nouvelle tentative "
                                     f"({amend_attempt}/{MAX_AMENDMENT_RETRIES})", "WARNING")
                    amend_instruction = (
                        f"{user_text}\n\nRAPPEL CRITIQUE : ta précédente tentative a supprimé ces IDs "
                        f"existants du Logic-Core fourni, ce qui est INTERDIT — conserve-les "
                        f"impérativement cette fois-ci, inchangés : {sorted(missing_ids)}"
                    )
            if missing_ids and not stable_missing:
                raise ValueError(
                    f"L'amendement a supprimé des IDs existants après {MAX_AMENDMENT_RETRIES} tentatives : "
                    f"{sorted(missing_ids)}"
                )
            if missing_ids and stable_missing:
                append_execution_trace(run_id, {
                    "step": 1,
                    "component": "llm_agent.extract_logic_core",
                    "action": "AMENDMENT_STABLE_ID_LOSS_ACCEPTED",
                    "missing_ids": sorted(missing_ids),
                })
                if verbose:
                    print_trace(1, total_steps,
                                 f"Avertissement : ces IDs disparaissent de façon stable malgré le rappel "
                                 f"explicite — probablement une conséquence légitime de la demande, "
                                 f"acceptée : {sorted(missing_ids)}", "WARNING")
        else:
            if not user_text:
                raise ValueError("Aucun texte fourni pour l'extraction.")
            if verbose:
                print_trace(1, total_steps, "Génération du Process Description")
            analysis = extract_structured_analysis(user_text, run_id=run_id, model=model)
            log_event(run_id, ActionType.ANALYSIS, "fact_extraction", "success",
                output_response=json.dumps(analysis, ensure_ascii=False))
            for category in ("participants", "activities", "events", "conditions", "relations", "gaps"):
                log_event(run_id, ActionType.ANALYSIS, f"{category}_extraction", "success",
                        output={category: analysis.get(category, [])})
            process_desc = build_process_description(analysis, run_id=run_id)
            if verbose:
                print_trace(2, total_steps, "Validation du Process Description")
            pd_result = validate_process_description(process_desc)
            log_event(
                run_id, ActionType.DEBUG, "process_description_validation",
                "success" if pd_result.ok else "failed",
                output_response=json.dumps(process_desc, ensure_ascii=False),
                errors=pd_result.errors or None,
            )
            pd_attempt = 0
            while not pd_result.ok and pd_attempt < MAX_PROCESS_DESCRIPTION_RETRIES:
                pd_attempt += 1
                if verbose:
                    print_trace(2, total_steps, f"Correction du Process Description ({pd_attempt}/{MAX_PROCESS_DESCRIPTION_RETRIES})", "WARNING")
                log_event(run_id, ActionType.DEBUG,
                          "process_description_validation", "failed", errors=pd_result.errors, attempt=pd_attempt)
                process_desc = heal_process_description(
                    process_desc, pd_result.errors, user_text, run_id=run_id,
                    attempt=pd_attempt, model=model,
                )
                pd_result = validate_process_description(process_desc)
            if not pd_result.ok:
                raise ValueError(f"Process Description invalide après {pd_attempt} corrections : {pd_result.errors}")
            if verbose:
                print_trace(3, total_steps, "Génération du Logic-Core depuis le Process Description")
            logic_core = generate_logic_core_from_pd(process_desc, run_id=run_id, model=model)

        logic_core = dict(logic_core)
        # Synthese initiale (retourne un dict de metriques)
        business_summary = print_business_summary(logic_core, step="Generation initiale", attempt=1)
        set_final_experiment_summary(run_id, business_summary, status="IN_PROGRESS")

        if verbose:
            print_trace(4, total_steps, "Validation du Logic-Core")
        val_res = validate_logic_core(logic_core, source_text=user_text if direct_logic_core is None else None)

        # Trace de la correction mecanique automatique (Niveau 1)
        if val_res.normalized_logic_core is not None:
            append_execution_trace(run_id, {
                "step": 2,
                "component": "validate.normalize_logic_core_graph",
                "action": "IN_MEMORY_AUTO_FIX",
                "status": "APPLIED" if val_res.ok else "PARTIAL_FIX",
            })
            logic_core = val_res.normalized_logic_core
            logic_core = dict(logic_core)

        log_event(
            run_id, ActionType.DEBUG, "logic_core_validation",
            "success" if val_res.ok else "failed",
            output_response=json.dumps(logic_core, ensure_ascii=False),
            errors=val_res.errors or None,
        )
        log_event(run_id, ActionType.DEBUG, "soundness_validation",
              "success" if val_res.ok else "failed",
              output={"valid": val_res.ok, "checks": {"connectivity": val_res.ok,
              "start_reachability": val_res.ok, "end_reachability": val_res.ok,
              "gateway_consistency": val_res.ok, "pool_sequence_flow_rules": val_res.ok},
              "errors": val_res.errors}, errors=val_res.errors or None)

        # Trace des erreurs brutes du validateur si echec (Niveau 2)
        if not val_res.ok:
            append_execution_trace(run_id, {
                "step": 3,
                "component": "validate.validate_logic_core",
                "action": "VALIDATION_FAILED",
                "raw_validator_errors": list(val_res.errors),
            })

        attempt = 0
        prev_error_count = len(val_res.errors)
        stopped_no_progress = False
        regenerated_once = False
        while not val_res.ok and attempt < max_heal_attempts and direct_logic_core is None:
            attempt += 1
            if verbose:
                print_trace(5, total_steps, f"Self-healing Logic-Core ({attempt}/{max_heal_attempts})", "WARNING")
            log_event(run_id, ActionType.DEBUG,
                      "logic_core_validation", "failed", errors=val_res.errors, attempt=attempt)

            # Trace du prompt envoye au LLM pour self-healing
            append_execution_trace(run_id, {
                "step": 4,
                "attempt": attempt,
                "component": "llm_agent.self_heal_logic_core",
                "action": "SELF_HEALING_PROMPT_SENT",
                "raw_errors_sent_to_llm": list(val_res.errors),
            })

            # Même stochasticité que l'amendement initial : une correction de
            # self-healing peut, par malchance, supprimer un ID existant en
            # plus de corriger l'erreur demandée (observé en usage réel, sur
            # des instructions n'ayant pourtant aucun rapport avec ces IDs).
            # On retente depuis le MÊME Logic-Core fautif (jamais depuis un
            # résultat déjà amputé, pour ne pas composer les dégâts), avec un
            # rappel explicite des IDs perdus à chaque nouvelle tentative.
            faulty_logic_core_for_heal = logic_core
            heal_errors = val_res.errors
            healed_logic_core = None
            missing_ids = set()
            prev_heal_missing_ids: set[str] | None = None
            heal_stable_missing = False
            for id_retry in range(1, MAX_AMENDMENT_RETRIES + 1):
                healed_logic_core = self_heal_logic_core(
                    faulty_logic_core_for_heal, heal_errors, user_context=user_text,
                    run_id=run_id, attempt=attempt, model=model,
                )
                if existing_logic_core is None:
                    break
                missing_ids = _illegitimate_missing_ids(existing_logic_core, healed_logic_core)
                if not missing_ids:
                    break
                # Même principe que pour l'amendement : un ensemble d'IDs
                # identique perdu deux fois de suite malgré le rappel explicite
                # est un comportement stable, pas un hasard — probablement une
                # conséquence légitime de la correction demandée.
                if missing_ids == prev_heal_missing_ids:
                    heal_stable_missing = True
                    break
                prev_heal_missing_ids = missing_ids
                if id_retry >= MAX_AMENDMENT_RETRIES:
                    raise ValueError(
                        f"Le self-healing a supprime des IDs existants après {MAX_AMENDMENT_RETRIES} "
                        f"tentatives : {sorted(missing_ids)}"
                    )
                if verbose:
                    print_trace(5, total_steps,
                                 f"Self-healing a supprimé des IDs existants — nouvelle tentative "
                                 f"({id_retry}/{MAX_AMENDMENT_RETRIES})", "WARNING")
                heal_errors = list(val_res.errors) + [
                    f"RAPPEL CRITIQUE : ta précédente tentative de correction a supprimé ces IDs existants, "
                    f"ce qui est INTERDIT — restaure-les impérativement, inchangés : {sorted(missing_ids)}"
                ]
            if missing_ids and heal_stable_missing and verbose:
                print_trace(5, total_steps,
                             f"Avertissement : ces IDs disparaissent de façon stable malgré le rappel "
                             f"explicite — probablement une conséquence légitime de la correction, "
                             f"acceptée : {sorted(missing_ids)}", "WARNING")
            logic_core = healed_logic_core
            val_res = validate_logic_core(logic_core, source_text=user_text if direct_logic_core is None else None)
            if val_res.normalized_logic_core is not None:
                logic_core = val_res.normalized_logic_core

            # Mise a jour du resume apres chaque iteration
            business_summary = print_business_summary(logic_core, step="Self-Healing", attempt=attempt)

            # Trace du resultat de re-validation apres self-healing
            append_execution_trace(run_id, {
                "step": 4,
                "attempt": attempt,
                "component": "llm_agent.self_heal_logic_core",
                "action": "SELF_HEALING_RESULT",
                "revalidation_success": val_res.ok,
                "remaining_raw_errors": list(val_res.errors) if not val_res.ok else [],
            })

            log_event(
                run_id, ActionType.DEBUG, "logic_core_revalidation",
                "success" if val_res.ok else "failed",
                output_response=json.dumps(logic_core, ensure_ascii=False),
                errors=val_res.errors or None,
                attempt=attempt,
            )

            # Coupe-circuit : si le nombre d'erreurs ne diminue pas d'une tentative à
            # l'autre, la boucle ne converge pas — continuer ne fait que muter la
            # structure au hasard (voire l'aggraver) plutôt que de la corriger.
            # Signaler un échec explicite maintenant plutôt que d'épuiser les
            # tentatives restantes sans progrès.
            if not val_res.ok and len(val_res.errors) >= prev_error_count:
                # Une structure qui ne progresse plus est souvent bancale à sa racine
                # (ex: un acteur jamais détecté à la génération initiale) : rafistoler
                # indéfiniment un Logic-Core déjà mal formé ne fait qu'empiler des
                # corrections ad hoc sans jamais converger. Avant d'abandonner, on
                # retourne UNE FOIS à la génération initiale depuis le Process
                # Description pour redétecter les acteurs/participants — un nouvel
                # essai de génération complet plutôt qu'un énième rafistolage.
                if process_desc is not None and not regenerated_once:
                    regenerated_once = True
                    if verbose:
                        print_trace(5, total_steps,
                                     f"Self-healing sans progrès à la tentative {attempt} — retour à la "
                                     "génération initiale du Logic-Core (redétection des acteurs)", "WARNING")
                    append_execution_trace(run_id, {
                        "step": 4,
                        "attempt": attempt,
                        "component": "llm_agent.generate_logic_core_from_pd",
                        "action": "SELF_HEALING_NO_PROGRESS_REGENERATE",
                        "prev_error_count": prev_error_count,
                        "new_error_count": len(val_res.errors),
                    })
                    logic_core = generate_logic_core_from_pd(process_desc, run_id=run_id, model=model)
                    logic_core = dict(logic_core)
                    val_res = validate_logic_core(logic_core, source_text=user_text if direct_logic_core is None else None)
                    if val_res.normalized_logic_core is not None:
                        logic_core = val_res.normalized_logic_core
                    business_summary = print_business_summary(logic_core, step="Régénération", attempt=attempt)
                    log_event(
                        run_id, ActionType.DEBUG, "logic_core_regeneration",
                        "success" if val_res.ok else "failed",
                        output_response=json.dumps(logic_core, ensure_ascii=False),
                        errors=val_res.errors or None,
                        attempt=attempt,
                    )
                    attempt = 0
                    prev_error_count = len(val_res.errors)
                    continue

                stopped_no_progress = True
                append_execution_trace(run_id, {
                    "step": 4,
                    "attempt": attempt,
                    "component": "llm_agent.self_heal_logic_core",
                    "action": "SELF_HEALING_NO_PROGRESS_STOP",
                    "prev_error_count": prev_error_count,
                    "new_error_count": len(val_res.errors),
                })
                if verbose:
                    print_trace(5, total_steps,
                                 f"Self-healing arrêté : aucune amélioration à la tentative {attempt} "
                                 f"({prev_error_count} -> {len(val_res.errors)} erreurs)", "ERROR")
                break
            prev_error_count = len(val_res.errors)

        if not val_res.ok:
            set_final_experiment_summary(run_id, business_summary, status="FAILED")
            reason = "aucune amélioration détectée entre deux tentatives" if stopped_no_progress else f"{attempt} corrections"
            raise ValueError(f"Logic-Core invalide apres {reason} : {val_res.errors}")

        # Les avertissements (non bloquants : GAPs métier, données mentionnées dans
        # le texte mais absentes du Logic-Core, etc.) étaient calculés par le
        # validateur mais jamais affichés — une omission silencieuse. On les
        # affiche toujours, succès ou non, pour ne rien perdre discrètement.
        if val_res.warnings:
            log_event(run_id, ActionType.DEBUG, "logic_core_warnings", "warning", errors=val_res.warnings)
            if verbose:
                for w in val_res.warnings:
                    print_trace(4, total_steps, f"Avertissement : {w}", "WARNING")

        if verbose:
            print_trace(6, total_steps, "Calcul du layout géométrique")
        layout = compute_layout(logic_core)
        log_event(run_id, ActionType.GENERATION, "layout", "success")
        if verbose:
            print_trace(7, total_steps, "Sérialisation BPMN XML")
        xml_str = generate_bpmn_xml(logic_core, layout)
        log_event(run_id, ActionType.GENERATION, "xml_generation", "success")
        if verbose:
            print_trace(8, total_steps, "Processus termine", "SUCCESS")
        set_final_experiment_summary(run_id, business_summary, status="SUCCESS")
        finalize_run(run_id, "success")
        return xml_str, logic_core
    except Exception:
        finalize_run(run_id, "failed")
        raise


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="BPMN Agent Enterprise : Générateur & Analyste de Processus BPMN 2.0."
    )
    parser.add_argument("text", nargs="?", help="Texte ou consigne décrivant le processus métier")
    parser.add_argument("--file", "-f", help="Fichier texte d'entrée (transcription d'entretien)")
    parser.add_argument("--logic-core", "-c", help="Logic-Core JSON existant (fichier local) pour mode amendement incrémental")
    parser.add_argument("--from-json", help="Compiler directement un fichier Logic-Core JSON en BPMN XML")
    parser.add_argument("--process-id", help="UUID d'un process existant en base MySQL : charge sa dernière version et enregistre une nouvelle version (édition incrémentale versionnée), au lieu d'une génération neuve")
    parser.add_argument("--process-name", help="Nom du nouveau process à créer ET sauvegarder en base MySQL (version 1) ; ignoré si --process-id est fourni. Sans --process-id ni --process-name, comportement inchangé (fichiers locaux uniquement, pas de base de données)")
    parser.add_argument("--out", "-o", default="process.bpmn", help="Fichier XML BPMN de sortie (.bpmn)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modèle Mistral AI à utiliser")
    parser.add_argument("--no-heal", action="store_true", help="Désactiver la boucle d'auto-réparation (self-healing)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Mode silencieux (pas de logs d'étapes)")
    args = parser.parse_args()

    verbose = not args.quiet

    # Mode versioning MySQL : import local (pas en tête de fichier) pour que
    # les usages sans --process-id/--process-name n'exigent jamais mysql-connector
    # ni un serveur MySQL joignable — comportement legacy strictement inchangé.
    db_parent_version_id = None
    if args.process_id or args.process_name:
        import db as _db
        _db.init_schema()

    existing = None
    if args.process_id:
        latest = _db.get_latest_version(args.process_id)
        if latest is None:
            parser.error(f"Aucune version trouvée en base pour --process-id={args.process_id}")
        existing = latest["logic_core_json"]
        db_parent_version_id = latest["version_id"]
    elif args.logic_core:
        existing = json.loads(Path(args.logic_core).read_text(encoding="utf-8"))

    direct_json = None
    if args.from_json:
        direct_json = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        user_text = None
    elif args.file:
        user_text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        user_text = args.text
    else:
        parser.error("Veuillez fournir une description, un fichier texte (--file) ou un JSON existant (--from-json).")

    max_heal = 0 if args.no_heal else MAX_SELF_HEALING_ATTEMPTS

    try:
        xml_content, logic_core = run_pipeline(
            user_text=user_text,
            existing_logic_core=existing,
            direct_logic_core=direct_json,
            max_heal_attempts=max_heal,
            verbose=verbose,
            model=args.model,
        )

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(xml_content, encoding="utf-8")

        # Sauvegarde synchrone du Logic-Core JSON pour les futures itérations d'atelier
        json_out_path = out_path.with_suffix(".logic-core.json")
        json_out_path.write_text(json.dumps(logic_core, ensure_ascii=False, indent=2), encoding="utf-8")

        if verbose:
            node_count = len(logic_core.get("nodes", []))
            edge_count = len(logic_core.get("edges", []))
            print(f"\n✨ Modèle BPMN 2.0 généré avec succès ({node_count} nœuds, {edge_count} transitions) !")
            print(f"📄 Schéma XML BPMN : {out_path.resolve()}")
            print(f"📦 Logic-Core JSON  : {json_out_path.resolve()}")

        # Persistance MySQL : exécutée SEULEMENT si --process-id ou --process-name
        # a été fourni, APRÈS que run_pipeline ait produit un résultat validé par
        # le même pipeline de validation/self-healing que le mode fichier — aucun
        # court-circuit de cette validation.
        if args.process_id:
            new_version_id = _db.add_version(
                args.process_id, user_text, logic_core, xml_content, db_parent_version_id,
            )
            if verbose:
                latest_meta = _db.get_latest_version(args.process_id)
                print(f"🗄️  Nouvelle version enregistrée en base : process_id={args.process_id}, "
                      f"version={latest_meta['version_number']}, version_id={new_version_id}")
        elif args.process_name:
            new_process_id = _db.create_process(args.process_name, user_text, logic_core, xml_content)
            if verbose:
                print(f"🗄️  Process créé et sauvegardé en base : process_id={new_process_id} (version 1)")

    except Exception as e:
        print(f"\n❌ Erreur : {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
