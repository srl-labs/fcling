"""The fabric in layers: what runs over the cables, and what runs over that.

The topology is the physical layer - nodes, ports, cables. Two more sit on it:

* the **underlay**: the routing sessions of the default network-instance, each
  an edge between the two nodes that hold it - BGP sessions, IS-IS adjacencies,
  OSPF neighbours. Both ends report a session; it is one edge, down unless
  every end that reports it has it up. A BGP session carrying EVPN is the
  overlay's control plane, and is marked so;
* the **overlay**: each mac-vrf and ip-vrf as one service across the nodes
  that carry it, the irbs that route a mac-vrf into an ip-vrf, and the BGP
  sessions an ip-vrf holds with what is attached to it (PE-CE).

A session names its peer by address. The far node is found from the port the
session runs on - an unnumbered peer is ``fe80::…%ethernet-1/1.0``, and the
cable on that port says who is there - else from whose interface or loopback
the address is. A peer that is no node of ours is kept, with its address and
no far end: on the underlay it is a neighbour outside the fabric, on the
overlay a CE, matched to the client on the port the session runs over.

An **ethernet-segment** is of no one layer: it is a set of ports facing one
host, and so among the cables; it serves every service with a subinterface on
those ports, and a forwarder is elected for each. It is listed once, with its
client, its nodes and, per service, who was elected.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import Any

from .fabric import Fabric, out_of_band, parent
from .lenses.common import Index, ip

#: What the layers read, over what the topology does.
REQUIRES = ("bgp-peers", "isis", "ospf", "bfd")

_LOOPBACK = ("system", "lo")


def _zone(address: str) -> str:
    """``fe80::1%ethernet-1/1.0`` runs on ``ethernet-1/1.0``."""
    return address.split("%", 1)[1].strip() if "%" in address else ""


class _Addresses:
    """Whose each address is: every interface address of every network-instance."""

    def __init__(self, fabric: Fabric) -> None:
        self.owners: dict[Any, list[tuple[str, str, str]]] = {}
        self.router_ids: dict[str, set[str]] = {}
        for r in fabric.get("ni"):
            if r.ni == "default" and r.router_id:
                self.router_ids.setdefault(r.router_id, set()).add(r.node)
            for prefix in r.prefixes:
                address = ip(prefix)
                if address is not None and not address.is_link_local:
                    self.owners.setdefault(address, []).append((r.node, r.ni, r.interface))

    def far(self, node: str, ni: str, address: str) -> tuple[str, str] | None:
        """``(node, interface)`` of the one other node *address* is on - in *ni* where several have it."""
        found = [o for o in self.owners.get(ip(address), ()) if o[0] != node]
        same = [o for o in found if o[1] == ni]
        nodes = {o[0] for o in same or found}
        if len(nodes) != 1:
            return None
        owner = (same or found)[0]
        return owner[0], owner[2]

    def port(self, node: str, ni: str, address: str) -> str:
        """The port of *node* that *address* is on; ``""`` for a loopback, or none."""
        for owner, where, interface in self.owners.get(ip(address), ()):
            if owner == node and where == ni and not interface.startswith(_LOOPBACK):
                return parent(interface)
        return ""

    def router(self, node: str, router_id: str) -> str | None:
        nodes = self.router_ids.get(router_id, set()) - {node}
        return next(iter(nodes)) if len(nodes) == 1 else None


def _cabled(index: Index, node: str, port: str) -> tuple[str, str] | None:
    """Who is at the far end of a port - of a LAG, of its first cabled member."""
    for p in index.lag_members.get((node, port)) or (port,):
        if (node, p) in index.cables:
            return index.cables[(node, p)]
    return None


class _Edges:
    """Sessions folded: each reported by one end or by both, and one edge either way."""

    def __init__(self, index: Index) -> None:
        self.index = index
        self.found: dict[Any, dict[str, Any]] = {}

    def add(
        self,
        protocol: str,
        ni: str,
        node: str,
        far: str | None,
        *,
        up: bool,
        state: str,
        port: str = "",
        far_port: str = "",
        peer: str = "",
        tag: Any = "",
        **facts: Any,
    ) -> None:
        if far is None:
            key: Any = (protocol, ni, node, peer, tag)
        else:
            # By the cable it runs on, where one is known: so both ends name the same edge.
            cabled = (port and _cabled(self.index, node, port)) or None
            if cabled and cabled[0] == far:
                far_port = far_port or cabled[1]
                where: Any = frozenset({(node, port), cabled})
            elif far_port and (back := _cabled(self.index, far, far_port)) and back[0] == node:
                port = port or back[1]
                where = frozenset({(far, far_port), back})
            else:
                where = tag
            key = (protocol, ni, frozenset({node, far}), where)
        edge = self.found.setdefault(
            key, {"protocol": protocol, "ni": ni, "nodes": [], "peer": "" if far else peer, "sides": {}}
        )
        for name in (node, far):
            if name and name not in edge["nodes"]:
                edge["nodes"].append(name)
        edge["sides"][node] = {"up": up, "state": state, "port": port, **facts}
        if far and far_port:
            edge.setdefault("ports", {})[far] = far_port
        if port:
            edge.setdefault("ports", {})[node] = port

    def result(self, unreachable: Iterable[str]) -> list[dict[str, Any]]:
        gone = set(unreachable)
        out = []
        for edge in self.found.values():
            sides: dict[str, dict[str, Any]] = edge["sides"]
            a, b = (sorted(edge["nodes"]) + [None])[:2]
            ports = edge.get("ports", {})
            if not all(s["up"] for s in sides.values()):
                state = "down"
            elif any(n in gone for n in edge["nodes"]):
                # What a lost node last said of its sessions is no answer.
                state = "unknown"
            else:
                state = "up"
            families = sorted({f for s in sides.values() for f in s.get("families", ())})
            # Each end's AS: what it says of itself, else what the other end says of it.
            a_as, b_as = (
                sides.get(name or "", {}).get("asn") or sides.get(other or "", {}).get("peer_asn")
                for name, other in ((a, b), (b, a))
            )
            bfd = {s.get("bfd", "") for s in sides.values()} - {""}
            row: dict[str, Any] = {
                "protocol": edge["protocol"],
                "ni": edge["ni"],
                "a": a,
                "b": b,
                "peer": edge["peer"],
                "state": state,
                "a_port": ports.get(a, ""),
                "b_port": ports.get(b, "") if b else "",
                "a_state": sides.get(a or "", {}).get("state", ""),
                "b_state": sides.get(b or "", {}).get("state", ""),
            }
            if edge["protocol"] == "bgp":
                row |= {
                    "kind": "bgp" if a_as is None or b_as is None else "ibgp" if a_as == b_as else "ebgp",
                    "a_as": a_as,
                    "b_as": b_as,
                    "families": families,
                    "evpn": "evpn" in families,
                    # Routes of the underlay itself: a session for VPN routes alone carries none.
                    "underlay": any(f not in VPN_FAMILIES for f in families) or not families,
                    "bfd": "down" if "down" in bfd else "up" if "up" in bfd else "on" if bfd else "",
                }
            else:
                row["level"] = next((s["level"] for s in sides.values() if s.get("level")), "")
            out.append(row)
        return sorted(out, key=lambda e: (e["protocol"], e["a"] or "", e["b"] or "", e["a_port"], e["peer"]))


#: The families of the overlay's routes, not the underlay's: a session for these alone runs between loopbacks.
VPN_FAMILIES = frozenset({"evpn", "l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast"})


def _bfd(fabric: Fabric) -> dict[tuple[str, str, Any], str]:
    """``(node, network-instance, remote address)`` -> ``up`` or ``down``."""
    return {
        (s.node, s.ni, ip(s.remote_address)): "up" if s.state.lower() == "up" else "down"
        for s in fabric.get("bfd")
        if ip(s.remote_address) is not None
    }


def _bgp(fabric: Fabric, index: Index, addresses: _Addresses) -> Iterable[dict[str, Any]]:
    """Every BGP session with where it runs and who is at the far end, as its own node sees it."""
    bfd = _bfd(fabric)
    for p in fabric.get("bgp-peers"):
        if not p.peer or out_of_band(p.ni):
            continue
        zone = _zone(p.peer)
        port = parent(zone) if zone else addresses.port(p.node, p.ni, p.local_address)
        far: str | None = None
        far_port = ""
        cabled = _cabled(index, p.node, port) if port else None
        if zone and cabled:
            far, far_port = cabled
        elif owner := addresses.far(p.node, p.ni, p.peer):
            far = owner[0]
            far_port = "" if owner[1].startswith(_LOOPBACK) else parent(owner[1])
        elif cabled and p.ni == "default":
            # Its own address is on a cabled port, and nobody we read has the peer's: the node at the far end.
            far, far_port = cabled
        address = ip(p.peer)
        seen = bfd.get((p.node, p.ni, address), "")
        yield {
            "record": p,
            "far": far,
            "port": port,
            "via": zone,
            "far_port": far_port,
            "up": p.state == "established",
            "families": tuple(f.name for f in p.families if f.enabled),
            "bfd": seen or ("on" if p.bfd else ""),
            "version": address.version if address is not None else 0,
        }


def _underlay(fabric: Fabric, index: Index, addresses: _Addresses, sessions: Sequence[dict[str, Any]]) -> Any:
    edges = _Edges(index)
    for s in sessions:
        p = s["record"]
        if p.ni != "default":
            continue
        edges.add(
            "bgp",
            p.ni,
            p.node,
            s["far"],
            up=s["up"],
            state=p.state,
            port=s["port"],
            far_port=s["far_port"],
            peer=p.peer,
            tag=s["version"],
            families=s["families"],
            asn=p.local_as,
            peer_asn=p.peer_as,
            bfd=s["bfd"],
        )
    for r in fabric.get("isis"):
        if r.passive or r.interface.startswith(_LOOPBACK) or out_of_band(r.interface):
            continue
        port = parent(r.interface)
        cabled = _cabled(index, r.node, port)
        far = cabled[0] if cabled else (fabric.resolve(r.hostname) if r.hostname else None)
        if far is None or far == r.node:
            continue
        # An interface with a cable and no adjacency on it: the adjacency that should be there.
        edges.add(
            "isis",
            r.ni,
            r.node,
            far,
            up=r.state.lower() == "up",
            state=r.state or "no adjacency",
            port=port,
            tag=r.level,
            level=r.level,
        )
    for r in fabric.get("ospf"):
        if r.passive or r.interface.startswith(_LOOPBACK) or out_of_band(r.interface):
            continue
        port = parent(r.interface)
        cabled = _cabled(index, r.node, port)
        far = cabled[0] if cabled else None
        if far is None and r.router_id:
            owner = addresses.far(r.node, r.ni, r.address) if r.address else None
            far = owner[0] if owner else addresses.router(r.node, r.router_id)
        if far is None or far == r.node:
            continue
        edges.add(
            "ospf",
            r.ni,
            r.node,
            far,
            up=r.state.lower() == "full",
            state=r.state or "no neighbor",
            port=port,
            tag=r.area,
            level=f"area {r.area}",
        )
    return edges.result(fabric.unreachable)


def _within(prefixes: Iterable[str], address: Any) -> bool:
    for prefix in prefixes:
        try:
            if address in ipaddress.ip_interface(prefix).network:
                return True
        except ValueError:
            continue
    return False


def _overlay(
    index: Index, sessions: Sequence[dict[str, Any]], clients: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {}
    irbs: list[dict[str, Any]] = []
    for (node, name), s in sorted(index.services.items()):
        if s.type not in ("mac-vrf", "ip-vrf") or out_of_band(name):
            continue
        access = [p for p in s.ports if p.kind != "irb"]
        service = services.setdefault(
            name,
            {"name": name, "type": s.type, "vnis": [], "evis": [], "linked": [], "subnets": [], "nodes": []},
        )
        for field, values in (("vnis", s.vnis), ("evis", s.evis), ("subnets", s.subnets)):
            service[field] += [v for v in values if v not in service[field]]
        for other in s.linked:
            linked = index.services.get((node, other))
            if linked and linked.type in ("mac-vrf", "ip-vrf") and other not in service["linked"]:
                service["linked"].append(other)
        service["nodes"].append(
            {
                "node": node,
                "oper": s.oper,
                "ports": len(access),
                "down": sum(1 for p in access if p.oper != "up"),
                "gateway": s.gateway,
            }
        )
        if s.type != "mac-vrf":
            continue
        for p in s.ports:
            routed = index.services.get((node, p.peer_ni)) if p.kind == "irb" and p.peer_ni else None
            if routed and routed.type == "ip-vrf":
                irbs.append(
                    {
                        "node": node,
                        "interface": p.name,
                        "mac_vrf": name,
                        "ip_vrf": p.peer_ni,
                        "oper": p.oper,
                        "addresses": list(p.addresses),
                        "anycast_gw": p.anycast_gw,
                    }
                )
    for service in services.values():
        service["linked"].sort()

    def client_on(node: str, **where: str) -> str | None:
        """The one client attached to *node* there; none when it could be any of several."""
        found = {
            c["id"]
            for c in clients
            for a in c["attachments"]
            if a["node"] == node and all(a[k] == v for k, v in where.items())
        }
        return found.pop() if len(found) == 1 else None

    pe_ce: list[dict[str, Any]] = []
    for session in sessions:
        p = session["record"]
        vrf = index.services.get((p.node, p.ni))
        if vrf is None or vrf.type != "ip-vrf":
            continue
        address = ip(p.peer)
        # The subinterface the session runs over: named by an unnumbered peer,
        # else the one whose subnet the peer is in.
        via = session["via"] or next(
            (port.name for port in vrf.ports if address is not None and _within(port.addresses, address)),
            "",
        )
        port = next((port for port in vrf.ports if port.name == via), None)
        bridge = port.peer_ni if port is not None and port.kind == "irb" else ""
        client = None
        if session["far"] is None and via:
            client = client_on(p.node, service=bridge) if bridge else client_on(p.node, subinterface=via)
        pe_ce.append(
            {
                "node": p.node,
                "service": p.ni,
                "peer": p.peer,
                "state": "up" if session["up"] else "down",
                "session_state": p.state,
                "local_as": p.local_as,
                "peer_as": p.peer_as,
                "families": list(session["families"]),
                "bfd": session["bfd"],
                "via": via,
                "bridge": bridge,
                "client": client,
                # A peer that is a node of ours: another PE of the fabric, not a CE.
                "far": session["far"],
            }
        )
    return {
        "services": sorted(services.values(), key=lambda x: (x["type"] != "ip-vrf", x["name"])),
        "irbs": irbs,
        "sessions": sorted(pe_ce, key=lambda x: (x["node"], x["service"], x["peer"])),
    }


def _segments(fabric: Fabric, index: Index, clients: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each ethernet-segment across the nodes it is on: the client it faces, the services it
    serves there, and for each the node elected to forward for it."""
    owner = index.loopback_owner
    found: dict[str, dict[str, Any]] = {}
    for es in fabric.get("es"):
        if not es.esi:
            continue
        seg = found.setdefault(
            es.esi,
            {
                "esi": es.esi,
                "names": [],
                "mode": es.mh_mode,
                # A virtual segment tracks a next-hop in an ip-vrf, not a port: no place among the cables.
                "virtual": es.type == "virtual" or (not any(es.interfaces) and bool(es.next_hops)),
                "client": None,
                "next_hops": [],
                "nodes": [],
                "services": {},
            },
        )
        if es.name and es.name not in seg["names"]:
            seg["names"].append(es.name)
        seg["next_hops"] = sorted({*seg["next_hops"], *(nh.address for nh in es.next_hops)})
        seg["nodes"].append({"node": es.node, "interfaces": [i for i in es.interfaces if i], "oper": es.oper})
        for a in es.associations:
            # Who this node has elected, as a node of ours where the candidate's address is one's loopback.
            elected = [owner.get(c.address, c.address) for c in a.candidates if c.designated]
            service = seg["services"].setdefault(a.ni, {"name": a.ni, "nodes": [], "df": []})
            service["nodes"].append(
                {
                    "node": es.node,
                    "df": elected[0] if elected else None,
                    "candidates": sorted(owner.get(c.address, c.address) for c in a.candidates),
                }
            )
            service["df"] = sorted({*service["df"], *elected})
    by_esi: dict[str, str] = {}
    for c in clients:
        for esi in {c.get("esi"), *(a.get("esi") for a in c["attachments"])}:
            if esi:
                by_esi.setdefault(esi, c["id"])
    out = []
    for esi, seg in sorted(found.items()):
        seg["client"] = by_esi.get(esi)
        seg["services"] = sorted(seg["services"].values(), key=lambda s: s["name"])
        # A service with a port on the segment and no election read for it is still one it serves.
        named = {s["name"] for s in seg["services"]}
        ports = {(n["node"], i) for n in seg["nodes"] for i in n["interfaces"]}
        for (node, name), s in sorted(index.services.items()):
            if name not in named and any((node, parent(p.name)) in ports for p in s.ports):
                named.add(name)
                seg["services"].append({"name": name, "nodes": [], "df": []})
        out.append(seg)
    return out


def build(fabric: Fabric, index: Index, clients: Sequence[dict[str, Any]]) -> dict[str, Any]:
    addresses = _Addresses(fabric)
    sessions = list(_bgp(fabric, index, addresses))
    return {
        "underlay": _underlay(fabric, index, addresses, sessions),
        "overlay": _overlay(index, sessions, clients),
        "segments": _segments(fabric, index, clients),
    }
