"""
Module-level globals for cross-cutting concerns.

FASE 3: Conditional logging — avoid string formatting on hot path.
"""

# Verbose flag — set from CLI --verbose or env VERBOSE=true
_VERBOSE: bool = False

# Sender task reference (set once in main.py after init)
SENDER = None  # type: ignore


def set_verbose(v: bool) -> None:
    global _VERBOSE
    _VERBOSE = v
