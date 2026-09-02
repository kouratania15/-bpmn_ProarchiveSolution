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
    "textAnnotation",
}


# ---------------------------------------------------------------------------
# Helpers de connectivité (utilisés par la passe de rattachement automatique)
# ---------------------------------------------------------------------------

def _bfs_reachable(start_ids: list[str], seq_edges: list[dict[str, Any]]) -> set[str]:
    """Retourne l'ensemble des ids atteignables par sequenceFlow depuis start_ids."""
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
    start_ids = [n["id"] for n in nodes if n.get("type") == "startEvent"] + sorted(message_targets)
    end_ids = [n["id"] for n in nodes if n.get("type") == "endEvent"]
    non_connectable = {"startEvent", "endEvent", "boundaryEvent"} | ARTIFACT_TYPES
    business_ids = [n["id"] for n in nodes if n.get("type") not in non_connectable]

    if not start_ids:
        # Pas de startEvent : rien à raccrocher en amont, la règle 6 de
        # validate_logic_core lèvera déjà l'erreur correspondante.
        return

    # --- 1. Rattachement de TOUTES les composantes inatteignables depuis un start ---
    reachable = _bfs_reachable(start_ids, seq_edges)
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
            reachable = _bfs_reachable(start_ids, seq_edges)

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

    node_map = {n["id"]: n for n in nodes if isinstance(n.get("id"), str)}
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
            if src_node and src_node.get("type") in ("startEvent", "boundaryEvent"):
                continue
            edge["type"] = "messageFlow"
            if not any(existing.get("id") == edge.get("id") for existing in message_edges):
                message_edges.append(edge)
            if edge in seq_edges:
                seq_edges.remove(edge)

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
            if src_node and src_node.get("type") in ("startEvent", "boundaryEvent"):
                continue
            edge["type"] = "messageFlow"
            if edge in seq_edges:
                seq_edges.remove(edge)
            if edge not in message_edges:
                message_edges.append(edge)

    synthetic_start_ids = []
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
        if node.get("type") == "startEvent" and nid.startswith("start_pool_"):
            synthetic_start_ids.append(nid)

    for pool_id, start_ids in pool_start_events.items():
        if len(start_ids) > 1:
            synthetic_start_ids.extend(start_id for start_id in start_ids if start_id.startswith("start_pool_"))

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
    return final_logic_core


def validate_process_description(process_desc: dict[str, Any]) -> ValidationResult:
    """Valide une Process Description contre son schema JSON."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(process_desc, dict):
        return ValidationResult(ok=False, errors=["Process Description must be a JSON object"])

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
                errors.append(
                    f"POOL-001: SequenceFlow '{seq.get('id')}' traverse les pools distincts ('{src_pool}' -> '{tgt_pool}'). Utilisez un 'messageFlow'."
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

        # StartEvent et BoundaryEvent n'ont pas d'entrée
        if ntype not in ("startEvent", "boundaryEvent") and len(seq_incoming[nid]) == 0 and not message_incoming:
            errors.append(f"Nœud '{nid}' ({ntype} : '{node.get('name', '')}') n'a aucune transition séquentielle entrante.")

        # EndEvent n'a pas de sortie
        if ntype != "endEvent" and len(seq_outgoing[nid]) == 0 and not message_outgoing:
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