"""
Types d'actions pour le logging structuré.
"""
from enum import Enum


class ActionType(Enum):
    """Actions enregistrées dans le log expérimental."""
    
    ANALYSIS = "ANALYSIS"
    GENERATION = "GENERATION"
    DEBUG = "DEBUG"
    FIX = "FIX"
