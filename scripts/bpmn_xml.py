"""
Générateur de XML BPMN 2.0 conforme au standard officiel OMG (ISO/IEC 19510).

Intègre la sémantique métier (processus, collaboration, pools, couloirs, tâches,
passerelles, boundary events, définitions d'événements) et les métadonnées de rendu
BPMNDI (dc:Bounds pour les formes, di:waypoint pour les arêtes et étiquettes).
"""
from __future__ import annotations

import html
from typing import Any


def _esc(val: Any) -> str:
    """Échappe les caractères spéciaux pour le XML."""
    if val is None:
        return ""
    return html.escape(str(val))


EVENT_DEF_TAGS = {
    "timer": "timerEventDefinition",
    "message": "messageEventDefinition",
    "error": "errorEventDefinition",
    "signal": "signalEventDefinition",
    "terminate": "terminateEventDefinition",
    "escalation": "escalationEventDefinition",
    "conditional": "conditionalEventDefinition",
    "compensation": "compensateEventDefinition",
    "link": "linkEventDefinition",
    "cancel": "cancelEventDefinition",
}

LOOP_CHARACTERISTICS_TAGS = {
    "multiInstanceParallel": ('bpmn:multiInstanceLoopCharacteristics', ' isSequential="false"'),
    "multiInstanceSequential": ('bpmn:multiInstanceLoopCharacteristics', ' isSequential="true"'),
    "standardLoop": ('bpmn:standardLoopCharacteristics', ''),
}


def _emit_loop_characteristics(lines: list[str], node: dict[str, Any], indent: str) -> None:
    """Émet le marqueur de boucle (multi-instance parallèle/séquentiel ou boucle
    standard) uniquement si explicitement renseigné — jamais par défaut."""
    loop = node.get("loopCharacteristics")
    if not isinstance(loop, str) or loop == "none" or loop not in LOOP_CHARACTERISTICS_TAGS:
        return
    tag, attrs = LOOP_CHARACTERISTICS_TAGS[loop]
    collection = node.get("loopCollection")
    if collection and loop.startswith("multiInstance"):
        lines.append(f'{indent}<{tag}{attrs}>')
        lines.append(f'{indent}  <bpmn:loopDataInputRef>{collection}</bpmn:loopDataInputRef>')
        lines.append(f'{indent}</{tag}>')
    else:
        lines.append(f'{indent}<{tag}{attrs} />')


def _emit_flow_node(
    lines: list[str],
    node: dict[str, Any],
    indent: str,
    incoming_map: dict[str, list[str]],
    outgoing_map: dict[str, list[str]],
    seq_edges: list[dict[str, Any]],
    process_node_ids_set: set[str],
    children_by_parent: dict[str, list[dict[str, Any]]],
) -> None:
    """Émet un flowNode et, s'il s'agit d'un subProcess, ses enfants imbriqués
    (parentSubProcessId) ainsi que les sequenceFlow qui les relient ENTRE EUX,
    récursivement. Sans cette imbrication, un subProcess ne serait qu'une coquille
    vide dans le XML : ses enfants apparaîtraient à plat comme frères du process,
    perdant toute relation de containment BPMN."""
    nid = node.get("id")
    ntype = str(node.get("type", "task"))
    nname = _esc(node.get("name", ""))
    doc = node.get("documentation")
    ev_def = node.get("eventDefinition")
    ev_detail = node.get("eventDetail")

    if ntype == "boundaryEvent":
        host_ref = node.get("attachedToRef", "")
        cancel_act = "true" if node.get("cancelActivity", True) else "false"
        lines.append(f'{indent}<bpmn:boundaryEvent id="{nid}" name="{nname}" attachedToRef="{host_ref}" cancelActivity="{cancel_act}">')
        if doc:
            lines.append(f'{indent}  <bpmn:documentation>{_esc(doc)}</bpmn:documentation>')
        for edge_id in outgoing_map.get(nid, []):
            if any(edge.get("id") == edge_id and edge.get("source") in process_node_ids_set and edge.get("target") in process_node_ids_set for edge in seq_edges):
                lines.append(f'{indent}  <bpmn:outgoing>{edge_id}</bpmn:outgoing>')
        if isinstance(ev_def, str) and ev_def in EVENT_DEF_TAGS:
            tag = EVENT_DEF_TAGS[ev_def]
            lines.append(f'{indent}  <bpmn:{tag} id="{nid}_def">')
            if ev_def == "timer" and ev_detail:
                lines.append(f'{indent}    <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">{_esc(ev_detail)}</bpmn:timeDuration>')
            lines.append(f'{indent}  </bpmn:{tag}>')
        lines.append(f'{indent}</bpmn:boundaryEvent>')
        return

    if ntype == "subProcess":
        # Un sous-processus transactionnel est un ÉLÉMENT XML distinct en BPMN 2.0
        # (bpmn:transaction, bordure double), pas un simple bpmn:subProcess avec un
        # attribut — cf. BUG 7 de rapport_tests_v2.md : sans ce tag dédié, aucune
        # sémantique de transaction/annulation groupée n'est portée par le XML,
        # quel que soit le contenu (cancelEndEvent, compensations) à l'intérieur.
        is_transaction = bool(node.get("isTransaction"))
        tag_name = "bpmn:transaction" if is_transaction else "bpmn:subProcess"
        triggered_attr = ' triggeredByEvent="true"' if node.get("triggeredByEvent") else ""
        compensation_attr = ' isForCompensation="true"' if node.get("isForCompensation") else ""
        lines.append(f'{indent}<{tag_name} id="{nid}" name="{nname}"{triggered_attr}{compensation_attr}>')
        if doc:
            lines.append(f'{indent}  <bpmn:documentation>{_esc(doc)}</bpmn:documentation>')
        for edge_id in incoming_map.get(nid, []):
            if any(edge.get("id") == edge_id and edge.get("source") in process_node_ids_set and edge.get("target") in process_node_ids_set for edge in seq_edges):
                lines.append(f'{indent}  <bpmn:incoming>{edge_id}</bpmn:incoming>')
        for edge_id in outgoing_map.get(nid, []):
            if any(edge.get("id") == edge_id and edge.get("source") in process_node_ids_set and edge.get("target") in process_node_ids_set for edge in seq_edges):
                lines.append(f'{indent}  <bpmn:outgoing>{edge_id}</bpmn:outgoing>')
        _emit_loop_characteristics(lines, node, indent + "  ")

        children = children_by_parent.get(nid, [])
        child_ids = {c.get("id") for c in children if isinstance(c.get("id"), str)}
        for child in children:
            _emit_flow_node(lines, child, indent + "  ", incoming_map, outgoing_map, seq_edges, process_node_ids_set, children_by_parent)

        for edge in seq_edges:
            eid, src, tgt = edge.get("id"), edge.get("source"), edge.get("target")
            if not isinstance(eid, str) or src not in child_ids or tgt not in child_ids:
                continue
            lines.append(f'{indent}  <bpmn:sequenceFlow id="{eid}" name="{_esc(edge.get("name", ""))}" sourceRef="{src}" targetRef="{tgt}">')
            if edge.get("condition"):
                lines.append(f'{indent}    <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">{_esc(edge["condition"])}</bpmn:conditionExpression>')
            lines.append(f'{indent}  </bpmn:sequenceFlow>')

        lines.append(f'{indent}</{tag_name}>')
        return

    if ntype in ("dataObjectReference", "dataInput", "dataOutput"):
        # dataInput/dataOutput (portée processus) sont modélisés comme des
        # dataObjectReference — BPMN 2.0 les distingue formellement via
        # ioSpecification, une construction lourde rarement rendue visuellement ;
        # le nom du nœud porte la distinction sémantique. isCollection est porté
        # par l'objet référencé (bpmn:dataObject), pas par la référence elle-même,
        # pour que les viewers affichent correctement le marqueur "III".
        obj_id = f"{nid}_obj"
        is_collection = "true" if node.get("isCollection") else "false"
        lines.append(f'{indent}<bpmn:dataObject id="{obj_id}" isCollection="{is_collection}" />')
        lines.append(f'{indent}<bpmn:dataObjectReference id="{nid}" name="{nname}" dataObjectRef="{obj_id}" />')
        return

    if ntype == "dataStoreReference":
        lines.append(f'{indent}<bpmn:dataStoreReference id="{nid}" name="{nname}" />')
        return

    if ntype == "textAnnotation":
        lines.append(f'{indent}<bpmn:textAnnotation id="{nid}"><bpmn:text>{nname}</bpmn:text></bpmn:textAnnotation>')
        return

    default_attr = ""
    if ntype == "exclusiveGateway":
        default_edge = next((edge for edge in seq_edges if edge.get("source") == nid and edge.get("isDefault") and edge.get("target") in process_node_ids_set), None)
        if default_edge is not None:
            default_attr = f' default="{default_edge["id"]}"'
    compensation_attr = ' isForCompensation="true"' if node.get("isForCompensation") else ""

    lines.append(f'{indent}<bpmn:{ntype} id="{nid}" name="{nname}"{default_attr}{compensation_attr}>')
    if doc:
        lines.append(f'{indent}  <bpmn:documentation>{_esc(doc)}</bpmn:documentation>')

    for edge_id in incoming_map.get(nid, []):
        if any(edge.get("id") == edge_id and edge.get("source") in process_node_ids_set and edge.get("target") in process_node_ids_set for edge in seq_edges):
            lines.append(f'{indent}  <bpmn:incoming>{edge_id}</bpmn:incoming>')
    for edge_id in outgoing_map.get(nid, []):
        if any(edge.get("id") == edge_id and edge.get("source") in process_node_ids_set and edge.get("target") in process_node_ids_set for edge in seq_edges):
            lines.append(f'{indent}  <bpmn:outgoing>{edge_id}</bpmn:outgoing>')

    if isinstance(ev_def, str) and ev_def in EVENT_DEF_TAGS:
        tag = EVENT_DEF_TAGS[ev_def]
        if ev_def == "link":
            # Le nom du lien (pas son id) fait office de clé d'appariement throw/catch.
            lines.append(f'{indent}  <bpmn:{tag} id="{nid}_def" name="{_esc(ev_detail or nname)}" />')
        else:
            lines.append(f'{indent}  <bpmn:{tag} id="{nid}_def">')
            if ev_def == "timer" and ev_detail:
                lines.append(f'{indent}    <bpmn:timeDuration xsi:type="bpmn:tFormalExpression">{_esc(ev_detail)}</bpmn:timeDuration>')
            lines.append(f'{indent}  </bpmn:{tag}>')
    _emit_loop_characteristics(lines, node, indent + "  ")
    lines.append(f'{indent}</bpmn:{ntype}>')


def generate_bpmn_xml(logic_core: dict[str, Any], layout_result: dict[str, Any]) -> str:
    """Génère le document XML BPMN 2.0 à partir d'un Logic-Core normalisé."""
    process_info = logic_core.get("process", {}) if isinstance(logic_core.get("process"), dict) else {}
    default_process_id = str(process_info.get("id", "Process_Main"))
    default_process_name = str(process_info.get("name", "Processus Métier"))
    is_executable = "true" if process_info.get("isExecutable", True) else "false"

    nodes = [node for node in logic_core.get("nodes", []) if isinstance(node, dict)]
    edges = [edge for edge in logic_core.get("edges", []) if isinstance(edge, dict)]
    pools = [pool for pool in logic_core.get("pools", []) if isinstance(pool, dict)]

    node_positions = layout_result.get("nodes", {}) if isinstance(layout_result, dict) else {}
    edge_routes = layout_result.get("edges", {}) if isinstance(layout_result, dict) else {}
    lane_positions = layout_result.get("lanes", {}) if isinstance(layout_result, dict) else {}
    pool_positions = layout_result.get("pools", {}) if isinstance(layout_result, dict) else {}

    pool_by_id = {pool.get("id"): pool for pool in pools if isinstance(pool.get("id"), str)}
    node_to_pool: dict[str, str] = {}
    for node in nodes:
        pool_id = node.get("poolId")
        if isinstance(pool_id, str) and pool_id in pool_by_id:
            node_to_pool[node.get("id")] = pool_id

    # Un enfant de subProcess (parentSubProcessId) n'a normalement pas besoin de son
    # propre poolId : il hérite de celui de son subProcess parent, transitivement.
    subprocess_ids = {n.get("id") for n in nodes if n.get("type") == "subProcess" and isinstance(n.get("id"), str)}
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        parent = n.get("parentSubProcessId")
        if isinstance(parent, str) and parent in subprocess_ids:
            children_by_parent.setdefault(parent, []).append(n)
    all_child_ids = {c.get("id") for kids in children_by_parent.values() for c in kids if isinstance(c.get("id"), str)}

    pool_process_ids: dict[str, str] = {}
    process_node_ids: dict[str, set[str]] = {}
    process_nodes_by_id: dict[str, list[dict[str, Any]]] = {}

    if pools:
        used_process_ids: set[str] = set()
        for pool in pools:
            pid = pool.get("id")
            if not isinstance(pid, str):
                continue
            pool_nodes = [node for node in nodes if node.get("poolId") == pid]
            pool_node_id_set = {n.get("id") for n in pool_nodes}
            changed = True
            while changed:
                changed = False
                for n in nodes:
                    nid = n.get("id")
                    if nid in pool_node_id_set:
                        continue
                    parent = n.get("parentSubProcessId")
                    if isinstance(parent, str) and parent in pool_node_id_set:
                        pool_nodes.append(n)
                        pool_node_id_set.add(nid)
                        changed = True
            if not pool_nodes:
                continue
            process_id = str(pool.get("processRef") or (default_process_id if len(pools) == 1 else f"{pid}_process"))
            # Ensure each pool gets a unique process_id even if processRef is shared
            if process_id in used_process_ids:
                process_id = f"{pid}_process"
            if process_id in used_process_ids:
                suffix = 2
                base = process_id
                while process_id in used_process_ids:
                    process_id = f"{base}_{suffix}"
                    suffix += 1
            used_process_ids.add(process_id)
            pool_process_ids[pid] = process_id
            process_node_ids[process_id] = {n.get("id") for n in pool_nodes if isinstance(n.get("id"), str)}
            process_nodes_by_id[process_id] = pool_nodes


    if not process_node_ids:
        process_node_ids[default_process_id] = {node.get("id") for node in nodes if isinstance(node.get("id"), str)}
        process_nodes_by_id[default_process_id] = nodes

    seq_edges: list[dict[str, Any]] = []
    msg_edges: list[dict[str, Any]] = []
    assoc_edges: list[dict[str, Any]] = []
    for edge in edges:
        edge_type = edge.get("type")
        if edge_type in ("association", "dataInputAssociation", "dataOutputAssociation"):
            assoc_edges.append(edge)
            continue
        if edge_type == "messageFlow":
            msg_edges.append(edge)
            continue

        src = edge.get("source")
        tgt = edge.get("target")
        src_pool = node_to_pool.get(src)
        tgt_pool = node_to_pool.get(tgt)
        if isinstance(src, str) and isinstance(tgt, str) and src_pool and tgt_pool and src_pool != tgt_pool:
            msg_edges.append(edge)
        else:
            seq_edges.append(edge)

    incoming_map: dict[str, list[str]] = {}
    outgoing_map: dict[str, list[str]] = {}
    for node_ids in process_node_ids.values():
        for edge in seq_edges:
            src = edge.get("source")
            tgt = edge.get("target")
            if isinstance(src, str) and src in node_ids:
                outgoing_map.setdefault(src, []).append(edge.get("id"))
            if isinstance(tgt, str) and tgt in node_ids:
                incoming_map.setdefault(tgt, []).append(edge.get("id"))

    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"')
    lines.append('                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"')
    lines.append('                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"')
    lines.append('                  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"')
    lines.append('                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    lines.append(f'                  id="Definitions_{default_process_id}"')
    lines.append('                  targetNamespace="http://bpmn.io/schema/bpmn"')
    lines.append('                  exporter="BPMN-Agent" exporterVersion="1.0">')

    # Génération des éléments <bpmn:message> pour chaque messageFlow (conforme BPMN 2.0)
    for msg in msg_edges:
        mid = msg.get("id")
        if isinstance(mid, str):
            mname = _esc(msg.get("name") or mid)
            lines.append(f'  <bpmn:message id="Message_{mid}" name="{mname}" />')

    collaboration_id = f"Collaboration_{default_process_id}"
    has_collaboration = bool(pools) or bool(msg_edges)
    if has_collaboration:
        lines.append(f'  <bpmn:collaboration id="{collaboration_id}">')
        if pools:
            for pool in pools:
                pid = pool.get("id")
                if not isinstance(pid, str):
                    continue
                pname = _esc(pool.get("name", pid))
                process_ref = pool_process_ids.get(pid)
                if process_ref:
                    lines.append(f'    <bpmn:participant id="{pid}" name="{pname}" processRef="{process_ref}" />')
                else:
                    lines.append(f'    <bpmn:participant id="{pid}" name="{pname}" />')
        else:
            lines.append(f'    <bpmn:participant id="Participant_Main" name="{_esc(default_process_name)}" processRef="{default_process_id}" />')

        for msg in msg_edges:
            mid = msg.get("id")
            src = msg.get("source")
            tgt = msg.get("target")
            if not isinstance(mid, str) or not isinstance(src, str) or not isinstance(tgt, str):
                continue
            mname = _esc(msg.get("name", ""))
            msg_ref = f"Message_{mid}"
            lines.append(f'    <bpmn:messageFlow id="{mid}" name="{mname}" sourceRef="{src}" targetRef="{tgt}" messageRef="{msg_ref}" />')
        lines.append('  </bpmn:collaboration>')

    process_entries: list[tuple[str, str, list[dict[str, Any]]]] = []
    if pools:
        for pool in pools:
            pid = pool.get("id")
            if not isinstance(pid, str):
                continue
            process_id = pool_process_ids.get(pid)
            if process_id is None:
                continue
            process_entries.append((process_id, str(pool.get("name", process_id)), process_nodes_by_id.get(process_id, [])))
    if not process_entries:
        process_entries.append((default_process_id, default_process_name, process_nodes_by_id.get(default_process_id, nodes)))

    for process_id, process_name, process_nodes in process_entries:
        process_node_ids_set = {node.get("id") for node in process_nodes if isinstance(node.get("id"), str)}
        lines.append(f'  <bpmn:process id="{process_id}" name="{_esc(process_name)}" isExecutable="{is_executable}">')

        process_lanes = []
        if pools:
            for pool in pools:
                pid = pool.get("id")
                if pool_process_ids.get(pid) != process_id:
                    continue
                process_lanes.extend(pool.get("lanes", []))
        if process_lanes:
            lines.append('    <bpmn:laneSet id="LaneSet_1">')
            for lane in process_lanes:
                lid = lane.get("id")
                lname = _esc(lane.get("name", ""))
                lines.append(f'      <bpmn:lane id="{lid}" name="{lname}">')
                for node in process_nodes:
                    # Un enfant de subProcess n'est jamais référencé directement par la
                    # lane externe : il est contenu dans le subProcess, pas un flowNode
                    # de premier niveau dans cette lane.
                    if node.get("laneId") == lid and node.get("id") not in all_child_ids:
                        lines.append(f'        <bpmn:flowNodeRef>{node["id"]}</bpmn:flowNodeRef>')
                lines.append('      </bpmn:lane>')
            lines.append('    </bpmn:laneSet>')

        # Les enfants de subProcess sont émis à l'intérieur de leur parent (voir
        # _emit_flow_node), jamais comme frères directs du process.
        for node in process_nodes:
            if node.get("id") in all_child_ids:
                continue
            _emit_flow_node(lines, node, "    ", incoming_map, outgoing_map, seq_edges, process_node_ids_set, children_by_parent)

        for edge in seq_edges:
            eid = edge.get("id")
            src = edge.get("source")
            tgt = edge.get("target")
            if not isinstance(eid, str) or not isinstance(src, str) or not isinstance(tgt, str):
                continue
            if src not in process_node_ids_set or tgt not in process_node_ids_set:
                continue
            if src in all_child_ids or tgt in all_child_ids:
                # Un sequenceFlow touchant un enfant de subProcess ne peut jamais être
                # émis au niveau du process englobant (BPMN interdit de relier un
                # élément imbriqué à un élément extérieur à son subProcess) : soit il a
                # déjà été émis à l'intérieur du subProcess parent (deux enfants du
                # même subProcess), soit c'est une arête invalide qui traverse la
                # frontière du subProcess (ex: subProcess -> son propre enfant) —
                # dans les deux cas, elle ne doit pas apparaître ici.
                continue
            lines.append(f'    <bpmn:sequenceFlow id="{eid}" name="{_esc(edge.get("name", ""))}" sourceRef="{src}" targetRef="{tgt}">')
            if edge.get("condition"):
                lines.append(f'      <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">{_esc(edge["condition"])}</bpmn:conditionExpression>')
            lines.append('    </bpmn:sequenceFlow>')

        for edge in assoc_edges:
            aid = edge.get("id")
            src = edge.get("source")
            tgt = edge.get("target")
            if isinstance(aid, str) and isinstance(src, str) and isinstance(tgt, str) and (src in process_node_ids_set or tgt in process_node_ids_set):
                lines.append(f'    <bpmn:association id="{aid}" sourceRef="{src}" targetRef="{tgt}" />')

        lines.append('  </bpmn:process>')

    lines.append('  <bpmndi:BPMNDiagram id="BPMNDiagram_1">')
    lines.append(f'    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="{collaboration_id if has_collaboration else default_process_id}">')

    if pools:
        for pool in pools:
            pid = pool.get("id")
            if not isinstance(pid, str):
                continue
            pos = pool_positions.get(pid, {"x": 80, "y": 80, "width": 1000, "height": 250})
            lines.append(f'      <bpmndi:BPMNShape id="{pid}_di" bpmnElement="{pid}" isHorizontal="true">')
            lines.append(f'        <dc:Bounds x="{int(pos["x"])}" y="{int(pos["y"])}" width="{int(pos["width"])}" height="{int(pos["height"])}" />')
            lines.append('      </bpmndi:BPMNShape>')

    for pool in pools:
        for lane in pool.get("lanes", []):
            lid = lane.get("id")
            if not isinstance(lid, str):
                continue
            pos = lane_positions.get(lid, {"x": 90, "y": 90, "width": 930, "height": 160})
            lines.append(f'      <bpmndi:BPMNShape id="{lid}_di" bpmnElement="{lid}" isHorizontal="true">')
            lines.append(f'        <dc:Bounds x="{int(pos["x"])}" y="{int(pos["y"])}" width="{int(pos["width"])}" height="{int(pos["height"])}" />')
            lines.append('      </bpmndi:BPMNShape>')

    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str):
            continue
        pos = node_positions.get(nid, {"x": 100, "y": 100, "width": 120, "height": 80})
        # Un subProcess DOIT déclarer isExpanded explicitement : sans cet attribut,
        # chaque viewer BPMN choisit son propre défaut (certains l'affichent réduit,
        # d'autres développé), ce qui produit un rendu incohérent d'un outil à
        # l'autre pour la même donnée. On l'affiche toujours développé par défaut,
        # puisque la génération produit systématiquement la structure complète.
        expanded_attr = ' isExpanded="true"' if node.get("type") == "subProcess" else ""
        lines.append(f'      <bpmndi:BPMNShape id="{nid}_di" bpmnElement="{nid}"{expanded_attr}>')
        lines.append(f'        <dc:Bounds x="{int(pos["x"])}" y="{int(pos["y"])}" width="{int(pos["width"])}" height="{int(pos["height"])}" />')
        lines.append('      </bpmndi:BPMNShape>')

    for edge in seq_edges + msg_edges + assoc_edges:
        eid = edge.get("id")
        if not isinstance(eid, str):
            continue
        waypoints = edge_routes.get(eid, [])
        if not waypoints and isinstance(edge.get("source"), str) and isinstance(edge.get("target"), str):
            src_pos = node_positions.get(edge["source"], {"x": 0, "y": 0, "width": 120, "height": 80})
            tgt_pos = node_positions.get(edge["target"], {"x": 200, "y": 0, "width": 120, "height": 80})
            waypoints = [
                {"x": src_pos["x"] + src_pos["width"], "y": src_pos["y"] + src_pos["height"] / 2},
                {"x": tgt_pos["x"], "y": tgt_pos["y"] + tgt_pos["height"] / 2},
            ]
        lines.append(f'      <bpmndi:BPMNEdge id="{eid}_di" bpmnElement="{eid}">')
        for pt in waypoints:
            lines.append(f'        <di:waypoint x="{int(pt["x"])}" y="{int(pt["y"])}" />')
        if edge.get("name"):
            mid_pt = waypoints[len(waypoints) // 2] if waypoints else {"x": 100, "y": 100}
            lines.append('        <bpmndi:BPMNLabel>')
            lines.append(f'          <dc:Bounds x="{int(mid_pt["x"]) - 20}" y="{int(mid_pt["y"]) - 20}" width="60" height="14" />')
            lines.append('        </bpmndi:BPMNLabel>')
        lines.append('      </bpmndi:BPMNEdge>')

    lines.append('    </bpmndi:BPMNPlane>')
    lines.append('  </bpmndi:BPMNDiagram>')
    lines.append('</bpmn:definitions>')
    return "\n".join(lines)
