# Rapport de tests — Versioning (amendements) — 2026-09-12 20:34

**Résultat global : 15/20 scénarios complets (V1+V2+V3) réussis**

| # | Test | Notion | V1 | V2 | V3 | IDs perdus V1→V2 | IDs perdus V2→V3 |
|---|------|--------|----|----|----|--------------------|--------------------|
| 1 | `v01_insertion_simple` | Insertion d'une tâche entre deux existantes | OK | OK | OK | flow_scanner_expédier | flow_poider_expédier |
| 2 | `v02_suppression` | Suppression pure d'une étape existante | OK | OK | ECHEC | flow_commande_sms, flow_sms_expedition, task_sms_confirmation | n/a |
| 3 | `v03_renommage` | Renommage d'une tâche sans changer la structure | OK | OK | OK | - | - |
| 4 | `v04_remplacement_tache` | Remplacement d'une tâche par une autre | OK | ECHEC | ECHEC | n/a | n/a |
| 5 | `v05_lineaire_vers_exclusif` | Transformation d'un flux linéaire en gateway exclusif | OK | OK | ECHEC | end_event, flow_approve_end, flow_start_submit, flow_submit_approve, task_approve_expense, task_submit_expense | n/a |
| 6 | `v06_exclusif_vers_inclusif` | Gateway inclusif introduit en amendement (2 conditions non exclusives) | OK | OK | OK | flow_send_technician_to_end | flow_gateway_to_electrician, flow_gateway_to_plumber, flow_send_technician_to_gateway, gateway_technician_type |
| 7 | `v07_ajout_acteur_interne` | Ajout d'un acteur interne (nouvelle lane) | OK | OK | OK | flow_confirmation_end | - |
| 8 | `v08_ajout_acteur_externe` | Ajout d'un acteur externe (nouvelle pool) | OK | OK | OK | flow_preparation_fin | flow_livraison_confirmation |
| 9 | `v09_ajout_boucle` | Ajout d'une boucle (retour en arrière) | ECHEC | ECHEC | ECHEC | n/a | n/a |
| 10 | `v10_ajout_parallelisme` | Ajout de parallélisme (gateway AND) | OK | OK | OK | flow_compte_rendu_decision | - |
| 11 | `v11_ajout_sous_processus` | Introduction d'un sous-processus intégré | OK | OK | OK | flow_treat_end | flow_propose_compensation_end |
| 12 | `v12_double_condition_imbriquee` | Amendement combinant deux conditions imbriquées | OK | OK | OK | flow_demande_envoie_devis | - |
| 13 | `v13_reference_inexistante` | Amendement référant à une étape qui n'existe pas (robustesse) | OK | OK | OK | flow_confirm_to_end | - |
| 14 | `v14_instruction_contradictoire` | Amendement contredisant une affirmation de la version précédente | OK | OK | OK | flow_client_to_system_task | flow_validation_to_system_task, task_system_manual_validation |
| 15 | `v15_chaine_trois_versions` | Chaîne de versions qui s'appuient les unes sur les autres | OK | OK | OK | flow_request_to_check_stock | - |
| 16 | `v16_ajout_data_object` | Ajout d'un objet de données en amendement | OK | OK | OK | flow_check_to_end | flow_register_to_end |
| 17 | `v17_ajout_timer` | Ajout d'un événement timer en amendement | OK | OK | ECHEC | flow_reply_end | n/a |
| 18 | `v18_ajout_multi_instance` | Introduction d'un multi-instance en amendement | OK | OK | OK | flow_evaluate_to_end | - |
| 19 | `v19_ajout_compensation` | Introduction d'un mécanisme de compensation en amendement | OK | OK | OK | - | flow_reserver_hotel_end |
| 20 | `v20_suppression_branche_entiere` | Suppression d'une branche entière de décision | OK | OK | OK | - | end_event_alternatif_propose, flow_alternatif_end, flow_gateway_alternatif, task_proposer_alternatif |

## Détails des échecs

### v02_suppression — Suppression pure d'une étape existante
```
V3: ssifiée : delete_task_simple
[TRACE] [1/8] ⚠️ Amendement a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [4/8] ▶ Validation du Logic-Core
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : Le self-healing a supprime des IDs existants après 3 tentatives : ['flow_commande_email', 'flow_email_expedition']

```

### v04_remplacement_tache — Remplacement d'une tâche par une autre
```
V2: ppel explicite — probablement une conséquence légitime de la demande, acceptée : ['task_envoyer_confirmation']
[TRACE] [4/8] ▶ Validation du Logic-Core
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : Le self-healing a supprime des IDs existants après 3 tentatives : ['pool_client', 'pool_system', 'process_commande']

```

### v05_lineaire_vers_exclusif — Transformation d'un flux linéaire en gateway exclusif
```
V3: rêté : aucune amélioration à la tentative 3 (2 -> 2 erreurs)

❌ Erreur : Logic-Core invalide apres aucune amélioration détectée entre deux tentatives : ["GATEWAY-002: ExclusiveGateway 'task_finance_reject_split_gw' doit avoir des branches décisionnelles cohérentes (libellées ou conditionnées).", "Passerelle exclusive XOR 'task_finance_reject_split_gw' : branches sans libellé ni condition : ['flow_finance_reject_to_notify_rejection', 'flow_finance_reject_to_notify_finance_rejection', 'flow_1']"]

```

### v09_ajout_boucle — Ajout d'une boucle (retour en arrière)
```
V1: Timeout dépassé
```

### v17_ajout_timer — Ajout d'un événement timer en amendement
```
V3: ool_support', 'process_support_ticket']
[TRACE] [5/8] ⚠️ Self-healing sans progrès à la tentative 1 — une dernière tentative avant abandon
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (2/5)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (1/3)
[TRACE] [5/8] ⚠️ Self-healing a supprimé des IDs existants — nouvelle tentative (2/3)

❌ Erreur : Le self-healing a supprime des IDs existants après 3 tentatives : ['flow_transfer_end', 'pool_support', 'process_support_ticket']

```


## Suspicions de perte d'identifiants (à vérifier manuellement)

- `v01_insertion_simple` (V1→V2) : flow_scanner_expédier
- `v01_insertion_simple` (V2→V3) : flow_poider_expédier
- `v02_suppression` (V1→V2) : flow_commande_sms, flow_sms_expedition, task_sms_confirmation
- `v05_lineaire_vers_exclusif` (V1→V2) : end_event, flow_approve_end, flow_start_submit, flow_submit_approve, task_approve_expense, task_submit_expense
- `v06_exclusif_vers_inclusif` (V1→V2) : flow_send_technician_to_end
- `v06_exclusif_vers_inclusif` (V2→V3) : flow_gateway_to_electrician, flow_gateway_to_plumber, flow_send_technician_to_gateway, gateway_technician_type
- `v07_ajout_acteur_interne` (V1→V2) : flow_confirmation_end
- `v08_ajout_acteur_externe` (V1→V2) : flow_preparation_fin
- `v08_ajout_acteur_externe` (V2→V3) : flow_livraison_confirmation
- `v10_ajout_parallelisme` (V1→V2) : flow_compte_rendu_decision
- `v11_ajout_sous_processus` (V1→V2) : flow_treat_end
- `v11_ajout_sous_processus` (V2→V3) : flow_propose_compensation_end
- `v12_double_condition_imbriquee` (V1→V2) : flow_demande_envoie_devis
- `v13_reference_inexistante` (V1→V2) : flow_confirm_to_end
- `v14_instruction_contradictoire` (V1→V2) : flow_client_to_system_task
- `v14_instruction_contradictoire` (V2→V3) : flow_validation_to_system_task, task_system_manual_validation
- `v15_chaine_trois_versions` (V1→V2) : flow_request_to_check_stock
- `v16_ajout_data_object` (V1→V2) : flow_check_to_end
- `v16_ajout_data_object` (V2→V3) : flow_register_to_end
- `v17_ajout_timer` (V1→V2) : flow_reply_end
- `v18_ajout_multi_instance` (V1→V2) : flow_evaluate_to_end
- `v19_ajout_compensation` (V2→V3) : flow_reserver_hotel_end
- `v20_suppression_branche_entiere` (V2→V3) : end_event_alternatif_propose, flow_alternatif_end, flow_gateway_alternatif, task_proposer_alternatif