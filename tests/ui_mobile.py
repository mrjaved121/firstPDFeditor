"""Phone / tablet layout test. Start the app first, then:

    python tests/ui_mobile.py [shots_dir]                 # iPhone 13
    DEVICE="iPad Mini" python tests/ui_mobile.py [dir]    # any Playwright device name
"""

import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import sample_pdf  # noqa: E402

URL = "http://127.0.0.1:5050"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
SHOTS.mkdir(parents=True, exist_ok=True)
DEVICE = os.environ.get("DEVICE", "iPhone 13")


def main():
    pdf = Path(tempfile.mkdtemp()) / "Entry Permit Form.pdf"
    pdf.write_bytes(sample_pdf())
    errors, step = [], [""]
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception:
            browser = pw.chromium.launch(channel="msedge")
        ctx = browser.new_context(**pw.devices[DEVICE])
        vw0, vh0 = pw.devices[DEVICE]["viewport"]["width"], pw.devices[DEVICE]["viewport"]["height"]
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"[{step[0]}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"[{step[0]}] {e}"))
        page.on("dialog", lambda d: d.accept())

        def go(name):
            step[0] = name
            print("-", name)

        def version():
            return page.evaluate("state.doc ? state.doc.version : -1")

        def wait_change(v):
            try:
                page.wait_for_function(f"state.doc && state.doc.version !== {v}", timeout=20000)
            except Exception:
                page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_failure.png")
                raise

        def sheet_tool(group, tool):
            page.click(f'.tool-group[data-group="{group}"]')
            page.wait_for_selector(".menu.open .sheet")
            page.click(f'.menu.open .tool[data-tool="{tool}"]')

        def drag(x0, y0, x1, y1):
            b = page.locator('.page[data-page="0"] .overlay').bounding_box()
            page.mouse.move(b["x"] + x0, b["y"] + y0)
            page.mouse.down()
            page.mouse.move(b["x"] + x1, b["y"] + y1, steps=6)
            page.mouse.up()

        def drawer(label, action):
            page.click("#drawerBtn")
            page.wait_for_function("document.body.classList.contains('drawer-open')")
            page.click(f'.main-menus .menu-btn:has-text("{label}")')
            page.click(f'.menu-item[data-action="{action}"]')

        go("load")
        page.goto(URL)
        page.wait_for_load_state("networkidle")
        while page.locator(".doc-tab").count():
            page.locator(".doc-tab .close").first.click()
            page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_1_empty.png")

        go("open via drawer")
        page.click("#drawerBtn")
        page.wait_for_function("document.body.classList.contains('drawer-open')")
        page.wait_for_timeout(400)  # slide-in animation
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_2_drawer.png")
        page.click('.main-menus .menu-btn:has-text("File")')
        page.set_input_files("#openInput", str(pdf))
        page.wait_for_selector(".page")
        page.wait_for_timeout(1200)
        assert not page.evaluate("document.body.classList.contains('drawer-open')"), "drawer should close"

        go("layout")
        m = page.evaluate("""() => ({
          vw: innerWidth, vh: innerHeight,
          overflow: document.documentElement.scrollWidth - innerWidth,
          top: document.querySelector('.topbar').offsetHeight,
          bottom: document.querySelector('.tools-row').offsetHeight,
          pageW: document.querySelector('.page').getBoundingClientRect().width,
          pageLeft: document.querySelector('.page').getBoundingClientRect().left,
          title: document.querySelector('#mobileTitle').textContent,
        })""")
        print("  ", m)
        assert m["overflow"] <= 0, "page scrolls sideways"
        assert m["top"] <= 64 and m["bottom"] <= 80, "bars too tall"
        assert m["pageW"] <= m["vw"] and m["pageLeft"] >= 0, "PDF should fit the width"
        assert m["pageW"] >= m["vw"] - 30, "the first page should fill the width"
        assert m["title"] == "Entry Permit Form.pdf"
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_3_doc.png")

        go("markup sheet + highlight")
        page.click('.tool-group[data-group="markup"]')
        page.wait_for_selector(".menu.open .sheet")
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_4_sheet.png")
        page.click('.menu.open .tool[data-tool="highlight"]')
        assert page.evaluate("document.querySelector('.tool-group[data-group=markup]').classList.contains('active')")
        v = version()
        drag(30, 60, 200, 75)
        wait_change(v)

        go("draw freehand")
        sheet_tool("draw", "ink")
        v = version()
        drag(40, 300, 200, 360)
        wait_change(v)

        go("style sheet")
        page.click(".style-menu .tool-group")
        page.wait_for_selector(".menu.open .props")
        page.fill("#sizeInput", "18")  # typing inside keeps the sheet open
        assert page.locator(".menu.open .props").is_visible()
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_5_style.png")
        page.click(".page-label")  # tap outside closes it
        assert not page.locator(".menu.open").count()

        go("typed signature")
        sheet_tool("sign", "sign")
        page.click('#signDialog .tab[data-tab="type"]')
        page.fill("#typedName", "Jane Doe")
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_6_sign.png")
        page.click("#signUse")
        v = version()
        drag(60, 220, 250, 280)
        wait_change(v)

        go("drawer action: watermark dialog")
        drawer("Security", "watermark")
        page.wait_for_selector("dialog[open]")
        box = page.locator("dialog[open]").bounding_box()
        assert box["width"] >= m["vw"] - 1, "dialog should be full width"
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_7_dialog.png")
        v = version()
        page.locator("dialog[open] button[type=submit]").click()
        wait_change(v)

        go("thumbnails panel")
        drawer("Pages", "thumbs")
        page.wait_for_function("document.body.classList.contains('thumbs-open')")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_8_thumbs.png")
        page.locator(".thumb").nth(1).click()  # select page 2
        assert page.evaluate("[...state.selected]") == [1]
        first_w = page.evaluate("state.doc.pages[0].w")
        v = version()
        page.click("#moveUpBtn")
        wait_change(v)
        assert page.evaluate("state.doc.pages[0].w") != first_w, "page 2 should now be first"
        assert page.evaluate("[...state.selected]") == [0], "the moved page stays selected"
        page.click("#thumbsClose")
        page.wait_for_function("!document.body.classList.contains('thumbs-open')")

        go("pinch zoom")
        page.click('.tool[data-tool="select"]')
        before = page.evaluate("state.scale")
        cdp = ctx.new_cdp_session(page)
        vb = page.locator("#viewer").bounding_box()
        cx, cy = vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2

        def touch(kind, d):
            pts = [] if kind == "touchEnd" else [{"x": cx - d, "y": cy, "id": 1}, {"x": cx + d, "y": cy, "id": 2}]
            cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})

        touch("touchStart", 40)
        for d in range(45, 120, 8):
            touch("touchMove", d)
        touch("touchEnd", 0)
        page.wait_for_timeout(500)
        after = page.evaluate("state.scale")
        print(f"   scale {before:.2f} -> {after:.2f}")
        assert after > before * 1.5, "pinch should zoom in"

        go("one finger scrolls, two fingers zoom")
        # The app must never block a one-finger swipe on the page (that is the
        # browser's scrolling); it takes over only two-finger gestures.
        res = page.evaluate("""() => {
          const ov = document.querySelector('.page .overlay');
          const r = ov.getBoundingClientRect();
          const t = (id, x, y) => new Touch({identifier: id, target: ov, clientX: x, clientY: y});
          const fire = (type, touches) => {
            const ev = new TouchEvent(type, {touches, targetTouches: touches, changedTouches: touches,
                                             cancelable: true, bubbles: true});
            ov.dispatchEvent(ev);
            return ev.defaultPrevented;
          };
          const x = r.left + 100, y = r.top + 100;
          const one = [fire('touchstart', [t(1, x, y)]), fire('touchmove', [t(1, x, y - 60)])];
          fire('touchend', []);
          const two = fire('touchstart', [t(1, x, y), t(2, x + 80, y)]);
          fire('touchend', []);
          return {one, two};
        }""")
        print("  ", res)
        assert res["one"] == [False, False], "one-finger swipes must reach the browser"
        assert res["two"] is True, "two-finger gestures are handled by the app"

        go("page indicator")
        assert page.locator("#pagePill").is_visible()
        page.evaluate("goToPage(0)")
        assert page.locator("#pageLabel").text_content() == "1 / 2"
        page.evaluate("document.querySelector('#viewer').scrollTop = 1e6")
        page.wait_for_function("document.querySelector('#pageLabel').textContent === '2 / 2'")
        page.evaluate("goToPage(0)")
        page.click('#pagePill [data-p="next"]')
        page.wait_for_function("state.currentPage === 1")
        assert page.locator("#pageLabel").text_content() == "2 / 2"
        page.click("#pageLabel")
        page.fill("dialog[open] input[name=page]", "1")
        page.locator("dialog[open] button[type=submit]").click()
        page.wait_for_function("state.currentPage === 0")

        go("zoom buttons")
        assert page.locator("#zoomFab").is_visible()
        s0 = page.evaluate("state.scale")
        page.click('#zoomFab [data-z="in"]')
        assert page.evaluate("state.scale") > s0 * 1.2
        page.click('#zoomFab [data-z="fit"]')
        assert page.evaluate("state.fit") == "width"
        assert abs(page.evaluate("state.scale - fitWidthScale()")) < 0.01

        go("double-tap zoom")
        page.click('.tool[data-tool="select"]')
        fit = page.evaluate("fitWidthScale()")
        pb = page.locator('.page[data-page="0"]').bounding_box()
        tx, ty = pb["x"] + pb["width"] * 0.4, max(pb["y"], vb["y"]) + 120

        def tap():
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": tx, "y": ty}]})
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

        tap(); page.wait_for_timeout(80); tap(); page.wait_for_timeout(400)
        zoomed = page.evaluate("state.scale")
        print(f"   double-tap: {fit:.2f} -> {zoomed:.2f}")
        assert zoomed > fit * 2, "double-tap should zoom in"
        page.wait_for_timeout(400)
        tap(); page.wait_for_timeout(80); tap(); page.wait_for_timeout(400)
        assert abs(page.evaluate("state.scale") - fit) < 0.02, "second double-tap goes back to fit width"

        go("pinch while drawing does not draw")
        sheet_tool("draw", "ink")
        v = version()
        s0 = page.evaluate("state.scale")
        touch("touchStart", 40)
        for d in range(45, 110, 8):
            touch("touchMove", d)
        touch("touchEnd", 0)
        page.wait_for_timeout(800)
        assert page.evaluate("state.scale") > s0 * 1.3, "pinch should zoom with a drawing tool selected"
        assert version() == v, "a pinch must not leave a pen stroke"
        page.click('.tool[data-tool="select"]')

        go("rotate keeps fit width")
        page.click('#zoomFab [data-z="fit"]')
        page.set_viewport_size({"width": vh0, "height": vw0})
        page.wait_for_timeout(700)
        land = page.evaluate("[state.scale, fitWidthScale(), state.fit]")
        print("   landscape:", land)
        assert land[2] == "width" and abs(land[0] - land[1]) < 0.01
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "no sideways page scroll"
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_10_landscape.png")
        page.set_viewport_size({"width": vw0, "height": vh0})
        page.wait_for_timeout(700)
        assert abs(page.evaluate("state.scale - fitWidthScale()")) < 0.01

        go("finger scrolling vs drawing")
        # Headless Chrome can't simulate a touch swipe, so check what decides it on a
        # real phone: the page lets a finger scroll unless a drawing tool is active.
        ta = "getComputedStyle(document.querySelector('.overlay')).touchAction"
        assert page.evaluate(ta) == "pan-x pan-y", page.evaluate(ta)
        assert page.evaluate("getComputedStyle(document.querySelector('#viewer')).touchAction") == "pan-x pan-y"
        sheet_tool("draw", "ink")
        assert page.evaluate(ta) == "none", "drawing should capture the finger"
        page.click('.tool[data-tool="select"]')
        assert page.evaluate(ta) == "pan-x pan-y"
        page.evaluate("setScale(2); document.querySelector('#viewer').scrollTop = 0")  # enough to scroll
        page.mouse.move(cx, cy)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(300)
        assert page.evaluate("document.querySelector('#viewer').scrollTop") > 100, "viewer should scroll"

        go("dark mode")
        drawer("View", "theme")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / f"{DEVICE.replace(' ', '_')}_9_dark.png")
        drawer("View", "theme")

        go("close")
        drawer("File", "closeTab")
        page.wait_for_selector("#dropZone:not([hidden])")
        browser.close()

    print("screenshots:", SHOTS)
    if errors:
        print("ERRORS:")
        for e in errors:
            print("  ", e)
        sys.exit(1)
    print("Mobile test passed.")


if __name__ == "__main__":
    main()
