"""
Core lifecycle package: ``bootstrap``/``teardown`` sequencing
(``app/core/bootstrap.py``), the FastAPI app factory (``app/core/app.py``),
process-wide state (``app/core/state.py``), and the Clock abstraction
(``app/core/clock.py``).

Deliberately no eager re-exports here (``from app.core.bootstrap import
bootstrap`` etc. — every call site already imports from the specific
submodule it needs). ``app/core/clock.py`` in particular has to stay
importable from low-level modules like ``app/aggregation/aggregator.py``
without pulling in ``app/core/bootstrap.py``'s full dependency chain
(which itself imports the aggregator) — an eager re-export here would
recreate exactly that circular import.
"""
from __future__ import annotations
