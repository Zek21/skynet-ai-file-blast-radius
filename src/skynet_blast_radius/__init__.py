"""Skynet AI File Blast Radius — what breaks if I change this file?

    from skynet_blast_radius import blast, set_root

    set_root("/path/to/repo")
    report = blast("src/core/protocol.py")
    report["risk_score"], report["risk_band"]

Answers a question a direct-importer count cannot: a file with two importers may
sit beneath two hundred transitive dependents, and those second-order dependents
are where the regression actually lands.
"""
from .engine import ROOT, blast, blast_gate, main, set_root

__version__ = "2.0.0"
__all__ = ["blast", "blast_gate", "set_root", "main", "ROOT", "__version__"]
