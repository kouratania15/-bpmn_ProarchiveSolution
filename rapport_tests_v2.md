# Rapport de tests BPMN v2 (nouveaux scénarios) — 2026-09-05 03:34

**Résultat global : 4/5 réussis, 1/5 échoués**

| # | Test | Notion testée | Statut | Durée |
|---|------|----------------|--------|-------|
| 1 | `v2_test05_gateway_inclusif` | Gateway inclusif (OR) | ✅ OK | 84.5s |
| 2 | `v2_test07_multi_acteurs` | Plusieurs acteurs / lanes | ✅ OK | 55.9s |
| 3 | `v2_test11_conditions_combinees` | Conditions combinées (3 branches) | ✅ OK | 55.4s |
| 4 | `v2_test19_escalation` | Événement d'escalade | ❌ ÉCHEC | 103.5s |
| 5 | `v2_test22_conditional_event` | Événement conditionnel | ✅ OK | 51.5s |

## Détails des échecs

### v2_test19_escalation — Événement d'escalade

```
[TRACE] [1/8] ▶ Génération du Process Description
[TRACE] [2/8] ▶ Validation du Process Description
[TRACE] [3/8] ▶ Génération du Logic-Core depuis le Process Description
[TRACE] [4/8] ▶ Validation du Logic-Core
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (2/5)
[TRACE] [5/8] ⚠️ Self-healing sans progrès à la tentative 2 — retour à la génération initiale du Logic-Core (redétection des acteurs)
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (2/5)
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (3/5)
[TRACE] [5/8] ❌ Self-healing arrêté : aucune amélioration à la tentative 3 (1 -> 4 erreurs)

❌ Erreur : Logic-Core invalide apres aucune amélioration détectée entre deux tentatives : ["Le nœud 'task_resoudre_incident_technicien_1' (task) a 2 sequenceFlow sortants alors qu'il n'est pas un gateway. Seul un gateway peut porter plusieurs branches : insérer un exclusiveGateway/inclusiveGateway juste après ce nœud et y rattacher ces branches.", "Le nœud 'task_traiter_incident' (Traiter un incident informatique) est atteint depuis 2 gateways différents et non liés (['task_prendre_charge_split_gw', 'timer_4h_split_gw']), pas depuis les branches sœurs d'un même gateway. C'est probablement la fusion erronée de deux mentions textuelles distinctes (ex: deux 'informer le client' à des étapes différentes) en un seul nœud partagé — séparer en autant de nœuds distincts que d'occurrences textuelles (cf. section 2 de SKILL.md).", "GATEWAY-002: ExclusiveGateway 'task_prendre_charge_split_gw' doit avoir des branches décisionnelles cohérentes (libellées ou conditionnées).", "Passerelle exclusive XOR 'task_prendre_charge_split_gw' : branches sans libellé ni condition : ['flow_prendre_charge_fin', 'flow_timer_4h_to_resoudre_2']"]
```
