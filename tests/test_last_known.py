"""sys-info never drops a node: one not read now is shown as last read, marked so."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from fcli import lastknown
from fcli.checks import ERROR, Finding
from fcli.cli import main
from fcli.delta import Change, Op, record_key
from fcli.gnmi.client import Tls
from fcli.inventory import Inventory, Node, from_clab
from fcli.reports import REGISTRY
from fcli.reports.system import NEVER_SEEN, UNREACHABLE, UP, Absent, SystemInfo, with_absent
from fcli.service import EngineService, LocalService
from fcli.store import Store, default_path

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget

ADDRESSES = {"leaf1": "172.30.30.11", "leaf2": "172.30.30.12", "leaf3": "172.30.30.13"}
LEAF2 = SystemInfo(
    node="leaf2",
    type="7220 IXR-D2L",
    serial_number="NS1234",
    part_number="3HE12345AARA01",
    hw_mac_address="1A:01:04:FF:00:00",
    last_booted="2026-11-01T20:20:00.092Z",
    software_version="26.7.2",
)
#: 2026-11-01T20:22:36Z
WENT = 1793564556.0

DATA = {
    f"/platform/chassis/{leaf}": {"srl_nokia-platform:platform": {"chassis": {leaf: value}}}
    for leaf, value in [
        ("type", "7220 IXR-D2L"),
        ("serial-number", "NS1234"),
        ("part-number", "3HE12345AARA01"),
        ("hw-mac-address", "1A:01:04:FF:00:00"),
        ("last-booted", "2026-11-01T20:20:00.092Z"),
    ]
} | {
    "/platform/control[slot=A]/software-version": {
        "srl_nokia-platform:platform": {"control": [{"slot": "A", "software-version": "v26.7.2-1-gabc"}]}
    },
}


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(DATA) as t:
        yield t


def _by_node(records: list[Any]) -> dict[str, Any]:
    return {r["node"] if isinstance(r, dict) else r.node: r for r in records}


def _unreachable(node: str) -> Finding:
    return Finding(node=node, check="node_unreachable", severity=ERROR, subject="gnmi", detail="nothing")


def _change(record: Any, ts: float, op: Op = Op.ADD, report: str = "sys-info") -> Change:
    dumped = record.model_dump(mode="json")
    before, after = (dumped, None) if op is Op.REMOVE else (None, dumped)
    return Change(ts, record.node, report, record_key(record), op, before, after)


def test_every_node_gets_a_row_said_for_what_it_is() -> None:
    leaf1 = LEAF2.model_copy(update={"node": "leaf1"})
    rows = _by_node(with_absent([leaf1], ADDRESSES, {"leaf2": Absent(seen=WENT, record=LEAF2)}))
    assert list(rows) == ["leaf1", "leaf2", "leaf3"]
    assert (rows["leaf1"].status, rows["leaf1"].last_seen, rows["leaf1"].address) == (UP, "", "172.30.30.11")
    # Seen once, then gone: everything it said, and when.
    down = rows["leaf2"]
    assert (down.status, down.last_seen, down.address) == (
        UNREACHABLE,
        "2026-11-01T20:22:36Z",
        "172.30.30.12",
    )
    assert (down.type, down.serial_number, down.part_number) == ("7220 IXR-D2L", "NS1234", "3HE12345AARA01")
    assert (down.hw_mac_address, down.software_version) == ("1A:01:04:FF:00:00", "26.7.2")
    # Never seen: the inventory's address, and that nothing else is known.
    never = rows["leaf3"]
    assert (never.status, never.address, never.type, never.serial_number) == (
        NEVER_SEEN,
        "172.30.30.13",
        "",
        "",
    )


def test_what_is_filled_in_is_not_what_a_record_is_filed_under() -> None:
    """The history keys sys-info by what the node said: an answer's status cannot change it."""
    (row,) = with_absent([LEAF2], {"leaf2": "a"}, {"leaf2": Absent(seen=WENT)})
    assert record_key(row) == record_key(LEAF2)


def test_a_one_shot_read_remembers_each_node_for_when_it_is_gone() -> None:
    first = lastknown.one_shot("lab", [LEAF2], {"leaf2": ADDRESSES["leaf2"]})
    assert first[0].status == UP
    assert lastknown.cache_path("lab").exists()
    (row,) = lastknown.one_shot("lab", [], {"leaf2": ADDRESSES["leaf2"]})
    assert (row.status, row.serial_number, row.software_version) == (UNREACHABLE, "NS1234", "26.7.2")
    assert row.last_seen.startswith(time.strftime("%Y-%m-%d", time.gmtime()))


def test_a_node_the_server_saw_go_is_answered_from_its_history() -> None:
    store = Store(default_path("lab"))
    store.write([_change(LEAF2, WENT - 120), _change(_unreachable("leaf2"), WENT, report="checks")])
    store.close()
    (row,) = lastknown.one_shot("lab", [], {"leaf2": ADDRESSES["leaf2"]})
    assert (row.status, row.last_seen, row.part_number) == (
        UNREACHABLE,
        "2026-11-01T20:22:36Z",
        "3HE12345AARA01",
    )


def test_a_newer_one_shot_reading_wins_over_an_older_history() -> None:
    store = Store(default_path("lab"))
    store.write([_change(LEAF2, WENT - 3600)])
    store.close()
    upgraded = LEAF2.model_copy(update={"software_version": "26.10.1"})
    lastknown.remember("lab", [upgraded], now=WENT)
    (row,) = lastknown.one_shot("lab", [], {"leaf2": ADDRESSES["leaf2"]})
    assert (row.software_version, row.last_seen) == ("26.10.1", "2026-11-01T20:22:36Z")


def test_at_marks_who_was_unreachable_then() -> None:
    store = Store(":memory:")
    store.write([_change(LEAF2, WENT - 120), _change(_unreachable("leaf2"), WENT, report="checks")])
    store.write([_change(_unreachable("leaf2"), WENT + 600, Op.REMOVE, report="checks")])
    state = store.state_at(WENT + 60, "sys-info")
    records = [SystemInfo.model_validate(r) for rows in state.values() for r in rows.values()]
    rows = _by_node(lastknown.at(store, WENT + 60, records, ADDRESSES))
    assert (rows["leaf2"].status, rows["leaf2"].last_seen, rows["leaf2"].serial_number) == (
        UNREACHABLE,
        "2026-11-01T20:22:36Z",
        "NS1234",
    )
    assert rows["leaf3"].status == NEVER_SEEN
    # Back by then.
    (later,) = lastknown.at(store, WENT + 900, records, {"leaf2": "a"})
    assert later.status == UP


async def test_the_engine_answers_a_down_node_as_it_last_read_it() -> None:
    nodes = tuple(Node(name, address) for name, address in ADDRESSES.items())
    leaf1 = LEAF2.model_copy(update={"node": "leaf1"})
    records = {"leaf1": [leaf1], "leaf2": [LEAF2]}
    engine = SimpleNamespace(
        plan=SimpleNamespace(reports={"sys-info": REGISTRY["sys-info"]}),
        store=None,
        listen=lambda listener: None,
        current=lambda report, names: [r for n in names for r in records.get(n, [])],
        status=lambda: [
            {"name": "leaf1", "status": "live", "error": "", "heard": WENT + 10},
            {"name": "leaf2", "status": "down", "error": "connection refused", "heard": WENT},
            {"name": "leaf3", "status": "down", "error": "connection refused", "heard": 0.0},
        ],
        raised={},
    )
    answer = await EngineService(engine, Inventory("lab", nodes)).records("sys-info")
    rows = _by_node(answer["records"])
    assert answer["errors"] == {"leaf2": "connection refused", "leaf3": "connection refused"}
    assert (rows["leaf1"]["status"], rows["leaf1"]["address"]) == (UP, "172.30.30.11")
    assert (rows["leaf2"]["status"], rows["leaf2"]["last_seen"], rows["leaf2"]["serial_number"]) == (
        UNREACHABLE,
        "2026-11-01T20:22:36Z",
        "NS1234",
    )
    assert (rows["leaf3"]["status"], rows["leaf3"]["address"]) == (NEVER_SEEN, "172.30.30.13")


async def test_a_node_read_once_then_lost_keeps_its_row(target: FakeTarget) -> None:
    def node(name: str, password: str = PASSWORD) -> Node:
        return Node(name, "127.0.0.1", target.port, USERNAME, password, Tls(insecure=True))

    up = await LocalService(Inventory("lab", (node("leaf1"), node("leaf2")))).records("sys-info")
    assert {r["status"] for r in up["records"]} == {UP} and not up["errors"]
    # leaf2 goes; leaf3 was never there.
    fabric = Inventory("lab", (node("leaf1"), node("leaf2", "wrong"), node("leaf3", "wrong")))
    answer = await LocalService(fabric).records("sys-info")
    rows = _by_node(answer["records"])
    assert set(answer["errors"]) == {"leaf2", "leaf3"}
    assert rows["leaf1"]["status"] == UP
    assert (rows["leaf2"]["status"], rows["leaf2"]["serial_number"], rows["leaf2"]["software_version"]) == (
        UNREACHABLE,
        "NS1234",
        "26.7.2",
    )
    assert rows["leaf2"]["last_seen"]
    assert (rows["leaf3"]["status"], rows["leaf3"]["address"], rows["leaf3"]["type"]) == (
        NEVER_SEEN,
        "127.0.0.1",
        "",
    )


def test_the_cli_table_shows_a_lost_node_as_last_read(target: FakeTarget, tmp_path: Path) -> None:
    def inventory(leaf2_password: str) -> list[str]:
        f = tmp_path / "inv.yml"
        defaults = {
            "username": USERNAME,
            "password": PASSWORD,
            "port": target.port,
            "tls": {"insecure": True},
        }
        nodes = {
            "leaf1": {"address": "127.0.0.1"},
            "leaf2": {"address": "127.0.0.1", "password": leaf2_password},
        }
        f.write_text(json.dumps({"defaults": defaults, "nodes": nodes}))
        return ["-c", str(f)]

    assert CliRunner().invoke(main, [*inventory(PASSWORD), "sys-info"]).exit_code == 0
    result = CliRunner().invoke(main, [*inventory("wrong"), "-o", "json", "sys-info"])
    assert result.exit_code == 1  # leaf2 failed, and says so
    rows = _by_node(json.loads(result.stdout))
    assert (rows["leaf2"]["status"], rows["leaf2"]["serial_number"]) == (UNREACHABLE, "NS1234")
    table = CliRunner().invoke(main, [*inventory("wrong"), "sys-info"], env={"COLUMNS": "200"}).stdout
    (row,) = [line for line in table.splitlines() if "leaf2" in line]
    assert "NS1234" in row and "3HE12345AARA01" in row and "unreachable" in row
    assert "last seen" in table


def test_a_node_never_seen_is_shown_as_the_topology_has_it(tmp_path: Path) -> None:
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    leaf1: {kind: nokia_srlinux, mgmt-ipv4: 172.30.30.11, labels: {role: leaf}}\n"
    )
    (node,) = from_clab(topo).nodes
    assert (node.address, node.managed_at) == ("clab-lab-leaf1", "172.30.30.11")
    (row,) = lastknown.one_shot("lab", [], {node.name: node.managed_at})
    assert (row.status, row.address, row.type, row.last_seen) == (NEVER_SEEN, "172.30.30.11", "", "")
