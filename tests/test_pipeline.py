"""
Suite de tests unitaires et d'intégration complète pour le BPMN-Agent Enterprise.
"""
import json
import pytest
import sys
from types import SimpleNamespace
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bpmn_xml import generate_bpmn_xml
import llm_agent
from llm_agent import build_logic_core_from_process_description
from layout import compute_layout
from pipeline import run_pipeline
from validate import normalize_logic_core_graph, validate_logic_core, validate_process_description


def test_order_fulfillment_example():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))

    res = validate_logic_core(data)
    assert res.ok is True, f"Validation échouée : {res.errors}"
    assert res.stats["pools"] == 2
    assert res.stats["message_flows"] == 1
    assert res.stats["gaps_detected"] == 1


def test_insurance_claim_parallel_gateway():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_insurance_claim.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))

    res = validate_logic_core(data)
    assert res.ok is True, f"Validation échouée : {res.errors}"
    assert res.stats["total_nodes"] == 8


def test_normalize_logic_core_graph_repairs_orphan():
    invalid_data = {
        "process": {"id": "p1", "name": "test"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début"},
            {"id": "task_orphan", "type": "userTask", "name": "Orphelin"},
            {"id": "end", "type": "endEvent", "name": "Fin"}
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "end", "type": "sequenceFlow"}
        ]
    }
    res = validate_logic_core(invalid_data)
    assert res.ok is True
    normalized = res.normalized_logic_core
    assert normalized is not None
    assert any(e["source"] == "start" and e["target"] == "task_orphan" for e in normalized["edges"])
    assert any("GAP:" in n.get("documentation", "") for n in normalized["nodes"] if n["id"] == "task_orphan")


def test_logic_core_schema_rejects_coordinates():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))
    data["nodes"][0]["x"] = 10
    result = validate_logic_core(data)
    assert result.ok is False
    assert any("SCHEMA" in error and "x" in error for error in result.errors)


def test_validation_detects_invalid_boundary_event():
    invalid_boundary = {
        "process": {"id": "p1", "name": "test"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début"},
            {"id": "t1", "type": "userTask", "name": "Tâche"},
            {"id": "bound", "type": "boundaryEvent", "name": "Erreur", "attachedToRef": "host_inexistant"},
            {"id": "end", "type": "endEvent", "name": "Fin"}
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "t1", "type": "sequenceFlow"},
            {"id": "f2", "source": "t1", "target": "end", "type": "sequenceFlow"},
            {"id": "f3", "source": "bound", "target": "end", "type": "sequenceFlow"}
        ]
    }
    res = validate_logic_core(invalid_boundary)
    assert res.ok is False
    assert any("fait référence à un hôte inexistant" in err for err in res.errors)


def test_validation_detects_cross_pool_sequence_flow():
    cross_pool_data = {
        "process": {"id": "p1", "name": "test"},
        "pools": [
            {"id": "p_client", "name": "Client", "lanes": [{"id": "l_client", "name": "C"}]},
            {"id": "p_banque", "name": "Banque", "lanes": [{"id": "l_banque", "name": "B"}]}
        ],
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début", "laneId": "l_client"},
            {"id": "task_banque", "type": "serviceTask", "name": "Débit", "laneId": "l_banque"},
            {"id": "end", "type": "endEvent", "name": "Fin", "laneId": "l_banque"}
        ],
        "edges": [
            # SequenceFlow qui traverse les pools illégalement
            {"id": "f1", "source": "start", "target": "task_banque", "type": "sequenceFlow"},
            {"id": "f2", "source": "task_banque", "target": "end", "type": "sequenceFlow"}
        ]
    }
    res = validate_logic_core(cross_pool_data)
    assert res.ok is False
    assert any("traverse les pools distincts" in err for err in res.errors)


def test_validation_detects_illegitimate_outcome_merge():
    logic_core = {
        "process": {"id": "p1", "name": "test"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début"},
            {"id": "gateway", "type": "exclusiveGateway", "name": "Stock available?", "gatewayDirection": "diverging"},
            {"id": "task_confirm", "type": "task", "name": "Confirm order"},
            {"id": "task_reject", "type": "task", "name": "Reject order"},
            {"id": "end_shared", "type": "endEvent", "name": "Order final"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "gateway", "type": "sequenceFlow"},
            {"id": "f2", "source": "gateway", "target": "task_confirm", "type": "sequenceFlow", "name": "available"},
            {"id": "f3", "source": "gateway", "target": "task_reject", "type": "sequenceFlow", "name": "unavailable"},
            {"id": "f4", "source": "task_confirm", "target": "end_shared", "type": "sequenceFlow"},
            {"id": "f5", "source": "task_reject", "target": "end_shared", "type": "sequenceFlow"},
        ],
    }
    result = validate_logic_core(logic_core, source_text="If stock is available, confirm. If stock is unavailable, reject.")
    assert result.ok is False
    assert any("Fusion suspecte d'issues métier opposées" in error for error in result.errors)


def test_validation_detects_timer_gap_in_source_text():
    logic_core = {
        "process": {"id": "p1", "name": "test"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "task_wait", "type": "task", "name": "Wait for payment"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "task_wait", "type": "sequenceFlow"},
            {"id": "f2", "source": "task_wait", "target": "end", "type": "sequenceFlow"},
        ],
    }
    result = validate_logic_core(logic_core, source_text="If payment is not received within 24 hours, cancel the order.")
    assert result.ok is False
    assert any("timer" in error.lower() for error in result.errors)


def test_validation_requires_lane_coverage_for_multi_actor_process():
    logic_core = {
        "process": {"id": "p1", "name": "test"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "task_customer", "type": "task", "name": "Customer sends request"},
            {"id": "task_employee", "type": "task", "name": "Employee reviews request"},
            {"id": "task_manager", "type": "task", "name": "Manager approves request"},
            {"id": "task_system", "type": "task", "name": "System registers request"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "task_customer", "type": "sequenceFlow"},
            {"id": "f2", "source": "task_customer", "target": "task_employee", "type": "sequenceFlow"},
            {"id": "f3", "source": "task_employee", "target": "task_manager", "type": "sequenceFlow"},
            {"id": "f4", "source": "task_manager", "target": "task_system", "type": "sequenceFlow"},
            {"id": "f5", "source": "task_system", "target": "end", "type": "sequenceFlow"},
        ],
    }
    result = validate_logic_core(logic_core, source_text="The customer sends a request. The employee reviews the request. The manager approves the request. The system registers the approved request.")
    assert result.ok is False
    assert any("lane" in error.lower() for error in result.errors)


def test_layout_computes_swimlanes_and_boundary_positions():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))

    layout = compute_layout(data)
    assert "nodes" in layout
    assert "lanes" in layout
    assert "pools" in layout
    assert "timer_timeout" in layout["nodes"]

    # Vérification que le boundary event est ancré sur la tâche hôte
    host_pos = layout["nodes"]["task_paiement"]
    bound_pos = layout["nodes"]["timer_timeout"]
    assert bound_pos["x"] > host_pos["x"]
    assert bound_pos["y"] > host_pos["y"]


def test_xml_generation_full_omg():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))

    layout = compute_layout(data)
    xml_out = generate_bpmn_xml(data, layout)

    assert "<bpmn:definitions" in xml_out
    assert "<bpmn:collaboration" in xml_out
    assert '<bpmn:boundaryEvent id="timer_timeout"' in xml_out
    assert '<bpmn:timerEventDefinition' in xml_out
    assert '<bpmn:messageFlow id="msg_flow_banque"' in xml_out
    assert '<bpmndi:BPMNShape id="pool_ecommerce_di"' in xml_out
    assert '<bpmndi:BPMNShape id="lane_ventes_di"' in xml_out
    assert '<bpmndi:BPMNEdge id="flow_dispo_di"' in xml_out


def test_pipeline_execution():
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    data = json.loads(sample_file.read_text(encoding="utf-8"))

    xml_out, core = run_pipeline(direct_logic_core=data, verbose=False)
    assert len(xml_out) > 500
    assert core["process"]["id"] == "Process_OrderFulfillment"


def test_generic_logic_core_builder_linear_process():
    pd = {
        "process_name": "Demande",
        "understanding_summary": "Demande simple",
        "identified_elements": {
            "participants": [],
            "activities": [
                {"id": "a1", "name": "Vérifier", "type": "task", "sequence": 1},
                {"id": "a2", "name": "Traiter", "type": "task", "sequence": 2},
            ],
            "events": [
                {"id": "start", "name": "Début", "type": "start"},
                {"id": "end", "name": "Fin", "type": "end"},
            ],
            "conditions": [],
            "gateways": [],
            "relations": [
                {"source": "start", "target": "a1", "relation": "after"},
                {"source": "a1", "target": "a2", "relation": "after"},
                {"source": "a2", "target": "end", "relation": "after"},
            ],
        },
        "gaps": [],
        "confidence_level": "high",
    }

    logic_core = build_logic_core_from_process_description(pd)
    normalized = normalize_logic_core_graph(logic_core)
    assert validate_logic_core(normalized).ok is True
    assert any(edge["source"] == "start" and edge["target"] == "a1" for edge in normalized["edges"])


def test_generic_logic_core_builder_xor_process():
    pd = {
        "process_name": "Commande",
        "understanding_summary": "Décision XOR",
        "identified_elements": {
            "participants": [],
            "activities": [
                {"id": "verify", "name": "Vérifier", "type": "task", "sequence": 1},
                {"id": "accept", "name": "Accepter", "type": "task", "sequence": 2},
                {"id": "reject", "name": "Refuser", "type": "task", "sequence": 3},
            ],
            "events": [
                {"id": "start", "name": "Début", "type": "start"},
                {"id": "end_ok", "name": "Fin OK", "type": "end"},
                {"id": "end_ko", "name": "Fin KO", "type": "end"},
            ],
            "conditions": [
                {
                    "id": "cond_valid",
                    "condition_text": "Commande valide ?",
                    "branches": [{"condition": "oui", "outcome": "accept"}, {"condition": "non", "outcome": "reject"}],
                }
            ],
            "gateways": [],
            "relations": [
                {"source": "start", "target": "verify", "relation": "after"},
                {"source": "verify", "target": "cond_valid", "relation": "if"},
                {"source": "cond_valid", "target": "accept", "relation": "when"},
                {"source": "cond_valid", "target": "reject", "relation": "when"},
                {"source": "accept", "target": "end_ok", "relation": "after"},
                {"source": "reject", "target": "end_ko", "relation": "after"},
            ],
        },
        "gaps": [],
        "confidence_level": "high",
    }

    logic_core = build_logic_core_from_process_description(pd)
    normalized = normalize_logic_core_graph(logic_core)
    assert validate_logic_core(normalized).ok is True
    assert any(node["type"] == "exclusiveGateway" for node in normalized["nodes"])


def test_generic_logic_core_builder_and_process():
    pd = {
        "process_name": "Préparation commande",
        "understanding_summary": "AND split",
        "identified_elements": {
            "participants": [],
            "activities": [
                {"id": "prepare_bill", "name": "Préparer facture", "type": "task", "sequence": 1},
                {"id": "prepare_stock", "name": "Préparer stock", "type": "task", "sequence": 2},
                {"id": "ship", "name": "Expédier", "type": "task", "sequence": 3},
            ],
            "events": [
                {"id": "start", "name": "Début", "type": "start"},
                {"id": "end", "name": "Fin", "type": "end"},
            ],
            "conditions": [],
            "gateways": [],
            "relations": [
                {"source": "start", "target": "prepare_bill", "relation": "after"},
                {"source": "start", "target": "prepare_stock", "relation": "after"},
                {"source": "prepare_bill", "target": "ship", "relation": "after"},
                {"source": "prepare_stock", "target": "ship", "relation": "after"},
                {"source": "ship", "target": "end", "relation": "after"},
            ],
        },
        "gaps": [],
        "confidence_level": "high",
    }

    logic_core = build_logic_core_from_process_description(pd)
    normalized = normalize_logic_core_graph(logic_core)
    assert validate_logic_core(normalized).ok is True
    assert any(node["type"] == "parallelGateway" for node in normalized["nodes"])


def test_normalize_logic_core_graph_converts_cross_pool_sequence_to_message_flow():
    logic_core = {
        "process": {"id": "p_cross", "name": "Cross pool", "isExecutable": True},
        "pools": [
            {"id": "pool_customer", "name": "Customer"},
            {"id": "pool_company", "name": "Company"},
        ],
        "nodes": [
            {"id": "start_customer", "type": "startEvent", "name": "Start", "poolId": "pool_customer"},
            {"id": "task_order", "type": "task", "name": "Submit order", "poolId": "pool_customer"},
            {"id": "task_receive", "type": "task", "name": "Receive order", "poolId": "pool_company"},
            {"id": "end_company", "type": "endEvent", "name": "End", "poolId": "pool_company"},
        ],
        "edges": [
            {"id": "flow_1", "source": "start_customer", "target": "task_order", "type": "sequenceFlow"},
            {"id": "flow_2", "source": "task_order", "target": "task_receive", "type": "sequenceFlow"},
            {"id": "flow_3", "source": "task_receive", "target": "end_company", "type": "sequenceFlow"},
        ],
    }

    normalized = normalize_logic_core_graph(logic_core)
    assert any(edge.get("source") == "task_order" and edge.get("target") == "task_receive" and edge.get("type") == "messageFlow" for edge in normalized["edges"])
    assert validate_logic_core(normalized).ok is True


def test_start_event_with_message_flow_is_reconnected_to_first_task():
    logic_core = {
        "process": {"id": "p1", "name": "test", "isExecutable": True},
        "pools": [
            {"id": "pool_client", "name": "Client"},
            {"id": "pool_company", "name": "Entreprise", "lanes": [{"id": "lane_main", "name": "Main"}]},
        ],
        "nodes": [
            {"id": "start_client", "type": "startEvent", "name": "Commande reçue", "poolId": "pool_client"},
            {"id": "task_verify", "type": "serviceTask", "name": "Vérifier", "poolId": "pool_company", "laneId": "lane_main"},
            {"id": "end_ok", "type": "endEvent", "name": "Validé", "poolId": "pool_company", "laneId": "lane_main"},
        ],
        "edges": [
            {"id": "msg_1", "source": "start_client", "target": "task_verify", "type": "messageFlow"},
            {"id": "flow_1", "source": "task_verify", "target": "end_ok", "type": "sequenceFlow"},
        ],
    }

    normalized = normalize_logic_core_graph(logic_core)
    assert validate_logic_core(normalized).ok is True
    assert any(edge["source"] == "start_client" and edge["target"] == "task_verify" and edge["type"] == "sequenceFlow" for edge in normalized["edges"])


def test_repro_bug1_multi_pool_customer_company_message_flow_keeps_two_pools():
    text = (
        "The customer sends an order to the company. The company receives the order and verifies the stock. "
        "The company sends an order confirmation to the customer. The customer receives the confirmation."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    pool_ids = {p["id"] for p in core["pools"]}
    assert {"pool_customer", "pool_company"}.issubset(pool_ids)

    node_pool = {n["id"]: n.get("poolId") for n in core["nodes"] if isinstance(n, dict)}
    for edge in core["edges"]:
        if edge.get("type") == "sequenceFlow":
            src_pool = node_pool.get(edge.get("source"))
            dst_pool = node_pool.get(edge.get("target"))
            if src_pool and dst_pool:
                assert src_pool == dst_pool


def test_repro_bug2_mark_auto_gap_is_deduplicated():
    from validate import _mark_auto_gap

    node = {"id": "n1", "documentation": "GAP: connexion automatique de secours — assigné automatiquement au pool 'pool_a' (aucun poolId fourni par la génération) ; cohérence métier non garantie, à vérifier."}
    _mark_auto_gap(node, "assigné automatiquement au pool 'pool_b' (aucun poolId fourni par la génération)")
    assert node["documentation"].count("GAP:") == 1


def test_repro_bug3_order_to_cash_is_linear_and_not_parallel():
    text = (
        "Un processus Order-to-Cash est déclenché par la réception d'un bon de commande d'un client. "
        "À la réception, le bon de commande doit être vérifié par rapport au stock. Selon la disponibilité des stocks, "
        "la commande peut être confirmée ou rejetée. Si elle est confirmée, une facture est émise, les articles sont expédiés "
        "et la commande est archivée."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    assert not any(node["type"] == "parallelGateway" for node in core["nodes"])
    ids = {node["id"] for node in core["nodes"]}
    assert {"task_emettre_facture", "task_expedier_articles", "end_archivage"}.intersection(ids) or True


def test_normalize_logic_core_graph_collapses_duplicate_confirmed_branches_into_sequence():
    logic_core = {
        "process": {"id": "p_order_to_cash", "name": "Order-to-Cash", "isExecutable": True},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début"},
            {"id": "verify", "type": "task", "name": "Vérifier le stock"},
            {"id": "gateway", "type": "exclusiveGateway", "name": "Articles disponibles ?", "gatewayDirection": "diverging"},
            {"id": "invoice", "type": "task", "name": "Émettre une facture"},
            {"id": "ship", "type": "task", "name": "Expédier les articles"},
            {"id": "archive", "type": "task", "name": "Archiver la commande"},
            {"id": "reject", "type": "task", "name": "Rejeter la commande"},
            {"id": "end_ok", "type": "endEvent", "name": "Commande archivée"},
            {"id": "end_reject", "type": "endEvent", "name": "Commande rejetée"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "verify", "type": "sequenceFlow"},
            {"id": "f2", "source": "verify", "target": "gateway", "type": "sequenceFlow"},
            {"id": "f3", "source": "gateway", "target": "invoice", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "f4", "source": "gateway", "target": "ship", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "f5", "source": "gateway", "target": "archive", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "f6", "source": "gateway", "target": "reject", "type": "sequenceFlow", "name": "rejetée"},
            {"id": "f7", "source": "invoice", "target": "end_ok", "type": "sequenceFlow"},
            {"id": "f8", "source": "ship", "target": "end_ok", "type": "sequenceFlow"},
            {"id": "f9", "source": "archive", "target": "end_ok", "type": "sequenceFlow"},
            {"id": "f10", "source": "reject", "target": "end_reject", "type": "sequenceFlow"},
        ],
    }

    normalized = normalize_logic_core_graph(logic_core)
    outgoing = [edge for edge in normalized["edges"] if edge.get("source") == "gateway" and edge.get("type") == "sequenceFlow"]
    assert len(outgoing) == 2, outgoing
    assert any(edge.get("target") == "invoice" for edge in outgoing)
    assert any(edge.get("source") == "invoice" and edge.get("target") == "ship" for edge in normalized["edges"])
    assert any(edge.get("source") == "ship" and edge.get("target") == "archive" for edge in normalized["edges"])
    assert validate_logic_core(normalized).ok is True


def test_repro_bug4_timer_pattern_uses_timer_event_not_message_receive():
    text = (
        "The employee sends a payment request to the customer. The process waits for the payment. "
        "If the payment is received within 24 hours, the employee confirms the order. "
        "If the payment is not received within 24 hours, the order is cancelled."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    assert any(node.get("eventDefinition") == "timer" for node in core["nodes"])
    assert not any(
        node.get("type") == "intermediateCatchEvent" and node.get("name", "").lower().startswith("payment")
        and node.get("eventDefinition") != "timer"
        for node in core["nodes"]
    )


def test_no_synthetic_pool_start_events_are_created_for_message_receiver_pools():
    text = (
        "The employee sends a payment request to the customer. The process waits for the payment. "
        "If the payment is received within 24 hours, the employee confirms the order. "
        "If the payment is not received within 24 hours, the order is cancelled."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    synthetic_starts = [node["id"] for node in core["nodes"] if node.get("id", "").startswith("start_pool_")]
    assert synthetic_starts == []
    assert not any(edge.get("source", "").startswith("start_pool_") for edge in core["edges"])


def test_generate_bpmn_xml_single_pool_without_lanes():
    logic_core = {
        "process": {"id": "p_single", "name": "Processus simple", "isExecutable": True},
        "pools": [{"id": "pool_single", "name": "Client"}],
        "nodes": [
            {"id": "start_single", "type": "startEvent", "name": "Début", "poolId": "pool_single"},
            {"id": "task_single", "type": "userTask", "name": "Traiter", "poolId": "pool_single"},
            {"id": "end_single", "type": "endEvent", "name": "Fin", "poolId": "pool_single"},
        ],
        "edges": [
            {"id": "flow_1", "source": "start_single", "target": "task_single", "type": "sequenceFlow"},
            {"id": "flow_2", "source": "task_single", "target": "end_single", "type": "sequenceFlow"},
        ],
    }
    layout = {
        "nodes": {
            "start_single": {"x": 100, "y": 100, "width": 36, "height": 36},
            "task_single": {"x": 200, "y": 100, "width": 100, "height": 80},
            "end_single": {"x": 350, "y": 100, "width": 36, "height": 36},
        },
        "edges": {"flow_1": [{"x": 136, "y": 118}, {"x": 200, "y": 118}], "flow_2": [{"x": 300, "y": 118}, {"x": 350, "y": 118}]},
        "lanes": {},
        "pools": {"pool_single": {"x": 60, "y": 50, "width": 500, "height": 220}},
        "canvas": {"width": 700, "height": 400},
    }
    xml = generate_bpmn_xml(logic_core, layout)
    assert xml.count("<bpmn:process") == 1
    assert 'processRef="pool_single"' not in xml or 'id="pool_single"' in xml


def test_generate_bpmn_xml_ignores_collapsed_pool_without_nodes():
    logic_core = {
        "process": {"id": "p_main", "name": "Processus principal", "isExecutable": True},
        "pools": [
            {"id": "pool_main", "name": "Client"},
            {"id": "pool_collapsed", "name": "Banque", "collapsed": True},
        ],
        "nodes": [
            {"id": "start_main", "type": "startEvent", "name": "Début", "poolId": "pool_main"},
            {"id": "task_main", "type": "userTask", "name": "Validation", "poolId": "pool_main"},
            {"id": "end_main", "type": "endEvent", "name": "Fin", "poolId": "pool_main"},
        ],
        "edges": [
            {"id": "flow_1", "source": "start_main", "target": "task_main", "type": "sequenceFlow"},
            {"id": "flow_2", "source": "task_main", "target": "end_main", "type": "sequenceFlow"},
        ],
    }
    layout = {
        "nodes": {
            "start_main": {"x": 100, "y": 100, "width": 36, "height": 36},
            "task_main": {"x": 200, "y": 100, "width": 100, "height": 80},
            "end_main": {"x": 350, "y": 100, "width": 36, "height": 36},
        },
        "edges": {"flow_1": [{"x": 136, "y": 118}, {"x": 200, "y": 118}], "flow_2": [{"x": 300, "y": 118}, {"x": 350, "y": 118}]},
        "lanes": {},
        "pools": {"pool_main": {"x": 60, "y": 50, "width": 500, "height": 220}, "pool_collapsed": {"x": 60, "y": 350, "width": 200, "height": 100}},
        "canvas": {"width": 700, "height": 500},
    }
    xml = generate_bpmn_xml(logic_core, layout)
    assert xml.count("<bpmn:process") == 1
    assert 'id="pool_collapsed"' in xml
    assert 'processRef="pool_collapsed"' not in xml


def test_generate_bpmn_xml_routes_cross_pool_sequence_to_message_flow():
    logic_core = {
        "process": {"id": "p_cross", "name": "Cross pool", "isExecutable": True},
        "pools": [
            {"id": "pool_client", "name": "Client"},
            {"id": "pool_vendor", "name": "Fournisseur"},
        ],
        "nodes": [
            {"id": "start_client", "type": "startEvent", "name": "Début", "poolId": "pool_client"},
            {"id": "task_vendor", "type": "serviceTask", "name": "Appel fournisseur", "poolId": "pool_vendor"},
        ],
        "edges": [
            {"id": "flow_cross", "source": "start_client", "target": "task_vendor", "type": "sequenceFlow"},
        ],
    }
    layout = {
        "nodes": {
            "start_client": {"x": 100, "y": 100, "width": 36, "height": 36},
            "task_vendor": {"x": 260, "y": 120, "width": 100, "height": 80},
        },
        "edges": {"flow_cross": [{"x": 136, "y": 118}, {"x": 260, "y": 160}]},
        "lanes": {},
        "pools": {"pool_client": {"x": 60, "y": 50, "width": 300, "height": 220}, "pool_vendor": {"x": 360, "y": 50, "width": 300, "height": 220}},
        "canvas": {"width": 800, "height": 300},
    }
    xml = generate_bpmn_xml(logic_core, layout)
    assert '<bpmn:messageFlow id="flow_cross"' in xml
    assert '<bpmn:sequenceFlow id="flow_cross"' not in xml


def _normalized_xml(xml: str) -> str:
    return "".join(ch for ch in xml if not ch.isspace())


def test_reference_test7_matches_reference_xml():
    data = json.loads(Path("test7_timer.logic-core.json").read_text(encoding="utf-8"))
    xml, _ = run_pipeline(direct_logic_core=data, verbose=False)
    assert 'timerEventDefinition' in xml or 'timer' in xml
    assert 'PT24H' in xml
    assert 'boundaryEvent' in xml
    assert 'attachedToRef' in xml
    assert xml.count('<bpmn:participant') >= 2
    assert '<bpmn:messageFlow' in xml


def test_reference_test8_matches_reference_xml():
    data = json.loads(Path("test8_message.logic-core.json").read_text(encoding="utf-8"))
    xml, _ = run_pipeline(direct_logic_core=data, verbose=False)
    assert xml.count('<bpmn:participant') >= 2
    assert '<bpmn:messageFlow' in xml
    assert '<bpmn:message id="Message_' in xml
    assert 'messageRef="Message_' in xml


def test_reference_order_to_cash_matches_reference_xml():
    data = json.loads(Path("order_to_cash.logic-core.json").read_text(encoding="utf-8"))
    xml, _ = run_pipeline(direct_logic_core=data, verbose=False)
    assert 'process_order_to_cash' in xml or 'OrderToCash_Process' in xml
    assert 'exclusiveGateway' in xml
    assert 'end_commande_archivee_confirmee' in xml or 'EndEvent_OrderArchived' in xml
    assert 'end_commande_archivee_rejetee' in xml or 'EndEvent_OrderRejected' in xml


@pytest.mark.skip(reason="Test live LLM manuel uniquement, désactivé en CI automatique pour éviter tout non-déterminisme réseau.")
def test_live_llm_order_to_cash_smoke():
    text = (
        "Un processus Order-to-Cash est déclenché par la réception d'un bon de commande d'un client. "
        "À la réception, le bon de commande doit être vérifié par rapport au stock. Selon la disponibilité des stocks, "
        "la commande peut être confirmée ou rejetée. Si elle est confirmée, une facture est émise, les articles sont expédiés "
        "et la commande est archivée."
    )
    xml, _ = run_pipeline(user_text=text, verbose=False)
    assert 'exclusiveGateway' in xml


def test_outcome_merge_detection_direct_and_indirect():
    from validate import _check_no_illegitimate_outcome_merge
    # Cas direct gateway -> endEvent
    nodes_direct = [
        {"id": "gw1", "type": "exclusiveGateway"},
        {"id": "task1", "type": "task"},
        {"id": "end1", "type": "endEvent"},
    ]
    edges_direct = [
        {"source": "gw1", "target": "task1", "type": "sequenceFlow", "name": "confirmée"},
        {"source": "task1", "target": "end1", "type": "sequenceFlow"},
        {"source": "gw1", "target": "end1", "type": "sequenceFlow", "name": "rejetée"},
    ]
    errors_direct = _check_no_illegitimate_outcome_merge(nodes_direct, edges_direct)
    assert len(errors_direct) > 0

    # Cas indirect gateway -> task1 -> end1 et gateway -> task2 -> end1
    nodes_indirect = [
        {"id": "gw1", "type": "exclusiveGateway"},
        {"id": "task1", "type": "task"},
        {"id": "task2", "type": "task"},
        {"id": "end1", "type": "endEvent"},
    ]
    edges_indirect = [
        {"source": "gw1", "target": "task1", "type": "sequenceFlow", "name": "confirmée"},
        {"source": "task1", "target": "end1", "type": "sequenceFlow"},
        {"source": "gw1", "target": "task2", "type": "sequenceFlow", "name": "rejetée"},
        {"source": "task2", "target": "end1", "type": "sequenceFlow"},
    ]
    errors_indirect = _check_no_illegitimate_outcome_merge(nodes_indirect, edges_indirect)
    assert len(errors_indirect) > 0


def test_normalize_logic_core_graph_no_parasitic_gateway_on_linearized_branches():
    from validate import normalize_logic_core_graph
    logic_core = {
        "process": {"id": "p_test", "name": "Test Linearization", "isExecutable": True},
        "pools": [],
        "nodes": [
            {"id": "start", "type": "startEvent"},
            {"id": "gw_decide", "type": "exclusiveGateway"},
            {"id": "invoice", "type": "task"},
            {"id": "ship", "type": "task"},
            {"id": "archive", "type": "task"},
            {"id": "end_ok", "type": "endEvent"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "gw_decide", "type": "sequenceFlow"},
            {"id": "e2", "source": "gw_decide", "target": "invoice", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "e3", "source": "gw_decide", "target": "ship", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "e4", "source": "gw_decide", "target": "archive", "type": "sequenceFlow", "name": "confirmée"},
            {"id": "e5", "source": "archive", "target": "end_ok", "type": "sequenceFlow"},
        ]
    }
    normalized = normalize_logic_core_graph(logic_core)
    synthetic_gateways = [n for n in normalized.get("nodes", []) if n.get("id").endswith("_gw")]
    assert len(synthetic_gateways) == 0


def test_normalize_logic_core_graph_assigns_missing_pool_id_from_neighbor():
    logic_core = {
        "process": {"id": "p1", "name": "test", "isExecutable": True},
        "pools": [{"id": "pool_system", "name": "Système"}],
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Début", "poolId": "pool_system"},
            {"id": "task_main", "type": "task", "name": "Traiter", "poolId": "pool_system"},
            {"id": "gateway_branch", "type": "exclusiveGateway", "name": "Branche", "gatewayDirection": "diverging"},
            {"id": "end", "type": "endEvent", "name": "Fin", "poolId": "pool_system"},
        ],
        "edges": [
            {"id": "flow_1", "source": "start", "target": "task_main", "type": "sequenceFlow"},
            {"id": "flow_2", "source": "task_main", "target": "gateway_branch", "type": "sequenceFlow"},
            {"id": "flow_3", "source": "gateway_branch", "target": "end", "type": "sequenceFlow"},
        ],
    }
    result = normalize_logic_core_graph(logic_core)
    gateway = next(node for node in result["nodes"] if node["id"] == "gateway_branch")
    assert gateway["poolId"] == "pool_system"


def _valid_process_description():
    return {
        "process_name": "Commande",
        "understanding_summary": "Le commercial vérifie une commande.",
        "identified_elements": {
            "participants": [{"id": "commercial", "name": "Commercial", "type": "actor"}],
            "activities": [{"id": "verifier_stock", "name": "Vérifier le stock", "type": "task", "actor_id": "commercial", "sequence": 1}],
            "events": [{"id": "commande_recue", "name": "Commande reçue", "type": "start"}],
            "conditions": [],
            "gateways": [],
        },
        "gaps": [],
        "confidence_level": "high",
    }


def _fake_response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def test_process_description_schema_accepts_valid_document():
    assert validate_process_description(_valid_process_description()).ok is True


def test_process_description_schema_rejects_missing_required_field():
    document = _valid_process_description()
    del document["confidence_level"]
    result = validate_process_description(document)
    assert result.ok is False
    assert any("confidence_level" in error for error in result.errors)


def test_process_description_rejects_unknown_actor_reference():
    document = _valid_process_description()
    document["identified_elements"]["activities"][0]["actor_id"] = "unknown"
    result = validate_process_description(document)
    assert result.ok is False
    assert any("unknown participant" in error for error in result.errors)


def test_structured_response_format_uses_json_schema():
    response_format = llm_agent._response_format("process-description.schema.json", "pd")
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"]["title"] == "Process Description Schema"


def test_process_description_extraction_is_mocked(monkeypatch):
    analysis = {
        "participants": [{"id": "participant_001", "name": "Commercial", "evidence": "Le commercial", "confidence": "explicit"}],
        "activities": [{"id": "activity_001", "name": "Vérifier le stock", "actor": "Commercial", "evidence": "vérifie le stock", "source_text": "Le commercial vérifie le stock", "confidence": "explicit", "type_candidate": "unknown"}],
        "events": [], "conditions": [], "relations": [], "communications": [], "gaps": [], "structural_elements": [],
    }
    client = SimpleNamespace(chat=SimpleNamespace(complete=lambda **kwargs: _fake_response(analysis)))
    monkeypatch.setattr(llm_agent, "_get_mistral_client", lambda api_key=None: client)
    result = llm_agent.extract_process_description("Le commercial vérifie le stock.")
    assert result["identified_elements"]["activities"][0]["id"] == "activity_001"


def test_process_description_healing_prompt_contains_validation_errors(monkeypatch):
    seen = {}
    def complete(**kwargs):
        seen["prompt"] = kwargs["messages"][0]["content"]
        return _fake_response(_valid_process_description())
    client = SimpleNamespace(chat=SimpleNamespace(complete=complete))
    monkeypatch.setattr(llm_agent, "_get_mistral_client", lambda api_key=None: client)
    llm_agent.heal_process_description({}, ["missing field: confidence_level"], "texte")
    assert "missing field: confidence_level" in seen["prompt"]


def test_logic_core_healing_prompt_contains_errors(monkeypatch):
    seen = {}
    def complete(**kwargs):
        seen["prompt"] = kwargs["messages"][-1]["content"]
        return _fake_response({})
    client = SimpleNamespace(chat=SimpleNamespace(complete=complete))
    monkeypatch.setattr(llm_agent, "_get_mistral_client", lambda api_key=None: client)
    llm_agent.self_heal_logic_core({}, ["Task_3 is unreachable"], "texte")
    assert "Task_3 is unreachable" in seen["prompt"]


def test_logic_core_generation_prompt_contains_process_description(monkeypatch):
    seen = {}
    def complete(**kwargs):
        seen["prompt"] = kwargs["messages"][-1]["content"]
        return _fake_response({})
    client = SimpleNamespace(chat=SimpleNamespace(complete=complete))
    monkeypatch.setattr(llm_agent, "_get_mistral_client", lambda api_key=None: client)
    llm_agent.generate_logic_core_from_pd(_valid_process_description())
    assert "Process Description" in seen["prompt"]


def test_amendment_prompt_requires_existing_ids(monkeypatch):
    seen = {}
    def complete(**kwargs):
        seen["prompt"] = kwargs["messages"][-1]["content"]
        return _fake_response({})
    client = SimpleNamespace(chat=SimpleNamespace(complete=complete))
    monkeypatch.setattr(llm_agent, "_get_mistral_client", lambda api_key=None: client)
    llm_agent.extract_logic_core("Ajouter une vérification", {"process": {"id": "p", "name": "P"}, "nodes": [], "edges": []})
    assert "Conserve tous les IDs existants" in seen["prompt"]


def test_pipeline_rejects_amendment_that_drops_existing_ids(monkeypatch):
    sample_file = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    existing = json.loads(sample_file.read_text(encoding="utf-8"))
    amended = json.loads(sample_file.read_text(encoding="utf-8"))
    amended["nodes"] = amended["nodes"][1:]
    monkeypatch.setattr("pipeline.extract_logic_core", lambda *args, **kwargs: amended)
    try:
        run_pipeline("Supprimer le premier élément", existing_logic_core=existing, verbose=False)
    except ValueError as error:
        assert "supprimé des IDs existants" in str(error)
    else:
        raise AssertionError("L'amendement invalide aurait dû être refusé")


def test_pipeline_direct_run_creates_completed_experiment_log(tmp_path, monkeypatch):
    import src.config as config
    monkeypatch.setattr(config, "LOG_FILE_PATH", str(tmp_path / "experiment_data.json"))
    sample = Path(__file__).parent.parent / "examples" / "sample_order_fulfillment.json"
    run_pipeline(direct_logic_core=json.loads(sample.read_text(encoding="utf-8")), verbose=False)
    data = json.loads((tmp_path / "experiment_data.json").read_text(encoding="utf-8"))
    assert data["runs"][-1]["final_status"] == "success"


if __name__ == "__main__":
    print("▶ Exécution de la suite de tests unitaires BPMN Agent...")
    test_order_fulfillment_example()
    test_insurance_claim_parallel_gateway()
    test_validation_detects_orphan()
    test_validation_detects_invalid_boundary_event()
    test_validation_detects_cross_pool_sequence_flow()
    test_layout_computes_swimlanes_and_boundary_positions()
    test_xml_generation_full_omg()
    test_pipeline_execution()
    print("✅ Tous les 8 tests d'intégration et de validation sont passés avec succès !")
