"""Compatibility shim for the replaced idle implementation.

Curiosity is now a first-class always-on supervisor.  Keep these aliases so
old launch scripts and saved configurations fail forward instead of breaking.
"""

from nightwatch.curiosity import (
    CuriosityConfig,
    CuriositySupervisor,
    IdleBehavior,
    IdleConfig,
)

__all__ = [
    "CuriosityConfig",
    "CuriositySupervisor",
    "IdleBehavior",
    "IdleConfig",
]
