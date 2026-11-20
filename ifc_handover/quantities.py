"""Parsing and exact interpretation of IFC physical quantities.

This is the quantity counterpart of :mod:`ifc_handover.values`: parsing is
deliberately separate from project-unit interpretation.

* :class:`QuantityReader` turns an ``IfcPhysicalQuantity`` STEP entity into a
  :class:`RawQuantity` -- its name, raw decimal spelling, explicit unit and
  structural state -- without consulting project units.
* :func:`interpret_quantity` applies a :class:`~ifc_handover.units.UnitResolver`
  and one project's unit assignment to that raw quantity.

Only the five ``IfcPhysicalSimpleQuantity`` subtypes required for spatial
take-off are interpreted:

============================== ================= ===========
entity                         value attribute   dimension
============================== ================= ===========
IfcQuantityCount               CountValue        COUNT
IfcQuantityLength              LengthValue       LENGTH
IfcQuantityArea                AreaValue         AREA
IfcQuantityVolume              VolumeValue       VOLUME
IfcQuantityWeight              WeightValue       MASS
============================== ================= ===========

Every other subclass of ``IfcPhysicalSimpleQuantity`` (and every
``IfcPhysicalComplexQuantity``) is retained as an *unsupported* declaration
with the reason; it is never dropped and never assigned a guessed value.

Values are read from the raw STEP arguments as exact :class:`fractions.Fraction`
numbers; binary floats are never involved.  Canonical units are ``count``,
``m``, ``m²``, ``m³`` and ``kg``.  A non-finite value, a dimension mismatch,
an offset unit or a unit reference cycle is a distinct failure state, never
zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .schema import canonical_entity_name, entity_attribute_map
from .step import (
    DERIVED,
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
    exact_si_decimal,
)

# Structural states for a raw quantity.
QSTATE_VALUE = "value"
QSTATE_EXPLICIT_NULL = "explicit_null"
QSTATE_UNSUPPORTED = "unsupported"

# Interpretation statuses (the six states a take-off line can end in are
# defined on the take-off dataset; here we only classify the declaration).
QSTATUS_OK = "ok"
QSTATUS_MISSING = "missing"
QSTATUS_UNSUPPORTED = "unsupported_type"
QSTATUS_MISSING_UNIT = "missing_unit"
QSTATUS_DIMENSION_MISMATCH = "dimension_mismatch"
QSTATUS_UNIT_CYCLE = "unit_cycle"
QSTATUS_UNRESOLVED_UNIT = "unresolved_unit"
QSTATUS_NON_FINITE = "non_finite_value"
QSTATUS_EXPLICIT_NULL = "explicit_null"
QSTATUS_CONFLICT = "conflict"

# The five supported simple quantity subtypes: (entity, value attr, measure
# type, dimension, canonical unit).  In IFC4 every IfcPhysicalSimpleQuantity
# has the argument order (Name, Description, Unit, <Value>, Formula).
QUANTITY_SPECS: dict[str, tuple[str, str, str, str, str]] = {
    "IfcQuantityCount": ("CountValue", "IfcCountMeasure", "COUNT", "count"),
    "IfcQuantityLength": ("LengthValue", "IfcLengthMeasure", "LENGTH", "m"),
    "IfcQuantityArea": ("AreaValue", "IfcAreaMeasure", "AREA", "m²"),
    "IfcQuantityVolume": ("VolumeValue", "IfcVolumeMeasure", "VOLUME", "m³"),
    "IfcQuantityWeight": ("WeightValue", "IfcWeightMeasure", "MASS", "kg"),
}

# Canonical unit per dimension.
CANONICAL_UNIT = {
    "COUNT": "count",
    "LENGTH": "m",
    "AREA": "m²",
    "VOLUME": "m³",
    "MASS": "kg",
}

# Measure type -> dimension, including the count measure that the property
# interpreter deliberately leaves uninterpreted.
MEASURE_DIMENSION: dict[str, str] = {
    "IFCCOUNTMEASURE": "COUNT",
    "IFCLENGTHMEASURE": "LENGTH",
    "IFCAREAMEASURE": "AREA",
    "IFCVOLUMEMEASURE": "VOLUME",
    "IFCWEIGHTMEASURE": "MASS",
}


@dataclass
class RawQuantity:
    """A parsed quantity declaration, before project-unit interpretation."""

    q_step_id: int
    q_kind: str
    name: str | None
    description: str | None
    formula: str | None
    unit_step_id: int | None
    value_attr: str | None
    measure_type: str | None
    dimension: str | None
    original_raw: str | None
    numeric: Fraction | None
    structural_state: str  # value | explicit_null | unsupported
    reason: str | None
    raw_attributes: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "q_step_id": self.q_step_id,
            "q_kind": self.q_kind,
            "name": self.name,
            "description": self.description,
            "formula": self.formula,
            "unit_step_id": self.unit_step_id,
            "value_attr": self.value_attr,
            "measure_type": self.measure_type,
            "dimension": self.dimension,
            "original_raw": self.original_raw,
            "numeric": (
                None
                if self.numeric is None
                else {
                    "numerator": str(self.numeric.numerator),
                    "denominator": str(self.numeric.denominator),
                }
            ),
            "structural_state": self.structural_state,
            "reason": self.reason,
            "raw_attributes": self.raw_attributes,
            "extra": self.extra,
        }


@dataclass
class QuantityInterpretation:
    """The result of interpreting one raw quantity for one project."""

    status: str
    dimension: str | None
    declared_type: str | None
    original_raw: str | None
    original_numeric: Fraction | None
    canonical_value: Fraction | None
    canonical_decimal: str | None
    canonical_decimal_exact: bool
    canonical_unit: str | None
    unit_source: str | None  # explicit | project | none
    reason: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    measured: MeasuredValue | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dimension": self.dimension,
            "declared_type": self.declared_type,
            "original_raw": self.original_raw,
            "original_numeric": (
                None
                if self.original_numeric is None
                else {
                    "numerator": str(self.original_numeric.numerator),
                    "denominator": str(self.original_numeric.denominator),
                }
            ),
            "canonical_value": (
                None
                if self.canonical_value is None
                else {
                    "numerator": str(self.canonical_value.numerator),
                    "denominator": str(self.canonical_value.denominator),
                }
            ),
            "canonical_decimal": self.canonical_decimal,
            "canonical_decimal_exact": self.canonical_decimal_exact,
            "canonical_unit": self.canonical_unit,
            "unit_source": self.unit_source,
            "reason": self.reason,
            "detail": dict(self.detail),
            "unit": None if self.measured is None or self.measured.unit is None
            else self.measured.unit.as_json(),
        }


class QuantityReader:
    """Parse ``IfcPhysicalQuantity`` entities from raw STEP arguments."""

    def __init__(self, step: StepFile, schema_name: str):
        self.step = step
        self.schema_name = schema_name
        self.attr_map = entity_attribute_map(schema_name)

    def _attributes(self, step_id: int, entity_type: str) -> dict[str, Any]:
        names = self.attr_map.get(entity_type) or self.attr_map.get(entity_type.upper())
        if names is None:
            return {}
        args = self.step.args(step_id)
        return {name: (args[i] if i < len(args) else None) for i, name in enumerate(names)}

    def read(self, step_id: int, entity_type: str) -> RawQuantity:
        entity_type = canonical_entity_name(self.schema_name, entity_type)
        names = self.attr_map.get(entity_type) or self.attr_map.get(entity_type.upper())
        args = self.step.args(step_id)
        raw_attrs = _plain_attrs(args)
        name = description = formula = None
        if names is not None:
            attrs = {name: (args[i] if i < len(args) else None) for i, name in enumerate(names)}
            name = _str(attrs.get("Name"))
            description = _str(attrs.get("Description"))
            formula = _str(attrs.get("Formula"))

        def unsupported(reason: str, **extra: Any) -> RawQuantity:
            return RawQuantity(
                q_step_id=step_id,
                q_kind=entity_type,
                name=name,
                description=description,
                formula=formula,
                unit_step_id=None,
                value_attr=None,
                measure_type=None,
                dimension=None,
                original_raw=None,
                numeric=None,
                structural_state=QSTATE_UNSUPPORTED,
                reason=reason,
                raw_attributes=raw_attrs,
                extra=dict(extra),
            )

        if names is None:
            return unsupported(f"entity type {entity_type} is not declared in {self.schema_name}")
        if len(args) != len(names):
            # Reading by position would mis-attribute the value; refuse.
            return unsupported(
                f"{entity_type} has {len(args)} arguments but the {self.schema_name} "
                f"schema declares {len(names)} attributes",
                attribute_count_mismatch={"found": len(args), "expected": len(names)},
            )
        spec = QUANTITY_SPECS.get(entity_type)
        if spec is None:
            # IfcPhysicalComplexQuantity or another simple subtype: retain,
            # never drop or guess.
            return unsupported(
                f"quantity kind {entity_type} is not one of the five supported "
                "IfcPhysicalSimpleQuantity subtypes"
            )
        value_attr, measure_type, dimension, _canonical = spec
        attrs = {n: (args[i] if i < len(args) else None) for i, n in enumerate(names)}
        unit_arg = attrs.get("Unit")
        unit_step = unit_arg.step_id if isinstance(unit_arg, Ref) else None
        value = attrs.get(value_attr)
        if value is None or value is DERIVED:
            return RawQuantity(
                q_step_id=step_id,
                q_kind=entity_type,
                name=name,
                description=description,
                formula=formula,
                unit_step_id=unit_step,
                value_attr=value_attr,
                measure_type=measure_type,
                dimension=dimension,
                original_raw=None,
                numeric=None,
                structural_state=QSTATE_EXPLICIT_NULL,
                reason=f"{value_attr} is explicitly null" if value is None
                else f"{value_attr} is derived (*) and not evaluated",
                raw_attributes=raw_attrs,
            )
        number = _number(value)
        if number is None:
            return unsupported(
                f"{entity_type}.{value_attr} must be an {measure_type} number, "
                f"got {value!r}"
            )
        numeric, raw_text, finite = number
        if not finite:
            return RawQuantity(
                q_step_id=step_id,
                q_kind=entity_type,
                name=name,
                description=description,
                formula=formula,
                unit_step_id=unit_step,
                value_attr=value_attr,
                measure_type=measure_type,
                dimension=dimension,
                original_raw=raw_text,
                numeric=None,
                structural_state=QSTATE_UNSUPPORTED,
                reason=f"non-finite numeric value {raw_text} cannot be accumulated",
                raw_attributes=raw_attrs,
                extra={"non_finite": True},
            )
        return RawQuantity(
            q_step_id=step_id,
            q_kind=entity_type,
            name=name,
            description=description,
            formula=formula,
            unit_step_id=unit_step,
            value_attr=value_attr,
            measure_type=measure_type,
            dimension=dimension,
            original_raw=raw_text,
            numeric=numeric,
            structural_state=QSTATE_VALUE,
            reason=None,
            raw_attributes=raw_attrs,
        )


def _number(value: Any) -> tuple[Fraction, str, bool] | None:
    """Extract an exact Fraction from a STEP numeric or typed numeric value."""

    if isinstance(value, NumberValue):
        raw = value.raw
        dec = value.value
    elif isinstance(value, TypedValue) and len(value.args) == 1 and isinstance(
        value.args[0], NumberValue
    ):
        raw = value.args[0].raw
        dec = value.args[0].value
    else:
        return None
    finite = _is_finite(dec)
    try:
        numeric = Fraction(dec)
    except (ValueError, ArithmeticError):
        finite = False
        numeric = Fraction(0)
    return numeric, raw, finite


def _is_finite(dec: Decimal) -> bool:
    return dec.is_finite()


def _str(value: Any) -> str | None:
    if isinstance(value, StringValue):
        return value.value
    if isinstance(value, str):
        return value
    return None


def _plain(value: Any) -> Any:
    from .step import EnumValue, ListValue

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


def _plain_attrs(args: tuple[Any, ...]) -> dict[str, Any]:
    return {f"arg_{i}": _plain(arg) for i, arg in enumerate(args)}


def interpret_quantity(
    raw: RawQuantity,
    interpreter: ValueInterpreter,
    resolver: UnitResolver,
    project_units: dict[str, dict[str, Any]],
) -> QuantityInterpretation:
    """Interpret one raw quantity against one project's unit assignment.

    Pure function: no I/O, no state on the reader.  An explicit unit on the
    quantity always wins; otherwise the project unit for the quantity's
    dimension is used.  Count quantities have no physical dimension and are
    kept as exact numbers in the canonical ``count`` unit.
    """

    base = QuantityInterpretation(
        status=QSTATUS_UNSUPPORTED,
        dimension=raw.dimension,
        declared_type=raw.measure_type,
        original_raw=raw.original_raw,
        original_numeric=raw.numeric,
        canonical_value=None,
        canonical_decimal=None,
        canonical_decimal_exact=False,
        canonical_unit=CANONICAL_UNIT.get(raw.dimension or ""),
        unit_source=None,
        reason=raw.reason,
    )
    if raw.structural_state == QSTATE_EXPLICIT_NULL:
        base.status = QSTATUS_EXPLICIT_NULL
        return base
    if raw.structural_state == QSTATE_UNSUPPORTED:
        if raw.extra.get("non_finite"):
            base.status = QSTATUS_NON_FINITE
        else:
            base.status = QSTATUS_UNSUPPORTED
        return base
    if raw.numeric is None or raw.dimension is None:
        base.status = QSTATUS_UNSUPPORTED
        return base

    numeric = raw.numeric
    dimension = raw.dimension

    if dimension == "COUNT":
        # Counts are dimensionless.  An explicit unit on a count is only
        # accepted when it carries no physical dimension; otherwise the
        # declaration's dimension is wrong for a count.
        if raw.unit_step_id is not None:
            unit = resolver.resolve(raw.unit_step_id)
            if unit.dimension is not None:
                base.status = QSTATUS_DIMENSION_MISMATCH
                base.unit_source = "explicit"
                base.reason = (
                    f"count quantity declares a {unit.dimension} unit; a count "
                    "has no physical dimension"
                )
                base.detail = {"unit_step_id": raw.unit_step_id}
                return base
            if unit.scale is not None and unit.scale != 1:
                base.status = QSTATUS_DIMENSION_MISMATCH
                base.unit_source = "explicit"
                base.reason = "count quantity carries a non-unit scaling factor"
                return base
            base.unit_source = "explicit"
        else:
            base.unit_source = "none"
        return _ok(base, numeric)

    # Physical dimension: explicit unit wins, else the project unit.
    if raw.unit_step_id is not None:
        measured = interpreter.interpret(
            raw.measure_type or "",
            numeric,
            raw.unit_step_id,
            original_raw=raw.original_raw,
        )
        measured_unit_source = "explicit"
    else:
        candidate = project_units.get(dimension)
        if candidate is None:
            base.status = QSTATUS_MISSING_UNIT
            base.unit_source = "none"
            base.reason = (
                f"quantity has no unit and the project assigns no {dimension} unit"
            )
            base.detail = {
                "project_units_available": sorted(project_units),
            }
            return base
        measured = interpreter.interpret(
            raw.measure_type or "",
            numeric,
            None,
            original_raw=raw.original_raw,
        )
        measured_unit_source = str(candidate.get("source", "project"))
    base.measured = measured
    base.unit_source = measured_unit_source
    if measured.status != "ok":
        base.status = _map_measured_status(measured.status)
        base.reason = measured.reason
        return base
    # Defence in depth: the interpreter's dimension must agree with the
    # quantity subtype's dimension.
    if measured.dimension != dimension:
        base.status = QSTATUS_DIMENSION_MISMATCH
        base.reason = (
            f"{raw.q_kind} requires {dimension} but its value resolves as "
            f"{measured.dimension}"
        )
        return base
    if measured.si_value is None:
        base.status = QSTATUS_UNRESOLVED_UNIT
        base.reason = "unit resolved without a canonical value"
        return base
    return _ok(base, measured.si_value, measured=measured)


def _map_measured_status(status: str) -> str:
    mapping = {
        "missing_unit": QSTATUS_MISSING_UNIT,
        "dimension_mismatch": QSTATUS_DIMENSION_MISMATCH,
        "unit_cycle": QSTATUS_UNIT_CYCLE,
        "unresolved_unit": QSTATUS_UNRESOLVED_UNIT,
        "uninterpreted": QSTATUS_UNSUPPORTED,
        "unsupported_type": QSTATUS_UNSUPPORTED,
    }
    return mapping.get(status, QSTATUS_UNRESOLVED_UNIT)


def _ok(
    base: QuantityInterpretation,
    value: Fraction,
    *,
    measured: MeasuredValue | None = None,
) -> QuantityInterpretation:
    base.status = QSTATUS_OK
    base.canonical_value = value
    base.canonical_unit = CANONICAL_UNIT.get(base.dimension or "")
    base.canonical_decimal, base.canonical_decimal_exact = exact_si_decimal(value)
    base.reason = None
    base.measured = measured
    return base


def quantity_identity(quantity_set_name: str, quantity_name: str | None, q_kind: str) -> str:
    """Identity of a quantity declaration.

    The identity is ``(quantity set name, quantity name, declared subtype)``.
    Two quantities with the same set and name but different declared types
    (e.g. an ``IfcQuantityLength`` and an ``IfcQuantityArea`` both called
    ``NetSize``) never share an identity and are never merged.
    """

    return f"{quantity_set_name}\u001f{quantity_name or ''}\u001f{q_kind}"


def quantity_signature_key(raw: RawQuantity, resolver: UnitResolver) -> tuple:
    """Hashable equality key for merging identical quantity declarations.

    Covers the kind, exact numeric value, explicit null/unsupported state,
    formula and the resolved explicit unit (STEP ids stripped).  Project units
    are excluded, mirroring property declaration equality.
    """

    unit_sig = None
    if raw.unit_step_id is not None:
        unit_json = resolver.resolve(raw.unit_step_id).as_json()
        unit_sig = _hashable(_strip_ids(unit_json))
    value = None
    if raw.structural_state == QSTATE_VALUE and raw.numeric is not None:
        value = (raw.structural_state, str(raw.numeric.numerator), str(raw.numeric.denominator))
    else:
        value = (raw.structural_state, raw.reason)
    return (raw.q_kind, value, unit_sig, raw.formula, raw.original_raw)


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
