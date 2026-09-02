"""
Configuration centralisée pour le BPMN-Agent Enterprise.
"""

# Modèle LLM par défaut
DEFAULT_MODEL = "mistral-small-latest"

# Température pour la reproductibilité
TEMPERATURE = 0.1

# Limites de tentatives
MAX_PROCESS_DESCRIPTION_RETRIES = 3
MAX_SELF_HEALING_ATTEMPTS = 3

# Niveau de trace
TRACE_LEVEL = "INFO"  # "INFO", "DEBUG", "WARNING", "ERROR"

# Chemin du fichier de log expérimental
LOG_FILE_PATH = "logs/experiment_data.json"
