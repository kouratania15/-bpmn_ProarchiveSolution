
"""
Construction déterministe du texte affiché dans le chat de l'interface web, à
partir du Logic-Core et du résultat de validation — sans aucun appel LLM.

Module pur (aucun effet de bord, aucune I/O réseau) : testable sans clé API
Mistral ni base MySQL. Réutilise les constantes de classification déjà
définies dans validate.py plutôt que de les redéfinir.
"""
from __future__ import annotations

import re
from typing import Any

from validate import ACTIVITY_TYPES, GATEWAY_TYPES

# Les messages d'erreur de validate.py sont déjà rédigés en français
# explicatif ; seuls les préfixes techniques (codes de règle, préfixe de
# schéma JSON) ont besoin d'être retirés pour un affichage utilisateur — pas
# besoin d'un dictionnaire de traduction complet pour chaque message.
_TECHNICAL_PREFIX_RE = re.compile(r"^(SCHEMA[^:]*:|[A-Z][A-Z-]*-\d+:)\s*")


def _humanize_error(message: str) -> str:
    """Retire le préfixe technique éventuel (ex: 'SCHEMA nodes[2]:', 'GATEWAY-002:',
    'POOL-001:') d'un message d'erreur de validate.py."""
    return _TECHNICAL_PREFIX_RE.sub("", message).strip()


def _pool_names(logic_core: dict[str, Any]) -> list[str]:
    pools = logic_core.get("pools") or []
    return [p.get("name") or p.get("id") for p in pools if isinstance(p, dict)]


def _gateway_branch_count(node_id: Any, edges: list[dict[str, Any]]) -> int:
    return sum(
        1 for e in edges
        if isinstance(e, dict) and e.get("type") in ("sequenceFlow", None) and e.get("source") == node_id
    )


def _build_summary(logic_core: dict[str, Any]) -> dict[str, Any]:
    """Lit directement le Logic-Core (aucune réinterprétation) pour produire
    les données structurées du résumé : participants, activités, décisions
    (avec leur nombre de branches), événements de fin."""
    nodes = logic_core.get("nodes") or []
    edges = logic_core.get("edges") or []

    activities = [
        n.get("name") or n.get("id") for n in nodes
        if isinstance(n, dict) and n.get("type") in ACTIVITY_TYPES
    ]
    decisions = [
        {
            "name": n.get("name") or n.get("id"),
            "type": n.get("type"),
            "branches": _gateway_branch_count(n.get("id"), edges),
        }
        for n in nodes
        # Seuls les gateways divergents (points de décision réels) sont
        # retenus — un gateway de convergence (join) n'est pas une "décision"
        # au sens métier du terme.
        if isinstance(n, dict) and n.get("type") in GATEWAY_TYPES and n.get("gatewayDirection") != "converging"
    ]
    end_events = [
        n.get("name") or n.get("id") for n in nodes
        if isinstance(n, dict) and n.get("type") == "endEvent"
    ]

    return {
        "participants": _pool_names(logic_core),
        "activities": activities,
        "decisions": decisions,
        "end_events": end_events,
    }


def _format_success_message(summary: dict[str, Any]) -> str:
    parts: list[str] = []

    participants = summary["participants"]
    if not participants:
        parts.append("Le processus a été généré avec succès.")
    elif len(participants) == 1:
        parts.append(f"Le processus a été généré avec succès, porté par {participants[0]}.")
    else:
        parts.append(
            "Le processus a été généré avec succès, impliquant "
            f"{len(participants)} participants : {', '.join(participants)}."
        )

    activities = summary["activities"]
    n_act = len(activities)
    if n_act:
        suffix = "s" if n_act != 1 else ""
        parts.append(f"Il comprend {n_act} activité{suffix} : {', '.join(activities)}.")

    decisions = summary["decisions"]
    if decisions:
        described = [f"{d['name']} ({d['branches']} branches)" for d in decisions]
        n_dec = len(decisions)
        suffix = "s" if n_dec != 1 else ""
        parts.append(f"Le processus comporte {n_dec} point{suffix} de décision : {', '.join(described)}.")

    end_events = summary["end_events"]
    if end_events:
        parts.append(f"Le processus peut se terminer par : {', '.join(end_events)}.")

    return " ".join(parts)


def _format_failure_message(errors: list[str]) -> str:
    humanized = [_humanize_error(e) for e in errors]
    n = len(humanized)
    suffix = "s" if n != 1 else ""
    intro = f"Le schéma généré contient {n} problème{suffix} à corriger :"
    bullet_list = "\n".join(f"- {msg}" for msg in humanized)
    return f"{intro}\n{bullet_list}"


def format_result(
    ok: bool,
    logic_core: dict[str, Any] | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Construit la réponse déterministe affichée dans le chat de l'UI web.

    Retourne toujours un dict avec les clés :
      - "message" (str) : le texte principal à afficher dans la bulle de chat.
      - "summary" (dict | None) : données structurées (participants,
        activités, décisions, événements de fin) — présent uniquement si
        ok=True.
      - "errors_technical" (list[str] | None) : messages bruts de
        validate.py, non humanisés — présent uniquement si ok=False, destiné
        à un panneau repliable côté UI.
      - "warnings" (list[str]) : toujours présent (peut être vide).

    Ne fait jamais appel à un LLM : le texte est entièrement construit par
    formatage de chaînes à partir de données déjà structurées, pour rester
    reproductible et testable.
    """
    if ok:
        if logic_core is None:
            raise ValueError("logic_core est requis quand ok=True")
        summary = _build_summary(logic_core)
        message = _format_success_message(summary)
        if warnings:
            message += "\n\nAvertissements : " + " ".join(warnings)
        return {"message": message, "summary": summary, "errors_technical": None, "warnings": warnings}

    return {
        "message": _format_failure_message(errors),
        "summary": None,
        "errors_technical": errors,
        "warnings": warnings,
    }
