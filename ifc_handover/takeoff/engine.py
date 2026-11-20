"""Take-off line construction and exact rational summarisation.

For every rule and every included object, one detail line is produced per
quantity identity the rule requires.  Each line resolves the object's
effective quantity from the repository's already-resolved quantity rows:

* an instance declaration of the identity overrides the type declaration,
  including an explicit zero and including an instance conflict -- there is
  no fallback to the type once an instance declaration exists;
* the type declaration is inherited only when the instance has no
  declaration of that identity;
* equal declarations at one level were merged during ingestion and every
  source coordinate is retained; differing declarations are a conflict and
  no declaration is picked.

The six line states are kept distinct: ``valid``, ``missing``, ``conflict``,
``unsupported``, ``unit_unavailable`` and ``scope_uncertain``.  Invalid or
missing lines never enter a total as zero.  Totals are exact
:class:`fractions.Fraction` sums; binary floats are never used.

All iteration is over streaming SQLite cursors with bounded write buffers;
no full list of objects, declarations, paths or lines is held in Python.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Any

from ..keys import canonical_json
from .dataset import (
    LINE_CONFLICT,
    LINE_MISSING,
    LINE_SCOPE_UNCERTAIN,
    LINE_UNSUPPORTED,
    LINE_UNIT_UNAVAILABLE,
    LINE_VALID,
    STATUS_CONFLICT,
    STATUS_DIMENSION_MISMATCH,
    STATUS_EXPLICIT_NULL,
    STATUS_MISSING,
    STATUS_MISSING_UNIT,
    STATUS_NON_FINITE,
    STATUS_OK,
    STATUS_SCOPE_UNCERTAIN,
    STATUS_UNRESOLVED_UNIT,
    STATUS_UNIT_CYCLE,
    STATUS_UNSUPPORTED,
)
from .inputs import QuantityIdentity, TakeoffRule

_FLUSH = 200

# value_status values that mean "the unit cannot be applied to this line".
UNIT_FAILURE_STATUSES = frozenset(
    {
        STATUS_MISSING_UNIT,
        STATUS_DIMENSION_MISMATCH,
        STATUS_UNIT_CYCLE,
        STATUS_UNRESOLVED_UNIT,
    }
)


class _BufferedInsert:
    def __init__(self, conn: sqlite3.Connection, sql: str):
        self.conn = conn
        self.sql = sql
        self.buffer: list[tuple[Any, ...]] = []

    def add(self, row: tuple[Any, ...]) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= _FLUSH:
            self.flush()

    def flush(self) -> None:
        if self.buffer:
            self.conn.executemany(self.sql, self.buffer)
            self.buffer.clear()


class TakeoffEngine:
    """Build object detail lines and grouped exact summaries."""

    def __init__(self, repo_conn: sqlite3.Connection, work_conn: sqlite3.Connection):
        self.repo = repo_conn
        self.work = work_conn
        self.details = _BufferedInsert(
            work_conn,
            "INSERT INTO object_details(rule_id, root_source_id, root_global_id, object_key, "
            "source_id, global_id, step_id, entity_type, name, type_key, qset_name, q_name, "
            "q_kind, identity, line_state, value_status, scope_used, dimension, "
            "canonical_unit, canonical_num, canonical_den, canonical_decimal, "
            "canonical_decimal_exact, original_raw, unit_source, reason, declarations_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        self.evidence = _BufferedInsert(
            work_conn,
            "INSERT OR IGNORE INTO evidence(owner_kind, owner_id, source_id, sha256, "
            "step_id, entity_type, role, line_start, line_end, raw_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
        )

    def run(self, rules: tuple[TakeoffRule, ...]) -> None:
        for rule in rules:
            self._build_rule(rule)
        self.details.flush()
        self.evidence.flush()
        self._build_summaries()
        self.work.commit()

    # -- per rule --------------------------------------------------------
    def _build_rule(self, rule: TakeoffRule) -> None:
        scope = self.work.execute(
            "SELECT scope_status FROM root_scopes WHERE rule_id=?", (rule.rule_id,)
        ).fetchone()
        scope_uncertain = scope is not None and scope["scope_status"] == "unverified"
        members = self.work.execute(
            "SELECT * FROM scope_membership WHERE rule_id=? AND membership='valid' "
            "ORDER BY source_id, step_id",
            (rule.rule_id,),
        )
        for member in members:
            object_row = self.repo.execute(
                "SELECT * FROM roots WHERE key=?", (member["object_key"],)
            ).fetchone()
            if object_row is None:
                continue
            # Only the identities bound to this object's entity type are
            # evaluated; cross-type identity strings never create extra lines.
            identities = rule.quantities_by_type.get(object_row["entity_type"], ())
            if not identities:
                continue
            # Stream the object's resolved quantities once, indexed for the
            # rule's identities; the object total is small by construction.
            resolved = {
                row["identity"]: row
                for row in self.repo.execute(
                    "SELECT * FROM resolved_quantities WHERE object_key=?",
                    (member["object_key"],)
                )
            }
            for identity in identities:
                self._build_line(
                    rule, object_row, identity, resolved.get(identity.identity),
                    scope_uncertain,
                )

    def _build_line(
        self,
        rule: TakeoffRule,
        object_row: sqlite3.Row,
        identity: QuantityIdentity,
        row: sqlite3.Row | None,
        scope_uncertain: bool,
    ) -> None:
        declarations = []
        if row is None:
            # A same-name, same-set declaration with a *different* declared
            # type is not the requested identity: if one exists, the object
            # does carry a quantity under this name/set but of a kind the rule
            # cannot compute, so the line is unsupported rather than missing.
            # This also catches retained unsupported subtypes (e.g. an
            # IfcPhysicalComplexQuantity carrying the requested name).
            other = self._same_name_different_kind(object_row["key"], identity)
            if other is not None:
                self._insert_line(
                    rule, object_row, identity,
                    line_state=LINE_UNSUPPORTED,
                    value_status=STATUS_UNSUPPORTED,
                    scope_used=other["scope_used"],
                    dimension=identity.dimension,
                    canonical_unit=identity.canonical_unit,
                    num=None, den=None, decimal=None, decimal_exact=False,
                    original_raw=other["original_raw"],
                    unit_source=other["unit_source"],
                    reason=other["reason"]
                    or "a quantity with this name exists but its declared type "
                    "is not the rule's declared type",
                    declarations=_load_json(other["json"]) or {},
                )
                return
            line_state = LINE_SCOPE_UNCERTAIN if scope_uncertain else LINE_MISSING
            value_status = STATUS_SCOPE_UNCERTAIN if scope_uncertain else STATUS_MISSING
            reason = (
                "the rule scope has unresolved edges; the quantity cannot be "
                "confirmed absent"
                if scope_uncertain
                else "no instance or type declaration of this quantity identity"
            )
            self._insert_line(
                rule, object_row, identity,
                line_state=line_state, value_status=value_status,
                scope_used=None, dimension=identity.dimension,
                canonical_unit=identity.canonical_unit,
                num=None, den=None, decimal=None, decimal_exact=False,
                original_raw=None, unit_source=None, reason=reason,
                declarations=[],
            )
            return
        detail = _load_json(row["json"]) or {}
        declarations = detail
        # Record the minimum STEP evidence for every declaration.
        owner = f"object_detail:{object_row['key']}:{identity.identity}"
        for source in detail.get("sources", []):
            self._add_entity_evidence(
                "object_detail", owner, source["source_id"], source["step_id"]
            )
        status = row["value_status"]
        if row["is_conflict"] or status == STATUS_CONFLICT:
            line_state = LINE_CONFLICT
        elif status == STATUS_OK:
            line_state = LINE_VALID
        elif status == STATUS_EXPLICIT_NULL:
            # An explicit null is a declaration the source deliberately left
            # without a value.  It is distinct from a missing identity but is
            # not a take-off quantity, so the line is classified missing and
            # never contributes a zero.
            line_state = LINE_MISSING
        elif status == STATUS_UNSUPPORTED or status == STATUS_NON_FINITE:
            line_state = LINE_UNSUPPORTED
        elif status in UNIT_FAILURE_STATUSES:
            line_state = LINE_UNIT_UNAVAILABLE
        else:
            line_state = LINE_UNSUPPORTED
        num = row["canonical_num"]
        den = row["canonical_den"]
        self._insert_line(
            rule, object_row, identity,
            line_state=line_state, value_status=status,
            scope_used=row["scope_used"], dimension=row["dimension"] or identity.dimension,
            canonical_unit=row["canonical_unit"] or identity.canonical_unit,
            num=num, den=den,
            decimal=row["canonical_decimal"],
            decimal_exact=bool(row["canonical_decimal_exact"]),
            original_raw=row["original_raw"],
            unit_source=row["unit_source"],
            reason=row["reason"],
            declarations=declarations,
        )

    def _same_name_different_kind(
        self, object_key: str, identity: QuantityIdentity
    ) -> sqlite3.Row | None:
        """Find a same-set/name quantity whose declared kind differs.

        The quantity identity includes the declared kind, so the row is not a
        direct match; it is evidence that a quantity under the requested name
        exists but cannot be computed for this identity.
        """

        return self.repo.execute(
            "SELECT * FROM resolved_quantities WHERE object_key=? "
            "AND qset_name=? AND q_name=? AND q_kind != ? "
            "ORDER BY q_kind LIMIT 1",
            (
                object_key,
                identity.quantity_set,
                identity.name,
                identity.declared_type,
            ),
        ).fetchone()

    def _insert_line(
        self,
        rule: TakeoffRule,
        object_row: sqlite3.Row,
        identity: QuantityIdentity,
        *,
        line_state: str,
        value_status: str,
        scope_used: str | None,
        dimension: str | None,
        canonical_unit: str | None,
        num: str | None,
        den: str | None,
        decimal: str | None,
        decimal_exact: bool,
        original_raw: str | None,
        unit_source: str | None,
        reason: str | None,
        declarations: list[Any],
    ) -> None:
        self.details.add(
            (
                rule.rule_id,
                rule.root_source_id,
                rule.root_global_id,
                object_row["key"],
                object_row["source_id"],
                object_row["global_id"],
                object_row["step_id"],
                object_row["entity_type"],
                object_row["name"],
                object_row["type_key"],
                identity.quantity_set,
                identity.name,
                identity.declared_type,
                identity.identity,
                line_state,
                value_status,
                scope_used,
                dimension,
                canonical_unit,
                num,
                den,
                decimal,
                int(decimal_exact),
                original_raw,
                unit_source,
                reason,
                canonical_json(
                    {
                        "identity": identity.as_json(),
                        "declarations": declarations,
                    }
                ),
            )
        )

    def _add_entity_evidence(
        self, owner_kind: str, owner_id: str, source_id: str, step_id: int
    ) -> None:
        for row in self.repo.execute(
            "SELECT * FROM source_entities WHERE source_id=? AND step_id=?",
            (source_id, step_id),
        ):
            self.evidence.add(
                (
                    owner_kind,
                    owner_id,
                    row["source_id"],
                    row["sha256"],
                    row["step_id"],
                    row["entity_type"],
                    row["role"],
                    row["line_start"],
                    row["line_end"],
                    row["raw_text"],
                )
            )

    # -- summaries -------------------------------------------------------
    def _build_summaries(self) -> None:
        """Group detail lines with exact Fraction totals.

        One summary row is emitted for every (rule, entity type, identity)
        combination the rules declare, even when no object is in scope, so an
        empty scope and a scope without matching declarations stay
        distinguishable.  Only one group's accumulator is held in Python at a
        time, so memory is bounded by the number of distinct groups, never by
        the object count.
        """

        sql = (
            "SELECT rule_id, root_source_id, root_global_id, entity_type, qset_name, "
            "q_name, q_kind, identity, canonical_unit, line_state, canonical_num, "
            "canonical_den, canonical_decimal_exact "
            "FROM object_details ORDER BY rule_id, entity_type, identity"
        )
        seen_groups: set[tuple[Any, ...]] = set()
        current: tuple[Any, ...] | None = None
        acc: dict[str, Any] | None = None
        rows: list[tuple[Any, ...]] = []

        def flush() -> None:
            nonlocal rows, acc
            if acc is not None:
                rows.append(self._summary_row(acc))
                if len(rows) >= _FLUSH:
                    self._flush_summaries(rows)
                    rows = []
                acc = None

        for row in self.work.execute(sql):
            token = (row["rule_id"], row["entity_type"], row["identity"])
            if current is not None and token != current:
                flush()
            if acc is None:
                current = token
                acc = self._new_accumulator(row)
                seen_groups.add(token)
            self._accumulate(acc, row)
        flush()
        # Zero-object combinations: rules whose scope has no included object
        # of the bound type still get an explicit zero summary.
        for rule_row in self.work.execute("SELECT * FROM rules ORDER BY ordinal"):
            rule_id = rule_row["rule_id"]
            for binding in self.work.execute(
                "SELECT * FROM rule_quantity_identities WHERE rule_id=? "
                "ORDER BY entity_type, identity",
                (rule_id,),
            ):
                token = (rule_id, binding["entity_type"], binding["identity"])
                if token in seen_groups:
                    continue
                rows.append(
                    self._empty_summary_row(rule_id, rule_row, binding)
                )
                seen_groups.add(token)
                if len(rows) >= _FLUSH:
                    self._flush_summaries(rows)
                    rows = []
        if rows:
            self._flush_summaries(rows)

    def _empty_summary_row(
        self, rule_id: str, rule_row: sqlite3.Row, binding: sqlite3.Row
    ) -> tuple[Any, ...]:
        return (
            rule_id,
            rule_row["root_source_id"],
            rule_row["root_global_id"],
            binding["entity_type"],
            binding["qset_name"],
            binding["q_name"],
            binding["q_kind"],
            binding["identity"],
            binding["canonical_unit"],
            0,  # included objects
            0, 0, 0, 0, 0, 0,
            "0", "1", "0", 1,
        )

    def _new_accumulator(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "root_source_id": row["root_source_id"],
            "root_global_id": row["root_global_id"],
            "entity_type": row["entity_type"],
            "qset_name": row["qset_name"],
            "q_name": row["q_name"],
            "q_kind": row["q_kind"],
            "identity": row["identity"],
            "canonical_unit": row["canonical_unit"],
            "included_objects": 0,
            "valid": 0,
            "missing": 0,
            "conflict": 0,
            "unsupported": 0,
            "unit_unavailable": 0,
            "scope_uncertain": 0,
            "total": Fraction(0),
            "decimal_exact": True,
        }

    def _accumulate(self, acc: dict[str, Any], row: sqlite3.Row) -> None:
        acc["included_objects"] += 1
        state = row["line_state"]
        acc[_STATE_FIELD[state]] += 1
        if state == LINE_VALID and row["canonical_num"] is not None:
            acc["total"] += Fraction(
                int(row["canonical_num"]), int(row["canonical_den"])
            )
            if not row["canonical_decimal_exact"]:
                acc["decimal_exact"] = False

    def _summary_row(self, acc: dict[str, Any]) -> tuple[Any, ...]:
        from .units_export import exact_decimal

        total: Fraction = acc["total"]
        decimal, exact = exact_decimal(total)
        return (
            acc["rule_id"],
            acc["root_source_id"],
            acc["root_global_id"],
            acc["entity_type"],
            acc["qset_name"],
            acc["q_name"],
            acc["q_kind"],
            acc["identity"],
            acc["canonical_unit"],
            acc["included_objects"],
            acc["valid"],
            acc["missing"],
            acc["conflict"],
            acc["unsupported"],
            acc["unit_unavailable"],
            acc["scope_uncertain"],
            str(total.numerator),
            str(total.denominator),
            decimal,
            int(exact and acc["decimal_exact"]),
        )

    def _flush_summaries(self, rows: list[tuple[Any, ...]]) -> None:
        self.work.executemany(
            "INSERT INTO quantity_summaries(rule_id, root_source_id, root_global_id, "
            "entity_type, qset_name, q_name, q_kind, identity, canonical_unit, "
            "included_objects, valid_count, missing_count, conflict_count, "
            "unsupported_count, unit_unavailable_count, scope_uncertain_count, "
            "total_num, total_den, total_decimal, total_decimal_exact) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


_STATE_FIELD = {
    LINE_VALID: "valid",
    LINE_MISSING: "missing",
    LINE_CONFLICT: "conflict",
    LINE_UNSUPPORTED: "unsupported",
    LINE_UNIT_UNAVAILABLE: "unit_unavailable",
    LINE_SCOPE_UNCERTAIN: "scope_uncertain",
}


def _load_json(text: str | None) -> Any:
    if text is None:
        return None
    import json

    return json.loads(text)
