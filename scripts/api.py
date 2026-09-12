"""
API web FastAPI exposant le pipeline BPMN existant (génération + amendement
versionné) et servant le frontend statique.

Ne réimplémente aucune logique de génération/validation/self-healing : appelle
uniquement pipeline.run_pipeline() et db.py, exactement comme le fait déjà la
CLI (scripts/pipeline.py:main). response_formatter.py construit le texte du
chat à partir des données déjà structurées retournées par ces appels — jamais
via un appel LLM supplémentaire.

Lancer avec :  uvicorn scripts.api:app --reload   (depuis la racine du projet)
"""
from __future__ import annotations

import logging
import sys



from pathlib import Path
from typing import Any

# Le pipeline (print_business_summary, print_trace, etc.) écrit sur
# stdout/stderr du texte pouvant contenir des caractères hors codepage Windows
# par défaut (ex: '≤', '€') — pipeline.py:main() s'en protège déjà pour la
# CLI, mais uvicorn ne passe jamais par main() : sans ce même reconfigure ici,
# une simple condition "montant ≤ 10000" générée par le LLM fait planter la
# requête entière avec un UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bpmn_agent.api")

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = Path(__file__).parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import response_formatter
from pipeline import PipelineValidationError, run_pipeline
from validate import validate_logic_core

app = FastAPI(title="BPMN Agent API")


class CreateProcessRequest(BaseModel):
    text: str


class AmendProcessRequest(BaseModel):
    instruction: str


def _build_response(
    *,
    process_id: str | None,
    process_name: str | None,
    version_number: int | None,
    ok: bool,
    logic_core: dict[str, Any] | None,
    bpmn_xml: str | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Assemble la réponse JSON commune aux endpoints de création/amendement,
    en déléguant tout le texte affichable à response_formatter (déterministe,
    sans LLM)."""
    formatted = response_formatter.format_result(ok=ok, logic_core=logic_core, errors=errors, warnings=warnings)
    return {
        "process_id": process_id,
        "process_name": process_name,
        "version_number": version_number,
        "status": "valid" if ok else "invalid",
        "message": formatted["message"],
        "summary": formatted["summary"],
        "errors_technical": formatted["errors_technical"],
        "warnings": formatted["warnings"],
        "logic_core": logic_core,
        "bpmn_xml": bpmn_xml,
    }


@app.get("/processes")
def list_processes_endpoint() -> list[dict[str, Any]]:
    """Liste tous les process (id, name, created_at) pour la sidebar."""
    return db.list_processes()


@app.post("/processes")
def create_process_endpoint(payload: CreateProcessRequest) -> dict[str, Any]:
    """Génération neuve : appelle run_pipeline() sans Logic-Core existant.
    Le nom du process n'est jamais saisi par l'utilisateur : run_pipeline()
    (via generate_logic_core_from_pd, déjà appelé en interne) produit toujours
    un logic_core["process"]["name"] métier significatif — on le réutilise tel
    quel, sans nouvel appel LLM ni heuristique côté client.
    Un échec de validation BPMN est une réponse 200 normale (status=invalid,
    voir plan) ; rien n'est persisté dans ce cas (comportement inchangé de
    db.create_process, qui n'est jamais appelé sur échec)."""
    try:
        result = run_pipeline(user_text=payload.text, verbose=False)
        process_name = result.logic_core.get("process", {}).get("name") or "Processus sans nom"
        process_id = db.create_process(process_name, payload.text, result.logic_core, result.xml)
        return _build_response(
            process_id=process_id, process_name=process_name, version_number=1,
            ok=True, logic_core=result.logic_core, bpmn_xml=result.xml, errors=[], warnings=result.warnings,
        )
    except PipelineValidationError as exc:
        return _build_response(
            process_id=None, process_name=None, version_number=None,
            ok=False, logic_core=None, bpmn_xml=None, errors=exc.errors, warnings=exc.warnings,
        )
    except Exception as exc:
        logger.exception("POST /processes failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/processes/{process_id}/amend")
def amend_process_endpoint(process_id: str, payload: AmendProcessRequest) -> dict[str, Any]:
    """Amendement versionné : charge la dernière version en base, appelle
    run_pipeline(existing_logic_core=...) — exactement le mode déjà utilisé
    par la CLI (--process-id). Sur échec, aucune nouvelle version n'est
    ajoutée ; le process reste sur sa dernière version valide."""
    latest = db.get_latest_version(process_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"Process introuvable : {process_id}")

    try:
        result = run_pipeline(
            user_text=payload.instruction,
            existing_logic_core=latest["logic_core_json"],
            verbose=False,
        )
        db.add_version(process_id, payload.instruction, result.logic_core, result.xml, latest["version_id"])
    except PipelineValidationError as exc:
        return _build_response(
            process_id=process_id, process_name=None, version_number=latest["version_number"],
            ok=False, logic_core=None, bpmn_xml=None, errors=exc.errors, warnings=exc.warnings,
        )
    except Exception as exc:
        logger.exception("POST /processes/%s/amend failed", process_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    new_latest = db.get_latest_version(process_id)
    return _build_response(
        process_id=process_id, process_name=None, version_number=new_latest["version_number"],
        ok=True, logic_core=result.logic_core, bpmn_xml=result.xml, errors=[], warnings=result.warnings,
    )


@app.get("/processes/{process_id}")
def get_process_endpoint(process_id: str) -> dict[str, Any]:
    """État courant (dernière version) + historique de conversation reconstruit.

    Seules des versions déjà validées sont jamais persistées (voir
    pipeline.py), donc chaque version historique est nécessairement ok=True :
    on rappelle validate_logic_core() sur le logic_core déjà stocké
    (déterministe, aucun appel Mistral) pour récupérer ses warnings et
    reconstruire le message de chat de cette version, sans avoir à stocker
    les warnings en base."""
    latest = db.get_latest_version(process_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"Process introuvable : {process_id}")

    conversation = []
    for meta in db.get_version_history(process_id):
        version = db.get_version(process_id, meta["version_number"])
        val_res = validate_logic_core(version["logic_core_json"])
        formatted = response_formatter.format_result(
            ok=val_res.ok,
            logic_core=version["logic_core_json"] if val_res.ok else None,
            errors=val_res.errors,
            warnings=val_res.warnings,
        )
        conversation.append({
            "version_number": meta["version_number"],
            "instruction_text": meta["instruction_text"],
            "created_at": str(meta["created_at"]),
            "message": formatted["message"],
        })

    return {
        "process_id": process_id,
        "version_number": latest["version_number"],
        "status": "valid",
        "logic_core": latest["logic_core_json"],
        "bpmn_xml": latest["bpmn_xml"],
        "conversation": conversation,
    }


@app.delete("/processes/{process_id}", status_code=204)
def delete_process_endpoint(process_id: str) -> None:
    """Supprime un process et tout son historique de versions (ON DELETE CASCADE)."""
    deleted = db.delete_process(process_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Process introuvable : {process_id}")


# Sert le frontend statique — enregistré APRÈS les routes API ci-dessus pour
# que le mount catch-all sur "/" ne les intercepte jamais (Starlette matche
# les routes dans leur ordre d'enregistrement).
STATIC_DIR = PROJECT_ROOT / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
