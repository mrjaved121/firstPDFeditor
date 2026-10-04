"""Conversions between PDF and other formats.

PDF -> Word uses pdf2docx; PDF -> Excel uses PyMuPDF's table finder; PDF ->
PowerPoint puts each page on a slide as a picture. Office -> PDF uses
LibreOffice or Microsoft Office when installed, and otherwise a built-in
converter that keeps text, tables and pictures but not every bit of layout.
"""

import html
import io
import os
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".jxr", ".pnm", ".psd"}
MUPDF_EXTS = {".svg", ".xps", ".oxps", ".epub", ".fb2", ".cbz", ".mobi", ".txt"}
OFFICE_EXTS = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv",
               ".ppt", ".pptx", ".odp"}
ACCEPTED_EXTS = IMAGE_EXTS | MUPDF_EXTS | OFFICE_EXTS | {".html", ".htm", ".pdf"}

A4 = pymupdf.paper_rect("a4")
EMU_PER_PT = 12700


class ConvertError(Exception):
    pass


# ------------------------------------------------------------ PDF -> other

def pdf_to_docx(pdf_bytes):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pdf2docx imports the old "fitz" name
        from pdf2docx import Converter
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.pdf", Path(tmp) / "out.docx"
        src.write_bytes(pdf_bytes)
        cv = Converter(str(src))
        try:
            cv.convert(str(dst))
        finally:
            cv.close()
        return dst.read_bytes()


def pdf_to_xlsx(doc):
    """One sheet per table found; pages without tables get their text lines."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    for n, page in enumerate(doc):
        tables = page.find_tables().tables
        if tables:
            for k, t in enumerate(tables):
                ws = wb.create_sheet(f"P{n + 1} T{k + 1}"[:31])
                for row in t.extract():
                    ws.append([_cell(v) for v in row])
        else:
            lines = [l for l in page.get_text("text").splitlines() if l.strip()]
            if not lines:
                continue
            ws = wb.create_sheet(f"Page {n + 1}"[:31])
            for l in lines:
                ws.append([l])
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 8), 60)
    if not wb.sheetnames:
        ws = wb.create_sheet("Empty")
        ws.append(["No text or tables were found. For scanned PDFs, run OCR first."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell(v):
    if v is None:
        return None
    v = v.replace("\n", " ").strip()
    plain = v.replace(",", "")
    # Numbers become numbers, except ones like "007" whose leading zero matters.
    if not any(c.isdigit() for c in plain) or (plain[:1] == "0" and len(plain) > 1 and plain[1:2] != "."):
        return v
    try:
        return int(plain) if plain.lstrip("-").isdigit() else float(plain)
    except ValueError:
        return v


def pdf_to_pptx(doc):
    """Each page becomes a slide showing the page as a picture; its text goes in the notes."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    first = doc[0].rect
    prs.slide_width = Emu(int(first.width * EMU_PER_PT))
    prs.slide_height = Emu(int(first.height * EMU_PER_PT))
    blank = prs.slide_layouts[6]
    for page in doc:
        slide = prs.slides.add_slide(blank)
        pix = page.get_pixmap(dpi=150)
        img = io.BytesIO(pix.tobytes("png"))
        # Fit the page inside the slide, keeping its proportions.
        sw, sh = prs.slide_width, prs.slide_height
        scale = min(sw / (page.rect.width * EMU_PER_PT), sh / (page.rect.height * EMU_PER_PT))
        w, h = int(page.rect.width * EMU_PER_PT * scale), int(page.rect.height * EMU_PER_PT * scale)
        slide.shapes.add_picture(img, (sw - w) // 2, (sh - h) // 2, w, h)
        text = page.get_text("text").strip()
        if text:
            slide.notes_slide.notes_text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def pdf_to_images(doc, pages, fmt, dpi, stem):
    """Returns (bytes, filename, mimetype): one image, or a zip of several."""
    fmt = fmt.lower()
    if fmt not in ("png", "jpg", "webp"):
        raise ConvertError("Choose PNG, JPG or WebP.")
    dpi = min(max(int(dpi), 36), 600)
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[fmt]

    def render(n):
        pix = doc[n].get_pixmap(dpi=dpi, alpha=False)
        if fmt == "png":
            return pix.tobytes("png")
        if fmt == "jpg":
            return pix.tobytes("jpg", jpg_quality=90)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=90)
        return out.getvalue()

    if len(pages) == 1:
        return render(pages[0]), f"{stem}_p{pages[0] + 1}.{fmt}", mime
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n in pages:
            z.writestr(f"{stem}_p{n + 1}.{fmt}", render(n))
    return buf.getvalue(), f"{stem}_{fmt}.zip", "application/zip"


# ------------------------------------------------------------ other -> PDF

def to_pdf(filename, data):
    """Convert an uploaded file to PDF bytes. Returns (pdf bytes, note)."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return data, ""
    if ext in IMAGE_EXTS:
        return images_to_pdf([data]), ""
    if ext in MUPDF_EXTS:
        src = pymupdf.open(stream=data, filetype=ext.lstrip("."))
        return src.convert_to_pdf(), ""
    if ext in (".html", ".htm"):
        return html_to_pdf(data.decode("utf-8", "replace")), ""
    if ext in OFFICE_EXTS:
        return office_to_pdf(filename, data)
    raise ConvertError(f"Cannot convert {ext or 'this'} files. Supported: "
                       + ", ".join(sorted(e.lstrip('.') for e in ACCEPTED_EXTS)))


def images_to_pdf(blobs):
    """One page per image, page size = image size at 72 dpi (capped to A4-ish scale)."""
    out = pymupdf.open()
    for data in blobs:
        try:
            img = pymupdf.open(stream=data)
            pdf = pymupdf.open("pdf", img.convert_to_pdf())
        except Exception:
            # Formats MuPDF can't read (e.g. some WebP): go through Pillow.
            im = Image.open(io.BytesIO(data))
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGBA" if "A" in im.mode else "RGB")
            png = io.BytesIO()
            im.save(png, "PNG")
            pdf = pymupdf.open("pdf", pymupdf.open(stream=png.getvalue()).convert_to_pdf())
        out.insert_pdf(pdf)
    return out.tobytes(garbage=3, deflate=True)


def find_soffice():
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")):
        p = Path(base) / "LibreOffice" / "program" / "soffice.exe"
        if p.exists():
            return str(p)
    return None


def _office_com_available(ext):
    if os.name != "nt":
        return False
    import winreg
    prog = _com_app(ext)
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog))
        return True
    except OSError:
        return False


def _com_app(ext):
    if ext in (".xls", ".xlsx", ".ods", ".csv"):
        return "Excel.Application"
    if ext in (".ppt", ".pptx", ".odp"):
        return "PowerPoint.Application"
    return "Word.Application"


def converters():
    """Which Office converters this computer has, for the UI."""
    return {
        "libreoffice": bool(find_soffice()),
        "msoffice": any(_office_com_available(e) for e in (".docx", ".xlsx", ".pptx")),
    }


def office_to_pdf(filename, data):
    ext = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input{ext}"
        src.write_bytes(data)
        out = Path(tmp) / "input.pdf"
        soffice = find_soffice()
        if soffice:
            try:
                subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                               check=True, timeout=180, capture_output=True)
                if out.exists():
                    return out.read_bytes(), "Converted with LibreOffice."
            except Exception:
                pass
        if _office_com_available(ext):
            try:
                _convert_with_ms_office(ext, src, out)
                if out.exists():
                    return out.read_bytes(), "Converted with Microsoft Office."
            except Exception:
                pass
    # Built-in fallback.
    if ext == ".docx":
        pdf = docx_to_pdf(data)
    elif ext in (".xlsx", ".csv"):
        pdf = sheet_to_pdf(data, ext)
    elif ext == ".pptx":
        pdf = pptx_to_pdf(data)
    else:
        raise ConvertError(f"Converting {ext} needs LibreOffice or Microsoft Office installed.")
    return pdf, ("Converted with the built-in converter (text, tables and pictures; "
                 "install LibreOffice for exact layout).")


def _convert_with_ms_office(ext, src, out):
    """Drive Word / Excel / PowerPoint through PowerShell COM automation."""
    s, o = str(src).replace("'", "''"), str(out).replace("'", "''")
    app = _com_app(ext)
    if app == "Word.Application":
        script = (f"$a=New-Object -ComObject Word.Application; $a.Visible=$false;"
                  f"$d=$a.Documents.Open('{s}',$false,$true); $d.ExportAsFixedFormat('{o}',17);"
                  f"$d.Close(0); $a.Quit()")
    elif app == "Excel.Application":
        script = (f"$a=New-Object -ComObject Excel.Application; $a.Visible=$false; $a.DisplayAlerts=$false;"
                  f"$d=$a.Workbooks.Open('{s}',0,$true); $d.ExportAsFixedFormat(0,'{o}');"
                  f"$d.Close($false); $a.Quit()")
    else:
        script = (f"$a=New-Object -ComObject PowerPoint.Application;"
                  f"$d=$a.Presentations.Open('{s}',$true,$false,$false); $d.SaveAs('{o}',32);"
                  f"$d.Close(); $a.Quit()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, timeout=180, capture_output=True)


# ---- built-in fallbacks

def _font_archive():
    """Fonts for HTML conversion: Arabic-capable fonts from the fonts folder."""
    import fonts
    arc = pymupdf.Archive()
    css = []
    for i, name in enumerate(("Tajawal Regular", "Noto Kufi Arabic Regular")):
        info = fonts.find_by_name(name)
        if info:
            arc.add(fonts.font_bytes(info), f"font{i}.ttf")
            css.append(f"@font-face {{font-family: Ar{i}; src: url(font{i}.ttf);}}")
    families = ", ".join(f"Ar{i}" for i in range(len(css)))
    return arc, "".join(css), families


def html_to_pdf(body_html, css_extra="", landscape=False, archive=None):
    arc, font_css, families = _font_archive()
    if archive is not None:
        for name, data in archive:
            arc.add(data, name)
    fam = f"sans-serif, {families}" if families else "sans-serif"
    css = (font_css + f"body {{font-family: {fam}; font-size: 11pt; line-height: 1.35;}}"
           "table {border-collapse: collapse; margin: 6pt 0; width: 100%;}"
           "td, th {border: 0.5pt solid #888; padding: 2pt 4pt; vertical-align: top;}"
           "img {max-width: 100%;}" + css_extra)
    page_rect = pymupdf.paper_rect("a4-l" if landscape else "a4")
    where = page_rect + (50, 50, -50, -50)
    story = pymupdf.Story(html=body_html, user_css=css, archive=arc)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more = True
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def docx_to_pdf(data):
    import docx
    from docx.oxml.ns import qn

    d = docx.Document(io.BytesIO(data))
    images = []
    parts = []

    def run_html(run):
        out = []
        for blip in run._element.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            part = run.part.related_parts.get(rid)
            if part is not None:
                name = f"img{len(images)}{Path(part.partname).suffix}"
                images.append((name, part.blob))
                out.append(f'<img src="{name}"/>')
        t = html.escape(run.text or "")
        if t:
            if run.bold:
                t = f"<b>{t}</b>"
            if run.italic:
                t = f"<i>{t}</i>"
            if run.underline:
                t = f"<u>{t}</u>"
            out.append(t)
        return "".join(out)

    def para_html(p):
        inner = "".join(run_html(r) for r in p.runs) or "&#160;"
        style = (p.style.name or "").lower() if p.style is not None else ""
        align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(p.alignment, None)
        attr = f' style="text-align:{align}"' if align else ""
        if style.startswith("heading"):
            lvl = "".join(ch for ch in style if ch.isdigit()) or "2"
            lvl = min(int(lvl), 6)
            return f"<h{lvl}{attr} dir=\"auto\">{inner}</h{lvl}>"
        if style == "title":
            return f"<h1{attr} dir=\"auto\">{inner}</h1>"
        if "list" in style:
            return f"<p{attr} dir=\"auto\">• {inner}</p>"
        return f"<p{attr} dir=\"auto\">{inner}</p>"

    def table_html(t):
        rows = []
        for row in t.rows:
            cells = "".join("<td>" + "".join(para_html(p) for p in c.paragraphs) + "</td>"
                            for c in row.cells)
            rows.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows)}</table>"

    body = d.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            parts.append(para_html(docx.text.paragraph.Paragraph(child, d)))
        elif child.tag == qn("w:tbl"):
            parts.append(table_html(docx.table.Table(child, d)))
    return html_to_pdf("".join(parts), "p {margin: 0 0 6pt 0;}", archive=images)


def sheet_to_pdf(data, ext):
    if ext == ".csv":
        import csv
        text = data.decode("utf-8-sig", "replace")
        sheets = [("Sheet", list(csv.reader(io.StringIO(text))))]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        sheets = []
        for ws in wb.worksheets:
            rows = [[("" if v is None else v) for v in row] for row in ws.iter_rows(values_only=True)]
            # Trim empty trailing rows / columns.
            while rows and not any(str(v).strip() for v in rows[-1]):
                rows.pop()
            width = max((max((i + 1 for i, v in enumerate(r) if str(v).strip()), default=0) for r in rows),
                        default=0)
            sheets.append((ws.title, [r[:width] for r in rows]))
    parts = []
    widest = 0
    for title, rows in sheets:
        if not rows:
            continue
        widest = max(widest, max(len(r) for r in rows))
        trs = "".join("<tr>" + "".join(f'<td dir="auto">{html.escape(_fmt(v))}</td>' for v in r) + "</tr>"
                      for r in rows)
        parts.append(f"<h3>{html.escape(str(title))}</h3><table>{trs}</table>")
    if not parts:
        parts.append("<p>(empty spreadsheet)</p>")
    return html_to_pdf("".join(parts), "body {font-size: 9pt;}", landscape=widest > 6)


def _fmt(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def pptx_to_pdf(data):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(io.BytesIO(data))
    w, h = prs.slide_width / EMU_PER_PT, prs.slide_height / EMU_PER_PT
    out = pymupdf.open()
    arc, font_css, families = _font_archive()
    fam = f"sans-serif, {families}" if families else "sans-serif"

    def shapes_of(container):
        for sh in container:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shapes_of(sh.shapes)
            else:
                yield sh

    for slide in prs.slides:
        page = out.new_page(width=w, height=h)
        for sh in shapes_of(slide.shapes):
            if sh.left is None or sh.width is None:
                continue
            r = pymupdf.Rect(sh.left / EMU_PER_PT, sh.top / EMU_PER_PT,
                             (sh.left + sh.width) / EMU_PER_PT, (sh.top + sh.height) / EMU_PER_PT)
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    page.insert_image(r, stream=sh.image.blob)
                except Exception:
                    pass
            elif getattr(sh, "has_table", False):
                trs = "".join("<tr>" + "".join(f'<td dir="auto">{html.escape(c.text)}</td>' for c in row.cells)
                              + "</tr>" for row in sh.table.rows)
                page.insert_htmlbox(r, f"<table>{trs}</table>", archive=arc, css=font_css +
                                    f"* {{font-family: {fam}; font-size: 10pt;}}"
                                    "table {border-collapse: collapse;} td {border: 0.5pt solid #888; padding: 2pt;}")
            elif getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
                paras = []
                for p in sh.text_frame.paragraphs:
                    size = next((run.font.size.pt for run in p.runs if run.font.size), 18)
                    runs = "".join(
                        (f"<b>{html.escape(run.text)}</b>" if run.font.bold else html.escape(run.text))
                        for run in p.runs) or "&#160;"
                    paras.append(f'<p dir="auto" style="font-size:{size}pt; margin:0 0 4pt 0">{runs}</p>')
                page.insert_htmlbox(r, "".join(paras), archive=arc,
                                    css=font_css + f"* {{font-family: {fam};}}")
    if out.page_count == 0:
        out.new_page(width=w, height=h)
    return out.tobytes(garbage=3, deflate=True)
