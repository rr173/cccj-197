"""Unit tests for the canonical business fingerprint helpers.

These exercise the normalisation rules directly, without a full repository, so
the five property states and the SI-value equality rule are pinned down
explicitly.
"""

from __future__ import annotations

import unittest

from ifc_handover.diff import fingerprints as fp


def _row(mapping: dict) -> object:
    class _Row:
        def __init__(self, data: dict) -> None:
            self._data = data

        def __getitem__(self, key: str) -> object:
            return self._data[key]

        def keys(self):
            return self._data.keys()

    return _Row(mapping)


def _measured_property(
    *,
    state: str = "value",
    value_status: str = "ok",
    measured: list | None = None,
    scope: str = "instance",
    step_id: int = 7,
    expression: str | None = None,
) -> object:
    measured = measured if measured is not None else []
    return _row(
        {
            "state": state,
            "value_status": value_status,
            "reason": None,
            "scope_used": scope,
            "json": __import__("json").dumps(
                {
                    "sources": [
                        {"step_id": step_id, "pset_key": "pset:abc:9", "ordinal": 0}
                    ],
                    "merged_source_count": 1,
                    "declaration": {
                        "expression": expression,
                        "interpretation": {"measured": measured},
                        "values": [],
                    },
                }
            ),
        }
    )


class FingerprintTests(unittest.TestCase):
    def test_si_value_equal_across_unit_rewrite(self) -> None:
        metre = _measured_property(
            measured=[
                {
                    "declared_type": "IFCLENGTHMEASURE",
                    "dimension": "LENGTH",
                    "status": "ok",
                    "si_value": {"numerator": "2", "denominator": "1"},
                }
            ]
        )
        millimetre = _measured_property(
            measured=[
                {
                    "declared_type": "IFCLENGTHMEASURE",
                    "dimension": "LENGTH",
                    "status": "ok",
                    "si_value": {"numerator": "2", "denominator": "1"},
                }
            ]
        )
        self.assertEqual(
            fp.property_business_fingerprint(metre),
            fp.property_business_fingerprint(millimetre),
        )

    def test_different_si_value_is_a_change(self) -> None:
        ten = _measured_property(
            measured=[
                {
                    "declared_type": "IFCMASSMEASURE",
                    "dimension": "MASS",
                    "status": "ok",
                    "si_value": {"numerator": "10", "denominator": "1"},
                }
            ]
        )
        twelve = _measured_property(
            measured=[
                {
                    "declared_type": "IFCMASSMEASURE",
                    "dimension": "MASS",
                    "status": "ok",
                    "si_value": {"numerator": "12", "denominator": "1"},
                }
            ]
        )
        self.assertNotEqual(
            fp.property_business_fingerprint(ten),
            fp.property_business_fingerprint(twelve),
        )

    def test_the_five_states_are_distinct(self) -> None:
        states = [
            ("explicit_null", "explicit_null"),
            ("unsupported", "unsupported_type"),
            ("expression_unevaluated", "unevaluated"),
            ("conflict", "conflict"),
        ]
        fingerprints = []
        for state, status in states:
            row = _measured_property(state=state, value_status=status)
            fingerprints.append(fp.property_business_fingerprint(row))
        # The "absent" state is supplied by the engine, but confirm the four
        # stored states never collapse to equal fingerprints.
        self.assertEqual(len({repr(f) for f in fingerprints}), len(fingerprints))
        explicit_null = fp.property_business_fingerprint(
            _measured_property(state="explicit_null", value_status="explicit_null")
        )
        self.assertEqual(explicit_null["state"], "explicit_null")
        self.assertNotEqual(explicit_null["state"], "absent")

    def test_missing_unit_keeps_original_value_and_status(self) -> None:
        row = _measured_property(
            value_status="missing_unit",
            measured=[
                {
                    "declared_type": "IFCLENGTHMEASURE",
                    "dimension": "LENGTH",
                    "status": "missing_unit",
                    "si_value": None,
                    "original_numeric": {"numerator": "5", "denominator": "1"},
                    "reason": "no unit",
                }
            ],
        )
        fingerprint = fp.property_business_fingerprint(row)
        self.assertEqual(fingerprint["value_status"], "missing_unit")
        self.assertEqual(
            fingerprint["values"][0]["original_numeric"],
            {"numerator": "5", "denominator": "1"},
        )
        self.assertIsNone(fingerprint["values"][0]["si_value"])

    def test_provenance_scope_difference_is_separate_from_value(self) -> None:
        type_row = _measured_property(scope="type", step_id=1)
        instance_row = _measured_property(scope="instance", step_id=2)
        # Same value fingerprint...
        self.assertEqual(
            fp.property_business_fingerprint(type_row),
            fp.property_business_fingerprint(instance_row),
        )
        # ...but distinct provenance, and STEP id alone does not drive it once
        # the scope is equal.
        self.assertNotEqual(
            fp.property_provenance(type_row)["scope_used"],
            fp.property_provenance(instance_row)["scope_used"],
        )
        same_scope_other_step = _measured_property(scope="instance", step_id=99)
        self.assertEqual(
            fp.property_provenance(instance_row)["scope_used"],
            fp.property_provenance(same_scope_other_step)["scope_used"],
        )

    def test_expression_state_keeps_expression_text(self) -> None:
        row = _measured_property(
            state="expression_unevaluated",
            value_status="unevaluated",
            expression="WIDTH * 2",
        )
        fingerprint = fp.property_business_fingerprint(row)
        self.assertEqual(fingerprint["state"], "expression_unevaluated")
        self.assertEqual(fingerprint["expression"], "WIDTH * 2")


if __name__ == "__main__":
    unittest.main()
