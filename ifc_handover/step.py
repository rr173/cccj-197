"""Small, dependency-free STEP Physical File reader.

IfcOpenShell is used for the semantic view of an IFC file.  This module is the
source view: it keeps exact decimal tokens, raw entity text, line coordinates
and all references (including references to entities the semantic layer drops,
such as a dangling ``#999``).  It deliberately implements no IFC semantics.

Only the ISO-10303-21 physical-file subset needed for provenance is parsed:

* ``#123 = TYPE(...) ;`` instances, including multi-line instances
* ``'strings'`` with doubled quote escapes
* ``/* comments */`` outside strings
* numbers, ``.ENUM.`` values, ``$``, ``*``, references, typed values and lists

The parser is streaming in the sense that it never builds a second copy of the
whole file: it records offsets into the already-loaded text and parses entity
arguments lazily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator


class StepError(ValueError):
    """Raised when a file is not a usable STEP physical file."""


class _Derived:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "*"


DERIVED = _Derived()


@dataclass(frozen=True)
class Ref:
    step_id: int

    def __repr__(self) -> str:
        return f"#{self.step_id}"


@dataclass(frozen=True)
class EnumValue:
    name: str

    def __repr__(self) -> str:
        return f".{self.name}."


@dataclass(frozen=True)
class StringValue:
    raw: str
    value: str

    def __repr__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class NumberValue:
    raw: str
    value: Decimal

    def __repr__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class TypedValue:
    type_name: str
    args: tuple[Any, ...]
    raw: str

    def __repr__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class ListValue:
    items: tuple[Any, ...]
    raw: str

    def __repr__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class BinaryValue:
    raw: str

    def __repr__(self) -> str:
        return self.raw


class EntityArgError(StepError):
    """Malformed entity argument text."""


_ENTITY_HEAD = re.compile(r"#(\d+)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NUMBER = re.compile(
    r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[Ee][+-]?\d+)?"
)
_REF = re.compile(r"#(\d+)")
_ENUM = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\.")
_TYPE_HEAD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def parse_decimal(raw: str) -> Decimal:
    """Parse a STEP numeric token exactly.

    ``Decimal`` is constructed from the source spelling, never from a binary
    float.  ``1.``, ``1.0`` and ``1`` all yield the same exact value while the
    original spelling remains available to callers.
    """

    token = raw.strip()
    if not token:
        raise InvalidOperation("empty number")
    # STEP allows a leading '+' which Decimal accepts as well, but not a
    # trailing decimal point in every Python version's Decimal parser.
    if token.endswith("."):
        token = token[:-1]
        if not token or token in {"+", "-"}:
            raise InvalidOperation(raw)
    return Decimal(token)


def canonical_decimal(value: Decimal) -> str:
    """Return an exact, non-exponent, human-readable decimal string."""

    if value == 0:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        return "0"
    return text


@dataclass(frozen=True)
class RawEntity:
    step_id: int
    type_name: str
    args_text: str
    start: int
    end: int
    line_start: int
    line_end: int
    text: str


def _decode_step_string(body: str) -> str:
    """Decode the common STEP string escapes.

    The source text is retained separately, so this only needs to be good
    enough for names used in business output.  IfcOpenShell remains the
    semantic decoder used by ingestion for entity attributes.
    """

    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            # A doubled quote has already been reduced to a single one by the
            # caller's raw slicing only when it is an escape.
            if i + 1 < n and body[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            out.append("'")
            i += 1
            continue
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        handled = False
        for marker, width, encoding in (
            ("\\X2\\", 4, "utf-16-be"),
            ("\\X4\\", 8, "utf-32-be"),
        ):
            if not body.startswith(marker, i):
                continue
            end = body.find("\\X0\\", i + len(marker))
            if end == -1:
                handled = True  # unterminated: leave literally
                break
            hex_text = body[i + len(marker) : end]
            try:
                out.append(bytes.fromhex(hex_text).decode(encoding, errors="replace"))
            except ValueError:
                out.append(body[i : end + 4])
            i = end + 4
            handled = True
            break
        if handled:
            continue
        simple = {
            "\\\\": "\\",
            "\\N\\": "\n",
            "\\S\\": " ",
        }
        if body.startswith("\\\\", i):
            out.append("\\")
            i += 2
        elif body.startswith("\\N\\", i):
            out.append("\n")
            i += 3
        elif body.startswith("\\S\\", i) and i + 3 < n:
            out.append(chr(ord(body[i + 3]) + 128))
            i += 4
        elif body.startswith("\\X\\", i) and i + 5 <= n:
            try:
                out.append(chr(int(body[i + 3 : i + 5], 16)))
            except ValueError:
                out.append(body[i : i + 5])
            i += 5
        else:
            # Unknown escape: copy through to the next backslash (or one char).
            nxt = body.find("\\", i + 1)
            if nxt == -1:
                out.append(body[i:])
                i = n
            else:
                out.append(body[i : nxt + 1])
                i = nxt + 1
    return "".join(out)


class _ValueParser:
    def __init__(self, text: str):
        self.text = text
        self.n = len(text)

    def skip_ws(self, i: int) -> int:
        while i < self.n and self.text[i] in " \t\r\n":
            i += 1
        return i

    def parse(self, i: int = 0) -> tuple[Any, int]:
        i = self.skip_ws(i)
        if i >= self.n:
            raise EntityArgError("unexpected end of value")
        text = self.text
        ch = text[i]
        if ch == "$":
            # A null is a complete value.  The list parser validates the
            # following delimiter (',' or ')'), so no lookahead is needed here.
            return None, i + 1
        if ch == "*":
            return DERIVED, i + 1
        if ch == "#":
            m = _REF.match(text, i)
            if not m:
                raise EntityArgError(f"bad reference at {i}")
            return Ref(int(m.group(1))), m.end()
        if ch == "'":
            return self._parse_string(i)
        if ch == ".":
            m = _ENUM.match(text, i)
            if m and not (i + 1 < self.n and text[i + 1].isdigit()):
                return EnumValue(m.group(1)), m.end()
            return self._parse_number(i)
        if ch in "+-" or ch.isdigit():
            return self._parse_number(i)
        if ch == "(":
            return self._parse_list(i)
        if ch == '"':
            end = text.find('"', i + 1)
            if end == -1:
                raise EntityArgError("unterminated binary")
            return BinaryValue(text[i : end + 1]), end + 1
        m = _TYPE_HEAD.match(text, i)
        if m:
            args, end = self._parse_list(m.end() - 1)
            return TypedValue(m.group(1).upper(), tuple(args.items), text[i:end]), end
        raise EntityArgError(f"unsupported value at {i}: {text[i:i+40]!r}")

    def _parse_string(self, i: int) -> tuple[StringValue, int]:
        start = i
        i += 1
        text = self.text
        chunks: list[str] = []
        while i < self.n:
            ch = text[i]
            if ch == "'":
                if i + 1 < self.n and text[i + 1] == "'":
                    chunks.append("''")
                    i += 2
                    continue
                i += 1
                raw = text[start:i]
                body = raw[1:-1]
                return StringValue(raw, _decode_step_string(body)), i
            chunks.append(ch)
            i += 1
        raise EntityArgError("unterminated string")

    def _parse_number(self, i: int) -> tuple[NumberValue, int]:
        m = _NUMBER.match(self.text, i)
        if not m:
            raise EntityArgError(f"bad number at {i}")
        raw = m.group(0)
        try:
            value = parse_decimal(raw)
        except InvalidOperation as exc:
            raise EntityArgError(f"bad number {raw!r}") from exc
        return NumberValue(raw, value), m.end()

    def _parse_list(self, i: int) -> tuple[ListValue, int]:
        start = i
        assert self.text[i] == "("
        i += 1
        items: list[Any] = []
        i = self.skip_ws(i)
        if i < self.n and self.text[i] == ")":
            return ListValue(tuple(items), self.text[start : i + 1]), i + 1
        while True:
            value, i = self.parse(i)
            items.append(value)
            i = self.skip_ws(i)
            if i >= self.n:
                raise EntityArgError("unterminated list")
            if self.text[i] == ",":
                i += 1
                continue
            if self.text[i] == ")":
                return ListValue(tuple(items), self.text[start : i + 1]), i + 1
            raise EntityArgError(f"expected ',' or ')' at {i}: {self.text[i:i+20]!r}")


def parse_args(args_text: str) -> tuple[Any, ...]:
    """Parse the argument list of an entity (without the surrounding parens)."""

    parser = _ValueParser("(" + args_text + ")")
    value, i = parser.parse(0)
    if not isinstance(value, ListValue):
        raise EntityArgError("entity arguments must be a parenthesised list")
    i = parser.skip_ws(i)
    if i != parser.n:
        raise EntityArgError(f"trailing text in arguments at {i}")
    return value.items


class StepFile:
    """Index of a STEP physical file with lazily parsed entity arguments."""

    def __init__(self, text: str, *, path: str | None = None):
        self.text = text
        self.path = path
        self.entities: dict[int, RawEntity] = {}
        self.duplicate_ids: list[dict[str, Any]] = []
        self._args: dict[int, tuple[Any, ...]] = {}
        self._line_starts: list[int] | None = None
        self._parse_index()

    @classmethod
    def from_path(cls, path: str) -> "StepFile":
        with open(path, "rb") as fh:
            data = fh.read()
        return cls(data.decode("utf-8", errors="replace"), path=path)

    def _parse_index(self) -> None:
        text = self.text
        n = len(text)
        i = 0
        line = 1
        while i < n:
            ch = text[i]
            if ch == "\n":
                line += 1
                i += 1
                continue
            if ch == "/" and i + 1 < n and text[i + 1] == "*":
                end = text.find("*/", i + 2)
                if end == -1:
                    raise StepError("unterminated comment")
                line += text.count("\n", i, end + 2)
                i = end + 2
                continue
            if ch == "'":
                start = i
                i += 1
                while i < n:
                    if text[i] == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            i += 2
                            continue
                        i += 1
                        break
                    if text[i] == "\n":
                        line += 1
                    i += 1
                else:
                    raise StepError("unterminated string in file")
                continue
            if ch == "#":
                m = _ENTITY_HEAD.match(text, i)
                if m:
                    step_id = int(m.group(1))
                    type_name = m.group(2).upper()
                    args_open = m.end() - 1
                    close = self._find_entity_end(args_open)
                    semi = close + 1
                    while semi < n and text[semi] in " \t\r\n":
                        semi += 1
                    if semi < n and text[semi] == ";":
                        semi += 1
                    else:
                        raise StepError(f"missing ';' after entity #{step_id}")
                    end = semi
                    line_end = line + text.count("\n", i, end)
                    raw = text[i:end]
                    args_start = args_open + 1
                    args_end = close
                    if step_id in self.entities:
                        first = self.entities[step_id]
                        self.duplicate_ids.append(
                            {
                                "step_id": step_id,
                                "first_type": first.type_name,
                                "first_lines": [first.line_start, first.line_end],
                                "duplicate_type": type_name,
                                "duplicate_lines": [line, line_end],
                            }
                        )
                    self.entities[step_id] = RawEntity(
                        step_id=step_id,
                        type_name=type_name,
                        args_text=text[args_start:args_end],
                        start=i,
                        end=end,
                        line_start=line,
                        line_end=line_end,
                        text=raw,
                    )
                    line = line_end
                    i = end
                    continue
            i += 1

    def _find_entity_end(self, open_index: int) -> int:
        """Find the matching ')' for the entity argument list."""

        text = self.text
        n = len(text)
        depth = 0
        i = open_index
        while i < n:
            ch = text[i]
            if ch == "'":
                i += 1
                while i < n:
                    if text[i] == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                else:
                    raise StepError("unterminated string")
                continue
            if ch == "/" and i + 1 < n and text[i + 1] == "*":
                end = text.find("*/", i + 2)
                if end == -1:
                    raise StepError("unterminated comment")
                i = end + 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        raise StepError("unterminated entity")

    def args(self, step_id: int) -> tuple[Any, ...]:
        entity = self.entities.get(step_id)
        if entity is None:
            raise KeyError(f"STEP entity #{step_id} not found")
        cached = self._args.get(step_id)
        if cached is None:
            cached = parse_args(entity.args_text)
            self._args[step_id] = cached
        return cached

    def entity(self, step_id: int) -> RawEntity:
        try:
            return self.entities[step_id]
        except KeyError as exc:
            raise KeyError(f"STEP entity #{step_id} not found") from exc

    def refs(self, step_id: int) -> list[int]:
        """All references in an entity, depth first, in source order."""

        out: list[int] = []

        def walk(value: Any) -> None:
            if isinstance(value, Ref):
                out.append(value.step_id)
            elif isinstance(value, TypedValue):
                for item in value.args:
                    walk(item)
            elif isinstance(value, ListValue):
                for item in value.items:
                    walk(item)

        for arg in self.args(step_id):
            walk(arg)
        return out

    def line_of(self, step_id: int) -> tuple[int, int]:
        entity = self.entity(step_id)
        return entity.line_start, entity.line_end

    def excerpt(self, step_ids: Iterable[int]) -> list[dict[str, Any]]:
        result = []
        for sid in sorted(set(step_ids)):
            ent = self.entities.get(sid)
            if ent is None:
                continue
            result.append(
                {
                    "step_id": sid,
                    "entity_type": ent.type_name,
                    "line_start": ent.line_start,
                    "line_end": ent.line_end,
                    "text": ent.text,
                }
            )
        return result

    def header_text(self) -> str:
        """Return the complete HEADER section, if present."""

        upper = self.text.upper()
        start = upper.find("HEADER;")
        end = upper.find("ENDSEC;", start + 7)
        if start == -1 or end == -1:
            return ""
        return self.text[start : end + len("ENDSEC;")]

    def __contains__(self, step_id: int) -> bool:
        return step_id in self.entities

    def __iter__(self) -> Iterator[RawEntity]:
        return iter(self.entities.values())


def build_excerpt_ifc(step_file: StepFile, step_ids: Iterable[int], source_note: str) -> str:
    """Build a readable STEP fragment containing selected raw entities.

    The result is intentionally a fragment, not a loadable IFC model: some
    references point outside the subset.  It exists so a reviewer can read the
    exact source spelling that produced a business value.
    """

    lines = [
        "/* IFC handover source excerpt. Not a complete model. */",
        f"/* {source_note} */",
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('Source excerpt'),'2;1');",
        "FILE_NAME('excerpt.ifc','',('ifc_handover'),(''),'','','');",
        "FILE_SCHEMA(('IFC4'));",
        "ENDSEC;",
        "DATA;",
    ]
    for ent in step_file.excerpt(step_ids):
        lines.append(f"/* lines {ent['line_start']}-{ent['line_end']} */")
        lines.append(ent["text"])
    lines.append("ENDSEC;")
    lines.append("END-ISO-10303-21;")
    return "\n".join(lines) + "\n"
