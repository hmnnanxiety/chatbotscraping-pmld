"""IDMC source configuration; the generic orchestrator knows only adapters."""
import json
from pathlib import Path
from dashboards import DASHBOARDS
from extractors.superset import SupersetAdapter
from extractors.cctv import CCTVNativeAdapter
from extractors.tableau_looker import TableauLookerAdapter

def configured_adapters(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    selected = [SupersetAdapter(d) for d in DASHBOARDS] if config.get("superset", True) else []
    if config.get("cctv_groups"):
        selected.append(CCTVNativeAdapter(config["cctv_groups"]))
    selected.extend(TableauLookerAdapter([source]) for source in config.get("web_sources", []))
    return selected
