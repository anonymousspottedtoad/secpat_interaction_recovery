"""Static derivation of a pattern's interaction ordering from source code.

A role mapping says which code fills each role, not when it runs, so the order
is inferred: :func:`detect` sends the role mapping to the Joern query service, which
locates each role in the repo's prebuilt Code Property Graph, walks the
control-flow graph from the entry role, and returns the interactions in the order
control actually reaches them. See ``JOERN.md`` here for how that works, and
``joern/walk.sc`` for the walk itself.

Detection is shared by every Level 3 formula; only the comparison of two derived
orderings is formula-specific, and that lives in the metric class.

This was ``secpat_scoring.sequence`` until scoring 0.23.0. It is its own package
because the recovery pipeline derives an order too (its stage 3), and needs none
of scoring to do it; ``secpat-scoring`` installs this one for the result types
Level 3 compares.
"""

from __future__ import annotations

from .detector import (
    SCHEMA_VERSION,
    DetectionResult,
    Interaction,
    Step,
    detect,
)
from .joern_service import frontend_for

__all__ = [
    "SCHEMA_VERSION",
    "DetectionResult",
    "Interaction",
    "Step",
    "detect",
    "frontend_for",
]
