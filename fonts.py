"""Fonts for writing text: discovery, matching, and shaped (Arabic-capable) output.

Text is written with PyMuPDF's HTML box, which shapes Arabic (joined letters)
and lays out right-to-left text correctly. Fonts come from:
  * the project's `fonts/` folder (drop your own .ttf/.otf files there), and
  * fonts installed in Windows.
"""

import glob
import html
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

import pymupdf

USER_FONT_DIR = Path(__file__).parent / "fonts"
SYSTEM_FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    # Linux / macOS (searched including sub-folders)
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
]
FONT_EXTS = (".ttf", ".otf", ".ttc")

ARABIC_RE = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")

_lock = threading.Lock()
_fonts = None  # id -> info dict
_seen_state = None


def is_rtl(text):
    return bool(ARABIC_RE.search(text))


def norm(name):
    """'ABCDEF+DINNextLTArabic-Bold' -> 'dinnextltarabicbold', 'Arial-BoldMT' -> 'arialbold'."""
    name = re.sub(r"^[A-Z]{6}\+", "", name or "")
    name = re.sub(r"[^a-z0-9]", "", name.lower())
    name = re.sub(r"(psmt|mt|ps)$", "", name)
    # Repeat, so "Times New Roman Regular" and "Times New Roman" agree.
    prev = None
    while prev != name:
        prev, name = name, re.sub(r"(regular|roman|book|normal)$", "", name)
    return name


def _scan_dir(folder, group):
    found = []
    if not folder.is_dir():
        return found
    paths = folder.iterdir() if group == "user" else folder.rglob("*")
    for path in sorted(paths):
        if path.suffix.lower() not in FONT_EXTS:
            continue
        try:
            f = pymupdf.Font(fontfile=str(path))
        except Exception:
            continue
        if not f.name or f.name.startswith(("Marlett", "Webdings", "Wingdings", "Symbol")):
            continue
        found.append({
            "id": f"{group}:{path.name}",
            "name": f.name,
            "path": str(path),
            "group": group,
            "arabic": bool(f.has_glyph(0x0627)),
            "bold": bool(f.is_bold) or "bold" in f.name.lower(),
            "italic": bool(f.is_italic),
        })
    return found


def _user_dir_state():
    try:
        return tuple(sorted((p.name, p.stat().st_mtime) for p in USER_FONT_DIR.iterdir()))
    except OSError:
        return ()


def all_fonts(refresh=False):
    """Every usable font. Re-scans when files are added to or removed from `fonts/`."""
    global _fonts, _seen_state
    with _lock:
        state = _user_dir_state()
        if _fonts is None or refresh or state != _seen_state:
            _seen_state = state
            items = _scan_dir(USER_FONT_DIR, "user")
            for d in SYSTEM_FONT_DIRS:
                items += _scan_dir(d, "system")
            fonts = {}
            for it in items:
                fonts.setdefault(it["id"], it)
            _fonts = fonts
        return _fonts


def font_list():
    """For the UI: user fonts first, then Windows fonts, alphabetical."""
    items = sorted(all_fonts().values(), key=lambda f: (f["group"] != "user", f["name"].lower()))
    return [{k: f[k] for k in ("id", "name", "group", "arabic")} for f in items]


@lru_cache(maxsize=32)
def _read(path):
    return Path(path).read_bytes()


def find_by_name(*names):
    """First installed font whose normalised name matches one of `names`."""
    fonts = all_fonts().values()
    for n in names:
        key = norm(n)
        # Prefer the user's fonts folder over Windows fonts.
        for f in sorted(fonts, key=lambda f: f["group"] != "user"):
            if norm(f["name"]) == key:
                return f
    return None


def weight_of(fontname, flags=0):
    """'Bold', 'Medium' or 'Regular' from a PDF font name like 'DINNextLTArabic-Medium'."""
    n = (fontname or "").lower()
    if "bold" in n or "black" in n or "heavy" in n or flags & 16:
        return "Bold"
    if "medium" in n or "demi" in n or "semi" in n:
        return "Medium"
    return "Regular"


def lookalike(original, text, flags=0):
    """Free stand-ins for paid fonts, e.g. DIN Next (LT Arabic) -> Tajawal / Barlow."""
    if "din" not in norm(original):
        return None
    w = weight_of(original, flags)
    if is_rtl(text):
        return find_by_name(f"Tajawal {w}", f"Noto Kufi Arabic {w}", "Tajawal Regular")
    return find_by_name(f"Barlow {w}", "Barlow Regular")


# Fonts often found in PDFs -> installed or bundled fonts that look (and space) the
# same, best first. Checked in order against the PDF font's name, so the narrow /
# condensed families come before their regular-width relatives.
SIMILAR = [
    (r"(helvetica|arial|nimbussans|swiss)\w*(narrow|condensed|cond|cn|compressed)",
     ["Arial Narrow", "TeX Gyre Heros Cn", "Liberation Sans Narrow"]),
    (r"helvetica|nimbussans|swiss721|texgyreheros|freesans",
     ["TeX Gyre Heros", "Arial", "Liberation Sans"]),
    (r"arial|liberationsans|arimo",
     ["Arial", "Liberation Sans", "TeX Gyre Heros"]),
    (r"times|nimbusroman|tinos|liberationserif|texgyretermes|freeserif",
     ["Times New Roman", "Liberation Serif"]),
    (r"courier|nimbusmono|cousine|liberationmono|freemono",
     ["Courier New", "Liberation Mono"]),
    (r"calibri|carlito|segoe|verdana|tahoma|trebuchet|opensans|roboto|lato|sourcesans|frutiger"
     r"|myriad|univers|gillsans|futura|montserrat|inter|notosans|dejavusans|ubuntu|poppins",
     ["Arial", "Liberation Sans", "TeX Gyre Heros"]),
    (r"georgia|cambria|caladea|garamond|palatino|bookantiqua|minion|baskerville|bookman"
     r"|century|notoserif|dejavuserif|merriweather",
     ["Times New Roman", "Liberation Serif"]),
    (r"consolas|lucidaconsole|menlo|monaco|sourcecode|inconsolata|mono",
     ["Courier New", "Liberation Mono"]),
]


def style_of(fontname, flags=0):
    """(bold, italic) from a PDF font name like 'Helvetica-BoldOblique' or 'Arial,Bold'."""
    n = (fontname or "").lower()
    bold = weight_of(fontname, flags) == "Bold"
    italic = "italic" in n or "oblique" in n or bool(flags & 2)
    return bold, italic


def _family(fam, bold, italic, text):
    """The installed member of family `fam` in the wanted style, if it can write `text`."""
    base = find_by_name(f"{fam} Regular", fam)
    if base is None:
        return None
    info = variant(base, bold, italic)
    return info if supports(font_bytes(info), text) else None


def similar(original, flags, text):
    """The closest available font to the PDF font `original` for writing `text`.

    Returns (font info, explanation) or (None, None).
    """
    bold, italic = style_of(original, flags)
    # Paid fonts with free near-identical stand-ins (DIN Next -> Tajawal / Barlow).
    info = lookalike(original, text, flags)
    if info and supports(font_bytes(info), text):
        return info, f"{info['name']} (look-alike for {original})"
    if not is_rtl(text):
        key = norm(original)
        for pattern, families in SIMILAR:
            if re.search(pattern, key):
                for fam in families:
                    info = _family(fam, bold, italic, text)
                    if info:
                        return info, f"{info['name']} (look-alike for {original})"
                break
    # Nothing known: go by the kind of font (fixed-width / serif / sans).
    name = (original or "").lower()
    if "cour" in name or "mono" in name or flags & 8:
        kind = "mono"
    elif "times" in name or ("serif" in name and "sans" not in name) or flags & 4:
        kind = "serif"
    else:
        kind = "sans"
    info = fallback_font(text, bold, italic, kind)
    if info:
        styled = variant(info, bold, italic)
        if supports(font_bytes(styled), text):
            info = styled
        return info, f"{info['name']} (closest match; {original} is not installed)"
    return None, None


STYLE_SUFFIX = re.compile(
    r"[\s-]+(regular|bold|italic|oblique|medium|semibold|light|black|bold italic|bold oblique)$",
    re.IGNORECASE)


def family_of(name):
    """'Arial Bold Italic' -> 'Arial'."""
    prev = None
    while prev != name:
        prev, name = name, STYLE_SUFFIX.sub("", name)
    return name


def variant(info, bold=False, italic=False):
    """The bold / italic member of `info`'s family, or `info` itself if none is installed."""
    if not (bold or italic):
        return info
    fam = family_of(info["name"])
    names = []
    if bold and italic:
        names += [f"{fam} Bold Italic", f"{fam} Bold Oblique", f"{fam} BoldItalic"]
    if bold:
        names += [f"{fam} Bold"]
    if italic and not bold:
        names += [f"{fam} Italic", f"{fam} Oblique"]
    return find_by_name(*names) or info


def fallback_font(text, bold=False, italic=False, family="sans"):
    """A reasonable installed font for `text` when nothing better is known."""
    b, i = (" Bold" if bold else ""), (" Italic" if italic else "")
    if is_rtl(text):
        cands = [f"Tajawal{b or ' Regular'}", f"Noto Kufi Arabic{b or ' Regular'}",
                 f"Segoe UI{b}", f"Arial{b}", f"Tahoma{b}"]
    elif family == "serif":
        cands = [f"Times New Roman{b}{i}", f"Times New Roman{b}",
                 f"Liberation Serif{b}{i}", f"Liberation Serif{b}", f"DejaVu Serif{b}"]
    elif family == "mono":
        cands = [f"Courier New{b}{i}", f"Courier New{b}",
                 f"Liberation Mono{b}{i}", f"Liberation Mono{b}", f"DejaVu Sans Mono{b}"]
    else:
        cands = []
    # Arial, or its look-alike on Linux, then the fonts bundled in fonts/.
    cands += [f"Arial{b}{i}", f"Arial{b}", "Arial",
              f"Liberation Sans{b}{i}", f"Liberation Sans{b}", "Liberation Sans",
              f"DejaVu Sans{b}", "DejaVu Sans", f"Barlow{b or ' Regular'}", "Barlow Regular"]
    return find_by_name(*cands)


def supports(fontbuffer, text):
    try:
        f = pymupdf.Font(fontbuffer=fontbuffer)
    except Exception:
        return False
    # Trimmed (subset) fonts can keep a letter's code but drop its shape, so
    # also require a non-empty outline.
    for ch in set(text):
        if ch.isspace():
            continue
        if not f.has_glyph(ord(ch)):
            return False
        try:
            if f.glyph_bbox(ord(ch)).is_empty and ch.isalnum():
                return False
        except Exception:
            pass
    return True


def font_bytes(info):
    return _read(info["path"])


# ------------------------------------------------------------ writing

def _html_and_css(text, size, color, align="left", bold=False, italic=False):
    body = "<br>".join(html.escape(line) for line in text.split("\n"))
    r, g, b = (int(round(c * 255)) for c in color)
    # bold / italic here only matter when no real bold / italic font file was
    # found; MuPDF then synthesises the style from the regular font.
    css = (
        "@font-face {font-family: F0; src: url(f0.ttf);}"
        f"* {{font-family: F0; font-size: {size}px; color: rgb({r},{g},{b});"
        f" font-weight: {'bold' if bold else 'normal'};"
        f" font-style: {'italic' if italic else 'normal'};"
        " margin: 0; padding: 0; line-height: 1.2;}"
    )
    if align not in ("left", "center", "right"):
        align = "left"
    return f'<div style="text-align:{align}">{body}</div>', css


def _archive(fontbuffer):
    arc = pymupdf.Archive()
    arc.add(fontbuffer, "f0.ttf")
    return arc


def measure(text, fontbuffer, size, bold=False, italic=False):
    """Lay the text out once on a scratch page.

    Returns (width, height, baseline offset from box top) in points.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=4000, height=4000)
    body, css = _html_and_css(text, size, (0, 0, 0), bold=bold, italic=italic)
    page.insert_htmlbox(page.rect, body, css=css, archive=_archive(fontbuffer), scale_low=1)
    x1 = y1 = 0.0
    baseline = None
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            for s in line["spans"]:
                x1 = max(x1, s["bbox"][2])
                y1 = max(y1, s["bbox"][3])
                if baseline is None:
                    baseline = s["origin"][1]
    if baseline is None:
        baseline = size
    return x1, y1, baseline


def write(page, text, fontbuffer, size, color, *, left=None, right=None,
          center=None, top=None, baseline=None, display=False, align="left",
          bold=False, italic=False, opacity=1):
    """Write `text` with the given font.

    Anchor horizontally by `left`, `right` or `center` and vertically by `top`
    or `baseline`. Coordinates are page space, or display space if `display`.
    `align` lines up the lines of multi-line text inside the box.
    """
    w, h, bl = measure(text, fontbuffer, size, bold, italic)
    pad = size * 0.5
    if left is not None:
        x0 = left
    elif center is not None:
        x0 = center - w / 2
    else:
        x0 = right - w
    y0 = top if top is not None else baseline - bl
    # The spare room goes where it can't move the text: after left-aligned
    # text, before right-aligned text, and evenly around centred text.
    lead = {"center": pad / 2, "right": pad}.get(align, 0)
    frame = pymupdf.Rect(x0 - lead, y0, x0 - lead + w + pad, y0 + h + pad)
    rotate = 0
    if display:
        a = frame.tl * page.derotation_matrix
        b = frame.br * page.derotation_matrix
        frame = pymupdf.Rect(a, b).normalize()
        rotate = page.rotation
    body, css = _html_and_css(text, size, color, align, bold, italic)
    spare, _ = page.insert_htmlbox(frame, body, css=css, archive=_archive(fontbuffer),
                                   rotate=rotate, scale_low=1, opacity=opacity)
    if spare < 0:
        raise ValueError("The text could not be placed (box too small).")
    _fix_space_mapping(page, fontbuffer)


def _fix_space_mapping(page, fontbuffer):
    """Make copied/searched text use normal spaces.

    Fonts such as Arial draw the space and the no-break space with the same
    glyph; the PDF's ToUnicode map then reports every space as U+00A0, so
    searching for "Jane Doe" would fail. Point that glyph back to U+0020.
    """
    font = pymupdf.Font(fontbuffer=fontbuffer)
    gid = font.has_glyph(0x20)
    if not gid:
        return
    doc = page.parent
    pattern = re.compile(rb"(<%04x>\s*)<00a0>" % gid, re.IGNORECASE)
    for xref, _ext, ftype, basefont, *_ in page.get_fonts():
        if ftype != "Type0" or basefont != font.name:
            continue
        kind, val = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        cmap_xref = int(val.split()[0])
        data = doc.xref_stream(cmap_xref)
        fixed = pattern.sub(rb"\1<0020>", data)
        if fixed != data:
            doc.update_stream(cmap_xref, fixed)
