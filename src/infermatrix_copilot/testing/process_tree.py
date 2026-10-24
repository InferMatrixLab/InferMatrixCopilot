"""Compatibility import for canonical process-tree primitives."""
import sys
from .. import process_tree as _implementation

sys.modules[__name__] = _implementation
