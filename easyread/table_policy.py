"""Opt-in experimental exact table parsing; independent of simple translation."""
import os
from contextlib import contextmanager
from contextvars import ContextVar

_explicit = ContextVar('explicit_table_job', default=False)


@contextmanager
def explicit_job():
    """Enable existing parsing only within an explicitly requested draft job."""
    token = _explicit.set(True)
    try:
        yield
    finally:
        _explicit.reset(token)

DISABLED = "Experimental table extraction is disabled"


def enabled():
    return _explicit.get() or os.environ.get("PAPERREAD_DISABLE_TABLE_EXTRACTION", "1").strip().lower() not in {"1", "true", "yes", "on"}
