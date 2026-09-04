"""Qocha — a local-first vault engine.

Hybrid semantic search and grounded, cited answers over a folder of
markdown notes. The engine is public; the vault it connects to stays
yours and private.
"""
from .commission import commission_prompt, survey, unprocessed
from .config import Config
from .indexer import Indexer
from .vault import Vault

__version__ = "0.4.3"
__all__ = ["Vault", "Config", "Indexer", "survey", "commission_prompt",
           "unprocessed", "__version__"]
