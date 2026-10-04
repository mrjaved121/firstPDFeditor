# PDF Editor

A PDF editor that runs on your own computer: a small Python (Flask + PyMuPDF) server with a browser UI.
Documents never leave the machine, except when you use the AI features.

## Run it

Double-click `start.bat` (installs the requirements the first time), or:

```
pip install -r requirements.txt
python app.py            # opens http://127.0.0.1:5050
```

Open documents are auto-saved in `workspace/` and reopen in their tabs after a browser or server restart
(the undo history is not kept across restarts).

## Features

**Editing**: edit existing text in place (keeps or matches the original font, including Arabic), add text
(font, size, colour, bold, italic, alignment), move / resize / rotate / crop / delete images, place PNG, JPG
and SVG images, rectangles, ellipses, lines, arrows, freehand drawing, highlight, underline, strikethrough,
sticky notes, white-out and black-out redaction that removes the content underneath.

**Pages**: drag-and-drop reordering, rotate 90/180/270°, delete, extract, insert blank pages, insert pages from
other PDFs (or images / Office files) at any position, split by page count or ranges, crop margins.

**Forms and signatures**: fill existing form fields, create text, multi-line, check box, drop-down and list
fields, fill empty date fields, signatures that are drawn, typed in a handwriting font or uploaded, date stamp.

**Security**: AES-256 password protection, permission limits (print, copy, edit, comment, fill forms), metadata
removal, text or image watermarks, Smart Redact (SSNs, Luhn-checked card numbers, emails, phone numbers,
IBANs, custom text or regex).

**Conversion**: PDF to Word, Excel (tables) and PowerPoint; PDF to JPG / PNG / WebP; Word, Excel, PowerPoint,
CSV, text, HTML, SVG, EPUB and images to PDF; compression; OCR to make scanned pages searchable.

- Office to PDF uses LibreOffice or Microsoft Office when installed, otherwise a built-in converter that keeps
  text, tables and pictures but not exact layout.
- OCR uses Tesseract if it is installed; otherwise it runs in the browser with Tesseract.js, which downloads
  its language data from the internet on first use (the document itself stays local).

**AI (Claude)**: summaries, chat with the document, and form auto-fill suggestions that you review before
saving. Set an Anthropic API key under AI → Settings (saved in `config.json`) or in the `ANTHROPIC_API_KEY`
environment variable. These features send the open document to the Anthropic API.

**Interface**: tabs for several documents, undo/redo, zoom (buttons, Ctrl+wheel, fit width, fit page), light
and dark mode, drag-and-drop opening, page thumbnails.

## Fonts

Put `.ttf` / `.otf` files in `fonts/` (or use **+ Font**) to make them available for adding and editing text.
The bundled Tajawal, Noto Kufi Arabic and Barlow fonts are used as free look-alikes for DIN Next.

## Tests

```
python -m unittest discover tests          # API tests
python app.py --no-browser                 # then, in another terminal:
python tests/ui_smoke.py                   # browser test (needs playwright and Chrome or Edge)
```
