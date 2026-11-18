"""Units: prefixes, prefix powers, conversion factors, offsets, cycles, dimensions."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from fractions import Fraction

from ifc_handover.step import StepFile
from ifc_handover.units import (
    DimensionMismatch,
    MissingUnit,
    UnitCycle,
    UnitResolver,
    ValueInterpreter,
    exact_si_decimal,
    measure_dimension,
)


UNITS = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('u.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCSIUNIT(*,.AREAUNIT.,.MILLI.,.SQUARE_METRE.);
#3=IFCSIUNIT(*,.VOLUMEUNIT.,.MILLI.,.CUBIC_METRE.);
#4=IFCSIUNIT(*,.MASSUNIT.,.KILO.,.GRAM.);
#5=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#6=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(0.0254),#5);
#7=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'INCH',#6);
#8=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(25.4),#1);
#9=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'BAD_INCH',#8);
#10=IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.);
#11=IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.);
#12=IFCSIUNIT(*,.MASSUNIT.,$,.GRAM.);
#13=IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.);
#14=IFCDERIVEDUNIT((#15),.VOLUMETRICFLOWRATEUNIT.,$);
#15=IFCDERIVEDUNITELEMENT(#3,1);
#16=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_A',#18);
#17=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_B',#19);
#18=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#17);
#19=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#16);
#20=IFCSIUNIT(*,.LENGTHUNIT.,.CENTI.,.METRE.);
#21=IFCCONVERSIONBASEDUNITWITHOFFSET(*,.LENGTHUNIT.,'FAHRENHEIT_L',#22,32.);
#22=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(1.),#5);
#23=IFCSIUNIT(*,.AREAUNIT.,.KILO.,.SQUARE_METRE.);
#24=IFCCONTEXTDEPENDENTUNIT(*,.LENGTHUNIT.,'DRAWING_UNIT');
#25=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.SQUARE_METRE.);
ENDSEC;
END-ISO-10303-21;
"""


class UnitResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = StepFile(UNITS)
        self.resolver = UnitResolver(self.step)

    def test_millimetre(self) -> None:
        unit = self.resolver.resolve(1)
        self.assertEqual(unit.dimension, "LENGTH")
        self.assertEqual(unit.scale, Fraction(1, 1000))
        self.assertEqual(unit.prefix, "MILLI")

    def test_area_prefix_power(self) -> None:
        unit = self.resolver.resolve(2)
        self.assertEqual(unit.dimension, "AREA")
        self.assertEqual(unit.scale, Fraction(1, 1_000_000))
        self.assertEqual(unit.exponent, 2)

    def test_volume_prefix_power(self) -> None:
        unit = self.resolver.resolve(3)
        self.assertEqual(unit.dimension, "VOLUME")
        self.assertEqual(unit.scale, Fraction(1, 1_000_000_000))
        self.assertEqual(unit.exponent, 3)

    def test_kilogram(self) -> None:
        unit = self.resolver.resolve(4)
        self.assertEqual(unit.dimension, "MASS")
        self.assertEqual(unit.scale, Fraction(1))

    def test_gram_has_kilogram_si_base(self) -> None:
        unit = self.resolver.resolve(12)
        self.assertEqual(unit.scale, Fraction(1, 1000))

    def test_kilo_square_metre(self) -> None:
        unit = self.resolver.resolve(23)
        self.assertEqual(unit.scale, Fraction(1_000_000))

    def test_conversion_factor_exact(self) -> None:
        unit = self.resolver.resolve(7)
        self.assertEqual(unit.dimension, "LENGTH")
        self.assertEqual(unit.scale, Fraction(254, 10_000))
        self.assertTrue(unit.usable)

    def test_conversion_with_prefixed_factor_unit(self) -> None:
        # 25.4 * mm = 0.0254 m
        unit = self.resolver.resolve(9)
        self.assertEqual(unit.scale, Fraction(254, 10_000))

    def test_derived_unit(self) -> None:
        unit = self.resolver.resolve(14)
        self.assertEqual(unit.dimension, "VOLUME")
        self.assertEqual(unit.scale, Fraction(1, 1_000_000_000))

    def test_cycle_is_reported(self) -> None:
        unit = self.resolver.resolve(16)
        self.assertFalse(unit.usable)
        self.assertTrue(unit.cycle)
        self.assertIn(16, unit.cycle)
        self.assertIn(17, unit.cycle)
        self.assertEqual(unit.detail["reason"], "unit_cycle")

    def test_offset_unit_is_not_silently_factored(self) -> None:
        unit = self.resolver.resolve(21)
        self.assertFalse(unit.usable)
        self.assertEqual(unit.offset, Fraction(32))
        self.assertEqual(unit.detail["reason"], "unresolved_unit")

    def test_si_name_dimension_mismatch(self) -> None:
        unit = self.resolver.resolve(25)
        self.assertFalse(unit.usable)
        self.assertEqual(unit.detail["reason"], "dimension_mismatch")

    def test_missing_unit_reference(self) -> None:
        unit = self.resolver.resolve(999)
        self.assertFalse(unit.usable)
        self.assertEqual(unit.detail["reason"], "unresolved_unit")

    def test_plane_angle_is_uninterpreted(self) -> None:
        unit = self.resolver.resolve(13)
        self.assertFalse(unit.usable)
        self.assertEqual(unit.detail["reason"], "uninterpreted_unit")

    def test_context_dependent_unit(self) -> None:
        unit = self.resolver.resolve(24)
        self.assertFalse(unit.usable)
        self.assertEqual(unit.detail["reason"], "unresolved_unit")


class ValueInterpretationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = StepFile(UNITS)
        self.resolver = UnitResolver(self.step)
        self.interp = ValueInterpreter(
            self.resolver,
            {
                "LENGTH": {"unit_step_id": 1, "source": "project"},
                "AREA": {"unit_step_id": 10, "source": "project"},
                "VOLUME": {"unit_step_id": 11, "source": "project"},
                "MASS": {"unit_step_id": 12, "source": "project"},
            },
        )

    def test_project_unit_is_used_only_without_property_unit(self) -> None:
        with_project = self.interp.interpret(
            "IFCLENGTHMEASURE", Fraction(1000), None, original_raw="IFCLENGTHMEASURE(1000.)"
        )
        self.assertEqual(with_project.status, "ok")
        self.assertEqual(with_project.si_value, Fraction(1))
        self.assertEqual(with_project.unit_source, "project")
        explicit = self.interp.interpret("IFCLENGTHMEASURE", Fraction(2), 5)
        self.assertEqual(explicit.si_value, Fraction(2))
        self.assertEqual(explicit.unit_source, "property")

    def test_missing_unit_is_not_zero(self) -> None:
        interp = ValueInterpreter(self.resolver, {})
        result = interp.interpret("IFCLENGTHMEASURE", Fraction(5), None)
        self.assertEqual(result.status, "missing_unit")
        self.assertIsNone(result.si_value)
        self.assertIsNone(result.si_decimal)

    def test_dimension_mismatch(self) -> None:
        result = self.interp.interpret("IFCLENGTHMEASURE", Fraction(7), 10)
        self.assertEqual(result.status, "dimension_mismatch")
        self.assertIsNone(result.si_value)

    def test_unit_cycle(self) -> None:
        result = self.interp.interpret("IFCLENGTHMEASURE", Fraction(4), 16)
        self.assertEqual(result.status, "unit_cycle")
        self.assertIsNone(result.si_value)
        self.assertIn(16, result.unit.cycle)

    def test_uninterpreted_measure_keeps_declared_type(self) -> None:
        result = self.interp.interpret("IFCPLANEANGLEMEASURE", Fraction(314, 100), None)
        self.assertEqual(result.status, "uninterpreted")
        self.assertEqual(result.declared_type, "IFCPLANEANGLEMEASURE")
        self.assertIsNone(result.si_value)

    def test_unknown_measure_type_is_unsupported(self) -> None:
        result = self.interp.interpret("IFCBOGUSMEASURE", Fraction(1), None)
        self.assertEqual(result.status, "uninterpreted")

    def test_non_terminating_decimal_is_exact_fraction(self) -> None:
        text, exact = exact_si_decimal(Fraction(1, 3))
        self.assertIsNone(text)
        self.assertFalse(exact)
        text, exact = exact_si_decimal(Fraction(254, 10_000))
        self.assertEqual(text, "0.0254")
        self.assertTrue(exact)

    def test_measure_dimension_classification(self) -> None:
        self.assertEqual(measure_dimension("IfcLengthMeasure"), ("LENGTH", "supported"))
        self.assertEqual(
            measure_dimension("IfcPositiveLengthMeasure"), ("LENGTH", "supported")
        )
        self.assertEqual(measure_dimension("IfcLabel"), (None, "uninterpreted"))
        self.assertEqual(measure_dimension("IfcBogus"), (None, "unsupported"))
        self.assertEqual(measure_dimension(None), (None, "unsupported"))


if __name__ == "__main__":
    unittest.main()
