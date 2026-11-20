"""Unit tests for exact quantity parsing and interpretation.

These tests build the tiny STEP/unit objects directly and therefore need no
IfcOpenShell; they pin the numeric guarantees (exact fractions, explicit
zero, non-finite rejection, dimension mismatch, unit cycle, count semantics)
before the full IFC drill is exercised.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from ifc_handover.quantities import (
    CANONICAL_UNIT,
    QSTATUS_DIMENSION_MISMATCH,
    QSTATUS_MISSING_UNIT,
    QSTATUS_NON_FINITE,
    QSTATUS_OK,
    QSTATUS_UNIT_CYCLE,
    QSTATUS_UNRESOLVED_UNIT,
    QSTATUS_UNSUPPORTED,
    QuantityReader,
    interpret_quantity,
    quantity_identity,
)
from ifc_handover.step import StepFile
from ifc_handover.units import UnitResolver, ValueInterpreter

STEP = """\
ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
FILE_NAME('q.ifc','2026-01-01T00:00:00',('t'),('t'),'x','x','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#3=IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.);
#4=IFCSIUNIT(*,.MASSUNIT.,.KILO.,.GRAM.);
#5=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'QUARTER',#6);
#6=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(0.25),#2);
#7=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_A',#9);
#8=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_B',#10);
#9=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#8);
#10=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#7);
#11=IFCQUANTITYLENGTH('L','$',#2,IFCLENGTHMEASURE(1.5),'$');
#12=IFCQUANTITYLENGTH('L','$','$',IFCLENGTHMEASURE(1500.),'$');
#13=IFCQUANTITYLENGTH('L','$',#3,IFCLENGTHMEASURE(2.),'$');
#14=IFCQUANTITYLENGTH('L','$',#7,IFCLENGTHMEASURE(7.),'$');
#15=IFCQUANTITYCOUNT('C','$','$',IFCCOUNTMEASURE(0.),'$');
#16=IFCQUANTITYCOUNT('C','$','$',IFCCOUNTMEASURE(4.),'$');
#17=IFCQUANTITYLENGTH('L','$',#3,IFCAREAMEASURE(2.),'$');
#18=IFCPHYSICALCOMPLEXQUANTITY('X','$',(),'layer','$','$');
#19=IFCQUANTITYAREA('A','$',#3,IFCAREAMEASURE(0.125),'$');
#20=IFCQUANTITYLENGTH('L','$',#5,IFCLENGTHMEASURE(1.),'$');
ENDSEC;
END-ISO-10303-21;
"""


class QuantityInterpretationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = StepFile(STEP)
        self.resolver = UnitResolver(self.step)
        self.reader = QuantityReader(self.step, "IFC4")
        self.project_units = {
            "LENGTH": {"unit_step_id": 1, "source": "project"},
            "AREA": {"unit_step_id": 3, "source": "project"},
            "MASS": {"unit_step_id": 4, "source": "project"},
        }
        self.interpreter = ValueInterpreter(self.resolver, self.project_units)

    def _interpret(self, step_id: int, type_name: str):
        raw = self.reader.read(step_id, type_name)
        return raw, interpret_quantity(
            raw, self.interpreter, self.resolver, self.project_units
        )

    def test_explicit_metre_length_is_exact_fraction(self) -> None:
        raw, result = self._interpret(11, "IfcQuantityLength")
        self.assertEqual(raw.numeric, Fraction(3, 2))
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(3, 2))
        self.assertEqual(result.canonical_unit, "m")
        self.assertEqual(result.unit_source, "explicit")
        self.assertEqual(result.canonical_decimal, "1.5")
        self.assertTrue(result.canonical_decimal_exact)

    def test_project_millimetre_prefix_conversion(self) -> None:
        _raw, result = self._interpret(12, "IfcQuantityLength")
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(3, 2))
        self.assertEqual(result.unit_source, "project")
        # Same canonical value as the explicit metre spelling, no float involved.
        self.assertEqual(result.canonical_decimal, "1.5")

    def test_dimension_mismatch_is_distinct_state(self) -> None:
        _raw, result = self._interpret(13, "IfcQuantityLength")
        self.assertEqual(result.status, QSTATUS_DIMENSION_MISMATCH)
        self.assertIsNone(result.canonical_value)

    def test_unit_cycle_is_detected(self) -> None:
        _raw, result = self._interpret(14, "IfcQuantityLength")
        self.assertEqual(result.status, QSTATUS_UNIT_CYCLE)
        self.assertIsNone(result.canonical_value)

    def test_explicit_zero_count_is_valid(self) -> None:
        raw, result = self._interpret(15, "IfcQuantityCount")
        self.assertEqual(raw.structural_state, "value")
        self.assertEqual(raw.numeric, Fraction(0))
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(0))
        self.assertEqual(result.canonical_unit, "count")
        self.assertEqual(CANONICAL_UNIT["COUNT"], "count")

    def test_count_needs_no_unit(self) -> None:
        _raw, result = self._interpret(16, "IfcQuantityCount")
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(4))
        self.assertEqual(result.unit_source, "none")

    def test_measure_dimension_mismatch_on_length_quantity(self) -> None:
        _raw, result = self._interpret(17, "IfcQuantityLength")
        self.assertEqual(result.status, QSTATUS_DIMENSION_MISMATCH)

    def test_unsupported_complex_quantity_is_retained(self) -> None:
        raw, result = self._interpret(18, "IfcPhysicalComplexQuantity")
        self.assertEqual(raw.structural_state, "unsupported")
        self.assertEqual(result.status, QSTATUS_UNSUPPORTED)
        self.assertIn("supported", result.reason or "")
        self.assertIsNone(result.canonical_value)

    def test_decimal_fraction_area_is_exact(self) -> None:
        _raw, result = self._interpret(19, "IfcQuantityArea")
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(1, 8))
        self.assertEqual(result.canonical_unit, "m²")
        self.assertEqual(result.canonical_decimal, "0.125")

    def test_conversion_unit_fraction(self) -> None:
        _raw, result = self._interpret(20, "IfcQuantityLength")
        self.assertEqual(result.status, QSTATUS_OK)
        self.assertEqual(result.canonical_value, Fraction(1, 4))
        self.assertEqual(result.canonical_decimal, "0.25")

    def test_missing_project_unit(self) -> None:
        raw = self.reader.read(12, "IfcQuantityLength")
        result = interpret_quantity(
            raw, ValueInterpreter(self.resolver, {}), self.resolver, {}
        )
        self.assertEqual(result.status, QSTATUS_MISSING_UNIT)

    def test_non_finite_value_is_rejected_not_guessed(self) -> None:
        from decimal import Decimal

        from ifc_handover.step import NumberValue
        from ifc_handover.quantities import _number

        value = NumberValue("NaN", Decimal("NaN"))
        numeric, _raw, finite = _number(value)
        self.assertFalse(finite)
        self.assertEqual(numeric, Fraction(0))

    def test_non_finite_status_mapping(self) -> None:
        # A hand-built raw quantity carrying the non-finite marker.
        from ifc_handover.quantities import (
            QSTATE_UNSUPPORTED,
            RawQuantity,
        )

        raw = RawQuantity(
            q_step_id=99,
            q_kind="IfcQuantityLength",
            name="L",
            description=None,
            formula=None,
            unit_step_id=None,
            value_attr="LengthValue",
            measure_type="IfcLengthMeasure",
            dimension="LENGTH",
            original_raw="NaN",
            numeric=None,
            structural_state=QSTATE_UNSUPPORTED,
            reason="non-finite numeric value NaN cannot be accumulated",
            extra={"non_finite": True},
        )
        result = interpret_quantity(
            raw, self.interpreter, self.resolver, self.project_units
        )
        self.assertEqual(result.status, QSTATUS_NON_FINITE)
        self.assertIsNone(result.canonical_value)

    def test_identity_includes_declared_type(self) -> None:
        length_id = quantity_identity("Q", "Gauge", "IfcQuantityLength")
        area_id = quantity_identity("Q", "Gauge", "IfcQuantityArea")
        self.assertNotEqual(length_id, area_id)

    def test_offset_conversion_unit_is_rejected(self) -> None:
        # IfcConversionBasedUnitWithOffset carries an offset that has no
        # meaning for a physical quantity; it must resolve as an unresolved
        # unit rather than silently applying the linear factor.
        step_text = STEP.replace(
            "ENDSEC;\nEND-ISO-10303-21;\n",
            "#21=IFCCONVERSIONBASEDUNITWITHOFFSET(*,.LENGTHUNIT.,'OFFSET_U',"
            "IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#2),32.);\n"
            "#22=IFCQUANTITYLENGTH('L','$',#21,IFCLENGTHMEASURE(2.),'$');\n"
            "ENDSEC;\nEND-ISO-10303-21;\n",
        )
        from ifc_handover.step import StepFile as _StepFile

        step = _StepFile(step_text)
        reader = QuantityReader(step, "IFC4")
        resolver = UnitResolver(step)
        raw = reader.read(22, "IfcQuantityLength")
        result = interpret_quantity(
            raw,
            ValueInterpreter(resolver, self.project_units),
            resolver,
            self.project_units,
        )
        self.assertEqual(result.status, QSTATUS_UNRESOLVED_UNIT)
        self.assertIsNone(result.canonical_value)


if __name__ == "__main__":
    unittest.main()
