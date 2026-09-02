"""Appels Mistral pour comprendre un processus et produire son Logic-Core."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.config import DEFAULT_MODEL, TEMPERATURE
from src.models.action import ActionType
from src.utils.logger import log_event

try:
	import jsonschema
except ImportError:
	jsonschema = None

load_dotenv()

ROOT = Path(__file__).parent.parent


def _load_json_schema(filename: str) -> dict[str, Any]:
	return json.loads((ROOT / "schema" / filename).read_text(encoding="utf-8"))


def _skill_prompt() -> str:
	path = ROOT / "SKILL.md"
	return path.read_text(encoding="utf-8") if path.exists() else "Expert BPMN 2.0."


def _get_mistral_client(api_key: str | None = None) -> Any:
	key = api_key or os.getenv("MISTRAL_API_KEY")
	if not key:
		raise ValueError("La variable d'environnement MISTRAL_API_KEY est manquante.")
	try:
		from mistralai.client import Mistral
	except ImportError as exc:
		raise ImportError("Installez la dépendance 'mistralai'.") from exc
	return Mistral(api_key=key)


def _response_format(schema_filename: str, name: str) -> dict[str, Any]:
	schema = _load_json_schema(schema_filename)
	return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}


def _complete(client: Any, model: str, messages: list[dict[str, str]], schema: str, name: str) -> str:
	response = client.chat.complete(
		model=model,
		messages=messages,
		response_format=_response_format(schema, name),
		temperature=TEMPERATURE,
	)
	content = response.choices[0].message.content
	if isinstance(content, list):
		content = "".join(str(item) for item in content)
	if not content:
		raise ValueError("Réponse vide reçue de Mistral AI.")
	return str(content).strip().removeprefix("```json").removesuffix("```").strip()


def _log_llm(run_id: str | None, action: ActionType, step: str, status: str,
			 prompt: str, response: str | None = None, errors: list[str] | None = None,
			 attempt: int = 1, model: str = DEFAULT_MODEL) -> None:
	if run_id:
		log_event(run_id, action, step, status, prompt, response, errors, attempt,
				  model=model, temperature=TEMPERATURE)


def _trace_prompt(messages: list[dict[str, str]]) -> str:
	"""Sérialise tous les messages envoyés au fournisseur LLM pour l'expérience."""
	return "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages)


def _is_mistral_model_issue(exc: Exception) -> bool:
	"""Détecte les erreurs de compte / modèle / plan / serveur (5xx) / réseau qui bloquent Mistral."""
	status_code = getattr(exc, "status_code", None) or getattr(exc, "raw_status_code", None)
	if status_code is not None and isinstance(status_code, int):
		if status_code in (401, 402, 403, 429) or (500 <= status_code <= 599):
			return True

	message = str(exc).lower()
	patterns = (
		"not available in your subscription tier",
		"subscription",
		"unauthorized",
		"forbidden",
		"access denied",
		"401", "402", "403", "429", "500", "502", "503", "504",
		"service unavailable", "temporarily unavailable", "internal_server_error",
		"too many requests", "overloaded",
		"model",
		"api key",
		"connecttimeout",
		"connection",
		"winerror 10060",
		"timeout",
		"sdkerror",
	)
	return any(pattern in message for pattern in patterns)


def _fallback_structured_analysis(user_text: str) -> dict[str, Any]:
	"""Fallback local déterministe par regex/heuristiques simples lorsqu'aucun LLM n'est disponible.

	NOTE ARCHITECTURALE : Ce fallback est intrinsèquement dégradé par rapport à l'extraction LLM.
	Conformément à la Règle 2 de SKILL.md (Fidélité au texte), il est STRICTEMENT CONSERVATEUR :
	il ne doit jamais inventer d'activités, de rôles ou de sous-processus non présents.
	"""
	text = (user_text or "").strip().lower()

	# Détecter les participants distincts (multi-pool)
	participant_keywords = {
		"customer": "Customer", "client": "Client", "employee": "Employee",
		"manager": "Manager", "system": "System", "bank": "Bank", "banque": "Banque",
		"company": "Company", "entreprise": "Entreprise", "supplier": "Supplier",
		"fournisseur": "Fournisseur", "user": "User", "utilisateur": "Utilisateur",
		"service": "Service", "provider": "Provider"
	}

	detected_participants = set()
	for keyword, name in participant_keywords.items():
		if keyword in text:
			detected_participants.add(name)

	participants: list[dict[str, Any]] = []
	if len(detected_participants) >= 2:
		for idx, pname in enumerate(sorted(detected_participants), 1):
			participants.append({
				"id": f"participant_{idx:03d}",
				"name": pname,
				"evidence": f"Mentionné dans le texte: {pname.lower()}",
				"source_text": text,
				"confidence": "explicit",
			})
	else:
		participants = [{
			"id": "participant_001",
			"name": "Acteur métier",
			"evidence": text[:200] or "Processus métier",
			"source_text": text,
			"confidence": "explicit",
		}]

	segments = [part.strip() for part in re.split(r"[.;!?]\s+", (user_text or "").strip()) if part.strip()]
	if not segments:
		segments = [text]

	activities: list[dict[str, Any]] = []
	conditions: list[dict[str, Any]] = []
	communications: list[dict[str, Any]] = []

	def _match_activity_for_keywords(keywords: list[str]) -> str | None:
	    for key in keywords:
	        for activity in activities:
	            name = str(activity.get("name", "")).lower()
	            if key in name:
	                return activity["id"]
	    return None



	for index, segment in enumerate(segments[:8], start=1):
		clean = re.sub(r"^(le|la|un|une|il faut|doit|est|on|nous|the|a|an)\s+", "", segment.strip(), flags=re.I)
		clean = clean.strip(" ,;:.-")
		if not clean:
			continue
		act_id = f"activity_{index:03d}"
		activities.append({
			"id": act_id,
			"name": clean[:80],
			"evidence": segment,
			"source_text": segment,
			"confidence": "explicit",
			"type_candidate": "unknown",
		})

		# Détection basique des conditions ("if", "si", "selon", "otherwise", "sinon")
		if re.search(r"\b(if|si|selon|depending|otherwise|sinon)\b", segment, re.I):
			condition_id = f"condition_{len(conditions)+1:03d}"
			positive_text = re.search(r"\b(?:if|si|selon|depending)\b\s+(.+?)(?:,|:|\s+then|\s+\-|\.|$)", segment, re.I)
			positive = (positive_text.group(1).strip() if positive_text else clean[:60])
			negative = "otherwise / not " + positive
			positive_outcome = _match_activity_for_keywords(["grant", "approve", "confirm", "valid", "available", "accept", "ship", "process"])
			negative_outcome = _match_activity_for_keywords(["reject", "invalid", "unavailable", "deny", "refuse", "cancel", "not approved"])
			if positive_outcome is None:
				positive_outcome = act_id
			if negative_outcome is None:
				negative_outcome = act_id
			conditions.append({
				"id": condition_id,
				"condition_text": positive[:60],
				"branches": [
					{"condition": positive[:60], "outcome": positive_outcome},
					{"condition": negative[:60], "outcome": negative_outcome},
				],
			})

		# Détection basique des communications (sends / receives)
		if re.search(r"\b(sends?|envoie|reço|receiv|transmet|notification)\b", segment, re.I) and len(participants) >= 2:
			communications.append({
				"id": f"comm_{len(communications)+1:03d}",
				"sender": participants[0]["id"],
				"receiver": participants[1]["id"],
				"message": clean[:40]
			})

	if not activities:
		activities = [{
			"id": "activity_001",
			"name": "Traitement de la demande",
			"evidence": text,
			"source_text": text,
			"confidence": "explicit",
			"type_candidate": "unknown",
		}]

	start_event = {
		"id": "event_001",
		"name": "Début du processus",
		"event_type": "start",
		"evidence": text[:200] or "Déclenchement du processus",
		"source_text": text,
		"confidence": "explicit",
	}
	end_event = {
		"id": "event_002",
		"name": "Fin du processus",
		"event_type": "end",
		"evidence": "Fin du processus métier",
		"source_text": text,
		"confidence": "explicit",
	}
	relations: list[dict[str, Any]] = []
	prev_node = start_event["id"]
	for idx, activity in enumerate(activities, start=1):
		relations.append({
			"id": f"relation_{idx:03d}",
			"source": prev_node,
			"target": activity["id"],
			"type": "sequence",
		})
		prev_node = activity["id"]
	relations.append({
		"id": f"relation_{len(activities) + 1:03d}",
		"source": prev_node,
		"target": end_event["id"],
		"type": "sequence",
	})
	return {
		"participants": participants,
		"activities": activities,
		"events": [start_event, end_event],
		"conditions": conditions,
		"relations": relations,
		"communications": communications,
		"gaps": [],
		"structural_elements": [],
	}


def extract_structured_analysis(user_text: str, run_id: str | None = None,
								api_key: str | None = None,
								model: str = DEFAULT_MODEL) -> dict[str, Any]:
	"""Extrait des faits traçables avant toute normalisation BPMN."""
	prompt = (
		"Extrait uniquement les faits justifiés par le texte. Produis une analyse structurée "
		"conforme au schema structured-analysis.schema.json. Chaque élément doit contenir une "
		"preuve textuelle; garde les conditions sémantiques et utilise unknown si le type est inconnu. "
		"N'invente aucun système, variable, protocole ou détail métier. Réponds uniquement en JSON.\n\n"
		f"Texte source:\n{user_text}"
	)
	messages = [{"role": "user", "content": prompt}]
	try:
		raw = _complete(_get_mistral_client(api_key), model, messages,
						"structured-analysis.schema.json", "structured_analysis")
		result = json.loads(raw)
		if jsonschema is not None:
			try:
				jsonschema.Draft202012Validator(_load_json_schema("structured-analysis.schema.json")).validate(result)
			except jsonschema.ValidationError as exc:
				_log_llm(run_id, ActionType.DEBUG, "analysis", "warning", _trace_prompt(messages), errors=[exc.message], model=model)
				# Le LLM génère souvent des champs de métadonnées additionnels (nom, type, libellé, ...)
				# sans que cela invalide l'analyse métier. On ne plant pas sur ce léger écart schema ;
				# la couche de self-healing aura la charge de corriger la structure BPMN plus tard.
				return result
		_log_llm(run_id, ActionType.ANALYSIS, "analysis", "success", _trace_prompt(messages), raw, model=model)
		return result
	except Exception as exc:
		if _is_mistral_model_issue(exc):
			fallback = _fallback_structured_analysis(user_text)
			_log_llm(run_id, ActionType.DEBUG, "analysis", "fallback", _trace_prompt(messages),
				 errors=[str(exc)], model=model)
			return fallback
		_log_llm(run_id, ActionType.DEBUG, "analysis", "failed", _trace_prompt(messages), errors=[str(exc)], model=model)
		if isinstance(exc, (json.JSONDecodeError, ValueError)):
			raise ValueError(f"Analyse structurée invalide : {exc}") from exc
		raise


def build_process_description(analysis: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
	"""Construit un Process Description sans réinterpréter le texte source."""
	participants = []
	for item in analysis.get("participants", []):
		participants.append({"id": item.get("id"), "name": item.get("name"), "type": "actor",
			"description": item.get("evidence", ""), "sourceAnalysisId": item.get("id"),
			"evidence": item.get("evidence", ""), "confidence": item.get("confidence", "medium")})
	participant_ids = {item.get("name"): item.get("id") for item in analysis.get("participants", []) if item.get("name") and item.get("id")}
	activities = []
	for index, item in enumerate(analysis.get("activities", []), 1):
		activity = {"id": item.get("id"), "name": item.get("name"), "type": "task",
			"sequence": index, "sourceAnalysisId": item.get("id"), "evidence": item.get("evidence", ""),
			"source_text": item.get("source_text", item.get("evidence", "")), "confidence": item.get("confidence", "medium")}
		if item.get("actor") in participant_ids:
			activity["actor_id"] = participant_ids[item["actor"]]
		activities.append(activity)
	events = [{"id": item.get("id"), "name": item.get("name"), "type": item.get("event_type", "intermediate"),
		"trigger": item.get("trigger", ""), "sourceAnalysisId": item.get("id"),
		"evidence": item.get("evidence", ""), "confidence": item.get("confidence", "medium")}
		for item in analysis.get("events", [])]
	conditions = [{"id": item.get("id"), "condition_text": item.get("condition"),
		"branches": [{"condition": branch.get("label"), "outcome": branch.get("meaning")}
			for branch in item.get("branches", [])], "sourceAnalysisId": item.get("id"),
		"evidence": item.get("evidence", "")} for item in analysis.get("conditions", [])]
	identified = {"participants": participants, "activities": activities, "events": events,
		"conditions": conditions, "gateways": [], "relations": analysis.get("relations", []),
		"sequence_flows": []}
	result = {"process_name": "Processus analysé", "understanding_summary": "Analyse structurée des faits source.",
		"identified_elements": identified, "gaps": [{"gap_id": gap["id"], "description": gap["description"],
			"severity": gap["severity"]} for gap in analysis.get("gaps", [])], "confidence_level": "medium",
		"source_text_summary": "Construit depuis l'analyse structurée."}
	if run_id:
		_log_llm(run_id, ActionType.GENERATION, "process_description_generation", "success",
				 json.dumps(analysis, ensure_ascii=False), json.dumps(result, ensure_ascii=False))
	return result


def extract_process_description(user_text: str, run_id: str | None = None,
								api_key: str | None = None,
								model: str = DEFAULT_MODEL) -> dict[str, Any]:
	"""Façade historique : l'analyse structurée est toujours l'étape source."""
	analysis = extract_structured_analysis(user_text, run_id=run_id, api_key=api_key, model=model)
	return build_process_description(analysis, run_id=run_id)


def build_logic_core_from_process_description(process_desc: dict[str, Any]) -> dict[str, Any]:
	"""Construit un Logic-Core BPMN cohérent à partir d'une Process Description.

	Le but est de convertir les relations sémantiques (after/if/when) en sequenceFlow
	BPMN de manière déterministe, sans dépendre du seul LLM pour la topologie du graphe.
	"""
	if not isinstance(process_desc, dict):
		raise ValueError("Process Description invalide : attendu un objet JSON.")

	identified = process_desc.get("identified_elements", {})
	if not isinstance(identified, dict):
		raise ValueError("identified_elements est absent ou invalide.")

	process_name = process_desc.get("process_name", "Processus BPMN")
	import unicodedata
	clean_name = unicodedata.normalize("NFKD", str(process_name)).encode("ascii", "ignore").decode("ascii")
	process_id = "Process_" + ''.join(ch if ch.isalnum() else '_' for ch in clean_name).strip('_') or "Process_BPMN"
	if not process_id[0].isalpha():
		process_id = "Process_" + process_id
	process_id = process_id[:80]

	nodes: list[dict[str, Any]] = []
	node_map: dict[str, dict[str, Any]] = {}
	edges: list[dict[str, Any]] = []

	def _ensure_node(node_id: str | None, name: str, node_type: str, **extra: Any) -> str | None:
		if not node_id:
			return None
		key = str(node_id)
		if key not in node_map:
			node_map[key] = {"id": key, "type": node_type, "name": name, **extra}
			nodes.append(node_map[key])
		return key

	def _ensure_edge(source: str | None, target: str | None, *, name: str | None = None,
					condition: str | None = None, edge_type: str = "sequenceFlow") -> None:
		if not source or not target:
			return
		for existing in edges:
			if existing.get("source") == source and existing.get("target") == target and existing.get("type") == edge_type:
				if name is not None and not existing.get("name"):
					existing["name"] = str(name)
				if condition is not None and not existing.get("condition"):
					existing["condition"] = str(condition)
				return
		edge: dict[str, Any] = {"id": f"flow_{len(edges) + 1}", "source": source, "target": target, "type": edge_type}
		if name is not None:
			edge["name"] = name
		if condition is not None:
			edge["condition"] = condition
		edges.append(edge)

	for event in identified.get("events", []):
		event_id = event.get("id")
		event_type = event.get("type")
		if event_type == "start":
			node_type = "startEvent"
		elif event_type == "end":
			node_type = "endEvent"
		else:
			node_type = "intermediateCatchEvent"
		_ensure_node(event_id, event.get("name", str(event_id)), node_type)

	for activity in identified.get("activities", []):
		activity_id = activity.get("id")
		legacy_type = activity.get("type")
		node_type = {
			"task": "task",
			"manual_task": "manualTask",
			"automated_task": "serviceTask",
			"subprocess": "subProcess",
		}.get(legacy_type, "task")
		_ensure_node(activity_id, activity.get("name", str(activity_id)), node_type)

	for gate in identified.get("gateways", []):
		gate_id = gate.get("id")
		gate_type = gate.get("type")
		node_type = {"exclusive": "exclusiveGateway", "parallel": "parallelGateway", "inclusive": "inclusiveGateway"}.get(gate_type, "exclusiveGateway")
		_ensure_node(gate_id, gate.get("description") or str(gate_id), node_type, gatewayDirection="diverging")

	for condition in identified.get("conditions", []):
		cond_id = condition.get("id")
		cond_name = condition.get("condition_text") or str(cond_id)
		_ensure_node(cond_id, cond_name, "exclusiveGateway", gatewayDirection="diverging")
		for branch in condition.get("branches", []):
			outcome = branch.get("outcome")
			if outcome:
				_ensure_node(str(outcome), str(outcome), "task")

	for relation in identified.get("relations", []):
		source = relation.get("source")
		target = relation.get("target")
		relation_type = relation.get("relation") or relation.get("type") or "after"
		if relation_type in {"after", "sequence", "next"}:
			_ensure_edge(source, target)
		elif relation_type in {"if", "when", "condition", "decision"}:
			_ensure_edge(source, target)
		else:
			_ensure_edge(source, target)

	for flow in identified.get("sequence_flows", []):
		_ensure_edge(flow.get("source"), flow.get("target"))

	for condition in identified.get("conditions", []):
		cond_id = condition.get("id")
		if not cond_id:
			continue
		for branch in condition.get("branches", []):
			outcome = branch.get("outcome")
			if outcome:
				branch_condition = branch.get("condition") or condition.get("condition_text") or outcome
				_ensure_edge(cond_id, str(outcome), name=str(branch_condition), condition=str(branch_condition))

	if not nodes:
		raise ValueError("Le Process Description ne contient aucun élément exploitable pour construire le Logic-Core.")

	start_nodes = [node["id"] for node in nodes if node["type"] == "startEvent"]
	end_nodes = [node["id"] for node in nodes if node["type"] == "endEvent"]
	business_nodes = [node["id"] for node in nodes if node["type"] not in {"startEvent", "endEvent"}]

	if start_nodes:
		reachable_from_start: set[str] = set()
		stack = list(start_nodes)
		while stack:
			cur = stack.pop()
			if cur in reachable_from_start:
				continue
			reachable_from_start.add(cur)
			for edge in edges:
				if edge.get("source") == cur and isinstance(edge.get("target"), str):
					stack.append(edge["target"])
		for business in business_nodes:
			if business not in reachable_from_start and not any(edge.get("target") == business for edge in edges):
				_ensure_edge(start_nodes[0], business)
				break

	if end_nodes:
		reachable_to_end: set[str] = set()
		stack = list(end_nodes)
		while stack:
			cur = stack.pop()
			if cur in reachable_to_end:
				continue
			reachable_to_end.add(cur)
			for edge in edges:
				if edge.get("target") == cur and isinstance(edge.get("source"), str):
					stack.append(edge["source"])
		for business in reversed(business_nodes):
			if business not in reachable_to_end and not any(edge.get("source") == business for edge in edges):
				_ensure_edge(business, end_nodes[0])
				break

	return {"process": {"id": process_id, "name": process_name, "isExecutable": True}, "nodes": nodes, "edges": edges}


def _repair_process_description(faulty_pd: dict[str, Any]) -> dict[str, Any]:
	"""Restaure une séquence conservative sans inventer d'éléments métier."""
	if not isinstance(faulty_pd, dict):
		return faulty_pd
	identified = faulty_pd.setdefault("identified_elements", {})
	if not isinstance(identified, dict):
		return faulty_pd

	ordered: list[dict[str, Any]] = []
	for category in ("events", "activities", "conditions"):
		for item in identified.get(category, []) or []:
			if isinstance(item, dict):
				ordered.append({"category": category, "item": item})

	# Déduire l'ordre d'apparition depuis la structure existante et conserver strictement les éléments déjà exposés.
	seen_pairs: set[tuple[str, str]] = set()
	relations = list(identified.get("relations", []) or [])
	for rel in relations:
		if isinstance(rel, dict):
			if isinstance(rel.get("source"), str) and isinstance(rel.get("target"), str):
				seen_pairs.add((rel["source"], rel["target"]))

	for index in range(len(ordered) - 1):
		current = ordered[index]["item"]
		next_item = ordered[index + 1]["item"]
		source_id = current.get("id")
		target_id = next_item.get("id")
		if isinstance(source_id, str) and isinstance(target_id, str) and (source_id, target_id) not in seen_pairs:
			relations.append({"id": f"relation_{len(relations) + 1}", "source": source_id, "target": target_id, "relation": "after"})
			seen_pairs.add((source_id, target_id))

	if ordered:
		start = ordered[0]["item"]
		end = ordered[-1]["item"]
		for category in ("events", "activities"):
			for item in identified.get(category, []) or []:
				if not isinstance(item, dict):
					continue
				item_id = item.get("id")
				if not isinstance(item_id, str):
					continue
				if item_id == start.get("id"):
					continue
				if item_id == end.get("id"):
					continue
			if all((source, item_id) not in seen_pairs for source in [e.get("id") for e in ordered if isinstance(e, dict) and isinstance(e.get("id"), str)]):
				for obj in ordered:
					obj_id = obj.get("item", {}).get("id")
					if isinstance(obj_id, str) and obj_id != item_id and (obj_id, item_id) not in seen_pairs:
						relations.append({"id": f"relation_{len(relations) + 1}", "source": obj_id, "target": item_id, "relation": "after"})
						seen_pairs.add((obj_id, item_id))
						break

	identified["relations"] = relations
	identified["sequence_flows"] = [
		{"source": rel.get("source"), "target": rel.get("target")} for rel in relations if isinstance(rel, dict) and isinstance(rel.get("source"), str) and isinstance(rel.get("target"), str)
	]
	return faulty_pd


def heal_process_description(faulty_pd: dict[str, Any], validation_errors: list[str],
							 user_text: str, run_id: str | None = None, attempt: int = 1,
							 api_key: str | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
	prompt = (
		"Corrige cette Process Description en tenant compte de chaque erreur de validation. "
		"Ne change pas les faits du texte et n'invente aucun élément. Réponds uniquement en JSON.\n"
		f"Erreurs précises:\n{json.dumps(validation_errors, ensure_ascii=False)}\n"
		f"Process Description invalide:\n{json.dumps(faulty_pd, ensure_ascii=False)}\n"
		f"Texte original:\n{user_text}"
	)
	messages: list[dict[str, str]] = []
	try:
		messages = [{"role": "user", "content": prompt}]
		raw = _complete(_get_mistral_client(api_key), model, messages,
						"process-description.schema.json", "process_description_fix")
		result = json.loads(raw)
		_log_llm(run_id, ActionType.FIX, "process_description_healing", "success", prompt, raw, attempt=attempt, model=model)
		return result
	except Exception as exc:
		repaired = _repair_process_description(faulty_pd)
		_log_llm(run_id, ActionType.FIX, "process_description_healing", "fallback", prompt,
				 errors=[str(exc)], attempt=attempt, model=model)
		if _is_mistral_model_issue(exc) or isinstance(exc, (ValueError, TypeError, KeyError, OSError)):
			return repaired
		raise ValueError(f"Correction du Process Description échouée : {exc}") from exc


def generate_logic_core_from_pd(process_desc: dict[str, Any], run_id: str | None = None,
								api_key: str | None = None,
								model: str = DEFAULT_MODEL) -> dict[str, Any]:
	prompt = (
		"Transforme la Process Description validée en Logic-Core BPMN 2.0 conforme au schema. "
		"Utilise SKILL.md comme règles expertes. Chaque élément doit être justifié par la description "
		"et ajoute sourceProcessElement lorsqu'un élément provient d'un élément Process Description. "
		"ou nécessaire à la structure BPMN. Ne mets aucune coordonnée, dimension ou waypoint. "
		"Réponds uniquement en JSON.\n\n"
		f"Process Description:\n{json.dumps(process_desc, ensure_ascii=False)}"
	)
	messages = [{"role": "system", "content": _skill_prompt()}, {"role": "user", "content": prompt}]
	try:
		raw = _complete(_get_mistral_client(api_key), model, messages, "logic-core.schema.json", "logic_core")
		result = json.loads(raw)
		# Sécuriser les relations de séquence même si le LLM a produit un graphe incomplet.
		if isinstance(result, dict):
			from validate import normalize_logic_core_graph
			result = normalize_logic_core_graph(result)
		_log_llm(run_id, ActionType.GENERATION, "logic_core", "success", _trace_prompt(messages), raw, model=model)
		return result
	except Exception as exc:
		if _is_mistral_model_issue(exc):
			fallback = build_logic_core_from_process_description(process_desc)
			_log_llm(run_id, ActionType.DEBUG, "logic_core", "fallback", _trace_prompt(messages),
				 errors=[str(exc)], model=model)
			return fallback
		_log_llm(run_id, ActionType.DEBUG, "logic_core", "failed", _trace_prompt(messages), errors=[str(exc)], model=model)
		if isinstance(exc, (json.JSONDecodeError, ValueError)):
			raise ValueError(f"Logic-Core invalide : {exc}") from exc
		raise


def extract_logic_core(user_text: str, existing_logic_core: dict[str, Any] | None = None,
					   run_id: str | None = None, api_key: str | None = None,
					   model: str = DEFAULT_MODEL) -> dict[str, Any]:
	"""API historique conservée pour les intégrations existantes."""
	if existing_logic_core is not None:
		prompt = (
			"Amende ce Logic-Core avec la demande utilisateur. Conserve tous les IDs existants et "
			"toutes les parties non concernées. Réponds uniquement en JSON.\n"
			f"Logic-Core:\n{json.dumps(existing_logic_core, ensure_ascii=False)}\nDemande:\n{user_text}"
		)
		messages = [{"role": "system", "content": _skill_prompt()}, {"role": "user", "content": prompt}]
		raw = _complete(_get_mistral_client(api_key), model, messages, "logic-core.schema.json", "logic_core_amendment")
		result = json.loads(raw)
		_log_llm(run_id, ActionType.GENERATION, "logic_core_amendment", "success", _trace_prompt(messages), raw, model=model)
		return result
	return generate_logic_core_from_pd(extract_process_description(user_text, run_id, api_key, model), run_id, api_key, model)

def self_heal_logic_core(faulty_logic_core: dict[str, Any], validation_errors: list[str],
						 user_context: str | None = None, run_id: str | None = None,
						 attempt: int = 1, api_key: str | None = None,
						 model: str = DEFAULT_MODEL) -> dict[str, Any]:
	prompt = (
		"Corrige chirurgicalement ce Logic-Core BPMN. Les erreurs ci-dessous sont celles du validator; "
		"corrige-les sans supprimer les parties correctes ni modifier les IDs existants. "
		"Réponds uniquement en JSON, sans coordonnées.\n"
		f"Erreurs:\n{json.dumps(validation_errors, ensure_ascii=False)}\n"
		f"Contexte:\n{user_context or ''}\nLogic-Core:\n{json.dumps(faulty_logic_core, ensure_ascii=False)}"
	)
	if any("Fusion suspecte d'issues métier opposées" in err for err in validation_errors):
		prompt += "\nRappel de correction : applique la Règle 15.6 : si deux branches opposées d'une décision aboutissent à des issues métier différentes et que le texte ne décrit pas d'étape commune explicite, sépare-les en deux endEvent distincts, conserve tous les IDs existants et ajoute uniquement les nouveaux endEvent nécessaires."
	messages: list[dict[str, str]] = []
	try:
		messages = [{"role": "system", "content": _skill_prompt()}, {"role": "user", "content": prompt}]
		raw = _complete(_get_mistral_client(api_key), model, messages,
					"logic-core.schema.json", "logic_core_fix")
		result = json.loads(raw)
		_log_llm(run_id, ActionType.FIX, "logic_core_healing", "success", _trace_prompt(messages), raw, attempt=attempt, model=model)
		return result
	except Exception as exc:
		from validate import normalize_logic_core_graph
		fallback = normalize_logic_core_graph(faulty_logic_core, user_context)
		_log_llm(run_id, ActionType.FIX, "logic_core_healing", "fallback", _trace_prompt(messages),
				 errors=[str(exc)], attempt=attempt, model=model)
		if _is_mistral_model_issue(exc) or isinstance(exc, (ValueError, TypeError, KeyError, OSError)):
			return fallback
		if isinstance(exc, (json.JSONDecodeError, ValueError)):
			raise ValueError(f"Self-healing Logic-Core échoué : {exc}") from exc
		raise
