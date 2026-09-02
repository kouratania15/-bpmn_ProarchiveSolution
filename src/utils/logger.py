"""
Systeme de logging structure pour experiment_data.json et experiment.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.models.action import ActionType
from src import config

# Fichier principal de tracabilite chronologique (nouveau format)
EXPERIMENT_FILE = Path("experiment.json")


# ------------------------------------------------------------------
# 1. Synthese metier (affichee en console + retournee en dict)
# ------------------------------------------------------------------

def print_business_summary(
    logic_core: dict[str, Any],
    step: str = "Generation initiale",
    attempt: int = 1,
) -> dict[str, Any]:
    """
    Affiche une synthese claire du processus compris par le LLM ET retourne
    un dictionnaire structure contenant exactement ces metriques.
    """
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    if not logic_core or not isinstance(logic_core, dict):
        print(f"\nX [{timestamp}] [{step.upper()}] Aucun processus valide a afficher.")
        return {}

    process_info = logic_core.get("process", {})
    process_name = process_info.get("name") if isinstance(process_info, dict) else None
    if not process_name:
        process_name = logic_core.get("process_name", "Processus non nomme")

    nodes = logic_core.get("nodes", [])
    edges = logic_core.get("edges", [])
    pools = logic_core.get("pools", [])

    pools_count = len(pools)
    lanes_count = sum(len(p.get("lanes", [])) for p in pools if isinstance(p, dict))
    tasks_count = sum(
        1 for n in nodes
        if isinstance(n, dict) and n.get("type") in (
            "task", "userTask", "serviceTask", "scriptTask",
            "manualTask", "sendTask", "receiveTask", "businessRuleTask",
        )
    )
    gateways_count = sum(
        1 for n in nodes
        if isinstance(n, dict) and "gateway" in (n.get("type") or "").lower()
    )
    events_count = sum(
        1 for n in nodes
        if isinstance(n, dict) and "event" in (n.get("type") or "").lower()
    )
    message_flows = [e for e in edges if isinstance(e, dict) and e.get("type") == "messageFlow"]

    summary_data: dict[str, Any] = {
        "step": step,
        "attempt": attempt,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "process_name": process_name,
        "metrics": {
            "pools_count": pools_count,
            "lanes_count": lanes_count,
            "nodes_count": len(nodes),
            "edges_count": len(edges),
            "tasks_count": tasks_count,
            "gateways_count": gateways_count,
            "events_count": events_count,
        },
        "structure_details": {
            "pools": [p.get("name", p.get("id")) for p in pools if isinstance(p, dict)],
            "nodes_summary": [
                f"[{n.get('type')}] {n.get('name', n.get('id', '?'))}"
                for n in nodes
                if isinstance(n, dict)
            ],
        },
    }

    is_healing = "heal" in step.lower() or attempt > 1
    icon = ">>>" if is_healing else "***"

    print("\n" + "=" * 70)
    print(f" {icon} SYNTHESE DE COMPREHENSION | Etape : {step} (Tentative #{attempt})")
    print(f" Heure : {timestamp}")
    print(f" Processus : {process_name}")
    print("=" * 70)

    if pools:
        print("\n Acteurs / Organisations detectes :")
        for pool in pools:
            if isinstance(pool, dict):
                print(f"   * Pool : {pool.get('name', pool.get('id'))}")
    else:
        print("\n Acteurs : Processus mono-partenaire (Un seul flux principal)")

    print("\n Elements du processus deduits du texte :")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type", "task")
        name = node.get("name", "Sans nom")
        if node_type in ("startEvent", "endEvent"):
            print(f"   * [EVENEMENT] {name} ({node_type})")
        elif "gateway" in node_type.lower():
            print(f"   * [DECISION] {name} ({node_type})")
        else:
            print(f"   * [ACTIVITE] {name} ({node_type})")

    if message_flows:
        print("\n Communications inter-organisations (Message Flows) :")
        for flow in message_flows:
            src = flow.get("source")
            tgt = flow.get("target")
            lbl = flow.get("name") or flow.get("condition") or "Echange"
            print(f"   * '{src}' -> '{tgt}' (Message : {lbl})")

    print(f"\n Statistiques : {len(nodes)} noeuds, {len(edges)} liens, "
          f"{pools_count} pools, {gateways_count} passerelles.")
    print("=" * 70 + "\n")

    return summary_data


# ------------------------------------------------------------------
# 2. Tracabilite chronologique -> experiment.json
# ------------------------------------------------------------------

def append_execution_trace(run_id: str, trace_entry: dict[str, Any]) -> None:
    """
    Ajoute une etape dans experiment.json sans supprimer l'historique.
    Structure: { run_id, business_summary, execution_trace: [...] }
    """
    data: dict[str, Any] = {
        "run_id": run_id,
        "business_summary": {},
        "execution_trace": [],
    }

    if EXPERIMENT_FILE.exists():
        try:
            content = json.loads(EXPERIMENT_FILE.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                data = content
        except Exception:
            pass

    if not isinstance(data.get("execution_trace"), list):
        data["execution_trace"] = []

    if "timestamp" not in trace_entry:
        trace_entry["timestamp"] = datetime.now().isoformat()

    data["execution_trace"].append(trace_entry)
    EXPERIMENT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_final_experiment_summary(
    run_id: str,
    summary_data: dict[str, Any],
    status: str = "SUCCESS",
) -> None:
    """Met a jour la section business_summary principale dans experiment.json."""
    data: dict[str, Any] = {
        "run_id": run_id,
        "business_summary": {},
        "execution_trace": [],
    }

    if EXPERIMENT_FILE.exists():
        try:
            content = json.loads(EXPERIMENT_FILE.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                data = content
        except Exception:
            pass

    enriched = dict(summary_data)
    enriched["final_status"] = status
    data["run_id"] = run_id
    data["business_summary"] = enriched

    EXPERIMENT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# 3. API historique (experiment_data.json) -- conservee intacte
# ------------------------------------------------------------------

def generate_run_id() -> str:
    """Genere un identifiant unique pour une execution."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_suffix = str(uuid4())[:8]
    return f"run_{timestamp}_{unique_suffix}"


def _ensure_log_file_exists() -> None:
    """Cree le fichier de log s'il n'existe pas."""
    log_path = Path(config.LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(json.dumps({"runs": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_experiment_data() -> dict[str, Any]:
    """Charge les donnees d'experience existantes."""
    _ensure_log_file_exists()
    try:
        content = Path(config.LOG_FILE_PATH).read_text(encoding="utf-8", errors="replace")
        return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"runs": []}


def _save_experiment_data(data: dict[str, Any]) -> None:
    """Sauvegarde les donnees d'experience."""
    _ensure_log_file_exists()
    Path(config.LOG_FILE_PATH).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def log_event(
    run_id: str,
    action: ActionType,
    step: str,
    status: str,
    input_prompt: str | None = None,
    output_response: str | None = None,
    errors: list[str] | None = None,
    attempt: int = 1,
    model: str | None = None,
    temperature: float | None = None,
    output: Any | None = None,
) -> None:
    """Enregistre un evenement structure dans experiment_data.json."""
    exp_data = _load_experiment_data()

    run = None
    for r in exp_data["runs"]:
        if r["run_id"] == run_id:
            run = r
            break

    if run is None:
        run = {
            "run_id": run_id,
            "timestamp": datetime.now().isoformat() + "Z",
            "events": [],
            "final_status": "in_progress",
        }
        exp_data["runs"].append(run)

    event: dict[str, Any] = {
        "event_id": f"evt_{len(run['events']) + 1:03d}",
        "timestamp": datetime.now().isoformat() + "Z",
        "action": action.value,
        "step": step,
        "status": status,
        "attempt": attempt,
        "agent": "bpmn_agent",
    }

    if model is not None:
        event["model"] = model
    if temperature is not None:
        event["temperature"] = temperature
    if input_prompt is not None:
        event["input_prompt"] = input_prompt
    if output_response is not None:
        event["output_response"] = output_response
        if output is None:
            try:
                output = json.loads(output_response)
            except (TypeError, json.JSONDecodeError):
                output = {"value": output_response}
    if output is not None:
        event["output"] = output
    if errors:
        event["errors"] = errors

    run["events"].append(event)
    _save_experiment_data(exp_data)


def finalize_run(run_id: str, final_status: str) -> None:
    """
    Finalise une execution :
    - Met a jour experiment_data.json (ancien format)
    - Ajoute PIPELINE_FINALIZE dans experiment.json (nouveau format)
    """
    exp_data = _load_experiment_data()
    found = False
    for run in exp_data["runs"]:
        if run["run_id"] == run_id:
            run["final_status"] = final_status
            found = True
            break
    if not found:
        exp_data["runs"].append({
            "run_id": run_id,
            "timestamp": datetime.now().isoformat() + "Z",
            "events": [],
            "final_status": final_status,
        })
    _save_experiment_data(exp_data)

    append_execution_trace(run_id, {
        "timestamp": datetime.now().isoformat(),
        "component": "pipeline",
        "action": "PIPELINE_FINALIZE",
        "status": final_status.upper(),
    })
