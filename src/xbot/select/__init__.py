"""Post selection: apply eligibility rules, return ranked eligible candidates."""
from .rules import evaluate, select_all

__all__ = ["evaluate", "select_all"]
