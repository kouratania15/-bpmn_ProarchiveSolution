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
    "textAnnotation": (120.0, 40.0),
}

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
    in_deg: dict[str, int] = {nid: 0 for nid in node_ids}

    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        etype = e.get("type", "sequenceFlow")
        if etype == "sequenceFlow" and src in adj and tgt in in_deg:
            adj[src].append(tgt)
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

    # Calcul des coordonnées physiques
    positions: dict[str, dict[str, float]] = {}
    cur_x = start_x

    for l_idx in sorted(layers.keys()):
        nids = layers[l_idx]
        cur_y = start_y
        for nid in nids:
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
            cur_y += h + node_spacing_y
        cur_x += layer_spacing_x

    return positions


def compute_layout(logic_core: dict[str, Any]) -> dict[str, Any]:
    """
    Calcule la disposition géométrique complète du diagramme BPMN 2.0.
    Gère les pools, les swimlanes, les boundaryEvents et le routage orthogonal des flux.
    """
    nodes = logic_core.get("nodes", [])
    edges = logic_core.get("edges", [])
    pools = logic_core.get("pools", [])

    # 1. Calcul initial des positions relatives par couches
    raw_positions = _compute_layered_positions(nodes, edges)

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
                # Pool sans lane spécifique
                pool_nodes = [n for n in nodes if n.get("poolId") == pool_id]
                pool_node_ids = {n["id"] for n in pool_nodes}
                p_positions = {nid: pos for nid, pos in raw_positions.items() if nid in pool_node_ids}

                lane_h = max(
                    max((pos["y"] + pos["height"] - current_pool_y + 60.0 for pos in p_positions.values()), default=MIN_LANE_HEIGHT),
                    MIN_LANE_HEIGHT,
                )
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

                # Trouver la largeur maximale
                for lane in lanes:
                    lid = lane["id"]
                    lane_node_ids = {n["id"] for n in nodes if n.get("laneId") == lid}
                    l_positions = {nid: pos for nid, pos in raw_positions.items() if nid in lane_node_ids}
                    max_x_in_lane = max((pos["x"] + pos["width"] for pos in l_positions.values()), default=700.0)
                    max_canvas_x = max(max_canvas_x, max_x_in_lane + 120.0)

                pool_total_w = max(max_canvas_x, 900.0)

                for lane in lanes:
                    lid = lane["id"]
                    lane_node_ids = {n["id"] for n in nodes if n.get("laneId") == lid}
                    l_positions = {nid: pos for nid, pos in raw_positions.items() if nid in lane_node_ids}

                    # Recalibrer les Y des nœuds dans cette lane
                    y_offset = current_lane_y + 40.0
                    for nid in lane_node_ids:
                        if nid in raw_positions:
                            raw_positions[nid]["y"] = y_offset
                            y_offset += raw_positions[nid]["height"] + 30.0

                    lane_h = max(y_offset - current_lane_y + 40.0, MIN_LANE_HEIGHT)
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