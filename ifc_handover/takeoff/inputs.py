"""Spatial quantity take-off rules input.

The rules file is plain UTF-8 JSON and is loaded strictly: unknown keys,
wrong types and malformed quantity identities reject the whole run before any
output is produced.

Rule file shape::

    {
      "rules_version": 1,
      "rules": [
        {
          "rule_id": "zone-a-pumps",
          "root": {"source_id": "mechanical", "global_id": "0_RV..."},
          "entity_types": ["IfcPump", "IfcValve"],
          "quantity_identities": [
            {"entity_type": "IfcPump",
             "quantity_set": "Qto_PumpBaseQuantities",
             "name": "Count", "declared_type": "IfcQuantityCount"},
            {"entity_type": "IfcValve",
             "quantity_set": "Qto_ValveBaseQuantities",
             "name": "TotalLength", "declared_type": "IfcQuantityLength"}
          ]
        }
      ]
    }

The optional ``entity_type`` field on a quantity identity binds it to one of
the rule's entity types; when omitted the identity applies to every entity
type of the rule.  A quantity identity is the triple
``(quantity_set, name, declared_type)``.  Two identities that share a set and
name but use different declared types (for example an
``IfcQuantityLength`` and an ``IfcQuantityArea`` both named ``NetSize``) are
different identities and are never merged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from .errors import RulesError

SUPPORTED_QUANTITY_TYPES = frozenset(
    {
        "IfcQuantityCount",
        "IfcQuantityLength",
        "IfcQuantityArea",
        "IfcQuantityVolume",
        "IfcQuantityWeight",
    }
)

_DECLARED_DIMENSION = {
    "IfcQuantityCount": "COUNT",
    "IfcQuantityLength": "LENGTH",
    "IfcQuantityArea": "AREA",
    "IfcQuantityVolume": "VOLUME",
    "IfcQuantityWeight": "MASS",
}
_CANONICAL_UNIT = {
    "COUNT": "count",
    "LENGTH": "m",
    "AREA": "m²",
    "VOLUME": "m³",
    "MASS": "kg",
}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_keys(
    obj: dict[str, Any],
    allowed: set[str],
    required: set[str],
    *,
    context: str,
) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise RulesError(f"{context}: unknown keys {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise RulesError(f"{context}: missing keys {sorted(missing)}")


def _non_empty_str(value: Any, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RulesError(f"{context}: {field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class QuantityIdentity:
    quantity_set: str
    name: str
    declared_type: str

    @property
    def identity(self) -> str:
        return f"{self.quantity_set}\u001f{self.name}\u001f{self.declared_type}"

    @property
    def dimension(self) -> str:
        return _DECLARED_DIMENSION[self.declared_type]

    @property
    def canonical_unit(self) -> str:
        return _CANONICAL_UNIT[self.dimension]

    def as_json(self) -> dict[str, Any]:
        return {
            "quantity_set": self.quantity_set,
            "name": self.name,
            "declared_type": self.declared_type,
            "identity": self.identity,
            "dimension": self.dimension,
            "canonical_unit": self.canonical_unit,
        }


@dataclass(frozen=True)
class TakeoffRule:
    rule_id: str
    root_source_id: str
    root_global_id: str
    entity_types: tuple[str, ...]
    quantities: tuple[QuantityIdentity, ...]
    # Entity type -> the quantity identities evaluated for objects of that
    # type.  One rule may cover several entity types with distinct identity
    # sets; cross-type identity strings never create detail lines.
    quantities_by_type: dict[str, tuple[QuantityIdentity, ...]]


@dataclass(frozen=True)
class TakeoffRules:
    rules: tuple[TakeoffRule, ...]
    raw: Any
    sha256: str
    path: str

    def identities(self) -> frozenset[str]:
        return frozenset(q.identity for rule in self.rules for q in rule.quantities)


def load_rules(path: str) -> TakeoffRules:
    context = "takeoff rules"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RulesError(f"rules: cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError("rules: top level must be a JSON object")
    _require_keys(
        raw,
        allowed={"rules_version", "rules"},
        required={"rules"},
        context=context,
    )
    if "rules_version" in raw and raw["rules_version"] != 1:
        raise RulesError(f"rules: unsupported rules_version {raw['rules_version']!r}")
    rules_raw = raw["rules"]
    if not isinstance(rules_raw, list) or not rules_raw:
        raise RulesError("rules: 'rules' must be a non-empty list")
    rules: list[TakeoffRule] = []
    seen_ids: set[str] = set()
    for index, rule_raw in enumerate(rules_raw):
        rctx = f"rules rule #{index}"
        if not isinstance(rule_raw, dict):
            raise RulesError(f"{rctx}: must be an object")
        _require_keys(
            rule_raw,
            allowed={"rule_id", "root", "entity_types", "quantity_identities"},
            required={"rule_id", "root", "entity_types", "quantity_identities"},
            context=rctx,
        )
        rule_id = _non_empty_str(
            rule_raw["rule_id"], field="rule_id", context=rctx
        )
        if rule_id in seen_ids:
            raise RulesError(f"{rctx}: duplicate rule_id {rule_id!r}")
        seen_ids.add(rule_id)
        root = rule_raw["root"]
        if not isinstance(root, dict):
            raise RulesError(f"{rctx}: 'root' must be an object")
        _require_keys(
            root,
            allowed={"source_id", "global_id"},
            required={"source_id", "global_id"},
            context=f"{rctx} root",
        )
        root_source = _non_empty_str(
            root["source_id"], field="source_id", context=f"{rctx} root"
        )
        root_gid = _non_empty_str(
            root["global_id"], field="global_id", context=f"{rctx} root"
        )
        entity_types = rule_raw["entity_types"]
        if (
            not isinstance(entity_types, list)
            or not entity_types
            or any(
                not isinstance(t, str) or not t.startswith("Ifc")
                for t in entity_types
            )
        ):
            raise RulesError(
                f"{rctx}: 'entity_types' must be a non-empty list of IFC entity type names"
            )
        entity_type_tuple = tuple(entity_types)
        q_raw = rule_raw["quantity_identities"]
        if not isinstance(q_raw, list) or not q_raw:
            raise RulesError(
                f"{rctx}: 'quantity_identities' must be a non-empty list"
            )
        quantities: list[QuantityIdentity] = []
        by_type: dict[str, list[QuantityIdentity]] = {
            entity_type: [] for entity_type in entity_type_tuple
        }
        seen_rule_identities: set[tuple[str, str, str, str | None]] = set()
        for q_index, item in enumerate(q_raw):
            qctx = f"{rctx} quantity_identity #{q_index}"
            if not isinstance(item, dict):
                raise RulesError(f"{qctx}: must be an object")
            _require_keys(
                item,
                allowed={
                    "entity_type",
                    "quantity_set",
                    "name",
                    "declared_type",
                },
                required={"quantity_set", "name", "declared_type"},
                context=qctx,
            )
            qset = _non_empty_str(
                item["quantity_set"], field="quantity_set", context=qctx
            )
            name = _non_empty_str(item["name"], field="name", context=qctx)
            declared_type = _non_empty_str(
                item["declared_type"], field="declared_type", context=qctx
            )
            if declared_type not in SUPPORTED_QUANTITY_TYPES:
                raise RulesError(
                    f"{qctx}: declared_type {declared_type!r} is not one of "
                    f"{sorted(SUPPORTED_QUANTITY_TYPES)}"
                )
            bound_type = item.get("entity_type")
            if bound_type is not None:
                if not isinstance(bound_type, str) or not bound_type.startswith("Ifc"):
                    raise RulesError(
                        f"{qctx}: entity_type must be an IFC entity type name"
                    )
                if bound_type not in entity_type_tuple:
                    raise RulesError(
                        f"{qctx}: entity_type {bound_type!r} is not listed in "
                        f"the rule's entity_types {list(entity_type_tuple)}"
                    )
            token = (qset, name, declared_type, bound_type)
            if token in seen_rule_identities:
                raise RulesError(
                    f"{qctx}: duplicate quantity identity inside rule {rule_id!r} "
                    f"(quantity_set={qset!r}, name={name!r}, "
                    f"declared_type={declared_type!r}, entity_type={bound_type!r})"
                )
            seen_rule_identities.add(token)
            identity = QuantityIdentity(qset, name, declared_type)
            quantities.append(identity)
            targets = (
                (bound_type,) if bound_type is not None else entity_type_tuple
            )
            for target in targets:
                by_type[target].append(identity)
        # De-duplicate each type slice while preserving order.
        quantities_by_type = {
            entity_type: tuple(dict.fromkeys(by_type[entity_type]))
            for entity_type in entity_type_tuple
        }
        rules.append(
            TakeoffRule(
                rule_id=rule_id,
                root_source_id=root_source,
                root_global_id=root_gid,
                entity_types=entity_type_tuple,
                quantities=tuple(quantities),
                quantities_by_type=quantities_by_type,
            )
        )
    return TakeoffRules(
        rules=tuple(rules),
        raw=raw,
        sha256=sha256_file(path),
        path=os.path.abspath(path),
    )
