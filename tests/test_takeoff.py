"""End-to-end tests for the spatial quantity take-off dataset.

The fixture and the hand-written expectation table live in
``takeoff_fixtures.py`` and ``docs/takeoff-drill-expectations.md``.  These
tests assert the full chain: ingest (with physical quantities), take-off
build, queries, JSONL export, dataset portability, failure preservation,
reordering invariance and bounded-memory behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from fractions import Fraction

import helpers
import takeoff_fixtures as fx
from fixtures import gid

from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.takeoff import (
    LINE_CONFLICT,
    LINE_MISSING,
    LINE_SCOPE_UNCERTAIN,
    LINE_UNSUPPORTED,
    LINE_UNIT_UNAVAILABLE,
    LINE_VALID,
    RulesError,
    TakeoffDataset,
    build_takeoff,
)
from ifc_handover.takeoff.dataset import (
    ANOMALY_EDGE_MISSING,
    ANOMALY_ROOT_UNRESOLVED,
)


def _ingest(testcase: unittest.TestCase, fixture_root: str) -> Repository:
    repo = Repository.create(testcase.path("repo.sqlite"))
    testcase.addCleanup(repo.close)
    store_dir = testcase.path("repo-sources")
    for entry in load_manifest(os.path.join(fixture_root, "manifest.json")):
        result = SourceIngestor(repo, entry, store_dir=store_dir).run()
        testcase.assertEqual(result.status, "ok", result.error)
    return repo


class TakeoffDrillTests(helpers.TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_root = os.path.join(
            tempfile.gettempdir(), "ifc-takeoff-drill-shared", "fixture"
        )
        os.makedirs(cls.fixture_root, exist_ok=True)
        cls.fixture = fx.build_drill_model(cls.fixture_root)

    def setUp(self) -> None:
        super().setUp()
        self.repo = _ingest(self, self.fixture_root)
        self.dest = self.path("takeoff-dataset")
        self.manifest = build_takeoff(
            self.repo.path, self.fixture.rules, self.dest
        )

    # -- ingestion evidence ---------------------------------------------
    def test_quantity_tables_were_ingested(self) -> None:
        stats = self.repo.stats()
        self.assertGreater(stats["quantity_declarations"], 0)
        self.assertGreater(stats["resolved_quantities"], 0)
        # The five supported subtypes plus the retained complex quantity.
        kinds = {
            r["q_kind"]
            for r in self.repo.conn.execute(
                "SELECT DISTINCT q_kind FROM quantity_declarations"
            )
        }
        self.assertIn("IfcPhysicalComplexQuantity", kinds)
        for kind in (
            "IfcQuantityCount",
            "IfcQuantityLength",
            "IfcQuantityArea",
            "IfcQuantityWeight",
        ):
            self.assertIn(kind, kinds)

    def test_unsupported_complex_quantity_is_retained(self) -> None:
        rows = self.repo.conn.execute(
            "SELECT * FROM quantity_declarations WHERE q_kind='IfcPhysicalComplexQuantity'"
        ).fetchall()
        # One complex quantity on P5, one on V3; both are retained evidence.
        self.assertEqual(len(rows), 2)
        for row in rows:
            detail = json.loads(row["json"])
            self.assertEqual(detail["structural_state"], "unsupported")
            self.assertIn("supported", detail["reason"])

    def test_all_five_supported_subtypes_are_retained(self) -> None:
        kinds = {
            r["q_kind"]
            for r in self.repo.conn.execute(
                "SELECT DISTINCT q_kind FROM quantity_declarations"
            )
        }
        for kind in (
            "IfcQuantityCount",
            "IfcQuantityLength",
            "IfcQuantityArea",
            "IfcQuantityVolume",
            "IfcQuantityWeight",
        ):
            self.assertIn(kind, kinds)
        # The V3 weight and volume resolve to exact canonical values.
        v3_key = f"gid:{fx.SOURCE}:{gid(fx.V3)}"
        by_name = {
            r["q_name"]: r
            for r in self.repo.conn.execute(
                "SELECT * FROM resolved_quantities WHERE object_key=?", (v3_key,)
            )
        }
        self.assertEqual(by_name["Weight"]["canonical_num"], "12")
        self.assertEqual(by_name["Weight"]["canonical_unit"], "kg")
        self.assertEqual(by_name["NetVolume"]["canonical_num"], "2")
        self.assertEqual(by_name["NetVolume"]["canonical_unit"], "m³")

    # -- scope -----------------------------------------------------------
    def test_scope_states_and_empty_scope_distinction(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            scopes = {r["rule_id"]: r for r in ds.root_scopes()}
        self.assertEqual(scopes[fx.RULE_A]["scope_status"], "unverified")
        self.assertEqual(scopes[fx.RULE_B]["scope_status"], "unverified")
        self.assertEqual(scopes[fx.RULE_A_DUP]["scope_status"], "unverified")
        self.assertEqual(scopes[fx.RULE_EMPTY]["scope_status"], "confirmed")
        self.assertEqual(scopes[fx.RULE_EMPTY]["included_objects"], 0)
        self.assertEqual(scopes[fx.RULE_MISSING_ROOT]["included_objects"], 0)
        # empty confirmed scope vs unresolvable root are different rows.
        self.assertEqual(scopes[fx.RULE_EMPTY]["anomaly_count"], 0)
        self.assertGreater(scopes[fx.RULE_MISSING_ROOT]["anomaly_count"], 0)

    def test_repeated_edge_counts_once_and_two_root_attribution(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            members = ds.conn.execute(
                "SELECT rule_id, COUNT(*) c FROM scope_membership "
                "WHERE membership='valid' GROUP BY rule_id"
            ).fetchall()
            counts = {r["rule_id"]: r["c"] for r in members}
        # Storey A: 6 pumps + 2 valves = 8; B1: 6 pumps + V1 = 7.
        self.assertEqual(counts[fx.RULE_A], 8)
        self.assertEqual(counts[fx.RULE_B], 7)
        self.assertEqual(counts[fx.RULE_A_DUP], 6)

    def test_broken_edge_in_one_root_does_not_poison_other_root(self) -> None:
        # The broken edge hangs under room B1, so both A and B scopes are
        # unverified.  Room D's separate scope (which never sees that edge)
        # must remain confirmed, proving damage is per-root.
        with TakeoffDataset(self.dest) as ds:
            scopes = {r["rule_id"]: r for r in ds.root_scopes()}
            d_anomalies = ds.anomalies(rule_id=fx.RULE_ROOM_D)
        self.assertEqual(scopes[fx.RULE_ROOM_D]["scope_status"], "confirmed")
        self.assertEqual(d_anomalies, [])
        # The empty storey-C scope is also confirmed and anomaly-free.
        self.assertEqual(scopes[fx.RULE_EMPTY]["scope_status"], "confirmed")

    def test_object_under_two_rules_is_attributed_once_per_rule(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            p1_rows = ds.details(global_id=gid(fx.P1))
        rule_ids = {row["rule_id"] for row in p1_rows}
        # P1 appears under rule A and the duplicate rule, but never twice
        # within one rule.
        for rule_id in rule_ids:
            per_rule = [r for r in p1_rows if r["rule_id"] == rule_id]
            identities = [r["identity"] for r in per_rule]
            self.assertEqual(len(identities), len(set(identities)))
        self.assertIn(fx.RULE_A, rule_ids)
        self.assertIn(fx.RULE_A_DUP, rule_ids)

    def test_missing_root_and_broken_edge_anomalies_keep_coordinates(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            anomalies = ds.anomalies()
        by_kind = {}
        for anomaly in anomalies:
            by_kind.setdefault(anomaly["anomaly_kind"], []).append(anomaly)
        self.assertIn(ANOMALY_ROOT_UNRESOLVED, by_kind)
        missing = by_kind[ANOMALY_ROOT_UNRESOLVED][0]
        self.assertEqual(missing["source_id"], fx.SOURCE)
        self.assertEqual(missing["global_id"], gid("to-no-such-root"))
        edge_anomalies = [
            a for a in anomalies if a["anomaly_kind"] == ANOMALY_EDGE_MISSING
        ]
        self.assertTrue(edge_anomalies)
        broken = edge_anomalies[0]
        self.assertEqual(broken["source_id"], fx.SOURCE)
        self.assertIsNotNone(broken["detail"]["child_step_id"])
        self.assertEqual(broken["detail"]["edge_state"], "missing")

    def test_globalidless_object_is_identity_anomaly_not_member(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            rows = ds.conn.execute(
                "SELECT * FROM scope_membership WHERE membership='excluded_identity'"
            ).fetchall()
            anomalies = ds.anomalies()
        self.assertTrue(rows)
        self.assertTrue(
            any(a["anomaly_kind"] == "object_identity_missing" for a in anomalies)
        )
        # STEP numbers must never become cross-object identity.
        for row in rows:
            self.assertIsNone(row["global_id"])

    def test_step_numbers_never_merge_distinct_objects(self) -> None:
        # The GlobalId-less object and a normal pump may share no business key;
        # every detail line's object_key for a gid object starts with 'gid:'.
        with TakeoffDataset(self.dest) as ds:
            step_keyed = ds.conn.execute(
                "SELECT DISTINCT object_key FROM object_details "
                "WHERE object_key NOT LIKE 'gid:%'"
            ).fetchall()
        self.assertEqual(step_keyed, [])

    # -- line states -----------------------------------------------------
    def _lines(self, rule_id: str):
        with TakeoffDataset(self.dest) as ds:
            return ds.details(rule_id=rule_id)

    def test_explicit_zero_is_valid_and_not_missing(self) -> None:
        lines = [
            line
            for line in self._lines(fx.RULE_A)
            if line["global_id"] == gid(fx.P2)
            and line["q_name"] == fx.QNAME_COUNT
            and line["q_kind"] == "IfcQuantityCount"
        ]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["line_state"], LINE_VALID)
        self.assertEqual(line["canonical_num"], "0")
        self.assertEqual(line["canonical_den"], "1")
        self.assertEqual(line["scope_used"], "instance")

    def test_instance_overrides_type_without_double_count(self) -> None:
        lines = [
            line
            for line in self._lines(fx.RULE_A)
            if line["global_id"] == gid(fx.P2)
            and line["q_name"] == fx.QNAME_LENGTH
        ]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["line_state"], LINE_VALID)
        self.assertEqual(line["canonical_decimal"], "2.5")
        self.assertEqual(line["scope_used"], "instance")
        # The shadowed type declaration is retained in the payload.
        inner = line["declarations"]["declarations"]
        self.assertIn("type_declaration_ignored", inner)

    def test_type_inheritance_only_without_instance_declaration(self) -> None:
        lines = [
            line
            for line in self._lines(fx.RULE_A)
            if line["global_id"] == gid(fx.P1)
        ]
        by_name = {(l["q_name"], l["q_kind"]): l for l in lines}
        count = by_name[(fx.QNAME_COUNT, "IfcQuantityCount")]
        length = by_name[(fx.QNAME_LENGTH, "IfcQuantityLength")]
        self.assertEqual(count["line_state"], LINE_VALID)
        self.assertEqual(count["canonical_num"], "4")
        self.assertEqual(count["scope_used"], "type")
        self.assertEqual(length["canonical_decimal"], "1")
        self.assertEqual(length["scope_used"], "type")

    def test_same_identity_conflict_is_not_picked(self) -> None:
        lines = [
            line
            for line in self._lines(fx.RULE_A)
            if line["global_id"] == gid(fx.P3)
            and line["q_name"] == fx.QNAME_LENGTH
        ]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["line_state"], LINE_CONFLICT)
        self.assertIsNone(line["canonical_num"])
        # Both conflicting declarations and their STEP coordinates survive.
        inner = line["declarations"]["declarations"]
        conflicting = inner.get("conflicting_declarations")
        self.assertIsNotNone(conflicting)
        self.assertEqual(len(conflicting), 2)
        for entry in conflicting:
            self.assertIn("q_step_id", entry)
            self.assertIn("interpretation", entry)
        # The distinct interpreted values (0.25 m vs 0.5 m) are both visible.
        decimals = {
            entry["interpretation"]["canonical_decimal"] for entry in conflicting
        }
        self.assertEqual(decimals, {"0.25", "0.5"})

    def test_same_name_different_types_are_separate_identities(self) -> None:
        lines = [
            line
            for line in self._lines(fx.RULE_A)
            if line["global_id"] == gid(fx.P4)
            and line["q_name"] == fx.QNAME_GAUGE
        ]
        self.assertEqual(len(lines), 2)
        by_kind = {l["q_kind"]: l for l in lines}
        length = by_kind["IfcQuantityLength"]
        area = by_kind["IfcQuantityArea"]
        self.assertEqual(length["line_state"], LINE_VALID)
        self.assertEqual(length["canonical_decimal"], "1")
        self.assertEqual(area["line_state"], LINE_VALID)
        self.assertEqual(area["canonical_decimal"], "0.00302525")
        self.assertEqual(area["canonical_unit"], "m²")
        # Exact fraction, and the decimal terminates.
        self.assertTrue(area["canonical_decimal_exact"])
        self.assertEqual(
            Fraction(int(area["canonical_num"]), int(area["canonical_den"])),
            Fraction(12101, 4_000_000),
        )

    def test_unit_cycle_and_dimension_mismatch_are_unit_unavailable(self) -> None:
        lines = self._lines(fx.RULE_A)
        p5 = [
            l
            for l in lines
            if l["global_id"] == gid(fx.P5) and l["q_name"] == fx.QNAME_LENGTH
        ][0]
        p6 = [
            l
            for l in lines
            if l["global_id"] == gid(fx.P6) and l["q_name"] == fx.QNAME_LENGTH
        ][0]
        self.assertEqual(p5["line_state"], LINE_UNIT_UNAVAILABLE)
        self.assertEqual(p5["value_status"], "unit_cycle")
        self.assertEqual(p6["line_state"], LINE_UNIT_UNAVAILABLE)
        self.assertEqual(p6["value_status"], "dimension_mismatch")

    def test_unsupported_declaration_retained_but_not_a_rule_line(self) -> None:
        # The complex quantity is not one of the rule's quantity identities,
        # so it produces no take-off line, but the repository retains the
        # unsupported declaration with its reason (asserted separately in
        # test_unsupported_complex_quantity_is_retained).
        with TakeoffDataset(self.dest) as ds:
            rows = ds.conn.execute(
                "SELECT * FROM object_details WHERE line_state=? "
                "AND rule_id != ?",
                (LINE_UNSUPPORTED, fx.RULE_ROOM_D),
            ).fetchall()
        self.assertEqual(rows, [])
        # V3 is in room D, outside storey A: no lines under rule A.
        v3_missing = [
            l
            for l in self._lines(fx.RULE_A)
            if l["global_id"] == gid(fx.V3)
        ]
        self.assertEqual(v3_missing, [])

    def test_unsupported_line_and_missing_lines_in_complete_scope(self) -> None:
        lines = self._lines(fx.RULE_ROOM_D)
        # Room D contains exactly V3; every valve identity gets one line.
        self.assertEqual(len(lines), 4)
        by_name = {l["q_name"]: l for l in lines}
        self.assertEqual(
            by_name[fx.QNAME_LENGTH]["line_state"], LINE_UNSUPPORTED
        )
        self.assertEqual(
            by_name[fx.QNAME_LENGTH]["value_status"], "unsupported_type"
        )
        self.assertEqual(by_name[fx.QNAME_COUNT]["line_state"], LINE_MISSING)
        self.assertEqual(by_name[fx.QNAME_AREA]["line_state"], LINE_MISSING)
        self.assertEqual(by_name[fx.QNAME_BORE]["line_state"], LINE_MISSING)
        with TakeoffDataset(self.dest) as ds:
            scope = {
                r["rule_id"]: r for r in ds.root_scopes()
            }[fx.RULE_ROOM_D]
        self.assertEqual(scope["scope_status"], "confirmed")
        self.assertEqual(scope["included_objects"], 1)

    def test_scope_uncertain_distinct_from_missing(self) -> None:
        lines = self._lines(fx.RULE_A)
        # P4 has no instance TotalLength, so it inherits the type value and
        # the line is valid even though the scope itself is unverified.
        p4_length = [
            l
            for l in lines
            if l["global_id"] == gid(fx.P4)
            and l["q_name"] == fx.QNAME_LENGTH
        ][0]
        self.assertEqual(p4_length["line_state"], LINE_VALID)
        self.assertEqual(p4_length["scope_used"], "type")
        # P6 has no instance Count, so it inherits the type Count and is a
        # valid line rather than a scope-uncertain one.
        p6_count = [
            l
            for l in lines
            if l["global_id"] == gid(fx.P6)
            and l["q_name"] == fx.QNAME_COUNT
        ][0]
        self.assertEqual(p6_count["line_state"], LINE_VALID)
        self.assertEqual(p6_count["scope_used"], "type")
        # A scope-uncertain line still exists for an identity the object
        # neither declares nor inherits (P4 NetArea).
        p4_area = [
            l
            for l in lines
            if l["global_id"] == gid(fx.P4)
            and l["q_name"] == fx.QNAME_AREA
        ][0]
        self.assertEqual(p4_area["line_state"], LINE_SCOPE_UNCERTAIN)
        # A complete-scope missing identity would be LINE_MISSING; the empty
        # storey-C scope has no lines at all.
        self.assertEqual(self._lines(fx.RULE_EMPTY), [])

    # -- exact summaries --------------------------------------------------
    def _summary(self, rule_id: str, entity_type: str, q_name: str, q_kind: str):
        with TakeoffDataset(self.dest) as ds:
            rows = ds.summaries(
                rule_id=rule_id, entity_type=entity_type, identity=None
            )
        matches = [
            r
            for r in rows
            if r["q_name"] == q_name and r["q_kind"] == q_kind
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_pump_count_total_is_sixteen(self) -> None:
        # Every pump without an instance Count inherits the type Count=4;
        # only P2 declares an instance Count (explicit 0).  So valid lines:
        # P1, P3, P4, P5, P6 = 5 * 4 plus P2 = 0 -> 20? No: P2's instance
        # count replaces the type, giving 4 + 0 + 4 + 4 + 4 + 4 = 20.
        row = self._summary(
            fx.RULE_A, "IfcPump", fx.QNAME_COUNT, "IfcQuantityCount"
        )
        self.assertEqual(row["included_objects"], 6)
        self.assertEqual(row["valid_count"], 6)
        self.assertEqual(row["missing_count"], 0)
        self.assertEqual(row["scope_uncertain_count"], 0)
        self.assertEqual(row["total_num"], "20")
        self.assertEqual(row["total_den"], "1")

    def test_pump_length_total_is_exact_nine_halves(self) -> None:
        row = self._summary(
            fx.RULE_A, "IfcPump", fx.QNAME_LENGTH, "IfcQuantityLength"
        )
        self.assertEqual(row["valid_count"], 3)
        self.assertEqual(row["conflict_count"], 1)
        self.assertEqual(row["unit_unavailable_count"], 2)
        self.assertEqual(row["scope_uncertain_count"], 0)
        self.assertEqual(
            Fraction(int(row["total_num"]), int(row["total_den"])),
            Fraction(9, 2),
        )
        self.assertEqual(row["total_decimal"], "4.5")
        self.assertTrue(row["total_decimal_exact"])

    def test_pump_area_total_is_one_eighth(self) -> None:
        row = self._summary(
            fx.RULE_A, "IfcPump", fx.QNAME_AREA, "IfcQuantityArea"
        )
        # Storey-A's scope is unverified (broken edge), so every absent
        # identity counts as scope_uncertain rather than missing.
        self.assertEqual(row["valid_count"], 1)
        self.assertEqual(row["missing_count"], 0)
        self.assertEqual(row["scope_uncertain_count"], 5)
        self.assertEqual(
            Fraction(int(row["total_num"]), int(row["total_den"])),
            Fraction(1, 8),
        )
        self.assertEqual(row["total_decimal"], "0.125")

    def test_valve_totals_and_prefix_conversion(self) -> None:
        length = self._summary(
            fx.RULE_A, "IfcValve", fx.QNAME_LENGTH, "IfcQuantityLength"
        )
        self.assertEqual(
            Fraction(int(length["total_num"]), int(length["total_den"])),
            Fraction(11, 2),
        )
        bore = self._summary(
            fx.RULE_A, "IfcValve", fx.QNAME_BORE, "IfcQuantityLength"
        )
        self.assertEqual(
            Fraction(int(bore["total_num"]), int(bore["total_den"])),
            Fraction(1, 4),
        )
        count = self._summary(
            fx.RULE_A, "IfcValve", fx.QNAME_COUNT, "IfcQuantityCount"
        )
        self.assertEqual(count["total_num"], "3")

    def test_summaries_are_recomputable_from_details(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            details = ds.details(rule_id=fx.RULE_A)
            summaries = ds.summaries(rule_id=fx.RULE_A)
        for summary in summaries:
            group = [
                d
                for d in details
                if d["entity_type"] == summary["entity_type"]
                and d["identity"] == summary["identity"]
            ]
            self.assertEqual(len(group), summary["included_objects"])
            total = Fraction(0)
            for line in group:
                if line["line_state"] == LINE_VALID:
                    total += Fraction(
                        int(line["canonical_num"]), int(line["canonical_den"])
                    )
            if summary["total_num"] is None:
                self.assertEqual(total, Fraction(0))
            else:
                self.assertEqual(
                    total,
                    Fraction(
                        int(summary["total_num"]), int(summary["total_den"])
                    ),
                )

    def test_every_rule_type_identity_combination_has_a_summary(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            combos = ds.conn.execute(
                "SELECT r.rule_id, re.entity_type, q.identity FROM rules r "
                "JOIN rule_entities re ON re.rule_id=r.rule_id "
                "JOIN rule_quantity_identities q ON q.rule_id=r.rule_id "
                "AND q.entity_type=re.entity_type"
            ).fetchall()
            summaries = {
                (s["rule_id"], s["entity_type"], s["identity"])
                for s in ds.summaries()
            }
        for combo in combos:
            self.assertIn(
                (combo["rule_id"], combo["entity_type"], combo["identity"]),
                summaries,
            )

    def test_empty_scope_summaries_are_explicit_zero_rows(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            rows = ds.summaries(rule_id=fx.RULE_EMPTY)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["included_objects"], 0)
            self.assertEqual(row["total_num"], "0")
            self.assertEqual(row["total_den"], "1")
            self.assertEqual(row["valid_count"], 0)

    # -- queries, export, portability ------------------------------------
    def test_filters_and_object_query(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            conflicts = ds.details(line_state=LINE_CONFLICT)
            self.assertTrue(conflicts)
            obj = ds.object_detail_by_identity(fx.SOURCE, gid(fx.P3))
            self.assertTrue(obj)
            self.assertTrue(
                any(line["line_state"] == LINE_CONFLICT for line in obj)
            )

    def test_jsonl_export_roundtrip_and_filters(self) -> None:
        jsonl = self.path("export.jsonl")
        with TakeoffDataset(self.dest) as ds:
            rows = 0
            with open(jsonl, "w", encoding="utf-8", newline="\n") as fh:
                for line in ds.iter_jsonl():
                    fh.write(json.dumps(line, sort_keys=True) + "\n")
                    rows += 1
        self.assertGreater(rows, 0)
        with open(jsonl, encoding="utf-8") as fh:
            kinds = {json.loads(line)["kind"] for line in fh}
        self.assertEqual(
            kinds, {"object_detail", "quantity_summary", "scope_anomaly"}
        )

    def test_dataset_reopens_after_copy_without_repository(self) -> None:
        copied = self.path("copied-dataset")
        shutil.copytree(self.dest, copied)
        with TakeoffDataset(copied) as ds:
            summary = ds.summary()
            self.assertGreater(summary["detail_lines"], 0)
            manifest = ds.manifest()
            self.assertIn("repository", manifest)
            self.assertIn("rules", manifest)
            self.assertIn("run", manifest)
        # Removing the repository must not affect the copy.
        os.remove(self.repo.path)
        with TakeoffDataset(copied) as ds:
            self.assertEqual(
                ds.summary()["detail_lines"], summary["detail_lines"]
            )

    def test_evidence_excerpts_are_minimal_and_present(self) -> None:
        with TakeoffDataset(self.dest) as ds:
            evidence = ds.evidence()
            owners = {e["owner_kind"] for e in evidence}
        self.assertIn("object_detail", owners)
        self.assertIn("scope_anomaly", owners)
        for item in evidence:
            self.assertTrue(item["raw_text"])
            self.assertIsNotNone(item["line_start"])

    # -- rules validation -------------------------------------------------
    def test_structurally_invalid_rules_reject_build(self) -> None:
        bad_rules = self.path("bad-rules.json")
        with open(bad_rules, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "rules": [
                        {
                            "rule_id": "bad",
                            "root": {"source_id": fx.SOURCE, "global_id": gid(fx.STOREY_A)},
                            "entity_types": ["IfcPump"],
                            "quantity_identities": [
                                {
                                    "entity_type": "IfcPump",
                                    "quantity_set": "Q",
                                    "name": "X",
                                    "declared_type": "IfcPropertySingleValue",
                                }
                            ],
                        }
                    ]
                },
                fh,
            )
        with self.assertRaises(RulesError):
            build_takeoff(self.repo.path, bad_rules, self.path("never"))

    def test_failed_build_preserves_previous_dataset(self) -> None:
        bad_rules = self.path("bad-rules-2.json")
        with open(bad_rules, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        before = os.listdir(self.dest)
        with self.assertRaises(Exception):
            build_takeoff(self.repo.path, bad_rules, self.dest)
        self.assertEqual(sorted(os.listdir(self.dest)), sorted(before))
        with TakeoffDataset(self.dest) as ds:
            self.assertGreater(ds.summary()["detail_lines"], 0)

    def test_repository_change_mid_run_rejects_and_preserves_dataset(self) -> None:
        from ifc_handover.diff.snapshot import InputModifiedError
        import ifc_handover.takeoff.verify as verify_mod

        original_assert = verify_mod.RepoSnapShot.assert_unchanged

        def fail_assert(self):  # type: ignore[no-untyped-def]
            raise InputModifiedError("simulated mid-run repository change")

        verify_mod.RepoSnapShot.assert_unchanged = fail_assert  # type: ignore[method-assign]
        try:
            with self.assertRaises(InputModifiedError):
                build_takeoff(
                    self.repo.path,
                    self.fixture.rules,
                    self.path("changed-mid-run"),
                )
        finally:
            verify_mod.RepoSnapShot.assert_unchanged = original_assert  # type: ignore[method-assign]
        self.assertFalse(
            os.path.exists(self.path("changed-mid-run")),
            "no directory that reads as a complete dataset may remain",
        )
        # The previous dataset is untouched.
        with TakeoffDataset(self.dest) as ds:
            self.assertGreater(ds.summary()["detail_lines"], 0)


if __name__ == "__main__":
    unittest.main()
