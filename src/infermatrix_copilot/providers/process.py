"""Compatibility import for the neutral process execution mechanism."""
import sys
from .. import process_execution as _implementation

sys.modules[__name__] = _implementation
