"""Compatibility alias for the Direct review domain service."""
import sys
from ...review import direct as _direct
sys.modules[__name__] = _direct
