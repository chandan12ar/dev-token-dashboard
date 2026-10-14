"""Drive the dashboard through the walkthrough script and capture frames.

Frames come from Chrome's screencast (JPEG, near-lossless) with wall-clock timestamps, so the
narration can be placed on the exact moment each beat started. build_video.py turns the frames
into a constant-frame-rate video and mixes in the audio.
"""
import asyncio
import base64
import json
import math
import os
import time

from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
LEAD = 0.30     # seconds between a beat's visuals starting and its narration starting
TAIL = 0.55     # pause after each sentence


def _ease(k):
    return 4 * k * k * k if k < .5 else 1 - math.pow(-2 * k + 2, 3) / 2


class Recorder:
    def __init__(self, page, size):
        self.page, self.size = page, size
        self.x, self.y = size[0] - 160, size[1] // 2

    async def v(self, expr, arg=None):
        return await self.page.evaluate(expr, arg)

    async def move(self, x, y, ms=800):
        x0, y0, t0 = self.x, self.y, time.perf_counter()
        while True:
            k = min(1.0, (time.perf_counter() - t0) * 1000 / max(ms, 1))
            e = _ease(k)
            await self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            if k >= 1:
                break
            await asyncio.sleep(0.011)
        self.x, self.y = x, y

    def _clamp(self, r):
        w, h = self.size
        return (min(max(r["x"], 340), w - 60), min(max(r["y"], 150), h - 130))

    async def point_at(self, target, ms=850):
        r = await self.v("t => V.rect(t)", target)
        if not r:
            print("   ! target not found:", target)
            return False
        await self.move(*self._clamp(r), ms=ms)
        return True

    async def do(self, act, budget, stages):
        """Run one action. `budget` is the time left in the beat, for actions marked "over"."""
        p = self.page
        if "stage" in act:
            await self.v("() => V.hideCursor()")
            await self.v("s => V.stage(s)", stages[act["stage"]] if act["stage"] else None)
            await asyncio.sleep(0.9 if act["stage"] else 0.8)
        elif "reveal" in act:
            n = await self.v("() => V.revealCount()")
            if act["reveal"] == "over":
                # spread the reveals across the sentence; the last item lands ~1s before it ends
                span = max(1.0, budget - 1.2)
                for i in range(1, n + 1):
                    await self.v("i => V.reveal(i)", i)
                    if i < n:
                        await asyncio.sleep(span / n)
            else:
                await self.v("i => V.reveal(i)", act["reveal"])
        elif "tab" in act:
            await self.v("() => V.spot(null)")
            if await self.point_at("nav:" + act["tab"], 750):
                await p.mouse.down(); await p.mouse.up()
            await self.v("() => scrollTo(0, 0)")
            await asyncio.sleep(1.1)
        elif "scroll" in act:
            y = await self.v("([t, c]) => V.scrollInfo(t, c)", [act["scroll"], bool(act.get("center"))])
            if y is None:
                print("   ! scroll target not found:", act["scroll"])
            else:
                await self.v("([y, ms]) => V.scrollTo(y, ms)", [y, act.get("ms", 1500)])
        elif "scroll_y" in act:
            await self.v("([y, ms]) => V.scrollTo(y, ms)", [act["scroll_y"], act.get("ms", 1200)])
        elif "scroll_sheet" in act:
            await self.v("""async () => {
                const el = document.getElementById('sBox'); if (!el) return;
                const max = el.scrollHeight - el.clientHeight; if (max < 8) return;
                const go = (a, b, ms) => new Promise(res => { const t0 = performance.now();
                  const f = n => { const k = Math.min(1, (n - t0) / ms); const e = k < .5 ? 4*k*k*k : 1 - Math.pow(-2*k+2, 3)/2;
                    el.scrollTop = a + (b - a) * e; k < 1 ? requestAnimationFrame(f) : res(); }; requestAnimationFrame(f); });
                await go(0, max * 0.7, 2200); await go(max * 0.7, 0, 1400); }""")
        elif "spot" in act:
            ok = await self.v("([t, l]) => V.spot(t, l)", [act["spot"], act.get("label")])
            if not ok and act["spot"]:
                print("   ! spot target not found:", act["spot"])
        elif "hover" in act:
            await self.point_at(act["hover"], act.get("ms", 900))
        elif "sweep" in act:
            r = await self.v("t => { const el = V.find(t); if (!el) return null; const c = el.querySelector('canvas') || el; "
                             "const q = c.getBoundingClientRect(); return {l:q.left, r:q.right, t:q.top, b:q.bottom}; }", act["sweep"])
            if r:
                w, h = self.size
                y = min(max((r["t"] + r["b"]) / 2, 170), h - 150)
                x0, x1 = max(r["l"], 340) + 40, min(r["r"], w - 40) - 40
                await self.move(x0, y, 700)
                span = max(1.0, (budget - 0.8) * 1000) if act.get("over") else act.get("ms", 2500)
                await self.move(x1, y, span)
        elif "click" in act:
            if await self.point_at(act["click"], 800):
                await asyncio.sleep(0.15)
                await p.mouse.down(); await p.mouse.up()
                await asyncio.sleep(0.5)
        elif "hscroll" in act:
            await self.v("([t, to, ms]) => V.hscroll(t, to, ms)", [act["hscroll"], act["to"], act.get("ms", 2000)])
        elif "key" in act:
            await p.keyboard.press(act["key"])
        elif "badge" in act:
            await self.v("t => V.badge(t)", act["badge"] or None)
        elif "theme" in act:
            want = act["theme"].capitalize()
            for _ in range(3):
                if (await self.v("() => document.getElementById('themeLabel').textContent")) == want:
                    break
                await self.point_at("#themeBtn", 800)
                await p.mouse.down(); await p.mouse.up()
                await asyncio.sleep(0.7)
        elif "wait" in act:
            await asyncio.sleep(act["wait"])
        else:
            raise ValueError("unknown action: %r" % (act,))


async def record(script, durations, url, frames_dir, chromium=None, size=(1600, 900), privacy=False,
                 captions=True, badge="", data_line="", log=print):
    """Returns {"frames": [(ts, path)], "t_end": ts, "beats": [{"start": ts, "dur": s, "i": n}]}."""
    os.makedirs(frames_dir, exist_ok=True)
    stages = script["stages"]
    W, H = size
    async with async_playwright() as pw:
        launch = {"headless": True, "args": ["--hide-scrollbars", "--force-device-scale-factor=1"]}
        if chromium:
            launch["executable_path"] = chromium
        browser = await pw.chromium.launch(**launch)
        ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1,
                                        color_scheme="light")
        await ctx.add_init_script("try{localStorage.setItem('dtd-theme','light');localStorage.setItem('dtd_priv','%s');}catch(e){}"
                                  % ("1" if privacy else "0"))
        page = await ctx.new_page()
        await page.goto(url)
        await page.add_script_tag(path=os.path.join(HERE, "overlay.js"))
        await page.evaluate("() => V.ready()")
        rec = Recorder(page, size)
        await page.mouse.move(rec.x, rec.y)

        # start on the title card so the first frame is already the intro, not the dashboard
        await page.evaluate("s => V.stage(s)", stages["intro"])
        await asyncio.sleep(1.0)
        await page.evaluate("() => V.hideCursor()")

        frames = []
        cdp = await ctx.new_cdp_session(page)
        pending = set()

        async def on_frame(ev):
            path = os.path.join(frames_dir, "f%06d.jpg" % len(frames))
            frames.append((ev["metadata"]["timestamp"], path))
            with open(path, "wb") as f:
                f.write(base64.b64decode(ev["data"]))
            await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

        def handler(ev):
            t = asyncio.ensure_future(on_frame(ev)); pending.add(t); t.add_done_callback(pending.discard)

        cdp.on("Page.screencastFrame", handler)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W, "maxHeight": H,
                                                "everyNthFrame": 1})
        await asyncio.sleep(0.3)

        beats = []
        for i, b in enumerate(script["beats"]):
            dur = durations[i] + LEAD + TAIL
            t0 = time.time()
            beats.append({"i": i, "start": t0, "dur": dur})
            text = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            has_stage = any("stage" in a and a["stage"] for a in b.get("do", []))
            await page.evaluate("t => V.caption(t)", text if captions and not has_stage else None)
            log("  [%2d/%d] %s" % (i + 1, len(script["beats"]), text[:70]))
            for a in b.get("do", []):
                a = json.loads(json.dumps(a).replace("{BADGE}", badge).replace("{DATA}", data_line))
                remaining = dur - (time.time() - t0)
                await rec.do(a, remaining, stages)
            left = dur - (time.time() - t0)
            if left > 0:
                await asyncio.sleep(left)
            else:
                beats[-1]["dur"] = time.time() - t0     # actions ran long: the beat grows to fit
        await page.evaluate("() => V.caption(null)")
        await asyncio.sleep(2.2)    # hold on the outro card
        t_end = time.time()
        await cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.4)
        if pending:
            await asyncio.gather(*pending)
        await browser.close()
    return {"frames": frames, "t_end": t_end, "beats": beats}
