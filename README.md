# BPMN Agent Enterprise (Architecture Python + Mistral AI + PyELK)


> Moteur agentique 100% Python dédié à l'extraction d'intentions de processus, la validation de *Soundness*, le layout géométrique hiérarchique (*pyelk*) et la génération de diagrammes BPMN 2.0 XML conformes OMG.

---

## 🏛️ Schéma d'Architecture du Pipeline

```
                       Transcription / Entretien Analyste
                                      │
                                      ▼
             ┌─────────────────────────────────────────────────┐
             │            1. EXTRACTION SÉMANTIQUE             │
             │           (llm_agent.py / Mistral AI)           │
             │      System Prompt: SKILL.md (OMG BPMN 2.0)     │
             └────────────────────────┬────────────────────────┘
                                      │
                                      ▼
             ┌─────────────────────────────────────────────────┐
             │                 LOGIC-CORE JSON                 │
             │       Conforme à schema/logic-core.schema.json  │
             └────────────────────────┬────────────────────────┘
                                      │
                                      ▼
             ┌─────────────────────────────────────────────────┐
             │       2. VALIDATEUR DE SOUNDNESS MÉTIER         │
             │                 (validate.py)                   │
             │  • Connexité BFS/DFS (Liveness / Deadlock-free) │
             │  • Respect des Swimlanes (Seq vs Msg Flows)     │
             │  • Détection de Boundary Events & Orphelins     │
             └───────────────┬─────────────────▲───────────────┘
                             │                 │
                      [Erreurs Détectées]      │ [Auto-Correction]
                             │                 │
                             ▼                 │
             ┌─────────────────────────────────┴───────────────┐
             │         BOUCLE DE SELF-HEALING (Pass 2)         │
             │     (llm_agent.py : self_heal_logic_core)       │
             └─────────────────────────────────────────────────┘
                             │
                      [Graphe Valide]
                             │
                             ▼
             ┌─────────────────────────────────────────────────┐
             │          3. DISPOSITION GÉOMÉTRIQUE             │
             │       (layout.py : pyelk / Sugiyama Layered)    │
             │  • Partitionnement des Pools & Lanes (Bounds)   │
             │  • Calcul d'ancrage des Boundary Events         │
             │  • Routage orthogonal des Waypoints à 90°       │
             └────────────────────────┬────────────────────────┘
                                      │
                                      ▼
             ┌─────────────────────────────────────────────────┐
             │         4. SÉRIALISATION BPMN 2.0 XML           │
             │                (bpmn_xml.py)                    │
             │  • Éléments sémantiques (bpmn:process, laneSet) │
             │  • Métadonnées BPMNDI (bpmndi:BPMNPlane, Shape) │
             └────────────────────────┬────────────────────────┘
                                      │
                                      ▼
               Fichier .bpmn (Compatible Camunda, Signavio, bpmn.io)
```

---

## 📁 Structure Complète du Projet

```
bpmn-agent/
├── SKILL.md                          # Directives expertes BPMN 2.0 (System Prompt LLM)
├── requirements.txt                  # Dépendances Python (mistralai, pyelk, pytest)
├── schema/
│   └── logic-core.schema.json        # Schéma JSON formel validant l'intégrité du graphe
├── scripts/
│   ├── llm_agent.py                  # Agent Mistral AI (Pass 1 Extraction + Pass 2 Self-Healing)
│   ├── validate.py                   # Validateur de soundness topologique et sémantique
│   ├── layout.py                     # Moteur géométrique pyelk / Sugiyama avec swimlanes
│   ├── bpmn_xml.py                   # Générateur XML BPMN 2.0 complet + BPMNDI
│   └── pipeline.py                   # Orchestrateur CLI en ligne de commande
├── examples/
│   ├── sample_order_fulfillment.json # Cas multi-pools, lanes, boundary timer event, message flow
│   ├── sample_order_fulfillment.bpmn # XML BPMN 2.0 généré
│   ├── sample_insurance_claim.json   # Cas passerelles parallèles AND (Fork/Join)
│   └── sample_insurance_claim.bpmn   # XML BPMN 2.0 généré
└── tests/
    └── test_pipeline.py              # Suite complète de tests unitaires et d'intégration
```

---

## ⚙️ Installation

```bash
cd bpmn-agent
pip install -r requirements.txt
```

Configuration de la clé API Mistral :
```bash
export MISTRAL_API_KEY="votre_cle_mistral_ici"
```

---

## 💻 Guide d'Utilisation CLI

### 1. Génération directe depuis une phrase ou transcription
```bash
python scripts/pipeline.py \
  "Quand une commande arrive, le service commercial vérifie le stock. Si les articles sont disponibles, on demande le débit bancaire avec un délai max de 24h. Si le paiement réussit, la logistique expédie le colis." \
  --out commande.bpmn
```

### 2. Depuis un fichier de transcription d'interview
```bash
python scripts/pipeline.py --file entretien_client.txt --out processus_metier.bpmn
```

### 3. Mode Amendement Incrémental (Temps Réel en Atelier)
Permet d'ajouter ou de modifier des étapes en cours d'entretien sans modifier les identifiants existants :
```bash
python scripts/pipeline.py \
  "Ajouter une double validation par le directeur si le montant dépasse 10 000 euros" \
  --logic-core commande.logic-core.json \
  --out commande_v2.bpmn
```

### 4. Compilation Hors-Ligne (Depuis un Logic-Core JSON existant)
```bash
python scripts/pipeline.py --from-json examples/sample_order_fulfillment.json --out output.bpmn
```

---

## 🧪 Exécution des Tests

```bash
python tests/test_pipeline.py
```
