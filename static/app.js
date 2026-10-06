// PDF Editor front end. The server does all PDF work; this file draws pages,
// turns mouse input into operations, and re-renders after each change.
// All coordinates sent to the server are PDF points of the page *as displayed*.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  doc: null,          // summary of the active document from the server
  tabs: [],           // [{id, name}] open documents
  scroll: {},         // doc id -> scrollTop, restored when switching tabs
  scale: 1.25,        // CSS pixels per PDF point
  tool: "select",
  selected: new Set(),
  lastClicked: null,
  currentPage: 0,
  pendingImage: null, // data URL waiting to be placed
  imgSel: null,       // selected image for the Images tool
  reselect: null,     // {page, rect}: select the image here after the next refresh
  caps: { ai: { configured: false }, tesseract: false },
  chats: {},          // doc id -> [{role, content}]
};

const TOOL_HINTS = {
  edittext: "Click a dashed line of text to change it. Enter saves, Esc cancels.",
  addtext: "Click where the text should go. Enter saves, Shift+Enter for a new line. B / I / alignment are in the toolbar.",
  image: "Click an image to select it. Drag to move, drag the corner to resize (Shift = free), or use Rotate / Crop / Delete.",
  highlight: "Drag over the text you want to highlight.",
  underline: "Drag over the text you want to underline.",
  strikeout: "Drag over the text you want to strike out.",
  note: "Click where the sticky note should go.",
  ink: "Draw with the mouse or finger. Each stroke is saved when you release.",
  rect: "Drag to draw a rectangle.",
  ellipse: "Drag to draw an ellipse.",
  line: "Drag to draw a line.",
  arrow: "Drag to draw an arrow.",
  whiteout: "Drag over an area to permanently erase it (text, images and drawings).",
  redact: "Drag over an area to permanently remove it and black it out.",
  croppage: "Drag over the part of the page to keep.",
  sign: "Drag a box on the page where the signature/image should go (or just click).",
  date: "Click where today's date should go. The format is in the toolbar.",
  form: "Fill in the blue fields, then press “Save form values”.",
  field: "Drag a box where the new form field should go.",
  eraser: "Click a red box to remove that annotation.",
};

// ------------------------------------------------------------ server calls

async function api(path, opts = {}) {
  showBusy(true);
  try {
    const res = await fetch(path, opts);
    const isJson = (res.headers.get("content-type") || "").includes("json");
    const body = isJson ? await res.json() : res;
    if (!res.ok) {
      const err = new Error((isJson && body.error) || `Request failed (${res.status})`);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  } finally {
    showBusy(false);
  }
}

const postJson = (path, data) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

const docUrl = (suffix = "") => `/api/doc/${state.doc.id}${suffix}`;

async function op(data) {
  try {
    const summary = await postJson(docUrl("/op"), data);
    applySummary(summary);
    if (summary.message) toast(summary.message);
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

async function downloadPost(path, data) {
  showBusy(true);
  try {
    const res = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}),
    });
    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try { msg = (await res.json()).error || msg; } catch {}
      throw new Error(msg);
    }
    saveBlob(await res.blob(), filenameFrom(res) || "download");
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  } finally {
    showBusy(false);
  }
}

function filenameFrom(res) {
  const cd = res.headers.get("content-disposition") || "";
  const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) return decodeURIComponent(star[1]);
  const m = cd.match(/filename="?([^";]+)"?/i);
  return m ? m[1] : null;
}

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

const readAsDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(r.error);
  r.readAsDataURL(file);
});

// ------------------------------------------------------------ tabs / open

async function openFiles(files) {
  for (const f of files) await openFile(f);
}

async function openFile(file, password) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  if (password) fd.append("password", password);
  try {
    const summary = await api("/api/open", { method: "POST", body: fd });
    addTab(summary);
  } catch (e) {
    if (e.status === 401) {
      const pw = prompt(password ? "Wrong password. Try again:" : `“${file.name}” is password protected. Password:`);
      if (pw) await openFile(file, pw);
    } else toast(e.message, true);
  }
}

async function importFiles(files) {
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  try {
    const summary = await api("/api/import", { method: "POST", body: fd });
    addTab(summary);
    toast(summary.message || `Created a PDF from ${files.length} file(s).`);
  } catch (e) { toast(e.message, true); }
}

async function insertFiles(files) {
  if (!files.length || !state.doc) return;
  const sel = selectedPages();
  const after = sel.length ? sel[sel.length - 1] : state.doc.pages.length - 1;
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  fd.append("after", String(after));
  try {
    const summary = await api(docUrl("/merge"), { method: "POST", body: fd });
    applySummary(summary);
    toast(`Inserted ${files.length} file(s) after page ${after + 1}.`);
  } catch (e) { toast(e.message, true); }
}

function addTab(summary) {
  if (!state.tabs.some((t) => t.id === summary.id)) state.tabs.push({ id: summary.id, name: summary.name });
  showDoc(summary);
}

function showDoc(summary) {
  if (state.doc) state.scroll[state.doc.id] = $("#viewer").scrollTop;
  state.selected.clear();
  state.imgSel = null;
  applySummary(summary, true);
  // New documents open at fit-to-width on phones, or whenever the page would not fit.
  const tooWide = Math.max(...summary.pages.map((p) => p.w)) * state.scale > $("#viewer").clientWidth - 40;
  if (!(summary.id in state.scroll) && (isPhone() || tooWide)) fitWidth();
  $("#viewer").scrollTop = state.scroll[summary.id] || 0;
  try { localStorage.setItem("pdfeditor.activeTab", summary.id); } catch {}
  renderAiLog();
}

async function activateTab(id) {
  if (state.doc && state.doc.id === id) return;
  try { showDoc(await api(`/api/doc/${id}`)); }
  catch (e) {
    toast(e.message, true);
    state.tabs = state.tabs.filter((t) => t.id !== id);
    renderTabs();
  }
}

async function closeTab(id) {
  const tab = state.tabs.find((t) => t.id === id);
  if (!tab) return;
  if (!confirm(`Close “${tab.name}”?\nDownload it first if you want to keep your changes.`)) return;
  try { await api(`/api/doc/${id}`, { method: "DELETE" }); } catch {}
  const i = state.tabs.findIndex((t) => t.id === id);
  state.tabs.splice(i, 1);
  delete state.chats[id];
  if (state.doc && state.doc.id === id) {
    const next = state.tabs[i] || state.tabs[i - 1];
    state.doc = null;
    if (next) await activateTab(next.id);
    else showEmpty();
  }
  renderTabs();
}

function renderTabs() {
  const bar = $("#tabsBar");
  bar.hidden = state.tabs.length === 0;
  bar.innerHTML = "";
  for (const t of state.tabs) {
    const el = document.createElement("div");
    el.className = "doc-tab" + (state.doc && state.doc.id === t.id ? " active" : "");
    el.title = t.name;
    el.innerHTML = `<span class="name">${esc(t.name)}</span><button class="close" title="Close">×</button>`;
    el.addEventListener("click", () => activateTab(t.id));
    el.addEventListener("auxclick", (e) => { if (e.button === 1) closeTab(t.id); });
    $(".close", el).addEventListener("click", (e) => { e.stopPropagation(); closeTab(t.id); });
    bar.appendChild(el);
  }
}

function showEmpty() {
  state.doc = null;
  $("#pages").innerHTML = "";
  $("#thumbs").innerHTML = "";
  $("#dropZone").hidden = false;
  $("#docName").textContent = "No document";
  $("#mobileTitle").textContent = "PDF Editor";
  $("#saveState").textContent = "";
  document.title = "PDF Editor";
  updateSelInfo();
  updateButtons();
  renderAiLog();
}

// Re-open the documents that were open last time (they are auto-saved on the server).
async function restoreTabs() {
  let list = [];
  try { list = await (await fetch("/api/docs")).json(); } catch { return; }
  state.tabs = list;
  if (!list.length) { renderTabs(); return; }
  let active = null;
  try { active = localStorage.getItem("pdfeditor.activeTab"); } catch {}
  const pick = list.find((t) => t.id === active) || list[list.length - 1];
  await activateTab(pick.id);
  if (list.length) toast(`Restored ${list.length} open document(s).`);
}

// ------------------------------------------------------------ rendering

function applySummary(summary, fresh = false) {
  const prev = state.doc;
  state.doc = summary;
  const tab = state.tabs.find((t) => t.id === summary.id);
  if (tab) tab.name = summary.name;
  for (const n of [...state.selected]) if (n >= summary.pages.length) state.selected.delete(n);
  $("#dropZone").hidden = true;
  $("#docName").textContent = summary.name;
  $("#mobileTitle").textContent = summary.name;
  $("#saveState").textContent = !summary.edited ? ""
    : state.caps.hosted ? "✓ Changes saved on the server while you work (download to keep them)"
    : "✓ Changes auto-saved on this computer";
  document.title = `${summary.name} – PDF Editor`;
  const keepScroll = !fresh && prev && prev.id === summary.id ? $("#viewer").scrollTop : null;
  renderPages();
  renderThumbs();
  renderTabs();
  if (keepScroll != null) $("#viewer").scrollTop = keepScroll;
  updateButtons();
  if (summary.hasForms && fresh && !summary.edited) toast("This PDF has form fields. Use “Fill form” to fill them in.");
}

function pageImgUrl(n, zoom) {
  return docUrl(`/page/${n}.png?zoom=${zoom.toFixed(2)}&v=${state.doc.version}`);
}

function renderPages() {
  const wrap = $("#pages");
  wrap.innerHTML = "";
  const dpr = window.devicePixelRatio || 1;
  const zoom = Math.min(state.scale * dpr, 4);
  state.doc.pages.forEach((p, n) => {
    const div = document.createElement("div");
    div.className = "page";
    div.dataset.page = n;
    div.style.width = `${p.w * state.scale}px`;
    div.style.height = `${p.h * state.scale}px`;
    div.innerHTML = `<span class="page-label">Page ${n + 1}</span>
      <img loading="lazy" alt="Page ${n + 1}" src="${pageImgUrl(n, zoom)}">
      <div class="overlay"></div>`;
    wrap.appendChild(div);
    attachOverlay($(".overlay", div), n);
  });
  $("#zoomLabel").textContent = `${Math.round(state.scale / 1.25 * 100)}%`;
  refreshToolLayer();
}

function renderThumbs() {
  const wrap = $("#thumbs");
  wrap.innerHTML = "";
  const dpr = window.devicePixelRatio || 1;
  state.doc.pages.forEach((p, n) => {
    const t = document.createElement("div");
    t.className = "thumb" + (state.selected.has(n) ? " selected" : "") + (n === state.currentPage ? " current" : "");
    t.draggable = true;
    t.dataset.page = n;
    const z = Math.min(150 / p.w, 200 / p.h) * dpr;
    t.innerHTML = `<img loading="lazy" src="${pageImgUrl(n, z)}" alt=""><div class="num">${n + 1}</div>`;
    t.addEventListener("click", (e) => onThumbClick(e, n));
    t.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/x-page", String(n)); e.dataTransfer.effectAllowed = "move"; });
    t.addEventListener("dragover", (e) => {
      if (!e.dataTransfer.types.includes("text/x-page")) return;
      e.preventDefault();
      const after = e.offsetY > t.offsetHeight / 2;
      t.classList.toggle("drop-after", after);
      t.classList.toggle("drop-before", !after);
    });
    t.addEventListener("dragleave", () => t.classList.remove("drop-after", "drop-before"));
    t.addEventListener("drop", (e) => {
      if (!e.dataTransfer.types.includes("text/x-page")) return;
      e.preventDefault();
      e.stopPropagation();
      t.classList.remove("drop-after", "drop-before");
      const from = Number(e.dataTransfer.getData("text/x-page"));
      const after = e.offsetY > t.offsetHeight / 2;
      movePage(from, n + (after ? 1 : 0));
    });
    wrap.appendChild(t);
  });
  updateSelInfo();
}

function onThumbClick(e, n) {
  if (isPhone()) {
    // No Ctrl/Shift on a phone: taps add or remove pages from the selection.
    state.selected.has(n) ? state.selected.delete(n) : state.selected.add(n);
    if (state.selected.has(n)) $(`.page[data-page="${n}"]`)?.scrollIntoView({ block: "start" });
  } else if (e.shiftKey && state.lastClicked != null) {
    const [a, b] = [Math.min(n, state.lastClicked), Math.max(n, state.lastClicked)];
    for (let i = a; i <= b; i++) state.selected.add(i);
  } else if (e.ctrlKey || e.metaKey) {
    state.selected.has(n) ? state.selected.delete(n) : state.selected.add(n);
  } else {
    state.selected = new Set([n]);
    $(`.page[data-page="${n}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  state.lastClicked = n;
  $$(".thumb").forEach((t) => t.classList.toggle("selected", state.selected.has(Number(t.dataset.page))));
  updateSelInfo();
  updateButtons();
}

function updateSelInfo() {
  const k = state.selected.size;
  $("#selInfo").textContent = state.doc
    ? `${state.doc.pages.length} page(s)` + (k ? ` · ${k} selected` : "") + ` · ${formatSize(state.doc.size)}`
    : "";
}

const formatSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);

function updateButtons() {
  const has = !!state.doc;
  $$(".needs-doc").forEach((b) => (b.disabled = !has));
  $$(".tool").forEach((b) => (b.disabled = !has && b.dataset.tool !== "select"));
  $$(".needs-sel").forEach((b) => (b.disabled = !has || state.selected.size === 0));
  $("#undoBtn").disabled = !has || !state.doc.canUndo;
  $("#redoBtn").disabled = !has || !state.doc.canRedo;
  $("#insertInput").disabled = !has;
}

// Move the selected pages one place up (-1) or down (+1), keeping them selected.
function moveSelected(step) {
  const sel = selectedPages();
  const order = state.doc.pages.map((_, i) => i);
  if (!sel.length || (step < 0 && sel[0] === 0) || (step > 0 && sel[sel.length - 1] === order.length - 1)) return;
  for (const n of step < 0 ? sel : [...sel].reverse()) {
    const i = order.indexOf(n);
    [order[i], order[i + step]] = [order[i + step], order[i]];
  }
  const moved = new Set(sel.map((n) => order.indexOf(n)));
  op({ op: "reorder", order }).then((ok) => {
    if (!ok) return;
    state.selected = moved;
    renderThumbs();
    updateButtons();
  });
}

function movePage(from, to) {
  const order = state.doc.pages.map((_, i) => i);
  order.splice(from, 1);
  order.splice(to > from ? to - 1 : to, 0, from);
  if (order.every((v, i) => v === i)) return;
  state.selected.clear();
  op({ op: "reorder", order });
}

// Track which page is in view (for the thumbnail highlight and "fit page").
function updateCurrentPage() {
  if (!state.doc) return;
  const viewer = $("#viewer");
  const mid = viewer.getBoundingClientRect().top + viewer.clientHeight / 3;
  let cur = 0;
  for (const p of $$(".page")) {
    if (p.getBoundingClientRect().top <= mid) cur = Number(p.dataset.page);
    else break;
  }
  if (cur !== state.currentPage) {
    state.currentPage = cur;
    $$(".thumb").forEach((t) => t.classList.toggle("current", Number(t.dataset.page) === cur));
  }
}

// ------------------------------------------------------------ tools

function setTool(tool) {
  if (tool === "sign") { openSignDialog(); return; }
  state.tool = tool;
  state.imgSel = null;
  document.body.dataset.tool = tool;
  markTool(tool);
  showHint(TOOL_HINTS[tool]);
  refreshToolLayer();
}

// The tool tip stays on large screens; on phones it fades after a few seconds
// so it doesn't cover the page.
let hintTimer;
function showHint(text) {
  const el = $("#toolHint");
  clearTimeout(hintTimer);
  el.hidden = !text;
  el.textContent = text || "";
  el.classList.remove("faded");
  if (text && isPhone()) hintTimer = setTimeout(() => el.classList.add("faded"), 4000);
}

// Highlight the active tool, and the group button whose menu holds it.
function markTool(tool) {
  $$(".tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  $$(".tool-menu").forEach((m) => {
    const btn = $(".tool-group", m);
    if (btn) btn.classList.toggle("active", !!$(`.tool[data-tool="${tool}"]`, m));
  });
}

// Draw tool-specific helpers (text boxes, annotation boxes, images, form fields) on top of pages.
async function refreshToolLayer() {
  $$(".overlay").forEach((o) => (o.innerHTML = ""));
  $("#formBar").hidden = state.tool !== "form" || !state.doc;
  if (!state.doc) return;
  const kind = { edittext: "lines", eraser: "annots", form: "widgets", image: "images" }[state.tool];
  if (!kind) return;
  const { id, version } = state.doc;
  let results;
  try {
    results = await Promise.all(
      state.doc.pages.map((_, n) => fetch(`/api/doc/${id}/page/${n}/${kind}`).then((r) => r.json()))
    );
  } catch { return; }
  if (!state.doc || state.doc.id !== id || version !== state.doc.version) return; // changed meanwhile
  results.forEach((items, n) => {
    const overlay = $(`.page[data-page="${n}"] .overlay`);
    if (!overlay || !Array.isArray(items)) return;
    if (kind === "lines") items.forEach((it) => addTextLineBox(overlay, n, it));
    if (kind === "annots") items.forEach((it) => addAnnotBox(overlay, n, it));
    if (kind === "widgets") items.forEach((it) => addFormField(overlay, it));
    if (kind === "images") items.forEach((it) => addImageBox(overlay, n, it));
  });
  if (kind === "images" && state.reselect) {
    const { page, rect } = state.reselect;
    state.reselect = null;
    const boxes = $$(`.page[data-page="${page}"] .img-box`);
    let best = null, bestD = Infinity;
    for (const b of boxes) {
      const d = b._img.bbox.reduce((s, v, i) => s + Math.abs(v - rect[i]), 0);
      if (d < bestD) { best = b; bestD = d; }
    }
    if (best && bestD < 20) selectImage(best);
  }
  if (kind === "annots" && results.every((r) => !r.length)) toast("There are no annotations to remove.");
  if (kind === "images" && results.every((r) => !r.length)) toast("There are no editable images. Use “Sign / Image” to add one.");
  if (kind === "widgets" && results.every((r) => !r.length)) toast("This PDF has no fillable form fields. Use “+ Field” to add some, or “Add text” to type on it.");
  return results;
}

const px = (v) => `${v * state.scale}px`;

function place(el, [x0, y0, x1, y1]) {
  el.style.left = px(x0); el.style.top = px(y0);
  el.style.width = px(x1 - x0); el.style.height = px(y1 - y0);
}

function addTextLineBox(overlay, n, line) {
  const box = document.createElement("div");
  box.className = "text-line";
  box.title = line.match ? `${line.text}\nFont: ${line.match.why}` : line.text;
  place(box, line.bbox);
  box.addEventListener("pointerdown", (e) => e.stopPropagation());
  box.addEventListener("click", (e) => {
    e.stopPropagation();
    const [x0, y0, x1, y1] = line.bbox;
    const input = document.createElement("input");
    input.className = "inline-edit";
    input.value = line.text;
    input.style.left = px(x0);
    input.style.top = px(y0);
    input.style.width = `${Math.max((x1 - x0) * state.scale + 40, 120)}px`;
    input.style.height = px(y1 - y0 + 2);
    input.style.fontSize = px(line.size * 0.95);
    input.style.color = line.color;
    input.dir = "auto";
    // Show the text in the font that will be used: the one picked in the Font
    // menu, or (Automatic) the PDF's own font / its closest look-alike.
    const auto = $("#fontSelect").value === "auto";
    input.style.fontFamily = previewFont(line.font);
    let badge = null;
    if (auto && line.match) {
      useFontFace(line.match.id).then((family) => {
        if (family) input.style.fontFamily = `"${family}", ${previewFont(line.font)}`;
      });
      badge = document.createElement("div");
      badge.className = "font-badge";
      badge.textContent = `Font: ${line.match.why}`;
      badge.style.left = px(x0);
      badge.style.top = `${(y1 + 2) * state.scale + 4}px`;
    }
    box.replaceWith(input);
    if (badge) input.after(badge);
    input.focus();
    input.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      badge?.remove();
      if (save && input.value !== line.text) {
        op({ op: "edit_text", page: n, pageBbox: line.pageBbox, text: input.value, font: $("#fontSelect").value });
      } else {
        input.replaceWith(box);
      }
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") finish(true);
      if (ev.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  });
  overlay.appendChild(box);
}

function addAnnotBox(overlay, n, a) {
  const box = document.createElement("div");
  box.className = "annot-box";
  box.title = `Remove ${a.type}`;
  place(box, a.bbox);
  box.addEventListener("pointerdown", (e) => e.stopPropagation());
  box.addEventListener("click", () => op({ op: "delete_annot", page: n, xref: a.xref }));
  overlay.appendChild(box);
}

function addFormField(overlay, w) {
  if (w.type === "Button" || w.type === "Signature") return;
  const box = document.createElement("div");
  box.className = "form-field";
  place(box, w.bbox);
  box.addEventListener("pointerdown", (e) => e.stopPropagation());
  const h = (w.bbox[3] - w.bbox[1]) * state.scale;
  let input;
  if (w.type === "CheckBox" || w.type === "RadioButton") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!w.value && w.value !== "Off";
  } else if (w.choices) {
    input = document.createElement("select");
    for (const c of w.choices) input.add(new Option(c, c, false, c === w.value));
  } else if (h > 40) {
    input = document.createElement("textarea");
    input.value = w.value || "";
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = w.value || "";
  }
  input.dataset.xref = w.xref;
  input.dataset.original = input.type === "checkbox" ? String(input.checked) : input.value;
  input.title = w.name;
  input.dir = "auto";
  input.disabled = w.readOnly;
  input.style.fontSize = `${Math.min(Math.max(h * 0.65, 9), 16)}px`;
  box.appendChild(input);
  overlay.appendChild(box);
}

async function saveForm() {
  const values = {};
  $$(".form-field [data-xref]").forEach((el) => {
    const v = el.type === "checkbox" ? el.checked : el.value;
    if (String(v) !== el.dataset.original) values[el.dataset.xref] = v;
  });
  if (!Object.keys(values).length) { toast("Nothing changed."); return; }
  if (await op({ op: "fill_form", values })) toast("Form saved.");
}

// ---- Images tool

function addImageBox(overlay, n, img) {
  const box = document.createElement("div");
  box.className = "img-box";
  box.title = `Image ${img.width}×${img.height} px`;
  box._img = img;
  box._page = n;
  place(box, img.bbox);
  box.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    if (state.imgSel?.box !== box) { selectImage(box); return; }
    startImageDrag(e, box);
  });
  overlay.appendChild(box);
}

function selectImage(box) {
  deselectImage();
  box.classList.add("selected");
  const handle = document.createElement("div");
  handle.className = "handle";
  handle.title = "Drag to resize (hold Shift to change the proportions)";
  box.appendChild(handle);
  const bar = $("#imageBar").cloneNode(true);
  bar.removeAttribute("id");
  bar.hidden = false;
  bar.style.left = "0";
  bar.style.top = "-38px";
  bar.addEventListener("pointerdown", (e) => e.stopPropagation());
  $$("[data-img]", bar).forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    imageAction(b.dataset.img);
  }));
  box.appendChild(bar);
  state.imgSel = { box, page: box._page, img: box._img, rect: [...box._img.bbox], bar };
}

function deselectImage() {
  const sel = state.imgSel;
  if (!sel) return;
  sel.box.classList.remove("selected", "cropping");
  $$(".handle, .image-bar, .crop-area", sel.box).forEach((el) => el.remove());
  state.imgSel = null;
}

function startImageDrag(e, box) {
  const sel = state.imgSel;
  const overlay = box.parentElement;
  const start = overlayPoint(overlay, e);
  const orig = [...sel.rect];
  const mode = sel.cropping ? "crop" : e.target.classList.contains("handle") ? "resize" : "move";
  e.preventDefault();
  box.setPointerCapture(e.pointerId);
  let cropEl = null, cropRect = null, moved = false;
  if (mode === "crop") {
    cropEl = document.createElement("div");
    cropEl.className = "crop-area";
    box.appendChild(cropEl);
  }
  const move = (ev) => {
    const [x, y] = overlayPoint(overlay, ev);
    const dx = x - start[0], dy = y - start[1];
    if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;
    if (mode === "move") {
      sel.rect = [orig[0] + dx, orig[1] + dy, orig[2] + dx, orig[3] + dy];
    } else if (mode === "resize") {
      let w = Math.max(orig[2] - orig[0] + dx, 8), h = Math.max(orig[3] - orig[1] + dy, 8);
      if (!ev.shiftKey) {  // keep proportions
        const ratio = (orig[2] - orig[0]) / (orig[3] - orig[1]);
        if (w / h > ratio) h = w / ratio; else w = h * ratio;
      }
      sel.rect = [orig[0], orig[1], orig[0] + w, orig[1] + h];
    } else {
      cropRect = normRect(start, [x, y]).map((v, i) => Math.min(Math.max(v, orig[i % 2]), orig[(i % 2) + 2]));
      cropEl.style.left = px(cropRect[0] - orig[0]);
      cropEl.style.top = px(cropRect[1] - orig[1]);
      cropEl.style.width = px(cropRect[2] - cropRect[0]);
      cropEl.style.height = px(cropRect[3] - cropRect[1]);
      return;
    }
    place(box, sel.rect);
  };
  const up = () => {
    box.removeEventListener("pointermove", move);
    box.removeEventListener("pointerup", up);
    box.removeEventListener("pointercancel", up);
    if (mode === "crop") {
      sel.cropping = false;
      box.classList.remove("cropping");
      cropEl.remove();
      if (!cropRect || cropRect[2] - cropRect[0] < 3 || cropRect[3] - cropRect[1] < 3) return;
      const w = orig[2] - orig[0], h = orig[3] - orig[1];
      const crop = [(cropRect[0] - orig[0]) / w, (cropRect[1] - orig[1]) / h,
                    (cropRect[2] - orig[0]) / w, (cropRect[3] - orig[1]) / h];
      commitImage({ rect: cropRect, crop });
    } else if (moved) {
      commitImage({ rect: sel.rect });
    }
  };
  box.addEventListener("pointermove", move);
  box.addEventListener("pointerup", up);
  box.addEventListener("pointercancel", up);
}

function commitImage(extra) {
  const sel = state.imgSel;
  if (!sel) return;
  const data = { op: "image_transform", page: sel.page, xref: sel.img.xref, bbox: sel.img.bbox, rect: sel.rect, ...extra };
  state.reselect = { page: sel.page, rect: data.rect };
  state.imgSel = null;
  op(data).then((ok) => { if (!ok) state.reselect = null; });
}

function imageAction(action) {
  const sel = state.imgSel;
  if (!sel) return;
  if (action === "delete") {
    state.imgSel = null;
    op({ op: "image_delete", page: sel.page, xref: sel.img.xref, bbox: sel.img.bbox });
  } else if (action === "rotate") {
    // Turn 90° clockwise about the centre: width and height swap.
    const [x0, y0, x1, y1] = sel.rect;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, w = x1 - x0, h = y1 - y0;
    commitImage({ rect: [cx - h / 2, cy - w / 2, cx + h / 2, cy + w / 2], rotate: 90 });
  } else if (action === "crop") {
    sel.cropping = true;
    sel.box.classList.add("cropping");
    toast("Drag over the part of the image to keep. Esc cancels.");
  }
}

// ---- mouse input on a page for drawing tools

function overlayPoint(overlay, e) {
  const r = overlay.getBoundingClientRect();
  return [(e.clientX - r.left) / state.scale, (e.clientY - r.top) / state.scale];
}

const CLICK_TOOLS = ["addtext", "note", "date"];
const DRAG_TOOLS = ["highlight", "underline", "strikeout", "rect", "ellipse", "line", "arrow",
  "whiteout", "redact", "croppage", "field", "ink", "place"];

function attachOverlay(overlay, n) {
  // Click tools open on "click" (after mouse-up) so the browser's own focus handling
  // can't immediately blur a new, still-empty text box.
  overlay.addEventListener("click", (e) => {
    if (e.target !== overlay) return;
    const p = overlayPoint(overlay, e);
    if (state.tool === "addtext") startAddText(overlay, n, p);
    else if (state.tool === "note") addNote(n, p);
    else if (state.tool === "date") stampDate(n, p);
  });

  overlay.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const tool = state.tool;
    if (tool === "image" && e.target === overlay) { deselectImage(); return; }
    if (!DRAG_TOOLS.includes(tool)) return;
    e.preventDefault();
    overlay.setPointerCapture(e.pointerId);
    const start = overlayPoint(overlay, e);
    const path = [start];
    const color = $("#colorInput").value;
    const width = Number($("#widthInput").value) || 2;

    let preview;
    if (tool === "ink" || tool === "line" || tool === "arrow") {
      preview = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      preview.setAttribute("class", "preview-svg");
      preview.innerHTML = `<polyline fill="none" stroke="${color}" stroke-width="${width * state.scale}"
        stroke-linecap="round" stroke-linejoin="round"/>`;
    } else {
      preview = document.createElement("div");
      preview.className = "preview-box";
    }
    overlay.appendChild(preview);

    const draw = (cur) => {
      if (preview.tagName === "svg") {
        const pts = tool === "ink" ? path : [start, cur];
        $("polyline", preview).setAttribute("points", pts.map(([x, y]) => `${x * state.scale},${y * state.scale}`).join(" "));
      } else {
        place(preview, normRect(start, cur));
      }
    };

    const move = (ev) => {
      const cur = overlayPoint(overlay, ev);
      if (tool === "ink") path.push(cur);
      draw(cur);
    };
    const up = (ev) => {
      overlay.removeEventListener("pointermove", move);
      overlay.removeEventListener("pointerup", up);
      overlay.removeEventListener("pointercancel", up);
      const end = overlayPoint(overlay, ev);
      preview.remove();
      finishDrag(tool, n, start, end, path, color, width);
    };
    overlay.addEventListener("pointermove", move);
    overlay.addEventListener("pointerup", up);
    overlay.addEventListener("pointercancel", up);
  });
}

function normRect([ax, ay], [bx, by]) {
  return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)];
}

function finishDrag(tool, page, start, end, path, color, width) {
  const rect = normRect(start, end);
  const tiny = rect[2] - rect[0] < 3 && rect[3] - rect[1] < 3;
  switch (tool) {
    case "ink":
      if (path.length > 1) op({ op: "ink", page, paths: [path], color, width });
      break;
    case "line":
    case "arrow":
      if (!tiny) op({ op: "line", page, p1: start, p2: end, color, width, arrow: tool === "arrow" });
      break;
    case "rect":
    case "ellipse":
      if (!tiny) op({ op: tool, page, rect, color, width });
      break;
    case "highlight":
      if (!tiny) op({ op: "highlight", page, rect, color: "#ffeb3b" });
      break;
    case "underline":
    case "strikeout":
      if (!tiny) op({ op: tool, page, rect, color });
      break;
    case "whiteout":
      if (!tiny) op({ op: "erase_area", page, rect });
      break;
    case "redact":
      if (!tiny) op({ op: "erase_area", page, rect, fill: "#000000" });
      break;
    case "croppage":
      if (!tiny) askCrop(page, rect);
      break;
    case "field":
      askField(page, tiny ? [start[0], start[1], start[0] + 150, start[1] + 22] : rect);
      break;
    case "place": {
      let r = rect;
      if (tiny || r[2] - r[0] < 20) r = [start[0], start[1], start[0] + 180, start[1] + 70];
      op({ op: "image", page, rect: r, dataUrl: state.pendingImage }).then((ok) => {
        if (ok) { state.pendingImage = null; setTool("select"); }
      });
      break;
    }
  }
}

function textStyle() {
  return {
    size: Number($("#sizeInput").value) || 14,
    color: $("#colorInput").value,
    font: $("#fontSelect").value,
    bold: $("#boldBtn").classList.contains("on"),
    italic: $("#italicBtn").classList.contains("on"),
    align: $("#alignSelect").value,
  };
}

function startAddText(overlay, n, [x, y]) {
  const style = textStyle();
  const ta = document.createElement("textarea");
  ta.className = "inline-edit";
  ta.rows = 1;
  ta.style.left = px(x);
  ta.style.top = px(y);
  ta.style.fontSize = px(style.size);
  ta.style.color = style.color;
  ta.style.fontFamily = previewFont("Helvetica");
  ta.style.fontWeight = style.bold ? "bold" : "normal";
  ta.style.fontStyle = style.italic ? "italic" : "normal";
  ta.style.textAlign = style.align;
  ta.dir = "auto";
  ta.placeholder = "Type…";
  overlay.appendChild(ta);
  const grow = () => {
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight + 2}px`;
    ta.style.width = "auto";
    ta.style.width = `${Math.max(ta.scrollWidth + 8, 80)}px`;
  };
  ta.addEventListener("input", grow);
  ta.focus();
  grow();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const text = ta.value.replace(/\s+$/, "");
    ta.remove();
    if (save && text) op({ op: "add_text", page: n, x, y, text, ...style });
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
    if (e.key === "Escape") finish(false);
  });
  ta.addEventListener("blur", () => finish(true));
  ta.addEventListener("pointerdown", (e) => e.stopPropagation());
  ta.addEventListener("click", (e) => e.stopPropagation());
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
  "September", "October", "November", "December"];

function formatDate(fmt, d = new Date()) {
  const pad = (v) => String(v).padStart(2, "0");
  return fmt
    .replace("YYYY", d.getFullYear())
    .replace("MMMM", MONTHS[d.getMonth()])
    .replace("MM", pad(d.getMonth() + 1))
    .replace("DD", pad(d.getDate()))
    .replace(/\bD\b/, d.getDate());
}

function stampDate(n, [x, y]) {
  op({ op: "add_text", page: n, x, y, text: formatDate($("#dateFormat").value), ...textStyle(), align: "left" });
}

async function addNote(n, point) {
  const r = await ask({
    title: "Sticky note",
    body: `<label class="field">Comment<textarea name="text" rows="4" dir="auto" required></textarea></label>`,
    ok: "Add note",
  });
  if (r && r.text.trim()) op({ op: "note", page: n, point, text: r.text, color: "#ffd400" });
}

async function askCrop(page, rect) {
  const k = state.selected.size;
  const r = await ask({
    title: "Crop pages",
    body: `<p>Keep only the marked area. The same margins are used on each page you choose.</p>
      <div class="checks">
        <label><input type="radio" name="which" value="this" checked> This page</label>
        <label><input type="radio" name="which" value="selected" ${k ? "" : "disabled"}> Selected pages (${k})</label>
        <label><input type="radio" name="which" value="all"> All pages</label>
      </div>`,
    ok: "Crop",
  });
  if (!r) return;
  const pages = r.which === "all" ? state.doc.pages.map((_, i) => i)
    : r.which === "selected" ? selectedPages() : [page];
  if (await op({ op: "crop_pages", page, rect, pages })) setTool("select");
}

let lastFieldType = "text";
async function askField(page, rect) {
  const r = await ask({
    title: "New form field",
    body: `<label class="field">Type
        <select name="type">
          ${[["text", "Text box"], ["multiline", "Text area (several lines)"], ["checkbox", "Check box"],
             ["dropdown", "Drop-down list"], ["list", "List box"]]
            .map(([v, l]) => `<option value="${v}" ${v === lastFieldType ? "selected" : ""}>${l}</option>`).join("")}
        </select></label>
      <label class="field">Name (optional; e.g. “Full name”, “Date”)<input type="text" name="name"></label>
      <label class="field" data-show="choices">Choices, separated by commas<input type="text" name="choices" placeholder="Yes, No, Maybe"></label>`,
    ok: "Add field",
    onOpen: (dlg) => {
      const sel = $("select[name=type]", dlg);
      const upd = () => ($("[data-show=choices]", dlg).hidden = !["dropdown", "list"].includes(sel.value));
      sel.addEventListener("change", upd);
      upd();
    },
  });
  if (!r) return;
  lastFieldType = r.type;
  op({ op: "add_field", page, rect, type: r.type, name: r.name, choices: r.choices });
}

// ------------------------------------------------------------ generic dialog

// Show a form in a modal dialog. Resolves to {name: value} (checkbox -> bool,
// radio -> checked value, file -> FileList) or null when cancelled.
function ask({ title, body, ok = "OK", cancel = "Cancel", onOpen }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.innerHTML = `<form method="dialog" class="dialog-body">
        <h2>${esc(title)}</h2>${body}
        <div class="row end">
          ${cancel ? `<button type="button" class="btn" data-cancel>${esc(cancel)}</button>` : ""}
          <button type="submit" class="btn primary">${esc(ok)}</button>
        </div></form>`;
    document.body.appendChild(dlg);
    const form = $("form", dlg);
    let result = null;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      result = {};
      for (const el of form.elements) {
        if (!el.name) continue;
        if (el.type === "checkbox") result[el.name] = el.checked;
        else if (el.type === "radio") { if (el.checked) result[el.name] = el.value; }
        else if (el.type === "file") result[el.name] = el.files;
        else result[el.name] = el.value;
      }
      dlg.close();
    });
    $("[data-cancel]", dlg)?.addEventListener("click", () => dlg.close());
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    if (onOpen) onOpen(dlg);
    dlg.showModal();
    $("input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea", dlg)?.focus();
  });
}

const pageChoice = () => `<div class="checks">
    <label><input type="radio" name="pages" value="all" checked> All pages</label>
    <label><input type="radio" name="pages" value="selected" ${state.selected.size ? "" : "disabled"}>
      Selected pages (${state.selected.size})</label>
  </div>`;
const chosenPages = (r) => (r.pages === "selected" ? selectedPages() : null);

// ------------------------------------------------------------ menu actions

// The pages a page command applies to: the selected thumbnails, else the page in view.
const targetPages = () => (state.selected.size ? selectedPages() : [state.currentPage]);

const actions = {
  download() { $("#downloadBtn").click(); },
  closeTab() { closeTab(state.doc.id); },
  thumbs() {
    if (isPhone()) setPanel(document.body.classList.contains("thumbs-open") ? null : "thumbs");
    else document.body.classList.toggle("thumbs-hidden");
  },
  rotateRight() { op({ op: "rotate", pages: targetPages(), angle: 90 }); },
  rotateLeft() { op({ op: "rotate", pages: targetPages(), angle: -90 }); },
  async deletePages() {
    const pages = targetPages();
    if (pages.length >= state.doc.pages.length) { toast("A PDF must keep at least one page.", true); return; }
    if (!confirm(`Delete page${pages.length > 1 ? "s" : ""} ${pages.map((n) => n + 1).join(", ")}?`)) return;
    if (await op({ op: "delete", pages })) { state.selected.clear(); renderThumbs(); updateButtons(); }
  },
  zoomIn() { setZoom(1.2); },
  zoomOut() { setZoom(1 / 1.2); },
  fitWidth() { fitWidth(); },
  fitPage() { fitPage(); },
  theme() { toggleTheme(); },
  blank() {
    const sel = selectedPages();
    op({ op: "insert_blank", after: sel.length ? sel[sel.length - 1] : state.doc.pages.length - 1 });
  },
  cropTool() { setTool("croppage"); },
  uncrop() {
    const pages = state.selected.size ? selectedPages() : state.doc.pages.map((_, i) => i);
    op({ op: "uncrop_pages", pages }).then((ok) => ok && toast("Crop removed."));
  },
  extract() {
    if (!state.selected.size) { toast("Select pages in the sidebar first (Ctrl/Shift-click).", true); return; }
    downloadPost(docUrl("/extract"), { pages: selectedPages() });
  },
  async split() {
    const r = await ask({
      title: "Split into files",
      body: `<div class="checks">
          <label><input type="radio" name="mode" value="every" checked> Every
            <input type="number" name="every" value="1" min="1" style="width:60px"> page(s)</label>
        </div>
        <div class="checks">
          <label><input type="radio" name="mode" value="ranges"> Page ranges
            <input type="text" name="ranges" placeholder="1-3, 4-6, 7-"></label>
        </div>
        <p class="muted">You get a .zip with one PDF per part.</p>`,
      ok: "Split",
    });
    if (!r) return;
    if (r.mode === "ranges") downloadPost(docUrl("/split"), { ranges: r.ranges });
    else downloadPost(docUrl("/split"), { every: parseInt(r.every, 10) || 1 });
  },
  toDocx() { convertTo("docx"); },
  toXlsx() { convertTo("xlsx"); },
  toPptx() { convertTo("pptx"); },
  async toImages() {
    const r = await ask({
      title: "PDF to images",
      body: `<div class="row">
          <label class="field">Format <select name="format"><option value="png">PNG</option>
            <option value="jpg" selected>JPG</option><option value="webp">WebP</option></select></label>
          <label class="field">Resolution (DPI) <input type="number" name="dpi" value="150" min="36" max="600"></label>
        </div>${pageChoice()}
        <p class="muted">Several pages are downloaded as a .zip.</p>`,
      ok: "Convert",
    });
    if (!r) return;
    convertTo("images", { format: r.format, dpi: Number(r.dpi) || 150, pages: chosenPages(r) });
  },
  async compress() {
    const r = await ask({
      title: "Compress",
      body: `<p>Current size: <b>${formatSize(state.doc.size)}</b>. Pictures are resampled and re-saved; text stays sharp.</p>
        <label class="field">Level <select name="level">
          <option value="low">Light (best quality, ~200 dpi)</option>
          <option value="medium" selected>Medium (good for email, ~150 dpi)</option>
          <option value="high">Strong (smallest file, ~96 dpi)</option></select></label>`,
      ok: "Compress",
    });
    if (r) op({ op: "compress", level: r.level });
  },
  async ocr() {
    const where = state.caps.tesseract
      ? "Tesseract on this computer is used."
      : "Runs in this browser with Tesseract.js (the first run downloads its language data from the internet; your document stays here).";
    const r = await ask({
      title: "OCR: make scanned pages searchable",
      body: `<p class="muted">${esc(where)}</p>
        <label class="field">Language <select name="lang">
          <option value="eng">English</option><option value="ara">Arabic</option>
          <option value="eng+ara">English + Arabic</option><option value="fra">French</option>
          <option value="deu">German</option><option value="spa">Spanish</option>
          <option value="urd">Urdu</option></select></label>
        ${pageChoice()}
        <label class="check"><input type="checkbox" name="force"> Also pages that already have text</label>`,
      ok: "Run OCR",
    });
    if (!r) return;
    let pages = chosenPages(r) || state.doc.pages.map((_, i) => i);
    if (state.caps.tesseract) {
      op({ op: "ocr", lang: r.lang, pages, force: r.force });
    } else {
      browserOcr(pages, r.lang, r.force);
    }
  },
  async export() {
    const r = await ask({
      title: "Password & permissions",
      body: `<label class="field">Password to open (leave empty for none)<input type="password" name="userPassword" autocomplete="new-password"></label>
        <label class="field">Owner password (needed to change permissions; optional)<input type="password" name="ownerPassword" autocomplete="new-password"></label>
        <fieldset><legend>Allow people to…</legend><div class="checks">
          <label><input type="checkbox" name="print" checked> Print</label>
          <label><input type="checkbox" name="copy" checked> Copy text</label>
          <label><input type="checkbox" name="edit" checked> Edit</label>
          <label><input type="checkbox" name="annotate" checked> Comment</label>
          <label><input type="checkbox" name="forms" checked> Fill forms</label>
        </div></fieldset>
        <label class="check"><input type="checkbox" name="cleanMetadata"> Also remove hidden metadata (author, software, dates)</label>
        <p class="muted">Encrypted with AES-256. Downloads a protected copy; the open document is not changed.</p>`,
      ok: "Download protected PDF",
    });
    if (!r) return;
    const permissions = { print: r.print, copy: r.copy, edit: r.edit, annotate: r.annotate, forms: r.forms };
    if (!r.userPassword && !r.ownerPassword && Object.values(permissions).every(Boolean) && !r.cleanMetadata) {
      toast("Set a password, untick a permission, or choose metadata removal.", true);
      return;
    }
    downloadPost(docUrl("/export"), { userPassword: r.userPassword, ownerPassword: r.ownerPassword, permissions, cleanMetadata: r.cleanMetadata });
  },
  cleanMeta() { op({ op: "clean_metadata" }); },
  async watermark() {
    const r = await ask({
      title: "Watermark",
      body: `<div class="checks">
          <label><input type="radio" name="kind" value="text" checked> Text</label>
          <label><input type="radio" name="kind" value="image"> Image / logo</label>
        </div>
        <div data-kind="text">
          <label class="field">Text<input type="text" name="text" value="CONFIDENTIAL" dir="auto"></label>
          <div class="row">
            <label class="field">Size<input type="number" name="size" value="60" min="8" max="300"></label>
            <label class="field">Colour<input type="color" name="color" value="#cc1a1a"></label>
          </div>
        </div>
        <div data-kind="image" hidden>
          <label class="field">Image file<input type="file" name="file" accept="image/png,image/jpeg,image/webp"></label>
          <label class="field">Width (% of page)<input type="number" name="scale" value="50" min="5" max="100"></label>
        </div>
        <div class="row">
          <label class="field">Angle <select name="angle"><option value="45" selected>45° diagonal</option>
            <option value="0">Straight</option><option value="90">Vertical</option></select></label>
          <label class="field">Opacity (%)<input type="number" name="opacity" value="25" min="2" max="100"></label>
        </div>${pageChoice()}`,
      ok: "Add watermark",
      onOpen: (dlg) => {
        const upd = () => {
          const kind = $("input[name=kind]:checked", dlg).value;
          $$("[data-kind]", dlg).forEach((d) => (d.hidden = d.dataset.kind !== kind));
        };
        $$("input[name=kind]", dlg).forEach((i) => i.addEventListener("change", upd));
      },
    });
    if (!r) return;
    const data = { op: "watermark", angle: Number(r.angle), opacity: Number(r.opacity) / 100, pages: chosenPages(r) };
    if (r.kind === "image") {
      if (!r.file.length) { toast("Choose an image file.", true); return; }
      data.dataUrl = await readAsDataUrl(r.file[0]);
      data.scale = Number(r.scale) / 100;
    } else {
      Object.assign(data, { text: r.text, size: Number(r.size), color: r.color });
    }
    op(data);
  },
  async smartRedact() {
    const r = await ask({
      title: "Smart redact",
      body: `<p>Find sensitive information, review the matches, then remove them for good.</p>
        <div class="checks">
          <label><input type="checkbox" name="ssn" checked> Social security numbers</label>
          <label><input type="checkbox" name="card" checked> Credit card numbers</label>
          <label><input type="checkbox" name="email" checked> Email addresses</label>
          <label><input type="checkbox" name="phone"> Phone numbers</label>
          <label><input type="checkbox" name="iban"> IBANs</label>
        </div>
        <label class="field">Also find this text<input type="text" name="custom" placeholder="e.g. a name or account number" dir="auto"></label>
        <label class="check"><input type="checkbox" name="regex"> Treat it as a regular expression</label>`,
      ok: "Find",
    });
    if (!r) return;
    const kinds = ["ssn", "card", "email", "phone", "iban"].filter((k) => r[k]);
    let matches;
    try { matches = await postJson(docUrl("/find"), { kinds, custom: r.custom, regex: r.regex }); }
    catch (e) { toast(e.message, true); return; }
    if (!matches.length) { toast("Nothing matching was found. (Scanned pages need OCR first.)"); return; }
    const labels = { ssn: "SSN", card: "Card", email: "Email", phone: "Phone", iban: "IBAN", custom: "Text" };
    const pick = await ask({
      title: `Found ${matches.length} item(s)`,
      body: `<div class="result-list">${matches.map((m, i) => `<label>
          <input type="checkbox" name="m${i}" checked>
          <span class="kind">p.${m.page + 1} · ${labels[m.kind] || m.kind}</span><span dir="auto">${esc(m.text)}</span>
        </label>`).join("")}</div>
        <p class="muted">The ticked items are blacked out and the text beneath is permanently deleted.</p>`,
      ok: "Redact ticked items",
    });
    if (!pick) return;
    const items = matches.filter((_, i) => pick[`m${i}`]);
    if (items.length) op({ op: "redact_areas", items });
  },
  async flatten() {
    if (await op({ op: "flatten" })) toast("Annotations and form fields are now part of the pages.");
  },
  aiSummary() { openAi(); summarize(); },
  aiChat() { openAi(); $("#aiQuestion").focus(); },
  aiAutofill() { aiAutofill(); },
  aiSettings() { aiSettings(); },
};

function convertTo(fmt, data) {
  const labels = { docx: "Word", xlsx: "Excel", pptx: "PowerPoint", images: "images" };
  toast(`Converting to ${labels[fmt]}…`);
  downloadPost(docUrl(`/convert/${fmt}`), data || {});
}

// ------------------------------------------------------------ OCR in the browser

let tesseractLoading = null;
function loadTesseract() {
  if (window.Tesseract) return Promise.resolve();
  tesseractLoading ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
    s.onload = resolve;
    s.onerror = () => { tesseractLoading = null; reject(new Error("Could not load the OCR engine. Check the internet connection, or install Tesseract on this computer.")); };
    document.head.appendChild(s);
  });
  return tesseractLoading;
}

async function browserOcr(pages, lang, force) {
  const { id } = state.doc;
  // Pages that already have text are skipped unless asked otherwise.
  if (!force) {
    const texts = await Promise.all(pages.map((n) => fetch(`/api/doc/${id}/page/${n}/lines`).then((r) => r.json())));
    pages = pages.filter((_, i) => !texts[i].length);
    if (!pages.length) { toast("Those pages already have text. Tick “Also pages that already have text” to OCR them anyway."); return; }
  }
  showBusy(true);
  let worker;
  try {
    await loadTesseract();
    toast("Starting OCR…");
    worker = await Tesseract.createWorker(lang, 1);
    const zoom = 2.5;  // ~180 dpi
    const out = [];
    for (const [i, n] of pages.entries()) {
      toast(`OCR: page ${n + 1} (${i + 1} of ${pages.length})…`);
      const { data } = await worker.recognize(pageImgUrl(n, zoom));
      const words = (data.words || [])
        .filter((w) => w.confidence > 30 && w.text.trim())
        .map((w) => ({ text: w.text, bbox: [w.bbox.x0 / zoom, w.bbox.y0 / zoom, w.bbox.x1 / zoom, w.bbox.y1 / zoom] }));
      if (words.length) out.push({ page: n, words });
    }
    if (!state.doc || state.doc.id !== id) return;
    if (!out.length) { toast("No text was recognised.", true); return; }
    await op({ op: "text_layer", pages: out });
  } catch (e) {
    toast(e.message || String(e), true);
  } finally {
    if (worker) worker.terminate();
    showBusy(false);
  }
}

// ------------------------------------------------------------ AI

function openAi() {
  $("#aiPanel").hidden = false;
  renderAiLog();
}

function chatFor() {
  if (!state.doc) return [];
  return (state.chats[state.doc.id] ??= []);
}

function renderAiLog() {
  const log = $("#aiLog");
  if ($("#aiPanel").hidden) return;
  const msgs = chatFor();
  log.innerHTML = msgs.length ? "" :
    `<p class="muted">${state.doc ? "Ask anything about the open PDF, e.g. “What is the expiry date of this license?”" : "Open a PDF first."}</p>`;
  for (const m of msgs) {
    const div = document.createElement("div");
    div.className = `msg ${m.role}${m.error ? " error" : ""}${m.pending ? " pending" : ""}`;
    div.dir = "auto";
    div.textContent = m.content;
    log.appendChild(div);
  }
  log.scrollTop = log.scrollHeight;
}

// On a hosted copy each visitor's key stays in their own browser and is sent
// with each AI request; locally the key is saved on this computer by the server.
const BROWSER_KEY = "pdfeditor.anthropicKey";
function browserKey() {
  try { return localStorage.getItem(BROWSER_KEY) || ""; } catch { return ""; }
}

async function ensureAiKey() {
  if (state.caps.ai.configured || (state.caps.hosted && browserKey())) return true;
  return aiSettings("Add your Anthropic API key to use the AI features.");
}

function aiPost(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (state.caps.hosted && browserKey()) headers["X-Anthropic-Key"] = browserKey();
  return api(docUrl(path), { method: "POST", headers, body: JSON.stringify(body) });
}

async function aiRequest(path, body, display) {
  if (!state.doc) return null;
  if (!(await ensureAiKey())) return null;
  const msgs = chatFor();
  if (display) msgs.push({ role: "user", content: display });
  const pending = { role: "assistant", content: "Thinking…", pending: true };
  msgs.push(pending);
  renderAiLog();
  try {
    const res = await aiPost(path, body);
    Object.assign(pending, { content: res.text, pending: false });
    return res.text;
  } catch (e) {
    Object.assign(pending, { content: e.message, pending: false, error: true });
    return null;
  } finally {
    renderAiLog();
  }
}

function summarize() {
  const length = $("#aiLength").value;
  aiRequest("/ai/summary", { length }, `Summarize this document (${$("#aiLength").selectedOptions[0].text.toLowerCase()}).`);
}

async function askQuestion(text) {
  if (!text.trim() || !state.doc) return;
  const history = chatFor().filter((m) => !m.error && !m.pending).map(({ role, content }) => ({ role, content }));
  history.push({ role: "user", content: text });
  await aiRequest("/ai/chat", { messages: history }, text);
}

async function aiSettings(reason) {
  if (state.caps.hosted) return browserKeySettings(reason);
  const caps = state.caps.ai;
  const status = caps.configured
    ? `A key is set (from ${caps.source === "settings" ? "these settings" : "the ANTHROPIC_API_KEY environment variable"}).`
    : "No key is set yet.";
  const r = await ask({
    title: "AI settings",
    body: `${reason ? `<p>${esc(reason)}</p>` : ""}
      <p class="muted">${esc(status)} Get a key at console.anthropic.com. It is stored in config.json on this computer.
        The AI features send the open document to Claude (model claude-opus-5-5).</p>
      <label class="field">Anthropic API key<input type="password" name="key" placeholder="sk-ant-…" autocomplete="off"></label>
      ${caps.source === "settings" ? `<label class="check"><input type="checkbox" name="remove"> Remove the saved key</label>` : ""}`,
    ok: "Save",
  });
  if (!r) return false;
  if (!r.key && !r.remove) return state.caps.ai.configured;
  try {
    const res = await postJson("/api/settings", { anthropicKey: r.remove ? "" : r.key.trim() });
    state.caps.ai = res.ai;
    toast(res.ai.configured ? "API key saved." : "API key removed.");
  } catch (e) { toast(e.message, true); }
  return state.caps.ai.configured;
}

async function browserKeySettings(reason) {
  const has = !!browserKey();
  const shared = state.caps.ai.configured;
  const r = await ask({
    title: "AI settings",
    body: `${reason ? `<p>${esc(reason)}</p>` : ""}
      <p class="muted">${has ? "Your key is saved in this browser." : shared ? "This site provides a key; you can use your own instead." : "No key is set yet."}
        Get a key at console.anthropic.com. It is kept only in this browser and sent with your AI requests;
        the AI features send the open document to Claude (model claude-opus-5-5).</p>
      <label class="field">Anthropic API key<input type="password" name="key" placeholder="sk-ant-…" autocomplete="off"></label>
      ${has ? `<label class="check"><input type="checkbox" name="remove"> Remove my key from this browser</label>` : ""}`,
    ok: "Save",
  });
  if (!r) return false;
  try {
    if (r.remove) { localStorage.removeItem(BROWSER_KEY); toast("API key removed."); }
    else if (r.key.trim()) { localStorage.setItem(BROWSER_KEY, r.key.trim()); toast("API key saved in this browser."); }
  } catch { toast("This browser can't store the key (private mode?).", true); }
  return !!browserKey() || shared;
}

async function aiAutofill() {
  if (!state.doc) return;
  if (!state.doc.hasForms) { toast("This PDF has no fillable form fields. Use “+ Field” to add some first.", true); return; }
  if (!(await ensureAiKey())) return;
  let saved = "";
  try { saved = localStorage.getItem("pdfeditor.autofillInfo") || ""; } catch {}
  const r = await ask({
    title: "AI auto-fill",
    body: `<p>Tell the AI the details to use. It suggests values; you review them before saving.</p>
      <label class="field">Your details<textarea name="info" rows="7" dir="auto"
        placeholder="Name: Jane Doe&#10;Address: 12 Main St, New York, NY 10001&#10;Phone: (212) 555-0100&#10;Email: jane@example.com">${esc(saved)}</textarea></label>
      <label class="check"><input type="checkbox" name="remember" ${saved ? "checked" : ""}> Remember these details in this browser</label>`,
    ok: "Suggest values",
  });
  if (!r) return;
  try {
    if (r.remember) localStorage.setItem("pdfeditor.autofillInfo", r.info);
    else localStorage.removeItem("pdfeditor.autofillInfo");
  } catch {}
  let res;
  try { res = await aiPost("/ai/autofill", { info: r.info }); }
  catch (e) { toast(e.message, true); return; }
  const values = res.values || {};
  if (state.tool !== "form") setTool("form");
  await waitForFields();
  let filled = 0;
  for (const [xref, value] of Object.entries(values)) {
    const el = $(`.form-field [data-xref="${xref}"]`);
    if (!el || el.disabled) continue;
    if (el.type === "checkbox") el.checked = /^(true|yes|1|on)$/i.test(String(value));
    else el.value = value;
    el.classList.add("ai-filled");
    filled++;
  }
  toast(filled ? `AI filled ${filled} field(s) (marked in orange). Check them, then press “Save form values”.`
    : "The AI could not fill any fields from those details.", !filled);
}

async function waitForFields() {
  for (let i = 0; i < 50 && !$(".form-field"); i++) await new Promise((r) => setTimeout(r, 100));
}

// ------------------------------------------------------------ fonts

async function loadFonts(selectId) {
  let list;
  try { list = await (await fetch("/api/fonts")).json(); } catch { return; }
  const sel = $("#fontSelect");
  const keep = selectId || sel.value;
  sel.innerHTML = '<option value="auto">Automatic</option>';
  for (const [group, label] of [["user", "Your fonts"], ["system", "Installed fonts"]]) {
    const items = list.filter((f) => f.group === group);
    if (!items.length) continue;
    const og = document.createElement("optgroup");
    og.label = label;
    for (const f of items) {
      const o = new Option(f.arabic ? `${f.name}  · عربي` : f.name, f.id);
      o.dataset.family = f.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "auto";
}

// Load a font from the server into the page (for previewing text in it).
// Resolves to the CSS family name, or null if the browser can't use it.
const fontFaces = new Map();
function useFontFace(id) {
  if (!fontFaces.has(id)) {
    const family = `pdfed-${fontFaces.size}`;
    const face = new FontFace(family, `url(/api/fonts/file/${encodeURIComponent(id)})`);
    fontFaces.set(id, face.load().then((f) => { document.fonts.add(f); return family; }).catch(() => null));
  }
  return fontFaces.get(id);
}

// CSS font-family for the on-screen typing box (browsers can use installed fonts by name).
function previewFont(original) {
  const opt = $("#fontSelect").selectedOptions[0];
  const name = opt && opt.value !== "auto" ? opt.dataset.family : (original || "").replace(/^[A-Z]{6}\+/, "");
  const family = name.replace(/\s+(Regular|Bold|Italic|Bold Italic|Light|Semibold)$/i, "");
  return `"${family}", "${name}", "Segoe UI", Arial, sans-serif`;
}

async function uploadFont(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await api("/api/fonts", { method: "POST", body: fd });
    await loadFonts(res.id);
    toast(`Added font: ${res.added}. It is now selected.`);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ signature dialog

const sign = { ctx: null, drawn: false };

function openSignDialog() {
  if (!state.doc) return;
  const dlg = $("#signDialog");
  clearSignCanvas();
  showSignTab("draw");
  $("#imageInput").value = "";
  $("#imagePreview").hidden = true;
  let last = null;
  try { last = localStorage.getItem("pdfeditor.signature"); } catch {}
  $("#signLast").hidden = !last;
  dlg.showModal();
}

function showSignTab(name) {
  $$("#signDialog .tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$("#signDialog .tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
  if (name === "type") { drawTyped(); $("#typedName").focus(); }
}

function clearSignCanvas() {
  const c = $("#signCanvas");
  sign.ctx = c.getContext("2d");
  sign.ctx.clearRect(0, 0, c.width, c.height);
  sign.drawn = false;
}

function setupSignCanvas() {
  const c = $("#signCanvas");
  let drawing = false;
  const pos = (e) => {
    const r = c.getBoundingClientRect();
    return [(e.clientX - r.left) * (c.width / r.width), (e.clientY - r.top) * (c.height / r.height)];
  };
  c.addEventListener("pointerdown", (e) => {
    drawing = true;
    c.setPointerCapture(e.pointerId);
    const ctx = sign.ctx;
    ctx.strokeStyle = $("#signColor").value;
    ctx.lineWidth = 3;
    ctx.lineCap = ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(...pos(e));
  });
  c.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    sign.ctx.lineTo(...pos(e));
    sign.ctx.stroke();
    sign.drawn = true;
  });
  const stop = () => (drawing = false);
  c.addEventListener("pointerup", stop);
  c.addEventListener("pointercancel", stop);
}

// Render the typed name in the chosen handwriting font.
function drawTyped() {
  const c = $("#typedCanvas");
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  const text = $("#typedName").value.trim();
  if (!text) return;
  const family = $("#typedFont").value;
  let size = 72;
  ctx.font = `${size}px "${family}", cursive`;
  while (ctx.measureText(text).width > c.width - 30 && size > 16) {
    size -= 4;
    ctx.font = `${size}px "${family}", cursive`;
  }
  ctx.fillStyle = $("#typedColor").value;
  ctx.textBaseline = "middle";
  ctx.direction = /[֐-ࣿ]/.test(text) ? "rtl" : "ltr";
  ctx.textAlign = "center";
  ctx.fillText(text, c.width / 2, c.height / 2);
}

// Crop the canvas to the drawn strokes so the placed image has no empty margins.
function trimmedCanvasDataUrl(c) {
  const { width, height } = c;
  const data = c.getContext("2d").getImageData(0, 0, width, height).data;
  let x0 = width, y0 = height, x1 = 0, y1 = 0;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > 0) {
        if (x < x0) x0 = x; if (x > x1) x1 = x;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < x0) return null;
  const pad = 4;
  x0 = Math.max(0, x0 - pad); y0 = Math.max(0, y0 - pad);
  x1 = Math.min(width, x1 + pad); y1 = Math.min(height, y1 + pad);
  const out = document.createElement("canvas");
  out.width = x1 - x0; out.height = y1 - y0;
  out.getContext("2d").drawImage(c, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
  return out.toDataURL("image/png");
}

function useSignature(dataUrl) {
  $("#signDialog").close();
  state.pendingImage = dataUrl;
  state.tool = "place";
  document.body.dataset.tool = "sign";
  markTool("sign");
  showHint(TOOL_HINTS.sign);
  refreshToolLayer();
}

function setupSignDialog() {
  setupSignCanvas();
  $("#signClear").addEventListener("click", clearSignCanvas);
  $$("#signDialog .tab").forEach((t) => t.addEventListener("click", () => showSignTab(t.dataset.tab)));
  for (const id of ["#typedName", "#typedFont", "#typedColor"]) $(id).addEventListener("input", drawTyped);
  $("#imageInput").addEventListener("change", async () => {
    const f = $("#imageInput").files[0];
    if (!f) return;
    $("#imagePreview").src = await readAsDataUrl(f);
    $("#imagePreview").hidden = false;
  });
  $("#signLast").addEventListener("click", () => {
    try { useSignature(localStorage.getItem("pdfeditor.signature")); } catch {}
  });
  $("#signUse").addEventListener("click", () => {
    const tab = $("#signDialog .tab.active").dataset.tab;
    if (tab === "upload") {
      const src = $("#imagePreview").src;
      if (!src || $("#imagePreview").hidden) { toast("Choose an image first.", true); return; }
      useSignature(src);
    } else if (tab === "type") {
      drawTyped();
      const url = trimmedCanvasDataUrl($("#typedCanvas"));
      if (!url) { toast("Type your name first.", true); return; }
      useSignature(url);
    } else {
      const url = sign.drawn && trimmedCanvasDataUrl($("#signCanvas"));
      if (!url) { toast("Draw your signature first.", true); return; }
      try { localStorage.setItem("pdfeditor.signature", url); } catch {}
      useSignature(url);
    }
  });
}

// ------------------------------------------------------------ misc UI

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 7000 : 4000);
}

let busyCount = 0, busyTimer;
function showBusy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  clearTimeout(busyTimer);
  if (busyCount > 0) busyTimer = setTimeout(() => ($("#busy").hidden = false), 300);
  else $("#busy").hidden = true;
}

function setScale(scale, anchorPage = state.currentPage) {
  if (!state.doc) return;
  state.scale = Math.min(Math.max(scale, 0.2), 5);
  renderPages();
  $(`.page[data-page="${anchorPage}"]`)?.scrollIntoView({ block: "start" });
}

const setZoom = (factor) => setScale(state.scale * factor);

const isPhone = () => matchMedia("(max-width: 900px)").matches;

function fitWidth() {
  if (!state.doc) return;
  // Phones fit the page in view (a wider landscape page then scrolls sideways);
  // larger screens fit the widest page.
  const w = isPhone()
    ? (state.doc.pages[state.currentPage] || state.doc.pages[0]).w
    : Math.max(...state.doc.pages.map((p) => p.w));
  setScale(($("#viewer").clientWidth - (isPhone() ? 18 : 60)) / w);
}

function fitPage() {
  if (!state.doc) return;
  const p = state.doc.pages[state.currentPage] || state.doc.pages[0];
  const v = $("#viewer");
  setScale(Math.min((v.clientWidth - 60) / p.w, (v.clientHeight - 50) / p.h));
}

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("pdfeditor.theme", root.dataset.theme); } catch {}
}

const selectedPages = () => [...state.selected].sort((a, b) => a - b);

async function history(kind) {
  if (!state.doc) return;
  try { applySummary(await postJson(docUrl(`/${kind}`))); }
  catch (e) { toast(e.message, true); }
}

const closeMenus = () => $$(".menu.open").forEach((m) => m.classList.remove("open"));

// Side panels on phones: "drawer" (main menus) or "thumbs" (pages), or null.
function setPanel(name) {
  document.body.classList.toggle("drawer-open", name === "drawer");
  document.body.classList.toggle("thumbs-open", name === "thumbs");
  $("#drawerBackdrop").hidden = !name;
  if (name !== "drawer") closeMenus();
}

const NO_DOC_ACTIONS = ["aiSettings", "theme", "thumbs"];

function setupMenus() {
  $$(".menu-btn").forEach((btn) => btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = btn.parentElement;
    const open = !menu.classList.contains("open");
    closeMenus();
    menu.classList.toggle("open", open);
  }));
  document.addEventListener("click", (e) => {
    // Clicks inside the Style panel or on a file picker keep the menu open.
    if (!e.target.closest(".menu-list label, .keep-open")) closeMenus();
  });
  $$(".menu-item[data-action]").forEach((item) => item.addEventListener("click", () => {
    closeMenus();
    const action = item.dataset.action;
    const fn = actions[action];
    if (!fn) return;
    if (action !== "thumbs") setPanel(null);
    if (!state.doc && !NO_DOC_ACTIONS.includes(action)) { toast("Open a PDF first.", true); return; }
    fn();
  }));
  $("#drawerBtn").addEventListener("click", () => setPanel(document.body.classList.contains("drawer-open") ? null : "drawer"));
  $("#drawerClose").addEventListener("click", () => setPanel(null));
  $("#thumbsClose").addEventListener("click", () => setPanel(null));
  $("#drawerBackdrop").addEventListener("click", () => setPanel(null));
  // A chosen file closes the drawer too.
  for (const id of ["#openInput", "#importInput", "#insertInput"]) $(id).addEventListener("change", () => setPanel(null));
}

// Two-finger pinch to zoom the pages (phones and touchpads).
function setupPinchZoom() {
  const viewer = $("#viewer");
  const wrap = $("#pages");
  let start = null;
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  viewer.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 2 || !state.doc) return;
    const r = viewer.getBoundingClientRect();
    const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left;
    const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
    start = { d: dist(e.touches), mx, my, x: viewer.scrollLeft + mx, y: viewer.scrollTop + my, ratio: 1 };
    wrap.style.transformOrigin = `${start.x}px ${start.y}px`;
  }, { passive: true });
  viewer.addEventListener("touchmove", (e) => {
    if (!start || e.touches.length !== 2) return;
    e.preventDefault();
    const scale = Math.min(Math.max(state.scale * dist(e.touches) / start.d, 0.2), 5);
    start.ratio = scale / state.scale;
    wrap.style.transform = `scale(${start.ratio})`;
  }, { passive: false });
  const end = () => {
    if (!start) return;
    const { ratio, x, y, mx, my } = start;
    start = null;
    wrap.style.transform = "";
    if (Math.abs(ratio - 1) < 0.02) return;
    state.scale = Math.min(Math.max(state.scale * ratio, 0.2), 5);
    renderPages();
    // Keep the point between the fingers where it was.
    viewer.scrollLeft = x * ratio - mx;
    viewer.scrollTop = y * ratio - my;
  };
  viewer.addEventListener("touchend", (e) => { if (e.touches.length < 2) end(); });
  viewer.addEventListener("touchcancel", end);
}

function setup() {
  for (const id of ["#openInput", "#openInput2"]) {
    $(id).addEventListener("change", (e) => { openFiles([...e.target.files]); e.target.value = ""; });
  }
  $("#importInput").addEventListener("change", (e) => { importFiles([...e.target.files]); e.target.value = ""; });
  $("#insertInput").addEventListener("change", (e) => {
    $$(".menu.open").forEach((m) => m.classList.remove("open"));
    insertFiles([...e.target.files]);
    e.target.value = "";
  });
  $("#downloadBtn").addEventListener("click", () => {
    if (!state.doc) return;
    const a = document.createElement("a");
    a.href = docUrl("/download");
    a.download = "";
    a.click();
  });
  $("#undoBtn").addEventListener("click", () => history("undo"));
  $("#redoBtn").addEventListener("click", () => history("redo"));
  $("#zoomIn").addEventListener("click", () => setZoom(1.2));
  $("#zoomOut").addEventListener("click", () => setZoom(1 / 1.2));
  $("#fitWidth").addEventListener("click", fitWidth);
  $("#fitPage").addEventListener("click", fitPage);
  $("#themeBtn").addEventListener("click", toggleTheme);
  $$(".tool").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  for (const id of ["#boldBtn", "#italicBtn"]) $(id).addEventListener("click", (e) => e.currentTarget.classList.toggle("on"));
  $("#saveFormBtn").addEventListener("click", saveForm);
  $("#fillDatesBtn").addEventListener("click", () => op({ op: "fill_dates", value: formatDate($("#dateFormat").value) }));
  $("#aiFillBtn").addEventListener("click", aiAutofill);

  $("#selAllBtn").addEventListener("click", () => {
    const all = state.selected.size === state.doc.pages.length;
    state.selected = all ? new Set() : new Set(state.doc.pages.map((_, i) => i));
    renderThumbs(); updateButtons();
  });
  $("#rotLBtn").addEventListener("click", () => op({ op: "rotate", pages: selectedPages(), angle: -90 }));
  $("#rotRBtn").addEventListener("click", () => op({ op: "rotate", pages: selectedPages(), angle: 90 }));
  $("#rot180Btn").addEventListener("click", () => op({ op: "rotate", pages: selectedPages(), angle: 180 }));
  $("#delPagesBtn").addEventListener("click", async () => {
    const pages = selectedPages();
    if (await op({ op: "delete", pages })) { state.selected.clear(); renderThumbs(); updateButtons(); }
  });
  $("#extractBtn").addEventListener("click", () => actions.extract());
  $("#blankBtn").addEventListener("click", () => actions.blank());
  $("#moveUpBtn").addEventListener("click", () => moveSelected(-1));
  $("#moveDownBtn").addEventListener("click", () => moveSelected(1));

  setupMenus();
  setupPinchZoom();
  setupSignDialog();
  const swatch = () => ($("#styleSwatch").style.background = $("#colorInput").value);
  $("#colorInput").addEventListener("input", swatch);
  swatch();
  loadFonts();
  $("#fontInput").addEventListener("change", (e) => { uploadFont(e.target.files[0]); e.target.value = ""; });

  // AI panel
  $("#aiClose").addEventListener("click", () => ($("#aiPanel").hidden = true));
  $("#aiSummarizeBtn").addEventListener("click", summarize);
  $("#aiClearBtn").addEventListener("click", () => { if (state.doc) state.chats[state.doc.id] = []; renderAiLog(); });
  $("#aiForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#aiQuestion").value;
    $("#aiQuestion").value = "";
    askQuestion(q);
  });
  $("#aiQuestion").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#aiForm").requestSubmit(); }
  });

  // Drag & drop files anywhere: PDFs open in tabs (or go into the current one),
  // other supported files are converted to a new PDF.
  const dz = $("#dropZone");
  window.addEventListener("dragover", (e) => {
    if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); dz.classList.add("over"); }
  });
  window.addEventListener("dragleave", () => dz.classList.remove("over"));
  window.addEventListener("drop", (e) => {
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    dz.classList.remove("over");
    const files = [...e.dataTransfer.files];
    const pdfs = files.filter((f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf");
    const others = files.filter((f) => !pdfs.includes(f));
    if (pdfs.length) {
      if (state.doc && confirm("Insert these PDF(s) into the current document?\n(Cancel opens them in new tabs.)")) insertFiles(pdfs);
      else openFiles(pdfs);
    }
    if (others.length) importFiles(others);
  });

  $("#viewer").addEventListener("scroll", updateCurrentPage, { passive: true });
  $("#viewer").addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey) || !state.doc) return;
    e.preventDefault();
    setZoom(e.deltaY < 0 ? 1.1 : 1 / 1.1);
  }, { passive: false });

  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();
    if (mod && key === "z" && !e.shiftKey) { e.preventDefault(); history("undo"); }
    else if (mod && (key === "y" || (key === "z" && e.shiftKey))) { e.preventDefault(); history("redo"); }
    else if (mod && key === "s") { e.preventDefault(); $("#downloadBtn").click(); }
    else if (mod && key === "o") { e.preventDefault(); $("#openInput").click(); }
    else if (mod && (key === "=" || key === "+")) { e.preventDefault(); setZoom(1.2); }
    else if (mod && key === "-") { e.preventDefault(); setZoom(1 / 1.2); }
    else if ((key === "delete" || key === "backspace") && state.imgSel) { e.preventDefault(); imageAction("delete"); }
    else if (key === "escape") {
      if (document.body.classList.contains("drawer-open") || document.body.classList.contains("thumbs-open")) setPanel(null);
      else if ($(".menu.open")) closeMenus();
      else if (state.imgSel?.cropping) { state.imgSel.cropping = false; state.imgSel.box.classList.remove("cropping"); }
      else if (state.imgSel) deselectImage();
      else setTool("select");
    }
  });

  setTool("select");
  updateButtons();
  fetch("/api/capabilities").then((r) => r.json()).then((c) => {
    state.caps = c;
    // Fonts can't be added to a shared server.
    if (c.hosted) $("#fontInput").closest("label").hidden = true;
  }).catch(() => {});
  restoreTabs();
}

setup();
