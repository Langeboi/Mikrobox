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
  chr: "M2 12a10 10 0 1 0 20 0a10 10 0 1 0-20 0 M6 12h12 M12 6v12 M6 12l2.6-2.6 M6 12l2.6 2.6 M18 12l-2.6-2.6 M18 12l-2.6 2.6 M12 6l-2.6 2.6 M12 6l2.6 2.6 M12 18l-2.6-2.6 M12 18l2.6-2.6",
  client: "M4 5h16v11H4z M9 20h6 M12 16v4",
  switch: "M3 8.5h18 M17 5l4 3.5-4 3.5 M21 15.5H3 M7 12l-4 3.5 4 3.5",
  rect: "M4 6h16v12H4z",
  ellipse: "M3 12a9 6.5 0 1 0 18 0a9 6.5 0 1 0-18 0",
  text: "M5 6h14 M12 6v13 M9 19h6",
  note: "M5 4h14v11l-5 5H5z M14 20v-5h5",
  gear: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M4.9 4.9l2.1 2.1 M17 17l2.1 2.1 M4.9 19.1l2.1-2.1 M17 7l2.1-2.1",
  close: "M6 6l12 12 M18 6L6 18",
  collapse: "M6 9l6 6 6-6",
  expand: "M6 15l6-6 6 6",
};
const CLOUD = "M-34 26a20 20 0 0 1-5-39a27 27 0 0 1 51-9a23 23 0 0 1 27 48z";
const icon = (name) => s("svg", { class: "icon", viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: ICONS[name] }));

const TOOLS = [
  { id: "select", label: "Select", tip: "Select, move and pan" },
  { id: "link", label: "Link", tip: "Connect two ports. Shortcut: Ctrl+drag from one device to another" },
  null,
  { id: "chr", label: "Router", tip: "Add a router (MikroTik CHR)", add: true },
  { id: "switch", label: "Switch", tip: "Add a switch (MikroTik CHR or Aruba AOS-CX)", add: true },
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
const FILL = { light: 9, medium: 20, strong: 36 };
const FONT = {
  s: { area: 11, text: 13, note: 11 }, m: { area: 13, text: 16, note: 13 },
  l: { area: 17, text: 22, note: 16 }, xl: { area: 24, text: 32, note: 20 },
};

const state = {
  labs: [], lab: null, sys: null,
  sel: null,        // {type: "node" | "link" | "mgmt" | "anno", id}
  tool: "select",
  linkFrom: null,
  activeDock: null,
  collapsed: false, // details panel folded down to its title
};
const view = { x: 90, y: 20, k: 1 };
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
  if (res.status === 401) {
    location.replace("/login"); // signed out, or the session ran out
    throw new Error("Sign in first.");
  }
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

const CLIENT = Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
window.addEventListener("pagehide", () => {
  const body = new Blob([JSON.stringify({ client: CLIENT })], { type: "application/json" });
  if (navigator.sendBeacon) navigator.sendBeacon("/api/presence/leave", body);
});
const WHO_COLORS = ["purple", "green", "amber", "red", "blue", "teal", "pink"];
// FNV-1a: a stable colour per name that spreads short names well.
const whoColor = (name) => WHO_COLORS[[...name].reduce((hash, ch) => Math.imul(hash ^ ch.charCodeAt(0), 16777619) >>> 0, 2166136261) % WHO_COLORS.length];

const beatBody = () => ({ client: CLIENT, lab: state.lab ? state.lab.id : null,
  node: state.sel && state.sel.type === "node" ? state.sel.id : null });

async function refresh(force) {
  const [labs, sys, pres] = await Promise.all([api("GET", "/labs"), api("GET", "/system"),
    api("POST", "/presence", beatBody()).catch(() => ({ others: [] }))]);
  state.labs = labs;
  state.sys = sys;
  state.others = pres.others;
  const before = state.lab ? state.lab.id : null;
  const wanted = before ?? readStore("lab", null);
  state.lab = labs.find((l) => l.id === wanted) || labs[0] || null;
  if (state.lab) noteChanges(state.lab);
  if (state.lab && state.lab.id !== before) loadView();
  if (textEdit && state.lab) {
    // Keep what is being typed; the server copy is a moment behind.
    const typing = state.lab.annotations.find((x) => x.id === textEdit.id);
    if (typing) typing.text = textEdit.text;
  }
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
const portName = (node, port) => (node.kind === "chr" ? `ether${port}`
  : node.kind === "aoscx" ? (port === 1 ? "mgmt" : `1/1/${port - 1}`)
  : `eth${port - 1}`);
const isVm = (node) => node.kind === "chr" || node.kind === "aoscx";
const roleName = (node) => (node.kind === "client" ? "Client" : node.role === "switch" ? "Switch" : "Router");

// The glyph shows the role; a small badge names the OS when it is not MikroTik.
function osBadge(n) {
  const p = platformInfo(n.kind);
  if (!p || !p.short) return null;
  const w = 10 + p.short.length * 7;
  return s("g", { class: `os-badge k-${n.kind}`, "aria-hidden": "true" },
    s("rect", { x: 30 - w + 4, y: 17, width: w, height: 15, rx: 7.5 }),
    s("text", { x: 30 - w / 2 + 4, y: 28 }, p.short));
}
const platformInfo = (kind) => ((state.sys && state.sys.platforms) || []).find((p) => p.kind === kind) || null;
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
  if (ref && ref.type === "node") followDock(ref.id);
  render(true);
  beatSoon(); // others see what you selected straight away
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
  // Leave room for whatever floats over the canvas, wherever the layout put it.
  const pad = { l: 30, r: 30, t: 30, b: 30 };
  const tools = $("#tools").getBoundingClientRect();
  if (tools.height > tools.width) pad.l = tools.right - r.left + 24;
  else pad.t = tools.bottom - r.top + 24;
  const insp = $("#inspector");
  if (!insp.hidden) {
    const ir = insp.getBoundingClientRect();
    if (ir.width > r.width * 0.6) pad.b = ir.height + 24;
    else pad.r = ir.width + 36;
  }
  const availW = Math.max(120, r.width - pad.l - pad.r), availH = Math.max(120, r.height - pad.t - pad.b);
  const k = Math.min(1.5, Math.max(0.25, Math.min(availW / (x1 - x0 || 1), availH / (y1 - y0 || 1))));
  view.k = k;
  view.x = pad.l + (availW - (x1 - x0) * k) / 2 - x0 * k;
  view.y = pad.t + (availH - (y1 - y0) * k) / 2 - y0 * k;
  saveView();
  renderCanvas();
}
function point(e) {
  const r = $("#canvas").getBoundingClientRect();
  return { x: (e.clientX - r.left - view.x) / view.k, y: (e.clientY - r.top - view.y) / view.k };
}

// ------------------------------------------------------------- render

function render(force) {
  if (state.fitNext) {
    state.fitNext = false;
    renderTop();
    fitView(); // a new lab opens framed, whatever the window size
    renderInspector(force);
    return;
  }
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

  // Other people with this lab open, one badge per person. Hovering says which device they
  // have selected; nothing is drawn on the canvas for it.
  const inLab = (state.others || []).filter((o) => state.lab && o.lab === state.lab.id);
  const here = [...new Set(inLab.map((o) => o.user))];
  $("#who").replaceChildren(...here.map((name) => {
    const on = [...new Set(inLab.filter((o) => o.user === name && o.node !== null).map((o) => nodeById(o.node)).filter(Boolean).map((n) => n.name))];
    const text = `${name} is in this lab` + (on.length ? `, on ${on.join(" and ")}` : "");
    return h("span", { class: `who c-${whoColor(name)}`, title: text, role: "img", "aria-label": text }, name.slice(0, 2));
  }));

  const sys = state.sys;
  // A side-by-side install (e.g. a test build) names itself so the two never get mixed up.
  const label = sys && sys.label;
  $("#install-label").hidden = !label;
  $("#install-label").textContent = label || "";
  document.title = label ? `Mikrobox (${label})` : "Mikrobox";
  $("#sys").textContent = sys
    ? `${sys.running_devices} / ${sys.max_devices} running` +
      (sys.mem_available_mb !== null ? ` · ${(sys.mem_available_mb / 1024).toFixed(1)} GB free` : "") +
      (sys.ksm && sys.ksm.running && sys.ksm.saved_mb >= 100 ? ` · ${(sys.ksm.saved_mb / 1024).toFixed(1)} GB shared` : "")
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
    }, icon(t.id), h("span", { class: "lbl" }, t.label));
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
  const area = a.kind === "rect" || a.kind === "ellipse";
  // Only areas take the fill and border options; the defaults come from the stylesheet.
  let look = null;
  if (area) {
    const parts = [];
    if (a.fill !== "light") parts.push(`fill:${a.fill === "none" ? "transparent" : `color-mix(in srgb, var(--c) ${FILL[a.fill]}%, transparent)`}`);
    if (a.border === "none") parts.push("stroke:none");
    else {
      if (a.border !== "dashed") parts.push(`stroke-dasharray:${a.border === "solid" ? "none" : `0.1 ${a.border_width * 3.5}`}`, "stroke-linecap:round");
      if (a.border_width !== 1.5) parts.push(`stroke-width:${a.border_width}`);
    }
    look = parts.join(";") || null;
  }
  if (a.kind === "ellipse") {
    g.append(s("ellipse", { class: "shape", style: look, cx: a.x + a.w / 2, cy: a.y + a.h / 2, rx: a.w / 2, ry: a.h / 2 }));
  } else {
    g.append(s("rect", { class: "shape", style: look, x: a.x, y: a.y, width: a.w, height: a.h,
      rx: a.kind === "note" ? 8 : a.kind === "rect" ? Math.min(a.radius, a.w / 2, a.h / 2) : 12 }));
  }
  const size = FONT[a.label_size] || FONT.m;
  if (area && a.text) {
    const round = a.kind === "ellipse";
    const pos = a.label_pos === "auto" ? (round ? "top" : "top-left") : a.label_pos;
    const boxH = size.area + 20;
    const inset = round ? a.h * 0.12 : 0;
    const y = pos.startsWith("top") ? a.y + inset
      : pos === "center" ? a.y + a.h / 2 - boxH / 2
      : a.y + a.h - boxH - inset;
    const narrow = round && pos !== "center";
    const fo = s("foreignObject", { x: narrow ? a.x + a.w * 0.15 : a.x, y, width: narrow ? a.w * 0.7 : a.w, height: boxH });
    fo.append(h("div", { class: "label", style: `font-size:${size.area}px;text-align:${pos.endsWith("left") ? "left" : "center"}` }, a.text));
    g.append(fo);
  } else if (!area) {
    const fo = s("foreignObject", { x: a.x, y: a.y, width: a.w, height: a.h });
    fo.append(h("div", { class: "body", style: a.label_size !== "m" ? `font-size:${a.kind === "text" ? size.text : size.note}px` : null },
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

// ---------------------------------------------------------------- motion
//
// Short animations for what just changed: a device starting or stopping, a new device,
// a new link, a link coming up. The canvas is rebuilt on every update, so each change is
// noted with its time and the rebuilt element starts its animation that far in (a negative
// delay): it plays once, smoothly, however often the canvas redraws. Only changes seen
// while a lab is open animate, never what was already there when it loaded. Turned off
// for people who ask their system for reduced motion (see app.css).
const MOTION_MS = { start: 1400, stop: 800, add: 420, link: 520, up: 1000 };
const motion = { lab: null, seen: null, nodes: new Map(), links: new Map() };
const linkUp = (lab, l) => {
  const a = lab.nodes.find((n) => n.id === l.a.node), b = l.b && lab.nodes.find((n) => n.id === l.b.node);
  return !!(a && a.running && (l.b ? b && b.running : true));
};
function noteChanges(lab) {
  const now = performance.now();
  const cur = { nodes: new Map(lab.nodes.map((n) => [n.id, n.running])), links: new Map(lab.links.map((l) => [l.id, linkUp(lab, l)])) };
  const prev = motion.lab === lab.id ? motion.seen : null;
  if (prev) {
    for (const [id, running] of cur.nodes) {
      if (!prev.nodes.has(id)) motion.nodes.set(id, { kind: "add", at: now });
      else if (running !== prev.nodes.get(id)) motion.nodes.set(id, { kind: running ? "start" : "stop", at: now });
    }
    for (const [id, up] of cur.links) {
      if (!prev.links.has(id)) motion.links.set(id, { kind: "link", at: now });
      else if (up && !prev.links.get(id)) motion.links.set(id, { kind: "up", at: now });
    }
  } else {
    motion.nodes.clear();
    motion.links.clear();
  }
  motion.lab = lab.id;
  motion.seen = cur;
}
// " m-<kind>" and the delay to put on the element, or null when nothing is playing.
function motionOf(map, id) {
  const m = map.get(id);
  if (!m) return null;
  const t = performance.now() - m.at;
  if (t >= MOTION_MS[m.kind]) { map.delete(id); return null; }
  return { cls: " m-" + m.kind, kind: m.kind, style: `--md:${-Math.round(t)}ms` };
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
      const mo = motionOf(motion.links, l.id);
      const down = (e) => {
        if (state.tool !== "select") return; // drawing or linking passes through
        e.stopPropagation();
        select({ type: "link", id: l.id });
      };
      world.append(
        s("g", { class: "link" + (isSel("link", l.id) ? " sel" : "") + (up ? " up" : "") + (mo ? mo.cls : ""),
          style: mo ? `${mo.style};--len:${Math.hypot(B.x - A.x, B.y - A.y).toFixed(1)}` : null },
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

  // A link being dragged out with Ctrl: a dashed line to the pointer, or to the device under it.
  const linking = drag && drag.kind === "link" && drag.moved ? drag : null;
  if (linking) {
    const B = linking.target ? refPoint(linking.target) : { x: linking.x, y: linking.y };
    world.append(s("line", { class: "rubber", x1: linking.x0, y1: linking.y0, x2: B.x, y2: B.y }));
  }

  const cls = (ref, extra) => "node" + (extra || "") + (sameRef(state.sel, ref) ? " sel" : "") + (sameRef(state.linkFrom, ref) ? " from" : "")
    + (linking && sameRef(linking.target, ref) ? " target" : "");
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
    const mo = motionOf(motion.nodes, n.id);
    world.append(
      s("g", { class: cls(ref, (n.running ? " running" : "") + ` k-${n.kind}` + (mo ? mo.cls : "")), transform: `translate(${n.x},${n.y})`,
        style: mo ? mo.style : null, ...handlers(ref) },
        s("title", {}, `${n.name} (${n.running ? "running" : "stopped"})` +
          (n.services && n.services.length ? `\nRuns: ${n.services.map((x) => SERVICES.find((d) => d[0] === x)[1]).join(", ")}` : "")),
        // Its own group, so an animation can scale the tile without touching where it sits.
        s("g", { class: "tile" },
          s("rect", { class: "box", x: -30, y: -30, width: 60, height: 60, rx: 14 }),
          s("path", { class: "glyph", d: ICONS[n.kind === "client" ? "client" : n.role === "switch" ? "switch" : "chr"], transform: "translate(-19.2,-19.2) scale(1.6)", "vector-effect": "non-scaling-stroke" }),
          osBadge(n),
          mo && mo.kind === "start" && [s("circle", { class: "ripple", cx: 26, cy: -26, r: 6.5 }),
                                        s("circle", { class: "ripple r2", cx: 26, cy: -26, r: 6.5 })],
          s("circle", { class: "dot", cx: 26, cy: -26, r: 6.5 }),
          // Containers on a PC with Docker: how many, in a small badge on the corner.
          n.docker_summary && n.docker_summary.total > 0 && s("g", { class: "cbadge", transform: "translate(25,24)" },
            s("title", {}, `${n.docker_summary.total} container${n.docker_summary.total > 1 ? "s" : ""}, ${n.docker_summary.running} running`),
            s("rect", { x: -15, y: -10, width: 30 + (String(n.docker_summary.total).length - 1) * 7, height: 20, rx: 10 }),
            s("path", { d: ICON_PATHS.cube, transform: "translate(-11,-6) scale(0.5)" }),
            s("text", { x: 7, y: 4 }, String(n.docker_summary.total))),
          s("text", { y: 48 }, n.name),
          s("text", { class: "sub", y: 62 }, n.mgmt_ip || (n.kind === "client" && clientAddress(n)) || "")),
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

let lastDown = { id: null, t: 0 };

function nodeDown(e, ref) {
  if (DRAW[state.tool]) return; // let the canvas start drawing
  e.preventDefault();
  e.stopPropagation();
  if (state.tool === "link") { linkClick(ref); return; }
  // Ctrl (or ⌘ on a Mac) and drag from a device draws a link to where it is dropped.
  if ((e.ctrlKey || e.metaKey) && state.tool === "select") {
    const p = point(e), at = refPoint(ref);
    drag = { kind: "link", from: ref, x0: at.x, y0: at.y, sx: p.x, sy: p.y, x: p.x, y: p.y, target: null, moved: false };
    state.linkFrom = ref;
    $("#canvas").classList.add("linking");
    renderCanvas();
    return;
  }
  // Double click on a device opens its console. Tracked by hand because the
  // canvas is redrawn on the first click, which swallows the native event.
  const now = Date.now();
  if (ref.type === "node" && lastDown.id === ref.id && now - lastDown.t < 450) {
    lastDown = { id: null, t: 0 };
    const n = nodeById(ref.id);
    if (n.running) openConsole(n);
    else toast(`Start ${n.name} to open its console.`);
    return;
  }
  lastDown = { id: ref.type === "node" ? ref.id : null, t: now };
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
    } else if (drag.kind === "link") {
      if (!drag.moved && Math.hypot(p.x - drag.sx, p.y - drag.sy) < 6) return;  // still a click
      drag.x = p.x; drag.y = p.y;
      const t = refAt(p);
      drag.target = t && !sameRef(t, drag.from) ? t : null;
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
  if (d.kind === "link") { finishLink(d); return; }
  if (!d.moved) return;
  if (d.kind === "mgmt") api("PATCH", labPath(), { mgmt_x: state.lab.mgmt_x, mgmt_y: state.lab.mgmt_y }).catch(fail);
  else if (d.kind === "node") { const n = nodeById(d.id); api("PATCH", `${labPath()}/nodes/${n.id}`, { x: n.x, y: n.y }).catch(fail); }
  else { const a = annoById(d.id); api("PATCH", `${labPath()}/annotations/${a.id}`, { x: a.x, y: a.y, w: a.w, h: a.h }).catch(fail); }
});

// Where a device or the Cloud sits, and which one is under a point on the canvas.
function refPoint(ref) {
  return ref.type === "mgmt" ? { x: state.lab.mgmt_x, y: state.lab.mgmt_y } : nodeById(ref.id);
}
function refAt(p) {
  const lab = state.lab;
  const n = lab.nodes.find((m) => Math.abs(p.x - m.x) <= 36 && Math.abs(p.y - m.y) <= 36);
  if (n) return { type: "node", id: n.id };
  if (Math.abs(p.x - lab.mgmt_x) <= 50 && Math.abs(p.y - lab.mgmt_y) <= 36) return { type: "mgmt", id: 0 };
  return null;
}

function finishLink(d) {
  $("#canvas").classList.remove("linking");
  if (!d.moved) {
    // A plain Ctrl+click: carry on as with the Link tool, the next device clicked completes it.
    setTool("link");
    state.linkFrom = d.from;
    render(true);
    return;
  }
  state.linkFrom = null;
  if (d.target) linkDialog(d.from, d.target);
  else render(true);
}

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

// On a Mac, Ctrl+click is also a right click; the link shortcut should not open a menu.
$("#canvas").addEventListener("contextmenu", (e) => { if (e.ctrlKey) e.preventDefault(); });

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
  // The first free ports are already chosen: Enter takes them, Tab reaches the lists.
  $("#modal-ok").focus();
}

// -------------------------------------------------------------- modal

function openModal(title, fields, okLabel, onSubmit, onCancel, wide) {
  $("#modal-box").classList.toggle("wide", !!wide);
  $("#modal-ok").classList.remove("danger-fill");
  $("#modal-ok").disabled = false;
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

function freeSpot() {
  // First free slot on a grid, starting near the middle of what is on screen.
  const r = $("#canvas").getBoundingClientRect();
  const cx = snap(((r.width || 900) / 2 - view.x) / view.k), cy = snap(((r.height || 600) / 2 - view.y) / view.k);
  for (let i = 0; ; i++) {
    const p = { x: cx - 160 + (i % 4) * 160, y: cy - 80 + Math.floor(i / 4) * 140 };
    if (!state.lab.nodes.some((n) => Math.hypot(n.x - p.x, n.y - p.y) < 110)) return p;
  }
}

function addNodeDialog(tool) {
  const at = freeSpot();
  if (tool === "client") {
    openModal("Add client", [
      { key: "name", label: "Hostname", value: nextName("PC") },
      { key: "addressing", label: "Addressing", type: "select", value: "static",
        options: [{ value: "static", label: "Fixed address" }, { value: "dhcp", label: "Ask DHCP for an address" }] },
      { key: "address", label: "IP address (optional)", placeholder: "192.168.10.2/24" },
      { key: "gateway", label: "Gateway (optional)", placeholder: "192.168.10.1" },
      { key: "dns", label: "DNS servers (optional)", placeholder: "1.1.1.1, 9.9.9.9" },
      { node: h("p", { class: "hint" }, "Web, iperf3, DHCP and DNS servers can be switched on in the client's settings.") },
    ], "Add", async (v) => {
      const body = { kind: "client", name: v.name, addressing: v.addressing, ...at };
      if (v.addressing !== "dhcp") for (const k of ["address", "gateway", "dns"]) if (v[k]) body[k] = v[k];
      const node = await api("POST", `${labPath()}/nodes`, body);
      state.sel = { type: "node", id: node.id };
    });
    const mode = $("#m-addressing");
    mode.addEventListener("change", () => { for (const k of ["address", "gateway", "dns"]) $("#m-" + k).disabled = mode.value === "dhcp"; });
    return;
  }
  addDeviceDialog(tool === "switch" ? "switch" : "router", at);
}

// A router or switch: pick the role from the toolbar, then the operating system here.
function addDeviceDialog(role, at) {
  const sys = state.sys || {};
  const choices = (sys.platforms || []).filter((p) => p.roles.includes(role));
  if (!choices.length) { toast("This server has no operating system for that.", true); return; }
  const remembered = prefs[`os_${role}`];
  const os = h("select", { id: "m-os" }, choices.map((p) =>
    h("option", { value: p.kind }, p.label + (p.version ? ` ${p.version}` : "")
      + (p.experimental ? " (experimental)" : "") + (p.image ? "" : " (image not installed)"))));
  os.value = choices.some((p) => p.kind === remembered) ? remembered : choices[0].kind;
  const note = h("div", {});
  const name = h("input", { id: "m-name", type: "text", autocomplete: "off" });
  const portsLabel = h("label", { for: "m-ports" });
  const ports = h("input", { id: "m-ports", type: "number", min: 1 });
  const cpus = h("input", { id: "m-cpus", type: "number", min: 1, max: 8 });
  const ramLabel = h("label", { for: "m-ram" });
  const ram = h("input", { id: "m-ram", type: "number" });
  let named = false;
  name.addEventListener("input", () => { named = true; });
  const field = (label, input) => h("div", { class: "field" }, label, input);
  const chosen = () => choices.find((p) => p.kind === os.value);

  const fill = () => {
    const p = chosen();
    if (!named) name.value = nextName(p.kind === "aoscx" ? "CX" : role === "switch" ? "SW" : "R");
    if (p.kind === "chr") {
      const max = sys.max_chr_ports || p.max_ports;
      portsLabel.textContent = `Ethernet ports (1-${max})`;
      ports.max = max;
      ports.value = role === "switch" ? max : prefs.defPorts;
      cpus.value = prefs.defCpus;
      ramLabel.textContent = "RAM in MB (blank = sized from ports)";
      ram.min = p.min_ram_mb; ram.value = ""; ram.placeholder = "auto";
    } else {
      portsLabel.textContent = `Data ports, 1/1/1 onwards (1-${p.max_ports - 1}). The mgmt port comes on top.`;
      ports.max = p.max_ports - 1;
      ports.value = p.default_ports - 1;
      cpus.value = p.default_cpus;
      ramLabel.textContent = `RAM in MB (at least ${p.min_ram_mb})`;
      ram.min = p.min_ram_mb; ram.value = p.default_ram_mb; ram.placeholder = "";
    }
    const lines = [];
    if (p.kind === "chr" && role === "switch") lines.push(h("p", {}, "Boots unconfigured: add its ports to a bridge in RouterOS to make it switch."));
    if (p.experimental) lines.push(h("p", {}, `Experimental. Takes a few minutes to boot. Log in as ${p.login}.`));
    if (!p.image) lines.push(h("p", { class: "warn-text" }, `The ${p.label} image is not installed yet. `,
      p.experimental
        ? [h("a", { href: "#", onclick: (e) => { e.preventDefault(); imagesDialog(p.kind); } }, "Add it now"), "."]
        : p.image_help));
    note.replaceChildren(...lines);
  };
  os.addEventListener("change", fill);
  fill();

  openModal(role === "switch" ? "Add switch" : "Add router", [
    { node: field(h("label", { for: "m-os" }, "Operating system"), os) },
    { node: note },
    { node: field(h("label", { for: "m-name" }, "Hostname"), name) },
    { node: field(portsLabel, ports) },
    { node: field(h("label", { for: "m-cpus" }, "vCPUs"), cpus) },
    { node: field(ramLabel, ram) },
  ], "Add", async () => {
    const p = chosen();
    if (!p.image && p.experimental) throw new Error(`Add the ${p.label} image first, with the link above.`);
    prefs[`os_${role}`] = p.kind;
    writeStore("prefs", prefs);
    const n = Number(ports.value) || (p.kind === "chr" ? prefs.defPorts : p.default_ports - 1);
    const body = { kind: p.kind, role, name: name.value.trim(), ...at, ports: p.kind === "chr" ? n : n + 1 };
    if (cpus.value) body.cpus = Number(cpus.value);
    if (ram.value) body.ram_mb = Number(ram.value);
    const node = await api("POST", `${labPath()}/nodes`, body);
    state.sel = { type: "node", id: node.id };
  });
  name.focus();
}

// ------------------------------------------------- images (experimental platforms)

function sizeText(bytes) {
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`;
}

function imagesDialog(kind, reason) {
  const p = platformInfo(kind);
  if (!p) { toast("This server does not know that platform.", true); return; }
  const status = h("p", {});
  const progress = h("progress", { max: 100, value: 0, hidden: true, style: "width:100%" });
  const file = h("input", { id: "m-image", type: "file", accept: ".zip,.ova,.vmdk,.qcow2" });
  const showStatus = (info) => {
    status.textContent = info.image
      ? `Installed${info.version ? `: version ${info.version}` : ""}, ${sizeText(info.image_size)}.`
      : "Not installed yet.";
  };
  showStatus(p);
  openModal(`${p.label} image`, [
    reason && { node: h("p", {}, reason) },
    { node: status },
    { node: h("p", {}, p.image_help) },
    { node: h("div", { class: "field" }, h("label", { for: "m-image" }, p.image ? "Replace it with" : "Image file"), file) },
    p.image && { node: h("p", { class: "warn-text" }, `Replacing the image resets every ${p.label} device to factory defaults. Stop them first.`) },
    { node: progress },
  ].filter(Boolean), "Upload", async () => {
    const f = file.files && file.files[0];
    if (!f) throw new Error("Choose the file you downloaded first.");
    progress.hidden = false;
    status.textContent = "Uploading";
    await new Promise((ok, fail) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/images/${kind}?filename=${encodeURIComponent(f.name)}`);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        progress.value = Math.round((e.loaded / e.total) * 100);
        status.textContent = `Uploading: ${sizeText(e.loaded)} of ${sizeText(e.total)}`;
      };
      xhr.onload = () => {
        if (xhr.status === 401) { location.replace("/login"); return; }
        if (xhr.status >= 200 && xhr.status < 300) { ok(); return; }
        let msg = `Upload failed (${xhr.status})`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (e) { /* keep the status */ }
        fail(new Error(msg));
      };
      xhr.onerror = () => fail(new Error("The upload was interrupted."));
      xhr.send(f);
    });
    progress.removeAttribute("value"); // indeterminate while the server converts
    status.textContent = "Unpacking and converting the disk. This takes a minute or two.";
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const info = (await api("GET", "/images")).find((x) => x.kind === kind);
      const st = info.import || {};
      if (st.state === "done") {
        progress.hidden = true;
        showStatus(info);
        toast(st.message);
        state.sys = await api("GET", "/system");
        return false; // close
      }
      if (st.state === "error") { progress.hidden = true; showStatus(info); throw new Error(st.message); }
    }
  });
}

// What a client runs, in the words shown in the inspector and on hover.
const SERVICES = [
  ["web", "Web server", "Serves a small page on port 80. Try curl from another client."],
  ["iperf", "iperf3 server", "Target for throughput tests from other clients."],
  ["dhcp", "DHCP server", "Hands out addresses .100 to .199 of its subnet, with its gateway and DNS."],
  ["dns", "DNS server", "Answers for every device in the lab as <name>.lab and forwards the rest."],
];
function clientAddress(n) {
  if (n.addressing === "dhcp") return n.leased || (n.running ? "DHCP…" : "DHCP");
  return n.address || "";
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
    saveBlob(await res.blob(), `${n.name}.txt`);
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
    { node: h("p", {}, "Uploads an exported config (.txt or .rsc) and runs /import on the router. It is applied on top of the current config.") },
    { key: "file", label: "Config file (.txt or .rsc)", type: "file", accept: ".txt,.rsc" },
    ...credFields(n),
    { node: out },
  ], "Import", async (v) => {
    if (!v.file) throw new Error("Choose a config file first.");
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
  const fold = () => { state.collapsed = !state.collapsed; renderInspector(true); };
  return h("div", { class: "head" }, h("h3", {}, title),
    h("button", { type: "button", "aria-label": state.collapsed ? "Show details" : "Hide details",
      title: state.collapsed ? "Show details" : "Hide details", "aria-expanded": String(!state.collapsed), onclick: fold },
      icon(state.collapsed ? "expand" : "collapse")),
    h("button", { type: "button", "aria-label": "Close details", title: "Close", onclick: () => select(null) }, icon("close")));
}

function renderInspector(force) {
  const box = $("#inspector");
  const active = document.activeElement;
  if (!force && active && box.contains(active) && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
  const lab = state.lab, sel = state.sel;
  box.hidden = !(lab && sel);
  box.classList.toggle("collapsed", state.collapsed);
  if (box.hidden) { box.replaceChildren(); return; }
  // Sections that do not apply come back as false; leave them out rather than print them.
  const fill = (items) => box.replaceChildren(...items.filter((item) => item !== false && item != null));
  if (sel.type === "mgmt") fill(mgmtInspector());
  else if (sel.type === "link") fill(linkInspector(lab.links.find((l) => l.id === sel.id)));
  else if (sel.type === "anno") fill(annoInspector(annoById(sel.id)));
  else fill(nodeInspector(nodeById(sel.id)));
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
            h("button", { type: "button", onclick: () => openPackets(live[0], live[1]) }, "Open packet view"),
            h("p", {}, h("a", { href: captureUrl(live[0], live[1], 30), download: "" }, "Download a 30 second capture")),
            h("p", {}, "Or watch it live in Wireshark:"),
            copyRow(`curl -sN ${state.user ? `-u ${state.user} ` : ""}${location.origin}${captureUrl(live[0], live[1])} | wireshark -k -i -`),
          ]
        : h("p", {}, "Start a device on this link to capture its traffic.")),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, "Delete link")),
  ];
}

function styleSelect(a, key, label, options) {
  const id = "i-" + key;
  const numeric = typeof options[0][0] === "number";
  const sel = h("select", { id, onchange: (e) => act(() => api("PATCH", `${labPath()}/annotations/${a.id}`,
    { [key]: numeric ? Number(e.target.value) : e.target.value })) },
    options.map(([value, text]) => h("option", { value }, text)));
  // A value set through the API that is not in the list is still shown.
  if (!options.some(([value]) => value === a[key])) sel.append(h("option", { value: a[key] }, String(a[key])));
  sel.value = String(a[key]);
  return h("div", { class: "field" }, h("label", { for: id }, label), sel);
}

let textEdit = null; // typing not saved yet: {id, path, text, timer}

function flushText() {
  if (!textEdit) return;
  const edit = textEdit;
  textEdit = null;
  clearTimeout(edit.timer);
  api("PATCH", edit.path, { text: edit.text }).catch((err) => toast(err.message, true));
}

function annoInspector(a) {
  const path = `${labPath()}/annotations/${a.id}`;
  const area = a.kind === "rect" || a.kind === "ellipse";
  // The canvas follows every keystroke; saving waits for a short pause in typing.
  const text = h("textarea", { id: "i-text", rows: area || a.kind === "text" ? 2 : 6, maxlength: 2000,
    oninput: (e) => {
      const live = annoById(a.id);
      if (live) live.text = e.target.value;
      if (textEdit) clearTimeout(textEdit.timer);
      textEdit = { id: a.id, path, text: e.target.value, timer: setTimeout(flushText, 500) };
      renderCanvas();
    },
    onblur: () => flushText() }, a.text);
  return [
    head(KIND_NAME[a.kind]),
    h("div", { class: "status" }, area ? "Marks a group of devices, such as a site or an OSPF area." : "Shown on the canvas for everyone using this lab."),
    h("div", { class: "field" }, h("label", { for: "i-text" }, area ? "Label" : "Text"), text),
    h("section", {}, h("h2", {}, "Colour"),
      h("div", { class: "swatches" }, COLORS.map((c) =>
        h("button", { type: "button", class: `c-${c}` + (a.color === c ? " on" : ""), "aria-label": c, title: c,
          "aria-pressed": String(a.color === c),
          onclick: () => act(() => api("PATCH", path, { color: c })) })))),
    h("section", {}, h("h2", {}, area ? "Style" : "Size"),
      area && styleSelect(a, "fill", "Fill", [["none", "None"], ["light", "Light"], ["medium", "Medium"], ["strong", "Strong"]]),
      area && styleSelect(a, "border", "Border", [["dashed", "Dashed"], ["solid", "Solid"], ["dotted", "Dotted"], ["none", "None"]]),
      area && styleSelect(a, "border_width", "Border thickness", [[1, "Thin"], [1.5, "Normal"], [3, "Thick"], [5, "Heavy"]]),
      a.kind === "rect" && styleSelect(a, "radius", "Corners", [[0, "Square"], [12, "Rounded"], [28, "Very round"]]),
      area && styleSelect(a, "label_pos", "Label position", [["auto", "Default"], ["top-left", "Top left"], ["top", "Top centre"], ["center", "Centre"], ["bottom-left", "Bottom left"], ["bottom", "Bottom centre"]]),
      styleSelect(a, "label_size", area ? "Label size" : "Text size", [["s", "Small"], ["m", "Normal"], ["l", "Large"], ["xl", "Extra large"]])),
    h("p", {}, "Drag it to move. Drag the corner handle to resize."),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, `Delete ${KIND_NAME[a.kind].toLowerCase()}`)),
  ];
}

function nodeInspector(n) {
  const isChr = n.kind === "chr";
  const isCx = n.kind === "aoscx";
  const cxInfo = isCx ? platformInfo("aoscx") : null;
  const inputs = {};
  const field = (key, label, type, extra) => {
    const id = "i-" + key;
    inputs[key] = h("input", { id, type, value: n[key] ?? "", disabled: n.running, ...extra });
    return h("div", { class: "field" }, h("label", { for: id }, label), inputs[key]);
  };
  const svcBoxes = {};
  const save = () => act(async () => {
    const body = {};
    const dhcp = inputs.addressing && inputs.addressing.value === "dhcp";
    for (const [k, input] of Object.entries(inputs)) {
      if (dhcp && (k === "address" || k === "gateway" || k === "dns")) continue;  // kept for switching back
      const raw = input.value.trim();
      const value = input.type === "number" ? Number(raw) : (raw || null);
      if (value !== n[k] && !(input.type === "number" && !raw)) body[k] = value;
    }
    if (Object.keys(body).length) await api("PATCH", `${labPath()}/nodes/${n.id}`, body);
  });
  const path = `${labPath()}/nodes/${n.id}`;
  const reachable = n.running && !!n.cloud_port;
  const clientFields = () => {
    const mode = h("select", { id: "i-addressing", disabled: n.running },
      h("option", { value: "static" }, "Fixed address"), h("option", { value: "dhcp" }, "DHCP"));
    mode.value = n.addressing || "static";
    inputs.addressing = mode;
    const fixed = [field("address", "IP address", "text", { placeholder: "192.168.10.2/24" }),
                   field("gateway", "Gateway", "text", { placeholder: "192.168.10.1" }),
                   field("dns", "DNS servers", "text", { placeholder: "1.1.1.1, 9.9.9.9" })];
    const lease = h("p", { class: "lease" });
    const sync = () => {
      const dhcp = mode.value === "dhcp";
      for (const k of ["address", "gateway", "dns"]) inputs[k].disabled = n.running || dhcp;
      for (const f of fixed) f.hidden = dhcp;
      lease.hidden = !dhcp;
      lease.textContent = !n.running ? "Asks for an address, gateway and DNS when it starts."
        : n.leased ? `Leased ${n.leased}.` : "Waiting for a DHCP server to answer.";
    };
    mode.addEventListener("change", sync);
    sync();
    return [h("div", { class: "field" }, h("label", { for: "i-addressing" }, "Addressing"), mode), lease, ...fixed];
  };

  const rows = [];
  for (let p = 1; p <= n.ports; p++) {
    rows.push(h("tr", {},
      h("td", { class: "mono" }, portName(n, p)),
      h("td", {}, peerText(n, p)),
      h("td", {}, n.running && [
        h("a", { href: "#", title: "Watch packets on this port", onclick: (e) => { e.preventDefault(); openPackets(n, p); } }, "Packets"),
        " · ",
        h("a", { href: captureUrl(n, p, 30), download: "", title: "Download a 30 second pcap for Wireshark" }, "pcap")])));
  }

  const access = [];
  if (n.mgmt_ip && (isChr || isCx)) {
    access.push(copyRow(n.ssh), h("p", {}, h("a", { href: `ssh://admin@${n.mgmt_ip}` }, "Open in your SSH client"),
      isCx ? [". The web interface is at ", h("a", { href: `https://${n.mgmt_ip}`, target: "_blank", rel: "noopener" }, `https://${n.mgmt_ip}`), "."]
        : ". WinBox and MobaXterm can connect to the same address."));
  } else if (n.mgmt_ip) {
    access.push(copyRow(n.mgmt_ip));
  } else if (n.cloud_port) {
    access.push(h("p", {}, n.running
      ? `Waiting to see which address ${portName(n, n.cloud_port)} was given. You can also check on the console${isChr ? " with /ip address print" : isCx ? " with show interface mgmt" : ""}.`
      : "Start the device to get an address."));
  } else {
    access.push(h("p", {}, "Link a port to the Cloud to give this device internet and reach it from your PC."));
  }

  return [
    head(n.name),
    h("div", { class: "status" + (n.running ? " running" : "") }, h("i", {}),
      (n.running ? "Running" : "Stopped") + " · " + (isVm(n)
        ? `${roleName(n)} · ${(platformInfo(n.kind) || {}).label || n.kind}${isCx ? " (experimental)" : ""}`
        : "Linux client")),
    isCx && n.running && h("p", {}, `It takes a few minutes to boot. Log in as ${cxInfo ? cxInfo.login : "admin"}.`),
    h("div", { class: "row" },
      n.running
        ? h("button", { type: "button", onclick: () => act(() => api("POST", path + "/stop")) }, "Stop")
        : h("button", { type: "button", class: "primary", onclick: () => act(() => api("POST", path + "/start")) }, "Start"),
      h("button", { type: "button", disabled: !n.running, onclick: () => openConsole(n) }, "Console")),
    n.kind === "client" && dockerSection(n),
    h("section", {},
      h("h2", {}, "Settings"),
      field("name", "Hostname", "text"),
      isChr
        ? h("div", { class: "grid3" },
            field("ports", "Ports", "number", { min: 1, max: 24 }),
            field("cpus", "vCPUs", "number", { min: 1, max: 8 }),
            field("ram_mb", "RAM (MB)", "number", { min: 128 }))
        : isCx
        ? h("div", { class: "grid3" },
            field("ports", "Ports + mgmt", "number", { min: 2, max: cxInfo ? cxInfo.max_ports : 17 }),
            field("cpus", "vCPUs", "number", { min: 1, max: 8 }),
            field("ram_mb", "RAM (MB)", "number", { min: cxInfo ? cxInfo.min_ram_mb : 2048 }))
        : clientFields(),
      n.running
        ? h("p", {}, "Stop the device to change its settings.")
        : h("button", { type: "button", onclick: save }, "Save changes")),
    n.kind === "client" && h("section", {}, h("h2", {}, "Services"),
      SERVICES.map(([key, label, tip]) => {
        svcBoxes[key] = h("input", { id: "i-svc-" + key, type: "checkbox", checked: (n.services || []).includes(key),
          disabled: n.running, onchange: () => act(() => api("PATCH", path,
            { services: Object.keys(svcBoxes).filter((k) => svcBoxes[k].checked) })) });
        return h("div", { class: "field check svc" }, svcBoxes[key],
          h("label", { for: "i-svc-" + key }, h("strong", {}, label), h("span", {}, tip)));
      }),
      n.running && h("p", {}, "Stop the client to change what it runs.")),
    h("section", {}, h("h2", {}, "Interfaces"), h("table", {}, h("tbody", {}, rows))),
    h("section", {}, h("h2", {}, "Access from your PC"), access),
    isChr && h("section", {}, h("h2", {}, "Configuration"),
      h("div", { class: "row" },
        h("button", { type: "button", disabled: !reachable, onclick: () => exportConfig(n) }, "Export"),
        h("button", { type: "button", disabled: !reachable, onclick: () => importConfig(n) }, "Import"),
        h("button", { type: "button", disabled: !reachable, onclick: () => pushIdentity(n) }, "Push hostname")),
      !reachable && h("p", {}, "Export, Import and Push hostname need the router running and linked to the Cloud. Backups only need it running."),
      h("p", {}, backupLine(n)),
      h("div", { class: "row" },
        h("button", { type: "button", disabled: !n.running, onclick: () => backupNow(n) }, "Back up now"),
        h("button", { type: "button", onclick: () => revisionsDialog(n.id) }, `Revisions (${n.backup.revisions})`)),
      state.lab.backup.scope === "selected" && h("div", { class: "field check", style: "margin-top:8px" },
        h("input", { id: "i-bk", type: "checkbox", checked: n.backup.included,
          onchange: (e) => act(() => api("PUT", `${path}/backup`, { included: e.target.checked })) }),
        h("label", { for: "i-bk" }, "Include in automatic backups")),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { type: "button", disabled: n.running, onclick: () => {
          if (!confirm(`Reset ${n.name} to factory defaults? Its configuration is erased.`)) return;
          act(async () => { await api("POST", path + "/reset"); toast(`${n.name} will boot with factory defaults.`); });
        } }, "Factory reset"))),
    isCx && h("section", {}, h("h2", {}, "Configuration"),
      h("p", {}, "Config export, backups and restore are MikroTik-only for now. Use checkpoints or copy running-config on the console."),
      h("div", { class: "row" },
        h("button", { type: "button", disabled: n.running, onclick: () => {
          if (!confirm(`Reset ${n.name} to factory defaults? Its configuration is erased.`)) return;
          act(async () => { await api("POST", path + "/reset"); toast(`${n.name} will boot with factory defaults.`); });
        } }, "Factory reset"))),
    h("section", {},
      h("div", { class: "row" },
        h("button", { type: "button", onclick: () => duplicateDialog(n) }, "Duplicate"),
        h("button", { type: "button", class: "danger", onclick: () => deleteSelected() }, "Delete device"))),
  ];
}

function duplicateDialog(n) {
  const isChr = isVm(n);
  const stem = n.name.replace(/\d+$/, "");
  const what = n.kind === "aoscx" || n.role === "switch" ? "switch" : n.kind === "chr" ? "router" : "client";
  const fields = [
    { node: h("p", {}, `Makes a new ${what} with the same settings as ${n.name}. Links are not copied.`) },
    { key: "name", label: "Hostname for the copy", value: stem !== n.name ? nextName(stem) : nextName(n.name + "-") },
  ];
  if (isChr && !n.running) fields.push({ key: "copy_config", label: `Also copy ${n.name}'s saved configuration`, type: "checkbox", value: false });
  if (isChr && n.running) fields.push({ node: h("p", {}, `The copy starts with factory defaults. To copy the configuration too, stop ${n.name} first.`) });
  openModal(`Duplicate ${n.name}`, fields, "Duplicate", async (v) => {
    const node = await api("POST", `${labPath()}/nodes/${n.id}/duplicate`, { name: v.name, copy_config: !!v.copy_config });
    state.sel = { type: "node", id: node.id };
  });
}

// ---------------------------------------------------------------- Docker on a PC
//
// Each Linux PC can run its own Docker engine (docker.py). The inspector lists its
// containers, kept fresh while the PC is selected; containers added here are saved with
// the lab. Ports a container publishes open in your browser through a link (weblinks.py).

const dockerCache = new Map();  // node id -> { data, at, loading }

function dockerData(n) {
  const c = dockerCache.get(n.id);
  if (!c || Date.now() - c.at > 2500 || c.running !== n.running) refreshDocker(n);
  return c && c.data;
}

async function refreshDocker(n) {
  const c = dockerCache.get(n.id) || { at: 0 };
  if (c.loading) return;
  c.loading = true;
  dockerCache.set(n.id, c);
  const running = n.running;
  try {
    c.data = await api("GET", `${labPath()}/nodes/${n.id}/containers`);
    c.running = running;
  } catch (e) { /* shown next time */ }
  c.at = Date.now();
  c.loading = false;
  if (state.sel && state.sel.type === "node" && state.sel.id === n.id) renderInspector();
}

function setDockerData(n, data) {
  dockerCache.set(n.id, { data, at: Date.now(), running: n.running });
  renderInspector(true);
}

// Keep the list fresh while a running PC with Docker is selected.
setInterval(() => {
  const n = state.sel && state.sel.type === "node" && nodeById(state.sel.id);
  if (n && n.kind === "client" && n.docker && n.running && !document.hidden) refreshDocker(n);
}, 3000);

const ICON_PATHS = {
  logs: "M5 6h14 M5 10h14 M5 14h9 M5 18h6",
  play: "M8 5l11 7-11 7z",
  stop: "M7 7h10v10H7z",
  trash: "M4 7h16 M9 7V4h6v3 M6 7l1 13h10l1-13",
  out: "M14 4h6v6 M20 4l-9 9 M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  plus: "M12 5v14 M5 12h14",
  cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M12 12l8-4.5 M12 12v9 M12 12L4 7.5",
  copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  check: "M5 12.5l4.5 4.5L19 7.5",
  reload: "M19 12a7 7 0 1 1-2.05-4.95 M19 4v4h-4",
  tall: "M12 4v16 M8 8l4-4 4 4 M8 16l4 4 4-4",
};
const svgIcon = (name, size = 15) => s("svg", { width: size, height: size, viewBox: "0 0 24 24", "aria-hidden": "true", class: "ico" }, s("path", { d: ICON_PATHS[name] }));

function dockerSection(n) {
  const path = `${labPath()}/nodes/${n.id}`;
  const sys = state.sys || {};
  const d = n.docker ? dockerData(n) : null;
  const eng = d && d.engine;
  const rows = (d && d.containers) || [];

  const toggle = (on) => act(async () => {
    const live = rows.filter((r) => r.running).length;
    if (!on && live && !confirm(`Turn off Docker on ${n.name}? Its ${live} running container${live > 1 ? "s stop" : " stops"}. Saved containers come back when you turn it on again.`)) {
      renderInspector(true);
      return;
    }
    setDockerData(n, await api("POST", `${path}/docker`, { on }));
  });

  let label = "Off";
  if (n.docker) {
    const st = n.running ? (eng ? eng.state : "starting") : "stopped";
    const run = rows.filter((r) => r.running).length;
    label = st === "running" ? `Docker ${eng.version || ""} · ${run} of ${rows.length} running`.replace("  ", " ")
      : st === "starting" ? "Starting…" : st === "failed" ? "Did not start" : n.running ? "On" : `On · starts with ${n.name}`;
  }
  const head = h("div", { class: "docker-head" },
    h("h2", {}, "Docker"),
    sys.docker !== false && h("label", { class: "switch", for: "i-docker" },
      h("span", { class: "switch-text" }, label),
      h("input", { id: "i-docker", type: "checkbox", role: "switch", checked: !!n.docker, onchange: (e) => toggle(e.target.checked) }),
      h("span", { class: "track", "aria-hidden": "true" })));

  if (sys.docker === false) {
    return h("section", { class: "docker" }, head,
      h("div", { class: "callout" },
        h("strong", {}, "Docker isn't installed on this server yet"),
        h("span", {}, "Run the installer again to add it. Labs and devices are kept."),
        copyRow("sudo ./scripts/install.sh")));
  }
  if (!n.docker) {
    return h("section", { class: "docker" }, head,
      h("p", {}, `Gives ${n.name} its own Docker engine: run containers from here or with `, h("code", {}, "docker"),
        ` in its console, with their own networks and ports published on ${n.name}'s address. Uses about 100 MB of memory while ${n.name} runs.`));
  }

  const engineOk = eng && eng.state === "running";
  const list = rows.map((r) => {
    const tcp = r.ports.filter((p) => p.proto === "tcp");
    const status = r.error ? h("span", { class: "ctr-error" }, r.error) : h("span", {}, r.busy || r.status);
    const actions = [];
    if (engineOk && r.state !== "pending") {
      actions.push(h("button", { type: "button", class: "icon-btn", title: "Logs", "aria-label": `Logs of ${r.name}`, onclick: () => openContainerLogs(n, r.name) }, svgIcon("logs")));
      actions.push(h("button", { type: "button", class: "icon-btn", title: r.running ? "Stop" : "Start", "aria-label": `${r.running ? "Stop" : "Start"} ${r.name}`,
        onclick: () => act(async () => setDockerData(n, await api("POST", `${path}/containers/${encodeURIComponent(r.name)}/${r.running ? "stop" : "start"}`))) },
        svgIcon(r.running ? "stop" : "play")));
    }
    actions.push(h("button", { type: "button", class: "icon-btn", title: "Remove", "aria-label": `Remove ${r.name}`, onclick: () => act(async () => {
      if (!confirm(`Remove the container ${r.name} from ${n.name}?` + (r.saved ? " It is also taken out of the lab." : ""))) return;
      setDockerData(n, await api("DELETE", `${path}/containers/${encodeURIComponent(r.name)}`));
    }) }, svgIcon("trash")));
    return h("div", { class: "ctr" },
      h("div", { class: "ctr-top" },
        h("i", { class: "ctr-dot" + (r.running ? " on" : r.error ? " bad" : "") }),
        h("strong", {}, r.name),
        h("span", { class: "ctr-image mono", title: r.image }, r.image),
        ...actions),
      h("div", { class: "ctr-meta" },
        status,
        h("span", { class: "spacer" }),
        ...r.ports.map((p) => h("span", { class: "chip mono", title: `${n.name} port ${p.host} leads to port ${p.container} in the container` },
          `${p.host} → ${p.container}${p.proto === "udp" ? "/udp" : ""}`)),
        r.running && n.running && tcp.length > 0 && h("span", { class: "open-pair" },
          h("button", { type: "button", class: "open-btn", title: `View in a Mikrobox tab`,
            onclick: () => act(() => openViewer({ node: n.id, container: r.name, port: tcp[0].host })) }, "View"),
          h("button", { type: "button", class: "open-btn", title: "Open in your browser (makes a link)", "aria-label": `Open ${r.name} in your browser`,
            onclick: () => openWebDialog({ node: n.id, container: r.name, mode: "link" }) }, svgIcon("out", 12)))));
  });

  return h("section", { class: "docker" }, head,
    eng && eng.state === "failed" && h("p", { class: "ctr-error" }, eng.message || "The Docker engine did not start."),
    list.length ? h("div", { class: "ctr-list" }, list) : h("p", {}, n.running && !engineOk ? "Starting the Docker engine…" : "No containers yet."),
    h("button", { type: "button", class: "add-ctr", onclick: () => addContainerDialog(n) }, svgIcon("plus"), "Add container"),
    h("p", { class: "hint" }, "Containers you start with ", h("code", {}, "docker"), " in the console show up here too. ",
      n.running && h("a", { href: "#", onclick: (e) => { e.preventDefault(); openWebDialog({ node: n.id }); } }, `Open another web page from ${n.name}`)));
}

const CONTAINER_PRESETS = [
  { label: "nginx", what: "Web server", image: "nginx:alpine", name: "web", ports: [[8080, 80]] },
  { label: "Apache httpd", what: "Web server", image: "httpd:alpine", name: "apache", ports: [[8081, 80]] },
  { label: "whoami", what: "Echoes each request", image: "traefik/whoami", name: "whoami", ports: [[8082, 80]] },
  { label: "Portainer", what: "Docker in a web page, HTTPS", image: "portainer/portainer-ce", name: "portainer", ports: [[9443, 9443]], socket: true },
  { label: "Grafana", what: "Dashboards", image: "grafana/grafana", name: "grafana", ports: [[3000, 3000]] },
  { label: "Uptime Kuma", what: "Uptime monitor", image: "louislam/uptime-kuma:1", name: "uptime", ports: [[3001, 3001]] },
  { label: "PostgreSQL", what: "Database", image: "postgres:17", name: "db", ports: [[5432, 5432]], env: [["POSTGRES_PASSWORD", ""]] },
  { label: "Other image", what: "Any image by name", image: "", name: "", ports: [] },
];

function addContainerDialog(n) {
  const known = ((dockerCache.get(n.id) || {}).data || {}).containers || [];
  const taken = new Set(known.flatMap((r) => r.ports.map((p) => `${p.host}/${p.proto}`)));
  const names = new Set(known.map((r) => r.name));
  const freePort = (port, proto = "tcp") => { while (taken.has(`${port}/${proto}`)) port += 1; return port; };
  const freeName = (base) => { if (!base) return ""; let x = base, i = 2; while (names.has(x)) x = `${base}${i++}`; return x; };

  const image = h("input", { id: "c-image", type: "text", class: "mono", autocomplete: "off", spellcheck: "false", placeholder: "nginx:alpine" });
  const name = h("input", { id: "c-name", type: "text", autocomplete: "off", spellcheck: "false" });
  const ports = h("div", { class: "ports-grid" });
  const envs = h("div", { class: "env-grid" });
  const restart = h("input", { id: "c-restart", type: "checkbox", checked: true });
  const socket = h("input", { id: "c-socket", type: "checkbox" });
  const preview = h("code", { class: "run-preview" });
  const where = h("span", { class: "hint" });
  const addr = n.kind === "client" && (n.addressing === "dhcp" ? (n.leased || "").split("/")[0] : (n.address || "").split("/")[0]);

  const portRow = (host = "", cont = "", proto = "tcp") => {
    const hp = h("input", { type: "number", min: 1, max: 65535, value: host, "aria-label": `Port on ${n.name}` });
    const cp = h("input", { type: "number", min: 1, max: 65535, value: cont, "aria-label": "Port in the container" });
    const pr = h("select", { "aria-label": "Protocol" }, h("option", { value: "tcp" }, "TCP"), h("option", { value: "udp" }, "UDP"));
    pr.value = proto;
    const row = h("div", { class: "port-row" }, hp, h("span", { "aria-hidden": "true" }, "→"), cp, pr,
      h("button", { type: "button", class: "icon-btn", "aria-label": "Remove this port", onclick: () => { row.remove(); update(); } }, "×"));
    row.read = () => ({ host: Number(hp.value), container: Number(cp.value), proto: pr.value });
    return row;
  };
  const envRow = (k = "", v = "") => {
    const ki = h("input", { type: "text", value: k, class: "mono", placeholder: "NAME", "aria-label": "Variable name", autocomplete: "off" });
    const vi = h("input", { type: "text", value: v, placeholder: "value", "aria-label": "Value", autocomplete: "off" });
    const row = h("div", { class: "env-row" }, ki, vi,
      h("button", { type: "button", class: "icon-btn", "aria-label": "Remove this variable", onclick: () => { row.remove(); update(); } }, "×"));
    row.read = () => [ki.value.trim(), vi.value];
    return row;
  };
  const read = () => ({
    name: name.value.trim(), image: image.value.trim(),
    ports: [...ports.children].map((r) => r.read()).filter((p) => p.host || p.container),
    env: Object.fromEntries([...envs.children].map((r) => r.read()).filter(([k]) => k)),
    restart: restart.checked, docker_socket: socket.checked,
  });
  const update = () => {
    const v = read();
    const parts = ["docker run -d", v.name && `--name ${v.name}`,
      ...v.ports.map((p) => `-p ${p.host || "?"}:${p.container || "?"}${p.proto === "udp" ? "/udp" : ""}`),
      ...Object.entries(v.env).map(([k, val]) => `-e ${k}=${/\s/.test(val) ? JSON.stringify(val) : val}`),
      v.docker_socket && "-v /var/run/docker.sock:/var/run/docker.sock", v.restart && "--restart unless-stopped", v.image || "IMAGE"];
    preview.textContent = parts.filter(Boolean).join(" ");
    const first = v.ports.find((p) => p.host && p.proto === "tcp");
    where.textContent = first ? `Reachable in the lab at ${addr || n.name}:${first.host}` : "";
  };

  const presetBtns = CONTAINER_PRESETS.map((p) => {
    const b = h("button", { type: "button", class: "preset", "aria-pressed": "false", onclick: () => choose(p, b) },
      h("strong", {}, p.label), h("span", {}, p.what));
    return b;
  });
  const choose = (p, btn) => {
    for (const b of presetBtns) b.setAttribute("aria-pressed", String(b === btn));
    image.value = p.image;
    name.value = freeName(p.name);
    ports.replaceChildren(...p.ports.map(([hp, cp]) => portRow(freePort(hp), cp)));
    envs.replaceChildren(...(p.env || []).map(([k, v]) => envRow(k, v)));
    socket.checked = !!p.socket;
    update();
    (p.image ? name : image).focus();
  };
  const form = h("div", { class: "add-ctr-form", oninput: update, onchange: update },
    h("p", { class: "lead" }, `Runs on ${n.name}'s own Docker engine, on the lab network.`),
    h("div", { class: "field" }, h("span", { class: "label" }, "Start from"), h("div", { class: "presets" }, presetBtns)),
    h("div", { class: "grid2" },
      h("div", { class: "field" }, h("label", { for: "c-image" }, "Image"), image),
      h("div", { class: "field" }, h("label", { for: "c-name" }, "Name"), name)),
    h("div", { class: "field" }, h("span", { class: "label" }, "Published ports"),
      h("div", { class: "port-head", "aria-hidden": "true" }, h("span", {}, `On ${n.name}`), h("span", {}), h("span", {}, "In the container"), h("span", {}, "Protocol"), h("span", {})),
      ports,
      h("div", { class: "row-inline" },
        h("button", { type: "button", class: "small", onclick: () => { ports.append(portRow(freePort(8080))); update(); } }, "+ Add port"), where)),
    h("div", { class: "field" }, h("span", { class: "label" }, "Environment variables"), envs,
      h("div", { class: "row-inline" },
        h("button", { type: "button", class: "small", onclick: () => { envs.append(envRow()); update(); } }, "+ Add variable"),
        h("span", { class: "hint" }, "For example a database password"))),
    h("div", { class: "field check" }, restart, h("label", { for: "c-restart" }, `Start it again whenever ${n.name} starts`)),
    h("div", { class: "field check" }, socket, h("label", { for: "c-socket" }, `Give it ${n.name}'s Docker socket (for tools that manage Docker, like Portainer)`)),
    h("div", { class: "run-box" }, h("span", { class: "hint" }, `The same as typing this in ${n.name}'s console:`), preview),
    h("p", { class: "hint" }, "Images are downloaded through this server, and a PC that already has an image shares it with the others."));
  choose(CONTAINER_PRESETS[0], presetBtns[0]);
  openModal(`Add a container to ${n.name}`, [{ node: form }], "Add container", async () => {
    const v = read();
    if (!v.image) throw new Error("Enter an image, like nginx:alpine.");
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(v.name)) throw new Error("Give it a name: letters, digits, dots, dashes or underscores.");
    if (v.ports.some((p) => !(p.host >= 1 && p.host <= 65535 && p.container >= 1 && p.container <= 65535))) throw new Error("Ports are numbers from 1 to 65535.");
    if (Object.entries(v.env).some(([k, val]) => k === "POSTGRES_PASSWORD" && !val)) throw new Error("Set a password for POSTGRES_PASSWORD.");
    setDockerData(n, await api("POST", `${labPath()}/nodes/${n.id}/containers`, v));
    if (n.running) toast(`${v.name} is being set up on ${n.name}. The first time, the image is downloaded.`);
  }, null, true);
  setTimeout(() => image.focus(), 0);
}

function openContainerLogs(n, name) {
  const key = `l:${n.id}:${name}`;
  if (!dock.has(key)) {
    const out = h("pre", { class: "logs-out" }, "Loading…");
    const follow = h("input", { type: "checkbox", id: `lf-${n.id}-${name}`, checked: true });
    const status = h("span", { class: "pv-status" });
    let timer = null;
    const load = async () => {
      try {
        const r = await api("GET", `${labPath()}/nodes/${n.id}/containers/${encodeURIComponent(name)}/logs?tail=500`);
        const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
        out.textContent = r.text || "(no output yet)";
        if (stick) out.scrollTop = out.scrollHeight;
        status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      } catch (e) { status.textContent = e.message; }
    };
    const pane = h("div", { class: "pv logs" },
      h("div", { class: "pv-bar" }, h("strong", {}, `${n.name} · ${name}`),
        h("button", { type: "button", onclick: load }, "Refresh"),
        h("span", { class: "act-toggle" }, follow, h("label", { for: follow.id }, "Follow")), status),
      out);
    $("#terms").append(pane);
    timer = setInterval(() => { if (follow.checked && state.activeDock === key && !document.hidden) load(); }, 2000);
    dock.set(key, { name: `${n.name} · ${name} logs`, pane, onShow: null, close: () => clearInterval(timer) });
    load();
  }
  dockShow(key);
}

// ---------------------------------------------------------------- web pages in the lab

function openWebDialog(pre = {}) {
  const pcs = state.lab.nodes.filter((x) => x.kind === "client" && x.running);
  if (!pcs.length) { toast("Start a Linux client first: pages are opened from one."); return; }
  const from = h("select", { id: "w-from" }, pcs.map((x) => h("option", { value: x.id }, `${x.name}${clientAddress(x) ? " · " + clientAddress(x).split("/")[0] : ""}`)));
  if (pre.node && pcs.some((x) => x.id === pre.node)) from.value = String(pre.node);
  const targets = h("fieldset", { class: "targets" }, h("legend", {}, "What to open"));
  const url = h("input", { id: "w-url", type: "text", class: "mono", placeholder: "http://10.0.0.1  or  https://10.99.1.12", autocomplete: "off", spellcheck: "false" });
  const urlBox = h("div", { class: "field url-box" }, h("label", { for: "w-url" }, "Address, as the PC would type it"), url);
  const result = h("div", { class: "web-result", hidden: true });
  let pick = pre.container ? `c:${pre.container}` : "url";

  const fill = () => {
    const n = nodeById(Number(from.value));
    const rows = (((dockerCache.get(n.id) || {}).data || {}).containers || []).filter((r) => r.running && r.ports.some((p) => p.proto === "tcp"));
    if (n.docker && !dockerCache.get(n.id)) refreshDocker(n).then(() => fill());
    const opts = rows.flatMap((r) => r.ports.filter((p) => p.proto === "tcp").map((p) => ({
      key: `c:${r.name}:${p.host}`, title: `${r.name} · ${r.image.split("/").pop().split(":")[0]}`,
      detail: `Container on ${n.name}, port ${p.host}`, container: r.name, port: p.host })));
    if (pick.startsWith("c:") && !pick.split(":")[2]) { const o = opts.find((x) => x.container === pick.slice(2)); pick = o ? o.key : "url"; }
    if (pick !== "url" && !opts.some((o) => o.key === pick)) pick = opts.length ? opts[0].key : "url";
    opts.push({ key: "url", title: "Another address", detail: `Anything ${n.name} can reach, like a router's WebFig` });
    targets.replaceChildren(h("legend", {}, "What to open"), ...opts.map((o, i) => {
      const id = `w-t${i}`;
      const radio = h("input", { id, type: "radio", name: "w-target", checked: pick === o.key, onchange: () => { pick = o.key; fill(); } });
      radio.option = o;
      return h("label", { for: id, class: "target" + (pick === o.key ? " on" : "") }, radio,
        h("span", { class: "t-text" }, h("strong", {}, o.title), h("span", {}, o.detail)));
    }), urlBox);
    urlBox.hidden = pick !== "url";
    result.hidden = true;
    $("#modal-ok").hidden = false;
  };
  const me = h("input", { id: "w-me", type: "radio", name: "w-who", checked: true });
  const anyone = h("input", { id: "w-any", type: "radio", name: "w-who" });
  const inTab = h("input", { id: "w-tab", type: "radio", name: "w-how", checked: pre.mode !== "link" });
  const inBrowser = h("input", { id: "w-browser", type: "radio", name: "w-how", checked: pre.mode === "link" });
  const who = h("fieldset", { class: "who-can" }, h("legend", {}, "Who can use the link"),
    h("div", { class: "field check" }, me, h("label", { for: "w-me" }, "Only this computer")),
    h("div", { class: "field check" }, anyone, h("label", { for: "w-any" }, "Anyone who can reach Mikrobox")));
  const how = () => {
    who.hidden = inTab.checked;
    $("#modal-ok").textContent = inTab.checked ? "Open tab" : "Make link";
    result.hidden = true;
    $("#modal-ok").hidden = false;
  };
  for (const r of [inTab, inBrowser]) r.addEventListener("change", how);
  const form = h("div", { class: "web-form" },
    h("p", { class: "lead" }, "Reach a web page inside the lab from this computer, over HTTP or HTTPS."),
    h("div", { class: "field" }, h("label", { for: "w-from" }, "From"), from,
      h("span", { class: "hint" }, "The page is fetched the way that PC sees it, through the lab's own links.")),
    targets,
    h("fieldset", { class: "who-can" }, h("legend", {}, "Open it"),
      h("div", { class: "field check" }, inTab, h("label", { for: "w-tab" }, "In a Mikrobox tab, next to the consoles")),
      h("div", { class: "field check" }, inBrowser, h("label", { for: "w-browser" }, "In your browser, with a link you can share"))),
    who,
    result,
    h("p", { class: "hint" }, "Links stay listed under Tools › Web access, and close when their PC stops."));
  from.addEventListener("change", () => { pick = "url"; fill(); });
  for (const r of [me, anyone]) r.addEventListener("change", () => { result.hidden = true; $("#modal-ok").hidden = false; });

  openModal("Open a web page in the lab", [{ node: form }], "Open tab", async () => {
    const node = Number(from.value);
    const chosen = [...targets.querySelectorAll("input[type=radio]")].find((r) => r.checked);
    const body = { lab: state.lab.id, node, who: anyone.checked ? "anyone" : "me" };
    if (!chosen || chosen.option.key === "url") {
      if (!url.value.trim()) throw new Error("Enter the address to open.");
      body.url = url.value.trim();
    } else {
      body.container = chosen.option.container;
      body.port = chosen.option.port;
    }
    if (inTab.checked) {
      await openViewer(body);
      return false;  // done: the tab is open
    }
    const link = await api("POST", "/web", body);
    const https = link.scheme === "https";
    result.replaceChildren(
      h("div", { class: "ok-line" }, svgIcon("check", 16), "Ready"),
      h("div", { class: "url-row" },
        h("code", { class: "mono" }, link.url),
        h("button", { type: "button", onclick: () => copyText(link.url) }, "Copy"),
        h("a", { class: "button primary", href: link.url, target: "_blank", rel: "noopener" }, "Open", svgIcon("out", 13))),
      h("span", { class: "hint" }, (https
        ? "HTTPS passes straight through, so you see the page's own certificate. A self-signed one gets a browser warning; that is expected."
        : "Plain HTTP, exactly as the page serves it.")
        + (link.who === "me" ? ` Only ${link.allow} can use this link.` : " Anyone who can reach this server can use this link.")));
    result.hidden = false;
    $("#modal-ok").hidden = true;
    $("#modal-cancel").textContent = "Close";
    if (dock.has("web")) loadWeb();
    return true;  // keep the dialog open to show the link
  });
  fill();
  how();
}

// ---------------------------------------------------------------- the viewer tab (test build)
//
// A lab web page in a tab of the bottom panel. It goes through a "view" link (weblinks.py),
// which makes the page fit for a frame; HTTPS pages are fetched without checking their
// certificate, which the tab says.

const viewers = new Map();  // dock key -> { link, frame, addr, overlay }

async function openViewer(target) {
  const link = await api("POST", "/web", { ...target, lab: state.lab.id, mode: "view", who: "me" });
  const key = `v:${link.node}:${link.id}`;
  const v = viewers.get(key);
  if (v) {
    v.link = link;
    v.go(link.path);
  } else {
    buildViewer(key, link, target);
  }
  if (readStore("dockHeight", null) === null) setDockHeight(Math.max(380, window.innerHeight * 0.5));
  dockShow(key);
}

function buildViewer(key, link, target) {
  const frame = h("iframe", { class: "vw-frame", title: link.title, referrerpolicy: "no-referrer-when-downgrade",
    sandbox: "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads" });
  const addr = h("input", { type: "text", class: "vw-addr mono", spellcheck: "false", autocomplete: "off",
    "aria-label": `Address, as ${link.node_name} sees it` });
  const overlay = h("div", { class: "vw-overlay", hidden: true });
  const secure = link.scheme === "https";
  const v = { link, frame, addr, overlay, target: { node: target.node, container: target.container, port: target.port, url: target.url } };
  v.go = (path) => {
    overlay.hidden = true;
    addr.value = v.link.target + path;
    frame.src = v.last = v.link.base + path;
  };
  const navigate = () => act(async () => {
    const text = addr.value.trim();
    const full = /^https?:\/\//i.test(text) ? text : `http://${text}`;
    let u;
    try { u = new URL(full); } catch (e) { throw new Error("Enter an address like http://10.0.0.1/ or https://10.0.0.1:8443/."); }
    const here = new URL(v.link.target);
    if (u.protocol === here.protocol && u.host === here.host) {
      v.go(u.pathname + u.search + u.hash);
    } else {
      await openViewer({ node: v.link.node, url: full });
    }
  });
  addr.addEventListener("keydown", (e) => { if (e.key === "Enter") navigate(); });
  const bar = h("div", { class: "vw-bar" },
    h("button", { type: "button", class: "icon-btn", title: "Reload", "aria-label": "Reload", onclick: () => { if (overlay.hidden) frame.src = v.last; } }, svgIcon("reload")),
    h("span", { class: `vw-scheme ${secure ? "https" : "http"}`, title: secure
      ? `${link.node_name} reaches this page over HTTPS. Mikrobox does not check its certificate, so a self-signed one works here without a warning.`
      : "Plain HTTP" }, secure ? "HTTPS · certificate not checked" : "HTTP"),
    addr,
    h("span", { class: "vw-from" }, `from ${link.node_name}`),
    h("button", { type: "button", class: "icon-btn", title: "Open in a browser tab", "aria-label": "Open in a browser tab",
      onclick: () => window.open(v.last, "_blank", "noopener") }, svgIcon("out")),
    h("button", { type: "button", class: "icon-btn", title: "Make the panel taller", "aria-label": "Make the panel taller",
      onclick: () => setDockHeight(window.innerHeight * 0.8) }, svgIcon("tall")));
  const pane = h("div", { class: "pv viewer" }, bar, h("div", { class: "vw-body" }, frame, overlay));
  $("#terms").append(pane);
  viewers.set(key, v);
  dock.set(key, { name: link.title, tip: `${link.target} from ${link.node_name}`, pane, onShow: null, close: () => {
    viewers.delete(key);
    // The link stays while another tab shows the same page.
    if (![...viewers.values()].some((o) => o.link.id === v.link.id)) api("DELETE", `/web/${v.link.id}`).catch(() => {});
  } });
  v.go(link.path);
}

// The frame's own page can't be read from here (another origin), but its link sees where it
// goes: the address bar and Reload follow that.
setInterval(async () => {
  const v = viewers.get(state.activeDock);
  if (!v || document.hidden || !v.overlay.hidden || document.activeElement === v.addr) return;
  let link;
  try { link = (await api("GET", `/web?lab=${v.link.lab}`)).find((l) => l.id === v.link.id); } catch (e) { return; }
  if (!link || document.activeElement === v.addr) return;
  v.addr.value = link.target + link.page;
  v.last = link.base + link.page;
}, 1500);

// A viewer's link closes when its PC stops: say so in the tab, with a way back.
async function checkViewers() {
  if (!viewers.size || !state.lab) return;
  let open, nodes;
  try {
    open = new Set((await api("GET", "/web")).map((l) => l.id));
    nodes = (await api("GET", labPath())).nodes;
  } catch (e) { return; }
  for (const [key, v] of viewers) {
    if (open.has(v.link.id) || !v.overlay.hidden) continue;
    const n = nodes.find((x) => x.id === v.link.node);
    v.frame.removeAttribute("src");
    v.overlay.replaceChildren(
      h("strong", {}, n && !n.running ? `${v.link.node_name} stopped` : "This page was closed"),
      h("span", {}, `Mikrobox reaches ${v.link.target} through ${v.link.node_name}.`),
      h("button", { type: "button", class: "primary", onclick: () => act(async () => {
        await openViewer(v.target);  // a new link, so a new tab
        dockClose(key);
      }) }, "Open again"));
    v.overlay.hidden = false;
  }
}

function copyText(text) {
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
    .then(() => toast("Copied."))
    .catch(() => { prompt("Copy this:", text); });
}

const webState = { pane: null, body: null };

function openWebAccess() {
  if (!dock.has("web")) {
    const body = h("div", { class: "web-body" });
    const pane = h("div", { class: "pv web" },
      h("div", { class: "web-top" },
        h("div", { class: "web-intro" }, h("strong", {}, "Web pages opened from the lab"),
          h("span", {}, "Each link leads to one page inside the lab. Links close when their PC stops, or after 8 hours without use.")),
        h("button", { type: "button", class: "primary", onclick: () => openWebDialog() }, svgIcon("plus"), "Open a web page")),
      body);
    $("#terms").append(pane);
    webState.pane = pane;
    webState.body = body;
    dock.set("web", { name: "Web access", pane, onShow: () => loadWeb(), close: () => { webState.pane = null; } });
  }
  dockShow("web");
  loadWeb();
}

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 45) return "Just now";
  if (s < 3600) return `${Math.round(s / 60)} minute${Math.round(s / 60) === 1 ? "" : "s"} ago`;
  const hr = Math.round(s / 3600);
  return `${hr} hour${hr === 1 ? "" : "s"} ago`;
}

async function loadWeb() {
  if (!webState.body || !state.lab) return;
  let links = [];
  try { links = await api("GET", `/web?lab=${state.lab.id}`); } catch (e) { webState.body.replaceChildren(h("p", {}, e.message)); return; }
  if (!links.length) {
    webState.body.replaceChildren(h("p", { class: "empty" }, "No links open in this lab. Open a container's page from its PC, or open any address the lab can reach."));
    return;
  }
  webState.body.replaceChildren(h("div", { class: "web-scroll" }, h("table", { class: "web-table" },
    h("thead", {}, h("tr", {}, ["Page", "From", "Link", "Who can use it", "Opened by", "Last used", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, links.map((l) => h("tr", {},
      h("td", {}, h("div", { class: "page" }, h("span", { class: `proto ${l.scheme}` }, l.scheme.toUpperCase()), h("strong", {}, l.title)),
        h("div", { class: "sub" }, l.detail)),
      h("td", {}, l.node_name),
      h("td", {}, h("div", { class: "url-cell" }, h("code", { class: "mono" }, l.url),
        h("button", { type: "button", class: "icon-btn", title: "Copy", "aria-label": `Copy the link to ${l.title}`, onclick: () => copyText(l.url) }, svgIcon("copy")),
        h("a", { class: "icon-btn", href: l.url, target: "_blank", rel: "noopener", title: "Open", "aria-label": `Open ${l.title}` }, svgIcon("out")))),
      h("td", { class: l.who === "anyone" ? "anyone" : "" }, l.mode === "view" ? `Viewer tab, only ${l.allow}` : l.who === "anyone" ? "Anyone who can reach Mikrobox" : `Only ${l.allow}`),
      h("td", {}, l.by || "someone"),
      h("td", { class: "sub" }, ago(l.used)),
      h("td", { class: "end" }, h("button", { type: "button", class: "small", "aria-label": `Close the link to ${l.title}`,
        onclick: () => act(async () => { await api("DELETE", `/web/${l.id}`); loadWeb(); }) }, "Close link"))))))));
}

function backupLine(n) {
  const b = state.lab.backup;
  let text = !b.enabled ? "Automatic backups are off for this lab."
    : n.backup.included ? `Backed up automatically every ${EVERY[b.interval]}.`
    : "Not included in automatic backups.";
  if (n.backup.last) text += ` Last attempt: ${n.backup.last.message.toLowerCase()}.`;
  return text;
}

function deleteSelected() {
  const sel = state.sel;
  if (!sel || sel.type === "mgmt") return;
  if (sel.type === "node") {
    const n = nodeById(sel.id);
    if (!confirm(`Delete ${n.name}? Its configuration is lost.`)) return;
    closeNodeTabs(n.id);
    act(async () => { await api("DELETE", `${labPath()}/nodes/${n.id}`); state.sel = null; });
  } else {
    const kind = sel.type === "link" ? "links" : "annotations";
    act(async () => { await api("DELETE", `${labPath()}/${kind}/${sel.id}`); state.sel = null; });
  }
}

// --------------------------------------------- dock: terminals, packets

// key -> {name, pane, onShow, close}. Keys: "c:<node>" terminal, "p:<node>:<port>" packets.
const dock = new Map();

function dockShow(key) {
  state.activeDock = key;
  renderDock();
}

function dockClose(key) {
  const d = dock.get(key);
  if (!d) return;
  d.close();
  d.pane.remove();
  dock.delete(key);
  if (state.activeDock === key) state.activeDock = dock.keys().next().value ?? null;
  renderDock();
}

const isNodeTab = (key, nodeId) => key === `c:${nodeId}` || [`p:${nodeId}:`, `t:${nodeId}:`, `l:${nodeId}:`, `v:${nodeId}:`].some((p) => key.startsWith(p));

function closeNodeTabs(nodeId) {
  for (const key of [...dock.keys()]) if (isNodeTab(key, nodeId)) dockClose(key);
}

// Selecting a device brings its console to the front of the panel, if it is open. A tab of
// that device already on show (its packets, a test) stays. Nothing new is opened.
function followDock(nodeId) {
  if (state.activeDock && isNodeTab(state.activeDock, nodeId)) return;
  if (dock.has(`c:${nodeId}`)) dockShow(`c:${nodeId}`);
}

function renderTabs() {
  $("#tabs").replaceChildren(...[...dock].map(([key, d]) =>
    h("div", { class: "tab" + (key === state.activeDock ? " on" : ""), title: d.tip || null },
      h("button", { type: "button", class: "name", onclick: () => dockShow(key) }, d.name + (d.badge || "")),
      h("button", { type: "button", "aria-label": `Close ${d.name}`, onclick: () => dockClose(key) }, "×"))));
}

function renderDock() {
  $("#dock").hidden = dock.size === 0;
  renderTabs();
  for (const [key, d] of dock) {
    d.pane.hidden = key !== state.activeDock;
    if (key === state.activeDock && d.onShow) requestAnimationFrame(d.onShow);
  }
}

function wsUrl(path) {
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api${path}`;
}

function openConsole(node) {
  const key = `c:${node.id}`;
  if (!dock.has(key)) {
    const pane = h("div", {});
    $("#terms").append(pane);
    $("#dock").hidden = false;
    const term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000 });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(pane);
    const ws = new WebSocket(wsUrl(`${labPath()}/nodes/${node.id}/console`));
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send("\r");
    ws.onmessage = (e) => {
      if (typeof e.data === "string" && e.data.charCodeAt(0) === 1) {
        // The console is shared: the server says who else has it open.
        const entry = dock.get(key), msg = JSON.parse(e.data.slice(1));
        if (entry && msg.viewers) {
          entry.badge = msg.viewers.length > 1 ? ` · ${msg.viewers.length} here` : "";
          entry.tip = msg.viewers.length > 1 ? `Shared console: ${msg.viewers.join(", ")}` : null;
          renderTabs();
        }
        return;
      }
      term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
    };
    ws.onclose = () => term.write("\r\n[disconnected]\r\n");
    term.onData((data) => { if (ws.readyState === 1) ws.send(data); });
    dock.set(key, {
      name: node.name, pane,
      onShow: () => { try { fit.fit(); term.focus(); } catch (e) { /* not visible yet */ } },
      close: () => { ws.close(); term.dispose(); },
    });
  }
  dockShow(key);
}

const PROTO_COLOR = {
  ICMP: "green", ICMPv6: "green", ARP: "amber", DHCP: "amber", OSPF: "purple", BGP: "purple",
  RIP: "purple", VRRP: "purple", BFD: "purple", LDP: "purple", PIM: "purple", IGMP: "purple",
  STP: "red", LLDP: "red", LACP: "red", MNDP: "red",
};

function openPackets(node, port) {
  const key = `p:${node.id}:${port}`;
  if (!dock.has(key)) {
    const title = `${node.name} ${portName(node, port)}`;
    const path = `${labPath()}/nodes/${node.id}/ports/${port}`;
    // gen counts connections: packet numbers from the server only mean something on the one that sent them.
    const st = { ws: null, gen: 0, paused: false, n: 0, picked: null, want: null, cache: new Map(), open: new Set(), closed: new Set() };
    const rows = h("tbody", {});
    const status = h("span", { class: "pv-status" }, "Connecting");
    const filter = h("input", { type: "text", placeholder: "Filter, e.g. icmp   or   port 179   or   not arp", "aria-label": "Capture filter" });
    const scroller = h("div", { class: "pv-scroll" },
      h("table", { class: "pv-table" },
        h("thead", {}, h("tr", {}, ["No.", "Time", "Source", "Destination", "Protocol", "Length", "Info"].map((t) => h("th", {}, t)))),
        rows));
    const pause = h("button", { type: "button", onclick: () => {
      st.paused = !st.paused;
      pause.textContent = st.paused ? "Resume" : "Pause";
      status.textContent = st.paused ? "Paused" : "Listening";
      // The server stops keeping new packets too, so everything on screen stays decodable.
      if (st.ws && st.ws.readyState === 1) st.ws.send(JSON.stringify({ pause: st.paused }));
    } }, "Pause");

    // ---- detail: protocol tree and bytes
    const sideHead = h("div", { class: "pv-side-head" });
    const raw = h("div", { class: "pv-raw mono" });
    const tree = h("div", { class: "pv-tree", role: "tree" });
    const hex = h("div", { class: "pv-hex mono" });
    const side = h("div", { class: "pv-side", hidden: true }, sideHead, raw, tree, hex);
    let bytes = [];  // [hex span, ascii span] per byte
    let marked = [];
    let current = null;  // the decoded packet on show
    const mark = (p, size) => {
      for (const el of marked) el.classList.remove("hl");
      marked = [];
      if (p === undefined) return;
      for (let i = p; i < Math.min(p + size, bytes.length); i++) for (const el of bytes[i]) { el.classList.add("hl"); marked.push(el); }
      const first = marked[0];
      if (first) {
        const top = first.offsetTop - hex.offsetTop;
        if (top < hex.scrollTop || top > hex.scrollTop + hex.clientHeight - 16) hex.scrollTop = top - 16;
      }
    };
    let chosen = null;
    const choose = (row, f) => {
      if (chosen) chosen.classList.remove("on");
      chosen = row;
      row.classList.add("on");
      mark(f.p, f.s);
    };
    const renderTree = (nodes, last) => nodes.map((f, i) => {
      const row = h("div", { class: "pv-f", tabindex: -1, title: f.n || "" }, f.t);
      row.addEventListener("click", (e) => { e.preventDefault(); choose(row, f); });
      row.addEventListener("mouseenter", () => { if (!chosen) mark(f.p, f.s); });
      row.addEventListener("mouseleave", () => { if (!chosen) mark(); });
      f.el = row;
      if (!f.c) return row;
      const byDefault = last && i === nodes.length - 1;  // the top layer, e.g. OSPF or BGP, starts open
      const box = h("details", { open: st.open.has(f.n) || (byDefault && !st.closed.has(f.n)) },
        h("summary", {}, row), h("div", { class: "pv-kids" }, renderTree(f.c, false)));
      box.addEventListener("toggle", () => {
        if (!f.n) return;
        if (box.open) { st.open.add(f.n); st.closed.delete(f.n); } else { st.closed.add(f.n); st.open.delete(f.n); }
      });
      f.box = box;
      return box;
    });
    // Clicking a byte selects the smallest field that covers it, opening its parents.
    const fieldAt = (nodes, i, trail = [], best = { size: Infinity, trail: null }) => {
      for (const f of nodes) {
        const here = [...trail, f];
        if (f.p !== undefined && i >= f.p && i < f.p + f.s && f.s <= best.size) best = { size: f.s, trail: here };
        if (f.c) best = fieldAt(f.c, i, here, best);
      }
      return best;
    };
    // Each byte takes 3 columns, plus one extra after the 8th: pad short last lines so the text column lines up.
    const pad = (k) => 49 - (k * 3 + (k >= 8 ? 1 : 0));
    const renderHex = (hx) => {
      bytes = [];
      marked = [];
      const lines = [];
      for (let off = 0; off < hx.length / 2; off += 16) {
        const hs = [], as = [];
        for (let i = off; i < Math.min(off + 16, hx.length / 2); i++) {
          const b = parseInt(hx.substr(i * 2, 2), 16);
          const a = h("span", { "data-i": i }, hx.substr(i * 2, 2));
          const c = h("span", { "data-i": i }, b >= 32 && b < 127 ? String.fromCharCode(b) : "·");
          bytes.push([a, c]);
          hs.push(a, i % 16 === 7 ? "  " : " ");
          as.push(c);
        }
        lines.push(h("div", {}, h("span", { class: "off" }, off.toString(16).padStart(4, "0")), "  ", ...hs,
          " ".repeat(pad(hs.length / 2)), " ", h("span", { class: "asc" }, ...as)));
      }
      hex.replaceChildren(...lines);
    };
    hex.addEventListener("click", (e) => {
      const i = e.target.dataset && e.target.dataset.i;
      if (i === undefined || !current) return;
      const trail = fieldAt(current.tree, Number(i)).trail;
      if (!trail) return;
      for (const f of trail.slice(0, -1)) if (f.box) f.box.open = true;
      const f = trail[trail.length - 1];
      choose(f.el, f);
      f.el.scrollIntoView({ block: "nearest" });
    });
    const showDecoded = (num, pk, d) => {
      sideHead.replaceChildren(h("strong", {}, `Packet ${num}`), h("span", {}, d.length ? `${d.length} bytes` : ""),
        h("button", { type: "button", class: "ghost", title: "Close the details", "aria-label": "Close the details", onclick: closeSide }, "×"));
      raw.textContent = pk.raw;
      chosen = null;
      current = d.tree ? d : null;
      if (d.error) { tree.replaceChildren(h("p", { class: "pv-note" }, d.error)); hex.replaceChildren(); bytes = []; return; }
      const frame = d.tree.find((f) => f.n === "frame");
      if (frame) frame.t = frame.t.replace(/^Frame 1:/, `Frame ${num}:`);
      tree.replaceChildren(...renderTree(d.tree, true));
      renderHex(d.hex);
    };
    const closeSide = () => {
      side.hidden = true;
      if (st.picked) st.picked.classList.remove("on");
      st.picked = null;
    };

    const pick = (tr, pk, num) => {
      if (st.picked) st.picked.classList.remove("on");
      st.picked = tr;
      tr.classList.add("on");
      side.hidden = false;
      const cached = st.cache.get(tr);
      if (cached) { showDecoded(num, pk, cached); return; }
      if (pk.gen !== st.gen || !st.ws || st.ws.readyState !== 1) {
        showDecoded(num, pk, { error: "This packet is from an earlier capture, so its bytes are gone. Download the pcap to keep traffic." });
        return;
      }
      st.want = { n: pk.n, tr, pk, num };
      showDecoded(num, pk, { error: "Decoding…" });
      st.ws.send(JSON.stringify({ decode: pk.n }));
    };

    const add = (list) => {
      const stick = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 30;
      for (const pk of list) {
        st.n += 1;
        const num = st.n;
        pk.gen = st.gen;
        const tr = h("tr", { tabindex: 0 },
          h("td", {}, num), h("td", {}, pk.time), h("td", {}, pk.src), h("td", {}, pk.dst),
          h("td", {}, h("span", { class: `tag c-${PROTO_COLOR[pk.proto] || "blue"}` }, pk.proto)),
          h("td", {}, pk.length), h("td", {}, pk.info));
        tr.addEventListener("click", () => pick(tr, pk, num));
        tr.addEventListener("keydown", (e) => {
          if (e.key === "Enter") pick(tr, pk, num);
          const next = e.key === "ArrowDown" ? tr.nextElementSibling : e.key === "ArrowUp" ? tr.previousElementSibling : null;
          if (next) { e.preventDefault(); next.focus(); next.click(); }
        });
        rows.append(tr);
      }
      while (rows.children.length > 2000) {
        if (rows.firstChild === st.picked) closeSide();
        st.cache.delete(rows.firstChild);
        rows.firstChild.remove();
      }
      if (stick) scroller.scrollTop = scroller.scrollHeight;
    };

    const connect = () => {
      if (st.ws) { st.ws.onclose = null; st.ws.close(); }
      const f = filter.value.trim();
      status.textContent = "Connecting";
      st.gen += 1;
      const ws = new WebSocket(wsUrl(`${path}/packets`) + (f ? `?filter=${encodeURIComponent(f)}` : ""));
      st.ws = ws;
      let failed = false;
      ws.onopen = () => {
        status.textContent = st.paused ? "Paused" : "Listening";
        if (st.paused) ws.send(JSON.stringify({ pause: true }));
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.decoded !== undefined) {
          const w = st.want;
          if (!w || w.n !== msg.decoded) return;
          if (!msg.error) st.cache.set(w.tr, msg);
          if (st.picked === w.tr) showDecoded(w.num, w.pk, msg);
          return;
        }
        if (msg.error) { failed = true; status.textContent = msg.error; return; }
        if (!st.paused && msg.packets) add(msg.packets);
      };
      ws.onclose = () => { if (!failed) status.textContent = "Stopped"; };
    };

    const pane = h("div", { class: "pv" },
      h("form", { class: "pv-bar", onsubmit: (e) => { e.preventDefault(); connect(); } },
        h("strong", {}, title), filter,
        h("button", { type: "submit" }, "Apply"),
        pause,
        h("button", { type: "button", onclick: () => { rows.replaceChildren(); st.n = 0; st.cache.clear(); closeSide(); } }, "Clear"),
        h("a", { href: `/api${path}/capture?seconds=30`, download: "", title: "Download a 30 second pcap for Wireshark" }, "pcap"),
        status),
      h("div", { class: "pv-body" }, scroller, side));
    $("#terms").append(pane);
    dock.set(key, { name: `${title} packets`, pane, onShow: null, close: () => { if (st.ws) { st.ws.onclose = null; st.ws.close(); } } });
    connect();
  }
  dockShow(key);
}

function reachDialog() {
  const clients = state.lab.nodes.filter((n) => n.kind === "client" && n.running);
  if (!clients.length) { toast("Start a client first. The test runs from a PC.", true); return; }
  const known = [];
  for (const n of state.lab.nodes) {
    const addr = n.kind === "client" && (n.addressing === "dhcp" ? n.leased : n.address);
    if (addr) known.push([addr.split("/")[0], n.name + ((n.services || []).includes("iperf") ? " (iperf3)" : "")]);
    if (n.mgmt_ip) known.push([n.mgmt_ip, `${n.name} (Cloud)`]);
  }
  const sel = state.sel && state.sel.type === "node" ? clients.find((n) => n.id === state.sel.id) : null;
  const to = h("input", { id: "m-to", type: "text", list: "m-to-list", autocomplete: "off", placeholder: "192.168.20.2" });
  const note = h("p", { class: "hint" });
  openModal("Connectivity test", [
    { key: "kind", label: "Test", type: "select", value: "reach",
      options: [{ value: "reach", label: "Ping, then traceroute" }, { value: "iperf", label: "Throughput (iperf3, 5 seconds)" }] },
    { node: note },
    { key: "from", label: "From", type: "select", value: (sel || clients[0]).id,
      options: clients.map((n) => ({ value: n.id, label: n.name + (n.address ? ` (${n.address})` : "") })) },
    { node: h("div", { class: "field" }, h("label", { for: "m-to" }, "To (address or name)"), to,
      h("datalist", { id: "m-to-list" }, known.map(([ip, name]) => h("option", { value: ip }, name)))) },
  ], "Run test", async (v) => {
    const target = to.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9.:-]*$/.test(target)) throw new Error("Enter an IP address or a host name.");
    openTest(nodeById(Number(v.from)), target, v.kind === "iperf" ? ["iperf"] : ["ping", "traceroute"]);
  });
  const kind = $("#m-kind");
  const sync = () => {
    note.textContent = kind.value === "iperf"
      ? "Measures throughput to a client running the iperf3 server (switch it on under Services). Through CHRs on the free licence, expect about 1 Mbit/s: that is the licence limit, not the lab."
      : "Runs ping, then traceroute, from a client to an address.";
  };
  kind.addEventListener("change", sync);
  sync();
}

function openTest(node, target, tools = ["ping", "traceroute"]) {
  const key = `t:${node.id}:${target}:${tools.join("+")}`;
  if (!dock.has(key)) {
    const out = h("pre", { tabindex: 0 });
    const status = h("span", { class: "pv-status" });
    const again = h("button", { type: "button" }, "Run again");
    const run = async () => {
      again.disabled = true;
      out.textContent = "";
      const results = {};
      for (const tool of tools) {
        status.className = "pv-status";
        status.textContent = `Running ${tool}`;
        try {
          const r = await api("POST", `${labPath()}/nodes/${node.id}/test`, { target, tool });
          results[tool] = r.ok;
          out.textContent += `$ ${r.command}\n${r.output || "(no output)"}\n\n`;
        } catch (e) {
          results[tool] = false;
          out.textContent += `${tool}: ${e.message}\n\n`;
        }
        out.scrollTop = out.scrollHeight;
      }
      if (tools[0] === "iperf") {
        const m = /([\d.]+) Mbits\/sec.*receiver/.exec(out.textContent);
        status.className = "pv-status " + (results.iperf ? "good" : "bad");
        status.textContent = results.iperf ? (m ? `${m[1]} Mbit/s` : "Done") : `No iperf3 server at ${target}`;
      } else {
        status.className = "pv-status " + (results.ping ? "good" : "bad");
        status.textContent = results.ping ? `${target} answers` : `No answer from ${target}`;
      }
      again.disabled = false;
    };
    again.addEventListener("click", run);
    const pane = h("div", { class: "pv" },
      h("div", { class: "pv-bar" }, h("strong", {}, `${node.name} to ${target}` + (tools[0] === "iperf" ? " (iperf3)" : "")), again, status), out);
    $("#terms").append(pane);
    dock.set(key, { name: `${node.name} → ${target}` + (tools[0] === "iperf" ? " iperf3" : ""), pane, onShow: null, close: () => {} });
    run();
  }
  dockShow(key);
}

function packetDialog() {
  const sel = state.sel;
  // A selected link opens straight away, on whichever end is running.
  if (sel && sel.type === "link") {
    const l = state.lab.links.find((x) => x.id === sel.id);
    const end = [l.a, l.b].find((e) => e && nodeById(e.node).running);
    if (end) { openPackets(nodeById(end.node), end.port); return; }
  }
  const options = [];
  for (const n of state.lab.nodes) {
    if (!n.running) continue;
    for (let p = 1; p <= n.ports; p++) {
      const peer = peerText(n, p);
      if (peer !== "Not connected") options.push({ value: `${n.id}:${p}`, label: `${n.name} ${portName(n, p)}  (to ${peer})`, node: n.id });
    }
  }
  if (!options.length) { toast("Start a device that has a link first. There is nothing to listen on yet.", true); return; }
  const mine = sel && sel.type === "node" ? options.find((o) => o.node === sel.id) : null;
  openModal("Packet view", [
    { key: "port", label: "Port to listen on", type: "select", options, value: (mine || options[0]).value },
  ], "Open", async (v) => {
    const [id, port] = v.port.split(":").map(Number);
    openPackets(nodeById(id), port);
  });
}

window.addEventListener("resize", () => {
  setDockHeight(readStore("dockHeight", null), false);
  const d = dock.get(state.activeDock);
  if (d && d.onShow) d.onShow();
});

// A short canvas (tall bottom panel, small window) gets an icon-only tool rail; labels stay as tooltips.
if (window.ResizeObserver) {
  new ResizeObserver(() => {
    const stage = $("#stage");
    const vertical = getComputedStyle($("#tools")).flexDirection === "column";
    $("#tools").classList.toggle("compact", vertical && stage.clientHeight < 560);
  }).observe($("#stage"));
}

// The bottom panel can be made taller by dragging its top edge; the height is kept per browser.
function setDockHeight(px, save = true) {
  const el = $("#dock");
  if (px === null || px === undefined) { el.style.height = ""; return; }
  const height = Math.round(Math.max(140, Math.min(px, window.innerHeight * 0.8)));
  el.style.height = height + "px";
  if (save) writeStore("dockHeight", height);
  const d = dock.get(state.activeDock);
  if (d && d.onShow) requestAnimationFrame(d.onShow);
}
(() => {
  const grip = $("#dock-grip");
  setDockHeight(readStore("dockHeight", null), false);
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("drag");
    const bottom = $("#dock").getBoundingClientRect().bottom;
    let frame = 0;
    const move = (ev) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setDockHeight(bottom - ev.clientY, false));
    };
    const up = () => {
      grip.classList.remove("drag");
      grip.removeEventListener("pointermove", move);
      setDockHeight($("#dock").getBoundingClientRect().height);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up, { once: true });
    grip.addEventListener("pointercancel", up, { once: true });
  });
  grip.addEventListener("dblclick", () => {
    const now = $("#dock").getBoundingClientRect().height;
    setDockHeight(now < window.innerHeight * 0.45 ? window.innerHeight * 0.6 : 300);
  });
  grip.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowUp" ? 30 : e.key === "ArrowDown" ? -30 : 0;
    if (!step) return;
    e.preventDefault();
    setDockHeight($("#dock").getBoundingClientRect().height + step);
  });
})();

// ------------------------------------------------------- export as PNG

const STYLE_PROPS = ["fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
  "font-size", "font-family", "font-weight", "text-anchor", "paint-order", "opacity"];

function wrapLines(text, font, width, nowrap) {
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.font = font;
  const out = [];
  for (const para of text.split("\n")) {
    if (nowrap) { out.push(para); continue; }
    let line = "";
    for (const word of para.split(/\s+/)) {
      const next = line ? line + " " + word : word;
      if (line && ctx.measureText(next).width > width) { out.push(line); line = word; }
      else line = next;
    }
    out.push(line);
  }
  return out;
}

// Text inside shapes is HTML on screen; an image needs it as plain SVG text.
function foreignToText(fo) {
  const div = fo.firstElementChild, cs = getComputedStyle(div);
  const size = parseFloat(cs.fontSize), lh = parseFloat(cs.lineHeight) || size * 1.45;
  const padX = parseFloat(cs.paddingLeft) || 0, padY = parseFloat(cs.paddingTop) || 0;
  const x0 = Number(fo.getAttribute("x")), y0 = Number(fo.getAttribute("y"));
  const w = Number(fo.getAttribute("width")), hgt = Number(fo.getAttribute("height"));
  const center = cs.textAlign === "center";
  const lines = wrapLines(div.textContent, `${cs.fontWeight} ${size}px ${cs.fontFamily}`, w - 2 * padX, cs.whiteSpace === "nowrap");
  const x = center ? x0 + w / 2 : x0 + padX;
  const max = Math.max(1, Math.floor((hgt - padY) / lh));
  return s("text", { style: `font-size:${size}px;font-family:${cs.fontFamily};font-weight:${cs.fontWeight};fill:${cs.color}`,
    "text-anchor": center ? "middle" : "start" },
    lines.slice(0, max).map((line, i) => s("tspan", { x, y: y0 + padY + (lh - size) / 2 + size * 0.82 + i * lh }, line)));
}

function inlineStyles(orig, clone) {
  if (orig.tagName !== "g") {
    const cs = getComputedStyle(orig);
    clone.setAttribute("style", STYLE_PROPS.map((p) => `${p}:${cs.getPropertyValue(p)}`).join(";"));
  }
  clone.removeAttribute("class");
  clone.removeAttribute("tabindex");
  const kids = [...orig.children], copies = [...clone.children];
  kids.forEach((kid, i) => {
    if (kid.tagName === "foreignObject") copies[i].replaceWith(foreignToText(kid));
    else if (kid.tagName === "title" || kid.classList.contains("hit")) copies[i].remove();
    else inlineStyles(kid, copies[i]);
  });
}

function contentBounds() {
  const lab = state.lab;
  const xs = [lab.mgmt_x - 60, lab.mgmt_x + 60], ys = [lab.mgmt_y - 40, lab.mgmt_y + 55];
  for (const n of lab.nodes) { xs.push(n.x - 60, n.x + 60); ys.push(n.y - 40, n.y + 72); }
  for (const a of lab.annotations) { xs.push(a.x - 4, a.x + a.w + 4); ys.push(a.y - 4, a.y + a.h + 4); }
  return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
}

async function topologyPng(scale) {
  const saved = { sel: state.sel, from: state.linkFrom };
  state.sel = null;
  state.linkFrom = null;
  renderCanvas(); // without selection outlines
  try {
    const world = $("#world");
    const clone = world.cloneNode(true);
    inlineStyles(world, clone);
    const b = contentBounds(), pad = 40;
    const w = Math.ceil(b.x1 - b.x0 + 2 * pad), hgt = Math.ceil(b.y1 - b.y0 + 2 * pad);
    clone.setAttribute("transform", `translate(${pad - b.x0},${pad - b.y0})`);
    const svg = s("svg", { xmlns: SVGNS, width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}` },
      s("rect", { width: w, height: hgt, fill: getComputedStyle(document.body).backgroundColor }), clone);
    const img = new Image();
    await new Promise((ok, fail) => {
      img.onload = ok;
      img.onerror = () => fail(new Error("The browser could not render the topology as an image."));
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(svg));
    });
    // Keep very large labs inside what browsers will allocate for a canvas.
    const k = Math.min(scale, 8000 / w, 8000 / hgt);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * k);
    canvas.height = Math.round(hgt * k);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    return await new Promise((ok, fail) => canvas.toBlob((blob) => (blob ? ok(blob) : fail(new Error("PNG export failed."))), "image/png"));
  } finally {
    state.sel = saved.sel;
    state.linkFrom = saved.from;
    renderCanvas();
  }
}

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
    for (const key of [...dock.keys()]) dockClose(key);
    act(async () => { await api("DELETE", labPath()); state.lab = null; state.sel = null; });
  },
  "cfg-export": () => { const n = selectedRouter(); if (n) exportConfig(n); },
  "cfg-export-all": () => exportAllConfigs(),
  "cfg-import": () => { const n = selectedRouter(); if (n) importConfig(n); },
  "cfg-identity": () => { const n = selectedRouter(); if (n) pushIdentity(n); },
  "tool-packets": () => packetDialog(),
  "tool-activity": () => openActivity(),
  "tool-web": () => openWebAccess(),
  "tool-view": () => openWebDialog(),
  "tool-nuke": () => nukeDialog(),
  "tool-test": () => reachDialog(),
  "tool-docs": async () => {
    toast("Building the documentation");
    try {
      const png = await topologyPng(2).catch(() => null);
      const b64 = png ? await new Promise((ok, fail) => {
        const reader = new FileReader();
        reader.onload = () => ok(String(reader.result).split(",")[1]);
        reader.onerror = () => fail(new Error("Could not read the topology image."));
        reader.readAsDataURL(png);
      }) : null;
      const res = await apiRaw("POST", `${labPath()}/documentation`, { png: b64 });
      saveBlob(await res.blob(), `${state.lab.name}-documentation.zip`);
      toast("Documentation exported.");
    } catch (e) { toast(e.message, true); }
    refresh(true).catch(() => {});
  },
  "bak-settings": () => backupDialog(),
  "bak-run": () => backupNow(null),
  "bak-compare": () => {
    const sel = state.sel && state.sel.type === "node" ? nodeById(state.sel.id) : null;
    revisionsDialog(sel && sel.kind === "chr" ? sel.id : null);
  },
  "acct-logout": async () => {
    try { await api("POST", "/auth/logout"); } catch (e) { /* going to the sign-in page anyway */ }
    location.replace("/login");
  },
  "acct-password": () => openModal("Change my password", [
    { key: "current", label: "Current password", type: "password" },
    { key: "fresh", label: "New password (at least 8 characters)", type: "password" },
    { key: "again", label: "New password again", type: "password" },
  ], "Change password", async (v) => {
    if (v.fresh !== v.again) throw new Error("The two new passwords are not the same.");
    await api("POST", "/account/password", { current: v.current, new: v.fresh });
    toast("Password changed.");
  }),
  "acct-users": () => accountsDialog(),
  "tool-png": async () => {
    try {
      saveBlob(await topologyPng(2), `${state.lab.name}.png`);
      toast("Topology exported.");
    } catch (e) { toast(e.message, true); }
  },
};

// ---------------------------------------------- backups and revisions

const EVERY = { 15: "15 minutes", 60: "hour", 360: "6 hours", 1440: "24 hours" };
const revTime = (id) => new Date(Date.UTC(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8),
  +id.slice(9, 11), +id.slice(11, 13), +id.slice(13, 15))).toLocaleString();

function backupDialog() {
  const b = state.lab.backup;
  const routers = state.lab.nodes.filter((n) => n.kind === "chr");
  const last = routers.filter((n) => n.backup && n.backup.last).map((n) =>
    h("li", {}, h("b", {}, n.name), h("span", { class: n.backup.last.ok ? "" : "bad" },
      `${n.backup.last.message} (${new Date(n.backup.last.time * 1000).toLocaleTimeString()})`)));
  openModal("Automatic backups", [
    { node: h("p", {}, "Saves each running router's config on a schedule. A new revision is kept only when the config has changed. Routers do not need a link to the Cloud: the config is read through the hypervisor.") },
    { key: "enabled", label: "Back up this lab automatically", type: "checkbox", value: b.enabled },
    { key: "scope", label: "Which routers", type: "select", value: b.scope,
      options: [{ value: "all", label: "All routers in this lab" }, { value: "selected", label: "Only routers I switch it on for" }] },
    { key: "interval", label: "How often", type: "select", value: b.interval,
      options: Object.entries(EVERY).map(([value, label]) => ({ value, label: "Every " + label })) },
    { node: h("h2", {}, "RouterOS login (fallback)") },
    { node: h("p", {}, "Only used over SSH for routers on the Cloud, when reading through the hypervisor does not work.") },
    { key: "username", label: "User", value: b.username },
    { key: "password", label: b.has_password ? "Password (leave blank to keep the saved one)" : "Password (blank on a fresh CHR)", type: "password", value: "" },
    { key: "sensitive", label: "Include passwords and keys in the backups", type: "checkbox", value: b.sensitive },
    { node: h("p", {}, "The login is stored on the server so backups can run unattended.") },
    last.length && { node: h("h2", {}, "Last attempt") },
    last.length && { node: h("ul", { class: "bk-list" }, last) },
  ].filter(Boolean), "Save", async (v) => {
    const body = { enabled: v.enabled, scope: v.scope, interval: Number(v.interval), username: v.username || "admin", sensitive: v.sensitive };
    if (v.password) body.password = v.password;
    await api("PUT", `${labPath()}/backup`, body);
    toast(v.enabled ? `Backing up every ${EVERY[v.interval]}.` : "Automatic backups are off.");
  });
  $("#m-password").autocomplete = "new-password";
}

async function backupNow(node) {
  toast(node ? `Backing up ${node.name}` : "Backing up");
  try {
    const { results } = await api("POST", `${labPath()}/backup/run`, node ? { node: node.id } : {});
    if (!results.length) { toast("No routers are included in backups.", true); return; }
    const ok = results.filter((r) => r.ok), bad = results.filter((r) => !r.ok);
    const changed = ok.filter((r) => r.changed).length;
    const summary = node
      ? `${node.name}: ${results[0].message}`
      : `${ok.length} backed up, ${changed} with changes` + (bad.length ? `. Skipped: ${bad.map((r) => `${r.name} (${r.message})`).join(", ")}` : "");
    toast(summary, !ok.length);
  } catch (e) { toast(e.message, true); }
  refresh(true).catch(() => {});
}

function revisionsDialog(nodeId) {
  const routers = state.lab.nodes.filter((n) => n.kind === "chr");
  if (!routers.length) { toast("This lab has no routers.", true); return; }
  const start = routers.find((n) => n.id === nodeId) || routers.find((n) => n.backup.revisions > 1) || routers.find((n) => n.backup.revisions) || routers[0];
  const pick = h("select", { id: "m-rnode" }, routers.map((n) => h("option", { value: n.id }, `${n.name} (${n.backup.revisions})`)));
  pick.value = String(start.id);
  const selA = h("select", { id: "m-ra" }), selB = h("select", { id: "m-rb" });
  const summary = h("p", {}), view = h("div", { class: "diff", tabindex: 0 }), links = h("p", {});
  const how = h("select", { id: "m-rhow", "aria-label": "How to restore" },
    h("option", { value: "replace" }, "Replace the whole config (router restarts)"),
    h("option", { value: "merge" }, "Apply on top of the current config"));
  const restoreRow = h("div", { class: "restore" });
  const restore = async (rev) => {
    const name = nodeById(Number(pick.value)).name;
    const what = how.value === "replace"
      ? `${name} is reset and restarted, then loads this revision. Its current config is saved as a new revision first. User accounts and passwords are not part of a config export, so sign in as admin afterwards.`
      : `The revision is imported on top of ${name}'s current config. Settings added since then stay.`;
    if (!confirm(`Restore ${name} to the revision from ${revTime(rev)}?\n\n${what}`)) return;
    try {
      const r = await api("POST", `${base()}/${rev}/restore`, { mode: how.value });
      toast(r.message, !r.ok);
      if (r.ok) loadNode();
    } catch (e) { toast(e.message, true); }
  };
  const base = () => `${labPath()}/nodes/${pick.value}/revisions`;

  const showDiff = async () => {
    view.replaceChildren();
    links.replaceChildren();
    restoreRow.replaceChildren();
    if (!selB.value) return;
    const name = nodeById(Number(pick.value)).name;
    restoreRow.append("Restore:", how,
      selA.value && selA.value !== selB.value && h("button", { type: "button", onclick: () => restore(selA.value) }, "Restore older"),
      h("button", { type: "button", onclick: () => restore(selB.value) }, selA.value ? "Restore newer" : "Restore this revision"));
    links.append("Download: ",
      selA.value && h("a", { href: `/api${base()}/${selA.value}`, download: "" }, "older"), selA.value && " · ",
      h("a", { href: `/api${base()}/${selB.value}`, download: "" }, selA.value ? "newer" : "this revision"));
    if (!selA.value) { summary.textContent = `${name} has one revision so far, so there is nothing to compare it with.`; return; }
    if (selA.value === selB.value) { summary.textContent = "Pick two different revisions."; return; }
    try {
      const d = await api("GET", `${base()}/diff?a=${selA.value}&b=${selB.value}`);
      summary.textContent = d.lines.length
        ? `${d.added} line${d.added === 1 ? "" : "s"} added, ${d.removed} removed.`
        : "These two revisions are the same.";
      view.replaceChildren(...d.lines.map((l) => h("div", { class: { "+": "add", "-": "del", "@": "hunk" }[l.t] || "" },
        l.t === "@" ? l.s : `${l.t} ${l.s}`)));
    } catch (e) { summary.textContent = e.message; }
  };

  const loadNode = async () => {
    let data;
    try { data = await api("GET", base()); } catch (e) { summary.textContent = e.message; return; }
    const opts = () => data.revisions.map((r) => h("option", { value: r.id }, `${revTime(r.id)} · ${r.lines} lines`));
    selA.replaceChildren(...opts());
    selB.replaceChildren(...opts());
    view.replaceChildren();
    links.replaceChildren();
    restoreRow.replaceChildren();
    if (!data.revisions.length) {
      summary.textContent = `No backups of ${data.name} yet. Use Back up now, or switch on automatic backups.`;
      return;
    }
    selB.value = data.revisions[0].id;           // newest
    if (data.revisions[1]) selA.value = data.revisions[1].id; else selA.replaceChildren();
    showDiff();
  };

  pick.addEventListener("change", loadNode);
  selA.addEventListener("change", showDiff);
  selB.addEventListener("change", showDiff);
  openModal("Compare revisions", [
    { node: h("div", { class: "cmp" },
      h("div", { class: "field" }, h("label", { for: "m-rnode" }, "Router"), pick),
      h("div", { class: "field" }, h("label", { for: "m-ra" }, "Older revision"), selA),
      h("div", { class: "field" }, h("label", { for: "m-rb" }, "Newer revision"), selB)) },
    { node: summary }, { node: view }, { node: links }, { node: restoreRow },
  ], "", null, null, true);
  loadNode();
}

function nukeDialog() {
  const lab = state.lab;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const devices = lab.nodes.length, links = lab.links.length, shapes = lab.annotations.length;
  const running = lab.nodes.filter((n) => n.running).length;
  const revisions = lab.nodes.reduce((sum, n) => sum + ((n.backup && n.backup.revisions) || 0), 0);
  if (!devices && !links && !shapes) { toast(`${lab.name} is already empty.`); return; }
  const others = [...new Set((state.others || []).filter((o) => o.lab === lab.id).map((o) => o.user))];

  const gone = [
    devices && `${plural(devices, "device", "devices")}${running ? `, ${running} of them running,` : ""} with their disks and configurations`,
    links && plural(links, "link", "links"),
    shapes && `${plural(shapes, "area, label or note", "areas, labels and notes")}`,
    revisions && `${plural(revisions, "saved config revision", "saved config revisions")}`,
  ].filter(Boolean);
  const typed = h("input", { id: "m-confirm", type: "text", autocomplete: "off", spellcheck: "false",
    placeholder: lab.name, oninput: () => { $("#modal-ok").disabled = typed.value.trim() !== lab.name; } });
  const exportLab = async (e) => {
    e.preventDefault();
    try {
      const res = await apiRaw("GET", `${labPath()}/export`);
      saveBlob(await res.blob(), `${lab.name}.mikrobox.json`);
      toast("Lab file downloaded.");
    } catch (err) { toast(err.message, true); }
  };

  openModal(`Nuke ${lab.name}?`, [
    { node: h("div", { class: "nuke" },
      h("p", {}, h("strong", {}, "This deletes everything in this lab and cannot be undone.")),
      h("ul", {}, gone.map((line) => h("li", {}, line))),
      h("p", {}, "Kept: the lab itself, its name and its backup settings. You get an empty canvas to start over."),
      others.length > 0 && h("p", {}, h("strong", {}, `${others.join(", ")} ${others.length === 1 ? "is" : "are"} in this lab right now.`),
        " It empties for them too, and their consoles close.")) },
    { node: h("p", {}, "Want a way back? ", h("a", { href: "#", onclick: exportLab }, "Download the lab file first"),
      revisions ? ". Saved config revisions are not in it; use Configs, Compare revisions to download any you need." : ".") },
    { node: h("div", { class: "field" }, h("label", { for: "m-confirm" }, ["Type ", h("strong", {}, lab.name), " to confirm"]), typed) },
  ], "Nuke lab", async () => {
    const r = await api("POST", `${labPath()}/clear`, { confirm: typed.value.trim() });
    for (const key of [...dock.keys()]) dockClose(key);
    state.sel = null;
    state.linkFrom = null;
    state.tool = "select";
    Object.assign(view, { x: 90, y: 20, k: 1 });
    saveView();
    const parts = [
      r.removed.devices && plural(r.removed.devices, "device", "devices"),
      r.removed.links && plural(r.removed.links, "link", "links"),
      r.removed.annotations && plural(r.removed.annotations, "shape or note", "shapes and notes"),
    ].filter(Boolean);
    toast(`${lab.name} is empty. Removed ${parts.join(", ")}.`);
  });
  $("#modal-ok").classList.add("danger-fill");
  $("#modal-ok").disabled = true;
  typed.focus();
}

async function accountsDialog() {
  let data;
  try { data = await api("GET", "/users"); } catch (e) { toast(e.message, true); return; }
  const list = h("ul", { class: "users" }, data.users.map((name) =>
    h("li", {}, h("span", {}, name + (name === data.me ? " (you)" : "")),
      name !== data.me && h("button", { type: "button", class: "danger", onclick: async () => {
        if (!confirm(`Remove the account ${name}?`)) return;
        try { await api("DELETE", `/users/${encodeURIComponent(name)}`); toast(`Removed ${name}.`); accountsDialog(); }
        catch (e) { toast(e.message, true); }
      } }, "Remove"))));
  openModal("Accounts", [
    { node: h("p", {}, "Everyone with an account can use all labs on this server.") },
    { node: list },
    { node: h("h2", {}, "Add an account") },
    { key: "username", label: "Username" },
    { key: "password", label: "Password (at least 8 characters)", type: "password" },
    { key: "again", label: "Password again", type: "password" },
  ], "Add account", async (v) => {
    if (v.password !== v.again) throw new Error("The two passwords are not the same.");
    await api("POST", "/users", { username: v.username, password: v.password });
    toast(`Added ${v.username}.`);
  });
  // Nothing typed yet, so do not let the browser put the saved login here.
  for (const id of ["#m-username", "#m-password", "#m-again"]) $(id).autocomplete = "new-password";
}

function switchLab(lab) {
  state.lab = lab;
  state.sel = null;
  state.linkFrom = null;
  writeStore("lab", lab.id);
  loadView();
  beatSoon();
  if (activityState.list && !activityState.all) loadActivity().catch(() => {});
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

$("#templates").addEventListener("click", async () => {
  let list;
  try { list = await api("GET", "/templates"); } catch (e) { toast(e.message, true); return; }
  const free = state.sys ? state.sys.mem_available_mb : null;
  const pick = h("select", { id: "m-template" }, list.map((t) => h("option", { value: t.id }, t.title)));
  const name = h("input", { id: "m-tname", type: "text", autocomplete: "off" });
  const info = h("div", { class: "tpl" });
  const show = () => {
    const t = list.find((x) => x.id === pick.value);
    name.value = t.id;
    info.replaceChildren(...[
      h("p", {}, t.summary),
      h("p", {}, `${t.routers} routers and switches, ${t.devices - t.routers} clients. Needs about ${(t.ram_mb / 1024).toFixed(1)} GB of memory when everything is running.`),
      free !== null && t.ram_mb > free && h("p", { class: "warn" }, `This server has ${(free / 1024).toFixed(1)} GB free, so not all of it can run at once.`),
      ...(t.needs || []).map((kind) => {
        const p = platformInfo(kind);
        return p && !p.image && h("p", { class: "warn" }, `Needs the ${p.label} image, which is not installed yet. Add it under Settings, Images.`);
      })].filter(Boolean));
  };
  pick.addEventListener("change", show);
  show();
  openModal("New lab from a template", [
    { node: h("div", { class: "field" }, h("label", { for: "m-template" }, "Template"), pick) },
    { node: info },
    { node: h("div", { class: "field" }, h("label", { for: "m-tname" }, "Lab name"), name) },
    { node: h("p", {}, "You get the devices, cabling, areas and a note with a suggested plan. Routers start unconfigured, with ether1 left free for the Cloud.") },
  ], "Create lab", async () => {
    const lab = await api("POST", `/templates/${pick.value}`, { name: name.value.trim() || null });
    switchLab(lab);
    state.fitNext = true;
    toast(`Created ${lab.name}.`);
  });
});

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
    { node: h("h2", {}, "Memory") },
    { key: "ksm", label: "Share identical memory between devices (KSM)", type: "checkbox", value: !!(sys.ksm && sys.ksm.enabled) },
    { node: h("p", {}, !sys.ksm || !sys.ksm.available
      ? "Not available on this server's kernel."
      : `Devices booted from the same image share the memory they have in common. It uses some CPU while scanning, and savings build up over a few minutes. ${sys.ksm.running ? `Saving ${(sys.ksm.saved_mb / 1024).toFixed(1)} GB right now.` : "Off right now."}`) },
    { node: h("h2", {}, "Images") },
    { node: h("dl", { class: "info" }, (sys.platforms || []).map((p) => row(p.label + (p.experimental ? " (experimental)" : ""),
      h("span", {}, p.image ? `installed${p.version ? ` ${p.version}` : ""}` : "not installed",
        p.experimental && [" · ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); imagesDialog(p.kind); } }, p.image ? "replace" : "add")])))) },
  ], "Save", async (v) => {
    Object.assign(prefs, {
      theme: v.theme, grid: v.grid, snap: v.snap, labels: v.labels,
      defPorts: Math.min(sys.max_chr_ports || 24, Math.max(1, Number(v.defPorts) || 8)),
      defCpus: Math.min(8, Math.max(1, Number(v.defCpus) || 1)),
    });
    writeStore("prefs", prefs);
    applyPrefs();
    if (sys.ksm && v.ksm !== sys.ksm.enabled) {
      const st = await api("PUT", "/host/ksm", { enabled: v.ksm });
      toast(st.enabled ? "Memory sharing is on." : "Memory sharing is off. Pages already shared stay shared.");
    }
  });
});

window.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement && document.activeElement.tagName);
  if (e.key === "Escape") {
    if (drag && drag.kind === "link") {  // drop the link being dragged out
      drag = null;
      state.linkFrom = null;
      $("#canvas").classList.remove("linking");
      render(true);
    } else if (!$("#modal").hidden) $("#modal-cancel").click();
    else if (state.tool !== "select") setTool("select");
    else if (state.sel) select(null);
    closeMenus();
  } else if ((e.key === "Delete" || e.key === "Backspace") && !typing && $("#modal").hidden && state.sel) {
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#terms")) return;
    e.preventDefault();
    deleteSelected();
  }
});

// ---------------------------------------------------- instant updates

// The server pushes a hint whenever something changes; the workspace then fetches again.
// A slow poll stays as a safety net, e.g. for a device that stops by itself.
let refreshTimer = null, refreshing = false, refreshAgain = false, eventDelay = 1000;

function scheduleRefresh(delay = 120) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(runRefresh, delay);
}

async function runRefresh() {
  if (drag || !$("#modal").hidden) { scheduleRefresh(400); return; } // not mid-drag or mid-dialog
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try { await refresh(false); } catch (e) { /* the next event or the poll tries again */ }
  refreshing = false;
  if (refreshAgain) { refreshAgain = false; scheduleRefresh(0); }
}

function connectEvents() {
  const ws = new WebSocket(wsUrl("/events"));
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (err) { return; }
    if (msg.t === "hello") { eventDelay = 1000; scheduleRefresh(0); return; }
    if (msg.t === "activity") { onActivity(msg.entry); return; }
    if (msg.t === "web") { if (dock.has("web")) loadWeb(); checkViewers(); return; }
    scheduleRefresh();
  };
  ws.onclose = () => {
    setTimeout(connectEvents, eventDelay);
    eventDelay = Math.min(eventDelay * 2, 15000);
  };
}

let beatTimer = null;
function beatSoon() {
  clearTimeout(beatTimer);
  beatTimer = setTimeout(async () => {
    try {
      state.others = (await api("POST", "/presence", beatBody())).others;
      renderTop();
      renderCanvas();
    } catch (e) { /* the poll sends the next one */ }
  }, 150);
}

setInterval(() => scheduleRefresh(0), 10000);
connectEvents();

// ---------------------------------------------------------- activity

const activityState = { entries: [], all: false, list: null };

function openActivity() {
  const key = "activity";
  if (!dock.has(key)) {
    const list = h("ol", { class: "act-list", tabindex: 0, "aria-label": "Activity, newest first" });
    const all = h("input", { type: "checkbox", id: "act-all", onchange: () => {
      activityState.all = all.checked;
      loadActivity().catch((e) => toast(e.message, true));
    } });
    const pane = h("div", { class: "pv" },
      h("div", { class: "pv-bar" }, h("strong", {}, "Activity"),
        h("label", { class: "act-toggle", for: "act-all" }, all, "All labs"),
        h("span", { class: "pv-status" }, "Updates live")),
      list);
    $("#terms").append(pane);
    activityState.list = list;
    dock.set(key, { name: "Activity", pane, onShow: null, close: () => { activityState.list = null; } });
    loadActivity().catch((e) => toast(e.message, true));
  }
  dockShow(key);
}

async function loadActivity() {
  const lab = !activityState.all && state.lab ? `lab=${state.lab.id}&` : "";
  activityState.entries = await api("GET", `/activity?${lab}limit=300`);
  renderActivity();
}

function activityRow(e) {
  const t = new Date(e.ts * 1000);
  const today = t.toDateString() === new Date().toDateString();
  const when = today ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : t.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return h("li", { class: `act ${e.kind}` },
    h("time", { datetime: t.toISOString(), title: t.toLocaleString() }, when),
    h("span", { class: `who c-${e.kind === "system" ? "gray" : whoColor(e.user)}`, "aria-hidden": "true" }, e.user.slice(0, 2)),
    h("span", { class: "act-text" }, h("b", {}, e.user), " ", e.text,
      activityState.all && e.lab_name ? h("span", { class: "act-lab" }, ` · ${e.lab_name}`) : null));
}

function renderActivity() {
  const list = activityState.list;
  if (!list) return;
  const rows = activityState.entries.slice(-300).reverse().map(activityRow);
  list.replaceChildren(...(rows.length ? rows : [h("li", { class: "act empty" }, "Nothing has happened here yet.")]));
}

function onActivity(entry) {
  const mine = state.lab && entry.lab === state.lab.id;
  if (activityState.list && (activityState.all || entry.lab == null || mine)) {
    activityState.entries.push(entry);
    renderActivity();
  }
  // Someone else removing or resetting things in your lab deserves a heads-up.
  if (entry.kind === "danger" && mine && state.user && entry.user !== state.user) toast(`${entry.user} ${entry.text}`);
}

applyPrefs();
fetch("/api/auth/status").then((r) => r.json()).then((st) => {
  state.user = st.user;
  $("#account").hidden = !st.enabled;
  if (st.user) $("#account-btn").textContent = st.user;
}).catch(() => {});
refresh(true).catch((e) => toast(e.message, true));
