"""Typed value extraction and property declaration parsing.

Parsing is deliberately separate from interpretation.

* :class:`PropertyReader` turns an ``IfcProperty`` STEP entity into a raw
  declaration: its declared values, explicit unit reference, expression text
  and structural state.  It does not consult project units.
* :func:`interpret_declaration` applies a :class:`ValueInterpreter` (which
  knows one project's unit assignment) to that declaration.  The same type
  declaration can therefore be interpreted differently for objects in
  different projects, and an instance declaration is never silently replaced
  by its type declaration.

Values are read from raw STEP arguments, never from Python floats, so decimal
spellings and conversion factors stay exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .schema import (
    canonical_entity_name,
    entity_attribute_map,
    value_type_names,
)
from .step import (
    DERIVED,
    BinaryValue,
    EnumValue,
    ListValue,
    NumberValue,
    Ref,
    StepFile,
    StringValue,
    TypedValue,
)
from .units import (
    MeasuredValue,
    UnitResolver,
    ValueInterpreter,
    fraction_json,
    measure_dimension,
)

PROPERTY_SINGLE_VALUE = "IfcPropertySingleValue"
PROPERTY_ENUMERATED_VALUE = "IfcPropertyEnumeratedValue"

# Resolved property states -------------------------------------------------
STATE_ABSENT = "absent"                      # query-only: no declaration
STATE_EXPLICIT_NULL = "explicit_null"        # declared with a null value
STATE_VALUE = "value"                        # declared with an actual value
STATE_UNSUPPORTED = "unsupported"            # unsupported property/value type
STATE_EXPRESSION = "expression_unevaluated"  # source expression, not evaluated
STATE_CONFLICT = "conflict"                  # same identity, differing values

# Value status -------------------------------------------------------------
VALUE_OK = "ok"
VALUE_UNINTERPRETED = "uninterpreted"
VALUE_MISSING_UNIT = "missing_unit"
VALUE_DIMENSION_MISMATCH = "dimension_mismatch"
VALUE_UNIT_CYCLE = "unit_cycle"
VALUE_UNRESOLVED_UNIT = "unresolved_unit"
VALUE_UNSUPPORTED = "unsupported_type"
VALUE_EXPLICIT_NULL = "explicit_null"
VALUE_UNEVALUATED = "unevaluated"
VALUE_CONFLICT = "conflict"

MEASUREMENT_FAILURES = {
    VALUE_MISSING_UNIT,
    VALUE_DIMENSION_MISMATCH,
    VALUE_UNIT_CYCLE,
    VALUE_UNRESOLVED_UNIT,
}


@dataclass
class ValueSummary:
    """A typed value in business-neutral form."""

    type_name: str | None
    raw: str
    kind: str  # number | string | boolean | logical | enum | list | binary | null | other
    numeric: Fraction | None = None
    text: str | None = None
    boolean: bool | None = None
    items: tuple["ValueSummary", ...] = ()
    schema_valid: bool = False
    unsupported_reason: str | None = None

    def canonical(self) -> Any:
        """Value-only payload used for equality of declarations."""

        if self.kind == "number" and self.numeric is not None:
            return {"kind": "number", "value": fraction_json(self.numeric)}
        if self.kind in {"string", "enum"}:
            return {"kind": self.kind, "value": self.text}
        if self.kind in {"boolean", "logical"}:
            return {"kind": self.kind, "value": self.boolean}
        if self.kind == "binary":
            return {"kind": "binary", "value": self.raw}
        if self.kind == "list":
            return {"kind": "list", "value": [i.canonical() for i in self.items]}
        return {"kind": self.kind, "value": self.raw}

    def as_json(self) -> dict[str, Any]:
        return {
            "declared_type": self.type_name,
            "raw": self.raw,
            "kind": self.kind,
            "numeric": None if self.numeric is None else fraction_json(self.numeric),
            "text": self.text,
            "boolean": self.boolean,
            "items": [i.as_json() for i in self.items],
            "schema_valid": self.schema_valid,
            "unsupported_reason": self.unsupported_reason,
            "canonical": self.canonical(),
        }


def summarise_typed_value(
    value: Any,
    schema_name: str,
    *,
    valid_types: frozenset[str] | None = None,
) -> ValueSummary:
    """Turn a raw STEP typed value into a :class:`ValueSummary`."""

    valid = valid_types if valid_types is not None else value_type_names(schema_name)
    if not isinstance(value, TypedValue):
        summary = _summarise_bare(value)
        summary.schema_valid = False
        summary.unsupported_reason = "value is not written as a typed IFC value"
        return summary
    type_name = value.type_name
    schema_valid = type_name in valid
    raw = value.raw
    if not schema_valid:
        return ValueSummary(
            type_name=type_name,
            raw=raw,
            kind="other",
            schema_valid=False,
            unsupported_reason=f"{type_name} is not a member of IfcValue in {schema_name}",
        )
    args = value.args
    if len(args) == 1 and isinstance(args[0], NumberValue):
        return ValueSummary(
            type_name=type_name,
            raw=raw,
            kind="number",
            numeric=Fraction(args[0].value),
            schema_valid=True,
        )
    if len(args) == 1 and isinstance(args[0], StringValue):
        return ValueSummary(
            type_name=type_name, raw=raw, kind="string", text=args[0].value, schema_valid=True
        )
    if len(args) == 1 and isinstance(args[0], EnumValue):
        upper = type_name.upper()
        name = args[0].name.upper()
        if upper == "IFCBOOLEAN":
            return ValueSummary(
                type_name=type_name,
                raw=raw,
                kind="boolean",
                boolean=name == "T",
                schema_valid=True,
            )
        if upper == "IFCLOGICAL":
            return ValueSummary(
                type_name=type_name,
                raw=raw,
                kind="logical",
                boolean=None if name == "U" else name == "T",
                schema_valid=True,
            )
        return ValueSummary(
            type_name=type_name, raw=raw, kind="enum", text=args[0].name, schema_valid=True
        )
    if len(args) == 1 and isinstance(args[0], BinaryValue):
        return ValueSummary(
            type_name=type_name, raw=raw, kind="binary", text=args[0].raw, schema_valid=True
        )
    if len(args) == 1 and isinstance(args[0], ListValue):
        return ValueSummary(
            type_name=type_name,
            raw=raw,
            kind="list",
            items=tuple(_summarise_bare(i) for i in args[0].items),
            schema_valid=True,
        )
    return ValueSummary(
        type_name=type_name,
        raw=raw,
        kind="other",
        schema_valid=True,
        unsupported_reason="typed value has an unsupported argument shape",
    )


def _summarise_bare(value: Any) -> ValueSummary:
    if isinstance(value, NumberValue):
        return ValueSummary(
            type_name=None, raw=value.raw, kind="number",
            numeric=Fraction(value.value), schema_valid=False,
        )
    if isinstance(value, StringValue):
        return ValueSummary(
            type_name=None, raw=value.raw, kind="string", text=value.value, schema_valid=False
        )
    if isinstance(value, EnumValue):
        return ValueSummary(
            type_name=None, raw=f".{value.name}.", kind="enum",
            text=value.name, schema_valid=False,
        )
    if isinstance(value, BinaryValue):
        return ValueSummary(
            type_name=None, raw=value.raw, kind="binary", text=value.raw, schema_valid=False
        )
    if isinstance(value, ListValue):
        return ValueSummary(
            type_name=None,
            raw=value.raw,
            kind="list",
            items=tuple(_summarise_bare(i) for i in value.items),
            schema_valid=False,
        )
    if value is None:
        return ValueSummary(type_name=None, raw="$", kind="null", schema_valid=False)
    if value is DERIVED:
        return ValueSummary(type_name=None, raw="*", kind="derived", schema_valid=False)
    return ValueSummary(type_name=None, raw=repr(value), kind="other", schema_valid=False)


@dataclass
class RawDeclaration:
    """A parsed property declaration, before project-unit interpretation."""

    prop_step_id: int
    prop_kind: str
    name: str | None
    description: str | None
    expression: str | None
    unit_step_id: int | None
    values: list[ValueSummary]
    structural_state: str  # value | explicit_null | unsupported | expression_unevaluated
    reason: str | None
    raw_attributes: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def canonical_values(self) -> list[Any]:
        return [v.canonical() for v in self.values]

    def as_json(self) -> dict[str, Any]:
        return {
            "prop_step_id": self.prop_step_id,
            "prop_kind": self.prop_kind,
            "name": self.name,
            "description": self.description,
            "expression": self.expression,
            "unit_step_id": self.unit_step_id,
            "values": [v.as_json() for v in self.values],
            "structural_state": self.structural_state,
            "reason": self.reason,
            "raw_attributes": self.raw_attributes,
            "extra": self.extra,
        }


@dataclass
class Interpretation:
    """The result of applying one project's units to one declaration."""

    state: str
    value_status: str
    measured: list[MeasuredValue | None]
    reason: str | None = None
    project_step_id: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "value_status": self.value_status,
            "measured": [None if m is None else m.as_json() for m in self.measured],
            "reason": self.reason,
            "project_step_id": self.project_step_id,
        }


class PropertyReader:
    """Parse supported and unsupported IfcProperty entities from raw STEP."""

    def __init__(
        self,
        step: StepFile,
        schema_name: str,
        resolver: UnitResolver,
    ):
        self.step = step
        self.schema_name = schema_name
        self.resolver = resolver
        self.attr_map = entity_attribute_map(schema_name)
        self.valid_types = value_type_names(schema_name)

    def attributes(self, step_id: int, entity_type: str) -> dict[str, Any]:
        names = self.attr_map.get(entity_type) or self.attr_map.get(entity_type.upper())
        if names is None:
            return {}
        args = self.step.args(step_id)
        return {name: (args[i] if i < len(args) else None) for i, name in enumerate(names)}

    def attribute_count_mismatch(self, step_id: int, entity_type: str) -> tuple[int, int] | None:
        names = self.attr_map.get(entity_type) or self.attr_map.get(entity_type.upper())
        if names is None:
            return None
        count = len(self.step.args(step_id))
        if count != len(names):
            return count, len(names)
        return None

    def read(self, step_id: int, entity_type: str) -> RawDeclaration:
        entity_type = canonical_entity_name(self.schema_name, entity_type)
        mismatch = self.attribute_count_mismatch(step_id, entity_type)
        if mismatch is not None:
            # Reading by position would silently attribute the wrong value to
            # the wrong attribute.  Refuse to interpret the entity instead.
            count, expected = mismatch
            return RawDeclaration(
                prop_step_id=step_id,
                prop_kind=entity_type,
                name=None,
                description=None,
                expression=None,
                unit_step_id=None,
                values=[],
                structural_state=STATE_UNSUPPORTED,
                reason=(
                    f"{entity_type} has {count} arguments but the {self.schema_name} "
                    f"schema declares {expected} attributes"
                ),
                raw_attributes={"_raw_args": repr(self.step.args(step_id))},
                extra={"attribute_count_mismatch": {"found": count, "expected": expected}},
            )
        attrs = self.attributes(step_id, entity_type)
        name = _str(attrs.get("Name"))
        description = _str(attrs.get("Description"))
        expression = _str(attrs.get("Expression"))
        formula = _str(attrs.get("Formula"))
        expression = expression or formula
        unit_arg = attrs.get("Unit")
        unit_step = unit_arg.step_id if isinstance(unit_arg, Ref) else None
        extra: dict[str, Any] = {}

        if entity_type == PROPERTY_SINGLE_VALUE:
            nominal = attrs.get("NominalValue")
            if nominal is None:
                return RawDeclaration(
                    prop_step_id=step_id,
                    prop_kind=entity_type,
                    name=name,
                    description=description,
                    expression=expression,
                    unit_step_id=unit_step,
                    values=[],
                    structural_state=STATE_EXPLICIT_NULL,
                    reason="NominalValue is explicitly null",
                    raw_attributes=_plain_attrs(attrs),
                )
            summary = summarise_typed_value(
                nominal, self.schema_name, valid_types=self.valid_types
            )
            structural_state, reason = _structural_state(summary)
            return RawDeclaration(
                prop_step_id=step_id,
                prop_kind=entity_type,
                name=name,
                description=description,
                expression=expression,
                unit_step_id=unit_step,
                values=[summary],
                structural_state=structural_state,
                reason=reason,
                raw_attributes=_plain_attrs(attrs),
            )

        if entity_type == PROPERTY_ENUMERATED_VALUE:
            values_arg = attrs.get("EnumerationValues")
            reference = attrs.get("EnumerationReference")
            enum_unit = None
            enum_name = None
            if isinstance(reference, Ref):
                enum_attrs = self.attributes(reference.step_id, "IfcPropertyEnumeration")
                enum_name = _str(enum_attrs.get("Name"))
                unit_arg2 = enum_attrs.get("Unit")
                if isinstance(unit_arg2, Ref):
                    enum_unit = unit_arg2.step_id
            items = values_arg.items if isinstance(values_arg, ListValue) else ()
            extra["enumeration_reference"] = {
                "step_id": reference.step_id if isinstance(reference, Ref) else None,
                "name": enum_name,
                "unit_step_id": enum_unit,
            }
            if not items:
                return RawDeclaration(
                    prop_step_id=step_id,
                    prop_kind=entity_type,
                    name=name,
                    description=description,
                    expression=expression,
                    unit_step_id=enum_unit,
                    values=[],
                    structural_state=STATE_EXPLICIT_NULL,
                    reason="EnumerationValues is empty or null",
                    raw_attributes=_plain_attrs(attrs),
                    extra=extra,
                )
            summaries = [
                summarise_typed_value(v, self.schema_name, valid_types=self.valid_types)
                for v in items
            ]
            states = [_structural_state(s) for s in summaries]
            if all(st == STATE_EXPRESSION for st, _ in states):
                structural_state = STATE_EXPRESSION
            elif all(st == STATE_UNSUPPORTED for st, _ in states):
                structural_state = STATE_UNSUPPORTED
            else:
                structural_state = STATE_VALUE
            reasons = [r for _, r in states if r]
            return RawDeclaration(
                prop_step_id=step_id,
                prop_kind=entity_type,
                name=name,
                description=description,
                expression=expression,
                unit_step_id=enum_unit,
                values=summaries,
                structural_state=structural_state,
                reason="; ".join(dict.fromkeys(reasons)) or None,
                raw_attributes=_plain_attrs(attrs),
                extra=extra,
            )

        # Unsupported property kind.  Keep expression text unaveraged and raw
        # attributes, never guess a numeric value.
        if expression:
            state = STATE_EXPRESSION
            reason = (
                f"{entity_type} carries a source expression; retained unevaluated"
            )
        else:
            state = STATE_UNSUPPORTED
            reason = f"property kind {entity_type} is not supported"
        return RawDeclaration(
            prop_step_id=step_id,
            prop_kind=entity_type,
            name=name,
            description=description,
            expression=expression,
            unit_step_id=unit_step,
            values=[],
            structural_state=state,
            reason=reason,
            raw_attributes=_plain_attrs(attrs),
            extra=extra,
        )


def _structural_state(summary: ValueSummary) -> tuple[str, str | None]:
    if not summary.schema_valid:
        return STATE_UNSUPPORTED, summary.unsupported_reason
    dimension, role = measure_dimension(summary.type_name)
    if role == "unsupported":
        return STATE_UNSUPPORTED, f"measure type {summary.type_name} is not supported"
    if role == "supported" and summary.kind != "number":
        return STATE_UNSUPPORTED, (
            f"{summary.type_name} requires a numeric value, got {summary.kind}"
        )
    if summary.kind == "other":
        return STATE_UNSUPPORTED, summary.unsupported_reason or "unsupported value shape"
    return STATE_VALUE, None


def interpret_declaration(
    declaration: RawDeclaration,
    interpreter: ValueInterpreter,
    *,
    project_step_id: int | None = None,
) -> Interpretation:
    """Apply units to a declaration.  Pure; no I/O."""

    if declaration.structural_state == STATE_EXPLICIT_NULL:
        return Interpretation(
            state=STATE_EXPLICIT_NULL,
            value_status=VALUE_EXPLICIT_NULL,
            measured=[],
            reason=declaration.reason,
            project_step_id=project_step_id,
        )
    if declaration.structural_state == STATE_EXPRESSION:
        return Interpretation(
            state=STATE_EXPRESSION,
            value_status=VALUE_UNEVALUATED,
            measured=[None for _ in declaration.values],
            reason=declaration.reason,
            project_step_id=project_step_id,
        )
    if declaration.structural_state == STATE_UNSUPPORTED:
        return Interpretation(
            state=STATE_UNSUPPORTED,
            value_status=VALUE_UNSUPPORTED,
            measured=[None for _ in declaration.values],
            reason=declaration.reason,
            project_step_id=project_step_id,
        )
    measured: list[MeasuredValue | None] = []
    statuses: list[str] = []
    reasons: list[str] = []
    for summary in declaration.values:
        if summary.kind != "number" or summary.numeric is None:
            # Valid non-numeric value (label, boolean, etc.).
            dimension, role = measure_dimension(summary.type_name)
            if role == "supported":
                statuses.append(VALUE_UNSUPPORTED)
                reasons.append(
                    f"{summary.type_name} requires a numeric value, got {summary.kind}"
                )
                measured.append(None)
            else:
                statuses.append(VALUE_UNINTERPRETED)
                measured.append(None)
            continue
        result = interpreter.interpret(
            summary.type_name,
            summary.numeric,
            declaration.unit_step_id,
            original_raw=summary.raw,
        )
        measured.append(result)
        statuses.append(result.status)
        if result.reason:
            reasons.append(result.reason)
    value_status = _combine_statuses(statuses)
    state = STATE_VALUE
    if value_status == VALUE_UNSUPPORTED:
        state = STATE_UNSUPPORTED
    return Interpretation(
        state=state,
        value_status=value_status,
        measured=measured,
        reason="; ".join(dict.fromkeys(reasons)) or None,
        project_step_id=project_step_id,
    )


def _combine_statuses(statuses: list[str]) -> str:
    if not statuses:
        return VALUE_UNSUPPORTED
    for status in (
        VALUE_MISSING_UNIT,
        VALUE_DIMENSION_MISMATCH,
        VALUE_UNIT_CYCLE,
        VALUE_UNRESOLVED_UNIT,
        VALUE_UNSUPPORTED,
        VALUE_UNINTERPRETED,
    ):
        if status in statuses:
            return status
    if VALUE_OK in statuses:
        return VALUE_OK
    return statuses[0]


def declaration_signature_key(
    declaration: RawDeclaration, resolver: UnitResolver
) -> tuple:
    """Hashable canonical equality key for merging identical declarations.

    This returns a hashable tuple and avoids materialising a JSON string per
    declaration, which matters for large files with hundreds of thousands of
    properties.  The comparison covers kind, declared values, the *resolved*
    explicit unit (STEP ids stripped) and the source expression.  Project units
    are deliberately excluded: two declarations with the same value and no unit
    are the same declaration even if their objects' projects would interpret
    them through different units.
    """

    unit_sig = None
    if declaration.unit_step_id is not None:
        unit_sig = _hashable(_strip_ids(resolver.resolve(declaration.unit_step_id).as_json()))
    values = tuple(
        _hashable(v.canonical()) for v in declaration.values
    )
    return (declaration.prop_kind, values, unit_sig, declaration.expression)


def _strip_ids(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _strip_ids(v) for k, v in data.items() if k != "unit_step_id"}
    if isinstance(data, list):
        return [_strip_ids(v) for v in data]
    return data


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def _str(value: Any) -> str | None:
    if isinstance(value, StringValue):
        return value.value
    if isinstance(value, str):
        return value
    return None


def _plain_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    return {key: _plain(value) for key, value in attrs.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, StringValue):
        return value.value
    if isinstance(value, NumberValue):
        return {"raw": value.raw, "decimal": str(value.value)}
    if isinstance(value, EnumValue):
        return value.name
    if isinstance(value, Ref):
        return {"step_id": value.step_id}
    if isinstance(value, ListValue):
        return [_plain(v) for v in value.items]
    if isinstance(value, TypedValue):
        return {"type": value.type_name, "raw": value.raw}
    if value is None:
        return None
    if value is DERIVED:
        return "*"
    return repr(value)
