"use strict";
/* Mikrobox lab workspace. Plain JavaScript, no build step. */

const $ = (q) => document.querySelector(q);
const SVGNS = "http://www.w3.org/2000/svg";

function make(ns, tag, attrs, kids) {
  const node = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (!ns && (k === "value" || k === "disabled" || k === "hidden" || k === "checked")) node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(2)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const h = (tag, attrs, ...kids) => make(null, tag, attrs, kids);
const s = (tag, attrs, ...kids) => make(SVGNS, tag, attrs, kids);

const ICONS = {
  select: "M5 3l14 7-6 2-2 6z",
  link: "M9 15l6-6 M10 6l1-1a4 4 0 0 1 6 6l-1 1 M14 18l-1 1a4 4 0 0 1-6-6l1-1",
  chr: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M7 12h10 M12 7v10 M7 12l2-2 M7 12l2 2 M17 12l-2-2 M17 12l-2 2 M12 7l-2 2 M12 7l2 2 M12 17l-2-2 M12 17l2-2",
  client: "M4 5h16v11H4z M9 20h6 M12 16v4",
  rect: "M4 6h16v12H4z",
  ellipse: "M3 12a9 6.5 0 1 0 18 0a9 6.5 0 1 0-18 0",
  text: "M5 6h14 M12 6v13 M9 19h6",
  note: "M5 4h14v11l-5 5H5z M14 20v-5h5",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M4.9 4.9l2.1 2.1 M17 17l2.1 2.1 M4.9 19.1l2.1-2.1 M17 7l2.1-2.1",
  close: "M6 6l12 12 M18 6L6 18",
};
const CLOUD = "M-34 26a20 20 0 0 1-5-39a27 27 0 0 1 51-9a23 23 0 0 1 27 48z";
const icon = (name) => s("svg", { class: "icon", viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: ICONS[name] }));

const TOOLS = [
  { id: "select", label: "Select", tip: "Select, move and pan" },
  { id: "link", label: "Link", tip: "Connect two ports" },
  null,
  { id: "chr", label: "Router", tip: "Add a MikroTik CHR router", add: true },
  { id: "client", label: "Client", tip: "Add a Linux client", add: true },
  null,
  { id: "rect", label: "Area", tip: "Draw a rectangular area", draw: true },
  { id: "ellipse", label: "Ellipse", tip: "Draw an elliptical area", draw: true },
  { id: "text", label: "Text", tip: "Add a text label", draw: true },
  { id: "note", label: "Note", tip: "Add a sticky note", draw: true },
];
const DRAW = { rect: [260, 180], ellipse: [260, 180], text: [180, 36], note: [200, 120] };
const KIND_NAME = { rect: "Area", ellipse: "Ellipse", text: "Text", note: "Note" };
const COLORS = ["gray", "blue", "green", "amber", "red", "purple"];

const state = {
  labs: [], lab: null, sys: null,
  sel: null,        // {type: "node" | "link" | "mgmt" | "anno", id}
  tool: "select",
  linkFrom: null,
  activeConsole: null,
};
const view = { x: 90, y: 20, k: 1 };
const consoles = new Map(); // node id -> {term, fit, ws, pane, name}
let drag = null;

// -------------------------------------------------------- preferences

const PREF_DEFAULTS = { theme: "system", grid: true, snap: true, labels: true, defPorts: 8, defCpus: 1 };
function readStore(key, fallback) {
  try { const v = localStorage.getItem("mikrobox." + key); return v === null ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
function writeStore(key, value) {
  try { localStorage.setItem("mikrobox." + key, JSON.stringify(value)); } catch (e) { /* ignore */ }
}
const prefs = { ...PREF_DEFAULTS, ...readStore("prefs", {}) };

function applyPrefs() {
  let theme = prefs.theme;
  if (theme === "system") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
  $("#stage").classList.toggle("grid", !!prefs.grid);
}

// ---------------------------------------------------------------- API

async function apiRaw(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    let msg = res.statusText || `Request failed (${res.status})`;
    if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail)) {
      msg = data.detail.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ");
    }
    throw new Error(msg);
  }
  return res;
}

async function api(method, path, body) {
  const res = await apiRaw(method, path, body);
  return res.status === 204 ? null : res.json();
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 2500);
}

async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function refresh(force) {
  const [labs, sys] = await Promise.all([api("GET", "/labs"), api("GET", "/system")]);
  state.labs = labs;
  state.sys = sys;
  const before = state.lab ? state.lab.id : null;
  const wanted = before ?? readStore("lab", null);
  state.lab = labs.find((l) => l.id === wanted) || labs[0] || null;
  if (state.lab && state.lab.id !== before) loadView();
  if (state.sel && state.lab) {
    const { type, id } = state.sel;
    if (type === "node" && !nodeById(id)) state.sel = null;
    if (type === "link" && !state.lab.links.some((l) => l.id === id)) state.sel = null;
    if (type === "anno" && !annoById(id)) state.sel = null;
  } else if (!state.lab) state.sel = null;
  render(force);
}

// ------------------------------------------------------------ helpers

const nodeById = (id) => state.lab.nodes.find((n) => n.id === id);
const annoById = (id) => state.lab.annotations.find((a) => a.id === id);
const portName = (node, port) => (node.kind === "chr" ? `ether${port}` : `eth${port - 1}`);
const labPath = () => `/labs/${state.lab.id}`;
const snap = (v) => (prefs.snap ? Math.round(v / 20) * 20 : Math.round(v));
const sameRef = (a, b) => !!a && !!b && a.type === b.type && a.id === b.id;

function freePorts(node) {
  const used = new Set();
  for (const l of state.lab.links) {
    for (const e of [l.a, l.b]) if (e && e.node === node.id) used.add(e.port);
  }
  const out = [];
  for (let p = 1; p <= node.ports; p++) if (!used.has(p)) out.push(p);
  return out;
}

function peerText(node, port) {
  for (const l of state.lab.links) {
    const mine = [l.a, l.b].find((e) => e && e.node === node.id && e.port === port);
    if (!mine) continue;
    if (!l.b) return "Cloud";
    const other = mine === l.a ? l.b : l.a;
    const peer = nodeById(other.node);
    return peer ? `${peer.name} ${portName(peer, other.port)}` : "?";
  }
  return "Not connected";
}

function cloudLabel() {
  const c = state.sys && state.sys.cloud;
  if (!c) return "";
  return c.mode === "bridged" ? (c.network || "your network") : `${c.network} (NAT)`;
}

function selectedRouter() {
  const n = state.sel && state.sel.type === "node" ? nodeById(state.sel.id) : null;
  if (!n || n.kind !== "chr") { toast("Select a router on the canvas first.", true); return null; }
  return n;
}

function select(ref) {
  state.sel = ref;
  render(true);
}

function setTool(tool) {
  state.tool = tool;
  state.linkFrom = null;
  render(true);
}

// --------------------------------------------------------------- view

function loadView() {
  const v = state.lab ? readStore("view." + state.lab.id, null) : null;
  Object.assign(view, v && v.k ? v : { x: 90, y: 20, k: 1 });
}
function saveView() {
  if (state.lab) writeStore("view." + state.lab.id, view);
}
function zoomAt(px, py, k) {
  k = Math.min(2.5, Math.max(0.25, k));
  view.x = px - ((px - view.x) / view.k) * k;
  view.y = py - ((py - view.y) / view.k) * k;
  view.k = k;
  saveView();
  renderCanvas();
}
function zoomBy(factor) {
  const r = $("#canvas").getBoundingClientRect();
  zoomAt(r.width / 2, r.height / 2, view.k * factor);
}
function fitView() {
  const lab = state.lab;
  if (!lab) return;
  const xs = [lab.mgmt_x - 60, lab.mgmt_x + 60], ys = [lab.mgmt_y - 40, lab.mgmt_y + 55];
  for (const n of lab.nodes) { xs.push(n.x - 50, n.x + 50); ys.push(n.y - 40, n.y + 70); }
  for (const a of lab.annotations) { xs.push(a.x, a.x + a.w); ys.push(a.y, a.y + a.h); }
  const r = $("#canvas").getBoundingClientRect();
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const padL = 110, pad = 40, padR = state.sel ? 350 : 40;
  const k = Math.min(1.5, Math.max(0.25,
    Math.min((r.width - padL - padR) / (x1 - x0 || 1), (r.height - 2 * pad) / (y1 - y0 || 1))));
  view.k = k;
  view.x = padL + (r.width - padL - padR - (x1 - x0) * k) / 2 - x0 * k;
  view.y = pad + (r.height - 2 * pad - (y1 - y0) * k) / 2 - y0 * k;
  saveView();
  renderCanvas();
}
function point(e) {
  const r = $("#canvas").getBoundingClientRect();
  return { x: (e.clientX - r.left - view.x) / view.k, y: (e.clientY - r.top - view.y) / view.k };
}

// ------------------------------------------------------------- render

function render(force) {
  renderTop();
  renderCanvas();
  renderInspector(force);
}

function renderTop() {
  const sel = $("#lab-select");
  sel.replaceChildren(...state.labs.map((l) => h("option", { value: l.id }, l.name)));
  if (state.lab) sel.value = String(state.lab.id);
  sel.hidden = !state.labs.length;
  for (const b of document.querySelectorAll("[data-needs-lab], #lab-start, #lab-stop")) b.disabled = !state.lab;

  const sys = state.sys;
  $("#sys").textContent = sys
    ? `${sys.running_devices} / ${sys.max_devices} running` +
      (sys.mem_available_mb !== null ? ` · ${(sys.mem_available_mb / 1024).toFixed(1)} GB free` : "")
    : "";
  const warn = [];
  if (sys && sys.dry_run) warn.push("Dry-run mode: nothing is actually started.");
  if (sys && !sys.dry_run && !sys.kvm) warn.push("KVM is not available on this VM, so routers will be very slow. On Proxmox, set the VM's CPU type to host.");
  if (sys && !sys.dry_run && !sys.chr_image) warn.push("The CHR image is missing. Run scripts/install.sh on the VM.");
  $("#banner").hidden = !warn.length;
  $("#banner").textContent = warn.join(" ");

  $("#tools").replaceChildren(...TOOLS.map((t) => {
    if (!t) return h("div", { class: "sep" });
    const on = !t.add && state.tool === t.id;
    return h("button", {
      type: "button", class: on ? "on" : "", title: t.tip, "aria-pressed": t.add ? null : String(on),
      disabled: !state.lab && t.id !== "select",
      onclick: () => (t.add ? addNodeDialog(t.id) : setTool(t.id)),
    }, icon(t.id), t.label);
  }));

  const draw = !!DRAW[state.tool];
  const canvas = $("#canvas");
  canvas.classList.toggle("tool-link", state.tool === "link");
  canvas.classList.toggle("tool-draw", draw);
  $("#hint").textContent =
    state.tool === "link"
      ? (state.linkFrom ? "Now click the device to connect it to." : "Click the first device, then the second. Link a port to the Cloud to give it internet and reach it from your PC.")
      : draw ? `Drag on the canvas to place the ${KIND_NAME[state.tool].toLowerCase()}, or just click.` : "";
  $("#zoom-reset").textContent = Math.round(view.k * 100) + "%";
}

function annoElement(a, selected) {
  const g = s("g", {
    class: `anno ${a.kind} c-${a.color}`, tabindex: 0,
    onpointerdown: (e) => annoDown(e, a),
    onkeydown: (e) => { if (e.key === "Enter") select({ type: "anno", id: a.id }); },
  }, s("title", {}, `${KIND_NAME[a.kind]}${a.text ? ": " + a.text : ""}`));
  if (a.kind === "ellipse") {
    g.append(s("ellipse", { class: "shape", cx: a.x + a.w / 2, cy: a.y + a.h / 2, rx: a.w / 2, ry: a.h / 2 }));
  } else {
    g.append(s("rect", { class: "shape", x: a.x, y: a.y, width: a.w, height: a.h, rx: a.kind === "note" ? 8 : 12 }));
  }
  const area = a.kind === "rect" || a.kind === "ellipse";
  if (a.text || !area) {
    const fo = area
      ? s("foreignObject", { x: a.kind === "ellipse" ? a.x + a.w * 0.15 : a.x, y: a.kind === "ellipse" ? a.y + a.h * 0.12 : a.y, width: a.kind === "ellipse" ? a.w * 0.7 : a.w, height: 32 })
      : s("foreignObject", { x: a.x, y: a.y, width: a.w, height: a.h });
    fo.append(h("div", { class: area ? "label" : "body", style: a.kind === "ellipse" ? "text-align:center" : null },
      a.text || (a.kind === "text" ? "Text" : "Note")));
    g.append(fo);
  }
  if (selected) {
    g.append(
      s("rect", { class: "sel-box", x: a.x - 4, y: a.y - 4, width: a.w + 8, height: a.h + 8, rx: 6 }),
      s("rect", { class: "handle", x: a.x + a.w - 3, y: a.y + a.h - 3, width: 12, height: 12, rx: 3,
        onpointerdown: (e) => { e.preventDefault(); e.stopPropagation(); drag = { kind: "resize", id: a.id, moved: false }; } }));
  }
  return g;
}

function renderCanvas() {
  const world = $("#world");
  world.replaceChildren();
  world.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
  const stage = $("#stage");
  stage.style.backgroundSize = `${24 * view.k}px ${24 * view.k}px`;
  stage.style.backgroundPosition = `${view.x}px ${view.y}px`;
  $("#zoom-reset").textContent = Math.round(view.k * 100) + "%";

  const lab = state.lab;
  const empty = $("#empty");
  empty.hidden = !!(lab && (lab.nodes.length || lab.annotations.length));
  empty.textContent = lab
    ? "Add a router or a client from the toolbar on the left to start building."
    : "Create a lab from the Lab menu to get started.";
  if (!lab) return;

  const isSel = (type, id) => sameRef(state.sel, { type, id });

  // Areas at the back, then links, then text and notes, then devices.
  for (const a of lab.annotations) {
    if (a.kind === "rect" || a.kind === "ellipse") world.append(annoElement(a, isSel("anno", a.id)));
  }

  const groups = new Map();
  for (const l of lab.links) {
    const key = l.b ? [l.a.node, l.b.node].sort((x, y) => x - y).join("-") : `m-${l.a.node}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(l);
  }
  for (const group of groups.values()) {
    group.forEach((l, i) => {
      const na = nodeById(l.a.node), nb = l.b ? nodeById(l.b.node) : null;
      if (!na || (l.b && !nb)) return;
      let A = { x: na.x, y: na.y };
      let B = nb ? { x: nb.x, y: nb.y } : { x: lab.mgmt_x, y: lab.mgmt_y };
      // Parallel links are fanned out; keep one direction so they do not cross.
      const flip = nb && l.a.node > l.b.node;
      const dx = flip ? A.x - B.x : B.x - A.x, dy = flip ? A.y - B.y : B.y - A.y;
      const len = Math.hypot(dx, dy) || 1;
      const off = (i - (group.length - 1) / 2) * 14;
      const ox = (-dy / len) * off, oy = (dx / len) * off;
      A = { x: A.x + ox, y: A.y + oy };
      B = { x: B.x + ox, y: B.y + oy };
      const ux = (B.x - A.x) / len, uy = (B.y - A.y) / len;
      // Labels sit clear of the device tile, and further out below it where the name is.
      const da = Math.min(60 + 14 * Math.max(0, uy), len * 0.45);
      const db = Math.min(60 + 14 * Math.max(0, -uy), len * 0.45);
      const up = na.running && (nb ? nb.running : true);
      const down = (e) => {
        if (state.tool !== "select") return; // drawing or linking passes through
        e.stopPropagation();
        select({ type: "link", id: l.id });
      };
      world.append(
        s("g", { class: "link" + (isSel("link", l.id) ? " sel" : "") + (up ? " up" : "") },
          s("line", { class: "vis", x1: A.x, y1: A.y, x2: B.x, y2: B.y }),
          prefs.labels && s("text", { x: A.x + ux * da, y: A.y + uy * da + 4 }, portName(na, l.a.port)),
          prefs.labels && nb && s("text", { x: B.x - ux * db, y: B.y - uy * db + 4 }, portName(nb, l.b.port)),
          s("line", { class: "hit", x1: A.x, y1: A.y, x2: B.x, y2: B.y, onpointerdown: down }),
        ),
      );
    });
  }

  for (const a of lab.annotations) {
    if (a.kind === "text" || a.kind === "note") world.append(annoElement(a, isSel("anno", a.id)));
  }

  const cls = (ref, extra) => "node" + (extra || "") + (sameRef(state.sel, ref) ? " sel" : "") + (sameRef(state.linkFrom, ref) ? " from" : "");
  const handlers = (ref) => ({
    tabindex: 0,
    onpointerdown: (e) => nodeDown(e, ref),
    onkeydown: (e) => { if (e.key === "Enter") (state.tool === "link" ? linkClick(ref) : select(ref)); },
  });

  const mref = { type: "mgmt", id: 0 };
  world.append(
    s("g", { class: cls(mref), transform: `translate(${lab.mgmt_x},${lab.mgmt_y})`, ...handlers(mref) },
      s("title", {}, "Cloud: your network and the internet"),
      s("path", { class: "box cloud", d: CLOUD }),
      s("text", { y: 9 }, "Cloud"),
      s("text", { class: "sub", y: 46 }, cloudLabel()),
    ),
  );
  for (const n of lab.nodes) {
    const ref = { type: "node", id: n.id };
    world.append(
      s("g", { class: cls(ref, n.running ? " running" : ""), transform: `translate(${n.x},${n.y})`, ...handlers(ref) },
        s("title", {}, `${n.name} (${n.running ? "running" : "stopped"})`),
        s("rect", { class: "box", x: -30, y: -30, width: 60, height: 60, rx: 14 }),
        s("path", { class: "glyph", d: ICONS[n.kind], transform: "translate(-15.6,-15.6) scale(1.3)" }),
        s("circle", { class: "dot", cx: 26, cy: -26, r: 6.5 }),
        s("text", { y: 48 }, n.name),
        s("text", { class: "sub", y: 62 }, n.mgmt_ip || (n.kind === "client" && n.address) || ""),
      ),
    );
  }

  if (drag && drag.kind === "draw" && drag.moved) {
    const b = drawBox();
    world.append(drag.tool === "ellipse"
      ? s("ellipse", { class: "draft", cx: b.x + b.w / 2, cy: b.y + b.h / 2, rx: b.w / 2, ry: b.h / 2 })
      : s("rect", { class: "draft", x: b.x, y: b.y, width: b.w, height: b.h, rx: 8 }));
  }
}

// ------------------------------------------------------- canvas input

function drawBox() {
  return {
    x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1),
    w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0),
  };
}

function nodeDown(e, ref) {
  if (DRAW[state.tool]) return; // let the canvas start drawing
  e.preventDefault();
  e.stopPropagation();
  if (state.tool === "link") { linkClick(ref); return; }
  const p = point(e);
  const at = ref.type === "mgmt" ? { x: state.lab.mgmt_x, y: state.lab.mgmt_y } : nodeById(ref.id);
  drag = { kind: ref.type, id: ref.id, dx: p.x - at.x, dy: p.y - at.y, moved: false };
  select(ref);
}

function annoDown(e, a) {
  if (state.tool !== "select") return; // drawing or linking passes through
  e.preventDefault();
  e.stopPropagation();
  const p = point(e);
  drag = { kind: "anno", id: a.id, dx: p.x - a.x, dy: p.y - a.y, moved: false };
  select({ type: "anno", id: a.id });
}

$("#canvas").addEventListener("pointerdown", (e) => {
  if (!state.lab) return;
  if (DRAW[state.tool]) {
    const p = point(e);
    drag = { kind: "draw", tool: state.tool, x0: p.x, y0: p.y, x1: p.x, y1: p.y, moved: false };
    return;
  }
  if (state.tool === "link") { state.linkFrom = null; render(true); return; }
  if (state.sel) select(null);
  drag = { kind: "pan", sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  $("#canvas").classList.add("panning");
});

window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (drag.kind === "pan") {
    view.x = drag.vx + e.clientX - drag.sx;
    view.y = drag.vy + e.clientY - drag.sy;
  } else {
    const p = point(e);
    if (drag.kind === "draw") {
      drag.x1 = p.x; drag.y1 = p.y;
      if (Math.hypot(drag.x1 - drag.x0, drag.y1 - drag.y0) < 6) return;
    } else if (drag.kind === "mgmt") {
      state.lab.mgmt_x = snap(p.x - drag.dx); state.lab.mgmt_y = snap(p.y - drag.dy);
    } else if (drag.kind === "node") {
      const n = nodeById(drag.id); n.x = snap(p.x - drag.dx); n.y = snap(p.y - drag.dy);
    } else if (drag.kind === "anno") {
      const a = annoById(drag.id); a.x = snap(p.x - drag.dx); a.y = snap(p.y - drag.dy);
    } else if (drag.kind === "resize") {
      const a = annoById(drag.id);
      a.w = Math.max(40, snap(p.x - a.x)); a.h = Math.max(24, snap(p.y - a.y));
    }
  }
  drag.moved = true;
  renderCanvas();
});

window.addEventListener("pointerup", () => {
  if (!drag) return;
  const d = drag;
  drag = null;
  $("#canvas").classList.remove("panning");
  const fail = (err) => toast(err.message, true);
  if (d.kind === "pan") { if (d.moved) saveView(); return; }
  if (d.kind === "draw") { finishDraw(d); return; }
  if (!d.moved) return;
  if (d.kind === "mgmt") api("PATCH", labPath(), { mgmt_x: state.lab.mgmt_x, mgmt_y: state.lab.mgmt_y }).catch(fail);
  else if (d.kind === "node") { const n = nodeById(d.id); api("PATCH", `${labPath()}/nodes/${n.id}`, { x: n.x, y: n.y }).catch(fail); }
  else { const a = annoById(d.id); api("PATCH", `${labPath()}/annotations/${a.id}`, { x: a.x, y: a.y, w: a.w, h: a.h }).catch(fail); }
});

function finishDraw(d) {
  drag = d;
  let box = drawBox();
  drag = null;
  const [dw, dh] = DRAW[d.tool];
  if (!d.moved || box.w < 12 || box.h < 12) box = { x: d.x0 - dw / 2, y: d.y0 - dh / 2, w: dw, h: dh };
  const body = {
    kind: d.tool, x: snap(box.x), y: snap(box.y), w: Math.max(40, snap(box.w)), h: Math.max(24, snap(box.h)),
    color: d.tool === "note" ? "amber" : d.tool === "text" ? "gray" : "blue", text: "",
  };
  state.tool = "select";
  act(async () => {
    const a = await api("POST", `${labPath()}/annotations`, body);
    state.sel = { type: "anno", id: a.id };
  }).then(() => { const t = $("#i-text"); if (t) t.focus(); });
}

$("#canvas").addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = $("#canvas").getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, view.k * Math.exp(-e.deltaY * 0.0015));
}, { passive: false });

function linkClick(ref) {
  if (!state.linkFrom) { state.linkFrom = ref; render(true); return; }
  const from = state.linkFrom;
  if (sameRef(from, ref)) { state.linkFrom = null; render(true); return; }
  linkDialog(from, ref);
}

function linkDialog(r1, r2) {
  // Put the real device first; the other side may be the management network.
  if (r1.type === "mgmt") [r1, r2] = [r2, r1];
  const a = nodeById(r1.id), b = r2.type === "node" ? nodeById(r2.id) : null;
  const fa = freePorts(a), fb = b ? freePorts(b) : [];
  if (!fa.length || (b && !fb.length)) {
    toast(`${(!fa.length ? a : b).name} has no free ports.`, true);
    setTool("select");
    return;
  }
  const opts = (node, ports) => ports.map((p) => ({ value: p, label: portName(node, p) }));
  const fields = [{ key: "pa", label: `Port on ${a.name}`, type: "select", options: opts(a, fa) }];
  if (b) fields.push({ key: "pb", label: `Port on ${b.name}`, type: "select", options: opts(b, fb) });
  openModal(b ? `Link ${a.name} to ${b.name}` : `Connect ${a.name} to the Cloud`, fields, "Create link",
    async (v) => {
      const body = { a: { node: a.id, port: Number(v.pa) } };
      if (b) body.b = { node: b.id, port: Number(v.pb) };
      await api("POST", `${labPath()}/links`, body);
      state.tool = "select";
      state.linkFrom = null;
    },
    () => setTool("select"));
}

// -------------------------------------------------------------- modal

function openModal(title, fields, okLabel, onSubmit, onCancel) {
  $("#modal-title").textContent = title;
  $("#modal-ok").textContent = okLabel;
  $("#modal-ok").hidden = !onSubmit;
  $("#modal-cancel").textContent = onSubmit ? "Cancel" : "Close";
  $("#modal-error").textContent = "";
  const inputs = {};
  $("#modal-fields").replaceChildren(...fields.map((f) => {
    if (f.node) return f.node;
    const id = "m-" + f.key;
    let input;
    if (f.type === "select") {
      input = h("select", { id }, f.options.map((o) => h("option", { value: o.value }, o.label)));
      if (f.value !== undefined) input.value = String(f.value);
    } else if (f.type === "textarea") {
      input = h("textarea", { id, rows: 6 }, f.value ?? "");
    } else if (f.type === "checkbox") {
      input = h("input", { id, type: "checkbox", checked: !!f.value });
    } else if (f.type === "file") {
      input = h("input", { id, type: "file", accept: f.accept });
    } else {
      input = h("input", { id, type: f.type || "text", value: f.value ?? "", placeholder: f.placeholder, min: f.min, max: f.max, autocomplete: "off" });
    }
    inputs[f.key] = input;
    const label = h("label", { for: id }, f.label);
    return f.type === "checkbox"
      ? h("div", { class: "field check" }, input, label)
      : h("div", { class: "field" }, label, input);
  }));
  const close = () => { $("#modal").hidden = true; };
  $("#modal-box").onsubmit = async (e) => {
    e.preventDefault();
    if (!onSubmit) { close(); return; }
    const values = {};
    for (const [k, input] of Object.entries(inputs)) {
      values[k] = input.type === "checkbox" ? input.checked
        : input.type === "file" ? (input.files && input.files[0]) || null
        : input.type === "password" ? input.value : input.value.trim();
    }
    $("#modal-ok").disabled = true;
    try {
      const keepOpen = await onSubmit(values);
      if (!keepOpen) close();
      await refresh(true);
    } catch (err) {
      $("#modal-error").textContent = err.message;
    } finally {
      $("#modal-ok").disabled = false;
    }
  };
  $("#modal-cancel").onclick = () => { close(); if (onCancel) onCancel(); };
  $("#modal").hidden = false;
  const first = Object.values(inputs)[0];
  if (first) first.focus();
}

function nextName(prefix) {
  const names = new Set(state.lab.nodes.map((n) => n.name));
  let i = 1;
  while (names.has(prefix + i)) i++;
  return prefix + i;
}

function addNodeDialog(kind) {
  // First free slot on a grid, starting near the middle of what is on screen.
  const r = $("#canvas").getBoundingClientRect();
  const cx = snap(((r.width || 900) / 2 - view.x) / view.k), cy = snap(((r.height || 600) / 2 - view.y) / view.k);
  let at = null;
  for (let i = 0; !at; i++) {
    const p = { x: cx - 160 + (i % 4) * 160, y: cy - 80 + Math.floor(i / 4) * 140 };
    if (!state.lab.nodes.some((n) => Math.hypot(n.x - p.x, n.y - p.y) < 110)) at = p;
  }
  const maxPorts = (state.sys && state.sys.max_chr_ports) || 24;
  const fields = kind === "chr"
    ? [
        { key: "name", label: "Hostname", value: nextName("R") },
        { key: "ports", label: `Ethernet ports (1-${maxPorts})`, type: "number", value: prefs.defPorts, min: 1, max: maxPorts },
        { key: "cpus", label: "vCPUs", type: "number", value: prefs.defCpus, min: 1, max: 8 },
        { key: "ram_mb", label: "RAM in MB (blank = sized from ports)", type: "number", placeholder: "auto", min: 128 },
      ]
    : [
        { key: "name", label: "Hostname", value: nextName("PC") },
        { key: "address", label: "IP address (optional)", placeholder: "192.168.10.2/24" },
        { key: "gateway", label: "Gateway (optional)", placeholder: "192.168.10.1" },
      ];
  openModal(kind === "chr" ? "Add router" : "Add client", fields, "Add", async (v) => {
    const body = { kind, name: v.name, ...at };
    for (const k of ["ports", "cpus", "ram_mb"]) if (v[k]) body[k] = Number(v[k]);
    for (const k of ["address", "gateway"]) if (v[k]) body[k] = v[k];
    const node = await api("POST", `${labPath()}/nodes`, body);
    state.sel = { type: "node", id: node.id };
  });
}

// ------------------------------------------------- router config tools

function credFields(n) {
  const saved = (() => { try { return JSON.parse(sessionStorage.getItem("mikrobox.creds")) || {}; } catch (e) { return {}; } })();
  const host = n ? [{ key: "host", label: n.mgmt_ip ? "Router address" : "Router address (not detected yet, enter it)", value: n.mgmt_ip || "" }] : [];
  return [
    ...host,
    { key: "username", label: "RouterOS user", value: saved.username || "admin" },
    { key: "password", label: "Password (blank on a fresh CHR)", type: "password", value: saved.password || "" },
  ];
}
function creds(v) {
  const c = { username: v.username || "admin", password: v.password || "" };
  try { sessionStorage.setItem("mikrobox.creds", JSON.stringify(c)); } catch (e) { /* ignore */ }
  if (v.host) c.host = v.host;
  return c;
}

function exportConfig(n) {
  openModal(`Export ${n.name}'s config`, [
    ...credFields(n),
    { key: "sensitive", label: "Include passwords and keys", type: "checkbox", value: false },
  ], "Export", async (v) => {
    const res = await apiRaw("POST", `${labPath()}/nodes/${n.id}/config/export`, { ...creds(v), sensitive: v.sensitive });
    saveBlob(await res.blob(), `${n.name}.rsc`);
  });
}

function exportAllConfigs() {
  openModal("Export all router configs", [
    { node: h("p", {}, "Runs /export on every running router that is linked to the Cloud and has a known address, and downloads one zip. The same login is used for all of them.") },
    ...credFields(),
    { key: "sensitive", label: "Include passwords and keys", type: "checkbox", value: false },
  ], "Export zip", async (v) => {
    const res = await apiRaw("POST", `${labPath()}/config/export`, { ...creds(v), sensitive: v.sensitive });
    const skipped = Number(res.headers.get("x-mikrobox-skipped") || 0);
    saveBlob(await res.blob(), `${state.lab.name}-configs.zip`);
    toast(skipped ? `Exported. ${skipped} router(s) skipped, see SKIPPED.txt in the zip.` : "Exported.");
  });
}

function importConfig(n) {
  const out = h("pre", { class: "out", hidden: true });
  openModal(`Import a config into ${n.name}`, [
    { node: h("p", {}, "Uploads a .rsc file and runs /import on the router. It is applied on top of the current config.") },
    { key: "file", label: "Config file (.rsc)", type: "file", accept: ".rsc,.txt" },
    ...credFields(n),
    { node: out },
  ], "Import", async (v) => {
    if (!v.file) throw new Error("Choose a .rsc file first.");
    const config = await v.file.text();
    const res = await api("POST", `${labPath()}/nodes/${n.id}/config/import`, { ...creds(v), config });
    out.hidden = false;
    out.textContent = res.output.trim() || "Imported with no output.";
    toast(`Config imported into ${n.name}.`);
    return true; // keep the dialog open so the router's output can be read
  });
}

function pushIdentity(n) {
  openModal(`Push hostname to ${n.name}`, [
    { node: h("p", {}, `Sets the RouterOS identity to "${n.name}".`) },
    ...credFields(n),
  ], "Push", async (v) => {
    await api("POST", `${labPath()}/nodes/${n.id}/identity`, creds(v));
    toast(`Identity set to ${n.name}.`);
  });
}

// ---------------------------------------------------------- inspector

function copyRow(text) {
  const copy = () => {
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Could not copy", true));
  };
  return h("div", { class: "copy" }, h("code", {}, text), h("button", { type: "button", onclick: copy }, "Copy"));
}

function captureUrl(node, port, seconds) {
  return `/api${labPath()}/nodes/${node.id}/ports/${port}/capture` + (seconds ? `?seconds=${seconds}` : "");
}

function head(title) {
  return h("div", { class: "head" }, h("h3", {}, title),
    h("button", { type: "button", class: "close", "aria-label": "Close details", onclick: () => select(null) }, icon("close")));
}

function renderInspector(force) {
  const box = $("#inspector");
  const active = document.activeElement;
  if (!force && active && box.contains(active) && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
  const lab = state.lab, sel = state.sel;
  box.hidden = !(lab && sel);
  if (box.hidden) { box.replaceChildren(); return; }
  if (sel.type === "mgmt") box.replaceChildren(...mgmtInspector());
  else if (sel.type === "link") box.replaceChildren(...linkInspector(lab.links.find((l) => l.id === sel.id)));
  else if (sel.type === "anno") box.replaceChildren(...annoInspector(annoById(sel.id)));
  else box.replaceChildren(...nodeInspector(nodeById(sel.id)));
}

function mgmtInspector() {
  const c = (state.sys && state.sys.cloud) || { mode: "nat", network: "10.99.0.0/16" };
  if (c.mode === "bridged") {
    return [
      head("Cloud"),
      h("div", { class: "status running" }, h("i", {}), "On your network"),
      h("p", {}, "A port linked here sits directly on the same network as this server. A fresh router asks for an address by DHCP on ether1, or you can set a static one in the same range. It has internet through your normal gateway, and your PC reaches it with no extra setup."),
      h("section", {}, h("h2", {}, "Network"),
        h("dl", { class: "info" },
          h("dt", {}, "Range"), h("dd", {}, c.network || "unknown"),
          h("dt", {}, "Gateway"), h("dd", {}, c.gateway || "unknown"),
          h("dt", {}, "Through"), h("dd", {}, c.uplink || "unknown"))),
      h("p", {}, "If devices get no address, check that the hypervisor allows more than one MAC address on this server's network card (on Proxmox: the firewall's MAC filter)."),
    ];
  }
  const host = location.hostname;
  const [net, bits] = (c.network || "10.99.0.0/16").split("/");
  const mask = bits === "16" ? "255.255.0.0" : "255.255.255.0";
  return [
    head("Cloud"),
    h("div", { class: "status" }, h("i", {}), "Internet through this server (NAT)"),
    h("p", {}, `A port linked here gets an address in ${c.network} by DHCP, with internet through this server. It is a private range, not your own network.`),
    h("section", {}, h("h2", {}, "Put devices on your own network"),
      h("p", {}, "Run this on the server to see what would change, then again with --apply:"),
      copyRow("sudo /opt/mikrobox/venv/bin/python -m mikrobox.cloudsetup enable")),
    h("section", {}, h("h2", {}, "Reach devices from your PC meanwhile"),
      h("p", {}, "Windows:"), copyRow(`route add ${net} mask ${mask} ${host}`),
      h("p", {}, "Linux or macOS:"), copyRow(`sudo ip route add ${net}/${bits} via ${host}`)),
  ];
}

function linkInspector(link) {
  const a = nodeById(link.a.node), b = link.b ? nodeById(link.b.node) : null;
  const ends = [[a, link.a.port]];
  if (b) ends.push([b, link.b.port]);
  const live = ends.find(([n]) => n.running);
  return [
    head("Link"),
    h("div", { class: "status" },
      `${a.name} ${portName(a, link.a.port)} to ` + (b ? `${b.name} ${portName(b, link.b.port)}` : "the Cloud")),
    h("section", {}, h("h2", {}, "Packet capture"),
      live
        ? [
            h("p", {}, h("a", { href: captureUrl(live[0], live[1], 30), download: "" }, "Download a 30 second capture")),
            h("p", {}, "Or watch it live in Wireshark:"),
            copyRow(`curl -sN ${location.origin}${captureUrl(live[0], live[1])} | wireshark -k -i -`),
          ]
        : h("p", {}, "Start a device on this link to capture its traffic.")),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, "Delete link")),
  ];
}

function annoInspector(a) {
  const path = `${labPath()}/annotations/${a.id}`;
  const area = a.kind === "rect" || a.kind === "ellipse";
  const text = h("textarea", { id: "i-text", rows: area || a.kind === "text" ? 2 : 6,
    onchange: (e) => act(() => api("PATCH", path, { text: e.target.value })) }, a.text);
  return [
    head(KIND_NAME[a.kind]),
    h("div", { class: "status" }, area ? "Marks a group of devices, such as a site or an OSPF area." : "Shown on the canvas for everyone using this lab."),
    h("div", { class: "field" }, h("label", { for: "i-text" }, area ? "Label" : "Text"), text),
    h("section", {}, h("h2", {}, "Colour"),
      h("div", { class: "swatches" }, COLORS.map((c) =>
        h("button", { type: "button", class: `c-${c}` + (a.color === c ? " on" : ""), "aria-label": c, title: c,
          "aria-pressed": String(a.color === c),
          onclick: () => act(() => api("PATCH", path, { color: c })) })))),
    h("p", {}, "Drag it to move. Drag the corner handle to resize."),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, `Delete ${KIND_NAME[a.kind].toLowerCase()}`)),
  ];
}

function nodeInspector(n) {
  const isChr = n.kind === "chr";
  const inputs = {};
  const field = (key, label, type, extra) => {
    const id = "i-" + key;
    inputs[key] = h("input", { id, type, value: n[key] ?? "", disabled: n.running, ...extra });
    return h("div", { class: "field" }, h("label", { for: id }, label), inputs[key]);
  };
  const save = () => act(async () => {
    const body = {};
    for (const [k, input] of Object.entries(inputs)) {
      const raw = input.value.trim();
      const value = input.type === "number" ? Number(raw) : (raw || null);
      if (value !== n[k] && !(input.type === "number" && !raw)) body[k] = value;
    }
    if (Object.keys(body).length) await api("PATCH", `${labPath()}/nodes/${n.id}`, body);
  });
  const path = `${labPath()}/nodes/${n.id}`;
  const reachable = n.running && !!n.cloud_port;

  const rows = [];
  for (let p = 1; p <= n.ports; p++) {
    rows.push(h("tr", {},
      h("td", { class: "mono" }, portName(n, p)),
      h("td", {}, peerText(n, p)),
      h("td", {}, n.running && h("a", { href: captureUrl(n, p, 30), download: "", title: "Download a 30 second packet capture" }, "Capture"))));
  }

  const access = [];
  if (n.mgmt_ip && isChr) {
    access.push(copyRow(n.ssh), h("p", {}, h("a", { href: `ssh://admin@${n.mgmt_ip}` }, "Open in your SSH client"),
      ". WinBox and MobaXterm can connect to the same address."));
  } else if (n.mgmt_ip) {
    access.push(copyRow(n.mgmt_ip));
  } else if (n.cloud_port) {
    access.push(h("p", {}, n.running
      ? `Waiting to see which address ${portName(n, n.cloud_port)} was given. You can also check on the console${isChr ? " with /ip address print" : ""}.`
      : "Start the device to get an address."));
  } else {
    access.push(h("p", {}, "Link a port to the Cloud to give this device internet and reach it from your PC."));
  }

  return [
    head(n.name),
    h("div", { class: "status" + (n.running ? " running" : "") }, h("i", {}),
      (n.running ? "Running" : "Stopped") + " · " + (isChr ? "CHR router" : "Linux client")),
    h("div", { class: "row" },
      n.running
        ? h("button", { type: "button", onclick: () => act(() => api("POST", path + "/stop")) }, "Stop")
        : h("button", { type: "button", class: "primary", onclick: () => act(() => api("POST", path + "/start")) }, "Start"),
      h("button", { type: "button", disabled: !n.running, onclick: () => openConsole(n) }, "Console")),
    h("section", {},
      h("h2", {}, "Settings"),
      field("name", "Hostname", "text"),
      isChr
        ? h("div", { class: "grid3" },
            field("ports", "Ports", "number", { min: 1, max: 24 }),
            field("cpus", "vCPUs", "number", { min: 1, max: 8 }),
            field("ram_mb", "RAM (MB)", "number", { min: 128 }))
        : [field("address", "IP address", "text", { placeholder: "192.168.10.2/24" }),
           field("gateway", "Gateway", "text", { placeholder: "192.168.10.1" })],
      n.running
        ? h("p", {}, "Stop the device to change its settings.")
        : h("button", { type: "button", onclick: save }, "Save changes")),
    h("section", {}, h("h2", {}, "Interfaces"), h("table", {}, h("tbody", {}, rows))),
    h("section", {}, h("h2", {}, "Access from your PC"), access),
    isChr && h("section", {}, h("h2", {}, "Configuration"),
      h("div", { class: "row" },
        h("button", { type: "button", disabled: !reachable, onclick: () => exportConfig(n) }, "Export"),
        h("button", { type: "button", disabled: !reachable, onclick: () => importConfig(n) }, "Import"),
        h("button", { type: "button", disabled: !reachable, onclick: () => pushIdentity(n) }, "Push hostname")),
      !reachable && h("p", {}, "These need the router running and linked to the Cloud."),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { type: "button", disabled: n.running, onclick: () => {
          if (!confirm(`Reset ${n.name} to factory defaults? Its configuration is erased.`)) return;
          act(async () => { await api("POST", path + "/reset"); toast(`${n.name} will boot with factory defaults.`); });
        } }, "Factory reset"))),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, "Delete device")),
  ];
}

function deleteSelected() {
  const sel = state.sel;
  if (!sel || sel.type === "mgmt") return;
  if (sel.type === "node") {
    const n = nodeById(sel.id);
    if (!confirm(`Delete ${n.name}? Its configuration is lost.`)) return;
    closeConsole(n.id);
    act(async () => { await api("DELETE", `${labPath()}/nodes/${n.id}`); state.sel = null; });
  } else {
    const kind = sel.type === "link" ? "links" : "annotations";
    act(async () => { await api("DELETE", `${labPath()}/${kind}/${sel.id}`); state.sel = null; });
  }
}

// ------------------------------------------------------------ console

function openConsole(node) {
  if (!consoles.has(node.id)) {
    const pane = h("div", {});
    $("#terms").append(pane);
    $("#dock").hidden = false;
    const term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000 });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(pane);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api${labPath()}/nodes/${node.id}/console`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send("\r");
    ws.onmessage = (e) => term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
    ws.onclose = () => term.write("\r\n[disconnected]\r\n");
    term.onData((data) => { if (ws.readyState === 1) ws.send(data); });
    consoles.set(node.id, { term, fit, ws, pane, name: node.name });
  }
  state.activeConsole = node.id;
  renderDock();
}

function closeConsole(id) {
  const c = consoles.get(id);
  if (!c) return;
  c.ws.close();
  c.term.dispose();
  c.pane.remove();
  consoles.delete(id);
  if (state.activeConsole === id) state.activeConsole = consoles.keys().next().value ?? null;
  renderDock();
}

function renderDock() {
  $("#dock").hidden = consoles.size === 0;
  $("#tabs").replaceChildren(...[...consoles].map(([id, c]) =>
    h("div", { class: "tab" + (id === state.activeConsole ? " on" : "") },
      h("button", { type: "button", class: "name", onclick: () => { state.activeConsole = id; renderDock(); } }, c.name),
      h("button", { type: "button", "aria-label": `Close ${c.name} console`, onclick: () => closeConsole(id) }, "×"))));
  for (const [id, c] of consoles) {
    c.pane.hidden = id !== state.activeConsole;
    if (id === state.activeConsole) {
      requestAnimationFrame(() => { try { c.fit.fit(); c.term.focus(); } catch (e) { /* not visible yet */ } });
    }
  }
}

window.addEventListener("resize", () => {
  const c = consoles.get(state.activeConsole);
  if (c) { try { c.fit.fit(); } catch (e) { /* ignore */ } }
});

// ---------------------------------------------------- menus and top bar

const ACTIONS = {
  "lab-new": () => openModal("New lab", [{ key: "name", label: "Name (letters, digits, - and _)", value: "" }], "Create", async (v) => {
    const lab = await api("POST", "/labs", { name: v.name });
    switchLab(lab);
  }),
  "lab-rename": () => openModal("Rename lab", [{ key: "name", label: "Name", value: state.lab.name }], "Rename",
    async (v) => { await api("PATCH", labPath(), { name: v.name }); }),
  "lab-import": () => openModal("Import lab", [
    { node: h("p", {}, "Loads a lab file exported from Mikrobox as a new lab. It brings the topology, shapes and notes; router configs are exported and imported separately.") },
    { key: "file", label: "Lab file (.json)", type: "file", accept: ".json" },
  ], "Import", async (v) => {
    if (!v.file) throw new Error("Choose a lab file first.");
    let data;
    try { data = JSON.parse(await v.file.text()); } catch (e) { throw new Error("That file is not valid JSON."); }
    const lab = await api("POST", "/labs/import", data);
    switchLab(lab);
    toast(`Imported as ${lab.name}.`);
  }),
  "lab-export": async () => {
    try {
      const res = await apiRaw("GET", `${labPath()}/export`);
      saveBlob(await res.blob(), `${state.lab.name}.mikrobox.json`);
    } catch (e) { toast(e.message, true); }
  },
  "lab-delete": () => {
    if (!confirm(`Delete lab ${state.lab.name} and all its devices?`)) return;
    for (const id of [...consoles.keys()]) closeConsole(id);
    act(async () => { await api("DELETE", labPath()); state.lab = null; state.sel = null; });
  },
  "cfg-export": () => { const n = selectedRouter(); if (n) exportConfig(n); },
  "cfg-export-all": () => exportAllConfigs(),
  "cfg-import": () => { const n = selectedRouter(); if (n) importConfig(n); },
  "cfg-identity": () => { const n = selectedRouter(); if (n) pushIdentity(n); },
};

function switchLab(lab) {
  state.lab = lab;
  state.sel = null;
  state.linkFrom = null;
  writeStore("lab", lab.id);
  loadView();
}

function closeMenus() {
  for (const m of document.querySelectorAll(".menu")) {
    m.querySelector(".menu-list").hidden = true;
    m.querySelector(".menu-btn").setAttribute("aria-expanded", "false");
  }
}
for (const m of document.querySelectorAll(".menu")) {
  const btn = m.querySelector(".menu-btn"), list = m.querySelector(".menu-list");
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = list.hidden;
    closeMenus();
    list.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  });
  list.addEventListener("click", (e) => {
    const act_ = e.target.closest("[data-act]");
    if (!act_) return;
    closeMenus();
    ACTIONS[act_.dataset.act]();
  });
}
document.addEventListener("click", closeMenus);

$("#lab-select").addEventListener("change", (e) => {
  const lab = state.labs.find((l) => l.id === Number(e.target.value));
  if (lab) switchLab(lab);
  render(true);
});
$("#lab-start").addEventListener("click", () => act(() => api("POST", labPath() + "/start")));
$("#lab-stop").addEventListener("click", () => act(() => api("POST", labPath() + "/stop")));
$("#zoom-in").addEventListener("click", () => zoomBy(1.25));
$("#zoom-out").addEventListener("click", () => zoomBy(0.8));
$("#zoom-reset").addEventListener("click", () => zoomBy(1 / view.k));
$("#zoom-fit").addEventListener("click", fitView);

$("#settings").append(icon("gear"));
$("#settings").addEventListener("click", () => {
  const sys = state.sys || {};
  const row = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
  openModal("Settings", [
    { node: h("h2", {}, "Appearance") },
    { key: "theme", label: "Theme", type: "select", value: prefs.theme,
      options: [{ value: "system", label: "Match my system" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }] },
    { key: "grid", label: "Show the dot grid", type: "checkbox", value: prefs.grid },
    { key: "snap", label: "Snap to the grid when moving things", type: "checkbox", value: prefs.snap },
    { key: "labels", label: "Show port names on links", type: "checkbox", value: prefs.labels },
    { node: h("h2", {}, "New routers") },
    { key: "defPorts", label: "Default number of ports", type: "number", value: prefs.defPorts, min: 1, max: sys.max_chr_ports || 24 },
    { key: "defCpus", label: "Default vCPUs", type: "number", value: prefs.defCpus, min: 1, max: 8 },
    { node: h("h2", {}, "This server") },
    { node: h("dl", { class: "info" },
      row("Mikrobox", sys.version || "?"),
      row("RouterOS image", sys.chr_image ? (sys.chr_version || "installed") : "missing"),
      row("KVM acceleration", sys.kvm ? "available" : "not available"),
      row("Device limit", `${sys.max_devices ?? "?"} running at once`),
      row("Free memory", sys.mem_available_mb != null ? `${sys.mem_available_mb} MB` : "?"),
      row("Cloud", sys.cloud ? (sys.cloud.mode === "bridged" ? `your network ${sys.cloud.network || ""}` : `NAT, ${sys.cloud.network}`) : "?"),
      row("API reference", h("a", { href: "/docs", target: "_blank", rel: "noopener" }, "/docs"))) },
  ], "Save", async (v) => {
    Object.assign(prefs, {
      theme: v.theme, grid: v.grid, snap: v.snap, labels: v.labels,
      defPorts: Math.min(sys.max_chr_ports || 24, Math.max(1, Number(v.defPorts) || 8)),
      defCpus: Math.min(8, Math.max(1, Number(v.defCpus) || 1)),
    });
    writeStore("prefs", prefs);
    applyPrefs();
  });
});

window.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement && document.activeElement.tagName);
  if (e.key === "Escape") {
    if (!$("#modal").hidden) $("#modal-cancel").click();
    else if (state.tool !== "select") setTool("select");
    else if (state.sel) select(null);
    closeMenus();
  } else if ((e.key === "Delete" || e.key === "Backspace") && !typing && $("#modal").hidden && state.sel) {
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#terms")) return;
    e.preventDefault();
    deleteSelected();
  }
});

setInterval(() => {
  if (!drag && $("#modal").hidden) refresh(false).catch(() => {});
}, 3000);

applyPrefs();
refresh(true).catch((e) => toast(e.message, true));
