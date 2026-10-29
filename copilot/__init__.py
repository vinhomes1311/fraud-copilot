"""Fraud investigation module. No scoring or transaction execution."""
from .service import generate_brief, generate_brief_async

__all__ = ["generate_brief", "generate_brief_async"]
