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

**Interface**: every feature sits in the File / Pages / Convert / Security / AI / View menus and seven tool
groups, so the screen stays clean. Tabs for several documents, undo/redo, zoom (buttons, Ctrl+wheel, pinch,
fit width, fit page), light and dark mode, drag-and-drop opening, page thumbnails.

**Phones and tablets** (screens up to 900 px wide): a slim top bar (menu, document name, undo, redo, download),
the menus in a slide-out drawer, the tools in a bottom bar with pop-up sheets, a slide-out page panel, full-screen
dialogs, PDFs opened at fit-to-width, finger scrolling and two-finger pinch zoom.

**Touch gestures and navigation**: one finger scrolls; two fingers pinch to zoom and move the page (also while a
drawing tool is selected, without drawing); double-tap zooms in where you tap and double-tap again fits the
width (double-click with the Pointer tool on a computer). Floating − / fit / + zoom buttons on touch screens, a
"‹ 2 / 5 ›" page indicator (tap the number to jump to a page), Home / End for the first / last page, and the
fit is kept when you rotate the phone or resize the window.

## Deploy (free) on Render

The repo has a `Dockerfile` and a `render.yaml`, so Render can build it straight from GitHub.

1. Sign in at https://render.com with your GitHub account.
2. **New → Blueprint**, pick this repository, and click **Apply** (or **New → Web Service**, pick the
   repo, Language **Docker**, Instance type **Free**).
3. Wait for the build (about 5 minutes). Your editor is at `https://<name>.onrender.com`.

Every push to `main` redeploys automatically.

The Docker image runs in **hosted mode** (`PDFEDITOR_HOSTED=1`):

- each browser only sees its own documents (identified by a cookie);
- documents are kept in memory and in `/tmp`, are forgotten after 6 idle hours, and are lost when the
  server restarts or sleeps, so download your work;
- at most 5 open documents per browser, 10 undo steps, 50 MB per upload;
- each visitor enters their own Anthropic API key (kept in their browser). Set `PDFEDITOR_SHARE_AI_KEY=1`
  and `ANTHROPIC_API_KEY` in Render's environment settings to let visitors use your key instead (you pay);
- adding fonts and changing server settings are turned off.

Free-plan limits: the service sleeps after 15 minutes without visitors and takes up to a minute to wake;
0.1 CPU and 512 MB RAM (the editor uses about 120–250 MB). Office → PDF uses the built-in converter (no
LibreOffice in the image) and OCR runs in the visitor's browser.

Other limits can be changed with environment variables: `PDFEDITOR_MAX_DOCS`, `PDFEDITOR_MAX_HISTORY`,
`PDFEDITOR_IDLE_HOURS`, `PDFEDITOR_MAX_UPLOAD_MB`.

To try the hosted build locally: `docker build -t pdf-editor .` then
`docker run -p 5050:10000 pdf-editor` and open http://127.0.0.1:5050.

## Fonts

Put `.ttf` / `.otf` files in `fonts/` (or use **+ Font**) to make them available for adding and editing text.

**Edit text** detects the PDF's font and writes your changes in the same font when it can, or else in the
closest free look-alike, keeping bold and italic. The typing box shows that font and names it.

| PDF font | Used for edits |
|---|---|
| Helvetica | TeX Gyre Heros (bundled) |
| Helvetica Condensed, Arial Narrow | TeX Gyre Heros Cn (bundled) |
| Arial | Arial, else Liberation Sans (bundled, same letter widths) |
| Times, Times New Roman | Times New Roman, else Liberation Serif (bundled) |
| Courier, Courier New | Courier New, else Liberation Mono (bundled) |
| DIN Next | Tajawal for Arabic, Barlow for Latin (bundled) |

See `fonts/README.txt` for the full list and the font licences.

## Tests

```
python -m unittest discover tests          # API tests
python app.py --no-browser                 # then, in another terminal:
python tests/ui_smoke.py                   # browser test (needs playwright and Chrome or Edge)
python tests/ui_mobile.py                  # phone test (iPhone 13); DEVICE="iPad Mini" for a tablet
```
