"""Observational context engine plugin."""

from agent.observational_compressor import ObservationalContextEngine

def create_engine():
    return ObservationalContextEngine()

__all__ = ["ObservationalContextEngine", "create_engine"]
