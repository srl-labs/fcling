"""What sys-info last said of a node that cannot be read now.

A dead node is the one whose chassis type, serial and part number are wanted
most - to replace it. So sys-info never drops a node: one it cannot read is
shown as it was last read, marked ``unreachable`` and with when that was.

The last reading comes from where there is one: the engine's records, the
history the server keeps, or what the last one-shot read of each node left
in ``<fabric>.sys-info.json`` beside that history.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .checks import ERROR, Finding
from .delta import record_key
from .report import Record
from .reports.system import ANSWERED, UNREACHABLE, Absent, SystemInfo, with_absent
from .store import BeforeHistory, Store, default_path

logger = logging.getLogger(__name__)

REPORT = "sys-info"
#: The finding of a node nothing could be read from, as the history files it.
UNREACHABLE_KEY = record_key(
    Finding(node="", check="node_unreachable", severity=ERROR, subject="gnmi", detail="")
)


def cache_path(fabric: str) -> Path:
    return default_path(fabric).with_name(f"{fabric}.sys-info.json")


def _said(record: SystemInfo) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude=set(ANSWERED))


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        kept = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return kept if isinstance(kept, dict) else {}


def remember(fabric: str, records: Iterable[SystemInfo], now: float | None = None) -> None:
    """Keep what *records* say and when, for when their nodes are gone."""
    records = list(records)
    if not records:
        return
    path = cache_path(fabric)
    kept = _read_cache(path)
    when = time.time() if now is None else now
    for record in records:
        kept[record.node] = {"seen": when, "record": _said(record)}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(kept, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("could not keep sys-info in %s: %s", path, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _infos(records: Iterable[Record]) -> list[SystemInfo]:
    return [r for r in records if isinstance(r, SystemInfo)]


def _validate(row: Any) -> SystemInfo | None:
    try:
        return SystemInfo.model_validate(row)
    except ValidationError:
        return None


def recall(fabric: str, nodes: Sequence[str]) -> dict[str, Absent]:
    """The last sys-info of each of *nodes* anyone kept, and when it was read:
    the newer of the history's and the one-shot cache's."""
    found: dict[str, Absent] = {}
    for node, entry in _read_cache(cache_path(fabric)).items():
        record = _validate(entry.get("record")) if node in nodes and isinstance(entry, dict) else None
        if record is not None:
            found[node] = Absent(seen=entry.get("seen"), record=record)
    path = default_path(fabric)
    if not path.exists():
        return found
    store = Store(path)
    try:
        current = store.state_at(time.time(), REPORT, nodes)
        down = store.raised_at("checks", UNREACHABLE_KEY, nodes=nodes)
        as_of = store.changed(REPORT, nodes)
    except BeforeHistory:
        return found
    finally:
        store.close()
    for node in nodes:
        record = next((r for r in map(_validate, current.get(node, {}).values()) if r is not None), None)
        cached = found.get(node)
        # Each is a time the node was there; the history saw it go, if it did, after both.
        seen = max(
            filter(None, (down.get(node), as_of.get(node), cached.seen if cached else None)), default=None
        )
        # The history's reading, unless the cache's is at least as new.
        if record is not None and (cached is None or (cached.seen or 0) < as_of.get(node, 0)):
            found[node] = Absent(seen=seen, record=record)
        elif cached is not None:
            found[node] = Absent(seen=seen, record=cached.record)
    return found


def one_shot(
    fabric: str,
    records: Iterable[Record],
    addresses: Mapping[str, str],
) -> list[SystemInfo]:
    """sys-info as read now from the nodes of *addresses*, each node it could
    not read filled in from what was kept; and what was read, kept."""
    records = _infos(records)
    remember(fabric, records)
    read = {r.node for r in records}
    missing = [n for n in addresses if n not in read]
    absent = recall(fabric, missing) if missing else {}
    return with_absent(records, addresses, {n: absent.get(n, Absent()) for n in missing})


def live(
    records: Iterable[Record],
    addresses: Mapping[str, str],
    streams: Iterable[Mapping[str, Any]],
    raised: Mapping[tuple[str, str], float],
) -> list[SystemInfo]:
    """sys-info as the engine has it: a node not live is shown as last read,
    with its stream's state and when it was last heard - or went down."""
    absent = {
        s["name"]: Absent(
            status=UNREACHABLE if s["status"] == "down" else s["status"],
            seen=s.get("heard") or raised.get((s["name"], UNREACHABLE_KEY)),
        )
        for s in streams
        if s["status"] != "live"
    }
    return with_absent(_infos(records), addresses, absent)


def at(store: Store, ts: float, records: Iterable[Record], addresses: Mapping[str, str]) -> list[SystemInfo]:
    """sys-info as the history had it at *ts*: a node unreachable then marked so,
    with when it went."""
    down = store.raised_at("checks", UNREACHABLE_KEY, ts, list(addresses))
    return with_absent(
        _infos(records), addresses, {n: Absent(status=UNREACHABLE, seen=t) for n, t in down.items()}
    )
