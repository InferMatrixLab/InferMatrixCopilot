#!/usr/bin/env python3
"""Repository entry point for the provider-owned wiki validator."""

from pathlib import Path
import os
import sys

# Source checkouts work before installation; portable trees use the installed provider.
_root = Path(__file__).resolve().parents[2]
if os.environ.get("INFERMATRIX_KNOWLEDGE_PROVIDER"):
    sys.path.insert(0, os.environ["INFERMATRIX_KNOWLEDGE_PROVIDER"])
elif (_root / "src" / "infermatrix_copilot").is_dir():
    sys.path.insert(0, str(_root / "src"))
from infermatrix_copilot.knowledge_service.validators import wiki as _validator

_validator.configure(_root / "knowledge")
if __name__ == "__main__":
    raise SystemExit(_validator.main())
# Preserve the tools' import surface for existing diagnostics and tests.
def __getattr__(name):
    return getattr(_validator, name)


sys.modules[__name__] = _validator
