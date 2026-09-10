"""
dashboards.py

Registry of known Superset-backed dashboards on the DWH portal.

Key fact (confirmed by inspecting the embedded viewer's HTML directly):
Superset's /api/v1/dashboard/{id} endpoint only accepts the NUMERIC
dashboard id — never the UUID. There is no API call that resolves
UUID -> numeric id; the numeric id is only visible inside the bootstrap
JSON embedded in the HTML of https://dwh.jogjaprov.go.id/embedded/<uuid>
when that page is rendered. Until an automated HTML-scrape resolver is
built, numeric_id must be found manually per dashboard (DevTools ->
Network -> filter "embedded" -> inspect the raw HTML response for a
"dashboard_title"/"id" pair) and filled in below.

Dashboards with numeric_id = None are skipped by the ingestion pipeline
until their id is found.
"""

from __future__ import annotations

from typing import List, Optional, TypedDict


class DashboardConfig(TypedDict):
    name: str
    uuid: str
    numeric_id: Optional[int]
    referer: Optional[str]  # override for the guest-token request; None = use generic portal referer


DASHBOARDS: List[DashboardConfig] = [
    # --- Confirmed numeric ids (verified via bootstrap HTML) -------------
    {"name": "Dashboard Pembangunan", "uuid": "27b5d07e-111a-449a-84a2-efc3b6cf5fa1", "numeric_id": 14, "referer": None},
    {"name": "Dashboard Kependudukan", "uuid": "886ade99-a1b9-442b-94cd-8e2e9671c381", "numeric_id": 23, "referer": None},
    {
        "name": "Dashboard Simpeg (Kepegawaian)",
        "uuid": "b575b146-15c6-4521-906d-dcc5fa5bed1e",
        "numeric_id": 13,
        # Confirmed via real browser traffic — this dashboard's guest-token
        # request 422s without this specific referer path. Format is
        # {parent-menu-slug}/{child-menu-slug}/{base64(urlencode(source))}.
        "referer": (
            "https://idmc.jogjaprov.go.id/dashboard-kepegawaian/dashboard-simpeg/"
            "c3VwZXJzZXQlM0FodHRwcyUzQSUyRiUyRmR3aC5qb2dqYXByb3YuZ28uaWQlN0NiNTc1"
            "YjE0Ni0xNWM2LTQ1MjEtOTA2ZC1kY2M1ZmE1YmVkMWU="
        ),
    },
    {"name": "Pertanahan V2", "uuid": "d8c94ab7-ab24-405c-8baa-cf6b82a17f44", "numeric_id": 36, "referer": None},
    {"name": "Profil Tenaga Kesehatan", "uuid": "49bb9ba6-06b7-4525-9ff7-a78e384e6854", "numeric_id": 66, "referer": None},
    {"name": "Profil Rumah Sakit", "uuid": "86d8feac-474a-4d74-9928-aaaf034fd1f5", "numeric_id": 67, "referer": None},
    {"name": "Profil Puskesmas", "uuid": "f9c8313f-5f64-4517-9720-5ee7689a50f8", "numeric_id": 65, "referer": None},
    {"name": "Profil Fasilitas Puskesmas", "uuid": "c333627c-1721-49c7-a6f1-c6369751bd45", "numeric_id": 64, "referer": None},
    {"name": "UMKM Marketplace", "uuid": "6d816cf9-91b5-4711-9b77-6ee93aa7bcca", "numeric_id": 37, "referer": None},
    {"name": "Harga Pangan DIY", "uuid": "380d5b59-7fad-4d96-9bbc-c0e8c0309e41", "numeric_id": 69, "referer": None},

    # --- Known UUID, numeric id not yet found -----------------------------
    {"name": "Dashboard CCTV (Surveillance)", "uuid": "e5f3e11f-f456-4729-aa68-53e53c1e459e", "numeric_id": None, "referer": None},
]


def known_dashboards() -> List[DashboardConfig]:
    """Only dashboards whose numeric_id has been found."""
    return [d for d in DASHBOARDS if d["numeric_id"] is not None]


def pending_dashboards() -> List[DashboardConfig]:
    """Dashboards still needing their numeric_id found manually."""
    return [d for d in DASHBOARDS if d["numeric_id"] is None]
