"""Command line entry points.

    python -m ifc_handover ingest --manifest manifest.json --repository repo.sqlite \
        [--store-dir sources] [--allow-hash-mismatch]
    python -m ifc_handover export --repository repo.sqlite --root <object-key> \
        [--root ...] --dest package/ [--label "..."]
    python -m ifc_handover query --repository repo.sqlite objects [--type T] ...
    python -m ifc_handover report --repository repo.sqlite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .diff import ChangeDataset, build_diff
from .export import HandoverPackage, export_subset
from .ingest import SourceIngestor, load_manifest
from .release import ReleaseDataset, build_release_verification
from .store import Repository
from .takeoff import TakeoffDataset, build_takeoff


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def cmd_ingest(args: argparse.Namespace) -> int:
    if not os.path.exists(args.repository):
        repo = Repository.create(args.repository)
    else:
        repo = Repository(args.repository)
    entries = load_manifest(args.manifest)
    failures = 0
    try:
        for entry in entries:
            result = SourceIngestor(
                repo,
                entry,
                store_dir=args.store_dir,
                strict_hash=not args.allow_hash_mismatch,
            ).run()
            status = "ok" if result.status == "ok" else f"FAILED: {result.error}"
            print(
                f"{result.source_id}: {status} "
                f"objects={result.objects} types={result.types} "
                f"properties={result.properties} failures={result.failures}",
                file=sys.stderr,
            )
            if result.status != "ok":
                failures += 1
    finally:
        stats = repo.stats()
        repo.close()
    _print_json({"ingested": len(entries), "failed": failures, "stats": stats})
    return 1 if failures else 0


def cmd_export(args: argparse.Namespace) -> int:
    repo = Repository(args.repository)
    try:
        summary = export_subset(repo, args.root, args.dest, label=args.label)
    finally:
        repo.close()
    _print_json(
        {
            "package": summary["package"],
            "roots": summary["roots"],
            "summary": summary["summary"],
            "objects": summary["selection"]["objects"],
            "excluded": summary["selection"]["excluded"],
        }
    )
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with Repository(args.repository) as repo:
        if args.what == "objects":
            rows = repo.query_objects(
                entity_type=args.type,
                type_key=args.type_key,
                ancestor=args.ancestor,
                usable=args.usable,
                include_types=args.include_types,
                property_pset=args.pset,
                property_name=args.property_name,
                property_state=args.state,
                property_status=args.status,
                limit=args.limit,
            )
            _print_json(
                [
                    {
                        "key": r["key"],
                        "source_id": r["source_id"],
                        "step_id": r["step_id"],
                        "global_id": r["global_id"],
                        "entity_type": r["entity_type"],
                        "name": r["name"],
                        "usable": r["usable"],
                        "conflict_state": r["conflict_state"],
                        "type_key": r["type_key"],
                    }
                    for r in rows
                ]
            )
        elif args.what == "object":
            row = repo.get_object(args.key)
            if row is None:
                print(f"object {args.key!r} not found", file=sys.stderr)
                return 2
            payload = {
                "object": row,
                "properties": repo.properties(args.key),
                "materials": repo.material_assignments(args.key),
                "ancestors": repo.ancestors(args.key),
                "relations": repo.relations_for(args.key),
                "external_references": repo.external_references(args.key),
            }
            _print_json(payload)
        elif args.what == "properties":
            _print_json(repo.properties(args.key))
        elif args.what == "ancestors":
            _print_json(repo.ancestors(args.key))
        elif args.what == "failures":
            _print_json(repo.failures(category=args.category, source_id=args.source))
        elif args.what == "declarations":
            _print_json(repo.all_declarations(args.source))
        elif args.what == "units":
            _print_json(repo.units_for_source(args.source))
        else:  # pragma: no cover - argparse constrains this
            raise SystemExit(f"unknown query {args.what}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    with Repository(args.repository) as repo:
        payload = {
            "stats": repo.stats(),
            "sources": repo.sources(),
            "failures": repo.failures(),
        }
    _print_json(payload)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a package can be read without the repository or source paths."""

    with HandoverPackage(args.package) as package:
        manifest = package.manifest()
        payload = {
            "package_version": manifest.get("package_version"),
            "roots": manifest.get("roots"),
            "summary": manifest.get("summary"),
            "objects": [o["name"] or o["object_key"] for o in package.objects()],
            "missing_excerpts": [
                entry["source_id"]
                for entry in manifest.get("sources", [])
                if entry.get("excerpt_path")
                and not os.path.isfile(os.path.join(args.package, entry["excerpt_path"]))
            ],
        }
    _print_json(payload)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Compare two read-only repositories into an independent change dataset."""

    manifest = build_diff(
        args.left,
        args.right,
        args.dest,
        identity_mapping_path=args.identity_map,
        label=args.label,
    )
    _print_json(
        {
            "dataset": args.dest,
            "change_count": manifest.get("change_count"),
            "category_counts": manifest.get("category_counts"),
            "left_fingerprint": manifest["left"]["fingerprint"],
            "right_fingerprint": manifest["right"]["fingerprint"],
        }
    )
    return 0


def cmd_diff_query(args: argparse.Namespace) -> int:
    with ChangeDataset(args.dataset) as dataset:
        rows = dataset.query(
            category=args.category,
            aspect=args.aspect,
            global_id=args.global_id,
            subject=args.subject,
        )
        if args.change_id:
            row = dataset.get(args.change_id)
            _print_json(row)
            return 0 if row is not None else 2
        _print_json(rows)
    return 0


def cmd_diff_report(args: argparse.Namespace) -> int:
    with ChangeDataset(args.dataset) as dataset:
        payload = {
            "manifest": dataset.manifest(),
            "summary": dataset.summary(),
            "side_sources": dataset.side_sources(),
            "identity_mappings": dataset.mappings(),
            "object_summary": dataset.object_summary(),
        }
    _print_json(payload)
    return 0


def cmd_diff_export(args: argparse.Namespace) -> int:
    with ChangeDataset(args.dataset) as dataset:
        rows = dataset.query(
            category=args.category,
            aspect=args.aspect,
            global_id=args.global_id,
            subject=args.subject,
        )
        with open(args.jsonl, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    _print_json({"jsonl": os.path.abspath(args.jsonl), "rows": len(rows)})
    return 0


def cmd_release_verify(args: argparse.Namespace) -> int:
    """Run one offline release verification into an independent dataset."""

    manifest = build_release_verification(
        args.repository,
        args.rules,
        args.ledger,
        args.checkpoints,
        args.dest,
        label=args.label,
    )
    _print_json(
        {
            "dataset": os.path.abspath(args.dest),
            "run": manifest.get("run"),
            "repository_fingerprint": manifest["repository"]["fingerprint"],
            "ledger_sha256": manifest["ledger"]["sha256"],
        }
    )
    return 0


def cmd_release_report(args: argparse.Namespace) -> int:
    with ReleaseDataset(args.dataset) as dataset:
        payload = {
            "manifest": dataset.manifest(),
            "summary": dataset.summary(),
            "zones": dataset.zones(),
            "checkpoints": dataset.checkpoints(),
            "zone_decisions": dataset.zone_decisions(),
            "sources": dataset.sources(),
        }
    _print_json(payload)
    return 0


def cmd_release_query(args: argparse.Namespace) -> int:
    with ReleaseDataset(args.dataset) as dataset:
        if args.what == "decisions":
            _print_json(dataset.zone_decisions(checkpoint=args.checkpoint, zone=args.zone))
        elif args.what == "matters":
            _print_json(
                dataset.matters(
                    checkpoint=args.checkpoint,
                    zone=args.zone,
                    device_global_id=args.device_global_id,
                    status=args.status,
                    matter_code=args.matter_code,
                )
            )
        elif args.what == "signoffs":
            _print_json(
                dataset.signoffs(
                    record_id=args.record_id,
                    checkpoint=args.checkpoint,
                    signoff_id=args.signoff_id,
                    classification=args.classification,
                    zone=args.zone,
                )
            )
        elif args.what == "anomalies":
            _print_json(dataset.anomalies(checkpoint=args.checkpoint))
        elif args.what == "scope":
            _print_json(dataset.scope_issues(zone=args.zone))
        elif args.what == "files":
            _print_json(dataset.file_checks())
        elif args.what == "evidence":
            _print_json(
                dataset.evidence(owner_kind=args.owner_kind, owner_id=args.owner_id)
            )
        elif args.what == "revisions":
            _print_json(dataset.revisions(signoff_id=args.signoff_id))
        else:  # pragma: no cover - argparse constrains this
            raise SystemExit(f"unknown release query {args.what}")
    return 0


def cmd_release_export(args: argparse.Namespace) -> int:
    with ReleaseDataset(args.dataset) as dataset:
        rows = 0
        with open(args.jsonl, "w", encoding="utf-8", newline="\n") as fh:
            for line in dataset.iter_jsonl():
                if args.checkpoint and line.get("checkpoint_id") != args.checkpoint:
                    continue
                if args.zone and line.get("zone_id") != args.zone:
                    continue
                fh.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
                rows += 1
    _print_json({"jsonl": os.path.abspath(args.jsonl), "rows": rows})
    return 0


def cmd_takeoff_build(args: argparse.Namespace) -> int:
    manifest = build_takeoff(
        args.repository, args.rules, args.dest, label=args.label
    )
    _print_json(
        {
            "dataset": os.path.abspath(args.dest),
            "run": manifest.get("run"),
            "repository_fingerprint": manifest["repository"]["fingerprint"],
            "rules_sha256": manifest["rules"]["sha256"],
        }
    )
    return 0


def cmd_takeoff_report(args: argparse.Namespace) -> int:
    with TakeoffDataset(args.dataset) as dataset:
        payload = {
            "manifest": dataset.manifest(),
            "summary": dataset.summary(),
            "rules": dataset.rules(),
            "root_scopes": dataset.root_scopes(),
            "sources": dataset.sources(),
        }
    _print_json(payload)
    return 0


def cmd_takeoff_query(args: argparse.Namespace) -> int:
    with TakeoffDataset(args.dataset) as dataset:
        if args.what == "details":
            _print_json(
                dataset.details(
                    rule_id=args.rule,
                    root_global_id=args.root_global_id,
                    entity_type=args.type,
                    identity=args.identity,
                    line_state=args.state,
                    global_id=args.global_id,
                )
            )
        elif args.what == "summaries":
            _print_json(
                dataset.summaries(
                    rule_id=args.rule,
                    entity_type=args.type,
                    identity=args.identity,
                )
            )
        elif args.what == "anomalies":
            _print_json(dataset.anomalies(rule_id=args.rule))
        elif args.what == "scopes":
            _print_json(dataset.root_scopes())
        elif args.what == "evidence":
            _print_json(
                dataset.evidence(owner_kind=args.owner_kind, owner_id=args.owner_id)
            )
        else:  # pragma: no cover - argparse constrains this
            raise SystemExit(f"unknown take-off query {args.what}")
    return 0


def cmd_takeoff_object(args: argparse.Namespace) -> int:
    with TakeoffDataset(args.dataset) as dataset:
        if args.object_key:
            rows = dataset.object_detail_by_key(args.object_key)
        else:
            rows = dataset.object_detail_by_identity(args.source_id, args.global_id)
        if not rows:
            print("no take-off detail for the requested object", file=sys.stderr)
            return 2
        _print_json(rows)
    return 0


def cmd_takeoff_export(args: argparse.Namespace) -> int:
    with TakeoffDataset(args.dataset) as dataset:
        rows = 0
        with open(args.jsonl, "w", encoding="utf-8", newline="\n") as fh:
            for line in dataset.iter_jsonl():
                if args.rule and line.get("rule_id") != args.rule:
                    continue
                if args.type and line.get("entity_type") != args.type:
                    continue
                if args.identity and line.get("identity") != args.identity:
                    continue
                if args.state and line.get("line_state") != args.state:
                    continue
                fh.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
                rows += 1
    _print_json({"jsonl": os.path.abspath(args.jsonl), "rows": rows})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc_handover",
        description="Offline IFC object repository and handover subset export.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest a manifest of IFC sources")
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--repository", required=True)
    ingest.add_argument(
        "--store-dir",
        default=None,
        help="directory for content-addressed source copies (recommended)",
    )
    ingest.add_argument("--allow-hash-mismatch", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    export = sub.add_parser("export", help="export a spatial subset package")
    export.add_argument("--repository", required=True)
    export.add_argument("--root", action="append", required=True)
    export.add_argument("--dest", required=True)
    export.add_argument("--label", default=None)
    export.set_defaults(func=cmd_export)

    query = sub.add_parser("query", help="query objects, properties and evidence")
    query.add_argument("--repository", required=True)
    query.add_argument(
        "what",
        choices=[
            "objects",
            "object",
            "properties",
            "ancestors",
            "failures",
            "declarations",
            "units",
        ],
    )
    query.add_argument("--key", default=None)
    query.add_argument("--type", default=None)
    query.add_argument("--type-key", default=None)
    query.add_argument("--ancestor", default=None)
    query.add_argument("--pset", default=None)
    query.add_argument("--property-name", default=None)
    query.add_argument("--state", default=None)
    query.add_argument("--status", default=None)
    query.add_argument("--source", default=None)
    query.add_argument("--category", default=None)
    query.add_argument("--limit", type=int, default=None)
    query.add_argument("--include-types", action="store_true")
    query.add_argument("--usable", dest="usable", action="store_true", default=None)
    query.add_argument("--unusable", dest="usable", action="store_false")
    query.set_defaults(func=cmd_query)

    report = sub.add_parser("report", help="repository statistics and failures")
    report.add_argument("--repository", required=True)
    report.set_defaults(func=cmd_report)

    verify = sub.add_parser("verify", help="verify an exported package")
    verify.add_argument("--package", required=True)
    verify.set_defaults(func=cmd_verify)

    diff = sub.add_parser(
        "diff",
        help="compare two read-only repositories and write a change dataset",
    )
    diff.add_argument("--left", required=True, help="older repository SQLite file")
    diff.add_argument("--right", required=True, help="newer repository SQLite file")
    diff.add_argument("--dest", required=True, help="output change dataset directory")
    diff.add_argument(
        "--identity-map",
        default=None,
        help="optional explicit one-to-one GlobalId correspondence JSON",
    )
    diff.add_argument("--label", default=None)
    diff.set_defaults(func=cmd_diff)

    diff_query = sub.add_parser(
        "diff-query", help="query change records from a change dataset"
    )
    diff_query.add_argument("--dataset", required=True)
    diff_query.add_argument("--category", default=None)
    diff_query.add_argument("--aspect", default=None)
    diff_query.add_argument("--global-id", dest="global_id", default=None)
    diff_query.add_argument("--subject", default=None)
    diff_query.add_argument("--change-id", default=None)
    diff_query.set_defaults(func=cmd_diff_query)

    diff_report = sub.add_parser(
        "diff-report", help="summarise a change dataset and its provenance"
    )
    diff_report.add_argument("--dataset", required=True)
    diff_report.set_defaults(func=cmd_diff_report)

    diff_export = sub.add_parser(
        "diff-export-jsonl", help="export filtered change records as JSONL"
    )
    diff_export.add_argument("--dataset", required=True)
    diff_export.add_argument("--jsonl", required=True)
    diff_export.add_argument("--category", default=None)
    diff_export.add_argument("--aspect", default=None)
    diff_export.add_argument("--global-id", dest="global_id", default=None)
    diff_export.add_argument("--subject", default=None)
    diff_export.set_defaults(func=cmd_diff_export)

    release = sub.add_parser(
        "release-verify",
        help="verify zone release conditions into an independent release dataset",
    )
    release.add_argument("--repository", required=True, help="ingested object repository SQLite")
    release.add_argument("--rules", required=True, help="acceptance rules JSON")
    release.add_argument("--ledger", required=True, help="signoff revision ledger JSONL")
    release.add_argument("--checkpoints", required=True, help="verification checkpoints JSON")
    release.add_argument("--dest", required=True, help="output release dataset directory")
    release.add_argument("--label", default=None)
    release.set_defaults(func=cmd_release_verify)

    release_report = sub.add_parser(
        "release-report", help="summarise a release dataset and its provenance"
    )
    release_report.add_argument("--dataset", required=True)
    release_report.set_defaults(func=cmd_release_report)

    release_query = sub.add_parser(
        "release-query", help="query matters, signoffs and evidence from a release dataset"
    )
    release_query.add_argument("--dataset", required=True)
    release_query.add_argument(
        "what",
        choices=[
            "decisions",
            "matters",
            "signoffs",
            "anomalies",
            "scope",
            "files",
            "evidence",
            "revisions",
        ],
    )
    release_query.add_argument("--checkpoint", default=None)
    release_query.add_argument("--zone", default=None)
    release_query.add_argument("--device-global-id", dest="device_global_id", default=None)
    release_query.add_argument("--status", default=None)
    release_query.add_argument("--matter-code", dest="matter_code", default=None)
    release_query.add_argument("--record-id", dest="record_id", default=None)
    release_query.add_argument("--signoff-id", dest="signoff_id", default=None)
    release_query.add_argument("--classification", default=None)
    release_query.add_argument("--owner-kind", dest="owner_kind", default=None)
    release_query.add_argument("--owner-id", dest="owner_id", default=None)
    release_query.set_defaults(func=cmd_release_query)

    release_export = sub.add_parser(
        "release-export-jsonl", help="export filtered release records as JSONL"
    )
    release_export.add_argument("--dataset", required=True)
    release_export.add_argument("--jsonl", required=True)
    release_export.add_argument("--checkpoint", default=None)
    release_export.add_argument("--zone", default=None)
    release_export.set_defaults(func=cmd_release_export)

    takeoff = sub.add_parser(
        "takeoff-build",
        help="build a spatial quantity take-off dataset from a read-only repository",
    )
    takeoff.add_argument("--repository", required=True, help="ingested object repository SQLite")
    takeoff.add_argument("--rules", required=True, help="UTF-8 take-off rules JSON")
    takeoff.add_argument("--dest", required=True, help="output take-off dataset directory")
    takeoff.add_argument("--label", default=None)
    takeoff.set_defaults(func=cmd_takeoff_build)

    takeoff_report = sub.add_parser(
        "takeoff-report", help="summarise a take-off dataset and its provenance"
    )
    takeoff_report.add_argument("--dataset", required=True)
    takeoff_report.set_defaults(func=cmd_takeoff_report)

    takeoff_query = sub.add_parser(
        "takeoff-query", help="query details, summaries and scope anomalies"
    )
    takeoff_query.add_argument("--dataset", required=True)
    takeoff_query.add_argument(
        "what", choices=["details", "summaries", "anomalies", "scopes", "evidence"]
    )
    takeoff_query.add_argument("--rule", default=None)
    takeoff_query.add_argument("--root-global-id", dest="root_global_id", default=None)
    takeoff_query.add_argument("--type", default=None)
    takeoff_query.add_argument("--identity", default=None)
    takeoff_query.add_argument("--state", default=None)
    takeoff_query.add_argument("--global-id", dest="global_id", default=None)
    takeoff_query.add_argument("--owner-kind", dest="owner_kind", default=None)
    takeoff_query.add_argument("--owner-id", dest="owner_id", default=None)
    takeoff_query.set_defaults(func=cmd_takeoff_query)

    takeoff_object = sub.add_parser(
        "takeoff-object", help="query one object's full quantity detail"
    )
    takeoff_object.add_argument("--dataset", required=True)
    takeoff_object.add_argument("--object-key", dest="object_key", default=None)
    takeoff_object.add_argument("--source-id", dest="source_id", default=None)
    takeoff_object.add_argument("--global-id", dest="global_id", default=None)
    takeoff_object.set_defaults(func=cmd_takeoff_object)

    takeoff_export = sub.add_parser(
        "takeoff-export-jsonl", help="export filtered take-off records as JSONL"
    )
    takeoff_export.add_argument("--dataset", required=True)
    takeoff_export.add_argument("--jsonl", required=True)
    takeoff_export.add_argument("--rule", default=None)
    takeoff_export.add_argument("--type", default=None)
    takeoff_export.add_argument("--identity", default=None)
    takeoff_export.add_argument("--state", default=None)
    takeoff_export.set_defaults(func=cmd_takeoff_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
