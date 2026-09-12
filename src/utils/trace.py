"""
Système de traces terminal pour l'observabilité du pipeline.
"""
import builtins
import sys
from typing import Optional


def print(*args, **kwargs):
    """Remplace le print() du module : une trace console est cosmétique, elle
    ne doit jamais faire planter la vraie logique métier qui l'entoure à
    cause d'un caractère (€, ≤, etc. généré par le LLM) hors du codepage de
    la console (ex: cp1252 sous Windows, notamment via uvicorn --reload dont
    le sous-processus n'hérite pas toujours d'un sys.stdout.reconfigure()
    fait au niveau du process principal)."""
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe_args = [
            a.encode(encoding, errors="replace").decode(encoding, errors="replace") if isinstance(a, str) else a
            for a in args
        ]
        builtins.print(*safe_args, **kwargs)


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
