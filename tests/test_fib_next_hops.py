"""fib-next-hops and the checks on it: next-hops the route table uses and the FIB leaves out.

The recording is SR Linux 25.10.5 (7220 IXR-D5, containerlab) with a VNI
conflict: an ip-vrf ``ss-vrf`` importing the routes of two tenant ip-vrfs, VNI 10
and VNI 20, from the same two leaves. Only the lowest VNI per remote VTEP is
programmed, so the VNI-20 next-hops are ``down-vni-conflict`` while the route
table shows them active.
"""

from __future__ import annotations

from pathlib import Path

from fcli.checks import CHECKS, ERROR
from fcli.fabric import Fabric
from fcli.inventory import Node
from fcli.reports import REGISTRY
from fcli.reports.routing import FibNextHop

from .replay import load, reads_state

RECORDING = Path(__file__).parent / "fixtures" / "vni_conflict" / "leaf3-25.10.5.json.gz"


def _recorded() -> list[FibNextHop]:
    data = load(RECORDING)
    entry = next(e for e in data["reports"] if e["report"] == "fib-next-hops")
    return [r for r in REGISTRY["fib-next-hops"].run(reads_state(data, entry)) if isinstance(r, FibNextHop)]


def _fabric(records: list[FibNextHop]) -> Fabric:
    names = sorted({r.node for r in records} or {"leaf3"})
    return Fabric.of([Node(n, n) for n in names], {"fib-next-hops": records})


def _run(check: str, records: list[FibNextHop]) -> list[object]:
    return list(CHECKS[check].run(_fabric(records)))


def test_a_vni_conflict_names_the_vrf_the_vtep_and_the_vni_left_out() -> None:
    rows = _recorded()
    assert {(r.ni, r.next_hop, r.vni, r.state) for r in rows} == {
        ("ss-vrf", "192.168.255.11", 20, "down-vni-conflict"),
        ("ss-vrf", "192.168.255.12", 20, "down-vni-conflict"),
    }
    # The /32s share one group with both VNIs; 20.0.0.0/24 has a group of VNI 20 alone.
    assert {(r.groups, r.groups_down) for r in rows} == {(3, 1)}


def test_vni_conflict_is_an_error_per_vrf_vtep_and_vni() -> None:
    findings = _run("vni_conflict", _recorded())
    assert [(f.node, f.severity, f.subject) for f in findings] == [
        ("leaf3", ERROR, "ss-vrf/192.168.255.11 vni 20"),
        ("leaf3", ERROR, "ss-vrf/192.168.255.12 vni 20"),
    ]
    assert "one VNI per remote VTEP" in findings[0].detail
    assert "1 with no member programmed" in findings[0].detail


def test_vni_conflict_leaves_a_hardware_failure_to_fib_nh_failed() -> None:
    failed = FibNextHop(
        node="leaf1", component="linecard 1/0", ni="vrf1", next_hop="10.0.0.2", state="failed", groups=1
    )
    assert _run("vni_conflict", [failed]) == []
    (finding,) = _run("fib_nh_failed", [failed])
    assert (finding.severity, finding.subject) == (ERROR, "vrf1/10.0.0.2")
    assert "out of hardware resources" in finding.detail


def test_a_next_hop_the_route_table_no_longer_has_is_still_reported() -> None:
    gone = FibNextHop(
        node="leaf1", component="linecard 1/0", next_hop="next-hop 42", state="down-vni-conflict", groups=1
    )
    (finding,) = _run("vni_conflict", [gone])
    assert finding.subject == "next-hop 42"


def test_nothing_is_reported_when_every_next_hop_is_programmed() -> None:
    assert _run("vni_conflict", []) == []
    assert _run("fib_nh_failed", []) == []
