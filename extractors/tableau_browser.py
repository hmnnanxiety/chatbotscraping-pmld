"""Initialize public Tableau viewer; read only its transmitted visible layout.

No summary/underlying-data commands are sent: the FO publisher denies those.
"""
import json
from urllib.parse import urlsplit
from extractors.tableau_public import TableauExtractionError, view_identity, decode_bootstrap, visible_cards

EMBEDDING_API = "https://public.tableau.com/javascripts/api/tableau.embedding.3.latest.min.js"


class TableauBrowser:
    def __init__(self, source, timeout_ms=60000):
        self.source, self.timeout_ms = source, timeout_ms
        self.driver = self.browser = None
        self.frames, self.capture_error = [], None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError
        except ImportError as exc:
            raise TableauExtractionError("Install optional browser requirements and Playwright Chromium", "browser_unavailable") from exc
        try:
            self.driver = sync_playwright().start()
            self.browser = self.driver.chromium.launch(headless=True)
            self.context = self.browser.new_context(ignore_https_errors=False, accept_downloads=False,
                                                    locale="en-US", viewport={"width":1400,"height":1000})
            self.page = self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
            workbook, view = view_identity(self.source["url"])
            expected = f"/vizql/w/{workbook}/v/{view}/bootstrapSession/"
            def capture(response):
                parsed = urlsplit(response.url)
                if parsed.scheme == "https" and parsed.hostname == "public.tableau.com" and parsed.path.startswith(expected):
                    try:
                        if response.status != 200:
                            raise TableauExtractionError("Tableau viewer bootstrap unavailable", "unsupported")
                        self.frames = decode_bootstrap(response.body())
                    except Exception as exc:
                        self.capture_error = exc
            self.page.on("response", capture)
            # An isolated host page uses the official embedding library; no user
            # browser profile, session reuse, DOM clicks or filter changes.
            self.page.set_content('''<html><body><script type="module">
                import {TableauViz,TableauEventType} from ''' + json.dumps(EMBEDDING_API) + ''';
                window.viz=new TableauViz(); viz.src=''' + json.dumps(self.source["url"]) + ''';
                viz.width='1400px';viz.height='1000px';
                viz.addEventListener(TableauEventType.FirstInteractive,()=>window.ready=true);
                viz.addEventListener(TableauEventType.VizLoadError,()=>window.loadFailed=true);
                document.body.appendChild(viz);
                </script></body></html>''')
            self.page.wait_for_function("window.ready || window.loadFailed")
            if self.page.evaluate("Boolean(window.loadFailed)"):
                raise TableauExtractionError("Tableau viewer failed to initialize", "unsupported")
            return self
        except Exception as exc:
            self.__exit__()
            if isinstance(exc, TableauExtractionError):
                raise
            if isinstance(exc, TimeoutError):
                raise TableauExtractionError("Tableau viewer readiness timed out", "timeout") from exc
            raise TableauExtractionError("Tableau browser unavailable; check Chromium installation and platform TLS trust", "browser_unavailable") from exc

    def __exit__(self, *args):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.driver:
                self.driver.stop()

    def snapshot(self):
        if self.capture_error:
            raise self.capture_error
        catalog = self.page.evaluate('''() => {
            const s=viz.workbook.activeSheet;
            return {view:s.name,worksheets:(s.worksheets||[s]).map(w=>w.name)};
        }''')
        _, view = view_identity(self.source["url"])
        if catalog["view"] != view:
            raise TableauExtractionError("Tableau active dashboard differs from configured view")
        result = visible_cards(self.frames, view, self.source["visible_metrics"])
        # isHidden describes a workbook tab, not a dashboard zone. A hidden tab
        # can still be a visible map inside the active dashboard. Require both
        # official dashboard membership and visible network zone identity.
        if not result["visible_worksheets"] or not set(result["visible_worksheets"]).issubset(catalog["worksheets"]):
            raise TableauExtractionError("Tableau visible worksheets disagree with active dashboard API")
        catalog["worksheets"] = result["visible_worksheets"]
        # Cross-check network captions against exposed text, not screenshots.
        texts = [f.locator("body").inner_text() for f in self.page.frames if urlsplit(f.url).hostname == "public.tableau.com"]
        if len(texts) != 1:
            raise TableauExtractionError("Missing/ambiguous Tableau visible viewer frame")
        visible = " ".join(texts[0].split())
        for card in result["cards"]:
            if " ".join(card["caption"].split()) not in visible:
                raise TableauExtractionError("Tableau network metric not present in visible viewer")
        return {**catalog, **result}
