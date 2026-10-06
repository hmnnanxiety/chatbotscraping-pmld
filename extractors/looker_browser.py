"""Optional isolated Chromium transport. Production TLS stays verified."""
from __future__ import annotations
import json
from pathlib import Path
import re
from extractors.looker_studio import LookerExtractionError, report_identity

DOM_SCRIPT = Path(__file__).with_name("looker_dom.js").read_text(encoding="utf-8")


def materialize_page(snapshot, indexed):
    """Assemble virtualized row segments, preserving every source position."""
    if snapshot.get("error"):
        raise LookerExtractionError(snapshot["error"], "unsupported")
    span = snapshot.get("range")
    if not isinstance(span, list) or len(span) != 3:
        raise LookerExtractionError("Looker table has no verifiable pagination range")
    first, last, total = span
    if any(type(v) is not int for v in span):
        raise LookerExtractionError("Invalid Looker pagination range")
    count = last-first+1 if total else 0
    keys = list(indexed)
    if keys and all(isinstance(k,tuple) and len(k)==2 and all(type(v) is int and v>=0 for v in k) for k in keys):
        # Native block/slot coordinates are stable even without display ordinals.
        # Never deduplicate by values or infer a fixed pixel height/block stride.
        keys.sort()
        blocks = sorted({k[0] for k in keys})
        valid = blocks == list(range(len(blocks)))
        for block in blocks:
            slots = [k[1] for k in keys if k[0]==block]
            valid = valid and slots == list(range(len(slots)))
        if not valid or len(keys)!=count:
            raise LookerExtractionError("Looker render-block row coverage incomplete")
    else:
        if any(type(k) is not int for k in keys):
            raise LookerExtractionError("Looker row position basis changed")
        keys.sort()
        if keys not in [list(range(count)), list(range(first-1,last))] or len(keys) != count:
            raise LookerExtractionError("Looker virtualized table row coverage incomplete")
    columns = snapshot["columns"]
    rows = [indexed[i] for i in keys]
    if columns and columns[0] == "":
        # Looker's display ordinal is not a data column. Reject any other blank header.
        if any(not r or not re.fullmatch(r"\d+\.", r[0]) for r in rows):
            raise LookerExtractionError("Unlabelled Looker column is not a row ordinal")
        if [int(r[0][:-1]) for r in rows] != list(range(first, last+1)):
            raise LookerExtractionError("Looker displayed ordinals disagree with pagination range")
        columns, rows = columns[1:], [r[1:] for r in rows]
    return {"columns": columns, "rows": rows, "row_start":first, "row_end":last,"total_rows":total}


class LookerBrowser:
    def __init__(self, source, timeout_ms=30000, max_scroll_steps=1000):
        self.source, self.timeout_ms, self.max_scroll_steps = source, timeout_ms, max_scroll_steps
        self.driver = self.browser = self.context = self.page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError, Error
        except ImportError as exc:
            raise LookerExtractionError("Install requirements-looker.txt and Playwright Chromium", "browser_unavailable") from exc
        self.TimeoutError, self.BrowserError = TimeoutError, Error
        try:
            self.driver = sync_playwright().start()
            self.browser = self.driver.chromium.launch(headless=True)
            self.context = self.browser.new_context(ignore_https_errors=False, accept_downloads=False,
                                                   locale="en-US", viewport={"width":1280,"height":900})
            self.page = self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
            self.page.goto(self.source["url"], wait_until="domcontentloaded")
            original = report_identity(self.source["url"])
            actual = report_identity(self.page.url)
            if actual[0] != original[0] or (original[1] and actual[1] != original[1]):
                raise LookerExtractionError("Looker navigation escaped configured report/page")
            self.resolved_page = actual[1] or original[1]
            self.page.locator(".lego-component").first.wait_for(state="attached")
            return self
        except Exception as exc:
            self.__exit__(None,None,None)
            if isinstance(exc, self.TimeoutError):
                raise LookerExtractionError("Looker viewer readiness timed out", "timeout") from exc
            if isinstance(exc, LookerExtractionError):
                raise
            raise LookerExtractionError("Chromium unavailable or viewer failed; check browser installation/TLS", "browser_unavailable") from exc

    def __exit__(self, *args):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.driver:
                self.driver.stop()

    def _chart(self, cid):
        if not re.fullmatch(r"cd-[A-Za-z0-9_-]+", cid):
            raise LookerExtractionError("Invalid Looker chart locator identity")
        chart = self.page.locator(".lego-component." + cid)
        if chart.count() != 1:
            raise LookerExtractionError("Looker component disappeared or duplicated")
        return chart

    def discover(self):
        # Wait for structured tables rather than taking an empty early-loading DOM as success.
        try:
            self.page.wait_for_function("() => [...document.querySelectorAll('.lego-component')].some(e => e.querySelector('.pageLabel') || e.querySelector('table'))")
            catalog = self.page.locator("body").evaluate(DOM_SCRIPT, "catalog")
            for c in catalog:
                c["page_id"] = self.resolved_page
            return catalog
        except self.TimeoutError as exc:
            raise LookerExtractionError("Looker has no ready structured tables", "unsupported") from exc

    def _ready(self, chart):
        try:
            chart.locator(".pageLabel, table").first.wait_for(state="attached")
            snapshot = chart.evaluate(DOM_SCRIPT, "snapshot")
            if snapshot.get("loading") or not snapshot.get("range"):
                raise LookerExtractionError("Looker chart loading/error; refusing partial table")
            return snapshot
        except self.TimeoutError as exc:
            raise LookerExtractionError("Looker table readiness timed out", "timeout") from exc

    def pages(self, cid):
        chart = self._chart(cid)
        chart.evaluate(DOM_SCRIPT, "reset_scroll")
        for _ in range(200):
            snapshot = self._ready(chart)
            indexed, base = {}, (snapshot.get("columns"), snapshot.get("range"))
            for step in range(self.max_scroll_steps):
                if (snapshot.get("columns"),snapshot.get("range")) != base:
                    raise LookerExtractionError("Looker table changed while scrolling")
                for item in snapshot.get("rows", []):
                    key, cells = ((item["block"],item["index"]) if item.get("block") is not None else item["index"]), item["cells"]
                    if key in indexed and indexed[key] != cells:
                        raise LookerExtractionError("Looker row changed during virtualization scroll")
                    indexed[key] = cells
                first,last,total = snapshot["range"]
                needed = last-first+1 if total else 0
                if len(indexed) == needed:
                    break
                if not chart.evaluate(DOM_SCRIPT,"scroll"):
                    # Final render may still be pending when the scroll position reaches the end.
                    try:
                        self.page.wait_for_function("([selector,before]) => { const el=document.querySelector(selector); return el && JSON.stringify(("+DOM_SCRIPT+")(el,'snapshot').rows) !== before; }",
                                                    arg=[".lego-component."+cid, json.dumps(snapshot["rows"], separators=(",", ":"))])
                    except self.TimeoutError as exc:
                        raise LookerExtractionError("Virtualized Looker rows cannot be fully read") from exc
                    snapshot = self._ready(chart)
                    continue
                try:
                    self.page.wait_for_function("([selector,before]) => { const el=document.querySelector(selector); return el && JSON.stringify(("+DOM_SCRIPT+")(el,'snapshot').rows) !== before; }",
                                                arg=[".lego-component."+cid, json.dumps(snapshot["rows"], separators=(",", ":"))],
                                                timeout=min(self.timeout_ms,1000))
                except self.TimeoutError:
                    # A buffered viewport need not change rows on every successful scroll.
                    # Continue toward the next block; coverage/step/end guards still apply.
                    pass
                snapshot = self._ready(chart)
            else:
                raise LookerExtractionError("Looker virtualization scroll limit exceeded")
            yield materialize_page(snapshot,indexed)
            if snapshot["range"][1] == snapshot["range"][2]:
                return
            if snapshot["next_disabled"]:
                raise LookerExtractionError("Looker next page disabled before advertised total")
            old_range = snapshot["range"]
            chart.locator(".pageForward").click()
            try:
                self.page.wait_for_function("([selector,old]) => { const el=document.querySelector(selector); const s=el&&("+DOM_SCRIPT+")(el,'snapshot'); return s && s.range && JSON.stringify(s.range)!==JSON.stringify(old) && !s.loading; }",
                                            arg=[".lego-component."+cid,old_range])
            except self.TimeoutError as exc:
                raise LookerExtractionError("Looker pagination did not advance", "timeout") from exc
            chart.evaluate(DOM_SCRIPT,"reset_scroll")
        raise LookerExtractionError("Looker pagination page limit exceeded")
