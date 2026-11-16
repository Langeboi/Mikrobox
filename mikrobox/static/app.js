"use strict";
/* Mikrobox lab workspace. Plain JavaScript, no build step. */

const $ = (q) => document.querySelector(q);
const SVGNS = "http://www.w3.org/2000/svg";

function make(ns, tag, attrs, kids) {
  const node = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (!ns && (k === "value" || k === "disabled" || k === "hidden")) node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const h = (tag, attrs, ...kids) => make(null, tag, attrs, kids);
const s = (tag, attrs, ...kids) => make(SVGNS, tag, attrs, kids);

const state = {
  labs: [], lab: null, sys: null,
  sel: null,        // {type: "node" | "link" | "mgmt", id}
  tool: "select",
  linkFrom: null,
  activeConsole: null,
};
const consoles = new Map(); // node id -> {term, fit, ws, pane, name}
let drag = null;

// ---------------------------------------------------------------- API

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let msg = res.statusText;
    if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail)) {
      msg = data.detail.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ");
    }
    throw new Error(msg);
  }
  return data;
}

function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 2500);
}

async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function refresh(force) {
  const [labs, sys] = await Promise.all([api("GET", "/labs"), api("GET", "/system")]);
  state.labs = labs;
  state.sys = sys;
  let wanted = state.lab ? state.lab.id : Number(readPref("lab"));
  state.lab = labs.find((l) => l.id === wanted) || labs[0] || null;
  if (state.sel && state.lab) {
    const { type, id } = state.sel;
    if (type === "node" && !nodeById(id)) state.sel = null;
    if (type === "link" && !state.lab.links.some((l) => l.id === id)) state.sel = null;
  } else if (!state.lab) state.sel = null;
  render(force);
}

function readPref(key) {
  try { return localStorage.getItem("mikrobox." + key); } catch (e) { return null; }
}
function writePref(key, value) {
  try { localStorage.setItem("mikrobox." + key, value); } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------ helpers

const nodeById = (id) => state.lab.nodes.find((n) => n.id === id);
const portName = (node, port) => (node.kind === "chr" ? `ether${port}` : `eth${port - 1}`);
const labPath = () => `/labs/${state.lab.id}`;

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
    if (!l.b) return "Management";
    const other = mine === l.a ? l.b : l.a;
    const peer = nodeById(other.node);
    return peer ? `${peer.name} ${portName(peer, other.port)}` : "?";
  }
  return "Not connected";
}

function pos(ref) {
  if (ref.type === "mgmt") return { x: state.lab.mgmt_x, y: state.lab.mgmt_y };
  const n = nodeById(ref.id);
  return { x: n.x, y: n.y };
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

// ------------------------------------------------------------- render

function render(force) {
  renderTop();
  renderCanvas();
  renderInspector(force);
}

function renderTop() {
  const sel = $("#lab-select");
  sel.replaceChildren(
    ...state.labs.map((l) => h("option", { value: l.id }, l.name)),
  );
  if (state.lab) sel.value = String(state.lab.id);
  sel.disabled = !state.labs.length;
  for (const id of ["#lab-delete", "#lab-start", "#lab-stop", "#add-chr", "#add-client", "#tool-link"]) {
    $(id).disabled = !state.lab;
  }
  const sys = state.sys;
  $("#sys").textContent = sys
    ? `${sys.running_devices} / ${sys.max_devices} devices running` +
      (sys.mem_available_mb !== null ? ` · ${sys.mem_available_mb} MB free` : "")
    : "";
  const warn = [];
  if (sys && sys.dry_run) warn.push("Dry-run mode: nothing is actually started.");
  if (sys && !sys.dry_run && !sys.kvm) warn.push("KVM is not available on this VM, so routers will be very slow. On Proxmox, set the VM's CPU type to host.");
  if (sys && !sys.dry_run && !sys.chr_image) warn.push("The CHR image is missing. Run scripts/install.sh on the VM.");
  $("#banner").hidden = !warn.length;
  $("#banner").textContent = warn.join(" ");

  $("#tool-select").classList.toggle("on", state.tool === "select");
  $("#tool-link").classList.toggle("on", state.tool === "link");
  $("#canvas").classList.toggle("linking", state.tool === "link");
  $("#hint").textContent =
    state.tool === "link"
      ? (state.linkFrom ? "Now click the device to connect it to." : "Click the first device, then the second. Link to Management to reach a device from your PC.")
      : "Drag devices to arrange them. Click one to see its details.";
}

const GLYPH = {
  chr: "M-13 0a13 13 0 1 0 26 0a13 13 0 1 0 -26 0 M-8 0h16 M0 -8v16 M-8 0l3 -3 M-8 0l3 3 M8 0l-3 -3 M8 0l-3 3 M0 -8l-3 3 M0 -8l3 3 M0 8l-3 -3 M0 8l3 -3",
  client: "M-14 -13h28v18h-28z M-7 11h14 M0 5v6",
};

function renderCanvas() {
  const svg = $("#canvas");
  svg.replaceChildren();
  const lab = state.lab;
  const empty = $("#empty");
  empty.hidden = !!(lab && lab.nodes.length);
  empty.textContent = lab
    ? "Add a router or a client from the left to start building."
    : "Create a lab to get started.";
  if (!lab) return;

  let maxX = lab.mgmt_x, maxY = lab.mgmt_y;
  for (const n of lab.nodes) { maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y); }
  svg.setAttribute("width", maxX + 200);
  svg.setAttribute("height", maxY + 160);

  // Links first so devices paint over them. Parallel links are fanned out.
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
      // Keep a stable direction so the fan-out does not flip per link.
      const flip = nb && l.a.node > l.b.node;
      const dx = (flip ? A.x - B.x : B.x - A.x), dy = (flip ? A.y - B.y : B.y - A.y);
      const len = Math.hypot(dx, dy) || 1;
      const off = (i - (group.length - 1) / 2) * 14;
      const ox = (-dy / len) * off, oy = (dx / len) * off;
      A = { x: A.x + ox, y: A.y + oy };
      B = { x: B.x + ox, y: B.y + oy };
      const ux = (B.x - A.x) / len, uy = (B.y - A.y) / len;
      // Labels sit clear of the device box, and further out below it where the name is.
      const da = Math.min(60 + 14 * Math.max(0, uy), len * 0.45);
      const db = Math.min(60 + 14 * Math.max(0, -uy), len * 0.45);
      const isSel = state.sel && state.sel.type === "link" && state.sel.id === l.id;
      const down = (e) => { e.stopPropagation(); select({ type: "link", id: l.id }); };
      svg.append(
        s("g", { class: "link" + (isSel ? " sel" : "") },
          s("line", { class: "vis", x1: A.x, y1: A.y, x2: B.x, y2: B.y }),
          s("text", { x: A.x + ux * da, y: A.y + uy * da + 4 }, portName(na, l.a.port)),
          nb && s("text", { x: B.x - ux * db, y: B.y - uy * db + 4 }, portName(nb, l.b.port)),
          s("line", { class: "hit", x1: A.x, y1: A.y, x2: B.x, y2: B.y, onpointerdown: down }),
        ),
      );
    });
  }

  const cls = (ref, extra) => {
    let c = "node" + (extra || "");
    if (state.sel && state.sel.type === ref.type && state.sel.id === ref.id) c += " sel";
    if (state.linkFrom && state.linkFrom.type === ref.type && state.linkFrom.id === ref.id) c += " from";
    return c;
  };
  const handlers = (ref) => ({
    tabindex: 0,
    onpointerdown: (e) => nodeDown(e, ref),
    onkeydown: (e) => { if (e.key === "Enter") (state.tool === "link" ? linkClick(ref) : select(ref)); },
  });

  const mref = { type: "mgmt", id: 0 };
  svg.append(
    s("g", { class: cls(mref), transform: `translate(${lab.mgmt_x},${lab.mgmt_y})`, ...handlers(mref) },
      s("rect", { class: "box", x: -58, y: -20, width: 116, height: 40, rx: 20 }),
      s("text", { y: 5 }, "Management"),
      s("text", { class: "sub", y: 36 }, state.sys ? state.sys.mgmt_network : ""),
    ),
  );
  for (const n of lab.nodes) {
    const ref = { type: "node", id: n.id };
    svg.append(
      s("g", { class: cls(ref, n.running ? " running" : ""), transform: `translate(${n.x},${n.y})`, ...handlers(ref) },
        s("title", {}, `${n.name} (${n.running ? "running" : "stopped"})`),
        s("rect", { class: "box", x: -32, y: -26, width: 64, height: 52, rx: 6 }),
        s("path", { class: "glyph", d: GLYPH[n.kind] }),
        s("circle", { class: "dot", cx: 30, cy: -24, r: 6 }),
        s("text", { y: 44 }, n.name),
        s("text", { class: "sub", y: 58 }, n.mgmt_ip || (n.kind === "client" && n.address) || ""),
      ),
    );
  }
}

// ------------------------------------------------------- canvas input

function point(e) {
  const r = $("#canvas").getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function nodeDown(e, ref) {
  e.preventDefault();
  e.stopPropagation();
  if (state.tool === "link") { linkClick(ref); return; }
  const p = point(e), at = pos(ref);
  drag = { ref, dx: p.x - at.x, dy: p.y - at.y, moved: false };
  select(ref);
}

window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const p = point(e);
  const x = Math.max(60, Math.round(p.x - drag.dx)), y = Math.max(40, Math.round(p.y - drag.dy));
  if (drag.ref.type === "mgmt") { state.lab.mgmt_x = x; state.lab.mgmt_y = y; }
  else { const n = nodeById(drag.ref.id); n.x = x; n.y = y; }
  drag.moved = true;
  renderCanvas();
});

window.addEventListener("pointerup", () => {
  if (!drag) return;
  const { ref, moved } = drag;
  drag = null;
  if (!moved) return;
  const at = pos(ref);
  const call = ref.type === "mgmt"
    ? api("PATCH", labPath(), { mgmt_x: at.x, mgmt_y: at.y })
    : api("PATCH", `${labPath()}/nodes/${ref.id}`, { x: at.x, y: at.y });
  call.catch((err) => toast(err.message, true));
});

$("#canvas").addEventListener("pointerdown", () => {
  if (state.tool === "link") { state.linkFrom = null; render(true); return; }
  if (state.sel) select(null);
});

function linkClick(ref) {
  if (!state.linkFrom) { state.linkFrom = ref; render(true); return; }
  const from = state.linkFrom;
  if (from.type === ref.type && from.id === ref.id) { state.linkFrom = null; render(true); return; }
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
  openModal(b ? `Link ${a.name} to ${b.name}` : `Connect ${a.name} to Management`, fields, "Create link",
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
  $("#modal-error").textContent = "";
  const inputs = {};
  $("#modal-fields").replaceChildren(...fields.map((f) => {
    const id = "m-" + f.key;
    const input = f.type === "select"
      ? h("select", { id }, f.options.map((o) => h("option", { value: o.value }, o.label)))
      : h("input", { id, type: f.type || "text", value: f.value ?? "", placeholder: f.placeholder, min: f.min, max: f.max });
    inputs[f.key] = input;
    return h("div", { class: "field" }, h("label", { for: id }, f.label), input);
  }));
  const close = () => { $("#modal").hidden = true; };
  $("#modal-box").onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    for (const [k, input] of Object.entries(inputs)) values[k] = input.value.trim();
    try {
      await onSubmit(values);
      close();
      await refresh(true);
    } catch (err) {
      $("#modal-error").textContent = err.message;
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
  // First grid slot that is not on top of an existing device.
  let at = null;
  for (let i = 0; !at; i++) {
    const p = { x: 260 + (i % 5) * 160, y: 130 + Math.floor(i / 5) * 150 };
    if (!state.lab.nodes.some((n) => Math.hypot(n.x - p.x, n.y - p.y) < 110)) at = p;
  }
  const fields = kind === "chr"
    ? [
        { key: "name", label: "Hostname", value: nextName("R") },
        { key: "ports", label: "Ethernet ports (1-24)", type: "number", value: 8, min: 1, max: 24 },
        { key: "cpus", label: "vCPUs", type: "number", value: 1, min: 1, max: 8 },
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

function renderInspector(force) {
  const box = $("#inspector");
  const active = document.activeElement;
  if (!force && active && box.contains(active) && active.tagName === "INPUT") return;
  const lab = state.lab, sel = state.sel;
  if (!lab) { box.replaceChildren(h("p", {}, "No lab yet. Use New lab to create one.")); return; }
  if (!sel) { box.replaceChildren(...labInspector(lab)); return; }
  if (sel.type === "mgmt") box.replaceChildren(...mgmtInspector());
  else if (sel.type === "link") box.replaceChildren(...linkInspector(lab.links.find((l) => l.id === sel.id)));
  else box.replaceChildren(...nodeInspector(nodeById(sel.id)));
}

function labInspector(lab) {
  const running = lab.nodes.filter((n) => n.running).length;
  return [
    h("h3", {}, lab.name),
    h("div", { class: "status" }, `${lab.nodes.length} devices, ${running} running, ${lab.links.length} links`),
    h("p", {}, "Select a device or a link to see its details."),
  ];
}

function mgmtInspector() {
  const sys = state.sys || {};
  const host = location.hostname;
  const [net, bits] = (sys.mgmt_network || "10.99.0.0/16").split("/");
  const mask = bits === "16" ? "255.255.0.0" : "255.255.255.0";
  return [
    h("h3", {}, "Management"),
    h("div", { class: "status" }, sys.mgmt_network || ""),
    h("p", {}, "Link a router's ether1 here and it gets an address by DHCP that you can SSH or WinBox to from your PC. Add this route on your PC once:"),
    h("section", {}, h("h2", {}, "Windows"), copyRow(`route add ${net} mask ${mask} ${host}`)),
    h("section", {}, h("h2", {}, "Linux or macOS"), copyRow(`sudo ip route add ${net}/${bits} via ${host}`)),
  ];
}

function linkInspector(link) {
  const a = nodeById(link.a.node), b = link.b ? nodeById(link.b.node) : null;
  const ends = [[a, link.a.port]];
  if (b) ends.push([b, link.b.port]);
  const live = ends.find(([n]) => n.running);
  return [
    h("h3", {}, "Link"),
    h("div", { class: "status" },
      `${a.name} ${portName(a, link.a.port)} to ` + (b ? `${b.name} ${portName(b, link.b.port)}` : "Management")),
    h("section", {}, h("h2", {}, "Packet capture"),
      live
        ? [
            h("p", {}, h("a", { href: captureUrl(live[0], live[1], 30), download: "" }, "Download a 30 second capture")),
            h("p", {}, "Or watch it live in Wireshark:"),
            copyRow(`curl -sN ${location.origin}${captureUrl(live[0], live[1])} | wireshark -k -i -`),
          ]
        : h("p", {}, "Start a device on this link to capture its traffic.")),
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => act(async () => {
        await api("DELETE", `${labPath()}/links/${link.id}`);
        state.sel = null;
      }) }, "Delete link")),
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
  } else {
    access.push(h("p", {}, "Link a port to Management to reach this device from your PC."));
  }

  return [
    h("h3", {}, n.name),
    h("div", { class: "status" + (n.running ? " running" : "") },
      (isChr ? "CHR router" : "Linux client") + (n.running ? " · Running" : " · Stopped")),
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
    h("section", {},
      h("button", { type: "button", class: "danger", onclick: () => {
        if (!confirm(`Delete ${n.name}? Its configuration is lost.`)) return;
        closeConsole(n.id);
        act(async () => { await api("DELETE", path); state.sel = null; });
      } }, "Delete device")),
  ];
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

// ------------------------------------------------------------- top bar

$("#lab-select").addEventListener("change", (e) => {
  state.lab = state.labs.find((l) => l.id === Number(e.target.value)) || null;
  state.sel = null;
  state.linkFrom = null;
  if (state.lab) writePref("lab", state.lab.id);
  render(true);
});

$("#lab-new").addEventListener("click", () => {
  openModal("New lab", [{ key: "name", label: "Name (letters, digits, - and _)", value: "" }], "Create", async (v) => {
    const lab = await api("POST", "/labs", { name: v.name });
    state.lab = lab;
    state.sel = null;
    writePref("lab", lab.id);
  });
});

$("#lab-delete").addEventListener("click", () => {
  if (!confirm(`Delete lab ${state.lab.name} and all its devices?`)) return;
  for (const id of [...consoles.keys()]) closeConsole(id);
  act(async () => { await api("DELETE", labPath()); state.lab = null; state.sel = null; });
});

$("#lab-start").addEventListener("click", () => act(() => api("POST", labPath() + "/start")));
$("#lab-stop").addEventListener("click", () => act(() => api("POST", labPath() + "/stop")));
$("#tool-select").addEventListener("click", () => setTool("select"));
$("#tool-link").addEventListener("click", () => setTool("link"));
$("#add-chr").addEventListener("click", () => addNodeDialog("chr"));
$("#add-client").addEventListener("click", () => addNodeDialog("client"));

window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#modal").hidden) $("#modal-cancel").click();
  else if (state.tool === "link") setTool("select");
});

setInterval(() => {
  if (!drag && $("#modal").hidden) refresh(false).catch(() => {});
}, 3000);

refresh(true).catch((e) => toast(e.message, true));
