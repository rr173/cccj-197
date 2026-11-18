"""Thin helpers over IfcOpenShell that avoid geometry.

IfcOpenShell is used only for parsing, attributes, type hierarchy and units as
data.  No geometry, placement, rendering, area or volume calculation is
performed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import ifcopenshell
import ifcopenshell.ifcopenshell_wrapper as _w


@lru_cache(maxsize=4)
def _schema(schema_name: str):
    return _w.schema_by_name(schema_name)


def schema_of(file: "ifcopenshell.file") -> str:
    return file.schema


def schema_declaration(schema_name: str, name: str):
    try:
        return _schema(schema_name).declaration_by_name(name)
    except Exception:
        return None


@lru_cache(maxsize=8192)
def type_chain(schema_name: str, name: str) -> list[str]:
    """The entity/type and its supertypes, most specific first."""

    decl = schema_declaration(schema_name, name)
    chain: list[str] = []
    while decl is not None and len(chain) < 32:
        chain.append(decl.name())
        if isinstance(decl, _w.type_declaration):
            break
        try:
            decl = decl.supertype()
        except Exception:
            decl = None
    return chain


@lru_cache(maxsize=65536)
def is_a(schema_name: str, name: str, base: str) -> bool:
    return base in type_chain(schema_name, name)


@lru_cache(maxsize=8192)
def all_attribute_names(schema_name: str, name: str) -> list[str]:
    decl = schema_declaration(schema_name, name)
    if decl is None or decl.type() != _w.entity:
        return []
    try:
        return [a.name() for a in decl.all_attributes()]
    except Exception:
        return []


@lru_cache(maxsize=8)
def value_type_names(schema_name: str) -> frozenset[str]:
    """All declared types that are members of the IfcValue select, transitively."""

    root = schema_declaration(schema_name, "IfcValue")
    seen: set[str] = set()

    def walk(decl: Any) -> None:
        if decl is None:
            return
        if isinstance(decl, _w.select_type):
            for member in decl.select_list():
                walk(member)
        elif isinstance(decl, (_w.type_declaration, _w.enumeration_type)):
            seen.add(decl.name())
        elif isinstance(decl, _w.entity):
            seen.add(decl.name())

    if root is not None:
        walk(root)
    # STEP typed-value names are upper case; keep both spellings comparable.
    return frozenset(seen) | frozenset(name.upper() for name in seen)


@lru_cache(maxsize=8)
def _entity_name_map(schema_name: str) -> dict[str, str]:
    schema = _schema(schema_name)
    out: dict[str, str] = {}
    try:
        for decl in schema.entities():
            out[decl.name().upper()] = decl.name()
    except Exception:
        pass
    return out


def canonical_entity_name(schema_name: str, name: str) -> str:
    """Canonical schema spelling for a STEP physical-file entity name."""

    if not name:
        return name
    if name[:1].isupper() and name[1:2].islower():
        return name
    return _entity_name_map(schema_name).get(name.upper(), name)


def entity_attribute_map(schema_name: str) -> dict[str, list[str]]:
    """Attribute order per entity type, cached per schema name."""

    @lru_cache(maxsize=8)
    def build(key: str) -> dict[str, tuple[str, ...]]:
        schema = _schema(key)
        out: dict[str, tuple[str, ...]] = {}
        try:
            for decl in schema.declarations():
                if isinstance(decl, _w.entity):
                    try:
                        names = tuple(a.name() for a in decl.all_attributes())
                    except Exception:
                        continue
                    out[decl.name()] = names
                    # STEP physical files spell entity names in upper case.
                    out[decl.name().upper()] = names
        except Exception:
            pass
        return out

    return {k: list(v) for k, v in build(schema_name).items()}
