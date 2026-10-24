"""Compatibility alias for the shared runtime resource resolver."""
import sys
from .. import resources as _resources
sys.modules[__name__] = _resources
