"""Local PDF editor: a small Flask server around PyMuPDF.

Coordinates: the browser works in *display* space (PDF points of the page as
shown, i.e. after /Rotate is applied). PyMuPDF's drawing, annotation and text
extraction APIs work in *unrotated* page space. `to_page()` / `rect_to_display()`
convert between the two at the API boundary.

Open documents are kept in memory with their undo history, and the current
version of each is also written to `workspace/` so that open tabs survive a
browser close or a server restart (undo history does not).
"""

import base64
import io
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
import webbrowser
import zipfile
from pathlib import Path

import pymupdf
from flask import Flask, abort, g, jsonify, request, send_file, send_from_directory
from PIL import Image

import ai
import convert
import fonts

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
WORKSPACE = Path(os.environ.get("PDFEDITOR_WORKSPACE") or ROOT / "workspace")


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# Hosted mode (PDFEDITOR_HOSTED=1) is for running on a public server: every
# browser only sees its own documents, memory use is capped, the AI key comes
# from each visitor's browser, and server-wide settings can't be changed.
HOSTED = _flag("PDFEDITOR_HOSTED")
# In hosted mode, let visitors use the server's ANTHROPIC_API_KEY (you pay for their use).
SHARE_AI_KEY = _flag("PDFEDITOR_SHARE_AI_KEY")
MAX_HISTORY = int(os.environ.get("PDFEDITOR_MAX_HISTORY", 10 if HOSTED else 30))
MAX_DOCS_PER_OWNER = int(os.environ.get("PDFEDITOR_MAX_DOCS", 5 if HOSTED else 1000))
IDLE_HOURS = float(os.environ.get("PDFEDITOR_IDLE_HOURS", 6 if HOSTED else 0))  # 0 = keep forever
MAX_UPLOAD_MB = int(os.environ.get("PDFEDITOR_MAX_UPLOAD_MB", 50 if HOSTED else 300))
OWNER_COOKIE = "pdfeditor_owner"

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# doc_id -> {"name", "versions": [bytes], "pos", "fonts_added", "owner", "touched"}
DOCS = {}
LOCK = threading.Lock()
_last_sweep = [0.0]


# ---------------------------------------------------------------- visitors

@app.before_request
def identify_visitor():
    """Each browser gets a random owner id (a cookie); locally there is one owner."""
    if not HOSTED:
        g.owner, g.new_owner = "local", False
        return
    owner = request.cookies.get(OWNER_COOKIE, "")
    g.new_owner = not re.fullmatch(r"[0-9a-f]{32}", owner)
    g.owner = secrets.token_hex(16) if g.new_owner else owner
    sweep_idle()


@app.after_request
def remember_visitor(resp):
    if getattr(g, "new_owner", False):
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        resp.set_cookie(OWNER_COOKIE, g.owner, max_age=30 * 86400, httponly=True,
                        samesite="Lax", secure=secure)
    return resp


def sweep_idle():
    """Forget documents nobody has touched for IDLE_HOURS (checked at most every minute)."""
    now = time.time()
    if not IDLE_HOURS or now - _last_sweep[0] < 60:
        return
    _last_sweep[0] = now
    cutoff = now - IDLE_HOURS * 3600
    with LOCK:
        for doc_id in [k for k, v in DOCS.items() if v.get("touched", now) < cutoff]:
            DOCS.pop(doc_id, None)
            forget(doc_id)


# ---------------------------------------------------------------- helpers

def get_entry(doc_id):
    entry = DOCS.get(doc_id)
    if entry is None or entry.get("owner", "local") != g.owner:
        abort(404, "Document not found. Please open it again.")
    entry["touched"] = time.time()
    return entry


def new_rev():
    """A starting revision number that is unique across restarts (so cached page images aren't reused)."""
    return int(time.time() * 1000)


def current_bytes(entry):
    return entry["versions"][entry["pos"]]


def open_current(entry):
    return pymupdf.open(stream=current_bytes(entry), filetype="pdf")


def save_version(entry, doc):
    data = doc.tobytes(garbage=3, deflate=True)
    # Drop any redo history, append, and trim.
    entry["versions"] = entry["versions"][: entry["pos"] + 1] + [data]
    if len(entry["versions"]) > MAX_HISTORY:
        entry["versions"] = entry["versions"][-MAX_HISTORY:]
    entry["pos"] = len(entry["versions"]) - 1
    entry["rev"] = entry.get("rev", new_rev()) + 1


def load_pdf(data, password=None):
    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            return None
        # Keep a decrypted working copy so later operations need no password.
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
        doc = pymupdf.open(stream=data, filetype="pdf")
    return doc


def to_page(page, x, y):
    return pymupdf.Point(x, y) * page.derotation_matrix


def rect_to_page(page, r):
    x0, y0, x1, y1 = r
    a = to_page(page, x0, y0)
    b = to_page(page, x1, y1)
    return pymupdf.Rect(a, b).normalize()


def rect_to_display(page, r):
    return list(pymupdf.Rect(r).transform(page.rotation_matrix).normalize())


def color(hexstr, default=(0, 0, 0)):
    if not hexstr:
        return default
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def int_to_rgb(c):
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def data_url_bytes(url):
    try:
        head, data = url.split(",", 1)
    except (AttributeError, ValueError):
        raise ValueError("The image data is missing.")
    return head, base64.b64decode(data)


def base14_for(fontname, flags):
    """Pick the closest built-in font for re-inserting edited text."""
    name = (fontname or "").lower()
    bold = "bold" in name or "black" in name or "heavy" in name or bool(flags & 16)
    italic = "italic" in name or "oblique" in name or bool(flags & 2)
    if "cour" in name or "mono" in name or bool(flags & 8):
        family = "cour"
    elif "times" in name or ("serif" in name and "sans" not in name) or bool(flags & 4):
        family = "tiro"
    else:
        family = "helv"
    table = {
        "helv": ["helv", "heit", "hebo", "hebi"],
        "tiro": ["tiro", "tiit", "tibo", "tibi"],
        "cour": ["cour", "coit", "cobo", "cobi"],
    }
    return table[family][(2 if bold else 0) + (1 if italic else 0)]


def page_info(doc):
    return [
        {"w": p.rect.width, "h": p.rect.height, "rotation": p.rotation}
        for p in doc
    ]


def doc_summary(doc_id):
    entry = get_entry(doc_id)
    doc = open_current(entry)
    has_forms = any(True for p in doc for _ in p.widgets())
    return {
        "id": doc_id,
        "name": entry["name"],
        # Changes on every edit / undo / redo; page image URLs include it.
        "version": entry.setdefault("rev", new_rev()),
        "edited": entry["pos"] > 0 or len(entry["versions"]) > 1,
        "pages": page_info(doc),
        "canUndo": entry["pos"] > 0,
        "canRedo": entry["pos"] < len(entry["versions"]) - 1,
        "hasForms": has_forms,
        "hasText": any(p.get_text("text").strip() for p in doc),
        "size": len(current_bytes(entry)),
    }


def new_doc(name, data):
    doc_id = uuid.uuid4().hex
    with LOCK:
        mine = sum(1 for v in DOCS.values() if v.get("owner", "local") == g.owner)
        if mine >= MAX_DOCS_PER_OWNER:
            abort(400, f"You can have up to {MAX_DOCS_PER_OWNER} documents open. Close a tab first.")
        DOCS[doc_id] = {"name": name, "versions": [data], "pos": 0, "owner": g.owner,
                        "touched": time.time(), "rev": new_rev()}
        persist(doc_id)
    return doc_id


# ---------------------------------------------------------------- auto-save

def persist(doc_id):
    """Write the current version to the workspace folder (called with LOCK held)."""
    entry = DOCS.get(doc_id)
    if entry is None:
        return
    try:
        WORKSPACE.mkdir(exist_ok=True)
        (WORKSPACE / f"{doc_id}.pdf").write_bytes(current_bytes(entry))
        meta = {"name": entry["name"], "fonts_added": bool(entry.get("fonts_added")),
                "owner": entry.get("owner", "local")}
        (WORKSPACE / f"{doc_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass  # auto-save is best effort; editing keeps working without it


def forget(doc_id):
    for ext in ("pdf", "json"):
        try:
            (WORKSPACE / f"{doc_id}.{ext}").unlink()
        except OSError:
            pass


def restore_workspace():
    if not WORKSPACE.is_dir():
        return
    for meta_path in sorted(WORKSPACE.glob("*.json"), key=lambda p: p.stat().st_mtime):
        doc_id = meta_path.stem
        pdf_path = WORKSPACE / f"{doc_id}.pdf"
        if not re.fullmatch(r"[0-9a-f]{32}", doc_id) or not pdf_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            data = pdf_path.read_bytes()
            pymupdf.open(stream=data, filetype="pdf").close()
        except Exception:
            continue
        DOCS[doc_id] = {"name": meta.get("name", "document.pdf"), "versions": [data], "pos": 0,
                        "fonts_added": meta.get("fonts_added", False),
                        "owner": meta.get("owner", "local"), "touched": time.time(),
                        "rev": new_rev()}


# ---------------------------------------------------------------- routes: files

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/capabilities")
def api_capabilities():
    try:
        pymupdf.get_tessdata()
        tesseract = True
    except Exception:
        tesseract = False
    return jsonify({"ai": ai_status(), "tesseract": tesseract, "hosted": HOSTED,
                    "maxUploadMb": MAX_UPLOAD_MB, **convert.converters()})


def ai_status():
    if not HOSTED:
        return ai.status()
    shared = SHARE_AI_KEY and ai.status()["configured"]
    return {"configured": shared, "source": "server" if shared else None, "perBrowser": True}


def ai_key():
    """The API key for this request: the visitor's own key in hosted mode."""
    if not HOSTED:
        return None  # ai.py uses the saved key or the environment
    key = request.headers.get("X-Anthropic-Key", "").strip()
    if key:
        return key
    if SHARE_AI_KEY:
        return None
    raise ai.AIError("Add your Anthropic API key in AI \u2192 Settings. It is kept in your browser only.")


@app.post("/api/settings")
def api_settings():
    if HOSTED:
        abort(403, "Server settings can't be changed on a hosted copy. Your API key stays in your browser.")
    body = request.get_json(silent=True) or {}
    if "anthropicKey" in body:
        ai.save_key(body["anthropicKey"] or "")
    return jsonify({"ai": ai.status()})


@app.get("/api/docs")
def api_docs():
    """This visitor's open documents (tabs), oldest first."""
    return jsonify([{"id": k, "name": v["name"]} for k, v in DOCS.items()
                    if v.get("owner", "local") == g.owner])


@app.delete("/api/doc/<doc_id>")
def api_close(doc_id):
    get_entry(doc_id)
    with LOCK:
        DOCS.pop(doc_id, None)
        forget(doc_id)
    return jsonify({"ok": True})


@app.post("/api/open")
def api_open():
    f = request.files.get("file")
    if f is None:
        abort(400, "No file uploaded.")
    data = f.read()
    try:
        doc = load_pdf(data, request.form.get("password"))
    except Exception as e:
        abort(400, f"Could not open PDF: {e}")
    if doc is None:
        return jsonify({"needsPassword": True}), 401
    doc_id = new_doc(f.filename or "document.pdf", doc.tobytes(garbage=1))
    return jsonify(doc_summary(doc_id))


@app.post("/api/import")
def api_import():
    """Convert images / Office / text files to one new PDF document."""
    files = request.files.getlist("file")
    if not files:
        abort(400, "No file uploaded.")
    notes = []
    try:
        images = [f for f in files if Path(f.filename).suffix.lower() in convert.IMAGE_EXTS]
        if len(images) == len(files):
            data = convert.images_to_pdf([f.read() for f in files])
        else:
            out = pymupdf.open()
            for f in files:
                pdf, note = convert.to_pdf(f.filename, f.read())
                if note:
                    notes.append(note)
                out.insert_pdf(pymupdf.open(stream=pdf, filetype="pdf"))
            data = out.tobytes(garbage=3, deflate=True)
    except convert.ConvertError as e:
        abort(400, str(e))
    except Exception as e:
        abort(400, f"Could not convert {files[0].filename}: {e}")
    name = Path(files[0].filename).stem + ".pdf"
    summary = doc_summary(new_doc(name, data))
    if notes:
        summary["message"] = notes[0]
    return jsonify(summary)


@app.get("/api/doc/<doc_id>")
def api_info(doc_id):
    return jsonify(doc_summary(doc_id))


@app.get("/api/doc/<doc_id>/page/<int:n>.png")
def api_render(doc_id, n):
    entry = get_entry(doc_id)
    zoom = min(max(float(request.args.get("zoom", 1.5)), 0.1), 6)
    doc = open_current(entry)
    if not 0 <= n < doc.page_count:
        abort(404)
    pix = doc[n].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True)
    resp = send_file(io.BytesIO(pix.tobytes("png")), mimetype="image/png")
    resp.headers["Cache-Control"] = "max-age=3600"  # URL includes version
    return resp


@app.get("/api/doc/<doc_id>/page/<int:n>/lines")
def api_lines(doc_id, n):
    """Text lines on a page, for the Edit Text tool."""
    doc = open_current(get_entry(doc_id))
    page = doc[n]
    return jsonify([{
        "bbox": rect_to_display(page, seg["bbox"]),
        "pageBbox": list(seg["bbox"]),
        "text": seg["text"],
        "size": round(seg["span"]["size"], 2),
        "font": seg["span"]["font"],
        "color": "#%06x" % seg["span"]["color"],
    } for seg in text_segments(page)])


def text_segments(page):
    """Editable pieces of text: each horizontal line, split where there is a wide gap.

    PDFs often put a label and its value (e.g. "رقم الرخصة" and "817977") on one
    line far apart; those become separate segments so editing one leaves the other.
    Arabic "presentation form" letters are normalised back to ordinary letters.
    """
    segments = []
    for blk in page.get_text("rawdict")["blocks"]:
        for line in blk.get("lines", []):
            if line["dir"] != (1.0, 0.0):
                continue  # only horizontal text is editable
            current, prev = None, None
            for span in line["spans"]:
                for ch in span["chars"]:
                    box = pymupdf.Rect(ch["bbox"])
                    if prev is not None:
                        gap = max(box.x0 - prev.x1, prev.x0 - box.x1)
                        if gap > span["size"] * 1.2:
                            current = None
                    if current is None:
                        current = {"chars": [], "bbox": pymupdf.Rect(box), "span": span,
                                   "origin": ch["origin"]}
                        segments.append(current)
                    current["chars"].append(ch["c"])
                    current["bbox"] |= box
                    prev = box
    out = []
    for seg in segments:
        text = unicodedata.normalize("NFKC", "".join(seg["chars"])).strip()
        if text:
            out.append({"text": text, "bbox": seg["bbox"], "span": seg["span"],
                        "origin": seg["origin"]})
    return out


@app.get("/api/doc/<doc_id>/page/<int:n>/annots")
def api_annots(doc_id, n):
    doc = open_current(get_entry(doc_id))
    page = doc[n]
    out = []
    for a in page.annots():
        out.append({"xref": a.xref, "type": a.type[1],
                    "bbox": rect_to_display(page, a.rect)})
    return jsonify(out)


@app.get("/api/doc/<doc_id>/page/<int:n>/images")
def api_images(doc_id, n):
    """Images on a page, for the image tool (inline images can't be edited)."""
    doc = open_current(get_entry(doc_id))
    page = doc[n]
    out = []
    for info in page.get_image_info(xrefs=True):
        if info["xref"] <= 0 or pymupdf.Rect(info["bbox"]).is_empty:
            continue
        if info["width"] <= 1 and info["height"] <= 1:
            continue  # the invisible stand-in left by a deleted image
        out.append({"xref": info["xref"], "bbox": rect_to_display(page, info["bbox"]),
                    "width": info["width"], "height": info["height"]})
    return jsonify(out)


@app.get("/api/doc/<doc_id>/page/<int:n>/widgets")
def api_widgets(doc_id, n):
    doc = open_current(get_entry(doc_id))
    page = doc[n]
    return jsonify([widget_info(page, w) for w in page.widgets()])


def widget_info(page, w):
    item = {
        "xref": w.xref,
        "name": w.field_name,
        "type": w.field_type_string,
        "value": w.field_value,
        "bbox": rect_to_display(page, w.rect),
        "readOnly": bool(w.field_flags & 1),
    }
    if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
                        pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        item["onState"] = w.on_state()
    if w.choice_values:
        item["choices"] = [c if isinstance(c, str) else c[1] for c in w.choice_values]
    return item


def widget_label(page, w):
    """Text printed just left of or above a form field (its visible label)."""
    r = w.rect
    left = page.get_textbox(pymupdf.Rect(r.x0 - 220, r.y0 - 2, r.x0, r.y1 + 2)).strip()
    above = page.get_textbox(pymupdf.Rect(r.x0 - 10, r.y0 - 22, r.x1 + 60, r.y0)).strip()
    return " | ".join(t.replace("\n", " ") for t in (left, above) if t)[:200]


def finished_bytes(entry):
    """The current PDF, with added fonts trimmed to the characters used."""
    data = current_bytes(entry)
    if not entry.get("fonts_added"):
        return data
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        doc.subset_fonts()
        return doc.tobytes(garbage=3, deflate=True)
    except Exception:
        return data  # a bigger file is better than a failed download


def open_finished(entry):
    return pymupdf.open(stream=finished_bytes(entry), filetype="pdf")


def stem_of(entry):
    return Path(entry["name"]).stem


@app.get("/api/doc/<doc_id>/download")
def api_download(doc_id):
    entry = get_entry(doc_id)
    name = stem_of(entry) + "_edited.pdf"
    return send_file(io.BytesIO(finished_bytes(entry)), mimetype="application/pdf",
                     as_attachment=True, download_name=name)


PERMISSIONS = {
    "print": pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ,
    "copy": pymupdf.PDF_PERM_COPY | pymupdf.PDF_PERM_ACCESSIBILITY,
    "edit": pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ASSEMBLE,
    "annotate": pymupdf.PDF_PERM_ANNOTATE,
    "forms": pymupdf.PDF_PERM_FORM,
}


@app.post("/api/doc/<doc_id>/export")
def api_export(doc_id):
    """Download with password protection, permission limits and/or metadata removal."""
    entry = get_entry(doc_id)
    b = request.get_json(silent=True) or {}
    doc = open_finished(entry)
    if b.get("cleanMetadata"):
        clean_metadata(doc)
    user_pw = b.get("userPassword") or ""
    owner_pw = b.get("ownerPassword") or ""
    allowed = b.get("permissions") or {}
    restricted = any(not allowed.get(k, True) for k in PERMISSIONS)
    opts = {"garbage": 3, "deflate": True}
    if user_pw or owner_pw or restricted:
        perm = 0
        for key, flag in PERMISSIONS.items():
            if allowed.get(key, True):
                perm |= flag
        # Restrictions need an owner password; make an unguessable one if none was given.
        opts.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_pw,
                    owner_pw=owner_pw or (secrets.token_urlsafe(24) if restricted or not user_pw else user_pw),
                    permissions=perm)
    data = doc.tobytes(**opts)
    suffix = "_protected" if "encryption" in opts else "_clean" if b.get("cleanMetadata") else "_edited"
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True,
                     download_name=stem_of(entry) + suffix + ".pdf")


@app.post("/api/doc/<doc_id>/convert/<fmt>")
def api_convert(doc_id, fmt):
    """PDF -> Word / Excel / PowerPoint / images (does not change the document)."""
    entry = get_entry(doc_id)
    b = request.get_json(silent=True) or {}
    stem = stem_of(entry)
    try:
        if fmt == "docx":
            data, name = convert.pdf_to_docx(finished_bytes(entry)), f"{stem}.docx"
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif fmt == "xlsx":
            data, name = convert.pdf_to_xlsx(open_current(entry)), f"{stem}.xlsx"
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pptx":
            data, name = convert.pdf_to_pptx(open_current(entry)), f"{stem}.pptx"
            mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        elif fmt == "images":
            doc = open_current(entry)
            pages = [n for n in b.get("pages") or range(doc.page_count) if 0 <= n < doc.page_count]
            data, name, mime = convert.pdf_to_images(doc, pages, b.get("format", "png"),
                                                     b.get("dpi", 150), stem)
        else:
            abort(400, f"Unknown format: {fmt}")
    except convert.ConvertError as e:
        abort(400, str(e))
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=name)


@app.get("/api/fonts")
def api_fonts():
    return jsonify(fonts.font_list())


@app.post("/api/fonts")
def api_add_font():
    """Copy an uploaded .ttf/.otf into the fonts folder."""
    if HOSTED:
        abort(403, "Adding fonts is turned off on this hosted copy.")
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "No font file uploaded.")
    name = Path(f.filename).name
    if Path(name).suffix.lower() not in fonts.FONT_EXTS:
        abort(400, "Please choose a .ttf, .otf or .ttc font file.")
    data = f.read()
    try:
        font = pymupdf.Font(fontbuffer=data)
    except Exception:
        abort(400, "That file is not a font PyMuPDF can read.")
    fonts.USER_FONT_DIR.mkdir(exist_ok=True)
    (fonts.USER_FONT_DIR / name).write_bytes(data)
    fonts.all_fonts(refresh=True)
    return jsonify({"added": font.name, "id": f"user:{name}", "fonts": fonts.font_list()})


@app.post("/api/doc/<doc_id>/extract")
def api_extract(doc_id):
    """Download selected pages as a new PDF (does not change the document)."""
    entry = get_entry(doc_id)
    pages = (request.get_json(silent=True) or {}).get("pages", [])
    src = open_finished(entry)
    out = pymupdf.open()
    for n in pages:
        out.insert_pdf(src, from_page=n, to_page=n)
    name = stem_of(entry) + "_pages.pdf"
    return send_file(io.BytesIO(out.tobytes(garbage=3, deflate=True)),
                     mimetype="application/pdf", as_attachment=True, download_name=name)


def parse_ranges(spec, count):
    """'1-3, 5, 7-' -> [(0, 2), (4, 4), (6, count-1)] (zero-based, inclusive)."""
    out = []
    for part in re.split(r"[,;\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ValueError(f"Can't read the page range “{part}”.")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1) or 1)
            b = int(m.group(2) or count)
        a, b = max(a, 1), min(b, count)
        if a > b:
            raise ValueError(f"The range “{part}” has no pages in this document.")
        out.append((a - 1, b - 1))
    if not out:
        raise ValueError("Enter page ranges such as 1-3, 4-6.")
    return out


@app.post("/api/doc/<doc_id>/split")
def api_split(doc_id):
    """Split into chunks of N pages, or into the given page ranges; returned as a zip."""
    entry = get_entry(doc_id)
    body = request.get_json(silent=True) or {}
    src = open_finished(entry)
    if body.get("ranges"):
        try:
            parts = parse_ranges(str(body["ranges"]), src.page_count)
        except ValueError as e:
            abort(400, str(e))
    else:
        every = max(int(body.get("every", 1)), 1)
        parts = [(s, min(s + every, src.page_count) - 1) for s in range(0, src.page_count, every)]
    stem = stem_of(entry)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for start, end in parts:
            part = pymupdf.open()
            part.insert_pdf(src, from_page=start, to_page=end)
            label = f"{start + 1}" if start == end else f"{start + 1}-{end + 1}"
            z.writestr(f"{stem}_p{label}.pdf", part.tobytes(garbage=3, deflate=True))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{stem}_split.zip")


# ---------------------------------------------------------------- routes: search & AI

LUHN_DIGITS = re.compile(r"\d")

SENSITIVE = {
    "card": re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
    "ssn": re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}(?![\d-])"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # Not inside a longer run of digit groups (card numbers, IDs).
    "phone": re.compile(r"(?<![\w+])(?<!\d[\s.-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])"
                        r"\d{3,4}[\s.-]?\d{3,4}(?![\s.-]?\d)"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),
}


def luhn_ok(digits):
    total = 0
    for i, d in enumerate(reversed(digits)):
        d = int(d)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def page_lines(page):
    """Each text line as (text, [char rect, ...]) for regex matching with positions."""
    out = []
    for blk in page.get_text("rawdict")["blocks"]:
        for line in blk.get("lines", []):
            chars, rects = [], []
            for span in line["spans"]:
                for ch in span["chars"]:
                    chars.append(ch["c"])
                    rects.append(pymupdf.Rect(ch["bbox"]))
            if chars:
                out.append(("".join(chars), rects))
    return out


def find_matches(doc, kinds, custom="", regex=False):
    patterns = [(k, SENSITIVE[k]) for k in kinds if k in SENSITIVE]
    if custom:
        try:
            patterns.append(("custom", re.compile(custom if regex else re.escape(custom), re.IGNORECASE)))
        except re.error as e:
            raise ValueError(f"The search pattern is not valid: {e}")
    results = []
    for n, page in enumerate(doc):
        for text, rects in page_lines(page):
            taken = []
            for kind, pat in patterns:
                for m in pat.finditer(text):
                    a, b = m.span()
                    if a == b or any(a < y and x < b for x, y in taken):
                        continue
                    found = m.group(0)
                    if kind == "card":
                        digits = "".join(LUHN_DIGITS.findall(found))
                        if not (13 <= len(digits) <= 19 and luhn_ok(digits)):
                            continue
                    taken.append((a, b))
                    box = pymupdf.Rect(rects[a])
                    for r in rects[a + 1:b]:
                        box |= r
                    results.append({"page": n, "kind": kind, "text": found.strip(),
                                    "bbox": rect_to_display(page, box)})
    return results


@app.post("/api/doc/<doc_id>/find")
def api_find(doc_id):
    """Find sensitive data (and/or custom text) for Smart Redact."""
    b = request.get_json(silent=True) or {}
    doc = open_current(get_entry(doc_id))
    try:
        return jsonify(find_matches(doc, b.get("kinds", []), b.get("custom", ""), b.get("regex", False)))
    except ValueError as e:
        abort(400, str(e))


def ai_route(fn):
    try:
        g.ai_key = ai_key()
        return jsonify(fn())
    except ai.AIError as e:
        abort(400, str(e))


@app.post("/api/doc/<doc_id>/ai/summary")
def api_ai_summary(doc_id):
    entry = get_entry(doc_id)
    b = request.get_json(silent=True) or {}
    return ai_route(lambda: {"text": ai.summarize(current_bytes(entry), open_current(entry),
                                                  b.get("length", "medium"), api_key=g.ai_key)})


@app.post("/api/doc/<doc_id>/ai/chat")
def api_ai_chat(doc_id):
    entry = get_entry(doc_id)
    b = request.get_json(silent=True) or {}
    return ai_route(lambda: {"text": ai.chat(current_bytes(entry), open_current(entry),
                                             b.get("messages", []), api_key=g.ai_key)})


@app.post("/api/doc/<doc_id>/ai/autofill")
def api_ai_autofill(doc_id):
    entry = get_entry(doc_id)
    b = request.get_json(silent=True) or {}
    doc = open_current(entry)
    fields = []
    for n, page in enumerate(doc):
        for w in page.widgets():
            if w.field_flags & 1 or w.field_type in (pymupdf.PDF_WIDGET_TYPE_BUTTON,
                                                     pymupdf.PDF_WIDGET_TYPE_SIGNATURE):
                continue
            f = {"xref": w.xref, "name": w.field_name, "type": w.field_type_string,
                 "page": n + 1, "label": widget_label(page, w), "current": w.field_value}
            if w.choice_values:
                f["choices"] = [c if isinstance(c, str) else c[1] for c in w.choice_values]
            fields.append(f)
    return ai_route(lambda: {"values": ai.autofill(current_bytes(entry), doc, fields,
                                                   b.get("info", ""), api_key=g.ai_key)})


# ---------------------------------------------------------------- routes: history

def _move(doc_id, step):
    entry = get_entry(doc_id)
    with LOCK:
        new = entry["pos"] + step
        if 0 <= new < len(entry["versions"]):
            entry["pos"] = new
            entry["rev"] = entry.get("rev", new_rev()) + 1
            persist(doc_id)
    return jsonify(doc_summary(doc_id))


@app.post("/api/doc/<doc_id>/undo")
def api_undo(doc_id):
    return _move(doc_id, -1)


@app.post("/api/doc/<doc_id>/redo")
def api_redo(doc_id):
    return _move(doc_id, 1)


# ---------------------------------------------------------------- routes: editing

@app.post("/api/doc/<doc_id>/merge")
def api_merge(doc_id):
    """Append other PDFs, or insert them after page `after` (zero-based)."""
    entry = get_entry(doc_id)
    files = request.files.getlist("file")
    if not files:
        abort(400, "No file uploaded.")
    after = request.form.get("after")
    with LOCK:
        doc = open_current(entry)
        at = doc.page_count if after in (None, "") else min(max(int(after) + 1, 0), doc.page_count)
        for f in files:
            data = f.read()
            if Path(f.filename).suffix.lower() != ".pdf":
                try:
                    data, _ = convert.to_pdf(f.filename, data)
                except convert.ConvertError as e:
                    abort(400, str(e))
            other = load_pdf(data, request.form.get("password"))
            if other is None:
                abort(400, f"{f.filename} is password protected.")
            doc.insert_pdf(other, start_at=at)
            at += other.page_count
        save_version(entry, doc)
        persist(doc_id)
    return jsonify(doc_summary(doc_id))


FONT_OPS = {"add_text", "edit_text", "watermark", "text_layer", "ocr"}


@app.post("/api/doc/<doc_id>/op")
def api_op(doc_id):
    entry = get_entry(doc_id)
    body = request.get_json(silent=True) or {}
    op = body.get("op")
    handler = OPS.get(op)
    if handler is None:
        abort(400, f"Unknown operation: {op}")
    with LOCK:
        doc = open_current(entry)
        try:
            message = handler(doc, body)
        except ValueError as e:
            abort(400, str(e))
        except (KeyError, IndexError, TypeError, RuntimeError) as e:
            abort(400, f"The operation failed: {e}")
        save_version(entry, doc)
        if op in FONT_OPS:
            entry["fonts_added"] = True
        persist(doc_id)
    summary = doc_summary(doc_id)
    if isinstance(message, str):
        summary["message"] = message
    return jsonify(summary)


# Each op receives (doc, body) and modifies doc in place. A returned string is
# shown to the user.

def op_rotate(doc, b):
    for n in b["pages"]:
        page = doc[n]
        page.set_rotation((page.rotation + int(b["angle"])) % 360)


def op_delete(doc, b):
    pages = sorted(set(b["pages"]))
    if len(pages) >= doc.page_count:
        raise ValueError("A PDF must keep at least one page.")
    doc.delete_pages(pages)


def op_reorder(doc, b):
    order = [int(n) for n in b["order"]]
    if sorted(order) != list(range(doc.page_count)):
        raise ValueError("Invalid page order.")
    doc.select(order)


def op_insert_blank(doc, b):
    after = int(b["after"])
    ref = doc[max(after, 0)] if doc.page_count else None
    w, h = (ref.rect.width, ref.rect.height) if ref else pymupdf.paper_size("a4")
    doc.new_page(pno=after + 1, width=w, height=h)


def op_crop_pages(doc, b):
    """Crop pages to a rectangle given in display coordinates of page `page`.

    Other pages get the same margins (distance from each edge) as that page.
    """
    ref = doc[b["page"]]
    x0, y0, x1, y1 = b["rect"]
    W, H = ref.rect.width, ref.rect.height
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
    if x1 - x0 < 20 or y1 - y0 < 20:
        raise ValueError("The crop area is too small.")
    margins = (x0, y0, W - x1, H - y1)
    for n in b.get("pages") or [b["page"]]:
        page = doc[n]
        w, h = page.rect.width, page.rect.height
        l, t, r, btm = margins
        if w - l - r < 20 or h - t - btm < 20:
            continue
        crop = rect_to_page(page, (l, t, w - r, h - btm))
        # set_cropbox wants mediabox coordinates; page.rect starts at the current cropbox.
        page.set_cropbox(crop + (page.cropbox.x0, page.cropbox.y0, page.cropbox.x0, page.cropbox.y0))


def op_uncrop_pages(doc, b):
    for n in b["pages"]:
        page = doc[n]
        page.set_cropbox(page.mediabox)


def op_add_text(doc, b):
    page = doc[b["page"]]
    size = float(b.get("size", 12))
    text = b["text"]
    choice = b.get("font") or "auto"
    bold, italic = bool(b.get("bold")), bool(b.get("italic"))
    align = b.get("align", "left")
    if choice != "auto" or fonts.is_rtl(text) or bold or italic:
        return _add_text_with_font(page, b, text, size, choice, bold, italic, align)
    # Standard font: a FreeText annotation, so "Remove mark" can delete it.
    lines = text.split("\n")
    width = max(pymupdf.get_text_length(l, fontname="helv", fontsize=size) for l in lines) + size
    height = size * 1.25 * len(lines) + size * 0.6
    x, y = b["x"], b["y"]
    # Build the rect in display space, then convert, so it reads upright.
    rect = rect_to_page(page, (x, y, x + width, y + height))
    annot = page.add_freetext_annot(
        rect, text, fontsize=size, fontname="helv",
        text_color=color(b.get("color")), fill_color=None,
        border_width=0, rotate=page.rotation,
        align={"left": 0, "center": 1, "right": 2}.get(align, 0),
    )
    annot.update()


def _add_text_with_font(page, b, text, size, choice, bold, italic, align):
    info = fonts.all_fonts().get(choice) if choice != "auto" else None
    note = ""
    if info is not None:
        styled = fonts.variant(info, bold, italic)
        if fonts.supports(fonts.font_bytes(styled), text):
            info = styled
    if info is None or not fonts.supports(fonts.font_bytes(info), text):
        if info is not None:
            note = f" ({info['name']} lacks some of these letters)"
        info = fonts.fallback_font(text, bold, italic)
        if info is not None:
            info = fonts.variant(info, bold, italic)
    if info is None:
        raise ValueError("No installed font can write this text.")
    # Synthesise bold / italic only when the chosen file isn't already that style.
    fake_bold = bold and not info["bold"]
    fake_italic = italic and not (info["italic"] or "italic" in info["name"].lower())
    fonts.write(page, text, fonts.font_bytes(info), size, color(b.get("color")),
                left=b["x"], top=b["y"], display=True, align=align,
                bold=fake_bold, italic=fake_italic)
    return f"Font: {info['name']}{note}"


def _shape_annot(doc, b, kind):
    page = doc[b["page"]]
    rect = rect_to_page(page, b["rect"])
    if kind == "rect":
        a = page.add_rect_annot(rect)
    else:
        a = page.add_circle_annot(rect)
    a.set_border(width=float(b.get("width", 2)))
    fill = color(b["fill"]) if b.get("fill") else None
    a.set_colors(stroke=color(b.get("color")), fill=fill)
    a.update()


def op_rect(doc, b):
    _shape_annot(doc, b, "rect")


def op_ellipse(doc, b):
    _shape_annot(doc, b, "ellipse")


def op_line(doc, b):
    page = doc[b["page"]]
    p1 = to_page(page, *b["p1"])
    p2 = to_page(page, *b["p2"])
    a = page.add_line_annot(p1, p2)
    a.set_border(width=float(b.get("width", 2)))
    a.set_colors(stroke=color(b.get("color")))
    if b.get("arrow"):
        a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
    a.update()


def op_ink(doc, b):
    page = doc[b["page"]]
    paths = [[tuple(to_page(page, x, y)) for x, y in path] for path in b["paths"]]
    a = page.add_ink_annot(paths)
    a.set_border(width=float(b.get("width", 2)))
    a.set_colors(stroke=color(b.get("color")))
    a.update()


def _markup(doc, b, kind, default_color):
    page = doc[b["page"]]
    rect = rect_to_page(page, b["rect"])
    # Mark the words inside the box if there are any, else the box itself.
    words = [pymupdf.Rect(w[:4]) for w in page.get_text("words")
             if pymupdf.Rect(w[:4]).intersects(rect)
             and (pymupdf.Rect(w[:4]) & rect).get_area() > 0.3 * pymupdf.Rect(w[:4]).get_area()]
    add = {"highlight": page.add_highlight_annot, "underline": page.add_underline_annot,
           "strikeout": page.add_strikeout_annot}[kind]
    a = add(words or rect)
    a.set_colors(stroke=color(b.get("color"), default_color))
    a.update()


def op_highlight(doc, b):
    _markup(doc, b, "highlight", (1, 1, 0))


def op_underline(doc, b):
    _markup(doc, b, "underline", (0, 0, 1))


def op_strikeout(doc, b):
    _markup(doc, b, "strikeout", (1, 0, 0))


def op_note(doc, b):
    """A sticky note (comment) at a point."""
    page = doc[b["page"]]
    text = (b.get("text") or "").strip()
    if not text:
        raise ValueError("The note is empty.")
    a = page.add_text_annot(to_page(page, *b["point"]), text, icon="Comment")
    a.set_colors(stroke=color(b.get("color"), (1, 0.8, 0)))
    a.set_info(title=b.get("author") or "")
    a.update()


def op_erase_area(doc, b):
    """Permanently remove everything inside a rectangle (white-out / black-out redaction)."""
    page = doc[b["page"]]
    rect = rect_to_page(page, b["rect"])
    page.add_redact_annot(rect, fill=color(b.get("fill"), (1, 1, 1)))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)


def op_redact_areas(doc, b):
    """Smart Redact: permanently remove many areas, across pages."""
    by_page = {}
    for it in b["items"]:
        by_page.setdefault(int(it["page"]), []).append(it["bbox"])
    fill = color(b.get("fill"), (0, 0, 0))
    for n, rects in by_page.items():
        page = doc[n]
        for r in rects:
            page.add_redact_annot(rect_to_page(page, r) + (-1, -1, 1, 1), fill=fill)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
    count = sum(len(v) for v in by_page.values())
    return f"Redacted {count} item(s). The text under them is gone for good."


def op_edit_text(doc, b):
    """Replace one line of existing text: remove it, then write the new text."""
    page = doc[b["page"]]
    target = pymupdf.Rect(b["pageBbox"])
    # Find the segment again to get its exact font details.
    seg = next((s for s in text_segments(page)
                if (s["bbox"] & target).get_area() > 0.8 * target.get_area()), None)
    if seg is None:
        raise ValueError("That text could not be found any more.")
    first = seg["span"]
    size = float(b.get("size") or first["size"])
    fill = color(b["color"]) if b.get("color") else int_to_rgb(first["color"])
    new_text = b["text"]
    # Choose the font before redacting, while the original is still on the page.
    fontbuf, used = _font_for_edit(doc, page, first, new_text, b.get("font") or "auto")

    # Shrink the box a little vertically so neighbouring lines are untouched.
    r = pymupdf.Rect(seg["bbox"])
    inset = r.height * 0.15
    r = pymupdf.Rect(r.x0, r.y0 + inset, r.x1, r.y1 - inset)
    page.add_redact_annot(r)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

    if not new_text.strip():
        return "Text removed."
    baseline = seg["origin"][1]
    if fontbuf is None:
        # No usable font file at all: fall back to a built-in PDF font.
        page.insert_text((seg["bbox"].x0, baseline), new_text, fontsize=size,
                         fontname=base14_for(first["font"], first["flags"]), color=fill)
        return "Font: standard PDF font (no matching font installed)"
    # Arabic/Hebrew text is right-aligned, so keep its right edge in place.
    if fonts.is_rtl(new_text):
        anchor = {"right": seg["bbox"].x1}
    else:
        anchor = {"left": seg["bbox"].x0}
    fonts.write(page, new_text, fontbuf, size, fill, baseline=baseline, **anchor)
    return f"Font: {used}"


def _font_for_edit(doc, page, span, text, choice):
    """Pick the font for replacement text. Returns (font bytes, description)."""
    if choice != "auto":
        info = fonts.all_fonts().get(choice)
        if info and fonts.supports(fonts.font_bytes(info), text):
            return fonts.font_bytes(info), info["name"]
    original = span["font"]
    # 1. The font embedded in this PDF, if it still contains every letter needed.
    #    (Usually it does not: PDFs embed only the letters they use.)
    for xref, _ext, _type, basefont, *_ in page.get_fonts():
        if fonts.norm(basefont) != fonts.norm(original):
            continue
        try:
            _, ext, _, buf = doc.extract_font(xref)
        except Exception:
            continue
        if buf and ext in ("ttf", "otf", "cff") and fonts.supports(buf, text):
            return buf, f"{original} (from this PDF)"
    # 2. The same font installed on this computer or in the fonts folder.
    info = fonts.find_by_name(original)
    if info and fonts.supports(fonts.font_bytes(info), text):
        return fonts.font_bytes(info), info["name"]
    # 3. A free look-alike for known paid fonts (DIN Next -> Tajawal / Barlow).
    info = fonts.lookalike(original, text, span["flags"])
    if info and fonts.supports(fonts.font_bytes(info), text):
        return fonts.font_bytes(info), f"{info['name']} (look-alike for {original})"
    # 4. The closest common font.
    name, flags = original.lower(), span["flags"]
    family = ("mono" if "cour" in name or "mono" in name or flags & 8 else
              "serif" if "times" in name or flags & 4 else "sans")
    bold = "bold" in name or "black" in name or bool(flags & 16)
    italic = "italic" in name or "oblique" in name or bool(flags & 2)
    info = fonts.fallback_font(text, bold, italic, family)
    if info:
        return fonts.font_bytes(info), f"{info['name']} (closest match; {original} is not installed)"
    return None, None


def op_delete_annot(doc, b):
    page = doc[b["page"]]
    for a in page.annots():
        if a.xref == b["xref"]:
            page.delete_annot(a)
            return
    raise ValueError("Annotation not found.")


def op_fill_form(doc, b):
    values = {int(k): v for k, v in b["values"].items()}
    for page in doc:
        for w in page.widgets():
            if w.xref not in values:
                continue
            v = values[w.xref]
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
                                pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                on = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1", "on")
                w.field_value = w.on_state() if on else "Off"
            else:
                w.field_value = v
            w.update()


DATE_FIELD = re.compile(r"date|dated|تاريخ|fecha|datum", re.IGNORECASE)


def op_fill_dates(doc, b):
    """Put today's date into empty text fields whose name or label mentions a date."""
    value = b["value"]
    count = 0
    for page in doc:
        for w in page.widgets():
            if w.field_type != pymupdf.PDF_WIDGET_TYPE_TEXT or w.field_flags & 1 or w.field_value:
                continue
            if DATE_FIELD.search(w.field_name or "") or DATE_FIELD.search(widget_label(page, w)):
                w.field_value = value
                w.update()
                count += 1
    if not count:
        raise ValueError("No empty date fields were found.")
    return f"Filled {count} date field(s) with {value}."


WIDGET_TYPES = {
    "text": pymupdf.PDF_WIDGET_TYPE_TEXT,
    "multiline": pymupdf.PDF_WIDGET_TYPE_TEXT,
    "checkbox": pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
    "dropdown": pymupdf.PDF_WIDGET_TYPE_COMBOBOX,
    "list": pymupdf.PDF_WIDGET_TYPE_LISTBOX,
}


def op_add_field(doc, b):
    """Create an interactive form field."""
    page = doc[b["page"]]
    kind = b.get("type", "text")
    if kind not in WIDGET_TYPES:
        raise ValueError(f"Unknown field type: {kind}")
    name = (b.get("name") or "").strip()
    existing = {w.field_name for p in doc for w in p.widgets()}
    if not name:
        i = 1
        while f"{kind}{i}" in existing:
            i += 1
        name = f"{kind}{i}"
    elif name in existing:
        raise ValueError(f"A field named “{name}” already exists.")
    w = pymupdf.Widget()
    w.field_type = WIDGET_TYPES[kind]
    w.field_name = name
    w.rect = rect_to_page(page, b["rect"])
    w.text_fontsize = 0  # auto size
    w.border_color = (0.4, 0.5, 0.7)
    w.border_width = 0.8
    w.fill_color = (0.93, 0.95, 1)
    if kind == "multiline":
        w.field_flags |= pymupdf.PDF_TX_FIELD_IS_MULTILINE
    if kind in ("dropdown", "list"):
        choices = [c.strip() for c in (b.get("choices") or "").split(",") if c.strip()]
        if not choices:
            raise ValueError("Enter the choices, separated by commas.")
        w.choice_values = choices
        w.field_value = choices[0]
    if kind == "checkbox":
        w.field_value = False
    page.add_widget(w)
    return f"Added {kind} field “{name}”."


def _image_pixels(doc, xref):
    """A PIL image of an embedded image, with transparency and in RGB(A)."""
    pix = pymupdf.Pixmap(doc, xref)
    smask = doc.xref_get_key(xref, "SMask")
    if smask[0] == "xref" and not pix.alpha:
        try:
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, int(smask[1].split()[0])))
        except Exception:
            pass
    if pix.colorspace is None:
        raise ValueError("This image type can't be edited.")
    if pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    mode = {(1, 0): "L", (1, 1): "LA", (3, 0): "RGB", (3, 1): "RGBA"}[(pix.colorspace.n, pix.alpha)]
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _find_image(page, xref, bbox):
    """The placement of image `xref` on `page` closest to display rect `bbox`."""
    target = pymupdf.Rect(bbox)
    best = None
    for info in page.get_image_info(xrefs=True):
        if info["xref"] != xref:
            continue
        r = pymupdf.Rect(rect_to_display(page, info["bbox"]))
        d = abs(r.x0 - target.x0) + abs(r.y0 - target.y0) + abs(r.x1 - target.x1) + abs(r.y1 - target.y1)
        if best is None or d < best[0]:
            best = (d, info)
    if best is None:
        raise ValueError("That image could not be found any more.")
    return best[1]


def _as_displayed(img, page, info):
    """Turn and mirror the image the way it appears on screen."""
    m = pymupdf.Matrix(info["transform"]) * page.rotation_matrix
    o = pymupdf.Point(0, 0) * m
    dx = pymupdf.Point(1, 0) * m - o
    dy = pymupdf.Point(0, 1) * m - o
    T = Image.Transpose
    if abs(dx.x) >= abs(dx.y):          # image x-axis runs horizontally
        if dx.x > 0 and dy.y > 0:
            return img
        if dx.x < 0 and dy.y < 0:
            return img.transpose(T.ROTATE_180)
        return img.transpose(T.FLIP_LEFT_RIGHT if dx.x < 0 else T.FLIP_TOP_BOTTOM)
    if dx.y > 0 and dy.x < 0:           # x runs down, y runs left: turned clockwise
        return img.transpose(T.ROTATE_270)
    if dx.y < 0 and dy.x > 0:
        return img.transpose(T.ROTATE_90)
    return img.transpose(T.TRANSPOSE if dx.y > 0 else T.TRANSVERSE)


def _strip_image_draw(doc, page, xref):
    """Delete the one "/Name Do" that draws image `xref` from the page's content.

    Only done when the image is drawn exactly once, directly by the page.
    """
    names = [it[7] for it in page.get_images(full=True) if it[0] == xref and it[9] == 0]
    if len(names) != 1:
        return False
    page.clean_contents()  # merge the content streams into one
    streams = page.get_contents()
    if len(streams) != 1:
        return False
    data = doc.xref_stream(streams[0])
    draw = re.compile(rb"/" + re.escape(names[0].encode()) + rb"\s+Do\b")
    if len(draw.findall(data)) != 1:
        return False
    doc.update_stream(streams[0], draw.sub(b"", data))
    return True


def _remove_image(doc, page, info):
    """Take one placed image off the page."""
    xref = info["xref"]
    if _strip_image_draw(doc, page, xref):
        return
    uses = sum(1 for p in doc for i in p.get_image_info(xrefs=True) if i["xref"] == xref)
    if uses == 1:
        page.delete_image(xref)  # replaces the picture with an invisible 1x1 one
    else:
        # Shared with other pages (e.g. a logo): blank only this placement's area.
        page.add_redact_annot(pymupdf.Rect(info["bbox"]), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)


def _encode(img, prefer_jpeg):
    out = io.BytesIO()
    if prefer_jpeg and img.mode in ("RGB", "L"):
        img.save(out, "JPEG", quality=92)
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def op_image_delete(doc, b):
    page = doc[b["page"]]
    _remove_image(doc, page, _find_image(page, b["xref"], b["bbox"]))


def op_image_transform(doc, b):
    """Move / resize / turn / crop an existing image.

    `rect` is the new display rectangle, `rotate` an extra clockwise turn in
    degrees, `crop` the kept part as fractions [x0, y0, x1, y1] of the image
    as displayed (before the extra turn).
    """
    page = doc[b["page"]]
    info = _find_image(page, b["xref"], b["bbox"])
    try:
        ext = doc.extract_image(b["xref"]).get("ext", "")
    except Exception:
        ext = ""
    img = _as_displayed(_image_pixels(doc, b["xref"]), page, info)
    crop = b.get("crop")
    if crop:
        x0, y0, x1, y1 = (min(max(float(v), 0), 1) for v in crop)
        if x1 - x0 < 0.01 or y1 - y0 < 0.01:
            raise ValueError("The crop area is too small.")
        w, h = img.size
        img = img.crop((round(x0 * w), round(y0 * h), max(round(x1 * w), round(x0 * w) + 1),
                        max(round(y1 * h), round(y0 * h) + 1)))
    turn = int(b.get("rotate", 0)) % 360
    if turn:
        img = img.rotate(-turn, expand=True)
    data = _encode(img, ext in ("jpeg", "jpg", "jpx"))
    _remove_image(doc, page, info)
    page.insert_image(rect_to_page(page, b["rect"]), stream=data, keep_proportion=False,
                      rotate=page.rotation)


def op_image(doc, b):
    """Place an image (signature, logo, photo or SVG) given as a data URL."""
    page = doc[b["page"]]
    head, data = data_url_bytes(b["dataUrl"])
    rect = rect_to_page(page, b["rect"])
    if "svg" in head:
        try:
            svg = pymupdf.open(stream=data, filetype="svg")
            src = pymupdf.open("pdf", svg.convert_to_pdf())
        except Exception:
            raise ValueError("That SVG file could not be read.")
        page.show_pdf_page(rect, src, 0, keep_proportion=True, rotate=page.rotation)
        return
    page.insert_image(rect, stream=data, keep_proportion=True, rotate=page.rotation)


def op_flatten(doc, b):
    """Burn annotations and form fields into the page content."""
    doc.bake(annots=True, widgets=True)


def clean_metadata(doc):
    doc.set_metadata({})
    doc.del_xml_metadata()
    for page in doc:
        for a in page.annots():
            a.set_info(title="", subject="")
            a.update()
    doc.scrub(attached_files=False, clean_pages=False, embedded_files=False, hidden_text=False,
              javascript=False, metadata=True, redactions=False, remove_links=False,
              reset_fields=False, reset_responses=False, thumbnails=True, xml_metadata=True)


def op_clean_metadata(doc, b):
    clean_metadata(doc)
    return "Removed author, software, dates and other hidden metadata."


def op_watermark(doc, b):
    """Text or image watermark on the chosen pages (default: every page)."""
    pages = b.get("pages") or range(doc.page_count)
    opacity = min(max(float(b.get("opacity", 0.25)), 0.02), 1)
    angle = float(b.get("angle", 45))
    if b.get("dataUrl"):
        _, data = data_url_bytes(b["dataUrl"])
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        alpha = img.getchannel("A").point(lambda v: int(v * opacity))
        img.putalpha(alpha)
        if angle % 360:
            img = img.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        out = io.BytesIO()
        img.save(out, "PNG")
        png = out.getvalue()
        scale = min(max(float(b.get("scale", 0.5)), 0.05), 1)
        for n in pages:
            page = doc[n]
            W, H = page.rect.width, page.rect.height
            w = W * scale
            h = w * img.height / img.width
            if h > H * scale:
                h = H * scale
                w = h * img.width / img.height
            r = ((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2)
            page.insert_image(rect_to_page(page, r), stream=png, keep_proportion=True,
                              rotate=page.rotation, overlay=True)
        return
    text = (b.get("text") or "").strip()
    if not text:
        raise ValueError("Enter the watermark text.")
    size = float(b.get("size", 60))
    col = color(b.get("color"), (0.8, 0.1, 0.1))
    if fonts.is_rtl(text):
        info = fonts.fallback_font(text, bold=True)
        if info is None:
            raise ValueError("No installed font can write this text.")
        for n in pages:
            page = doc[n]
            W, H = page.rect.width, page.rect.height
            fonts.write(page, text, fonts.font_bytes(info), size, col, center=W / 2,
                        top=H / 2 - size * 0.7, display=True, align="center", opacity=opacity)
        return "Arabic watermarks are written straight across (not at an angle)."
    font = "hebo"
    width = pymupdf.get_text_length(text, fontname=font, fontsize=size)
    for n in pages:
        page = doc[n]
        W, H = page.rect.width, page.rect.height
        center = to_page(page, W / 2, H / 2)
        # Lay the text out horizontally around the centre, then turn it about the centre.
        start = pymupdf.Point(center.x - width / 2, center.y + size * 0.35)
        turn = pymupdf.Matrix(angle + page.rotation)
        page.insert_text(start, text, fontsize=size, fontname=font, color=col,
                         fill_opacity=opacity, morph=(center, turn), overlay=True)


def op_compress(doc, b):
    level = b.get("level", "medium")
    dpi, quality = {"low": (200, 85), "medium": (150, 70), "high": (96, 55)}.get(level, (150, 70))
    before = len(doc.tobytes())
    doc.rewrite_images(dpi_threshold=dpi + 10, dpi_target=dpi, quality=quality)
    for page in doc:
        page.clean_contents()
    after = len(doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                            use_objstms=True))
    return f"Compressed: {before / 1024:,.0f} KB → about {after / 1024:,.0f} KB."


def _ocr_font():
    """A font with Latin and Arabic letters for the invisible OCR text layer."""
    return fonts.find_by_name("Arial", "Noto Kufi Arabic Regular", "Tajawal Regular", "Segoe UI",
                             "DejaVu Sans", "Liberation Sans")


def add_text_layer(page, words):
    """Write invisible, selectable text over a scanned page.

    `words` are {"text", "bbox"} in display coordinates.
    """
    info = _ocr_font()
    if info:
        fontname = "OCRText"
        page.insert_font(fontname=fontname, fontbuffer=fonts.font_bytes(info))
        font = pymupdf.Font(fontbuffer=fonts.font_bytes(info))
    else:
        fontname, font = "helv", pymupdf.Font("helv")
    added = 0
    for w in words:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        x0, y0, x1, y1 = w["bbox"]
        length = font.text_length(text, fontsize=1)
        if x1 <= x0 or y1 <= y0 or not length:
            continue
        size = min((y1 - y0) * 0.9, (x1 - x0) / length)
        if size < 1:
            continue
        # Text extraction reads right-to-left words in visual order, so store them reversed.
        stored = text[::-1] if fonts.is_rtl(text) else text
        # The trailing space keeps neighbouring words apart when the text is copied.
        page.insert_text(to_page(page, x0, y1 - (y1 - y0) * 0.2), stored + " ", fontsize=size,
                         fontname=fontname, render_mode=3, rotate=page.rotation)
        added += 1
    return added


def op_text_layer(doc, b):
    """Searchable text from in-browser OCR."""
    total = 0
    for item in b["pages"]:
        total += add_text_layer(doc[int(item["page"])], item["words"])
    if not total:
        raise ValueError("No text was recognised.")
    return f"Made {len(b['pages'])} page(s) searchable ({total} words)."


def op_ocr(doc, b):
    """Searchable text using Tesseract installed on this computer."""
    lang = b.get("lang", "eng")
    total = 0
    pages = b.get("pages") or range(doc.page_count)
    for n in pages:
        page = doc[n]
        if page.get_text("text").strip() and not b.get("force"):
            continue
        tp = page.get_textpage_ocr(language=lang, dpi=300, full=True)
        words = [{"text": w[4], "bbox": rect_to_display(page, w[:4])}
                 for w in page.get_text("words", textpage=tp)]
        total += add_text_layer(page, words)
    if not total:
        raise ValueError("No text was recognised (pages that already have text are skipped).")
    return f"Recognised {total} words. The text can now be searched and copied."


OPS = {
    "rotate": op_rotate,
    "delete": op_delete,
    "reorder": op_reorder,
    "insert_blank": op_insert_blank,
    "crop_pages": op_crop_pages,
    "uncrop_pages": op_uncrop_pages,
    "add_text": op_add_text,
    "rect": op_rect,
    "ellipse": op_ellipse,
    "line": op_line,
    "ink": op_ink,
    "highlight": op_highlight,
    "underline": op_underline,
    "strikeout": op_strikeout,
    "note": op_note,
    "erase_area": op_erase_area,
    "redact_areas": op_redact_areas,
    "edit_text": op_edit_text,
    "delete_annot": op_delete_annot,
    "fill_form": op_fill_form,
    "fill_dates": op_fill_dates,
    "add_field": op_add_field,
    "image": op_image,
    "image_delete": op_image_delete,
    "image_transform": op_image_transform,
    "flatten": op_flatten,
    "clean_metadata": op_clean_metadata,
    "watermark": op_watermark,
    "compress": op_compress,
    "text_layer": op_text_layer,
    "ocr": op_ocr,
}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def json_error(e):
    return jsonify({"error": getattr(e, "description", str(e))}), e.code


restore_workspace()

if __name__ == "__main__":
    # On a server, run with gunicorn instead (see Dockerfile). Keep one worker
    # process: open documents live in this process's memory.
    import sys
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5050))
    if "--no-browser" not in sys.argv and not HOSTED:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"PDF Editor running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False, threaded=True)
