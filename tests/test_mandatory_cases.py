"""
Test des 6 cas obligatoires spécifiés dans la demande utilisateur (Section 15).
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest
from scripts.pipeline import run_pipeline
from scripts.validate import validate_logic_core


def test_case_1_lineaire():
    text = (
        "The customer places an order. The sales employee registers the order. "
        "The warehouse prepares the package. The employee ships the package. "
        "The customer receives the order."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    # Pas de passerelle
    gateways = [n for n in core.get("nodes", []) if "gateway" in n.get("type", "").lower()]
    assert len(gateways) == 0, f"Attendu 0 gateways, trouvé: {gateways}"


def test_case_2_xor_simple():
    text = (
        "The customer submits a loan application. The employee reviews the application. "
        "If the application is approved, the employee grants the loan. "
        "Otherwise, the employee rejects the application."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    # Exactement 1 XOR
    exclusive_gateways = [n for n in core.get("nodes", []) if n.get("type") == "exclusiveGateway"]
    assert len(exclusive_gateways) >= 1, "Attendu au moins 1 XOR métier"


def test_case_3_xor_avec_merge():
    text = (
        "The customer places an order. The employee checks the order. "
        "If the order is complete, the employee validates it. "
        "Otherwise, the employee asks the customer for missing information. "
        "After the information is received, the employee validates the order. "
        "The order is then processed."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors


def test_case_4_and():
    text = (
        "When an order is confirmed, the company prepares the invoice and packages the products at the same time. "
        "After both activities are completed, the order is shipped."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
    parallel_gateways = [n for n in core.get("nodes", []) if n.get("type") == "parallelGateway"]
    assert len(parallel_gateways) >= 1, "Attendu parallelGateway pour 'at the same time'"


def test_case_5_multiple_xor():
    text = (
        "The customer submits an order. The employee checks the stock. "
        "If the product is unavailable, the employee rejects the order. "
        "Otherwise, the employee checks the payment. "
        "If the payment is valid, the employee confirms the order. "
        "Otherwise, the employee rejects the order."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors


def test_case_6_or():
    text = (
        "When a request is submitted, several options can be selected simultaneously. "
        "The team handles the chosen options. Once all selected options are completed, the request is closed."
    )
    xml, core = run_pipeline(user_text=text, verbose=False)
    assert xml
    res = validate_logic_core(core)
    assert res.ok is True, res.errors
