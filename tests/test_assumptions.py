"""Keep the assumption registry and the code in step (and, below, the docs).

assumptions.py is the source of truth. This fails when:
- the code cites an id that isn't in the registry (a typo, or a removed assumption)
- an assumption is cited nowhere in the code or tests (nothing depends on it, so why keep it?)
"""

from __future__ import annotations

import re
from pathlib import Path

from webos_mcp.assumptions import ASSUMPTIONS

ROOT = Path(__file__).parent.parent
ID = re.compile(r"\bW-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def cited_in(paths: list[Path]) -> set[str]:
    return {m for path in paths for m in ID.findall(path.read_text())}


def test_ids_are_unique_and_well_formed():
    ids = [a.id for a in ASSUMPTIONS]
    assert len(ids) == len(set(ids))
    assert all(ID.fullmatch(i) for i in ids)


def test_every_claim_has_a_source():
    for a in ASSUMPTIONS:
        assert a.source.strip(), a.id


def test_verified_ones_say_what_was_seen():
    for a in ASSUMPTIONS:
        if a.status != "simulator-only":
            assert a.note, f"{a.id}: say which TV and what was seen"
