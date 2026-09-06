# Rapport de tests BPMN v2 (nouveaux scénarios) — 2026-09-05 16:58

**Résultat global : 2/4 réussis, 2/4 échoués**

| # | Test | Notion testée | Statut | Durée |
|---|------|----------------|--------|-------|
| 1 | `v2_test23_event_subprocess` | Sous-processus événementiel (interruptif) | ❌ ÉCHEC | 168.5s |
| 2 | `v2_test24_call_activity` | Call Activity (processus réutilisable) | ✅ OK | 149.2s |
| 3 | `v2_test25_adhoc_subprocess` | Sous-processus Ad-Hoc (ordre libre) | ✅ OK | 62.3s |
| 4 | `v2_test26_transactional_subprocess` | Sous-processus transactionnel | ❌ ÉCHEC | 2837.6s |

## Détails des échecs

### v2_test23_event_subprocess — Sous-processus événementiel (interruptif)

```
[TRACE] [1/8] ▶ Génération du Process Description
[TRACE] [2/8] ▶ Validation du Process Description
[TRACE] [2/8] ⚠️ Correction du Process Description (1/3)
[TRACE] [3/8] ▶ Génération du Logic-Core depuis le Process Description
[TRACE] [4/8] ▶ Validation du Logic-Core
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ⚠️ Self-healing sans progrès à la tentative 1 — retour à la génération initiale du Logic-Core (redétection des acteurs)
[TRACE] [5/8] ⚠️ Self-healing Logic-Core (1/5)
[TRACE] [5/8] ❌ Self-healing arrêté : aucune amélioration à la tentative 1 (1 -> 1 erreurs)

❌ Erreur : Logic-Core invalide apres aucune amélioration détectée entre deux tentatives : ["Flux 'flow_message_annulation' (messageFlow) : source 'pool_client' introuvable."]
```

### v2_test26_transactional_subprocess — Sous-processus transactionnel

```
Timeout dépassé (300s)
```
