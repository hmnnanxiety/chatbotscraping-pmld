"""Strict extraction of publisher-authored, visible Tableau dashboard metric cards.

This is deliberately NOT worksheet/underlying-data extraction. No permission
workarounds, guessed CSV endpoints, images or OCR are used.
"""
from __future__ import annotations
import copy
import json
import re
from urllib.parse import parse_qsl, quote, urlsplit

from contracts import SourceTarget, UnifiedRecord, metadata_for


class TableauExtractionError(ValueError):
    def __init__(self, message, category="validation"):
        super().__init__(message)
        self.ingestion_category = category


def view_identity(url):
    p = urlsplit(url)
    match = re.fullmatch(r"/views/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/?", p.path)
    display = {":language", ":display_count", ":origin", ":showVizHome"}
    if (p.scheme != "https" or p.hostname != "public.tableau.com" or
            p.port not in (None, 443) or p.username or p.password or p.fragment or
            not match or any(k not in display for k, _ in parse_qsl(p.query))):
        raise TableauExtractionError("Expected a public Tableau workbook/view URL with presentation parameters only")
    return match.group(1), match.group(2)


def decode_bootstrap(body):
    """Decode the length-framed JSON delivered by the public viewer, not a dump.

    Tableau frame lengths count JavaScript UTF-16 code units, not UTF-8 bytes.
    Decode each JSON boundary, then verify its advertised length. Reject garbage;
    never retain session identifiers or raw bootstrap payloads in output.
    """
    if not isinstance(body, bytes) or not body or len(body) > 5_000_000:
        raise TableauExtractionError("Invalid/oversized Tableau viewer response")
    cursor, frames = 0, []
    try:
        text = body.decode("utf-8")
        decoder = json.JSONDecoder()
        while cursor < len(text):
            match = re.match(r"([0-9]+);", text[cursor:])
            if not match:
                raise ValueError("missing frame length")
            size = int(match[1]); cursor += len(match[0])
            frame, end = decoder.raw_decode(text, cursor)
            if not size or len(text[cursor:end].encode("utf-16-le")) // 2 != size:
                raise ValueError("invalid frame length")
            frames.append(frame)
            cursor = end
    except (ValueError, UnicodeError) as exc:
        raise TableauExtractionError("Malformed/truncated Tableau viewer JSON") from exc
    return frames


def visible_cards(frames, view, definitions):
    models = [f for f in frames if isinstance(f, dict) and f.get("sheetName") == view]
    if len(models) != 1:
        raise TableauExtractionError("Missing/ambiguous Tableau configured view")
    try:
        zones = models[0]["worldUpdate"]["applicationPresModel"]["workbookPresModel"]["dashboardPresModel"]["zones"]
    except (KeyError, TypeError) as exc:
        raise TableauExtractionError("Tableau visible dashboard layout unavailable", "unsupported") from exc
    if not isinstance(zones, dict):
        raise TableauExtractionError("Invalid Tableau dashboard zones")
    found, update, publisher, worksheets = {}, set(), set(), set()
    for zone_id, zone in sorted(zones.items()):
        if not isinstance(zone, dict) or zone.get("isVisible") is not True:
            continue
        holder = zone.get("presModelHolder", {})
        if not isinstance(holder, dict):
            raise TableauExtractionError("Invalid Tableau visible zone model")
        visual = holder.get("visual", {})
        identity = visual.get("visualIdPresModel", {})
        if identity.get("worksheet"):
            if identity.get("dashboard") != view or not isinstance(identity["worksheet"], str):
                raise TableauExtractionError("Tableau visible worksheet escaped configured dashboard")
            worksheets.add(identity["worksheet"])
        text = holder.get("dashboardText", {}).get("caption", "")
        title = holder.get("visual", {}).get("visualTitle", {}).get("caption", "")
        for caption in (text, title):
            if not isinstance(caption, str):
                raise TableauExtractionError("Invalid Tableau visible caption")
            if caption.startswith("Last Update :"):
                update.add(caption.split(":", 1)[1].strip())
            if caption.startswith("Data Source :"):
                publisher.add(caption.split(":", 1)[1].strip())
        if zone.get("zoneCommon", {}).get("zoneType") != "text":
            continue
        lines = [s.strip() for s in text.splitlines() if s.strip()]
        if not lines or lines[0] not in definitions:
            continue
        label = lines[0]
        if label in found or len(lines) != 2:
            raise TableauExtractionError("Duplicate/ambiguous Tableau metric card")
        value = re.fullmatch(r"([+-]?\d+(?:[.,]\d+)*)\s+([^\s]+)", lines[1])
        if not value or value[2] != definitions[label]:
            raise TableauExtractionError("Missing/invalid Tableau metric value or unit")
        found[label] = {"metric": label, "display_value": value[1], "unit": value[2],
                        "zone_id": str(zone_id), "caption": text}
    if set(found) != set(definitions):
        raise TableauExtractionError("Required visible Tableau metrics missing; no metadata-only fallback", "unsupported")
    if len(update) > 1 or len(publisher) > 1:
        raise TableauExtractionError("Conflicting Tableau publisher/update captions")
    return {"cards": [found[k] for k in sorted(found)],
            "visible_worksheets": sorted(worksheets),
            "source_update_date": next(iter(update), None),
            "publisher": next(iter(publisher), None)}


class TableauPublicAdapter:
    source_type = "tableau_public"

    def __init__(self, source, browser_factory=None):
        self.config = copy.deepcopy(source)
        self.workbook, self.view = view_identity(source["url"])
        metrics = source.get("visible_metrics")
        if (not source.get("id") or not source.get("name") or not isinstance(metrics, dict) or
                not metrics or any(not isinstance(k, str) or not k.strip() or
                                   not isinstance(v, str) or not v.strip() for k, v in metrics.items())):
            raise TableauExtractionError("Tableau source requires id/name and explicit metric labels/units")
        self.adapter_id = f"{self.source_type}:{self.workbook}"
        self.target_prefixes = (self.adapter_id + ":" + self.view + "/",)
        if browser_factory is None:
            from extractors.tableau_browser import TableauBrowser
            browser_factory = TableauBrowser
        self.browser_factory, self._snapshot = browser_factory, None

    def _fetch(self):
        with self.browser_factory(self.config) as browser:
            return browser.snapshot()

    def discover(self):
        self._snapshot = None
        snapshot = self._fetch()
        self._validate(snapshot)
        self._snapshot = copy.deepcopy(snapshot)
        options = {"dashboard_name": self.config["name"], "health_scope": self.adapter_id}
        targets = [SourceTarget(self.source_type, self.workbook, self.view + "/visible_metrics",
                                self.config["url"], "Published dashboard metrics", options)]
        # Real worksheet identities, returned by the official active-sheet API.
        # They are explicitly unsupported, not empty successful data records.
        for worksheet in sorted(snapshot["worksheets"]):
            targets.append(SourceTarget(self.source_type, self.workbook,
                self.view + "/worksheet/" + quote(worksheet, safe=""), self.config["url"], worksheet,
                {**options, "skip_reason": "unsupported: Tableau worksheet data export is not enabled for this source; only published visible metric cards are supported"}))
        return targets

    def _validate(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("view") != self.view:
            raise TableauExtractionError("Tableau viewer escaped configured view")
        worksheets = snapshot.get("worksheets")
        if (not isinstance(worksheets, list) or not worksheets or
                any(not isinstance(w, str) or not w for w in worksheets) or len(set(worksheets)) != len(worksheets)):
            raise TableauExtractionError("Missing/duplicate Tableau worksheet identity")
        cards = snapshot.get("cards")
        if (not isinstance(cards, list) or any(not isinstance(c, dict) for c in cards) or
                {c.get("metric") for c in cards} != set(self.config["visible_metrics"]) or
                len(cards) != len(self.config["visible_metrics"])):
            raise TableauExtractionError("Incomplete Tableau metric coverage")
        for card in cards:
            if (not isinstance(card.get("display_value"), str) or
                    not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", card["display_value"]) or
                    card.get("unit") != self.config["visible_metrics"][card["metric"]]):
                raise TableauExtractionError("Invalid Tableau metric row")

    def extract(self, target):
        if target.source_type != self.source_type or target.dashboard_id != self.workbook or target.source_url != self.config["url"]:
            raise TableauExtractionError("Tableau target escaped configured source")
        if target.chart_id != self.view + "/visible_metrics":
            raise TableauExtractionError("Detailed Tableau worksheet data unsupported; public summary reader returned PermissionDenied (403)", "unsupported")
        snapshot = copy.deepcopy(self._snapshot) if self._snapshot is not None else self._fetch()
        self._validate(snapshot)
        rows = [{k: c[k] for k in ("metric", "display_value", "unit")} for c in snapshot["cards"]]
        meta = metadata_for(target, platform="tableau_public", extraction_mode="visible_metric_cards",
            columns=["metric", "display_value", "unit"], workbook=self.workbook, view=self.view,
            source_config_id=self.config["id"], worksheets=sorted(snapshot["worksheets"]),
            source_update_date=snapshot.get("source_update_date"), publisher=snapshot.get("publisher"),
            is_complete=False, metric_cards_complete=True,
            unit=next(iter(set(self.config["visible_metrics"].values()))) if len(set(self.config["visible_metrics"].values())) == 1 else None,
            value_encoding="publisher_display_strings", data_semantics="publisher-authored dashboard text, not worksheet query results",
            extraction_scope="configured landing view; published metric cards only",
            filter_binding="not inferred: publisher-authored cards are not assumed to respond to worksheet filters",
            detailed_data_status="unsupported_permission_denied", metric_zone_ids={c["metric"]: c["zone_id"] for c in snapshot["cards"]})
        return [UnifiedRecord(self.source_type, self.workbook, target.chart_id, rows, meta, target.id).normalized()]
