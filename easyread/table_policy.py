"""Opt-in experimental exact table parsing; independent of simple translation."""
import os

DISABLED = "Experimental table extraction is disabled"


def enabled():
    return os.environ.get("PAPERREAD_DISABLE_TABLE_EXTRACTION", "1").strip().lower() not in {"1", "true", "yes", "on"}
