# BPMN Agent Enterprise (Architecture Python + Mistral AI + PyELK)


> Moteur agentique 100% Python dédié à l'extraction d'intentions de processus, la validation de *Soundness*, le layout géométrique hiérarchique (*pyelk*) et la génération de diagrammes BPMN 2.0 XML conformes OMG.
>
> Inclut un mode de **versioning MySQL** : un processus peut être créé une première fois puis évoluer par des instructions de modification en langage naturel successives, avec conservation d'un historique complet des versions (voir section dédiée plus bas).

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
├── requirements.txt                  # Dépendances Python (mistralai, pyelk, mysql-connector-python, pytest)
├── schema/
│   └── logic-core.schema.json        # Schéma JSON formel validant l'intégrité du graphe
├── schema.sql                        # Schéma MySQL (tables processes / process_versions)
├── .env.example                      # Modèle de configuration (clé Mistral + identifiants MySQL)
├── test_db_connection.py             # Script isolé de vérification de la connexion MySQL
├── scripts/
│   ├── llm_agent.py                  # Agent Mistral AI (Pass 1 Extraction + Pass 2 Self-Healing)
│   ├── validate.py                   # Validateur de soundness topologique et sémantique
│   ├── layout.py                     # Moteur géométrique pyelk / Sugiyama avec swimlanes
│   ├── bpmn_xml.py                   # Générateur XML BPMN 2.0 complet + BPMNDI
│   ├── db.py                         # Persistance MySQL du versioning (CRUD process/versions)
│   └── pipeline.py                   # Orchestrateur CLI (génération + mode versioning)
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

Copier `.env.example` en `.env` et renseigner vos identifiants (clé API Mistral, et identifiants MySQL si vous utilisez le mode versioning) :
```bash
cp .env.example .env
```
```env
MISTRAL_API_KEY=votre_cle_mistral_ici

# Uniquement nécessaire pour le mode versioning (voir plus bas)
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=votre_utilisateur_mysql
DB_PASSWORD=votre_mot_de_passe_mysql
DB_NAME=bpmn
```

Le mode versioning nécessite un serveur MySQL joignable ; les tables sont créées automatiquement au premier usage. Pour vérifier la connexion isolément avant de lancer le pipeline :
```bash
python test_db_connection.py
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

### 5. Mode Versioning MySQL (Historique de Processus)

Contrairement au mode amendement incrémental (§3, purement local via fichier), ce mode persiste chaque version en base de données et conserve l'historique complet d'un processus identifié par un UUID.

**Création initiale** — sauvegarde une version 1 en base :
```bash
python scripts/pipeline.py \
  "Le service RH reçoit une demande de congé. Le système appelle le processus standard de validation hiérarchique." \
  --process-name "Processus de congé" --out congé_v1.bpmn
```
La commande affiche l'UUID du process créé, à réutiliser pour toute modification ultérieure.

**Modification versionnée** — charge la dernière version en base, applique l'instruction, sauvegarde une nouvelle version :
```bash
python scripts/pipeline.py \
  "Ajoute une étape où le manager doit approuver la demande avant l'envoi au système de validation hiérarchique." \
  --process-id <UUID_du_process> --out congé_v2.bpmn
```

Chaque modification passe par exactement le même pipeline de validation/self-healing que la génération initiale — aucun raccourci. Les identifiants (nœuds et flux) non concernés par la demande sont conservés d'une version à l'autre.

Consultation de l'historique et des versions via `scripts/db.py` (`get_version_history`, `get_version`, `get_latest_version`).

Sans `--process-id` ni `--process-name`, le comportement reste celui des modes 1 à 4 (aucune interaction avec MySQL).

---

## 🧪 Exécution des Tests

```bash
python tests/test_pipeline.py
```
