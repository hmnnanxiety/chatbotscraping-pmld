"""IDMC portal visibility and exact source mapping; adapters keep their identities."""
from __future__ import annotations
import copy
from dataclasses import dataclass
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from extractors.base import HTTPClient, retry_source

PORTAL = "https://idmc.jogjaprov.go.id"
MENU_URL = PORTAL + "/backend/api/v1/menus"
SITE_CONFIG_URL = PORTAL + "/backend/api/v1/config"
SCOPE_VERSION = 2
log = logging.getLogger("etl.portal")

def source_key(source):
    """Match transport identity, never human labels or substring UUID guesses."""
    if not isinstance(source, str) or not source:
        return None
    if source.startswith("superset:"):
        url, sep, uuid = source[len("superset:"):].partition("|")
        parsed = urlsplit(url)
        if not sep or not uuid or parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        return ("superset", url.rstrip("/"), uuid)
    if source.startswith("cctv."):
        group = source[len("cctv."):]
        return ("cctv_native", "cctv-" + group) if group else None
    parsed = urlsplit(source)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"public.tableau.com", "lookerstudio.google.com", "datastudio.google.com"}:
        return None
    # Remove display-only Tableau parameters, retaining dataset/filter parameters.
    presentation = {":showVizHome", ":language", ":display_count", ":origin", "publish"}
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if host != "public.tableau.com" or k not in presentation]
    url = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"),
                     urlencode(sorted(query)), ""))
    return ("tableau_looker", url)

def inventory_key(item):
    if item["source_type"] == "superset":
        return source_key("superset:https://dwh.jogjaprov.go.id|" + item["id"])
    if item["source_type"] == "cctv_native":
        return ("cctv_native", item["id"])
    return source_key(item["config"]["url"])

def record_key(record):
    if record.source_type == "superset":
        # target_id keeps UUID scope, including records whose metadata lacks UUID.
        parts = record.target_id.split(":")
        uuid = record.metadata.get("dashboard_uuid") or (parts[1] if len(parts) > 2 else "")
        return source_key("superset:https://dwh.jogjaprov.go.id|" + uuid)
    if record.source_type == "cctv_native":
        return ("cctv_native", record.dashboard_id)
    return source_key(record.metadata.get("source_url"))

def portal_keys(item):
    keys = {inventory_key(item)}
    keys.update(source_key(alias) for alias in item.get("portal_sources", []))
    keys.discard(None)
    return keys

def portal_entries(menus, site_config):
    if not isinstance(menus, list):
        raise ValueError("Portal menus must be a list; refusing unverified scope")
    entries, visible_menus, seen = [], [], set()
    def visit(nodes, ancestors=(), active=True):
        if not isinstance(nodes, list):
            raise ValueError("Portal menu children must be a list")
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node["name"].strip():
                raise ValueError("Portal menu name missing/invalid")
            if type(node.get("status")) not in (int, bool) or node["status"] not in (0, 1):
                raise ValueError("Portal menu status missing/invalid")
            if type(node.get("id")) not in (int, str) or not str(node["id"]):
                raise ValueError("Portal menu ID missing/invalid")
            identity = str(node["id"])
            if identity in seen:
                raise ValueError("Duplicate portal menu ID")
            seen.add(identity)
            visible = active and node["status"] == 1
            path = ancestors + (node["name"],)
            if visible and not ancestors:
                visible_menus.append(node["name"])
            if visible and node.get("source"):
                if not isinstance(node["source"], str):
                    raise ValueError("Invalid portal source")
                entries.append({"portal_menu_name": path[0], "portal_page_name": node["name"],
                                "portal_page_id": identity, "portal_path": list(path),
                                "source": node["source"]})
            visit(node.get("children", []), path, visible)
    visit(menus)
    if not isinstance(site_config, dict) or not isinstance(site_config.get("data"), list) or not site_config["data"]:
        raise ValueError("Portal site config missing/invalid; refusing unverified home scope")
    first = site_config["data"][0]
    if not isinstance(first, dict) or "dashboard_url" not in first:
        raise ValueError("Portal home dashboard_url missing")
    home = first["dashboard_url"]
    if home is not None and not isinstance(home, str):
        raise ValueError("Portal home dashboard_url invalid")
    if home:
        entries.append({"portal_menu_name": "Beranda", "portal_page_name": "Beranda",
                        "portal_page_id": "home", "portal_path": ["Beranda"], "source": home})
    entries.sort(key=lambda e: (tuple(e["portal_path"]), e["portal_page_id"], e["source"]))
    return entries, sorted(visible_menus)

@dataclass
class PortalScope:
    inventory: list
    entries: list
    visible_menu_names: list

    @classmethod
    def from_payloads(cls, inventory, menus, site_config):
        entries, names = portal_entries(menus, site_config)
        return cls(inventory, entries, names)

    def matches(self, item):
        keys = portal_keys(item)
        return [e for e in self.entries if source_key(e["source"]) in keys]

    @property
    def selected(self):
        return [item for item in self.inventory if self.matches(item)]

    def summary(self):
        selected = self.selected
        mapped_keys = {key for item in selected for key in portal_keys(item)}
        return {
            "status": "verified", "scope_version": SCOPE_VERSION,
            "menu_api_url": MENU_URL, "site_config_url": SITE_CONFIG_URL,
            "visible_menu_names": self.visible_menu_names,
            "visible_entries": self.entries,
            "mapped_sources": [{"source_type": item["source_type"], "source_id": item["id"],
                                "source_dashboard_name": item["name"], "portal_entries": self.matches(item)}
                               for item in selected],
            "excluded_sources": [{"source_type": item["source_type"], "source_id": item["id"],
                                  "source_dashboard_name": item["name"],
                                  "reason": "No active portal entry (including all ancestors) or home mapping"}
                                 for item in self.inventory if not self.matches(item)],
            "unmapped_visible_entries": [e for e in self.entries if source_key(e["source"]) not in mapped_keys],
        }

    def apply(self, records):
        """Gate previous AND new records; refresh names even on stale carry-forward."""
        allowed = {inventory_key(item): self.matches(item) for item in self.selected}
        result, excluded = {}, []
        for identity, record in sorted(records.items()):
            matches = allowed.get(record_key(record))
            if not matches:
                excluded.append(identity)
                continue
            record = copy.deepcopy(record)
            m = record.metadata
            m["source_dashboard_name"] = m["dashboard_name"]
            m["source_chart_name"] = m["chart_name"]
            first = matches[0]
            m.update({k: first[k] for k in ("portal_menu_name", "portal_page_name", "portal_page_id", "portal_path")})
            m.update(portal_visible=True, portal_entries=matches)
            result[identity] = record.normalized()
        return result, excluded

@retry_source
def fetch_scope(inventory, http=None):
    http = http or HTTPClient()
    menus = http.get(MENU_URL).json()
    config = http.get(SITE_CONFIG_URL).json()
    scope = PortalScope.from_payloads(inventory, menus, config)
    log.info("Portal scope: %d active menus, %d configured sources mapped, %d excluded, %d visible entries unmapped",
             len(scope.visible_menu_names), len(scope.selected),
             len(scope.inventory) - len(scope.selected), len(scope.summary()["unmapped_visible_entries"]))
    return scope

class ScopedAdapters(list):
    """List compatibility for callers, with one verified scope snapshot per run."""
    def __init__(self, adapters, scope):
        super().__init__(adapters)
        self.portal_scope = scope
