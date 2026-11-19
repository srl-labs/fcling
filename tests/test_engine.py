"""The engine against an in-process target: sync, live changes, history."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from fcli.engine import Engine, Status
from fcli.gnmi.client import Tls
from fcli.inventory import Node
from fcli.report import NodeState, Read, Record, Report, default_columns
from fcli.reports import REGISTRY
from fcli.store import AsyncStore

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget

NEIGHBORS = "/network-instance[name=*]/protocols/bgp/neighbor"
DATA = {
    NEIGHBORS: {
        "srl_nokia-network-instance:network-instance": [
            {
                "name": "default",
                "protocols": {
                    "srl_nokia-bgp:bgp": {
                        "neighbor": [
                            {"peer-address": "10.0.0.1", "session-state": "established", "peer-as": 65001},
                            {"peer-address": "10.0.0.2", "session-state": "active", "peer-as": 65002},
                        ]
                    }
                },
            }
        ]
    },
}
PEER = "/network-instance[name=default]/protocols/bgp/neighbor[peer-address={}]"


class Missing(Record):
    thing: str


def _nothing(node: NodeState) -> Iterator[Missing]:
    yield from ()


#: Built, not registered: it reads what the target lacks, and is no command.
MISSING = Report(
    name="test-missing",
    help="reads what the target lacks",
    reads=(Read("/platform/fabric[slot=*]"),),
    record=Missing,
    build=_nothing,
    columns=default_columns(Missing),
)


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(DATA, unknown=frozenset({"fabric"})) as t:
        yield t


@pytest.fixture
async def engine(target: FakeTarget, tmp_path: Path) -> AsyncIterator[tuple[Engine, list[dict[str, Any]]]]:
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"], MISSING], store, settle=0.01, checks=False)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    yield eng, events
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()


async def _until(condition: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def _changes(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(e["op"], e["report"], e["key"]) for e in events if e["type"] == "change"]


async def test_syncs_without_what_the_node_lacks(
    engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget
) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    stream = eng.streams["leaf1"]
    assert stream.status is Status.LIVE
    assert [r.path for r in stream.excluded] == ["/platform/fabric[slot=*]"]
    # Refused once with the path, then subscribed without it.
    assert any("platform/fabric" in p for p in target.servicer.subscribes[0])
    assert not any("platform/fabric" in p for p in target.servicer.subscribes[-1])
    assert sorted(_changes(events)) == [
        ("add", "bgp-peers", '["default","10.0.0.1"]'),
        ("add", "bgp-peers", '["default","10.0.0.2"]'),
    ]


async def test_live_changes_are_kept(engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    before = time.time()
    events.clear()

    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    await _until(lambda: _changes(events))
    assert _changes(events) == [("change", "bgp-peers", '["default","10.0.0.2"]')]
    change = next(e for e in events if e["type"] == "change")
    assert (change["before"]["state"], change["after"]["state"]) == ("active", "established")
    assert change["seq"] > 0

    events.clear()
    target.push(deletes=[PEER.format("10.0.0.1")])
    await _until(lambda: _changes(events))
    assert _changes(events) == [("remove", "bgp-peers", '["default","10.0.0.1"]')]

    assert eng.store is not None
    history = await eng.store.history(since=before)
    assert [h["op"] for h in history] == ["change", "remove"]
    then = await eng.store.state_at(before, "bgp-peers")
    assert {r["peer"]: r["state"] for r in then["leaf1"].values()} == {
        "10.0.0.1": "established",
        "10.0.0.2": "active",
    }


async def test_counters_alone_are_not_history(
    engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget
) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    events.clear()
    target.push(
        {
            PEER.format("10.0.0.1") + "/afi-safi[afi-safi-name=evpn]": {
                "admin-state": "enable",
                "received-routes": "5",
            }
        }
    )
    await _until(lambda: _changes(events))
    # A family appearing is a change; its count moving afterwards is not.
    assert _changes(events)[0][0] == "change"
    events.clear()
    target.push({PEER.format("10.0.0.1") + "/afi-safi[afi-safi-name=evpn]/received-routes": "6"})
    await _until(lambda: _changes(events))
    assert _changes(events) == [("update", "bgp-peers", '["default","10.0.0.1"]')]
    assert next(e for e in events if e["type"] == "change")["seq"] == 0


async def test_findings_are_raised_and_cleared(target: FakeTarget, tmp_path: Path) -> None:
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"]], store, settle=0.01)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:

        def findings() -> list[tuple[str, str]]:
            return sorted((f.check, f.subject) for f in eng.current("checks"))

        await _until(lambda: findings() == [("bgp_down", "default/10.0.0.2")])
        (raised,) = eng.raised.values()
        assert raised <= time.time()
        target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
        await _until(lambda: findings() == [])
        assert ("remove", "checks", '["bgp_down","default/10.0.0.2"]') in _changes(events)
        assert eng.raised == {}
        assert eng.store is not None
        kept = await eng.store.history(reports=["checks"])
        assert [c["op"] for c in kept] == ["add", "remove"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.close()


async def test_a_lost_node_stays_lost_while_it_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each reconnect must not clear node_unreachable, only to raise it again when it fails."""
    import socket

    import fcli.engine

    monkeypatch.setattr(fcli.engine, "BACKOFF", (0.05, 0.2))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once closed
    node = Node("spine1", "127.0.0.1", port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"]], store, settle=0.01)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:
        await _until(lambda: [f.check for f in eng.current("checks")] == ["node_unreachable"])
        await asyncio.sleep(1.5)  # many attempts at 0.05-0.2s apart
        states = [e["status"] for e in events if e["type"] == "node"]
        assert states and states[-1] == "down"
        assert "connecting" not in states[states.index("down") :]
        assert [op for op, report, _ in _changes(events) if report == "checks"] == ["add"]
        assert eng.fabric().unreachable == {"spine1": eng.streams["spine1"].lost}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.close()


def test_a_set_coming_back_in_another_order_is_no_change() -> None:
    from fcli.delta import diff, keyed
    from fcli.reports.routing import Route, RouteNextHop
    from fcli.reports.services import BgpSession, Service

    def route(*hops: str) -> Route:
        return Route(
            node="leaf1",
            ni="default",
            prefix="10.0.0.0/24",
            next_hops=tuple(RouteNextHop(address=a) for a in hops),
        )

    def service(*peers: str) -> Service:
        return Service(
            node="leaf1",
            name="ipvrf1",
            bgp_peers=peers,
            bgp_sessions=tuple(BgpSession(peer=p) for p in peers),
        )

    assert route("10.0.0.10", "10.0.0.2") == route("10.0.0.2", "10.0.0.10")
    assert [h.address for h in route("10.0.0.10", "10.0.0.2").next_hops] == ["10.0.0.2", "10.0.0.10"]
    assert not diff("leaf1", "services", keyed([service("b", "a")]), keyed([service("a", "b")]), 0.0)
    assert diff("leaf1", "services", keyed([service("a")]), keyed([service("a", "b")]), 0.0)
    # A field not declared unordered keeps the order it was given.
    assert Service(node="leaf1", name="ipvrf1", import_rts=("2:2", "1:1")).import_rts == ("2:2", "1:1")


def test_a_counter_ticking_changes_only_the_reports_that_read_counters() -> None:
    """An ON_CHANGE subscription to an interface streams its statistics about once a
    second: every report reading the interface re-ran on each tick, for nothing."""
    from fcli.engine import Plan
    from fcli.gnmi import path as gpath

    plan = Plan([r for r in REGISTRY.values() if not r.hidden])

    def touched(path: str) -> frozenset[str]:
        return plan.touched(gpath.parse(path))

    subif = "/interface[name=ethernet-1/1]/subinterface[index=0]"
    assert {"subif", "services", "ni"} <= touched(f"{subif}/oper-state")
    assert touched(f"{subif}/statistics/in-packets") == frozenset()
    # Deeper than the first elements the plan looks at: still counters.
    assert (
        touched("/interface[name=lag1]/lag/member[name=ethernet-1/1]/lacp/statistics/lacp-in-pkts") == set()
    )
    assert "lag" in touched("/interface[name=lag1]/lag/member[name=ethernet-1/1]/oper-state")
    # The report that reads counters still hears them.
    assert touched("/interface[name=ethernet-1/1]/statistics/in-octets") == {"ifstats"}


def test_the_engine_waits_longer_after_a_long_pass() -> None:
    from fcli.engine import MAX_SETTLE

    eng = Engine([Node("leaf1", "127.0.0.1:1")], [REGISTRY["bgp-peers"]], settle=0.5)
    assert eng.wait() == 0.5
    eng.busy = 3.0
    assert eng.wait() == 3.0
    eng.busy = 60.0
    assert eng.wait() == MAX_SETTLE


async def test_an_empty_container_already_held_is_no_news(target: FakeTarget) -> None:
    """SR Linux sends ``/interface[name=lag1]`` as ``{}`` every second: every report
    reading interfaces re-ran on it, for nothing. An empty one that creates is news."""
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    eng = Engine([node], [REGISTRY["bgp-peers"]], settle=0.01, checks=False)
    passes: list[set[str]] = []
    evaluate = eng.evaluate

    def counted() -> Any:
        passes.append(set().union(*(s.dirty for s in eng.streams.values())))
        return evaluate()

    eng.evaluate = counted  # type: ignore[method-assign]
    task = asyncio.create_task(eng.run())
    try:
        await _until(lambda: len(eng.current("bgp-peers")) == 2 and target.streaming)
        await asyncio.sleep(0.3)
        settled = len(passes)
        target.push({PEER.format("10.0.0.1"): {}})
        await asyncio.sleep(0.5)
        assert len(passes) == settled
        target.push({PEER.format("10.0.0.3"): {}})
        await _until(lambda: len(passes) > settled)
        assert passes[-1] == {"bgp-peers"}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_only_the_keys_a_read_names_decide_what_an_update_touches() -> None:
    """Cached without the keys no read names: a MAC address in each made every update a miss."""
    from fcli.engine import Plan
    from fcli.gnmi import path as gpath

    plan = Plan([r for r in REGISTRY.values() if not r.hidden])
    irb = plan.touched(gpath.parse("/interface[name=irb0]/subinterface[index=1]/oper-state"))
    eth = plan.touched(gpath.parse("/interface[name=ethernet-1/1]/subinterface[index=1]/oper-state"))
    assert "irb" in irb and "irb" not in eth
    macs = [
        plan.touched(gpath.parse(f"/network-instance[name=v{i}]/bridge-table/mac-table/mac[address={i}]/x"))
        for i in range(50)
    ]
    assert len(set(macs)) == 1 and "mac" in macs[0]
    assert plan.affected.cache_info().currsize < 10


def test_the_reads_under_one_list_share_a_subscription() -> None:
    """SR Linux keeps the order of one subscription only: a network-instance's ``type``
    streamed apart from the rest of it came back after its delete, as an empty one."""
    from fcli.engine import Plan
    from fcli.gnmi import path as gpath

    plan = Plan([r for r in REGISTRY.values() if not r.hidden])
    chunks = plan.chunks(20)
    assert sorted(r.path for c in chunks for r in c if r.path in {x.path for x in plan.streamed}) == sorted(
        r.path for r in plan.streamed
    )
    assert all(len(c) <= 20 for c in chunks)

    def where(prefix: tuple[str, ...]) -> set[int]:
        return {
            i
            for i, c in enumerate(chunks)
            for r in c
            if tuple(e.name for e in gpath.parse(r.path)[: len(prefix)]) == prefix
        }

    assert len(where(("network-instance",))) == 1
    # A FIB table on its own: the node ends a subscription to it that changes too fast.
    fib = [i for i, c in enumerate(chunks) if any("/fib-table/" in r.path for r in c)]
    assert len(fib) == 1 and len(chunks[fib[0]]) == 2  # the table and the heartbeat


async def test_a_subscription_the_node_ends_for_an_overflow_is_made_again_alone(target: FakeTarget) -> None:
    """SR Linux ends a subscription to a FIB table that changed faster than it could
    send. The node is not down: that subscription is made again, and what it says now
    replaces what it said before - a peer gone meanwhile included - all at once."""
    import grpc

    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    eng = Engine([node], [REGISTRY["bgp-peers"]], settle=0.01, checks=False)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:
        await _until(lambda: len(eng.current("bgp-peers")) == 2 and target.streaming)
        subscribes = len(target.servicer.subscribes)
        settled = len(events)
        import copy

        from fcli.gnmi import path as gp

        # 10.0.0.1 gone while no one was told: the node now has only 10.0.0.2.
        key = gp.schema(gp.parse(NEIGHBORS))
        now = copy.deepcopy(target.servicer.data[key])
        bgp = now["srl_nokia-network-instance:network-instance"][0]["protocols"]["srl_nokia-bgp:bgp"]
        bgp["neighbor"] = bgp["neighbor"][1:]
        target.servicer.data[key] = now
        target.fail(grpc.StatusCode.RESOURCE_EXHAUSTED, "State response has overflowed its change buffer")
        await _until(lambda: [p.peer for p in eng.current("bgp-peers")] == ["10.0.0.2"])
        assert len(target.servicer.subscribes) == subscribes + 1
        assert [e["status"] for e in events[settled:] if e["type"] == "node"] == []  # never down or syncing
        assert eng.streams["leaf1"].status is Status.LIVE
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
