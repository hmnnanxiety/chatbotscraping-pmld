"""IDMC source configuration; the generic orchestrator knows only adapters."""
import json
from pathlib import Path
from dashboards import DASHBOARDS
from extractors.superset import SupersetAdapter
from extractors.cctv import CCTVNativeAdapter
from extractors.tableau_looker import TableauLookerAdapter
from extractors.looker_studio import LookerStudioAdapter

def source_inventory(config):
    """Keep configured source identity independently of current portal visibility."""
    items = [{"source_type": "superset", "id": d["uuid"], "name": d["name"], "config": d}
             for d in DASHBOARDS] if config.get("superset", True) else []
    aliases = config.get("cctv_portal_aliases", {})
    if not isinstance(aliases, dict) or set(aliases) - set(config.get("cctv_groups", [])):
        raise ValueError("CCTV aliases must refer to configured location groups")
    for values in aliases.values():
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.startswith("cctv.") for v in values):
            raise ValueError("CCTV portal aliases must be lists of cctv.* sources")
    items.extend({"source_type": "cctv_native", "id": group, "name": group, "config": group,
                  "portal_sources": aliases.get(group, [])}
                 for group in config.get("cctv_groups", []))
    items.extend({"source_type": "tableau_looker", "id": s["id"], "name": s["name"], "config": s}
                 for s in config.get("web_sources", []))
    items.extend({"source_type": "looker_studio", "id": s["id"], "name": s["name"], "config": s}
                 for s in config.get("looker_sources", []))
    return items

def configured_adapters(config_path):
    from portal_scope import fetch_scope, ScopedAdapters
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    scope = fetch_scope(source_inventory(config))
    selected = []
    for item in scope.selected:
        if item["source_type"] == "superset":
            selected.append(SupersetAdapter(item["config"]))
        elif item["source_type"] == "cctv_native":
            selected.append(CCTVNativeAdapter([item["id"]]))
        elif item["source_type"] == "looker_studio":
            selected.append(LookerStudioAdapter(item["config"]))
        else:
            selected.append(TableauLookerAdapter([item["config"]]))
    return ScopedAdapters(selected, scope)
