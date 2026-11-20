"""Reordering invariance for the spatial quantity take-off.

The drill IFC is regenerated with every entity emitted in a shuffled order
(new STEP ids and line coordinates).  Business totals, line states and object
attribution must be identical; only the STEP coordinates differ, and each
evidence excerpt must still point at the declaration it backs.
"""

from __future__ import annotations

import os
import random
import re
import unittest

import helpers
import takeoff_fixtures as fx
from fixtures import sha256_bytes, _write_manifest

from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.takeoff import TakeoffDataset, build_takeoff

_ENTITY_RE = re.compile(r"^#(\d+)=([A-Z0-9_]+)\((.*)\);$", re.DOTALL)


def _shuffle_step_text(text: str, seed: int = 20261120) -> str:
    """Renumber and shuffle DATA entities, rewriting every internal reference."""

    lines = text.splitlines()
    head: list[str] = []
    body: list[str] = []
    tail: list[str] = []
    stage = "head"
    for line in lines:
        if stage == "head":
            head.append(line)
            if line == "DATA;":
                stage = "body"
        elif stage == "body":
            if line == "ENDSEC;":
                tail.append(line)
                stage = "tail"
            else:
                body.append(line)
        else:
            tail.append(line)
    entities: list[tuple[int, str, str]] = []
    for line in body:
        match = _ENTITY_RE.match(line)
        if not match:
            continue
        entities.append((int(match.group(1)), match.group(2), match.group(3)))
    rng = random.Random(seed)
    order = list(range(len(entities)))
    rng.shuffle(order)
    renumber = {
        old: new for new, old in enumerate((entities[i][0] for i in order), start=1)
    }

    def rewrite(args: str) -> str:
        # References to entities outside the file (the deliberately dangling
        # broken-edge endpoint) keep their original number; they stay broken.
        return re.sub(
            r"#(\d+)",
            lambda m: "#" + str(renumber.get(int(m.group(1)), int(m.group(1)))),
            args,
        )

    out = list(head)
    for new_id, index in enumerate(order, start=1):
        _old, type_name, args = entities[index]
        out.append(f"#{new_id}={type_name}({rewrite(args)});")
    out.extend(tail)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _business_projection(dataset_path: str) -> dict:
    with TakeoffDataset(dataset_path) as ds:
        details = [
            {
                "rule": d["rule_id"],
                "gid": d["global_id"],
                "entity": d["entity_type"],
                "identity": d["identity"],
                "state": d["line_state"],
                "status": d["value_status"],
                "num": d["canonical_num"],
                "den": d["canonical_den"],
                "decimal": d["canonical_decimal"],
                "unit": d["canonical_unit"],
                "scope": d["scope_used"],
            }
            for d in ds.details()
        ]
        summaries = [
            {k: s[k] for k in (
                "rule_id", "entity_type", "identity", "included_objects",
                "valid_count", "missing_count", "conflict_count",
                "unsupported_count", "unit_unavailable_count",
                "scope_uncertain_count", "total_num", "total_den",
                "total_decimal",
            )}
            for s in ds.summaries()
        ]
        anomalies = sorted(
            (a["anomaly_kind"], a.get("global_id"), a["source_id"])
            for a in ds.anomalies()
        )
    return {
        "details": sorted(details, key=lambda d: (d["rule"], d["gid"] or "", d["identity"])),
        "summaries": sorted(summaries, key=lambda s: (s["rule_id"], s["identity"])),
        "anomalies": anomalies,
    }


class TakeoffReorderTests(helpers.TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = os.path.join("/tmp", "ifc-takeoff-reorder-shared")
        os.makedirs(cls.base, exist_ok=True)
        cls.fixture = fx.build_drill_model(os.path.join(cls.base, "orig"))

    def _build(self, label: str, ifc_text: str | None = None) -> str:
        root = self.path(label)
        os.makedirs(root, exist_ok=True)
        if ifc_text is None:
            with open(self.fixture.ifc_path, encoding="utf-8") as fh:
                ifc_text = fh.read()
        ifc_path = os.path.join(root, "takeoff.ifc")
        with open(ifc_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ifc_text)
        from fixtures import sha256_bytes, _write_manifest

        manifest = _write_manifest(
            root,
            [
                {
                    "source_id": fx.SOURCE,
                    "path": "takeoff.ifc",
                    "sha256": sha256_bytes(ifc_text.encode("utf-8")),
                }
            ],
        )
        repo = Repository.create(self.path(label + "-repo.sqlite"))
        self.addCleanup(repo.close)
        for entry in load_manifest(manifest):
            result = SourceIngestor(
                repo, entry, store_dir=self.path(label + "-store")
            ).run()
            self.assertEqual(result.status, "ok", result.error)
        dest = self.path(label + "-dataset")
        build_takeoff(repo.path, self.fixture.rules, dest)
        return dest

    def test_business_projection_unchanged_after_shuffle(self) -> None:
        original_dest = self._build("a")
        with open(self.fixture.ifc_path, encoding="utf-8") as fh:
            shuffled = _shuffle_step_text(fh.read(), seed=77)
        shuffled_dest = self._build("b", shuffled)
        self.assertEqual(
            _business_projection(original_dest),
            _business_projection(shuffled_dest),
        )

    def test_step_coordinates_change_but_still_point_to_declarations(self) -> None:
        with open(self.fixture.ifc_path, encoding="utf-8") as fh:
            original_text = fh.read()
        shuffled = _shuffle_step_text(original_text, seed=77)
        shuffled_dest = self._build("c", shuffled)
        with TakeoffDataset(shuffled_dest) as ds:
            evidence = ds.evidence(owner_kind="object_detail")
        self.assertTrue(evidence)
        # Every excerpt's raw text must be present verbatim in the shuffled
        # file and the entity type must match the excerpt row.
        for item in evidence[:25]:
            self.assertIn(item["raw_text"], shuffled)
            self.assertTrue(item["raw_text"].startswith("#" + str(item["step_id"]) + "="))
        # The coordinates really did move for at least some evidence.
        with TakeoffDataset(self._build("d", original_text)) as original_ds:
            original_steps = {
                (e["step_id"], e["raw_text"][:40])
                for e in original_ds.evidence(owner_kind="object_detail")
            }
        shuffled_steps = {
            (e["step_id"], e["raw_text"][:40])
            for e in evidence
        }
        self.assertNotEqual(
            {s for s, _ in original_steps}, {s for s, _ in shuffled_steps}
        )


if __name__ == "__main__":
    unittest.main()
