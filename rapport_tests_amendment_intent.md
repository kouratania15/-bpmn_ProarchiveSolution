# Rapport de tests — Architecture d'amendement en 5 étapes — 2026-09-12 19:04

**Résultat global : 25/34 sous-catégories réussies (création + amendement)**

| # | Test | Catégorie | operation_type attendu | Création | Amendement | IDs perdus |
|---|------|-----------|-------------------------|----------|------------|------------|
| 1 | `a01_insert_sequential` | Ajout | `insert_sequential` | OK | ECHEC | n/a |
| 2 | `a02_insert_conditional_exclusive` | Ajout | `insert_conditional_exclusive` | OK | ECHEC | n/a |
| 3 | `a03_insert_conditional_inclusive` | Ajout | `insert_conditional_inclusive` | OK | OK | flow_report_send_technician |
| 4 | `a04_insert_parallel` | Ajout | `insert_parallel` | OK | OK | flow_compte_rendu_end, flow_entretien_compte_rendu |
| 5 | `a05_insert_event_based_gateway` | Ajout | `insert_event_based_gateway` | OK | OK | flow_confirmer_end |
| 6 | `a06_insert_complex_gateway` | Ajout | `insert_complex_gateway` | OK | OK | flow_evaluation_approve, flow_soumission_evaluation, task_evaluation_comite |
| 7 | `a07_insert_loop_backward` | Ajout | `insert_loop_backward` | OK | OK | - |
| 8 | `a08_add_actor_internal_lane` | Ajout | `add_actor_internal_lane` | OK | OK | flow_confirmer_end |
| 9 | `a09_add_actor_external_pool` | Ajout | `add_actor_external_pool` | OK | OK | flow_magasin_prepare_to_end |
| 10 | `a10_add_subprocess_embedded` | Ajout | `add_subprocess_embedded` | OK | OK | flow_agent_end |
| 11 | `a11_add_call_activity` | Ajout | `add_call_activity` | OK | ECHEC | n/a |
| 12 | `a12_add_subprocess_adhoc` | Ajout | `add_subprocess_adhoc` | OK | ECHEC | n/a |
| 13 | `a13_add_subprocess_transactional` | Ajout | `add_subprocess_transactional` | OK | ECHEC | n/a |
| 14 | `a14_add_subprocess_event` | Ajout | `add_subprocess_event` | OK | OK | - |
| 15 | `a15_add_timer_event` | Ajout | `add_timer_event` | OK | OK | - |
| 16 | `a16_add_signal_event` | Ajout | `add_signal_event` | OK | OK | end_process_completed, flow_correct_end, flow_detect_correct, flow_start_detect, start_detect_defect, task_correct_defect, task_detect_defect |
| 17 | `a17_add_escalation_event` | Ajout | `add_escalation_event` | OK | OK | flow_deposit_to_treat |
| 18 | `a18_add_compensation_event` | Ajout | `add_compensation_event` | OK | OK | - |
| 19 | `a19_add_conditional_event` | Ajout | `add_conditional_event` | OK | OK | - |
| 20 | `a20_add_terminate_event` | Ajout | `add_terminate_event` | OK | OK | - |
| 21 | `a21_add_multi_instance` | Ajout | `add_multi_instance` | OK | OK | flow_evaluate_to_end |
| 22 | `a22_add_data_object` | Ajout | `add_data_object` | OK | OK | flow_check_to_end |
| 23 | `d01_delete_task_simple` | Suppression | `delete_task_simple` | OK | OK | flow_order_sms, flow_sms_shipping, task_send_sms |
| 24 | `d02_delete_gateway_branch` | Suppression | `delete_gateway_branch` | OK | OK | end_poste_alternatif_propose, flow_decision_partial_match, flow_poste_alternatif_end, task_proposer_poste_alternatif |
| 25 | `d03_delete_gateway_full` | Suppression | `delete_gateway_full` | OK | ECHEC | n/a |
| 26 | `d04_delete_actor` | Suppression | `delete_actor` | OK | OK | - |
| 27 | `d05_delete_subprocess` | Suppression | `delete_subprocess` | OK | OK | end_traiter_reclamation, flow_compenser_end, flow_evaluer_compenser, flow_start_verifier, flow_verifier_evaluer, start_traiter_reclamation, task_evaluer_validite, task_proposer_compensation, task_verifier_historique |
| 28 | `d06_delete_event` | Suppression | `delete_event` | OK | ECHEC | n/a |
| 29 | `d07_delete_data_object` | Suppression | `delete_data_object` | OK | OK | assoc_supplier_db_check |
| 30 | `m01_rename_only` | Modification | `rename_only` | OK | OK | - |
| 31 | `m02_replace_task` | Modification | `replace_task` | OK | ECHEC | n/a |
| 32 | `m03_replace_gateway_type` | Modification | `replace_gateway_type` | OK | OK | - |
| 33 | `m04_change_task_actor` | Modification | `change_task_actor` | ECHEC | ECHEC | n/a |
| 34 | `m05_change_gateway_condition` | Modification | `change_gateway_condition` | OK | OK | - |

## Détails des échecs

### a01_insert_sequential — Ajout (insert_sequential)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : {'value': 'insert_sequential'}

❌ Erreur : unhashable type: 'dict'

```

### a02_insert_conditional_exclusive — Ajout (insert_conditional_exclusive)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : {'value': 'insert_conditional_exclusive'}

❌ Erreur : unhashable type: 'dict'

```

### a11_add_call_activity — Ajout (add_call_activity)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : {'type': 'string', 'enum': ['add_call_activity']}

❌ Erreur : unhashable type: 'dict'

```

### a12_add_subprocess_adhoc — Ajout (add_subprocess_adhoc)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : add_subprocess_adhoc
[TRACE] [4/8] ▶ Validation du Logic-Core
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : Le self-healing a supprime des IDs existants après 3 tentatives : ['pool_equipe_evénementiel']

```

### a13_add_subprocess_transactional — Ajout (add_subprocess_transactional)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : {'value': 'add_compensation_event'}

❌ Erreur : unhashable type: 'dict'

```

### d03_delete_gateway_full — Suppression (delete_gateway_full)
```
AMEND: xistants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Avertissement : ces IDs disparaissent de façon stable malgré le rappel explicite — probablement une conséquence légitime de la correction, acceptée : ['lane_employe', 'lane_manager', 'pool_entreprise', 'process_validation_demande_achat']
[TRACE] [5/8] ⚠️ Self-healing sans progrès à la tentative 1 — une dernière tentative avant abandon
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (2/5)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : Le self-healing a supprime des IDs existants après 3 tentatives : ['end_demande_approuvee', 'lane_employe', 'lane_manager', 'pool_entreprise', 'process_validation_demande_achat']

```

### d06_delete_event — Suppression (delete_event)
```
AMEND: [TRACE] [1/8] ▶ Amendement incrémental du Logic-Core existant
[TRACE] [1/8] ▶ Intention classifiée : delete_event
[TRACE] [1/8] ⚠️ Amendement a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [1/8] ⚠️ Amendement a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : L'amendement a supprimé des IDs existants après 3 tentatives : ['flow_agent_to_gw', 'flow_agent_to_resolved', 'flow_start_to_submit', 'flow_submit_to_agent', 'flow_transfer_to_escalated']

```

### m02_replace_task — Modification (replace_task)
```
AMEND:  aucune amélioration détectée entre deux tentatives : ["Le nœud 'task_send_confirmation_push_via_email' (sendTask) a 2 sequenceFlow sortants alors qu'il n'est pas un gateway. Seul un gateway peut porter plusieurs branches : insérer un exclusiveGateway/inclusiveGateway juste après ce nœud et y rattacher ces branches.", "Nœud 'task_send_confirmation_push_via_email' (sendTask : 'Envoyer email de confirmation (ancienne méthode)') n'a aucune transition séquentielle entrante.", "Nœud 'task_send_confirmation_push_via_email' (sendTask) est inaccessible depuis les événements de début.", "Nœud 'end_task_send_confirmation_email' (endEvent) est inaccessible depuis les événements de début.", "Nœud 'end_task_send_confirmation_push_via_email' (endEvent) est inaccessible depuis les événements de début."]

```

### m04_change_task_actor — Modification (change_task_actor)
```
V1: Timeout dépassé
```


## Suspicions de perte d'identifiants (à vérifier manuellement)

- `a03_insert_conditional_inclusive` : flow_report_send_technician
- `a04_insert_parallel` : flow_compte_rendu_end, flow_entretien_compte_rendu
- `a05_insert_event_based_gateway` : flow_confirmer_end
- `a06_insert_complex_gateway` : flow_evaluation_approve, flow_soumission_evaluation, task_evaluation_comite
- `a08_add_actor_internal_lane` : flow_confirmer_end
- `a09_add_actor_external_pool` : flow_magasin_prepare_to_end
- `a10_add_subprocess_embedded` : flow_agent_end
- `a16_add_signal_event` : end_process_completed, flow_correct_end, flow_detect_correct, flow_start_detect, start_detect_defect, task_correct_defect, task_detect_defect
- `a17_add_escalation_event` : flow_deposit_to_treat
- `a21_add_multi_instance` : flow_evaluate_to_end
- `a22_add_data_object` : flow_check_to_end
- `d01_delete_task_simple` : flow_order_sms, flow_sms_shipping, task_send_sms
- `d02_delete_gateway_branch` : end_poste_alternatif_propose, flow_decision_partial_match, flow_poste_alternatif_end, task_proposer_poste_alternatif
- `d05_delete_subprocess` : end_traiter_reclamation, flow_compenser_end, flow_evaluer_compenser, flow_start_verifier, flow_verifier_evaluer, start_traiter_reclamation, task_evaluer_validite, task_proposer_compensation, task_verifier_historique
- `d07_delete_data_object` : assoc_supplier_db_check