"""Reconstruct unit evidence from the repository after ingestion.

Exports must not depend on the original file paths.  A stored source is
re-parsed from the repository's content-addressed copy when necessary so
project unit assignments, unit definitions and raw excerpts can be emitted.
"""

from __future__ import annotations

import os
from typing import Any

from .step import StepFile
from .store import Repository
from .units import UnitResolver


class StoredSource:
    """A source's stored bytes plus lazily rebuilt STEP/unit views."""

    def __init__(self, repo: Repository, source_id: str):
        self.repo = repo
        self.source_id = source_id
        self.row = repo.source(source_id)
        if self.row is None:
            raise KeyError(f"unknown source {source_id}")
        self._step: StepFile | None = None
        self._resolver: UnitResolver | None = None

    @property
    def sha256(self) -> str:
        return self.row.get("sha256") or ""

    @property
    def stored_path(self) -> str | None:
        return self.row.get("stored_path")

    def step(self) -> StepFile | None:
        if self._step is None:
            path = self.stored_path
            if not path or not os.path.isfile(path):
                return None
            self._step = StepFile.from_path(path)
        return self._step

    def resolver(self) -> UnitResolver | None:
        if self._resolver is None:
            step = self.step()
            if step is None:
                return None
            self._resolver = UnitResolver(step)
        return self._resolver

    def unit(self, step_id: int) -> dict[str, Any] | None:
        resolver = self.resolver()
        if resolver is None:
            return None
        unit = resolver.resolve(step_id)
        return unit.as_json()

    def unit_closure_ids(self, step_id: int) -> list[int]:
        """All unit/measure-with-unit STEP ids transitively used by a unit."""

        step = self.step()
        if step is None:
            return []
        seen: set[int] = set()
        queue = [step_id]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            if current not in step.entities:
                continue
            seen.add(current)
            for ref in step.refs(current):
                entity = step.entities.get(ref)
                if entity is None:
                    continue
                if entity.type_name in {
                    "IFCSIUNIT",
                    "IFCCONVERSIONBASEDUNIT",
                    "IFCCONVERSIONBASEDUNITWITHOFFSET",
                    "IFCDERIVEDUNIT",
                    "IFCCONTEXTDEPENDENTUNIT",
                    "IFCMONETARYUNIT",
                    "IFCMEASUREWITHUNIT",
                    "IFCDERIVEDUNITELEMENT",
                    "IFCDIMENSIONALEXPONENTS",
                }:
                    queue.append(ref)
        return sorted(seen)


def reconstruct_unit_row(source_row: dict[str, Any], unit_json: dict[str, Any]) -> dict[str, Any]:
    """Return the unit evidence in a self-describing export form."""

    return {
        "unit_step_id": unit_json.get("unit_step_id"),
        "kind": unit_json.get("kind"),
        "dimension": unit_json.get("dimension"),
        "scale_to_si": unit_json.get("scale_to_si"),
        "offset_to_si": unit_json.get("offset_to_si"),
        "label": unit_json.get("label"),
        "prefix": unit_json.get("prefix"),
        "si_name": unit_json.get("si_name"),
        "exponent": unit_json.get("exponent"),
        "elements": unit_json.get("elements"),
        "cycle": unit_json.get("cycle"),
        "detail": unit_json.get("detail"),
    }
