"""Canonical business fingerprints for the semantics already resolved by the store.

The diff never re-interprets raw IFC.  It compares the business semantics the
repository already resolved and normalises them into fingerprints whose
equality means "same business meaning":

* numeric measured values compare on their exact SI value, so a 2 m writing and
  a 2000 mm writing of the same length are equal even though the raw spellings,
  declared units and unit sources differ;
* the five property states -- explicit null, missing, conflict, unsupported and
  unevaluated -- stay distinct and are never folded into "" or 0;
* declaration provenance (instance vs type scope, source STEP ids) is carried
  separately from the business fingerprint, so moving a property from a type
  declaration to an instance declaration with the same effective value is a
  provenance change, not a value change.
"""

from __future__ import annotations

import json
from typing import Any

# Property states, mirrored from values.py but restated here so the diff layer
# has one explicit vocabulary to diff against.
STATE_ABSENT = "absent"
STATE_EXPLICIT_NULL = "explicit_null"
STATE_VALUE = "value"
STATE_UNSUPPORTED = "unsupported"
STATE_EXPRESSION = "expression_unevaluated"
STATE_CONFLICT = "conflict"

# A property row that cannot be diffed deterministically on either side.
NON_COMPARABLE_STATES = frozenset({STATE_CONFLICT})


def load_detail(row: Any) -> dict[str, Any]:
    text = row["json"] if "json" in row.keys() else None
    if not text:
        return {}
    return json.loads(text)


def property_business_fingerprint(row: Any) -> dict[str, Any]:
    """Business-value fingerprint of one resolved property row.

    Two rows with equal fingerprints carry the same effective business value,
    regardless of raw spelling, declared unit or declaration scope.
    """

    detail = load_detail(row)
    state = row["state"]
    value_status = row["value_status"]
    if state == STATE_CONFLICT:
        return {"state": STATE_CONFLICT, "value_status": value_status}
    if state == STATE_EXPLICIT_NULL:
        return {"state": STATE_EXPLICIT_NULL, "value_status": value_status}
    if state == STATE_EXPRESSION:
        # The source expression text is part of the retained meaning.
        declaration = detail.get("declaration") or {}
        return {
            "state": STATE_EXPRESSION,
            "value_status": value_status,
            "expression": declaration.get("expression"),
        }
    if state == STATE_UNSUPPORTED:
        return {
            "state": STATE_UNSUPPORTED,
            "value_status": value_status,
            "reason": row["reason"],
        }
    # state == "value": compare on the resolved measured/typed payload.
    declaration = detail.get("declaration") or {}
    interpretation = declaration.get("interpretation") or {}
    measured = interpretation.get("measured") or []
    values: list[Any] = []
    for item in measured:
        if item is None:
            values.append(None)
            continue
        values.append(_measured_fingerprint(item))
    # Non-numeric interpreted values (labels, booleans, enums) are not present
    # in `measured`; fall back to the typed value summaries.
    if not measured:
        for summary in declaration.get("values") or []:
            values.append(_summary_fingerprint(summary))
    return {
        "state": STATE_VALUE,
        "value_status": value_status,
        "values": values,
    }


def _measured_fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status")
    base = {
        "declared_type": item.get("declared_type"),
        "dimension": item.get("dimension"),
        "status": status,
    }
    if status == "ok":
        # The exact SI value is the unit-independent business value.  Two
        # spellings that normalise to the same fraction are equal.
        base["si_value"] = item.get("si_value")
        return base
    # Failure statuses (missing_unit, dimension_mismatch, unit_cycle,
    # unresolved_unit): the SI value is unavailable, so the exact original
    # numeric/typed value is the only comparable content.
    base["si_value"] = None
    base["original_numeric"] = item.get("original_numeric")
    base["reason"] = item.get("reason")
    return base


def _summary_fingerprint(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": summary.get("kind"),
        "canonical": summary.get("canonical"),
        "schema_valid": summary.get("schema_valid"),
    }


def property_provenance(row: Any) -> dict[str, Any]:
    """Declaration provenance, deliberately separate from the value.

    Captures where the effective declaration came from (instance vs type) and
    which source entities back it, without STEP ids becoming identity.
    """

    detail = load_detail(row)
    sources = detail.get("sources") or []
    return {
        "scope_used": row["scope_used"],
        "prop_step_id": _first(sources, "step_id"),
        "pset_step_id": _pset_step_id(sources),
        "merged_source_count": detail.get("merged_source_count"),
        "had_type_declaration_masked": bool(detail.get("type_declaration_ignored")),
    }


def _pset_step_id(sources: list[dict[str, Any]]) -> int | None:
    for source in sources:
        pset_key = source.get("pset_key") or ""
        # pset keys are pset:<sha>:<step_id>
        tail = pset_key.rsplit(":", 1)
        if len(tail) == 2 and tail[1].isdigit():
            return int(tail[1])
    return None


def _first(sources: list[dict[str, Any]], key: str) -> Any:
    for source in sources:
        if source.get(key) is not None:
            return source[key]
    return None


def material_tree_fingerprint(
    nodes: list[Any], edges: list[Any]
) -> dict[str, Any]:
    """Ordered structural fingerprint of a material tree.

    Layer order is significant: the same layer materials in a different order
    produce a different fingerprint, and each layer keeps its ordinal so a
    change can be located to a specific layer.  STEP ids are intentionally
    excluded: they are source-local provenance and must not make two identical
    structures look different across versions.
    """

    node_index = {row["key"]: row for row in nodes}
    ordered_layers: list[dict[str, Any]] = []
    layer_materials: dict[str, str] = {}
    other_edges: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda e: (e["slot"], e["ordinal"])):
        if edge["slot"] == "layer":
            child = node_index.get(edge["child_key"])
            ordered_layers.append(
                {
                    "ordinal": edge["ordinal"],
                    "layer_name": child["name"] if child is not None else None,
                    "layer_detail": _node_detail(child),
                }
            )
        elif edge["slot"] == "layer_material":
            parent = node_index.get(edge["parent_key"])
            child = node_index.get(edge["child_key"])
            if parent is not None and child is not None:
                layer_materials[parent["key"]] = child["name"]
        else:
            other_edges.append(
                {
                    "slot": edge["slot"],
                    "ordinal": edge["ordinal"],
                    "child_name": None
                    if node_index.get(edge["child_key"]) is None
                    else node_index[edge["child_key"]]["name"],
                }
            )
    # Resolve each layer's material by the layer node's own key.
    for layer in ordered_layers:
        layer_key = next(
            (
                key
                for key, row in node_index.items()
                if row["name"] == layer["layer_name"] and row["kind"] == "layer"
            ),
            None,
        )
        layer["material_name"] = layer_materials.get(layer_key)
    return {
        "layers": ordered_layers,
        "other_edges": other_edges,
        "node_kinds": sorted(
            (row["kind"] + ":" + (row["name"] or "")) for row in nodes
        ),
    }


def _node_detail(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return json.loads(row["json"]) if row["json"] else {}
    except (TypeError, ValueError):
        return {}
