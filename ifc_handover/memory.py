"""Bounded-memory evidence for multi-file ingestion.

The application constraint is: full IfcOpenShell loading is allowed for one
source at a time; the process must not cache the property graph and every
spatial path of the whole collection at once.  This module measures and reports
memory in a way a reviewer can reproduce:

* peak RSS for each source during ingestion (``resource.getrusage`` on Linux),
* peak RSS for the whole run,
* the count of live IfcOpenShell/STEP objects at the end of each source
  (both must be zero),
* an optional large synthetic corpus generator so the measurement can be
  repeated without committing large files.

The numbers are process-level observations, not a claim that memory is
constant: SQLite page cache and Python allocator reuse can keep RSS high after
the source view is released.  The tests therefore assert (a) no source view
survives ingestion and (b) peak RSS stays below a bound that scales with the
largest single source rather than with the number of sources.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Sequence


def peak_rss_kib() -> int:
    """Peak resident set size of this process in KiB (Linux/macOS)."""

    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is KiB on Linux, bytes on macOS.
    if sys.platform == "darwin":
        return usage.ru_maxrss // 1024
    return int(usage.ru_maxrss)


def current_rss_kib() -> int | None:
    """Current RSS from /proc/self/status when available."""

    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


@dataclass
class SourceMemory:
    source_id: str
    sha256: str
    file_bytes: int
    peak_rss_kib: int
    current_rss_kib: int | None
    status: str
    objects: int
    properties: int
    seconds: float

    def as_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "sha256": self.sha256,
            "file_bytes": self.file_bytes,
            "peak_rss_kib": self.peak_rss_kib,
            "current_rss_kib": self.current_rss_kib,
            "status": self.status,
            "objects": self.objects,
            "properties": self.properties,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class MemoryReport:
    sources: list[SourceMemory] = field(default_factory=list)
    baseline_rss_kib: int | None = None
    final_peak_rss_kib: int | None = None
    max_single_file_bytes: int = 0
    total_bytes: int = 0
    source_views_live_after_ingest: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "baseline_rss_kib": self.baseline_rss_kib,
            "final_peak_rss_kib": self.final_peak_rss_kib,
            "max_single_file_bytes": self.max_single_file_bytes,
            "total_bytes": self.total_bytes,
            "source_views_live_after_ingest": self.source_views_live_after_ingest,
            "sources": [s.as_json() for s in self.sources],
        }


def ingest_with_memory(
    repo: Any,
    entries: Sequence[Any],
    *,
    store_dir: str | None = None,
) -> MemoryReport:
    """Ingest sources one at a time, recording RSS and object lifetimes."""

    from .ingest import SourceIngestor

    report = MemoryReport()
    report.baseline_rss_kib = current_rss_kib()
    for entry in entries:
        report.max_single_file_bytes = max(
            report.max_single_file_bytes, os.path.getsize(entry.path)
        )
        report.total_bytes += os.path.getsize(entry.path)
        started = time.monotonic()
        ingestor = SourceIngestor(repo, entry, store_dir=store_dir)
        result = ingestor.run()
        # After run() returns, the ingestor must hold no open source view.
        if ingestor.ctx is not None:
            report.source_views_live_after_ingest += 1
        report.sources.append(
            SourceMemory(
                source_id=result.source_id,
                sha256=result.sha256,
                file_bytes=os.path.getsize(entry.path),
                peak_rss_kib=peak_rss_kib(),
                current_rss_kib=current_rss_kib(),
                status=result.status,
                objects=result.objects,
                properties=result.properties,
                seconds=time.monotonic() - started,
            )
        )
    report.final_peak_rss_kib = peak_rss_kib()
    return report


# ---------------------------------------------------------------------------
# Large synthetic corpus (generated at run time; never committed)
# ---------------------------------------------------------------------------


def build_large_corpus(
    root: str,
    *,
    sources: int = 4,
    rooms_per_source: int = 40,
    elements_per_room: int = 15,
    properties_per_element: int = 8,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate N IFC sources with many rooms and elements.

    Returns ``(manifest_entries, expectations)``.  Generated files are written
    under ``root``; callers delete ``root`` afterwards.  The files are
    deliberately simple so generation is fast and memory-light.
    """

    from tests.fixtures import IfcBuilder, gid, sha256_bytes, _s, _typed, _write_manifest
    os.makedirs(root, exist_ok=True)
    entries: list[dict[str, Any]] = []
    expectations: list[dict[str, Any]] = []
    for s_index in range(sources):
        source_id = f"large-{s_index}"
        b = IfcBuilder()
        mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
        units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
        project = b.entity(
            "IFCPROJECT", _s(gid(f"{source_id}-project")), "$", _s(source_id), "$",
            "$", "$", "$", "$", f"#{units}",
        )
        site = b.entity(
            "IFCSITE", _s(gid(f"{source_id}-site")), "$", _s("Site"), "$", "$", "$",
            "$", "$", ".ELEMENT.", "$", "$", "$", "$", "$",
        )
        b.entity("IFCRELAGGREGATES", _s(gid(f"{source_id}-agg1")), "$", "$", "$",
                 f"#{project}", f"(#{site})")
        for r_index in range(rooms_per_source):
            room = b.entity(
                "IFCSPACE", _s(gid(f"{source_id}-room-{r_index}")), "$",
                _s(f"Room {r_index}"), "$", "$", "$", "$", _s(f"R{r_index}"),
                ".ELEMENT.", ".SPACE.", "$",
            )
            b.entity("IFCRELAGGREGATES", _s(gid(f"{source_id}-agg-room-{r_index}")), "$",
                     "$", "$", f"#{site}", f"(#{room})")
            elements = []
            for e_index in range(elements_per_room):
                element = b.entity(
                    "IFCBUILDINGELEMENTPROXY",
                    _s(gid(f"{source_id}-elem-{r_index}-{e_index}")), "$",
                    _s(f"E{r_index}-{e_index}"), "$", "$", "$", "$", "$",
                    ".NOTDEFINED.",
                )
                elements.append(element)
                props = []
                for p_index in range(properties_per_element):
                    props.append(
                        b.entity(
                            "IFCPROPERTYSINGLEVALUE",
                            _s(f"P{p_index}"), "$",
                            _typed("IFCLENGTHMEASURE", f"{1000 + p_index}."), "$",
                        )
                    )
                pset = b.entity(
                    "IFCPROPERTYSET", _s(gid(f"{source_id}-pset-{r_index}-{e_index}")),
                    "$", _s("Pset_Large"), "$", "(" + ",".join(f"#{p}" for p in props) + ")",
                )
                b.entity(
                    "IFCRELDEFINESBYPROPERTIES",
                    _s(gid(f"{source_id}-rel-{r_index}-{e_index}")), "$", "$", "$",
                    f"(#{element})", f"#{pset}",
                )
            b.entity(
                "IFCRELCONTAINEDINSPATIALSTRUCTURE",
                _s(gid(f"{source_id}-cont-room-{r_index}")), "$", "$", "$",
                "(" + ",".join(f"#{e}" for e in elements) + ")", f"#{room}",
            )
        text = b.render(file_name=f"{source_id}.ifc")
        path = os.path.join(root, f"{source_id}.ifc")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        digest = sha256_bytes(text.encode("utf-8"))
        entries.append({"source_id": source_id, "path": f"{source_id}.ifc", "sha256": digest})
        expectations.append(
            {
                "source_id": source_id,
                "rooms": rooms_per_source,
                "elements": rooms_per_source * elements_per_room,
                "properties": rooms_per_source * elements_per_room * properties_per_element,
                "bytes": len(text.encode("utf-8")),
            }
        )
    _write_manifest(root, entries)
    return entries, expectations


def run_in_subprocess(script: str, *, timeout: int = 600) -> dict[str, Any]:
    """Run a measurement script in a fresh interpreter and parse its JSON.

    Used by tests so allocator state from earlier tests does not distort RSS.
    """

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "measure.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run(
            [sys.executable, "-I", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"measurement subprocess failed ({proc.returncode}):\n{proc.stderr}"
            )
        return json.loads(proc.stdout)
