# BPMN Agent Enterprise (Architecture Python + Mistral AI + PyELK)


> Moteur agentique 100% Python dédié à l'extraction d'intentions de processus, la validation de *Soundness*, le layout géométrique hiérarchique (*pyelk*) et la génération de diagrammes BPMN 2.0 XML conformes OMG.
>
> Utilisable en **CLI** (génération/amendement en local, sans base de données) ou via une **interface web de type chat** (FastAPI + frontend statique) avec **versioning MySQL** : un processus peut être créé une première fois puis évoluer par des instructions de modification en langage naturel successives, avec conservation d'un historique complet des versions (voir sections dédiées plus bas).

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
             │  • Réparations mécaniques déterministes         │
             │    (artefacts, croisements de pools, gateways)  │
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

La validation (étape 2) répare automatiquement, sans appel LLM, les erreurs structurelles les plus fréquentes (ex : un artefact relié par erreur à un flux de séquence, un flux traversant deux pools) avant même de solliciter la boucle de self-healing — celle-ci ne traite que ce qui nécessite un vrai jugement métier.

---

## 📁 Structure Complète du Projet

```
bpmn-agent/
├── SKILL.md                          # Directives expertes BPMN 2.0 (System Prompt LLM)
├── requirements.txt                  # Dépendances Python (mistralai, pyelk, fastapi, mysql-connector-python...)
├── schema.sql                        # Schéma MySQL (tables processes / process_versions)
├── .env.example                      # Modèle de configuration (clé Mistral + identifiants MySQL)
├── schema/
│   ├── logic-core.schema.json        # Schéma JSON formel validant l'intégrité du graphe
│   ├── process-description.schema.json
│   ├── structured-analysis.schema.json
│   └── amendment-intent.schema.json
├── src/
│   ├── config.py                     # Constantes centralisées (modèle par défaut, limites de tentatives)
│   ├── models/action.py              # Types d'actions journalisées (logging structuré)
│   └── utils/                        # Logger applicatif + traçage d'exécution
├── scripts/
│   ├── llm_agent.py                  # Agent Mistral AI (Pass 1 Extraction + Pass 2 Self-Healing)
│   ├── validate.py                   # Validateur de soundness + réparations mécaniques automatiques
│   ├── layout.py                     # Moteur géométrique pyelk / Sugiyama avec swimlanes
│   ├── bpmn_xml.py                   # Générateur XML BPMN 2.0 complet + BPMNDI
│   ├── db.py                         # Persistance MySQL du versioning (CRUD process/versions)
│   ├── pipeline.py                   # Orchestrateur CLI (génération + mode versioning)
│   ├── api.py                        # API web FastAPI (chat) exposant le même pipeline
│   └── response_formatter.py         # Construction du texte affiché dans le chat (sans appel LLM)
└── static/
    ├── index.html                    # Frontend de l'interface chat
    ├── app.js
    └── styles.css
```

---

## ⚙️ Installation

```bash
cd bpmn-agent
pip install -r requirements.txt
```

Copier `.env.example` en `.env` et renseigner vos identifiants (clé API Mistral, et identifiants MySQL — obligatoires pour l'interface web, optionnels pour la CLI en mode fichiers locaux) :
```bash
cp .env.example .env
```
```env
MISTRAL_API_KEY=votre_cle_mistral_ici

# Nécessaire pour l'interface web (chat) et le mode versioning CLI (voir plus bas)
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=votre_utilisateur_mysql
DB_PASSWORD=votre_mot_de_passe_mysql
DB_NAME=bpmn
```

Les tables MySQL sont créées automatiquement au premier usage (aucune migration manuelle à lancer).

---

## 💬 Interface Web (Chat)

Lancer le serveur depuis la racine du projet :
```bash
uvicorn scripts.api:app --reload
```
Puis ouvrir `http://127.0.0.1:8000` dans un navigateur — le frontend statique (`static/`) y est servi directement.

Chaque processus créé via le chat est automatiquement persisté en base MySQL (une version 1), et chaque instruction de modification envoyée ensuite crée une nouvelle version sans jamais réécrire l'historique. Ce mode requiert donc `DB_HOST`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` renseignés dans `.env`.

Endpoints exposés (`scripts/api.py`) :

| Méthode | Route | Rôle |
|---|---|---|
| `GET`  | `/processes` | Liste des processus existants (id, nom, date de création) |
| `POST` | `/processes` | Génère un nouveau processus à partir d'un texte libre |
| `POST` | `/processes/{id}/amend` | Applique une instruction de modification en langage naturel, crée une nouvelle version |
| `GET`  | `/processes/{id}` | Détail d'un processus (dernière version) |
| `DELETE` | `/processes/{id}` | Supprime un processus et son historique |

En cas d'échec de génération, la réponse reste `HTTP 200` avec un message clair côté chat (`ok: false`) — rien n'est jamais persisté en base tant que le schéma généré n'est pas valide.

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
Permet d'ajouter ou de modifier des étapes en cours d'entretien sans modifier les identifiants existants, **sans passer par la base de données** (fichier Logic-Core local) :
```bash
python scripts/pipeline.py \
  "Ajouter une double validation par le directeur si le montant dépasse 10 000 euros" \
  --logic-core commande.logic-core.json \
  --out commande_v2.bpmn
```

### 4. Compilation Hors-Ligne (Depuis un Logic-Core JSON existant)
```bash
python scripts/pipeline.py --from-json mon_processus.logic-core.json --out output.bpmn
```

### 5. Mode Versioning MySQL (Historique de Processus)

Contrairement au mode amendement incrémental (§3, purement local via fichier), ce mode persiste chaque version en base de données et conserve l'historique complet d'un processus identifié par un UUID — c'est le même mécanisme qu'utilise l'interface web.

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

### Options additionnelles

| Option | Effet |
|---|---|
| `--model <nom>` | Modèle Mistral AI à utiliser (défaut : voir `src/config.py`) |
| `--no-heal` | Désactive la boucle d'auto-réparation (self-healing) — utile pour observer la sortie brute du LLM |
| `--quiet` / `-q` | Supprime les logs d'étapes intermédiaires |

---

## 🧪 Vérifier son installation

Le projet ne fournit pas encore de suite de tests automatisés versionnée. Pour vérifier rapidement que tout est bien configuré :

- **CLI** : lancer l'exemple de la section 1 ci-dessus et vérifier qu'un fichier `.bpmn` valide est produit.
- **Interface web** : lancer `uvicorn scripts.api:app --reload`, créer un processus via le chat et vérifier qu'il apparaît bien dans `GET /processes`.
