"""sys-info: the chassis and the software it runs; config-commits: who changed it, when."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import ClassVar

from ..report import Column, NodeState, Read, Record, as_int, report

_CHASSIS_LEAVES = ("type", "serial-number", "part-number", "hw-mac-address", "last-booted")

#: A node read now.
UP = "up"
#: A node not read now, shown as it last was.
UNREACHABLE = "unreachable"
#: A node never read: what the inventory says of it, and nothing more.
NEVER_SEEN = "never seen"


class SystemInfo(Record):
    #: What a record is filed under: what the node said of itself, not what
    #: is filled in as it is answered.
    key: ClassVar[tuple[str, ...]] = (
        "type",
        "serial_number",
        "part_number",
        "hw_mac_address",
        "last_booted",
        "software_version",
    )

    type: str = ""
    serial_number: str = ""
    part_number: str = ""
    hw_mac_address: str = ""
    last_booted: str = ""
    #: The release alone, ``26.7.1``: SR Linux reports ``v26.7.1-554-g78ed635f70a``.
    software_version: str = ""
    #: Where it is managed: the topology's mgmt address, or else what fcling
    #: reaches it at. This and the rest are filled in as sys-info is answered,
    #: never kept: a node never read has them too.
    address: str = ""
    #: ``up``, or why the rest is not from now: ``unreachable``, ``never seen``,
    #: or the engine's word for a node it is (re)connecting to.
    status: str = ""
    #: When a node not read now was last read, UTC; empty when nobody knows.
    last_seen: str = ""


#: The fields filled in as sys-info is answered, not read from the node.
ANSWERED = frozenset({"address", "status", "last_seen"})


@dataclass(frozen=True)
class Absent:
    """A node sys-info has nothing from now: why, and what it said last."""

    status: str = UNREACHABLE
    #: When it was last read, if anyone knows.
    seen: float | None = None
    #: What it said then, if anyone kept it.
    record: SystemInfo | None = None


def _utc(ts: float | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def with_absent(
    records: Iterable[SystemInfo], addresses: Mapping[str, str], absent: Mapping[str, Absent]
) -> list[SystemInfo]:
    """One row for each node of *addresses*: as read now, as it was last read -
    marked so - or, for a node never read, what the inventory knows. A dead
    node is the one whose chassis and serial are wanted most: it never drops out.
    """
    read = {r.node: r for r in records}
    out = []
    for node, address in addresses.items():
        gone = absent.get(node)
        record = read.get(node)
        if gone is None and record is not None:
            out.append(record.model_copy(update={"address": address, "status": UP}))
            continue
        gone = gone or Absent(status=NEVER_SEEN)
        record = record or gone.record
        if record is None:
            out.append(SystemInfo(node=node, address=address, status=NEVER_SEEN))
        else:
            out.append(
                record.model_copy(
                    update={"address": address, "status": gone.status, "last_seen": _utc(gone.seen)}
                )
            )
    return out


@report(
    "sys-info",
    "System information of nodes",
    record=SystemInfo,
    columns=[
        *(Column(name.replace("_", "-"), name) for name in SystemInfo.key),
        Column("mgmt", "address"),
        Column("status", lambda r: f"{r.status}\nlast seen {r.last_seen}" if r.last_seen else r.status),
    ],
    # The leaves alone: the chassis container also carries power and environment readings.
    reads=[
        *(Read(f"/platform/chassis/{leaf}") for leaf in _CHASSIS_LEAVES),
        Read("/platform/control[slot=A]/software-version"),
    ],
)
def sys_info(node: NodeState) -> Iterator[SystemInfo]:
    chassis = node.tree.get("/platform/chassis") or {}
    version = str(node.tree.get("/platform/control[slot=A]/software-version") or "")
    yield SystemInfo(
        node=node.name,
        type=str(chassis.get("type") or ""),
        serial_number=str(chassis.get("serial-number") or ""),
        part_number=str(chassis.get("part-number") or ""),
        hw_mac_address=str(chassis.get("hw-mac-address") or ""),
        last_booted=str(chassis.get("last-booted") or ""),
        software_version=version.split("-")[0].lstrip("v"),
    )


class ConfigCommit(Record):
    """One commit to the running configuration, as the node logs it.

    SR Linux keeps a bounded log, so the oldest commits age out: it says who
    changed the configuration and when, not what.
    """

    key: ClassVar[tuple[str, ...]] = ("id",)

    id: int
    #: ``complete``, or how a commit that did not finish ended.
    status: str = ""
    username: str = ""
    comment: str = ""
    #: The candidate it was made in: ``shared``, ``private``, ``exclusive``.
    type: str = ""
    #: The candidate's name, ``default`` for the shared one.
    session: str = ""
    started: str = ""
    ended: str = ""


@report(
    "config-commits",
    "Commits each node logged to its configuration",
    record=ConfigCommit,
    reads=[Read("/system/configuration/commit")],
    columns=[
        Column("id", "id"),
        Column("status", "status"),
        Column("user", "username"),
        Column("comment", "comment"),
        Column("session", "session"),
        Column("type", "type"),
        Column("started", "started"),
        Column("ended", "ended"),
    ],
)
def config_commits(node: NodeState) -> Iterator[ConfigCommit]:
    commits = []
    for m in node.tree.select("/system/configuration/commit"):
        entry = m.value
        commit_id = as_int(entry.get("id"))
        if commit_id is None:
            continue
        commits.append(
            ConfigCommit(
                node=node.name,
                id=commit_id,
                status=str(entry.get("status") or ""),
                username=str(entry.get("username") or ""),
                comment=str(entry.get("comment") or ""),
                type=str(entry.get("type") or ""),
                session=str(entry.get("name") or ""),
                started=str(entry.get("started") or ""),
                ended=str(entry.get("ended") or ""),
            )
        )
    yield from sorted(commits, key=lambda c: c.id)
