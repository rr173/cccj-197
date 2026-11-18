"""STEP physical-file parsing: exact numbers, references, raw text."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  (sets sys.path)

from ifc_handover.step import (
    DERIVED,
    EnumValue,
    ListValue,
    NumberValue,
    Ref,
    StepFile,
    StringValue,
    TypedValue,
    build_excerpt_ifc,
    canonical_decimal,
    parse_args,
    parse_decimal,
)
from decimal import Decimal
from fractions import Fraction


SAMPLE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
FILE_NAME('sample.ifc','2026-01-01T00:00:00',('a'),('b'),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
/* a comment with #99 and 'quotes' */
#1=IFCPROJECT('0YvctVUKr0kugbFTf53O9L',$,'P \\X2\\00E9\\X0\\',$,$,$,$,$,$);
#2=IFCPROPERTYSINGLEVALUE('Tricky',$,IFCLABEL('it''s /* not a comment */'),$);
#3=IFCRELAGGREGATES('1YvctVUKr0kugbFTf53O9L',$,$,$,#1,(#2,#999));
#4=IFCCARTESIANPOINT((1.5,-2.25,3.E-2));
#5=IFCPROPERTYSINGLEVALUE('Empty',$,IFCLABEL(''),$);
#6=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
ENDSEC;
END-ISO-10303-21;
"""


class StepParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = StepFile(SAMPLE, path="sample.ifc")

    def test_entities_and_lines(self) -> None:
        self.assertEqual(len(self.step.entities), 6)
        self.assertEqual(self.step.entity(1).line_start, 9)
        self.assertEqual(self.step.entity(1).line_end, 9)
        self.assertEqual(self.step.entity(4).type_name, "IFCCARTESIANPOINT")

    def test_comment_is_ignored(self) -> None:
        # #99 appears only inside a comment, and must not be indexed.
        self.assertNotIn(99, self.step.entities)
        self.assertEqual(self.step.entity(1).type_name, "IFCPROJECT")

    def test_exact_numbers(self) -> None:
        args = self.step.args(4)
        points = args[0]
        self.assertIsInstance(points, ListValue)
        values = [p.value for p in points.items]
        self.assertEqual(values[0], Decimal("1.5"))
        self.assertEqual(values[1], Decimal("-2.25"))
        self.assertEqual(values[2], Decimal("0.03"))
        raw = [p.raw for p in points.items]
        self.assertEqual(raw, ["1.5", "-2.25", "3.E-2"])

    def test_decimal_spelling_preserved_and_exact(self) -> None:
        a = parse_decimal("1000.")
        b = parse_decimal("1E3")
        self.assertEqual(a, b)
        self.assertEqual(Fraction(a), Fraction(1000))
        self.assertEqual(canonical_decimal(Decimal("0.0020")), "0.002")
        self.assertEqual(canonical_decimal(Decimal("-0")), "0")

    def test_strings_and_escapes(self) -> None:
        args = self.step.args(2)
        value = args[2]
        self.assertIsInstance(value, TypedValue)
        self.assertEqual(value.type_name, "IFCLABEL")
        self.assertEqual(value.args[0].value, "it's /* not a comment */")
        project_name = self.step.args(1)[2]
        self.assertEqual(project_name.value, "P é")

    def test_references_include_dangling(self) -> None:
        refs = self.step.refs(3)
        self.assertEqual(refs, [1, 2, 999])
        self.assertNotIn(999, self.step.entities)

    def test_typed_value_roundtrip(self) -> None:
        args = self.step.args(2)
        self.assertIsInstance(args[1], type(None))
        self.assertIsInstance(args[2], TypedValue)
        # '$' before the value is the null Unit attribute, not a value.

    def test_enum_and_derived(self) -> None:
        args = self.step.args(6)
        self.assertIs(args[0], DERIVED)
        self.assertIsInstance(args[1], EnumValue)
        self.assertEqual(args[1].name, "LENGTHUNIT")
        self.assertEqual(args[2].name, "MILLI")

    def test_empty_string_is_not_null(self) -> None:
        args = self.step.args(5)
        empty = args[2]
        self.assertIsInstance(empty, TypedValue)
        self.assertIsInstance(empty.args[0], StringValue)
        self.assertEqual(empty.args[0].value, "")

    def test_excerpt_is_readable_and_complete(self) -> None:
        text = build_excerpt_ifc(self.step, [2, 999, 6], "test")
        self.assertIn("#2=IFCPROPERTYSINGLEVALUE", text)
        self.assertIn("#6=IFCSIUNIT", text)
        self.assertNotIn("#999=", text)
        self.assertIn("ISO-10303-21;", text)

    def test_header_text(self) -> None:
        header = self.step.header_text()
        self.assertIn("FILE_SCHEMA(('IFC4'));", header)
        self.assertNotIn("END-ISO", header)

    def test_malformed_arguments_raise(self) -> None:
        with self.assertRaises(Exception):
            parse_args("'unterminated")

    def test_duplicate_step_ids_are_reported(self) -> None:
        text = SAMPLE.replace(
            "#6=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);",
            "#6=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);\n"
            "#6=IFCPIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);",
        )
        step = StepFile(text)
        self.assertEqual(len(step.duplicate_ids), 1)
        self.assertEqual(step.duplicate_ids[0]["step_id"], 6)
        self.assertEqual(step.duplicate_ids[0]["first_type"], "IFCSIUNIT")
        self.assertEqual(step.duplicate_ids[0]["duplicate_type"], "IFCPIUNIT")

    def test_binary_value(self) -> None:
        text = SAMPLE.replace(
            "#5=IFCPROPERTYSINGLEVALUE('Empty',$,IFCLABEL(''),$);",
            "#5=IFCPROPERTYSINGLEVALUE('Bin',$,IFCBINARY(\"0A\"),$);",
        )
        step = StepFile(text)
        value = step.args(5)[2]
        self.assertEqual(value.type_name, "IFCBINARY")


if __name__ == "__main__":
    unittest.main()
