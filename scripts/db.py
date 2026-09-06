"""
Persistance MySQL pour le versioning des schémas BPMN.

Chaque fonction publique ouvre sa propre connexion courte (ouvre, fait son
travail dans une transaction, commit ou rollback, ferme) — un pattern adapté
à un usage CLI/script comme ce projet, pas à un pool de connexions partagé
pour un serveur web à fort trafic.

Schéma de référence : schema.sql (racine du projet).
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def _connect() -> mysql.connector.pooling.PooledMySQLConnection | mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
    )


@contextmanager
def _transaction() -> Iterator[Any]:
    """Ouvre connexion + curseur (dict), commit si le bloc réussit, rollback sinon, ferme toujours."""
    conn = _connect()
    cur = conn.cursor(dictionary=True)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_schema() -> None:
    """Crée les tables processes / process_versions si elles n'existent pas encore (idempotent)."""
    with _transaction() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processes (
                id CHAR(36) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
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


def create_process(name: str, instruction_text: str, logic_core_json: dict[str, Any], bpmn_xml: str) -> str:
    """Crée un nouveau process ET sa version 1 (création initiale), dans une seule transaction.

    Retourne l'id (UUID str) du process créé.
    """
    process_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    with _transaction() as cur:
        cur.execute("INSERT INTO processes (id, name) VALUES (%s, %s)", (process_id, name))
        cur.execute(
            """
            INSERT INTO process_versions
                (id, process_id, version_number, instruction_text, logic_core_json, bpmn_xml, parent_version_id)
            VALUES (%s, %s, 1, %s, %s, %s, NULL)
            """,
            (version_id, process_id, instruction_text, json.dumps(logic_core_json, ensure_ascii=False), bpmn_xml),
        )
    return process_id


def add_version(
    process_id: str,
    instruction_text: str,
    logic_core_json: dict[str, Any],
    bpmn_xml: str,
    parent_version_id: str | None,
) -> str:
    """Ajoute une nouvelle version à un process existant (numéro auto-incrémenté = max+1).

    Retourne l'id (UUID str) de la version créée.
    """
    version_id = str(uuid.uuid4())
    with _transaction() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS max_version FROM process_versions WHERE process_id = %s",
            (process_id,),
        )
        next_version = cur.fetchone()["max_version"] + 1
        cur.execute(
            """
            INSERT INTO process_versions
                (id, process_id, version_number, instruction_text, logic_core_json, bpmn_xml, parent_version_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id, process_id, next_version, instruction_text,
                json.dumps(logic_core_json, ensure_ascii=False), bpmn_xml, parent_version_id,
            ),
        )
    return version_id


def _row_to_version_dict(row: dict[str, Any]) -> dict[str, Any]:
    logic_core = row["logic_core_json"]
    if isinstance(logic_core, str):
        logic_core = json.loads(logic_core)
    return {
        "version_id": row["id"],
        "process_id": row["process_id"],
        "version_number": row["version_number"],
        "instruction_text": row["instruction_text"],
        "logic_core_json": logic_core,
        "bpmn_xml": row["bpmn_xml"],
        "parent_version_id": row["parent_version_id"],
        "created_at": row["created_at"],
    }


def get_latest_version(process_id: str) -> dict[str, Any] | None:
    """Retourne la version la plus récente d'un process (logic_core_json, bpmn_xml, version_number, ...).

    Retourne None si le process n'existe pas ou n'a aucune version.
    """
    with _transaction() as cur:
        cur.execute(
            "SELECT * FROM process_versions WHERE process_id = %s ORDER BY version_number DESC LIMIT 1",
            (process_id,),
        )
        row = cur.fetchone()
    return _row_to_version_dict(row) if row else None


def get_version(process_id: str, version_number: int) -> dict[str, Any] | None:
    """Retourne une version précise et complète (JSON + XML)."""
    with _transaction() as cur:
        cur.execute(
            "SELECT * FROM process_versions WHERE process_id = %s AND version_number = %s",
            (process_id, version_number),
        )
        row = cur.fetchone()
    return _row_to_version_dict(row) if row else None


def get_version_history(process_id: str) -> list[dict[str, Any]]:
    """Retourne les métadonnées légères de toutes les versions d'un process
    (version_number, instruction_text, created_at) — sans JSON/XML complet,
    pour un affichage type "historique des conversations"."""
    with _transaction() as cur:
        cur.execute(
            """
            SELECT version_number, instruction_text, created_at
            FROM process_versions
            WHERE process_id = %s
            ORDER BY version_number ASC
            """,
            (process_id,),
        )
        return cur.fetchall()
