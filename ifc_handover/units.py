"""Unit interpretation for measured property values.

Design constraints:

* Conversion is exact.  Factors are :class:`fractions.Fraction` values built
  from the decimal spellings in the source, so ``1000 mm -> 1 m`` is exact.
  A decimal rendering of the canonical SI value is produced only when the
  fraction's denominator terminates in base 10; otherwise the exact fraction
  is kept and the decimal is marked inexact.
* Only length, area, volume and mass are interpreted.  Other measures are
  carried with their declared type and left uninterpreted rather than guessed.
* Four failure modes are distinguished from a value of zero:
  ``missing_unit``, ``dimension_mismatch``, ``unit_cycle`` and
  ``unresolved_unit``.  A failed value is never silently dropped, nor are the
  object's other properties.
* Prefix powers derived from length are supported: an SI unit whose name is
  ``SQUARE_METRE`` / ``CUBIC_METRE`` with a prefix (e.g. ``MILLI``) gets the
  prefix raised to the dimension power (10^-3 -> 10^-6 area, 10^-9 volume).
* Offset-bearing conversion units are recognised but not applied to the four
  supported dimensions (an offset is meaningless for length/area/volume/mass).
  They are reported as unresolved evidence rather than silently using the
  factor alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

from .step import (
    EnumValue,
    ListValue,
    NumberValue,
    Ref,
    StepFile,
    StringValue,
    TypedValue,
    canonical_decimal,
)

SI_PREFIX_EXPONENT: dict[str, int] = {
    "EXA": 18,
    "PETA": 15,
    "TERA": 12,
    "GIGA": 9,
    "MEGA": 6,
    "KILO": 3,
    "HECTO": 2,
    "DECA": 1,
    "DECI": -1,
    "CENTI": -2,
    "MILLI": -3,
    "MICRO": -6,
    "NANO": -9,
    "PICO": -12,
    "FEMTO": -15,
    "ATTO": -18,
}

_DIMENSION_OF_UNIT_TYPE: dict[str, str] = {
    "LENGTHUNIT": "LENGTH",
    "AREAUNIT": "AREA",
    "VOLUMEUNIT": "VOLUME",
    "MASSUNIT": "MASS",
}

# (length, mass, time) exponents for the four supported dimensions.
_DIMENSION_EXPONENT: dict[str, tuple[int, int, int]] = {
    "LENGTH": (1, 0, 0),
    "AREA": (2, 0, 0),
    "VOLUME": (3, 0, 0),
    "MASS": (0, 1, 0),
}

# Measure type name -> dimension.  The supported set is deliberately small.
_MEASURE_DIMENSION: dict[str, str] = {
    "IFCLENGTHMEASURE": "LENGTH",
    "IFCNONNEGATIVELENGTHMEASURE": "LENGTH",
    "IFCPOSITIVELENGTHMEASURE": "LENGTH",
    "IFCPARAMETERVALUE": "LENGTH",
    "IFCAREAMEASURE": "AREA",
    "IFCVOLUMEMEASURE": "VOLUME",
    "IFCMASSMEASURE": "MASS",
}

# Known measure/type names that are valid IfcValue members but are outside the
# four interpretable dimensions.  These are carried as ``uninterpreted``.
_KNOWN_UNINTERPRETED = {
    "IFCREAL",
    "IFCINTEGER",
    "IFCINTEGERCOUNTRATEMEASURE",
    "IFCCOUNTMEASURE",
    "IFCNUMERICMEASURE",
    "IFCRATIOMEASURE",
    "IFCNORMALISEDRATIOMEASURE",
    "IFCPOSITIVERATIOMEASURE",
    "IFCPLANEANGLEMEASURE",
    "IFCPOSITIVEPLANEANGLEMEASURE",
    "IFCTHERMODYNAMICTEMPERATUREMEASURE",
    "IFCTIMEMEASURE",
    "IFCCONTEXTDEPENDENTMEASURE",
    "IFCLABEL",
    "IFCTEXT",
    "IFCIDENTIFIER",
    "IFCDESCRIPTIVEMEASURE",
    "IFCBOOLEAN",
    "IFCLOGICAL",
    "IFCDATE",
    "IFCDATETIME",
    "IFCTIME",
    "IFCDURATION",
    "IFCTIMESTAMP",
    "IFCBINARY",
    "IFCCOMPLEXNUMBER",
    "IFCMONETARYMEASURE",
    "IFCMOLECULARWEIGHTMEASURE",
    "IFCPHMEASURE",
    "IFCABSORBEDDOSEMEASURE",
    "IFCDOSEEQUIVALENTMEASURE",
    "IFCELECTRICCURRENTMEASURE",
    "IFCLUMINOUSINTENSITYMEASURE",
    "IFCSOLIDANGLEMEASURE",
}


class UnitError(Exception):
    """Base class for unit interpretation failures."""

    reason = "unresolved_unit"


class MissingUnit(UnitError):
    reason = "missing_unit"


class DimensionMismatch(UnitError):
    reason = "dimension_mismatch"


class UnitCycle(UnitError):
    reason = "unit_cycle"


@dataclass
class UnitInteger:
    """A resolved unit definition, with source provenance.

    ``scale`` converts a source magnitude to the canonical SI unit for
    ``dimension``.  It is ``None`` when the unit cannot be used.
    """

    unit_id: int
    kind: str  # si | conversion | derived | context | monetary | unsupported
    unit_type: str | None
    dimension: str | None
    scale: Fraction | None
    offset: Fraction = Fraction(0)
    label: str = ""
    prefix: str | None = None
    si_name: str | None = None
    exponent: int = 1
    elements: tuple[dict[str, Any], ...] = ()
    cycle: tuple[int, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.scale is not None and self.dimension is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "unit_step_id": self.unit_id,
            "kind": self.kind,
            "unit_type": self.unit_type,
            "dimension": self.dimension,
            "scale_to_si": (
                None
                if self.scale is None
                else fraction_json(self.scale)
            ),
            "offset_to_si": fraction_json(self.offset),
            "label": self.label,
            "prefix": self.prefix,
            "si_name": self.si_name,
            "exponent": self.exponent,
            "elements": list(self.elements),
            "cycle": list(self.cycle),
            "detail": dict(self.detail),
        }


def fraction_json(value: Fraction) -> dict[str, str]:
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _fraction_from_number(value: Any) -> Fraction:
    if isinstance(value, NumberValue):
        return Fraction(value.value)
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    raise UnitError(f"not a numeric conversion factor: {value!r}")


def _enum(value: Any) -> str | None:
    return value.name if isinstance(value, EnumValue) else None


class UnitResolver:
    """Resolve STEP unit entities into exact :class:`UnitInteger` objects.

    Bound to one :class:`StepFile`; caches results per STEP id and tracks the
    active resolution stack so reference cycles become explicit ``unit_cycle``
    evidence.  It never follows references across files.
    """

    def __init__(self, step_file: StepFile):
        self.step = step_file
        self._cache: dict[int, UnitInteger] = {}
        self._instack: list[int] = []

    # -- public API -----------------------------------------------------
    def resolve(self, unit_id: int) -> UnitInteger:
        """Resolve a unit.  Never raises; inspect ``usable``/``detail``."""

        if unit_id in self._cache:
            return self._cache[unit_id]
        if unit_id in self._instack:
            start = self._instack.index(unit_id)
            cycle = tuple(self._instack[start:] + [unit_id])
            result = UnitInteger(
                unit_id=unit_id,
                kind="cycle",
                unit_type=None,
                dimension=None,
                scale=None,
                cycle=cycle,
                detail={"reason": "unit_cycle", "message": f"unit reference cycle: {cycle}"},
            )
            return result
        existing = self.step.entities.get(unit_id)
        if existing is None:
            result = UnitInteger(
                unit_id=unit_id,
                kind="missing",
                unit_type=None,
                dimension=None,
                scale=None,
                detail={
                    "reason": "unresolved_unit",
                    "message": f"unit #{unit_id} is not present in the source file",
                },
            )
            self._cache[unit_id] = result
            return result
        if existing.type_name not in {
            "IFCSIUNIT",
            "IFCCONVERSIONBASEDUNIT",
            "IFCCONVERSIONBASEDUNITWITHOFFSET",
            "IFCDERIVEDUNIT",
            "IFCCONTEXTDEPENDENTUNIT",
            "IFCMONETARYUNIT",
        }:
            result = UnitInteger(
                unit_id=unit_id,
                kind="unsupported",
                unit_type=None,
                dimension=None,
                scale=None,
                detail={
                    "reason": "unresolved_unit",
                    "message": f"#{unit_id} is {existing.type_name}, not a unit entity",
                },
            )
            self._cache[unit_id] = result
            return result
        self._instack.append(unit_id)
        try:
            result = self._resolve_entity(unit_id, existing.type_name, self.step.args(unit_id))
        finally:
            self._instack.pop()
        self._cache[unit_id] = result
        return result

    def prefetch(self, unit_id: int) -> None:
        """Resolve now so the small unit graph is cached during ingestion."""

        self.resolve(unit_id)

    # -- internals ------------------------------------------------------
    def _resolve_entity(
        self, unit_id: int, type_name: str, args: tuple[Any, ...]
    ) -> UnitInteger:
        if type_name == "IFCSIUNIT":
            return self._resolve_si(unit_id, args)
        if type_name in {"IFCCONVERSIONBASEDUNIT", "IFCCONVERSIONBASEDUNITWITHOFFSET"}:
            return self._resolve_conversion(unit_id, args, type_name)
        if type_name == "IFCDERIVEDUNIT":
            return self._resolve_derived(unit_id, args)
        if type_name == "IFCCONTEXTDEPENDENTUNIT":
            unit_type = _enum(args[1]) if len(args) > 1 else None
            return UnitInteger(
                unit_id=unit_id,
                kind="context",
                unit_type=unit_type,
                dimension=_DIMENSION_OF_UNIT_TYPE.get(unit_type or ""),
                scale=None,
                label=str(args[2].value)
                if len(args) > 2 and isinstance(args[2], StringValue)
                else "",
                detail={
                    "reason": "unresolved_unit",
                    "message": "context-dependent unit has no offline definition",
                },
            )
        # Monetary unit
        return UnitInteger(
            unit_id=unit_id,
            kind="monetary",
            unit_type="MONETARYUNIT",
            dimension=None,
            scale=None,
            label=str(args[0].value) if args and isinstance(args[0], StringValue) else "",
            detail={
                "reason": "uninterpreted_unit",
                "message": "monetary units are outside the supported dimensions",
            },
        )

    def _resolve_si(self, unit_id: int, args: tuple[Any, ...]) -> UnitInteger:
        # IfcSIUnit: Dimensions (derived), UnitType, Prefix, Name
        unit_type = _enum(args[1]) if len(args) > 1 else None
        prefix = args[2].name if len(args) > 2 and isinstance(args[2], EnumValue) else None
        name = args[3].name if len(args) > 3 and isinstance(args[3], EnumValue) else None
        dimension = _DIMENSION_OF_UNIT_TYPE.get(unit_type or "")
        if dimension is None:
            return UnitInteger(
                unit_id=unit_id,
                kind="si",
                unit_type=unit_type,
                dimension=None,
                scale=None,
                prefix=prefix,
                si_name=name,
                detail={
                    "reason": "uninterpreted_unit",
                    "message": f"SI unit type {unit_type} is outside the supported dimensions",
                },
            )
        expected_name = {
            "LENGTH": "METRE",
            "AREA": "SQUARE_METRE",
            "VOLUME": "CUBIC_METRE",
            "MASS": "GRAM",
        }[dimension]
        if name != expected_name:
            return UnitInteger(
                unit_id=unit_id,
                kind="si",
                unit_type=unit_type,
                dimension=dimension,
                scale=None,
                prefix=prefix,
                si_name=name,
                detail={
                    "reason": "dimension_mismatch",
                    "message": "SI unit name does not realise its declared unit type",
                    "expected_name": expected_name,
                },
            )
        # Area/volume units derived from length raise the prefix to the second
        # or third power; mass and length are linear.  ``exponent`` is also
        # reported in unit evidence so a reviewer can see the power applied.
        exponent = _DIMENSION_EXPONENT[dimension][0] or 1
        base = Fraction(1, 1000) if name == "GRAM" else Fraction(1)
        p = SI_PREFIX_EXPONENT.get(prefix or "", 0)
        scale = base * (Fraction(10) ** (p * exponent))
        return UnitInteger(
            unit_id=unit_id,
            kind="si",
            unit_type=unit_type,
            dimension=dimension,
            scale=scale,
            label=f"{prefix + '.' if prefix else ''}{name}",
            prefix=prefix,
            si_name=name,
            exponent=exponent,
        )

    def _resolve_conversion(
        self, unit_id: int, args: tuple[Any, ...], type_name: str
    ) -> UnitInteger:
        unit_type = _enum(args[1]) if len(args) > 1 else None
        name = args[2].value if len(args) > 2 and isinstance(args[2], StringValue) else ""
        factor_ref = args[3] if len(args) > 3 else None
        offset = Fraction(0)
        has_offset_attr = type_name == "IFCCONVERSIONBASEDUNITWITHOFFSET"
        if has_offset_attr and len(args) > 4:
            try:
                offset = _fraction_from_number(args[4])
            except UnitError:
                offset = Fraction(0)

        def fail(reason: str, message: str, **detail: Any) -> UnitInteger:
            payload = {"reason": reason, "message": message}
            payload.update(detail)
            cycle = tuple(detail.get("cycle") or ())
            if reason == "unit_cycle" and not cycle:
                cycle = (unit_id,)
            return UnitInteger(
                unit_id=unit_id,
                kind="cycle" if cycle else "conversion",
                unit_type=unit_type,
                dimension=None,
                scale=None,
                offset=offset,
                label=name,
                cycle=cycle,
                detail=payload,
            )

        if not isinstance(factor_ref, Ref):
            return fail("unresolved_unit", "conversion factor is not a reference")
        mwu = self.step.entities.get(factor_ref.step_id)
        if mwu is None:
            return fail(
                "unresolved_unit",
                f"conversion factor #{factor_ref.step_id} is missing",
                failed_unit=factor_ref.step_id,
            )
        if mwu.type_name != "IFCMEASUREWITHUNIT":
            return fail(
                "unresolved_unit",
                f"#{factor_ref.step_id} is {mwu.type_name}, not IfcMeasureWithUnit",
                failed_unit=factor_ref.step_id,
            )
        mwu_args = self.step.args(factor_ref.step_id)
        value_arg = mwu_args[0] if mwu_args else None
        unit_arg = mwu_args[1] if len(mwu_args) > 1 else None
        number_arg = value_arg
        if isinstance(value_arg, TypedValue):
            if not value_arg.args:
                return fail("unresolved_unit", "conversion factor typed value is empty")
            number_arg = value_arg.args[0]
        try:
            magnitude = _fraction_from_number(number_arg)
        except UnitError:
            return fail("unresolved_unit", "conversion factor value is not a number")
        if not isinstance(unit_arg, Ref):
            return fail("unresolved_unit", "conversion factor unit is not a reference")
        inner = self.resolve(unit_arg.step_id)
        if inner.scale is None or inner.dimension is None:
            reason = inner.detail.get("reason", "unresolved_unit")
            if inner.cycle:
                reason = "unit_cycle"
            return fail(
                reason,
                f"conversion factor unit #{unit_arg.step_id} is unusable: "
                f"{inner.detail.get('message', '')}".strip(),
                failed_unit=unit_arg.step_id,
                cycle=list(inner.cycle),
            )
        declared_dimension = _DIMENSION_OF_UNIT_TYPE.get(unit_type or "")
        if declared_dimension and declared_dimension != inner.dimension:
            return UnitInteger(
                unit_id=unit_id,
                kind="conversion",
                unit_type=unit_type,
                dimension=inner.dimension,
                scale=None,
                offset=offset,
                label=name,
                detail={
                    "reason": "dimension_mismatch",
                    "message": "conversion unit type disagrees with its factor unit",
                    "declared_dimension": declared_dimension,
                    "factor_dimension": inner.dimension,
                },
            )
        scale = magnitude * inner.scale
        offset_note = "conversion offset is not applied to supported dimensions"
        if offset != 0 and declared_dimension:
            return UnitInteger(
                unit_id=unit_id,
                kind="conversion",
                unit_type=unit_type,
                dimension=inner.dimension,
                scale=None,
                offset=offset,
                label=name,
                detail={
                    "reason": "unresolved_unit",
                    "message": offset_note,
                    "factor": fraction_json(magnitude),
                    "factor_unit": unit_arg.step_id,
                },
            )
        return UnitInteger(
            unit_id=unit_id,
            kind="conversion",
            unit_type=unit_type,
            dimension=inner.dimension,
            scale=scale,
            offset=offset,
            label=name,
            detail={
                "factor": fraction_json(magnitude),
                "factor_unit": unit_arg.step_id,
                "offset_present": offset != 0,
            },
        )

    def _resolve_derived(self, unit_id: int, args: tuple[Any, ...]) -> UnitInteger:
        elements_arg = args[0] if args else None
        unit_type = _enum(args[1]) if len(args) > 1 else None

        def fail(reason: str, message: str, **detail: Any) -> UnitInteger:
            payload = {"reason": reason, "message": message}
            payload.update(detail)
            return UnitInteger(
                unit_id=unit_id,
                kind="derived",
                unit_type=unit_type,
                dimension=None,
                scale=None,
                detail=payload,
            )

        if not isinstance(elements_arg, ListValue) or not elements_arg.items:
            return fail("unresolved_unit", "derived unit has no element list")
        scale = Fraction(1)
        elements: list[dict[str, Any]] = []
        dims = [0, 0, 0]
        for element in elements_arg.items:
            if not isinstance(element, Ref):
                return fail("unresolved_unit", "derived unit element is not a reference")
            ent = self.step.entities.get(element.step_id)
            if ent is None or ent.type_name != "IFCDERIVEDUNITELEMENT":
                return fail(
                    "unresolved_unit",
                    f"#{element.step_id} is not an IfcDerivedUnitElement",
                    failed_unit=element.step_id,
                )
            el_args = self.step.args(element.step_id)
            unit_ref = el_args[0] if el_args else None
            exponent_arg = el_args[1] if len(el_args) > 1 else None
            if not isinstance(unit_ref, Ref) or not isinstance(exponent_arg, NumberValue):
                return fail(
                    "unresolved_unit",
                    f"malformed derived unit element #{element.step_id}",
                    failed_unit=element.step_id,
                )
            exponent = int(exponent_arg.value)
            inner = self.resolve(unit_ref.step_id)
            if inner.scale is None or inner.dimension is None:
                return fail(
                    inner.detail.get("reason", "unresolved_unit"),
                    f"derived unit element #{unit_ref.step_id} is unusable",
                    failed_unit=unit_ref.step_id,
                    cycle=list(inner.cycle),
                )
            scale *= inner.scale**exponent
            edge = _DIMENSION_EXPONENT.get(inner.dimension, (0, 0, 0))
            dims = [d + e * exponent for d, e in zip(dims, edge)]
            elements.append(
                {
                    "unit_step_id": unit_ref.step_id,
                    "exponent": exponent,
                    "unit": inner.as_json(),
                }
            )
        dimension = _dimension_from_exponents(tuple(dims))
        return UnitInteger(
            unit_id=unit_id,
            kind="derived",
            unit_type=unit_type,
            dimension=dimension,
            scale=scale,
            label=unit_type or "",
            elements=tuple(elements),
            detail={
                "exponents": {"length": dims[0], "mass": dims[1], "time": dims[2]},
                "unit_type": unit_type,
                "reason": None if dimension else "dimension_mismatch",
            },
        )


def _dimension_from_exponents(exponents: tuple[int, int, int]) -> str | None:
    length, mass, time = exponents
    if time != 0:
        return None
    if length == 1 and mass == 0:
        return "LENGTH"
    if length == 2 and mass == 0:
        return "AREA"
    if length == 3 and mass == 0:
        return "VOLUME"
    if length == 0 and mass == 1:
        return "MASS"
    return None


def measure_dimension(type_name: str | None) -> tuple[str | None, str]:
    """Classify a measure type name.

    Returns ``(dimension, role)`` with role ``supported``, ``uninterpreted`` or
    ``unsupported``.  Unknown names are ``unsupported`` and must be surfaced,
    never guessed.
    """

    if not type_name:
        return None, "unsupported"
    key = type_name.upper()
    if key in _MEASURE_DIMENSION:
        return _MEASURE_DIMENSION[key], "supported"
    if key in _KNOWN_UNINTERPRETED:
        return None, "uninterpreted"
    return None, "unsupported"


def exact_si_decimal(value: Fraction, max_places: int = 60) -> tuple[str | None, bool]:
    """Render an exact fraction as a decimal if its expansion terminates."""

    denominator = value.denominator
    d = denominator
    twos = 0
    fives = 0
    while d % 2 == 0:
        d //= 2
        twos += 1
    while d % 5 == 0:
        d //= 5
        fives += 1
    if d != 1:
        return None, False
    places = max(twos, fives)
    if places > max_places:
        return None, False
    with localcontext() as ctx:
        ctx.prec = max(80, len(str(abs(value.numerator))) + places + 10)
        dec = Decimal(value.numerator) / Decimal(value.denominator)
    return canonical_decimal(dec), True


@dataclass
class MeasuredValue:
    """Outcome of interpreting one IFC measure.

    ``status`` is ``ok``, ``missing_unit``, ``dimension_mismatch``,
    ``unit_cycle``, ``unresolved_unit``, ``uninterpreted`` or ``explicit_null``.
    A non-``ok`` status is evidence, never a zero.
    """

    status: str
    declared_type: str | None
    dimension: str | None
    original_raw: str | None = None
    original_numeric: Fraction | None = None
    si_value: Fraction | None = None
    si_decimal: str | None = None
    si_decimal_exact: bool = False
    unit_source: str | None = None
    unit: UnitInteger | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "declared_type": self.declared_type,
            "dimension": self.dimension,
            "original_raw": self.original_raw,
            "original_numeric": (
                None if self.original_numeric is None else fraction_json(self.original_numeric)
            ),
            "si_value": None if self.si_value is None else fraction_json(self.si_value),
            "si_decimal": self.si_decimal,
            "si_decimal_exact": self.si_decimal_exact,
            "unit_source": self.unit_source,
            "reason": self.reason,
            "detail": dict(self.detail),
            "unit": None if self.unit is None else self.unit.as_json(),
        }


class ValueInterpreter:
    """Interpret typed IFC values using a unit resolver and a project unit map.

    ``project_units`` maps a dimension (LENGTH/AREA/VOLUME/MASS) to the
    ``IfcUnitAssignment`` unit STEP id chosen for that dimension, together with
    the place it was found.  ``source`` records whether the unit came from the
    property, the project assignment, or nowhere.
    """

    def __init__(
        self,
        resolver: UnitResolver,
        project_units: dict[str, dict[str, Any]] | None = None,
    ):
        self.resolver = resolver
        self.project_units = project_units or {}

    def interpret(
        self,
        declared_type: str | None,
        numeric: Fraction | None,
        unit_ref: int | None,
        *,
        original_raw: str | None = None,
    ) -> MeasuredValue:
        dimension, role = measure_dimension(declared_type)
        if numeric is None:
            return MeasuredValue(
                status="explicit_null",
                declared_type=declared_type,
                dimension=dimension,
                original_raw=original_raw,
                reason="no value supplied",
            )
        base = MeasuredValue(
            status="ok",
            declared_type=declared_type,
            dimension=dimension,
            original_raw=original_raw,
            original_numeric=numeric,
        )
        if role == "unsupported":
            base.status = "uninterpreted"
            base.reason = f"measure type {declared_type} is outside supported dimensions"
            return base
        if role == "uninterpreted":
            base.status = "uninterpreted"
            base.reason = (
                f"measure type {declared_type} is valid but not a length/area/volume/mass"
            )
            return base
        # Supported dimension: resolve unit.
        unit: UnitInteger | None = None
        source: str | None = None
        if unit_ref is not None:
            unit = self.resolver.resolve(unit_ref)
            source = "property"
            if unit.detail.get("reason") == "unresolved_unit" and unit.kind == "missing":
                base.status = "missing_unit"
                base.reason = unit.detail.get("message")
                base.unit = unit
                base.unit_source = "property"
                return base
        else:
            candidate = self.project_units.get(dimension or "")
            if candidate is None:
                base.status = "missing_unit"
                base.reason = (
                    f"property has no unit and the project assigns no {dimension} unit"
                )
                base.unit_source = None
                base.detail = {
                    "project_units_available": sorted(self.project_units),
                }
                return base
            unit = self.resolver.resolve(int(candidate["unit_step_id"]))
            source = str(candidate.get("source", "project"))
            if unit.scale is None:
                base.status = _unit_failure_status(unit)
                base.reason = unit.detail.get("message")
                base.unit = unit
                base.unit_source = source
                return base
        base.unit = unit
        base.unit_source = source
        assert unit is not None
        if unit.scale is None:
            base.status = _unit_failure_status(unit)
            base.reason = unit.detail.get("message")
            return base
        if unit.dimension != dimension:
            base.status = "dimension_mismatch"
            base.reason = (
                f"value declares {dimension} but its unit {unit.label or unit.unit_id} "
                f"is {unit.dimension}"
            )
            base.detail = {"unit_dimension": unit.dimension}
            return base
        if unit.offset != 0:
            base.status = "unresolved_unit"
            base.reason = "offset-bearing units are not applied to this dimension"
            base.detail = {"offset": str(unit.offset)}
            return base
        si = numeric * unit.scale
        base.si_value = si
        base.si_decimal, base.si_decimal_exact = exact_si_decimal(si)
        return base


def _unit_failure_status(unit: UnitInteger) -> str:
    reason = unit.detail.get("reason")
    if unit.cycle:
        return "unit_cycle"
    if reason in {"unit_cycle", "missing_unit", "dimension_mismatch"}:
        return str(reason)
    if unit.kind == "missing":
        return "missing_unit"
    if reason == "uninterpreted_unit":
        return "uninterpreted"
    return "unresolved_unit"
