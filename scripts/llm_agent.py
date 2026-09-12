"""Appels Mistral pour comprendre un processus et produire son Logic-Core."""
from __future__ import annotations

import json
import os
import re
import time
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

try:
	import httpx
except ImportError:
	httpx = None

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


def _complete(client: Any, model: str, messages: list[dict[str, str]], schema: str, name: str,
			  max_retries: int = 3) -> str:
	last_exc: Exception | None = None
	for attempt in range(max_retries + 1):
		try:
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
		except ValueError:
			raise
		except Exception as exc:
			status_code = getattr(exc, "status_code", None) or getattr(exc, "raw_status_code", None)
			is_rate_limited = status_code == 429 or "429" in str(exc) or "rate_limited" in str(exc).lower()
			# Les pannes réseau transitoires (DNS, coupure Wi-Fi, timeout de connexion)
			# remontent comme httpx.TransportError et non comme un statut HTTP : ce
			# n'est ni un rate-limit ni une erreur de compte, mais ça reste presque
			# toujours résolu par une simple nouvelle tentative quelques secondes après.
			is_transient_network_error = httpx is not None and isinstance(exc, httpx.TransportError)
			if (is_rate_limited or is_transient_network_error) and attempt < max_retries:
				last_exc = exc
				time.sleep(2 ** attempt)  # backoff : 1s, 2s, 4s
				continue
			raise
	raise last_exc  # pragma: no cover - inatteignable en pratique


_SCHEMA_METADATA_KEYS = {"$schema", "$id", "$defs", "definitions", "title"}


def _strip_schema_metadata(obj: Any) -> Any:
	"""Retire les clés de méta-schéma (ex: $id, $schema) que certains modèles recopient par erreur."""
	if isinstance(obj, dict):
		return {k: v for k, v in obj.items() if k not in _SCHEMA_METADATA_KEYS}
	return obj


def _unwrap_if_wrapped(obj: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
	"""Si le modèle enveloppe sa réponse dans une clé racine inattendue (ex:
	{"logic_core": {...contenu réel...}} au lieu du contenu directement à la
	racine), _prune_extra_properties supprimerait TOUT le contenu utile : aucune
	propriété racine attendue ('process', 'nodes', 'edges'...) ne correspond,
	et le résultat élagué devient un objet vide — observé en usage réel sur un
	amendement pourtant parfaitement correct en substance, silencieusement
    réduit à {} par ce seul détail d'enveloppe. Détecter ce cas AVANT l'élagage
	et déballer plutôt que de laisser l'élagage tout supprimer."""
	required = schema.get("required")
	if not isinstance(required, list) or not required or set(required).issubset(obj.keys()):
		return obj
	for value in obj.values():
		if isinstance(value, dict) and set(required).issubset(value.keys()):
			return value
	return obj


def _prune_extra_properties(obj: Any, schema: dict[str, Any]) -> Any:
	"""Retire récursivement les propriétés non déclarées dans le schema quand
	additionalProperties=false. Les schémas de ce projet (structured-analysis,
	process-description, logic-core) partagent des noms de champs très proches
	(ex: 'source_text' existe dans l'un mais pas l'autre) ; un modèle qui corrige
	un JSON existant recopie parfois un champ du "mauvais" schema. Élaguer ces
	champs superflus est plus robuste que de faire échouer toute la génération
	pour un détail sans rapport avec le fond de la correction demandée."""
	if not isinstance(schema, dict):
		return obj
	if isinstance(obj, dict) and schema.get("type") == "object":
		obj = _unwrap_if_wrapped(obj, schema)
		properties = schema.get("properties", {})
		if schema.get("additionalProperties") is False:
			obj = {k: v for k, v in obj.items() if k in properties}
		for key in list(obj.keys()):
			sub_schema = properties.get(key)
			if isinstance(sub_schema, dict):
				obj[key] = _prune_extra_properties(obj[key], sub_schema)
		return obj
	if isinstance(obj, list) and schema.get("type") == "array":
		item_schema = schema.get("items")
		if isinstance(item_schema, dict):
			return [_prune_extra_properties(item, item_schema) for item in obj]
		return obj
	return obj


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

	# Panne réseau transitoire (DNS/coupure/timeout) : remonte comme httpx.TransportError
	# (ex: ConnectError "[Errno 11001] getaddrinfo failed"), sans status_code ni mot-clé
	# reconnu par les motifs textuels ci-dessous — sans ce cas explicite, une simple
	# coupure Wi-Fi fait planter tout le pipeline avec une erreur opaque au lieu de
	# basculer sur le repli local comme les autres pannes Mistral.
	if httpx is not None and isinstance(exc, httpx.TransportError):
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
				"condition": positive[:60],
				"evidence": segment,
				"branches": [
					{"label": positive[:60], "meaning": positive_outcome},
					{"label": negative[:60], "meaning": negative_outcome},
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
		"Règle critique sur 'events' : ne JAMAIS créer un event qui ne fait que redécrire "
		"l'accomplissement d'une activité déjà listée dans 'activities' (ex: si 'Envoyer confirmation "
		"au client' existe déjà comme activity, ne PAS créer en plus un event 'Confirmation envoyée au "
		"client' — c'est le même instant métier compté deux fois). Le tableau 'events' est réservé à : "
		"(a) l'event de début du processus (event_type='start'), (b) un event réellement typé "
		"(timer/message/error/signal/cancellation) qui porte une information que l'activité seule ne "
		"porte pas (ex: un délai précis, un message externe). Une simple notification/confirmation déjà "
		"couverte par une activité ne justifie jamais un event supplémentaire.\n\n"
		f"Texte source:\n{user_text}"
	)
	messages = [{"role": "user", "content": prompt}]
	try:
		raw = _complete(_get_mistral_client(api_key), model, messages,
						"structured-analysis.schema.json", "structured_analysis")
		result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("structured-analysis.schema.json"))
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
	def _dicts(items: Any) -> list[dict[str, Any]]:
		# extract_structured_analysis n'est pas garanti à 100% par le schéma JSON
		# structuré de Mistral (déjà observé : un élément de tableau peut arriver
		# comme simple chaîne au lieu d'un objet) — un item non-dict ne peut de
		# toute façon pas être exploité comme participant/activité/etc., on
		# l'ignore plutôt que de planter tout le pipeline sur un seul élément
		# mal formé.
		return [item for item in (items if isinstance(items, list) else []) if isinstance(item, dict)]

	participants = []
	for item in _dicts(analysis.get("participants")):
		participants.append({"id": item.get("id"), "name": item.get("name"), "type": "actor",
			"description": item.get("evidence", ""), "sourceAnalysisId": item.get("id"),
			"evidence": item.get("evidence", ""), "confidence": item.get("confidence", "medium")})
	participant_ids = {item.get("name"): item.get("id") for item in _dicts(analysis.get("participants")) if item.get("name") and item.get("id")}
	activities = []
	for index, item in enumerate(_dicts(analysis.get("activities")), 1):
		activity = {"id": item.get("id"), "name": item.get("name"), "type": "task",
			"sequence": index, "sourceAnalysisId": item.get("id"), "evidence": item.get("evidence", ""),
			"source_text": item.get("source_text", item.get("evidence", "")), "confidence": item.get("confidence", "medium")}
		actor = item.get("actor")
		if isinstance(actor, list):
			actor = actor[0] if actor else None
		if actor in participant_ids:
			activity["actor_id"] = participant_ids[actor]
		activities.append(activity)
	# extract_structured_analysis génère systématiquement, pour une grande partie des
	# activités, un "event" fantôme qui ne fait que redécrire le même instant métier
	# (ex: activity "Envoyer confirmation au client" + event "Confirmation envoyée au
	# client" avec trigger=cet id d'activité) — sans qu'aucune relation ne référence
	# jamais cet event (les relations ne connectent que des activities/conditions).
	# Un tel event est structurellement toujours orphelin, une erreur de validation
	# fatale que le self-healing ne corrige pas de façon fiable en 3 tentatives
	# (régression confirmée en usage réel). Un event de type intermediate/end dont le
	# trigger pointe vers une activité déjà listée n'apporte aucune information que
	# l'activité ne porte déjà : on le retire mécaniquement, sans dépendre du LLM.
	# Les events start (jamais dupliqués ainsi) et les events réellement typés
	# (timer/message/error/signal/cancellation, qui portent une sémantique BPMN que
	# l'activité seule ne porte pas) sont conservés intacts.
	activity_ids = {item.get("id") for item in _dicts(analysis.get("activities")) if item.get("id")}
	events = [
		{"id": item.get("id"), "name": item.get("name"), "type": item.get("event_type", "intermediate"),
		 "trigger": item.get("trigger", ""), "sourceAnalysisId": item.get("id"),
		 "evidence": item.get("evidence", ""), "confidence": item.get("confidence", "medium")}
		for item in _dicts(analysis.get("events"))
		if not (
			item.get("event_type") in ("intermediate", "end")
			and item.get("trigger") in activity_ids
		)
	]
	conditions = [{"id": item.get("id"), "condition_text": item.get("condition"),
		"branches": [{"condition": branch.get("label"), "outcome": branch.get("meaning")}
			for branch in _dicts(item.get("branches"))], "sourceAnalysisId": item.get("id"),
		"evidence": item.get("evidence", "")} for item in _dicts(analysis.get("conditions"))]
	relations = []
	for index, item in enumerate(_dicts(analysis.get("relations")), 1):
		raw_relation = item.get("relation")
		relation_type = "message" if raw_relation == "message" else "sequence"
		relations.append({"id": item.get("id") or f"relation_{index:03d}",
			"source": item.get("source"), "target": item.get("target"), "type": relation_type})
	identified = {"participants": participants, "activities": activities, "events": events,
		"conditions": conditions, "gateways": [], "relations": relations,
		"sequence_flows": []}
	result = {"process_name": "Processus analysé", "understanding_summary": "Analyse structurée des faits source.",
		"identified_elements": identified, "gaps": [{"gap_id": gap.get("id"), "description": gap.get("description"),
			"severity": gap.get("severity")} for gap in _dicts(analysis.get("gaps"))], "confidence_level": "medium",
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
			relations.append({"id": f"relation_{len(relations) + 1}", "source": source_id, "target": target_id, "type": "sequence"})
			seen_pairs.add((source_id, target_id))

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
		result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("process-description.schema.json"))
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
		result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("logic-core.schema.json"))
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


# Catalogue partagé entre extract_amendment_intent (classification, étape [A]) et
# extract_logic_core (application, étape [B]) : une seule source pour le triplet
# (déclencheur textuel, règle courte, pointeur SKILL.md §24) évite de dupliquer les 34
# sous-catégories dans deux prompts qui pourraient diverger avec le temps.
_AMENDMENT_OPERATION_CATALOG: list[dict[str, str]] = [
	{"operation_type": "insert_sequential", "section": "27.1.1",
	 "trigger": "\"ajoute X après Y\" / \"insère X avant Y\" / \"ajoute une étape de X\", SANS marqueur conditionnel",
	 "rule": "Insertion séquentielle simple : AUCUN gateway créé. \"après\"/\"avant\" décrit un ordre, jamais une condition."},
	{"operation_type": "insert_conditional_exclusive", "section": "27.1.2",
	 "trigger": "marqueur conditionnel explicite : \"si\", \"sinon\", \"selon que\", \"dans le cas où\", \"à condition que\"",
	 "rule": "Créer un exclusiveGateway après le nœud d'ancrage, une branche par issue décrite, chaque sequenceFlow sortant nommé/conditionné."},
	{"operation_type": "insert_conditional_inclusive", "section": "27.1.3",
	 "trigger": "au moins deux conditions énoncées indépendamment, pouvant être vraies simultanément pour un même cas",
	 "rule": "inclusiveGateway (jamais exclusiveGateway), refermé par un second inclusiveGateway convergent avant toute tâche commune. Détection sur la STRUCTURE SYNTAXIQUE, pas sur un vocabulaire déjà vu en exemple."},
	{"operation_type": "insert_parallel", "section": "27.1.4",
	 "trigger": "\"en parallèle\", \"en même temps\", \"simultanément\", \"pendant que\"",
	 "rule": "parallelGateway en split, refermé par un parallelGateway convergent (jamais un exclusif) avant la tâche commune suivante."},
	{"operation_type": "insert_event_based_gateway", "section": "27.1.5",
	 "trigger": "\"attend soit X soit Y, selon ce qui arrive en premier\"",
	 "rule": "eventBasedGateway en point de divergence DIRECT (jamais précédé d'une tâche \"attendre\"), suivi des catch events concurrents."},
	{"operation_type": "insert_complex_gateway", "section": "27.1.6",
	 "trigger": "seuil de comptage (\"au moins N des M critères/conditions/votes\")",
	 "rule": "complexGateway avec la condition de seuil documentée en langage naturel — jamais approximé par exclusif/inclusif."},
	{"operation_type": "insert_loop_backward", "section": "27.1.7",
	 "trigger": "\"corrige et resoumets\", \"recommence\", \"à nouveau X\", \"tant que\", \"jusqu'à ce que\"",
	 "rule": "Ajouter un sequenceFlow de retour vers le nœud EXISTANT identifié — ne jamais dupliquer ce nœud."},
	{"operation_type": "add_actor_internal_lane", "section": "27.1.8",
	 "trigger": "rôle/service mentionné sans qualification d'externalité, ou explicitement interne à l'organisation",
	 "rule": "Créer une nouvelle lane dans la pool principale existante, jamais une pool séparée."},
	{"operation_type": "add_actor_external_pool", "section": "27.1.9",
	 "trigger": "qualification explicite d'externalité (\"externe\", \"partenaire\", \"fournisseur\", \"banque\", \"prestataire\", \"tiers\", \"sous-traitant\")",
	 "rule": "Créer une nouvelle pool séparée, reliée par messageFlow — jamais par sequenceFlow direct."},
	{"operation_type": "add_subprocess_embedded", "section": "27.1.10",
	 "trigger": "\"ce qui inclut...\", \"composé de...\", une action qui se décompose en plusieurs sous-étapes énumérées",
	 "rule": "Créer un subProcess contenant TOUTES les sous-étapes énumérées comme enfants directs, avec son propre startEvent/endEvent internes."},
	{"operation_type": "add_call_activity", "section": "27.1.11",
	 "trigger": "\"appelle le processus standard de X\", réutilisation explicite ailleurs",
	 "rule": "Un seul élément callActivity avec calledElement — jamais une pool séparée simulant l'appel, jamais un subProcess détaillé en double."},
	{"operation_type": "add_subprocess_adhoc", "section": "27.1.12",
	 "trigger": "\"n'importe quel ordre\", \"selon les disponibilités\", \"sans ordre imposé\"",
	 "rule": "adHocSubProcess (marqueur \"~\"), tâches internes SANS sequenceFlow stricts — jamais un gateway parallèle."},
	{"operation_type": "add_subprocess_transactional", "section": "27.1.13",
	 "trigger": "\"comme une seule opération\", \"si une étape échoue, annuler toutes les précédentes\"",
	 "rule": "subProcess avec isTransaction=true, cancelEndEvent interne ou boundaryEvent cancel attaché."},
	{"operation_type": "add_subprocess_event", "section": "27.1.14",
	 "trigger": "\"à tout moment pendant ce traitement, si X survient\"",
	 "rule": "subProcess avec triggeredByEvent=true, déclenché par un start event interne typé. Reste dans la MÊME pool que le processus qu'il interrompt."},
	{"operation_type": "add_timer_event", "section": "27.1.15",
	 "trigger": "délai exprimé en unité de temps (\"après N heures/jours\", \"sous N\")",
	 "rule": "boundaryEvent avec eventDefinition=timer, eventDetail au format ISO 8601. Interruptif par défaut, sauf marqueur \"sans interrompre\". Ne jamais confondre avec une escalade générique."},
	{"operation_type": "add_signal_event", "section": "27.1.16",
	 "trigger": "broadcast à plusieurs récepteurs potentiels",
	 "rule": "throw signal event + catch signal events — jamais un flux de message point-à-point. Le flux normal continue par sequenceFlow classique dans la MÊME pool."},
	{"operation_type": "add_escalation_event", "section": "27.1.17",
	 "trigger": "condition métier non temporelle (\"si le montant dépasse X\") déclenchant une remontée hiérarchique",
	 "rule": "escalationEvent / escalationBoundaryEvent."},
	{"operation_type": "add_compensation_event", "section": "27.1.18",
	 "trigger": "\"annule automatiquement [action déjà effectuée] si [échec ultérieur]\"",
	 "rule": "compensationBoundaryEvent attaché à la tâche compensée + tâche isForCompensation reliée par association + throw compensation event dans la branche d'échec."},
	{"operation_type": "add_conditional_event", "section": "27.1.19",
	 "trigger": "\"reste en attente jusqu'à ce que [condition] devienne vraie\"",
	 "rule": "intermediateCatchEvent eventDefinition=conditional, placé DANS le flux principal."},
	{"operation_type": "add_terminate_event", "section": "27.1.20",
	 "trigger": "\"le processus s'arrête immédiatement dans son ensemble\"",
	 "rule": "endEvent eventDefinition=terminate."},
	{"operation_type": "add_multi_instance", "section": "27.1.21",
	 "trigger": "\"chacun des N fait X indépendamment\" (parallèle) ou \"pour chaque X, l'un après l'autre\" (séquentiel)",
	 "rule": "UNE SEULE tâche avec loopCharacteristics — jamais N tâches nommées distinctes reliées par un exclusiveGateway."},
	{"operation_type": "add_data_object", "section": "27.1.22",
	 "trigger": "\"en utilisant X\", \"en se basant sur X\", \"enregistre dans X\", \"consulte X\"",
	 "rule": "dataObjectReference ou dataStoreReference selon la nature de X, relié par association."},
	{"operation_type": "delete_task_simple", "section": "27.2.1",
	 "trigger": "retrait d'une tâche simple au milieu d'un flux",
	 "rule": "Retirer le nœud et ses edges. Reconnecter DIRECTEMENT le prédécesseur au successeur, en conservant leur ordre relatif exact. Ne jamais créer de boucle qui n'existait pas."},
	{"operation_type": "delete_gateway_branch", "section": "27.2.2",
	 "trigger": "retrait d'une branche entière d'un gateway",
	 "rule": "Retirer la branche. Si une seule branche reste, retirer aussi le gateway et reconnecter en séquentiel."},
	{"operation_type": "delete_gateway_full", "section": "27.2.3",
	 "trigger": "suppression complète d'un gateway",
	 "rule": "Reconnexion directe prédécesseur → successeur (même règle que delete_task_simple)."},
	{"operation_type": "delete_actor", "section": "27.2.4",
	 "trigger": "suppression d'un acteur (lane ou pool)",
	 "rule": "Réassigner ou retirer explicitement toutes les tâches de cet acteur — jamais de tâches orphelines sans poolId/laneId valide."},
	{"operation_type": "delete_subprocess", "section": "27.2.5",
	 "trigger": "suppression d'un sous-processus",
	 "rule": "Remonter les sous-étapes au niveau parent si demandé, ou tout retirer et reconnecter (même règle que delete_task_simple)."},
	{"operation_type": "delete_event", "section": "27.2.6",
	 "trigger": "suppression d'un événement (timer, signal, etc.)",
	 "rule": "boundaryEvent -> retirer uniquement l'événement et sa branche, la tâche hôte reste inchangée. Événement dans le flux principal -> reconnecter (même règle que delete_task_simple)."},
	{"operation_type": "delete_data_object", "section": "27.2.7",
	 "trigger": "suppression d'un objet de données",
	 "rule": "Retirer la référence et son association, n'affecte jamais le flux de séquence."},
	{"operation_type": "rename_only", "section": "27.3.1",
	 "trigger": "renommage pur, sans changement de structure",
	 "rule": "Changer UNIQUEMENT \"name\". ID, type, position, connexions : inchangés."},
	{"operation_type": "replace_task", "section": "27.3.2",
	 "trigger": "\"remplace X par Y\"",
	 "rule": "Le TYPE BPMN du nouveau nœud doit être réévalué à partir de la nature de Y SEULE — jamais hérité du type de X. Les edges de X sont repris à l'identique."},
	{"operation_type": "replace_gateway_type", "section": "27.3.3",
	 "trigger": "les branches d'un gateway existant deviennent, ou cessent d'être, mutuellement exclusives",
	 "rule": "Convertir exclusif <-> inclusif en ajoutant/retirant le gateway de convergence correspondant."},
	{"operation_type": "change_task_actor", "section": "27.3.4",
	 "trigger": "\"c'est maintenant [autre acteur] qui fait X\"",
	 "rule": "Changer poolId/laneId. Si changement de pool, réévaluer si les edges adjacents doivent devenir messageFlow (règle add_actor_external_pool + POOL-001)."},
	{"operation_type": "change_gateway_condition", "section": "27.3.5",
	 "trigger": "modification du critère d'une condition déjà modélisée sur un gateway existant",
	 "rule": "Mettre à jour UNIQUEMENT \"condition\"/\"name\" des sequenceFlow sortants concernés — jamais recréer le gateway ni changer son ID."},
]

_AMENDMENT_OPERATION_BY_TYPE = {entry["operation_type"]: entry for entry in _AMENDMENT_OPERATION_CATALOG}

# Les 3 pièges déjà observés en usage réel (cf. section 27 de SKILL.md) sont dupliqués ici
# tels quels, en plus de leur présence dans le system prompt (SKILL.md) : un rappel inline,
# au plus près du point où l'erreur se produit, s'est montré plus fiable qu'un rappel noyé
# dans un prompt système déjà très long (cf. le bug du gateway inventé sur v01_insertion_simple).
_AMENDMENT_PITFALLS = {
	"insert_sequential": (
		"PIÈGE À ÉVITER : ne jamais transformer une insertion simple en décision conditionnelle sous "
		"prétexte que \"après\"/\"avant\" pourrait sembler introduire une alternative — c'est un repère "
		"de position dans le flux, jamais un critère de décision, sauf marqueur conditionnel explicite."
	),
	"insert_conditional_inclusive": (
		"PIÈGE À ÉVITER : ce pattern doit être détecté sur la STRUCTURE SYNTAXIQUE du texte (plusieurs "
		"conditions énoncées indépendamment, non mutuellement exclusives), jamais sur un vocabulaire "
		"métier précis déjà vu en exemple."
	),
	"replace_task": (
		"PIÈGE À ÉVITER : un remplacement successif repart TOUJOURS de la description du nouveau "
		"remplacement seule, jamais de la classification du remplacement précédent — le type BPMN n'est "
		"jamais hérité."
	),
}


def extract_amendment_intent(user_text: str, existing_logic_core: dict[str, Any],
							 run_id: str | None = None, api_key: str | None = None,
							 model: str = DEFAULT_MODEL) -> dict[str, Any]:
	"""Étape [A] de l'architecture d'amendement : classifie l'opération demandée AVANT de
	l'appliquer, plutôt que de laisser un unique appel LLM aller directement du texte au
	nouveau Logic-Core sans jamais formaliser quelle opération est en jeu. Symétrique au
	Process Description qui existe déjà pour la génération initiale (extract_process_description).

	Ne modifie rien : produit uniquement l'intention structurée (operation_type, ancres
	réelles, preuve textuelle, diff attendu) consommée ensuite par extract_logic_core (étape
	[B]) et validate_amendment_diff (étape [D])."""
	catalog_lines = "\n".join(
		f"- {entry['operation_type']} : {entry['trigger']}" for entry in _AMENDMENT_OPERATION_CATALOG
	)
	prompt = (
		"Classifie PRÉCISÉMENT l'opération d'amendement demandée par l'utilisateur sur ce Logic-Core "
		"existant. Choisis EXACTEMENT une valeur de operation_type parmi celles listées ci-dessous "
		"(la plus spécifique possible, jamais une approximation). Résous target_anchors en IDs RÉELS "
		"déjà présents dans le Logic-Core fourni — jamais devinés par proximité de nom : si un élément "
		"visé n'a pas d'ID correspondant évident, laisse target_anchors vide plutôt que d'inventer un ID.\n\n"
		"Catalogue des opérations (operation_type : déclencheur textuel) :\n"
		f"{catalog_lines}\n\n"
		"evidence_quote : la citation exacte du texte de la demande justifiant ce choix.\n"
		"conditional_evidence : le marqueur conditionnel exact trouvé (\"si\", \"sinon\", \"selon que\", "
		"...), ou null si la demande n'en contient aucun — NE JAMAIS en inventer un.\n"
		"replacement_new_type : uniquement pour replace_task/replace_gateway_type, le type BPMN attendu "
		"du nouvel élément réévalué depuis sa propre nature ; null sinon.\n"
		"expected_diff : ta meilleure estimation du nombre de nœuds ajoutés/retirés et de gateways "
		"ajoutés par cette opération.\n\n"
		f"Logic-Core existant (pour résoudre les IDs réels) :\n{json.dumps(existing_logic_core, ensure_ascii=False)}\n"
		f"Instruction d'amendement :\n{user_text}"
	)
	messages = [{"role": "user", "content": prompt}]
	raw = _complete(_get_mistral_client(api_key), model, messages,
					"amendment-intent.schema.json", "amendment_intent")
	result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("amendment-intent.schema.json"))
	# _prune_extra_properties ne recurse que dans les sous-schemas object/array : un champ
	# scalaire (operation_type, "type": "string" + "enum") dont la valeur arrive malgré tout
	# sous forme de dict (observé en usage réel : {"value": "insert_sequential"}, ou même le
	# fragment de schéma lui-même {"type": "string", "enum": [...]}) traverse cette fonction
	# sans être corrigé, puis fait planter tout appelant qui l'utilise comme clé de dict
	# (TypeError: unhashable type: 'dict'). Tenter une récupération sur les formes déjà
	# observées avant d'abandonner proprement.
	op_type = result.get("operation_type") if isinstance(result, dict) else None
	if isinstance(op_type, dict):
		if isinstance(op_type.get("value"), str):
			op_type = op_type["value"]
		elif isinstance(op_type.get("const"), str):
			op_type = op_type["const"]
		elif isinstance(op_type.get("enum"), list) and op_type["enum"] and isinstance(op_type["enum"][0], str):
			op_type = op_type["enum"][0]
		else:
			op_type = None
		result["operation_type"] = op_type
	if not isinstance(result, dict) or result.get("operation_type") not in _AMENDMENT_OPERATION_BY_TYPE:
		raise ValueError(f"operation_type invalide ou non reconnu retourné par la classification : {op_type!r}")
	_log_llm(run_id, ActionType.ANALYSIS, "amendment_intent", "success", _trace_prompt(messages), raw, model=model)
	return result


def extract_logic_core(user_text: str, existing_logic_core: dict[str, Any] | None = None,
					   run_id: str | None = None, api_key: str | None = None,
					   model: str = DEFAULT_MODEL, intent: dict[str, Any] | None = None) -> dict[str, Any]:
	"""API historique conservée pour les intégrations existantes.

	`intent` (optionnel) : sortie de extract_amendment_intent (étape [A]) — quand fourni, la
	demande d'amendement n'est plus appliquée à l'aveugle : l'opération déjà classifiée, ses
	ancres réelles et la règle précise correspondante (cf. section 27 de SKILL.md) sont
	injectées explicitement, plutôt que de compter uniquement sur le LLM pour reclassifier
	implicitement l'opération une seconde fois au moment de l'appliquer."""
	if existing_logic_core is not None:
		prompt = (
			"Amende ce Logic-Core avec la demande utilisateur. Conserve tous les IDs existants (nœuds, "
			"edges, pools, lanes) et toutes les parties non concernées. Réponds uniquement en JSON.\n"
			"Rappel critique (règle 21 du système, cf. 'Insertion') : si la demande insère une nouvelle "
			"étape C entre deux éléments A et B déjà reliés par un sequenceFlow existant, ce sequenceFlow "
			"A->B a déjà un ID — RÉUTILISE cet ID EXACT pour le segment A->C résultant (change seulement "
			"son 'target' de B vers C), et crée un NOUVEL ID uniquement pour le second segment C->B. "
			"Renommer l'ID du sequenceFlow A->B original est INTERDIT, même si sa cible change : c'est "
			"une suppression d'ID au même titre qu'un nœud supprimé, et la modification sera rejetée.\n"
			"MÊME RÈGLE si la demande transforme un flux A->B existant en embranchement conditionnel "
			"(A->gateway->[B, C], une nouvelle alternative ajoutée à une décision) : RÉUTILISE l'ID du "
			"sequenceFlow A->B existant pour le segment gateway->B (change seulement sa 'source' de A vers "
			"le nouveau gateway), et crée des IDs nouveaux uniquement pour A->gateway et gateway->C. Cette "
			"règle s'applique à TOUT sequenceFlow existant qui reste conceptuellement le même flux mais "
			"voit son point de départ ou d'arrivée réaffecté à un nœud nouvellement inséré — jamais un "
			"renommage, toujours une réaffectation de la même arête.\n"
		)
		if intent is not None:
			op_type = intent.get("operation_type")
			catalog_entry = _AMENDMENT_OPERATION_BY_TYPE.get(op_type)
			diff = intent.get("expected_diff") or {}
			rule_pointer = (
				f" (cf. section {catalog_entry['section']} de SKILL.md : {catalog_entry['rule']})"
				if catalog_entry else ""
			)
			prompt += (
				f"\nIntention déjà classifiée (étape préalable) : operation_type = '{op_type}'{rule_pointer}.\n"
				f"Ancre(s) réelle(s) visée(s) dans le Logic-Core ci-dessous : {intent.get('target_anchors')}.\n"
				f"Preuve textuelle : \"{intent.get('evidence_quote')}\".\n"
				f"Marqueur conditionnel détecté dans la demande : {intent.get('conditional_evidence') or 'AUCUN'}.\n"
				f"Ampleur structurelle attendue : +{diff.get('nodes_added', 0)} nœud(s), "
				f"-{diff.get('nodes_removed', 0)} nœud(s), +{diff.get('gateways_added', 0)} gateway(s) — un écart "
				"important par rapport à cette estimation doit être délibéré, pas accidentel.\n"
				"Applique STRICTEMENT la règle de cette opération, pas une autre.\n"
			)
			if intent.get("replacement_new_type"):
				prompt += (
					f"Type BPMN attendu pour le nouvel élément : '{intent['replacement_new_type']}' — réévalué "
					"depuis la nature de ce nouvel élément SEULE, jamais hérité du type de l'ancien.\n"
				)
			pitfall = _AMENDMENT_PITFALLS.get(op_type)
			if pitfall:
				prompt += f"{pitfall}\n"
		prompt += f"Logic-Core:\n{json.dumps(existing_logic_core, ensure_ascii=False)}\nDemande:\n{user_text}"
		messages = [{"role": "system", "content": _skill_prompt()}, {"role": "user", "content": prompt}]
		raw = _complete(_get_mistral_client(api_key), model, messages, "logic-core.schema.json", "logic_core_amendment")
		result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("logic-core.schema.json"))
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
		prompt += (
			"\nRappel de correction : avant toute chose, vérifie si l'une des deux branches correspond en "
			"réalité à une BOUCLE explicitement décrite dans le texte (ex: \"the employee corrects it and "
			"the system validates it again\" = la branche 'incorrect/non' doit reboucler par sequenceFlow "
			"vers la tâche de (re)validation déjà existante, PAS se terminer par un endEvent). Si c'est le "
			"cas, applique la Règle 15.5 : ajoute uniquement ce sequenceFlow de retour, ne crée AUCUN "
			"nouvel endEvent, et conserve tous les IDs existants.\n"
			"Si en revanche les deux branches représentent deux issues métier réellement terminales et "
			"distinctes (aucune re-tentative décrite dans le texte), applique la Règle 15.6 : sépare-les en "
			"deux endEvent distincts, conserve tous les IDs existants et ajoute uniquement les nouveaux "
			"endEvent strictement nécessaires — ne recrée jamais un endEvent qui existe déjà sous un autre nom."
		)
	if any("n'est jamais refermé par un gateway convergent du même type" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (Règle 10.2/10.3) : le split inclusiveGateway/parallelGateway visé doit être "
			"refermé par un second gateway du MÊME type (gatewayDirection='converging') AVANT la tâche commune où "
			"ses branches se rejoignent actuellement. Ajoute ce gateway convergent et fais pointer chaque branche "
			"vers lui plutôt que directement vers la tâche commune ; ne crée pas de nouveau endEvent pour ça."
		)
	if any("est de type message (eventDefinition='message') mais n'est relié à aucun messageFlow" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (section 3) : cet événement message n'a aucune communication externe réelle "
			"(aucun messageFlow vers/depuis un autre pool). Remplace-le par un sequenceFlow direct entre la tâche "
			"précédente et la tâche/gateway suivante, et supprime ce nœud événement — ne le garde que s'il existe "
			"vraiment un échange entre deux pools distincts décrit dans le texte."
		)
	if any("mais n'est qu'un simple maillon de passage dans la chaîne" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (section 3) : cet événement error/signal n'est qu'une condition évaluée "
			"par un gateway déguisée en événement — le texte décrit une simple branche conditionnelle ('si "
			"une erreur survient...'), pas un événement distinct à attendre/capturer. Supprime ce nœud "
			"événement et relie directement le gateway (ou la tâche précédente) à la tâche suivante par "
			"sequenceFlow, en conservant le label de la branche s'il y en a un."
		)
	if any("n'est pas un point de communication valide" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : un messageFlow ne doit jamais partir/arriver sur un gateway. Convertis ce "
			"messageFlow en sequenceFlow interne (la décision reste modélisée par le gateway), et si une "
			"communication externe est réellement décrite dans le texte, fais porter le messageFlow uniquement "
			"sur la tâche d'envoi/réception concernée, pas sur le gateway."
		)
	if any("n'a pas de nom explicite" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : donne à chaque endEvent sans nom un nom explicite décrivant l'issue "
			"métier qu'il représente (ex: 'Prêt approuvé', 'Commande annulée'), sans changer son id."
		)
	if any("est inaccessible depuis les événements de début" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : au moins un nœud n'est relié par AUCUN chemin de sequenceFlow depuis un "
			"startEvent — c'est un nœud orphelin, ou une branche/étape ajoutée sans être raccordée au reste du "
			"flux existant (fréquent quand un amendement ajoute plusieurs nœuds liés entre eux mais oublie le "
			"sequenceFlow qui les connecte au flux principal). Relie-le explicitement : ajoute le sequenceFlow "
			"manquant depuis le nœud/gateway précédent pertinent d'après le texte vers ce nœud (ou vers le "
			"premier nœud de sa branche s'il en fait partie), sans changer les IDs déjà présents ni dupliquer "
			"un nœud existant."
		)
	if any("doit avoir des branches décisionnelles cohérentes" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : cet exclusiveGateway a des branches sans libellé ni condition — un "
			"exclusiveGateway auto-inséré mécaniquement par le validateur (car le nœud précédent portait "
			"plusieurs sequenceFlow sortants) n'a PAS reçu de libellés, ce n'est pas forcément une vraie "
			"décision métier. Relis le texte source pour trancher : (1) si c'est réellement une décision avec "
			"un critère explicite, ajoute à chaque sequenceFlow sortant un 'name'/'condition' décrivant ce "
			"critère (ex: 'Montant > 500€' / 'Montant ≤ 500€') ; (2) si le texte décrit en réalité un DÉLAI "
			"('si aucune réponse sous 24h...', 'si non traité avant...'), ce n'est pas une décision mais un "
			"timeout — supprime ce gateway et les sequenceFlow qu'il porte, remplace-les par un boundaryEvent "
			"(eventDefinition='timer', eventDetail au format ISO 8601, attachedToRef=la tâche concernée) relié "
			"par sequenceFlow à la branche d'échéance dépassée (cf. section sur les timers de SKILL.md) ; (3) si "
			"les branches représentent en réalité deux actions simultanées et non une alternative, remplace ce "
			"gateway par un parallelGateway et justifie-le explicitement dans son nom (ex: 'en parallèle')."
		)
	if any("sequenceFlow sortants alors qu'il n'est pas un gateway" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : ce nœud a probablement fusionné à tort DEUX occurrences textuelles "
			"distinctes d'une action similaire (ex: deux 'informer le client' dans des branches/motifs "
			"différents — cf. section 2 de SKILL.md). Vérifie D'ABORD le texte original : si les branches "
			"sortantes correspondent à deux mentions narrativement différentes, la correction PRÉFÉRÉE est "
			"de séparer ce nœud en autant de nœuds distincts que d'occurrences, chacun avec son propre "
			"sequenceFlow unique vers sa propre tâche/fin d'origine — PAS d'insérer un gateway après un nœud "
			"fusionné à tort. N'insère un exclusiveGateway/inclusiveGateway (cf. Règle 10.3) que si les "
			"branches représentent réellement UNE SEULE décision du texte avec plusieurs issues, pas deux "
			"actions distinctes réunies par erreur."
		)
	if any("relie directement le subProcess" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (section 9.1) : supprime le sequenceFlow reliant le subProcess à son "
			"propre enfant. Le sequenceFlow entrant du subProcess doit déjà cibler le subProcess lui-même "
			"(pas un de ses enfants) ; le point d'entrée interne est déduit automatiquement de l'enfant sans "
			"prédécesseur parmi les autres enfants — aucune arête supplémentaire n'est nécessaire pour ça."
		)
	if any("est atteint depuis" in err and "gateways" in err and "différents et non liés" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (section 2) : ce nœud fusionne à tort deux mentions textuelles distinctes "
			"(deux gateways différents et non liés y mènent, pas deux branches sœurs d'un même gateway). "
			"Retourne au texte original et identifie les DEUX passages narratifs distincts qui ont été "
			"fusionnés. Crée un nœud SÉPARÉ pour chacun (avec un id et, si besoin, un nom légèrement différent "
			"pour refléter son contexte propre), et fais pointer chaque chemin entrant vers SON PROPRE nœud "
			"plutôt que vers le nœud partagé actuel. Conserve tous les autres IDs existants."
		)
	if any("implique le gateway" in err and "point de communication" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction (POOL-001) : un gateway ne peut jamais être relié par messageFlow ni "
			"avoir un sequenceFlow direct vers un pool différent. Deux options : (a) si la tâche de l'autre "
			"côté est en réalité exécutée par le même acteur que le gateway, corrige son poolId pour qu'elle "
			"rejoigne le pool du gateway (sequenceFlow direct, pas de messageFlow) ; (b) sinon, insère une "
			"tâche intermédiaire dans le pool du gateway (ex: 'Transmettre la décision'), relie le gateway à "
			"cette tâche par sequenceFlow, puis fais porter le messageFlow SUR cette tâche intermédiaire vers "
			"la tâche de l'autre pool — jamais directement depuis/vers le gateway lui-même."
		)
	# Rappels pour les erreurs de validate_amendment_diff (étape [D]) : ce sont des écarts entre
	# ce qui a été DEMANDÉ (intention classifiée en [A]) et ce qui a été RÉELLEMENT produit, pas
	# des erreurs de soundness BPMN générique — le rappel doit donc pointer vers l'intention
	# déclarée plutôt que reformuler une règle de structure déjà couverte ailleurs.
	if any("AMENDMENT-DIFF-1" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : cette opération a été classifiée comme une insertion séquentielle "
			"SANS marqueur conditionnel, mais un ou plusieurs gateways ont été créés. Retire ce(s) "
			"gateway(s) et les branches qu'il(s) porte(nt), reconnecte simplement en série "
			"prédécesseur -> nouvelle étape -> successeur, sans jamais introduire de décision."
		)
	if any("AMENDMENT-DIFF-2" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : la suppression demandée ne correspond pas exactement à ce qui a été "
			"ciblé — soit un élément à supprimer est encore présent, soit un élément non visé a disparu. "
			"Ne retire QUE les ancres explicitement ciblées par l'intention déclarée, reconnecte le reste "
			"à l'identique, et restaure tout élément non ciblé qui aurait disparu par erreur."
		)
	if any("AMENDMENT-DIFF-3" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : cette suppression a introduit une boucle arrière qui n'existait pas "
			"avant l'amendement. Une suppression ne doit JAMAIS créer de cycle : reconnecte directement le "
			"prédécesseur du nœud supprimé à son successeur, en ligne droite, sans retour en arrière."
		)
	if any("AMENDMENT-DIFF-4" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : le type BPMN du nœud de remplacement ne correspond pas au type "
			"attendu (réévalué depuis la description du nouvel élément SEULE, cf. règle 3.2 / section "
			"27.3.2 de SKILL.md). Corrige le 'type' de ce nœud pour qu'il corresponde exactement au type "
			"attendu, sans changer son id ni ses edges déjà repris de l'ancien nœud."
		)
	if any("AMENDMENT-DIFF-6" in err for err in validation_errors):
		prompt += (
			"\nRappel de correction : le sous-type mécaniquement vérifiable de cette opération d'ajout "
			"n'est pas correctement modélisé (ex: un timer sans eventDefinition='timer', ou un "
			"multi-instance modélisé à tort comme plusieurs tâches nommées 'X 1'/'X 2' au lieu d'une seule "
			"tâche avec loopCharacteristics). Corrige la structure pour qu'elle corresponde exactement au "
			"pattern attendu de cette sous-catégorie (cf. section 27 de SKILL.md)."
		)
	messages: list[dict[str, str]] = []
	try:
		messages = [{"role": "system", "content": _skill_prompt()}, {"role": "user", "content": prompt}]
		raw = _complete(_get_mistral_client(api_key), model, messages,
					"logic-core.schema.json", "logic_core_fix")
		result = _prune_extra_properties(_strip_schema_metadata(json.loads(raw)), _load_json_schema("logic-core.schema.json"))
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
