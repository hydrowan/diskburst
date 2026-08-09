"use strict";

// Catppuccin Mocha accents, fixed slot order (hue marks the top-level branch).
const PALETTE = ["#89b4fa", "#fab387", "#a6e3a1", "#cba6f7", "#f38ba8", "#f9e2af", "#94e2d5", "#f5c2e7"];
const FILE_GRAY = "#6c7086";
const SURFACE = "#12121c"; // matches --surface-1 (near-black ground)

const CX = 420, CY = 420, R_HOLE = 118, RINGS = 4;
const RING_W = (408 - R_HOLE) / RINGS;
const MIN_ARC_DEG = 0.25;

const state = {
  path: "",
  data: null,
  status: null,
  largestPath: null, // path the "Largest files" panel currently shows; null = stale
  doneHandled: false,
  centerSize: 0,
  ageLens: false,
  diffBaseline: "prev",
  lastScanState: "",
  liveStamp: 0,
  filter: { q: "", mode: "fuzzy", exts: new Set() },
};

function filterActive() {
  return Boolean(state.filter.q.trim() || state.filter.exts.size);
}

function filterParams() {
  if (!filterActive()) return "";
  const f = state.filter;
  return (
    `&q=${encodeURIComponent(f.q.trim())}&mode=${f.mode}` +
    `&exts=${encodeURIComponent([...f.exts].join(","))}`
  );
}

// Age lens buckets: newest → oldest. Old + big = the safest deletions.
const AGE_BUCKETS = [
  { days: 7, color: "#94e2d5", label: "this week" },
  { days: 30, color: "#89b4fa", label: "this month" },
  { days: 180, color: "#cba6f7", label: "6 months" },
  { days: 730, color: "#fab387", label: "2 years" },
  { days: Infinity, color: "#f38ba8", label: "older" },
];

function ageColor(item) {
  const mtime = item.type === "file" ? item.mtime : item.newest;
  if (!mtime) return FILE_GRAY;
  const days = (Date.now() / 1000 - mtime) / 86400;
  return AGE_BUCKETS.find((b) => days <= b.days).color;
}

function fmtAgeShort(mtime) {
  if (!mtime) return "";
  const days = (Date.now() / 1000 - mtime) / 86400;
  if (days < 1) return "today";
  if (days < 30) return `${Math.round(days)}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${(days / 365).toFixed(1)}y ago`;
}

const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let u = 0;
  while (n >= 1000 && u < units.length - 1) { n /= 1000; u++; }
  return n.toFixed(n >= 100 || u === 0 ? 0 : n >= 10 ? 1 : 2) + " " + units[u];
}
function fmtCount(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
  return String(n);
}
const shortenHome = (p) => p.replace(/^\/Users\/[^/]+/, "~");

// Ease a numeric text element from its last value to `to` (reactive tiles/center).
function tweenNum(el, to, fmt) {
  const from = el._v ?? to;
  el._v = to;
  if (from === to) { el.textContent = fmt(to); return; }
  cancelAnimationFrame(el._raf);
  const start = performance.now(), dur = 450;
  const step = (t) => {
    const k = Math.min((t - start) / dur, 1);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = fmt(from + (to - from) * e);
    if (k < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 1600);
}

async function copyPath(p) {
  try {
    await navigator.clipboard.writeText(p);
    toast("Copied: " + (p.length > 60 ? shortenHome(p).slice(0, 57) + "…" : shortenHome(p)));
  } catch {
    toast("Copy failed");
  }
}

async function reveal(p, mode = "reveal") {
  const res = await fetch("/api/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: p, mode }),
  });
  toast(res.ok ? (mode === "open" ? "Opened in Finder" : "Revealed in Finder") : "Could not open");
}

async function rescan() {
  await fetch("/api/rescan", { method: "POST" });
  state.doneHandled = false;
  state.largestPath = null;
  toast("Rescan started");
}

async function rescanDir() {
  if (!state.path) return rescan();
  const res = await fetch("/api/rescan_dir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: state.path }),
  });
  const d = await res.json();
  if (d.ok) {
    state.doneHandled = false;
    state.largestPath = null;
    toast("Rescanning " + shortenHome(state.path));
  } else {
    toast(d.error || "Could not rescan");
  }
}

// ---------------------------------------------------------------- data

let treeRequest = 0;

async function loadTree(opts = { animate: false }) {
  const request = ++treeRequest;
  const res = await fetch(
    `/api/tree?path=${encodeURIComponent(state.path)}&depth=4${filterParams()}`
  );
  const data = await res.json();
  if (request !== treeRequest) return; // a newer navigation superseded this response
  if (data.error) {
    if (res.status === 400) return toast(data.error); // bad filter (e.g. invalid regex)
    state.path = ""; // stale path (e.g. mid-scan) — fall back to the top
    return loadTree(opts);
  }
  state.data = data;
  render(opts);
}

async function loadLargest() {
  const scope = state.path;
  const res = await fetch(
    `/api/largest?n=300&path=${encodeURIComponent(scope)}${filterParams()}`
  );
  const data = await res.json();
  if (data.error) return toast(data.error);
  state.largestPath = scope;
  renderLargest(data.files, scope);
}

function largestTabActive() {
  return !$("top").hidden;
}

// Zoom path lives in the URL hash: browser back/forward and deep links work.
function navigate(path) {
  if (path === state.path) return;
  location.hash = path ? "#" + encodeURIComponent(path) : "";
}

function applyLocation() {
  state.path = decodeURIComponent(location.hash.slice(1));
  state.largestPath = null;
  loadTree({ animate: true });
  if (largestTabActive()) loadLargest();
  if (!$("diff").hidden) loadDiff(); // Changes tab follows the zoom
}

function parentPath() {
  const roots = state.status ? state.status.roots : [];
  if (!state.path || roots.includes(state.path)) return "";
  return state.path.slice(0, state.path.lastIndexOf("/")) || "/";
}

function navigateUp() {
  if (!state.path) return;
  navigate(parentPath());
}

// ------------------------------------------------------------- sunburst

// Semantic color pins, stable across zooms and rescans; others fill remaining
// palette slots in size order.
function assignBranchColors(items) {
  const pinned = items.map((item) => {
    if (item.type !== "dir") return null;
    if (/^\/Users\/[^/]+$/.test(item.path || "")) return "#89b4fa"; // home → blue
    if (item.name === "Library") return "#cba6f7"; // any Library → mauve
    return null;
  });
  const used = new Set(pinned.filter(Boolean));
  let slot = 0;
  return items.map((item, i) => {
    if (pinned[i]) return pinned[i];
    while (slot < PALETTE.length && used.has(PALETTE[slot])) slot++;
    const color = PALETTE[slot % PALETTE.length];
    slot++;
    return color;
  });
}

let branchColors = [];

function arcFill(topIdx, depth, type, item) {
  if (type === "rest") return "#45475a";
  let base;
  if (state.ageLens && item) base = ageColor(item);
  else base = type === "dir" ? branchColors[topIdx] || PALETTE[topIdx % 8] : FILE_GRAY;
  const mixPct = Math.min((depth - 1) * 11, 33);
  // deeper rings recede toward the surface so hue still marks the branch
  return mixPct ? `color-mix(in oklab, ${base}, ${SURFACE} ${mixPct}%)` : base;
}

function polar(r, aDeg) {
  const a = ((aDeg - 90) * Math.PI) / 180;
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
}

function arcPathD(r0, r1, a0, a1) {
  if (a1 - a0 >= 359.99) {  // full annulus: two half-arcs to avoid degenerate arc flags
    const mid = a0 + 180;
    return arcPathD(r0, r1, a0, mid) + " " + arcPathD(r0, r1, mid, a1);
  }
  const large = a1 - a0 > 180 ? 1 : 0;
  const [x0, y0] = polar(r1, a0), [x1, y1] = polar(r1, a1);
  const [x2, y2] = polar(r0, a1), [x3, y3] = polar(r0, a0);
  return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r1},${r1} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)} ` +
         `L${x2.toFixed(2)},${y2.toFixed(2)} A${r0},${r0} 0 ${large} 0 ${x3.toFixed(2)},${y3.toFixed(2)} Z`;
}

function collectArcs(root) {
  const arcs = [];
  const total = Math.max(root.size, 1);
  const walk = (items, depth, startA, topIdx) => {
    let a = startA;
    (items || []).forEach((item, i) => {
      const span = (item.size / total) * 360;
      if (span >= MIN_ARC_DEG && depth <= RINGS) {
        const idx = depth === 1 ? i : topIdx;
        arcs.push({ item, depth, a0: a, a1: a + span, topIdx: idx });
        if (item.type === "dir") walk(item.children, depth + 1, a, idx);
      }
      a += span;
    });
  };
  walk(root.children, 1, 0, 0);
  return arcs;
}

// Hover-link registries, rebuilt each render: branch index -> elements.
let arcsByBranch = new Map();
let rowByBranch = new Map();

function setBranchHighlight(idx, on) {
  for (const el of arcsByBranch.get(idx) || []) el.classList.toggle("hl", on);
  const row = rowByBranch.get(idx);
  if (row) row.classList.toggle("hl", on);
}

function render(opts = { animate: false }) {
  const svg = $("sunburst");
  const data = state.data;
  if (!data) return;
  svg.textContent = "";
  svg.classList.toggle("noanim", !opts.animate);
  arcsByBranch = new Map();
  branchColors = assignBranchColors(data.children || []);
  const ns = "http://www.w3.org/2000/svg";

  for (const arc of collectArcs(data)) {
    const r0 = R_HOLE + (arc.depth - 1) * RING_W;
    const p = document.createElementNS(ns, "path");
    p.setAttribute("class", "arc");
    p.setAttribute("d", arcPathD(r0, r0 + RING_W - 1, arc.a0, arc.a1));
    [p._cx, p._cy] = polar(r0 + RING_W / 2, (arc.a0 + arc.a1) / 2); // wedge centroid (viewBox coords)
    p.setAttribute("fill", arcFill(arc.topIdx, arc.depth, arc.item.type, arc.item));
    p.setAttribute("stroke", "var(--surface-1)");
    p.setAttribute("stroke-width", "2");
    if (opts.animate) p.style.animationDelay = `${(arc.depth - 1) * 70}ms`;
    if (arc.item.type !== "rest") {
      if (!arcsByBranch.has(arc.topIdx)) arcsByBranch.set(arc.topIdx, []);
      arcsByBranch.get(arc.topIdx).push(p);
    }
    p.addEventListener("pointerenter", (e) => {
      showTooltip(e, arc.item);
      if (arc.item.type !== "rest") {
        const row = rowByBranch.get(arc.topIdx);
        if (row) row.classList.add("hl");
      }
    });
    p.addEventListener("pointermove", moveTooltip);
    p.addEventListener("pointerleave", () => {
      hideTooltip();
      const row = rowByBranch.get(arc.topIdx);
      if (row) row.classList.remove("hl");
    });
    p.addEventListener("click", () => {
      if (arc.item.type === "dir" && arc.item.path) navigate(arc.item.path);
      else if (arc.item.type === "file") copyPath(arc.item.path);
    });
    svg.appendChild(p);

    const span = arc.a1 - arc.a0;
    if (arc.depth === 1 && span > 14 && arc.item.type !== "rest") {
      svg.appendChild(radialLabel(ns, arc, r0 + RING_W / 2, span));
    }
  }
  svg.appendChild(centerGroup(ns, data));
  if (opts.animate) {
    svg.animate(
      [{ transform: "scale(0.97)", opacity: 0.5 }, { transform: "scale(1)", opacity: 1 }],
      { duration: 240, easing: "ease-out" }
    );
  }
  renderBreadcrumb();
  renderContents(data, opts);
  $("open-current").hidden = !state.path;
  $("rescan-dir-btn").hidden = !state.path;
  $("back-btn").disabled = !state.path;
  const active = filterActive();
  $("filter-summary").hidden = !active;
  $("filter-clear").hidden = !active;
  if (active) {
    $("filter-summary").textContent =
      `${fmtBytes(data.size)} matched · ${fmtCount(data.files || 0)} files`;
  }
  renderAgeLegend();
}

function renderAgeLegend() {
  const legend = $("age-legend");
  legend.hidden = !state.ageLens;
  if (!state.ageLens || legend.childElementCount) return;
  for (const bucket of AGE_BUCKETS) {
    const item = document.createElement("span");
    const chip = document.createElement("i");
    chip.style.background = bucket.color;
    item.append(chip, document.createTextNode(bucket.label));
    legend.appendChild(item);
  }
}

function radialLabel(ns, arc, r, span) {
  const mid = (arc.a0 + arc.a1) / 2;
  const [x, y] = polar(r, mid);
  const t = document.createElementNS(ns, "text");
  let name = shortenHome(arc.item.name);
  if (name.length > 13) name = name.slice(0, 12) + "…";
  t.textContent = span > 40 ? `${name} · ${fmtBytes(arc.item.size)}` : name;
  t.setAttribute("class", "arc-label");
  t.setAttribute("x", x);
  t.setAttribute("y", y);
  t.setAttribute("text-anchor", "middle");
  t.setAttribute("dominant-baseline", "central");
  const rot = mid <= 180 ? mid - 90 : mid + 90;
  t.setAttribute("transform", `rotate(${rot.toFixed(1)},${x.toFixed(1)},${y.toFixed(1)})`);
  return t;
}

function centerGroup(ns, data) {
  const g = document.createElementNS(ns, "g");
  g.id = "center-group";
  const c = document.createElementNS(ns, "circle");
  c.setAttribute("cx", CX); c.setAttribute("cy", CY); c.setAttribute("r", R_HOLE - 8);
  c.setAttribute("fill", "var(--surface-1)");
  g.appendChild(c);
  const mk = (cls, dy, text) => {
    const t = document.createElementNS(ns, "text");
    t.setAttribute("class", cls);
    t.setAttribute("x", CX); t.setAttribute("y", CY + dy);
    t.setAttribute("text-anchor", "middle");
    t.textContent = text;
    g.appendChild(t);
    return t;
  };
  let name = state.path ? shortenHome(state.path) : "All scanned";
  if (name.length > 24) name = "…" + name.slice(-23);
  mk("center-name", -26, name);
  const sizeEl = mk("center-size", 8, "");
  sizeEl._v = state.centerSize;
  tweenNum(sizeEl, data.size, fmtBytes);
  state.centerSize = data.size;
  mk("center-hint", 32, state.path ? "click to go up" : `${fmtCount(data.files || 0)} files`);
  g.addEventListener("click", navigateUp);
  return g;
}

// -------------------------------------------------------------- tooltip

function showTooltip(e, item) {
  const tt = $("tooltip");
  tt.hidden = false;
  const total = state.data ? state.data.size : 0;
  const pct = total ? ((item.size / total) * 100).toFixed(1) + "% of view" : "";
  tt.innerHTML = "";
  const name = document.createElement("div");
  name.className = "tt-name";
  name.textContent = shortenHome(item.name) + (item.type === "dir" ? "/" : "");
  const size = document.createElement("div");
  size.className = "tt-size";
  size.textContent = `${fmtBytes(item.size)} · ${pct}` + (item.files ? ` · ${fmtCount(item.files)} files` : "");
  tt.append(name, size);
  if (item.path) {
    const path = document.createElement("div");
    path.className = "tt-path";
    path.textContent = shortenHome(item.path) + (item.type === "dir" ? "  (click to zoom)" : "  (click to copy path)");
    tt.append(path);
  }
  moveTooltip(e);
}
function moveTooltip(e) {
  const tt = $("tooltip");
  const pad = 14;
  let x = e.clientX + pad, y = e.clientY + pad;
  const r = tt.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tt.style.left = x + "px";
  tt.style.top = y + "px";
}
function hideTooltip() { $("tooltip").hidden = true; }

// -------------------------------------------------------------- sidebar

function actionButtons(path) {
  const wrap = document.createElement("span");
  wrap.className = "row-actions";
  const mk = (label, title, fn) => {
    const b = document.createElement("button");
    b.className = "icon-btn";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(path); });
    wrap.appendChild(b);
  };
  mk("copy", "Copy path", copyPath);
  mk("finder", "Reveal in Finder", (p) => reveal(p));
  return wrap;
}

function renderContents(data, opts = { animate: false }) {
  const panel = $("contents");
  const previousSelected = panel.querySelector(".row.selected")?.dataset.path ?? null;
  panel.textContent = "";
  panel.classList.toggle("noanim", !opts.animate);
  rowByBranch = new Map();
  selIdx = -1;
  selBranch = -1;
  const items = data.children || [];
  const max = Math.max(...items.map((c) => c.size), 1);
  items.forEach((item, i) => {
    const row = document.createElement("div");
    row.className = "row" + (item.type === "dir" && item.path ? " is-dir" : "");
    row.dataset.path = item.path || "";
    row.dataset.type = item.type;
    if (opts.animate) row.style.animationDelay = `${Math.min(i, 20) * 14}ms`;
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = item.type === "rest" ? "var(--surface1)" : arcFill(i, 1, item.type, item);
    const main = document.createElement("div");
    main.className = "row-main";
    const nm = document.createElement("div");
    nm.className = "row-name";
    nm.textContent = shortenHome(item.name) + (item.type === "dir" ? "/" : "");
    if (item.type === "dir" && item.files) {
      const cnt = document.createElement("span");
      cnt.className = "count";
      cnt.textContent = fmtCount(item.files) + " files" + (item.error ? " · partly unreadable" : "");
      nm.appendChild(cnt);
    }
    const bar = document.createElement("div");
    bar.className = "row-bar";
    const fill = document.createElement("i");
    fill.style.width = ((item.size / max) * 100).toFixed(1) + "%";
    fill.style.background = sw.style.background;
    bar.appendChild(fill);
    main.append(nm, bar);
    const size = document.createElement("span");
    size.className = "row-size";
    size.textContent = fmtBytes(item.size);
    row.append(sw, main, size);
    if (item.path) row.append(actionButtons(item.path));
    if (item.type === "dir" && item.path) row.addEventListener("click", () => navigate(item.path));
    else if (item.type === "file") row.addEventListener("click", () => copyPath(item.path));
    if (item.type !== "rest") {
      rowByBranch.set(i, row);
      row.addEventListener("pointerenter", () => setBranchHighlight(i, true));
      row.addEventListener("pointerleave", () => {
        if (i !== selBranch) setBranchHighlight(i, false);
      });
    }
    panel.appendChild(row);
  });
  if (panel.hidden) return;
  const rows = [...panel.querySelectorAll(".row")];
  if (pendingSelect !== null && state.path === pendingSelect.view) {
    const wanted = pendingSelect.row;
    pendingSelect = null;
    const idx = wanted === "__first__" ? 0 : rows.findIndex((r) => r.dataset.path === wanted);
    setSel(idx >= 0 ? idx : 0);
  } else if (previousSelected !== null) {
    // survive live-refresh re-renders: selection only clears via Esc or navigation
    const idx = rows.findIndex((r) => r.dataset.path === previousSelected);
    if (idx >= 0) setSel(idx, { scroll: false });
  }
}

function renderLargest(files, scope) {
  const panel = $("top");
  panel.textContent = "";
  selIdx = -1;
  const label = document.createElement("div");
  label.className = "panel-scope";
  label.textContent = `largest files within ${scope ? shortenHome(scope) : "all scanned roots"}`;
  panel.appendChild(label);
  files.forEach((f, i) => {
    const row = document.createElement("div");
    row.className = "row file-row";
    row.dataset.path = f.path;
    row.dataset.type = "file";
    row.style.animationDelay = `${Math.min(i, 20) * 12}ms`;
    const main = document.createElement("div");
    main.className = "row-main";
    const nm = document.createElement("div");
    nm.className = "row-name";
    nm.textContent = f.path.split("/").pop();
    const path = document.createElement("div");
    path.className = "row-path";
    path.textContent = shortenHome(f.path) + (f.mtime ? ` · modified ${fmtAgeShort(f.mtime)}` : "");
    main.append(nm, path);
    const size = document.createElement("span");
    size.className = "row-size";
    size.textContent = fmtBytes(f.size);
    row.append(main, size, actionButtons(f.path));
    row.addEventListener("click", () => copyPath(f.path));
    panel.appendChild(row);
  });
}

// ---------------------------------------------------- diff / advisor / dupes

const KIND_LABELS = {
  "file-added": "new",
  "file-removed": "removed",
  "file-changed": "grew",
  "dir-added": "new dir",
  "dir-removed": "removed dir",
  "dir-files": "files in dir",
};

async function loadDiff() {
  const panel = $("diff");
  panel.innerHTML = '<div class="panel-note">comparing against snapshot…</div>';
  const res = await fetch(
    `/api/diff?baseline=${state.diffBaseline}&path=${encodeURIComponent(state.path)}`
  );
  const d = await res.json();
  panel.textContent = "";
  selIdx = -1;
  if (!d.available) {
    panel.innerHTML =
      '<div class="panel-note">No previous snapshot yet — history builds up each time a scan completes. Rescan later and changes will show here.</div>';
    return;
  }
  const note = document.createElement("div");
  note.className = "panel-note";
  const span = (Date.now() - d.baseline_saved_at * 1000) / 1000;
  const headline = document.createElement("b");
  headline.textContent = `${fmtDuration(span)} of change`;
  note.append(headline);
  if (d.scope) note.append(` within ${shortenHome(d.scope)}`);
  note.append(" · baseline ");
  const picker = document.createElement("select");
  picker.id = "baseline-select";
  for (const snap of d.snapshots) {
    const option = document.createElement("option");
    option.value = String(snap.stamp);
    option.textContent = `${fmtDuration(snap.age)} ago`;
    picker.appendChild(option);
  }
  if (d.baseline_stamp) picker.value = String(d.baseline_stamp);
  picker.addEventListener("change", () => {
    state.diffBaseline = picker.value;
    loadDiff();
  });
  note.append(picker);
  panel.appendChild(note);
  if (!d.entries.length) {
    panel.insertAdjacentHTML("beforeend", '<div class="panel-note">No significant changes (≥5 MB).</div>');
    return;
  }
  d.entries.forEach((entry, i) => {
    const row = document.createElement("div");
    row.className = "row file-row";
    row.dataset.path = entry.path;
    row.dataset.type = entry.kind.startsWith("dir") ? "dir" : "file";
    row.style.animationDelay = `${Math.min(i, 20) * 12}ms`;
    const main = document.createElement("div");
    main.className = "row-main";
    const nm = document.createElement("div");
    nm.className = "row-name";
    nm.textContent = entry.path.split("/").pop();
    const tag = document.createElement("span");
    tag.className = "kind-tag";
    tag.textContent = KIND_LABELS[entry.kind] || entry.kind;
    nm.appendChild(tag);
    const path = document.createElement("div");
    path.className = "row-path";
    path.textContent = shortenHome(entry.path);
    main.append(nm, path);
    const delta = document.createElement("span");
    delta.className = "delta " + (entry.delta > 0 ? "grew" : "freed");
    delta.textContent = (entry.delta > 0 ? "+" : "−") + fmtBytes(Math.abs(entry.delta));
    row.append(main, delta, actionButtons(entry.path));
    row.addEventListener("click", () => {
      if (row.dataset.type === "dir") navigate(entry.path);
      else copyPath(entry.path);
    });
    panel.appendChild(row);
  });
}

async function loadAdvisor() {
  const panel = $("advisor");
  panel.innerHTML = '<div class="panel-note">checking known reclaimable locations…</div>';
  const { entries } = await (await fetch("/api/advisor")).json();
  panel.textContent = "";
  selIdx = -1;
  const total = entries.reduce((a, e) => a + e.size, 0);
  const note = document.createElement("div");
  note.className = "panel-note";
  note.innerHTML = `<b>${fmtBytes(total)}</b> in caches and rebuildable data. Commands are suggestions — copy, review, run them yourself.`;
  panel.appendChild(note);
  entries.forEach((entry, i) => {
    const block = document.createElement("div");
    block.className = "group";
    block.style.animationDelay = `${Math.min(i, 20) * 14}ms`;
    const head = document.createElement("div");
    head.className = "group-head";
    const title = document.createElement("span");
    title.textContent = entry.title;
    const size = document.createElement("span");
    size.className = "wasted";
    size.textContent = fmtBytes(entry.size);
    head.append(title, size);
    block.appendChild(head);
    if (entry.path) {
      const path = document.createElement("div");
      path.className = "path-line";
      path.textContent = shortenHome(entry.path);
      path.style.cursor = "pointer";
      path.title = "Zoom to this folder";
      path.addEventListener("click", () => navigate(entry.path));
      block.appendChild(path);
    }
    const noteLine = document.createElement("div");
    noteLine.className = "path-line";
    noteLine.textContent = entry.note;
    block.appendChild(noteLine);
    const cmd = document.createElement("code");
    cmd.className = "cmd";
    cmd.textContent = entry.command;
    cmd.title = "Click to copy";
    cmd.addEventListener("click", () => copyPath(entry.command));
    block.appendChild(cmd);
    panel.appendChild(block);
  });
}

async function loadDupes() {
  const panel = $("dupes");
  panel.innerHTML = '<div class="panel-note">fingerprinting size-matched files (head+tail hash)…</div>';
  const d = await (await fetch("/api/duplicates")).json();
  panel.textContent = "";
  selIdx = -1;
  const note = document.createElement("div");
  note.className = "panel-note";
  note.innerHTML = d.groups.length
    ? `<b>${fmtBytes(d.wasted)}</b> in probable duplicates (files ≥50 MB, matched by size + head/tail fingerprint). ` +
      `Use <b>verify full SHA-256</b> on a group before acting on it. Suggested commands keep the ` +
      `<b style="color:var(--green)">green</b> copy and symlink the rest to it.`
    : "No probable duplicates found among files ≥50 MB.";
  panel.appendChild(note);
  d.groups.forEach((group, i) => {
    const block = document.createElement("div");
    block.className = "group";
    block.style.animationDelay = `${Math.min(i, 20) * 14}ms`;
    const head = document.createElement("div");
    head.className = "group-head";
    const label = document.createElement("span");
    label.textContent = `${fmtBytes(group.size)} × ${group.files.length}`;
    const wasted = document.createElement("span");
    wasted.className = "wasted";
    wasted.textContent = `${fmtBytes(group.wasted)} reclaimable`;
    head.append(label, wasted);
    block.appendChild(head);
    for (const file of group.files) {
      const line = document.createElement("div");
      line.className = "path-line" + (file === group.keep ? " keep" : "");
      const text = document.createElement("span");
      text.className = "path-text";
      text.textContent = (file === group.keep ? "keep  " : "dup   ") + shortenHome(file);
      text.title = "Click to copy path";
      text.addEventListener("click", () => copyPath(file));
      const finder = document.createElement("button");
      finder.className = "icon-btn";
      finder.textContent = "finder";
      finder.title = "Reveal in Finder";
      finder.addEventListener("click", (e) => {
        e.stopPropagation();
        reveal(file);
      });
      line.append(text, finder);
      block.appendChild(line);
    }
    const verify = document.createElement("button");
    verify.className = "icon-btn";
    verify.textContent = "verify full SHA-256";
    verify.title = "Hash every byte of each file to confirm they are identical";
    verify.addEventListener("click", async () => {
      verify.disabled = true;
      verify.textContent = `hashing ${fmtBytes(group.size * group.files.length)}…`;
      try {
        const res = await fetch("/api/duplicates/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paths: group.files }),
        });
        const v = await res.json();
        const badge = document.createElement("span");
        badge.className = "verify-badge " + (v.match ? "ok" : "bad");
        const digest = Object.values(v.hashes).find(Boolean) || "";
        badge.textContent = v.match
          ? ` ✓ identical (sha256 ${digest.slice(0, 12)}…)`
          : " ✗ full hashes differ — NOT duplicates, do not delete";
        verify.replaceWith(badge);
      } catch {
        verify.disabled = false;
        verify.textContent = "verify full SHA-256";
        toast("Verification failed");
      }
    });
    block.appendChild(verify);
    for (const command of group.commands) {
      const cmd = document.createElement("code");
      cmd.className = "cmd";
      cmd.textContent = command;
      cmd.title = "Click to copy — review before running (verify SHA first)";
      cmd.addEventListener("click", () => copyPath(command));
      block.appendChild(cmd);
    }
    panel.appendChild(block);
  });
}

function renderBreadcrumb() {
  const bc = $("breadcrumb");
  bc.textContent = "";
  const roots = state.status ? state.status.roots : [];
  const link = (label, target) => {
    const a = document.createElement("a");
    a.textContent = label;
    a.addEventListener("click", () => navigate(target));
    bc.appendChild(a);
  };
  const sep = () => {
    const s = document.createElement("span");
    s.className = "sep";
    s.textContent = "›";
    bc.appendChild(s);
  };
  link("All", "");
  if (!state.path) return;
  const parts = state.path.split("/").filter(Boolean);
  let acc = "";
  for (const part of parts) {
    acc += "/" + part;
    sep();
    const inRoots = roots.some((r) => acc === r || acc.startsWith(r.replace(/\/$/, "") + "/"));
    if (inRoots && acc !== state.path) link(part, acc);
    else {
      const s = document.createElement("span");
      s.textContent = part;
      bc.appendChild(s);
    }
  }
}

// --------------------------------------------------------------- status

function fmtAge(sec) {
  if (sec < 90) return "just now";
  if (sec < 5400) return `${Math.round(sec / 60)}m ago`;
  return `${(sec / 3600).toFixed(1)}h ago`;
}

function fmtDuration(sec) {
  if (sec < 5400) return `${Math.max(1, Math.round(sec / 60))} min`;
  if (sec < 129600) return `${(sec / 3600).toFixed(1)} h`;
  return `${(sec / 86400).toFixed(1)} days`;
}

function renderStatus(st) {
  const freeEl = $("free-val");
  tweenNum(freeEl, st.disk.free, fmtBytes);
  freeEl.classList.toggle("critical", st.disk.free < 20e9);
  tweenNum($("scanned-val"), st.bytes, fmtBytes);
  tweenNum($("files-val"), st.files, fmtCount);
  const busy = st.state !== "done";
  if (st.state === "scanning") {
    $("status-val").textContent = "scanning…";
    $("status-sub").textContent = st.errors ? `${fmtCount(st.errors)} unreadable (skipped)` : "";
    $("scan-current").textContent = shortenHome(st.current || "");
  } else if (st.state === "rescanning") {
    const r = st.rescan || {};
    $("status-val").textContent = r.scope ? "rescanning dir…" : "rescanning…";
    $("status-sub").textContent = st.cache_age != null ? `showing scan from ${fmtAge(st.cache_age)}` : "showing previous scan";
    $("scan-current").textContent =
      (r.scope ? `${shortenHome(r.scope)} · ` : "") +
      `${fmtCount(r.files || 0)} files · ${fmtBytes(r.bytes || 0)} rescanned · ` +
      shortenHome(r.current || "");
  } else {
    $("status-val").textContent = st.cache_age != null ? `cached ${fmtAge(st.cache_age)}` : `done in ${st.elapsed}s`;
    $("status-sub").textContent = st.errors ? `${fmtCount(st.errors)} unreadable (skipped)` : "all readable";
  }
  $("scanbar").hidden = !busy;
  $("cancel-rescan").hidden = st.state !== "rescanning"; // initial scan can't be cancelled
  $("rescan-btn").classList.toggle("spinning", busy);
  const home = st.roots.find((r) => /^\/Users\/[^/]+$/.test(r));
  $("trash-btn").hidden = !home;
}

async function poll() {
  try {
    const st = await (await fetch("/api/status")).json();
    state.status = st;
    renderStatus(st);
    if (st.state !== "done") state.doneHandled = false;
    if (st.state === "done" && !state.doneHandled) {
      state.doneHandled = true;
      state.largestPath = null;
      state.liveStamp = st.live?.last_change || 0;
      await loadTree({ animate: true });
      const active = document.querySelector(".tab.active")?.dataset.tab;
      if (TAB_LOADERS[active]) TAB_LOADERS[active]();
      if (state.lastScanState && state.lastScanState !== "done") toast("Scan complete — view updated");
    } else if (
      st.state === "done" &&
      st.live?.enabled &&
      st.live.last_change > state.liveStamp
    ) {
      // filesystem changed under us — quietly refresh the current view
      state.liveStamp = st.live.last_change;
      loadTree({ animate: false });
    }
    $("live-ind").hidden = !st.live?.enabled;
    state.lastScanState = st.state;
  } catch { /* server briefly busy — keep polling */ }
  setTimeout(poll, 1000);
}

async function refreshLoop() {
  if (state.status && state.status.state === "scanning") {
    await loadTree({ animate: false }).catch(() => {});
  }
  setTimeout(refreshLoop, 3000);
}

// ------------------------------------------------------------- keyboard

let selIdx = -1;
let selBranch = -1; // branch currently highlighted on the graph by selection
// Row to auto-select once the contents render for `view` lands ({row, view});
// gated on the view path so a racing refresh render can't consume it early.
let pendingSelect = null;

function visiblePanel() {
  return document.querySelector(".panel:not([hidden])");
}
function panelRows() {
  return [...visiblePanel().querySelectorAll(".row")];
}

function clearGraphSelection() {
  if (selBranch >= 0) setBranchHighlight(selBranch, false);
  selBranch = -1;
  hideTooltip();
}

function setSel(idx, opts = { scroll: true }) {
  const rows = panelRows();
  if (!rows.length) return;
  selIdx = Math.max(0, Math.min(rows.length - 1, idx));
  rows.forEach((r, i) => r.classList.toggle("selected", i === selIdx));
  if (opts.scroll) rows[selIdx].scrollIntoView({ block: "nearest" });
  clearGraphSelection();
  if (visiblePanel().id !== "contents") return;
  // mirror the selection on the sunburst: raise the branch, pin its tooltip
  const item = (state.data?.children || [])[selIdx];
  if (!item) return;
  const arc = item.type === "rest" ? null : (arcsByBranch.get(selIdx) || [])[0];
  if (item.type !== "rest") {
    selBranch = selIdx;
    setBranchHighlight(selBranch, true);
  }
  // rest rows and thin slices have no arc — anchor the tooltip to the row instead
  let anchor;
  const ctm = arc && $("sunburst").getScreenCTM();
  if (arc && ctm) {
    // project the wedge centroid from viewBox coords to screen coords
    const point = new DOMPoint(arc._cx, arc._cy).matrixTransform(ctm);
    anchor = { clientX: point.x, clientY: point.y };
  } else {
    const box = rows[selIdx].getBoundingClientRect();
    anchor = { clientX: Math.max(8, box.x - 440), clientY: box.y };
  }
  showTooltip(anchor, item);
}

function clearSelection() {
  selIdx = -1;
  panelRows().forEach((r) => r.classList.remove("selected"));
  clearGraphSelection();
}

function selectedRow() {
  return panelRows()[selIdx] || null;
}

const TAB_ORDER = ["contents", "top", "diff", "advisor", "dupes"];

function cycleTab(backwards) {
  const current = document.querySelector(".tab.active")?.dataset.tab || "contents";
  const idx = TAB_ORDER.indexOf(current);
  const next = TAB_ORDER[(idx + (backwards ? -1 : 1) + TAB_ORDER.length) % TAB_ORDER.length];
  document.querySelector(`.tab[data-tab="${next}"]`).click();
}

const KEY_ACTIONS = {
  j: () => setSel(selIdx >= panelRows().length - 1 ? 0 : selIdx + 1), // wraps
  k: () => setSel(selIdx <= 0 ? Infinity : selIdx - 1), // wraps (and k-from-nothing = bottom)
  g: () => setSel(0),
  G: () => setSel(Infinity),
  h: () => {
    if (!state.path) return;
    pendingSelect = { row: state.path, view: parentPath() }; // re-select the dir we left
    navigateUp();
  },
  l: () => {
    const row = selectedRow();
    if (row && row.dataset.type === "dir" && row.dataset.path) {
      pendingSelect = { row: "__first__", view: row.dataset.path };
      navigate(row.dataset.path);
    }
  },
  Tab: (e) => cycleTab(e.shiftKey),
  "/": () => $("filter-input").focus(),
  Escape: () => {
    if (selIdx >= 0) return clearSelection(); // first esc: deselect
    if (filterActive()) return clearFilter(); // second: drop the filter
  },
  Enter: () => {
    const path = selectedRow()?.dataset.path || state.path;
    if (path) reveal(path);
  },
  y: () => {
    const path = selectedRow()?.dataset.path || state.path;
    if (path) copyPath(path);
  },
  r: () => rescanDir(),
  R: () => rescan(),
  a: () => $("age-btn").click(),
};

document.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement) {
    if (e.key === "Escape" || e.key === "Enter") e.target.blur();
    return; // typing in the filter box — vim keys stay out of the way
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const action = KEY_ACTIONS[e.key];
  if (!action) return;
  e.preventDefault();
  action(e);
});

// ----------------------------------------------------------------- init

const TAB_LOADERS = { top: loadLargest, diff: loadDiff, advisor: loadAdvisor, dupes: loadDupes };

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== tab.dataset.tab));
    $(tab.dataset.tab).hidden = false;
    selIdx = -1;
    const load = TAB_LOADERS[tab.dataset.tab];
    if (tab.dataset.tab === "top") {
      if (state.largestPath !== state.path) loadLargest();
    } else if (load) {
      load();
    }
  });
});
// ------------------------------------------------------- filter controls

function applyFilter() {
  state.largestPath = null;
  loadTree({ animate: true });
  if (largestTabActive()) loadLargest();
}

function clearFilter() {
  state.filter.q = "";
  state.filter.exts.clear();
  $("filter-input").value = "";
  document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
  applyFilter();
}

let filterDebounce;
$("filter-input").addEventListener("input", (e) => {
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(() => {
    state.filter.q = e.target.value;
    applyFilter();
  }, 350);
});
document.querySelectorAll("#filter-mode .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#filter-mode .seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.filter.mode = btn.dataset.mode;
    if (state.filter.q.trim()) applyFilter();
  });
});
document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const exts = chip.dataset.exts.split(",");
    const on = chip.classList.toggle("active");
    for (const ext of exts) on ? state.filter.exts.add(ext) : state.filter.exts.delete(ext);
    applyFilter();
  });
});
$("filter-clear").addEventListener("click", clearFilter);

$("rescan-btn").addEventListener("click", rescan);
$("rescan-dir-btn").addEventListener("click", rescanDir);
$("cancel-rescan").addEventListener("click", async () => {
  const res = await fetch("/api/rescan/cancel", { method: "POST" });
  const d = await res.json();
  toast(d.ok ? "Rescan cancelled" : d.error || "Nothing to cancel");
});
$("age-btn").addEventListener("click", () => {
  state.ageLens = !state.ageLens;
  $("age-btn").classList.toggle("active", state.ageLens);
  render({ animate: true });
});
$("open-current").addEventListener("click", () => {
  if (state.path) reveal(state.path, "open");
});
$("trash-btn").addEventListener("click", () => {
  const home = (state.status?.roots || []).find((r) => /^\/Users\/[^/]+$/.test(r));
  if (home) navigate(home + "/.Trash");
});
$("back-btn").addEventListener("click", () => {
  // real history when we have it; otherwise step up a level
  if (window.navigation?.canGoBack) history.back();
  else navigateUp();
});
window.addEventListener("hashchange", applyLocation);

poll();
applyLocation();
refreshLoop();
