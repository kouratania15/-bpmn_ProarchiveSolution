"""
Module de validation structurelle, sémantique et topologique BPMN 2.0.
Inspiré des règles formelles de soundness de Workflow Nets et de Stieges/bpmn-generator.

Vérifications effectuées :
1. Structure globale & présence des champs obligatoires
2. Unicité stricte des identifiants (process, pools, lanes, nodes, edges)
3. Intégrité référentielle des sources et cibles
4. Soundness topologique : Reachability (connexité depuis startEvent et vers endEvent)
5. Validation des Boundary Events (hôte existant, pas d'entrée, au moins 1 sortie)
6. Conformité des Passerelles (Divergentes / Convergentes, branches étiquetées, 1 flux default max)
7. Ségrégation des Couloirs et Pools (interdiction de SequenceFlow inter-pools, validation MessageFlow)
8. Intégrité des sous-processus (référencement parent valide)
9. Détection des annotations GAP (lacunes métier)
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import jsonschema
except ImportError:
    jsonschema = None


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    normalized_logic_core: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


GATEWAY_TYPES = {
    "exclusiveGateway",
    "parallelGateway",
    "inclusiveGateway",
    "eventBasedGateway",
    "complexGateway",
}

ACTIVITY_TYPES = {
    "task",
    "userTask",
    "serviceTask",
    "scriptTask",
    "manualTask",
    "sendTask",
    "receiveTask",
    "businessRuleTask",
    "callActivity",
    "subProcess",
}

ARTIFACT_TYPES = {
    "dataObjectReference",
    "dataStoreReference",
    "dataInput",
    "dataOutput",
    "textAnnotation",
}

VALID_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


_EVENT_DEF_INFERENCE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("timer", ["wait", "hour", "heure", "délai", "delai", "timeout", "jour", "minute", "day"]),
    ("message", ["message", "notification", "réponse", "reponse", "reçoit", "recoit", "email", "courriel"]),
    ("signal", ["signal", "alerte", "diffuse"]),
    ("error", ["erreur", "error", "panne", "échec technique", "echec technique"]),
]


def _ensure_catch_and_boundary_event_definition(nodes: list[dict[str, Any]]) -> None:
    """Un intermediateCatchEvent/intermediateThrowEvent/boundaryEvent SANS
    eventDefinition valide (absent ou 'none') est TOUJOURS une erreur fatale en
    BPMN 2.0 — un événement de capture doit obligatoirement porter un
    déclencheur, ce n'est pas affaire de style. Observé en pratique : le
    self-healing peut échouer à corriger ce défaut sur plusieurs tentatives
    consécutives (il régénère un nœud sans rapport plutôt que de fixer le nœud
    fautif, cf. section 19.1 de SKILL.md), faisant échouer tout le pipeline
    après épuisement des tentatives au lieu d'une simple inférence locale.
    Réparé mécaniquement par inférence prudente à partir du nom (mots-clés
    fiables uniquement), avec 'conditional' comme repli générique sûr — un
    événement de capture sans déclencheur nommément identifiable représente le
    plus souvent une attente d'un état/condition (cf. section 8.1 de
    SKILL.md). Toujours annoté GAP : une inférence automatique n'est jamais
    silencieuse, elle doit rester vérifiable contre le texte source."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") not in ("intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent"):
            continue
        edef = node.get("eventDefinition")
        if edef and edef != "none":
            continue
        name_lower = (node.get("name") or "").lower()
        inferred = "conditional"
        for candidate, keywords in _EVENT_DEF_INFERENCE_KEYWORDS:
            if any(kw in name_lower for kw in keywords):
                inferred = candidate
                break
        node["eventDefinition"] = inferred
        _mark_auto_gap(
            node,
            f"eventDefinition manquant, inféré automatiquement comme '{inferred}' à partir du nom du nœud — "
            "à vérifier contre le texte source (cf. section 8.1 de SKILL.md).",
        )


def _sanitize_id(raw_id: str, existing_ids: set[str]) -> str:
    """Translittère un ID en ASCII strict conforme au schema (ex: accents FR),
    sans dépendre du LLM pour deviner la contrainte — il régénère sinon le
    même accent à chaque tentative de self-healing."""
    ascii_id = unicodedata.normalize("NFKD", raw_id).encode("ascii", "ignore").decode("ascii")
    ascii_id = re.sub(r"[^A-Za-z0-9_-]", "_", ascii_id).strip("_")
    if not ascii_id or not ascii_id[0].isalpha():
        ascii_id = "n_" + ascii_id if ascii_id else "n"
    candidate = ascii_id
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{ascii_id}_{suffix}"
        suffix += 1
    return candidate


PROCESS_DESCRIPTION_ID_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _sanitize_process_description_id(raw_id: str, existing_ids: set[str]) -> str:
    """Équivalent de _sanitize_id pour le Process Description, dont le schema
    impose des IDs strictement minuscules (^[a-z_][a-z0-9_]*$, pas de tiret,
    pas de majuscule). Un accent recopié depuis un nom métier en français
    (ex: 'expédition_commande') fait échouer la validation JSON-Schema ; sans
    cette passe mécanique, seule la correction LLM (heal_process_description)
    peut le réparer, et elle peut échouer plusieurs tentatives de suite sur le
    même accent (observé en usage réel)."""
    ascii_id = unicodedata.normalize("NFKD", raw_id).encode("ascii", "ignore").decode("ascii").lower()
    ascii_id = re.sub(r"[^a-z0-9_]", "_", ascii_id).strip("_")
    if not ascii_id or not (ascii_id[0].isalpha() or ascii_id[0] == "_"):
        ascii_id = "n_" + ascii_id if ascii_id else "n"
    candidate = ascii_id
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{ascii_id}_{suffix}"
        suffix += 1
    return candidate


def _sanitize_process_description_ids(process_desc: dict[str, Any]) -> None:
    """Assainit en place tout ID d'identified_elements non conforme au pattern
    du schema, et propage le renommage à toutes les références croisées
    connues (actor_id, source/target de relations et sequence_flows, outcome
    de branches de condition, related_element de gap)."""
    if not isinstance(process_desc, dict):
        return
    elements = process_desc.get("identified_elements")
    if not isinstance(elements, dict):
        return

    id_bearing_keys = ("participants", "activities", "events", "conditions", "gateways", "relations")
    existing_ids: set[str] = set()
    for key in id_bearing_keys:
        for item in elements.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                existing_ids.add(item["id"])

    rename_map: dict[str, str] = {}
    for key in id_bearing_keys:
        for item in elements.get(key) or []:
            if not isinstance(item, dict):
                continue
            iid = item.get("id")
            if isinstance(iid, str) and not PROCESS_DESCRIPTION_ID_PATTERN.match(iid):
                new_id = _sanitize_process_description_id(iid, existing_ids)
                existing_ids.discard(iid)
                existing_ids.add(new_id)
                rename_map[iid] = new_id
                item["id"] = new_id

    if not rename_map:
        return

    for item in elements.get("activities") or []:
        if isinstance(item, dict) and item.get("actor_id") in rename_map:
            item["actor_id"] = rename_map[item["actor_id"]]
    for key in ("relations", "sequence_flows"):
        for item in elements.get(key) or []:
            if not isinstance(item, dict):
                continue
            for side in ("source", "target"):
                if item.get(side) in rename_map:
                    item[side] = rename_map[item[side]]
    for item in elements.get("conditions") or []:
        if not isinstance(item, dict):
            continue
        for branch in item.get("branches") or []:
            if isinstance(branch, dict) and branch.get("outcome") in rename_map:
                branch["outcome"] = rename_map[branch["outcome"]]

    for gap in process_desc.get("gaps") or []:
        if isinstance(gap, dict) and gap.get("related_element") in rename_map:
            gap["related_element"] = rename_map[gap["related_element"]]


# ---------------------------------------------------------------------------
# Helpers de connectivité (utilisés par la passe de rattachement automatique)
# ---------------------------------------------------------------------------

def _bfs_reachable(
    start_ids: list[str],
    seq_edges: list[dict[str, Any]],
    boundary_map: dict[str, list[str]] | None = None,
) -> set[str]:
    """Retourne l'ensemble des ids atteignables par sequenceFlow depuis start_ids.

    `boundary_map` (host_id -> [boundaryEvent ids attachés]) permet de traiter un
    boundaryEvent comme atteint dès que son hôte l'est — un boundaryEvent n'a
    JAMAIS de sequenceFlow entrant (cf. section 15.2 de SKILL.md), sa sémantique
    BPMN de déclenchement passe par attachedToRef, pas par une arête. Sans ce
    traitement, un nœud qui n'est atteignable QUE via un boundaryEvent (ex: un
    gateway inséré juste après par _ensure_gateway_after_residual_multi_out) est
    vu à tort comme une composante déconnectée par cette BFS, ce qui déclenche un
    rattachement de secours erroné — potentiellement une arête bouclant vers
    l'hôte du boundaryEvent lui-même (cf. régression confirmée sur un scénario
    d'escalade timer, rapport de suivi post rapport_tests_v2.md)."""
    adj: dict[str, list[str]] = {}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str) and isinstance(t, str):
            adj.setdefault(s, []).append(t)
    visited: set[str] = set()
    stack = list(start_ids)
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for nxt in adj.get(cur, []):
            if nxt not in visited:
                stack.append(nxt)
        if boundary_map:
            for b_id in boundary_map.get(cur, []):
                if b_id not in visited:
                    stack.append(b_id)
    return visited


def _weakly_connected_components(node_ids: list[str], seq_edges: list[dict[str, Any]]) -> list[list[str]]:
    """Regroupe node_ids en composantes connexes (arêtes considérées non orientées),
    pour identifier des sous-graphes métier entiers qui flottent, déconnectés
    du reste, plutôt que de traiter chaque nœud isolément."""
    node_set = set(node_ids)
    adj: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if s in node_set and t in node_set:
            adj[s].add(t)
            adj[t].add(s)
    visited: set[str] = set()
    components: list[list[str]] = []
    for nid in node_ids:
        if nid in visited:
            continue
        comp: list[str] = []
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in visited:
                    stack.append(nxt)
        components.append(comp)
    return components


def _mark_auto_gap(node: dict[str, Any], reason: str) -> None:
    """Annote un nœud d'un GAP indiquant une reconnexion automatique de secours,
    pour que la connexion soit visible et distinguable d'une relation déduite
    du texte source (voir section 11 de validate_logic_core)."""
    existing = node.get("documentation") or ""
    tag = f"GAP: connexion automatique de secours — {reason} ; cohérence métier non garantie, à vérifier."

    def _canonicalize(text: str) -> str:
        text = re.sub(r"['\"][^'\"]+['\"]", "?", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip().lower()

    if "gap:" in existing.lower():
        normalized_existing = _canonicalize(existing)
        normalized_reason = _canonicalize(reason)
        if normalized_reason in normalized_existing or normalized_existing in normalized_reason:
            return

    if tag in existing:
        return
    node["documentation"] = (existing + " " + tag).strip()


def _is_antagonistic_branch_pair(label_a: str, label_b: str) -> bool:
    """Indique si deux libellés de branche représentent deux issues opposées."""
    left = (label_a or "").lower()
    right = (label_b or "").lower()
    pairs = [
        ("confirm", "reject"), ("confirm", "rejet"), ("confirmed", "rejected"),
        ("confirmée", "rejetée"), ("confirmé", "rejeté"),
        ("accept", "refus"), ("accept", "reject"), ("valid", "invalid"),
        ("validé", "refusé"), ("valide", "invalide"),
        ("approved", "rejected"), ("available", "unavailable"),
        ("disponible", "indisponible"), ("ok", "ko"), ("yes", "no"), ("oui", "non"),
    ]
    for a, b in pairs:
        if (a in left and b in right) or (b in left and a in right):
            return True
    return False


def _check_no_illegitimate_outcome_merge(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], source_text: str | None = None) -> list[str]:
    """Détecte les fusions d'issues métier opposées vers un même endEvent final sans passer par un XOR-join."""
    errors: list[str] = []
    if not nodes or not edges:
        return errors

    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    seq_edges = [
        e for e in edges
        if isinstance(e, dict)
        and e.get("type") in ("sequenceFlow", None)
        and isinstance(e.get("source"), str)
        and isinstance(e.get("target"), str)
    ]

    outgoing_by_node: dict[str, list[dict[str, Any]]] = {}
    incoming_by_node: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        outgoing_by_node.setdefault(e["source"], []).append(e)
        incoming_by_node.setdefault(e["target"], []).append(e)

    common_step_hint = False
    if source_text:
        common_step_hint = bool(re.search(r"(in all cases|dans tous les cas|finalement|quelle que soit|après la décision|après le choix|toujours|même dans tous les cas)", source_text, re.I))

    if common_step_hint:
        return errors

    def _business_outcome_category(text: str) -> str:
        low = (text or "").lower()
        if any(k in low for k in ["reject", "rejected", "refus", "invalid", "unavailable", "cancel", "denied", "not approved", "not valid", "ko", "no"]):
            return "reject"
        if any(k in low for k in ["confirm", "confirmed", "approved", "available", "valid", "accept", "grant", "ok", "yes", "ship", "processed"]):
            return "confirm"
        return "other"

    def _branch_business_category(start_target: str) -> str:
        visited: set[str] = set()
        stack = [start_target]
        categories: set[str] = set()
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            curr_node = node_map.get(curr)
            if isinstance(curr_node, dict):
                for label in (curr_node.get("name"), curr_node.get("documentation")):
                    if isinstance(label, str):
                        category = _business_outcome_category(label)
                        if category != "other":
                            categories.add(category)
            for e in outgoing_by_node.get(curr, []):
                stack.append(e["target"])
        return next(iter(sorted(categories))) if categories else "other"

    def get_reachable_ends_and_joins(start_target: str) -> tuple[set[str], set[str]]:
        """Trouve tous les endEvents et toutes les passerelles convergentes (XOR-join) atteignables depuis start_target."""
        reachable_ends: set[str] = set()
        reachable_joins: set[str] = set()
        visited: set[str] = set()
        stack = [start_target]
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            curr_node = node_map.get(curr, {})

            inc_count = len(incoming_by_node.get(curr, []))
            if curr_node.get("type") in GATEWAY_TYPES and (curr_node.get("gatewayDirection") == "converging" or inc_count >= 2):
                reachable_joins.add(curr)
                continue

            if curr_node.get("type") == "endEvent":
                reachable_ends.add(curr)
            out_edges = outgoing_by_node.get(curr, [])
            if not out_edges and curr_node.get("type") != "boundaryEvent":
                reachable_ends.add(curr)
            for e in out_edges:
                tgt = e["target"]
                if tgt not in visited:
                    stack.append(tgt)
        return reachable_ends, reachable_joins

    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or node.get("type") != "exclusiveGateway":
            continue

        outs = outgoing_by_node.get(nid, [])
        if len(outs) < 2:
            continue

        for i in range(len(outs)):
            for j in range(i + 1, len(outs)):
                e1 = outs[i]
                e2 = outs[j]
                label1 = str(e1.get("name") or e1.get("condition") or "")
                label2 = str(e2.get("name") or e2.get("condition") or "")

                if not _is_antagonistic_branch_pair(label1, label2):
                    continue

                ends1, joins1 = get_reachable_ends_and_joins(e1["target"])
                ends2, joins2 = get_reachable_ends_and_joins(e2["target"])

                if joins1.intersection(joins2):
                    continue

                category1 = _branch_business_category(e1["target"])
                category2 = _branch_business_category(e2["target"])
                if category1 == category2 and category1 in {"reject", "confirm"}:
                    continue

                shared_ends = ends1.intersection(ends2)
                for end_id in sorted(shared_ends):
                    errors.append(
                        f"Fusion suspecte d'issues métier opposées dans '{end_id}' : "
                        f"les branches opposées ('{label1}' et '{label2}') issues de la passerelle '{nid}' "
                        f"aboutissent au même événement/résultat de fin. "
                        f"Veuillez créer des endEvent distincts."
                    )

    return errors



def _check_timer_coverage(logic_core: dict[str, Any], source_text: str | None = None) -> list[str]:
    """Détecte un délai exprimé dans le texte sans timer BPMN associé."""
    if not source_text:
        return []
    patterns = [
        r"within\s+\d+\s*(hour|hours|minute|minutes|day|days)",
        r"after\s+\d+\s*(hour|hours|minute|minutes|day|days)",
        r"sous\s+\d+\s*(heure|heures|minute|minutes|jour|jours)",
        r"dans\s+un\s+délai\s+de",
        r"timeout",
        r"délai",
    ]
    if not any(re.search(pattern, source_text, re.I) for pattern in patterns):
        return []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    has_timer = any(isinstance(node, dict) and node.get("eventDefinition") == "timer" for node in nodes)
    if has_timer:
        return []
    return [
        "Le texte source mentionne un délai temporel mais aucun événement 'timer' n'a été modélisé. Ajouter un eventDefinition: timer avec eventDetail au format ISO 8601 correspondant."
    ]


def _check_lane_coverage_for_multi_actor(logic_core: dict[str, Any], source_text: str | None = None) -> list[str]:
    """Vérifie qu'un processus multi-acteurs dispose de lanes distinctes."""
    if not source_text:
        return []
    actors = [
        "customer", "client", "employee", "manager", "system", "employe", "gestionnaire",
        "agent", "supplier", "fournisseur", "bank", "banque", "company", "entreprise",
        "conseiller", "analyste", "responsable", "directeur", "directrice", "chef",
        "gerant", "gérant", "assistant", "assistante", "superviseur", "auditeur",
        "comptable", "juriste", "technicien", "ingenieur", "ingénieur", "vendeur",
        "acheteur", "livreur", "controleur", "contrôleur", "administrateur",
    ]
    mentions = []
    lower_text = source_text.lower()
    for actor in actors:
        if actor in lower_text and actor not in mentions:
            mentions.append(actor)
    if len(mentions) < 3:
        return []

    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    if any(isinstance(node, dict) and node.get("laneId") for node in nodes):
        return []
    pools = logic_core.get("pools", []) if isinstance(logic_core, dict) else []
    if len(pools) > 1:
        return []
    return [
        f"Le texte implique {len(mentions)} rôles distincts ({', '.join(mentions)}) mais aucune lane n'a été créée. Créer une lane par rôle dans un pool unique (cf. règle 6.3 de SKILL.md)."
    ]


def _check_matching_join_for_split(logic_core: dict[str, Any]) -> list[str]:
    """Un split inclusiveGateway/parallelGateway doit toujours être refermé par un
    gateway convergent du MÊME type avant d'atteindre une tâche commune. Contrairement
    à exclusiveGateway (une seule branche active, fusion implicite autorisée, cf. 15.6),
    une fusion implicite après un split inclusif/parallèle est structurellement invalide :
    plusieurs branches actives simultanément exécuteraient la tâche commune en double
    sans synchronisation (cf. règle 10.2/10.3 de SKILL.md)."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}
    seq_edges = [e for e in edges if isinstance(e, dict) and e.get("type") in ("sequenceFlow", None)]

    outgoing: dict[str, list[str]] = {}
    incoming_count: dict[str, int] = {}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str):
            outgoing.setdefault(s, []).append(t)
        if isinstance(t, str):
            incoming_count[t] = incoming_count.get(t, 0) + 1

    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in ("inclusiveGateway", "parallelGateway"):
            continue
        if node.get("gatewayDirection") == "converging":
            continue
        nid = node.get("id")
        outs = outgoing.get(nid, [])
        if len(outs) < 2:
            continue

        # Pour chaque branche, avance tant qu'il n'y a qu'un seul chemin possible ;
        # s'arrête au premier gateway rencontré ou à toute bifurcation/fusion (frontière
        # naturelle de synchronisation).
        boundaries: list[str | None] = []
        for start in outs:
            visited: set[str] = set()
            cur: str | None = start
            boundary: str | None = None
            while isinstance(cur, str) and cur not in visited:
                visited.add(cur)
                cur_node = node_map.get(cur, {})
                if cur_node.get("type") in GATEWAY_TYPES:
                    boundary = cur
                    break
                if incoming_count.get(cur, 0) >= 2:
                    # Point de convergence (2+ prédécesseurs) : c'est ICI qu'il faut
                    # vérifier la présence d'un join, même si ce nœud n'a lui-même
                    # qu'une seule sortie — sinon on continue de marcher au-delà du
                    # vrai point de fusion jusqu'au prochain nœud à sortie multiple
                    # (ex: l'endEvent final), et la fusion illégale passe inaperçue.
                    boundary = cur
                    break
                nxts = outgoing.get(cur, [])
                if len(nxts) != 1:
                    boundary = cur
                    break
                cur = nxts[0]
            boundaries.append(boundary)

        seen: dict[str, int] = {}
        for b in boundaries:
            if b:
                seen[b] = seen.get(b, 0) + 1

        for boundary_id, count in seen.items():
            if count < 2:
                continue
            boundary_node = node_map.get(boundary_id, {})
            if boundary_node.get("type") == "endEvent":
                # Converger vers un endEvent est du BPMN standard : terminer un jeton
                # ne requiert aucune synchronisation entre branches, contrairement à
                # l'exécution d'une tâche partagée qui serait déclenchée en double.
                continue
            is_valid_join = (
                boundary_node.get("type") == node.get("type")
                and incoming_count.get(boundary_id, 0) >= 2
            )
            if is_valid_join:
                continue
            errors.append(
                f"Le split '{nid}' ({node.get('type')}) n'est jamais refermé par un gateway convergent du même "
                f"type : {count} de ses branches se rejoignent directement sur '{boundary_id}' "
                f"({boundary_node.get('type') or 'nœud inconnu'}) au lieu de passer par un "
                f"{node.get('type')} convergent (gatewayDirection='converging'). Ajouter ce gateway de "
                "convergence avant cette étape commune (règle 10.2/10.3 de SKILL.md)."
            )
    return errors


def _check_message_events_and_flows(logic_core: dict[str, Any]) -> list[str]:
    """Un événement message n'a de sens que s'il communique réellement avec un autre
    pool ; un messageFlow ne doit jamais partir/arriver sur un gateway (nœud de
    contrôle interne, pas un point de communication). Un événement error/signal isolé,
    simple maillon de passage sans throw/catch correspondant, est presque toujours une
    condition évaluée par un gateway déguisée en événement. Sans ce garde-fou, le LLM
    insère parfois un événement (message, error, signal...) ou un messageFlow entre
    deux nœuds purement internes, ce qui n'a aucune justification textuelle (cf.
    section 3 de SKILL.md)."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    message_flow_endpoints: set[str] = set()
    for e in edges:
        if not isinstance(e, dict) or e.get("type") != "messageFlow":
            continue
        for side in ("source", "target"):
            val = e.get(side)
            if isinstance(val, str):
                message_flow_endpoints.add(val)
                endpoint_node = node_map.get(val, {})
                if endpoint_node.get("type") in GATEWAY_TYPES:
                    errors.append(
                        f"Le messageFlow '{e.get('id')}' relie un gateway ('{val}'), qui n'est pas un point de "
                        "communication valide. Un messageFlow doit relier des tâches/événements, jamais un "
                        "gateway — modéliser la décision en sequenceFlow interne et ne faire porter le "
                        "messageFlow que sur la tâche d'envoi/réception concernée."
                    )

    incoming_seq_count: dict[str, int] = {}
    outgoing_seq_count: dict[str, int] = {}
    for e in edges:
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str):
            outgoing_seq_count[s] = outgoing_seq_count.get(s, 0) + 1
        if isinstance(t, str):
            incoming_seq_count[t] = incoming_seq_count.get(t, 0) + 1

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") not in ("intermediateCatchEvent", "intermediateThrowEvent"):
            continue
        ev_def = node.get("eventDefinition")
        nid = node.get("id")
        if ev_def == "message":
            if nid not in message_flow_endpoints:
                errors.append(
                    f"L'événement '{nid}' ({node.get('name')}) est de type message "
                    "(eventDefinition='message') mais n'est relié à aucun messageFlow réel vers/depuis un autre "
                    "pool. Si aucune communication externe n'est décrite dans le texte, remplacer cet événement "
                    "par un sequenceFlow direct entre les deux tâches internes (cf. section 3 de SKILL.md)."
                )
            continue
        if ev_def in ("error", "signal", "escalation"):
            # Un événement error/signal isolé, simple maillon de passage dans la chaîne
            # (1 entrée, 1 sortie, aucun messageFlow), sans throw/catch correspondant
            # ailleurs, n'est presque toujours qu'une condition évaluée déguisée en
            # événement — cf. section 3 de SKILL.md : gateway -> tâche en sequenceFlow direct.
            if nid in message_flow_endpoints:
                continue
            has_matching_throw_or_catch = any(
                isinstance(other, dict) and other.get("id") != nid
                and other.get("type") in ("intermediateCatchEvent", "intermediateThrowEvent", "endEvent", "boundaryEvent")
                and other.get("eventDefinition") == ev_def
                for other in nodes
            )
            if has_matching_throw_or_catch:
                continue
            if incoming_seq_count.get(nid, 0) == 1 and outgoing_seq_count.get(nid, 0) == 1:
                errors.append(
                    f"L'événement '{nid}' ({node.get('name')}) est de type {ev_def} "
                    f"(eventDefinition='{ev_def}') mais n'est qu'un simple maillon de passage dans la chaîne "
                    "(1 entrée, 1 sortie, aucun messageFlow, aucun throw/catch correspondant ailleurs). C'est "
                    "probablement une condition évaluée par un gateway déguisée en événement — remplacer par "
                    "un sequenceFlow direct entre le gateway et la tâche suivante (cf. section 3 de SKILL.md)."
                )
    return errors


def _split_cross_gateway_merged_node(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
) -> None:
    """Répare mécaniquement le pattern détecté par _check_cross_gateway_task_merge :
    un nœud non-gateway atteint depuis 2+ gateways distincts et non liés (pas les
    branches sœurs d'un même gateway) est presque toujours la fusion erronée de
    deux mentions textuelles distinctes (cf. section 2 de SKILL.md). Rendu
    structurellement impossible plutôt que seulement détecté après coup — même
    principe que _split_erroneously_merged_tasks (BUG 2), étendu ici au cas où
    la fusion illégitime survient par CONVERGENCE depuis des gateways différents
    plutôt que par un fan-out direct (régression confirmée : ce câblage peut être
    introduit PAR une étape de self-healing elle-même)."""
    incoming_by_target: dict[str, list[dict[str, Any]]] = {}
    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(t, str):
            incoming_by_target.setdefault(t, []).append(e)
        if isinstance(s, str):
            outgoing_by_source.setdefault(s, []).append(e)

    def _nearest_ancestor_gateway(start_id: str) -> str | None:
        visited: set[str] = set()
        stack = [start_id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            cur_node = node_map.get(cur, {})
            if cur_node.get("type") in GATEWAY_TYPES:
                return cur
            for e in incoming_by_target.get(cur, []):
                src = e.get("source")
                if isinstance(src, str):
                    stack.append(src)
        return None

    for node in list(nodes):
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or node.get("type") in GATEWAY_TYPES or node.get("type") in ("endEvent", "startEvent", "boundaryEvent"):
            continue
        ins = incoming_by_target.get(nid, [])
        if len(ins) < 2:
            continue

        gateway_groups: dict[str, list[dict[str, Any]]] = {}
        none_group: list[dict[str, Any]] = []
        for e in ins:
            src = e.get("source")
            anc = _nearest_ancestor_gateway(src) if isinstance(src, str) else None
            if anc is None:
                none_group.append(e)
            else:
                gateway_groups.setdefault(anc, []).append(e)

        # Même condition de déclenchement EXACTE que _check_cross_gateway_task_merge :
        # au moins 2 gateways ancêtres distincts (les prédécesseurs sans gateway
        # ancêtre ne comptent pas dans le seuil, cf. check d'origine).
        if len(gateway_groups) < 2:
            continue

        sorted_gw_ids = sorted(gateway_groups.keys())
        outs_template = outgoing_by_source.get(nid, [])

        for i, gw_id in enumerate(sorted_gw_ids):
            if i == 0:
                continue  # le premier groupe (+ les prédécesseurs sans gateway ancêtre) reste sur le nœud d'origine
            group_edges = gateway_groups[gw_id]
            new_id = f"{nid}_2"
            suffix = 3
            while new_id in node_map:
                new_id = f"{nid}_{suffix}"
                suffix += 1
            new_node = {**node, "id": new_id}
            node_map[new_id] = new_node
            nodes.append(new_node)
            for e in group_edges:
                e["target"] = new_id
            for out_e in outs_template:
                clone_out = dict(out_e)
                clone_out["id"] = f"{out_e.get('id')}_{new_id}"
                clone_out["source"] = new_id
                edges.append(clone_out)
                seq_edges.append(clone_out)
            _mark_auto_gap(
                new_node,
                f"nœud dédoublé automatiquement : atteint depuis le gateway '{gw_id}', non lié au gateway "
                f"d'origine de '{nid}' — fusion erronée probable de deux mentions textuelles distinctes "
                "(cf. section 2 de SKILL.md).",
            )


def _check_cross_gateway_task_merge(logic_core: dict[str, Any]) -> list[str]:
    """Un nœud non-gateway atteint depuis DEUX gateways différents et non liés
    (chaque chemin remontant à un gateway distinct, pas aux branches sœurs d'un même
    gateway) est presque toujours la fusion erronée de deux mentions textuelles
    distinctes en un seul nœud partagé (ex: deux 'informer le client' à des étapes
    différentes du processus) — cf. section 2 de SKILL.md. La fusion implicite de
    branches SŒURS d'un même gateway reste normale et n'est pas signalée (règle 15.6)."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    incoming_by_target: dict[str, list[str]] = {}
    for e in edges:
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str) and isinstance(t, str):
            incoming_by_target.setdefault(t, []).append(s)

    def _nearest_ancestor_gateway(start_id: str) -> str | None:
        visited: set[str] = set()
        stack = [start_id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            cur_node = node_map.get(cur, {})
            if cur_node.get("type") in GATEWAY_TYPES:
                return cur
            for src in incoming_by_target.get(cur, []):
                stack.append(src)
        return None

    for nid, preds in incoming_by_target.items():
        node = node_map.get(nid, {})
        if node.get("type") in GATEWAY_TYPES or node.get("type") in ("endEvent", "startEvent", "boundaryEvent"):
            continue
        if len(preds) < 2:
            continue
        ancestor_gateways = {_nearest_ancestor_gateway(p) for p in preds}
        ancestor_gateways.discard(None)
        if len(ancestor_gateways) >= 2:
            errors.append(
                f"Le nœud '{nid}' ({node.get('name')}) est atteint depuis {len(ancestor_gateways)} gateways "
                f"différents et non liés ({sorted(ancestor_gateways)}), pas depuis les branches sœurs d'un "
                "même gateway. C'est probablement la fusion erronée de deux mentions textuelles distinctes "
                "(ex: deux 'informer le client' à des étapes différentes) en un seul nœud partagé — séparer "
                "en autant de nœuds distincts que d'occurrences textuelles (cf. section 2 de SKILL.md)."
            )
    return errors


def _check_data_object_flow_type(logic_core: dict[str, Any]) -> list[str]:
    """Un Data Object/Data Store/Data Input/Data Output ne circule JAMAIS par
    sequenceFlow ni messageFlow — uniquement par association (data association),
    une ligne pointillée fine distincte des deux autres types de flux. Cf. la
    section Data Objects de SKILL.md."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    artifact_ids = {
        n.get("id") for n in nodes
        if isinstance(n, dict) and n.get("type") in ARTIFACT_TYPES and isinstance(n.get("id"), str)
    }
    if not artifact_ids:
        return []
    for e in edges:
        if not isinstance(e, dict):
            continue
        etype = e.get("type")
        if etype not in ("sequenceFlow", "messageFlow", None):
            continue
        src, tgt = e.get("source"), e.get("target")
        if src in artifact_ids or tgt in artifact_ids:
            artifact_id = src if src in artifact_ids else tgt
            errors.append(
                f"Le flux '{e.get('id')}' ({etype or 'sequenceFlow'}) touche l'artefact '{artifact_id}' "
                "(Data Object/Data Store). Un artefact ne circule jamais par sequenceFlow ni messageFlow : "
                "remplacer ce flux par une 'association' (ou 'dataInputAssociation'/'dataOutputAssociation')."
            )
    return errors


_DATA_OBJECT_TRIGGER_PATTERNS = [
    (r"\ben utilisant\b", "en utilisant"),
    (r"\ben se basant sur\b", "en se basant sur"),
    (r"\bà partir d[eu']\b", "à partir de"),
    (r"\bsur la base d[eu']\b", "sur la base de"),
    (r"\benregistre dans\b", "enregistre dans"),
    (r"\bconsulte\b", "consulte"),
    (r"\barchive dans\b", "archive dans"),
    (r"\bproduit le document\b", "produit le document"),
    (r"\bmet à jour le dossier\b", "met à jour le dossier"),
    (r"\bbased on\b", "based on"),
    (r"\busing the\b", "using the"),
    (r"\brecords? (?:it |this )?in\b", "records in"),
]


_EXTERNAL_ORG_KEYWORDS_RE = re.compile(
    r"\b(externe|extérieur|partenaire|tiers|prestataire|sous-traitant|fournisseur|supplier|"
    r"banque|bank|external|third[- ]party|vendor|outsourc)\w*",
    re.I,
)


def _check_pool_split_without_external_evidence(logic_core: dict[str, Any], source_text: str | None) -> list[str]:
    """AVERTISSEMENT (non bloquant) — BUG 1 de rapport_tests_v2.md : confirmé de
    façon récurrente (5+ tests) que le renforcement du prompt seul (section 6.1/
    6.2 de SKILL.md) ne suffit pas à empêcher la création d'une pool séparée pour
    un rôle interne au nom institutionnel ('Service Financier', 'Expert senior',
    'Service de Compensation', 'Système Informatique'...). Contrairement à
    l'heuristique inclusive/exclusive retirée cette session (jugement sémantique
    fin par nœud, 3 faux positifs), ce contrôle est volontairement GROSSIER et
    global au texte entier (présence d'AU MOINS UN mot d'appartenance externe
    n'importe où dans le texte) plutôt que par pool individuelle — un texte
    décrivant une VRAIE organisation externe emploie presque toujours au moins un
    de ces mots quelque part (cf. les exemples de référence banque/fournisseur de
    la section 6.2), alors qu'un texte purement interne n'en emploie aucun. Reste
    un AVERTISSEMENT, jamais une erreur bloquante ni une correction automatique :
    le risque de faux positif sur un vocabulaire métier imprévu est réel, et une
    pool légitime ne doit jamais être supprimée sur la seule foi de ce signal."""
    if not source_text:
        return []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    pools = logic_core.get("pools", []) if isinstance(logic_core, dict) else []
    if len(pools) < 2:
        return []
    if _EXTERNAL_ORG_KEYWORDS_RE.search(source_text):
        return []
    pool_names = [p.get("name", p.get("id")) for p in pools if isinstance(p, dict)]
    return [
        f"{len(pools)} pools ont été créées ({pool_names}) mais aucun mot d'appartenance externe "
        "('externe', 'partenaire', 'fournisseur', 'banque', 'prestataire', 'tiers'...) n'apparaît dans le "
        "texte source. Vérifier qu'aucun rôle interne à l'organisation (service, département, expert) n'a été "
        "isolé à tort dans sa propre pool au lieu d'une lane (cf. section 6.1.bis de SKILL.md)."
    ]


_NAME_TRAILING_NUMBER_RE = re.compile(r"^(.*?)\s*[\s_#-]*(\d+)\s*$")


def _check_multi_instance_modeled_as_named_branches(logic_core: dict[str, Any]) -> list[str]:
    """AVERTISSEMENT (non bloquant) — BUG 6 de rapport_tests_v2.md : une itération
    sur une collection ('pour chaque X de la liste') est parfois modélisée à tort
    comme N tâches NOMMÉES distinctes reliées par un exclusiveGateway (ex:
    'Validation par le responsable 1' / '... 2') plutôt qu'une seule tâche avec
    loopCharacteristics. Détection purement SYNTAXIQUE (même libellé à l'exception
    d'un numéro final) et non sémantique, pour éviter la fragilité d'une
    heuristique de langage naturel (cf. heuristique inclusive/exclusive retirée
    cette session après 3 faux positifs) : ne se déclenche que sur un motif
    mécanique et vérifiable, jamais sur une supposition de sens."""
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    warnings: list[str] = []
    for gw in nodes:
        if not isinstance(gw, dict) or gw.get("type") != "exclusiveGateway":
            continue
        gw_id = gw.get("id")
        targets = [
            e.get("target") for e in edges
            if isinstance(e, dict) and e.get("type") in ("sequenceFlow", None) and e.get("source") == gw_id
        ]
        stems: dict[str, list[str]] = {}
        for tid in targets:
            tnode = node_map.get(tid)
            if not isinstance(tnode, dict) or tnode.get("type") not in ACTIVITY_TYPES:
                continue
            name = tnode.get("name") or ""
            match = _NAME_TRAILING_NUMBER_RE.match(name.strip())
            if not match or not match.group(1).strip():
                continue
            stems.setdefault(match.group(1).strip().lower(), []).append(tid)

        for stem, ids in stems.items():
            if len(ids) >= 2:
                warnings.append(
                    f"Les tâches {sorted(ids)} issues de la passerelle exclusive '{gw_id}' partagent le même "
                    f"libellé à un numéro près ('{stem} 1', '{stem} 2', ...) : ceci ressemble à une itération sur "
                    "une collection modélisée à tort comme des branches nommées distinctes plutôt qu'une tâche "
                    "unique avec loopCharacteristics multi-instance (cf. section 9.1 de SKILL.md)."
                )
    return warnings


_THRESHOLD_COUNT_PATTERNS = [
    r"au moins\s+\d+\s+(?:des?|sur)\s+\d+",
    r"\d+\s+(?:sur|parmi)\s+\d+\s+(?:critères?|conditions?|validations?|votes?|approbations?)",
    r"at least\s+\d+\s+(?:of|out of)\s+\d+",
]


def _check_threshold_count_needs_complex_gateway(logic_core: dict[str, Any], source_text: str | None) -> list[str]:
    """AVERTISSEMENT (non bloquant) — BUG 8 de rapport_tests_v2.md : un texte
    décrivant un seuil de comptage ('au moins N des M critères') appelle un
    complexGateway (cf. section 10.5 de SKILL.md) ; l'absence totale de
    complexGateway dans le Logic-Core alors que ce motif textuel est présent est
    un signal fiable d'approximation par exclusive/inclusive standard."""
    if not source_text:
        return []
    if not any(re.search(p, source_text, re.I) for p in _THRESHOLD_COUNT_PATTERNS):
        return []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    has_complex = any(isinstance(n, dict) and n.get("type") == "complexGateway" for n in nodes)
    if has_complex:
        return []
    return [
        "Le texte source semble décrire un seuil de comptage de conditions (ex: 'au moins N des M critères') "
        "mais aucun complexGateway n'a été modélisé. Vérifier si un exclusiveGateway/inclusiveGateway a été "
        "utilisé à la place pour approximer cette sémantique (cf. section 10.5 de SKILL.md)."
    ]


_LOOP_TEXT_KEYWORDS_RE = re.compile(
    r"\b(tant que|jusqu['’]à ce que|à nouveau|de nouveau|répéter|renouveler|encore une fois|"
    r"as long as|until|again|repeat)\b",
    re.I,
)


def _check_unjustified_backward_loop(logic_core: dict[str, Any], source_text: str | None) -> list[str]:
    """AVERTISSEMENT (non bloquant) — BUG 8 de rapport_tests_v2.md : une boucle
    arrière (cycle détecté dans le graphe sequenceFlow) sans aucun mot-clé de
    répétition dans le texte source est un signal fiable d'un câblage erroné
    (retour vers une étape déjà passée sans preuve textuelle), pas d'une vraie
    boucle métier (cf. règle 15.5 de SKILL.md). Détection par un vrai algorithme
    de cycle sur le graphe orienté, pas par un ordre de création des nœuds."""
    if source_text and _LOOP_TEXT_KEYWORDS_RE.search(source_text):
        return []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    adj: dict[str, list[str]] = {}
    for e in edges:
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str) and isinstance(t, str):
            adj.setdefault(s, []).append(t)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n.get("id"): WHITE for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}
    back_edges: list[tuple[str, str]] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v) == GRAY:
                back_edges.append((u, v))
            elif color.get(v) == WHITE:
                dfs(v)
        color[u] = BLACK

    for nid in list(color):
        if color.get(nid) == WHITE:
            dfs(nid)

    if not back_edges:
        return []
    return [
        f"Boucle arrière détectée dans le graphe ({', '.join(f'{s}->{t}' for s, t in back_edges)}) sans aucun "
        "mot-clé de répétition dans le texte source ('tant que', 'jusqu'à ce que', 'à nouveau'...) : vérifier "
        "qu'il ne s'agit pas d'un câblage erroné vers une étape déjà passée (cf. règle 15.5 de SKILL.md)."
    ]


def _check_missing_data_objects_warning(logic_core: dict[str, Any], source_text: str | None) -> list[str]:
    """AVERTISSEMENT (non bloquant) : le texte source contient un déclencheur textuel
    explicite de Data Object/Data Store (cf. section Data Objects de SKILL.md) mais
    le Logic-Core généré n'en contient AUCUN. Signale une omission silencieuse au
    lieu de la laisser disparaître sans trace — cf. demande explicite de l'auteur.
    Volontairement une simple présence/absence globale (pas un appariement par
    tâche) pour rester fiable : un contrôle plus fin par tâche risquerait les mêmes
    faux positifs qu'une précédente heuristique texte retirée cette session."""
    if not source_text:
        return []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    has_data_object = any(
        isinstance(n, dict) and n.get("type") in ("dataObjectReference", "dataStoreReference", "dataInput", "dataOutput")
        for n in nodes
    )
    if has_data_object:
        return []
    matched = [label for pattern, label in _DATA_OBJECT_TRIGGER_PATTERNS if re.search(pattern, source_text, re.I)]
    if not matched:
        return []
    return [
        f"Le texte source semble mentionner une donnée/document manipulé (tournure détectée : "
        f"'{matched[0]}') mais aucun dataObjectReference/dataStoreReference n'a été créé dans le "
        "Logic-Core. Vérifier si une donnée explicitement citée dans le texte a été omise "
        "(cf. section Data Objects de SKILL.md)."
    ]


def _check_transaction_subprocess(logic_core: dict[str, Any]) -> list[str]:
    """BUG 7 de rapport_tests_v2.md : une annulation groupée ('cancelEventDefinition',
    bpmn:transaction) n'a de sens qu'à l'intérieur d'un sous-processus transactionnel
    (isTransaction=true) — jamais isolée. Règle déterministe, pas une heuristique de
    langage : l'emplacement structurel d'un cancel event est un invariant BPMN 2.0
    strict, vérifiable sans ambiguïté."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    transaction_ids = {
        n.get("id") for n in nodes
        if isinstance(n, dict) and n.get("type") == "subProcess" and n.get("isTransaction") and isinstance(n.get("id"), str)
    }

    for node in nodes:
        if not isinstance(node, dict) or node.get("eventDefinition") != "cancel":
            continue
        nid = node.get("id")
        if node.get("type") == "boundaryEvent":
            host = node.get("attachedToRef")
            if host not in transaction_ids:
                errors.append(
                    f"Le boundaryEvent '{nid}' est de type cancel (eventDefinition='cancel') mais n'est pas "
                    f"attaché ('attachedToRef') à un subProcess transactionnel (isTransaction=true). Un cancel "
                    "event n'est valide qu'en bordure d'une transaction BPMN (cf. section 9.1 de SKILL.md)."
                )
        elif node.get("type") == "endEvent":
            parent = node.get("parentSubProcessId")
            if parent not in transaction_ids:
                errors.append(
                    f"L'endEvent '{nid}' est de type cancel (eventDefinition='cancel') mais n'est pas interne "
                    "('parentSubProcessId') à un subProcess transactionnel (isTransaction=true). Un cancelEndEvent "
                    "n'est valide qu'à l'intérieur d'une transaction BPMN (cf. section 9.1 de SKILL.md)."
                )
        else:
            errors.append(
                f"Le nœud '{nid}' ({node.get('type')}) porte eventDefinition='cancel', valide uniquement sur un "
                "endEvent interne ou un boundaryEvent attaché à un subProcess transactionnel."
            )

    return errors


def _check_missing_transaction_cancel_warning(logic_core: dict[str, Any]) -> list[str]:
    """AVERTISSEMENT (non bloquant) : un subProcess transactionnel sans aucun
    cancelEndEvent/boundaryEvent cancel ne modélise aucune annulation groupée
    réelle — juste une bordure double sans effet (cf. BUG 7 de rapport_tests_v2.md)."""
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    transaction_ids = {
        n.get("id") for n in nodes
        if isinstance(n, dict) and n.get("type") == "subProcess" and n.get("isTransaction") and isinstance(n.get("id"), str)
    }
    warnings: list[str] = []
    for tx_id in transaction_ids:
        has_cancel = any(
            isinstance(n, dict) and n.get("eventDefinition") == "cancel"
            and (n.get("attachedToRef") == tx_id or n.get("parentSubProcessId") == tx_id)
            for n in nodes
        )
        if not has_cancel:
            warnings.append(
                f"Le subProcess transactionnel '{tx_id}' n'a aucun cancelEndEvent interne ni boundaryEvent "
                "cancel attaché : l'annulation groupée en cas d'échec n'est pas modélisée (cf. section 9.1 de "
                "SKILL.md)."
            )
    return warnings


def _check_subprocess_boundary_edges(logic_core: dict[str, Any]) -> list[str]:
    """Un sequenceFlow ne doit jamais relier un subProcess à l'un de ses propres
    enfants (parentSubProcessId le référençant), ni dans un sens ni dans l'autre :
    le flux entrant cible le subProcess comme boîte noire, le point d'entrée interne
    est déduit de l'enfant sans prédécesseur interne (cf. section 9.1 de SKILL.md).
    Une telle arête est de toute façon invalide en BPMN 2.0 strict (un sequenceFlow
    ne peut pas traverser la frontière de containment d'un subProcess)."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    child_to_parent: dict[str, str] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        parent = n.get("parentSubProcessId")
        nid = n.get("id")
        if isinstance(parent, str) and isinstance(nid, str):
            child_to_parent[nid] = parent

    for e in edges:
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        src, tgt = e.get("source"), e.get("target")
        if not isinstance(src, str) or not isinstance(tgt, str):
            continue
        if child_to_parent.get(tgt) == src or child_to_parent.get(src) == tgt:
            errors.append(
                f"Le sequenceFlow '{e.get('id')}' relie directement le subProcess '{src if src in child_to_parent.values() else tgt}' "
                f"à son propre enfant ('{tgt if child_to_parent.get(tgt) else src}'). Supprimer cette arête : le "
                "point d'entrée interne du subProcess est déduit automatiquement de l'enfant sans prédécesseur "
                "parmi les autres enfants (cf. section 9.1 de SKILL.md)."
            )
    return errors


_WAIT_TASK_NAME_RE = re.compile(
    r"\b(attend(?:re|s)?|patient(?:e|er)|wait(?:s|ing)?)\b", re.I
)


def _splice_out_wait_task_before_event_based_gateway(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
) -> None:
    """BUG 9 de rapport_tests_v2.md : le point de divergence d'une attente du
    PREMIER événement parmi plusieurs concurrents DOIT être l'eventBasedGateway
    lui-même — jamais une tâche "attendre X ou Y" insérée juste avant (cf. section
    10.4 de SKILL.md). Une telle tâche, quand elle n'a qu'un seul flux entrant et
    un seul flux sortant vers un eventBasedGateway, est mécaniquement supprimable
    sans perte d'information : son unique rôle narratif (l'attente) est déjà
    porté par le gateway et ses catch events, donc la retirer et rebrancher son
    prédécesseur directement sur le gateway est une simplification sûre, jamais
    une correction ambiguë."""
    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}
    incoming_by_target: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str):
            outgoing_by_source.setdefault(s, []).append(e)
        if isinstance(t, str):
            incoming_by_target.setdefault(t, []).append(e)

    for node in list(nodes):
        if not isinstance(node, dict) or node.get("type") in GATEWAY_TYPES:
            continue
        nid = node.get("id")
        if not isinstance(nid, str):
            continue
        name = node.get("name") or ""
        if not _WAIT_TASK_NAME_RE.search(name):
            continue
        outs = outgoing_by_source.get(nid, [])
        if len(outs) != 1:
            continue
        target_node = node_map.get(outs[0].get("target"), {})
        if target_node.get("type") != "eventBasedGateway":
            continue

        ins = incoming_by_target.get(nid, [])
        out_edge = outs[0]
        gw_id = target_node.get("id")

        for in_edge in ins:
            in_edge["target"] = gw_id
        edges.remove(out_edge)
        if out_edge in seq_edges:
            seq_edges.remove(out_edge)
        nodes.remove(node)
        node_map.pop(nid, None)
        if isinstance(gw_id, str):
            _mark_auto_gap(
                target_node,
                f"tâche d'attente '{nid}' supprimée automatiquement : le point de divergence d'un "
                "eventBasedGateway doit être le gateway lui-même, jamais précédé d'une tâche 'attendre X ou Y' "
                "(cf. section 10.4 de SKILL.md).",
            )


def _split_erroneously_merged_tasks(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
) -> None:
    """Un nœud non-gateway avec N sequenceFlow sortants ET exactement N entrants
    (N >= 2) est presque toujours la fusion erronée de N tâches distinctes issues
    de branches différentes (cf. section 2 de SKILL.md) — deux occurrences
    textuelles au nom similaire fusionnées à tort. Compter sur le LLM pour le
    scinder à chaque self-healing s'est révélé fragile en pratique (la fusion
    peut même être introduite PAR le self-healing en corrigeant autre chose).
    Cette réparation le rend structurellement impossible plutôt que simplement
    détecté après coup : le nœud est dédoublé automatiquement, chaque entrant
    apparié positionnellement à un sortant, à chaque normalisation — y compris
    après le self-healing, qui rappelle normalize_logic_core_graph."""
    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}
    incoming_by_target: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str):
            outgoing_by_source.setdefault(s, []).append(e)
        if isinstance(t, str):
            incoming_by_target.setdefault(t, []).append(e)

    for node in list(nodes):
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or node.get("type") in GATEWAY_TYPES:
            continue
        outs = outgoing_by_source.get(nid, [])
        ins = incoming_by_target.get(nid, [])
        if len(outs) < 2 or len(outs) != len(ins):
            continue  # pas le pattern univoque 1:1 -> laisser au self-healing LLM

        for i in range(1, len(outs)):
            new_id = f"{nid}_{i + 1}"
            suffix = 2
            while new_id in node_map:
                new_id = f"{nid}_{i + 1}_{suffix}"
                suffix += 1
            new_node = {**node, "id": new_id}
            node_map[new_id] = new_node
            nodes.append(new_node)
            ins[i]["target"] = new_id
            outs[i]["source"] = new_id


_RETRY_TEXT_KEYWORDS_RE = re.compile(
    r"\b(retry|retries|réessai\w*|nouvelle tentative|recommence\w*|à nouveau la même tâche)\b",
    re.I,
)


def _ensure_subprocess_children_share_pool(nodes: list[dict[str, Any]], node_map: dict[str, dict[str, Any]]) -> None:
    """Tout enfant d'un subProcess (parentSubProcessId) DOIT appartenir à la
    MÊME pool que son conteneur — un subProcess est une boîte noire unique du
    point de vue des pools (cf. section 9.1 de SKILL.md), il ne peut jamais
    avoir des enfants répartis dans des pools différentes. Régression
    confirmée : le start event interne d'un sous-processus événementiel
    déclenché par un acteur externe hérite parfois à tort le poolId de cet
    acteur EXTERNE (celui qui déclenche l'événement) au lieu du poolId de son
    propre conteneur — produisant un sequenceFlow interne qui traverse deux
    pools, une erreur fatale (POOL-001) que le self-healing ne corrige pas de
    façon fiable malgré plusieurs tentatives."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        parent_id = node.get("parentSubProcessId")
        if not isinstance(parent_id, str):
            continue
        parent_node = node_map.get(parent_id)
        if not isinstance(parent_node, dict):
            continue
        parent_pool = parent_node.get("poolId")
        if isinstance(parent_pool, str) and node.get("poolId") != parent_pool:
            _mark_auto_gap(
                node,
                f"poolId corrigé automatiquement en '{parent_pool}' pour correspondre à son subProcess "
                f"conteneur '{parent_id}' : un enfant de subProcess ne peut jamais appartenir à une pool "
                "différente de son conteneur (cf. section 9.1 de SKILL.md).",
            )
            node["poolId"] = parent_pool


def _fix_gateway_pool_crossing_edges(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    pools: list[dict[str, Any]],
) -> None:
    """Un gateway ne peut JAMAIS être un point de communication inter-pools (ni
    source ni cible d'un messageFlow, ni relié directement par un sequenceFlow
    à un nœud d'une autre pool) — cf. section 10.7 de SKILL.md. Deux régressions
    confirmées (sous-processus événementiel + sous-processus transactionnel,
    suivi de rapport_tests_v2.md) montrent que le rappel dans le prompt de
    self-healing (llm_agent.py) n'est PAS appliqué de façon fiable : le
    self-healing épuise ses tentatives sans converger, malgré une explication
    textuelle correcte du correctif attendu. Rendu structurellement impossible
    ici, sur le même principe que le fix mécanique du BUG 2 : insertion
    automatique d'une tâche intermédiaire dans la pool du gateway, qui porte
    seule le messageFlow vers/depuis l'autre pool — le gateway ne garde qu'un
    sequenceFlow classique vers/depuis cette tâche, dans sa propre pool."""
    pool_name_by_id = {p.get("id"): p.get("name", p.get("id")) for p in pools if isinstance(p, dict) and isinstance(p.get("id"), str)}

    def _unique_task_id(base: str) -> str:
        candidate = f"{base}_notify"
        suffix = 2
        while candidate in node_map:
            candidate = f"{base}_notify_{suffix}"
            suffix += 1
        return candidate

    for e in list(seq_edges):
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        src, tgt = e.get("source"), e.get("target")
        src_node = node_map.get(src)
        tgt_node = node_map.get(tgt)
        if not isinstance(src_node, dict) or not isinstance(tgt_node, dict):
            continue
        src_pool = src_node.get("poolId")
        tgt_pool = tgt_node.get("poolId")
        if not (isinstance(src_pool, str) and isinstance(tgt_pool, str) and src_pool != tgt_pool):
            continue

        src_is_gw = src_node.get("type") in GATEWAY_TYPES
        tgt_is_gw = tgt_node.get("type") in GATEWAY_TYPES
        if src_is_gw == tgt_is_gw:
            # Ni l'un ni l'autre (cas géré ailleurs par l'auto-conversion en
            # messageFlow), ou les deux à la fois (cas dégénéré trop rare et
            # ambigu pour une réparation mécanique fiable) : laisser tel quel.
            continue

        if src_is_gw:
            # Direction A : le gateway est la SOURCE -> insérer la tâche relais
            # dans la pool du gateway, elle seule porte le messageFlow sortant.
            new_id = _unique_task_id(src)
            label = e.get("name") or e.get("condition")
            task_name = f"Transmettre : {label}" if label else f"Notifier {pool_name_by_id.get(tgt_pool, tgt_pool)}"
            new_task: dict[str, Any] = {"id": new_id, "type": "task", "name": task_name, "poolId": src_pool}
            node_map[new_id] = new_task
            nodes.append(new_task)
            e["target"] = new_id  # reste un sequenceFlow, maintenant entièrement dans la pool du gateway
            msg_edge = {"id": f"{e.get('id')}_msg", "source": new_id, "target": tgt, "type": "messageFlow"}
            edges.append(msg_edge)
            _mark_auto_gap(
                new_task,
                f"tâche relais insérée automatiquement entre le gateway '{src}' et la pool '{tgt_pool}' : un "
                "gateway ne peut jamais porter directement un messageFlow inter-pool (cf. section 10.7 de "
                "SKILL.md).",
            )
        else:
            # Direction B : le gateway est la CIBLE -> insérer la tâche relais
            # dans la pool du gateway, elle seule reçoit le messageFlow entrant.
            new_id = _unique_task_id(tgt)
            new_task = {
                "id": new_id, "type": "task",
                "name": f"Recevoir déclenchement de {pool_name_by_id.get(src_pool, src_pool)}",
                "poolId": tgt_pool,
            }
            node_map[new_id] = new_task
            nodes.append(new_task)
            e["target"] = new_id
            e["type"] = "messageFlow"
            if e in seq_edges:
                seq_edges.remove(e)
            new_seq_edge = {"id": f"{e.get('id')}_seq", "source": new_id, "target": tgt, "type": "sequenceFlow"}
            edges.append(new_seq_edge)
            seq_edges.append(new_seq_edge)
            _mark_auto_gap(
                new_task,
                f"tâche relais insérée automatiquement entre la pool '{src_pool}' et le gateway '{tgt}' : un "
                "gateway ne peut jamais recevoir directement un messageFlow inter-pool (cf. section 10.7 de "
                "SKILL.md).",
            )


def _remove_boundary_event_reboop_to_own_host(
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    source_text: str | None,
) -> None:
    """Un boundaryEvent attaché à une tâche T ne doit JAMAIS voir sa branche
    reboucler vers T elle-même — directement (le boundaryEvent lui-même ciblant
    T) OU indirectement, à N sauts, via une tâche de sa propre branche (ex:
    timer -> escalade -> prise en charge -> [reboop] -> T). Un boundary event
    ouvre une branche latérale INDÉPENDANTE (ex: escalade en parallèle), il ne
    redémarre jamais l'activité à laquelle il est attaché — le cas indirect a
    été observé en pratique (le self-healing route l'edge de reboop un ou
    plusieurs sauts plus loin dans la branche plutôt que sur l'edge immédiate
    du boundaryEvent, ce qui échappait à une détection limitée au premier saut).
    On retire ICI uniquement l'arête qui CIBLE T (pas toute la branche), à
    n'importe quelle profondeur dans le sous-graphe atteignable depuis la sortie
    du boundaryEvent — sauf si le texte source décrit explicitement une nouvelle
    tentative ('retry', 'réessaie', 'nouvelle tentative'), cf. section 8.1 de
    SKILL.md."""
    if source_text and _RETRY_TEXT_KEYWORDS_RE.search(source_text):
        return

    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        s = e.get("source")
        if isinstance(s, str):
            outgoing_by_source.setdefault(s, []).append(e)

    for node in list(node_map.values()):
        if not isinstance(node, dict) or node.get("type") != "boundaryEvent":
            continue
        host = node.get("attachedToRef")
        bid = node.get("id")
        if not isinstance(host, str) or not isinstance(bid, str):
            continue

        visited: set[str] = set()
        stack = [bid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            for e in list(outgoing_by_source.get(cur, [])):
                if e.get("target") == host:
                    if e in edges:
                        edges.remove(e)
                    if e in seq_edges:
                        seq_edges.remove(e)
                    outgoing_by_source[cur] = [x for x in outgoing_by_source.get(cur, []) if x is not e]
                    _mark_auto_gap(
                        node,
                        f"flux erroné vers son propre hôte ('{host}') retiré automatiquement depuis '{cur}' : "
                        "un boundaryEvent (et toute sa branche) n'a jamais vocation à redémarrer l'activité à "
                        "laquelle il est attaché (cf. section 8.1 de SKILL.md).",
                    )
                    continue
                nxt = e.get("target")
                if isinstance(nxt, str) and nxt not in visited:
                    stack.append(nxt)


def _ensure_gateway_after_residual_multi_out(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
) -> None:
    """Filet de sécurité structurel pour BUG 2 (rapport_tests_v2.md) : après
    _split_erroneously_merged_tasks (qui traite le cas univoque N entrants = N
    sortants), TOUT nœud non-gateway encore muni de 2+ sequenceFlow sortants —
    notamment le cas asymétrique (ex: 1 entrant, 2 sortants) qu'une fusion
    symétrique ne couvre pas — doit être rendu structurellement valide AVANT
    que le validateur ne puisse même le voir, pas seulement détecté après coup
    par _check_non_gateway_branching. Un nœud pareil est TOUJOURS un pattern
    invalide en BPMN 2.0 (seul un gateway peut porter plusieurs branches) ;
    la correction la plus sûre par défaut, quelle que soit l'origine du défaut
    (génération initiale ou régression introduite PAR une étape de
    self-healing), est d'insérer un exclusiveGateway juste après ce nœud et d'y
    rattacher ses flux sortants existants (noms/conditions préservés) — jamais
    de fusionner davantage ni de laisser l'erreur fatale atteindre la sortie
    finale du pipeline après épuisement des tentatives de self-healing.
    """
    outgoing_by_source: dict[str, list[dict[str, Any]]] = {}
    for e in seq_edges:
        s = e.get("source")
        if isinstance(s, str):
            outgoing_by_source.setdefault(s, []).append(e)

    for node in list(nodes):
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or node.get("type") in GATEWAY_TYPES:
            continue
        outs = outgoing_by_source.get(nid, [])
        if len(outs) < 2:
            continue

        # Des doublons exacts (même cible) ne sont pas une vraie branche : les
        # dédupliquer d'abord évite d'insérer un gateway à une seule sortie utile
        # (structurellement valide mais sans aucun sens), qui ne ferait que
        # déplacer le problème sans le résoudre.
        seen_targets: set[str] = set()
        deduped_outs = []
        for out_edge in outs:
            tgt = out_edge.get("target")
            if tgt in seen_targets:
                edges.remove(out_edge)
                if out_edge in seq_edges:
                    seq_edges.remove(out_edge)
                continue
            seen_targets.add(tgt)
            deduped_outs.append(out_edge)
        outs = deduped_outs
        outgoing_by_source[nid] = outs
        if len(outs) < 2:
            continue

        gw_id = f"{nid}_split_gw"
        suffix = 2
        while gw_id in node_map:
            gw_id = f"{nid}_split_gw_{suffix}"
            suffix += 1
        gw_node: dict[str, Any] = {
            "id": gw_id,
            "type": "exclusiveGateway",
            "name": "",
            "gatewayDirection": "diverging",
        }
        if isinstance(node.get("poolId"), str):
            gw_node["poolId"] = node["poolId"]
        if isinstance(node.get("laneId"), str):
            gw_node["laneId"] = node["laneId"]
        node_map[gw_id] = gw_node
        nodes.append(gw_node)

        for out_edge in outs:
            out_edge["source"] = gw_id

        new_edge = {"id": f"flow_{nid}_to_{gw_id}", "source": nid, "target": gw_id, "type": "sequenceFlow"}
        node_map_ids = {n.get("id") for n in nodes}
        if new_edge["id"] in {e.get("id") for e in edges}:
            new_edge["id"] = f"{new_edge['id']}_2"
        edges.append(new_edge)
        seq_edges.append(new_edge)
        outgoing_by_source[nid] = [new_edge]
        outgoing_by_source[gw_id] = outs

        _mark_auto_gap(
            node,
            f"un exclusiveGateway ('{gw_id}') a été inséré automatiquement après ce nœud car il portait "
            "plusieurs sequenceFlow sortants sans passerelle — vérifier qu'il ne s'agit pas d'une fusion "
            "erronée de deux mentions textuelles distinctes plutôt que d'une vraie décision.",
        )


def _check_non_gateway_branching(logic_core: dict[str, Any]) -> list[str]:
    """Un nœud qui n'est pas un gateway ne doit avoir qu'UN SEUL sequenceFlow sortant.
    Plusieurs sequenceFlow (souvent nommés/conditionnés) partant directement d'une
    tâche ou d'un événement, sans passer par un gateway explicite, est une erreur de
    modélisation : la décision doit être portée par un exclusiveGateway/inclusiveGateway
    inséré juste après ce nœud. Ce défaut produit typiquement un graphe incohérent où
    un événement intermédiaire injustifié n'est traversé que par une seule des branches
    (cf. section 3 de SKILL.md)."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    edges = logic_core.get("edges", []) if isinstance(logic_core, dict) else []
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}

    outgoing_count: dict[str, int] = {}
    for e in edges:
        if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
            continue
        s = e.get("source")
        if isinstance(s, str):
            outgoing_count[s] = outgoing_count.get(s, 0) + 1

    for nid, count in outgoing_count.items():
        if count < 2:
            continue
        node = node_map.get(nid, {})
        if node.get("type") in GATEWAY_TYPES:
            continue
        errors.append(
            f"Le nœud '{nid}' ({node.get('type') or 'inconnu'}) a {count} sequenceFlow sortants alors qu'il "
            "n'est pas un gateway. Seul un gateway peut porter plusieurs branches : insérer un "
            "exclusiveGateway/inclusiveGateway juste après ce nœud et y rattacher ces branches."
        )
    return errors


def _check_named_end_events(logic_core: dict[str, Any]) -> list[str]:
    """Chaque endEvent ET chaque boundaryEvent doit avoir un nom explicite — un
    élément sans nom (cercle vide) est toujours une erreur de modélisation. Pour
    un boundaryEvent en particulier, l'absence de nom est un signal fiable que
    le type de déclencheur n'a pas été correctement classifié en amont (ex: un
    délai confondu avec une escalade générique, cf. BUG 3 de rapport_tests_v2.md)
    plutôt qu'un simple oubli cosmétique — la génération doit être reprise depuis
    la classification du déclencheur, pas poursuivie avec un nœud incomplet."""
    errors: list[str] = []
    nodes = logic_core.get("nodes", []) if isinstance(logic_core, dict) else []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in ("endEvent", "boundaryEvent"):
            continue
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            continue
        if node.get("type") == "endEvent":
            errors.append(
                f"L'endEvent '{node.get('id')}' n'a pas de nom explicite. Chaque endEvent doit décrire l'issue "
                "métier qu'il représente (ex: 'Prêt approuvé', 'Commande annulée')."
            )
        else:
            errors.append(
                f"Le boundaryEvent '{node.get('id')}' (eventDefinition='{node.get('eventDefinition')}') n'a pas "
                "de nom explicite. C'est le signe que le type de déclencheur n'a pas été correctement identifié "
                "en amont (ex: un délai exprimé en unité de temps doit être un boundaryEvent timer nommé, jamais "
                "une escalade générique sans nom) — reprendre la classification du déclencheur plutôt que de "
                "nommer arbitrairement ce nœud."
            )
    return errors



# NOTE : une heuristique texte tentant de détecter "conditions quasi-identiques non
# mutuellement exclusives" (pour suggérer inclusiveGateway) a été essayée ici et
# retirée après 3 faux positifs distincts en usage réel (commande.json, test_loop,
# et 'approuvé'/'rejeté' non reconnu comme antonyme). Une liste d'antonymes/négations
# codée en dur ne peut pas suivre la richesse du langage naturel de façon fiable, et
# un faux positif ici pousse le self-healing à dégrader une structure déjà correcte.
# La détection exclusif/inclusif reste gérée par la règle 10.3 de SKILL.md (guidage
# à la génération) + le contrôle déterministe _check_matching_join_for_split
# (qui, lui, ne dépend d'aucune heuristique de langage).


_SEND_TASK_NAME_RE = re.compile(
    r"^(?:envoyer|envoie|transmettre|transmet|notifier|notifie|informer|informe)\s+"
    r"(?:un|une|le|la|les|des|l['’])?\s*(.+?)"
    r"(?:\s+(?:au|à la|aux|à|vers)\s+.+)?$",
    re.I,
)
_SEND_TASK_NAME_RE_EN = re.compile(
    r"^(?:send|sends|notify|notifies|inform|informs|transmit|transmits)\s+"
    r"(?:a|an|the)?\s*(.+?)(?:\s+to\s+.+)?$",
    re.I,
)


def _derive_end_event_name_for_send_task(task_name: str) -> str:
    """Dérive un nom d'endEvent business-meaningful à partir du nom d'une tâche
    d'envoi/notification laissée en impasse (ex: 'Envoyer un message de réclamation
    au fournisseur' -> 'Réclamation envoyée'), plutôt que le générique 'Fin
    (rattachement automatique)' qui ne dit rien du contexte métier."""
    text = (task_name or "").strip()
    match = _SEND_TASK_NAME_RE.match(text)
    if match:
        obj = re.sub(r"^(message|notification)\s+de\s+", "", match.group(1).strip(), flags=re.I)
        if obj:
            return f"{obj[0].upper()}{obj[1:]} envoyé(e)"
    match_en = _SEND_TASK_NAME_RE_EN.match(text)
    if match_en:
        obj = match_en.group(1).strip()
        if obj:
            return f"{obj[0].upper()}{obj[1:]} sent"
    return f"{text} (terminé)" if text else "Fin (rattachement automatique)"


def _ensure_subprocess_internal_events(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    add_edge: Callable[..., None],
) -> None:
    """Un subProcess doit avoir son propre startEvent et endEvent, internes à ses
    limites et distincts de ceux du processus parent — pas seulement un ensemble de
    tâches reliées entre elles. Comme pour un processus normal (cf. section 3 de
    SKILL.md : les événements structurels sont exemptés de la fidélité stricte), on
    complète automatiquement plutôt que d'exiger que le LLM y pense à chaque fois.
    """
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        parent = n.get("parentSubProcessId") if isinstance(n, dict) else None
        if isinstance(parent, str):
            children_by_parent.setdefault(parent, []).append(n)

    for sp_id, children in children_by_parent.items():
        child_ids = {c["id"] for c in children if isinstance(c.get("id"), str)}
        if not child_ids:
            continue
        has_start = any(c.get("type") == "startEvent" for c in children)
        has_end = any(c.get("type") == "endEvent" for c in children)

        incoming_within = {
            e.get("target") for e in seq_edges
            if e.get("source") in child_ids and e.get("target") in child_ids
        }
        outgoing_within = {
            e.get("source") for e in seq_edges
            if e.get("source") in child_ids and e.get("target") in child_ids
        }

        if not has_start:
            entry_candidates = sorted(child_ids - incoming_within)
            entry = entry_candidates[0] if entry_candidates else next(iter(sorted(child_ids)), None)
            if entry:
                start_id = f"start_{sp_id}"
                suffix = 2
                while start_id in node_map:
                    start_id = f"start_{sp_id}_{suffix}"
                    suffix += 1
                start_node: dict[str, Any] = {
                    "id": start_id, "type": "startEvent", "name": "Début",
                    "eventDefinition": "none", "parentSubProcessId": sp_id,
                }
                node_map[start_id] = start_node
                nodes.append(start_node)
                add_edge(start_id, entry)

        if not has_end:
            exit_candidates = sorted(child_ids - outgoing_within)
            exit_node = exit_candidates[-1] if exit_candidates else None
            if exit_node:
                end_id = f"end_{sp_id}"
                suffix = 2
                while end_id in node_map:
                    end_id = f"end_{sp_id}_{suffix}"
                    suffix += 1
                end_node: dict[str, Any] = {
                    "id": end_id, "type": "endEvent", "name": "Fin",
                    "eventDefinition": "none", "parentSubProcessId": sp_id,
                }
                node_map[end_id] = end_node
                nodes.append(end_node)
                add_edge(exit_node, end_id)


def _ensure_named_end_for_dangling_sends(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    message_edges: list[dict[str, Any]] | None,
    add_edge: Callable[..., None],
) -> None:
    """Une tâche d'envoi/notification en impasse (aucun sequenceFlow ni messageFlow
    sortant) doit se terminer par SON PROPRE endEvent nommé d'après son contexte
    métier — pas être absorbée silencieusement dans le endEvent de secours générique
    et partagé de _ensure_full_connectivity. Sans cette passe dédiée, une branche
    métier réellement terminale (ex: 'envoyer une réclamation') perd son identité
    au profit d'un endEvent anonyme partagé avec d'autres impasses sans rapport.
    """
    outgoing_seq: dict[str, int] = {}
    for e in seq_edges:
        s = e.get("source")
        if isinstance(s, str):
            outgoing_seq[s] = outgoing_seq.get(s, 0) + 1
    outgoing_msg_sources = {
        e.get("source") for e in (message_edges or [])
        if isinstance(e, dict) and isinstance(e.get("source"), str)
    }

    non_connectable = {"startEvent", "endEvent", "boundaryEvent"} | ARTIFACT_TYPES | GATEWAY_TYPES
    for node in list(nodes):
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or node.get("type") in non_connectable:
            continue
        if node.get("isForCompensation"):
            # Une activité de compensation n'est JAMAIS reliée au flux normal par
            # sequenceFlow (cf. section 8.1 de SKILL.md) : ce n'est pas une impasse
            # à combler, elle n'est atteinte que via une association de compensation.
            continue
        if isinstance(node.get("parentSubProcessId"), str):
            # Le dernier enfant d'un subProcess n'a normalement aucune sortie interne
            # (son "issue" est le sequenceFlow sortant du subProcess lui-même) : ce
            # n'est pas une impasse à combler par un endEvent synthétique.
            continue
        if outgoing_seq.get(nid, 0) > 0 or nid in outgoing_msg_sources:
            continue
        name = node.get("name") or ""
        if not (_SEND_TASK_NAME_RE.match(name.strip()) or _SEND_TASK_NAME_RE_EN.match(name.strip())):
            continue

        end_name = _derive_end_event_name_for_send_task(name)
        end_id = f"end_{nid}"
        suffix = 2
        while end_id in node_map:
            end_id = f"end_{nid}_{suffix}"
            suffix += 1
        end_node: dict[str, Any] = {"id": end_id, "type": "endEvent", "name": end_name}
        if isinstance(node.get("poolId"), str):
            end_node["poolId"] = node["poolId"]
        node_map[end_id] = end_node
        nodes.append(end_node)
        add_edge(nid, end_id)
        outgoing_seq[nid] = outgoing_seq.get(nid, 0) + 1


def _ensure_full_connectivity(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    add_edge: Callable[..., None],
    message_edges: list[dict[str, Any]] | None = None,
) -> None:
    """
    Garantit, en dernier recours et pour N'IMPORTE QUEL processus métier, que
    tous les nœuds sont reliés au flux principal : atteignables depuis un
    startEvent et capables d'atteindre un endEvent.

    Cette passe existe parce que le Logic-Core est généré par un second appel
    LLM (generate_logic_core_from_pd) indépendant des relations déjà validées
    au niveau du Process Description : rien ne garantit que Mistral relie
    effectivement sa propre chaîne de tâches au startEvent. Sans cette passe,
    un sous-graphe métier entier peut flotter, complètement déconnecté, et le
    self-healing LLM n'a alors qu'un message d'erreur textuel pour deviner
    comment tout reconnecter — ce qui échoue régulièrement après plusieurs
    tentatives.

    Chaque arête ajoutée ici est tracée comme GAP sur le nœud concerné :
    la connexion est garantie mécaniquement, PAS déduite du texte source.
    """
    order_index = {n["id"]: i for i, n in enumerate(nodes)}
    message_targets = {
        e.get("target")
        for e in (message_edges or [])
        if isinstance(e, dict) and isinstance(e.get("target"), str)
    }
    # Un boundaryEvent n'a jamais de sequenceFlow entrant : il est "atteint" via
    # attachedToRef dès que son hôte l'est (cf. docstring de _bfs_reachable).
    boundary_map: dict[str, list[str]] = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("type") == "boundaryEvent" and isinstance(n.get("attachedToRef"), str) and isinstance(n.get("id"), str):
            boundary_map.setdefault(n["attachedToRef"], []).append(n["id"])
    # Un startEvent/endEvent interne à un subProcess (parentSubProcessId défini)
    # n'est pas un point d'entrée/sortie du PROCESSUS PARENT : l'inclure fausserait
    # le calcul d'accessibilité du flux principal (cf. section 9.1 de SKILL.md).
    start_ids = [
        n["id"] for n in nodes
        if n.get("type") == "startEvent" and not isinstance(n.get("parentSubProcessId"), str)
    ] + sorted(message_targets)
    end_ids = [
        n["id"] for n in nodes
        if n.get("type") == "endEvent" and not isinstance(n.get("parentSubProcessId"), str)
    ]
    non_connectable = {"startEvent", "endEvent", "boundaryEvent"} | ARTIFACT_TYPES
    # Les enfants de subProcess (parentSubProcessId) ne doivent jamais être rattachés
    # au flux principal depuis l'extérieur : leur connectivité est interne au
    # subProcess (garantie par leurs propres sequenceFlow entre enfants), et le
    # subProcess lui-même — pas ses enfants — porte la connexion vers le reste du
    # diagramme (cf. section 9.1 de SKILL.md).
    subprocess_child_ids = {n["id"] for n in nodes if isinstance(n.get("parentSubProcessId"), str)}
    business_ids = [
        n["id"] for n in nodes
        if n.get("type") not in non_connectable
        and n["id"] not in subprocess_child_ids
        and not n.get("isForCompensation")
        # Un subProcess événementiel (triggeredByEvent=true) n'a JAMAIS de
        # sequenceFlow entrant par conception : il est déclenché par son propre
        # startEvent interne (message/error/signal/timer), pas par le flux du
        # processus parent (cf. section 9.1 de SKILL.md). Sans cette exclusion,
        # cette passe le force-rattache à tort au dernier nœud "atteignable"
        # trouvé, créant un sequenceFlow qui n'a aucun sens métier (régression
        # confirmée sur un scénario de sous-processus d'annulation).
        and not (n.get("type") == "subProcess" and n.get("triggeredByEvent"))
    ]

    if not start_ids:
        # Pas de startEvent : rien à raccrocher en amont, la règle 6 de
        # validate_logic_core lèvera déjà l'erreur correspondante.
        return

    # --- 1. Rattachement de TOUTES les composantes inatteignables depuis un start ---
    reachable = _bfs_reachable(start_ids, seq_edges, boundary_map)
    unreached = [nid for nid in business_ids if nid not in reachable]

    if unreached:
        components = _weakly_connected_components(unreached, seq_edges)
        components.sort(key=lambda comp: min(order_index.get(nid, 0) for nid in comp))

        def _current_attach_point() -> str:
            """Choisit le meilleur point de rattachement déjà connecté au
            start : un nœud atteignable, sans sortie, qui n'est pas un
            endEvent. On évite de rattacher un graphe inachevé directement
            depuis un start déjà actif, car cela crée des branches inexactes
            et des faux parallèles au moment du normalisation finale."""
            outgoing_count: dict[str, int] = {}
            for e in seq_edges:
                s = e.get("source")
                if isinstance(s, str):
                    outgoing_count[s] = outgoing_count.get(s, 0) + 1
            candidates = [
                nid for nid in reachable
                if outgoing_count.get(nid, 0) == 0
                and node_map.get(nid, {}).get("type") not in {"endEvent", "startEvent"}
            ]
            if candidates:
                candidates.sort(key=lambda nid: order_index.get(nid, 0))
                return candidates[-1]
            fallback_candidates = [
                nid for nid in reachable
                if node_map.get(nid, {}).get("type") not in {"endEvent", "startEvent"}
            ]
            if fallback_candidates:
                fallback_candidates.sort(key=lambda nid: order_index.get(nid, 0))
                return fallback_candidates[-1]
            return start_ids[0]

        for comp in components:
            comp_set = set(comp)
            targeted = {e.get("target") for e in seq_edges if e.get("source") in comp_set}
            roots = [nid for nid in comp if nid not in targeted]
            root = (
                min(roots, key=lambda nid: order_index.get(nid, 0))
                if roots
                else min(comp, key=lambda nid: order_index.get(nid, 0))
            )

            attach_point = _current_attach_point()
            source_pool = node_map.get(attach_point, {}).get("poolId")
            target_pool = node_map.get(root, {}).get("poolId")
            if not (source_pool and target_pool and source_pool != target_pool):
                add_edge(attach_point, root)
                _mark_auto_gap(
                    node_map[root],
                    f"rattaché automatiquement au flux principal depuis '{attach_point}'",
                )
            reachable = _bfs_reachable(start_ids, seq_edges, boundary_map)

    # --- 2. Rattachement de toute impasse (hors endEvent) vers une sortie ---
    # Operate PER-POOL to avoid creating cross-pool spurious edges.
    # Also skip nodes that have outgoing messageFlow (they are not dead ends,
    # they are sending messages to another pool).
    outgoing_seq_count: dict[str, int] = {}
    for e in seq_edges:
        s = e.get("source")
        if isinstance(s, str):
            outgoing_seq_count[s] = outgoing_seq_count.get(s, 0) + 1

    outgoing_msg_sources = {
        e.get("source")
        for e in (message_edges or [])
        if isinstance(e, dict) and isinstance(e.get("source"), str)
    }

    # Group business nodes by pool
    pool_business_map: dict[str | None, list[str]] = {}
    for nid in business_ids:
        p = node_map.get(nid, {}).get("poolId")
        pool_business_map.setdefault(p, []).append(nid)

    pool_end_map: dict[str | None, list[str]] = {}
    for eid in end_ids:
        p = node_map.get(eid, {}).get("poolId")
        pool_end_map.setdefault(p, []).append(eid)

    incoming_seq_count: dict[str, int] = {}
    for e in seq_edges:
        t = e.get("target")
        if isinstance(t, str):
            incoming_seq_count[t] = incoming_seq_count.get(t, 0) + 1

    for pool_key, pool_biz_ids in pool_business_map.items():
        pool_ends = pool_end_map.get(pool_key, [])
        orphan_ends = [eid for eid in pool_ends if incoming_seq_count.get(eid, 0) == 0]

        dead_ends = sorted(
            (nid for nid in pool_biz_ids
             if outgoing_seq_count.get(nid, 0) == 0
             and nid not in outgoing_msg_sources),
            key=lambda nid: order_index.get(nid, 0),
        )

        if not dead_ends and not orphan_ends:
            continue

        if not pool_ends:
            new_end_id = f"end_{pool_key}" if pool_key else "end_auto_reconnect"
            suffix = 1
            while new_end_id in node_map:
                suffix += 1
                new_end_id = f"end_{pool_key}_{suffix}" if pool_key else f"end_auto_reconnect_{suffix}"
            end_node: dict[str, Any] = {
                "id": new_end_id,
                "type": "endEvent",
                "name": "Fin (rattachement automatique)",
            }
            if pool_key:
                end_node["poolId"] = pool_key
            node_map[new_end_id] = end_node
            nodes.append(end_node)
            end_ids.append(new_end_id)
            pool_ends = [new_end_id]
            _mark_auto_gap(end_node, "endEvent créé faute d'événement de fin exploitable")

        target_end = pool_ends[0]
        for nid in dead_ends:
            add_edge(nid, target_end)
            incoming_seq_count[target_end] = incoming_seq_count.get(target_end, 0) + 1
            _mark_auto_gap(
                node_map[target_end],
                f"reçoit une transition de secours depuis '{nid}'",
            )

        if orphan_ends and pool_biz_ids:
            last_biz = pool_biz_ids[-1]
            for o_end in orphan_ends:
                if incoming_seq_count.get(o_end, 0) == 0:
                    add_edge(last_biz, o_end)
                    incoming_seq_count[o_end] = incoming_seq_count.get(o_end, 0) + 1
                    _mark_auto_gap(
                        node_map[o_end],
                        f"reçoit une transition de secours depuis '{last_biz}'",
                    )


def _ensure_pool_segregation(
    nodes: list[dict[str, Any]],
    pools: list[dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
) -> None:
    """Fusionne automatiquement les pools reliées uniquement par des sequenceFlow.

    En BPMN 2.0, un sequenceFlow ne peut jamais traverser deux pools distincts.
    Lorsque le Logic-Core produit plusieurs pools pour le même processus interne,
    cette passe les regroupe dans un seul pool racine en créant des lanes, sans
    dépendre du type de gateway.
    
    IMPORTANT: Ne fusionner les pools que si elles sont connectées par sequenceFlow
    TRAVERSANT les limites de pool. Les pools représentant des participants distincts
    doivent rester séparées pour permettre les messageFlow.
    """
    if not pools:
        return

    pool_ids = {p["id"] for p in pools if isinstance(p, dict) and isinstance(p.get("id"), str)}
    if len(pool_ids) < 2:
        return

    node_pool: dict[str, str] = {}
    for node in nodes:
        pid = node.get("poolId")
        if isinstance(pid, str) and pid in pool_ids:
            node_pool[node["id"]] = pid

    parent = {pid: pid for pid in pool_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            root, other = sorted([ra, rb])
            parent[other] = root

    # Identifier les messageFlow déclarées pour éviter de les confondre avec des sequenceFlow crossant
    message_pairs = {
        (e.get("source"), e.get("target"))
        for e in (edges or [])
        if isinstance(e, dict) and e.get("type") == "messageFlow" and isinstance(e.get("source"), str) and isinstance(e.get("target"), str)
    }
    
    # IMPORTANT: Si des messageFlow sont déjà explicitement déclarés, les pools impliquées
    # ne doivent JAMAIS être fusionnées. Cela indiquerait une conception multi-participant
    # intentionnelle.
    message_pool_pairs: set[tuple[str, str]] = set()
    for src_pool, tgt_pool in message_pairs:
        src_p = node_pool.get(src_pool)
        tgt_p = node_pool.get(tgt_pool)
        if src_p and tgt_p and src_p != tgt_p:
            message_pool_pairs.add((min(src_p, tgt_p), max(src_p, tgt_p)))
    
    # Identifier aussi les sequenceFlow qui DOIVENT être converties en messageFlow
    # parce qu'elles traversent déjà les limites de pool : signale une tentative
    # de modélisation multi-pool qui est INTENTIONNELLE.
    crossing_found = False
    for edge in seq_edges:
        src = edge.get("source")
        dst = edge.get("target")
        src_pool = node_pool.get(src)
        dst_pool = node_pool.get(dst)
        if src_pool and dst_pool and src_pool != dst_pool:
            # Cette arête traverse les pools. Si elle est déjà signalée comme messageFlow,
            # ne pas forcer la fusion.
            if (src, dst) in message_pairs or (dst, src) in message_pairs:
                continue
            # Si cette paire de pools est déjà connectée par messageFlow explicite,
            # ne pas fusionner.
            pool_pair = (min(src_pool, dst_pool), max(src_pool, dst_pool))
            if pool_pair in message_pool_pairs:
                continue
            # Sinon, c'est une violation : la fusion automatique est justifiée.
            union(src_pool, dst_pool)
            crossing_found = True

    if not crossing_found:
        return

    groups: dict[str, list[str]] = {}
    for pid in pool_ids:
        groups.setdefault(find(pid), []).append(pid)

    pool_map = {p["id"]: p for p in pools if isinstance(p, dict) and isinstance(p.get("id"), str)}

    for root, members in groups.items():
        if len(members) <= 1:
            continue
        root_pool = pool_map[root]
        merged_lanes = list(root_pool.get("lanes", []))
        existing_lane_ids = {lane.get("id") for lane in merged_lanes if isinstance(lane, dict) and lane.get("id")}

        for pid in members:
            if pid == root:
                continue
            member_pool = pool_map.get(pid)
            if not isinstance(member_pool, dict):
                continue
            lane_id = f"lane_{pid}"
            if lane_id not in existing_lane_ids:
                merged_lanes.append({"id": lane_id, "name": member_pool.get("name", pid)})
                existing_lane_ids.add(lane_id)
            for node in nodes:
                if node.get("poolId") == pid:
                    node["poolId"] = root
                    if not node.get("laneId"):
                        node["laneId"] = lane_id
                    _mark_auto_gap(
                        node,
                        f"pool '{pid}' fusionné automatiquement dans '{root}' "
                        "(sequenceFlow inter-pool détecté ; le même processus interne a été regroupé en lane)",
                    )
        root_pool["lanes"] = merged_lanes

    pools[:] = [p for p in pools if p.get("id") not in parent or find(p["id"]) == p["id"]]


def _ensure_pool_assignment(
    nodes: list[dict[str, Any]],
    node_map: dict[str, dict[str, Any]],
    seq_edges: list[dict[str, Any]],
    pools: list[dict[str, Any]],
) -> None:
    """Assigne un poolId à tout nœud sans poolId quand des pools existent.

    Un nœud sans poolId dans un Logic-Core multi-pool disparaît silencieusement
    du XML généré, mais garde son BPMNShape dans le BPMNDI, ce qui invalide le
    BPMN pour Camunda. La règle de correction est déterministe : on propage le
    poolId depuis les nœuds voisins par sequenceFlow; sinon on prend le pool unique
    disponible s'il n'y en a qu'un.
    """
    if not pools:
        return

    adj: dict[str, list[str]] = {}
    for edge in seq_edges:
        src = edge.get("source")
        tgt = edge.get("target")
        if isinstance(src, str) and isinstance(tgt, str):
            adj.setdefault(src, []).append(tgt)
            adj.setdefault(tgt, []).append(src)

    unassigned = [n["id"] for n in nodes if isinstance(n.get("id"), str) and not n.get("poolId")]
    if not unassigned:
        return

    pools_with_nodes = {n.get("poolId") for n in nodes if isinstance(n.get("poolId"), str)}
    if len(pools_with_nodes) == 1:
        fallback_pool = next(iter(pools_with_nodes))
    elif not pools_with_nodes and len(pools) == 1:
        # Aucun nœud n'a de poolId nulle part : si un seul pool existe dans le
        # Logic-Core, c'est le seul candidat possible malgré l'absence totale
        # de nœuds déjà rattachés.
        fallback_pool = pools[0].get("id")
    else:
        fallback_pool = None

    for nid in unassigned:
        visited = {nid}
        queue = [nid]
        found_pool = None
        while queue and found_pool is None:
            cur = queue.pop(0)
            for nxt in adj.get(cur, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                candidate_pool = node_map.get(nxt, {}).get("poolId")
                if candidate_pool:
                    found_pool = candidate_pool
                    break
                queue.append(nxt)

        assigned_pool = found_pool or fallback_pool
        if assigned_pool:
            node_map[nid]["poolId"] = assigned_pool
            _mark_auto_gap(
                node_map[nid],
                f"assigné automatiquement au pool '{assigned_pool}' (aucun poolId fourni par la génération)",
            )


def normalize_logic_core_graph(logic_core: dict[str, Any], source_text: str | None = None) -> dict[str, Any]:
    """Normalise le graphe séquentiel BPMN en ajoutant les flux manquants sans créer de relation invalide sur les boundary events."""
    if not isinstance(logic_core, dict):
        return logic_core

    nodes = []
    for n in logic_core.get("nodes", []):
        if not isinstance(n, dict):
            continue
        # Nettoyer les champs None (ex: parentSubProcessId: null) qui violent le schema
        clean_n = {k: v for k, v in n.items() if v is not None}
        nodes.append(clean_n)
    edges = []
    for e in logic_core.get("edges", []):
        if not isinstance(e, dict):
            continue
        clean = {k: v for k, v in e.items() if v is not None}
        edges.append(clean)
    pools = [dict(p) for p in logic_core.get("pools", []) if isinstance(p, dict)]
    if not nodes:
        return logic_core

    # Assainir les IDs de nœuds non conformes au pattern ASCII strict du schema
    # (ex: accents recopiés depuis un nom métier français) et propager le
    # renommage à toutes les références (edges, attachedToRef).
    existing_node_ids = {n["id"] for n in nodes if isinstance(n.get("id"), str)}
    node_rename_map: dict[str, str] = {}
    for n in nodes:
        nid = n.get("id")
        if isinstance(nid, str) and not VALID_ID_PATTERN.match(nid):
            new_id = _sanitize_id(nid, existing_node_ids)
            existing_node_ids.discard(nid)
            existing_node_ids.add(new_id)
            node_rename_map[nid] = new_id
            n["id"] = new_id
    if node_rename_map:
        for e in edges:
            if e.get("source") in node_rename_map:
                e["source"] = node_rename_map[e["source"]]
            if e.get("target") in node_rename_map:
                e["target"] = node_rename_map[e["target"]]
        for n in nodes:
            if n.get("attachedToRef") in node_rename_map:
                n["attachedToRef"] = node_rename_map[n["attachedToRef"]]

    process_info = logic_core.get("process")
    sanitized_process: dict[str, Any] | None = None
    if isinstance(process_info, dict):
        pid = process_info.get("id")
        if isinstance(pid, str) and not VALID_ID_PATTERN.match(pid):
            sanitized_process = dict(process_info)
            sanitized_process["id"] = _sanitize_id(pid, set())

    # --- Garantie d'un eventDefinition valide sur tout événement de capture,
    # AVANT toute autre passe : un catch/boundary event sans déclencheur est une
    # erreur fatale de schéma, jamais laissée survivre jusqu'à la sortie finale
    # du pipeline après échec du self-healing (cf. régression confirmée sur le
    # scénario d'attente conditionnelle du stock). ---
    _ensure_catch_and_boundary_event_definition(nodes)

    # Une lane unique dans un pool ne sépare aucun rôle et n'apporte donc rien
    # (cf. règle 6.3 de SKILL.md : les lanes servent à distinguer PLUSIEURS
    # rôles au sein d'un même participant). On la fusionne dans son pool pour
    # éviter le double en-tête (Pool + Lane) redondant à l'affichage.
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pool_lanes = pool.get("lanes") or []
        if len(pool_lanes) == 1:
            lone_lane_id = pool_lanes[0].get("id") if isinstance(pool_lanes[0], dict) else None
            pool["lanes"] = []
            if lone_lane_id:
                for n in nodes:
                    if isinstance(n, dict) and n.get("laneId") == lone_lane_id:
                        n.pop("laneId", None)

    node_map = {n["id"]: n for n in nodes if isinstance(n.get("id"), str)}

    # --- Cohérence de pool des enfants de subProcess, AVANT toute passe
    # d'assignation/propagation de poolId : un enfant ne doit jamais diverger
    # de la pool de son conteneur. ---
    _ensure_subprocess_children_share_pool(nodes, node_map)

    seq_edges = [e for e in edges if e.get("type") in ("sequenceFlow", None)]

    def next_flow_id() -> str:
        used = []
        for e in edges:
            eid = e.get("id")
            if isinstance(eid, str) and eid.startswith("flow_"):
                suffix = eid.split("_", 1)[1]
                if suffix.isdigit():
                    used.append(int(suffix))
        return f"flow_{max(used, default=0) + 1}"

    def has_edge(src: str, dst: str) -> bool:
        return any(e.get("source") == src and e.get("target") == dst and e.get("type") in ("sequenceFlow", None) for e in seq_edges)

    def add_edge(src: str, dst: str, *, name: str | None = None, condition: str | None = None, edge_type: str = "sequenceFlow") -> None:
        if src == dst or src not in node_map or dst not in node_map:
            return
        if has_edge(src, dst):
            return
        edge = {"id": next_flow_id(), "source": src, "target": dst, "type": edge_type}
        if name is not None:
            edge["name"] = name
        if condition is not None:
            edge["condition"] = condition
        edges.append(edge)
        seq_edges.append(edge)

    lane_to_pool: dict[str, str] = {}
    pool_lane_ids: dict[str, list[str]] = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pid = pool.get("id")
        if not pid:
            continue
        lanes = []
        for lane in pool.get("lanes", []):
            if isinstance(lane, dict) and isinstance(lane.get("id"), str):
                lid = lane["id"]
                lanes.append(lid)
                lane_to_pool[lid] = pid
        if lanes:
            pool_lane_ids[pid] = lanes

    for node in nodes:
        if not node.get("poolId") and node.get("laneId") in lane_to_pool:
            node["poolId"] = lane_to_pool[node["laneId"]]

    for node in nodes:
        pool_id = node.get("poolId")
        lane_id = node.get("laneId")
        if not isinstance(pool_id, str):
            continue
        if lane_id == pool_id:
            preferred_lanes = pool_lane_ids.get(pool_id, [])
            if preferred_lanes and isinstance(preferred_lanes[0], str):
                node["laneId"] = preferred_lanes[0]
            else:
                node.pop("laneId", None)
        elif isinstance(lane_id, str) and lane_id not in set(pool_lane_ids.get(pool_id, [])):
            if pool_id in pool_lane_ids and pool_lane_ids[pool_id]:
                node["laneId"] = pool_lane_ids[pool_id][0]
            else:
                node.pop("laneId", None)

    for n in nodes:
        if isinstance(n, dict) and "laneId" in n and not isinstance(n["laneId"], str):
            del n["laneId"]

    pool_by_id = {p.get("id"): p for p in pools if isinstance(p, dict) and isinstance(p.get("id"), str)}
    message_edges = [e for e in edges if e.get("type") == "messageFlow"]
    node_pool_map = {n.get("id"): n.get("poolId") for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("poolId"), str)}
    for edge in list(edges):
        if edge.get("type") not in ("sequenceFlow", None):
            continue
        src = edge.get("source")
        dst = edge.get("target")
        src_pool = node_pool_map.get(src)
        dst_pool = node_pool_map.get(dst)
        if isinstance(src, str) and isinstance(dst, str) and src_pool and dst_pool and src_pool != dst_pool:
            src_node = node_map.get(src)
            dst_node = node_map.get(dst)
            if src_node and src_node.get("type") in ("startEvent", "boundaryEvent"):
                continue
            # Un gateway ne peut jamais être un point de communication valide : le
            # convertir silencieusement en messageFlow produirait une arête invalide
            # (cf. section 3 de SKILL.md). Laisser le sequenceFlow tel quel ici : la
            # règle POOL-001 le signalera comme une vraie erreur à restructurer
            # (déplacer la tâche dans le pool du gateway, ou faire porter le
            # messageFlow sur une tâche intermédiaire, jamais sur le gateway).
            if (src_node and src_node.get("type") in GATEWAY_TYPES) or (dst_node and dst_node.get("type") in GATEWAY_TYPES):
                continue
            edge["type"] = "messageFlow"
            if not any(existing.get("id") == edge.get("id") for existing in message_edges):
                message_edges.append(edge)
            if edge in seq_edges:
                seq_edges.remove(edge)

    # Assigner le poolId manquant AVANT d'injecter un startEvent synthétique par
    # pool ci-dessous : sinon, un startEvent généré par le LLM mais pas encore
    # rattaché à un pool (poolId absent à ce stade) est invisible au test
    # "ce pool a-t-il déjà un startEvent ?", et un second startEvent synthétique
    # est injecté en double une fois le rattachement effectué plus bas.
    _ensure_pool_assignment(nodes, node_map, seq_edges, pools)

    # Ids des startEvent injectés automatiquement ci-dessous (un par pool sans
    # startEvent propre), suivis explicitement plutôt que redevinés par un motif
    # de nom ("start_pool_...") : un pool métier nommé "pool_systeme"/"pool_banque"
    # (convention de nommage très courante) produit un id "start_pool_systeme" qui
    # collisionnait avec l'ancien filtre par préfixe de la passe de déduplication
    # plus bas, faisant supprimer à tort l'UNIQUE startEvent de ce pool — coupant
    # tout le pool du reste du graphe sans qu'aucune erreur explicite ne le signale
    # (cf. bug 5 du rapport de tests : graphe déconnecté sur un événement conditionnel).
    synthetic_pool_start_ids: set[str] = set()

    for pool in pools:
        if not isinstance(pool, dict):
            continue
        pool_id = pool.get("id")
        if not isinstance(pool_id, str):
            continue
        pool_nodes = [n for n in nodes if isinstance(n, dict) and n.get("poolId") == pool_id]
        if not any(n.get("type") == "startEvent" for n in pool_nodes):
            pool_business = [n for n in pool_nodes if n.get("type") not in {"startEvent", "endEvent", "boundaryEvent"}]
            if not pool_business:
                continue

            # Find the first business node that has no incoming sequenceFlow
            # within this pool (i.e. the natural entry point).
            first_business = next(
                (n for n in pool_business
                 if not any(
                     e.get("target") == n["id"] and e.get("type") in ("sequenceFlow", None)
                     for e in seq_edges
                 )),
                None,
            ) or pool_business[0]
            start_id = f"start_{pool_id}"
            suffix = 2
            while start_id in node_map:
                start_id = f"start_{pool_id}_{suffix}"
                suffix += 1
            start_node = {"id": start_id, "type": "startEvent", "name": f"Start {pool.get('name', pool_id)}", "poolId": pool_id}
            node_map[start_id] = start_node
            nodes.append(start_node)
            synthetic_pool_start_ids.add(start_id)
            add_edge(start_id, first_business["id"])
            # Ne pas marquer un GAP de normalisation pour les pools collapsed :
            # l'injection d'un startEvent synthétique y est un comportement structurel
            # normal, pas une lacune métier.
            if not pool.get("collapsed", False):
                _mark_auto_gap(
                    first_business,
                    f"rattaché automatiquement au flux interne du pool '{pool_id}'",
                )


    start_nodes = [n["id"] for n in nodes if n.get("type") == "startEvent"]
    end_nodes = [n["id"] for n in nodes if n.get("type") == "endEvent"]
    boundary_nodes = [n["id"] for n in nodes if n.get("type") == "boundaryEvent"]
    business_nodes = [n["id"] for n in nodes if n.get("type") not in {"startEvent", "endEvent", "boundaryEvent"}]
    for msg in list(edges):
        if msg.get("type") != "messageFlow":
            continue
        src = msg.get("source")
        tgt = msg.get("target")
        if not isinstance(src, str) or not isinstance(tgt, str):
            continue
        src_node = node_map.get(src)
        tgt_node = node_map.get(tgt)
        if not src_node or not tgt_node:
            continue
        if src_node.get("poolId") and src_node.get("poolId") == tgt_node.get("poolId"):
            msg["type"] = "sequenceFlow"
            seq_edges.append(msg)
            message_edges = [e for e in message_edges if e is not msg]

    parallel_keywords = [
        "parallel", "en parallèle", "en même temps", "simultanément", "at the same time",
        "same time", "simultaneously", "concurrently", "concurrent", "together", "ensemble",
        "plusieurs tâches", "in parallel", "at once"
    ]
    text_has_parallel = bool(source_text) and any(keyword in (source_text or "").lower() for keyword in parallel_keywords)
    if source_text is not None and not text_has_parallel:
        for node in nodes:
            if node.get("type") != "parallelGateway":
                continue
            # Ne pas convertir automatiquement une passerelle explicite en XOR si le texte
            # décrit un vrai parallélisme. La règle de métier a priorité sur la déduction
            # heuristique : on garde le type explicitement présent dans le Process Description.
            node["documentation"] = (
                node.get("documentation", "")
                + " GAP: parallélisme explicite conservé; la normalisation ne doit pas inventer de décision exclusive sans preuve métier."
            ).strip()
    for start_id in start_nodes:
        if any(e.get("source") == start_id and e.get("type") in ("sequenceFlow", None) for e in seq_edges):
            continue
        for msg in message_edges:
            if msg.get("source") != start_id or not isinstance(msg.get("target"), str):
                continue
            target = msg["target"]
            if target not in node_map:
                continue
            # Ne jamais dupliquer en sequenceFlow une relation déjà portée par un
            # messageFlow qui traverse une frontière de pool CONNUE : un startEvent
            # "déclencheur ponctuel" (qui n'a d'autre rôle que d'envoyer ce message,
            # cf. section 6.2 de SKILL.md) n'a besoin d'AUCUNE continuation interne —
            # créer ce sequenceFlow produisait un flux inter-pool invalide qu'une
            # passe ultérieure redirigeait ensuite vers le conteneur subProcess du
            # target, une erreur fatale POOL-001 (régression confirmée).
            start_pool = node_map[start_id].get("poolId")
            target_pool = node_map.get(target, {}).get("poolId")
            if isinstance(start_pool, str) and isinstance(target_pool, str) and start_pool != target_pool:
                break
            if not has_edge(start_id, target):
                add_edge(start_id, target)
            if not node_map[start_id].get("poolId") and node_map.get(target, {}).get("poolId"):
                node_map[start_id]["poolId"] = node_map[target]["poolId"]
            break

    for node in nodes:
        if isinstance(node, dict):
            ntype = node.get("type")
            name_lower = (node.get("name") or "").lower()
            if ntype == "intermediateCatchEvent":
                if any(kw in name_lower for kw in ["wait", "hour", "heure", "délai", "timeout", "24h", "payment", "paiement"]):
                    node["eventDefinition"] = "timer"
                    if not node.get("eventDetail"):
                        node["eventDetail"] = "PT24H"

    linearized_nodes: set[str] = set()

    for gateway_id, gateway_node in list(node_map.items()):
        if not isinstance(gateway_node, dict) or gateway_node.get("type") not in GATEWAY_TYPES:
            continue
        if gateway_node.get("type") != "exclusiveGateway":
            continue

        outgoing = [e for e in seq_edges if isinstance(e, dict) and e.get("source") == gateway_id]
        if len(outgoing) <= 1:
            continue

        branches_by_label: dict[str, list[dict[str, Any]]] = {}
        for edge in outgoing:
            label = (edge.get("name") or edge.get("condition") or "").strip().lower()
            if label:
                branches_by_label.setdefault(label, []).append(edge)

        for label, edges_with_label in branches_by_label.items():
            if len(edges_with_label) <= 1:
                continue

            targets = [e.get("target") for e in edges_with_label if isinstance(e.get("target"), str) and e.get("target") in node_map]
            if len(targets) <= 1:
                continue

            for t in targets:
                linearized_nodes.add(t)

            # Keep only first target from gateway, remove duplicates
            primary_target = targets[0]
            for duplicate_target in targets[1:]:
                duplicate_edges = [e for e in edges_with_label if e.get("target") == duplicate_target]
                for duplicate_edge in duplicate_edges:
                    edges.remove(duplicate_edge)
                    seq_edges.remove(duplicate_edge)

            # Now create linear chain: primary_target → duplicate_targets[0] → duplicate_targets[1] → ...
            for i, dup_target in enumerate(targets[1:]):
                prev_target = targets[i]
                
                # Remove any existing parallel edges from prev_target to other nodes
                # but keep only one outgoing edge for the linear chain
                existing_to_remove = [e for e in seq_edges if e.get("source") == prev_target and e.get("target") != dup_target]
                for e_rem in existing_to_remove:
                    if e_rem.get("target") not in node_map:
                        continue
                    edges.remove(e_rem)
                    seq_edges.remove(e_rem)
                
                # Create the sequential edge
                edge_id = f"{prev_target}_to_{dup_target}"
                new_edge = {
                    "id": edge_id,
                    "source": prev_target,
                    "target": dup_target,
                    "type": "sequenceFlow",
                }
                edges.append(new_edge)
                seq_edges.append(new_edge)
                
                dup_node = node_map.get(dup_target, {})
                if isinstance(dup_node, dict):
                    _mark_auto_gap(
                        dup_node,
                        f"branche dupliquée (label '{label}') fusionnée en séquence après '{prev_target}'",
                    )

    # Refresh seq_edges dynamically before computing outgoing targets
    seq_edges[:] = [e for e in edges if isinstance(e, dict) and e.get("type") in ("sequenceFlow", None)]

    outgoing_targets: dict[str, list[str]] = {}
    for edge in seq_edges:
        src = edge.get("source")
        tgt = edge.get("target")
        if isinstance(src, str) and isinstance(tgt, str):
            outgoing_targets.setdefault(src, []).append(tgt)

    # Insertion minimale de passerelles synthétiques uniquement lorsqu'il existe
    # une preuve structurelle forte d'un split parallèle : plusieurs sorties sans
    # labels/conditions qui convergent ensuite vers un même nœud de synchronisation.
    # On n'invente pas de gateway pour des branches de décision explicites ou pour
    # des forks sans preuve de convergence commune.
    for source, targets in list(outgoing_targets.items()):
        source_type = node_map.get(source, {}).get("type")
        unique_targets = list(dict.fromkeys(targets))
        if source_type in GATEWAY_TYPES or source in end_nodes or source in boundary_nodes:
            continue
        if source in linearized_nodes:
            continue
        if len(unique_targets) <= 1:
            continue

        has_condition_or_label = any(
            edge.get("source") == source and (edge.get("condition") or edge.get("name"))
            for edge in seq_edges
        )
        if has_condition_or_label:
            continue
        if source_text is not None and not text_has_parallel:
            continue

        common_descendants: set[str] | None = None
        for target in unique_targets:
            reachable: set[str] = set()
            stack = [target]
            while stack:
                cur = stack.pop()
                if cur in reachable:
                    continue
                reachable.add(cur)
                for edge in seq_edges:
                    if edge.get("source") == cur and isinstance(edge.get("target"), str):
                        stack.append(edge["target"])
            if common_descendants is None:
                common_descendants = reachable
            else:
                common_descendants &= reachable

        if not common_descendants:
            continue

        gateway_type = "parallelGateway"
        gateway_id = f"{source}_gw"
        if gateway_id not in node_map:
            node_map[gateway_id] = {"id": gateway_id, "type": gateway_type, "name": f"{source} branch", "gatewayDirection": "diverging"}
            nodes.append(node_map[gateway_id])

        for target in unique_targets:
            if has_edge(source, target):
                for e in list(edges):
                    if e.get("source") == source and e.get("target") == target and e.get("type") == "sequenceFlow":
                        edges.remove(e)
                        seq_edges[:] = [x for x in seq_edges if x is not e]
                        break
            add_edge(source, gateway_id)
            add_edge(gateway_id, target)

    # --- Assignation automatique du poolId manquant (désactivée pour éviter les
    # faux rattachements de participants). ---
    _ensure_pool_assignment(nodes, node_map, seq_edges, pools)

    # --- Aucune fusion automatique de pools / lanes ; la validation explicite
    # de la structure fait office de garde-fou. ---

    # --- Suppression mécanique des sequenceFlow reliant un subProcess à son propre
    # enfant (cf. section 9.1 de SKILL.md) : le self-healing LLM ne corrige pas
    # fiablement ce cas, alors que c'est une simple arête à retirer — le point
    # d'entrée interne du subProcess est déduit de l'enfant sans prédécesseur.
    child_to_parent_sp: dict[str, str] = {
        n["id"]: n["parentSubProcessId"]
        for n in nodes
        if isinstance(n.get("id"), str) and isinstance(n.get("parentSubProcessId"), str)
    }
    if child_to_parent_sp:
        boundary_edges = [
            e for e in edges
            if isinstance(e, dict)
            and (
                child_to_parent_sp.get(e.get("target")) == e.get("source")
                or child_to_parent_sp.get(e.get("source")) == e.get("target")
            )
        ]
        for e in boundary_edges:
            edges.remove(e)
            if e in seq_edges:
                seq_edges.remove(e)

        # Cas plus large : un enfant de subProcess relié à un nœud EXTÉRIEUR à ce
        # même subProcess (pas le conteneur, pas un enfant frère) — ex: l'endEvent
        # interne pointant directement vers la tâche suivante du processus parent.
        # Une telle arête doit apparaître comme partant/arrivant du subProcess
        # LUI-MÊME, pas de son enfant interne (cf. section 9.1 de SKILL.md).
        degenerate_edges = []
        for e in edges:
            if not isinstance(e, dict) or e.get("type") not in ("sequenceFlow", None):
                continue
            src, tgt = e.get("source"), e.get("target")
            src_parent = child_to_parent_sp.get(src)
            tgt_parent = child_to_parent_sp.get(tgt)
            new_src, new_tgt = src, tgt
            if src_parent is not None and tgt != src_parent and tgt_parent != src_parent:
                new_src = src_parent
            if tgt_parent is not None and src != tgt_parent and src_parent != tgt_parent:
                new_tgt = tgt_parent
            if new_src != src or new_tgt != tgt:
                if new_src == new_tgt:
                    degenerate_edges.append(e)
                else:
                    e["source"], e["target"] = new_src, new_tgt
        for e in degenerate_edges:
            if e in edges:
                edges.remove(e)
            if e in seq_edges:
                seq_edges.remove(e)

    # --- Réparation mécanique d'un gateway relié directement à une autre pool
    # (sequenceFlow ou messageFlow) : un gateway n'est JAMAIS un point de
    # communication valide. Le rappel dans le prompt de self-healing ne suffit
    # pas de façon fiable (régressions confirmées) — insertion automatique
    # d'une tâche relais dans la pool du gateway. ---
    _fix_gateway_pool_crossing_edges(nodes, node_map, edges, seq_edges, pools)

    # --- Suppression mécanique d'un flux de boundaryEvent qui reboucle vers sa
    # propre tâche hôte, AVANT toute passe de dédoublement/gateway : cette arête
    # est TOUJOURS erronée (un boundary event ouvre une branche latérale, jamais
    # un redémarrage) et fausserait sinon les décisions des passes suivantes. ---
    _remove_boundary_event_reboop_to_own_host(edges, seq_edges, node_map, source_text)

    # --- Dédoublement mécanique des tâches fusionnées à tort (N entrants = N
    # sortants sur un nœud non-gateway), AVANT toute autre passe : une fusion
    # invalide ne doit jamais survivre à la normalisation, qu'elle vienne de la
    # génération initiale ou d'une régression introduite par le self-healing. ---
    _split_erroneously_merged_tasks(nodes, node_map, edges, seq_edges)

    # --- Dédoublement mécanique d'un nœud fusionné à tort par CONVERGENCE depuis
    # 2+ gateways non liés (cf. section 2 de SKILL.md) : même principe que
    # ci-dessus, pour le pattern où la fusion vient d'une convergence plutôt que
    # d'un fan-out direct — régression confirmée introduite par le self-healing
    # lui-même. ---
    _split_cross_gateway_merged_node(nodes, node_map, edges, seq_edges)

    # --- Filet de sécurité pour tout nœud non-gateway multi-sortant restant
    # (cas asymétrique non couvert par le dédoublement ci-dessus, cf. BUG 2 de
    # rapport_tests_v2.md) : rendu structurellement valide par insertion d'un
    # gateway, AVANT que la validation ne puisse même constater l'erreur. ---
    _ensure_gateway_after_residual_multi_out(nodes, node_map, edges, seq_edges)

    # --- Suppression mécanique d'une tâche "attendre X ou Y" insérée à tort avant
    # un eventBasedGateway (cf. BUG 9 de rapport_tests_v2.md) : le gateway EST le
    # point d'attente, une tâche intermédiaire n'y ajoute aucune information. ---
    _splice_out_wait_task_before_event_based_gateway(nodes, node_map, edges, seq_edges)

    # --- Chaque subProcess doit avoir son propre startEvent/endEvent internes
    # (cf. section 9.1 de SKILL.md), AVANT toute autre passe de reconnexion. ---
    _ensure_subprocess_internal_events(nodes, node_map, seq_edges, add_edge)

    # Recalculé à partir de `edges` (source de vérité) plutôt que réutilisé tel
    # quel : plusieurs passes mécaniques ci-dessus (ex: _fix_gateway_pool_crossing_edges)
    # créent de nouveaux messageFlow directement dans `edges` sans forcément mettre
    # à jour cette liste locale — une désynchronisation ferait ignorer ces nouvelles
    # arêtes par le calcul d'accessibilité ci-dessous (régression confirmée : un
    # nœud relié uniquement par un messageFlow fraîchement créé était vu à tort
    # comme une composante déconnectée et rattaché n'importe où).
    message_edges = [e for e in edges if isinstance(e, dict) and e.get("type") == "messageFlow"]

    # --- Terminaison nommée des tâches d'envoi/notification en impasse, AVANT le
    # rattachement générique de secours (voir _ensure_named_end_for_dangling_sends) ---
    _ensure_named_end_for_dangling_sends(nodes, node_map, seq_edges, message_edges, add_edge)

    # --- Garantie de connectivité totale (voir _ensure_full_connectivity) ---
    _ensure_full_connectivity(nodes, node_map, seq_edges, add_edge, message_edges)

    # --- Sécurité finale : aucun sequenceFlow ne doit traverser deux pools.
    # Cette règle s'applique même après les réparations automatiques, sinon le
    # graphe exporté en BPMN reste invalide pour Camunda malgré la validation.
    final_node_pool_map = {n.get("id"): n.get("poolId") for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("poolId"), str)}
    message_pairs = {
        (e.get("source"), e.get("target"))
        for e in edges
        if isinstance(e, dict) and e.get("type") == "messageFlow" and isinstance(e.get("source"), str) and isinstance(e.get("target"), str)
    }
    for edge in list(edges):
        if edge.get("type") not in ("sequenceFlow", None):
            continue
        src = edge.get("source")
        dst = edge.get("target")
        src_pool = final_node_pool_map.get(src)
        dst_pool = final_node_pool_map.get(dst)
        if isinstance(src, str) and isinstance(dst, str) and src_pool and dst_pool and src_pool != dst_pool:
            src_node = node_map.get(src)
            dst_node = node_map.get(dst)
            if src_node and src_node.get("type") in ("startEvent", "boundaryEvent"):
                continue
            # Un gateway ne peut jamais être un point de communication valide : le
            # convertir silencieusement en messageFlow produirait une arête invalide
            # (cf. section 3 de SKILL.md). Laisser le sequenceFlow tel quel ici : la
            # règle POOL-001 le signalera comme une vraie erreur à restructurer
            # (déplacer la tâche dans le pool du gateway, ou faire porter le
            # messageFlow sur une tâche intermédiaire, jamais sur le gateway).
            if (src_node and src_node.get("type") in GATEWAY_TYPES) or (dst_node and dst_node.get("type") in GATEWAY_TYPES):
                continue
            edge["type"] = "messageFlow"
            if edge in seq_edges:
                seq_edges.remove(edge)
            if edge not in message_edges:
                message_edges.append(edge)

    pool_start_events: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if not isinstance(nid, str):
            continue
        if node.get("type") == "startEvent":
            pool_id = node.get("poolId")
            if isinstance(pool_id, str):
                pool_start_events.setdefault(pool_id, []).append(nid)

    # Un pool ne doit garder qu'un seul startEvent : si un vrai startEvent (issu
    # du texte/LLM) coexiste avec celui injecté automatiquement plus haut, ne
    # retirer QUE celui explicitement marqué comme synthétique
    # (synthetic_pool_start_ids), JAMAIS deviné par un motif sur l'id du pool —
    # un pool nommé "pool_systeme" produit un id "start_pool_systeme" qui n'a
    # rien de spécial à supprimer s'il est le SEUL startEvent du pool.
    synthetic_start_ids: list[str] = []
    for pool_id, start_ids in pool_start_events.items():
        if len(start_ids) > 1:
            synthetic_start_ids.extend(sid for sid in start_ids if sid in synthetic_pool_start_ids)

    synthetic_start_ids = list(dict.fromkeys(synthetic_start_ids))
    if synthetic_start_ids:
        nodes[:] = [node for node in nodes if node.get("id") not in synthetic_start_ids]
        edges[:] = [edge for edge in edges if edge.get("source") not in synthetic_start_ids and edge.get("target") not in synthetic_start_ids]

    # --- Prune empty pools (pools with zero associated nodes, unless collapsed) ---
    pool_ids_with_nodes = {n.get("poolId") for n in nodes if isinstance(n, dict) and isinstance(n.get("poolId"), str)}
    pools[:] = [p for p in pools if isinstance(p, dict) and (p.get("id") in pool_ids_with_nodes or p.get("collapsed"))]

    final_logic_core = dict(logic_core)
    final_logic_core["nodes"] = nodes
    final_logic_core["edges"] = edges
    final_logic_core["pools"] = pools
    if sanitized_process is not None:
        final_logic_core["process"] = sanitized_process
    return final_logic_core


def validate_process_description(process_desc: dict[str, Any]) -> ValidationResult:
    """Valide une Process Description contre son schema JSON."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(process_desc, dict):
        return ValidationResult(ok=False, errors=["Process Description must be a JSON object"])

    # Assainissement mécanique des IDs non conformes (accents FR, etc.) AVANT
    # la validation JSON-Schema — en place, donc visible par l'appelant sans
    # dépendre d'une correction LLM qui peut échouer plusieurs fois de suite
    # sur le même ID (cf. _sanitize_process_description_ids).
    _sanitize_process_description_ids(process_desc)

    schema_path = Path(__file__).parent.parent / "schema" / "process-description.schema.json"
    if jsonschema is not None:
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            for error in jsonschema.Draft202012Validator(schema).iter_errors(process_desc):
                location = ".".join(str(part) for part in error.absolute_path)
                errors.append(f"{location}: {error.message}" if location else error.message)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Unable to load Process Description schema: {exc}")
    else:
        required_fields = {"process_name", "understanding_summary", "identified_elements", "gaps", "confidence_level"}
        errors.extend(f"Missing required field: '{field}'" for field in sorted(required_fields - process_desc.keys()))

    elements = process_desc.get("identified_elements", {})
    gaps = process_desc.get("gaps", [])
    if not isinstance(elements, dict):
        errors.append("identified_elements must be an object")
    if not isinstance(gaps, list):
        errors.append("gaps must be an array")

    if isinstance(elements, dict):
        required_elements = {"participants", "activities", "events", "conditions", "gateways"}
        errors.extend(
            f"identified_elements must contain '{field}'"
            for field in sorted(required_elements - elements.keys())
        )

        # Une extraction totalement vide (ni activité, ni événement) n'est jamais un
        # résultat valide pour un texte source non vide : c'est le signe que le LLM
        # a échoué à lire le texte, pas qu'il décrit un processus sans étapes. Sans
        # ce garde-fou, un Process Description vide passe la validation structurelle
        # (les clés existent, juste vides) et le Logic-Core suivant est généré sans
        # aucun contenu réel à modéliser — ce qui pousse le LLM à halluciner un
        # résultat sans rapport avec le texte source plutôt que d'échouer bruyamment.
        activities_present = bool(elements.get("activities"))
        events_present = bool(elements.get("events"))
        if not activities_present and not events_present:
            errors.append(
                "identified_elements ne contient aucune activité ni aucun événement : "
                "l'extraction n'a capturé aucun contenu du texte source (échec probable "
                "du LLM d'extraction) — à corriger en réessayant l'extraction, pas en "
                "inventant un contenu de substitution."
            )

        participant_ids = {
            item.get("id") for item in elements.get("participants", [])
            if isinstance(item, dict)
        }
        for activity in elements.get("activities", []):
            actor_id = activity.get("actor_id") if isinstance(activity, dict) else None
            if actor_id and actor_id not in participant_ids:
                errors.append(f"Activity actor_id references unknown participant: '{actor_id}'")

        activities = [item for item in elements.get("activities", []) if isinstance(item, dict)]
        events = [item for item in elements.get("events", []) if isinstance(item, dict)]

        activity_ids = {item.get("id") for item in activities if isinstance(item.get("id"), str)}
        event_ids = {item.get("id") for item in events if isinstance(item.get("id"), str)}

        seq_ids: set[str] = set()
        relation_containers = []
        for key in ("relations", "sequence_flows"):
            container = elements.get(key)
            if isinstance(container, list):
                relation_containers.append(container)
        for container in relation_containers:
            for item in container:
                if not isinstance(item, dict):
                    continue
                for side in ("source", "target"):
                    val = item.get(side)
                    if isinstance(val, str):
                        seq_ids.add(val)

        if relation_containers and (activity_ids or event_ids):
            activity_by_id = {item.get("id"): item for item in activities}
            event_by_id = {item.get("id"): item for item in events}
        
            # Identifier les outcomes des conditions pour les exclure du contrôle strict
            condition_outcomes: set[str] = set()
            for condition in elements.get("conditions", []):
                if isinstance(condition, dict):
                    for branch in condition.get("branches", []):
                        if isinstance(branch, dict):
                            outcome = branch.get("outcome")
                            if isinstance(outcome, str):
                                condition_outcomes.add(outcome)

            for elem_id in sorted(activity_ids | event_ids):
                if elem_id in seq_ids:
                    continue
                # Les outcomes des branches de condition sont autorisés à être orphelins
                # dans les relations : le self-healing ou la normalisation les connecteront
                if elem_id in condition_outcomes:
                    continue
                if elem_id in activity_by_id:
                    label = f" ({activity_by_id[elem_id].get('name', elem_id)})"
                elif elem_id in event_by_id:
                    label = f" ({event_by_id[elem_id].get('name', elem_id)})"
                else:
                    label = ""
                errors.append(
                    f"Élément '{elem_id}'{label} n'apparaît dans aucune relation de séquence — "
                    "le LLM a probablement omis son rattachement au flux du processus."
                )

    stats = {
        "has_participants": int(bool(elements.get("participants"))) if isinstance(elements, dict) else 0,
        "has_activities": int(bool(elements.get("activities"))) if isinstance(elements, dict) else 0,
        "num_gaps": len(gaps) if isinstance(gaps, list) else 0,
    }

    ok = len(errors) == 0
    return ValidationResult(ok=ok, errors=errors, warnings=warnings, stats=stats)


def validate_logic_core(logic_core: dict[str, Any], source_text: str | None = None) -> ValidationResult:
    """Valide rigoureusement un Logic-Core JSON selon le standard BPMN 2.0.

    Principe fondamental : on valide TOUJOURS le même objet qu'on renvoie.
    normalize_logic_core_graph produit une version corrigée (reconnexion
    des orphelins, scission des gateways implicites) ; c'est CETTE version,
    et uniquement elle, qui est vérifiée par toutes les règles ci-dessous
    ET renvoyée via normalized_logic_core. Vérifier l'original et renvoyer
    le corrigé (ou l'inverse) produit un résultat incohérent : soit des
    erreurs fantômes sur des problèmes déjà réparés, soit un 'ok=True' qui
    ne correspond pas à ce qui est réellement exporté en XML.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(logic_core, dict):
        return ValidationResult(ok=False, errors=["Le Logic-Core doit être un objet JSON valide."])

    normalized_logic_core = normalize_logic_core_graph(logic_core, source_text=source_text)
    validation_source = normalized_logic_core  # On valide le graphe normalisé, celui réellement exporté.
    errors.extend(_check_no_illegitimate_outcome_merge(validation_source.get("nodes", []), validation_source.get("edges", []), source_text))
    errors.extend(_check_timer_coverage(validation_source, source_text))
    errors.extend(_check_lane_coverage_for_multi_actor(validation_source, source_text))
    errors.extend(_check_matching_join_for_split(validation_source))
    errors.extend(_check_message_events_and_flows(validation_source))
    errors.extend(_check_named_end_events(validation_source))
    errors.extend(_check_non_gateway_branching(validation_source))
    errors.extend(_check_subprocess_boundary_edges(validation_source))
    errors.extend(_check_data_object_flow_type(validation_source))
    errors.extend(_check_cross_gateway_task_merge(validation_source))
    errors.extend(_check_transaction_subprocess(validation_source))

    schema_path = Path(__file__).parent.parent / "schema" / "logic-core.schema.json"
    if jsonschema is not None:
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator = jsonschema.Draft202012Validator(schema)
            for error in validator.iter_errors(validation_source):
                location = "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}"
                    for part in error.absolute_path
                ).lstrip(".")
                errors.append(f"SCHEMA {location}: {error.message}" if location else f"SCHEMA: {error.message}")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Impossible de charger le schema Logic-Core: {exc}")
    else:
        errors.append("La dépendance jsonschema est requise pour valider le Logic-Core")

    # 1. Validation de l'objet process
    process_info = validation_source.get("process")
    if not isinstance(process_info, dict) or not process_info.get("id"):
        errors.append("Le champ 'process.id' est obligatoire et manquant.")

    nodes_list = validation_source.get("nodes", [])
    edges_list = validation_source.get("edges", [])
    pools_list = validation_source.get("pools", [])

    if not isinstance(nodes_list, list) or len(nodes_list) == 0:
        errors.append("Le Logic-Core ne contient aucun nœud ('nodes').")
        return ValidationResult(ok=False, errors=errors, normalized_logic_core=validation_source)

    # Dictionnaire des nœuds
    nodes_map: dict[str, dict[str, Any]] = {}
    for n in nodes_list:
        if isinstance(n, dict) and "id" in n:
            nodes_map[n["id"]] = n

    # Dictionnaire des pools et lanes
    pools_map: dict[str, dict[str, Any]] = {}
    lanes_map: dict[str, dict[str, Any]] = {}
    lane_to_pool: dict[str, str] = {}
    pool_lane_ids: dict[str, list[str]] = {}

    for pool in pools_list:
        if isinstance(pool, dict) and "id" in pool:
            pid = pool["id"]
            pools_map[pid] = pool
            lane_ids_here: list[str] = []
            for lane in pool.get("lanes", []):
                if isinstance(lane, dict) and "id" in lane:
                    lid = lane["id"]
                    lanes_map[lid] = lane
                    lane_to_pool[lid] = pid
                    lane_ids_here.append(lid)
            if lane_ids_here:
                pool_lane_ids[pid] = lane_ids_here

    # 2. Unicité des identifiants
    all_node_ids = [n.get("id") for n in nodes_list if isinstance(n, dict)]
    dupe_nodes = {i for i in all_node_ids if i and all_node_ids.count(i) > 1}
    if dupe_nodes:
        errors.append(f"Identifiants de nœuds dupliqués : {sorted(dupe_nodes)}")

    all_edge_ids = [e.get("id") for e in edges_list if isinstance(e, dict)]
    dupe_edges = {i for i in all_edge_ids if i and all_edge_ids.count(i) > 1}
    if dupe_edges:
        errors.append(f"Identifiants de flux (edges) dupliqués : {sorted(dupe_edges)}")

    all_pool_ids = [p.get("id") for p in pools_list if isinstance(p, dict)]
    dupe_pools = {i for i in all_pool_ids if i and all_pool_ids.count(i) > 1}
    if dupe_pools:
        errors.append(f"Identifiants de pools dupliqués : {sorted(dupe_pools)}")

    if pools_map:
        unassigned_after_normalization = [
            n.get("id") for n in nodes_list
            if isinstance(n, dict) and n.get("poolId") not in pools_map
        ]
        if unassigned_after_normalization:
            errors.append(
                "Nœuds sans 'poolId' valide alors que des pools sont déclarés "
                f"({sorted(pools_map.keys())}) : {sorted(unassigned_after_normalization)}. "
                "Chaque nœud doit être explicitement rattaché à un pool."
            )

    # 3. Validation des Boundary Events et des Event Definitions
    INTERMEDIATE_AND_BOUNDARY_TYPES = {"intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent"}
    for nid, n in nodes_map.items():
        ntype = n.get("type")
        if ntype in INTERMEDIATE_AND_BOUNDARY_TYPES:
            edef = n.get("eventDefinition")
            if not edef or edef == "none":
                errors.append(
                    f"Événement '{nid}' ({ntype}) : 'eventDefinition' est '{edef or 'manquant'}'. "
                    "En BPMN 2.0, un événement intermédiaire ou boundary doit obligatoirement spécifier une définition d'événement valide (ex: timer, message, signal, error)."
                )

    boundary_nodes = [n for n in nodes_list if n.get("type") == "boundaryEvent"]
    for b in boundary_nodes:
        bid = b.get("id")
        host_ref = b.get("attachedToRef")
        if not host_ref:
            errors.append(f"BoundaryEvent '{bid}' n'a pas d'attribut 'attachedToRef' vers son activité hôte.")
        elif host_ref not in nodes_map:
            errors.append(f"BoundaryEvent '{bid}' fait référence à un hôte inexistant '{host_ref}'.")
        else:
            host_type = nodes_map[host_ref].get("type")
            if host_type not in ACTIVITY_TYPES:
                errors.append(f"BoundaryEvent '{bid}' est rattaché à '{host_ref}' ({host_type}), qui n'est pas une activité valide.")

    # 4. Intégrité référentielle des Edges
    valid_targets = set(nodes_map.keys()) | set(pools_map.keys())
    valid_sources = set(nodes_map.keys()) | set(pools_map.keys())
    valid_flow_node_targets = set(nodes_map.keys())
    valid_flow_node_sources = set(nodes_map.keys())

    sequence_edges = [e for e in edges_list if e.get("type") == "sequenceFlow" or not e.get("type")]
    message_edges = [e for e in edges_list if e.get("type") == "messageFlow"]
    assoc_edges = [e for e in edges_list if e.get("type") in ("association", "dataInputAssociation", "dataOutputAssociation")]

    for e in edges_list:
        eid = e.get("id", "sans_id")
        src = e.get("source")
        tgt = e.get("target")
        etype = e.get("type", "sequenceFlow")

        if etype == "messageFlow":
            if src in pools_map:
                errors.append(
                    f"Le messageFlow '{eid}' pointe depuis un pool ('{src}') au lieu d'un nœud de flux. "
                    "Un messageFlow doit obligatoirement relier deux flowNodes (ex: un sendTask/endEvent d'un côté à un receiveTask/startEvent de l'autre)."
                )
            elif not src or src not in valid_flow_node_sources:
                errors.append(f"Flux '{eid}' ({etype}) : source '{src}' introuvable.")

            if tgt in pools_map:
                errors.append(
                    f"Le messageFlow '{eid}' pointe vers un pool ('{tgt}') au lieu d'un nœud de flux. "
                    "Un messageFlow doit obligatoirement relier deux flowNodes (ex: un sendTask/endEvent d'un côté à un receiveTask/startEvent de l'autre)."
                )
            elif not tgt or tgt not in valid_flow_node_targets:
                errors.append(f"Flux '{eid}' ({etype}) : cible '{tgt}' introuvable.")
        else:
            if not src or src not in valid_sources:
                errors.append(f"Flux '{eid}' ({etype}) : source '{src}' introuvable.")
            if not tgt or tgt not in valid_targets:
                errors.append(f"Flux '{eid}' ({etype}) : cible '{tgt}' introuvable.")

        # Règle : BoundaryEvent ne peut pas recevoir de SequenceFlow
        if tgt in nodes_map and nodes_map[tgt].get("type") == "boundaryEvent" and etype == "sequenceFlow":
            errors.append(f"Flux '{eid}' : un boundaryEvent '{tgt}' ne peut JAMAIS avoir de transition entrante.")

    # 5. Règle BPMN 2.0 : Ségrégation des Pools pour SequenceFlow vs MessageFlow
    if pools_list:
        node_pool_map: dict[str, str] = {}
        for nid, n in nodes_map.items():
            pool_id = n.get("poolId")
            lane_id = n.get("laneId")
            if not pool_id and lane_id and lane_id in lane_to_pool:
                pool_id = lane_to_pool[lane_id]
            if pool_id:
                node_pool_map[nid] = pool_id

        message_pairs = {
            (msg.get("source"), msg.get("target"))
            for msg in message_edges
            if isinstance(msg.get("source"), str) and isinstance(msg.get("target"), str)
        }
        for seq in sequence_edges:
            src = seq.get("source")
            tgt = seq.get("target")
            src_pool = node_pool_map.get(src)
            tgt_pool = node_pool_map.get(tgt)
            if src_pool and tgt_pool and src_pool != tgt_pool:
                if (src, tgt) in message_pairs or (tgt, src) in message_pairs:
                    continue
                src_node = nodes_map.get(src, {})
                tgt_node = nodes_map.get(tgt, {})
                if src_node.get("type") in GATEWAY_TYPES or tgt_node.get("type") in GATEWAY_TYPES:
                    gateway_id = src if src_node.get("type") in GATEWAY_TYPES else tgt
                    errors.append(
                        f"POOL-001: SequenceFlow '{seq.get('id')}' traverse les pools distincts ('{src_pool}' -> "
                        f"'{tgt_pool}') et implique le gateway '{gateway_id}'. Un gateway ne peut JAMAIS être un "
                        "point de communication (messageFlow) : soit déplacer la tâche de l'autre côté dans le "
                        "même pool que le gateway si elle est en réalité exécutée par le même acteur, soit "
                        "insérer une tâche intermédiaire dans le pool du gateway qui porte, elle, le messageFlow "
                        "vers l'autre pool."
                    )
                else:
                    errors.append(
                        f"POOL-001: SequenceFlow '{seq.get('id')}' traverse les pools distincts ('{src_pool}' -> "
                        f"'{tgt_pool}'). Utilisez un 'messageFlow'."
                    )

        for msg in message_edges:
            src = msg.get("source")
            tgt = msg.get("target")
            src_pool = node_pool_map.get(src)
            tgt_pool = node_pool_map.get(tgt)
            if src_pool and tgt_pool and src_pool == tgt_pool:
                errors.append(
                    f"MESSAGE-002: MessageFlow '{msg.get('id')}' est interne au même participant ('{src_pool}'). Un messageFlow doit relier deux participants distincts."
                )

    for nid, node in nodes_map.items():
        ntype = node.get("type")
        if ntype == "parallelGateway":
            label = f"{node.get('name','')} {node.get('documentation','')}".lower()
            source_parallel_hint = bool(source_text) and any(keyword in (source_text or "").lower() for keyword in [
                "parallel", "simultaneously", "concurrent", "en même temps", "simultanément",
                "at the same time", "same time", "and split", "instruction parallèle", "synchronisation"
            ])
            explicit_parallel = source_parallel_hint or any(keyword in label for keyword in [
                "parallel", "simultaneously", "concurrent", "en même temps", "simultanément",
                "and split", "instruction parallèle", "synchronisation"
            ])
            if source_text is not None and not explicit_parallel and ("auto" in label or "fallback" in label):
                errors.append(f"GATEWAY-001: ParallelGateway '{nid}' n'est pas justifié par la logique métier. Un parallèle doit être explicite dans le texte.")

        if ntype in GATEWAY_TYPES:
            outs = [e for e in sequence_edges if e.get("source") == nid]
            if ntype == "exclusiveGateway" and len(outs) >= 2:
                labels = [str((e.get("name") or e.get("condition") or "")).strip().lower() for e in outs]
                if not any(labels) or all(not label for label in labels):
                    errors.append(f"GATEWAY-002: ExclusiveGateway '{nid}' doit avoir des branches décisionnelles cohérentes (libellées ou conditionnées).")

    for nid, node in nodes_map.items():
        if node.get("type") in {"intermediateCatchEvent", "receiveTask"}:
            name = str(node.get("name", "")).lower()
            if not isinstance(node.get("name"), str):
                continue
            if node.get("eventDefinition") == "timer":
                continue
            if any(token in name for token in ["within 24 hours", "within 24h", "after 24 hours", "timeout", "délai", "under 24 hours", "dans 24h", "dans 24 heures", "avant 24 heures"]):
                continue
            if any(marker in name for marker in ["payment received", "paiement reçu", "received payment", "paiement reçu"]):
                errors.append(f"EVENT-001: '{nid}' représente un message reçu, pas un Timer Event.")

    # 6. Présence minimale d'événements de début et de fin
    node_types = [n.get("type") for n in nodes_list]
    has_start = any(t == "startEvent" for t in node_types)
    has_end = any(t == "endEvent" for t in node_types)

    if not has_start:
        errors.append("Le processus ne contient aucun événement de début ('startEvent').")
    if not has_end:
        errors.append("Le processus ne contient aucun événement de fin ('endEvent').")

    # 7. Continuité du graphe séquentiel (Entrées / Sorties)
    seq_incoming: dict[str, list[str]] = {nid: [] for nid in nodes_map}
    seq_outgoing: dict[str, list[str]] = {nid: [] for nid in nodes_map}

    for e in sequence_edges:
        src = e.get("source")
        tgt = e.get("target")
        if src in seq_outgoing:
            seq_outgoing[src].append(e.get("id"))
        if tgt in seq_incoming:
            seq_incoming[tgt].append(e.get("id"))

    for nid, node in nodes_map.items():
        ntype = node.get("type")
        if ntype in ARTIFACT_TYPES:
            continue

        message_incoming = any(
            isinstance(e, dict) and e.get("type") == "messageFlow" and e.get("target") == nid
            for e in edges_list
        )
        message_outgoing = any(
            isinstance(e, dict) and e.get("type") == "messageFlow" and e.get("source") == nid
            for e in edges_list
        )

        # Un enfant de subProcess (parentSubProcessId) est exempté de ces deux règles :
        # son entrée/sortie interne est déduite de sa position dans la chaîne des
        # AUTRES enfants (premier = pas de prédécesseur interne, dernier = pas de
        # successeur interne), pas d'un sequenceFlow externe (cf. section 9.1 de
        # SKILL.md — un tel sequenceFlow externe est d'ailleurs retiré automatiquement).
        is_subprocess_child = isinstance(node.get("parentSubProcessId"), str)

        # Une activité de compensation (isForCompensation=true) n'est JAMAIS reliée
        # au flux normal par sequenceFlow — elle n'est atteinte que via une
        # association de compensation depuis un throw/boundaryEvent compensation/
        # cancel (cf. section 8.1 de SKILL.md). Absence totale d'entrée/sortie
        # séquentielle y est donc normale, pas une impasse à signaler.
        is_compensation = bool(node.get("isForCompensation"))

        # Un subProcess événementiel (triggeredByEvent=true) n'a par conception
        # ni sequenceFlow entrant (déclenché par son propre startEvent interne,
        # pas par le flux parent) ni nécessairement de sortant (il peut se
        # contenter de terminer la branche qu'il interrompt) — cf. section 9.1
        # de SKILL.md.
        is_event_subprocess = ntype == "subProcess" and bool(node.get("triggeredByEvent"))

        # StartEvent et BoundaryEvent n'ont pas d'entrée
        if ntype not in ("startEvent", "boundaryEvent") and not is_subprocess_child and not is_compensation and not is_event_subprocess and len(seq_incoming[nid]) == 0 and not message_incoming:
            errors.append(f"Nœud '{nid}' ({ntype} : '{node.get('name', '')}') n'a aucune transition séquentielle entrante.")

        # EndEvent n'a pas de sortie
        if ntype != "endEvent" and not is_subprocess_child and not is_compensation and not is_event_subprocess and len(seq_outgoing[nid]) == 0 and not message_outgoing:
            errors.append(f"Nœud '{nid}' ({ntype} : '{node.get('name', '')}') n'a aucune transition séquentielle sortante (impasse).")

    # 8. Validation approfondie des Passerelles (Gateways)
    for nid, node in nodes_map.items():
        ntype = node.get("type")
        if ntype in GATEWAY_TYPES:
            direction = node.get("gatewayDirection")
            outs = seq_outgoing.get(nid, [])
            ins = seq_incoming.get(nid, [])

            if not direction:
                warnings.append(
                    f"Passerelle '{nid}' ({ntype}) : 'gatewayDirection' non spécifié (recommandé: 'diverging' ou 'converging')."
                )

            # Passerelle divergente
            if direction == "diverging" or len(outs) > 1:
                if len(outs) < 2:
                    warnings.append(f"Passerelle divergente '{nid}' a moins de 2 sorties ({len(outs)}).")
                else:
                    out_edge_objs = [e for e in sequence_edges if e.get("source") == nid]
                    if ntype == "exclusiveGateway":
                        unlabeled = [e.get("id") for e in out_edge_objs if not e.get("name") and not e.get("condition")]
                        if unlabeled:
                            errors.append(
                                f"Passerelle exclusive XOR '{nid}' : branches sans libellé ni condition : {unlabeled}"
                            )
                        default_count = sum(1 for e in out_edge_objs if e.get("isDefault"))
                        if default_count > 1:
                            errors.append(
                                f"Passerelle '{nid}' : plusieurs flux marqués par défaut ('isDefault': true)."
                            )

            # Passerelle convergente
            if direction == "converging" or len(ins) > 1:
                if len(ins) < 2:
                    warnings.append(f"Passerelle convergente '{nid}' a moins de 2 entrées ({len(ins)}).")

    # 9. Topologie Soundness (Connexité BFS/DFS)
    start_nodes = [nid for nid, n in nodes_map.items() if n.get("type") == "startEvent"]
    visited_from_start: set[str] = set()

    def _traverse_forward(cur: str):
        if cur in visited_from_start:
            return
        visited_from_start.add(cur)
        # Nœuds enfants via sequenceFlows
        for edge_id in seq_outgoing.get(cur, []):
            edge_obj = next((e for e in sequence_edges if e.get("id") == edge_id), None)
            if edge_obj and edge_obj.get("target"):
                _traverse_forward(edge_obj["target"])
        # A participant receiving a message is reachable through the
        # collaboration from the sender's reachable flow; it does not need a
        # synthetic start event in its pool.
        for edge in edges_list:
            if edge.get("type") == "messageFlow" and edge.get("source") == cur:
                target = edge.get("target")
                if isinstance(target, str) and target in nodes_map:
                    _traverse_forward(target)
        # Traiter également les boundary events rattachés à cette activité
        for b in boundary_nodes:
            if b.get("attachedToRef") == cur:
                _traverse_forward(b["id"])

    for s in start_nodes:
        _traverse_forward(s)

    for nid, node in nodes_map.items():
        if node.get("type") in ARTIFACT_TYPES:
            continue
        if isinstance(node.get("parentSubProcessId"), str):
            continue  # connectivité interne au subProcess, cf. section 9.1 de SKILL.md
        if node.get("isForCompensation"):
            continue  # atteinte uniquement via association de compensation, jamais par sequenceFlow
        if node.get("type") == "subProcess" and node.get("triggeredByEvent"):
            continue  # déclenché par son propre startEvent interne, jamais par le flux parent (section 9.1)
        if nid not in visited_from_start:
            errors.append(f"Nœud '{nid}' ({node.get('type')}) est inaccessible depuis les événements de début.")

    # 10. Cohérence des couloirs (Lanes)
    if pools_list:
        declared_lane_ids = set(lanes_map.keys())
        for nid, n in nodes_map.items():
            lid = n.get("laneId")
            pid = n.get("poolId")
            if lid and lid not in declared_lane_ids:
                if pid and pid in pools_map and pid == lid:
                    continue
                if pid and pid in pools_map and not pool_lane_ids.get(pid):
                    continue
                errors.append(f"Nœud '{nid}' référence le couloir '{lid}' non déclaré dans pools[].lanes.")

    # 11. Détection des lacunes (GAPs)
    gap_count = 0
    for nid, n in nodes_map.items():
        doc = n.get("documentation", "")
        if isinstance(doc, str) and "GAP:" in doc:
            gap_count += 1
            gap_text = doc.split("GAP:", 1)[1].strip()
            warnings.append(f"Nœud '{nid}' : Lacune métier (GAP) identifiée -> {gap_text}")

    warnings.extend(_check_missing_data_objects_warning(validation_source, source_text))
    warnings.extend(_check_missing_transaction_cancel_warning(validation_source))
    warnings.extend(_check_pool_split_without_external_evidence(validation_source, source_text))
    warnings.extend(_check_multi_instance_modeled_as_named_branches(validation_source))
    warnings.extend(_check_threshold_count_needs_complex_gateway(validation_source, source_text))
    warnings.extend(_check_unjustified_backward_loop(validation_source, source_text))

    stats = {
        "total_nodes": len(nodes_list),
        "total_edges": len(edges_list),
        "sequence_flows": len(sequence_edges),
        "message_flows": len(message_edges),
        "pools": len(pools_list),
        "lanes": len(lanes_map),
        "gaps_detected": gap_count,
    }

    return ValidationResult(
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
        normalized_logic_core=normalized_logic_core,
    )