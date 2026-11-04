"""``python -m tests.release_coverage``: the SR Linux releases recorded, as the README shows them."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .replay import reads_recordings, recordings

#: The release lines a fabric is expected to run. Each has a leaf and a spine in both sets of recordings.
SUPPORTED = ("25.10", "26.3", "26.7")


def release_key(release: str) -> tuple[int, ...]:
    """A release as numbers, so 25.10 sorts after 25.3."""
    return tuple(int(part) for part in release.split("."))


def release_line(release: str) -> str:
    return ".".join(release.split(".")[:2])


def _cell(paths: Iterable[Path], line: str) -> str:
    nodes = Counter(p.parent.name for p in paths if release_line(p.parent.name) == line)
    releases = sorted(nodes, key=release_key)
    return ", ".join(f"{r} ({nodes[r]} node{'' if nodes[r] == 1 else 's'})" for r in releases)


def table() -> str:
    """One row per release line: what is recorded of it, in each set of recordings."""
    parity, reads = recordings(), reads_recordings()
    lines = {release_line(p.parent.name) for p in (*parity, *reads)}
    rows = [
        "| SR Linux | Rows match the original fcli | fcling's own reads and subscriptions |",
        "|---|---|---|",
    ]
    for line in sorted(lines, key=release_key):
        rows.append(f"| {line} | {_cell(parity, line) or '-'} | {_cell(reads, line) or '-'} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(table())
