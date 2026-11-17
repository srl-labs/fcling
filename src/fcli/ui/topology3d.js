// The fabric in layers: the cables, the routing that runs over them, the services over that -
// three planes stacked, each node in the same place on every one, turned by hand.

import { html, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { natural } from "./api.js";
import { Details, Search, layout, remember, remembered } from "./topology.js";

export const LAYERS = [
  { id: "physical", title: "Physical", note: "nodes, ports, cables" },
  { id: "underlay", title: "Underlay", note: "routing sessions" },
  { id: "overlay", title: "Overlay", note: "mac-vrfs, ip-vrfs, irbs, PE-CE" },
];
/** Where the drawing is seen from: turned about the vertical, tilted above the horizon, the planes this far apart. */
const CAMERA = { yaw: -24, pitch: 32, gap: 200 };
const CAMERA_KEY = "fcli-camera";
/** A node's footprint on a plane, and on the overlay, where its services stand on it. */
const PAD = 34;
const FOOT = 82;
const MARGIN = 78;
/** One colour per service, the same on every node that carries it. */
const PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#ca8a04", "#dc2626", "#4f46e5", "#65a30d"];
/** More services than this are not all drawn at once: one tenant, or one service, is. */
const CROWD = 12;

const short = (port) => String(port || "").replace(/^ethernet-/, "e");
const pairOf = (a, b) => [a, b].sort(natural).join("|");

/** An underlay edge's id: stable while the sessions are, whatever order they come in. */
function withIds(edges) {
  const seen = new Map();
  return edges.map((e) => {
    const base = [e.protocol, e.a, e.b ?? "", e.a_port, e.peer].join("|");
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return { ...e, id: n ? `${base}|${n}` : base };
  });
}

/** The services a scope shows: all of them, a tenant (an ip-vrf and what is routed into it), or one and what it is linked to. */
function inScope(services, scope) {
  if (scope === "all") return services;
  const name = scope.slice(scope.indexOf(":") + 1);
  const picked = services.find((s) => s.name === name);
  if (!picked) return services;
  return services.filter((s) => s.name === name || s.linked.includes(name) || picked.linked.includes(s.name));
}

function defaultScope(services) {
  if (services.length <= CROWD) return "all";
  const tenant = services.find((s) => s.type === "ip-vrf");
  return tenant ? `tenant:${tenant.name}` : `service:${services[0].name}`;
}

/** What the selection touches, as keys: n: node, l: cable pair, e: session, s: service, i: service@node, c: client, x: CE. */
function touched(selected, graph, edges, overlay) {
  if (!selected) return null;
  const keep = new Set();
  const { type, id } = selected;
  const ce = (s) => keep.add(`x:${s.client ? `c:${s.client}` : `${s.service}|${s.peer}`}`);
  if (type === "node") {
    keep.add(`n:${id}`);
    for (const l of graph.links) if (l.a === id || l.b === id) keep.add(`l:${l.a}|${l.b}`).add(`n:${l.a}`).add(`n:${l.b}`);
    for (const e of edges) if (e.a === id || e.b === id) keep.add(`e:${e.id}`).add(`n:${e.a}`).add(`n:${e.b}`);
    for (const s of overlay.services) if (s.nodes.some((n) => n.node === id)) keep.add(`i:${s.name}@${id}`);
    for (const c of graph.clients) if (c.nodes.includes(id)) keep.add(`c:${c.id}`);
    for (const s of overlay.sessions) if (s.node === id) ce(s);
  } else if (type === "link") {
    const [a, b] = id.split("|");
    keep.add(`l:${id}`).add(`n:${a}`).add(`n:${b}`);
    for (const e of edges) if (e.b && pairOf(e.a, e.b) === pairOf(a, b) && e.a_port) keep.add(`e:${e.id}`);
  } else if (type === "session") {
    const e = edges.find((x) => x.id === id);
    if (e) {
      keep.add(`e:${e.id}`).add(`n:${e.a}`);
      if (e.b) keep.add(`n:${e.b}`);
      // The cable it runs on: a session between loopbacks runs on none in particular.
      if (e.b && e.a_port) for (const l of graph.links) if (pairOf(l.a, l.b) === pairOf(e.a, e.b)) keep.add(`l:${l.a}|${l.b}`);
    }
  } else if (type === "service") {
    const service = overlay.services.find((s) => s.name === id);
    keep.add(`s:${id}`);
    for (const n of service?.nodes ?? []) {
      keep.add(`n:${n.node}`);
      for (const other of service.linked) keep.add(`i:${other}@${n.node}`);
    }
    for (const c of graph.clients) if (c.attachments.some((a) => a.service === id)) keep.add(`c:${c.id}`);
    for (const s of overlay.sessions) if (s.service === id || s.bridge === id) ce(s);
  } else if (type === "client") {
    keep.add(`c:${id}`).add(`x:c:${id}`);
    const client = graph.clients.find((c) => c.id === id);
    for (const a of client?.attachments ?? []) keep.add(`n:${a.node}`).add(`i:${a.service}@${a.node}`);
    for (const s of overlay.sessions) if (s.client === id) keep.add(`i:${s.service}@${s.node}`);
  }
  return keep;
}

/**
 * `toggle` is the control that leaves this view, shown first among its own: which planes are drawn,
 * which services the overlay shows, how far apart the planes stand.
 */
export function Layers({ graph, status, selected, onSelect, toggle = null }) {
  const box = useRef(null);
  const edges = useMemo(() => withIds(graph.layers.underlay), [graph]);
  const overlay = graph.layers.overlay;
  const [camera, setCameraState] = useState(() => ({ ...CAMERA, ...remembered(CAMERA_KEY, {}) }));
  const setCamera = (next) => {
    setCameraState(next);
    remember(CAMERA_KEY, next);
  };
  const [hidden, setHidden] = useState(() => new Set(remembered("fcli-layers-hidden", [])));
  const toggleLayer = (id) => {
    const next = new Set(hidden);
    if (!next.delete(id)) next.add(id);
    if (next.size === LAYERS.length) return; // one plane at least
    setHidden(next);
    remember("fcli-layers-hidden", [...next]);
  };
  const [showClients, setShowClients] = useState(() => remembered("fcli-clients", false));
  const [scopePick, setScope] = useState(() => remembered("fcli-layer-scope", ""));
  const scopes = useMemo(
    () => [
      "all",
      ...overlay.services.filter((s) => s.type === "ip-vrf").map((s) => `tenant:${s.name}`),
      ...overlay.services.map((s) => `service:${s.name}`),
    ],
    [overlay],
  );
  // A selected service is shown whatever the scope was.
  const scope = selected?.type === "service" && !inScope(overlay.services, scopes.includes(scopePick) ? scopePick : "all").some((s) => s.name === selected.id)
    ? `service:${selected.id}`
    : scopes.includes(scopePick) ? scopePick : defaultScope(overlay.services);
  const shown = useMemo(() => inScope(overlay.services, scope), [overlay, scope]);
  const colour = useMemo(() => new Map(overlay.services.map((s, i) => [s.name, PALETTE[i % PALETTE.length]])), [overlay]);

  // The clients drawn: the CEs, what the selection is or touches, or every one when asked for.
  const drawnGraph = useMemo(() => {
    if (showClients) return graph;
    const keep = new Set(overlay.sessions.map((s) => s.client).filter(Boolean));
    if (selected?.type === "client") keep.add(selected.id);
    if (selected?.type === "service") {
      for (const c of graph.clients) if (c.attachments.some((a) => a.service === selected.id)) keep.add(c.id);
    }
    return { ...graph, clients: graph.clients.filter((c) => keep.has(c.id)) };
  }, [graph, overlay, showClients, selected]);

  const placed = useMemo(() => {
    const base = layout(drawnGraph);
    // CEs no client was found for: one per peer of a service, under the nodes that hold a session with it.
    const stubs = new Map();
    for (const s of overlay.sessions) {
      if (s.client || s.far) continue;
      const key = `${s.service}|${s.peer}`;
      if (!stubs.has(key)) stubs.set(key, { key, service: s.service, peer: s.peer, peer_as: s.peer_as, nodes: [] });
      stubs.get(key).nodes.push(s.node);
    }
    const ys = [...base.nodes.values(), ...base.clients.values()].map((p) => p.y);
    const row = Math.max(0, ...ys) + 110;
    const want = [...stubs.values()]
      .map((s) => {
        const xs = s.nodes.filter((n) => base.nodes.has(n)).map((n) => base.nodes.get(n).x);
        return { ...s, x: xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0 };
      })
      .sort((a, b) => a.x - b.x || natural(a.key, b.key));
    want.forEach((s, i) => {
      if (i) s.x = Math.max(s.x, want[i - 1].x + 104);
      s.y = row;
    });
    const points = [...base.nodes.values(), ...base.clients.values(), ...want];
    const bounds = points.length
      ? {
          x0: Math.min(...points.map((p) => p.x)) - MARGIN,
          x1: Math.max(...points.map((p) => p.x)) + MARGIN,
          y0: Math.min(...points.map((p) => p.y)) - MARGIN,
          y1: Math.max(...points.map((p) => p.y)) + MARGIN * 0.7,
        }
      : { x0: 0, x1: 400, y0: 0, y1: 300 };
    return { nodes: base.nodes, clients: base.clients, stubs: new Map(want.map((s) => [s.key, s])), bounds };
  }, [drawnGraph, overlay]);

  // Where each service stands on a node's footprint: ip-vrfs behind, the mac-vrfs routed into each in front of it.
  const slots = useMemo(() => {
    const routers = shown.filter((s) => s.type === "ip-vrf").sort((a, b) => natural(a.name, b.name));
    const order = new Map(routers.map((s, i) => [s.name, i]));
    const behind = (s) => Math.min(routers.length, ...s.linked.filter((l) => order.has(l)).map((l) => order.get(l)));
    const bridges = shown.filter((s) => s.type !== "ip-vrf").sort((a, b) => behind(a) - behind(b) || natural(a.name, b.name));
    const at = new Map();
    for (const [row, dy] of [[routers, -24], [bridges, 24]]) {
      const step = Math.min(30, (FOOT * 2 - 24) / Math.max(1, row.length));
      row.forEach((s, i) => at.set(s.name, { dx: (i - (row.length - 1) / 2) * step, dy }));
    }
    return at;
  }, [shown]);

  // ------------------------------------------------------------------ the camera
  const visible = LAYERS.filter((l) => !hidden.has(l.id));
  const level = new Map(visible.map((l, i) => [l.id, i * camera.gap]));
  const yaw = (camera.yaw * Math.PI) / 180;
  const pitch = (camera.pitch * Math.PI) / 180;
  const [cos, sin, tilt, lift] = [Math.cos(yaw), Math.sin(yaw), Math.sin(pitch), Math.cos(pitch)];
  const centre = { x: (placed.bounds.x0 + placed.bounds.x1) / 2, y: (placed.bounds.y0 + placed.bounds.y1) / 2 };
  /** A point of a plane on the screen: turned, tilted, lifted to its plane. No perspective: what is alike stays alike. */
  const P = (layer, x, y) => {
    const dx = x - centre.x;
    const dy = y - centre.y;
    return { x: dx * cos - dy * sin, y: (dx * sin + dy * cos) * tilt - level.get(layer) * lift };
  };
  const corners = (layer) => {
    const b = placed.bounds;
    return [P(layer, b.x0, b.y0), P(layer, b.x1, b.y0), P(layer, b.x1, b.y1), P(layer, b.x0, b.y1)];
  };
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setSize({ w: entry.contentRect.width, h: entry.contentRect.height }));
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  // Fitted to the window, whatever the turn, until panned or zoomed; Fit hands it back.
  const [held, setHeld] = useState(null);
  const all = visible.flatMap((l) => corners(l.id));
  const frame = {
    x0: Math.min(...all.map((p) => p.x)) - 175,
    x1: Math.max(...all.map((p) => p.x)) + 20,
    y0: Math.min(...all.map((p) => p.y)) - 20,
    y1: Math.max(...all.map((p) => p.y)) + 20,
  };
  const fitK = size.w > 0 ? Math.min(size.w / (frame.x1 - frame.x0), size.h / (frame.y1 - frame.y0), 1.5) : 1;
  const view = held ?? {
    k: fitK,
    x: size.w / 2 - ((frame.x0 + frame.x1) / 2) * fitK,
    y: size.h / 2 - ((frame.y0 + frame.y1) / 2) * fitK,
  };

  const drag = useRef(null);
  const swallow = useRef(false);
  const onWheel = (e) => {
    e.preventDefault();
    const rect = box.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const k = Math.min(4, Math.max(0.15, view.k * Math.exp(-e.deltaY * 0.0015)));
    setHeld({ k, x: mx - ((mx - view.x) * k) / view.k, y: my - ((my - view.y) * k) / view.k });
  };
  const onPointerDown = (e) => {
    const pan = e.shiftKey || e.button === 1 || e.button === 2;
    drag.current = { kind: pan ? "pan" : "orbit", x: e.clientX, y: e.clientY, view, camera, moved: false };
  };
  const onPointerMove = (e) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    if (!d.moved) box.current.setPointerCapture(e.pointerId);
    d.moved = true;
    if (d.kind === "pan") setHeld({ ...d.view, x: d.view.x + dx, y: d.view.y + dy });
    else {
      setCamera({
        ...d.camera,
        yaw: Math.max(-80, Math.min(80, d.camera.yaw + dx * 0.3)),
        pitch: Math.max(8, Math.min(88, d.camera.pitch + dy * 0.3)),
      });
    }
  };
  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (!d.moved) onSelect(null);
    swallow.current = d.moved;
  };
  const pick = (selection) => (e) => {
    e.stopPropagation();
    if (swallow.current) {
      swallow.current = false;
      return;
    }
    onSelect(selection);
  };
  const quiet = (e) => e.stopPropagation();

  // ------------------------------------------------------------------ what is drawn
  const near = useMemo(() => touched(selected, graph, edges, overlay), [selected, graph, edges, overlay]);
  const dim = (...keys) => (near && !keys.some((k) => near.has(k)) ? " dim" : "");
  const lit = (...keys) => (near && keys.some((k) => near.has(k)) ? " lit" : "");
  const is = (type, id) => (selected?.type === type && selected.id === id ? " selected" : "");
  const live = new Map((status?.nodes ?? []).map((n) => [n.name, n]));
  const at = (name) => placed.nodes.get(name);
  /** A line between two points of a plane, moved sideways by `off` so parallel ones do not hide each other. */
  const line = (layer, a, b, off = 0, inset = 0) => {
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const [ux, uy] = [(b.x - a.x) / len, (b.y - a.y) / len];
    const from = { x: a.x - uy * off + ux * inset, y: a.y + ux * off + uy * inset };
    const to = { x: b.x - uy * off - ux * inset, y: b.y + ux * off - uy * inset };
    return { p: P(layer, from.x, from.y), q: P(layer, to.x, to.y), len, ux, uy };
  };
  const d = ({ p, q }) => `M${p.x},${p.y} L${q.x},${q.y}`;
  const ellipse = (layer, p, r, cls) => {
    const c = P(layer, p.x, p.y);
    return html`<ellipse class=${cls} cx=${c.x} cy=${c.y} rx=${r} ry=${Math.max(3, r * tilt)} />`;
  };

  const plane = (l) => {
    const pts = corners(l.id);
    const left = pts.reduce((a, b) => (b.x < a.x ? b : a));
    return html`<g class=${`plane plane-${l.id}`}>
      <polygon points=${pts.map((p) => `${p.x},${p.y}`).join(" ")} />
      <text class="plane-title" x=${left.x - 10} y=${left.y - 2}>${l.title}</text>
      <text class="plane-note" x=${left.x - 10} y=${left.y + 12}>${l.note}</text>
    </g>`;
  };

  /** A node's place on a plane: its footprint, its name under it, and one line more. */
  const pad = (layer, n, r, sub) => {
    const p = at(n.name);
    if (!p) return null;
    const c = P(layer, p.x, p.y);
    const s = live.get(n.name);
    const down = n.unreachable || (s && s.status === "down");
    const health = layer === "physical" ? ` health-${down ? "down" : n.health}` : down ? " health-down" : "";
    return html`<g class=${`pad${health}${is("node", n.name)}${dim(`n:${n.name}`)}`} onClick=${pick({ type: "node", id: n.name })} onPointerDown=${quiet}>
      ${ellipse(layer, p, r, "foot")}
      <text class="name" x=${c.x} y=${c.y + r * tilt + 13}>${n.name}</text>
      ${sub && html`<text class="sub" x=${c.x} y=${c.y + r * tilt + 25}>${sub}</text>`}
      <title>${[n.name, n.tier, n.type, n.asn != null && `AS ${n.asn}`, n.ipv4, n.ipv6].filter(Boolean).join("\n")}</title>
    </g>`;
  };

  const physical = () => {
    const out = [];
    for (const c of graph.clients) {
      const p = placed.clients.get(c.id);
      if (!p) continue;
      for (const n of c.nodes) if (at(n)) out.push(html`<path class=${`attach${dim(`c:${c.id}`)}`} d=${d(line("physical", p, at(n)))} />`);
    }
    for (const l of graph.links) {
      const [a, b] = [at(l.a), at(l.b)];
      if (!a || !b) continue;
      const id = `${l.a}|${l.b}`;
      const show = near?.has(`l:${id}`) || is("node", l.a) || is("node", l.b);
      out.push(html`<g class=${`cable-group${is("link", id)}${dim(`l:${id}`)}${lit(`l:${id}`)}`} onClick=${pick({ type: "link", id })} onPointerDown=${quiet}>
        ${l.cables.map((c, i) => {
          const off = (i - (l.cables.length - 1) / 2) * 6;
          const whole = line("physical", a, b, off);
          const ports = line("physical", a, b, off, Math.min(PAD + 10, whole.len / 3));
          return html`<path class="hit" d=${d(whole)} />
            <path class=${`cable state-${c.state || "unknown"}`} d=${d(whole)} />
            <circle class="port" cx=${ports.p.x} cy=${ports.p.y} r="2.6" />
            <circle class="port" cx=${ports.q.x} cy=${ports.q.y} r="2.6" />
            ${show && html`<text class="port-name" x=${ports.p.x + 5} y=${ports.p.y - 4}>${short(c.a_port)}</text>
              <text class="port-name" x=${ports.q.x + 5} y=${ports.q.y - 4}>${short(c.b_port)}</text>`}
            <title>${`${l.a} ${c.a_port} ↔ ${l.b} ${c.b_port}: ${c.state || "state unknown"}`}</title>`;
        })}
      </g>`);
    }
    for (const n of graph.nodes) out.push(pad("physical", n, PAD, n.type || n.tier));
    for (const c of graph.clients) {
      const p = placed.clients.get(c.id);
      if (!p) continue;
      const q = P("physical", p.x, p.y);
      const label = c.label || c.name || c.id;
      out.push(html`<g class=${`host${is("client", c.id)}${dim(`c:${c.id}`)}`} onClick=${pick({ type: "client", id: c.id })} onPointerDown=${quiet}>
        <rect x=${q.x - 46} y=${q.y - 10} width="92" height="20" rx="10" />
        <text x=${q.x} y=${q.y + 4}>${label.length > 13 ? `${label.slice(0, 12)}…` : label}</text>
        <title>${label}</title>
      </g>`);
    }
    return out;
  };

  const underlay = () => {
    const out = [];
    const beside = new Map();
    const outside = new Map();
    for (const e of edges) {
      const a = at(e.a);
      if (!a) continue;
      const cls = `session ${e.protocol} state-${e.state}${e.evpn ? " evpn" : ""}${e.protocol === "bgp" && !e.underlay ? " evpn-only" : ""}`;
      const about = [
        `${e.a}${e.a_port ? ` ${e.a_port}` : ""} ↔ ${e.b ?? e.peer}${e.b_port ? ` ${e.b_port}` : ""}`,
        `${e.protocol === "bgp" ? e.kind : e.protocol}${e.level ? ` ${e.level}` : ""}: ${e.state}`,
        (e.families ?? []).join(", "),
      ].filter(Boolean).join("\n");
      const group = (body) => html`<g class=${`${cls}${is("session", e.id)}${dim(`e:${e.id}`)}${lit(`e:${e.id}`)}`}
                                      onClick=${pick({ type: "session", id: e.id })} onPointerDown=${quiet}>${body}<title>${about}</title></g>`;
      if (!e.b || !at(e.b)) {
        // A peer that is no node of ours: a stub out of the plane's back, its address at the end.
        const i = outside.get(e.a) ?? 0;
        outside.set(e.a, i + 1);
        const far = { x: a.x + 46 + i * 34, y: a.y - 74 };
        const seg = line("underlay", a, far);
        out.push(group(html`<path class="hit" d=${d(seg)} /><path class="wire" d=${d(seg)} />
          <rect class="outside" x=${seg.q.x - 4} y=${seg.q.y - 4} width="8" height="8" transform=${`rotate(45 ${seg.q.x} ${seg.q.y})`} />
          <text class="peer" x=${seg.q.x + 8} y=${seg.q.y + 3}>${e.peer || e.b}</text>`));
        continue;
      }
      const b = at(e.b);
      const pair = `${e.a}|${e.b}`;
      const i = beside.get(pair) ?? 0;
      beside.set(pair, i + 1);
      const off = (i % 2 ? 1 : -1) * Math.ceil(i / 2) * 7;
      if (e.protocol === "bgp" && !e.underlay) {
        // Between loopbacks, over whatever the underlay routes it on: an arc, clear of the sessions on the cables.
        const { p, q, len, ux, uy } = line("underlay", a, b, off);
        const bow = 34 + len * 0.14;
        const c = P("underlay", (a.x + b.x) / 2 - uy * bow, (a.y + b.y) / 2 + ux * bow);
        const arc = `M${p.x},${p.y} Q${c.x},${c.y} ${q.x},${q.y}`;
        out.push(group(html`<path class="hit" d=${arc} /><path class="wire" d=${arc} />`));
      } else {
        const seg = line("underlay", a, b, off);
        out.push(group(html`<path class="hit" d=${d(seg)} />${e.evpn && html`<path class="halo" d=${d(seg)} />`}<path class="wire" d=${d(seg)} />`));
      }
    }
    for (const n of graph.nodes) out.push(pad("underlay", n, PAD, n.asn != null ? `AS ${n.asn}` : n.ipv4));
    return out;
  };

  const overlayPlane = () => {
    const out = [];
    const where = (service, node) => {
      const [p, slot] = [at(node), slots.get(service)];
      return p && slot ? { x: p.x + slot.dx, y: p.y + slot.dy } : null;
    };
    const names = new Set(shown.map((s) => s.name));
    for (const n of graph.nodes) out.push(pad("overlay", n, FOOT, ""));
    // One service on several nodes is one service: its instances joined, in its colour.
    for (const s of shown) {
      const pts = s.nodes.map((n) => where(s.name, n.node)).filter(Boolean).sort((a, b) => a.x - b.x || a.y - b.y);
      if (pts.length < 2) continue;
      const path = pts.map((p, i) => `${i ? "L" : "M"}${P("overlay", p.x, p.y).x},${P("overlay", p.x, p.y).y}`).join(" ");
      out.push(html`<path class=${`span${dim(`s:${s.name}`)}${lit(`s:${s.name}`)}`} d=${path} stroke=${colour.get(s.name)} />`);
    }
    for (const irb of overlay.irbs) {
      const [a, b] = [where(irb.mac_vrf, irb.node), where(irb.ip_vrf, irb.node)];
      if (!a || !b) continue;
      const keys = [`s:${irb.mac_vrf}`, `s:${irb.ip_vrf}`, `i:${irb.mac_vrf}@${irb.node}`, `i:${irb.ip_vrf}@${irb.node}`];
      out.push(html`<path class=${`irb${irb.oper === "up" ? "" : " down"}${dim(...keys)}`} d=${d(line("overlay", a, b))}>
        <title>${`${irb.node} ${irb.interface}: ${irb.mac_vrf} → ${irb.ip_vrf}${irb.addresses.length ? `\n${irb.addresses.join(", ")}` : ""}${irb.oper === "up" ? "" : `\n${irb.oper || "down"}`}`}</title>
      </path>`);
    }
    const ces = new Map();
    for (const s of overlay.sessions) {
      if (!names.has(s.service)) continue;
      const from = where(s.service, s.node);
      if (!from) continue;
      const key = s.client ? `c:${s.client}` : `${s.service}|${s.peer}`;
      const to = s.far ? (where(s.service, s.far) ?? at(s.far)) : s.client ? placed.clients.get(s.client) : placed.stubs.get(key);
      if (!to) continue;
      const keys = [`s:${s.service}`, `i:${s.service}@${s.node}`, `x:${key}`];
      out.push(html`<path class=${`pe-ce state-${s.state}${dim(...keys)}`} d=${d(line("overlay", from, to))}>
        <title>${`${s.node} ${s.service} ↔ ${s.peer}${s.peer_as != null ? ` (AS ${s.peer_as})` : ""}: ${s.session_state}${s.via ? `\nover ${s.via}` : ""}`}</title>
      </path>`);
      if (!s.far && !ces.has(key)) {
        const client = s.client && graph.clients.find((c) => c.id === s.client);
        ces.set(key, { key, to, client, label: client ? client.label || client.name || client.id : s.peer, peer_as: s.peer_as });
      }
    }
    for (const s of shown) {
      for (const n of s.nodes) {
        const w = where(s.name, n.node);
        if (!w) continue;
        const c = P("overlay", w.x, w.y);
        const trouble = n.oper !== "up" ? " down" : n.down ? " degraded" : "";
        const labelled = is("service", s.name) && n === s.nodes[0];
        out.push(html`<g class=${`instance ${s.type}${trouble}${is("service", s.name)}${dim(`s:${s.name}`, `i:${s.name}@${n.node}`)}`}
                         onClick=${pick({ type: "service", id: s.name })} onPointerDown=${quiet}>
          ${s.type === "ip-vrf"
            ? html`<circle cx=${c.x} cy=${c.y} r="8.5" fill=${colour.get(s.name)} />
                <path class="mark" d=${`M${c.x - 4.5},${c.y} H${c.x + 4.5} M${c.x},${c.y - 4.5} V${c.y + 4.5}`} />`
            : html`<rect x=${c.x - 7.5} y=${c.y - 7.5} width="15" height="15" rx="3.5" fill=${colour.get(s.name)} />`}
          ${labelled && html`<text class="service-name" x=${c.x} y=${c.y - 13}>${s.name}</text>`}
          <title>${`${s.name} on ${n.node} (${s.type}${s.vnis.length ? `, VNI ${s.vnis.join(", ")}` : ""})\n${n.oper || "?"}${n.ports ? `, ${n.ports - n.down}/${n.ports} ports up` : ""}`}</title>
        </g>`);
      }
    }
    for (const ce of ces.values()) {
      const c = P("overlay", ce.to.x, ce.to.y);
      const click = ce.client ? { onClick: pick({ type: "client", id: ce.client.id }) } : {};
      out.push(html`<g class=${`ce${dim(`x:${ce.key}`)}`} ...${click} onPointerDown=${quiet}>
        <rect x=${c.x - 6} y=${c.y - 6} width="12" height="12" transform=${`rotate(45 ${c.x} ${c.y})`} />
        <text class="name" x=${c.x} y=${c.y + 20}>${ce.label.length > 16 ? `${ce.label.slice(0, 15)}…` : ce.label}</text>
        ${ce.peer_as != null && html`<text class="sub" x=${c.x} y=${c.y + 32}>AS ${ce.peer_as}</text>`}
        <title>CE ${ce.label}</title>
      </g>`);
    }
    return out;
  };

  /** What ties a plane to the one above: each node is the same node, each CE the same host. */
  const rise = (lower, upper, p, cls) => {
    const [from, to] = [P(lower, p.x, p.y), P(upper, p.x, p.y)];
    return html`<path class=${`riser${cls}`} d=${`M${from.x},${from.y} L${to.x},${to.y}`} />`;
  };
  const risers = (lower, upper) =>
    graph.nodes.filter((n) => at(n.name)).map((n) => rise(lower, upper, at(n.name), `${dim(`n:${n.name}`)}${lit(`n:${n.name}`)}`));
  const hostRisers = () =>
    [...placed.clients]
      .filter(([id]) => overlay.sessions.some((s) => s.client === id))
      .map(([id, p]) => rise("physical", "overlay", p, dim(`c:${id}`, `x:c:${id}`)));
  const content = { physical, underlay, overlay: overlayPlane };
  const shownTypes = new Set(shown.map((s) => s.type));

  return html`
    <div class="topology layers" ref=${box} onWheel=${onWheel} onPointerDown=${onPointerDown} onContextMenu=${(e) => e.preventDefault()}
         onPointerDownCapture=${() => (swallow.current = false)}
         onPointerMove=${onPointerMove} onPointerUp=${onPointerUp} onPointerCancel=${onPointerUp}>
      <svg width="100%" height="100%">
        <g transform=${`translate(${view.x},${view.y}) scale(${view.k})`}>
          ${visible.map((l, i) => html`<g class=${`layer layer-${l.id}`}>
            ${plane(l)}
            ${content[l.id]()}
            ${visible[i + 1] && risers(l.id, visible[i + 1].id)}
            ${l.id === "physical" && !hidden.has("overlay") && hostRisers()}
          </g>`)}
        </g>
      </svg>
      <div class="map-tools" onPointerDown=${quiet} onWheel=${quiet}>
        <${Search} graph=${graph} onPick=${(hit) => onSelect({ type: hit.type, id: hit.id })} />
        <button class=${showClients ? "on" : ""} aria-pressed=${showClients} title=${showClients ? "Only the CEs and what is selected" : "Show every client"}
                onClick=${() => {
                  setShowClients(!showClients);
                  remember("fcli-clients", !showClients);
                }}>Clients</button>
        <button onClick=${() => setCamera({ ...CAMERA })} title="Turn the drawing back to where it started">Reset</button>
        <button onClick=${() => setHeld(null)} title="Fit the layers to the window">Fit</button>
        <button onClick=${() => setHeld({ ...view, k: Math.min(4, view.k * 1.25) })} title="Zoom in">+</button>
        <button onClick=${() => setHeld({ ...view, k: Math.max(0.15, view.k / 1.25) })} title="Zoom out">−</button>
      </div>
      <div class="map-controls" onPointerDown=${quiet} onWheel=${quiet}>
        ${toggle}
        ${LAYERS.map((l) => html`<label title=${l.note}><input type="checkbox" checked=${!hidden.has(l.id)} onChange=${() => toggleLayer(l.id)} />${l.title}</label>`)}
        <label><span>overlay</span>
          <select value=${scope} aria-label="The services the overlay shows"
                  onChange=${(e) => {
                    setScope(e.target.value);
                    remember("fcli-layer-scope", e.target.value);
                    if (selected?.type === "service") onSelect(null);
                  }}>
            <option value="all">every service (${overlay.services.length})</option>
            <optgroup label="tenant">${scopes.filter((s) => s.startsWith("tenant:")).map((s) => html`<option value=${s}>${s.slice(7)}</option>`)}</optgroup>
            <optgroup label="service">${scopes.filter((s) => s.startsWith("service:")).map((s) => html`<option value=${s}>${s.slice(8)}</option>`)}</optgroup>
          </select>
        </label>
        <label title="How far apart the planes stand"><span>gap</span>
          <input type="range" min="80" max="420" step="10" value=${camera.gap} onInput=${(e) => setCamera({ ...camera, gap: Number(e.target.value) })} />
        </label>
      </div>
      <div class="legend layer-legend" onPointerDown=${quiet} onWheel=${quiet}>
        ${!hidden.has("underlay") && html`<div>
          <span><i class="ln session-up"></i>session up</span>
          <span><i class="ln session-down"></i>down</span>
          <span><i class="ln session-evpn"></i>carries EVPN</span>
          <span><i class="ln session-igp"></i>IS-IS, OSPF</span>
        </div>`}
        ${!hidden.has("overlay") && html`<div>
          ${shownTypes.has("ip-vrf") && html`<span><i class="glyph router"></i>ip-vrf</span>`}
          ${shownTypes.has("mac-vrf") && html`<span><i class="glyph bridge"></i>mac-vrf</span>`}
          <span><i class="ln irb"></i>irb</span>
          <span><i class="ln pe-ce"></i>PE-CE BGP</span>
        </div>
        <div class="services">${shown.map((s) => html`<a class=${is("service", s.name)} onClick=${() => onSelect({ type: "service", id: s.name })}>
          <i class=${`glyph ${s.type === "ip-vrf" ? "router" : "bridge"}`} style=${`background:${colour.get(s.name)}`}></i>${s.name}</a>`)}</div>`}
        <div class="muted">Drag to turn, Shift-drag to move, scroll to zoom.</div>
      </div>
    </div>
  `;
}

// --------------------------------------------------------------------------- details

const statePill = (state) => html`<span class=${`pill state-${state === "up" ? "up" : state === "down" ? "down" : "unknown"}`}>${state || "?"}</span>`;

function Sessions({ rows, node, onSelect }) {
  return html`<table class="grid">
    <thead><tr><th>peer</th><th>protocol</th><th>port</th><th>state</th></tr></thead>
    <tbody>${rows.map((e) => {
      const near = e.a === node;
      return html`<tr><td><a onClick=${() => onSelect({ type: "session", id: e.id })}>${(near ? e.b : e.a) ?? e.peer}</a></td>
        <td>${e.protocol === "bgp" ? e.kind : e.protocol}${e.evpn ? " +evpn" : ""}${e.level ? ` ${e.level}` : ""}</td>
        <td class="mono">${(near ? e.a_port : e.b_port) || "loopback"}</td><td>${statePill(e.state)}</td></tr>`;
    })}</tbody>
  </table>`;
}

function PeCe({ rows, graph, onSelect }) {
  return html`<table class="grid">
    <thead><tr><th>node</th><th>peer</th><th>AS</th><th>over</th><th>CE</th><th>state</th></tr></thead>
    <tbody>${rows.map((s) => {
      const client = s.client && graph.clients.find((c) => c.id === s.client);
      return html`<tr><td><a onClick=${() => onSelect({ type: "node", id: s.node })}>${s.node}</a></td>
        <td class="mono">${s.peer}</td><td class="mono">${s.peer_as ?? ""}</td><td class="mono">${s.via}</td>
        <td>${client ? html`<a onClick=${() => onSelect({ type: "client", id: client.id })}>${client.label || client.name || client.id}</a>` : s.far ?? ""}</td>
        <td><span class=${`pill state-${s.state}`} title=${s.session_state}>${s.state}</span></td></tr>`;
    })}</tbody>
  </table>`;
}

/** What a click on the layers shows: a service or a session here; a node, a cable or a client as the topology has it, and what the layers add. */
export function LayerDetails({ selection, graph, status, checks, at, onSelect }) {
  const edges = useMemo(() => withIds(graph.layers.underlay), [graph]);
  const overlay = graph.layers.overlay;
  const existing = html`<${Details} selection=${selection} graph=${graph} status=${status} checks=${checks} at=${at} onSelect=${onSelect} />`;
  if (!selection) {
    const down = edges.filter((e) => e.state === "down");
    const ce = overlay.sessions.filter((s) => s.state !== "up");
    return html`${existing}<div class="details">
      <h3>Layers</h3>
      <dl class="facts">
        <dt>underlay</dt><dd>${edges.length} sessions${down.length ? html`, <span class="error-text">${down.length} down</span>` : ""}</dd>
        <dt>overlay</dt><dd>${overlay.services.filter((s) => s.type === "ip-vrf").length} ip-vrfs, ${overlay.services.filter((s) => s.type === "mac-vrf").length} mac-vrfs, ${overlay.irbs.length} irbs</dd>
        <dt>PE-CE</dt><dd>${overlay.sessions.length} sessions${ce.length ? html`, <span class="error-text">${ce.length} down</span>` : ""}</dd>
      </dl>
      ${down.length > 0 && html`<h3>Sessions down</h3><${Sessions} rows=${down} node=${down[0].a} onSelect=${onSelect} />`}
    </div>`;
  }
  if (selection.type === "session") {
    const e = edges.find((x) => x.id === selection.id);
    if (!e) return html`<div class="details"><p class="muted">That session is not in this view.</p></div>`;
    const node = (name) => html`<a onClick=${() => onSelect({ type: "node", id: name })}>${name}</a>`;
    return html`<div class="details">
      <h2>${node(e.a)} ↔ ${e.b ? node(e.b) : e.peer} ${statePill(e.state)}</h2>
      <dl class="facts">
        <dt>protocol</dt><dd>${e.protocol === "bgp" ? `BGP (${e.kind})` : e.protocol.toUpperCase()}${e.level ? ` ${e.level}` : ""}</dd>
        <dt>instance</dt><dd>${e.ni}</dd>
        ${e.protocol === "bgp" && html`<dt>families</dt><dd>${e.families.join(", ") || "none enabled"}</dd>`}
        ${e.bfd && html`<dt>BFD</dt><dd>${e.bfd === "on" ? "enabled" : e.bfd}</dd>`}
        ${!e.b && html`<dt>peer</dt><dd class="mono">${e.peer} <span class="muted">no node of this fabric</span></dd>`}
      </dl>
      <table class="grid">
        <thead><tr><th>end</th><th>port</th>${e.protocol === "bgp" && html`<th>AS</th>`}<th>says</th></tr></thead>
        <tbody>
          <tr><td>${e.a}</td><td class="mono">${e.a_port || "loopback"}</td>${e.protocol === "bgp" && html`<td class="mono">${e.a_as ?? ""}</td>`}<td>${e.a_state || html`<span class="muted">nothing</span>`}</td></tr>
          ${e.b && html`<tr><td>${e.b}</td><td class="mono">${e.b_port || "loopback"}</td>${e.protocol === "bgp" && html`<td class="mono">${e.b_as ?? ""}</td>`}<td>${e.b_state || html`<span class="muted">nothing</span>`}</td></tr>`}
        </tbody>
      </table>
      <p class="muted small">A session is up when every end that reports it has it up.${e.protocol === "bgp" && !e.underlay ? " It carries EVPN alone: the overlay's control plane, between loopbacks." : ""}</p>
      ${e.protocol === "bgp" && html`<p><a href=${`#/report/bgp-peers?f.NI=${encodeURIComponent(`=${e.ni}`)}`}>All BGP peers</a></p>`}
    </div>`;
  }
  if (selection.type === "service") {
    const s = overlay.services.find((x) => x.name === selection.id);
    if (!s) return html`<div class="details"><p class="muted">${selection.id} is not in this view.</p></div>`;
    const irbs = overlay.irbs.filter((i) => i.mac_vrf === s.name || i.ip_vrf === s.name);
    const sessions = overlay.sessions.filter((x) => x.service === s.name || x.bridge === s.name);
    return html`<div class="details">
      <h2>${s.name} <span class="pill">${s.type}</span></h2>
      <dl class="facts">
        ${s.vnis.length > 0 && html`<dt>VNI</dt><dd class="mono">${s.vnis.join(", ")}</dd>`}
        ${s.evis.length > 0 && html`<dt>EVI</dt><dd class="mono">${s.evis.join(", ")}</dd>`}
        ${s.subnets.length > 0 && html`<dt>subnets</dt><dd class="mono">${s.subnets.join(", ")}</dd>`}
        ${s.linked.length > 0 &&
        html`<dt>${s.type === "ip-vrf" ? "mac-vrfs" : "routed by"}</dt>
          <dd>${s.linked.map((l) => html`<a class="chip" onClick=${() => onSelect({ type: "service", id: l })}>${l}</a>`)}</dd>`}
      </dl>
      <table class="grid">
        <thead><tr><th>node</th><th>oper</th><th>ports up</th></tr></thead>
        <tbody>${s.nodes.map(
          (n) => html`<tr><td><a onClick=${() => onSelect({ type: "node", id: n.node })}>${n.node}</a>${n.gateway ? html` <span class="pill">gateway</span>` : ""}</td>
            <td>${statePill(n.oper === "up" ? "up" : n.oper ? "down" : "")}</td><td>${n.ports ? `${n.ports - n.down}/${n.ports}` : "—"}</td></tr>`,
        )}</tbody>
      </table>
      ${irbs.length > 0 &&
      html`<h3>IRBs</h3>
        <table class="grid">
          <thead><tr><th>node</th><th>irb</th><th>${s.type === "ip-vrf" ? "mac-vrf" : "ip-vrf"}</th><th>addresses</th><th>oper</th></tr></thead>
          <tbody>${irbs.map(
            (i) => html`<tr><td>${i.node}</td><td class="mono">${i.interface}</td>
              <td><a onClick=${() => onSelect({ type: "service", id: s.type === "ip-vrf" ? i.mac_vrf : i.ip_vrf })}>${s.type === "ip-vrf" ? i.mac_vrf : i.ip_vrf}</a></td>
              <td class="mono">${i.addresses.join(" ")}${i.anycast_gw ? html` <span class="pill">anycast</span>` : ""}</td>
              <td>${statePill(i.oper === "up" ? "up" : "down")}</td></tr>`,
          )}</tbody>
        </table>`}
      ${sessions.length > 0 && html`<h3>PE-CE sessions</h3><${PeCe} rows=${sessions} graph=${graph} onSelect=${onSelect} />`}
      <p><a href=${`#/lens/service?name=${encodeURIComponent(s.name)}`}>The service on every node</a></p>
    </div>`;
  }
  if (selection.type === "node") {
    const mine = edges.filter((e) => e.a === selection.id || e.b === selection.id);
    const services = overlay.services.filter((s) => s.nodes.some((n) => n.node === selection.id));
    const sessions = overlay.sessions.filter((s) => s.node === selection.id);
    return html`${existing}<div class="details">
      <h3>Routing sessions</h3>
      ${mine.length ? html`<${Sessions} rows=${mine} node=${selection.id} onSelect=${onSelect} />` : html`<p class="muted">None in the default instance.</p>`}
      ${services.length > 0 &&
      html`<h3>Services</h3>
        <p>${services.map((s) => html`<a class="chip" onClick=${() => onSelect({ type: "service", id: s.name })}>${s.name}</a>`)}</p>`}
      ${sessions.length > 0 && html`<h3>PE-CE sessions</h3><${PeCe} rows=${sessions} graph=${graph} onSelect=${onSelect} />`}
    </div>`;
  }
  return existing;
}
