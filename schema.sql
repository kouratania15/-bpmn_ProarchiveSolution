-- Schéma MySQL pour le versioning des schémas BPMN générés par le pipeline.
-- Référence canonique : test_db_connection.py et db.py doivent rester cohérents avec ce fichier.

CREATE TABLE IF NOT EXISTS processes (
    id CHAR(36) PRIMARY KEY,               -- UUID
    name VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_versions (
    id CHAR(36) PRIMARY KEY,               -- UUID
    process_id CHAR(36) NOT NULL,
    version_number INT NOT NULL,
    instruction_text TEXT NOT NULL,        -- la phrase qui a produit cette version
    logic_core_json JSON NOT NULL,         -- l'état complet du Logic-Core à cette version
    bpmn_xml LONGTEXT NOT NULL,            -- le XML BPMN généré pour cette version
    parent_version_id CHAR(36) NULL,       -- NULL si c'est la version initiale
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (process_id) REFERENCES processes(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_version_id) REFERENCES process_versions(id) ON DELETE SET NULL,
    UNIQUE KEY unique_version_per_process (process_id, version_number)
);

CREATE INDEX idx_process_versions_process_id ON process_versions(process_id);
