"""
Script isolé et autonome : prouve que la connexion Python -> MySQL fonctionne,
INDÉPENDAMMENT du reste du projet (pipeline BPMN, etc.).

Ce script est volontairement commenté ligne par ligne, comme une leçon, pour
expliquer CHAQUE étape à quelqu'un qui n'a jamais connecté Python à une base
de données. Une fois qu'il tourne sans erreur, on branchera le vrai pipeline
BPMN dessus (db.py, versioning des schémas).

Lancer avec :  python test_db_connection.py
"""

# ---------------------------------------------------------------------------
# ÉTAPE 0 — Les imports
# ---------------------------------------------------------------------------
# `os` : pour lire les variables d'environnement (os.environ) une fois que
#        python-dotenv les a chargées depuis le fichier .env.
# `uuid` : pour générer des identifiants uniques (UUID) — nos tables utilisent
#          des UUID en clé primaire plutôt que des entiers auto-incrémentés,
#          car un process/une version doit avoir un id stable et imprévisible.
# `json` : pour transformer un dict Python en texte JSON (et inversement),
#          car la colonne `logic_core_json` est de type JSON côté MySQL.
# `datetime` n'est PAS importé explicitement : mysql-connector-python
#          convertit déjà automatiquement les colonnes DATETIME en objets
#          `datetime.datetime` Python, utilisables directement.
# `mysql.connector` : le pilote (driver) qui sait parler le protocole réseau
#          de MySQL. C'est la bibliothèque tierce installée via
#          `pip install mysql-connector-python`.
# `dotenv.load_dotenv` : lit le fichier .env à la racine du projet et injecte
#          son contenu dans les variables d'environnement du processus Python
#          en cours — c'est ce qui permet de ne JAMAIS écrire d'identifiants
#          en dur dans le code.
import os
import uuid
import json

import mysql.connector
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# ÉTAPE 1 — Charger les identifiants depuis .env
# ---------------------------------------------------------------------------
# `load_dotenv()` cherche un fichier nommé ".env" dans le répertoire courant
# (ou les répertoires parents) et charge chaque ligne "CLE=valeur" comme une
# variable d'environnement. Après cet appel, `os.environ["DB_HOST"]` (etc.)
# devient disponible, exactement comme si on avait fait `set DB_HOST=...`
# dans le terminal avant de lancer Python.
load_dotenv()

# `os.environ["X"]` lève une erreur explicite si la variable X est absente —
# préférable à un mot de passe vide qui échouerait silencieusement plus loin.
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ["DB_PORT"])
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]


def main() -> None:
    # -----------------------------------------------------------------------
    # ÉTAPE 2 — Ouvrir la connexion
    # -----------------------------------------------------------------------
    # `mysql.connector.connect(...)` ouvre une connexion TCP vers le serveur
    # MySQL (host:port), s'authentifie avec user/password, puis sélectionne
    # la base `database` donnée. L'objet `conn` retourné représente CETTE
    # session ouverte avec le serveur — tant qu'elle est ouverte, le serveur
    # réserve des ressources pour nous (d'où l'importance de la fermer à la
    # fin, cf. étape 7).
    conn = mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
    )
    print(f"Connexion à MySQL réussie (host={DB_HOST}, database={DB_NAME})")

    # -----------------------------------------------------------------------
    # ÉTAPE 3 — Le curseur : l'objet qui exécute des requêtes SQL
    # -----------------------------------------------------------------------
    # La connexion (`conn`) est le "tuyau" vers le serveur ; le curseur
    # (`cursor`) est l'objet qu'on utilise pour ENVOYER des requêtes SQL à
    # travers ce tuyau et RÉCUPÉRER les résultats. On peut ouvrir plusieurs
    # curseurs sur une même connexion, mais un seul suffit ici.
    # `dictionary=True` fait que les lignes lues (fetchone/fetchall) sont
    # retournées comme des dict {"nom_colonne": valeur} plutôt que des tuples
    # positionnels — beaucoup plus lisible pour la suite du script.
    cursor = conn.cursor(dictionary=True)

    # -----------------------------------------------------------------------
    # ÉTAPE 4 — Créer les tables si elles n'existent pas encore
    # -----------------------------------------------------------------------
    # `cursor.execute(sql)` envoie UNE requête SQL au serveur et attend sa
    # réponse. `CREATE TABLE IF NOT EXISTS` est idempotent : si la table
    # existe déjà (ex: on relance ce script une 2e fois), MySQL ne fait rien
    # et ne lève pas d'erreur.
    #
    # Ces deux CREATE TABLE sont la version autonome (recopiée ici pour que
    # ce script n'ait besoin d'AUCUN autre fichier du projet) du schéma
    # canonique défini dans schema.sql.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processes (
            id CHAR(36) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS process_versions (
            id CHAR(36) PRIMARY KEY,
            process_id CHAR(36) NOT NULL,
            version_number INT NOT NULL,
            instruction_text TEXT NOT NULL,
            logic_core_json JSON NOT NULL,
            bpmn_xml LONGTEXT NOT NULL,
            parent_version_id CHAR(36) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (process_id) REFERENCES processes(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_version_id) REFERENCES process_versions(id) ON DELETE SET NULL,
            UNIQUE KEY unique_version_per_process (process_id, version_number)
        )
    """)
    print("Tables vérifiées/créées : processes, process_versions")

    # -----------------------------------------------------------------------
    # ÉTAPE 5 — Insérer un process factice et une version factice
    # -----------------------------------------------------------------------
    # `uuid.uuid4()` génère un identifiant unique aléatoire (128 bits) — on le
    # convertit en `str(...)` car la colonne SQL est un CHAR(36), pas un type
    # UUID natif (MySQL n'en a pas).
    #
    # Le point CRITIQUE ici : on utilise des PLACEHOLDERS (`%s`) dans la
    # requête, et on passe les valeurs réelles à part, dans un tuple, comme
    # 2e argument de `cursor.execute(sql, valeurs)`. C'est ce qu'on appelle
    # une "requête paramétrée". NE JAMAIS construire la requête par
    # concaténation de chaînes (ex: f"...VALUES ('{name}')") : un nom de
    # process contenant une apostrophe casserait la requête, et pire, un
    # texte malveillant pourrait injecter du SQL arbitraire (injection SQL).
    # Le driver se charge d'échapper correctement chaque valeur à notre place.
    process_id = str(uuid.uuid4())
    process_name = "Test connexion"
    cursor.execute(
        "INSERT INTO processes (id, name) VALUES (%s, %s)",
        (process_id, process_name),
    )
    print(f'Process de test inséré : id={process_id}, name="{process_name}"')

    version_id = str(uuid.uuid4())
    instruction_text = "Ceci est un test de connexion"
    # `json.dumps(...)` transforme le dict Python en texte JSON, car la
    # colonne `logic_core_json` attend une CHAÎNE de caractères respectant la
    # syntaxe JSON — MySQL la valide et la stocke sous son type JSON natif.
    logic_core_json = json.dumps({"nodes": [], "test": True})
    bpmn_xml_placeholder = "<bpmn:definitions></bpmn:definitions>"
    cursor.execute(
        """
        INSERT INTO process_versions
            (id, process_id, version_number, instruction_text, logic_core_json, bpmn_xml, parent_version_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (version_id, process_id, 1, instruction_text, logic_core_json, bpmn_xml_placeholder, None),
    )
    print("Version 1 insérée pour ce process.")

    # -----------------------------------------------------------------------
    # ÉTAPE 6 — Valider les écritures avec commit()
    # -----------------------------------------------------------------------
    # Par défaut, mysql-connector-python ouvre une TRANSACTION implicite :
    # les INSERT ci-dessus ne sont pour l'instant que des changements en
    # attente, visibles uniquement dans cette connexion. `conn.commit()` les
    # rend définitifs et visibles par toute autre connexion à la base. Sans
    # ce commit, un `conn.close()` (ou un crash) annulerait silencieusement
    # tout ce qui vient d'être inséré (rollback implicite).
    conn.commit()

    # -----------------------------------------------------------------------
    # ÉTAPE 7 — Relire la version depuis la base et l'afficher
    # -----------------------------------------------------------------------
    # On relit volontairement DEPUIS LA BASE (pas depuis les variables
    # Python `instruction_text`/`logic_core_json` déjà en mémoire) pour
    # prouver que les données ont bien été écrites ET relues correctement,
    # pas seulement que le code Python fonctionne en mémoire.
    cursor.execute(
        "SELECT instruction_text, logic_core_json, created_at "
        "FROM process_versions WHERE process_id = %s AND version_number = %s",
        (process_id, 1),
    )
    # `fetchone()` récupère UNE ligne de résultat (celle qu'on attend, vu
    # l'UNIQUE KEY (process_id, version_number)) sous forme de dict grâce à
    # `dictionary=True` défini à l'étape 3.
    row = cursor.fetchone()

    print("Lecture de la version 1 :")
    print(f"  instruction_text = \"{row['instruction_text']}\"")
    # La colonne JSON peut revenir soit comme une chaîne JSON, soit déjà
    # décodée en dict/list selon la version du connecteur — on gère les deux
    # cas pour un affichage cohérent.
    logic_core_value = row["logic_core_json"]
    if isinstance(logic_core_value, str):
        logic_core_value = json.loads(logic_core_value)
    print(f"  logic_core_json  = {json.dumps(logic_core_value)}")
    print(f"  created_at        = {row['created_at']}")

    # -----------------------------------------------------------------------
    # ÉTAPE 8 — Fermer proprement le curseur et la connexion
    # -----------------------------------------------------------------------
    # `cursor.close()` libère les ressources associées à ce curseur côté
    # driver. `conn.close()` ferme la connexion TCP avec le serveur MySQL et
    # libère la session côté serveur. Toujours fermer dans cet ordre (curseur
    # puis connexion) une fois qu'on n'a plus besoin de la base.
    cursor.close()
    conn.close()
    print("Connexion fermée proprement.")


if __name__ == "__main__":
    main()
