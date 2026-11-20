"""Strict validation tests for the take-off rules file.

These tests exercise only the JSON rule parser and need no IfcOpenShell.
"""

from __future__ import annotations

import json
import unittest

import helpers

from ifc_handover.takeoff import RulesError, load_rules


class RulesLoadingTests(helpers.TempDirTestCase):
    def _write(self, payload) -> str:
        path = self.path("rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _valid(self, **overrides) -> dict:
        rule = {
            "rule_id": "r1",
            "root": {"source_id": "s", "global_id": "g"},
            "entity_types": ["IfcPump"],
            "quantity_identities": [
                {
                    "entity_type": "IfcPump",
                    "quantity_set": "Qto_PumpBaseQuantities",
                    "name": "TotalLength",
                    "declared_type": "IfcQuantityLength",
                }
            ],
        }
        rule.update(overrides)
        return {"rules_version": 1, "rules": [rule]}

    def test_valid_loads(self) -> None:
        rules = load_rules(self._write(self._valid()))
        self.assertEqual(len(rules.rules), 1)
        identity = rules.rules[0].quantities[0]
        self.assertEqual(identity.dimension, "LENGTH")
        self.assertEqual(identity.canonical_unit, "m")

    def test_unknown_top_level_key_rejected(self) -> None:
        payload = self._valid()
        payload["extra"] = 1
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))

    def test_unsupported_declared_type_rejected(self) -> None:
        payload = self._valid()
        payload["rules"][0]["quantity_identities"][0]["declared_type"] = "IfcQuantityTime"
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))

    def test_unknown_entity_binding_rejected(self) -> None:
        payload = self._valid()
        payload["rules"][0]["quantity_identities"][0]["entity_type"] = "IfcValve"
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))

    def test_unbound_identity_applies_to_every_rule_type(self) -> None:
        payload = self._valid()
        del payload["rules"][0]["quantity_identities"][0]["entity_type"]
        rules = load_rules(self._write(payload))
        rule = rules.rules[0]
        self.assertEqual(
            rule.quantities_by_type["IfcPump"], rule.quantities
        )

    def test_same_name_different_types_are_both_allowed(self) -> None:
        payload = self._valid()
        payload["rules"][0]["quantity_identities"].append(
            {
                "entity_type": "IfcPump",
                "quantity_set": "Qto_PumpBaseQuantities",
                "name": "TotalLength",
                "declared_type": "IfcQuantityArea",
            }
        )
        rules = load_rules(self._write(payload))
        identities = rules.rules[0].quantities
        self.assertEqual(len(identities), 2)
        self.assertNotEqual(
            identities[0].identity, identities[1].identity
        )

    def test_duplicate_identity_in_one_rule_rejected(self) -> None:
        payload = self._valid()
        payload["rules"][0]["quantity_identities"].append(
            dict(payload["rules"][0]["quantity_identities"][0])
        )
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))

    def test_duplicate_rule_id_rejected(self) -> None:
        payload = self._valid()
        payload["rules"].append(dict(payload["rules"][0]))
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))

    def test_empty_rules_list_rejected(self) -> None:
        with self.assertRaises(RulesError):
            load_rules(self._write({"rules": []}))

    def test_non_utf8_or_bad_json_rejected(self) -> None:
        path = self.path("bad.json")
        with open(path, "wb") as fh:
            fh.write(b"{not valid json")
        with self.assertRaises(RulesError):
            load_rules(path)

    def test_entity_type_must_look_like_ifc(self) -> None:
        payload = self._valid(entity_types=["Pump"])
        with self.assertRaises(RulesError):
            load_rules(self._write(payload))


if __name__ == "__main__":
    unittest.main()
