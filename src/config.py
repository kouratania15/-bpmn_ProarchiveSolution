"""
Configuration centralisée pour le BPMN-Agent Enterprise.
"""

# Modèle LLM par défaut
# NOTE : "mistral-small-latest" est rate-limité à 0 req/min sur les comptes sans
# plan payant actif. "open-mistral-nemo" (comme les modèles ministral-*) reste
# accessible gratuitement sans carte bancaire.
DEFAULT_MODEL = "open-mistral-nemo"

# Température pour la reproductibilité
TEMPERATURE = 0.1

# Limites de tentatives
MAX_PROCESS_DESCRIPTION_RETRIES = 3
# +2 par rapport à la valeur initiale (3) pour laisser une chance de plus au
# self-healing sur les textes complexes (boucles + plusieurs décisions), sans
# multiplier excessivement les appels API sur un compte à quota limité.
MAX_SELF_HEALING_ATTEMPTS = 5

# Niveau de trace
TRACE_LEVEL = "INFO"  # "INFO", "DEBUG", "WARNING", "ERROR"

# Chemin du fichier de log expérimental
LOG_FILE_PATH = "logs/experiment_data.json"
