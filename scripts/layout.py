"""
Moteur de disposition géométrique BPMN 2.0 (pyelk & Pure-Python Sugiyama Layered avec partitionnement swimlanes).

Calcule :
- Les dimensions et coordonnées (x, y, width, height) pour chaque nœud (tâches, événements, passerelles, artefacts)
- La position des boundaryEvents attachés sur le pourtour de leur activité hôte
- Les boîtes englobantes des Couloirs (Lanes) et des Participants (Pools)
- Les waypoints orthogonaux précis pour chaque transition (sequenceFlow, messageFlow, association)
"""
from __future__ import annotations

from typing import Any

# Dimensions officielles conformes au standard BPMN 2.0 (Camunda / bpmn-js)
DEFAULT_SIZES: dict[str, tuple[float, float]] = {
    "startEvent": (36.0, 36.0),
    "endEvent": (36.0, 36.0),
    "intermediateCatchEvent": (36.0, 36.0),
    "intermediateThrowEvent": (36.0, 36.0),
    "boundaryEvent": (36.0, 36.0),
    "task": (100.0, 80.0),
    "userTask": (100.0, 80.0),
    "serviceTask": (100.0, 80.0),
    "scriptTask": (100.0, 80.0),
    "manualTask": (100.0, 80.0),
    "sendTask": (100.0, 80.0),
    "receiveTask": (100.0, 80.0),
    "businessRuleTask": (100.0, 80.0),
    "callActivity": (100.0, 80.0),
    "subProcess": (200.0, 120.0),
    "exclusiveGateway": (50.0, 50.0),
    "parallelGateway": (50.0, 50.0),
    "inclusiveGateway": (50.0, 50.0),
    "eventBasedGateway": (50.0, 50.0),
    "complexGateway": (50.0, 50.0),
    "dataObjectReference": (36.0, 50.0),
    "dataStoreReference": (50.0, 50.0),
    "dataInput": (36.0, 50.0),
    "dataOutput": (36.0, 50.0),
    "textAnnotation": (120.0, 40.0),
}

ARTIFACT_NODE_TYPES = {"dataObjectReference", "dataStoreReference", "dataInput", "dataOutput", "textAnnotation"}

LANE_HEADER_WIDTH = 30.0
POOL_HEADER_WIDTH = 30.0
MIN_LANE_HEIGHT = 160.0


def _compute_layered_positions(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    start_x: float = 120.0,
    start_y: float = 80.0,
    layer_spacing_x: float = 160.0,
    node_spacing_y: float = 50.0,
) -> dict[str, dict[str, float]]:
    """
    Algorithme de Sugiyama simplifié par couches pour le calcul des coordonnées X/Y.
    """
    node_ids = [n["id"] for n in nodes if n.get("type") != "boundaryEvent"]
    node_by_id = {n["id"]: n for n in nodes}

    # Calcul du graphe d'adjacence séquentiel
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    rev_adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    in_deg: dict[str, int] = {nid: 0 for nid in node_ids}

    # Le messageFlow doit aussi contraindre l'ordre des couches : un nœud dont
    # la seule entrée est un messageFlow a un in_deg de 0 en ne comptant que le
    # sequenceFlow, et se retrouve placé dans la toute première colonne — avant
    # son propre expéditeur. Le trait doit alors repartir en arrière au lieu
    # d'avancer, ce qui produit un rendu confus quand deux pools communiquent.
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        etype = e.get("type", "sequenceFlow")
        if etype in ("sequenceFlow", "messageFlow") and src in adj and tgt in in_deg:
            adj[src].append(tgt)
            rev_adj[tgt].append(src)
            in_deg[tgt] += 1

    # Attribution des couches topologiques (BFS par niveaux)
    layers: dict[int, list[str]] = {}
    node_layer: dict[str, int] = {}
    queue = [nid for nid, deg in in_deg.items() if deg == 0]
    if not queue and node_ids:
        queue = [node_ids[0]]

    curr_layer = 0
    visited = set()
    while queue:
        next_queue = []
        layers[curr_layer] = []
        for nid in queue:
            if nid in visited:
                continue
            visited.add(nid)
            node_layer[nid] = curr_layer
            layers[curr_layer].append(nid)
            for succ in adj.get(nid, []):
                if succ not in visited:
                    next_queue.append(succ)
        queue = list(dict.fromkeys(next_queue))
        curr_layer += 1

    # Attribution de couche par défaut pour les éventuels résidus / cycles
    for nid in node_ids:
        if nid not in node_layer:
            node_layer[nid] = curr_layer
            layers.setdefault(curr_layer, []).append(nid)

    # Calcul des coordonnées physiques.
    # Au sein de chaque couche, les nœuds sont ordonnés par barycentre de leurs
    # prédécesseurs déjà placés (heuristique Sugiyama standard) : un nœud qui a
    # un seul prédécesseur hérite du même rang vertical que lui. Un enchaînement
    # séquentiel simple (A -> B -> C) reste ainsi parfaitement horizontal, et
    # seules les vraies bifurcations (plusieurs successeurs simultanés) sont
    # écartées verticalement. Sans cela, chaque couche repartait de haut en bas
    # dans l'ordre du parcours BFS, produisant un tracé en zigzag même pour un
    # flux strictement séquentiel.
    positions: dict[str, dict[str, float]] = {}
    slot_of: dict[str, int] = {}
    cur_x = start_x

    for l_idx in sorted(layers.keys()):
        nids = layers[l_idx]
        if l_idx == 0:
            ordered = nids
        else:
            def _barycenter(nid: str, _slot_of: dict[str, int] = slot_of) -> float:
                preds = [p for p in rev_adj.get(nid, []) if p in _slot_of]
                if not preds:
                    return float("inf")
                return sum(_slot_of[p] for p in preds) / len(preds)

            ordered = sorted(nids, key=_barycenter)

        cur_y = start_y
        for slot, nid in enumerate(ordered):
            node_obj = node_by_id.get(nid, {})
            ntype = node_obj.get("type", "task")
            w, h = DEFAULT_SIZES.get(ntype, (100.0, 80.0))

            positions[nid] = {
                "x": cur_x,
                "y": cur_y,
                "width": w,
                "height": h,
                "laneId": node_obj.get("laneId"),
                "poolId": node_obj.get("poolId"),
            }
            slot_of[nid] = slot
            cur_y += h + node_spacing_y
        cur_x += layer_spacing_x

    return positions


def _remap_rows_into_band(
    node_ids: set[str],
    positions: dict[str, dict[str, float]],
    band_start_y: float,
) -> float:
    """Retasse un ensemble de nœuds (ceux d'un pool ou d'une lane) dans une
    bande verticale, en conservant les rangées relatives déjà calculées par
    _compute_layered_positions : les nœuds qui partageaient la même ligne
    (donc le même rang dans le flux principal) restent alignés entre eux ;
    seules les vraies bifurcations occupent des lignes distinctes.

    Sans ce remappage par rangée, réattribuer un y strictement croissant à
    chaque nœud dans un ordre arbitraire (ex: itération d'un set) casse
    l'alignement déjà correct et recrée un tracé en zigzag.
    """
    band_positions = {nid: positions[nid] for nid in node_ids if nid in positions}
    if not band_positions:
        return MIN_LANE_HEIGHT

    distinct_rows = sorted({pos["y"] for pos in band_positions.values()})
    row_index = {y: i for i, y in enumerate(distinct_rows)}

    row_height: dict[int, float] = {}
    for pos in band_positions.values():
        r = row_index[pos["y"]]
        row_height[r] = max(row_height.get(r, 0.0), pos["height"])

    row_y: dict[int, float] = {}
    cursor = band_start_y + 40.0
    for r in sorted(row_height.keys()):
        row_y[r] = cursor
        cursor += row_height[r] + 30.0

    for pos in band_positions.values():
        pos["y"] = row_y[row_index[pos["y"]]]

    return max(cursor - band_start_y + 40.0, MIN_LANE_HEIGHT)


def compute_layout(logic_core: dict[str, Any]) -> dict[str, Any]:
    """
    Calcule la disposition géométrique complète du diagramme BPMN 2.0.
    Gère les pools, les swimlanes, les boundaryEvents et le routage orthogonal des flux.
    """
    nodes = logic_core.get("nodes", [])
    edges = logic_core.get("edges", [])
    pools = logic_core.get("pools", [])

    # 1. Calcul initial des positions relatives par couches. Les enfants d'un
    # subProcess (parentSubProcessId) sont exclus de cette passe de premier niveau :
    # ils sont positionnés séparément ci-dessous, à l'intérieur des limites de leur
    # subProcess, pour ne pas polluer la grille X/Y du diagramme principal.
    subprocess_ids = {
        n["id"] for n in nodes
        if isinstance(n, dict) and n.get("type") == "subProcess" and isinstance(n.get("id"), str)
    }
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        parent = n.get("parentSubProcessId")
        if isinstance(parent, str) and parent in subprocess_ids:
            children_by_parent.setdefault(parent, []).append(n)
    child_node_ids = {c["id"] for children in children_by_parent.values() for c in children if isinstance(c.get("id"), str)}

    # Les artefacts (Data Object/Data Store/annotation) n'ont aucun sequenceFlow —
    # ils circulent par association, hors flux de séquence (cf. section sur les
    # Data Objects de SKILL.md). Sans cette exclusion, ils atterriraient tous en
    # première couche (in_deg=0 dans le graphe sequenceFlow), mélangés aux vrais
    # points d'entrée. Ils sont positionnés séparément ci-dessous, près de la
    # tâche à laquelle ils sont associés.
    artifact_ids = {
        n["id"] for n in nodes
        if isinstance(n, dict) and n.get("type") in ARTIFACT_NODE_TYPES and isinstance(n.get("id"), str)
    }

    top_level_nodes = [n for n in nodes if not (isinstance(n, dict) and n.get("id") in child_node_ids | artifact_ids)]
    top_level_edges = [
        e for e in edges
        if isinstance(e, dict) and e.get("source") not in child_node_ids and e.get("target") not in child_node_ids
    ]

    raw_positions = _compute_layered_positions(top_level_nodes, top_level_edges)

    # 1bis. Positionnement interne (RELATIF) des enfants de chaque subProcess, et
    # agrandissement de sa boîte pour les contenir. L'offset absolu n'est appliqué
    # qu'à la toute fin (étape 2bis), une fois la position définitive du subProcess
    # connue — celle-ci peut encore bouger lors de l'ajustement par swimlane
    # ci-dessous (_remap_rows_into_band réattribue les Y pour caser le subProcess
    # dans sa lane), et appliquer l'offset trop tôt désynchroniserait les enfants
    # de leur parent une fois celui-ci déplacé.
    SUBPROCESS_PADDING_X = 40.0
    SUBPROCESS_PADDING_TOP = 50.0  # espace réservé à l'en-tête/nom du subProcess
    SUBPROCESS_PADDING_BOTTOM = 30.0

    children_relative_positions: dict[str, dict[str, dict[str, float]]] = {}
    for sp_id, children in children_by_parent.items():
        sp_pos = raw_positions.get(sp_id)
        if sp_pos is None:
            continue
        child_ids = {c["id"] for c in children if isinstance(c.get("id"), str)}
        child_edges = [
            e for e in edges
            if isinstance(e, dict) and e.get("source") in child_ids and e.get("target") in child_ids
        ]
        child_positions = _compute_layered_positions(
            children, child_edges, start_x=SUBPROCESS_PADDING_X, start_y=SUBPROCESS_PADDING_TOP,
        )
        if child_positions:
            max_x = max(pos["x"] + pos["width"] for pos in child_positions.values())
            max_y = max(pos["y"] + pos["height"] for pos in child_positions.values())
        else:
            max_x, max_y = 200.0, 120.0

        sp_pos["width"] = max(sp_pos["width"], max_x + SUBPROCESS_PADDING_X)
        sp_pos["height"] = max(sp_pos["height"], max_y + SUBPROCESS_PADDING_BOTTOM)
        children_relative_positions[sp_id] = child_positions

        # Repousser la couche suivante (et tout ce qui vient après) si la largeur
        # requise par ce subProcess dépasse l'espacement standard entre couches —
        # ce qui arrive même sans enfants, car la taille par défaut d'un subProcess
        # (200px) dépasse déjà l'espacement de couche standard (160px). Comparer au
        # besoin réel plutôt qu'à une "croissance" évite de rater ce cas de base.
        required_right_edge = sp_pos["x"] + sp_pos["width"]
        next_layer_xs = [pos["x"] for oid, pos in raw_positions.items() if oid != sp_id and pos["x"] > sp_pos["x"]]
        if next_layer_xs:
            next_layer_x = min(next_layer_xs)
            push = required_right_edge - next_layer_x
            if push > 0:
                for other_id, other_pos in raw_positions.items():
                    if other_id != sp_id and other_pos["x"] >= next_layer_x:
                        other_pos["x"] += push

    # 2. Ajustement par Swimlanes (si des pools/lanes sont déclarés)
    lane_positions: dict[str, dict[str, float]] = {}
    pool_positions: dict[str, dict[str, float]] = {}

    has_lanes = any(len(p.get("lanes", [])) > 0 for p in pools)

    if pools:
        current_pool_y = 50.0
        max_canvas_x = 0.0

        for pool in pools:
            pool_id = pool["id"]
            lanes = pool.get("lanes", [])
            pool_lanes_start_y = current_pool_y
            lane_count = max(len(lanes), 1)

            if not lanes:
                # Pool sans lane spécifique. Les enfants de subProcess sont exclus :
                # leur position est déjà figée (imbriquée dans leur subProcess parent),
                # les inclure ici les ferait réétaler par _remap_rows_into_band.
                pool_nodes = [n for n in nodes if n.get("poolId") == pool_id and n.get("id") not in child_node_ids]
                pool_node_ids = {n["id"] for n in pool_nodes}

                lane_h = _remap_rows_into_band(pool_node_ids, raw_positions, current_pool_y)
                p_positions = {nid: pos for nid, pos in raw_positions.items() if nid in pool_node_ids}
                pool_w = max((pos["x"] + pos["width"] + 100.0 for pos in p_positions.values()), default=800.0)
                max_canvas_x = max(max_canvas_x, pool_w)

                pool_positions[pool_id] = {
                    "x": 60.0,
                    "y": current_pool_y,
                    "width": pool_w,
                    "height": lane_h,
                }
                current_pool_y += lane_h + 40.0
            else:
                # Pool avec couloirs (Lanes)
                current_lane_y = current_pool_y
                lane_heights = {}

                # Trouver la largeur maximale (hors enfants de subProcess, cf. ci-dessous)
                for lane in lanes:
                    lid = lane["id"]
                    lane_node_ids = {n["id"] for n in nodes if n.get("laneId") == lid and n.get("id") not in child_node_ids}
                    l_positions = {nid: pos for nid, pos in raw_positions.items() if nid in lane_node_ids}
                    max_x_in_lane = max((pos["x"] + pos["width"] for pos in l_positions.values()), default=700.0)
                    max_canvas_x = max(max_canvas_x, max_x_in_lane + 120.0)

                pool_total_w = max(max_canvas_x, 900.0)

                for lane in lanes:
                    lid = lane["id"]
                    # Les enfants de subProcess sont exclus : leur position (déjà
                    # imbriquée dans leur subProcess parent) ne doit pas être réétalée.
                    lane_node_ids = {n["id"] for n in nodes if n.get("laneId") == lid and n.get("id") not in child_node_ids}

                    lane_h = _remap_rows_into_band(lane_node_ids, raw_positions, current_lane_y)
                    lane_heights[lid] = lane_h

                    lane_positions[lid] = {
                        "x": 90.0,  # Décalé du header de pool
                        "y": current_lane_y,
                        "width": pool_total_w - 30.0,
                        "height": lane_h,
                    }
                    current_lane_y += lane_h

                total_pool_h = current_lane_y - pool_lanes_start_y
                pool_positions[pool_id] = {
                    "x": 60.0,
                    "y": pool_lanes_start_y,
                    "width": pool_total_w,
                    "height": total_pool_h,
                }
                current_pool_y = current_lane_y + 40.0

    # 2bis. Application de l'offset absolu aux enfants de subProcess, maintenant que
    # la position définitive (post-swimlane) de chaque subProcess est connue.
    for sp_id, child_positions in children_relative_positions.items():
        sp_pos = raw_positions.get(sp_id)
        if sp_pos is None:
            continue
        for cid, cpos in child_positions.items():
            raw_positions[cid] = {
                **cpos,
                "x": cpos["x"] + sp_pos["x"],
                "y": cpos["y"] + sp_pos["y"],
            }

    # 2ter. Positionnement des artefacts (Data Object/Data Store/annotation) reliés
    # par association : au-dessus de la tâche associée, décalés les uns des autres
    # si plusieurs artefacts partagent la même tâche. Position par défaut (coin
    # supérieur gauche) si l'artefact n'a aucune association exploitable.
    if artifact_ids:
        assoc_partner: dict[str, str] = {}
        for e in edges:
            if not isinstance(e, dict) or e.get("type") not in ("association", "dataInputAssociation", "dataOutputAssociation"):
                continue
            s, t = e.get("source"), e.get("target")
            if s in artifact_ids and isinstance(t, str):
                assoc_partner[s] = t
            elif t in artifact_ids and isinstance(s, str):
                assoc_partner[t] = s

        slot_by_partner: dict[str, int] = {}
        for node in nodes:
            nid = node.get("id") if isinstance(node, dict) else None
            if nid not in artifact_ids:
                continue
            w, h = DEFAULT_SIZES.get(node.get("type"), (36.0, 50.0))
            partner = assoc_partner.get(nid)
            partner_pos = raw_positions.get(partner) if partner else None
            if partner_pos is not None:
                slot = slot_by_partner.get(partner, 0)
                slot_by_partner[partner] = slot + 1
                raw_positions[nid] = {
                    "x": partner_pos["x"] + (partner_pos["width"] - w) / 2.0 + slot * (w + 30.0),
                    "y": max(10.0, partner_pos["y"] - h - 40.0),
                    "width": w,
                    "height": h,
                }
            else:
                raw_positions[nid] = {"x": 60.0, "y": 20.0, "width": w, "height": h}

    # 3. Positionnement précis des Boundary Events sur le pourtour de leur hôte
    for node in nodes:
        if node.get("type") == "boundaryEvent":
            bid = node["id"]
            host_id = node.get("attachedToRef")
            w, h = DEFAULT_SIZES["boundaryEvent"]
            if host_id and host_id in raw_positions:
                host_pos = raw_positions[host_id]
                # Placement sur le bord inférieur droit de l'activité hôte
                raw_positions[bid] = {
                    "x": host_pos["x"] + host_pos["width"] - (w / 2.0),
                    "y": host_pos["y"] + host_pos["height"] - (h / 2.0),
                    "width": w,
                    "height": h,
                    "attachedToRef": host_id,
                }
            else:
                raw_positions[bid] = {
                    "x": 200.0,
                    "y": 200.0,
                    "width": w,
                    "height": h,
                }

    # 4. Calcul des waypoints orthogonaux des transitions (Edges)
    edge_routes: dict[str, list[dict[str, float]]] = {}
    for edge in edges:
        eid = edge.get("id")
        src_id = edge.get("source")
        tgt_id = edge.get("target")

        src_pos = raw_positions.get(src_id) or pool_positions.get(src_id)
        tgt_pos = raw_positions.get(tgt_id) or pool_positions.get(tgt_id)

        if src_pos and tgt_pos:
            # Source : Sortie droite ou bas (si boundary)
            if src_id in raw_positions and raw_positions[src_id].get("attachedToRef"):
                # Sortie depuis le bas d'un boundary event
                start_pt = {
                    "x": src_pos["x"] + src_pos["width"] / 2.0,
                    "y": src_pos["y"] + src_pos["height"],
                }
            else:
                start_pt = {
                    "x": src_pos["x"] + src_pos["width"],
                    "y": src_pos["y"] + src_pos["height"] / 2.0,
                }

            # Cible : Entrée gauche ou haut
            end_pt = {
                "x": tgt_pos["x"],
                "y": tgt_pos["y"] + tgt_pos["height"] / 2.0,
            }

            # Tracé orthogonal en L ou en Z (angles droits déterministes)
            if abs(start_pt["y"] - end_pt["y"]) < 4.0:
                waypoints = [start_pt, end_pt]
            elif start_pt["x"] < end_pt["x"]:
                mid_x = start_pt["x"] + (end_pt["x"] - start_pt["x"]) / 2.0
                waypoints = [
                    start_pt,
                    {"x": mid_x, "y": start_pt["y"]},
                    {"x": mid_x, "y": end_pt["y"]},
                    end_pt,
                ]
            else:
                # Flux retour (Loopback / Cycle vers l'amont)
                offset_y = max(start_pt["y"], end_pt["y"]) + 60.0
                waypoints = [
                    start_pt,
                    {"x": start_pt["x"] + 30.0, "y": start_pt["y"]},
                    {"x": start_pt["x"] + 30.0, "y": offset_y},
                    {"x": end_pt["x"] - 30.0, "y": offset_y},
                    {"x": end_pt["x"] - 30.0, "y": end_pt["y"]},
                    end_pt,
                ]

            edge_routes[eid] = waypoints

    # Dimensions globales de la toile
    max_w = max((p["x"] + p["width"] for p in raw_positions.values()), default=1000.0) + 120.0
    max_h = max((p["y"] + p["height"] for p in raw_positions.values()), default=700.0) + 120.0

    return {
         
        "nodes": raw_positions,
        "edges": edge_routes,
        "lanes": lane_positions,
        "pools": pool_positions,
        "canvas": {"width": max_w, "height": max_h},
    }