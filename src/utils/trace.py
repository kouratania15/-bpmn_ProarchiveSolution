"""
Système de traces terminal pour l'observabilité du pipeline.
"""
import sys
from typing import Optional

# Configuration globale
TRACE_ENABLED = True


def set_trace_level(level: str) -> None:
    """Configure le niveau de trace."""
    global TRACE_ENABLED
    TRACE_ENABLED = level in ("INFO", "DEBUG")


def print_trace(
    step_num: int,
    total_steps: int,
    message: str,
    level: str = "INFO"
) -> None:
    """
    Affiche une trace formatée dans le terminal.
    
    Args:
        step_num: Numéro de l'étape
        total_steps: Nombre total d'étapes
        message: Message à afficher
        level: Niveau de trace (INFO, DEBUG, WARNING, ERROR)
    """
    if not TRACE_ENABLED:
        return
    
    symbols = {
        "INFO": "▶",
        "DEBUG": "⚙️",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "SUCCESS": "✅",
    }
    
    symbol = symbols.get(level, "•")
    trace_msg = f"[TRACE] [{step_num}/{total_steps}] {symbol} {message}"
    print(trace_msg, file=sys.stderr)


def print_step(step_num: int, total_steps: int, message: str) -> None:
    """Alias pour print_trace avec niveau INFO."""
    print_trace(step_num, total_steps, message, level="INFO")


def print_success(message: str) -> None:
    """Affiche un message de succès."""
    print(f"✅ {message}", file=sys.stderr)


def print_error(message: str) -> None:
    """Affiche un message d'erreur."""
    print(f"❌ {message}", file=sys.stderr)
