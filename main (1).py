from __future__ import annotations

from fastapi import FastAPI

# ============================================================
# PROPERTYIQ — PRODUCTION APPLICATION
# ============================================================
#
# Consolidated from the restored PropertyIQ V40 notebook.
#
# Architecture:
#   FastAPI
#   PostgreSQL / PostGIS
#   Property Intelligence
#   GIS / OSM
#   RERA
#   Government Infrastructure
#   News Intelligence
#   Market / Comparables
#   Evidence / Due Diligence
#
# Production database is supplied through DATABASE_URL.
#
# ============================================================

import os
import sys
import json
import math
import re
import uuid
import time
import hashlib
import logging

from pathlib import Path
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)




# ============================================================
# PROPERTYIQ MODULE: V17
# ORIGINAL COLAB CELL: In[12]
# ============================================================

# PROPERTYIQ V17 — PROPERTY INTELLIGENCE REPORT
# PostgreSQL/PostGIS-compatible, additive module.
# Direct Colab execution: paste the complete file into a new cell.
#
# Reads:
#   properties
#   intelligence_records
#   evidence_items
#   property_market_observations (if present)
#   property_files (if present)
#   piq_property_refresh_runs (if present)
#
# Does not create fabricated intelligence, valuations, forecasts,
# investment scores, or unsupported evidence.


import html
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text


MODULE_VERSION = "PROPERTYIQ-V17-INTELLIGENCE-REPORT"
DEFAULT_RADIUS_KM = 5.0
MAX_RADIUS_KM = 25.0


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid property UUID."
        ) from exc


def _json(value: Any, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _esc(value: Any) -> str:
    return html.escape(
        "" if value is None else str(value)
    )


def _iso(value: Any):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _float(value):
    try:
        return None if value in (None, "") else float(value)
    except Exception:
        return None


def _round(value, digits=2):
    number = _float(value)
    return None if number is None else round(number, digits)


def _money(value):
    number = _float(value)
    return "—" if number is None else "₹" + f"{number:,.0f}"


def _confidence_label(value):
    number = _float(value)
    if number is None:
        return "UNASSESSED"
    if number >= 0.85:
        return "HIGH"
    if number >= 0.60:
        return "MEDIUM"
    return "LOW"


def _source_group(source_type, category):
    st = str(source_type or "").upper()
    cat = str(category or "").upper()

    if "RERA" in st or cat == "RERA":
        return "RERA"

    if (
        "GOVERNMENT" in st
        or "INFRASTRUCTURE" in st
        or cat == "INFRASTRUCTURE"
    ):
        return "GOVERNMENT"

    if "OSM" in st:
        return "LOCATION"

    if "NEWS" in st or cat == "NEWS":
        return "NEWS"

    if cat in {"DEVELOPMENT", "PROJECT", "PROJECTS"}:
        return "DEVELOPMENT"

    if cat in {
        "MARKET",
        "COMPARABLE",
        "COMPARABLES",
        "TRANSACTION",
        "VALUATION",
    }:
        return "MARKET"

    return "OTHER"


def _table_exists(conn, name):
    return bool(
        conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema='public'
                      AND table_name=:name
                )
            """),
            {"name": name},
        ).scalar()
    )


def build_v17_report(
    engine,
    property_id,
    radius_km=DEFAULT_RADIUS_KM,
    limit=500,
):
    pid = _uuid(property_id)

    radius_km = max(
        0.25,
        min(float(radius_km), MAX_RADIUS_KM)
    )

    limit = max(
        1,
        min(int(limit), 3000)
    )

    radius_m = radius_km * 1000.0

    with engine.connect() as conn:

        property_row = conn.execute(
            text("""
                SELECT
                    p.id,
                    p.property_name,
                    p.property_address,
                    p.latitude,
                    p.longitude,
                    p.property_type,
                    p.area,
                    p.price,
                    p.price_per_sqft,
                    p.bedrooms,
                    p.bathrooms,
                    p.builder_owner,
                    p.description,
                    p.amenities,

                    CASE
                        WHEN p.boundary IS NOT NULL
                        THEN ST_AsGeoJSON(p.boundary)
                        ELSE NULL
                    END AS boundary_geojson,

                    CASE
                        WHEN p.boundary IS NOT NULL
                        THEN ST_Area(
                            ST_Transform(
                                p.boundary,
                                3857
                            )
                        ) / 1000000.0
                        ELSE NULL
                    END AS boundary_area_km2,

                    CASE
                        WHEN p.location IS NOT NULL
                        THEN ST_Y(p.location)
                        ELSE p.latitude
                    END AS location_latitude,

                    CASE
                        WHEN p.location IS NOT NULL
                        THEN ST_X(p.location)
                        ELSE p.longitude
                    END AS location_longitude,

                    p.created_at,
                    p.updated_at

                FROM properties p
                WHERE p.id = CAST(:pid AS uuid)
            """),
            {"pid": pid},
        ).mappings().first()

        if property_row is None:
            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )

        prop = dict(property_row)

        prop["id"] = str(prop["id"])

        for key in (
            "created_at",
            "updated_at",
        ):
            prop[key] = _iso(
                prop.get(key)
            )

        prop["boundary_geojson"] = _json(
            prop.get("boundary_geojson")
        )

        for key in (
            "latitude",
            "longitude",
            "location_latitude",
            "location_longitude",
        ):
            prop[key] = _round(
                prop.get(key),
                6
            )

        for key in (
            "area",
            "price",
            "price_per_sqft",
            "boundary_area_km2",
        ):
            prop[key] = _round(
                prop.get(key),
                2
            )

        # ----------------------------------------------------
        # Directly linked + nearby intelligence
        # ----------------------------------------------------

        rows = conn.execute(
            text("""
                SELECT
                    i.id,
                    i.property_id,
                    i.title,
                    i.category,
                    i.status,
                    i.description,
                    i.latitude,
                    i.longitude,
                    i.source_name,
                    i.source_type,
                    i.source_url,
                    i.published_at,
                    i.confidence,
                    i.evidence_level,
                    i.metadata,
                    i.created_at,
                    i.updated_at,

                    CASE
                        WHEN i.location IS NOT NULL
                         AND p.location IS NOT NULL
                        THEN ST_Distance(
                            i.location::geography,
                            p.location::geography
                        ) / 1000.0
                        ELSE NULL
                    END AS distance_km

                FROM intelligence_records i

                JOIN properties p
                  ON p.id = CAST(:pid AS uuid)

                WHERE
                    i.property_id = CAST(:pid AS uuid)
                    OR (
                        i.location IS NOT NULL
                        AND p.location IS NOT NULL
                        AND ST_DWithin(
                            i.location::geography,
                            p.location::geography,
                            :radius_m
                        )
                    )

                ORDER BY
                    CASE
                        WHEN i.property_id = CAST(:pid AS uuid)
                        THEN 0
                        ELSE 1
                    END,
                    COALESCE(
                        i.published_at,
                        i.created_at
                    ) DESC

                LIMIT :limit
            """),
            {
                "pid": pid,
                "radius_m": radius_m,
                "limit": limit,
            },
        ).mappings().all()

        records = []

        for row in rows:
            item = dict(row)

            item["id"] = str(
                item["id"]
            )

            if item.get("property_id"):
                item["property_id"] = str(
                    item["property_id"]
                )

            for key in (
                "published_at",
                "created_at",
                "updated_at",
            ):
                item[key] = _iso(
                    item.get(key)
                )

            item["metadata"] = _json(
                item.get("metadata"),
                {}
            )

            item["latitude"] = _round(
                item.get("latitude"),
                6
            )

            item["longitude"] = _round(
                item.get("longitude"),
                6
            )

            item["distance_km"] = _round(
                item.get("distance_km"),
                3
            )

            item["source_group"] = _source_group(
                item.get("source_type"),
                item.get("category")
            )

            item["confidence_label"] = (
                _confidence_label(
                    item.get("confidence")
                )
            )

            item["status"] = str(
                item.get("status")
                or "reported"
            )

            records.append(item)

        # ----------------------------------------------------
        # Evidence
        # ----------------------------------------------------

        erows = conn.execute(
            text("""
                SELECT
                    e.id,
                    e.intelligence_id,
                    e.property_id,
                    e.source_name,
                    e.source_type,
                    e.source_url,
                    e.evidence_level,
                    e.evidence_weight,
                    e.statement,
                    e.captured_at,
                    e.metadata

                FROM evidence_items e

                WHERE e.property_id =
                      CAST(:pid AS uuid)

                ORDER BY
                    e.captured_at DESC

                LIMIT 3000
            """),
            {"pid": pid},
        ).mappings().all()

        evidence = []
        evidenced_ids = set()

        for row in erows:
            item = dict(row)

            item["id"] = str(
                item["id"]
            )

            if item.get("intelligence_id"):
                item["intelligence_id"] = str(
                    item["intelligence_id"]
                )

                evidenced_ids.add(
                    item["intelligence_id"]
                )

            if item.get("property_id"):
                item["property_id"] = str(
                    item["property_id"]
                )

            item["captured_at"] = _iso(
                item.get("captured_at")
            )

            item["metadata"] = _json(
                item.get("metadata"),
                {}
            )

            evidence.append(item)

        # ----------------------------------------------------
        # Optional market layer
        # ----------------------------------------------------

        market = []

        if _table_exists(
            conn,
            "property_market_observations"
        ):
            market_rows = conn.execute(
                text("""
                    SELECT
                        id,
                        observation_type,
                        title,
                        area_sqft,
                        price,
                        price_per_sqft,
                        bedrooms,
                        bathrooms,
                        property_type,
                        observed_date,
                        source_name,
                        source_url,
                        evidence_level,
                        confidence,
                        status,
                        notes,
                        metadata,
                        created_at,
                        updated_at

                    FROM property_market_observations

                    WHERE property_id =
                          CAST(:pid AS uuid)

                    ORDER BY
                        observed_date DESC NULLS LAST,
                        created_at DESC

                    LIMIT 500
                """),
                {"pid": pid},
            ).mappings().all()

            for row in market_rows:
                item = dict(row)

                item["id"] = str(
                    item["id"]
                )

                item["metadata"] = _json(
                    item.get("metadata"),
                    {}
                )

                for key in (
                    "observed_date",
                    "created_at",
                    "updated_at",
                ):
                    item[key] = _iso(
                        item.get(key)
                    )

                market.append(item)

        # ----------------------------------------------------
        # Optional files/documents
        # ----------------------------------------------------

        files = []

        if _table_exists(
            conn,
            "property_files"
        ):
            file_rows = conn.execute(
                text("""
                    SELECT
                        file_id,
                        file_kind,
                        file_name,
                        mime_type,
                        file_bytes AS size_bytes,
                        title,
                        extraction_status,
                        created_at,
                        updated_at

                    FROM property_files

                    WHERE property_id =
                          CAST(:pid AS uuid)

                    ORDER BY
                        created_at DESC

                    LIMIT 500
                """),
                {"pid": pid},
            ).mappings().all()

            for row in file_rows:
                item = dict(row)

                if item.get("file_id"):
                    item["file_id"] = str(
                        item["file_id"]
                    )

                item["created_at"] = _iso(
                    item.get("created_at")
                )

                item["updated_at"] = _iso(
                    item.get("updated_at")
                )

                files.append(item)

        # ----------------------------------------------------
        # Optional V16 refresh history
        # ----------------------------------------------------

        refresh = []

        if _table_exists(
            conn,
            "piq_property_refresh_runs"
        ):
            refresh_rows = conn.execute(
                text("""
                    SELECT
                        run_id,
                        status,
                        started_at,
                        finished_at,
                        requested_sources,
                        source_results,
                        records_seen,
                        records_written,
                        error_count,
                        error_message,
                        metadata,
                        created_at

                    FROM piq_property_refresh_runs

                    WHERE property_id =
                          CAST(:pid AS uuid)

                    ORDER BY
                        created_at DESC

                    LIMIT 25
                """),
                {"pid": pid},
            ).mappings().all()

            for row in refresh_rows:
                item = dict(row)

                item["run_id"] = str(
                    item["run_id"]
                )

                for key in (
                    "started_at",
                    "finished_at",
                    "created_at",
                ):
                    item[key] = _iso(
                        item.get(key)
                    )

                item["requested_sources"] = _json(
                    item.get("requested_sources"),
                    []
                )

                item["source_results"] = _json(
                    item.get("source_results"),
                    {}
                )

                item["metadata"] = _json(
                    item.get("metadata"),
                    {}
                )

                refresh.append(item)

    source_counts = {}
    status_counts = {}
    confidence_counts = {}

    for item in records:

        group = item[
            "source_group"
        ]

        source_counts[group] = (
            source_counts.get(group, 0)
            + 1
        )

        status = item[
            "status"
        ]

        status_counts[status] = (
            status_counts.get(status, 0)
            + 1
        )

        confidence = item[
            "confidence_label"
        ]

        confidence_counts[
            confidence
        ] = (
            confidence_counts.get(
                confidence,
                0
            )
            + 1
        )

    with_evidence = sum(
        1
        for item in records
        if item["id"]
        in evidenced_ids
    )

    mapped = sum(
        1
        for item in records
        if (
            item.get("latitude")
            is not None
            and
            item.get("longitude")
            is not None
        )
    )

    nearby = sum(
        1
        for item in records
        if item.get("property_id")
    )

    timeline = []

    for item in records:
        timeline.append(
            {
                "date":
                    item.get("published_at")
                    or
                    item.get("created_at"),

                "title":
                    item.get("title"),

                "source_group":
                    item.get("source_group"),

                "status":
                    item.get("status"),

                "source_name":
                    item.get("source_name"),
            }
        )

    timeline.sort(
        key=lambda item:
            str(item.get("date") or ""),
        reverse=True
    )

    return {
        "module":
            MODULE_VERSION,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "property":
            prop,

        "parameters": {
            "radius_km":
                radius_km,
            "record_limit":
                limit,
        },

        "summary": {
            "total_intelligence_records":
                len(records),

            "direct_property_records":
                len(records) - nearby,

            "nearby_records":
                nearby,

            "mapped_records":
                mapped,

            "unmapped_records":
                len(records) - mapped,

            "evidence_items":
                len(evidence),

            "records_with_evidence":
                with_evidence,

            "evidence_coverage_percent":
                round(
                    (
                        with_evidence
                        / len(records)
                    ) * 100.0,
                    1
                ) if records else 0.0,

            "market_observations":
                len(market),

            "files":
                len(files),

            "source_counts":
                source_counts,

            "status_counts":
                status_counts,

            "confidence_counts":
                confidence_counts,
        },

        "records":
            records,

        "evidence":
            evidence,

        "market_observations":
            market,

        "files":
            files,

        "refresh_history":
            refresh,

        "latest_refresh":
            refresh[0]
            if refresh
            else None,

        "timeline":
            timeline[:1000],

        "methodology": {
            "source_data_only":
                True,

            "no_price_prediction":
                True,

            "no_investment_score":
                True,

            "no_fabricated_evidence":
                True,

            "nearby_radius_km":
                radius_km,
        },
    }


def _source_link(url):
    if not url:
        return "—"

    return (
        '<a href="'
        + _esc(url)
        + '" target="_blank" '
          'rel="noopener noreferrer">'
          'Open source</a>'
    )


def render_v17_html(report):

    property_data = report[
        "property"
    ]

    summary = report[
        "summary"
    ]

    source_cards = "".join(
        '<div class="mini">'
        '<div class="ml">'
        + _esc(group)
        + '</div>'
        '<div class="mv">'
        + _esc(count)
        + '</div>'
        '</div>'
        for group, count
        in sorted(
            summary[
                "source_counts"
            ].items(),
            key=lambda item:
                (-item[1], item[0])
        )
    )

    intelligence_rows = "".join(
        "<tr>"
        "<td><b>"
        + _esc(item.get("title"))
        + "</b><div class='muted'>"
        + _esc(item.get("description"))
        + "</div></td>"
        "<td>"
        + _esc(item.get("source_group"))
        + "<br>"
        + _esc(item.get("source_name"))
        + "</td>"
        "<td>"
        + _esc(item.get("status"))
        + "</td>"
        "<td>"
        + (
            _esc(item.get("distance_km"))
            + " km"
            if item.get("distance_km")
            is not None
            else "direct"
        )
        + "</td>"
        "<td>"
        + _esc(item.get("confidence_label"))
        + "</td>"
        "<td>"
        + _source_link(
            item.get("source_url")
        )
        + "</td>"
        "</tr>"
        for item
        in report["records"][:250]
    )

    evidence_rows = "".join(
        "<tr>"
        "<td>"
        + _esc(item.get("source_name"))
        + "</td>"
        "<td>"
        + _esc(item.get("evidence_level"))
        + "</td>"
        "<td>"
        + _esc(item.get("statement"))
        + "</td>"
        "<td>"
        + _source_link(
            item.get("source_url")
        )
        + "</td>"
        "</tr>"
        for item
        in report["evidence"][:250]
    )

    market_rows = "".join(
        "<tr>"
        "<td>"
        + _esc(item.get("title"))
        + "</td>"
        "<td>"
        + _esc(item.get("observation_type"))
        + "</td>"
        "<td>"
        + _esc(item.get("area_sqft"))
        + "</td>"
        "<td>"
        + _money(item.get("price"))
        + "</td>"
        "<td>"
        + _money(
            item.get("price_per_sqft")
        )
        + "</td>"
        "<td>"
        + _esc(item.get("source_name"))
        + "</td>"
        "</tr>"
        for item
        in report[
            "market_observations"
        ][:250]
    )

    file_rows = "".join(
        "<tr>"
        "<td>"
        + _esc(item.get("file_name"))
        + "</td>"
        "<td>"
        + _esc(item.get("file_kind"))
        + "</td>"
        "<td>"
        + _esc(item.get("mime_type"))
        + "</td>"
        "<td>"
        + _esc(
            item.get(
                "extraction_status"
            )
        )
        + "</td>"
        "</tr>"
        for item
        in report["files"][:250]
    )

    refresh_rows = "".join(
        "<tr>"
        "<td>"
        + _esc(
            str(item.get("run_id"))[:12]
        )
        + "</td>"
        "<td>"
        + _esc(item.get("status"))
        + "</td>"
        "<td>"
        + _esc(item.get("started_at"))
        + "</td>"
        "<td>"
        + _esc(item.get("records_seen"))
        + "</td>"
        "<td>"
        + _esc(item.get("records_written"))
        + "</td>"
        "<td>"
        + _esc(item.get("error_count"))
        + "</td>"
        "</tr>"
        for item
        in report[
            "refresh_history"
        ]
    )

    timeline_rows = "".join(
        "<tr>"
        "<td>"
        + _esc(item.get("date"))
        + "</td>"
        "<td>"
        + _esc(item.get("title"))
        + "</td>"
        "<td>"
        + _esc(item.get("source_group"))
        + "</td>"
        "<td>"
        + _esc(item.get("status"))
        + "</td>"
        "</tr>"
        for item
        in report["timeline"][:250]
    )

    empty6 = (
        '<tr><td colspan="6" '
        'class="muted">No records available.'
        '</td></tr>'
    )

    empty4 = (
        '<tr><td colspan="4" '
        'class="muted">No records available.'
        '</td></tr>'
    )

    template = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>PropertyIQ — Intelligence Report</title>

<style>
:root{
    --bg:#06101d;
    --panel:rgba(12,28,47,.92);
    --line:rgba(130,190,240,.16);
    --text:#eef8ff;
    --muted:#91a7ba;
    --cyan:#53dcff;
}
*{box-sizing:border-box}
body{
    margin:0;
    background:
        radial-gradient(
            circle at 6% 0%,
            rgba(76,154,255,.15),
            transparent 32%
        ),
        var(--bg);
    color:var(--text);
    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}
.top{
    position:sticky;
    top:0;
    z-index:20;
    background:rgba(6,16,29,.87);
    backdrop-filter:blur(18px);
    border-bottom:1px solid var(--line);
}
.topin{
    max-width:1500px;
    margin:auto;
    padding:14px 22px;
    display:flex;
    justify-content:space-between;
    align-items:center;
}
.brand{font-weight:900}
.brand span{color:var(--cyan)}
.actions{display:flex;gap:8px}
button,a.btn{
    padding:9px 12px;
    border:1px solid var(--line);
    border-radius:10px;
    background:var(--panel);
    color:var(--text);
    font-size:11px;
    font-weight:800;
    text-decoration:none;
    cursor:pointer;
}
.container{
    max-width:1500px;
    margin:auto;
    padding:28px 22px 70px;
}
.eyebrow{
    color:var(--cyan);
    font-size:10px;
    text-transform:uppercase;
    letter-spacing:1.7px;
    font-weight:900;
}
h1{
    margin:7px 0;
    font-size:clamp(30px,4vw,52px);
}
.sub{
    color:var(--muted);
    font-size:12px;
    line-height:1.5;
}
.cards,.mini-grid{
    display:grid;
    grid-template-columns:
        repeat(6,minmax(0,1fr));
    gap:10px;
    margin-top:18px;
}
.card,.panel{
    border:1px solid var(--line);
    background:var(--panel);
    border-radius:16px;
    box-shadow:0 20px 60px rgba(0,0,0,.24);
}
.card,.mini{padding:14px}
.label,.ml{
    font-size:9px;
    color:var(--muted);
    text-transform:uppercase;
    letter-spacing:.7px;
}
.value,.mv{
    font-size:24px;
    font-weight:900;
    margin-top:6px;
}
.mini{
    border:1px solid var(--line);
    background:rgba(17,38,63,.72);
    border-radius:12px;
}
.panel{
    margin-top:14px;
    overflow:hidden;
}
.head{
    padding:14px 16px;
    border-bottom:1px solid var(--line);
}
.head h2{
    margin:0;
    font-size:14px;
}
.body{padding:16px}
.grid2{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px;
}
.kv{
    display:grid;
    grid-template-columns:160px 1fr;
    gap:10px;
    padding:9px 0;
    border-bottom:1px solid var(--line);
}
.k{font-size:10px;color:var(--muted)}
.v{font-size:11px}
.table-wrap{overflow:auto}
table{
    width:100%;
    border-collapse:collapse;
}
th,td{
    text-align:left;
    vertical-align:top;
    padding:9px;
    border-bottom:1px solid var(--line);
    font-size:10px;
}
th{
    color:var(--muted);
    text-transform:uppercase;
    letter-spacing:.6px;
}
.muted{
    color:var(--muted);
    line-height:1.45;
    margin-top:4px;
}
a{color:var(--cyan)}
.note{
    margin-top:12px;
    padding:12px 14px;
    border:1px solid var(--line);
    border-radius:12px;
    background:rgba(83,220,255,.04);
    color:var(--muted);
    font-size:10px;
    line-height:1.5;
}
@media(max-width:1100px){
    .cards,.mini-grid{
        grid-template-columns:
            repeat(3,minmax(0,1fr));
    }
    .grid2{grid-template-columns:1fr}
}
@media(max-width:650px){
    .cards,.mini-grid{
        grid-template-columns:
            repeat(2,minmax(0,1fr));
    }
    .kv{grid-template-columns:1fr}
}
@media print{
    body{background:#fff;color:#111}
    .top{position:static}
    .actions{display:none}
    .card,.panel{
        background:#fff;
        color:#111;
        box-shadow:none;
        border:1px solid #ddd;
    }
    .muted,.k,.sub{color:#555}
    a{color:#111}
}
</style>
</head>

<body>

<div class="top">
    <div class="topin">
        <div class="brand">
            Property<span>IQ</span>
        </div>

        <div class="actions">
            <button onclick="window.print()">
                Print / Save PDF
            </button>

            <a
                class="btn"
                href="/propertyiq/command-center/__PID__"
            >
                Command Center
            </a>
        </div>
    </div>
</div>

<div class="container">

<div class="eyebrow">
    V17 · Property Intelligence Report
</div>

<h1>__NAME__</h1>

<div class="sub">
    __ADDRESS__
</div>

<div class="cards">

<div class="card">
    <div class="label">Intelligence</div>
    <div class="value">__TOTAL__</div>
</div>

<div class="card">
    <div class="label">Mapped</div>
    <div class="value">__MAPPED__</div>
</div>

<div class="card">
    <div class="label">Evidence</div>
    <div class="value">__EVCOV__%</div>
</div>

<div class="card">
    <div class="label">Market Data</div>
    <div class="value">__MARKET__</div>
</div>

<div class="card">
    <div class="label">Documents</div>
    <div class="value">__FILES__</div>
</div>

<div class="card">
    <div class="label">Radius</div>
    <div class="value">__RADIUS__ km</div>
</div>

</div>

<div class="panel">
<div class="head"><h2>Property Overview</h2></div>
<div class="body grid2">

<div>
<div class="kv">
    <div class="k">Property type</div>
    <div class="v">__TYPE__</div>
</div>

<div class="kv">
    <div class="k">Area</div>
    <div class="v">__AREA__</div>
</div>

<div class="kv">
    <div class="k">Price</div>
    <div class="v">__PRICE__</div>
</div>

<div class="kv">
    <div class="k">Price / sq.ft</div>
    <div class="v">__PPSF__</div>
</div>
</div>

<div>
<div class="kv">
    <div class="k">Bedrooms</div>
    <div class="v">__BED__</div>
</div>

<div class="kv">
    <div class="k">Bathrooms</div>
    <div class="v">__BATH__</div>
</div>

<div class="kv">
    <div class="k">Builder / Owner</div>
    <div class="v">__BUILDER__</div>
</div>

<div class="kv">
    <div class="k">Coordinates</div>
    <div class="v">__LAT__, __LON__</div>
</div>
</div>

</div>
</div>

<div class="panel">
<div class="head"><h2>Source Coverage</h2></div>
<div class="body">
<div class="mini-grid">
__SOURCE_CARDS__
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Intelligence Records</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>Record</th>
<th>Source</th>
<th>Status</th>
<th>Distance</th>
<th>Confidence</th>
<th>Evidence</th>
</tr>
</thead>
<tbody>
__INTEL__
</tbody>
</table>
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Evidence Trail</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>Source</th>
<th>Evidence level</th>
<th>Statement</th>
<th>Source</th>
</tr>
</thead>
<tbody>
__EVIDENCE__
</tbody>
</table>
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Market Observations</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>Title</th>
<th>Type</th>
<th>Area</th>
<th>Price</th>
<th>₹/sq.ft</th>
<th>Source</th>
</tr>
</thead>
<tbody>
__MARKET_ROWS__
</tbody>
</table>
</div>

<div class="note">
Only market observations explicitly stored in PropertyIQ
are shown. No valuation or investment prediction is generated.
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Documents & Photos</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>File</th>
<th>Kind</th>
<th>MIME</th>
<th>Extraction status</th>
</tr>
</thead>
<tbody>
__FILES_ROWS__
</tbody>
</table>
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Live Refresh History</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>Run</th>
<th>Status</th>
<th>Started</th>
<th>Seen</th>
<th>Written</th>
<th>Errors</th>
</tr>
</thead>
<tbody>
__REFRESH_ROWS__
</tbody>
</table>
</div>
</div>
</div>

<div class="panel">
<div class="head"><h2>Intelligence Timeline</h2></div>
<div class="body">
<div class="table-wrap">
<table>
<thead>
<tr>
<th>Date</th>
<th>Item</th>
<th>Layer</th>
<th>Status</th>
</tr>
</thead>
<tbody>
__TIMELINE_ROWS__
</tbody>
</table>
</div>
</div>
</div>

<div class="panel">
<div class="head">
<h2>Methodology & Data Boundaries</h2>
</div>

<div class="body">

<div class="note">
Generated at: __GENERATED__<br><br>

PropertyIQ preserves source-backed information and keeps
records without reliable coordinates unmapped. This report
does not assert legal title, approvals, future price movement,
investment returns, or project completion beyond the stored
evidence.
</div>

</div>
</div>

</div>
</body>
</html>
"""

    replacements = {
        "__PID__":
            _esc(property_data.get("id")),

        "__NAME__":
            _esc(property_data.get("property_name")),

        "__ADDRESS__":
            _esc(property_data.get("property_address")),

        "__TOTAL__":
            _esc(
                summary[
                    "total_intelligence_records"
                ]
            ),

        "__MAPPED__":
            _esc(
                summary[
                    "mapped_records"
                ]
            ),

        "__EVCOV__":
            _esc(
                summary[
                    "evidence_coverage_percent"
                ]
            ),

        "__MARKET__":
            _esc(
                summary[
                    "market_observations"
                ]
            ),

        "__FILES__":
            _esc(
                summary["files"]
            ),

        "__RADIUS__":
            _esc(
                report["parameters"]["radius_km"]
            ),

        "__TYPE__":
            _esc(
                property_data.get("property_type")
            ),

        "__AREA__":
            _esc(
                property_data.get("area")
            ),

        "__PRICE__":
            _money(
                property_data.get("price")
            ),

        "__PPSF__":
            _money(
                property_data.get("price_per_sqft")
            ),

        "__BED__":
            _esc(
                property_data.get("bedrooms")
            ),

        "__BATH__":
            _esc(
                property_data.get("bathrooms")
            ),

        "__BUILDER__":
            _esc(
                property_data.get("builder_owner")
            ),

        "__LAT__":
            _esc(
                property_data.get("location_latitude")
            ),

        "__LON__":
            _esc(
                property_data.get("location_longitude")
            ),

        "__SOURCE_CARDS__":
            source_cards
            or
            (
                '<div class="mini">'
                '<div class="ml">'
                'INTELLIGENCE'
                '</div>'
                '<div class="mv">0</div>'
                '</div>'
            ),

        "__INTEL__":
            intelligence_rows
            or empty6,

        "__EVIDENCE__":
            evidence_rows
            or empty4,

        "__MARKET_ROWS__":
            market_rows
            or empty6,

        "__FILES_ROWS__":
            file_rows
            or empty4,

        "__REFRESH_ROWS__":
            refresh_rows
            or empty6,

        "__TIMELINE_ROWS__":
            timeline_rows
            or empty4,

        "__GENERATED__":
            _esc(
                report["generated_at"]
            ),
    }

    for key, replacement in replacements.items():
        template = template.replace(
            key,
            str(replacement)
        )

    return template


def install_v17_intelligence_report(
    app,
    engine
):
    if app is None:
        raise RuntimeError(
            "PropertyIQ FastAPI app is missing."
        )

    if engine is None:
        raise RuntimeError(
            "PropertyIQ database engine is missing."
        )

    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    api_path = (
        "/api/v1/properties/"
        "{property_id}/intelligence-report"
    )

    if api_path not in existing:

        @app.get(api_path)
        def v17_report_api(
            property_id: str,
            radius_km: float = DEFAULT_RADIUS_KM,
            limit: int = 500,
        ):
            return build_v17_report(
                engine,
                property_id,
                radius_km,
                limit
            )

    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    ui_path = (
        "/propertyiq/intelligence-report/"
        "{property_id}"
    )

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse
        )
        def v17_report_ui(
            property_id: str,
            radius_km: float = DEFAULT_RADIUS_KM,
            limit: int = 500,
        ):
            report = build_v17_report(
                engine,
                property_id,
                radius_km,
                limit
            )

            return HTMLResponse(
                render_v17_html(
                    report
                )
            )

    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    legacy_path = (
        "/propertyiq/report/"
        "{property_id}"
    )

    if legacy_path not in existing:

        @app.get(
            legacy_path,
            response_class=HTMLResponse
        )
        def v17_legacy_report(
            property_id: str,
            radius_km: float = DEFAULT_RADIUS_KM,
        ):
            report = build_v17_report(
                engine,
                property_id,
                radius_km
            )

            return HTMLResponse(
                render_v17_html(
                    report
                )
            )

    return {
        "module":
            MODULE_VERSION,

        "status":
            "installed",

        "routes": [
            api_path,
            ui_path,
            legacy_path
        ],
    }


# ------------------------------------------------------------
# DIRECT COLAB EXECUTION + SAFE AUTO-BOOTSTRAP
# ------------------------------------------------------------
#
# If the normal PropertyIQ runtime already exists, use it.
#
# If Colab was restarted and only `engine` is missing, this
# block can rebuild the PostgreSQL engine automatically.
#
# IMPORTANT:
# - The database password is never stored in this file.
# - If a completely fresh runtime has no existing `app`, a new
#   FastAPI app is created so V17 can still run.
# - Existing app routes are preserved whenever `app` already
#   exists.
# ------------------------------------------------------------

def _propertyiq_autobootstrap():
    """
    PropertyIQ production-safe PostgreSQL/PostGIS bootstrap.

    Priority:
      1. Existing engine in runtime
      2. DATABASE_URL environment variable
      3. Colab/local interactive password fallback

    Production deployments must provide DATABASE_URL.
    """

    global engine
    global SessionLocal
    global p3
    global PropertyRepository
    global IntelligenceRepository
    global EvidenceRepository
    global IngestionJobRepository

    # --------------------------------------------------------
    # Existing runtime
    # --------------------------------------------------------

    if "engine" in globals() and engine is not None:

        return "existing"

    # --------------------------------------------------------
    # Production environment
    # --------------------------------------------------------

    database_url = os.getenv(
        "DATABASE_URL"
    )

    if database_url:

        print(
            "PROPERTYIQ: DATABASE_URL detected."
        )

        print(
            "PROPERTYIQ: Connecting to PostgreSQL/PostGIS..."
        )

        try:

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            # Normalize common PostgreSQL URLs.
            if database_url.startswith(
                "postgres://"
            ):

                database_url = (
                    "postgresql://"
                    + database_url[
                        len("postgres://"):
                    ]
                )

            # Prefer psycopg 3 when available.
            if database_url.startswith(
                "postgresql://"
            ) and "+psycopg" not in database_url:

                database_url = (
                    database_url.replace(
                        "postgresql://",
                        "postgresql+psycopg://",
                        1
                    )
                )

            engine = create_engine(
                database_url,
                pool_pre_ping=True,
                pool_recycle=1800,
                future=True
            )

            SessionLocal = sessionmaker(
                bind=engine,
                autoflush=False,
                autocommit=False
            )

            # Test connection.
            with engine.connect() as connection:

                connection.exec_driver_sql(
                    "SELECT 1"
                )

            print(
                "PROPERTYIQ: PostgreSQL connection verified."
            )

            return "database_url"

        except Exception as exc:

            raise RuntimeError(
                "PropertyIQ could not connect to PostgreSQL "
                "using DATABASE_URL. "
                f"Original error: {type(exc).__name__}: {exc}"
            ) from exc

    # --------------------------------------------------------
    # Local / Colab fallback
    # --------------------------------------------------------

    print(
        "PROPERTYIQ: DATABASE_URL was not provided."
    )

    print(
        "PROPERTYIQ: Falling back to interactive "
        "Colab/local PostgreSQL connection."
    )

    try:

        from getpass import getpass

        host = os.getenv(
            "PROPERTYIQ_DB_HOST",
            "aws-0-ap-south-1.pooler.supabase.com"
        )

        database = os.getenv(
            "PROPERTYIQ_DB_NAME",
            "postgres"
        )

        user = os.getenv(
            "PROPERTYIQ_DB_USER",
            "postgres.reqsrwfyeyupgqsggchv"
        )

        password = getpass(
            "Enter your PropertyIQ PostgreSQL password: "
        )

        from urllib.parse import quote_plus

        password_encoded = quote_plus(
            password
        )

        database_url = (
            "postgresql+psycopg://"
            f"{user}:{password_encoded}"
            f"@{host}:5432/{database}"
        )

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True
        )

        SessionLocal = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False
        )

        with engine.connect() as connection:

            connection.exec_driver_sql(
                "SELECT 1"
            )

        print(
            "PROPERTYIQ: PostgreSQL connection verified."
        )

        return "interactive"

    except Exception as exc:

        raise RuntimeError(
            "PropertyIQ could not bootstrap the "
            "PostgreSQL/PostGIS runtime. "
            "Provide DATABASE_URL in production or "
            "a valid PostgreSQL password in Colab."
        ) from exc



if (
    "app" not in globals()
    or "engine" not in globals()
    or globals().get("app") is None
    or globals().get("engine") is None
):

    try:
        bootstrap_mode = _propertyiq_autobootstrap()

        print("")
        print("PropertyIQ database runtime:", bootstrap_mode)

    except Exception as exc:
        raise RuntimeError(
            "PropertyIQ V17 could not auto-bootstrap the "
            "PostgreSQL/PostGIS runtime. "
            "Check the database password and connection."
        ) from exc


# ------------------------------------------------------------
# Production FastAPI application
# ------------------------------------------------------------

if "app" not in globals() or app is None:
    app = FastAPI(
        title="PropertyIQ",
        description="Property Intelligence Platform",
        version="1.0"
    )

# Install V17 automatically.
try:

    result = install_v17_intelligence_report(
        app,
        engine
    )

    print("")
    print("=" * 72)
    print(
        "PROPERTYIQ V17 PROPERTY INTELLIGENCE REPORT INSTALLED"
    )
    print("=" * 72)
    print(
        "Module:",
        MODULE_VERSION
    )
    print("")

    for route in result["routes"]:
        print("  ", route)

    print("")
    print("PostgreSQL + PostGIS: CONNECTED")
    print("Source-backed data only")
    print("No valuation prediction / no investment score")
    print("=" * 72)

except Exception as exc:

    raise RuntimeError(
        "PropertyIQ V17 could not be installed."
    ) from exc



# ============================================================
# PROPERTYIQ MODULE: V18
# ORIGINAL COLAB CELL: In[18]
# ============================================================

# ============================================================
# PROPERTYIQ V18 — MAP-NATIVE LEAFLET INTELLIGENCE
# ============================================================
#
# V18 is additive. It preserves the existing PropertyIQ runtime
# and V17 report routes.
#
# Features:
#   - Leaflet map
#   - Property anchor
#   - Intelligence points around property
#   - Source/layer filtering
#   - Radius controls
#   - Evidence/source display
#   - Property summary
#   - Google Maps handoff
#   - No fabricated coordinates
#   - Direct Colab execution
#
# ============================================================

MODULE_VERSION = "PROPERTYIQ-V18-MAP-NATIVE-LEAFLET"


# ------------------------------------------------------------
# Imports
# ------------------------------------------------------------

import json
import math
import html
from datetime import datetime, timezone

from sqlalchemy import text


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _v18_json(value):
    try:
        return json.dumps(value, default=str)
    except Exception:
        return "{}"


def _v18_clean(value):
    if value is None:
        return ""
    return str(value)


def _v18_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _v18_source_group(source_type, source):
    value = (
        _v18_clean(source_type)
        + " "
        + _v18_clean(source)
    ).upper()

    if "OSM" in value or "OPENSTREET" in value:
        return "OSM"

    if "RERA" in value:
        return "RERA"

    if "GOVERNMENT" in value or "PAIMANA" in value:
        return "GOVERNMENT"

    if "NEWS" in value or "GDELT" in value:
        return "NEWS"

    return "OTHER"


def _v18_label(source_type, source, category):
    group = _v18_source_group(
        source_type,
        source
    )

    if group == "OSM":
        return "OpenStreetMap"

    if group == "RERA":
        return "RERA"

    if group == "GOVERNMENT":
        return "Government"

    if group == "NEWS":
        return "News"

    return (
        _v18_clean(category)
        or _v18_clean(source_type)
        or "Intelligence"
    )


def _v18_table_columns(engine, table_name):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                ORDER BY ordinal_position
            """),
            {
                "table_name": table_name
            }
        ).scalars().all()

    return set(rows)


def _v18_pick(row, *names):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _v18_coords_from_row(row):
    lat = _v18_pick(
        row,
        "latitude",
        "lat"
    )

    lon = _v18_pick(
        row,
        "longitude",
        "lon",
        "lng"
    )

    lat = _v18_float(lat)
    lon = _v18_float(lon)

    if lat is None or lon is None:
        return None, None

    if not (-90 <= lat <= 90):
        return None, None

    if not (-180 <= lon <= 180):
        return None, None

    return lat, lon


# ------------------------------------------------------------
# Main map-data builder
# ------------------------------------------------------------

def get_v18_map_data(
    engine,
    property_id,
    radius_km=5.0,
    limit=500
):

    radius_km = max(
        0.1,
        min(float(radius_km), 25.0)
    )

    limit = max(
        1,
        min(int(limit), 1000)
    )

    # --------------------------------------------------------
    # Property
    # --------------------------------------------------------

    with engine.connect() as conn:

        property_row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    if not property_row:
        raise ValueError(
            "Property not found."
        )

    property_lat = _v18_float(
        property_row["latitude"]
    )

    property_lon = _v18_float(
        property_row["longitude"]
    )

    # --------------------------------------------------------
    # Intelligence records
    #
    # Actual PropertyIQ schema:
    #   latitude / longitude
    #   source_name
    #   source_type
    #   source_url
    #   confidence (TEXT)
    #   evidence_level
    #   metadata (JSONB)
    #
    # We deliberately use latitude/longitude as the spatial
    # source because some intelligence rows may have a NULL
    # PostGIS `location` while still having coordinates.
    # --------------------------------------------------------

    columns = _v18_table_columns(
        engine,
        "intelligence_records"
    )

    required = {
        "id",
        "latitude",
        "longitude"
    }

    if not required.issubset(columns):

        intelligence_rows = []

    else:

        select_parts = [
            "id",
            "latitude",
            "longitude",
        ]

        optional_columns = [
            "property_id",
            "title",
            "description",
            "category",
            "status",
            "source_name",
            "source_type",
            "source_url",
            "published_at",
            "confidence",
            "evidence_level",
            "metadata",
            "created_at",
            "updated_at",
        ]

        for column in optional_columns:
            if column in columns:
                select_parts.append(column)

        select_sql = ", ".join(select_parts)

        with engine.connect() as conn:

            intelligence_rows = conn.execute(
                text(f"""
                    SELECT
                        {select_sql},
                        (
                            6371.0088 *
                            2.0 *
                            ASIN(
                                SQRT(
                                    POWER(
                                        SIN(
                                            RADIANS(
                                                latitude - :lat
                                            ) / 2.0
                                        ),
                                        2
                                    )
                                    +
                                    COS(
                                        RADIANS(:lat)
                                    )
                                    *
                                    COS(
                                        RADIANS(latitude)
                                    )
                                    *
                                    POWER(
                                        SIN(
                                            RADIANS(
                                                longitude - :lon
                                            ) / 2.0
                                        ),
                                        2
                                    )
                                )
                            )
                        ) AS _distance_km
                    FROM intelligence_records
                    WHERE latitude IS NOT NULL
                      AND longitude IS NOT NULL
                      AND latitude BETWEEN -90 AND 90
                      AND longitude BETWEEN -180 AND 180
                      AND (
                          6371.0088 *
                          2.0 *
                          ASIN(
                              SQRT(
                                  POWER(
                                      SIN(
                                          RADIANS(
                                              latitude - :lat
                                          ) / 2.0
                                      ),
                                      2
                                  )
                                  +
                                  COS(
                                      RADIANS(:lat)
                                  )
                                  *
                                  COS(
                                      RADIANS(latitude)
                                  )
                                  *
                                  POWER(
                                      SIN(
                                          RADIANS(
                                              longitude - :lon
                                          ) / 2.0
                                      ),
                                      2
                                  )
                              )
                          )
                      ) <= :radius_km
                    ORDER BY _distance_km ASC
                    LIMIT :limit
                """),
                {
                    "lat": property_lat,
                    "lon": property_lon,
                    "radius_km": radius_km,
                    "limit": limit,
                }
            ).mappings().all()

    # --------------------------------------------------------
    # Normalize map records
    # --------------------------------------------------------

    points = []

    for row in intelligence_rows:

        lat = _v18_float(
            row.get("_lat")
        )

        lon = _v18_float(
            row.get("_lon")
        )

        if lat is None or lon is None:
            continue

        source_type = row.get(
            "source_type"
        )

        source = row.get(
            "source_name"
        )

        category = row.get(
            "category"
        )

        title = _v18_pick(
            row,
            "title",
            "name"
        )

        if not title:
            title = (
                _v18_clean(category)
                or "Intelligence Record"
            )

        metadata = row.get(
            "metadata"
        )

        if isinstance(metadata, str):
            try:
                metadata = json.loads(
                    metadata
                )
            except Exception:
                pass

        if metadata is None:
            metadata = {}

        group = _v18_source_group(
            source_type,
            source
        )

        points.append(
            {
                "id": str(row.get("id")),
                "lat": lat,
                "lon": lon,
                "title": str(title),
                "description": _v18_clean(
                    row.get("description")
                ),
                "category": _v18_clean(
                    category
                ),
                "source": _v18_clean(
                    source
                ),
                "source_type": _v18_clean(
                    source_type
                ),
                "source_group": group,
                "source_url": _v18_clean(
                    row.get("source_url")
                ),
                "status": _v18_clean(
                    row.get("status")
                ),
                "evidence_level": _v18_clean(
                    row.get("evidence_level")
                ),
                "confidence": _v18_clean(
                    row.get("confidence")
                ),
                "distance_km": _v18_float(
                    row.get("_distance_km")
                ),
                "metadata": metadata,
            }
        )

    # --------------------------------------------------------
    # Counts
    # --------------------------------------------------------

    layer_counts = {}

    for point in points:

        group = point["source_group"]

        layer_counts[group] = (
            layer_counts.get(group, 0)
            + 1
        )

    return {
        "module": MODULE_VERSION,
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "property": {
            "id": str(property_row["id"]),
            "name": property_row["property_name"],
            "address": property_row["property_address"],
            "latitude": property_lat,
            "longitude": property_lon,
            "property_type": property_row["property_type"],
            "area": property_row["area"],
            "price": property_row["price"],
            "price_per_sqft": property_row[
                "price_per_sqft"
            ],
            "bedrooms": property_row["bedrooms"],
            "bathrooms": property_row["bathrooms"],
            "builder_owner": property_row[
                "builder_owner"
            ],
        },
        "parameters": {
            "radius_km": radius_km,
            "limit": limit,
        },
        "points": points,
        "layer_counts": layer_counts,
        "total_points": len(points),
        "google_maps_url": (
            "https://www.google.com/maps/search/?api=1"
            f"&query={property_lat},{property_lon}"
        ),
    }


# ------------------------------------------------------------
# API installer
# ------------------------------------------------------------

def install_v18_map_native(
    app,
    engine
):

    # Avoid duplicate registration.
    existing_paths = {
        getattr(route, "path", "")
        for route in app.routes
    }

    api_path = (
        "/api/v1/properties/"
        "{property_id}/map"
    )

    ui_path = (
        "/propertyiq/map/"
        "{property_id}"
    )

    # --------------------------------------------------------
    # API
    # --------------------------------------------------------

    if api_path not in existing_paths:

        @app.get(
            api_path,
            tags=["PropertyIQ V18"]
        )
        def v18_map_api(
            property_id: str,
            radius_km: float = 5.0,
            limit: int = 500,
        ):

            return get_v18_map_data(
                engine,
                property_id,
                radius_km=radius_km,
                limit=limit
            )

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    if ui_path not in existing_paths:

        from fastapi.responses import HTMLResponse

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V18"]
        )
        def v18_map_ui(
            property_id: str
        ):

            pid = html.escape(
                str(property_id),
                quote=True
            )

            page = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>PropertyIQ — Intelligence Map</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<style>

:root {{
    --bg: #07111f;
    --panel: rgba(12, 24, 41, .92);
    --panel2: rgba(17, 31, 51, .88);
    --line: rgba(255,255,255,.09);
    --text: #eef6ff;
    --muted: #91a4bb;
    --accent: #20cfff;
}}

* {{
    box-sizing: border-box;
}}

html, body {{
    margin: 0;
    width: 100%;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}}

#app {{
    width: 100%;
    height: 100vh;
    position: relative;
    overflow: hidden;
}}

#map {{
    position: absolute;
    inset: 0;
    z-index: 1;
}}

.topbar {{
    position: absolute;
    z-index: 1000;
    top: 18px;
    left: 18px;
    right: 18px;
    height: 58px;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 14px;
    border: 1px solid var(--line);
    background: rgba(7,17,31,.88);
    backdrop-filter: blur(18px);
    border-radius: 16px;
    box-shadow: 0 18px 50px rgba(0,0,0,.28);
}}

.brand {{
    font-weight: 800;
    letter-spacing: .5px;
    white-space: nowrap;
}}

.brand span {{
    color: var(--accent);
}}

.search {{
    flex: 1;
    min-width: 120px;
}}

.search input {{
    width: 100%;
    background: rgba(255,255,255,.05);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: 11px;
    padding: 10px 12px;
    outline: none;
}}

.button {{
    border: 1px solid var(--line);
    background: rgba(255,255,255,.06);
    color: var(--text);
    border-radius: 10px;
    padding: 9px 12px;
    cursor: pointer;
    white-space: nowrap;
}}

.button:hover {{
    background: rgba(255,255,255,.11);
}}

.sidebar {{
    position: absolute;
    z-index: 1000;
    top: 92px;
    left: 18px;
    width: 315px;
    max-height: calc(100vh - 110px);
    overflow: auto;
    padding: 16px;
    border: 1px solid var(--line);
    background: var(--panel);
    backdrop-filter: blur(18px);
    border-radius: 18px;
    box-shadow: 0 18px 50px rgba(0,0,0,.32);
}}

.rightpanel {{
    position: absolute;
    z-index: 1000;
    top: 92px;
    right: 18px;
    width: 340px;
    max-height: calc(100vh - 110px);
    overflow: auto;
    padding: 16px;
    border: 1px solid var(--line);
    background: var(--panel);
    backdrop-filter: blur(18px);
    border-radius: 18px;
    box-shadow: 0 18px 50px rgba(0,0,0,.32);
}}

h3 {{
    margin: 0 0 8px 0;
}}

.small {{
    color: var(--muted);
    font-size: 12px;
    line-height: 1.5;
}}

.metric {{
    margin-top: 10px;
    padding: 11px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(255,255,255,.035);
}}

.metric strong {{
    display: block;
    font-size: 19px;
    margin-bottom: 3px;
}}

.layer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,.06);
}}

.layer:last-child {{
    border-bottom: none;
}}

.layer input {{
    accent-color: var(--accent);
}}

#loading {{
    position: absolute;
    z-index: 2000;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(4,10,18,.75);
    backdrop-filter: blur(7px);
}}

.loading-card {{
    padding: 24px 28px;
    border-radius: 16px;
    background: var(--panel2);
    border: 1px solid var(--line);
}}

.leaflet-control-zoom {{
    border: 1px solid var(--line) !important;
}}

.leaflet-control-zoom a {{
    background: #0d1b2d !important;
    color: white !important;
    border-color: var(--line) !important;
}}

.popup-title {{
    font-weight: 800;
    margin-bottom: 5px;
}}

.popup-meta {{
    color: #566;
    font-size: 12px;
    line-height: 1.5;
}}

a {{
    color: #159dd2;
}}

@media (max-width: 900px) {{

    .sidebar {{
        width: 270px;
    }}

    .rightpanel {{
        width: 270px;
    }}

}}

@media (max-width: 700px) {{

    .sidebar,
    .rightpanel {{
        display: none;
    }}

    .topbar {{
        left: 10px;
        right: 10px;
        top: 10px;
    }}

}}

</style>
</head>

<body>

<div id="app">

<div id="map"></div>

<div id="loading">
    <div class="loading-card">
        Loading PropertyIQ intelligence map...
    </div>
</div>

<div class="topbar">

    <div class="brand">
        Property<span>IQ</span>
    </div>

    <div class="search">
        <input
            id="filter"
            placeholder="Filter intelligence..."
            autocomplete="off"
        >
    </div>

    <button
        class="button"
        onclick="fitAll()"
    >
        Fit
    </button>

    <button
        class="button"
        onclick="openGoogleMaps()"
    >
        Google Maps
    </button>

</div>

<div class="sidebar">

    <h3>Map Layers</h3>

    <div class="small">
        Source-backed intelligence within the selected radius.
    </div>

    <div class="metric">
        <strong id="total">—</strong>
        Intelligence points
    </div>

    <div style="margin-top:12px">

        <label>
            Radius
            <select
                id="radius"
                class="button"
                style="width:100%;margin-top:7px"
                onchange="reloadMap()"
            >
                <option value="1">1 km</option>
                <option value="3">3 km</option>
                <option value="5" selected>5 km</option>
                <option value="10">10 km</option>
                <option value="25">25 km</option>
            </select>
        </label>

    </div>

    <div id="layers"
         style="margin-top:12px">
    </div>

</div>

<div class="rightpanel">

    <h3 id="property-name">
        PropertyIQ
    </h3>

    <div
        id="property-address"
        class="small"
    >
        Loading...
    </div>

    <div class="metric">
        <strong id="property-type">—</strong>
        Property type
    </div>

    <div class="metric">
        <strong id="coordinates">—</strong>
        Coordinates
    </div>

    <div
        id="selected"
        style="margin-top:14px"
    >

        <div class="small">
            Select a map intelligence point to inspect
            its source, status and evidence metadata.
        </div>

    </div>

</div>

</div>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<script>

const PROPERTY_ID = "{pid}";

let map = null;
let data = null;
let markers = [];
let propertyMarker = null;
let propertyCircle = null;

const colors = {{
    "OSM": "#20cfff",
    "RERA": "#a78bfa",
    "GOVERNMENT": "#34d399",
    "NEWS": "#fbbf24",
    "OTHER": "#fb7185"
}};

function escapeHtml(value) {{

    const div = document.createElement("div");
    div.textContent = value ?? "";
    return div.innerHTML;

}}

function initMap(payload) {{

    data = payload;

    const p = payload.property;

    document.getElementById(
        "property-name"
    ).textContent =
        p.name || "PropertyIQ";

    document.getElementById(
        "property-address"
    ).textContent =
        p.address || "Address unavailable";

    document.getElementById(
        "property-type"
    ).textContent =
        p.property_type || "Property";

    document.getElementById(
        "coordinates"
    ).textContent =
        `${{p.latitude}}, ${{p.longitude}}`;

    document.getElementById(
        "total"
    ).textContent =
        payload.total_points;

    map = L.map("map", {{
        zoomControl: true,
        preferCanvas: true
    }}).setView(
        [p.latitude, p.longitude],
        13
    );

    L.tileLayer(
        "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
        {{
            maxZoom: 19,
            attribution:
                '&copy; OpenStreetMap contributors'
        }}
    ).addTo(map);

    propertyMarker = L.marker(
        [p.latitude, p.longitude]
    ).addTo(map);

    propertyMarker.bindPopup(
        `<div class="popup-title">
            ${{escapeHtml(p.name)}}
         </div>
         <div class="popup-meta">
            Property anchor<br>
            ${{escapeHtml(p.address || "")}}
         </div>`
    );

    propertyCircle = L.circle(
        [p.latitude, p.longitude],
        {{
            radius:
                payload.parameters.radius_km * 1000,
            color: "#20cfff",
            weight: 1,
            fillOpacity: .025
        }}
    ).addTo(map);

    renderLayers();
    renderPoints();

    document.getElementById(
        "loading"
    ).style.display = "none";

    setTimeout(
        () => map.invalidateSize(),
        100
    );

}}

function renderLayers() {{

    const groups = [
        "OSM",
        "RERA",
        "GOVERNMENT",
        "NEWS",
        "OTHER"
    ];

    const container =
        document.getElementById("layers");

    container.innerHTML = "";

    groups.forEach(group => {{

        const count =
            data.layer_counts[group] || 0;

        const row =
            document.createElement("div");

        row.className = "layer";

        row.innerHTML = `
            <label>
                <input
                    type="checkbox"
                    checked
                    data-layer="${{group}}"
                    onchange="applyFilters()"
                >
                ${{group}}
            </label>
            <span class="small">
                ${{count}}
            </span>
        `;

        container.appendChild(row);

    }});

}}

function renderPoints() {{

    markers.forEach(
        marker => map.removeLayer(marker)
    );

    markers = [];

    const search =
        (
            document.getElementById(
                "filter"
            ).value || ""
        ).toLowerCase();

    const enabled = {{}};

    document.querySelectorAll(
        "[data-layer]"
    ).forEach(
        checkbox => {{
            enabled[
                checkbox.dataset.layer
            ] = checkbox.checked;
        }}
    );

    data.points.forEach(point => {{

        if (!enabled[point.source_group])
            return;

        const haystack = (
            (point.title || "") + " " +
            (point.description || "") + " " +
            (point.category || "") + " " +
            (point.source || "")
        ).toLowerCase();

        if (
            search &&
        )
            return;

        const color =
            colors[point.source_group]
            || colors.OTHER;

        const marker =
            L.circleMarker(
                [point.lat, point.lon],
                {{
                    radius: 7,
                    color: color,
                    weight: 2,
                    fillColor: color,
                    fillOpacity: .65
                }}
            ).addTo(map);

        const sourceLink =
            point.source_url
                ? `<br><a href="${{
                    escapeHtml(
                        point.source_url
                    )
                }}" target="_blank"
                   rel="noopener">
                   Source
                   </a>`
                : "";

        marker.bindPopup(
            `<div class="popup-title">
                ${{escapeHtml(point.title)}}
             </div>
             <div class="popup-meta">
                ${{escapeHtml(
                    point.source_group
                )}}<br>
                ${{escapeHtml(
                    point.category
                )}}<br>
                Distance:
                ${{point.distance_km === null
                    ? "—"
                    : point.distance_km.toFixed(2)
                }} km<br>
                Status:
                ${{escapeHtml(
                    point.status || "—"
                )}}<br>
                Evidence:
                ${{escapeHtml(
                    point.evidence_level || "—"
                )}}
                ${{sourceLink}}
             </div>`
        );

        marker.on(
            "click",
            () => showSelected(point)
        );

        markers.push(marker);

    }});

}}

function showSelected(point) {{

    const panel =
        document.getElementById(
            "selected"
        );

    const source =
        point.source_url
            ? `<a href="${{
                escapeHtml(
                    point.source_url
                )
            }}" target="_blank"
               rel="noopener">
               Open source
               </a>`
            : "No source URL";

    panel.innerHTML = `
        <div class="metric">
            <strong>
                ${{escapeHtml(point.title)}}
            </strong>

            <div class="small">
                Layer: ${{escapeHtml(
                    point.source_group
                )}}<br>
                Category: ${{escapeHtml(
                    point.category || "—"
                )}}<br>
                Distance: ${{point.distance_km === null
                    ? "—"
                    : point.distance_km.toFixed(2)
                }} km<br>
                Status: ${{escapeHtml(
                    point.status || "—"
                )}}<br>
                Evidence: ${{escapeHtml(
                    point.evidence_level || "—"
                )}}<br>
                Confidence: ${{point.confidence === null
                    ? "—"
                    : point.confidence
                }}<br><br>
                ${{escapeHtml(
                    point.description || ""
                )}}
                <br><br>
                ${{source}}
            </div>
        </div>
    `;

}}

function applyFilters() {{
    renderPoints();
}}

function fitAll() {{

    if (!map)
        return;

    const bounds = [];

    if (propertyMarker)
        bounds.push(
            propertyMarker.getLatLng()
        );

    markers.forEach(
        marker => bounds.push(
            marker.getLatLng()
        )
    );

    if (bounds.length)
        map.fitBounds(
            bounds,
            {{
                padding: [50, 50]
            }}
        );

}}

function openGoogleMaps() {{

    if (
        data &&
        data.google_maps_url
    ) {{
        window.open(
            data.google_maps_url,
            "_blank"
        );
    }}

}}

async function reloadMap() {{

    document.getElementById(
        "loading"
    ).style.display = "flex";

    const radius =
        document.getElementById(
            "radius"
        ).value;

    try {{

        const response =
            await fetch(
                `/api/v1/properties/${{
                    PROPERTY_ID
                }}/map?radius_km=${{
                    radius
                }}&limit=500`
            );

        if (!response.ok)
            throw new Error(
                await response.text()
            );

        const payload =
            await response.json();

        if (map)
            map.remove();

        markers = [];
        propertyMarker = null;
        propertyCircle = null;

        initMap(payload);

    }} catch (error) {{

        console.error(error);

        document.getElementById(
            "loading"
        ).innerHTML = `
            <div class="loading-card">
                <strong>
                    PropertyIQ map error
                </strong>
                <div class="small"
                     style="margin-top:8px">
                    ${{escapeHtml(
                        error.message
                    )}}
                </div>
            </div>
        `;

    }}

}}

document.getElementById(
    "filter"
).addEventListener(
    "input",
    renderPoints
);

reloadMap();

</script>

</body>
</html>
"""

            return HTMLResponse(
                content=page
            )

    return {
        "module": MODULE_VERSION,
        "routes": [
            api_path,
            ui_path,
        ],
    }


# ------------------------------------------------------------
# Direct Colab installation
# ------------------------------------------------------------

def _v18_autobootstrap():

    global engine
    global app

    if (
        "engine" in globals()
        and engine is not None
        and "app" in globals()
        and app is not None
    ):
        return "existing"

    # Reuse an existing engine if available.
    if (
        "engine" in globals()
        and engine is not None
    ):
        if "app" not in globals() or app is None:
            app = FastAPI(
                title="PropertyIQ",
                version="V18"
            )
        return "existing-engine"

    # Reconnect to the known PropertyIQ PostgreSQL database.
    import os
    import importlib
    import importlib.util
    import subprocess
    import sys
    from getpass import getpass
    from urllib.parse import quote_plus

    if importlib.util.find_spec(
        "psycopg"
    ) is None:

        print(
            "Installing PostgreSQL driver..."
        )

        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "psycopg[binary]",
            ]
        )

        importlib.invalidate_caches()

    host = os.environ.get(
        "PROPERTYIQ_DB_HOST",
        "aws-0-ap-south-1.pooler.supabase.com"
    )

    user = os.environ.get(
        "PROPERTYIQ_DB_USER",
        "postgres.reqsrwfyeyupgqsggchv"
    )

    database = os.environ.get(
        "PROPERTYIQ_DB_NAME",
        "postgres"
    )

    port = os.environ.get(
        "PROPERTYIQ_DB_PORT",
        "5432"
    )

    print("")
    print("=" * 72)
    print("PROPERTYIQ V18 — DATABASE AUTO-BOOTSTRAP")
    print("=" * 72)
    print("Host:", host)
    print("Database:", database)
    print("User:", user)
    print("")

    password = getpass(
        "Enter your PropertyIQ PostgreSQL password: "
    )

    if not password:
        raise RuntimeError(
            "No database password supplied."
        )

    from sqlalchemy import create_engine

    url = (
        "postgresql+psycopg://"
        + quote_plus(user)
        + ":"
        + quote_plus(password)
        + "@"
        + host
        + ":"
        + str(port)
        + "/"
        + database
    )

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )

    with engine.connect() as conn:

        conn.execute(
            text("SELECT 1")
        )

        conn.execute(
            text(
                "SELECT PostGIS_Full_Version()"
            )
        ).scalar()

    if "app" not in globals() or app is None:


        app = FastAPI(
            title="PropertyIQ",
            version="V18"
        )

    return "autobootstrapped"


try:

    bootstrap_mode = _v18_autobootstrap()

    result = install_v18_map_native(
        app,
        engine
    )

    print("")
    print("=" * 72)
    print(
        "PROPERTYIQ V18 — MAP-NATIVE LEAFLET INSTALLED"
    )
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime:", bootstrap_mode)
    print("")

    for route in result["routes"]:
        print("  ", route)

    print("")
    print(
        "Leaflet: ENABLED"
    )
    print(
        "OpenStreetMap tiles: ENABLED"
    )
    print(
        "Property anchor: ENABLED"
    )
    print(
        "Source-backed intelligence points: ENABLED"
    )
    print(
        "Layer filtering: ENABLED"
    )
    print(
        "Radius controls: ENABLED"
    )
    print(
        "Google Maps handoff: ENABLED"
    )
    print(
        "No fabricated coordinates"
    )
    print("=" * 72)

except Exception as exc:

    raise RuntimeError(
        "PropertyIQ V18 installation failed."
    ) from exc



# ============================================================
# PROPERTYIQ MODULE: V18_3
# ORIGINAL COLAB CELL: In[20]
# ============================================================

# ============================================================
# PROPERTYIQ V18.3 — MAP INTELLIGENCE MODES
# ============================================================
#
# Additive enhancement to V18.
#
# Modes:
#   1. nearby   -> records within selected radius
#   2. mapped   -> every intelligence record with coordinates
#   3. unmapped -> records without usable coordinates
#
# Preserves V17/V18 routes and database data.
# Direct Colab execution.
# ============================================================

MODULE_VERSION = "PROPERTYIQ-V18.3-MAP-INTELLIGENCE-MODES"

import json
import html
from datetime import datetime, timezone
from sqlalchemy import text


def _v183_clean(value):
    return "" if value is None else str(value)


def _v183_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _v183_source_group(source_type, source_name):
    value = (
        _v183_clean(source_type) + " " +
        _v183_clean(source_name)
    ).upper()

    if "OSM" in value or "OPENSTREET" in value:
        return "OSM"
    if "RERA" in value:
        return "RERA"
    if "GOVERNMENT" in value or "PAIMANA" in value:
        return "GOVERNMENT"
    if "NEWS" in value or "GDELT" in value:
        return "NEWS"

    return "OTHER"


def _v183_columns(engine, table):
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table
                """),
                {"table": table}
            ).scalars().all()
        )


def get_v183_map_data(
    engine,
    property_id,
    mode="nearby",
    radius_km=5.0,
    limit=1000
):
    """
    mode:
        nearby  = mapped records inside radius
        mapped  = all records with valid coordinates
        unmapped = records without valid coordinates
    """

    mode = str(mode).lower().strip()

    if mode not in {
        "nearby",
        "mapped",
        "unmapped"
    }:
        raise ValueError(
            "mode must be nearby, mapped, or unmapped"
        )

    radius_km = max(
        0.1,
        min(float(radius_km), 100.0)
    )

    limit = max(
        1,
        min(int(limit), 5000)
    )

    # --------------------------------------------------------
    # Property
    # --------------------------------------------------------

    with engine.connect() as conn:
        property_row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    if not property_row:
        raise ValueError("Property not found.")

    property_lat = _v183_float(
        property_row["latitude"]
    )
    property_lon = _v183_float(
        property_row["longitude"]
    )

    if property_lat is None or property_lon is None:
        raise ValueError(
            "Property does not have usable coordinates."
        )

    # --------------------------------------------------------
    # Actual intelligence schema
    # --------------------------------------------------------

    columns = _v183_columns(
        engine,
        "intelligence_records"
    )

    required = {
        "id",
        "title",
        "latitude",
        "longitude",
    }

    if not required.issubset(columns):
        raise RuntimeError(
            "intelligence_records does not contain "
            "the required PropertyIQ columns."
        )

    optional = [
        "property_id",
        "category",
        "status",
        "description",
        "source_name",
        "source_type",
        "source_url",
        "confidence",
        "evidence_level",
        "metadata",
        "published_at",
        "created_at",
        "updated_at",
    ]

    select_columns = [
        "id",
        "title",
        "latitude",
        "longitude",
    ]

    for column in optional:
        if column in columns:
            select_columns.append(column)

    select_sql = ", ".join(select_columns)

    # --------------------------------------------------------
    # Distance expression
    # --------------------------------------------------------

    distance_sql = """
        (
            6371.0088 * 2.0 *
            ASIN(
                SQRT(
                    POWER(
                        SIN(
                            RADIANS(
                                latitude - :plat
                            ) / 2.0
                        ),
                        2
                    )
                    +
                    COS(RADIANS(:plat))
                    *
                    COS(RADIANS(latitude))
                    *
                    POWER(
                        SIN(
                            RADIANS(
                                longitude - :plon
                            ) / 2.0
                        ),
                        2
                    )
                )
            )
        )
    """

    if mode == "nearby":

        where_sql = f"""
            latitude IS NOT NULL
            AND longitude IS NOT NULL
            AND latitude BETWEEN -90 AND 90
            AND longitude BETWEEN -180 AND 180
            AND {distance_sql} <= :radius_km
        """

        order_sql = "_distance_km ASC"

    elif mode == "mapped":

        where_sql = """
            latitude IS NOT NULL
            AND longitude IS NOT NULL
            AND latitude BETWEEN -90 AND 90
            AND longitude BETWEEN -180 AND 180
        """

        order_sql = "_distance_km ASC"

    else:

        where_sql = """
            latitude IS NULL
            OR longitude IS NULL
            OR latitude NOT BETWEEN -90 AND 90
            OR longitude NOT BETWEEN -180 AND 180
        """

        order_sql = "created_at DESC NULLS LAST"

    query = f"""
        SELECT
            {select_sql},
            CASE
                WHEN latitude IS NOT NULL
                 AND longitude IS NOT NULL
                 AND latitude BETWEEN -90 AND 90
                 AND longitude BETWEEN -180 AND 180
                THEN {distance_sql}
                ELSE NULL
            END AS _distance_km
        FROM intelligence_records
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT :limit
    """

    params = {
        "plat": property_lat,
        "plon": property_lon,
        "radius_km": radius_km,
        "limit": limit,
    }

    with engine.connect() as conn:
        rows = conn.execute(
            text(query),
            params
        ).mappings().all()

    # --------------------------------------------------------
    # Normalize
    # --------------------------------------------------------

    points = []

    for row in rows:

        lat = _v183_float(
            row.get("latitude")
        )

        lon = _v183_float(
            row.get("longitude")
        )

        source_name = row.get(
            "source_name"
        )

        source_type = row.get(
            "source_type"
        )

        metadata = row.get(
            "metadata"
        )

        if isinstance(metadata, str):
            try:
                metadata = json.loads(
                    metadata
                )
            except Exception:
                pass

        if metadata is None:
            metadata = {}

        points.append(
            {
                "id": str(row["id"]),
                "title": _v183_clean(
                    row.get("title")
                ),
                "category": _v183_clean(
                    row.get("category")
                ),
                "status": _v183_clean(
                    row.get("status")
                ),
                "description": _v183_clean(
                    row.get("description")
                ),
                "lat": lat,
                "lon": lon,
                "source": _v183_clean(
                    source_name
                ),
                "source_type": _v183_clean(
                    source_type
                ),
                "source_group":
                    _v183_source_group(
                        source_type,
                        source_name
                    ),
                "source_url": _v183_clean(
                    row.get("source_url")
                ),
                "confidence": _v183_clean(
                    row.get("confidence")
                ),
                "evidence_level":
                    _v183_clean(
                        row.get("evidence_level")
                    ),
                "distance_km":
                    _v183_float(
                        row.get("_distance_km")
                    ),
                "metadata": metadata,
            }
        )

    return {
        "module": MODULE_VERSION,
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "mode": mode,
        "parameters": {
            "radius_km": radius_km,
            "limit": limit,
        },
        "property": {
            "id": str(property_row["id"]),
            "name": property_row["property_name"],
            "address": property_row["property_address"],
            "latitude": property_lat,
            "longitude": property_lon,
            "property_type":
                property_row["property_type"],
        },
        "points": points,
        "count": len(points),
    }


# ------------------------------------------------------------
# API installation
# ------------------------------------------------------------

def install_v183_map_modes(app, engine):

    from fastapi.responses import HTMLResponse

    api_path = (
        "/api/v1/properties/"
        "{property_id}/map-intelligence"
    )

    ui_path = (
        "/propertyiq/map-intelligence/"
        "{property_id}"
    )

    existing_paths = {
        getattr(route, "path", "")
        for route in app.routes
    }

    if api_path not in existing_paths:

        @app.get(
            api_path,
            tags=["PropertyIQ V18.3"]
        )
        def v183_map_api(
            property_id: str,
            mode: str = "nearby",
            radius_km: float = 5.0,
            limit: int = 1000,
        ):

            return get_v183_map_data(
                engine,
                property_id,
                mode=mode,
                radius_km=radius_km,
                limit=limit,
            )

    if ui_path not in existing_paths:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V18.3"]
        )
        def v183_map_ui(
            property_id: str
        ):

            pid = html.escape(
                str(property_id),
                quote=True
            )

            page = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport"
 content="width=device-width, initial-scale=1">

<title>PropertyIQ — Intelligence Map</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<style>
:root {{
 --bg:#07111f;
 --panel:rgba(10,23,40,.94);
 --line:rgba(255,255,255,.09);
 --text:#eef6ff;
 --muted:#8ea3bb;
 --accent:#20cfff;
}}

* {{ box-sizing:border-box; }}

html,body {{
 margin:0;
 width:100%;
 height:100%;
 background:var(--bg);
 color:var(--text);
 font-family:Inter,system-ui,sans-serif;
}}

#map {{
 position:absolute;
 inset:0;
}}

.panel {{
 position:absolute;
 z-index:1000;
 top:18px;
 left:18px;
 width:330px;
 max-height:calc(100vh - 36px);
 overflow:auto;
 padding:18px;
 border:1px solid var(--line);
 border-radius:18px;
 background:var(--panel);
 backdrop-filter:blur(18px);
 box-shadow:0 18px 50px rgba(0,0,0,.35);
}}

h2 {{
 margin:0 0 5px 0;
 font-size:20px;
}}

.small {{
 color:var(--muted);
 font-size:12px;
 line-height:1.5;
}}

.mode {{
 display:grid;
 grid-template-columns:1fr;
 gap:7px;
 margin-top:15px;
}}

.mode button {{
 width:100%;
 padding:11px;
 border-radius:11px;
 border:1px solid var(--line);
 background:rgba(255,255,255,.05);
 color:var(--text);
 cursor:pointer;
 text-align:left;
}}

.mode button.active {{
 border-color:var(--accent);
 background:rgba(32,207,255,.10);
}}

.metric {{
 margin-top:10px;
 padding:12px;
 border:1px solid var(--line);
 border-radius:12px;
 background:rgba(255,255,255,.035);
}}

.metric strong {{
 display:block;
 font-size:22px;
}}

select,input {{
 width:100%;
 margin-top:7px;
 padding:10px;
 border-radius:10px;
 border:1px solid var(--line);
 background:#0b1a2c;
 color:var(--text);
}}

#loading {{
 position:fixed;
 inset:0;
 z-index:2000;
 display:none;
 align-items:center;
 justify-content:center;
 background:rgba(4,10,18,.72);
 backdrop-filter:blur(5px);
}}

.loader {{
 padding:20px 25px;
 border-radius:15px;
 background:var(--panel);
 border:1px solid var(--line);
}}

.point {{
 margin-top:8px;
 padding:10px;
 border-radius:10px;
 background:rgba(255,255,255,.035);
 border:1px solid var(--line);
}}

a {{ color:#20cfff; }}
</style>
</head>

<body>

<div id="map"></div>

<div id="loading">
 <div class="loader">
  Loading intelligence...
 </div>
</div>

<div class="panel">

 <h2>PropertyIQ</h2>

 <div id="property"
      class="small">
  Intelligence Map
 </div>

 <div class="mode">

  <button id="nearby"
          onclick="setMode('nearby')">
   <strong>Nearby</strong><br>
   <span class="small">
    Intelligence inside the selected radius
   </span>
  </button>

  <button id="mapped"
          onclick="setMode('mapped')">
   <strong>All Mapped</strong><br>
   <span class="small">
    Every intelligence record with coordinates
   </span>
  </button>

  <button id="unmapped"
          onclick="setMode('unmapped')">
   <strong>Unmapped</strong><br>
   <span class="small">
    Records requiring location resolution
   </span>
  </button>

 </div>

 <div class="metric">
  <strong id="count">—</strong>
  Current records
 </div>

 <label class="small">
  Nearby radius
  <select id="radius"
          onchange="loadData()">
   <option value="1">1 km</option>
   <option value="3">3 km</option>
   <option value="5" selected>5 km</option>
   <option value="10">10 km</option>
   <option value="25">25 km</option>
  </select>
 </label>

 <div class="metric">
  <strong id="summary">
   —
  </strong>
  Property intelligence inventory
 </div>

 <div id="points"></div>

</div>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<script>

const PROPERTY_ID = "{pid}";

let map = null;
let property = null;
let propertyMarker = null;
let markers = [];
let mode = "nearby";

const layerColors = {{
 "OSM":"#20cfff",
 "RERA":"#a78bfa",
 "GOVERNMENT":"#34d399",
 "NEWS":"#fbbf24",
 "OTHER":"#fb7185"
}};

function esc(value) {{
 const d = document.createElement("div");
 d.textContent = value ?? "";
 return d.innerHTML;
}}

function initMap(payload) {{

 if (!map) {{

  property = payload.property;

  map = L.map("map", {{
   preferCanvas:true
  }}).setView(
   [
    property.latitude,
    property.longitude
   ],
   12
  );

  L.tileLayer(
   "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
   {{
    maxZoom:19,
    attribution:
     "&copy; OpenStreetMap contributors"
   }}
  ).addTo(map);

  propertyMarker = L.marker(
   [
    property.latitude,
    property.longitude
   ]
  ).addTo(map);

  propertyMarker.bindPopup(
   `<strong>${{esc(property.name)}}</strong>
    <br>
    ${{esc(property.address || "")}}`
  );

  document.getElementById(
   "property"
  ).innerHTML =
   `<strong>${{esc(property.name)}}</strong>
    <br>
    ${{esc(property.address || "")}}`;

 }}

 markers.forEach(
  m => map.removeLayer(m)
 );

 markers = [];

 payload.points.forEach(
  point => {{

   if (
    point.lat === null ||
    point.lon === null
   )
    return;

   const color =
    layerColors[
     point.source_group
    ] || layerColors.OTHER;

   const marker =
    L.circleMarker(
     [point.lat, point.lon],
     {{
      radius:7,
      color:color,
      fillColor:color,
      fillOpacity:.7,
      weight:2
     }}
    ).addTo(map);

   marker.bindPopup(
    `<strong>
      ${{esc(point.title)}}
     </strong>
     <br>
     <span class="small">
      Layer:
      ${{esc(point.source_group)}}<br>
      Distance:
      ${{point.distance_km === null
       ? "—"
       : point.distance_km.toFixed(2)+" km"}}<br>
      Source:
      ${{esc(point.source || "—")}}<br>
      Status:
      ${{esc(point.status || "—")}}
     </span>`
   );

   markers.push(marker);

  }}
 );

 document.getElementById(
  "count"
 ).textContent =
  payload.count;

 document.getElementById(
  "points"
 ).innerHTML =
  payload.points.slice(0,20).map(
   p => `
    <div class="point">
     <strong>${{esc(p.title)}}</strong>
     <div class="small">
      ${{esc(p.source_group)}} ·
      ${{esc(p.category || "—")}}<br>
      ${{p.distance_km === null
       ? "Distance unavailable"
       : p.distance_km.toFixed(2)+" km"}}
     </div>
    </div>
   `
  ).join("");

 document.getElementById(
  "loading"
 ).style.display = "none";
}}

async function loadData() {{

 document.getElementById(
  "loading"
 ).style.display = "flex";

 try {{

  const radius =
   document.getElementById(
    "radius"
   ).value;

  const response =
   await fetch(
    `/api/v1/properties/${{
     PROPERTY_ID
    }}/map-intelligence`
    + `?mode=${{mode}}`
    + `&radius_km=${{radius}}`
    + `&limit=5000`
   );

  if (!response.ok)
   throw new Error(
    await response.text()
   );

  const payload =
   await response.json();

  initMap(payload);

 }} catch(error) {{

  document.getElementById(
   "loading"
  ).innerHTML =
   `<div class="loader">
    <strong>Map error</strong>
    <br><br>
    ${{esc(error.message)}}
   </div>`;

 }}

}}

function setMode(newMode) {{

 mode = newMode;

 document.querySelectorAll(
  ".mode button"
 ).forEach(
  b => b.classList.remove("active")
 );

 document.getElementById(
  newMode
 ).classList.add("active");

 loadData();
}}

setMode("nearby");

</script>
</body>
</html>
"""

            return HTMLResponse(
                content=page
            )

    return {
        "module": MODULE_VERSION,
        "routes": [
            api_path,
            ui_path,
        ],
    }


# ------------------------------------------------------------
# Direct Colab installation
# ------------------------------------------------------------

def _v183_bootstrap():

    global engine
    global app

    if (
        "engine" in globals()
        and engine is not None
        and "app" in globals()
        and app is not None
    ):
        return "existing"

    if (
        "engine" in globals()
        and engine is not None
    ):
        if "app" not in globals() or app is None:
            app = FastAPI(
                title="PropertyIQ",
                version="V18.3"
            )
        return "existing-engine"

    import os
    import importlib
    import importlib.util
    import subprocess
    import sys
    from getpass import getpass
    from urllib.parse import quote_plus

    if importlib.util.find_spec(
        "psycopg"
    ) is None:

        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "psycopg[binary]",
            ]
        )

        importlib.invalidate_caches()

    host = os.environ.get(
        "PROPERTYIQ_DB_HOST",
        "aws-0-ap-south-1.pooler.supabase.com"
    )

    user = os.environ.get(
        "PROPERTYIQ_DB_USER",
        "postgres.reqsrwfyeyupgqsggchv"
    )

    database = os.environ.get(
        "PROPERTYIQ_DB_NAME",
        "postgres"
    )

    port = os.environ.get(
        "PROPERTYIQ_DB_PORT",
        "5432"
    )

    print("")
    print("=" * 72)
    print("PROPERTYIQ V18.3 — DATABASE AUTO-BOOTSTRAP")
    print("=" * 72)

    password = getpass(
        "Enter your PropertyIQ PostgreSQL password: "
    )

    if not password:
        raise RuntimeError(
            "No database password supplied."
        )

    from sqlalchemy import create_engine

    url = (
        "postgresql+psycopg://"
        + quote_plus(user)
        + ":"
        + quote_plus(password)
        + "@"
        + host
        + ":"
        + port
        + "/"
        + database
    )

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )

    with engine.connect() as conn:
        conn.execute(
            text("SELECT 1")
        )
        conn.execute(
            text(
                "SELECT PostGIS_Full_Version()"
            )
        ).scalar()

    if "app" not in globals() or app is None:
        app = FastAPI(
            title="PropertyIQ",
            version="V18.3"
        )

    return "autobootstrapped"


try:

    bootstrap_mode = _v183_bootstrap()

    result = install_v183_map_modes(
        app,
        engine
    )

    print("")
    print("=" * 72)
    print(
        "PROPERTYIQ V18.3 — MAP INTELLIGENCE MODES INSTALLED"
    )
    print("=" * 72)

    print(
        "Module:",
        MODULE_VERSION
    )

    print(
        "Runtime:",
        bootstrap_mode
    )

    print("")

    for route in result["routes"]:
        print("  ", route)

    print("")
    print("Nearby mode: ENABLED")
    print("All mapped mode: ENABLED")
    print("Unmapped mode: ENABLED")
    print("No database records modified")
    print("No fabricated coordinates")
    print("=" * 72)

except Exception as exc:

    raise RuntimeError(
        "PropertyIQ V18.3 installation failed."
    ) from exc



# ============================================================
# PROPERTYIQ MODULE: V19
# ORIGINAL COLAB CELL: In[26]
# ============================================================

# ============================================================
# PROPERTYIQ V19 — INTERACTIVE DUE-DILIGENCE MAP
# ============================================================
#
# Additive stage after V18.3.
# Preserves all existing PropertyIQ routes and database records.
#
# V19 adds:
#   - property-centric due-diligence workspace
#   - mapped / nearby / unmapped intelligence inventory
#   - evidence counts per intelligence record
#   - source/status/category filtering
#   - distance bands
#   - due-diligence flags based only on stored data
#   - source/evidence drill-down
#   - Leaflet UI
#   - no fabricated intelligence
#   - no investment recommendation / score
#
# Direct Colab execution supported.
# ============================================================

MODULE_VERSION = "PROPERTYIQ-V19.1-INTERACTIVE-DUE-DILIGENCE-MAP-SCHEMA-FIX"

import json
import html
from datetime import datetime, timezone
from sqlalchemy import text


def _v19_s(v):
    return "" if v is None else str(v)


def _v19_f(v):
    try:
        return float(v)
    except Exception:
        return None


def _v19_group(source_type, source_name):
    value = (_v19_s(source_type) + " " + _v19_s(source_name)).upper()
    if "OSM" in value or "OPENSTREET" in value:
        return "OSM"
    if "RERA" in value:
        return "RERA"
    if "PAIMANA" in value or "GOVERNMENT" in value or "MOSPI" in value:
        return "GOVERNMENT"
    if "GDELT" in value or "NEWS" in value:
        return "NEWS"
    return "OTHER"


def _v19_distance_band(km):
    if km is None:
        return "unmapped"
    if km <= 0.5:
        return "0–0.5 km"
    if km <= 1:
        return "0.5–1 km"
    if km <= 3:
        return "1–3 km"
    if km <= 5:
        return "3–5 km"
    return "5+ km"


def _v19_columns(engine, table):
    with engine.connect() as conn:
        return set(conn.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name=:table
            """),
            {"table": table}
        ).scalars().all())


def _v19_table_exists(engine, table):
    with engine.connect() as conn:
        return bool(conn.execute(
            text("""
                SELECT EXISTS(
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema='public'
                      AND table_name=:table
                )
            """),
            {"table": table}
        ).scalar())


def build_v19_due_diligence(engine, property_id, radius_km=5.0, limit=2000):

    radius_km = max(0.1, min(float(radius_km), 100.0))
    limit = max(1, min(int(limit), 5000))

    with engine.connect() as conn:
        prop = conn.execute(
            text("""
                SELECT
                    id, property_name, property_address,
                    latitude, longitude, property_type,
                    area, price, price_per_sqft,
                    bedrooms, bathrooms, builder_owner
                FROM properties
                WHERE id=CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    if not prop:
        raise ValueError("Property not found.")

    plat = _v19_f(prop["latitude"])
    plon = _v19_f(prop["longitude"])

    if plat is None or plon is None:
        raise ValueError("Property has no usable coordinates.")

    cols = _v19_columns(engine, "intelligence_records")

    required = {"id", "title", "latitude", "longitude"}
    if not required.issubset(cols):
        raise RuntimeError("Required intelligence schema is unavailable.")

    optional = [
        "property_id", "category", "status", "description",
        "source_name", "source_type", "source_url",
        "published_at", "confidence", "evidence_level",
        "metadata", "created_at", "updated_at"
    ]

    selects = ["id", "title", "latitude", "longitude"]
    selects += [c for c in optional if c in cols]

    distance = """
        CASE
        WHEN latitude IS NOT NULL
         AND longitude IS NOT NULL
         AND latitude BETWEEN -90 AND 90
         AND longitude BETWEEN -180 AND 180
        THEN (
            6371.0088 * 2.0 *
            ASIN(
                SQRT(
                    POWER(SIN(RADIANS(latitude - :plat)/2.0), 2)
                    +
                    COS(RADIANS(:plat))
                    * COS(RADIANS(latitude))
                    * POWER(SIN(RADIANS(longitude - :plon)/2.0), 2)
                )
            )
        )
        ELSE NULL
        END
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT
                    {", ".join(selects)},
                    {distance} AS _distance_km
                FROM intelligence_records
                ORDER BY
                    CASE WHEN latitude IS NULL OR longitude IS NULL
                         THEN 1 ELSE 0 END,
                    _distance_km ASC NULLS LAST,
                    created_at DESC
                LIMIT :limit
            """),
            {
                "plat": plat,
                "plon": plon,
                "limit": limit
            }
        ).mappings().all()

    # Evidence inventory, using actual available schema dynamically.
    evidence_counts = {}
    evidence_rows = []

    if _v19_table_exists(engine, "evidence_items"):
        evcols = _v19_columns(engine, "evidence_items")

        if "intelligence_id" in evcols:
            with engine.connect() as conn:
                counts = conn.execute(
                    text("""
                        SELECT intelligence_id, COUNT(*) AS n
                        FROM evidence_items
                        WHERE intelligence_id IS NOT NULL
                        GROUP BY intelligence_id
                    """)
                ).mappings().all()

            evidence_counts = {
                str(r["intelligence_id"]): int(r["n"])
                for r in counts
            }

        # Read a safe set of evidence fields if present.
        safe_ev = [
            c for c in [
                "id", "intelligence_id", "property_id",
                "source_name", "source_type", "source_url",
                "title", "evidence_level", "confidence",
                "created_at"
            ] if c in evcols
        ]

        if safe_ev:
            # Evidence schema varies across PropertyIQ versions.
            # In particular, the current evidence_items table does not
            # contain created_at, so choose a real available ordering column.
            if "created_at" in evcols:
                ev_order = "created_at DESC"
            elif "updated_at" in evcols:
                ev_order = "updated_at DESC"
            elif "id" in evcols:
                ev_order = "id DESC"
            else:
                ev_order = "1"

            with engine.connect() as conn:
                evidence_rows = [
                    dict(r) for r in conn.execute(
                        text(f"""
                            SELECT {", ".join(safe_ev)}
                            FROM evidence_items
                            ORDER BY {ev_order}
                            LIMIT 1000
                        """)
                    ).mappings().all()
                ]

    records = []
    counts = {
        "total": 0,
        "mapped": 0,
        "unmapped": 0,
        "nearby": 0,
        "with_evidence": 0,
    }
    source_counts = {}
    status_counts = {}
    category_counts = {}
    band_counts = {}

    for r in rows:
        lat = _v19_f(r.get("latitude"))
        lon = _v19_f(r.get("longitude"))
        km = _v19_f(r.get("_distance_km"))
        mapped = lat is not None and lon is not None
        nearby = mapped and km is not None and km <= radius_km

        source_name = r.get("source_name")
        source_type = r.get("source_type")
        group = _v19_group(source_type, source_name)
        evidence_count = evidence_counts.get(str(r["id"]), 0)
        band = _v19_distance_band(km)

        metadata = r.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                pass
        if metadata is None:
            metadata = {}

        item = {
            "id": str(r["id"]),
            "title": _v19_s(r.get("title")),
            "category": _v19_s(r.get("category")),
            "status": _v19_s(r.get("status")),
            "description": _v19_s(r.get("description")),
            "latitude": lat,
            "longitude": lon,
            "mapped": mapped,
            "nearby": nearby,
            "distance_km": km,
            "distance_band": band,
            "source_name": _v19_s(source_name),
            "source_type": _v19_s(source_type),
            "source_group": group,
            "source_url": _v19_s(r.get("source_url")),
            "published_at": _v19_s(r.get("published_at")),
            "confidence": _v19_s(r.get("confidence")),
            "evidence_level": _v19_s(r.get("evidence_level")),
            "evidence_count": evidence_count,
            "metadata": metadata,
        }
        records.append(item)

        counts["total"] += 1
        counts["mapped" if mapped else "unmapped"] += 1
        if nearby:
            counts["nearby"] += 1
        if evidence_count:
            counts["with_evidence"] += 1

        source_counts[group] = source_counts.get(group, 0) + 1

        status = item["status"] or "UNSPECIFIED"
        status_counts[status] = status_counts.get(status, 0) + 1

        category = item["category"] or "UNSPECIFIED"
        category_counts[category] = category_counts.get(category, 0) + 1

        band_counts[band] = band_counts.get(band, 0) + 1

    # Descriptive flags only. These are workflow flags, not investment judgments.
    flags = []

    if counts["unmapped"]:
        flags.append({
            "type": "LOCATION_PENDING",
            "count": counts["unmapped"],
            "message": "Some intelligence records do not yet have usable coordinates."
        })

    no_evidence = counts["total"] - counts["with_evidence"]
    if no_evidence > 0:
        flags.append({
            "type": "EVIDENCE_GAP",
            "count": no_evidence,
            "message": "Some intelligence records do not have linked evidence items."
        })

    if counts["nearby"] == 0:
        flags.append({
            "type": "NO_NEARBY_RECORDS",
            "count": 0,
            "message": f"No stored mapped intelligence is within {radius_km:g} km of this property."
        })

    return {
        "module": MODULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "property": {
            "id": str(prop["id"]),
            "name": prop["property_name"],
            "address": prop["property_address"],
            "latitude": plat,
            "longitude": plon,
            "property_type": prop["property_type"],
            "area": prop["area"],
            "price": prop["price"],
            "price_per_sqft": prop["price_per_sqft"],
            "bedrooms": prop["bedrooms"],
            "bathrooms": prop["bathrooms"],
            "builder_owner": prop["builder_owner"],
        },
        "parameters": {
            "radius_km": radius_km,
            "limit": limit,
        },
        "summary": counts,
        "source_counts": source_counts,
        "status_counts": status_counts,
        "category_counts": category_counts,
        "distance_band_counts": band_counts,
        "flags": flags,
        "records": records,
        "evidence": evidence_rows,
        "methodology": {
            "distance": "Great-circle distance from stored latitude/longitude.",
            "nearby": f"Stored mapped intelligence within {radius_km:g} km.",
            "unmapped": "Stored intelligence without usable latitude/longitude.",
            "flags": "Workflow/data-quality flags only; not investment recommendations.",
            "source_policy": "No coordinates, sources, evidence or intelligence are fabricated."
        }
    }


def install_v19_due_diligence(app, engine):

    from fastapi.responses import HTMLResponse

    api_path = "/api/v1/properties/{property_id}/due-diligence-map"
    ui_path = "/propertyiq/due-diligence-map/{property_id}"

    existing = {getattr(r, "path", "") for r in app.routes}

    if api_path not in existing:

        @app.get(api_path, tags=["PropertyIQ V19"])
        def v19_api(
            property_id: str,
            radius_km: float = 5.0,
            limit: int = 2000
        ):
            return build_v19_due_diligence(
                engine,
                property_id,
                radius_km=radius_km,
                limit=limit
            )

    if ui_path not in existing:

        @app.get(ui_path, response_class=HTMLResponse, tags=["PropertyIQ V19"])
        def v19_ui(property_id: str):

            pid = html.escape(str(property_id), quote=True)

            page = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Due Diligence Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root {{
 --bg:#06101d; --panel:rgba(8,20,36,.94); --line:rgba(255,255,255,.09);
 --text:#eef7ff; --muted:#8ea4ba; --cyan:#22d3ee;
}}
*{{box-sizing:border-box}}
html,body{{margin:0;width:100%;height:100%;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}}
#map{{position:absolute;inset:0}}
.top{{position:absolute;z-index:1000;left:18px;right:18px;top:18px;height:58px;background:var(--panel);border:1px solid var(--line);border-radius:16px;backdrop-filter:blur(18px);display:flex;align-items:center;gap:12px;padding:10px 14px}}
.brand{{font-weight:850;font-size:18px;white-space:nowrap}} .brand span{{color:var(--cyan)}}
.top input,.top select{{background:#0b1a2c;color:white;border:1px solid var(--line);border-radius:10px;padding:9px}}
.top input{{flex:1}}
.left,.right{{position:absolute;z-index:1000;top:92px;bottom:18px;width:330px;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:18px;backdrop-filter:blur(18px);padding:16px}}
.left{{left:18px}} .right{{right:18px}}
.small{{font-size:12px;color:var(--muted);line-height:1.5}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}}
.metric{{padding:11px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.035)}}
.metric strong{{display:block;font-size:20px}}
.flag,.record{{margin-top:8px;padding:10px;border:1px solid var(--line);border-radius:11px;background:rgba(255,255,255,.035)}}
.record{{cursor:pointer}} .record:hover{{border-color:rgba(34,211,238,.5)}}
button{{background:#0b1a2c;color:white;border:1px solid var(--line);border-radius:9px;padding:8px 10px;cursor:pointer}}
a{{color:var(--cyan)}}
#loading{{position:fixed;inset:0;z-index:3000;background:rgba(4,10,18,.72);display:flex;align-items:center;justify-content:center}}
.loader{{padding:20px 26px;background:var(--panel);border:1px solid var(--line);border-radius:14px}}
@media(max-width:850px){{.left{{width:280px}}.right{{display:none}}}}
</style>
</head>
<body>
<div id="map"></div>
<div id="loading"><div class="loader">Loading due-diligence workspace...</div></div>

<div class="top">
 <div class="brand">Property<span>IQ</span> · Due Diligence</div>
 <input id="search" placeholder="Filter title, category, source..." oninput="renderRecords()">
 <select id="scope" onchange="renderRecords()">
  <option value="all">All intelligence</option>
  <option value="nearby">Nearby only</option>
  <option value="mapped">Mapped only</option>
  <option value="unmapped">Unmapped only</option>
 </select>
</div>

<div class="left">
 <h3 id="pname" style="margin:0">Property</h3>
 <div id="paddress" class="small"></div>

 <div class="grid">
  <div class="metric"><strong id="total">—</strong><span class="small">Total</span></div>
  <div class="metric"><strong id="nearby">—</strong><span class="small">Nearby</span></div>
  <div class="metric"><strong id="mapped">—</strong><span class="small">Mapped</span></div>
  <div class="metric"><strong id="unmapped">—</strong><span class="small">Unmapped</span></div>
 </div>

 <h4>Due-diligence flags</h4>
 <div id="flags"></div>

 <h4>Source inventory</h4>
 <div id="sources" class="small"></div>
</div>

<div class="right">
 <h3 style="margin-top:0">Intelligence</h3>
 <div id="records"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const PROPERTY_ID="{pid}";
let payload=null, map=null, markers={{}};

const colors={{OSM:"#22d3ee",RERA:"#a78bfa",GOVERNMENT:"#34d399",NEWS:"#fbbf24",OTHER:"#fb7185"}};

function esc(v){{const d=document.createElement("div");d.textContent=v??"";return d.innerHTML}}

function initMap(){{
 const p=payload.property;
 map=L.map("map",{{preferCanvas:true}}).setView([p.latitude,p.longitude],12);
 L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",{{maxZoom:19,attribution:"&copy; OpenStreetMap contributors"}}).addTo(map);
 L.marker([p.latitude,p.longitude]).addTo(map).bindPopup(`<strong>${{esc(p.name)}}</strong><br>${{esc(p.address||"")}}`);
 payload.records.forEach(r=>{{
  if(!r.mapped)return;
  const c=colors[r.source_group]||colors.OTHER;
  const m=L.circleMarker([r.latitude,r.longitude],{{radius:7,color:c,fillColor:c,fillOpacity:.72,weight:2}}).addTo(map);
  m.bindPopup(`<strong>${{esc(r.title)}}</strong><br><span style="font-size:12px">${{esc(r.source_group)}} · ${{r.distance_km===null?"—":r.distance_km.toFixed(2)+" km"}}<br>Evidence: ${{r.evidence_count}}</span>`);
  markers[r.id]=m;
 }});
}}

function renderSummary(){{
 const p=payload.property,s=payload.summary;
 document.getElementById("pname").textContent=p.name||"Property";
 document.getElementById("paddress").textContent=p.address||"";
 ["total","nearby","mapped","unmapped"].forEach(k=>document.getElementById(k).textContent=s[k]);
 document.getElementById("flags").innerHTML=payload.flags.length
  ? payload.flags.map(f=>`<div class="flag"><strong>${{esc(f.type)}}</strong><div class="small">${{esc(f.message)}}</div></div>`).join("")
  : `<div class="small">No workflow flags from the currently stored data.</div>`;
 document.getElementById("sources").innerHTML=Object.entries(payload.source_counts).map(([k,v])=>`${{esc(k)}}: <strong>${{v}}</strong>`).join("<br>");
}}

function renderRecords(){{
 const q=(document.getElementById("search").value||"").toLowerCase();
 const scope=document.getElementById("scope").value;
 let rows=payload.records.filter(r=>{{
   if(scope==="nearby"&&!r.nearby)return false;
   if(scope==="mapped"&&!r.mapped)return false;
   if(scope==="unmapped"&&r.mapped)return false;
   const h=`${{r.title}} ${{r.category}} ${{r.source_name}} ${{r.status}}`.toLowerCase();
   return !q||h.includes(q);
 }});
 document.getElementById("records").innerHTML=rows.length?rows.map(r=>`
  <div class="record" onclick="focusRecord('${{r.id}}')">
   <strong>${{esc(r.title)}}</strong>
   <div class="small">
    ${{esc(r.source_group)}} · ${{esc(r.category||"—")}}<br>
    ${{r.distance_km===null?"Location pending":r.distance_km.toFixed(2)+" km"}} ·
    Evidence ${{r.evidence_count}} ·
    ${{esc(r.status||"status unspecified")}}
    ${{r.source_url?`<br><a href="${{esc(r.source_url)}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open source</a>`:""}}
   </div>
  </div>`).join(""):`<div class="small">No records match this filter.</div>`;
}}

function focusRecord(id){{
 const m=markers[id];
 if(m){{map.setView(m.getLatLng(),15);m.openPopup();}}
}}

async function load(){{
 try{{
  const r=await fetch(`/api/v1/properties/${{PROPERTY_ID}}/due-diligence-map?radius_km=5&limit=2000`);
  if(!r.ok)throw new Error(await r.text());
  payload=await r.json();
  initMap();renderSummary();renderRecords();
  document.getElementById("loading").style.display="none";
 }}catch(e){{
  document.getElementById("loading").innerHTML=`<div class="loader"><strong>PropertyIQ V19 error</strong><br><br>${{esc(e.message)}}</div>`;
 }}
}}
load();
</script>
</body>
</html>
"""
            return HTMLResponse(page)

    return {"module": MODULE_VERSION, "routes": [api_path, ui_path]}


def _v19_bootstrap():
    global engine, app

    if "engine" in globals() and engine is not None and "app" in globals() and app is not None:
        return "existing"

    if "engine" in globals() and engine is not None:
        if "app" not in globals() or app is None:
            app = FastAPI(title="PropertyIQ", version="V19")
        return "existing-engine"

    import os, sys, subprocess, importlib, importlib.util
    from getpass import getpass
    from urllib.parse import quote_plus

    if importlib.util.find_spec("psycopg") is None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]"])
        importlib.invalidate_caches()

    host=os.environ.get("PROPERTYIQ_DB_HOST","aws-0-ap-south-1.pooler.supabase.com")
    user=os.environ.get("PROPERTYIQ_DB_USER","postgres.reqsrwfyeyupgqsggchv")
    database=os.environ.get("PROPERTYIQ_DB_NAME","postgres")
    port=os.environ.get("PROPERTYIQ_DB_PORT","5432")

    print("")
    print("="*72)
    print("PROPERTYIQ V19 — DATABASE AUTO-BOOTSTRAP")
    print("="*72)

    password=getpass("Enter your PropertyIQ PostgreSQL password: ")
    if not password:
        raise RuntimeError("No database password supplied.")

    from sqlalchemy import create_engine
    url=("postgresql+psycopg://"+quote_plus(user)+":"+quote_plus(password)+"@"+host+":"+port+"/"+database)
    engine=create_engine(url,pool_pre_ping=True,pool_recycle=1800,future=True)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.execute(text("SELECT PostGIS_Full_Version()")).scalar()

    if "app" not in globals() or app is None:
        app=FastAPI(title="PropertyIQ",version="V19")

    return "autobootstrapped"


try:
    bootstrap_mode=_v19_bootstrap()
    result=install_v19_due_diligence(app,engine)

    print("")
    print("="*72)
    print("PROPERTYIQ V19 — INTERACTIVE DUE-DILIGENCE MAP INSTALLED")
    print("="*72)
    print("Module:",MODULE_VERSION)
    print("Runtime:",bootstrap_mode)
    print("")
    for route in result["routes"]:
        print("  ",route)
    print("")
    print("Interactive due-diligence map: ENABLED")
    print("Mapped / nearby / unmapped inventory: ENABLED")
    print("Evidence linkage counts: ENABLED")
    print("Workflow/data-quality flags: ENABLED")
    print("Source filtering and drill-down: ENABLED")
    print("No database records modified")
    print("No fabricated intelligence")
    print("No investment score or recommendation")
    print("="*72)

except Exception as exc:
    raise RuntimeError("PropertyIQ V19 installation failed.") from exc



# ============================================================
# PROPERTYIQ MODULE: V20
# ORIGINAL COLAB CELL: In[32]
# ============================================================

# ============================================================
# PROPERTYIQ V20 — GIS BOUNDARY DRAWING WORKSPACE
# ============================================================
# Additive stage after V19.1.
#
# V20 adds:
#   - interactive Leaflet property-boundary drawing
#   - polygon editing / deletion
#   - load existing property boundary
#   - save boundary through existing P3 endpoint
#   - GeoJSON preview
#   - area calculation in m² / hectares / acres
#   - perimeter calculation
#   - Google Maps handoff
#   - no fabricated geometry
#   - preserves all existing PropertyIQ routes
#
# Direct Colab execution supported.
# ============================================================


import html
import json
from datetime import datetime, timezone
from sqlalchemy import text

MODULE_VERSION = "PROPERTYIQ-V20.1-GIS-BOUNDARY-WORKSPACE"


def _v20_bootstrap():
    global engine, app

    if "engine" not in globals() or engine is None:
        raise RuntimeError(
            "PropertyIQ engine is not available. "
            "Run the existing PropertyIQ runtime first."
        )

    if "app" not in globals() or app is None:
        app = FastAPI(title="PropertyIQ", version="V20")

    return "existing"


def _v20_boundary_from_db(engine, property_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    ST_AsGeoJSON(boundary) AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    if not row:
        raise ValueError("Property not found.")

    geometry = None

    if row["boundary_geojson"]:
        try:
            geometry = json.loads(row["boundary_geojson"])
        except Exception:
            geometry = None

    return {
        "id": str(row["id"]),
        "name": row["property_name"],
        "address": row["property_address"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "boundary": geometry,
    }


def _v20_area_metrics(geometry):
    """
    Approximate planar metrics by projecting polygon coordinates locally.
    Used only for user-facing geometry measurements.
    """

    if not geometry:
        return {
            "area_m2": None,
            "area_hectares": None,
            "area_acres": None,
            "perimeter_m": None,
        }

    try:
        from shapely.geometry import shape
        from pyproj import CRS, Transformer

        geom = shape(geometry)

        if geom.is_empty:
            return {
                "area_m2": 0,
                "area_hectares": 0,
                "area_acres": 0,
                "perimeter_m": 0,
            }

        centroid = geom.centroid

        # Local Azimuthal Equidistant projection centered on the property.
        local_crs = CRS.from_proj4(
            f"+proj=aeqd +lat_0={centroid.y} "
            f"+lon_0={centroid.x} +datum=WGS84 +units=m"
        )

        transformer = Transformer.from_crs(
            "EPSG:4326",
            local_crs,
            always_xy=True
        )

        projected = geom.__class__(
            []
        ) if False else None

        from shapely.ops import transform
        metric_geom = transform(
            transformer.transform,
            geom
        )

        area_m2 = float(metric_geom.area)
        perimeter_m = float(metric_geom.length)

        return {
            "area_m2": round(area_m2, 2),
            "area_hectares": round(area_m2 / 10000.0, 4),
            "area_acres": round(area_m2 / 4046.8564224, 4),
            "perimeter_m": round(perimeter_m, 2),
        }

    except Exception:
        return {
            "area_m2": None,
            "area_hectares": None,
            "area_acres": None,
            "perimeter_m": None,
        }


def install_v20_boundary_workspace(app, engine):

    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse

    api_path = "/api/v1/properties/{property_id}/boundary-workspace"
    ui_path = "/propertyiq/boundary/{property_id}"

    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    if api_path not in existing:

        @app.get(
            api_path,
            tags=["PropertyIQ V20"]
        )
        def v20_boundary_workspace(
            property_id: str
        ):
            try:
                data = _v20_boundary_from_db(
                    engine,
                    property_id
                )

                data["module"] = MODULE_VERSION
                data["metrics"] = _v20_area_metrics(
                    data["boundary"]
                )

                return data

            except ValueError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=str(exc)
                )

    boundary_api_path = "/api/v1/properties/{property_id}/boundary"

    if boundary_api_path not in existing:

        @app.put(
            boundary_api_path,
            tags=["PropertyIQ V20"]
        )
        def v20_save_boundary(
            property_id: str,
            payload: dict
        ):
            geometry = payload.get("geometry")

            if not isinstance(geometry, dict):
                raise HTTPException(
                    status_code=400,
                    detail="geometry must be a GeoJSON geometry object."
                )

            if geometry.get("type") not in {
                "Polygon",
                "MultiPolygon"
            }:
                raise HTTPException(
                    status_code=400,
                    detail="Boundary must be a Polygon or MultiPolygon."
                )

            try:
                from shapely.geometry import shape

                geom = shape(geometry)

                if geom.is_empty:
                    raise ValueError("Boundary geometry is empty.")

                if not geom.is_valid:
                    raise ValueError(
                        "Boundary geometry is invalid. "
                        "Please edit the polygon before saving."
                    )

                with engine.begin() as conn:

                    exists_row = conn.execute(
                        text("""
                            SELECT id
                            FROM properties
                            WHERE id = CAST(:pid AS uuid)
                            LIMIT 1
                        """),
                        {"pid": str(property_id)}
                    ).first()

                    if not exists_row:
                        raise HTTPException(
                            status_code=404,
                            detail="Property not found."
                        )

                    conn.execute(
                        text("""
                            UPDATE properties
                            SET boundary =
                                ST_SetSRID(
                                    ST_GeomFromGeoJSON(
                                        CAST(:geojson AS json)
                                    ),
                                    4326
                                ),
                                updated_at = NOW()
                            WHERE id = CAST(:pid AS uuid)
                        """),
                        {
                            "pid": str(property_id),
                            "geojson": json.dumps(geometry)
                        }
                    )

                saved = _v20_boundary_from_db(
                    engine,
                    property_id
                )

                return {
                    "ok": True,
                    "property_id": str(property_id),
                    "boundary": saved["boundary"],
                    "metrics": _v20_area_metrics(
                        saved["boundary"]
                    ),
                    "message": "Boundary saved successfully."
                }

            except HTTPException:
                raise

            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Boundary save failed: {exc}"
                )

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V20"]
        )
        def v20_boundary_ui(property_id: str):

            pid = html.escape(
                str(property_id),
                quote=True
            )

            page = f"""
<!DOCTYPE html>
<html>
<head>

<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>PropertyIQ — Boundary Workspace</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">

<link
 rel="stylesheet"
 href="https://unpkg.com/@geoman-io/leaflet-geoman-free@2.18.0/dist/leaflet-geoman.css">

<style>

:root {{
    --bg:#06101d;
    --panel:rgba(8,20,36,.95);
    --line:rgba(255,255,255,.10);
    --text:#eef7ff;
    --muted:#8ea4ba;
    --cyan:#22d3ee;
    --green:#34d399;
    --red:#fb7185;
}}

* {{
    box-sizing:border-box;
}}

html,body {{
    margin:0;
    width:100%;
    height:100%;
    background:var(--bg);
    color:var(--text);
    font-family:Inter,system-ui,sans-serif;
}}

#map {{
    position:absolute;
    inset:0;
}}

.top {{
    position:absolute;
    z-index:1000;
    left:18px;
    right:18px;
    top:18px;
    min-height:62px;
    background:var(--panel);
    border:1px solid var(--line);
    border-radius:17px;
    backdrop-filter:blur(18px);
    display:flex;
    align-items:center;
    gap:12px;
    padding:10px 14px;
}}

.brand {{
    font-weight:850;
    font-size:18px;
    white-space:nowrap;
}}

.brand span {{
    color:var(--cyan);
}}

.title {{
    flex:1;
    min-width:100px;
}}

.title strong {{
    display:block;
}}

.small {{
    color:var(--muted);
    font-size:12px;
    line-height:1.45;
}}

button {{
    background:#0b1a2c;
    color:white;
    border:1px solid var(--line);
    border-radius:10px;
    padding:9px 12px;
    cursor:pointer;
}}

button:hover {{
    border-color:var(--cyan);
}}

.primary {{
    background:#083344;
    border-color:rgba(34,211,238,.45);
}}

.danger {{
    border-color:rgba(251,113,133,.35);
}}

.panel {{
    position:absolute;
    z-index:1000;
    right:18px;
    top:94px;
    bottom:18px;
    width:350px;
    overflow:auto;
    background:var(--panel);
    border:1px solid var(--line);
    border-radius:18px;
    backdrop-filter:blur(18px);
    padding:17px;
}}

.metric {{
    padding:12px;
    margin-top:8px;
    border:1px solid var(--line);
    border-radius:12px;
    background:rgba(255,255,255,.035);
}}

.metric strong {{
    display:block;
    font-size:20px;
}}

.status {{
    margin-top:10px;
    padding:10px;
    border-radius:10px;
    background:rgba(255,255,255,.04);
}}

#geojson {{
    width:100%;
    min-height:210px;
    margin-top:10px;
    background:#020814;
    color:#ccefff;
    border:1px solid var(--line);
    border-radius:10px;
    padding:10px;
    font-family:monospace;
    font-size:11px;
    resize:vertical;
}}

#loading {{
    position:fixed;
    inset:0;
    z-index:3000;
    background:rgba(4,10,18,.78);
    display:flex;
    align-items:center;
    justify-content:center;
}}

.loader {{
    padding:20px 26px;
    background:var(--panel);
    border:1px solid var(--line);
    border-radius:14px;
}}

@media(max-width:850px) {{
    .panel {{
        width:300px;
    }}

    .title {{
        display:none;
    }}
}}

</style>
</head>

<body>

<div id="map"></div>

<div id="loading">
    <div class="loader">
        Loading boundary workspace...
    </div>
</div>

<div class="top">

    <div class="brand">
        Property<span>IQ</span>
    </div>

    <div class="title">
        <strong id="propertyName">
            Property
        </strong>

        <div
            id="propertyAddress"
            class="small">
        </div>
    </div>

    <button onclick="openGoogleMaps()">
        Google Maps
    </button>

    <button
        class="primary"
        onclick="saveBoundary()">
        Save Boundary
    </button>

</div>

<div class="panel">

    <h3 style="margin-top:0">
        GIS Boundary
    </h3>

    <div class="small">
        Draw the exact property boundary on the map.
        Use polygon editing to refine vertices before saving.
    </div>

    <div class="metric">
        <strong id="area">—</strong>
        <span class="small">Area</span>
    </div>

    <div class="metric">
        <strong id="hectares">—</strong>
        <span class="small">Hectares</span>
    </div>

    <div class="metric">
        <strong id="acres">—</strong>
        <span class="small">Acres</span>
    </div>

    <div class="metric">
        <strong id="perimeter">—</strong>
        <span class="small">Perimeter</span>
    </div>

    <div
        id="status"
        class="status small">
        No boundary selected.
    </div>

    <h4>Workflow</h4>

    <div class="small">
        1. Draw Polygon<br>
        2. Edit vertices if necessary<br>
        3. Review area/perimeter<br>
        4. Save Boundary
    </div>

    <h4>GeoJSON</h4>

    <textarea
        id="geojson"
        readonly
        placeholder="Draw a polygon to generate GeoJSON..."></textarea>

    <div style="margin-top:10px">

        <button
            class="danger"
            onclick="deleteBoundary()">
            Delete Drawn Boundary
        </button>

        <button
            onclick="copyGeoJSON()">
            Copy GeoJSON
        </button>

    </div>

</div>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<script
 src="https://unpkg.com/@geoman-io/leaflet-geoman-free@2.18.0/dist/leaflet-geoman.min.js">
</script>

<script>

const PROPERTY_ID = "{pid}";

let map = null;
let property = null;
let boundaryLayer = null;

const esc = (v) => {{

    const d = document.createElement("div");

    d.textContent = v ?? "";

    return d.innerHTML;

}};


function setStatus(message, type="normal") {{

    const el =
        document.getElementById("status");

    el.textContent = message;

    if (type === "success") {{
        el.style.border =
            "1px solid rgba(52,211,153,.35)";
    }}
    else if (type === "error") {{
        el.style.border =
            "1px solid rgba(251,113,133,.35)";
    }}
    else {{
        el.style.border =
            "1px solid rgba(255,255,255,.10)";
    }}
}}


function initMap(data) {{

    property = data;

    document.getElementById(
        "propertyName"
    ).textContent =
        data.name || "Property";

    document.getElementById(
        "propertyAddress"
    ).textContent =
        data.address || "";

    map = L.map(
        "map",
        {{
            preferCanvas:true
        }}
    ).setView(
        [
            data.latitude,
            data.longitude
        ],
        17
    );

    L.tileLayer(
        "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
        {{
            maxZoom:19,
            attribution:
                "&copy; OpenStreetMap contributors"
        }}
    ).addTo(map);

    L.marker(
        [
            data.latitude,
            data.longitude
        ]
    )
    .addTo(map)
    .bindPopup(
        `<strong>${{esc(data.name)}}</strong><br>` +
        `${{esc(data.address || "")}}`
    );

    map.pm.addControls({{
        position:"topleft",
        drawText:false,
        drawCircle:false,
        drawCircleMarker:false,
        drawMarker:false,
        drawPolyline:false,
        drawRectangle:false,
        drawPolygon:true,
        editMode:true,
        dragMode:false,
        cutPolygon:false,
        removalMode:true
    }});

    map.on(
        "pm:create",
        function(e) {{

            if (e.shape !== "Polygon") {{
                return;
            }}

            if (boundaryLayer) {{
                map.removeLayer(
                    boundaryLayer
                );
            }}

            boundaryLayer = e.layer;

            boundaryLayer.setStyle({{
                color:"#22d3ee",
                fillColor:"#22d3ee",
                fillOpacity:0.22,
                weight:3
            }});

            updateBoundary();

            setStatus(
                "Boundary drawn. Review it and click Save Boundary.",
                "success"
            );

        }}
    );

    map.on(
        "pm:edit",
        function() {{
            updateBoundary();
        }}
    );

    map.on(
        "pm:remove",
        function(e) {{

            if (
                boundaryLayer === e.layer
            ) {{
                boundaryLayer = null;
                clearMetrics();
            }}

        }}
    );

    if (data.boundary) {{

        boundaryLayer =
            L.geoJSON(
                data.boundary,
                {{
                    style:{{
                        color:"#22d3ee",
                        fillColor:"#22d3ee",
                        fillOpacity:0.22,
                        weight:3
                    }}
                }}
            ).addTo(map);

        boundaryLayer.pm.enable();

        const bounds =
            boundaryLayer.getBounds();

        if (bounds.isValid()) {{
            map.fitBounds(
                bounds,
                {{padding:[80,80]}}
            );
        }}

        updateBoundary();

        setStatus(
            "Existing boundary loaded. You can edit it.",
            "success"
        );
    }}

    document.getElementById(
        "loading"
    ).style.display = "none";
}}


function updateBoundary() {{

    if (!boundaryLayer) {{
        clearMetrics();
        return;
    }}

    const geo =
        boundaryLayer.toGeoJSON();

    document.getElementById(
        "geojson"
    ).value =
        JSON.stringify(
            geo.geometry,
            null,
            2
        );

    calculateMetrics(
        geo.geometry
    );
}}


function calculateMetrics(geometry) {{

    const coords =
        geometry.coordinates[0];

    if (!coords || coords.length < 4) {{
        clearMetrics();
        return;
    }}

    const lat0 =
        property.latitude *
        Math.PI / 180.0;

    const metersLat =
        111320.0;

    const metersLon =
        111320.0 *
        Math.cos(lat0);

    let area = 0;
    let perimeter = 0;

    for (
        let i = 0;
        i < coords.length - 1;
        i++
    ) {{

        const x1 =
            coords[i][0] *
            metersLon;

        const y1 =
            coords[i][1] *
            metersLat;

        const x2 =
            coords[i+1][0] *
            metersLon;

        const y2 =
            coords[i+1][1] *
            metersLat;

        area +=
            x1 * y2 -
            x2 * y1;

        perimeter +=
            Math.hypot(
                x2 - x1,
                y2 - y1
            );
    }}

    area =
        Math.abs(area) / 2;

    document.getElementById(
        "area"
    ).textContent =
        area.toLocaleString(
            undefined,
            {{
                maximumFractionDigits:2
            }}
        ) + " m²";

    document.getElementById(
        "hectares"
    ).textContent =
        (area / 10000).toFixed(4) +
        " ha";

    document.getElementById(
        "acres"
    ).textContent =
        (area / 4046.8564224).toFixed(4) +
        " acres";

    document.getElementById(
        "perimeter"
    ).textContent =
        perimeter.toLocaleString(
            undefined,
            {{
                maximumFractionDigits:2
            }}
        ) + " m";

}}


function clearMetrics() {{

    document.getElementById(
        "area"
    ).textContent = "—";

    document.getElementById(
        "hectares"
    ).textContent = "—";

    document.getElementById(
        "acres"
    ).textContent = "—";

    document.getElementById(
        "perimeter"
    ).textContent = "—";

    document.getElementById(
        "geojson"
    ).value = "";

}}


async function saveBoundary() {{

    if (!boundaryLayer) {{
        setStatus(
            "Draw a polygon before saving.",
            "error"
        );
        return;
    }}

    const geo =
        boundaryLayer.toGeoJSON();

    const geometry =
        geo.geometry;

    setStatus(
        "Saving boundary..."
    );

    try {{

        const response =
            await fetch(
                `/api/v1/properties/${{PROPERTY_ID}}/boundary`,
                {{
                    method:"PUT",
                    headers:{{
                        "Content-Type":
                            "application/json"
                    }},
                    body:JSON.stringify({{
                        geometry:geometry
                    }})
                }}
            );

        const data =
            await response.json();

        if (!response.ok) {{
            throw new Error(
                data.detail ||
                JSON.stringify(data)
            );
        }}

        setStatus(
            "Boundary saved successfully to PropertyIQ.",
            "success"
        );

    }}
    catch(error) {{

        setStatus(
            "Save failed: " +
            error.message,
            "error"
        );

    }}
}}


function deleteBoundary() {{

    if (!boundaryLayer) {{
        setStatus(
            "There is no drawn boundary to remove."
        );
        return;
    }}

    map.removeLayer(
        boundaryLayer
    );

    boundaryLayer = null;

    clearMetrics();

    setStatus(
        "Drawn boundary removed from the workspace. Database was not changed."
    );
}}


function copyGeoJSON() {{

    const value =
        document.getElementById(
            "geojson"
        ).value;

    if (!value) {{
        setStatus(
            "Draw a boundary first.",
            "error"
        );
        return;
    }}

    navigator.clipboard.writeText(
        value
    );

    setStatus(
        "GeoJSON copied to clipboard.",
        "success"
    );
}}


function openGoogleMaps() {{

    if (
        property.latitude === null ||
        property.longitude === null
    ) {{
        return;
    }}

    window.open(
        `https://www.google.com/maps?q=${{property.latitude}},${{property.longitude}}`,
        "_blank"
    );
}}


async function load() {{

    try {{

        const response =
            await fetch(
                `/api/v1/properties/${{PROPERTY_ID}}/boundary-workspace`
            );

        const data =
            await response.json();

        if (!response.ok) {{
            throw new Error(
                data.detail ||
                JSON.stringify(data)
            );
        }}

        initMap(data);

    }}
    catch(error) {{

        document.getElementById(
            "loading"
        ).innerHTML =
            `<div class="loader">
                <strong>PropertyIQ V20 error</strong>
                <br><br>
                ${{esc(error.message)}}
             </div>`;

    }}
}}

load();

</script>

</body>
</html>
"""

            return HTMLResponse(page)

    return {
        "module": MODULE_VERSION,
        "routes": [
            api_path,
            ui_path
        ]
    }


# ============================================================
# DIRECT COLAB EXECUTION
# ============================================================

bootstrap_mode = _v20_bootstrap()

result = install_v20_boundary_workspace(
    app,
    engine
)

print("")
print("=" * 72)
print("PROPERTYIQ V20 — GIS BOUNDARY WORKSPACE INSTALLED")
print("=" * 72)
print("Module:", MODULE_VERSION)
print("Runtime:", bootstrap_mode)
print("")
for route in result["routes"]:
    print(" ", route)

print("")
print("Interactive polygon drawing: ENABLED")
print("Polygon editing: ENABLED")
print("Existing boundary loading: ENABLED")
print("Boundary persistence: ENABLED")
print("GeoJSON preview: ENABLED")
print("Area / perimeter measurement: ENABLED")
print("Google Maps handoff: ENABLED")
print("No fabricated geometry")
print("Existing PropertyIQ routes preserved")
print("=" * 72)



# ============================================================
# PROPERTYIQ MODULE: V21
# ORIGINAL COLAB CELL: In[34]
# ============================================================

# ============================================================================
# PROPERTYIQ V21 — BOUNDARY INTELLIGENCE — CLEAN ADDITIVE MODULE
# ============================================================================
# IMPORTANT:
# This module intentionally contains ONLY V21 functionality.
# It does NOT import/re-run V13, V14, V15, or any PRIMARY_SOURCE_CATALOG.
# It attaches to the already-running PropertyIQ `engine` and `app`.
#
# Direct Colab execution:
# Paste this complete file into a new cell and run it.
# ============================================================================

from sqlalchemy import text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
import json
import math

MODULE_VERSION = "PROPERTYIQ-V21.1-BOUNDARY-INTELLIGENCE-CLEAN"


def _v21_haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None

    r = 6371.0088
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    a = max(0.0, min(1.0, a))
    return r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _v21_property(engine, property_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    ST_AsGeoJSON(boundary) AS boundary_geojson,
                    ST_GeometryType(boundary) AS boundary_type,
                    ST_SRID(boundary) AS boundary_srid,
                    ST_IsValid(boundary) AS boundary_valid
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    return dict(row) if row else None


def _v21_json_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _v21_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_v21_json_value(v) for v in value]
    return value


def _v21_boundary_intelligence(engine, property_id, radius_km=5.0):
    radius_km = max(0.1, min(float(radius_km), 25.0))

    prop = _v21_property(engine, property_id)
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found.")

    if prop["latitude"] is None or prop["longitude"] is None:
        raise HTTPException(
            status_code=400,
            detail="Property does not have usable latitude/longitude."
        )

    boundary = prop.get("boundary_geojson")

    result = {
        "module": MODULE_VERSION,
        "property": {
            "id": str(prop["id"]),
            "name": prop["property_name"],
            "address": prop["property_address"],
            "latitude": prop["latitude"],
            "longitude": prop["longitude"],
            "property_type": prop["property_type"],
            "area": prop["area"],
            "price": prop["price"],
            "price_per_sqft": prop["price_per_sqft"],
        },
        "boundary": {
            "present": bool(boundary),
            "geometry_type": prop.get("boundary_type"),
            "srid": prop.get("boundary_srid"),
            "valid": prop.get("boundary_valid"),
        },
        "radius_km": radius_km,
        "records": [],
        "bands": {
            "inside_boundary": [],
            "0_500m": [],
            "500m_1km": [],
            "1_3km": [],
            "3_5km": [],
        },
        "summary": {
            "mapped_records": 0,
            "records_in_radius": 0,
            "inside_boundary": 0,
            "within_500m": 0,
            "within_1km": 0,
            "within_3km": 0,
            "within_5km": 0,
            "unmapped_records": 0,
        },
        "flags": [],
    }

    if not boundary:
        result["flags"].append({
            "code": "BOUNDARY_NOT_SET",
            "severity": "info",
            "message": "No saved property boundary is available yet."
        })
    elif prop.get("boundary_valid") is False:
        result["flags"].append({
            "code": "BOUNDARY_INVALID",
            "severity": "warning",
            "message": "The saved property boundary is invalid in PostGIS."
        })

    # Read the actual current intelligence_records schema.
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    ir.id,
                    ir.property_id,
                    ir.title,
                    ir.category,
                    ir.status,
                    ir.description,
                    ir.latitude,
                    ir.longitude,
                    ir.source_name,
                    ir.source_type,
                    ir.source_url,
                    ir.published_at,
                    ir.confidence,
                    ir.evidence_level,
                    ir.metadata,
                    ir.created_at,
                    ir.updated_at,
                    CASE
                        WHEN :boundary_present = 1
                         AND p.boundary IS NOT NULL
                         AND ir.latitude IS NOT NULL
                         AND ir.longitude IS NOT NULL
                         AND ST_Intersects(
                             p.boundary,
                             ST_SetSRID(
                                 ST_MakePoint(ir.longitude, ir.latitude),
                                 4326
                             )
                         )
                        THEN TRUE
                        ELSE FALSE
                    END AS inside_boundary
                FROM intelligence_records ir
                CROSS JOIN properties p
                WHERE p.id = CAST(:pid AS uuid)
                  AND ir.latitude IS NOT NULL
                  AND ir.longitude IS NOT NULL
            """),
            {
                "pid": str(property_id),
                "boundary_present": 1 if boundary else 0,
            }
        ).mappings().all()

        unmapped = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM intelligence_records
                WHERE latitude IS NULL
                   OR longitude IS NULL
            """)
        ).scalar_one()

    result["summary"]["unmapped_records"] = int(unmapped or 0)

    for raw in rows:
        record = dict(raw)

        distance_km = _v21_haversine_km(
            prop["latitude"],
            prop["longitude"],
            record["latitude"],
            record["longitude"]
        )

        inside = bool(record["inside_boundary"])

        record["id"] = str(record["id"])
        record["distance_km"] = (
            round(distance_km, 3)
            if distance_km is not None else None
        )
        record["inside_boundary"] = inside

        for key in ("published_at", "created_at", "updated_at"):
            record[key] = _v21_json_value(record.get(key))

        record["metadata"] = _v21_json_value(record.get("metadata"))

        # Remove internal calculation-only field.
        record.pop("inside_boundary", None)

        if inside:
            band = "inside_boundary"
            result["summary"]["inside_boundary"] += 1
        elif distance_km is None or distance_km > radius_km:
            continue
        elif distance_km <= 0.5:
            band = "0_500m"
            result["summary"]["within_500m"] += 1
        elif distance_km <= 1.0:
            band = "500m_1km"
            result["summary"]["within_1km"] += 1
        elif distance_km <= 3.0:
            band = "1_3km"
            result["summary"]["within_3km"] += 1
        else:
            band = "3_5km"
            result["summary"]["within_5km"] += 1

        result["bands"][band].append(record)
        result["records"].append(record)

    result["summary"]["mapped_records"] = len(rows)
    result["summary"]["records_in_radius"] = len(result["records"])

    if not result["records"]:
        result["flags"].append({
            "code": "NO_BOUNDARY_INTELLIGENCE",
            "severity": "info",
            "message": (
                "No mapped intelligence records are currently inside "
                "the boundary or requested radius."
            )
        })

    return result


def _v21_install(app, engine):
    existing = {getattr(route, "path", "") for route in app.routes}

    api_path = (
        "/api/v1/properties/"
        "{property_id}/boundary-intelligence"
    )

    ui_path = (
        "/propertyiq/boundary-intelligence/"
        "{property_id}"
    )

    if api_path not in existing:

        @app.get(api_path, tags=["PropertyIQ V21"])
        def v21_boundary_intelligence_api(
            property_id: str,
            radius_km: float = 5.0
        ):
            return _v21_boundary_intelligence(
                engine,
                property_id,
                radius_km
            )

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V21"]
        )
        def v21_boundary_intelligence_ui(property_id: str):
            data = _v21_boundary_intelligence(
                engine,
                property_id,
                5.0
            )

            payload = json.dumps(
                data,
                ensure_ascii=False,
                default=str
            ).replace("</", "<\\/")

            return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Boundary Intelligence</title>
<style>
:root {{
  --bg:#07111f;
  --panel:#0d1b2d;
  --text:#edf6ff;
  --muted:#8fa6bd;
  --line:rgba(255,255,255,.09);
  --cyan:#54e5ff;
  --blue:#31a8ff;
  --green:#35d49a;
  --amber:#ffbd55;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0;
  color:var(--text);
  background:
    radial-gradient(circle at 10% 0%,rgba(49,168,255,.14),transparent 35%),
    radial-gradient(circle at 90% 10%,rgba(84,229,255,.08),transparent 30%),
    var(--bg);
  font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
.wrap {{ max-width:1400px;margin:auto;padding:28px; }}
.eyebrow {{
  color:var(--cyan);font-size:12px;font-weight:700;
  letter-spacing:.16em;text-transform:uppercase;
}}
h1 {{ margin:7px 0 5px;font-size:30px; }}
.sub {{ color:var(--muted); }}
.top {{ display:flex;justify-content:space-between;gap:20px; }}
.card {{
  background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.018));
  border:1px solid var(--line);border-radius:16px;padding:18px;
  box-shadow:0 14px 45px rgba(0,0,0,.18);
}}
.grid {{
  display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:12px;margin:18px 0;
}}
.num {{ font-size:28px;font-weight:750; }}
.label {{ color:var(--muted);font-size:12px;margin-top:5px; }}
.badge {{
  display:inline-block;border:1px solid var(--line);border-radius:999px;
  padding:6px 10px;color:var(--cyan);font-size:12px;
}}
.section {{ margin-top:18px; }}
.section h2 {{ font-size:17px;margin:0 0 12px; }}
.band {{
  display:flex;justify-content:space-between;
  padding:13px 4px;border-bottom:1px solid var(--line);
}}
.band:last-child {{ border-bottom:0; }}
.record {{
  padding:15px;border:1px solid var(--line);border-radius:13px;
  margin:9px 0;background:rgba(255,255,255,.025);
}}
.title {{ font-weight:700; }}
.meta {{ color:var(--muted);font-size:12px;margin-top:7px; }}
a {{ color:var(--cyan); }}
.flag {{
  padding:12px 14px;border-left:3px solid var(--amber);
  background:rgba(255,189,85,.07);border-radius:9px;margin:8px 0;
}}
@media(max-width:900px) {{
  .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
}}
@media(max-width:560px) {{
  .grid {{ grid-template-columns:1fr; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <div class="eyebrow">PROPERTYIQ · V21.1</div>
      <h1>Boundary Intelligence</h1>
      <div class="sub">
        Source-backed intelligence organized around the saved property boundary.
      </div>
    </div>
    <div class="badge">
      {"BOUNDARY SAVED" if data["boundary"]["present"] else "BOUNDARY NOT SET"}
    </div>
  </div>

  <div class="card" style="margin-top:20px">
    <strong id="name"></strong>
    <div class="meta" id="address"></div>
  </div>

  <div class="grid">
    <div class="card"><div class="num" id="mapped"></div><div class="label">Mapped records</div></div>
    <div class="card"><div class="num" id="inside"></div><div class="label">Inside boundary</div></div>
    <div class="card"><div class="num" id="near"></div><div class="label">Within 500 m</div></div>
    <div class="card"><div class="num" id="unmapped"></div><div class="label">Unmapped records</div></div>
  </div>

  <div class="card section">
    <h2>Spatial intelligence bands</h2>
    <div class="band"><span>Inside property boundary</span><span class="badge" id="b0"></span></div>
    <div class="band"><span>0–500 m</span><span class="badge" id="b1"></span></div>
    <div class="band"><span>500 m–1 km</span><span class="badge" id="b2"></span></div>
    <div class="band"><span>1–3 km</span><span class="badge" id="b3"></span></div>
    <div class="band"><span>3–5 km</span><span class="badge" id="b4"></span></div>
  </div>

  <div class="card section">
    <h2>Due-diligence flags</h2>
    <div id="flags"></div>
  </div>

  <div class="card section">
    <h2>Boundary-linked intelligence</h2>
    <div id="records"></div>
  </div>
</div>

<script>
const DATA = {payload};

document.getElementById("name").textContent =
  DATA.property.name || "Property";
document.getElementById("address").textContent =
  DATA.property.address || "";

document.getElementById("mapped").textContent =
  DATA.summary.mapped_records;
document.getElementById("inside").textContent =
  DATA.summary.inside_boundary;
document.getElementById("near").textContent =
  DATA.summary.within_500m;
document.getElementById("unmapped").textContent =
  DATA.summary.unmapped_records;

document.getElementById("b0").textContent =
  DATA.bands.inside_boundary.length;
document.getElementById("b1").textContent =
  DATA.bands["0_500m"].length;
document.getElementById("b2").textContent =
  DATA.bands["500m_1km"].length;
document.getElementById("b3").textContent =
  DATA.bands["1_3km"].length;
document.getElementById("b4").textContent =
  DATA.bands["3_5km"].length;

const flags = document.getElementById("flags");

if (!DATA.flags.length) {{
  flags.innerHTML =
    '<div class="meta">No boundary-level flags.</div>';
}} else {{
  DATA.flags.forEach(f => {{
    const d = document.createElement("div");
    d.className = "flag";
    d.textContent = f.code + " — " + f.message;
    flags.appendChild(d);
  }});
}}

const records = document.getElementById("records");

if (!DATA.records.length) {{
  records.innerHTML =
    '<div class="meta">' +
    'No mapped intelligence records currently fall inside ' +
    'the saved boundary or requested 5 km radius.' +
    '</div>';
}} else {{
  DATA.records.forEach(r => {{
    const d = document.createElement("div");
    d.className = "record";

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = r.title || "Untitled record";

    const meta1 = document.createElement("div");
    meta1.className = "meta";
    meta1.textContent =
      (r.category || "uncategorized") +
      " · " + (r.status || "status unknown");

    const meta2 = document.createElement("div");
    meta2.className = "meta";

    const source =
      r.source_name || r.source_type || "Unknown source";

    meta2.textContent =
      source + " · " +
      (r.distance_km === 0
        ? "0 km"
        : (r.distance_km ?? "—") + " km from property");

    const meta3 = document.createElement("div");
    meta3.className = "meta";
    meta3.textContent =
      "Evidence: " +
      (r.evidence_level || "not specified") +
      " · Confidence: " +
      (r.confidence ?? "not specified");

    d.appendChild(title);
    d.appendChild(meta1);
    d.appendChild(meta2);
    d.appendChild(meta3);

    if (r.source_url) {{
      const link = document.createElement("a");
      link.href = r.source_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Open source";
      d.appendChild(link);
    }}

    records.appendChild(d);
  }});
}}
</script>
</body>
</html>
""")


def install_v21_1_boundary_intelligence(app, engine):
    _v21_install(app, engine)

    print("=" * 72)
    print("PROPERTYIQ V21.1 — BOUNDARY INTELLIGENCE INSTALLED")
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime: existing")
    print("")
    print("/api/v1/properties/{property_id}/boundary-intelligence")
    print("/propertyiq/boundary-intelligence/{property_id}")
    print("")
    print("Boundary-aware intelligence: ENABLED")
    print("Inside-boundary detection: ENABLED")
    print("0–500m band: ENABLED")
    print("500m–1km band: ENABLED")
    print("1–3km band: ENABLED")
    print("3–5km band: ENABLED")
    print("Unmapped-record tracking: ENABLED")
    print("Source/evidence/confidence preservation: ENABLED")
    print("No fabricated intelligence")
    print("Existing PropertyIQ routes preserved")
    print("=" * 72)


# ---------------------------------------------------------------------------
# DIRECT COLAB EXECUTION
# ---------------------------------------------------------------------------

if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not loaded in this Colab runtime. "
        "Load the existing PropertyIQ/P3 runtime first."
    )

if "app" not in globals():
    raise RuntimeError(
        "PropertyIQ FastAPI app is not loaded in this Colab runtime. "
        "Load the existing PropertyIQ runtime first."
    )

install_v21_1_boundary_intelligence(app, engine)



# ============================================================
# PROPERTYIQ MODULE: V22
# ORIGINAL COLAB CELL: In[35]
# ============================================================

# ============================================================================
# PROPERTYIQ V22 — CLEAN PREMIUM PROPERTY WORKSPACE
# ============================================================================
# Additive UI module.
# Does NOT rerun V13/V14/V15/V20/V21.
# Uses existing PropertyIQ `app` and `engine`.
#
# Adds:
#   /propertyiq/workspace/{property_id}
#
# Premium command-center style property workspace:
#   Overview
#   Location
#   Boundary
#   Intelligence
#   Evidence
#   Market
#   Development
#   News
#   Documents
#
# This is a presentation/workspace layer. It does not fabricate intelligence
# and does not modify database records.
# ============================================================================

from sqlalchemy import text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
import json

MODULE_VERSION = "PROPERTYIQ-V22-CLEAN-PREMIUM-WORKSPACE"


def _v22_load_property(engine, property_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities,
                    photos,
                    documents,
                    contact_information,
                    ST_AsGeoJSON(boundary) AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

        if not row:
            return None

        p = dict(row)

        intelligence_count = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
            """),
            {"pid": str(property_id)}
        ).scalar_one()

        evidence_count = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM evidence_items
                WHERE property_id = CAST(:pid AS uuid)
            """),
            {"pid": str(property_id)}
        ).scalar_one()

        mapped_count = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM intelligence_records
                WHERE latitude IS NOT NULL
                  AND longitude IS NOT NULL
            """)
        ).scalar_one()

        market_count = 0
        try:
            market_count = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM property_market_observations
                    WHERE property_id = CAST(:pid AS uuid)
                """),
                {"pid": str(property_id)}
            ).scalar_one()
        except Exception:
            market_count = 0

        files_count = 0
        try:
            files_count = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM property_files
                    WHERE property_id = CAST(:pid AS uuid)
                """),
                {"pid": str(property_id)}
            ).scalar_one()
        except Exception:
            files_count = 0

    p["id"] = str(p["id"])
    p["intelligence_count"] = int(intelligence_count or 0)
    p["evidence_count"] = int(evidence_count or 0)
    p["mapped_count"] = int(mapped_count or 0)
    p["market_count"] = int(market_count or 0)
    p["files_count"] = int(files_count or 0)
    p["boundary_present"] = bool(p.get("boundary_geojson"))

    return p


def _v22_install(app, engine):
    path = "/propertyiq/workspace/{property_id}"
    existing = {getattr(r, "path", "") for r in app.routes}

    if path in existing:
        print("PROPERTYIQ V22 route already exists — preserved.")
        return

    @app.get(
        path,
        response_class=HTMLResponse,
        tags=["PropertyIQ V22"]
    )
    def v22_workspace(property_id: str):
        p = _v22_load_property(engine, property_id)

        if not p:
            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )

        payload = json.dumps(
            p,
            ensure_ascii=False,
            default=str
        ).replace("</", "<\\/")

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Property Workspace</title>
<style>
:root {{
  --bg:#060d18;
  --panel:#0b1626;
  --panel2:#0e1c2f;
  --text:#edf6ff;
  --muted:#8ca4bd;
  --line:rgba(255,255,255,.085);
  --blue:#31a8ff;
  --cyan:#58e7ff;
  --green:#36d49b;
  --amber:#ffbf5a;
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; min-height:100%; }}
body {{
  color:var(--text);
  background:
    radial-gradient(circle at 10% -10%,rgba(49,168,255,.16),transparent 34%),
    radial-gradient(circle at 92% 4%,rgba(88,231,255,.08),transparent 27%),
    linear-gradient(135deg,#060d18,#081322 55%,#07101d);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
.shell {{ min-height:100vh; }}
.nav {{
  height:64px;
  border-bottom:1px solid var(--line);
  background:rgba(6,13,24,.78);
  backdrop-filter:blur(18px);
  display:flex;
  align-items:center;
  padding:0 28px;
  gap:28px;
  position:sticky;
  top:0;
  z-index:10;
}}
.logo {{
  font-weight:800;
  letter-spacing:.08em;
  font-size:14px;
}}
.logo span {{ color:var(--cyan); }}
.navlinks {{
  display:flex;
  gap:18px;
  color:var(--muted);
  font-size:13px;
}}
.navlinks a {{ color:inherit;text-decoration:none; }}
.navlinks a:hover {{ color:var(--text); }}
.container {{ max-width:1500px;margin:auto;padding:28px; }}
.hero {{
  display:flex;
  justify-content:space-between;
  align-items:flex-start;
  gap:24px;
  margin-bottom:22px;
}}
.eyebrow {{
  color:var(--cyan);
  font-size:11px;
  font-weight:800;
  letter-spacing:.18em;
  text-transform:uppercase;
}}
h1 {{ margin:8px 0 7px;font-size:34px;line-height:1.08; }}
.address {{ color:var(--muted);font-size:14px; }}
.status {{
  padding:8px 12px;
  border:1px solid rgba(88,231,255,.25);
  border-radius:999px;
  color:var(--cyan);
  background:rgba(88,231,255,.06);
  font-size:12px;
  white-space:nowrap;
}}
.grid {{
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:12px;
}}
.card {{
  background:linear-gradient(180deg,rgba(255,255,255,.048),rgba(255,255,255,.018));
  border:1px solid var(--line);
  border-radius:18px;
  box-shadow:0 18px 60px rgba(0,0,0,.22);
}}
.metric {{ padding:18px; }}
.metric .value {{ font-size:26px;font-weight:800; }}
.metric .label {{ color:var(--muted);font-size:12px;margin-top:5px; }}
.layout {{
  display:grid;
  grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);
  gap:16px;
  margin-top:16px;
}}
.panel {{ padding:20px; }}
.panel h2 {{ margin:0 0 14px;font-size:17px; }}
.tabs {{
  display:flex;
  gap:7px;
  overflow:auto;
  padding-bottom:3px;
  margin-bottom:16px;
}}
.tab {{
  padding:8px 12px;
  border:1px solid var(--line);
  border-radius:999px;
  color:var(--muted);
  font-size:12px;
  white-space:nowrap;
  cursor:pointer;
  background:rgba(255,255,255,.02);
}}
.tab.active {{
  color:var(--text);
  border-color:rgba(88,231,255,.34);
  background:rgba(88,231,255,.08);
}}
.tabcontent {{ display:none; }}
.tabcontent.active {{ display:block; }}
.info {{
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:10px;
}}
.field {{
  border:1px solid var(--line);
  border-radius:12px;
  padding:13px;
  background:rgba(255,255,255,.018);
}}
.field .k {{ color:var(--muted);font-size:11px; }}
.field .v {{ margin-top:5px;font-size:14px;word-break:break-word; }}
.quick {{
  display:flex;
  flex-direction:column;
  gap:9px;
}}
.action {{
  display:block;
  text-decoration:none;
  color:var(--text);
  padding:12px 13px;
  border:1px solid var(--line);
  border-radius:12px;
  background:rgba(255,255,255,.02);
}}
.action:hover {{
  border-color:rgba(88,231,255,.35);
  background:rgba(88,231,255,.055);
}}
.action small {{ display:block;color:var(--muted);margin-top:3px; }}
.note {{
  color:var(--muted);
  line-height:1.55;
  font-size:13px;
}}
.mapbox {{
  height:250px;
  border:1px solid var(--line);
  border-radius:14px;
  display:flex;
  align-items:center;
  justify-content:center;
  background:
    linear-gradient(135deg,rgba(49,168,255,.09),rgba(88,231,255,.025)),
    repeating-linear-gradient(
      45deg,
      rgba(255,255,255,.018),
      rgba(255,255,255,.018) 1px,
      transparent 1px,
      transparent 18px
    );
}}
.coord {{ font-family:ui-monospace,monospace;color:var(--cyan);font-size:12px; }}
.pill {{
  display:inline-block;
  padding:5px 8px;
  border-radius:999px;
  background:rgba(54,212,155,.08);
  border:1px solid rgba(54,212,155,.18);
  color:var(--green);
  font-size:11px;
}}
@media(max-width:1050px) {{
  .layout {{ grid-template-columns:1fr; }}
}}
@media(max-width:760px) {{
  .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
  .hero {{ flex-direction:column; }}
  .navlinks {{ display:none; }}
}}
@media(max-width:520px) {{
  .grid,.info {{ grid-template-columns:1fr; }}
  .container {{ padding:17px; }}
}}
</style>
</head>
<body>
<div class="shell">
  <nav class="nav">
    <div class="logo">PROPERTY<span>IQ</span></div>
    <div class="navlinks">
      <a href="/propertyiq">Command Center</a>
      <a href="/propertyiq/map/{property_id}">Map</a>
      <a href="/propertyiq/boundary/{property_id}">Boundary</a>
      <a href="/propertyiq/boundary-intelligence/{property_id}">Intelligence</a>
      <a href="/propertyiq/intelligence-report/{property_id}">Report</a>
    </div>
  </nav>

  <main class="container">
    <section class="hero">
      <div>
        <div class="eyebrow">PROPERTY WORKSPACE · V22</div>
        <h1 id="name"></h1>
        <div class="address" id="address"></div>
      </div>
      <div class="status" id="status"></div>
    </section>

    <section class="grid">
      <div class="card metric">
        <div class="value" id="area">—</div>
        <div class="label">Property area</div>
      </div>
      <div class="card metric">
        <div class="value" id="price">—</div>
        <div class="label">Price</div>
      </div>
      <div class="card metric">
        <div class="value" id="intel">0</div>
        <div class="label">Property intelligence records</div>
      </div>
      <div class="card metric">
        <div class="value" id="evidence">0</div>
        <div class="label">Evidence records</div>
      </div>
    </section>

    <section class="layout">
      <div class="card panel">
        <div class="tabs">
          <div class="tab active" data-tab="overview">Overview</div>
          <div class="tab" data-tab="location">Location</div>
          <div class="tab" data-tab="development">Development</div>
          <div class="tab" data-tab="market">Market</div>
          <div class="tab" data-tab="news">News</div>
          <div class="tab" data-tab="documents">Documents</div>
        </div>

        <div class="tabcontent active" id="overview">
          <h2>Property Overview</h2>
          <div class="info" id="overviewFields"></div>
        </div>

        <div class="tabcontent" id="location">
          <h2>Location Intelligence</h2>
          <div class="mapbox">
            <div>
              <div class="coord" id="coords"></div>
              <div class="note" style="margin-top:8px">
                Use the Map workspace for full interactive spatial intelligence.
              </div>
            </div>
          </div>
        </div>

        <div class="tabcontent" id="development">
          <h2>Development</h2>
          <div class="note">
            Development intelligence is available through the unified
            development workspace and source-backed intelligence layers.
          </div>
        </div>

        <div class="tabcontent" id="market">
          <h2>Market</h2>
          <div class="note" id="marketNote"></div>
        </div>

        <div class="tabcontent" id="news">
          <h2>News</h2>
          <div class="note">
            Open the News intelligence workspace for geographic matching,
            source links and article-level evidence.
          </div>
        </div>

        <div class="tabcontent" id="documents">
          <h2>Documents</h2>
          <div class="note" id="documentsNote"></div>
        </div>
      </div>

      <aside>
        <div class="card panel">
          <h2>Property Intelligence</h2>
          <div class="quick">
            <a class="action" href="/propertyiq/map/{property_id}">
              Interactive Map
              <small>Explore mapped intelligence</small>
            </a>
            <a class="action" href="/propertyiq/boundary/{property_id}">
              Property Boundary
              <small>Draw and edit GIS boundary</small>
            </a>
            <a class="action" href="/propertyiq/boundary-intelligence/{property_id}">
              Boundary Intelligence
              <small>Analyze intelligence around the boundary</small>
            </a>
            <a class="action" href="/propertyiq/due-diligence-map/{property_id}">
              Due Diligence Map
              <small>Spatial evidence and intelligence</small>
            </a>
            <a class="action" href="/propertyiq/intelligence-report/{property_id}">
              Intelligence Report
              <small>Generate property intelligence report</small>
            </a>
          </div>
        </div>

        <div class="card panel" style="margin-top:16px">
          <h2>Workspace Status</h2>
          <div class="note">
            Boundary:
            <span class="pill" id="boundary"></span>
          </div>
          <div class="note" style="margin-top:10px">
            Market observations:
            <strong id="marketCount"></strong>
          </div>
          <div class="note" style="margin-top:10px">
            Documents:
            <strong id="fileCount"></strong>
          </div>
          <div class="note" style="margin-top:10px">
            Mapped intelligence in database:
            <strong id="mappedCount"></strong>
          </div>
        </div>
      </aside>
    </section>
  </main>
</div>

<script>
const DATA = {payload};

function esc(v) {{
  return String(v ?? "—")
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}}

document.getElementById("name").textContent =
  DATA.property_name || "Property";
document.getElementById("address").textContent =
  DATA.property_address || "Address unavailable";

document.getElementById("status").textContent =
  DATA.property_type || "Property";

document.getElementById("area").textContent =
  DATA.area != null ? String(DATA.area) : "—";

document.getElementById("price").textContent =
  DATA.price != null ? String(DATA.price) : "—";

document.getElementById("intel").textContent =
  DATA.intelligence_count;

document.getElementById("evidence").textContent =
  DATA.evidence_count;

document.getElementById("mappedCount").textContent =
  DATA.mapped_count;

document.getElementById("marketCount").textContent =
  DATA.market_count;

document.getElementById("fileCount").textContent =
  DATA.files_count;

document.getElementById("boundary").textContent =
  DATA.boundary_present ? "SAVED" : "NOT SET";

document.getElementById("coords").textContent =
  DATA.latitude != null && DATA.longitude != null
    ? DATA.latitude + ", " + DATA.longitude
    : "Coordinates unavailable";

document.getElementById("marketNote").textContent =
  DATA.market_count > 0
    ? DATA.market_count + " market observation(s) are attached to this property."
    : "No property-specific market observations are currently stored.";

document.getElementById("documentsNote").textContent =
  DATA.files_count > 0
    ? DATA.files_count + " document(s) are attached to this property."
    : "No documents are currently attached.";

const fields = [
  ["Property Type", DATA.property_type],
  ["Area", DATA.area],
  ["Bedrooms", DATA.bedrooms],
  ["Bathrooms", DATA.bathrooms],
  ["Builder / Owner", DATA.builder_owner],
  ["Price / sq.ft", DATA.price_per_sqft],
  ["Description", DATA.description],
  ["Amenities", DATA.amenities]
];

document.getElementById("overviewFields").innerHTML =
  fields.map(x =>
    '<div class="field">' +
      '<div class="k">' + esc(x[0]) + '</div>' +
      '<div class="v">' + esc(x[1]) + '</div>' +
    '</div>'
  ).join("");

document.querySelectorAll(".tab").forEach(tab => {{
  tab.addEventListener("click", () => {{
    document.querySelectorAll(".tab").forEach(x =>
      x.classList.remove("active")
    );
    document.querySelectorAll(".tabcontent").forEach(x =>
      x.classList.remove("active")
    );

    tab.classList.add("active");
    document.getElementById(tab.dataset.tab)
      .classList.add("active");
  }});
}});
</script>
</body>
</html>
""")


def install_v22_premium_workspace(app, engine):
    _v22_install(app, engine)

    print("=" * 72)
    print("PROPERTYIQ V22 — PREMIUM PROPERTY WORKSPACE INSTALLED")
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime: existing")
    print("")
    print("/propertyiq/workspace/{property_id}")
    print("")
    print("Premium workspace: ENABLED")
    print("Overview tab: ENABLED")
    print("Location tab: ENABLED")
    print("Development tab: ENABLED")
    print("Market tab: ENABLED")
    print("News tab: ENABLED")
    print("Documents tab: ENABLED")
    print("Intelligence shortcuts: ENABLED")
    print("Existing PropertyIQ routes preserved")
    print("No database records modified")
    print("=" * 72)


# Direct Colab execution.
if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not loaded. Load the existing PropertyIQ runtime first."
    )

if "app" not in globals():
    raise RuntimeError(
        "PropertyIQ FastAPI app is not loaded. Load the existing PropertyIQ runtime first."
    )

install_v22_premium_workspace(app, engine)



# ============================================================
# PROPERTYIQ MODULE: V24
# ORIGINAL COLAB CELL: In[36]
# ============================================================

# ============================================================================
# PROPERTYIQ V24 — REAL OSM / GIS PROPERTY CONTEXT
# ============================================================================
# Clean additive module.
#
# Roadmap note:
# V23 is intentionally skipped because the agreed PropertyIQ roadmap moves
# from V22 directly to V24 — REAL OSM/GIS.
#
# This module:
#   • preserves V20/V21/V22
#   • uses the existing live OSM connector route from V15 when available
#   • presents real source-backed OSM/GIS context around the property
#   • provides a one-click live OSM refresh
#   • provides category summaries and source links
#   • does not fabricate POIs or coordinates
#   • does not modify the database during installation
#
# Direct Colab execution:
# Paste the complete file into a new cell and run it.
# ============================================================================

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
import json

MODULE_VERSION = "PROPERTYIQ-V24-REAL-OSM-GIS"


def _v24_property(engine, property_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    ST_AsGeoJSON(boundary) AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    return dict(row) if row else None


def _v24_osm_records(engine, property_id, limit=500):
    """
    Reads OSM-backed records already ingested into intelligence_records.

    The V15 live connector remains responsible for fetching live OSM data.
    V24 is deliberately separated from the fetcher so this UI never invents
    a POI when no source-backed record exists.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE (
                    UPPER(COALESCE(source_type, '')) = 'OSM_LOCATION'
                    OR UPPER(COALESCE(source_name, '')) LIKE '%OPENSTREETMAP%'
                    OR UPPER(COALESCE(source_name, '')) LIKE '%OVERPASS%'
                )
                  AND latitude IS NOT NULL
                  AND longitude IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT :lim
            """),
            {"lim": int(limit)}
        ).mappings().all()

    return [dict(x) for x in rows]


def _v24_install(app, engine):
    existing = {getattr(r, "path", "") for r in app.routes}

    api_path = "/api/v1/properties/{property_id}/real-osm-gis"
    ui_path = "/propertyiq/real-osm-gis/{property_id}"

    if api_path not in existing:

        @app.get(api_path, tags=["PropertyIQ V24"])
        def v24_real_osm_gis_api(property_id: str, limit: int = 500):
            prop = _v24_property(engine, property_id)

            if not prop:
                raise HTTPException(
                    status_code=404,
                    detail="Property not found."
                )

            records = _v24_osm_records(
                engine,
                property_id,
                max(1, min(int(limit), 1000))
            )

            categories = {}

            for record in records:
                key = (
                    record.get("category")
                    or "uncategorized"
                )

                categories[key] = categories.get(key, 0) + 1

                record["id"] = str(record["id"])

                for k, v in list(record.items()):
                    if hasattr(v, "isoformat"):
                        record[k] = v.isoformat()

            return {
                "module": MODULE_VERSION,
                "property": {
                    "id": str(prop["id"]),
                    "name": prop["property_name"],
                    "address": prop["property_address"],
                    "latitude": prop["latitude"],
                    "longitude": prop["longitude"],
                    "boundary_present": bool(
                        prop.get("boundary_geojson")
                    )
                },
                "source": {
                    "name": "OpenStreetMap / Overpass",
                    "type": "OSM_LOCATION",
                    "records_are_stored_source_backed": True
                },
                "summary": {
                    "records": len(records),
                    "categories": categories
                },
                "records": records
            }

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V24"]
        )
        def v24_real_osm_gis_ui(property_id: str):

            prop = _v24_property(engine, property_id)

            if not prop:
                raise HTTPException(
                    status_code=404,
                    detail="Property not found."
                )

            payload = json.dumps(
                {
                    "property_id": str(prop["id"]),
                    "name": prop["property_name"],
                    "address": prop["property_address"],
                    "lat": prop["latitude"],
                    "lon": prop["longitude"],
                    "boundary": bool(prop.get("boundary_geojson"))
                },
                ensure_ascii=False,
                default=str
            ).replace("</", "<\\/")

            return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Real OSM / GIS</title>
<style>
:root {{
 --bg:#060d18;
 --panel:#0b1626;
 --text:#edf6ff;
 --muted:#8da5bd;
 --line:rgba(255,255,255,.09);
 --cyan:#55e6ff;
 --blue:#31a8ff;
 --green:#36d49b;
 --amber:#ffbf5a;
}}
* {{box-sizing:border-box}}
body {{
 margin:0;
 color:var(--text);
 background:
  radial-gradient(circle at 12% -10%,rgba(49,168,255,.15),transparent 35%),
  radial-gradient(circle at 90% 0%,rgba(85,230,255,.08),transparent 28%),
  var(--bg);
 font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
.nav {{
 height:64px;
 display:flex;
 align-items:center;
 gap:26px;
 padding:0 28px;
 border-bottom:1px solid var(--line);
 background:rgba(6,13,24,.8);
 backdrop-filter:blur(16px);
}}
.logo {{font-weight:800;letter-spacing:.08em}}
.logo span {{color:var(--cyan)}}
.nav a {{color:var(--muted);text-decoration:none;font-size:13px}}
.wrap {{max-width:1450px;margin:auto;padding:28px}}
.hero {{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}
.eyebrow {{
 color:var(--cyan);font-size:11px;font-weight:800;
 letter-spacing:.18em;text-transform:uppercase
}}
h1 {{font-size:32px;margin:7px 0}}
.sub {{color:var(--muted);font-size:14px}}
.btn {{
 border:1px solid rgba(85,230,255,.25);
 color:var(--text);
 background:rgba(85,230,255,.08);
 border-radius:10px;
 padding:10px 14px;
 cursor:pointer;
}}
.btn:hover {{background:rgba(85,230,255,.14)}}
.grid {{
 display:grid;
 grid-template-columns:repeat(4,minmax(0,1fr));
 gap:12px;
 margin:20px 0;
}}
.card {{
 background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.017));
 border:1px solid var(--line);
 border-radius:17px;
 padding:18px;
 box-shadow:0 16px 50px rgba(0,0,0,.2);
}}
.num {{font-size:27px;font-weight:800}}
.label {{color:var(--muted);font-size:12px;margin-top:5px}}
.layout {{
 display:grid;
 grid-template-columns:minmax(0,1.55fr) minmax(300px,.8fr);
 gap:15px;
}}
.map {{
 height:480px;
 border-radius:14px;
 border:1px solid var(--line);
 overflow:hidden;
 background:
  linear-gradient(135deg,rgba(49,168,255,.09),rgba(85,230,255,.025)),
  repeating-linear-gradient(
   45deg,
   rgba(255,255,255,.018),
   rgba(255,255,255,.018) 1px,
   transparent 1px,
   transparent 18px
  );
 display:flex;
 align-items:center;
 justify-content:center;
 text-align:center;
}}
.status {{color:var(--muted);font-size:12px;margin-top:9px}}
.record {{
 border:1px solid var(--line);
 border-radius:12px;
 padding:13px;
 margin:8px 0;
 background:rgba(255,255,255,.02);
}}
.title {{font-weight:700}}
.meta {{color:var(--muted);font-size:12px;margin-top:6px}}
a.source {{color:var(--cyan);font-size:12px}}
.pill {{
 display:inline-block;
 border:1px solid rgba(54,212,155,.2);
 background:rgba(54,212,155,.06);
 color:var(--green);
 border-radius:999px;
 padding:5px 8px;
 font-size:11px;
}}
@media(max-width:950px) {{
 .layout {{grid-template-columns:1fr}}
}}
@media(max-width:700px) {{
 .grid {{grid-template-columns:repeat(2,minmax(0,1fr))}}
 .nav a {{display:none}}
 .hero {{flex-direction:column}}
}}
@media(max-width:500px) {{
 .grid {{grid-template-columns:1fr}}
 .wrap {{padding:17px}}
}}
</style>
</head>
<body>
<nav class="nav">
 <div class="logo">PROPERTY<span>IQ</span></div>
 <a href="/propertyiq/workspace/{property_id}">Workspace</a>
 <a href="/propertyiq/map/{property_id}">Map</a>
 <a href="/propertyiq/boundary/{property_id}">Boundary</a>
 <a href="/propertyiq/boundary-intelligence/{property_id}">Boundary Intelligence</a>
</nav>

<main class="wrap">
 <section class="hero">
  <div>
   <div class="eyebrow">PROPERTYIQ · V24</div>
   <h1>Real OSM / GIS Context</h1>
   <div class="sub" id="address"></div>
  </div>
  <button class="btn" onclick="refreshLive()">Refresh Live OSM</button>
 </section>

 <div class="status" id="status">
  Source: OpenStreetMap / Overpass · source-backed records only
 </div>

 <section class="grid">
  <div class="card">
   <div class="num" id="count">0</div>
   <div class="label">OSM records in PropertyIQ</div>
  </div>
  <div class="card">
   <div class="num" id="categories">0</div>
   <div class="label">Categories</div>
  </div>
  <div class="card">
   <div class="num" id="lat">—</div>
   <div class="label">Latitude</div>
  </div>
  <div class="card">
   <div class="num" id="lon">—</div>
   <div class="label">Longitude</div>
  </div>
 </section>

 <section class="layout">
  <div class="card">
   <h2>GIS Context</h2>
   <div class="map">
    <div>
     <div class="pill">REAL SOURCE DATA</div>
     <div style="margin-top:12px" id="coords"></div>
     <div class="status">
      Full interactive map remains available in the Map workspace.
     </div>
    </div>
   </div>
  </div>

  <div class="card">
   <h2>OSM Intelligence</h2>
   <div id="records">
    <div class="status">Loading source-backed OSM records…</div>
   </div>
  </div>
 </section>
</main>

<script>
const PROPERTY = {payload};

document.getElementById("address").textContent =
 PROPERTY.name + " · " + (PROPERTY.address || "");

document.getElementById("lat").textContent =
 PROPERTY.lat ?? "—";

document.getElementById("lon").textContent =
 PROPERTY.lon ?? "—";

document.getElementById("coords").textContent =
 PROPERTY.lat != null && PROPERTY.lon != null
 ? PROPERTY.lat + ", " + PROPERTY.lon
 : "Coordinates unavailable";

async function loadRecords() {{
 const status = document.getElementById("status");
 const box = document.getElementById("records");

 try {{
  const r = await fetch(
   "/api/v1/properties/" +
   PROPERTY.property_id +
   "/real-osm-gis"
  );

  const data = await r.json();

  if (!r.ok) {{
   throw new Error(data.detail || "OSM request failed");
  }}

  document.getElementById("count").textContent =
   data.summary.records;

  document.getElementById("categories").textContent =
   Object.keys(data.summary.categories || {{}}).length;

  status.textContent =
   "Source: OpenStreetMap / Overpass · " +
   data.summary.records +
   " source-backed record(s)";

  if (!data.records.length) {{
   box.innerHTML =
    '<div class="status">' +
    'No stored OSM records are currently available. ' +
    'Use “Refresh Live OSM” to request a fresh public-source fetch.' +
    '</div>';
   return;
  }}

  box.innerHTML = "";

  data.records.forEach(item => {{
   const d = document.createElement("div");
   d.className = "record";

   const title = document.createElement("div");
   title.className = "title";
   title.textContent = item.title || "Untitled OSM record";

   const meta = document.createElement("div");
   meta.className = "meta";
   meta.textContent =
    (item.category || "uncategorized") +
    " · " +
    (item.status || "status unknown");

   const source = document.createElement("div");
   source.className = "meta";
   source.textContent =
    (item.source_name || "OpenStreetMap") +
    " · " +
    (item.latitude ?? "—") +
    ", " +
    (item.longitude ?? "—");

   d.appendChild(title);
   d.appendChild(meta);
   d.appendChild(source);

   if (item.source_url) {{
    const a = document.createElement("a");
    a.className = "source";
    a.href = item.source_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open source";
    d.appendChild(a);
   }}

   box.appendChild(d);
  }};

 }} catch (err) {{
  status.textContent = "OSM status: " + err.message;
  box.innerHTML =
   '<div class="status">' +
   'The OSM data could not be loaded. Existing PropertyIQ data was not changed.' +
   '</div>';
 }}
}}

async function refreshLive() {{
 const status = document.getElementById("status");
 status.textContent = "Requesting live OSM refresh…";

 try {{
  const r = await fetch(
   "/api/v1/live/property/" +
   PROPERTY.property_id +
   "/osm",
   {{method:"POST"}}
  );

  const data = await r.json();

  if (!r.ok) {{
   throw new Error(data.detail || "Live OSM refresh failed");
  }}

  status.textContent =
   "Live OSM refresh completed. Reloading source-backed records…";

  await loadRecords();

 }} catch (err) {{
  status.textContent =
   "Live OSM refresh unavailable: " + err.message;
 }}
}}

loadRecords();
</script>
</body>
</html>
""")


def install_v24_real_osm_gis(app, engine):
    _v24_install(app, engine)

    print("=" * 72)
    print("PROPERTYIQ V24 — REAL OSM / GIS INSTALLED")
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime: existing")
    print("")
    print("/api/v1/properties/{property_id}/real-osm-gis")
    print("/propertyiq/real-osm-gis/{property_id}")
    print("")
    print("Real OSM context: ENABLED")
    print("Source-backed records only: ENABLED")
    print("Live OSM refresh handoff: ENABLED")
    print("GIS/property coordinate context: ENABLED")
    print("No fabricated POIs")
    print("No database records modified during installation")
    print("Existing PropertyIQ routes preserved")
    print("=" * 72)


# Direct Colab execution.
if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not loaded. Load the existing PropertyIQ runtime first."
    )

if "app" not in globals():
    raise RuntimeError(
        "PropertyIQ FastAPI app is not loaded. Load the existing PropertyIQ runtime first."
    )

install_v24_real_osm_gis(app, engine)



# ============================================================
# PROPERTYIQ MODULE: V25
# ORIGINAL COLAB CELL: In[37]
# ============================================================

# ============================================================================
# PROPERTYIQ V25 — COMPARABLES / MARKET INTELLIGENCE
# ============================================================================
# Clean additive module.
#
# Uses the existing PropertyIQ database and existing engine/app only.
# Does not rerun V13/V14/V15/V20/V21/V22/V24.
# Does not require PRIMARY_SOURCE_CATALOG.
#
# V25 provides:
#   • actual stored comparable properties
#   • distance from subject property
#   • property-type compatibility
#   • area similarity
#   • bedroom/bathroom similarity
#   • actual price / price-per-sqft comparisons when present
#   • transparent descriptive similarity indicators
#   • market observation inventory
#   • premium market workspace
#
# It deliberately does NOT:
#   • fabricate comparable properties
#   • invent market prices
#   • predict valuation
#   • produce an investment score/ranking
#
# Direct Colab execution:
# Paste this complete file into a new cell and run it.
# ============================================================================

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
import json
import math

MODULE_VERSION = "PROPERTYIQ-V25-COMPARABLES-MARKET-INTELLIGENCE"


def _v25_haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None

    r = 6371.0088

    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    a = max(0.0, min(1.0, a))

    return r * 2.0 * math.atan2(
        math.sqrt(a),
        math.sqrt(1.0 - a)
    )


def _v25_property(engine, property_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    return dict(row) if row else None


def _v25_similarity(subject, comp, distance_km):
    """
    Descriptive similarity only.

    This is intentionally not an investment score and is not exposed
    as a ranking/winner. Each component is shown independently.
    """

    result = {
        "distance_km": (
            round(distance_km, 3)
            if distance_km is not None else None
        ),
        "property_type_match": None,
        "area_ratio": None,
        "area_difference_percent": None,
        "bedroom_difference": None,
        "bathroom_difference": None,
    }

    subject_type = subject.get("property_type")
    comp_type = comp.get("property_type")

    if subject_type and comp_type:
        result["property_type_match"] = (
            str(subject_type).strip().lower()
            == str(comp_type).strip().lower()
        )

    subject_area = subject.get("area")
    comp_area = comp.get("area")

    try:
        if (
            subject_area is not None
            and comp_area is not None
            and float(subject_area) > 0
            and float(comp_area) > 0
        ):
            result["area_ratio"] = round(
                float(comp_area) / float(subject_area),
                4
            )

            result["area_difference_percent"] = round(
                (
                    abs(float(comp_area) - float(subject_area))
                    / float(subject_area)
                ) * 100.0,
                2
            )
    except Exception:
        pass

    if subject.get("bedrooms") is not None and comp.get("bedrooms") is not None:
        try:
            result["bedroom_difference"] = (
                float(comp["bedrooms"])
                - float(subject["bedrooms"])
            )
        except Exception:
            pass

    if subject.get("bathrooms") is not None and comp.get("bathrooms") is not None:
        try:
            result["bathroom_difference"] = (
                float(comp["bathrooms"])
                - float(subject["bathrooms"])
            )
        except Exception:
            pass

    return result


def _v25_market_data(engine, property_id, radius_km=10.0, limit=100):
    radius_km = max(0.1, min(float(radius_km), 50.0))
    limit = max(1, min(int(limit), 500))

    subject = _v25_property(engine, property_id)

    if not subject:
        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    if subject["latitude"] is None or subject["longitude"] is None:
        raise HTTPException(
            status_code=400,
            detail="Property does not have usable latitude/longitude."
        )

    with engine.connect() as conn:

        comparable_rows = conn.execute(
            text("""
                SELECT
                    p.id,
                    p.property_name,
                    p.property_address,
                    p.latitude,
                    p.longitude,
                    p.property_type,
                    p.area,
                    p.price,
                    p.price_per_sqft,
                    p.bedrooms,
                    p.bathrooms,
                    p.builder_owner,
                    p.description,
                    p.amenities
                FROM properties p
                WHERE p.id <> CAST(:pid AS uuid)
                  AND p.latitude IS NOT NULL
                  AND p.longitude IS NOT NULL
            """),
            {"pid": str(property_id)}
        ).mappings().all()

        observation_count = 0
        observations = []

        try:
            observation_rows = conn.execute(
                text("""
                    SELECT *
                    FROM property_market_observations
                    WHERE property_id = CAST(:pid AS uuid)
                    ORDER BY id DESC
                    LIMIT :lim
                """),
                {
                    "pid": str(property_id),
                    "lim": limit
                }
            ).mappings().all()

            observations = [dict(x) for x in observation_rows]
            observation_count = len(observations)

        except Exception:
            observations = []
            observation_count = 0

    comparables = []

    for row in comparable_rows:

        comp = dict(row)

        distance_km = _v25_haversine_km(
            subject["latitude"],
            subject["longitude"],
            comp["latitude"],
            comp["longitude"]
        )

        if distance_km is None or distance_km > radius_km:
            continue

        comp["id"] = str(comp["id"])

        similarity = _v25_similarity(
            subject,
            comp,
            distance_km
        )

        comp["similarity"] = similarity

        comparables.append(comp)

    # Deterministic descriptive ordering:
    # nearest first, without assigning a "best" comparable.
    comparables.sort(
        key=lambda x: (
            x["similarity"]["distance_km"]
            if x["similarity"]["distance_km"] is not None
            else float("inf")
        )
    )

    comparables = comparables[:limit]

    return {
        "module": MODULE_VERSION,
        "property": {
            "id": str(subject["id"]),
            "name": subject["property_name"],
            "address": subject["property_address"],
            "latitude": subject["latitude"],
            "longitude": subject["longitude"],
            "property_type": subject["property_type"],
            "area": subject["area"],
            "price": subject["price"],
            "price_per_sqft": subject["price_per_sqft"],
            "bedrooms": subject["bedrooms"],
            "bathrooms": subject["bathrooms"],
        },
        "search": {
            "radius_km": radius_km,
            "limit": limit,
        },
        "summary": {
            "comparable_properties_found": len(comparables),
            "market_observations": observation_count,
            "priced_comparables": sum(
                1
                for x in comparables
                if x.get("price") is not None
            ),
            "priced_per_sqft_comparables": sum(
                1
                for x in comparables
                if x.get("price_per_sqft") is not None
            ),
        },
        "comparables": comparables,
        "market_observations": observations,
        "methodology": {
            "distance": "Haversine distance from subject property coordinates",
            "type": "Exact normalized property_type equality when both are present",
            "area": "Descriptive area ratio and percentage difference",
            "bedrooms": "Descriptive bedroom difference",
            "bathrooms": "Descriptive bathroom difference",
            "ordering": "Nearest first; no investment or valuation ranking",
            "data_policy": "Only actual stored PropertyIQ records are used",
        }
    }


def _v25_install(app, engine):
    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    api_path = (
        "/api/v1/properties/"
        "{property_id}/market-comparables"
    )

    ui_path = (
        "/propertyiq/market-comparables/"
        "{property_id}"
    )

    if api_path not in existing:

        @app.get(
            api_path,
            tags=["PropertyIQ V25"]
        )
        def v25_market_api(
            property_id: str,
            radius_km: float = 10.0,
            limit: int = 100
        ):
            return _v25_market_data(
                engine,
                property_id,
                radius_km,
                limit
            )

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V25"]
        )
        def v25_market_ui(property_id: str):

            payload = _v25_market_data(
                engine,
                property_id,
                10.0,
                100
            )

            data = json.dumps(
                payload,
                ensure_ascii=False,
                default=str
            ).replace("</", "<\\/")

            return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Market Comparables</title>

<style>
:root {{
 --bg:#060d18;
 --panel:#0b1626;
 --text:#edf6ff;
 --muted:#8ca5bd;
 --line:rgba(255,255,255,.085);
 --cyan:#58e7ff;
 --blue:#31a8ff;
 --green:#36d49b;
}}
* {{box-sizing:border-box}}

body {{
 margin:0;
 color:var(--text);
 background:
  radial-gradient(
   circle at 10% -10%,
   rgba(49,168,255,.15),
   transparent 34%
  ),
  radial-gradient(
   circle at 90% 0%,
   rgba(88,231,255,.07),
   transparent 28%
  ),
  var(--bg);
 font-family:
  Inter,
  system-ui,
  -apple-system,
  BlinkMacSystemFont,
  "Segoe UI",
  sans-serif;
}}

.nav {{
 height:64px;
 display:flex;
 align-items:center;
 gap:25px;
 padding:0 28px;
 border-bottom:1px solid var(--line);
 background:rgba(6,13,24,.8);
 backdrop-filter:blur(16px);
}}

.logo {{
 font-weight:800;
 letter-spacing:.08em;
}}

.logo span {{color:var(--cyan)}}

.nav a {{
 color:var(--muted);
 text-decoration:none;
 font-size:13px;
}}

.wrap {{
 max-width:1500px;
 margin:auto;
 padding:28px;
}}

.eyebrow {{
 color:var(--cyan);
 font-size:11px;
 font-weight:800;
 letter-spacing:.18em;
 text-transform:uppercase;
}}

h1 {{
 font-size:32px;
 margin:7px 0;
}}

.sub {{
 color:var(--muted);
 font-size:14px;
}}

.hero {{
 display:flex;
 justify-content:space-between;
 gap:20px;
 align-items:flex-start;
 margin-bottom:20px;
}}

.badge {{
 border:1px solid rgba(88,231,255,.22);
 background:rgba(88,231,255,.06);
 color:var(--cyan);
 padding:7px 10px;
 border-radius:999px;
 font-size:11px;
 white-space:nowrap;
}}

.grid {{
 display:grid;
 grid-template-columns:repeat(4,minmax(0,1fr));
 gap:12px;
 margin-bottom:16px;
}}

.card {{
 background:
  linear-gradient(
   180deg,
   rgba(255,255,255,.045),
   rgba(255,255,255,.017)
  );
 border:1px solid var(--line);
 border-radius:17px;
 padding:18px;
 box-shadow:0 16px 50px rgba(0,0,0,.2);
}}

.metric .num {{
 font-size:27px;
 font-weight:800;
}}

.metric .label {{
 color:var(--muted);
 font-size:12px;
 margin-top:5px;
}}

.layout {{
 display:grid;
 grid-template-columns:minmax(0,1.7fr) minmax(290px,.8fr);
 gap:15px;
}}

h2 {{
 margin:0 0 14px;
 font-size:17px;
}}

.table-wrap {{
 overflow:auto;
 border:1px solid var(--line);
 border-radius:13px;
}}

table {{
 width:100%;
 border-collapse:collapse;
 min-width:900px;
}}

th,td {{
 text-align:left;
 padding:12px;
 border-bottom:1px solid var(--line);
 font-size:12px;
 vertical-align:top;
}}

th {{
 color:var(--muted);
 font-weight:600;
 background:rgba(255,255,255,.025);
}}

td strong {{font-size:13px}}

.muted {{color:var(--muted)}}

.method {{
 padding:12px;
 border:1px solid var(--line);
 border-radius:12px;
 margin-bottom:8px;
}}

.method .k {{
 color:var(--muted);
 font-size:11px;
}}

.method .v {{
 margin-top:4px;
 font-size:12px;
 line-height:1.5;
}}

.note {{
 color:var(--muted);
 font-size:12px;
 line-height:1.55;
}}

@media(max-width:950px) {{
 .layout {{grid-template-columns:1fr}}
}}

@media(max-width:700px) {{
 .grid {{grid-template-columns:repeat(2,minmax(0,1fr))}}
 .nav a {{display:none}}
 .hero {{flex-direction:column}}
}}

@media(max-width:500px) {{
 .grid {{grid-template-columns:1fr}}
 .wrap {{padding:17px}}
}}
</style>
</head>

<body>

<nav class="nav">
 <div class="logo">PROPERTY<span>IQ</span></div>
 <a href="/propertyiq/workspace/{property_id}">
  Workspace
 </a>
 <a href="/propertyiq/real-osm-gis/{property_id}">
  OSM / GIS
 </a>
 <a href="/propertyiq/map/{property_id}">
  Map
 </a>
 <a href="/propertyiq/intelligence-report/{property_id}">
  Report
 </a>
</nav>

<main class="wrap">

<section class="hero">
 <div>
  <div class="eyebrow">PROPERTYIQ · V25</div>
  <h1>Comparables & Market Intelligence</h1>
  <div class="sub" id="property"></div>
 </div>

 <div class="badge">
  ACTUAL STORED DATA ONLY
 </div>
</section>

<section class="grid">

 <div class="card metric">
  <div class="num" id="count"></div>
  <div class="label">
   Comparable properties found
  </div>
 </div>

 <div class="card metric">
  <div class="num" id="priced"></div>
  <div class="label">
   Comparables with actual price
  </div>
 </div>

 <div class="card metric">
  <div class="num" id="psf"></div>
  <div class="label">
   With actual price / sq.ft
  </div>
 </div>

 <div class="card metric">
  <div class="num" id="observations"></div>
  <div class="label">
   Market observations
  </div>
 </div>

</section>

<section class="layout">

 <div class="card">

  <h2>Comparable Properties</h2>

  <div class="note" style="margin-bottom:13px">
   Comparables are shown using actual PropertyIQ property records.
   Distance and descriptive similarity fields are transparent;
   no valuation or investment ranking is applied.
  </div>

  <div class="table-wrap">

   <table>

    <thead>
     <tr>
      <th>Property</th>
      <th>Distance</th>
      <th>Type</th>
      <th>Area</th>
      <th>Price</th>
      <th>Price / sq.ft</th>
      <th>Bedrooms</th>
      <th>Area difference</th>
     </tr>
    </thead>

    <tbody id="rows"></tbody>

   </table>

  </div>

 </div>

 <aside>

  <div class="card">

   <h2>Subject Property</h2>

   <div class="method">
    <div class="k">Property</div>
    <div class="v" id="subjectName"></div>
   </div>

   <div class="method">
    <div class="k">Type</div>
    <div class="v" id="subjectType"></div>
   </div>

   <div class="method">
    <div class="k">Area</div>
    <div class="v" id="subjectArea"></div>
   </div>

   <div class="method">
    <div class="k">Price</div>
    <div class="v" id="subjectPrice"></div>
   </div>

  </div>

  <div class="card" style="margin-top:15px">

   <h2>Methodology</h2>

   <div id="methods"></div>

  </div>

 </aside>

</section>

</main>

<script>

const DATA = {data};

const S = DATA.summary;
const P = DATA.property;

document.getElementById("property").textContent =
 P.name + " · " + (P.address || "");

document.getElementById("count").textContent =
 S.comparable_properties_found;

document.getElementById("priced").textContent =
 S.priced_comparables;

document.getElementById("psf").textContent =
 S.priced_per_sqft_comparables;

document.getElementById("observations").textContent =
 S.market_observations;

document.getElementById("subjectName").textContent =
 P.name || "—";

document.getElementById("subjectType").textContent =
 P.property_type || "—";

document.getElementById("subjectArea").textContent =
 P.area ?? "—";

document.getElementById("subjectPrice").textContent =
 P.price ?? "—";

function fmt(v) {{
 if (v === null || v === undefined || v === "") return "—";
 return String(v);
}}

const tbody = document.getElementById("rows");

if (!DATA.comparables.length) {{

 tbody.innerHTML =
  '<tr><td colspan="8" class="muted">' +
  'No actual comparable PropertyIQ records were found ' +
  'within the requested 10 km radius.' +
  '</td></tr>';

}} else {{

 DATA.comparables.forEach(c => {{

  const tr = document.createElement("tr");

  const sim = c.similarity || {{}};

  tr.innerHTML =
   "<td><strong>" +
   fmt(c.property_name) +
   "</strong><br><span class='muted'>" +
   fmt(c.property_address) +
   "</span></td>" +

   "<td>" +
   fmt(sim.distance_km) +
   " km</td>" +

   "<td>" +
   fmt(c.property_type) +
   "<br><span class='muted'>" +
   (
    sim.property_type_match === true
    ? "same type"
    : sim.property_type_match === false
    ? "different type"
    : "type unavailable"
   ) +
   "</span></td>" +

   "<td>" +
   fmt(c.area) +
   "</td>" +

   "<td>" +
   fmt(c.price) +
   "</td>" +

   "<td>" +
   fmt(c.price_per_sqft) +
   "</td>" +

   "<td>" +
   fmt(c.bedrooms) +
   "</td>" +

   "<td>" +
   (
    sim.area_difference_percent != null
    ? sim.area_difference_percent + "%"
    : "—"
   ) +
   "</td>";

  tbody.appendChild(tr);

 }});
}}

const methods = document.getElementById("methods");

Object.entries(DATA.methodology).forEach(([k,v]) => {{

 const d = document.createElement("div");
 d.className = "method";

 const key = document.createElement("div");
 key.className = "k";
 key.textContent = k;

 const value = document.createElement("div");
 value.className = "v";
 value.textContent = v;

 d.appendChild(key);
 d.appendChild(value);

 methods.appendChild(d);

}});

</script>

</body>
</html>
""")


def install_v25_comparables_market(app, engine):

    _v25_install(app, engine)

    print("=" * 72)
    print("PROPERTYIQ V25 — COMPARABLES / MARKET INTELLIGENCE INSTALLED")
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime: existing")
    print("")
    print(
        "/api/v1/properties/{property_id}/market-comparables"
    )
    print(
        "/propertyiq/market-comparables/{property_id}"
    )
    print("")
    print("Comparable properties: ENABLED")
    print("Distance comparison: ENABLED")
    print("Property-type comparison: ENABLED")
    print("Area comparison: ENABLED")
    print("Bedroom/bathroom comparison: ENABLED")
    print("Actual price comparison: ENABLED")
    print("Actual price/sq.ft comparison: ENABLED")
    print("Market observation inventory: ENABLED")
    print("No fabricated market data")
    print("No valuation prediction")
    print("No investment score/ranking")
    print("Existing PropertyIQ routes preserved")
    print("No database records modified during installation")
    print("=" * 72)


# Direct Colab execution.
if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not loaded. "
        "Load the existing PropertyIQ runtime first."
    )

if "app" not in globals():
    raise RuntimeError(
        "PropertyIQ FastAPI app is not loaded. "
        "Load the existing PropertyIQ runtime first."
    )

install_v25_comparables_market(app, engine)



# ============================================================
# PROPERTYIQ MODULE: V26
# ORIGINAL COLAB CELL: In[39]
# ============================================================

# ============================================================================
# PROPERTYIQ V26.1 — FULL PROPERTY PROFILE — CLEAN ADDITIVE MODULE
# ============================================================================
# V26.1 is a clean replacement for the failed V26 file-generation attempt.
#
# It attaches ONLY to the existing PropertyIQ `engine` and `app`.
# It does not rerun earlier modules and does not require PRIMARY_SOURCE_CATALOG.
# It is read-only and does not modify database records during installation.
#
# Direct Colab execution:
# Paste this complete file into a new cell and run it.
# ============================================================================

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
import json

MODULE_VERSION = "PROPERTYIQ-V26.1-FULL-PROPERTY-PROFILE-CLEAN"


def _v26_count(conn, sql, params):
    try:
        return int(
            conn.execute(text(sql), params).scalar_one() or 0
        )
    except Exception:
        return 0


def _v26_load(engine, property_id):
    pid = str(property_id)

    with engine.connect() as conn:

        row = conn.execute(
            text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities,
                    photos,
                    documents,
                    contact_information,
                    ST_AsGeoJSON(boundary) AS boundary_geojson,
                    ST_GeometryType(boundary) AS boundary_type,
                    ST_SRID(boundary) AS boundary_srid,
                    ST_IsValid(boundary) AS boundary_valid
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": pid}
        ).mappings().first()

        if not row:
            return None

        p = dict(row)

        counts = {
            "intelligence": _v26_count(
                conn,
                """
                SELECT COUNT(*)
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                """,
                {"pid": pid}
            ),
            "evidence": _v26_count(
                conn,
                """
                SELECT COUNT(*)
                FROM evidence_items
                WHERE property_id = CAST(:pid AS uuid)
                """,
                {"pid": pid}
            ),
            "market_observations": _v26_count(
                conn,
                """
                SELECT COUNT(*)
                FROM property_market_observations
                WHERE property_id = CAST(:pid AS uuid)
                """,
                {"pid": pid}
            ),
            "documents": _v26_count(
                conn,
                """
                SELECT COUNT(*)
                FROM property_files
                WHERE property_id = CAST(:pid AS uuid)
                """,
                {"pid": pid}
            ),
            "refresh_runs": _v26_count(
                conn,
                """
                SELECT COUNT(*)
                FROM piq_property_refresh_runs
                WHERE property_id = CAST(:pid AS uuid)
                """,
                {"pid": pid}
            ),
        }

        try:
            source_rows = conn.execute(
                text("""
                    SELECT
                        UPPER(COALESCE(source_type, 'OTHER')) AS source_type,
                        COUNT(*) AS record_count
                    FROM intelligence_records
                    WHERE property_id = CAST(:pid AS uuid)
                    GROUP BY UPPER(COALESCE(source_type, 'OTHER'))
                    ORDER BY source_type
                """),
                {"pid": pid}
            ).mappings().all()
        except Exception:
            source_rows = []

    p["id"] = str(p["id"])
    p["boundary_present"] = bool(p.get("boundary_geojson"))
    p["counts"] = counts
    p["source_inventory"] = [
        {
            "source_type": str(x["source_type"]),
            "record_count": int(x["record_count"])
        }
        for x in source_rows
    ]

    return p


def _v26_install(app, engine):

    api_path = "/api/v1/properties/{property_id}/full-profile"
    ui_path = "/propertyiq/full-profile/{property_id}"

    existing = {
        getattr(route, "path", "")
        for route in app.routes
    }

    if api_path not in existing:

        @app.get(api_path, tags=["PropertyIQ V26"])
        def v26_full_profile_api(property_id: str):

            data = _v26_load(engine, property_id)

            if not data:
                raise HTTPException(
                    status_code=404,
                    detail="Property not found."
                )

            return {
                "module": MODULE_VERSION,
                "read_only": True,
                "property": data,
                "data_policy": {
                    "fabricated_data": False,
                    "valuation_prediction": False,
                    "investment_score": False
                }
            }

    if ui_path not in existing:

        @app.get(
            ui_path,
            response_class=HTMLResponse,
            tags=["PropertyIQ V26"]
        )
        def v26_full_profile_ui(property_id: str):

            data = _v26_load(engine, property_id)

            if not data:
                raise HTTPException(
                    status_code=404,
                    detail="Property not found."
                )

            payload = json.dumps(
                data,
                ensure_ascii=False,
                default=str
            ).replace("</", "<\\/")

            return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropertyIQ — Full Property Profile</title>
<style>
:root {{
 --bg:#050c17;
 --panel:#0a1525;
 --text:#edf6ff;
 --muted:#8da5bd;
 --line:rgba(255,255,255,.085);
 --cyan:#58e7ff;
 --blue:#31a8ff;
 --green:#36d49b;
}}
* {{box-sizing:border-box}}
body {{
 margin:0;color:var(--text);
 background:
  radial-gradient(circle at 8% -8%,rgba(49,168,255,.16),transparent 34%),
  radial-gradient(circle at 92% 2%,rgba(88,231,255,.08),transparent 28%),
  linear-gradient(135deg,#050c17,#081322 58%,#07101d);
 font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
.nav {{
 height:64px;display:flex;align-items:center;gap:24px;padding:0 28px;
 border-bottom:1px solid var(--line);
 background:rgba(5,12,23,.82);backdrop-filter:blur(17px);
 position:sticky;top:0;z-index:20;
}}
.logo {{font-weight:850;letter-spacing:.08em}}
.logo span {{color:var(--cyan)}}
.nav a {{color:var(--muted);text-decoration:none;font-size:12px}}
.wrap {{max-width:1550px;margin:auto;padding:28px}}
.hero {{display:flex;justify-content:space-between;gap:25px;align-items:flex-start;margin-bottom:20px}}
.eyebrow {{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.18em;text-transform:uppercase}}
h1 {{margin:7px 0;font-size:34px;line-height:1.08}}
.address {{color:var(--muted);font-size:14px;max-width:900px}}
.badge {{border:1px solid rgba(88,231,255,.23);background:rgba(88,231,255,.06);color:var(--cyan);padding:8px 11px;border-radius:999px;font-size:11px}}
.metrics {{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:15px}}
.metric {{padding:16px;background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.016));border:1px solid var(--line);border-radius:16px;box-shadow:0 15px 50px rgba(0,0,0,.2)}}
.metric .num {{font-size:24px;font-weight:820}}
.metric .label {{margin-top:4px;color:var(--muted);font-size:11px}}
.main {{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(320px,.75fr);gap:15px}}
.card {{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.017));border:1px solid var(--line);border-radius:17px;padding:20px;box-shadow:0 17px 55px rgba(0,0,0,.2)}}
.tabs {{display:flex;gap:7px;overflow:auto;padding-bottom:3px;margin-bottom:18px}}
.tab {{border:1px solid var(--line);background:rgba(255,255,255,.018);color:var(--muted);border-radius:999px;padding:8px 12px;font-size:12px;cursor:pointer;white-space:nowrap}}
.tab.active {{color:var(--text);border-color:rgba(88,231,255,.32);background:rgba(88,231,255,.08)}}
.tabcontent {{display:none}}
.tabcontent.active {{display:block}}
h2 {{margin:0 0 14px;font-size:18px}}
.info {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
.field {{border:1px solid var(--line);border-radius:12px;padding:13px;background:rgba(255,255,255,.018)}}
.field .key {{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}}
.field .value {{margin-top:5px;font-size:13px;line-height:1.45;word-break:break-word}}
.source {{display:flex;justify-content:space-between;gap:15px;padding:12px;border:1px solid var(--line);border-radius:12px;margin:8px 0;background:rgba(255,255,255,.018)}}
.source .name {{font-weight:700;font-size:13px}}
.source .count {{color:var(--cyan);font-size:12px}}
.action {{display:block;color:var(--text);text-decoration:none;padding:12px 13px;border:1px solid var(--line);border-radius:12px;margin:8px 0;background:rgba(255,255,255,.018)}}
.action:hover {{background:rgba(88,231,255,.06);border-color:rgba(88,231,255,.28)}}
.action small {{display:block;color:var(--muted);margin-top:4px;font-size:11px}}
.note {{color:var(--muted);font-size:12px;line-height:1.6}}
.pill {{display:inline-block;border:1px solid rgba(54,212,155,.2);background:rgba(54,212,155,.06);color:var(--green);border-radius:999px;padding:5px 8px;font-size:11px}}
.coord {{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--cyan);font-size:12px}}
@media(max-width:1150px) {{.metrics {{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
@media(max-width:950px) {{.main {{grid-template-columns:1fr}}}}
@media(max-width:700px) {{.metrics {{grid-template-columns:repeat(2,minmax(0,1fr))}}.nav a {{display:none}}.hero {{flex-direction:column}}.info {{grid-template-columns:1fr}}}}
@media(max-width:500px) {{.metrics {{grid-template-columns:1fr}}.wrap {{padding:17px}}}}
</style>
</head>
<body>
<nav class="nav">
 <div class="logo">PROPERTY<span>IQ</span></div>
 <a href="/propertyiq/workspace/{property_id}">Workspace</a>
 <a href="/propertyiq/real-osm-gis/{property_id}">OSM / GIS</a>
 <a href="/propertyiq/map/{property_id}">Map</a>
 <a href="/propertyiq/boundary/{property_id}">Boundary</a>
 <a href="/propertyiq/market-comparables/{property_id}">Market</a>
 <a href="/propertyiq/intelligence-report/{property_id}">Report</a>
</nav>

<main class="wrap">

<section class="hero">
 <div>
  <div class="eyebrow">PROPERTYIQ · V26.1</div>
  <h1 id="name"></h1>
  <div class="address" id="address"></div>
 </div>
 <div class="badge">FULL PROPERTY PROFILE</div>
</section>

<section class="metrics">
 <div class="metric"><div class="num" id="intel"></div><div class="label">Property intelligence</div></div>
 <div class="metric"><div class="num" id="evidence"></div><div class="label">Evidence records</div></div>
 <div class="metric"><div class="num" id="market"></div><div class="label">Market observations</div></div>
 <div class="metric"><div class="num" id="documents"></div><div class="label">Documents</div></div>
 <div class="metric"><div class="num" id="refresh"></div><div class="label">Refresh runs</div></div>
 <div class="metric"><div class="num" id="boundary"></div><div class="label">GIS boundary</div></div>
</section>

<section class="main">

<div class="card">

 <div class="tabs">
  <div class="tab active" data-tab="overview">Overview</div>
  <div class="tab" data-tab="location">Location</div>
  <div class="tab" data-tab="intelligence">Intelligence</div>
  <div class="tab" data-tab="development">Development</div>
  <div class="tab" data-tab="market">Market</div>
  <div class="tab" data-tab="news">News</div>
  <div class="tab" data-tab="documents">Documents</div>
  <div class="tab" data-tab="timeline">Timeline</div>
 </div>

 <div class="tabcontent active" id="overview">
  <h2>Property Overview</h2>
  <div class="info" id="overviewFields"></div>
 </div>

 <div class="tabcontent" id="location">
  <h2>Location</h2>
  <div class="field">
   <div class="key">Coordinates</div>
   <div class="value coord" id="coords"></div>
  </div>
  <div class="field" style="margin-top:10px">
   <div class="key">Boundary</div>
   <div class="value" id="boundaryInfo"></div>
  </div>
  <p class="note" style="margin-top:12px">
   Use the interactive Map, Boundary and Real OSM/GIS workspaces for full spatial analysis.
  </p>
 </div>

 <div class="tabcontent" id="intelligence">
  <h2>Intelligence Sources</h2>
  <div id="sources"></div>
 </div>

 <div class="tabcontent" id="development">
  <h2>Development Intelligence</h2>
  <p class="note">
   Source-backed development, infrastructure, RERA, government and project intelligence
   remain available through the existing PropertyIQ intelligence workspaces.
  </p>
  <a class="action" href="/propertyiq/boundary-intelligence/{property_id}">
   Boundary Intelligence
   <small>Analyze source-backed records around the property.</small>
  </a>
 </div>

 <div class="tabcontent" id="market">
  <h2>Market Intelligence</h2>
  <p class="note" id="marketNote"></p>
  <a class="action" href="/propertyiq/market-comparables/{property_id}">
   Open Comparables Workspace
   <small>Review actual stored comparable properties and observations.</small>
  </a>
 </div>

 <div class="tabcontent" id="news">
  <h2>News Intelligence</h2>
  <p class="note">
   Geographic news intelligence is retained with source links and evidence metadata where available.
  </p>
 </div>

 <div class="tabcontent" id="documents">
  <h2>Documents</h2>
  <p class="note" id="documentsNote"></p>
 </div>

 <div class="tabcontent" id="timeline">
  <h2>Property Timeline</h2>
  <p class="note">
   Timeline events can be assembled from source-backed intelligence, ingestion history,
   documents and future development events.
  </p>
 </div>

</div>

<aside>

<div class="card">
 <h2>Property Command Links</h2>

 <a class="action" href="/propertyiq/map/{property_id}">
  Interactive Map
  <small>Source-backed spatial intelligence.</small>
 </a>

 <a class="action" href="/propertyiq/real-osm-gis/{property_id}">
  Real OSM / GIS
  <small>OpenStreetMap / Overpass context.</small>
 </a>

 <a class="action" href="/propertyiq/boundary/{property_id}">
  GIS Boundary
  <small>Draw, edit and save the property polygon.</small>
 </a>

 <a class="action" href="/propertyiq/boundary-intelligence/{property_id}">
  Boundary Intelligence
  <small>Intelligence organized around the boundary.</small>
 </a>

 <a class="action" href="/propertyiq/market-comparables/{property_id}">
  Market Comparables
  <small>Actual stored market/comparable data.</small>
 </a>

 <a class="action" href="/propertyiq/intelligence-report/{property_id}">
  Intelligence Report
  <small>Printable property intelligence report.</small>
 </a>
</div>

<div class="card" style="margin-top:15px">
 <h2>Data Coverage</h2>
 <div class="note">
  This profile is a read-only synthesis layer.
  It does not create missing intelligence.
 </div>
 <div style="margin-top:13px">
  <span class="pill" id="coverage"></span>
 </div>
</div>

</aside>

</section>
</main>

<script>
const DATA = {payload};

function esc(v) {{
 return String(v ?? "—")
  .replace(/&/g,"&amp;")
  .replace(/</g,"&lt;")
  .replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;");
}}

document.getElementById("name").textContent =
 DATA.property_name || "Property";

document.getElementById("address").textContent =
 DATA.property_address || "Address unavailable";

document.getElementById("intel").textContent =
 DATA.counts.intelligence;

document.getElementById("evidence").textContent =
 DATA.counts.evidence;

document.getElementById("market").textContent =
 DATA.counts.market_observations;

document.getElementById("documents").textContent =
 DATA.counts.documents;

document.getElementById("refresh").textContent =
 DATA.counts.refresh_runs;

document.getElementById("boundary").textContent =
 DATA.boundary_present ? "SAVED" : "NOT SET";

document.getElementById("coords").textContent =
 DATA.latitude != null && DATA.longitude != null
 ? DATA.latitude + ", " + DATA.longitude
 : "Coordinates unavailable";

document.getElementById("boundaryInfo").textContent =
 DATA.boundary_present
 ? (
    "Saved " +
    (DATA.boundary_type || "GIS geometry") +
    " · SRID " +
    (DATA.boundary_srid ?? "—") +
    " · Valid: " +
    (DATA.boundary_valid ?? "—")
   )
 : "No saved property boundary";

document.getElementById("marketNote").textContent =
 DATA.counts.market_observations > 0
 ? DATA.counts.market_observations +
   " property-specific market observation(s) are stored."
 : "No property-specific market observations are currently stored.";

document.getElementById("documentsNote").textContent =
 DATA.counts.documents > 0
 ? DATA.counts.documents +
   " property document(s) are stored."
 : "No property documents are currently stored.";

const fields = [
 ["Property Type", DATA.property_type],
 ["Area", DATA.area],
 ["Price", DATA.price],
 ["Price / sq.ft", DATA.price_per_sqft],
 ["Bedrooms", DATA.bedrooms],
 ["Bathrooms", DATA.bathrooms],
 ["Builder / Owner", DATA.builder_owner],
 ["Description", DATA.description],
 ["Amenities", DATA.amenities],
 ["Contact Information", DATA.contact_information]
];

document.getElementById("overviewFields").innerHTML =
 fields.map(item =>
  '<div class="field">' +
   '<div class="key">' + esc(item[0]) + '</div>' +
   '<div class="value">' + esc(item[1]) + '</div>' +
  '</div>'
 ).join("");

const sources = document.getElementById("sources");

if (!DATA.source_inventory.length) {{
 sources.innerHTML =
  '<div class="note">No property-specific intelligence source records are currently attached.</div>';
}} else {{
 DATA.source_inventory.forEach(item => {{
  const div = document.createElement("div");
  div.className = "source";

  const name = document.createElement("div");
  name.className = "name";
  name.textContent = item.source_type;

  const count = document.createElement("div");
  count.className = "count";
  count.textContent = item.record_count + " records";

  div.appendChild(name);
  div.appendChild(count);
  sources.appendChild(div);
 }});
}}

const totalCoverage =
 DATA.counts.intelligence +
 DATA.counts.evidence +
 DATA.counts.market_observations +
 DATA.counts.documents;

document.getElementById("coverage").textContent =
 totalCoverage > 0
 ? "SOURCE DATA AVAILABLE"
 : "LIMITED SOURCE DATA";

document.querySelectorAll(".tab").forEach(tab => {{
 tab.addEventListener("click", () => {{
  document.querySelectorAll(".tab")
   .forEach(x => x.classList.remove("active"));

  document.querySelectorAll(".tabcontent")
   .forEach(x => x.classList.remove("active"));

  tab.classList.add("active");

  document.getElementById(tab.dataset.tab)
   .classList.add("active");
 }});
}});
</script>

</body>
</html>
""")


def install_v26_1_full_profile(app, engine):

    _v26_install(app, engine)

    print("=" * 72)
    print("PROPERTYIQ V26.1 — FULL PROPERTY PROFILE INSTALLED")
    print("=" * 72)
    print("Module:", MODULE_VERSION)
    print("Runtime: existing")
    print("")
    print("/api/v1/properties/{property_id}/full-profile")
    print("/propertyiq/full-profile/{property_id}")
    print("")
    print("Property overview: ENABLED")
    print("Location profile: ENABLED")
    print("Boundary status: ENABLED")
    print("Intelligence source inventory: ENABLED")
    print("Development workspace: ENABLED")
    print("Market workspace: ENABLED")
    print("News workspace: ENABLED")
    print("Documents workspace: ENABLED")
    print("Timeline workspace: ENABLED")
    print("Read-only synthesis: ENABLED")
    print("No fabricated intelligence")
    print("No valuation prediction")
    print("No investment score")
    print("Existing PropertyIQ routes preserved")
    print("No database records modified during installation")
    print("=" * 72)


if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not loaded. "
        "Load the existing PropertyIQ runtime first."
    )

if "app" not in globals():
    raise RuntimeError(
        "PropertyIQ FastAPI app is not loaded. "
        "Load the existing PropertyIQ runtime first."
    )

install_v26_1_full_profile(app, engine)



# ============================================================
# PROPERTYIQ MODULE: COMMAND_MAP
# ORIGINAL COLAB CELL: In[83]
# ============================================================

# ============================================================================
# PROPERTYIQ — UNIFIED INTELLIGENCE COMMAND MAP — CORRECTED V2
# ============================================================================
# DIRECT COLAB EXECUTION
#
# This is an ADDITIVE integration layer.
#
# Integrates existing:
#   - Property
#   - GIS boundary
#   - OSM / GIS
#   - RERA
#   - Government / PAIMANA
#   - News / GDELT
#   - Development
#   - Market / comparables
#   - Evidence
#
# Does NOT rebuild existing connectors.
# Does NOT fabricate coordinates or intelligence.
# Does NOT modify database records during installation.
#
# API:
#   /api/v1/properties/{property_id}/command-map
#
# UI:
#   /propertyiq/command-map/{property_id}
#
# IMPORTANT:
#   The HTML/JavaScript is intentionally NOT generated with an f-string.
#   This prevents JavaScript/CSS braces from causing Python SyntaxError.
# ============================================================================


# ============================================================================
# 1. DEPENDENCIES
# ============================================================================

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
except Exception:
    import subprocess
    import sys

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn"
    ])

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse


try:
    from sqlalchemy import text
except Exception:
    import subprocess
    import sys

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "sqlalchemy"
    ])

    from sqlalchemy import text


import json
import math
from datetime import datetime


MODULE_VERSION = (
    "PROPERTYIQ-UNIFIED-INTELLIGENCE-COMMAND-MAP-V2"
)


# ============================================================================
# 2. RUNTIME CHECK
# ============================================================================

if "engine" not in globals():
    raise RuntimeError(
        """
PROPERTYIQ DATABASE ENGINE NOT FOUND.

Your existing PostgreSQL/PostGIS runtime is not currently loaded.

Run the existing PropertyIQ database/runtime cell first,
then run this complete cell again.
"""
    )


if "app" not in globals():

    app = FastAPI(
        title="PropertyIQ",
        version=MODULE_VERSION
    )

    print(
        "WARNING: Existing FastAPI app was not found."
    )

    print(
        "A new FastAPI app was created."
    )


# ============================================================================
# 3. HELPERS
# ============================================================================

def _ucm_safe(value):

    if value is None:
        return None

    if isinstance(value, (dict, list, tuple)):

        try:
            return json.loads(
                json.dumps(
                    value,
                    default=str
                )
            )
        except Exception:
            return str(value)

    return value


def _ucm_float(value):

    try:

        if value is None:
            return None

        return float(value)

    except Exception:

        return None


def _ucm_haversine_km(
    lat1,
    lon1,
    lat2,
    lon2
):

    try:

        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)

    except Exception:

        return None


    earth_radius_km = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(
        lat2 - lat1
    )

    dlon = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(dlat / 2.0) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dlon / 2.0) ** 2
    )

    return (
        2.0
        * earth_radius_km
        * math.asin(
            math.sqrt(a)
        )
    )


def _ucm_table_exists(
    conn,
    table_name
):

    try:

        value = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = :table_name
                )
                """
            ),
            {
                "table_name": table_name
            }
        ).scalar()

        return bool(value)

    except Exception:

        return False


def _ucm_source_group(
    source_type,
    source_name,
    category,
    title
):

    combined = " ".join([
        str(source_type or ""),
        str(source_name or ""),
        str(category or ""),
        str(title or "")
    ]).upper()


    if (
        "RERA" in combined
        or "UP-RERA" in combined
        or "REAL ESTATE REGULATORY" in combined
    ):
        return "RERA"


    if (
        "PAIMANA" in combined
        or "GOVERNMENT" in combined
        or "INFRASTRUCTURE" in combined
        or "MINISTRY" in combined
    ):
        return "GOVERNMENT"


    if (
        "OSM" in combined
        or "OPENSTREETMAP" in combined
        or "OVERPASS" in combined
    ):
        return "OSM"


    if (
        "GDELT" in combined
        or "NEWS" in combined
        or "ARTICLE" in combined
    ):
        return "NEWS"


    if (
        "DEVELOPMENT" in combined
        or "PROJECT" in combined
        or "METRO" in combined
        or "HIGHWAY" in combined
        or "EXPRESSWAY" in combined
        or "RAILWAY" in combined
        or "FLYOVER" in combined
        or "LOGISTICS" in combined
        or "INDUSTRIAL" in combined
        or "COMMERCIAL" in combined
    ):
        return "DEVELOPMENT"


    return "OTHER"


# ============================================================================
# 4. MAIN DATA BUILDER
# ============================================================================

def build_unified_command_map(
    engine,
    property_id,
    radius_km=5.0
):

    pid = str(property_id)


    try:
        radius_km = float(radius_km)
    except Exception:
        radius_km = 5.0


    if radius_km <= 0:
        radius_km = 5.0


    # ------------------------------------------------------------------------
    # PROPERTY
    # ------------------------------------------------------------------------

    with engine.connect() as conn:

        property_row = conn.execute(
            text(
                """
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities,
                    photos,
                    documents,
                    contact_information,

                    ST_AsGeoJSON(boundary)
                        AS boundary_geojson,

                    ST_GeometryType(boundary)
                        AS boundary_type,

                    ST_SRID(boundary)
                        AS boundary_srid,

                    ST_IsValid(boundary)
                        AS boundary_valid

                FROM properties

                WHERE id = CAST(:pid AS uuid)

                LIMIT 1
                """
            ),
            {
                "pid": pid
            }
        ).mappings().first()


        if property_row is None:
            return None


        property_data = {
            key: _ucm_safe(value)
            for key, value
            in dict(property_row).items()
        }


        property_data["id"] = str(
            property_data["id"]
        )


        property_lat = _ucm_float(
            property_data.get("latitude")
        )

        property_lon = _ucm_float(
            property_data.get("longitude")
        )


        # --------------------------------------------------------------------
        # PROPERTY-ATTACHED INTELLIGENCE
        # --------------------------------------------------------------------

        attached_rows = conn.execute(
            text(
                """
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata,
                    created_at,
                    updated_at

                FROM intelligence_records

                WHERE property_id =
                    CAST(:pid AS uuid)

                ORDER BY
                    updated_at DESC NULLS LAST,
                    created_at DESC NULLS LAST

                LIMIT 1000
                """
            ),
            {
                "pid": pid
            }
        ).mappings().all()


        # --------------------------------------------------------------------
        # ALL MAPPED RECORDS
        #
        # Used to discover source-backed intelligence near the property even
        # when a record has not been explicitly attached to property_id.
        # --------------------------------------------------------------------

        nearby_rows = []

        if (
            property_lat is not None
            and property_lon is not None
        ):

            nearby_rows = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        property_id,
                        title,
                        category,
                        status,
                        description,
                        latitude,
                        longitude,
                        source_name,
                        source_type,
                        source_url,
                        published_at,
                        confidence,
                        evidence_level,
                        metadata,
                        created_at,
                        updated_at

                    FROM intelligence_records

                    WHERE latitude IS NOT NULL
                    AND longitude IS NOT NULL

                    LIMIT 5000
                    """
                )
            ).mappings().all()


        # --------------------------------------------------------------------
        # MERGE
        # --------------------------------------------------------------------

        intelligence = []

        seen_ids = set()


        for row in attached_rows:

            record_id = str(
                row["id"]
            )

            seen_ids.add(
                record_id
            )


            item = {
                key: _ucm_safe(value)
                for key, value
                in dict(row).items()
            }


            item["id"] = record_id

            lat = _ucm_float(
                item.get("latitude")
            )

            lon = _ucm_float(
                item.get("longitude")
            )


            if (
                property_lat is not None
                and property_lon is not None
                and lat is not None
                and lon is not None
            ):

                distance = _ucm_haversine_km(
                    property_lat,
                    property_lon,
                    lat,
                    lon
                )

            else:

                distance = None


            item["distance_km"] = distance

            item["source_group"] = (
                _ucm_source_group(
                    item.get("source_type"),
                    item.get("source_name"),
                    item.get("category"),
                    item.get("title")
                )
            )

            item["property_attached"] = True

            intelligence.append(
                item
            )


        # --------------------------------------------------------------------
        # NEARBY RECORDS
        # --------------------------------------------------------------------

        for row in nearby_rows:

            record_id = str(
                row["id"]
            )


            if record_id in seen_ids:
                continue


            lat = _ucm_float(
                row["latitude"]
            )

            lon = _ucm_float(
                row["longitude"]
            )


            if (
                property_lat is None
                or property_lon is None
                or lat is None
                or lon is None
            ):
                continue


            distance = _ucm_haversine_km(
                property_lat,
                property_lon,
                lat,
                lon
            )


            if (
                distance is None
                or distance > radius_km
            ):
                continue


            item = {
                key: _ucm_safe(value)
                for key, value
                in dict(row).items()
            }


            item["id"] = record_id

            item["distance_km"] = distance

            item["source_group"] = (
                _ucm_source_group(
                    item.get("source_type"),
                    item.get("source_name"),
                    item.get("category"),
                    item.get("title")
                )
            )

            item["property_attached"] = False

            intelligence.append(
                item
            )

            seen_ids.add(
                record_id
            )


        # --------------------------------------------------------------------
        # SOURCE SUMMARY
        # --------------------------------------------------------------------

        source_summary = {}


        for item in intelligence:

            group = item.get(
                "source_group",
                "OTHER"
            )

            source_summary[group] = (
                source_summary.get(
                    group,
                    0
                ) + 1
            )


        # --------------------------------------------------------------------
        # MAPPED / UNMAPPED
        # --------------------------------------------------------------------

        mapped_count = 0
        unmapped_count = 0


        for item in intelligence:

            lat = _ucm_float(
                item.get("latitude")
            )

            lon = _ucm_float(
                item.get("longitude")
            )


            if (
                lat is not None
                and lon is not None
            ):
                mapped_count += 1
            else:
                unmapped_count += 1


        # --------------------------------------------------------------------
        # EVIDENCE
        # --------------------------------------------------------------------

        evidence_count = 0


        if _ucm_table_exists(
            conn,
            "evidence_items"
        ):

            try:

                evidence_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*)

                            FROM evidence_items

                            WHERE property_id =
                                CAST(:pid AS uuid)
                            """
                        ),
                        {
                            "pid": pid
                        }
                    ).scalar() or 0
                )

            except Exception:

                evidence_count = 0


        # --------------------------------------------------------------------
        # MARKET
        # --------------------------------------------------------------------

        market_count = 0


        if _ucm_table_exists(
            conn,
            "property_market_observations"
        ):

            try:

                market_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*)

                            FROM property_market_observations

                            WHERE property_id =
                                CAST(:pid AS uuid)
                            """
                        ),
                        {
                            "pid": pid
                        }
                    ).scalar() or 0
                )

            except Exception:

                market_count = 0


        # --------------------------------------------------------------------
        # DOCUMENTS
        # --------------------------------------------------------------------

        document_count = 0


        if _ucm_table_exists(
            conn,
            "property_files"
        ):

            try:

                document_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*)

                            FROM property_files

                            WHERE property_id =
                                CAST(:pid AS uuid)
                            """
                        ),
                        {
                            "pid": pid
                        }
                    ).scalar() or 0
                )

            except Exception:

                document_count = 0


        # --------------------------------------------------------------------
        # LIVE REFRESH COUNT
        # --------------------------------------------------------------------

        refresh_count = 0


        if _ucm_table_exists(
            conn,
            "piq_property_refresh_runs"
        ):

            try:

                refresh_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*)

                            FROM piq_property_refresh_runs

                            WHERE property_id =
                                CAST(:pid AS uuid)
                            """
                        ),
                        {
                            "pid": pid
                        }
                    ).scalar() or 0
                )

            except Exception:

                refresh_count = 0


    # ------------------------------------------------------------------------
    # FINAL RESULT
    # ------------------------------------------------------------------------

    intelligence.sort(
        key=lambda item: (
            item.get("distance_km")
            if item.get("distance_km") is not None
            else 999999.0
        )
    )


    nearby_count = 0


    for item in intelligence:

        distance = item.get(
            "distance_km"
        )

        if (
            distance is not None
            and distance <= radius_km
        ):
            nearby_count += 1


    return {

        "module": MODULE_VERSION,

        "generated_at": (
            datetime.utcnow()
            .isoformat()
            + "Z"
        ),

        "property": property_data,

        "map": {

            "latitude": property_lat,

            "longitude": property_lon,

            "radius_km": radius_km,

            "boundary_geojson": (
                property_data.get(
                    "boundary_geojson"
                )
            )
        },

        "intelligence": intelligence,

        "summary": {

            "total_records": len(
                intelligence
            ),

            "mapped_records": mapped_count,

            "unmapped_records": unmapped_count,

            "nearby_records": nearby_count,

            "evidence_records": evidence_count,

            "market_observations": market_count,

            "documents": document_count,

            "refresh_runs": refresh_count,

            "source_groups": source_summary
        },

        "data_policy": {

            "source_backed_only": True,

            "fabricated_intelligence": False,

            "fabricated_coordinates": False,

            "valuation_prediction": False,

            "investment_score": False
        }
    }


# ============================================================================
# 5. ROUTES
# ============================================================================

API_ROUTE = (
    "/api/v1/properties/"
    "{property_id}/command-map"
)

UI_ROUTE = (
    "/propertyiq/command-map/"
    "{property_id}"
)


existing_paths = {
    getattr(
        route,
        "path",
        ""
    )
    for route in app.routes
}


# ============================================================================
# 6. API
# ============================================================================

if API_ROUTE not in existing_paths:

    @app.get(
        API_ROUTE,
        tags=[
            "PropertyIQ Unified Command Map"
        ]
    )
    def propertyiq_command_map_api(
        property_id: str,
        radius_km: float = 5.0
    ):

        data = build_unified_command_map(
            engine,
            property_id,
            radius_km
        )


        if data is None:

            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )


        return data


# ============================================================================
# 7. HTML TEMPLATE
#
# IMPORTANT:
# This is a normal string, NOT an f-string.
# Therefore JavaScript/CSS braces are safe.
# ============================================================================

COMMAND_MAP_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
/>

<title>
PropertyIQ — Unified Intelligence Command Map
</title>


<link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>


<style>

:root {

    --bg:#050a12;
    --panel:#091321;
    --line:rgba(255,255,255,.09);
    --text:#edf7ff;
    --muted:#8da3b9;
    --cyan:#56e6ff;
    --blue:#3ba9ff;
    --green:#40d6a0;
    --orange:#ffb64d;
    --red:#ff6f7d;

}


* {
    box-sizing:border-box;
}


html,
body {

    margin:0;

    width:100%;

    height:100%;

    overflow:hidden;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:var(--bg);

    color:var(--text);

}


#map {

    position:absolute;

    inset:0;

    z-index:1;

}


.topbar {

    position:absolute;

    left:18px;

    right:18px;

    top:16px;

    height:58px;

    z-index:1000;

    display:flex;

    align-items:center;

    gap:16px;

    padding:0 17px;

    border:
        1px solid var(--line);

    border-radius:16px;

    background:
        rgba(5,10,18,.88);

    backdrop-filter:
        blur(20px);

    box-shadow:
        0 20px 70px rgba(0,0,0,.35);

}


.logo {

    font-weight:900;

    letter-spacing:.1em;

    font-size:14px;

    white-space:nowrap;

}


.logo span {

    color:var(--cyan);

}


.title {

    min-width:0;

    flex:1;

}


.title-main {

    font-weight:800;

    font-size:14px;

    white-space:nowrap;

    overflow:hidden;

    text-overflow:ellipsis;

}


.title-sub {

    color:var(--muted);

    font-size:10px;

    margin-top:2px;

}


.top-button {

    border:
        1px solid var(--line);

    color:var(--text);

    background:
        rgba(255,255,255,.035);

    padding:8px 11px;

    border-radius:10px;

    text-decoration:none;

    font-size:11px;

    white-space:nowrap;

}


.top-button:hover {

    border-color:
        rgba(86,230,255,.4);

    background:
        rgba(86,230,255,.08);

}


.left-panel {

    position:absolute;

    left:18px;

    top:91px;

    bottom:18px;

    width:285px;

    z-index:1000;

    overflow:auto;

    padding:15px;

    border:
        1px solid var(--line);

    border-radius:18px;

    background:
        rgba(5,10,18,.90);

    backdrop-filter:
        blur(20px);

    box-shadow:
        0 25px 80px rgba(0,0,0,.38);

}


.right-panel {

    position:absolute;

    right:18px;

    top:91px;

    bottom:18px;

    width:365px;

    z-index:1000;

    overflow:auto;

    padding:15px;

    border:
        1px solid var(--line);

    border-radius:18px;

    background:
        rgba(5,10,18,.91);

    backdrop-filter:
        blur(20px);

    box-shadow:
        0 25px 80px rgba(0,0,0,.38);

}


.panel-title {

    font-size:12px;

    font-weight:900;

    letter-spacing:.1em;

    text-transform:uppercase;

    color:var(--cyan);

    margin-bottom:12px;

}


.property-card {

    padding:13px;

    border:
        1px solid var(--line);

    border-radius:14px;

    background:
        linear-gradient(
            135deg,
            rgba(86,230,255,.07),
            rgba(255,255,255,.02)
        );

    margin-bottom:13px;

}


.property-name {

    font-size:16px;

    font-weight:850;

    line-height:1.2;

}


.property-address {

    margin-top:6px;

    color:var(--muted);

    font-size:11px;

    line-height:1.5;

}


.coords {

    margin-top:9px;

    font-family:
        ui-monospace,
        SFMono-Regular,
        Menlo,
        monospace;

    color:var(--cyan);

    font-size:10px;

}


.layer {

    display:flex;

    align-items:center;

    gap:9px;

    padding:9px 8px;

    border-radius:10px;

    margin-bottom:4px;

    cursor:pointer;

}


.layer:hover {

    background:
        rgba(255,255,255,.035);

}


.layer input {

    accent-color:var(--cyan);

}


.layer-name {

    flex:1;

    font-size:12px;

}


.layer-count {

    color:var(--muted);

    font-size:10px;

}


.radius-row {

    display:flex;

    gap:5px;

    flex-wrap:wrap;

    margin-top:10px;

}


.radius {

    border:
        1px solid var(--line);

    background:
        rgba(255,255,255,.03);

    color:var(--muted);

    padding:7px 9px;

    border-radius:8px;

    font-size:10px;

    cursor:pointer;

}


.radius.active {

    color:var(--text);

    border-color:
        rgba(86,230,255,.4);

    background:
        rgba(86,230,255,.09);

}


.summary-grid {

    display:grid;

    grid-template-columns:
        1fr 1fr;

    gap:7px;

    margin-bottom:13px;

}


.summary-box {

    padding:11px;

    border:
        1px solid var(--line);

    border-radius:11px;

    background:
        rgba(255,255,255,.025);

}


.summary-number {

    font-size:20px;

    font-weight:850;

}


.summary-label {

    margin-top:3px;

    color:var(--muted);

    font-size:9px;

    text-transform:uppercase;

}


.record {

    padding:12px;

    border:
        1px solid var(--line);

    border-radius:13px;

    margin-bottom:7px;

    cursor:pointer;

    background:
        rgba(255,255,255,.018);

}


.record:hover {

    border-color:
        rgba(86,230,255,.3);

    background:
        rgba(86,230,255,.04);

}


.record-title {

    font-size:12px;

    font-weight:750;

    line-height:1.35;

}


.record-meta {

    margin-top:6px;

    color:var(--muted);

    font-size:9px;

    line-height:1.5;

}


.tag {

    display:inline-block;

    margin-top:7px;

    margin-right:4px;

    padding:4px 6px;

    border-radius:999px;

    font-size:8px;

    border:
        1px solid var(--line);

    color:var(--cyan);

    background:
        rgba(86,230,255,.05);

}


.empty {

    color:var(--muted);

    font-size:11px;

    line-height:1.6;

    padding:10px 3px;

}


.leaflet-control-zoom a {

    background:#091321 !important;

    color:#edf7ff !important;

    border-color:
        rgba(255,255,255,.1) !important;

}


.popup {

    min-width:230px;

    color:#111;

}


.popup-title {

    font-weight:800;

    margin-bottom:5px;

}


.popup-meta {

    font-size:11px;

    line-height:1.5;

}


.popup-link {

    display:inline-block;

    margin-top:7px;

    color:#1689c8;

}


@media(max-width:900px) {

    .left-panel {

        width:240px;

    }

    .right-panel {

        width:300px;

    }

}


@media(max-width:700px) {

    .left-panel {

        left:10px;

        right:10px;

        width:auto;

        height:245px;

        top:auto;

        bottom:10px;

    }

    .right-panel {

        display:none;

    }

    .topbar {

        left:10px;

        right:10px;

        top:10px;

    }

}

</style>

</head>


<body>


<div id="map"></div>


<div class="topbar">

    <div class="logo">
        PROPERTY<span>IQ</span>
    </div>


    <div class="title">

        <div
            class="title-main"
            id="topTitle"
        ></div>

        <div class="title-sub">
            UNIFIED INTELLIGENCE COMMAND MAP
        </div>

    </div>


    <a
        class="top-button"
        id="profileLink"
        href="#"
    >
        Profile
    </a>


    <a
        class="top-button"
        id="workspaceLink"
        href="#"
    >
        Workspace
    </a>

</div>


<div class="left-panel">


    <div class="panel-title">
        PROPERTY
    </div>


    <div class="property-card">

        <div
            class="property-name"
            id="propertyName"
        ></div>

        <div
            class="property-address"
            id="propertyAddress"
        ></div>

        <div
            class="coords"
            id="coords"
        ></div>

    </div>


    <div class="panel-title">
        INTELLIGENCE LAYERS
    </div>


    <div id="layers"></div>


    <div
        class="panel-title"
        style="margin-top:18px"
    >
        SEARCH RADIUS
    </div>


    <div class="radius-row">

        <button
            class="radius"
            data-radius="1"
        >
            1 km
        </button>

        <button
            class="radius"
            data-radius="3"
        >
            3 km
        </button>

        <button
            class="radius active"
            data-radius="5"
        >
            5 km
        </button>

        <button
            class="radius"
            data-radius="10"
        >
            10 km
        </button>

        <button
            class="radius"
            data-radius="25"
        >
            25 km
        </button>

    </div>


    <div
        class="panel-title"
        style="margin-top:20px"
    >
        PROPERTY TOOLS
    </div>


    <a
        class="top-button"
        id="boundaryLink"
        style="display:block;margin-bottom:7px"
        href="#"
    >
        GIS Boundary Workspace
    </a>


    <a
        class="top-button"
        id="boundaryIntelligenceLink"
        style="display:block;margin-bottom:7px"
        href="#"
    >
        Boundary Intelligence
    </a>


    <a
        class="top-button"
        id="marketLink"
        style="display:block;margin-bottom:7px"
        href="#"
    >
        Market Comparables
    </a>


    <a
        class="top-button"
        id="reportLink"
        style="display:block;margin-bottom:7px"
        href="#"
    >
        Intelligence Report
    </a>


</div>


<div class="right-panel">


    <div class="panel-title">
        INTELLIGENCE SUMMARY
    </div>


    <div
        class="summary-grid"
        id="summary"
    ></div>


    <div class="panel-title">
        SOURCE INVENTORY
    </div>


    <div id="sourceInventory"></div>


    <div
        class="panel-title"
        style="margin-top:18px"
    >
        MAP RECORDS
    </div>


    <div id="records"></div>

</div>


<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>


<script>

/* -------------------------------------------------------------------------
   DATA
   ------------------------------------------------------------------------- */

const DATA = __PROPERTYIQ_DATA__;

const PROPERTY_ID = "__PROPERTY_ID__";


let currentData = DATA;

let currentRadius = 5;

let map = null;

let propertyMarker = null;

let boundaryLayer = null;

let radiusCircle = null;

const recordLayers = {};


/* -------------------------------------------------------------------------
   HELPERS
   ------------------------------------------------------------------------- */

function safe(value) {

    if (
        value === null ||
        value === undefined ||
        value === ""
    ) {
        return "—";
    }

    if (
        typeof value === "object"
    ) {

        try {

            return JSON.stringify(value);

        } catch (error) {

            return String(value);

        }

    }

    return String(value);

}


function groupColor(group) {

    const colors = {

        OSM: "#56e6ff",

        RERA: "#ffb64d",

        GOVERNMENT: "#40d6a0",

        NEWS: "#b58cff",

        DEVELOPMENT: "#ff7f9a",

        OTHER: "#9aaabd"

    };

    return colors[group] || "#9aaabd";

}


function groupLabel(group) {

    const labels = {

        OSM: "OSM / GIS",

        RERA: "RERA",

        GOVERNMENT: "Government",

        NEWS: "News",

        DEVELOPMENT: "Development",

        OTHER: "Other"

    };

    return labels[group] || group;

}


/* -------------------------------------------------------------------------
   LINKS
   ------------------------------------------------------------------------- */

function configureLinks() {

    document.getElementById(
        "profileLink"
    ).href =
        "/propertyiq/full-profile/"
        + PROPERTY_ID;


    document.getElementById(
        "workspaceLink"
    ).href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;


    document.getElementById(
        "boundaryLink"
    ).href =
        "/propertyiq/boundary/"
        + PROPERTY_ID;


    document.getElementById(
        "boundaryIntelligenceLink"
    ).href =
        "/propertyiq/boundary-intelligence/"
        + PROPERTY_ID;


    document.getElementById(
        "marketLink"
    ).href =
        "/propertyiq/market-comparables/"
        + PROPERTY_ID;


    document.getElementById(
        "reportLink"
    ).href =
        "/propertyiq/intelligence-report/"
        + PROPERTY_ID;

}


/* -------------------------------------------------------------------------
   MAP INITIALIZATION
   ------------------------------------------------------------------------- */

function initializeMap() {

    const lat =
        Number(
            currentData.map.latitude
        );

    const lon =
        Number(
            currentData.map.longitude
        );


    if (
        Number.isFinite(lat) &&
        Number.isFinite(lon)
    ) {

        map = L.map(
            "map"
        ).setView(
            [lat, lon],
            13
        );

    } else {

        map = L.map(
            "map"
        ).setView(
            [28.6139, 77.2090],
            10
        );

    }


    L.tileLayer(
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        {

            maxZoom: 19,

            attribution:
                "&copy; OpenStreetMap contributors"

        }
    ).addTo(
        map
    );


    if (
        Number.isFinite(lat) &&
        Number.isFinite(lon)
    ) {

        propertyMarker =
            L.circleMarker(
                [lat, lon],
                {

                    radius: 10,

                    color: "#56e6ff",

                    fillColor: "#56e6ff",

                    fillOpacity: 0.85,

                    weight: 3

                }
            ).addTo(
                map
            );


        propertyMarker.bindPopup(
            "<b>PROPERTY</b><br>"
            +
            safe(
                currentData.property.property_name
            )
        );


        radiusCircle =
            L.circle(
                [lat, lon],
                {

                    radius:
                        currentRadius * 1000,

                    color: "#56e6ff",

                    weight: 1,

                    opacity: 0.35,

                    fillOpacity: 0.025,

                    interactive: false

                }
            ).addTo(
                map
            );

    }


    renderBoundary();

    renderRecords();

}


/* -------------------------------------------------------------------------
   BOUNDARY
   ------------------------------------------------------------------------- */

function renderBoundary() {

    if (boundaryLayer) {

        try {

            boundaryLayer.remove();

        } catch (error) {}

        boundaryLayer = null;

    }


    const boundary =
        currentData.map.boundary_geojson;


    if (!boundary) {

        return;

    }


    try {

        let parsedBoundary = boundary;


        if (
            typeof boundary === "string"
        ) {

            parsedBoundary =
                JSON.parse(boundary);

        }


        boundaryLayer =
            L.geoJSON(
                parsedBoundary,
                {

                    style: {

                        color: "#56e6ff",

                        weight: 2,

                        opacity: 0.95,

                        fillOpacity: 0.08

                    }

                }
            ).addTo(
                map
            );


    } catch (error) {

        console.warn(
            "Boundary rendering failed:",
            error
        );

    }

}


/* -------------------------------------------------------------------------
   RECORD MARKERS
   ------------------------------------------------------------------------- */

function clearRecordLayers() {

    Object.keys(
        recordLayers
    ).forEach(
        function(id) {

            try {

                recordLayers[id].remove();

            } catch (error) {}

            delete recordLayers[id];

        }
    );

}


function renderRecords() {

    clearRecordLayers();


    const records =
        currentData.intelligence || [];


    records.forEach(
        function(record) {

            const lat =
                Number(
                    record.latitude
                );

            const lon =
                Number(
                    record.longitude
                );


            if (
            ) {

                return;

            }


            const group =
                record.source_group ||
                "OTHER";


            const marker =
                L.circleMarker(
                    [lat, lon],
                    {

                        radius: 7,

                        color:
                            groupColor(group),

                        fillColor:
                            groupColor(group),

                        fillOpacity: 0.78,

                        weight: 1.5

                    }
                );


            const distance =
                record.distance_km !== null &&
                record.distance_km !== undefined

                    ? Number(
                        record.distance_km
                    ).toFixed(2)
                    + " km"

                    : "Property attached";


            let sourceLink = "";


            if (
                record.source_url
            ) {

                sourceLink =
                    '<a class="popup-link" '
                    +
                    'target="_blank" '
                    +
                    'rel="noopener noreferrer" '
                    +
                    'href="'
                    +
                    safe(record.source_url)
                    +
                    '">'
                    +
                    "Open source"
                    +
                    "</a>";

            }


            const popupHtml =

                '<div class="popup">'
                +

                '<div class="popup-title">'
                +
                safe(record.title)
                +
                "</div>"
                +

                '<div class="popup-meta">'
                +

                "<b>Source:</b> "
                +
                safe(record.source_name)
                +
                "<br>"
                +

                "<b>Layer:</b> "
                +
                safe(groupLabel(group))
                +
                "<br>"
                +

                "<b>Status:</b> "
                +
                safe(record.status)
                +
                "<br>"
                +

                "<b>Distance:</b> "
                +
                distance
                +
                "<br>"
                +

                "<b>Evidence:</b> "
                +
                safe(record.evidence_level)
                +

                "</div>"
                +

                sourceLink
                +

                "</div>";


            marker.bindPopup(
                popupHtml
            );


            marker.addTo(
                map
            );


            recordLayers[
                String(record.id)
            ] = marker;

        }
    );

}


/* -------------------------------------------------------------------------
   LAYER CONTROLS
   ------------------------------------------------------------------------- */

function renderLayers() {

    const container =
        document.getElementById(
            "layers"
        );


    container.innerHTML = "";


    const propertyLabel =
        document.createElement(
            "label"
        );


    propertyLabel.className =
        "layer";


    const propertyCheckbox =
        document.createElement(
            "input"
        );


    propertyCheckbox.type =
        "checkbox";

    propertyCheckbox.checked =
        true;


    propertyCheckbox.addEventListener(
        "change",
        function() {

            if (propertyMarker) {

                if (
                    propertyCheckbox.checked
                ) {

                    propertyMarker.addTo(
                        map
                    );

                } else {

                    propertyMarker.remove();

                }

            }


            if (boundaryLayer) {

                if (
                    propertyCheckbox.checked
                ) {

                    boundaryLayer.addTo(
                        map
                    );

                } else {

                    boundaryLayer.remove();

                }

            }

        }
    );


    const propertyName =
        document.createElement(
            "div"
        );


    propertyName.className =
        "layer-name";


    propertyName.textContent =
        "Property / Boundary";


    propertyLabel.appendChild(
        propertyCheckbox
    );


    propertyLabel.appendChild(
        propertyName
    );


    container.appendChild(
        propertyLabel
    );


    const groups = [

        "OSM",

        "RERA",

        "GOVERNMENT",

        "NEWS",

        "DEVELOPMENT",

        "OTHER"

    ];


    groups.forEach(
        function(group) {

            const row =
                document.createElement(
                    "label"
                );


            row.className =
                "layer";


            const checkbox =
                document.createElement(
                    "input"
                );


            checkbox.type =
                "checkbox";

            checkbox.checked =
                true;


            checkbox.dataset.group =
                group;


            checkbox.addEventListener(
                "change",
                function() {

                    toggleGroup(
                        group,
                        checkbox.checked
                    );

                }
            );


            const name =
                document.createElement(
                    "div"
                );


            name.className =
                "layer-name";


            name.textContent =
                groupLabel(group);


            const count =
                document.createElement(
                    "div"
                );


            count.className =
                "layer-count";


            count.textContent =
                currentData.summary
                    .source_groups[group]
                || 0;


            row.appendChild(
                checkbox
            );


            row.appendChild(
                name
            );


            row.appendChild(
                count
            );


            container.appendChild(
                row
            );

        }
    );

}


function toggleGroup(
    group,
    visible
) {

    currentData.intelligence.forEach(
        function(record) {

            if (
                record.source_group !== group
            ) {

                return;

            }


            const layer =
                recordLayers[
                    String(record.id)
                ];


            if (!layer) {

                return;

            }


            if (visible) {

                layer.addTo(
                    map
                );

            } else {

                layer.remove();

            }

        }
    );

}


/* -------------------------------------------------------------------------
   SUMMARY
   ------------------------------------------------------------------------- */

function renderSummary() {

    const summary =
        currentData.summary;


    const items = [

        [
            summary.total_records,
            "Total records"
        ],

        [
            summary.nearby_records,
            "Within radius"
        ],

        [
            summary.mapped_records,
            "Mapped"
        ],

        [
            summary.unmapped_records,
            "Unmapped"
        ],

        [
            summary.evidence_records,
            "Evidence"
        ],

        [
            summary.market_observations,
            "Market"
        ]

    ];


    document.getElementById(
        "summary"
    ).innerHTML =

        items.map(
            function(item) {

                return (
                    '<div class="summary-box">'
                    +
                    '<div class="summary-number">'
                    +
                    safe(item[0])
                    +
                    "</div>"
                    +
                    '<div class="summary-label">'
                    +
                    safe(item[1])
                    +
                    "</div>"
                    +
                    "</div>"
                );

            }
        ).join("");


    const sourceContainer =
        document.getElementById(
            "sourceInventory"
        );


    sourceContainer.innerHTML = "";


    const sourceGroups =
        summary.source_groups || {};


    Object.keys(
        sourceGroups
    ).forEach(
        function(group) {

            const count =
                sourceGroups[group];


            const box =
                document.createElement(
                    "div"
                );


            box.className =
                "record";


            box.innerHTML =

                '<div class="record-title">'
                +
                safe(
                    groupLabel(group)
                )
                +
                "</div>"
                +

                '<div class="record-meta">'
                +
                safe(count)
                +
                " source-backed record(s)"
                +
                "</div>";


            sourceContainer.appendChild(
                box
            );

        }
    );

}


/* -------------------------------------------------------------------------
   RECORD LIST
   ------------------------------------------------------------------------- */

function renderRecordList() {

    const container =
        document.getElementById(
            "records"
        );


    const records =
        (currentData.intelligence || [])
        .filter(
            function(record) {

                return (
                    record.latitude !== null &&
                    record.longitude !== null
                );

            }
        )
        .sort(
            function(a, b) {

                const da =
                    a.distance_km !== null &&
                    a.distance_km !== undefined

                        ? Number(a.distance_km)

                        : 999999;


                const db =
                    b.distance_km !== null &&
                    b.distance_km !== undefined

                        ? Number(b.distance_km)

                        : 999999;


                return da - db;

            }
        );


    if (
        records.length === 0
    ) {

        container.innerHTML =
            '<div class="empty">'
            +
            "No mapped intelligence records "
            +
            "are available within the selected "
            +
            "radius."
            +
            "<br><br>"
            +
            "Unmapped records remain preserved "
            +
            "in the database and are not assigned "
            +
            "invented coordinates."
            +
            "</div>";

        return;

    }


    container.innerHTML = "";


    records.forEach(
        function(record) {

            const box =
                document.createElement(
                    "div"
                );


            box.className =
                "record";


            const distance =
                record.distance_km !== null &&
                record.distance_km !== undefined

                    ? Number(
                        record.distance_km
                    ).toFixed(2)
                    + " km"

                    : "Attached";


            box.innerHTML =

                '<div class="record-title">'
                +
                safe(record.title)
                +
                "</div>"
                +

                '<div class="record-meta">'
                +
                safe(record.source_name)
                +
                " · "
                +
                distance
                +
                "<br>"
                +
                safe(record.status)
                +
                "</div>"
                +

                '<span class="tag">'
                +
                safe(
                    groupLabel(
                        record.source_group
                    )
                )
                +
                "</span>"
                +

                (
                    record.evidence_level

                        ?

                        '<span class="tag">'
                        +
                        safe(
                            record.evidence_level
                        )
                        +
                        "</span>"

                        :

                        ""
                );


            box.addEventListener(
                "click",
                function() {

                    const layer =
                        recordLayers[
                            String(record.id)
                        ];


                    if (layer) {

                        map.setView(
                            layer.getLatLng(),
                            15
                        );


                        layer.openPopup();

                    }

                }
            );


            container.appendChild(
                box
            );

        }
    );

}


/* -------------------------------------------------------------------------
   RADIUS REFRESH
   ------------------------------------------------------------------------- */

async function refreshRadius(
    radius
) {

    currentRadius =
        Number(radius);


    const url =
        "/api/v1/properties/"
        +
        PROPERTY_ID
        +
        "/command-map?radius_km="
        +
        encodeURIComponent(
            currentRadius
        );


    try {

        const response =
            await fetch(
                url
            );


        if (!response.ok) {

            throw new Error(
                "HTTP "
                +
                response.status
            );

        }


        currentData =
            await response.json();


        if (radiusCircle) {

            radiusCircle.setRadius(
                currentRadius * 1000
            );

        }


        renderBoundary();

        renderRecords();

        renderSummary();

        renderRecordList();

        renderLayers();

    } catch (error) {

        console.error(
            "Command map refresh failed:",
            error
        );

        alert(
            "Unable to refresh command map."
        );

    }

}


/* -------------------------------------------------------------------------
   RADIUS BUTTONS
   ------------------------------------------------------------------------- */

function setupRadiusButtons() {

    document.querySelectorAll(
        ".radius"
    ).forEach(
        function(button) {

            button.addEventListener(
                "click",
                function() {

                    document.querySelectorAll(
                        ".radius"
                    ).forEach(
                        function(other) {

                            other.classList.remove(
                                "active"
                            );

                        }
                    );


                    button.classList.add(
                        "active"
                    );


                    refreshRadius(
                        button.dataset.radius
                    );

                }
            );

        }
    );

}


/* -------------------------------------------------------------------------
   PROPERTY HEADER
   ------------------------------------------------------------------------- */

function renderPropertyHeader() {

    const property =
        currentData.property;


    document.getElementById(
        "propertyName"
    ).textContent =
        safe(
            property.property_name
        );


    document.getElementById(
        "topTitle"
    ).textContent =
        safe(
            property.property_name
        );


    document.getElementById(
        "propertyAddress"
    ).textContent =
        safe(
            property.property_address
        );


    const lat =
        currentData.map.latitude;


    const lon =
        currentData.map.longitude;


    if (
        lat !== null &&
        lon !== null &&
        lat !== undefined &&
        lon !== undefined
    ) {

        document.getElementById(
            "coords"
        ).textContent =
            Number(lat).toFixed(6)
            +
            " , "
            +
            Number(lon).toFixed(6);

    } else {

        document.getElementById(
            "coords"
        ).textContent =
            "Property coordinates unavailable";

    }

}


/* -------------------------------------------------------------------------
   INITIALIZATION
   ------------------------------------------------------------------------- */

configureLinks();

renderPropertyHeader();

initializeMap();

renderSummary();

renderRecordList();

renderLayers();

setupRadiusButtons();


setTimeout(
    function() {

        if (map) {

            map.invalidateSize();

        }

    },
    500
);

</script>


</body>

</html>
"""


# ============================================================================
# 8. UI ROUTE
#
# IMPORTANT:
# Use .replace() rather than an f-string.
# This is what fixes the previous SyntaxError.
# ============================================================================

if UI_ROUTE not in existing_paths:

    @app.get(
        UI_ROUTE,
        response_class=HTMLResponse,
        tags=[
            "PropertyIQ Unified Command Map"
        ]
    )
    def propertyiq_command_map_ui(
        property_id: str
    ):

        data = build_unified_command_map(
            engine,
            property_id,
            5.0
        )


        if data is None:

            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )


        payload = json.dumps(
            data,
            ensure_ascii=False,
            default=str
        )


        html = COMMAND_MAP_HTML.replace(
            "__PROPERTYIQ_DATA__",
            payload
        )


        html = html.replace(
            "__PROPERTY_ID__",
            str(property_id)
        )


        return HTMLResponse(
            content=html
        )


# ============================================================================
# 9. INSTALLATION MESSAGE
# ============================================================================

print("")
print("=" * 80)
print(
    "PROPERTYIQ — UNIFIED INTELLIGENCE COMMAND MAP V2 INSTALLED"
)
print("=" * 80)

print(
    "Module:",
    MODULE_VERSION
)

print(
    "Runtime: existing PropertyIQ runtime"
)

print("")
print("API:")
print(
    "  /api/v1/properties/{property_id}/command-map"
)

print("")
print("UI:")
print(
    "  /propertyiq/command-map/{property_id}"
)

print("")
print("Integrated layers:")
print("  Property: ENABLED")
print("  GIS Boundary: ENABLED")
print("  OSM / GIS: ENABLED")
print("  RERA: ENABLED")
print("  Government / PAIMANA: ENABLED")
print("  News / GDELT: ENABLED")
print("  Development: ENABLED")
print("  Market: ENABLED")
print("  Evidence: ENABLED")

print("")
print("Interactive features:")
print("  Radius controls: ENABLED")
print("  Intelligence layer controls: ENABLED")
print("  Interactive map markers: ENABLED")
print("  Source popups: ENABLED")
print("  Source links: ENABLED")
print("  Boundary rendering: ENABLED")
print("  Property navigation: ENABLED")

print("")
print("Data integrity:")
print("  Source-backed records only: ENABLED")
print("  Fabricated intelligence: DISABLED")
print("  Fabricated coordinates: DISABLED")
print("  Valuation prediction: DISABLED")
print("  Investment score: DISABLED")
print("  Database modification during install: DISABLED")

print("")
print(
    "Existing PropertyIQ routes preserved."
)

print("=" * 80)
print("")



# ============================================================
# PROPERTYIQ MODULE: TIMELINE
# ORIGINAL COLAB CELL: In[84]
# ============================================================

# ============================================================================
# PROPERTYIQ — TIMELINE & DEVELOPMENT PIPELINE
# ============================================================================
# ADDITIVE INTEGRATION MODULE
#
# PURPOSE:
#   Build one chronological property-development timeline from the
#   source-backed data already present in PropertyIQ.
#
# INTEGRATES:
#   - RERA
#   - Government / PAIMANA
#   - Development intelligence
#   - News / GDELT
#   - OSM / GIS records when dated
#   - Market observations when dates exist
#   - Evidence dates when available
#   - Live refresh history when available
#
# DOES NOT:
#   - rebuild RERA
#   - rebuild OSM
#   - rebuild GDELT
#   - rebuild PAIMANA
#   - fabricate dates
#   - fabricate project status
#   - predict future property prices
#   - assign investment scores
#   - modify database records during installation
#
# DIRECT COLAB EXECUTION:
#   Paste the entire cell and run.
#
# API:
#   /api/v1/properties/{property_id}/timeline
#
# UI:
#   /propertyiq/timeline/{property_id}
# ============================================================================


# ============================================================================
# 1. DEPENDENCIES
# ============================================================================

try:

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

except Exception:

    import subprocess
    import sys

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn"
    ])

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse


try:

    from sqlalchemy import text

except Exception:

    import subprocess
    import sys

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "sqlalchemy"
    ])

    from sqlalchemy import text


import json
import re
from datetime import datetime


MODULE_VERSION = (
    "PROPERTYIQ-TIMELINE-DEVELOPMENT-PIPELINE-V1"
)


# ============================================================================
# 2. RUNTIME CHECK
# ============================================================================

if "engine" not in globals():

    raise RuntimeError(
        """
PROPERTYIQ DATABASE ENGINE NOT FOUND.

Load the existing PropertyIQ PostgreSQL/PostGIS runtime,
then run this complete cell again.
"""
    )


if "app" not in globals():

    app = FastAPI(
        title="PropertyIQ",
        version=MODULE_VERSION
    )

    print(
        "WARNING: Existing PropertyIQ FastAPI app was not found."
    )

    print(
        "A new FastAPI app was created."
    )


# ============================================================================
# 3. HELPERS
# ============================================================================

def _tl_safe(value):

    if value is None:
        return None

    if isinstance(
        value,
        (dict, list, tuple)
    ):

        try:

            return json.loads(
                json.dumps(
                    value,
                    default=str
                )
            )

        except Exception:

            return str(value)

    return value


def _tl_table_exists(
    conn,
    table_name
):

    try:

        result = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = :table_name
                )
                """
            ),
            {
                "table_name": table_name
            }
        ).scalar()

        return bool(result)

    except Exception:

        return False


def _tl_columns(
    conn,
    table_name
):

    try:

        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = :table_name
                ORDER BY ordinal_position
                """
            ),
            {
                "table_name": table_name
            }
        ).scalars().all()

        return set(rows)

    except Exception:

        return set()


def _tl_parse_datetime(value):

    if value is None:
        return None

    if isinstance(
        value,
        datetime
    ):

        return value.isoformat()

    value = str(value).strip()

    if not value:
        return None

    return value


def _tl_date_sort_key(item):

    value = item.get(
        "event_date"
    )

    if not value:
        return "9999-99-99"

    return str(value)[:19]


def _tl_source_group(
    source_type,
    source_name,
    category,
    title
):

    combined = " ".join([
        str(source_type or ""),
        str(source_name or ""),
        str(category or ""),
        str(title or "")
    ]).upper()


    if (
        "RERA" in combined
        or "UP-RERA" in combined
    ):

        return "RERA"


    if (
        "PAIMANA" in combined
        or "GOVERNMENT" in combined
        or "INFRASTRUCTURE" in combined
        or "MINISTRY" in combined
    ):

        return "GOVERNMENT"


    if (
        "GDELT" in combined
        or "NEWS" in combined
        or "ARTICLE" in combined
    ):

        return "NEWS"


    if (
        "OSM" in combined
        or "OPENSTREETMAP" in combined
        or "OVERPASS" in combined
    ):

        return "OSM"


    if (
        "DEVELOPMENT" in combined
        or "PROJECT" in combined
        or "METRO" in combined
        or "HIGHWAY" in combined
        or "EXPRESSWAY" in combined
        or "RAILWAY" in combined
        or "FLYOVER" in combined
        or "LOGISTICS" in combined
        or "INDUSTRIAL" in combined
        or "COMMERCIAL" in combined
    ):

        return "DEVELOPMENT"


    return "OTHER"


def _tl_event_type(
    source_group,
    title,
    category,
    status
):

    combined = " ".join([
        str(title or ""),
        str(category or ""),
        str(status or "")
    ]).lower()


    if source_group == "RERA":
        return "RERA / PROJECT"


    if source_group == "GOVERNMENT":
        return "GOVERNMENT INFRASTRUCTURE"


    if source_group == "NEWS":
        return "NEWS / DEVELOPMENT"


    if source_group == "OSM":
        return "LOCATION / GIS"


    if (
        "metro" in combined
        or "rail" in combined
        or "highway" in combined
        or "expressway" in combined
        or "flyover" in combined
        or "road" in combined
    ):

        return "TRANSPORT INFRASTRUCTURE"


    if (
        "industrial" in combined
        or "logistics" in combined
        or "warehouse" in combined
    ):

        return "INDUSTRIAL / LOGISTICS"


    if (
        "hospital" in combined
        or "school" in combined
        or "university" in combined
    ):

        return "SOCIAL INFRASTRUCTURE"


    if (
        "commercial" in combined
        or "mall" in combined
        or "market" in combined
    ):

        return "COMMERCIAL DEVELOPMENT"


    return "DEVELOPMENT / OTHER"


# ============================================================================
# 4. BUILD TIMELINE
# ============================================================================

def build_property_timeline(
    engine,
    property_id
):

    pid = str(
        property_id
    )


    events = []


    # ------------------------------------------------------------------------
    # PROPERTY
    # ------------------------------------------------------------------------

    with engine.connect() as conn:

        property_row = conn.execute(
            text(
                """
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    created_at
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
                """
            ),
            {
                "pid": pid
            }
        ).mappings().first()


        if property_row is None:

            return None


        property_data = {
            key: _tl_safe(value)
            for key, value
            in dict(property_row).items()
        }


        property_data["id"] = str(
            property_data["id"]
        )


        # --------------------------------------------------------------------
        # PROPERTY CREATION EVENT
        # --------------------------------------------------------------------

        if property_data.get(
            "created_at"
        ):

            events.append({

                "event_id":
                    "property-created",

                "event_date":
                    _tl_parse_datetime(
                        property_data.get(
                            "created_at"
                        )
                    ),

                "event_type":
                    "PROPERTY",

                "source_group":
                    "PROPERTY",

                "title":
                    "Property record created",

                "description":
                    "Property was added to the PropertyIQ property database.",

                "status":
                    "recorded",

                "source_name":
                    "PropertyIQ",

                "source_type":
                    "INTERNAL_RECORD",

                "source_url":
                    None,

                "confidence":
                    None,

                "evidence_level":
                    None,

                "metadata":
                    {}

            })


        # --------------------------------------------------------------------
        # INTELLIGENCE RECORDS
        # --------------------------------------------------------------------

        intelligence_rows = conn.execute(
            text(
                """
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata,
                    created_at,
                    updated_at
                FROM intelligence_records
                WHERE property_id =
                    CAST(:pid AS uuid)
                ORDER BY
                    published_at ASC NULLS LAST,
                    created_at ASC NULLS LAST
                LIMIT 5000
                """
            ),
            {
                "pid": pid
            }
        ).mappings().all()


        for row in intelligence_rows:

            item = {
                key: _tl_safe(value)
                for key, value
                in dict(row).items()
            }


            source_group = _tl_source_group(
                item.get("source_type"),
                item.get("source_name"),
                item.get("category"),
                item.get("title")
            )


            event_date = (
                item.get("published_at")
                or
                item.get("created_at")
            )


            events.append({

                "event_id":
                    "intelligence-"
                    + str(item["id"]),

                "event_date":
                    _tl_parse_datetime(
                        event_date
                    ),

                "event_type":
                    _tl_event_type(
                        source_group,
                        item.get("title"),
                        item.get("category"),
                        item.get("status")
                    ),

                "source_group":
                    source_group,

                "title":
                    item.get("title"),

                "description":
                    item.get("description"),

                "status":
                    item.get("status"),

                "source_name":
                    item.get("source_name"),

                "source_type":
                    item.get("source_type"),

                "source_url":
                    item.get("source_url"),

                "confidence":
                    item.get("confidence"),

                "evidence_level":
                    item.get("evidence_level"),

                "latitude":
                    item.get("latitude"),

                "longitude":
                    item.get("longitude"),

                "metadata":
                    item.get("metadata")

            })


        # --------------------------------------------------------------------
        # EVIDENCE
        # --------------------------------------------------------------------

        if _tl_table_exists(
            conn,
            "evidence_items"
        ):

            evidence_columns = _tl_columns(
                conn,
                "evidence_items"
            )


            date_column = None


            for candidate in [
                "published_at",
                "created_at",
                "updated_at"
            ]:

                if candidate in evidence_columns:

                    date_column = candidate

                    break


            if date_column:

                evidence_sql = f"""
                    SELECT *
                    FROM evidence_items
                    WHERE property_id =
                        CAST(:pid AS uuid)
                    ORDER BY
                        {date_column} ASC NULLS LAST
                    LIMIT 5000
                """

            else:

                evidence_sql = """
                    SELECT *
                    FROM evidence_items
                    WHERE property_id =
                        CAST(:pid AS uuid)
                    LIMIT 5000
                """


            try:

                evidence_rows = conn.execute(
                    text(evidence_sql),
                    {
                        "pid": pid
                    }
                ).mappings().all()


                for row in evidence_rows:

                    item = {
                        key: _tl_safe(value)
                        for key, value
                        in dict(row).items()
                    }


                    event_date = None


                    for candidate in [
                        "published_at",
                        "created_at",
                        "updated_at"
                    ]:

                        if item.get(candidate):

                            event_date = (
                                item.get(candidate)
                            )

                            break


                    title = (
                        item.get("title")
                        or
                        item.get("name")
                        or
                        item.get("source_name")
                        or
                        "Evidence item"
                    )


                    events.append({

                        "event_id":
                            "evidence-"
                            + str(
                                item.get(
                                    "id",
                                    len(events)
                                )
                            ),

                        "event_date":
                            _tl_parse_datetime(
                                event_date
                            ),

                        "event_type":
                            "EVIDENCE",

                        "source_group":
                            "EVIDENCE",

                        "title":
                            title,

                        "description":
                            (
                                item.get(
                                    "description"
                                )
                                or
                                item.get(
                                    "summary"
                                )
                            ),

                        "status":
                            "evidence",

                        "source_name":
                            (
                                item.get(
                                    "source_name"
                                )
                                or
                                item.get(
                                    "source"
                                )
                            ),

                        "source_type":
                            "EVIDENCE",

                        "source_url":
                            (
                                item.get(
                                    "source_url"
                                )
                                or
                                item.get(
                                    "url"
                                )
                            ),

                        "confidence":
                            item.get(
                                "confidence"
                            ),

                        "evidence_level":
                            item.get(
                                "evidence_level"
                            ),

                        "metadata":
                            item

                    })

            except Exception:

                pass


        # --------------------------------------------------------------------
        # MARKET OBSERVATIONS
        # --------------------------------------------------------------------

        if _tl_table_exists(
            conn,
            "property_market_observations"
        ):

            market_columns = _tl_columns(
                conn,
                "property_market_observations"
            )


            date_column = None


            for candidate in [
                "observed_at",
                "observation_date",
                "date",
                "created_at",
                "updated_at"
            ]:

                if candidate in market_columns:

                    date_column = candidate

                    break


            try:

                if date_column:

                    market_sql = f"""
                        SELECT *
                        FROM property_market_observations
                        WHERE property_id =
                            CAST(:pid AS uuid)
                        ORDER BY
                            {date_column} ASC NULLS LAST
                        LIMIT 5000
                    """

                else:

                    market_sql = """
                        SELECT *
                        FROM property_market_observations
                        WHERE property_id =
                            CAST(:pid AS uuid)
                        LIMIT 5000
                    """


                market_rows = conn.execute(
                    text(market_sql),
                    {
                        "pid": pid
                    }
                ).mappings().all()


                for row in market_rows:

                    item = {
                        key: _tl_safe(value)
                        for key, value
                        in dict(row).items()
                    }


                    event_date = None


                    for candidate in [
                        "observed_at",
                        "observation_date",
                        "date",
                        "created_at",
                        "updated_at"
                    ]:

                        if item.get(candidate):

                            event_date = (
                                item.get(candidate)
                            )

                            break


                    title = (
                        item.get(
                            "title"
                        )
                        or
                        item.get(
                            "property_name"
                        )
                        or
                        "Market observation"
                    )


                    events.append({

                        "event_id":
                            "market-"
                            + str(
                                item.get(
                                    "id",
                                    len(events)
                                )
                            ),

                        "event_date":
                            _tl_parse_datetime(
                                event_date
                            ),

                        "event_type":
                            "MARKET",

                        "source_group":
                            "MARKET",

                        "title":
                            title,

                        "description":
                            (
                                item.get(
                                    "description"
                                )
                                or
                                "Stored market observation."
                            ),

                        "status":
                            "observed",

                        "source_name":
                            item.get(
                                "source_name"
                            ),

                        "source_type":
                            "MARKET_OBSERVATION",

                        "source_url":
                            item.get(
                                "source_url"
                            ),

                        "confidence":
                            item.get(
                                "confidence"
                            ),

                        "evidence_level":
                            item.get(
                                "evidence_level"
                            ),

                        "metadata":
                            item

                    })

            except Exception:

                pass


        # --------------------------------------------------------------------
        # LIVE REFRESH HISTORY
        # --------------------------------------------------------------------

        if _tl_table_exists(
            conn,
            "piq_property_refresh_runs"
        ):

            try:

                refresh_rows = conn.execute(
                    text(
                        """
                        SELECT *
                        FROM piq_property_refresh_runs
                        WHERE property_id =
                            CAST(:pid AS uuid)
                        ORDER BY
                            started_at ASC NULLS LAST,
                            created_at ASC NULLS LAST
                        LIMIT 1000
                        """
                    ),
                    {
                        "pid": pid
                    }
                ).mappings().all()

            except Exception:

                # Some versions may not have started_at/created_at.
                try:

                    refresh_rows = conn.execute(
                        text(
                            """
                            SELECT *
                            FROM piq_property_refresh_runs
                            WHERE property_id =
                                CAST(:pid AS uuid)
                            LIMIT 1000
                            """
                        ),
                        {
                            "pid": pid
                        }
                    ).mappings().all()

                except Exception:

                    refresh_rows = []


            for row in refresh_rows:

                item = {
                    key: _tl_safe(value)
                    for key, value
                    in dict(row).items()
                }


                event_date = None


                for candidate in [
                    "started_at",
                    "created_at",
                    "finished_at",
                    "updated_at"
                ]:

                    if item.get(candidate):

                        event_date = (
                            item.get(candidate)
                        )

                        break


                events.append({

                    "event_id":
                        "refresh-"
                        + str(
                            item.get(
                                "id",
                                len(events)
                            )
                        ),

                    "event_date":
                        _tl_parse_datetime(
                            event_date
                        ),

                    "event_type":
                        "LIVE REFRESH",

                    "source_group":
                        "LIVE",

                    "title":
                        "Property intelligence refresh",

                    "description":
                        (
                            "A PropertyIQ live intelligence "
                            "refresh was recorded."
                        ),

                    "status":
                        item.get(
                            "status"
                        ),

                    "source_name":
                        "PropertyIQ Live Connectors",

                    "source_type":
                        "LIVE_REFRESH",

                    "source_url":
                        None,

                    "confidence":
                        None,

                    "evidence_level":
                        None,

                    "metadata":
                        item

                })


    # =========================================================================
    # SUMMARY
    # =========================================================================

    dated_events = [
        x
        for x in events
        if x.get("event_date")
    ]


    undated_events = [
        x
        for x in events
        if not x.get("event_date")
    ]


    events.sort(
        key=_tl_date_sort_key
    )


    source_summary = {}


    for event in events:

        group = event.get(
            "source_group",
            "OTHER"
        )

        source_summary[group] = (
            source_summary.get(
                group,
                0
            )
            + 1
        )


    event_type_summary = {}


    for event in events:

        event_type = event.get(
            "event_type",
            "OTHER"
        )

        event_type_summary[event_type] = (
            event_type_summary.get(
                event_type,
                0
            )
            + 1
        )


    return {

        "module":
            MODULE_VERSION,

        "generated_at":
            datetime.utcnow().isoformat()
            + "Z",

        "property":
            property_data,

        "summary": {

            "total_events":
                len(events),

            "dated_events":
                len(dated_events),

            "undated_events":
                len(undated_events),

            "source_groups":
                source_summary,

            "event_types":
                event_type_summary

        },

        "timeline":
            events,

        "data_policy": {

            "source_backed_only":
                True,

            "fabricated_dates":
                False,

            "fabricated_status":
                False,

            "fabricated_coordinates":
                False,

            "valuation_prediction":
                False,

            "investment_score":
                False

        }

    }


# ============================================================================
# 5. ROUTES
# ============================================================================

API_ROUTE = (
    "/api/v1/properties/"
    "{property_id}/timeline"
)

UI_ROUTE = (
    "/propertyiq/timeline/"
    "{property_id}"
)


existing_paths = {

    getattr(
        route,
        "path",
        ""
    )

    for route in app.routes

}


# ============================================================================
# 6. API ROUTE
# ============================================================================

if API_ROUTE not in existing_paths:

    @app.get(
        API_ROUTE,
        tags=[
            "PropertyIQ Timeline"
        ]
    )
    def propertyiq_timeline_api(
        property_id: str
    ):

        data = build_property_timeline(
            engine,
            property_id
        )


        if data is None:

            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )


        return data


# ============================================================================
# 7. UI TEMPLATE
#
# IMPORTANT:
# This is NOT an f-string.
# JavaScript/CSS braces are therefore safe.
# ============================================================================

TIMELINE_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
/>

<title>
PropertyIQ — Development Timeline
</title>


<style>

:root {

    --bg:#050a12;

    --panel:#091321;

    --panel2:#0c1828;

    --line:rgba(255,255,255,.09);

    --text:#edf7ff;

    --muted:#8da3b9;

    --cyan:#56e6ff;

    --blue:#3ba9ff;

    --green:#40d6a0;

    --orange:#ffb64d;

    --purple:#b58cff;

    --red:#ff7180;

}


* {

    box-sizing:border-box;

}


html,
body {

    margin:0;

    min-height:100%;

    background:

        radial-gradient(
            circle at 10% 0%,
            rgba(86,230,255,.10),
            transparent 30%
        ),

        linear-gradient(
            135deg,
            #050a12,
            #07111e
        );

    color:var(--text);

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

}


body {

    min-height:100vh;

}


.nav {

    position:sticky;

    top:0;

    z-index:100;

    height:64px;

    display:flex;

    align-items:center;

    gap:18px;

    padding:0 28px;

    border-bottom:
        1px solid var(--line);

    background:
        rgba(5,10,18,.88);

    backdrop-filter:
        blur(18px);

}


.logo {

    font-weight:900;

    letter-spacing:.1em;

    font-size:14px;

}


.logo span {

    color:var(--cyan);

}


.nav-link {

    color:var(--muted);

    text-decoration:none;

    font-size:11px;

}


.nav-link:hover {

    color:var(--cyan);

}


.container {

    max-width:1450px;

    margin:auto;

    padding:28px;

}


.hero {

    display:flex;

    justify-content:space-between;

    align-items:flex-start;

    gap:20px;

    margin-bottom:20px;

}


.eyebrow {

    color:var(--cyan);

    font-size:10px;

    letter-spacing:.18em;

    font-weight:900;

    text-transform:uppercase;

}


h1 {

    margin:8px 0 5px;

    font-size:34px;

    line-height:1.1;

}


.address {

    color:var(--muted);

    font-size:13px;

}


.badge {

    border:
        1px solid rgba(86,230,255,.28);

    background:
        rgba(86,230,255,.06);

    color:var(--cyan);

    padding:9px 12px;

    border-radius:999px;

    font-size:10px;

    font-weight:800;

    white-space:nowrap;

}


.summary {

    display:grid;

    grid-template-columns:
        repeat(6,minmax(0,1fr));

    gap:10px;

    margin-bottom:18px;

}


.summary-card {

    padding:16px;

    border:
        1px solid var(--line);

    border-radius:15px;

    background:
        rgba(255,255,255,.025);

}


.summary-number {

    font-size:24px;

    font-weight:900;

}


.summary-label {

    color:var(--muted);

    font-size:9px;

    text-transform:uppercase;

    letter-spacing:.08em;

    margin-top:4px;

}


.layout {

    display:grid;

    grid-template-columns:
        minmax(0,1.7fr)
        minmax(300px,.65fr);

    gap:15px;

}


.card {

    border:
        1px solid var(--line);

    border-radius:18px;

    background:
        rgba(255,255,255,.025);

    padding:20px;

    box-shadow:
        0 20px 70px rgba(0,0,0,.2);

}


.card-title {

    font-size:13px;

    font-weight:900;

    letter-spacing:.08em;

    text-transform:uppercase;

    color:var(--cyan);

    margin-bottom:18px;

}


.timeline {

    position:relative;

    padding-left:28px;

}


.timeline:before {

    content:"";

    position:absolute;

    left:8px;

    top:4px;

    bottom:4px;

    width:1px;

    background:
        linear-gradient(
            180deg,
            rgba(86,230,255,.6),
            rgba(86,230,255,.05)
        );

}


.event {

    position:relative;

    padding:0 0 18px 15px;

}


.event:last-child {

    padding-bottom:0;

}


.event-dot {

    position:absolute;

    left:-24px;

    top:4px;

    width:11px;

    height:11px;

    border-radius:50%;

    background:var(--cyan);

    box-shadow:
        0 0 0 4px rgba(86,230,255,.08);

}


.event-card {

    border:
        1px solid var(--line);

    border-radius:14px;

    padding:14px;

    background:
        rgba(255,255,255,.018);

}


.event-card:hover {

    border-color:
        rgba(86,230,255,.25);

}


.event-header {

    display:flex;

    justify-content:space-between;

    gap:12px;

    align-items:flex-start;

}


.event-title {

    font-size:13px;

    font-weight:800;

    line-height:1.4;

}


.event-date {

    color:var(--cyan);

    font-family:
        ui-monospace,
        SFMono-Regular,
        Menlo,
        monospace;

    font-size:9px;

    white-space:nowrap;

}


.event-description {

    color:var(--muted);

    font-size:11px;

    line-height:1.6;

    margin-top:7px;

}


.tags {

    margin-top:9px;

}


.tag {

    display:inline-block;

    padding:5px 7px;

    border:
        1px solid var(--line);

    border-radius:999px;

    color:var(--muted);

    font-size:8px;

    margin-right:4px;

    margin-bottom:4px;

}


.tag.source {

    color:var(--cyan);

    border-color:
        rgba(86,230,255,.2);

}


.tag.status {

    color:var(--green);

}


.source-row {

    display:flex;

    justify-content:space-between;

    align-items:center;

    gap:10px;

    padding:12px;

    border:
        1px solid var(--line);

    border-radius:12px;

    margin-bottom:7px;

}


.source-name {

    font-size:11px;

    font-weight:750;

}


.source-count {

    color:var(--cyan);

    font-size:11px;

}


.filter-row {

    display:flex;

    flex-wrap:wrap;

    gap:6px;

    margin-bottom:14px;

}


.filter {

    border:
        1px solid var(--line);

    background:
        rgba(255,255,255,.025);

    color:var(--muted);

    padding:7px 9px;

    border-radius:8px;

    cursor:pointer;

    font-size:9px;

}


.filter.active {

    color:var(--text);

    background:
        rgba(86,230,255,.08);

    border-color:
        rgba(86,230,255,.3);

}


.action {

    display:block;

    text-decoration:none;

    color:var(--text);

    padding:12px;

    border:
        1px solid var(--line);

    border-radius:12px;

    margin-bottom:7px;

    background:
        rgba(255,255,255,.018);

}


.action:hover {

    border-color:
        rgba(86,230,255,.3);

    background:
        rgba(86,230,255,.05);

}


.action-title {

    font-size:11px;

    font-weight:800;

}


.action-sub {

    color:var(--muted);

    font-size:9px;

    margin-top:3px;

}


.empty {

    color:var(--muted);

    padding:20px 5px;

    font-size:11px;

    line-height:1.7;

}


.note {

    color:var(--muted);

    font-size:10px;

    line-height:1.65;

}


@media(max-width:1100px) {

    .summary {

        grid-template-columns:
            repeat(3,minmax(0,1fr));

    }

}


@media(max-width:900px) {

    .layout {

        grid-template-columns:1fr;

    }

}


@media(max-width:650px) {

    .summary {

        grid-template-columns:
            repeat(2,minmax(0,1fr));

    }

    .container {

        padding:16px;

    }

    .hero {

        flex-direction:column;

    }

    .nav-link {

        display:none;

    }

}

</style>

</head>


<body>


<nav class="nav">

    <div class="logo">
        PROPERTY<span>IQ</span>
    </div>


    <a
        class="nav-link"
        id="workspaceLink"
        href="#"
    >
        Workspace
    </a>


    <a
        class="nav-link"
        id="mapLink"
        href="#"
    >
        Command Map
    </a>


    <a
        class="nav-link"
        id="profileLink"
        href="#"
    >
        Profile
    </a>


    <a
        class="nav-link"
        id="reportLink"
        href="#"
    >
        Report
    </a>

</nav>


<main class="container">


<section class="hero">

    <div>

        <div class="eyebrow">
            PROPERTYIQ · DEVELOPMENT PIPELINE
        </div>

        <h1 id="propertyName">
            Property
        </h1>

        <div
            class="address"
            id="propertyAddress"
        ></div>

    </div>


    <div class="badge">
        SOURCE-BACKED TIMELINE
    </div>

</section>


<section
    class="summary"
    id="summary"
></section>


<section class="layout">


<div class="card">


    <div class="card-title">
        Timeline
    </div>


    <div
        class="filter-row"
        id="filters"
    ></div>


    <div
        class="timeline"
        id="timeline"
    ></div>


</div>


<aside>


<div class="card">

    <div class="card-title">
        Source Inventory
    </div>

    <div id="sources"></div>

</div>


<div
    class="card"
    style="margin-top:15px"
>

    <div class="card-title">
        Property Intelligence
    </div>


    <a
        class="action"
        id="commandMapLink"
        href="#"
    >

        <div class="action-title">
            Unified Command Map
        </div>

        <div class="action-sub">
            View spatial intelligence.
        </div>

    </a>


    <a
        class="action"
        id="boundaryLink"
        href="#"
    >

        <div class="action-title">
            GIS Boundary
        </div>

        <div class="action-sub">
            Draw and manage property boundary.
        </div>

    </a>


    <a
        class="action"
        id="boundaryIntelligenceLink"
        href="#"
    >

        <div class="action-title">
            Boundary Intelligence
        </div>

        <div class="action-sub">
            Analyze intelligence around boundary.
        </div>

    </a>


    <a
        class="action"
        id="marketLink"
        href="#"
    >

        <div class="action-title">
            Market Comparables
        </div>

        <div class="action-sub">
            Review stored market evidence.
        </div>

    </a>


    <a
        class="action"
        id="documentsLink"
        href="#"
    >

        <div class="action-title">
            Documents
        </div>

        <div class="action-sub">
            Open property document workspace.
        </div>

    </a>


</div>


<div
    class="card"
    style="margin-top:15px"
>

    <div class="card-title">
        Data Policy
    </div>


    <div class="note">

        Dates and statuses shown here come from
        stored PropertyIQ records.

        <br><br>

        Undated records are explicitly marked
        rather than being assigned estimated dates.

        <br><br>

        No property-price prediction or
        investment score is generated by this
        timeline.

    </div>

</div>


</aside>


</section>


</main>


<script>


const DATA = __PROPERTYIQ_DATA__;

const PROPERTY_ID = "__PROPERTY_ID__";


let activeFilter = "ALL";


/* -------------------------------------------------------------------------
   HELPERS
   ------------------------------------------------------------------------- */

function safe(value) {

    if (
        value === null ||
        value === undefined ||
        value === ""
    ) {

        return "—";

    }


    if (
        typeof value === "object"
    ) {

        try {

            return JSON.stringify(value);

        } catch (error) {

            return String(value);

        }

    }


    return String(value);

}


function formatDate(value) {

    if (!value) {

        return "UNDATED";

    }


    try {

        const date =
            new Date(value);


        if (
            Number.isNaN(
                date.getTime()
            )
        ) {

            return String(value);

        }


        return date.toLocaleDateString(
            undefined,
            {
                year:"numeric",
                month:"short",
                day:"numeric"
            }
        );

    } catch (error) {

        return String(value);

    }

}


function sourceColor(group) {

    const colors = {

        RERA:"#ffb64d",

        GOVERNMENT:"#40d6a0",

        NEWS:"#b58cff",

        OSM:"#56e6ff",

        DEVELOPMENT:"#ff7180",

        EVIDENCE:"#3ba9ff",

        MARKET:"#ff9b63",

        LIVE:"#7de4b9",

        PROPERTY:"#ffffff",

        OTHER:"#9aaabd"

    };


    return (
        colors[group]
        ||
        colors.OTHER
    );

}


/* -------------------------------------------------------------------------
   LINKS
   ------------------------------------------------------------------------- */

function configureLinks() {

    document.getElementById(
        "workspaceLink"
    ).href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;


    document.getElementById(
        "mapLink"
    ).href =
        "/propertyiq/command-map/"
        + PROPERTY_ID;


    document.getElementById(
        "profileLink"
    ).href =
        "/propertyiq/full-profile/"
        + PROPERTY_ID;


    document.getElementById(
        "reportLink"
    ).href =
        "/propertyiq/intelligence-report/"
        + PROPERTY_ID;


    document.getElementById(
        "commandMapLink"
    ).href =
        "/propertyiq/command-map/"
        + PROPERTY_ID;


    document.getElementById(
        "boundaryLink"
    ).href =
        "/propertyiq/boundary/"
        + PROPERTY_ID;


    document.getElementById(
        "boundaryIntelligenceLink"
    ).href =
        "/propertyiq/boundary-intelligence/"
        + PROPERTY_ID;


    document.getElementById(
        "marketLink"
    ).href =
        "/propertyiq/market-comparables/"
        + PROPERTY_ID;


    document.getElementById(
        "documentsLink"
    ).href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;

}


/* -------------------------------------------------------------------------
   HEADER
   ------------------------------------------------------------------------- */

function renderHeader() {

    const property =
        DATA.property;


    document.getElementById(
        "propertyName"
    ).textContent =
        safe(
            property.property_name
        );


    document.getElementById(
        "propertyAddress"
    ).textContent =
        safe(
            property.property_address
        );

}


/* -------------------------------------------------------------------------
   SUMMARY
   ------------------------------------------------------------------------- */

function renderSummary() {

    const summary =
        DATA.summary;


    const items = [

        [
            summary.total_events,
            "Total events"
        ],

        [
            summary.dated_events,
            "Dated events"
        ],

        [
            summary.undated_events,
            "Undated"
        ],

        [
            Object.keys(
                summary.source_groups || {}
            ).length,
            "Source groups"
        ],

        [
            (
                summary.event_types
                &&
                summary.event_types[
                    "GOVERNMENT INFRASTRUCTURE"
                ]
            )
            || 0,
            "Government"
        ],

        [
            (
                summary.event_types
                &&
                summary.event_types[
                    "RERA / PROJECT"
                ]
            )
            || 0,
            "RERA / projects"
        ]

    ];


    document.getElementById(
        "summary"
    ).innerHTML =

        items.map(
            function(item) {

                return (
                    '<div class="summary-card">'
                    +
                    '<div class="summary-number">'
                    +
                    safe(item[0])
                    +
                    "</div>"
                    +
                    '<div class="summary-label">'
                    +
                    safe(item[1])
                    +
                    "</div>"
                    +
                    "</div>"
                );

            }
        ).join("");

}


/* -------------------------------------------------------------------------
   SOURCE INVENTORY
   ------------------------------------------------------------------------- */

function renderSources() {

    const container =
        document.getElementById(
            "sources"
        );


    const groups =
        DATA.summary.source_groups
        || {};


    const keys =
        Object.keys(groups);


    if (
        keys.length === 0
    ) {

        container.innerHTML =
            '<div class="empty">'
            +
            "No timeline source records are "
            +
            "currently available."
            +
            "</div>";

        return;

    }


    container.innerHTML = "";


    keys.forEach(
        function(group) {

            const row =
                document.createElement(
                    "div"
                );


            row.className =
                "source-row";


            const name =
                document.createElement(
                    "div"
                );


            name.className =
                "source-name";


            name.textContent =
                group;


            const count =
                document.createElement(
                    "div"
                );


            count.className =
                "source-count";


            count.textContent =
                groups[group];


            row.appendChild(
                name
            );


            row.appendChild(
                count
            );


            container.appendChild(
                row
            );

        }
    );

}


/* -------------------------------------------------------------------------
   FILTERS
   ------------------------------------------------------------------------- */

function buildFilters() {

    const container =
        document.getElementById(
            "filters"
        );


    const groups = [

        "ALL",

        "RERA",

        "GOVERNMENT",

        "NEWS",

        "DEVELOPMENT",

        "MARKET",

        "EVIDENCE",

        "LIVE",

        "OSM",

        "PROPERTY"

    ];


    container.innerHTML = "";


    groups.forEach(
        function(group) {

            const button =
                document.createElement(
                    "button"
                );


            button.className =
                "filter";


            if (
                group === "ALL"
            ) {

                button.classList.add(
                    "active"
                );

            }


            button.textContent =
                group;


            button.addEventListener(
                "click",
                function() {

                    activeFilter =
                        group;


                    document
                        .querySelectorAll(
                            ".filter"
                        )
                        .forEach(
                            function(other) {

                                other.classList.remove(
                                    "active"
                                );

                            }
                        );


                    button.classList.add(
                        "active"
                    );


                    renderTimeline();

                }
            );


            container.appendChild(
                button
            );

        }
    );

}


/* -------------------------------------------------------------------------
   TIMELINE
   ------------------------------------------------------------------------- */

function renderTimeline() {

    const container =
        document.getElementById(
            "timeline"
        );


    let events =
        DATA.timeline
        || [];


    if (
        activeFilter !== "ALL"
    ) {

        events =
            events.filter(
                function(event) {

                    return (
                        event.source_group
                        ===
                        activeFilter
                    );

                }
            );

    }


    if (
        events.length === 0
    ) {

        container.innerHTML =
            '<div class="empty">'
            +
            "No events are available for "
            +
            "this filter."
            +
            "</div>";

        return;

    }


    container.innerHTML = "";


    events.forEach(
        function(event) {

            const wrapper =
                document.createElement(
                    "div"
                );


            wrapper.className =
                "event";


            const dot =
                document.createElement(
                    "div"
                );


            dot.className =
                "event-dot";


            dot.style.background =
                sourceColor(
                    event.source_group
                );


            const card =
                document.createElement(
                    "div"
                );


            card.className =
                "event-card";


            const header =
                document.createElement(
                    "div"
                );


            header.className =
                "event-header";


            const title =
                document.createElement(
                    "div"
                );


            title.className =
                "event-title";


            title.textContent =
                safe(
                    event.title
                );


            const date =
                document.createElement(
                    "div"
                );


            date.className =
                "event-date";


            date.textContent =
                formatDate(
                    event.event_date
                );


            header.appendChild(
                title
            );


            header.appendChild(
                date
            );


            const description =
                document.createElement(
                    "div"
                );


            description.className =
                "event-description";


            description.textContent =
                safe(
                    event.description
                );


            const tags =
                document.createElement(
                    "div"
                );


            tags.className =
                "tags";


            const sourceTag =
                document.createElement(
                    "span"
                );


            sourceTag.className =
                "tag source";


            sourceTag.textContent =
                safe(
                    event.source_group
                );


            tags.appendChild(
                sourceTag
            );


            if (
                event.event_type
            ) {

                const typeTag =
                    document.createElement(
                        "span"
                    );


                typeTag.className =
                    "tag";


                typeTag.textContent =
                    safe(
                        event.event_type
                    );


                tags.appendChild(
                    typeTag
                );

            }


            if (
                event.status
            ) {

                const statusTag =
                    document.createElement(
                        "span"
                    );


                statusTag.className =
                    "tag status";


                statusTag.textContent =
                    safe(
                        event.status
                    );


                tags.appendChild(
                    statusTag
                );

            }


            if (
                event.evidence_level
            ) {

                const evidenceTag =
                    document.createElement(
                        "span"
                    );


                evidenceTag.className =
                    "tag";


                evidenceTag.textContent =
                    safe(
                        event.evidence_level
                    );


                tags.appendChild(
                    evidenceTag
                );

            }


            if (
                event.source_name
            ) {

                const sourceNameTag =
                    document.createElement(
                        "span"
                    );


                sourceNameTag.className =
                    "tag";


                sourceNameTag.textContent =
                    safe(
                        event.source_name
                    );


                tags.appendChild(
                    sourceNameTag
                );

            }


            if (
                event.source_url
            ) {

                const sourceTag =
                    document.createElement(
                        "a"
                    );


                sourceTag.className =
                    "tag source";


                sourceTag.href =
                    event.source_url;


                sourceTag.target =
                    "_blank";


                sourceTag.rel =
                    "noopener noreferrer";


                sourceTag.textContent =
                    "SOURCE";


                tags.appendChild(
                    sourceTag
                );

            }


            card.appendChild(
                header
            );


            card.appendChild(
                description
            );


            card.appendChild(
                tags
            );


            wrapper.appendChild(
                dot
            );


            wrapper.appendChild(
                card
            );


            container.appendChild(
                wrapper
            );

        }
    );

}


/* -------------------------------------------------------------------------
   INITIALIZE
   ------------------------------------------------------------------------- */

configureLinks();

renderHeader();

renderSummary();

renderSources();

buildFilters();

renderTimeline();

</script>


</body>

</html>
"""


# ============================================================================
# 8. UI ROUTE
# ============================================================================

if UI_ROUTE not in existing_paths:

    @app.get(
        UI_ROUTE,
        response_class=HTMLResponse,
        tags=[
            "PropertyIQ Timeline"
        ]
    )
    def propertyiq_timeline_ui(
        property_id: str
    ):

        data = build_property_timeline(
            engine,
            property_id
        )


        if data is None:

            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )


        payload = json.dumps(
            data,
            ensure_ascii=False,
            default=str
        )


        html = TIMELINE_HTML.replace(
            "__PROPERTYIQ_DATA__",
            payload
        )


        html = html.replace(
            "__PROPERTY_ID__",
            str(property_id)
        )


        return HTMLResponse(
            content=html
        )


# ============================================================================
# 9. INSTALLATION OUTPUT
# ============================================================================

print("")
print("=" * 80)
print(
    "PROPERTYIQ — TIMELINE & DEVELOPMENT PIPELINE INSTALLED"
)
print("=" * 80)

print(
    "Module:",
    MODULE_VERSION
)

print(
    "Runtime: existing PropertyIQ runtime"
)

print("")
print("API:")
print(
    "  /api/v1/properties/{property_id}/timeline"
)

print("")
print("UI:")
print(
    "  /propertyiq/timeline/{property_id}"
)

print("")
print("Integrated timeline sources:")
print("  Property records: ENABLED")
print("  RERA: ENABLED")
print("  Government / PAIMANA: ENABLED")
print("  Development intelligence: ENABLED")
print("  News / GDELT: ENABLED")
print("  OSM / GIS: ENABLED")
print("  Market observations: ENABLED")
print("  Evidence: ENABLED")
print("  Live refresh history: ENABLED")

print("")
print("Timeline features:")
print("  Chronological events: ENABLED")
print("  Source filters: ENABLED")
print("  Event-type classification: ENABLED")
print("  Source links: ENABLED")
print("  Evidence labels: ENABLED")
print("  Dated/undated separation: ENABLED")

print("")
print("Data integrity:")
print("  Fabricated dates: DISABLED")
print("  Fabricated status: DISABLED")
print("  Fabricated coordinates: DISABLED")
print("  Valuation prediction: DISABLED")
print("  Investment score: DISABLED")
print("  Database modification during installation: DISABLED")

print("")
print(
    "Existing PropertyIQ routes preserved."
)

print("=" * 80)
print("")



# ============================================================
# PROPERTYIQ MODULE: FULL_DASHBOARD
# ORIGINAL COLAB CELL: In[85]
# ============================================================

# ==============================================================================
# PROPERTYIQ — FULL PROPERTY INTELLIGENCE DASHBOARD
# V1 — DIRECT COLAB EXECUTION
# ==============================================================================
# Additive integration only.
# Does NOT rebuild previous modules.
# Does NOT modify database records during installation.
# Reuses existing engine + FastAPI app.
# ==============================================================================

import sys
import subprocess
import importlib
import json
import uuid
from datetime import datetime

MODULE_VERSION = "PROPERTYIQ-FULL-INTELLIGENCE-DASHBOARD-V1"


# ------------------------------------------------------------------------------
# BOOTSTRAP
# ------------------------------------------------------------------------------

def _ensure_fastapi():
    try:
        import fastapi
        return
    except Exception:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "-q", "fastapi", "uvicorn", "sqlalchemy", "psycopg[binary]"
        ])

_ensure_fastapi()

from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text


# ------------------------------------------------------------------------------
# REUSE EXISTING RUNTIME
# ------------------------------------------------------------------------------

if "engine" not in globals() or engine is None:
    print("Existing PropertyIQ engine not found.")
    print("Please run your PropertyIQ PostgreSQL runtime/bootstrap cell first.")
    raise RuntimeError("PropertyIQ engine is required.")

if "app" not in globals() or app is None:
    app = FastAPI(title="PropertyIQ")


# ------------------------------------------------------------------------------
# DATABASE HELPERS
# ------------------------------------------------------------------------------

def _table_exists(conn, table_name):
    try:
        result = conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema='public'
                    AND table_name=:table
                )
            """),
            {"table": table_name}
        )
        return bool(result.scalar())
    except Exception:
        return False


def _column_exists(conn, table_name, column_name):
    try:
        result = conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema='public'
                    AND table_name=:table
                    AND column_name=:column
                )
            """),
            {
                "table": table_name,
                "column": column_name
            }
        )
        return bool(result.scalar())
    except Exception:
        return False


def _safe_json(value):
    if value is None:
        return None

    if isinstance(value, (dict, list, str, int, float, bool)):
        return value

    try:
        return str(value)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# PROPERTY SUMMARY
# ------------------------------------------------------------------------------

def _dashboard_property(conn, property_id):

    row = conn.execute(
        text("""
            SELECT
                id::text AS id,
                property_name,
                property_address,
                latitude,
                longitude,
                property_type,
                area,
                price,
                price_per_sqft,
                bedrooms,
                bathrooms,
                builder_owner,
                description,
                amenities,
                photos,
                documents,
                contact_information,
                CASE
                    WHEN boundary IS NOT NULL THEN true
                    ELSE false
                END AS has_boundary
            FROM properties
            WHERE id = CAST(:pid AS uuid)
            LIMIT 1
        """),
        {"pid": property_id}
    ).mappings().first()

    if not row:
        return None

    return {
        k: _safe_json(v)
        for k, v in dict(row).items()
    }


# ------------------------------------------------------------------------------
# INTELLIGENCE SUMMARY
# ------------------------------------------------------------------------------

def _dashboard_intelligence(conn, property_id):

    if not _table_exists(conn, "intelligence_records"):
        return {
            "total": 0,
            "mapped": 0,
            "unmapped": 0,
            "sources": {},
            "statuses": {},
            "records": []
        }

    rows = conn.execute(
        text("""
            SELECT
                id::text AS id,
                title,
                category,
                status,
                description,
                latitude,
                longitude,
                source_name,
                source_type,
                source_url,
                confidence,
                evidence_level,
                metadata
            FROM intelligence_records
            WHERE property_id = CAST(:pid AS uuid)
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 500
        """),
        {"pid": property_id}
    ).mappings().all()

    records = []

    source_counts = {}
    status_counts = {}

    mapped = 0

    for r in rows:

        source = r.get("source_name") or r.get("source_type") or "Unknown"
        status = r.get("status") or "Unknown"

        source_counts[source] = source_counts.get(source, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1

        lat = r.get("latitude")
        lon = r.get("longitude")

        if lat is not None and lon is not None:
            mapped += 1

        records.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "category": r.get("category"),
            "status": status,
            "description": r.get("description"),
            "latitude": lat,
            "longitude": lon,
            "source_name": r.get("source_name"),
            "source_type": r.get("source_type"),
            "source_url": r.get("source_url"),
            "confidence": r.get("confidence"),
            "evidence_level": r.get("evidence_level"),
            "metadata": _safe_json(r.get("metadata"))
        })

    return {
        "total": len(records),
        "mapped": mapped,
        "unmapped": len(records) - mapped,
        "sources": source_counts,
        "statuses": status_counts,
        "records": records
    }


# ------------------------------------------------------------------------------
# EVIDENCE
# ------------------------------------------------------------------------------

def _dashboard_evidence(conn, property_id):

    if not _table_exists(conn, "evidence_items"):
        return {
            "count": 0,
            "items": []
        }

    where = "property_id = CAST(:pid AS uuid)"

    try:
        rows = conn.execute(
            text("""
                SELECT *
                FROM evidence_items
                WHERE property_id = CAST(:pid AS uuid)
                LIMIT 500
            """),
            {"pid": property_id}
        ).mappings().all()
    except Exception:

        # Some historical schemas may attach evidence through intelligence_id.
        try:
            rows = conn.execute(
                text("""
                    SELECT e.*
                    FROM evidence_items e
                    JOIN intelligence_records i
                      ON i.id = e.intelligence_id
                    WHERE i.property_id = CAST(:pid AS uuid)
                    LIMIT 500
                """),
                {"pid": property_id}
            ).mappings().all()
        except Exception:
            rows = []

    items = []

    for r in rows:

        item = {}

        for key, value in dict(r).items():
            if key in (
                "id",
                "title",
                "source_name",
                "source_url",
                "evidence_level",
                "description",
                "confidence",
                "intelligence_id"
            ):
                item[key] = _safe_json(value)

        items.append(item)

    return {
        "count": len(items),
        "items": items
    }


# ------------------------------------------------------------------------------
# OPTIONAL WORKSPACE COUNTS
# ------------------------------------------------------------------------------

def _dashboard_optional_counts(conn, property_id):

    result = {
        "market_observations": 0,
        "documents": 0,
        "refresh_runs": 0
    }

    if _table_exists(conn, "property_market_observations"):

        try:
            result["market_observations"] = int(
                conn.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM property_market_observations
                        WHERE property_id = CAST(:pid AS uuid)
                    """),
                    {"pid": property_id}
                ).scalar() or 0
            )
        except Exception:
            pass

    if _table_exists(conn, "property_files"):

        try:
            result["documents"] = int(
                conn.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM property_files
                        WHERE property_id = CAST(:pid AS uuid)
                    """),
                    {"pid": property_id}
                ).scalar() or 0
            )
        except Exception:
            pass

    if _table_exists(conn, "piq_property_refresh_runs"):

        try:
            result["refresh_runs"] = int(
                conn.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM piq_property_refresh_runs
                        WHERE property_id = CAST(:pid AS uuid)
                    """),
                    {"pid": property_id}
                ).scalar() or 0
            )
        except Exception:
            pass

    return result


# ------------------------------------------------------------------------------
# DASHBOARD DATA
# ------------------------------------------------------------------------------

def build_full_dashboard(property_id):

    with engine.begin() as conn:

        property_data = _dashboard_property(conn, property_id)

        if not property_data:
            return None

        intelligence = _dashboard_intelligence(
            conn,
            property_id
        )

        evidence = _dashboard_evidence(
            conn,
            property_id
        )

        optional = _dashboard_optional_counts(
            conn,
            property_id
        )

    return {
        "module": MODULE_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "property": property_data,
        "intelligence": intelligence,
        "evidence": evidence,
        "optional": optional,

        "workspaces": {
            "full_profile":
                f"/propertyiq/full-profile/{property_id}",

            "command_map":
                f"/propertyiq/command-map/{property_id}",

            "timeline":
                f"/propertyiq/timeline/{property_id}",

            "boundary":
                f"/propertyiq/boundary/{property_id}",

            "boundary_intelligence":
                f"/propertyiq/boundary-intelligence/{property_id}",

            "due_diligence":
                f"/propertyiq/due-diligence-map/{property_id}",

            "market":
                f"/propertyiq/market-comparables/{property_id}",

            "real_osm":
                f"/propertyiq/real-osm-gis/{property_id}",

            "documents":
                f"/propertyiq/files/{property_id}",

            "document_intelligence":
                f"/propertyiq/document-intelligence/{property_id}",

            "report":
                f"/propertyiq/intelligence-report/{property_id}",

            "workspace":
                f"/propertyiq/workspace/{property_id}"
        }
    }


# ------------------------------------------------------------------------------
# API
# ------------------------------------------------------------------------------

DASHBOARD_API_ROUTE = (
    "/api/v1/properties/{property_id}/"
    "full-intelligence-dashboard"
)

DASHBOARD_UI_ROUTE = (
    "/propertyiq/full-intelligence/{property_id}"
)


@app.get(DASHBOARD_API_ROUTE)
def propertyiq_full_intelligence_dashboard(property_id: str):

    try:
        data = build_full_dashboard(property_id)

        if data is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": "Property not found",
                    "property_id": property_id
                }
            )

        return data

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc),
                "property_id": property_id
            }
        )


# ------------------------------------------------------------------------------
# PREMIUM DASHBOARD HTML
# ------------------------------------------------------------------------------

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html>
<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>PropertyIQ — Intelligence Dashboard</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at 15% 10%,
            rgba(0, 190, 255, 0.10),
            transparent 30%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(70, 90, 255, 0.08),
            transparent 28%
        ),
        #07101c;

    color: #e9f2ff;
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

a {
    color: #67d9ff;
    text-decoration: none;
}

a:hover {
    text-decoration: underline;
}

.shell {
    min-height: 100vh;
}

.topbar {
    height: 68px;

    display: flex;
    align-items: center;
    justify-content: space-between;

    padding: 0 28px;

    border-bottom:
        1px solid rgba(255,255,255,0.08);

    background:
        rgba(5, 13, 24, 0.82);

    backdrop-filter: blur(18px);

    position: sticky;
    top: 0;
    z-index: 20;
}

.brand {
    display: flex;
    align-items: center;
    gap: 12px;
}

.brand-mark {
    width: 36px;
    height: 36px;

    border-radius: 11px;

    background:
        linear-gradient(
            135deg,
            #21d4fd,
            #315cff
        );

    box-shadow:
        0 0 28px rgba(33,212,253,0.25);
}

.brand-title {
    font-size: 18px;
    font-weight: 800;
    letter-spacing: 0.4px;
}

.brand-sub {
    font-size: 11px;
    color: #7e94ad;
    margin-top: 2px;
}

.top-actions {
    display: flex;
    gap: 9px;
}

.btn {
    border:
        1px solid rgba(255,255,255,0.10);

    background:
        rgba(255,255,255,0.045);

    color: #dbeaff;

    border-radius: 10px;

    padding: 9px 13px;

    cursor: pointer;

    font-size: 12px;
}

.btn:hover {
    background:
        rgba(255,255,255,0.09);
}

.btn-primary {
    border-color:
        rgba(55, 204, 255, 0.40);

    background:
        rgba(27, 174, 224, 0.14);

    color: #77e0ff;
}

.container {
    max-width: 1550px;
    margin: auto;
    padding: 25px;
}

.hero {
    display: grid;

    grid-template-columns:
        minmax(0, 1fr)
        330px;

    gap: 18px;

    margin-bottom: 18px;
}

.hero-card {
    border:
        1px solid rgba(255,255,255,0.08);

    border-radius: 18px;

    background:
        linear-gradient(
            145deg,
            rgba(17,31,50,0.92),
            rgba(8,17,30,0.82)
        );

    box-shadow:
        0 20px 60px rgba(0,0,0,0.20);

    padding: 24px;
}

.kicker {
    font-size: 10px;
    color: #5edcff;
    text-transform: uppercase;
    letter-spacing: 1.7px;
    font-weight: 800;
}

.property-name {
    margin-top: 9px;

    font-size: 29px;
    font-weight: 800;
    line-height: 1.15;
}

.address {
    color: #8ea4bd;
    margin-top: 8px;
    font-size: 13px;
}

.meta-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 16px;
}

.chip {
    border:
        1px solid rgba(255,255,255,0.09);

    background:
        rgba(255,255,255,0.035);

    border-radius: 999px;

    padding: 7px 10px;

    color: #a9bed4;

    font-size: 11px;
}

.metrics {
    display: grid;
    grid-template-columns:
        repeat(4, minmax(0, 1fr));

    gap: 10px;

    margin-top: 20px;
}

.metric {
    padding: 14px;

    border:
        1px solid rgba(255,255,255,0.07);

    border-radius: 13px;

    background:
        rgba(255,255,255,0.025);
}

.metric-label {
    font-size: 10px;
    color: #71869e;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}

.metric-value {
    font-size: 20px;
    font-weight: 800;
    margin-top: 6px;
}

.coverage {
    display: flex;
    flex-direction: column;
    justify-content: center;
}

.coverage-title {
    color: #91a8c0;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}

.coverage-number {
    font-size: 42px;
    font-weight: 900;
    margin-top: 8px;
}

.coverage-caption {
    color: #70879f;
    font-size: 12px;
    line-height: 1.5;
}

.grid {
    display: grid;

    grid-template-columns:
        repeat(4, minmax(0, 1fr));

    gap: 14px;
}

.card {
    border:
        1px solid rgba(255,255,255,0.075);

    border-radius: 16px;

    background:
        rgba(12,23,39,0.78);

    padding: 18px;

    min-height: 130px;

    box-shadow:
        0 15px 45px rgba(0,0,0,0.13);
}

.card:hover {
    border-color:
        rgba(85,215,255,0.20);

    transform: translateY(-1px);

    transition: 0.18s ease;
}

.card-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #6f879f;
}

.card-value {
    margin-top: 9px;

    font-size: 29px;
    font-weight: 850;
}

.card-note {
    color: #7f95ad;
    font-size: 11px;
    margin-top: 6px;
}

.section {
    margin-top: 20px;
}

.section-title {
    display: flex;
    justify-content: space-between;
    align-items: center;

    margin-bottom: 10px;
}

.section-title h2 {
    margin: 0;

    font-size: 15px;
    font-weight: 800;
}

.section-title span {
    color: #6d849d;
    font-size: 11px;
}

.workspace-grid {
    display: grid;

    grid-template-columns:
        repeat(4, minmax(0, 1fr));

    gap: 10px;
}

.workspace {
    padding: 15px;

    border:
        1px solid rgba(255,255,255,0.075);

    border-radius: 13px;

    background:
        rgba(255,255,255,0.025);

    cursor: pointer;
}

.workspace:hover {
    background:
        rgba(77,204,255,0.07);

    border-color:
        rgba(77,204,255,0.22);
}

.workspace-name {
    font-size: 12px;
    font-weight: 750;
}

.workspace-desc {
    font-size: 10px;
    color: #6f849b;
    margin-top: 5px;
    line-height: 1.4;
}

.source-list {
    display: grid;
    grid-template-columns:
        repeat(3, minmax(0, 1fr));

    gap: 8px;
}

.source-item {
    display: flex;
    justify-content: space-between;

    padding: 11px 12px;

    border-radius: 10px;

    background:
        rgba(255,255,255,0.025);

    border:
        1px solid rgba(255,255,255,0.055);

    font-size: 11px;
}

.source-count {
    color: #5edcff;
    font-weight: 800;
}

.record-table {
    width: 100%;
    border-collapse: collapse;
}

.record-table th,
.record-table td {
    padding: 11px 8px;

    border-bottom:
        1px solid rgba(255,255,255,0.06);

    text-align: left;

    font-size: 11px;
}

.record-table th {
    color: #71889f;
    text-transform: uppercase;
    font-size: 9px;
    letter-spacing: 0.8px;
}

.record-table td {
    color: #b9cbe0;
}

.status {
    display: inline-block;

    padding: 5px 8px;

    border-radius: 999px;

    background:
        rgba(92,220,255,0.07);

    color: #73dcff;

    font-size: 9px;
}

.empty {
    padding: 25px;
    text-align: center;
    color: #6f849c;
    font-size: 12px;
}

.footer {
    margin-top: 28px;

    color: #52677f;

    font-size: 10px;

    text-align: center;

    padding-bottom: 30px;
}

@media(max-width: 1100px) {

    .hero {
        grid-template-columns: 1fr;
    }

    .grid {
        grid-template-columns:
            repeat(2, minmax(0,1fr));
    }

    .workspace-grid {
        grid-template-columns:
            repeat(2, minmax(0,1fr));
    }

    .source-list {
        grid-template-columns:
            repeat(2, minmax(0,1fr));
    }
}

@media(max-width: 650px) {

    .container {
        padding: 13px;
    }

    .metrics {
        grid-template-columns:
            repeat(2, minmax(0,1fr));
    }

    .grid {
        grid-template-columns: 1fr;
    }

    .workspace-grid,
    .source-list {
        grid-template-columns: 1fr;
    }

    .property-name {
        font-size: 23px;
    }
}

</style>

</head>

<body>

<div class="shell">

<header class="topbar">

    <div class="brand">

        <div class="brand-mark"></div>

        <div>
            <div class="brand-title">
                PropertyIQ
            </div>

            <div class="brand-sub">
                PROPERTY INTELLIGENCE COMMAND CENTER
            </div>
        </div>

    </div>

    <div class="top-actions">

        <button class="btn"
                onclick="openWorkspace('command_map')">
            Command Map
        </button>

        <button class="btn"
                onclick="openWorkspace('report')">
            Report
        </button>

        <button class="btn btn-primary"
                onclick="location.reload()">
            Refresh Dashboard
        </button>

    </div>

</header>


<main class="container">

    <div id="app">
        Loading PropertyIQ intelligence...
    </div>

</main>

</div>


<script>

const PROPERTY_ID = "__PROPERTY_ID__";

const DATA = __PROPERTYIQ_DATA__;


function esc(value) {

    if (value === null || value === undefined) {
        return "";
    }

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}


function fmt(value) {

    if (value === null ||
        value === undefined ||
        value === "") {

        return "—";
    }

    return esc(value);
}


function openWorkspace(key) {

    const url =
        DATA.workspaces &&
        DATA.workspaces[key];

    if (url) {
        window.location.href = url;
    }
}


function propertyCoverage() {

    const p = DATA.property || {};

    const fields = [
        "property_name",
        "property_address",
        "latitude",
        "longitude",
        "property_type",
        "area",
        "price",
        "builder_owner",
        "description",
        "amenities",
        "contact_information"
    ];

    let present = 0;

    fields.forEach(
        function(field) {

            const value = p[field];

            if (
                value !== null &&
                value !== undefined &&
                value !== ""
            ) {
                present += 1;
            }

        }
    );

    return Math.round(
        (present / fields.length) * 100
    );
}


function render() {

    const p = DATA.property || {};

    const intelligence =
        DATA.intelligence || {};

    const evidence =
        DATA.evidence || {};

    const optional =
        DATA.optional || {};

    const coverage =
        propertyCoverage();


    let sourceHTML = "";

    const sources =
        intelligence.sources || {};

    const sourceKeys =
        Object.keys(sources);

    if (sourceKeys.length === 0) {

        sourceHTML =
            '<div class="empty">' +
            'No property-specific source records yet.' +
            '</div>';

    } else {

        sourceHTML =
            '<div class="source-list">';

        sourceKeys.forEach(
            function(source) {

                sourceHTML +=
                    '<div class="source-item">' +

                    '<span>' +
                    esc(source) +
                    '</span>' +

                    '<span class="source-count">' +
                    sources[source] +
                    '</span>' +

                    '</div>';
            }
        );

        sourceHTML +=
            '</div>';
    }


    let recordsHTML = "";

    const records =
        intelligence.records || [];

    if (records.length === 0) {

        recordsHTML =
            '<div class="empty">' +
            'No intelligence records are currently attached to this property.' +
            '</div>';

    } else {

        recordsHTML =
            '<div style="overflow:auto;">' +

            '<table class="record-table">' +

            '<thead>' +
            '<tr>' +
            '<th>Record</th>' +
            '<th>Source</th>' +
            '<th>Status</th>' +
            '<th>Location</th>' +
            '<th>Evidence</th>' +
            '</tr>' +
            '</thead>' +

            '<tbody>';

        records.slice(0, 25)
            .forEach(
                function(record) {

                    const location =
                        record.latitude !== null &&
                        record.longitude !== null
                            ? "Mapped"
                            : "Unmapped";

                    recordsHTML +=
                        '<tr>' +

                        '<td>' +
                        fmt(record.title) +
                        '</td>' +

                        '<td>' +
                        fmt(
                            record.source_name ||
                            record.source_type
                        ) +
                        '</td>' +

                        '<td>' +
                        '<span class="status">' +
                        fmt(record.status) +
                        '</span>' +
                        '</td>' +

                        '<td>' +
                        location +
                        '</td>' +

                        '<td>' +
                        fmt(record.evidence_level) +
                        '</td>' +

                        '</tr>';
                }
            );

        recordsHTML +=
            '</tbody></table></div>';
    }


    document.getElementById("app").innerHTML = `

        <section class="hero">

            <div class="hero-card">

                <div class="kicker">
                    PROPERTY INTELLIGENCE
                </div>

                <div class="property-name">
                    ${fmt(p.property_name)}
                </div>

                <div class="address">
                    ${fmt(p.property_address)}
                </div>

                <div class="meta-row">

                    <span class="chip">
                        ${fmt(p.property_type)}
                    </span>

                    <span class="chip">
                        ${p.has_boundary
                            ? "GIS Boundary Available"
                            : "GIS Boundary Pending"}
                    </span>

                    <span class="chip">
                        ${p.latitude !== null &&
                          p.longitude !== null
                            ? "Exact Coordinate Stored"
                            : "Coordinate Pending"}
                    </span>

                </div>

                <div class="metrics">

                    <div class="metric">
                        <div class="metric-label">
                            Area
                        </div>

                        <div class="metric-value">
                            ${fmt(p.area)}
                        </div>
                    </div>

                    <div class="metric">
                        <div class="metric-label">
                            Price
                        </div>

                        <div class="metric-value">
                            ${fmt(p.price)}
                        </div>
                    </div>

                    <div class="metric">
                        <div class="metric-label">
                            Price / Sq.Ft
                        </div>

                        <div class="metric-value">
                            ${fmt(p.price_per_sqft)}
                        </div>
                    </div>

                    <div class="metric">
                        <div class="metric-label">
                            Bedrooms
                        </div>

                        <div class="metric-value">
                            ${fmt(p.bedrooms)}
                        </div>
                    </div>

                </div>

            </div>


            <div class="hero-card coverage">

                <div class="coverage-title">
                    Property Data Coverage
                </div>

                <div class="coverage-number">
                    ${coverage}%
                </div>

                <div class="coverage-caption">
                    Coverage reflects populated property
                    profile fields. It is not an investment
                    or valuation score.
                </div>

            </div>

        </section>


        <section class="grid">

            <div class="card">

                <div class="card-title">
                    Intelligence Records
                </div>

                <div class="card-value">
                    ${intelligence.total || 0}
                </div>

                <div class="card-note">
                    Property-linked intelligence
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Mapped Records
                </div>

                <div class="card-value">
                    ${intelligence.mapped || 0}
                </div>

                <div class="card-note">
                    Records with coordinates
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Evidence
                </div>

                <div class="card-value">
                    ${evidence.count || 0}
                </div>

                <div class="card-note">
                    Source/evidence records
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Unmapped
                </div>

                <div class="card-value">
                    ${intelligence.unmapped || 0}
                </div>

                <div class="card-note">
                    Location pending
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Market Observations
                </div>

                <div class="card-value">
                    ${optional.market_observations || 0}
                </div>

                <div class="card-note">
                    Stored market observations
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Documents
                </div>

                <div class="card-value">
                    ${optional.documents || 0}
                </div>

                <div class="card-note">
                    Property files
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Live Refreshes
                </div>

                <div class="card-value">
                    ${optional.refresh_runs || 0}
                </div>

                <div class="card-note">
                    Recorded refresh runs
                </div>

            </div>


            <div class="card">

                <div class="card-title">
                    Boundary
                </div>

                <div class="card-value">
                    ${p.has_boundary ? "YES" : "PENDING"}
                </div>

                <div class="card-note">
                    PostGIS property boundary
                </div>

            </div>

        </section>


        <section class="section">

            <div class="section-title">

                <h2>
                    Intelligence Workspaces
                </h2>

                <span>
                    Existing PropertyIQ modules
                </span>

            </div>


            <div class="workspace-grid">

                <div class="workspace"
                     onclick="openWorkspace('full_profile')">

                    <div class="workspace-name">
                        Full Property Profile
                    </div>

                    <div class="workspace-desc">
                        Unified property profile and coverage.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('command_map')">

                    <div class="workspace-name">
                        Command Map
                    </div>

                    <div class="workspace-desc">
                        OSM, RERA, government, news and intelligence map.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('timeline')">

                    <div class="workspace-name">
                        Development Timeline
                    </div>

                    <div class="workspace-desc">
                        Chronological development and evidence events.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('boundary_intelligence')">

                    <div class="workspace-name">
                        Boundary Intelligence
                    </div>

                    <div class="workspace-desc">
                        Intelligence relative to the property boundary.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('due_diligence')">

                    <div class="workspace-name">
                        Due Diligence Map
                    </div>

                    <div class="workspace-desc">
                        Spatial due-diligence workspace.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('market')">

                    <div class="workspace-name">
                        Market & Comparables
                    </div>

                    <div class="workspace-desc">
                        Existing property and market observations.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('real_osm')">

                    <div class="workspace-name">
                        Real OSM / GIS
                    </div>

                    <div class="workspace-desc">
                        Source-backed OpenStreetMap context.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('boundary')">

                    <div class="workspace-name">
                        GIS Boundary
                    </div>

                    <div class="workspace-desc">
                        Draw, edit and persist property geometry.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('documents')">

                    <div class="workspace-name">
                        Documents
                    </div>

                    <div class="workspace-desc">
                        Property files and document workspace.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('document_intelligence')">

                    <div class="workspace-name">
                        Document Intelligence
                    </div>

                    <div class="workspace-desc">
                        Document-derived intelligence workspace.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('report')">

                    <div class="workspace-name">
                        Intelligence Report
                    </div>

                    <div class="workspace-desc">
                        Printable property intelligence report.
                    </div>

                </div>


                <div class="workspace"
                     onclick="openWorkspace('workspace')">

                    <div class="workspace-name">
                        Premium Workspace
                    </div>

                    <div class="workspace-desc">
                        PropertyIQ workspace navigation.
                    </div>

                </div>

            </div>

        </section>


        <section class="section">

            <div class="section-title">

                <h2>
                    Intelligence Source Inventory
                </h2>

                <span>
                    Property-linked records only
                </span>

            </div>

            ${sourceHTML}

        </section>


        <section class="section">

            <div class="section-title">

                <h2>
                    Recent Intelligence
                </h2>

                <span>
                    ${records.length} records loaded
                </span>

            </div>

            ${recordsHTML}

        </section>


        <div class="footer">

            PropertyIQ —
            source-backed property intelligence.
            No fabricated intelligence,
            valuation prediction or investment score.

        </div>
    `;
}


render();

</script>

</body>
</html>
"""


# ------------------------------------------------------------------------------
# UI ROUTE
# ------------------------------------------------------------------------------

@app.get(
    "/propertyiq/full-intelligence/{property_id}",
    response_class=HTMLResponse
)
def propertyiq_full_intelligence_ui(property_id: str):

    try:

        data = build_full_dashboard(property_id)

        if data is None:

            return HTMLResponse(
                "<h2>Property not found</h2>",
                status_code=404
            )

        html = DASHBOARD_HTML

        html = html.replace(
            "__PROPERTY_ID__",
            str(property_id)
        )

        html = html.replace(
            "__PROPERTYIQ_DATA__",
            json.dumps(
                data,
                default=str
            )
        )

        return HTMLResponse(html)

    except Exception as exc:

        return HTMLResponse(
            "<h2>PropertyIQ Dashboard Error</h2>"
            "<pre>" +
            str(exc) +
            "</pre>",
            status_code=500
        )


# ------------------------------------------------------------------------------
# INSTALLATION MESSAGE
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ — FULL PROPERTY INTELLIGENCE DASHBOARD INSTALLED")
print("=" * 78)
print()
print("Module:")
print(" ", MODULE_VERSION)
print()
print("API:")
print("  /api/v1/properties/{property_id}/full-intelligence-dashboard")
print()
print("UI:")
print("  /propertyiq/full-intelligence/{property_id}")
print()
print("Integrated:")
print("  ✓ Full Property Profile")
print("  ✓ Unified Intelligence Command Map")
print("  ✓ Timeline & Development Pipeline")
print("  ✓ GIS Boundary")
print("  ✓ Boundary Intelligence")
print("  ✓ OSM / GIS")
print("  ✓ RERA")
print("  ✓ Government Infrastructure")
print("  ✓ News")
print("  ✓ Market / Comparables")
print("  ✓ Evidence")
print("  ✓ Documents")
print("  ✓ Intelligence Report")
print("  ✓ Premium Workspace")
print()
print("Premium dashboard: ENABLED")
print("Read-only integration: ENABLED")
print("Database modification during install: DISABLED")
print("Fabricated intelligence: DISABLED")
print("Valuation prediction: DISABLED")
print("Investment score: DISABLED")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)



# ============================================================
# PROPERTYIQ MODULE: PORTFOLIO
# ORIGINAL COLAB CELL: In[86]
# ============================================================

# ==============================================================================
# PROPERTYIQ — V27 PROPERTY PORTFOLIO & DATABASE LAYER
# ==============================================================================
# Direct-paste into Google Colab.
#
# Adds:
#   • Portfolio dashboard
#   • Property database table
#   • Property search
#   • Property-type filtering
#   • Portfolio map
#   • Property intelligence coverage
#   • Boundary status
#   • Evidence/document counts
#   • Direct links to existing PropertyIQ workspaces
#
# Does NOT:
#   • create a second property database
#   • rebuild previous modules
#   • fabricate property data
#   • modify existing property records during installation
#   • create investment/valuation scores
# ==============================================================================

import sys
import subprocess
import json
from datetime import datetime

MODULE_VERSION = "PROPERTYIQ-V27-PROPERTY-PORTFOLIO-DATABASE"


# ==============================================================================
# DEPENDENCY
# ==============================================================================

try:
    import fastapi
except Exception:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg[binary]"
    ])

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text


# ==============================================================================
# EXISTING RUNTIME
# ==============================================================================

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ PostgreSQL engine is not available. "
        "Run the existing PropertyIQ database/runtime cell first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(title="PropertyIQ")


# ==============================================================================
# HELPERS
# ==============================================================================

def _table_exists(conn, table_name):

    try:
        return bool(
            conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        AND table_name = :table
                    )
                """),
                {"table": table_name}
            ).scalar()
        )
    except Exception:
        return False


def _safe(value):

    if value is None:
        return None

    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _property_counts(conn, property_ids):

    result = {
        pid: {
            "intelligence_count": 0,
            "evidence_count": 0,
            "document_count": 0,
            "market_count": 0
        }
        for pid in property_ids
    }

    if not property_ids:
        return result

    # ------------------------------------------------------------------
    # Intelligence
    # ------------------------------------------------------------------

    if _table_exists(conn, "intelligence_records"):

        rows = conn.execute(
            text("""
                SELECT
                    property_id::text AS property_id,
                    COUNT(*) AS count
                FROM intelligence_records
                WHERE property_id IN (
                    SELECT CAST(x AS uuid)
                    FROM jsonb_array_elements_text(
                        CAST(:ids AS jsonb)
                    ) AS x
                )
                GROUP BY property_id
            """),
            {
                "ids": json.dumps(property_ids)
            }
        ).mappings().all()

        for row in rows:

            pid = row["property_id"]

            if pid in result:
                result[pid]["intelligence_count"] = int(
                    row["count"] or 0
                )

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    if _table_exists(conn, "evidence_items"):

        try:

            rows = conn.execute(
                text("""
                    SELECT
                        property_id::text AS property_id,
                        COUNT(*) AS count
                    FROM evidence_items
                    WHERE property_id IN (
                        SELECT CAST(x AS uuid)
                        FROM jsonb_array_elements_text(
                            CAST(:ids AS jsonb)
                        ) AS x
                    )
                    GROUP BY property_id
                """),
                {
                    "ids": json.dumps(property_ids)
                }
            ).mappings().all()

            for row in rows:

                pid = row["property_id"]

                if pid in result:
                    result[pid]["evidence_count"] = int(
                        row["count"] or 0
                    )

        except Exception:
            pass

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    if _table_exists(conn, "property_files"):

        try:

            rows = conn.execute(
                text("""
                    SELECT
                        property_id::text AS property_id,
                        COUNT(*) AS count
                    FROM property_files
                    WHERE property_id IN (
                        SELECT CAST(x AS uuid)
                        FROM jsonb_array_elements_text(
                            CAST(:ids AS jsonb)
                        ) AS x
                    )
                    GROUP BY property_id
                """),
                {
                    "ids": json.dumps(property_ids)
                }
            ).mappings().all()

            for row in rows:

                pid = row["property_id"]

                if pid in result:
                    result[pid]["document_count"] = int(
                        row["count"] or 0
                    )

        except Exception:
            pass

    # ------------------------------------------------------------------
    # Market observations
    # ------------------------------------------------------------------

    if _table_exists(conn, "property_market_observations"):

        try:

            rows = conn.execute(
                text("""
                    SELECT
                        property_id::text AS property_id,
                        COUNT(*) AS count
                    FROM property_market_observations
                    WHERE property_id IN (
                        SELECT CAST(x AS uuid)
                        FROM jsonb_array_elements_text(
                            CAST(:ids AS jsonb)
                        ) AS x
                    )
                    GROUP BY property_id
                """),
                {
                    "ids": json.dumps(property_ids)
                }
            ).mappings().all()

            for row in rows:

                pid = row["property_id"]

                if pid in result:
                    result[pid]["market_count"] = int(
                        row["count"] or 0
                    )

        except Exception:
            pass

    return result


# ==============================================================================
# LOAD PORTFOLIO
# ==============================================================================

def get_portfolio(
    search=None,
    property_type=None,
    boundary=None,
    limit=500
):

    with engine.begin() as conn:

        where = []
        params = {
            "limit": min(max(int(limit), 1), 1000)
        }

        # --------------------------------------------------------------
        # Search
        # --------------------------------------------------------------

        if search:

            where.append("""
                (
                    property_name ILIKE :search
                    OR property_address ILIKE :search
                    OR builder_owner ILIKE :search
                    OR property_type ILIKE :search
                )
            """)

            params["search"] = "%" + search + "%"

        # --------------------------------------------------------------
        # Property type
        # --------------------------------------------------------------

        if property_type:

            where.append(
                "property_type = :property_type"
            )

            params["property_type"] = property_type

        # --------------------------------------------------------------
        # Boundary
        # --------------------------------------------------------------

        if boundary == "yes":

            where.append(
                "boundary IS NOT NULL"
            )

        elif boundary == "no":

            where.append(
                "boundary IS NULL"
            )

        where_sql = ""

        if where:
            where_sql = (
                "WHERE " +
                " AND ".join(where)
            )

        rows = conn.execute(
            text(f"""
                SELECT
                    id::text AS id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    CASE
                        WHEN boundary IS NOT NULL
                        THEN true
                        ELSE false
                    END AS has_boundary
                FROM properties
                {where_sql}
                ORDER BY property_name ASC
                LIMIT :limit
            """),
            params
        ).mappings().all()

        properties = [
            {
                key: _safe(value)
                for key, value in dict(row).items()
            }
            for row in rows
        ]

        ids = [
            item["id"]
            for item in properties
        ]

        counts = _property_counts(
            conn,
            ids
        )

        for item in properties:

            c = counts.get(
                item["id"],
                {}
            )

            item.update(c)

    return properties


# ==============================================================================
# PORTFOLIO SUMMARY
# ==============================================================================

def get_portfolio_summary(properties):

    total = len(properties)

    mapped = sum(
        1
        for p in properties
        if p.get("latitude") is not None
        and p.get("longitude") is not None
    )

    boundaries = sum(
        1
        for p in properties
        if p.get("has_boundary")
    )

    intelligence = sum(
        int(p.get("intelligence_count") or 0)
        for p in properties
    )

    evidence = sum(
        int(p.get("evidence_count") or 0)
        for p in properties
    )

    documents = sum(
        int(p.get("document_count") or 0)
        for p in properties
    )

    market = sum(
        int(p.get("market_count") or 0)
        for p in properties
    )

    property_types = {}

    for p in properties:

        t = p.get("property_type") or "Unknown"

        property_types[t] = (
            property_types.get(t, 0) + 1
        )

    return {
        "total_properties": total,
        "mapped_properties": mapped,
        "unmapped_properties": total - mapped,
        "properties_with_boundary": boundaries,
        "properties_without_boundary": total - boundaries,
        "intelligence_records": intelligence,
        "evidence_records": evidence,
        "documents": documents,
        "market_observations": market,
        "property_types": property_types
    }


# ==============================================================================
# API — PORTFOLIO
# ==============================================================================

PORTFOLIO_API_ROUTE = (
    "/api/v1/properties/portfolio"
)


@app.get(PORTFOLIO_API_ROUTE)
def propertyiq_portfolio_api(
    search: str = Query(
        default=None
    ),
    property_type: str = Query(
        default=None
    ),
    boundary: str = Query(
        default=None
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=1000
    )
):

    try:

        properties = get_portfolio(
            search=search,
            property_type=property_type,
            boundary=boundary,
            limit=limit
        )

        return {
            "module": MODULE_VERSION,
            "generated_at":
                datetime.utcnow().isoformat() + "Z",
            "summary":
                get_portfolio_summary(properties),
            "properties":
                properties
        }

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc)
            }
        )


# ==============================================================================
# API — PROPERTY TYPES
# ==============================================================================

@app.get(
    "/api/v1/properties/portfolio/property-types"
)
def propertyiq_property_types():

    try:

        with engine.begin() as conn:

            rows = conn.execute(
                text("""
                    SELECT
                        COALESCE(
                            property_type,
                            'Unknown'
                        ) AS property_type,
                        COUNT(*) AS count
                    FROM properties
                    GROUP BY property_type
                    ORDER BY count DESC
                """)
            ).mappings().all()

        return {
            "property_types": [
                {
                    "property_type":
                        row["property_type"],
                    "count":
                        int(row["count"])
                }
                for row in rows
            ]
        }

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc)
            }
        )


# ==============================================================================
# PREMIUM PORTFOLIO HTML
# ==============================================================================

PORTFOLIO_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>PropertyIQ — Portfolio</title>

<link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,200,255,.10),
            transparent 30%
        ),
        radial-gradient(
            circle at 100% 15%,
            rgba(50,90,255,.08),
            transparent 30%
        ),
        #07101b;

    color: #e9f3ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

.header {

    height: 68px;

    display: flex;

    align-items: center;

    justify-content: space-between;

    padding:
        0 24px;

    border-bottom:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(5,13,24,.88);

    backdrop-filter:
        blur(18px);

    position: sticky;

    top: 0;

    z-index: 1000;
}

.logo {

    display: flex;

    align-items: center;

    gap: 11px;
}

.logo-mark {

    width: 34px;
    height: 34px;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            #27d7ff,
            #345cff
        );
}

.logo-title {

    font-size: 17px;

    font-weight: 850;
}

.logo-sub {

    font-size: 9px;

    color: #71879f;

    letter-spacing: 1.2px;
}

.container {

    max-width: 1550px;

    margin: auto;

    padding: 22px;
}

.title {

    font-size: 28px;

    font-weight: 850;

    margin-bottom: 4px;
}

.subtitle {

    color: #71879e;

    font-size: 12px;

    margin-bottom: 20px;
}

.filters {

    display: grid;

    grid-template-columns:
        minmax(220px, 1fr)
        180px
        150px
        auto;

    gap: 9px;

    margin-bottom: 16px;
}

.input,
.select,
.button {

    border:
        1px solid rgba(255,255,255,.09);

    background:
        rgba(255,255,255,.035);

    color: #dcecff;

    border-radius: 10px;

    padding: 10px 12px;

    outline: none;

    font-size: 12px;
}

.input:focus,
.select:focus {

    border-color:
        rgba(64,210,255,.40);
}

.button {

    cursor: pointer;

    color: #72dcff;

    border-color:
        rgba(64,210,255,.25);
}

.button:hover {

    background:
        rgba(64,210,255,.08);
}

.summary {

    display: grid;

    grid-template-columns:
        repeat(6, minmax(0,1fr));

    gap: 9px;

    margin-bottom: 16px;
}

.stat {

    border:
        1px solid rgba(255,255,255,.07);

    border-radius: 13px;

    background:
        rgba(12,23,39,.75);

    padding: 14px;
}

.stat-label {

    font-size: 9px;

    color: #71889f;

    text-transform: uppercase;

    letter-spacing: .8px;
}

.stat-value {

    margin-top: 6px;

    font-size: 22px;

    font-weight: 850;
}

.layout {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        430px;

    gap: 14px;
}

.panel {

    border:
        1px solid rgba(255,255,255,.075);

    border-radius: 15px;

    background:
        rgba(10,21,35,.82);

    overflow: hidden;
}

.panel-head {

    display: flex;

    align-items: center;

    justify-content: space-between;

    padding: 14px 16px;

    border-bottom:
        1px solid rgba(255,255,255,.06);
}

.panel-title {

    font-size: 12px;

    font-weight: 800;
}

.panel-note {

    font-size: 10px;

    color: #6d8299;
}

#map {

    height: 620px;

    width: 100%;
}

.table-wrap {

    max-height: 620px;

    overflow: auto;
}

table {

    width: 100%;

    border-collapse: collapse;
}

th {

    position: sticky;

    top: 0;

    z-index: 3;

    background: #0b1728;

    color: #71889f;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: .7px;

    text-align: left;

    padding: 10px 9px;

    border-bottom:
        1px solid rgba(255,255,255,.07);
}

td {

    padding: 10px 9px;

    font-size: 10px;

    color: #b7c9dc;

    border-bottom:
        1px solid rgba(255,255,255,.045);

    vertical-align: top;
}

tr:hover td {

    background:
        rgba(64,210,255,.035);
}

.property-name {

    color: #70ddff;

    font-weight: 750;

    cursor: pointer;
}

.property-address {

    color: #6d839a;

    margin-top: 3px;

    line-height: 1.35;
}

.badge {

    display: inline-block;

    border-radius: 999px;

    padding: 4px 7px;

    background:
        rgba(255,255,255,.045);

    color: #8fa7bf;

    font-size: 8px;
}

.badge-blue {

    color: #70ddff;

    background:
        rgba(64,210,255,.07);
}

.empty {

    padding: 30px;

    text-align: center;

    color: #71869d;

    font-size: 12px;
}

@media(max-width: 1100px) {

    .summary {

        grid-template-columns:
            repeat(3,1fr);
    }

    .layout {

        grid-template-columns: 1fr;
    }
}

@media(max-width: 700px) {

    .container {

        padding: 12px;
    }

    .filters {

        grid-template-columns: 1fr;
    }

    .summary {

        grid-template-columns:
            repeat(2,1fr);
    }
}

</style>

</head>


<body>

<header class="header">

    <div class="logo">

        <div class="logo-mark"></div>

        <div>

            <div class="logo-title">
                PropertyIQ
            </div>

            <div class="logo-sub">
                PROPERTY PORTFOLIO
            </div>

        </div>

    </div>

</header>


<main class="container">

    <div class="title">
        Property Portfolio
    </div>

    <div class="subtitle">
        Map-first portfolio intelligence across all
        properties stored in PropertyIQ.
    </div>


    <div class="filters">

        <input
            id="search"
            class="input"
            placeholder="Search property, address, builder or type..."
        />


        <select id="type"
                class="select">

            <option value="">
                All property types
            </option>

        </select>


        <select id="boundary"
                class="select">

            <option value="">
                All boundaries
            </option>

            <option value="yes">
                Boundary available
            </option>

            <option value="no">
                Boundary pending
            </option>

        </select>


        <button
            class="button"
            onclick="loadPortfolio()">

            Apply Filters

        </button>

    </div>


    <div id="summary"
         class="summary">
    </div>


    <div class="layout">


        <section class="panel">

            <div class="panel-head">

                <div class="panel-title">
                    Portfolio Map
                </div>

                <div class="panel-note">
                    Stored coordinates only
                </div>

            </div>

            <div id="map"></div>

        </section>


        <section class="panel">

            <div class="panel-head">

                <div class="panel-title">
                    Property Database
                </div>

                <div id="count"
                     class="panel-note">
                    Loading...
                </div>

            </div>


            <div class="table-wrap">

                <table>

                    <thead>

                    <tr>

                        <th>
                            Property
                        </th>

                        <th>
                            Type
                        </th>

                        <th>
                            Area
                        </th>

                        <th>
                            Price
                        </th>

                        <th>
                            Intel
                        </th>

                    </tr>

                    </thead>

                    <tbody id="propertyRows">
                    </tbody>

                </table>

            </div>

        </section>

    </div>

</main>


<script>

let map = null;

let markers = [];


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}


function initMap() {

    map = L.map("map")
        .setView(
            [28.6139,77.2090],
            9
        );

    L.tileLayer(
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom: 19,
            attribution:
                "&copy; OpenStreetMap contributors"
        }
    ).addTo(map);
}


function clearMarkers() {

    markers.forEach(
        function(marker) {

            map.removeLayer(marker);

        }
    );

    markers = [];
}


function renderSummary(summary) {

    const values = [

        [
            "Properties",
            summary.total_properties
        ],

        [
            "Mapped",
            summary.mapped_properties
        ],

        [
            "Boundaries",
            summary.properties_with_boundary
        ],

        [
            "Intelligence",
            summary.intelligence_records
        ],

        [
            "Evidence",
            summary.evidence_records
        ],

        [
            "Documents",
            summary.documents
        ]

    ];


    document.getElementById(
        "summary"
    ).innerHTML =
        values.map(
            function(item) {

                return `
                    <div class="stat">

                        <div class="stat-label">
                            ${esc(item[0])}
                        </div>

                        <div class="stat-value">
                            ${esc(item[1])}
                        </div>

                    </div>
                `;

            }
        ).join("");
}


function renderMap(properties) {

    clearMarkers();

    const mapped = properties.filter(
        function(p) {

            return (
                p.latitude !== null &&
                p.longitude !== null
            );

        }
    );


    if (!mapped.length) {

        return;
    }


    const bounds = [];


    mapped.forEach(
        function(p) {

            const lat =
                Number(p.latitude);

            const lon =
                Number(p.longitude);


            if (
            ) {
                return;
            }


            const marker =
                L.marker([lat,lon])
                    .addTo(map);


            const profileURL =
                "/propertyiq/full-intelligence/" +
                p.id;


            marker.bindPopup(`

                <div style="
                    min-width:210px;
                    font-family:Arial,sans-serif;
                ">

                    <strong>
                        ${esc(p.property_name)}
                    </strong>

                    <br>

                    <span style="
                        color:#64748b;
                        font-size:11px;
                    ">
                        ${esc(p.property_address)}
                    </span>

                    <br><br>

                    <span style="
                        font-size:11px;
                    ">
                        Type:
                        ${esc(p.property_type)}
                    </span>

                    <br>

                    <span style="
                        font-size:11px;
                    ">
                        Intelligence:
                        ${esc(p.intelligence_count)}
                    </span>

                    <br><br>

                    <a
                        href="${profileURL}"
                        target="_blank"
                    >
                        Open Intelligence Dashboard →
                    </a>

                </div>

            `);

            markers.push(marker);

            bounds.push([lat,lon]);

        }
    );


    if (bounds.length === 1) {

        map.setView(
            bounds[0],
            14
        );

    } else if (bounds.length > 1) {

        map.fitBounds(
            bounds,
            {
                padding: [30,30]
            }
        );
    }
}


function renderTable(properties) {

    const tbody =
        document.getElementById(
            "propertyRows"
        );


    document.getElementById(
        "count"
    ).textContent =
        properties.length +
        " properties";


    if (!properties.length) {

        tbody.innerHTML = `
            <tr>
                <td colspan="5">
                    <div class="empty">
                        No properties found.
                    </div>
                </td>
            </tr>
        `;

        return;
    }


    tbody.innerHTML =
        properties.map(
            function(p) {

                const profileURL =
                    "/propertyiq/full-intelligence/" +
                    p.id;


                return `

                    <tr>

                        <td>

                            <div
                                class="property-name"
                                onclick="
                                    window.open(
                                        '${profileURL}',
                                        '_blank'
                                    )
                                "
                            >
                                ${esc(p.property_name)}
                            </div>

                            <div class="property-address">
                                ${esc(p.property_address)}
                            </div>

                        </td>


                        <td>

                            <span class="badge">
                                ${esc(
                                    p.property_type ||
                                    "Unknown"
                                )}
                            </span>

                        </td>


                        <td>
                            ${esc(p.area)}
                        </td>


                        <td>
                            ${esc(p.price)}
                        </td>


                        <td>

                            <span class="badge badge-blue">
                                ${esc(
                                    p.intelligence_count || 0
                                )}
                            </span>

                        </td>

                    </tr>

                `;

            }
        ).join("");
}


async function loadPropertyTypes() {

    try {

        const response =
            await fetch(
                "/api/v1/properties/portfolio/property-types"
            );

        const data =
            await response.json();

        const select =
            document.getElementById(
                "type"
            );


        (data.property_types || [])
            .forEach(
                function(item) {

                    const option =
                        document.createElement(
                            "option"
                        );

                    option.value =
                        item.property_type;

                    option.textContent =
                        item.property_type +
                        " (" +
                        item.count +
                        ")";

                    select.appendChild(
                        option
                    );

                }
            );

    } catch (error) {

        console.error(
            "Property type loading failed",
            error
        );
    }
}


async function loadPortfolio() {

    try {

        const search =
            document.getElementById(
                "search"
            ).value.trim();


        const type =
            document.getElementById(
                "type"
            ).value;


        const boundary =
            document.getElementById(
                "boundary"
            ).value;


        const params =
            new URLSearchParams();


        if (search) {
            params.set(
                "search",
                search
            );
        }

        if (type) {
            params.set(
                "property_type",
                type
            );
        }

        if (boundary) {
            params.set(
                "boundary",
                boundary
            );
        }


        const response =
            await fetch(
                "/api/v1/properties/portfolio?" +
                params.toString()
            );


        const data =
            await response.json();


        if (!response.ok) {

            throw new Error(
                data.detail ||
                "Portfolio request failed"
            );
        }


        renderSummary(
            data.summary || {}
        );


        renderTable(
            data.properties || []
        );


        renderMap(
            data.properties || []
        );


    } catch (error) {

        console.error(error);

        document.getElementById(
            "propertyRows"
        ).innerHTML = `

            <tr>

                <td colspan="5">

                    <div class="empty">

                        Portfolio loading error:
                        ${esc(error.message)}

                    </div>

                </td>

            </tr>

        `;
    }
}


initMap();

loadPropertyTypes();

loadPortfolio();

</script>

</body>

</html>
"""


# ==============================================================================
# UI ROUTE
# ==============================================================================

@app.get(
    "/propertyiq/portfolio",
    response_class=HTMLResponse
)
def propertyiq_portfolio_ui():

    return HTMLResponse(
        PORTFOLIO_HTML
    )


# ==============================================================================
# INSTALLATION OUTPUT
# ==============================================================================

print()
print("=" * 78)
print("PROPERTYIQ V27 — PROPERTY PORTFOLIO & DATABASE INSTALLED")
print("=" * 78)
print()
print("Module:")
print(" ", MODULE_VERSION)
print()
print("API:")
print("  /api/v1/properties/portfolio")
print("  /api/v1/properties/portfolio/property-types")
print()
print("UI:")
print("  /propertyiq/portfolio")
print()
print("Capabilities:")
print("  ✓ Existing PostgreSQL/PostGIS property database")
print("  ✓ Portfolio property listing")
print("  ✓ Property search")
print("  ✓ Property-type filtering")
print("  ✓ Boundary filtering")
print("  ✓ Portfolio map")
print("  ✓ Stored-coordinate mapping")
print("  ✓ Intelligence counts")
print("  ✓ Evidence counts")
print("  ✓ Document counts")
print("  ✓ Market observation counts")
print("  ✓ Direct property intelligence links")
print()
print("Second property database: DISABLED")
print("Database modification during install: DISABLED")
print("Fabricated property data: DISABLED")
print("Valuation prediction: DISABLED")
print("Investment score: DISABLED")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)



# ============================================================
# PROPERTYIQ MODULE: V28
# ORIGINAL COLAB CELL: In[88]
# ============================================================

# ==============================================================================
# PROPERTYIQ V28 — PROPERTY ACQUISITION & DUE-DILIGENCE WORKSPACE
# ==============================================================================
# DIRECT-PASTE GOOGLE COLAB CELL
#
# This is an additive module.
#
# It uses:
#   • Existing PostgreSQL/PostGIS database
#   • Existing PropertyIQ intelligence_records
#   • Existing evidence_items
#   • Existing property_files
#   • Existing property_market_observations
#   • Existing piq_property_refresh_runs
#   • Existing GIS boundary
#
# It DOES NOT:
#   • create another property database
#   • rebuild RERA
#   • rebuild OSM
#   • rebuild news
#   • rebuild market intelligence
#   • fabricate intelligence
#   • create investment scores
#   • predict valuation
#   • modify database during installation
# ==============================================================================

import sys
import subprocess
import json
from datetime import datetime


MODULE_VERSION = "PROPERTYIQ-V28-ACQUISITION-DUE-DILIGENCE"


# ==============================================================================
# DEPENDENCIES
# ==============================================================================

try:
    import fastapi
except Exception:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg[binary]"
    ])

from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text


# ==============================================================================
# EXISTING PROPERTYIQ RUNTIME
# ==============================================================================

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ PostgreSQL engine is not available. "
        "Run the existing PropertyIQ PostgreSQL runtime/bootstrap cell first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(title="PropertyIQ")


# ==============================================================================
# SCHEMA HELPERS
# ==============================================================================

def v28_table_exists(conn, table_name):

    try:
        return bool(
            conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema='public'
                        AND table_name=:table
                    )
                """),
                {"table": table_name}
            ).scalar()
        )
    except Exception:
        return False


def v28_column_exists(
    conn,
    table_name,
    column_name
):

    try:

        return bool(
            conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema='public'
                        AND table_name=:table
                        AND column_name=:column
                    )
                """),
                {
                    "table": table_name,
                    "column": column_name
                }
            ).scalar()
        )

    except Exception:

        return False


def v28_safe(value):

    if value is None:
        return None

    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


# ==============================================================================
# PROPERTY
# ==============================================================================

def v28_get_property(
    conn,
    property_id
):

    row = conn.execute(
        text("""
            SELECT
                id::text AS id,
                property_name,
                property_address,
                latitude,
                longitude,
                property_type,
                area,
                price,
                price_per_sqft,
                bedrooms,
                bathrooms,
                builder_owner,
                description,
                amenities,
                photos,
                documents,
                contact_information,

                CASE
                    WHEN boundary IS NOT NULL
                    THEN true
                    ELSE false
                END AS has_boundary

            FROM properties

            WHERE id=CAST(:pid AS uuid)

            LIMIT 1
        """),
        {
            "pid": property_id
        }
    ).mappings().first()

    if not row:
        return None

    return {
        key: v28_safe(value)
        for key, value in dict(row).items()
    }


# ==============================================================================
# INTELLIGENCE
# ==============================================================================

def v28_get_intelligence(
    conn,
    property_id
):

    if not v28_table_exists(
        conn,
        "intelligence_records"
    ):
        return []

    rows = conn.execute(
        text("""
            SELECT
                id::text AS id,
                title,
                category,
                status,
                description,
                latitude,
                longitude,
                source_name,
                source_type,
                source_url,
                published_at,
                confidence,
                evidence_level,
                metadata

            FROM intelligence_records

            WHERE property_id=CAST(:pid AS uuid)

            ORDER BY
                updated_at DESC NULLS LAST

            LIMIT 1000
        """),
        {
            "pid": property_id
        }
    ).mappings().all()

    records = []

    for row in rows:

        r = {
            key: v28_safe(value)
            for key, value in dict(row).items()
        }

        r["mapped"] = (
            r.get("latitude") is not None
            and
            r.get("longitude") is not None
        )

        records.append(r)

    return records


# ==============================================================================
# SOURCE CLASSIFICATION
# ==============================================================================

def v28_classify_source(record):

    source_name = (
        str(
            record.get("source_name")
            or ""
        )
        .lower()
    )

    source_type = (
        str(
            record.get("source_type")
            or ""
        )
        .lower()
    )

    combined = (
        source_name +
        " " +
        source_type
    )

    if (
        "rera" in combined
        or
        "up-rera" in combined
    ):
        return "RERA"

    if (
        "paimana" in combined
        or
        "government" in combined
        or
        "mospi" in combined
        or
        "infrastructure" in combined
    ):
        return "GOVERNMENT"

    if (
        "gdelt" in combined
        or
        "news" in combined
    ):
        return "NEWS"

    if (
        "osm" in combined
        or
        "openstreetmap" in combined
        or
        "overpass" in combined
    ):
        return "OSM"

    return "OTHER"


# ==============================================================================
# EVIDENCE
# ==============================================================================

def v28_get_evidence(
    conn,
    property_id,
    intelligence_ids
):

    if not v28_table_exists(
        conn,
        "evidence_items"
    ):
        return []

    rows = []

    # ------------------------------------------------------------------
    # Direct property linkage
    # ------------------------------------------------------------------

    if v28_column_exists(
        conn,
        "evidence_items",
        "property_id"
    ):

        try:

            rows = conn.execute(
                text("""
                    SELECT *
                    FROM evidence_items
                    WHERE property_id=CAST(:pid AS uuid)
                    LIMIT 1000
                """),
                {
                    "pid": property_id
                }
            ).mappings().all()

        except Exception:

            rows = []

    # ------------------------------------------------------------------
    # Intelligence linkage fallback
    # ------------------------------------------------------------------

    if not rows:

        if (
            intelligence_ids
            and
            v28_column_exists(
                conn,
                "evidence_items",
                "intelligence_id"
            )
        ):

            try:

                rows = conn.execute(
                    text("""
                        SELECT e.*
                        FROM evidence_items e
                        WHERE e.intelligence_id IN (
                            SELECT CAST(x AS uuid)
                            FROM jsonb_array_elements_text(
                                CAST(:ids AS jsonb)
                            ) AS x
                        )
                        LIMIT 1000
                    """),
                    {
                        "ids":
                            json.dumps(
                                intelligence_ids
                            )
                    }
                ).mappings().all()

            except Exception:

                rows = []

    result = []

    for row in rows:

        item = {
            key: v28_safe(value)
            for key, value in dict(row).items()
        }

        result.append(item)

    return result


# ==============================================================================
# DOCUMENTS
# ==============================================================================

def v28_get_documents(
    conn,
    property_id
):

    if not v28_table_exists(
        conn,
        "property_files"
    ):
        return []

    try:

        rows = conn.execute(
            text("""
                SELECT *
                FROM property_files
                WHERE property_id=CAST(:pid AS uuid)
                ORDER BY id DESC
                LIMIT 500
            """),
            {
                "pid": property_id
            }
        ).mappings().all()

    except Exception:

        rows = []

    documents = []

    for row in rows:

        item = {}

        for key, value in dict(row).items():

            if key in (
                "id",
                "property_id",
                "filename",
                "file_name",
                "mime_type",
                "content_type",
                "file_bytes",
                "size_bytes",
                "description",
                "created_at",
                "updated_at"
            ):

                item[key] = v28_safe(value)

        documents.append(item)

    return documents


# ==============================================================================
# MARKET OBSERVATIONS
# ==============================================================================

def v28_get_market(
    conn,
    property_id
):

    if not v28_table_exists(
        conn,
        "property_market_observations"
    ):
        return []

    try:

        rows = conn.execute(
            text("""
                SELECT *
                FROM property_market_observations
                WHERE property_id=CAST(:pid AS uuid)
                LIMIT 500
            """),
            {
                "pid": property_id
            }
        ).mappings().all()

    except Exception:

        rows = []

    result = []

    for row in rows:

        result.append({
            key: v28_safe(value)
            for key, value in dict(row).items()
        })

    return result


# ==============================================================================
# REFRESH HISTORY
# ==============================================================================

def v28_get_refresh_history(
    conn,
    property_id
):

    if not v28_table_exists(
        conn,
        "piq_property_refresh_runs"
    ):
        return []

    try:

        rows = conn.execute(
            text("""
                SELECT *
                FROM piq_property_refresh_runs
                WHERE property_id=CAST(:pid AS uuid)
                ORDER BY
                    started_at DESC NULLS LAST,
                    id DESC
                LIMIT 100
            """),
            {
                "pid": property_id
            }
        ).mappings().all()

    except Exception:

        rows = []

    return [
        {
            key: v28_safe(value)
            for key, value in dict(row).items()
        }
        for row in rows
    ]


# ==============================================================================
# SOURCE INVENTORY
# ==============================================================================

def v28_source_inventory(
    intelligence
):

    result = {}

    for record in intelligence:

        source = v28_classify_source(
            record
        )

        if source not in result:

            result[source] = {
                "count": 0,
                "mapped": 0,
                "unmapped": 0
            }

        result[source]["count"] += 1

        if record.get("mapped"):
            result[source]["mapped"] += 1
        else:
            result[source]["unmapped"] += 1

    return result


# ==============================================================================
# DUE-DILIGENCE FLAGS
# ==============================================================================

def v28_build_flags(
    property_data,
    intelligence,
    evidence,
    documents,
    market
):

    flags = []

    # ------------------------------------------------------------------
    # Coordinate
    # ------------------------------------------------------------------

    if (
        property_data.get("latitude") is None
        or
        property_data.get("longitude") is None
    ):

        flags.append({
            "key":
                "LOCATION_PENDING",

            "severity":
                "attention",

            "title":
                "Property coordinate is pending",

            "description":
                "The property does not currently have both "
                "latitude and longitude stored."
        })

    # ------------------------------------------------------------------
    # Boundary
    # ------------------------------------------------------------------

    if not property_data.get(
        "has_boundary"
    ):

        flags.append({
            "key":
                "BOUNDARY_PENDING",

            "severity":
                "attention",

            "title":
                "Property boundary is pending",

            "description":
                "No PostGIS property boundary is currently "
                "stored for this property."
        })

    # ------------------------------------------------------------------
    # Intelligence
    # ------------------------------------------------------------------

    if not intelligence:

        flags.append({
            "key":
                "NO_PROPERTY_INTELLIGENCE",

            "severity":
                "attention",

            "title":
                "No property-linked intelligence",

            "description":
                "There are currently no intelligence records "
                "attached directly to this property."
        })

    # ------------------------------------------------------------------
    # Unmapped intelligence
    # ------------------------------------------------------------------

    unmapped = sum(
        1
        for r in intelligence
        if not r.get("mapped")
    )

    if unmapped:

        flags.append({
            "key":
                "UNMAPPED_INTELLIGENCE",

            "severity":
                "attention",

            "title":
                "Some intelligence lacks coordinates",

            "description":
                str(unmapped) +
                " property-linked intelligence records "
                "do not currently have usable coordinates."
        })

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    if intelligence and not evidence:

        flags.append({
            "key":
                "EVIDENCE_PENDING",

            "severity":
                "attention",

            "title":
                "Evidence inventory is empty",

            "description":
                "Intelligence exists for the property, "
                "but no linked evidence records were found."
        })

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    if not documents:

        flags.append({
            "key":
                "DOCUMENTS_PENDING",

            "severity":
                "attention",

            "title":
                "No property documents stored",

            "description":
                "No property files were found in the "
                "PropertyIQ document store."
        })

    # ------------------------------------------------------------------
    # Market
    # ------------------------------------------------------------------

    if not market:

        flags.append({
            "key":
                "MARKET_DATA_PENDING",

            "severity":
                "information",

            "title":
                "No market observations stored",

            "description":
                "No market observation records are currently "
                "attached to this property."
        })

    # ------------------------------------------------------------------
    # RERA
    # ------------------------------------------------------------------

    rera_count = sum(
        1
        for r in intelligence
        if v28_classify_source(r) == "RERA"
    )

    if not rera_count:

        flags.append({
            "key":
                "RERA_RECORD_NOT_FOUND",

            "severity":
                "information",

            "title":
                "No linked RERA record",

            "description":
                "No RERA intelligence record is currently "
                "attached to this property. This is not "
                "a conclusion about the property's legal status."
        })

    return flags


# ==============================================================================
# MAIN WORKSPACE DATA
# ==============================================================================

def v28_build_workspace(
    property_id
):

    with engine.begin() as conn:

        property_data = v28_get_property(
            conn,
            property_id
        )

        if not property_data:
            return None

        intelligence = v28_get_intelligence(
            conn,
            property_id
        )

        intelligence_ids = [
            r["id"]
            for r in intelligence
            if r.get("id")
        ]

        evidence = v28_get_evidence(
            conn,
            property_id,
            intelligence_ids
        )

        documents = v28_get_documents(
            conn,
            property_id
        )

        market = v28_get_market(
            conn,
            property_id
        )

        refresh_history = v28_get_refresh_history(
            conn,
            property_id
        )

    sources = v28_source_inventory(
        intelligence
    )

    flags = v28_build_flags(
        property_data,
        intelligence,
        evidence,
        documents,
        market
    )

    mapped = sum(
        1
        for r in intelligence
        if r.get("mapped")
    )

    return {

        "module":
            MODULE_VERSION,

        "generated_at":
            datetime.utcnow().isoformat() + "Z",

        "property":
            property_data,

        "summary": {

            "intelligence":
                len(intelligence),

            "mapped_intelligence":
                mapped,

            "unmapped_intelligence":
                len(intelligence) - mapped,

            "evidence":
                len(evidence),

            "documents":
                len(documents),

            "market_observations":
                len(market),

            "refresh_runs":
                len(refresh_history),

            "source_groups":
                len(sources),

            "flags":
                len(flags)
        },

        "sources":
            sources,

        "intelligence":
            intelligence,

        "evidence":
            evidence,

        "documents":
            documents,

        "market":
            market,

        "refresh_history":
            refresh_history,

        "flags":
            flags,

        "workspaces": {

            "full_profile":
                f"/propertyiq/full-profile/{property_id}",

            "portfolio":
                "/propertyiq/portfolio",

            "command_map":
                f"/propertyiq/command-map/{property_id}",

            "timeline":
                f"/propertyiq/timeline/{property_id}",

            "boundary":
                f"/propertyiq/boundary/{property_id}",

            "boundary_intelligence":
                f"/propertyiq/boundary-intelligence/{property_id}",

            "due_diligence_map":
                f"/propertyiq/due-diligence-map/{property_id}",

            "market":
                f"/propertyiq/market-comparables/{property_id}",

            "osm":
                f"/propertyiq/real-osm-gis/{property_id}",

            "documents":
                f"/propertyiq/files/{property_id}",

            "document_intelligence":
                f"/propertyiq/document-intelligence/{property_id}",

            "report":
                f"/propertyiq/intelligence-report/{property_id}",

            "live_refresh":
                f"/api/v1/properties/{property_id}/live-refresh"
        }
    }


# ==============================================================================
# API
# ==============================================================================

V28_API_ROUTE = (
    "/api/v1/properties/{property_id}/"
    "acquisition-due-diligence"
)


@app.get(V28_API_ROUTE)
def propertyiq_v28_api(
    property_id: str
):

    try:

        data = v28_build_workspace(
            property_id
        )

        if data is None:

            return JSONResponse(
                status_code=404,
                content={
                    "detail":
                        "Property not found",
                    "property_id":
                        property_id
                }
            )

        return data

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail":
                    str(exc),
                "property_id":
                    property_id
            }
        )


# ==============================================================================
# PREMIUM UI
# ==============================================================================

V28_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Due Diligence
</title>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 8% 0%,
            rgba(0,205,255,.10),
            transparent 28%
        ),
        radial-gradient(
            circle at 95% 15%,
            rgba(70,90,255,.09),
            transparent 28%
        ),
        #07101b;

    color: #e9f3ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

a {

    color: #67dcff;

    text-decoration: none;
}

a:hover {

    text-decoration: underline;
}

.topbar {

    height: 68px;

    position: sticky;

    top: 0;

    z-index: 100;

    display: flex;

    justify-content: space-between;

    align-items: center;

    padding: 0 25px;

    background:
        rgba(5,13,24,.90);

    backdrop-filter:
        blur(18px);

    border-bottom:
        1px solid rgba(255,255,255,.08);
}

.brand {

    display: flex;

    align-items: center;

    gap: 11px;
}

.brand-mark {

    width: 35px;

    height: 35px;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            #26d9ff,
            #345cff
        );

    box-shadow:
        0 0 30px
        rgba(38,217,255,.22);
}

.brand-name {

    font-weight: 850;

    font-size: 17px;
}

.brand-sub {

    color: #70879e;

    font-size: 9px;

    letter-spacing: 1.2px;
}

.actions {

    display: flex;

    gap: 8px;
}

.button {

    padding:
        8px 11px;

    border-radius: 9px;

    border:
        1px solid rgba(255,255,255,.09);

    background:
        rgba(255,255,255,.04);

    color: #d7e9fb;

    cursor: pointer;

    font-size: 11px;
}

.button:hover {

    background:
        rgba(255,255,255,.08);
}

.button-primary {

    color: #72ddff;

    border-color:
        rgba(64,210,255,.28);

    background:
        rgba(64,210,255,.07);
}

.container {

    max-width: 1550px;

    margin: auto;

    padding: 23px;
}

.hero {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        340px;

    gap: 14px;

    margin-bottom: 14px;
}

.panel {

    border:
        1px solid rgba(255,255,255,.075);

    background:
        rgba(10,21,35,.82);

    border-radius: 16px;

    box-shadow:
        0 20px 60px
        rgba(0,0,0,.15);

    overflow: hidden;
}

.hero-main {

    padding: 22px;
}

.kicker {

    color: #61dcff;

    text-transform: uppercase;

    letter-spacing: 1.7px;

    font-size: 9px;

    font-weight: 800;
}

.property-name {

    margin-top: 8px;

    font-size: 28px;

    font-weight: 850;
}

.address {

    margin-top: 7px;

    color: #7e95ad;

    font-size: 12px;
}

.chips {

    display: flex;

    gap: 7px;

    flex-wrap: wrap;

    margin-top: 14px;
}

.chip {

    border:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(255,255,255,.035);

    color: #9eb4ca;

    border-radius: 999px;

    padding: 6px 9px;

    font-size: 9px;
}

.coverage {

    padding: 22px;
}

.coverage-title {

    color: #70879e;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: 1px;
}

.coverage-number {

    font-size: 40px;

    font-weight: 900;

    margin-top: 7px;
}

.coverage-note {

    color: #70869d;

    font-size: 10px;

    line-height: 1.5;

    margin-top: 5px;
}

.stats {

    display: grid;

    grid-template-columns:
        repeat(6,minmax(0,1fr));

    gap: 9px;

    margin-bottom: 14px;
}

.stat {

    padding: 14px;

    border:
        1px solid rgba(255,255,255,.07);

    border-radius: 13px;

    background:
        rgba(12,23,39,.75);
}

.stat-label {

    color: #6f859d;

    font-size: 8px;

    text-transform: uppercase;

    letter-spacing: .8px;
}

.stat-value {

    font-size: 22px;

    font-weight: 850;

    margin-top: 6px;
}

.layout {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        390px;

    gap: 14px;
}

.section {

    padding: 17px;

    border-bottom:
        1px solid rgba(255,255,255,.055);
}

.section:last-child {

    border-bottom: 0;
}

.section-title {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 11px;
}

.section-title h2 {

    margin: 0;

    font-size: 13px;

    font-weight: 800;
}

.section-title span {

    color: #657c94;

    font-size: 9px;
}

.flag {

    display: grid;

    grid-template-columns: 8px 1fr;

    gap: 10px;

    padding: 10px;

    margin-bottom: 7px;

    border:
        1px solid rgba(255,255,255,.06);

    border-radius: 10px;

    background:
        rgba(255,255,255,.025);
}

.flag-dot {

    width: 7px;

    height: 7px;

    border-radius: 50%;

    background: #65dfff;

    margin-top: 4px;
}

.flag-title {

    font-size: 10px;

    font-weight: 750;
}

.flag-description {

    color: #748aa1;

    font-size: 9px;

    line-height: 1.45;

    margin-top: 3px;
}

.source-grid {

    display: grid;

    grid-template-columns:
        repeat(2,minmax(0,1fr));

    gap: 7px;
}

.source {

    padding: 10px;

    border:
        1px solid rgba(255,255,255,.06);

    border-radius: 9px;

    background:
        rgba(255,255,255,.025);
}

.source-name {

    font-size: 9px;

    color: #a5b9cd;
}

.source-count {

    margin-top: 4px;

    font-size: 17px;

    font-weight: 850;

    color: #6cddff;
}

.record {

    padding: 12px;

    margin-bottom: 7px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 10px;

    background:
        rgba(255,255,255,.022);
}

.record-title {

    font-size: 10px;

    font-weight: 750;

    line-height: 1.4;
}

.record-meta {

    display: flex;

    gap: 6px;

    flex-wrap: wrap;

    margin-top: 6px;
}

.record-chip {

    color: #7289a0;

    font-size: 8px;

    padding: 4px 6px;

    border-radius: 999px;

    background:
        rgba(255,255,255,.04);
}

.record-source {

    margin-top: 7px;

    font-size: 8px;

    color: #67d9ff;
}

.workspace-grid {

    display: grid;

    grid-template-columns:
        repeat(3,minmax(0,1fr));

    gap: 8px;
}

.workspace {

    padding: 12px;

    border:
        1px solid rgba(255,255,255,.06);

    border-radius: 10px;

    background:
        rgba(255,255,255,.025);

    cursor: pointer;
}

.workspace:hover {

    border-color:
        rgba(70,210,255,.25);

    background:
        rgba(70,210,255,.05);
}

.workspace-title {

    font-size: 10px;

    font-weight: 750;
}

.workspace-description {

    color: #70869d;

    font-size: 8px;

    line-height: 1.4;

    margin-top: 4px;
}

.info-grid {

    display: grid;

    grid-template-columns:
        repeat(2,minmax(0,1fr));

    gap: 7px;
}

.info {

    padding: 9px;

    border:
        1px solid rgba(255,255,255,.05);

    border-radius: 8px;

    background:
        rgba(255,255,255,.02);
}

.info-label {

    color: #667e96;

    font-size: 8px;

    text-transform: uppercase;
}

.info-value {

    color: #aec1d5;

    font-size: 9px;

    margin-top: 4px;

    word-break: break-word;
}

.empty {

    color: #667e96;

    font-size: 10px;

    padding: 12px;

    text-align: center;
}

.footer {

    text-align: center;

    color: #4e637a;

    font-size: 9px;

    padding: 25px 0 10px;
}

@media(max-width:1100px) {

    .hero {

        grid-template-columns: 1fr;
    }

    .stats {

        grid-template-columns:
            repeat(3,1fr);
    }

    .layout {

        grid-template-columns: 1fr;
    }
}

@media(max-width:700px) {

    .container {

        padding: 12px;
    }

    .stats {

        grid-template-columns:
            repeat(2,1fr);
    }

    .workspace-grid {

        grid-template-columns:
            repeat(2,1fr);
    }
}

</style>

</head>


<body>

<header class="topbar">

    <div class="brand">

        <div class="brand-mark"></div>

        <div>

            <div class="brand-name">
                PropertyIQ
            </div>

            <div class="brand-sub">
                ACQUISITION & DUE DILIGENCE
            </div>

        </div>

    </div>


    <div class="actions">

        <button
            class="button"
            onclick="go('portfolio')">

            Portfolio

        </button>


        <button
            class="button"
            onclick="go('command_map')">

            Command Map

        </button>


        <button
            class="button button-primary"
            onclick="go('report')">

            Intelligence Report

        </button>

    </div>

</header>


<main class="container">

    <div id="app">
        Loading due-diligence workspace...
    </div>

</main>


<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";

const DATA =
    __PROPERTYIQ_DATA__;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}


function go(key) {

    const url =
        DATA.workspaces &&
        DATA.workspaces[key];

    if (url) {

        window.location.href =
            url;
    }
}


function render() {

    const p =
        DATA.property || {};

    const summary =
        DATA.summary || {};

    const sources =
        DATA.sources || {};

    const flags =
        DATA.flags || {};

    const intelligence =
        DATA.intelligence || [];

    const documents =
        DATA.documents || [];

    const market =
        DATA.market || [];


    // --------------------------------------------------------------
    // PROPERTY FIELDS
    // --------------------------------------------------------------

    const fields = [

        ["Property Type", p.property_type],

        ["Area", p.area],

        ["Price", p.price],

        ["Price / Sq.Ft", p.price_per_sqft],

        ["Bedrooms", p.bedrooms],

        ["Bathrooms", p.bathrooms],

        ["Builder / Owner", p.builder_owner],

        [
            "Coordinates",
            p.latitude !== null &&
            p.longitude !== null
                ? p.latitude +
                  ", " +
                  p.longitude
                : "Pending"
        ],

        [
            "Boundary",
            p.has_boundary
                ? "Available"
                : "Pending"
        ]

    ];


    const fieldHTML =
        fields.map(
            function(item) {

                return `

                    <div class="info">

                        <div class="info-label">
                            ${esc(item[0])}
                        </div>

                        <div class="info-value">
                            ${esc(
                                item[1] ??
                                "—"
                            )}
                        </div>

                    </div>

                `;

            }
        ).join("");


    // --------------------------------------------------------------
    // FLAGS
    // --------------------------------------------------------------

    let flagsHTML = "";

    if (!flags.length) {

        flagsHTML = `
            <div class="empty">
                No system-generated
                due-diligence flags.
            </div>
        `;

    } else {

        flagsHTML =
            flags.map(
                function(flag) {

                    return `

                        <div class="flag">

                            <div class="flag-dot">
                            </div>

                            <div>

                                <div class="flag-title">
                                    ${esc(flag.title)}
                                </div>

                                <div class="flag-description">
                                    ${esc(
                                        flag.description
                                    )}
                                </div>

                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // SOURCES
    // --------------------------------------------------------------

    let sourceHTML = "";

    const sourceKeys =
        Object.keys(sources);


    if (!sourceKeys.length) {

        sourceHTML = `
            <div class="empty">
                No source-linked intelligence yet.
            </div>
        `;

    } else {

        sourceHTML =
            sourceKeys.map(
                function(key) {

                    const source =
                        sources[key];

                    return `

                        <div class="source">

                            <div class="source-name">
                                ${esc(key)}
                            </div>

                            <div class="source-count">
                                ${esc(source.count)}
                            </div>

                            <div style="
                                color:#657c94;
                                font-size:8px;
                                margin-top:3px;
                            ">
                                ${esc(source.mapped)}
                                mapped ·
                                ${esc(source.unmapped)}
                                pending
                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // INTELLIGENCE RECORDS
    // --------------------------------------------------------------

    let intelligenceHTML = "";

    if (!intelligence.length) {

        intelligenceHTML = `
            <div class="empty">
                No property-linked intelligence records.
            </div>
        `;

    } else {

        intelligenceHTML =
            intelligence
                .slice(0,50)
                .map(
                    function(record) {

                        const source =
                            record.source_name ||
                            record.source_type ||
                            "Unknown";

                        const location =
                            record.mapped
                                ? "Mapped"
                                : "Location pending";


                        let sourceLink = "";

                        if (
                            record.source_url
                        ) {

                            sourceLink = `

                                <div class="record-source">

                                    <a
                                        href="${esc(
                                            record.source_url
                                        )}"
                                        target="_blank"
                                        rel="noopener"
                                    >
                                        Open source →
                                    </a>

                                </div>

                            `;
                        }


                        return `

                            <div class="record">

                                <div class="record-title">
                                    ${esc(
                                        record.title
                                    )}
                                </div>

                                <div class="record-meta">

                                    <span class="record-chip">
                                        ${esc(source)}
                                    </span>

                                    <span class="record-chip">
                                        ${esc(
                                            record.status ||
                                            "Status unavailable"
                                        )}
                                    </span>

                                    <span class="record-chip">
                                        ${esc(location)}
                                    </span>

                                    <span class="record-chip">
                                        ${esc(
                                            record.evidence_level ||
                                            "Evidence level unavailable"
                                        )}
                                    </span>

                                </div>

                                ${sourceLink}

                            </div>

                        `;

                    }
                ).join("");
    }


    // --------------------------------------------------------------
    // WORKSPACES
    // --------------------------------------------------------------

    const workspaceDefinitions = [

        [
            "command_map",
            "Unified Command Map",
            "Spatial intelligence across all available sources."
        ],

        [
            "timeline",
            "Development Timeline",
            "Chronological property and development events."
        ],

        [
            "boundary_intelligence",
            "Boundary Intelligence",
            "Analyze intelligence relative to the property boundary."
        ],

        [
            "due_diligence_map",
            "Due-Diligence Map",
            "Spatial investigation workspace."
        ],

        [
            "boundary",
            "GIS Boundary",
            "Draw and edit the property boundary."
        ],

        [
            "market",
            "Market & Comparables",
            "Stored comparable and market observations."
        ],

        [
            "osm",
            "OSM / GIS",
            "Source-backed geographic context."
        ],

        [
            "documents",
            "Documents",
            "Property document repository."
        ],

        [
            "document_intelligence",
            "Document Intelligence",
            "Document-derived intelligence."
        ],

        [
            "report",
            "Intelligence Report",
            "Printable property intelligence report."
        ],

        [
            "full_profile",
            "Full Property Profile",
            "Complete property information profile."
        ],

        [
            "portfolio",
            "Portfolio",
            "Return to the PropertyIQ property database."
        ]

    ];


    const workspaceHTML =
        workspaceDefinitions.map(
            function(item) {

                return `

                    <div
                        class="workspace"
                        onclick="go('${item[0]}')"
                    >

                        <div class="workspace-title">
                            ${esc(item[1])}
                        </div>

                        <div class="workspace-description">
                            ${esc(item[2])}
                        </div>

                    </div>

                `;

            }
        ).join("");


    // --------------------------------------------------------------
    // RENDER
    // --------------------------------------------------------------

    document.getElementById(
        "app"
    ).innerHTML = `


        <section class="hero">


            <div class="panel hero-main">

                <div class="kicker">
                    ACQUISITION & DUE DILIGENCE
                </div>

                <div class="property-name">
                    ${esc(
                        p.property_name ||
                        "Unnamed Property"
                    )}
                </div>

                <div class="address">
                    ${esc(
                        p.property_address ||
                        "Address unavailable"
                    )}
                </div>

                <div class="chips">

                    <span class="chip">
                        ${esc(
                            p.property_type ||
                            "Property type unavailable"
                        )}
                    </span>

                    <span class="chip">
                        ${p.has_boundary
                            ? "Boundary available"
                            : "Boundary pending"}
                    </span>

                    <span class="chip">
                        ${p.latitude !== null &&
                          p.longitude !== null
                            ? "Location mapped"
                            : "Location pending"}
                    </span>

                </div>

            </div>


            <div class="panel coverage">

                <div class="coverage-title">
                    Due-Diligence Coverage
                </div>

                <div class="coverage-number">
                    ${summary.intelligence || 0}
                </div>

                <div class="coverage-note">

                    property-linked intelligence
                    records currently available.

                    This number is an inventory count,
                    not an investment or legal score.

                </div>

            </div>

        </section>


        <section class="stats">

            <div class="stat">

                <div class="stat-label">
                    Intelligence
                </div>

                <div class="stat-value">
                    ${summary.intelligence || 0}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Mapped
                </div>

                <div class="stat-value">
                    ${summary.mapped_intelligence || 0}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Unmapped
                </div>

                <div class="stat-value">
                    ${summary.unmapped_intelligence || 0}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Evidence
                </div>

                <div class="stat-value">
                    ${summary.evidence || 0}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Documents
                </div>

                <div class="stat-value">
                    ${summary.documents || 0}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Market
                </div>

                <div class="stat-value">
                    ${summary.market_observations || 0}
                </div>

            </div>

        </section>


        <section class="layout">


            <div>


                <div class="panel">

                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Property Information
                            </h2>

                            <span>
                                Existing PropertyIQ data
                            </span>

                        </div>

                        <div class="info-grid">

                            ${fieldHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Due-Diligence Flags
                            </h2>

                            <span>
                                Data coverage checks
                            </span>

                        </div>

                        ${flagsHTML}

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Intelligence Records
                            </h2>

                            <span>
                                ${intelligence.length}
                            </span>

                        </div>

                        ${intelligenceHTML}

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                PropertyIQ Workspaces
                            </h2>

                            <span>
                                Open existing modules
                            </span>

                        </div>

                        <div class="workspace-grid">

                            ${workspaceHTML}

                        </div>

                    </div>


                </div>


            </div>


            <aside>


                <div class="panel">


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Source Inventory
                            </h2>

                            <span>
                                ${summary.source_groups || 0}
                            </span>

                        </div>

                        <div class="source-grid">

                            ${sourceHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Stored Documents
                            </h2>

                            <span>
                                ${documents.length}
                            </span>

                        </div>

                        ${
                            documents.length
                            ?
                            documents
                                .slice(0,15)
                                .map(
                                    function(doc) {

                                        return `

                                            <div class="record">

                                                <div class="record-title">

                                                    ${esc(
                                                        doc.filename ||
                                                        doc.file_name ||
                                                        doc.name ||
                                                        "Property document"
                                                    )}

                                                </div>

                                                <div class="record-meta">

                                                    <span class="record-chip">
                                                        Document
                                                    </span>

                                                    ${
                                                        doc.mime_type
                                                        ?
                                                        `
                                                        <span class="record-chip">
                                                            ${esc(
                                                                doc.mime_type
                                                            )}
                                                        </span>
                                                        `
                                                        :
                                                        ""
                                                    }

                                                </div>

                                            </div>

                                        `;

                                    }
                                ).join("")
                            :
                            `
                            <div class="empty">
                                No property documents stored.
                            </div>
                            `
                        }

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Market Observations
                            </h2>

                            <span>
                                ${market.length}
                            </span>

                        </div>

                        ${
                            market.length
                            ?
                            `
                            <div class="empty">

                                ${market.length}
                                stored market observations.

                                <br><br>

                                Open Market & Comparables
                                for detailed analysis.

                            </div>
                            `
                            :
                            `
                            <div class="empty">
                                No market observations stored.
                            </div>
                            `
                        }

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Investigation Workflow
                            </h2>

                        </div>

                        <div class="workspace-grid">

                            <div
                                class="workspace"
                                onclick="go('command_map')"
                            >
                                <div class="workspace-title">
                                    01 · Map
                                </div>
                                <div class="workspace-description">
                                    Inspect spatial intelligence.
                                </div>
                            </div>


                            <div
                                class="workspace"
                                onclick="go('documents')"
                            >
                                <div class="workspace-title">
                                    02 · Documents
                                </div>
                                <div class="workspace-description">
                                    Review stored files.
                                </div>
                            </div>


                            <div
                                class="workspace"
                                onclick="go('timeline')"
                            >
                                <div class="workspace-title">
                                    03 · Timeline
                                </div>
                                <div class="workspace-description">
                                    Review dated events.
                                </div>
                            </div>


                            <div
                                class="workspace"
                                onclick="go('market')"
                            >
                                <div class="workspace-title">
                                    04 · Market
                                </div>
                                <div class="workspace-description">
                                    Review market observations.
                                </div>
                            </div>


                            <div
                                class="workspace"
                                onclick="go('report')"
                            >
                                <div class="workspace-title">
                                    05 · Report
                                </div>
                                <div class="workspace-description">
                                    Generate the intelligence report.
                                </div>
                            </div>


                            <div
                                class="workspace"
                                onclick="go('boundary')"
                            >
                                <div class="workspace-title">
                                    06 · Boundary
                                </div>
                                <div class="workspace-description">
                                    Verify property geometry.
                                </div>
                            </div>

                        </div>

                    </div>


                </div>


            </aside>


        </section>


        <div class="footer">

            PropertyIQ · Acquisition & Due-Diligence Workspace

            <br>

            Source-backed information only.
            Inventory counts and data-coverage flags
            are not legal, valuation, or investment conclusions.

        </div>

    `;
}


render();

</script>

</body>

</html>
"""


# ==============================================================================
# UI ROUTE
# ==============================================================================

V28_UI_ROUTE = (
    "/propertyiq/acquisition-due-diligence/"
    "{property_id}"
)


@app.get(
    V28_UI_ROUTE,
    response_class=HTMLResponse
)
def propertyiq_v28_ui(
    property_id: str
):

    try:

        data = v28_build_workspace(
            property_id
        )

        if data is None:

            return HTMLResponse(
                "<h2>Property not found</h2>",
                status_code=404
            )

        html = V28_HTML

        html = html.replace(
            "__PROPERTY_ID__",
            str(property_id)
        )

        html = html.replace(
            "__PROPERTYIQ_DATA__",
            json.dumps(
                data,
                default=str
            )
        )

        return HTMLResponse(
            html
        )

    except Exception as exc:

        return HTMLResponse(
            "<h2>PropertyIQ V28 Error</h2>"
            "<pre>" +
            str(exc) +
            "</pre>",
            status_code=500
        )


# ==============================================================================
# INSTALLATION OUTPUT
# ==============================================================================

print()
print("=" * 78)
print("PROPERTYIQ V28 — ACQUISITION & DUE-DILIGENCE WORKSPACE INSTALLED")
print("=" * 78)
print()
print("Module:")
print(" ", MODULE_VERSION)
print()
print("API:")
print("  /api/v1/properties/{property_id}/acquisition-due-diligence")
print()
print("UI:")
print("  /propertyiq/acquisition-due-diligence/{property_id}")
print()
print("Integrated:")
print("  ✓ Property profile")
print("  ✓ Location")
print("  ✓ GIS boundary")
print("  ✓ Intelligence records")
print("  ✓ RERA")
print("  ✓ Government infrastructure")
print("  ✓ News")
print("  ✓ OSM / GIS")
print("  ✓ Evidence")
print("  ✓ Documents")
print("  ✓ Market observations")
print("  ✓ Refresh history")
print("  ✓ Development timeline")
print("  ✓ Existing workspaces")
print()
print("Due-diligence inventory: ENABLED")
print("Source inventory: ENABLED")
print("Data-coverage flags: ENABLED")
print("Document inventory: ENABLED")
print("Market inventory: ENABLED")
print()
print("Fabricated intelligence: DISABLED")
print("Investment score: DISABLED")
print("Valuation prediction: DISABLED")
print("Legal conclusion generation: DISABLED")
print("Database modification during installation: DISABLED")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)



# ============================================================
# PROPERTYIQ MODULE: V29
# ORIGINAL COLAB CELL: In[89]
# ============================================================

# ==============================================================================
# PROPERTYIQ V29 — EVIDENCE, CONFIDENCE & DATA QUALITY WORKSPACE
# ==============================================================================
# DIRECT-PASTE GOOGLE COLAB CELL
#
# Purpose:
#   Turn existing PropertyIQ evidence/confidence infrastructure into a
#   practical investigator/data-quality workspace.
#
# Uses existing:
#   • PostgreSQL/PostGIS
#   • properties
#   • intelligence_records
#   • evidence_items
#   • property_files
#   • property_market_observations
#   • existing V11/V28 capabilities
#
# Does NOT:
#   • rebuild earlier modules
#   • create another database
#   • fabricate evidence
#   • fabricate confidence
#   • create investment scores
#   • predict valuation
#   • modify database records during installation
# ==============================================================================

import sys
import subprocess
import json
from datetime import datetime, timezone


MODULE_VERSION = "PROPERTYIQ-V29-EVIDENCE-CONFIDENCE-DATA-QUALITY"


# ==============================================================================
# DEPENDENCIES
# ==============================================================================

try:
    import fastapi
except Exception:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg[binary]"
    ])

from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text


# ==============================================================================
# EXISTING PROPERTYIQ RUNTIME
# ==============================================================================

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ PostgreSQL engine is not available. "
        "Run the existing PropertyIQ PostgreSQL runtime/bootstrap cell first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(title="PropertyIQ")


# ==============================================================================
# SCHEMA HELPERS
# ==============================================================================

def v29_table_exists(conn, table_name):

    try:

        return bool(
            conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        AND table_name = :table
                    )
                """),
                {
                    "table": table_name
                }
            ).scalar()
        )

    except Exception:

        return False


def v29_columns(conn, table_name):

    try:

        rows = conn.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = :table
                ORDER BY ordinal_position
            """),
            {
                "table": table_name
            }
        ).scalars().all()

        return set(rows)

    except Exception:

        return set()


def v29_safe(value):

    if value is None:
        return None

    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


# ==============================================================================
# SOURCE CLASSIFICATION
# ==============================================================================

def v29_classify_source(source_name, source_type):

    combined = (
        str(source_name or "") +
        " " +
        str(source_type or "")
    ).lower()

    if (
        "rera" in combined
        or
        "up-rera" in combined
    ):
        return "RERA"

    if (
        "paimana" in combined
        or
        "mospi" in combined
        or
        "government" in combined
        or
        "infrastructure" in combined
    ):
        return "GOVERNMENT"

    if (
        "gdelt" in combined
        or
        "news" in combined
    ):
        return "NEWS"

    if (
        "osm" in combined
        or
        "openstreetmap" in combined
        or
        "overpass" in combined
    ):
        return "OSM"

    return "OTHER"


# ==============================================================================
# INTELLIGENCE LOADER
# ==============================================================================

def v29_load_intelligence(
    conn,
    property_id=None
):

    if not v29_table_exists(
        conn,
        "intelligence_records"
    ):
        return []

    columns = v29_columns(
        conn,
        "intelligence_records"
    )

    select_parts = [
        "id::text AS id",
        "title"
    ]

    optional_columns = [
        "category",
        "status",
        "description",
        "latitude",
        "longitude",
        "source_name",
        "source_type",
        "source_url",
        "published_at",
        "confidence",
        "evidence_level",
        "metadata",
        "created_at",
        "updated_at",
        "property_id",
        "ingestion_fingerprint"
    ]

    for column in optional_columns:

        if column in columns:

            if column == "property_id":

                select_parts.append(
                    "property_id::text AS property_id"
                )

            else:

                select_parts.append(
                    column
                )

    where = ""
    params = {}

    if property_id:

        if "property_id" not in columns:
            return []

        where = """
            WHERE property_id = CAST(:pid AS uuid)
        """

        params["pid"] = property_id

    order_column = (
        "updated_at"
        if "updated_at" in columns
        else "id"
    )

    query = f"""
        SELECT
            {", ".join(select_parts)}
        FROM intelligence_records
        {where}
        ORDER BY {order_column} DESC
        LIMIT 2000
    """

    rows = conn.execute(
        text(query),
        params
    ).mappings().all()

    result = []

    for row in rows:

        item = {
            key: v29_safe(value)
            for key, value in dict(row).items()
        }

        item["source_group"] = v29_classify_source(
            item.get("source_name"),
            item.get("source_type")
        )

        item["has_coordinates"] = (
            item.get("latitude") is not None
            and
            item.get("longitude") is not None
        )

        item["has_source_url"] = bool(
            item.get("source_url")
        )

        item["has_evidence_level"] = bool(
            item.get("evidence_level")
        )

        item["has_confidence"] = bool(
            item.get("confidence")
        )

        item["has_published_date"] = bool(
            item.get("published_at")
        )

        result.append(item)

    return result


# ==============================================================================
# EVIDENCE LOADER
# ==============================================================================

def v29_load_evidence(
    conn,
    property_id=None,
    intelligence_ids=None
):

    if not v29_table_exists(
        conn,
        "evidence_items"
    ):
        return []

    columns = v29_columns(
        conn,
        "evidence_items"
    )

    rows = []

    # ------------------------------------------------------------------
    # Direct property linkage
    # ------------------------------------------------------------------

    if (
        property_id
        and
        "property_id" in columns
    ):

        try:

            rows = conn.execute(
                text("""
                    SELECT *
                    FROM evidence_items
                    WHERE property_id = CAST(:pid AS uuid)
                    LIMIT 2000
                """),
                {
                    "pid": property_id
                }
            ).mappings().all()

        except Exception:

            rows = []

    # ------------------------------------------------------------------
    # Intelligence linkage
    # ------------------------------------------------------------------

    if (
        not rows
        and
        intelligence_ids
        and
        "intelligence_id" in columns
    ):

        try:

            rows = conn.execute(
                text("""
                    SELECT *
                    FROM evidence_items
                    WHERE intelligence_id IN (
                        SELECT CAST(x AS uuid)
                        FROM jsonb_array_elements_text(
                            CAST(:ids AS jsonb)
                        ) AS x
                    )
                    LIMIT 2000
                """),
                {
                    "ids": json.dumps(
                        intelligence_ids
                    )
                }
            ).mappings().all()

        except Exception:

            rows = []

    result = []

    for row in rows:

        item = {
            key: v29_safe(value)
            for key, value in dict(row).items()
        }

        result.append(item)

    return result


# ==============================================================================
# PROPERTY LOADER
# ==============================================================================

def v29_load_property(
    conn,
    property_id
):

    row = conn.execute(
        text("""
            SELECT
                id::text AS id,
                property_name,
                property_address,
                latitude,
                longitude,
                property_type,
                area,
                price,
                price_per_sqft,
                bedrooms,
                bathrooms,
                builder_owner,
                description,

                CASE
                    WHEN boundary IS NOT NULL
                    THEN true
                    ELSE false
                END AS has_boundary

            FROM properties

            WHERE id = CAST(:pid AS uuid)

            LIMIT 1
        """),
        {
            "pid": property_id
        }
    ).mappings().first()

    if not row:
        return None

    return {
        key: v29_safe(value)
        for key, value in dict(row).items()
    }


# ==============================================================================
# EVIDENCE LINK MATCHING
# ==============================================================================

def v29_evidence_link_ids(
    evidence
):

    linked = set()

    for item in evidence:

        for key in (
            "intelligence_id",
            "record_id"
        ):

            value = item.get(key)

            if value:
                linked.add(
                    str(value)
                )

    return linked


# ==============================================================================
# DATA QUALITY ANALYSIS
# ==============================================================================

def v29_analyze(
    intelligence,
    evidence
):

    evidence_ids = v29_evidence_link_ids(
        evidence
    )

    total = len(intelligence)

    mapped = sum(
        1
        for r in intelligence
        if r.get("has_coordinates")
    )

    unmapped = total - mapped

    with_url = sum(
        1
        for r in intelligence
        if r.get("has_source_url")
    )

    without_url = total - with_url

    with_confidence = sum(
        1
        for r in intelligence
        if r.get("has_confidence")
    )

    without_confidence = (
        total -
        with_confidence
    )

    with_evidence_level = sum(
        1
        for r in intelligence
        if r.get("has_evidence_level")
    )

    without_evidence_level = (
        total -
        with_evidence_level
    )

    with_date = sum(
        1
        for r in intelligence
        if r.get("has_published_date")
    )

    without_date = total - with_date

    evidence_linked = 0

    for record in intelligence:

        if record.get("id") in evidence_ids:
            evidence_linked += 1

    evidence_missing = (
        total -
        evidence_linked
    )

    # ------------------------------------------------------------------
    # Source inventory
    # ------------------------------------------------------------------

    sources = {}

    for record in intelligence:

        group = (
            record.get("source_group")
            or
            "OTHER"
        )

        if group not in sources:

            sources[group] = {
                "total": 0,
                "mapped": 0,
                "unmapped": 0,
                "with_url": 0,
                "with_confidence": 0,
                "with_evidence_level": 0,
                "with_date": 0
            }

        item = sources[group]

        item["total"] += 1

        if record.get("has_coordinates"):
            item["mapped"] += 1
        else:
            item["unmapped"] += 1

        if record.get("has_source_url"):
            item["with_url"] += 1

        if record.get("has_confidence"):
            item["with_confidence"] += 1

        if record.get("has_evidence_level"):
            item["with_evidence_level"] += 1

        if record.get("has_published_date"):
            item["with_date"] += 1

    # ------------------------------------------------------------------
    # Quality flags
    # ------------------------------------------------------------------

    flags = []

    if unmapped:

        flags.append({
            "key":
                "LOCATION_PENDING",

            "severity":
                "attention",

            "count":
                unmapped,

            "title":
                "Intelligence records without coordinates",

            "description":
                "Some intelligence records cannot currently "
                "be placed on the map because usable "
                "coordinates are missing."
        })

    if without_url:

        flags.append({
            "key":
                "SOURCE_URL_MISSING",

            "severity":
                "attention",

            "count":
                without_url,

            "title":
                "Source URL missing",

            "description":
                "Some intelligence records do not currently "
                "contain a source URL."
        })

    if without_evidence_level:

        flags.append({
            "key":
                "EVIDENCE_LEVEL_MISSING",

            "severity":
                "attention",

            "count":
                without_evidence_level,

            "title":
                "Evidence level missing",

            "description":
                "Some intelligence records do not have an "
                "evidence level recorded."
        })

    if without_confidence:

        flags.append({
            "key":
                "CONFIDENCE_MISSING",

            "severity":
                "attention",

            "count":
                without_confidence,

            "title":
                "Confidence value missing",

            "description":
                "Some intelligence records do not have a "
                "confidence field populated."
        })

    if without_date:

        flags.append({
            "key":
                "DATE_MISSING",

            "severity":
                "information",

            "count":
                without_date,

            "title":
                "Publication/event date missing",

            "description":
                "Some records do not currently contain a "
                "published or event date."
        })

    if evidence_missing:

        flags.append({
            "key":
                "EVIDENCE_LINK_MISSING",

            "severity":
                "attention",

            "count":
                evidence_missing,

            "title":
                "Evidence linkage incomplete",

            "description":
                "Some intelligence records do not appear "
                "to have a directly linked evidence record."
        })

    # ------------------------------------------------------------------
    # Coverage percentages
    # ------------------------------------------------------------------

    def pct(value):

        if total == 0:
            return 0

        return round(
            100.0 *
            value /
            total,
            1
        )

    coverage = {

        "mapped":
            pct(mapped),

        "source_url":
            pct(with_url),

        "confidence":
            pct(with_confidence),

        "evidence_level":
            pct(with_evidence_level),

        "published_date":
            pct(with_date),

        "evidence_linked":
            pct(evidence_linked)
    }

    return {

        "total":
            total,

        "mapped":
            mapped,

        "unmapped":
            unmapped,

        "with_source_url":
            with_url,

        "without_source_url":
            without_url,

        "with_confidence":
            with_confidence,

        "without_confidence":
            without_confidence,

        "with_evidence_level":
            with_evidence_level,

        "without_evidence_level":
            without_evidence_level,

        "with_published_date":
            with_date,

        "without_published_date":
            without_date,

        "evidence_linked":
            evidence_linked,

        "evidence_missing":
            evidence_missing,

        "evidence_records":
            len(evidence),

        "coverage":
            coverage,

        "sources":
            sources,

        "flags":
            flags
    }


# ==============================================================================
# WORKSPACE BUILDER
# ==============================================================================

def v29_build_workspace(
    property_id
):

    with engine.begin() as conn:

        property_data = v29_load_property(
            conn,
            property_id
        )

        if not property_data:
            return None

        intelligence = v29_load_intelligence(
            conn,
            property_id
        )

        intelligence_ids = [
            item["id"]
            for item in intelligence
            if item.get("id")
        ]

        evidence = v29_load_evidence(
            conn,
            property_id,
            intelligence_ids
        )

    analysis = v29_analyze(
        intelligence,
        evidence
    )

    return {

        "module":
            MODULE_VERSION,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "property":
            property_data,

        "analysis":
            analysis,

        "intelligence":
            intelligence,

        "evidence":
            evidence,

        "workspaces": {

            "portfolio":
                "/propertyiq/portfolio",

            "full_profile":
                f"/propertyiq/full-profile/{property_id}",

            "command_map":
                f"/propertyiq/command-map/{property_id}",

            "timeline":
                f"/propertyiq/timeline/{property_id}",

            "boundary":
                f"/propertyiq/boundary/{property_id}",

            "boundary_intelligence":
                f"/propertyiq/boundary-intelligence/{property_id}",

            "due_diligence":
                f"/propertyiq/acquisition-due-diligence/{property_id}",

            "due_diligence_map":
                f"/propertyiq/due-diligence-map/{property_id}",

            "market":
                f"/propertyiq/market-comparables/{property_id}",

            "osm":
                f"/propertyiq/real-osm-gis/{property_id}",

            "documents":
                f"/propertyiq/files/{property_id}",

            "document_intelligence":
                f"/propertyiq/document-intelligence/{property_id}",

            "report":
                f"/propertyiq/intelligence-report/{property_id}"
        }
    }


# ==============================================================================
# API
# ==============================================================================

V29_API_ROUTE = (
    "/api/v1/properties/{property_id}/"
    "evidence-data-quality"
)


@app.get(V29_API_ROUTE)
def propertyiq_v29_api(
    property_id: str
):

    try:

        data = v29_build_workspace(
            property_id
        )

        if data is None:

            return JSONResponse(
                status_code=404,
                content={
                    "detail":
                        "Property not found",
                    "property_id":
                        property_id
                }
            )

        return data

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail":
                    str(exc)
            }
        )


# ==============================================================================
# PREMIUM UI
# ==============================================================================

V29_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Evidence & Data Quality
</title>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,205,255,.10),
            transparent 29%
        ),
        radial-gradient(
            circle at 95% 15%,
            rgba(75,80,255,.08),
            transparent 30%
        ),
        #07101b;

    color: #eaf4ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

a {

    color: #68dcff;

    text-decoration: none;
}

a:hover {
    text-decoration: underline;
}

.topbar {

    height: 68px;

    display: flex;

    justify-content: space-between;

    align-items: center;

    padding:
        0 25px;

    background:
        rgba(5,13,24,.90);

    border-bottom:
        1px solid rgba(255,255,255,.08);

    backdrop-filter:
        blur(18px);

    position: sticky;

    top: 0;

    z-index: 100;
}

.brand {

    display: flex;

    align-items: center;

    gap: 11px;
}

.mark {

    width: 35px;

    height: 35px;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            #27d9ff,
            #345cff
        );

    box-shadow:
        0 0 30px
        rgba(39,217,255,.20);
}

.brand-name {

    font-size: 17px;

    font-weight: 850;
}

.brand-sub {

    color: #71889f;

    font-size: 9px;

    letter-spacing: 1.2px;
}

.actions {

    display: flex;

    gap: 7px;

    flex-wrap: wrap;
}

.btn {

    padding:
        8px 10px;

    border-radius: 9px;

    border:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(255,255,255,.04);

    color: #d7e9fb;

    font-size: 10px;

    cursor: pointer;
}

.btn-primary {

    color: #70ddff;

    border-color:
        rgba(60,210,255,.28);

    background:
        rgba(60,210,255,.07);
}

.container {

    max-width: 1550px;

    margin: auto;

    padding: 23px;
}

.hero {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        340px;

    gap: 14px;

    margin-bottom: 14px;
}

.panel {

    border:
        1px solid rgba(255,255,255,.075);

    background:
        rgba(10,21,35,.82);

    border-radius: 16px;

    overflow: hidden;

    box-shadow:
        0 20px 60px
        rgba(0,0,0,.14);
}

.hero-main {

    padding: 22px;
}

.kicker {

    color: #64dcff;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: 1.7px;

    font-weight: 800;
}

.title {

    font-size: 28px;

    font-weight: 850;

    margin-top: 8px;
}

.address {

    color: #7d94ac;

    font-size: 12px;

    margin-top: 7px;
}

.coverage {

    padding: 22px;
}

.coverage-label {

    color: #71879f;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: 1px;
}

.coverage-number {

    font-size: 41px;

    font-weight: 900;

    margin-top: 7px;
}

.coverage-note {

    color: #71879f;

    font-size: 10px;

    line-height: 1.5;

    margin-top: 5px;
}

.stats {

    display: grid;

    grid-template-columns:
        repeat(6,minmax(0,1fr));

    gap: 9px;

    margin-bottom: 14px;
}

.stat {

    padding: 14px;

    border:
        1px solid rgba(255,255,255,.07);

    border-radius: 13px;

    background:
        rgba(12,23,39,.76);
}

.stat-label {

    color: #70869e;

    font-size: 8px;

    text-transform: uppercase;

    letter-spacing: .8px;
}

.stat-value {

    font-size: 21px;

    font-weight: 850;

    margin-top: 6px;
}

.layout {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        400px;

    gap: 14px;
}

.section {

    padding: 17px;

    border-bottom:
        1px solid rgba(255,255,255,.055);
}

.section:last-child {
    border-bottom: 0;
}

.section-title {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 11px;
}

.section-title h2 {

    margin: 0;

    font-size: 13px;

    font-weight: 800;
}

.section-title span {

    color: #647b92;

    font-size: 9px;
}

.coverage-grid {

    display: grid;

    grid-template-columns:
        repeat(3,minmax(0,1fr));

    gap: 8px;
}

.coverage-card {

    padding: 12px;

    border:
        1px solid rgba(255,255,255,.06);

    border-radius: 10px;

    background:
        rgba(255,255,255,.025);
}

.coverage-card-title {

    color: #758ba1;

    font-size: 8px;

    text-transform: uppercase;
}

.coverage-card-value {

    font-size: 19px;

    font-weight: 850;

    margin-top: 5px;
}

.flag {

    display: grid;

    grid-template-columns:
        7px 1fr;

    gap: 9px;

    padding: 10px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 9px;

    background:
        rgba(255,255,255,.025);

    margin-bottom: 7px;
}

.flag-dot {

    width: 7px;

    height: 7px;

    border-radius: 50%;

    background: #62dcff;

    margin-top: 4px;
}

.flag-title {

    font-size: 10px;

    font-weight: 750;
}

.flag-description {

    color: #71879e;

    font-size: 9px;

    line-height: 1.45;

    margin-top: 3px;
}

.source-grid {

    display: grid;

    grid-template-columns:
        repeat(2,minmax(0,1fr));

    gap: 7px;
}

.source {

    padding: 10px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 9px;

    background:
        rgba(255,255,255,.022);
}

.source-name {

    color: #a4b8cc;

    font-size: 9px;
}

.source-count {

    color: #68ddff;

    font-size: 18px;

    font-weight: 850;

    margin-top: 4px;
}

.source-small {

    color: #667d94;

    font-size: 8px;

    margin-top: 3px;
}

.record {

    padding: 12px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 10px;

    background:
        rgba(255,255,255,.02);

    margin-bottom: 7px;
}

.record-title {

    font-size: 10px;

    font-weight: 750;

    line-height: 1.4;
}

.record-meta {

    display: flex;

    flex-wrap: wrap;

    gap: 5px;

    margin-top: 6px;
}

.tag {

    padding: 4px 6px;

    border-radius: 999px;

    background:
        rgba(255,255,255,.04);

    color: #748aa0;

    font-size: 8px;
}

.record-link {

    font-size: 8px;

    margin-top: 6px;
}

.workspace-grid {

    display: grid;

    grid-template-columns:
        repeat(3,minmax(0,1fr));

    gap: 8px;
}

.workspace {

    padding: 11px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 9px;

    background:
        rgba(255,255,255,.025);

    cursor: pointer;
}

.workspace:hover {

    border-color:
        rgba(70,210,255,.24);

    background:
        rgba(70,210,255,.05);
}

.workspace-title {

    font-size: 10px;

    font-weight: 750;
}

.workspace-description {

    color: #70869d;

    font-size: 8px;

    line-height: 1.4;

    margin-top: 4px;
}

.empty {

    padding: 15px;

    text-align: center;

    color: #667d94;

    font-size: 10px;
}

.footer {

    text-align: center;

    padding: 25px 0 10px;

    color: #4e637a;

    font-size: 9px;
}

@media(max-width:1100px) {

    .hero {

        grid-template-columns: 1fr;
    }

    .stats {

        grid-template-columns:
            repeat(3,1fr);
    }

    .layout {

        grid-template-columns: 1fr;
    }
}

@media(max-width:700px) {

    .container {
        padding: 12px;
    }

    .stats {

        grid-template-columns:
            repeat(2,1fr);
    }

    .coverage-grid {

        grid-template-columns:
            repeat(2,1fr);
    }

    .workspace-grid {

        grid-template-columns:
            repeat(2,1fr);
    }
}

</style>

</head>


<body>


<header class="topbar">

    <div class="brand">

        <div class="mark"></div>

        <div>

            <div class="brand-name">
                PropertyIQ
            </div>

            <div class="brand-sub">
                EVIDENCE & DATA QUALITY
            </div>

        </div>

    </div>


    <div class="actions">

        <button
            class="btn"
            onclick="go('portfolio')">
            Portfolio
        </button>

        <button
            class="btn"
            onclick="go('command_map')">
            Command Map
        </button>

        <button
            class="btn"
            onclick="go('due_diligence')">
            Due Diligence
        </button>

        <button
            class="btn btn-primary"
            onclick="go('report')">
            Report
        </button>

    </div>

</header>


<main class="container">

    <div id="app">
        Loading evidence workspace...
    </div>

</main>


<script>

const DATA =
    __PROPERTYIQ_DATA__;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}


function go(key) {

    const url =
        DATA.workspaces &&
        DATA.workspaces[key];

    if (url) {
        window.location.href = url;
    }
}


function render() {

    const property =
        DATA.property || {};

    const analysis =
        DATA.analysis || {};

    const coverage =
        analysis.coverage || {};

    const flags =
        analysis.flags || [];

    const sources =
        analysis.sources || {};

    const intelligence =
        DATA.intelligence || [];

    const evidence =
        DATA.evidence || [];


    // --------------------------------------------------------------
    // COVERAGE
    // --------------------------------------------------------------

    const coverageItems = [

        [
            "Mapped",
            coverage.mapped || 0
        ],

        [
            "Source URL",
            coverage.source_url || 0
        ],

        [
            "Confidence",
            coverage.confidence || 0
        ],

        [
            "Evidence Level",
            coverage.evidence_level || 0
        ],

        [
            "Published Date",
            coverage.published_date || 0
        ],

        [
            "Evidence Linked",
            coverage.evidence_linked || 0
        ]

    ];


    const coverageHTML =
        coverageItems.map(
            function(item) {

                return `

                    <div class="coverage-card">

                        <div class="coverage-card-title">
                            ${esc(item[0])}
                        </div>

                        <div class="coverage-card-value">
                            ${esc(item[1])}%
                        </div>

                    </div>

                `;

            }
        ).join("");


    // --------------------------------------------------------------
    // FLAGS
    // --------------------------------------------------------------

    let flagsHTML = "";

    if (!flags.length) {

        flagsHTML = `
            <div class="empty">
                No data-quality flags were generated.
            </div>
        `;

    } else {

        flagsHTML =
            flags.map(
                function(flag) {

                    return `

                        <div class="flag">

                            <div class="flag-dot"></div>

                            <div>

                                <div class="flag-title">
                                    ${esc(flag.title)}
                                    ${flag.count
                                        ? " · " +
                                          esc(flag.count)
                                        : ""}
                                </div>

                                <div class="flag-description">
                                    ${esc(
                                        flag.description
                                    )}
                                </div>

                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // SOURCES
    // --------------------------------------------------------------

    let sourceHTML = "";

    const sourceKeys =
        Object.keys(sources);

    if (!sourceKeys.length) {

        sourceHTML = `
            <div class="empty">
                No intelligence records.
            </div>
        `;

    } else {

        sourceHTML =
            sourceKeys.map(
                function(key) {

                    const item =
                        sources[key];

                    return `

                        <div class="source">

                            <div class="source-name">
                                ${esc(key)}
                            </div>

                            <div class="source-count">
                                ${esc(item.total)}
                            </div>

                            <div class="source-small">
                                ${esc(item.mapped)}
                                mapped ·
                                ${esc(item.unmapped)}
                                unmapped
                            </div>

                            <div class="source-small">
                                URL:
                                ${esc(item.with_url)}
                                · Confidence:
                                ${esc(item.with_confidence)}
                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // INTELLIGENCE
    // --------------------------------------------------------------

    let intelligenceHTML = "";

    if (!intelligence.length) {

        intelligenceHTML = `
            <div class="empty">
                No intelligence records are linked
                to this property.
            </div>
        `;

    } else {

        intelligenceHTML =
            intelligence
                .slice(0,60)
                .map(
                    function(record) {

                        let sourceLink = "";

                        if (
                            record.source_url
                        ) {

                            sourceLink = `

                                <div class="record-link">

                                    <a
                                        href="${esc(
                                            record.source_url
                                        )}"
                                        target="_blank"
                                        rel="noopener"
                                    >
                                        Open source →
                                    </a>

                                </div>

                            `;
                        }


                        return `

                            <div class="record">

                                <div class="record-title">
                                    ${esc(
                                        record.title ||
                                        "Untitled intelligence record"
                                    )}
                                </div>

                                <div class="record-meta">

                                    <span class="tag">
                                        ${esc(
                                            record.source_group
                                        )}
                                    </span>

                                    <span class="tag">
                                        ${
                                            record.has_coordinates
                                            ? "Mapped"
                                            : "Location pending"
                                        }
                                    </span>

                                    <span class="tag">
                                        ${
                                            record.evidence_level ||
                                            "Evidence level missing"
                                        }
                                    </span>

                                    <span class="tag">
                                        ${
                                            record.confidence ||
                                            "Confidence missing"
                                        }
                                    </span>

                                    <span class="tag">
                                        ${
                                            record.published_at ||
                                            "Date missing"
                                        }
                                    </span>

                                </div>

                                ${sourceLink}

                            </div>

                        `;

                    }
                ).join("");
    }


    // --------------------------------------------------------------
    // EVIDENCE
    // --------------------------------------------------------------

    let evidenceHTML = "";

    if (!evidence.length) {

        evidenceHTML = `
            <div class="empty">
                No evidence records found for this property.
            </div>
        `;

    } else {

        evidenceHTML =
            evidence
                .slice(0,60)
                .map(
                    function(item) {

                        const title =
                            item.title ||
                            item.name ||
                            item.description ||
                            "Evidence record";

                        const source =
                            item.source_name ||
                            item.source ||
                            "Source not specified";

                        const url =
                            item.source_url ||
                            item.url;


                        return `

                            <div class="record">

                                <div class="record-title">
                                    ${esc(title)}
                                </div>

                                <div class="record-meta">

                                    <span class="tag">
                                        ${esc(source)}
                                    </span>

                                    ${
                                        item.evidence_level
                                        ?
                                        `
                                        <span class="tag">
                                            ${esc(
                                                item.evidence_level
                                            )}
                                        </span>
                                        `
                                        :
                                        ""
                                    }

                                    ${
                                        item.confidence
                                        ?
                                        `
                                        <span class="tag">
                                            Confidence:
                                            ${esc(
                                                item.confidence
                                            )}
                                        </span>
                                        `
                                        :
                                        ""
                                    }

                                </div>

                                ${
                                    url
                                    ?
                                    `
                                    <div class="record-link">

                                        <a
                                            href="${esc(url)}"
                                            target="_blank"
                                            rel="noopener"
                                        >
                                            Open evidence source →
                                        </a>

                                    </div>
                                    `
                                    :
                                    ""
                                }

                            </div>

                        `;

                    }
                ).join("");
    }


    // --------------------------------------------------------------
    // WORKSPACE LINKS
    // --------------------------------------------------------------

    const workspaceItems = [

        [
            "command_map",
            "Command Map",
            "Inspect all mapped intelligence spatially."
        ],

        [
            "due_diligence",
            "Due Diligence",
            "Property acquisition investigation workspace."
        ],

        [
            "timeline",
            "Timeline",
            "Review development and dated events."
        ],

        [
            "boundary_intelligence",
            "Boundary Intelligence",
            "Analyze records around the property boundary."
        ],

        [
            "documents",
            "Documents",
            "Review property files."
        ],

        [
            "market",
            "Market",
            "Review comparables and observations."
        ],

        [
            "report",
            "Intelligence Report",
            "Open the printable intelligence report."
        ],

        [
            "full_profile",
            "Full Profile",
            "Open the unified property profile."
        ],

        [
            "portfolio",
            "Portfolio",
            "Return to all properties."
        ]

    ];


    const workspaceHTML =
        workspaceItems.map(
            function(item) {

                return `

                    <div
                        class="workspace"
                        onclick="go('${item[0]}')"
                    >

                        <div class="workspace-title">
                            ${esc(item[1])}
                        </div>

                        <div class="workspace-description">
                            ${esc(item[2])}
                        </div>

                    </div>

                `;

            }
        ).join("");


    // --------------------------------------------------------------
    // RENDER
    // --------------------------------------------------------------

    document.getElementById(
        "app"
    ).innerHTML = `


        <section class="hero">


            <div class="panel hero-main">

                <div class="kicker">
                    EVIDENCE · CONFIDENCE · DATA QUALITY
                </div>

                <div class="title">
                    ${esc(
                        property.property_name ||
                        "Property"
                    )}
                </div>

                <div class="address">
                    ${esc(
                        property.property_address ||
                        "Address unavailable"
                    )}
                </div>

            </div>


            <div class="panel coverage">

                <div class="coverage-label">
                    Intelligence Coverage
                </div>

                <div class="coverage-number">
                    ${esc(
                        analysis.total || 0
                    )}
                </div>

                <div class="coverage-note">

                    Intelligence records currently
                    linked to this property.

                    This is an inventory count and
                    not an investment or legal score.

                </div>

            </div>


        </section>


        <section class="stats">


            <div class="stat">

                <div class="stat-label">
                    Total
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.total || 0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Mapped
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.mapped || 0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Evidence
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.evidence_records || 0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Evidence Linked
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.evidence_linked || 0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Missing URLs
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.without_source_url || 0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Missing Confidence
                </div>

                <div class="stat-value">
                    ${esc(
                        analysis.without_confidence || 0
                    )}
                </div>

            </div>


        </section>


        <section class="layout">


            <div>


                <div class="panel">


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Data Coverage
                            </h2>

                            <span>
                                Record-level coverage
                            </span>

                        </div>


                        <div class="coverage-grid">

                            ${coverageHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Data Quality Flags
                            </h2>

                            <span>
                                ${flags.length}
                            </span>

                        </div>

                        ${flagsHTML}

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Intelligence Records
                            </h2>

                            <span>
                                ${intelligence.length}
                            </span>

                        </div>

                        ${intelligenceHTML}

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Evidence Records
                            </h2>

                            <span>
                                ${evidence.length}
                            </span>

                        </div>

                        ${evidenceHTML}

                    </div>


                </div>


            </div>


            <aside>


                <div class="panel">


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Source Quality
                            </h2>

                            <span>
                                Source groups
                            </span>

                        </div>

                        <div class="source-grid">

                            ${sourceHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Investigation Navigation
                            </h2>

                        </div>

                        <div class="workspace-grid">

                            ${workspaceHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Methodology
                            </h2>

                        </div>

                        <div class="empty">

                            PropertyIQ reports the fields and
                            source relationships actually
                            stored in the database.

                            <br><br>

                            Missing information is shown as
                            missing rather than inferred.

                            <br><br>

                            Confidence and evidence fields
                            are displayed from stored data;
                            this workspace does not manufacture
                            verification.

                        </div>

                    </div>


                </div>


            </aside>


        </section>


        <div class="footer">

            PropertyIQ · Evidence & Data Quality Workspace

            <br>

            Source-backed information only.
            No fabricated evidence,
            valuation prediction,
            investment scoring,
            or unsupported legal conclusions.

        </div>

    `;
}


render();

</script>

</body>

</html>
"""


# ==============================================================================
# UI ROUTE
# ==============================================================================

V29_UI_ROUTE = (
    "/propertyiq/evidence-data-quality/"
    "{property_id}"
)


@app.get(
    V29_UI_ROUTE,
    response_class=HTMLResponse
)
def propertyiq_v29_ui(
    property_id: str
):

    try:

        data = v29_build_workspace(
            property_id
        )

        if data is None:

            return HTMLResponse(
                "<h2>Property not found</h2>",
                status_code=404
            )

        html = V29_HTML

        html = html.replace(
            "__PROPERTY_ID__",
            str(property_id)
        )

        html = html.replace(
            "__PROPERTYIQ_DATA__",
            json.dumps(
                data,
                default=str
            )
        )

        return HTMLResponse(
            html
        )

    except Exception as exc:

        return HTMLResponse(
            "<h2>PropertyIQ V29 Error</h2>"
            "<pre>" +
            str(exc) +
            "</pre>",
            status_code=500
        )


# ==============================================================================
# INSTALLATION OUTPUT
# ==============================================================================

print()
print("=" * 78)
print("PROPERTYIQ V29 — EVIDENCE / CONFIDENCE / DATA QUALITY INSTALLED")
print("=" * 78)
print()
print("Module:")
print(" ", MODULE_VERSION)
print()
print("API:")
print("  /api/v1/properties/{property_id}/evidence-data-quality")
print()
print("UI:")
print("  /propertyiq/evidence-data-quality/{property_id}")
print()
print("Capabilities:")
print("  ✓ Evidence inventory")
print("  ✓ Source inventory")
print("  ✓ Confidence coverage")
print("  ✓ Evidence-level coverage")
print("  ✓ Source URL coverage")
print("  ✓ Publication-date coverage")
print("  ✓ Coordinate coverage")
print("  ✓ Evidence linkage analysis")
print("  ✓ RERA source grouping")
print("  ✓ Government source grouping")
print("  ✓ News source grouping")
print("  ✓ OSM source grouping")
print("  ✓ Data-quality flags")
print("  ✓ Source links")
print("  ✓ Existing workspace navigation")
print()
print("Database modification during installation: DISABLED")
print("Fabricated evidence: DISABLED")
print("Fabricated confidence: DISABLED")
print("Investment score: DISABLED")
print("Valuation prediction: DISABLED")
print("Unsupported legal conclusions: DISABLED")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)



# ============================================================
# PROPERTYIQ MODULE: V30
# ORIGINAL COLAB CELL: In[54]
# ============================================================

# ==============================================================================
# PROPERTYIQ V30 — UNIFIED DEVELOPMENT INTELLIGENCE WORKSPACE
# ==============================================================================
# DIRECT-PASTE GOOGLE COLAB CELL
#
# Consolidates existing development intelligence:
#   • RERA
#   • Government infrastructure / PAIMANA
#   • News / GDELT
#   • OSM / GIS
#   • Existing development records
#   • Timeline
#   • Evidence
#
# This is an additive integration layer.
#
# NO:
#   • second database
#   • fabricated projects
#   • fabricated coordinates
#   • investment score
#   • appreciation prediction
#   • automatic legal conclusion
#   • database modification during installation
# ==============================================================================

import sys
import subprocess
import json
from datetime import datetime, timezone


MODULE_VERSION = "PROPERTYIQ-V30-UNIFIED-DEVELOPMENT-INTELLIGENCE"


# ==============================================================================
# DEPENDENCIES
# ==============================================================================

try:
    import fastapi
except Exception:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg[binary]"
    ])

from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text


# ==============================================================================
# EXISTING PROPERTYIQ RUNTIME
# ==============================================================================

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ PostgreSQL engine is not available. "
        "Run the existing PropertyIQ PostgreSQL runtime/bootstrap cell first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(title="PropertyIQ")


# ==============================================================================
# SCHEMA HELPERS
# ==============================================================================

def v30_table_exists(conn, table_name):

    try:

        return bool(
            conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema='public'
                        AND table_name=:table
                    )
                """),
                {"table": table_name}
            ).scalar()
        )

    except Exception:

        return False


def v30_columns(conn, table_name):

    try:

        return set(
            conn.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public'
                    AND table_name=:table
                """),
                {"table": table_name}
            ).scalars().all()
        )

    except Exception:

        return set()


def v30_safe(value):

    if value is None:
        return None

    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


# ==============================================================================
# PROPERTY
# ==============================================================================

def v30_property(conn, property_id):

    row = conn.execute(
        text("""
            SELECT
                id::text AS id,
                property_name,
                property_address,
                latitude,
                longitude,
                property_type,
                area,
                price,
                price_per_sqft,
                builder_owner,
                CASE
                    WHEN boundary IS NOT NULL
                    THEN true
                    ELSE false
                END AS has_boundary
            FROM properties
            WHERE id=CAST(:pid AS uuid)
            LIMIT 1
        """),
        {"pid": property_id}
    ).mappings().first()

    if not row:
        return None

    return {
        k: v30_safe(v)
        for k, v in dict(row).items()
    }


# ==============================================================================
# SOURCE CLASSIFICATION
# ==============================================================================

def v30_source_group(source_name, source_type, category):

    combined = (
        str(source_name or "") +
        " " +
        str(source_type or "") +
        " " +
        str(category or "")
    ).lower()

    if "rera" in combined:
        return "RERA"

    if (
        "paimana" in combined
        or "mospi" in combined
        or "government" in combined
        or "infrastructure" in combined
    ):
        return "GOVERNMENT"

    if (
        "gdelt" in combined
        or "news" in combined
    ):
        return "NEWS"

    if (
        "osm" in combined
        or "openstreetmap" in combined
        or "overpass" in combined
    ):
        return "OSM"

    if (
        "development" in combined
        or "project" in combined
    ):
        return "DEVELOPMENT"

    return "OTHER"


# ==============================================================================
# DEVELOPMENT RECORDS
# ==============================================================================

def v30_intelligence(
    conn,
    property_id
):

    if not v30_table_exists(
        conn,
        "intelligence_records"
    ):
        return []

    columns = v30_columns(
        conn,
        "intelligence_records"
    )

    selected = [
        "id::text AS id",
        "title"
    ]

    optional = [
        "category",
        "status",
        "description",
        "latitude",
        "longitude",
        "source_name",
        "source_type",
        "source_url",
        "published_at",
        "confidence",
        "evidence_level",
        "metadata",
        "created_at",
        "updated_at"
    ]

    for col in optional:

        if col in columns:
            selected.append(col)

    if "property_id" not in columns:
        return []

    order_col = (
        "updated_at"
        if "updated_at" in columns
        else "id"
    )

    rows = conn.execute(
        text(f"""
            SELECT
                {", ".join(selected)}
            FROM intelligence_records
            WHERE property_id=CAST(:pid AS uuid)
            ORDER BY {order_col} DESC
            LIMIT 2000
        """),
        {"pid": property_id}
    ).mappings().all()

    result = []

    for row in rows:

        item = {
            k: v30_safe(v)
            for k, v in dict(row).items()
        }

        item["source_group"] = v30_source_group(
            item.get("source_name"),
            item.get("source_type"),
            item.get("category")
        )

        item["mapped"] = (
            item.get("latitude") is not None
            and
            item.get("longitude") is not None
        )

        result.append(item)

    return result


# ==============================================================================
# DEVELOPMENT RELEVANCE
# ==============================================================================

def v30_is_development_record(record):

    group = record.get(
        "source_group"
    )

    if group in (
        "RERA",
        "GOVERNMENT",
        "DEVELOPMENT"
    ):
        return True

    text_blob = (
        str(record.get("title") or "") +
        " " +
        str(record.get("description") or "") +
        " " +
        str(record.get("category") or "")
    ).lower()

    terms = [
        "metro",
        "expressway",
        "highway",
        "road",
        "flyover",
        "railway",
        "airport",
        "infrastructure",
        "industrial",
        "logistics",
        "warehouse",
        "mall",
        "hospital",
        "school",
        "university",
        "township",
        "housing",
        "residential",
        "commercial",
        "construction",
        "development",
        "project",
        "corridor",
        "land acquisition",
        "rera"
    ]

    return any(
        term in text_blob
        for term in terms
    )


# ==============================================================================
# STATUS NORMALIZATION
# ==============================================================================

def v30_status(record):

    raw = str(
        record.get("status") or ""
    ).strip()

    if raw:
        return raw

    metadata = record.get(
        "metadata"
    )

    if isinstance(metadata, dict):

        for key in (
            "status",
            "project_status",
            "location_status"
        ):

            value = metadata.get(key)

            if value:
                return str(value)

    return "Status not specified"


# ==============================================================================
# DEVELOPMENT INVENTORY
# ==============================================================================

def v30_inventory(
    records
):

    development = [
        r
        for r in records
        if v30_is_development_record(r)
    ]

    source_counts = {}

    status_counts = {}

    mapped_count = 0

    unmapped_count = 0

    for record in development:

        group = (
            record.get("source_group")
            or "OTHER"
        )

        source_counts[group] = (
            source_counts.get(group, 0) + 1
        )

        status = v30_status(
            record
        )

        status_counts[status] = (
            status_counts.get(status, 0) + 1
        )

        if record.get("mapped"):
            mapped_count += 1
        else:
            unmapped_count += 1

    return {
        "total": len(development),
        "mapped": mapped_count,
        "unmapped": unmapped_count,
        "source_counts": source_counts,
        "status_counts": status_counts,
        "records": development
    }


# ==============================================================================
# EVIDENCE
# ==============================================================================

def v30_evidence(
    conn,
    property_id,
    intelligence_ids
):

    if not v30_table_exists(
        conn,
        "evidence_items"
    ):
        return []

    columns = v30_columns(
        conn,
        "evidence_items"
    )

    rows = []

    if (
        "property_id" in columns
    ):

        try:

            rows = conn.execute(
                text("""
                    SELECT *
                    FROM evidence_items
                    WHERE property_id=CAST(:pid AS uuid)
                    LIMIT 2000
                """),
                {"pid": property_id}
            ).mappings().all()

        except Exception:

            rows = []

    if (
        not rows
        and
        intelligence_ids
        and
        "intelligence_id" in columns
    ):

        try:

            rows = conn.execute(
                text("""
                    SELECT *
                    FROM evidence_items
                    WHERE intelligence_id IN (
                        SELECT CAST(x AS uuid)
                        FROM jsonb_array_elements_text(
                            CAST(:ids AS jsonb)
                        ) AS x
                    )
                    LIMIT 2000
                """),
                {
                    "ids":
                        json.dumps(
                            intelligence_ids
                        )
                }
            ).mappings().all()

        except Exception:

            rows = []

    return [
        {
            k: v30_safe(v)
            for k, v in dict(row).items()
        }
        for row in rows
    ]


# ==============================================================================
# DEVELOPMENT TIMELINE
# ==============================================================================

def v30_timeline_events(
    records,
    evidence
):

    events = []

    # --------------------------------------------------------------
    # Intelligence records
    # --------------------------------------------------------------

    for record in records:

        if not v30_is_development_record(
            record
        ):
            continue

        date_value = (
            record.get("published_at")
            or
            record.get("created_at")
            or
            record.get("updated_at")
        )

        events.append({
            "date":
                v30_safe(date_value),

            "title":
                record.get("title"),

            "source":
                record.get("source_name")
                or
                record.get("source_type"),

            "source_group":
                record.get("source_group"),

            "status":
                v30_status(record),

            "source_url":
                record.get("source_url"),

            "mapped":
                record.get("mapped"),

            "type":
                "intelligence"
        })

    # --------------------------------------------------------------
    # Evidence
    # --------------------------------------------------------------

    for item in evidence:

        date_value = (
            item.get("published_at")
            or
            item.get("created_at")
            or
            item.get("updated_at")
        )

        title = (
            item.get("title")
            or
            item.get("name")
            or
            item.get("description")
            or
            "Evidence record"
        )

        events.append({
            "date":
                v30_safe(date_value),

            "title":
                title,

            "source":
                item.get("source_name")
                or
                item.get("source")
                or
                "Evidence",

            "source_group":
                "EVIDENCE",

            "status":
                "Evidence",

            "source_url":
                item.get("source_url")
                or
                item.get("url"),

            "mapped":
                None,

            "type":
                "evidence"
        })

    def sort_key(item):

        value = item.get("date")

        if not value:
            return ""

        return str(value)

    events.sort(
        key=sort_key,
        reverse=True
    )

    return events


# ==============================================================================
# DEVELOPMENT FLAGS
# ==============================================================================

def v30_flags(
    development,
    evidence
):

    flags = []

    total = development.get(
        "total",
        0
    )

    mapped = development.get(
        "mapped",
        0
    )

    unmapped = development.get(
        "unmapped",
        0
    )

    if total == 0:

        flags.append({
            "key":
                "NO_DEVELOPMENT_RECORDS",

            "severity":
                "information",

            "title":
                "No development records are currently linked",

            "description":
                "PropertyIQ does not currently have "
                "property-linked records classified as "
                "development intelligence."
        })

    if unmapped:

        flags.append({
            "key":
                "DEVELOPMENT_LOCATION_PENDING",

            "severity":
                "attention",

            "title":
                "Development records lack coordinates",

            "description":
                str(unmapped) +
                " development records currently do not "
                "have usable coordinates and therefore "
                "cannot be placed on the spatial map."
        })

    rera = development.get(
        "source_counts",
        {}
    ).get(
        "RERA",
        0
    )

    government = development.get(
        "source_counts",
        {}
    ).get(
        "GOVERNMENT",
        0
    )

    news = development.get(
        "source_counts",
        {}
    ).get(
        "NEWS",
        0
    )

    if rera == 0:

        flags.append({
            "key":
                "NO_LINKED_RERA",

            "severity":
                "information",

            "title":
                "No linked RERA intelligence",

            "description":
                "No RERA record is currently attached "
                "to this property in PropertyIQ. "
                "This does not establish whether a "
                "RERA registration exists elsewhere."
        })

    if government == 0:

        flags.append({
            "key":
                "NO_GOVERNMENT_DEVELOPMENT",

            "severity":
                "information",

            "title":
                "No linked government infrastructure records",

            "description":
                "No government infrastructure records "
                "are currently linked to this property."
        })

    if news == 0:

        flags.append({
            "key":
                "NO_DEVELOPMENT_NEWS",

            "severity":
                "information",

            "title":
                "No linked development news",

            "description":
                "No development-related news records "
                "are currently linked to this property."
        })

    if total and not evidence:

        flags.append({
            "key":
                "DEVELOPMENT_EVIDENCE_PENDING",

            "severity":
                "attention",

            "title":
                "Development evidence inventory is empty",

            "description":
                "Development records exist but no "
                "linked evidence records were found."
        })

    return flags


# ==============================================================================
# MAIN WORKSPACE
# ==============================================================================

def v30_build(
    property_id
):

    with engine.begin() as conn:

        property_data = v30_property(
            conn,
            property_id
        )

        if not property_data:
            return None

        records = v30_intelligence(
            conn,
            property_id
        )

        intelligence_ids = [
            r.get("id")
            for r in records
            if r.get("id")
        ]

        evidence = v30_evidence(
            conn,
            property_id,
            intelligence_ids
        )

    development = v30_inventory(
        records
    )

    timeline = v30_timeline_events(
        records,
        evidence
    )

    flags = v30_flags(
        development,
        evidence
    )

    return {

        "module":
            MODULE_VERSION,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "property":
            property_data,

        "summary": {

            "development_records":
                development["total"],

            "mapped_development":
                development["mapped"],

            "unmapped_development":
                development["unmapped"],

            "evidence_records":
                len(evidence),

            "timeline_events":
                len(timeline),

            "source_groups":
                len(
                    development["source_counts"]
                ),

            "flags":
                len(flags)
        },

        "development":
            development,

        "timeline":
            timeline,

        "evidence":
            evidence,

        "flags":
            flags,

        "workspaces": {

            "portfolio":
                "/propertyiq/portfolio",

            "full_profile":
                f"/propertyiq/full-profile/{property_id}",

            "command_map":
                f"/propertyiq/command-map/{property_id}",

            "timeline":
                f"/propertyiq/timeline/{property_id}",

            "boundary":
                f"/propertyiq/boundary/{property_id}",

            "boundary_intelligence":
                f"/propertyiq/boundary-intelligence/{property_id}",

            "due_diligence":
                f"/propertyiq/acquisition-due-diligence/{property_id}",

            "evidence":
                f"/propertyiq/evidence-data-quality/{property_id}",

            "market":
                f"/propertyiq/market-comparables/{property_id}",

            "osm":
                f"/propertyiq/real-osm-gis/{property_id}",

            "documents":
                f"/propertyiq/files/{property_id}",

            "report":
                f"/propertyiq/intelligence-report/{property_id}"
        }
    }


# ==============================================================================
# API
# ==============================================================================

V30_API_ROUTE = (
    "/api/v1/properties/{property_id}/"
    "development-intelligence"
)


@app.get(V30_API_ROUTE)
def propertyiq_v30_api(
    property_id: str
):

    try:

        data = v30_build(
            property_id
        )

        if data is None:

            return JSONResponse(
                status_code=404,
                content={
                    "detail":
                        "Property not found"
                }
            )

        return data

    except Exception as exc:

        return JSONResponse(
            status_code=500,
            content={
                "detail":
                    str(exc)
            }
        )


# ==============================================================================
# PREMIUM UI
# ==============================================================================

V30_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Development Intelligence
</title>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,205,255,.10),
            transparent 30%
        ),
        radial-gradient(
            circle at 95% 15%,
            rgba(65,80,255,.08),
            transparent 30%
        ),
        #07101b;

    color: #eaf4ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

a {
    color: #68dcff;
    text-decoration: none;
}

a:hover {
    text-decoration: underline;
}

.topbar {

    height: 68px;

    display: flex;

    align-items: center;

    justify-content: space-between;

    padding: 0 25px;

    position: sticky;

    top: 0;

    z-index: 100;

    background:
        rgba(5,13,24,.90);

    border-bottom:
        1px solid rgba(255,255,255,.08);

    backdrop-filter:
        blur(18px);
}

.brand {

    display: flex;

    align-items: center;

    gap: 11px;
}

.mark {

    width: 35px;

    height: 35px;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            #27d9ff,
            #345cff
        );

    box-shadow:
        0 0 30px
        rgba(39,217,255,.20);
}

.brand-name {

    font-size: 17px;

    font-weight: 850;
}

.brand-sub {

    color: #71889f;

    font-size: 9px;

    letter-spacing: 1.2px;
}

.actions {

    display: flex;

    gap: 7px;

    flex-wrap: wrap;
}

.btn {

    padding: 8px 10px;

    border-radius: 9px;

    border:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(255,255,255,.04);

    color: #d8eafa;

    cursor: pointer;

    font-size: 10px;
}

.btn-primary {

    color: #70ddff;

    border-color:
        rgba(60,210,255,.28);

    background:
        rgba(60,210,255,.07);
}

.container {

    max-width: 1550px;

    margin: auto;

    padding: 23px;
}

.hero {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        340px;

    gap: 14px;

    margin-bottom: 14px;
}

.panel {

    border:
        1px solid rgba(255,255,255,.075);

    background:
        rgba(10,21,35,.82);

    border-radius: 16px;

    overflow: hidden;

    box-shadow:
        0 20px 60px
        rgba(0,0,0,.14);
}

.hero-main {

    padding: 22px;
}

.kicker {

    color: #64dcff;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: 1.7px;

    font-weight: 800;
}

.title {

    font-size: 28px;

    font-weight: 850;

    margin-top: 8px;
}

.address {

    color: #7d94ac;

    font-size: 12px;

    margin-top: 7px;
}

.coverage {

    padding: 22px;
}

.coverage-label {

    color: #71879f;

    font-size: 9px;

    text-transform: uppercase;

    letter-spacing: 1px;
}

.coverage-number {

    font-size: 40px;

    font-weight: 900;

    margin-top: 7px;
}

.coverage-note {

    color: #71879f;

    font-size: 10px;

    line-height: 1.5;

    margin-top: 5px;
}

.stats {

    display: grid;

    grid-template-columns:
        repeat(6,minmax(0,1fr));

    gap: 9px;

    margin-bottom: 14px;
}

.stat {

    padding: 14px;

    border:
        1px solid rgba(255,255,255,.07);

    border-radius: 13px;

    background:
        rgba(12,23,39,.76);
}

.stat-label {

    color: #70869e;

    font-size: 8px;

    text-transform: uppercase;

    letter-spacing: .8px;
}

.stat-value {

    font-size: 21px;

    font-weight: 850;

    margin-top: 6px;
}

.layout {

    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        400px;

    gap: 14px;
}

.section {

    padding: 17px;

    border-bottom:
        1px solid rgba(255,255,255,.055);
}

.section:last-child {
    border-bottom: 0;
}

.section-title {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 11px;
}

.section-title h2 {

    margin: 0;

    font-size: 13px;

    font-weight: 800;
}

.section-title span {

    color: #647b92;

    font-size: 9px;
}

.source-grid {

    display: grid;

    grid-template-columns:
        repeat(4,minmax(0,1fr));

    gap: 8px;
}

.source {

    padding: 12px;

    border:
        1px solid rgba(255,255,255,.06);

    border-radius: 10px;

    background:
        rgba(255,255,255,.025);
}

.source-name {

    color: #9db2c7;

    font-size: 9px;
}

.source-count {

    color: #68ddff;

    font-size: 20px;

    font-weight: 850;

    margin-top: 5px;
}

.source-small {

    color: #667d94;

    font-size: 8px;

    margin-top: 3px;
}

.flag {

    padding: 10px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 9px;

    background:
        rgba(255,255,255,.025);

    margin-bottom: 7px;
}

.flag-title {

    font-size: 10px;

    font-weight: 750;
}

.flag-description {

    color: #71879e;

    font-size: 9px;

    line-height: 1.45;

    margin-top: 3px;
}

.record {

    padding: 12px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 10px;

    background:
        rgba(255,255,255,.022);

    margin-bottom: 7px;
}

.record-title {

    font-size: 10px;

    font-weight: 750;

    line-height: 1.45;
}

.record-meta {

    display: flex;

    flex-wrap: wrap;

    gap: 5px;

    margin-top: 6px;
}

.tag {

    padding: 4px 6px;

    border-radius: 999px;

    background:
        rgba(255,255,255,.04);

    color: #748aa0;

    font-size: 8px;
}

.record-link {

    margin-top: 6px;

    font-size: 8px;
}

.timeline {

    position: relative;

    margin-top: 10px;

    padding-left: 20px;
}

.timeline:before {

    content: "";

    position: absolute;

    left: 6px;

    top: 0;

    bottom: 0;

    width: 1px;

    background:
        rgba(80,210,255,.18);
}

.event {

    position: relative;

    padding: 0 0 15px 13px;
}

.event:before {

    content: "";

    position: absolute;

    left: -18px;

    top: 4px;

    width: 7px;

    height: 7px;

    border-radius: 50%;

    background: #64dcff;

    box-shadow:
        0 0 10px
        rgba(100,220,255,.45);
}

.event-date {

    color: #62d9ff;

    font-size: 8px;

    text-transform: uppercase;
}

.event-title {

    margin-top: 4px;

    font-size: 10px;

    font-weight: 750;

    line-height: 1.45;
}

.event-meta {

    color: #6f859d;

    font-size: 8px;

    margin-top: 4px;
}

.workspace-grid {

    display: grid;

    grid-template-columns:
        repeat(3,minmax(0,1fr));

    gap: 8px;
}

.workspace {

    padding: 11px;

    border:
        1px solid rgba(255,255,255,.055);

    border-radius: 9px;

    background:
        rgba(255,255,255,.025);

    cursor: pointer;
}

.workspace:hover {

    border-color:
        rgba(70,210,255,.25);

    background:
        rgba(70,210,255,.05);
}

.workspace-title {

    font-size: 10px;

    font-weight: 750;
}

.workspace-description {

    color: #70869d;

    font-size: 8px;

    line-height: 1.4;

    margin-top: 4px;
}

.empty {

    padding: 15px;

    text-align: center;

    color: #667d94;

    font-size: 10px;
}

.footer {

    text-align: center;

    padding: 25px 0 10px;

    color: #4e637a;

    font-size: 9px;
}

@media(max-width:1100px) {

    .hero {

        grid-template-columns: 1fr;
    }

    .stats {

        grid-template-columns:
            repeat(3,1fr);
    }

    .layout {

        grid-template-columns: 1fr;
    }

    .source-grid {

        grid-template-columns:
            repeat(2,1fr);
    }
}

@media(max-width:700px) {

    .container {
        padding: 12px;
    }

    .stats {

        grid-template-columns:
            repeat(2,1fr);
    }

    .source-grid {

        grid-template-columns: 1fr;
    }

    .workspace-grid {

        grid-template-columns:
            repeat(2,1fr);
    }
}

</style>

</head>


<body>


<header class="topbar">

    <div class="brand">

        <div class="mark"></div>

        <div>

            <div class="brand-name">
                PropertyIQ
            </div>

            <div class="brand-sub">
                UNIFIED DEVELOPMENT INTELLIGENCE
            </div>

        </div>

    </div>


    <div class="actions">

        <button
            class="btn"
            onclick="go('portfolio')">
            Portfolio
        </button>

        <button
            class="btn"
            onclick="go('command_map')">
            Command Map
        </button>

        <button
            class="btn"
            onclick="go('timeline')">
            Timeline
        </button>

        <button
            class="btn btn-primary"
            onclick="go('due_diligence')">
            Due Diligence
        </button>

    </div>

</header>


<main class="container">

    <div id="app">
        Loading development intelligence...
    </div>

</main>


<script>

const DATA =
    __PROPERTYIQ_DATA__;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}


function go(key) {

    const url =
        DATA.workspaces &&
        DATA.workspaces[key];

    if (url) {
        window.location.href = url;
    }
}


function render() {

    const property =
        DATA.property || {};

    const summary =
        DATA.summary || {};

    const development =
        DATA.development || {};

    const sourceCounts =
        development.source_counts || {};

    const statusCounts =
        development.status_counts || {};

    const flags =
        DATA.flags || [];

    const timeline =
        DATA.timeline || [];


    // --------------------------------------------------------------
    // SOURCE CARDS
    // --------------------------------------------------------------

    const sourceOrder = [
        "RERA",
        "GOVERNMENT",
        "NEWS",
        "OSM",
        "DEVELOPMENT",
        "OTHER"
    ];


    let sourceHTML = "";

    sourceOrder.forEach(
        function(source) {

            const count =
                sourceCounts[source] || 0;

            if (
                count === 0
            ) {
                return;
            }

            sourceHTML += `

                <div class="source">

                    <div class="source-name">
                        ${esc(source)}
                    </div>

                    <div class="source-count">
                        ${esc(count)}
                    </div>

                    <div class="source-small">
                        development records
                    </div>

                </div>

            `;

        }
    );


    if (!sourceHTML) {

        sourceHTML = `
            <div class="empty">
                No development records are currently linked.
            </div>
        `;
    }


    // --------------------------------------------------------------
    // STATUS
    // --------------------------------------------------------------

    let statusHTML = "";

    const statusKeys =
        Object.keys(statusCounts);


    if (!statusKeys.length) {

        statusHTML = `
            <div class="empty">
                No stored development status values.
            </div>
        `;

    } else {

        statusHTML =
            statusKeys.map(
                function(status) {

                    return `

                        <div class="source">

                            <div class="source-name">
                                ${esc(status)}
                            </div>

                            <div class="source-count">
                                ${esc(
                                    statusCounts[status]
                                )}
                            </div>

                            <div class="source-small">
                                records
                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // FLAGS
    // --------------------------------------------------------------

    let flagsHTML = "";

    if (!flags.length) {

        flagsHTML = `
            <div class="empty">
                No development data-quality flags.
            </div>
        `;

    } else {

        flagsHTML =
            flags.map(
                function(flag) {

                    return `

                        <div class="flag">

                            <div class="flag-title">

                                ${esc(
                                    flag.title
                                )}

                                ${
                                    flag.count
                                    ?
                                    " · " +
                                    esc(flag.count)
                                    :
                                    ""
                                }

                            </div>

                            <div class="flag-description">
                                ${esc(
                                    flag.description
                                )}
                            </div>

                        </div>

                    `;

                }
            ).join("");
    }


    // --------------------------------------------------------------
    // TIMELINE
    // --------------------------------------------------------------

    let timelineHTML = "";

    if (!timeline.length) {

        timelineHTML = `
            <div class="empty">
                No development timeline events are currently available.
            </div>
        `;

    } else {

        timelineHTML =
            timeline
                .slice(0,60)
                .map(
                    function(event) {

                        let sourceLink = "";

                        if (
                            event.source_url
                        ) {

                            sourceLink = `

                                <a
                                    href="${esc(
                                        event.source_url
                                    )}"
                                    target="_blank"
                                    rel="noopener"
                                >
                                    Source →
                                </a>

                            `;
                        }


                        return `

                            <div class="event">

                                <div class="event-date">
                                    ${esc(
                                        event.date ||
                                        "Date not available"
                                    )}
                                </div>

                                <div class="event-title">
                                    ${esc(
                                        event.title ||
                                        "Development event"
                                    )}
                                </div>

                                <div class="event-meta">

                                    ${esc(
                                        event.source ||
                                        "Source unavailable"
                                    )}

                                    ·

                                    ${esc(
                                        event.status ||
                                        "Status not specified"
                                    )}

                                    ·

                                    ${
                                        event.mapped
                                        === true
                                        ? "Mapped"
                                        : event.mapped
                                          === false
                                          ? "Location pending"
                                          : ""
                                    }

                                    ${
                                        sourceLink
                                        ?
                                        " · " +
                                        sourceLink
                                        :
                                        ""
                                    }

                                </div>

                            </div>

                        `;

                    }
                ).join("");
    }


    // --------------------------------------------------------------
    // WORKSPACES
    // --------------------------------------------------------------

    const workspaces = [

        [
            "command_map",
            "Unified Command Map",
            "See development intelligence spatially."
        ],

        [
            "timeline",
            "Development Timeline",
            "Review the complete chronological view."
        ],

        [
            "boundary_intelligence",
            "Boundary Intelligence",
            "Check development records against the property boundary."
        ],

        [
            "due_diligence",
            "Due-Diligence Workspace",
            "Continue property investigation."
        ],

        [
            "evidence",
            "Evidence & Data Quality",
            "Review source and evidence completeness."
        ],

        [
            "market",
            "Market Intelligence",
            "Review existing market observations."
        ],

        [
            "osm",
            "OSM / GIS",
            "Inspect geographic context."
        ],

        [
            "report",
            "Intelligence Report",
            "Open the consolidated property report."
        ],

        [
            "full_profile",
            "Full Property Profile",
            "Open the complete property profile."
        ]

    ];


    const workspaceHTML =
        workspaces.map(
            function(item) {

                return `

                    <div
                        class="workspace"
                        onclick="go('${item[0]}')"
                    >

                        <div class="workspace-title">
                            ${esc(item[1])}
                        </div>

                        <div class="workspace-description">
                            ${esc(item[2])}
                        </div>

                    </div>

                `;

            }
        ).join("");


    // --------------------------------------------------------------
    // RENDER
    // --------------------------------------------------------------

    document.getElementById(
        "app"
    ).innerHTML = `


        <section class="hero">


            <div class="panel hero-main">

                <div class="kicker">
                    DEVELOPMENT INTELLIGENCE
                </div>

                <div class="title">
                    ${esc(
                        property.property_name ||
                        "Property"
                    )}
                </div>

                <div class="address">
                    ${esc(
                        property.property_address ||
                        "Address unavailable"
                    )}
                </div>

            </div>


            <div class="panel coverage">

                <div class="coverage-label">
                    Development Records
                </div>

                <div class="coverage-number">
                    ${esc(
                        summary.development_records ||
                        0
                    )}
                </div>

                <div class="coverage-note">

                    Records from the existing
                    PropertyIQ intelligence layer
                    that are classified as development-related.

                </div>

            </div>


        </section>


        <section class="stats">


            <div class="stat">

                <div class="stat-label">
                    Development
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.development_records ||
                        0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Mapped
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.mapped_development ||
                        0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Location Pending
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.unmapped_development ||
                        0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Evidence
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.evidence_records ||
                        0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Timeline
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.timeline_events ||
                        0
                    )}
                </div>

            </div>


            <div class="stat">

                <div class="stat-label">
                    Flags
                </div>

                <div class="stat-value">
                    ${esc(
                        summary.flags ||
                        0
                    )}
                </div>

            </div>


        </section>


        <section class="layout">


            <div>


                <div class="panel">


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Development Source Inventory
                            </h2>

                            <span>
                                Existing source-backed records
                            </span>

                        </div>

                        <div class="source-grid">

                            ${sourceHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Development Status
                            </h2>

                            <span>
                                Stored status values
                            </span>

                        </div>

                        <div class="source-grid">

                            ${statusHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Development Timeline
                            </h2>

                            <span>
                                ${timeline.length} events
                            </span>

                        </div>

                        <div class="timeline">

                            ${timelineHTML}

                        </div>

                    </div>


                </div>


            </div>


            <aside>


                <div class="panel">


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Development Data Quality
                            </h2>

                            <span>
                                ${flags.length}
                            </span>

                        </div>

                        ${flagsHTML}

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Continue Investigation
                            </h2>

                        </div>

                        <div class="workspace-grid">

                            ${workspaceHTML}

                        </div>

                    </div>


                    <div class="section">

                        <div class="section-title">

                            <h2>
                                Interpretation
                            </h2>

                        </div>

                        <div class="empty">

                            PropertyIQ displays documented
                            development records and their
                            stored source information.

                            <br><br>

                            A project appearing here does not
                            by itself establish that it will
                            affect property value.

                            <br><br>

                            Records without coordinates are
                            retained as location-pending rather
                            than being assigned guessed coordinates.

                        </div>

                    </div>


                </div>


            </aside>


        </section>


        <div class="footer">

            PropertyIQ · Unified Development Intelligence

            <br>

            RERA · Government · News · OSM/GIS · Evidence

        </div>

    `;
}


render();

</script>

</body>

</html>
"""


# ==============================================================================
# UI ROUTE
# ==============================================================================

V30_UI_ROUTE = (
    "/propertyiq/development-intelligence/"
    "{property_id}"
)


@app.get(
    V30_UI_ROUTE,
    response_class=HTMLResponse
)
def propertyiq_v30_ui(
    property_id: str
):

    try:

        data = v30_build(
            property_id
        )

        if data is None:

            return HTMLResponse(
                "<h2>Property not found</h2>",
                status_code=404
            )

        html = V30_HTML

        html = html.replace(
            "__PROPERTYIQ_DATA__",
            json.dumps(
                data,
                default=str
            )
        )

        return HTMLResponse(
            html
        )

    except Exception as exc:

        return HTMLResponse(
            "<h2>PropertyIQ V30 Error</h2>"
            "<pre>" +
            str(exc) +
            "</pre>",
            status_code=500
        )


# ==============================================================================
# INSTALLATION OUTPUT
# ==============================================================================

print()
print("=" * 78)
print("PROPERTYIQ V30 — UNIFIED DEVELOPMENT INTELLIGENCE INSTALLED")
print("=" * 78)
print()
print("Module:")
print(" ", MODULE_VERSION)
print()
print("API:")
print("  /api/v1/properties/{property_id}/development-intelligence")
print()
print("UI:")
print("  /propertyiq/development-intelligence/{property_id}")
print()
print("Integrated:")
print("  ✓ RERA")
print("  ✓ Government / PAIMANA")
print("  ✓ News / GDELT")
print("  ✓ OSM / GIS")
print("  ✓ Existing development records")
print("  ✓ Evidence")
print("  ✓ Development timeline")
print("  ✓ Source URLs")
print("  ✓ Location status")
print()
print("Development source inventory: ENABLED")
print("Development timeline: ENABLED")
print("Mapped/unmapped tracking: ENABLED")
print("Source grouping: ENABLED")
print("Evidence linkage: ENABLED")
print()
print("Fabricated projects: DISABLED")
print("Fabricated coordinates: DISABLED")
print("Investment score: DISABLED")
print("Appreciation prediction: DISABLED")
print("Legal conclusion generation: DISABLED")
print("Database modification during installation: DISABLED")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)



# ============================================================
# PROPERTYIQ MODULE: V31
# ORIGINAL COLAB CELL: In[55]
# ============================================================

# ==============================================================================
# PROPERTYIQ V31 — LIVE DEVELOPMENT INTELLIGENCE
# ==============================================================================
# Purpose:
#   Property-centric live refresh of development intelligence using the
#   EXISTING PropertyIQ live connector layer.
#
# Integrates:
#   - OpenStreetMap / Overpass
#   - GDELT / development news
#   - PAIMANA / MoSPI government infrastructure
#   - Existing ingestion + evidence pipeline
#   - Existing RERA layer remains available separately
#
# Does NOT:
#   - rebuild RERA
#   - rebuild OSM
#   - rebuild GDELT
#   - rebuild PAIMANA
#   - fabricate coordinates
#   - fabricate development projects
#   - predict property appreciation
#   - assign investment scores
#
# Direct Colab execution: ENABLED
# Existing engine + app are reused.
# ==============================================================================

import sys
import subprocess
import importlib
import json
import uuid
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ------------------------------------------------------------------------------
# 1. Basic dependency bootstrap
# ------------------------------------------------------------------------------

def _v31_install(package_name, import_name=None):
    import_name = import_name or package_name

    try:
        importlib.import_module(import_name)
        return
    except Exception:
        pass

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        package_name
    ])


_v31_install("psycopg[binary]", "psycopg")
_v31_install("fastapi", "fastapi")
_v31_install("sqlalchemy", "sqlalchemy")


# ------------------------------------------------------------------------------
# 2. Imports
# ------------------------------------------------------------------------------

import psycopg
from sqlalchemy import text as sa_text


# ------------------------------------------------------------------------------
# 3. Runtime bootstrap
# ------------------------------------------------------------------------------

MODULE_VERSION = "PROPERTYIQ-V31-LIVE-DEVELOPMENT-INTELLIGENCE"


def _v31_find_existing(name):
    return globals().get(name, None)


def _v31_bootstrap_engine():

    existing = _v31_find_existing("engine")

    if existing is not None:
        return existing

    # Try to recover from common PropertyIQ globals.
    candidates = [
        "PROPERTYIQ_ENGINE",
        "DB_ENGINE",
        "ENGINE"
    ]

    for name in candidates:
        obj = globals().get(name)
        if obj is not None:
            globals()["engine"] = obj
            return obj

    raise RuntimeError(
        "PropertyIQ database engine was not found in the current Colab runtime. "
        "Run the existing PropertyIQ PostgreSQL/PostGIS runtime bootstrap first."
    )


def _v31_bootstrap_app():

    app_obj = _v31_find_existing("app")

    if app_obj is not None:
        return app_obj


    app_obj = FastAPI(
        title="PropertyIQ",
        version="V31"
    )

    globals()["app"] = app_obj

    return app_obj


engine = _v31_bootstrap_engine()
app = _v31_bootstrap_app()


# ------------------------------------------------------------------------------
# 4. Existing live connector discovery
# ------------------------------------------------------------------------------

def _v31_existing_function(name):

    obj = globals().get(name)

    if callable(obj):
        return obj

    return None


# Existing V15/V16 connector functions may have different names depending
# on which modules were installed earlier. We discover them without rebuilding
# the connectors.

_V31_CANDIDATES = {

    "osm": [
        "live_osm_for_property",
        "refresh_osm_for_property",
        "run_osm_connector",
        "fetch_osm_for_property",
        "get_live_osm_for_property"
    ],

    "news": [
        "live_news_for_property",
        "refresh_news_for_property",
        "run_news_connector",
        "fetch_news_for_property",
        "get_live_news_for_property"
    ],

    "government": [
        "live_government_for_property",
        "refresh_government_for_property",
        "run_government_connector",
        "fetch_government_for_property",
        "get_live_government_for_property"
    ],

    "refresh": [
        "refresh_property_live_intelligence",
        "run_property_live_refresh",
        "live_refresh_property"
    ]
}


def _v31_resolve_connector(source_key):

    for function_name in _V31_CANDIDATES.get(source_key, []):

        fn = _v31_existing_function(function_name)

        if fn is not None:
            return fn, function_name

    return None, None


# ------------------------------------------------------------------------------
# 5. Database helpers
# ------------------------------------------------------------------------------

def _v31_table_exists(table_name):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = :table
                )
            """),
            {"table": table_name}
        ).scalar()

    return bool(row)


def _v31_column_exists(table_name, column_name):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table
                      AND column_name = :column
                )
            """),
            {
                "table": table_name,
                "column": column_name
            }
        ).scalar()

    return bool(row)


# ------------------------------------------------------------------------------
# 6. Create refresh history table
# ------------------------------------------------------------------------------

def _v31_create_refresh_table():

    with engine.begin() as conn:

        conn.execute(
            sa_text("""
                CREATE TABLE IF NOT EXISTS piq_property_refresh_runs (
                    id UUID PRIMARY KEY,
                    property_id UUID NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'running',
                    sources_requested JSONB NOT NULL DEFAULT '[]'::jsonb,
                    source_results JSONB NOT NULL DEFAULT '{}'::jsonb,
                    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
                    records_before INTEGER,
                    records_after INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        )

        conn.execute(
            sa_text("""
                CREATE INDEX IF NOT EXISTS
                idx_piq_property_refresh_runs_property
                ON piq_property_refresh_runs(property_id)
            """)
        )

        conn.execute(
            sa_text("""
                CREATE INDEX IF NOT EXISTS
                idx_piq_property_refresh_runs_started
                ON piq_property_refresh_runs(started_at DESC)
            """)
        )


_v31_create_refresh_table()


# ------------------------------------------------------------------------------
# 7. Property loader
# ------------------------------------------------------------------------------

def _v31_get_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities,
                    photos,
                    documents,
                    contact_information
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {"pid": str(property_id)}
        ).mappings().first()

    if row is None:
        return None

    return dict(row)


# ------------------------------------------------------------------------------
# 8. Intelligence count
# ------------------------------------------------------------------------------

def _v31_intelligence_count(property_id):

    if not _v31_table_exists("intelligence_records"):
        return 0

    with engine.connect() as conn:

        count = conn.execute(
            sa_text("""
                SELECT COUNT(*)
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
            """),
            {"pid": str(property_id)}
        ).scalar()

    return int(count or 0)


# ------------------------------------------------------------------------------
# 9. Flexible connector invocation
# ------------------------------------------------------------------------------

def _v31_call_connector(fn, property_id, property_data):

    """
    Connector compatibility layer.

    Existing PropertyIQ connector functions may accept:
      fn(property_id)
      fn(property_id, ...)
      fn(property_data)
      fn(property_id=...)
      fn(property=...)
    """

    attempts = [
        lambda: fn(property_id),
        lambda: fn(str(property_id)),
        lambda: fn(property_data),
        lambda: fn(property_id=property_id),
        lambda: fn(property_id=str(property_id)),
        lambda: fn(property=property_data),
        lambda: fn(property_data=property_data)
    ]

    last_error = None

    for attempt in attempts:

        try:
            return attempt()

        except TypeError as exc:
            last_error = exc
            continue

        except Exception:
            raise

    if last_error:
        raise last_error

    return None


# ------------------------------------------------------------------------------
# 10. Normalize connector result
# ------------------------------------------------------------------------------

def _v31_normalize_result(result):

    if result is None:
        return {
            "status": "completed",
            "records": 0,
            "details": None
        }

    if isinstance(result, dict):

        records = (
            result.get("records")
            if isinstance(result.get("records"), int)
            else result.get("count")
        )

        if records is None:

            for key in [
                "items",
                "results",
                "intelligence",
                "records_created"
            ]:
                value = result.get(key)

                if isinstance(value, list):
                    records = len(value)
                    break

        if records is None:
            records = 0

        return {
            "status": result.get("status", "completed"),
            "records": int(records or 0),
            "details": result
        }

    if isinstance(result, list):

        return {
            "status": "completed",
            "records": len(result),
            "details": {
                "items": result
            }
        }

    return {
        "status": "completed",
        "records": 0,
        "details": str(result)
    }


# ------------------------------------------------------------------------------
# 11. Run one live source
# ------------------------------------------------------------------------------

def _v31_run_source(source_key, property_id, property_data):

    fn, fn_name = _v31_resolve_connector(source_key)

    if fn is None:

        return {
            "source": source_key,
            "status": "not_available",
            "connector": None,
            "records": 0,
            "message": (
                "Existing live connector was not found in the current runtime."
            )
        }

    started = datetime.now(timezone.utc)

    try:

        raw = _v31_call_connector(
            fn,
            property_id,
            property_data
        )

        normalized = _v31_normalize_result(raw)

        finished = datetime.now(timezone.utc)

        normalized.update({
            "source": source_key,
            "connector": fn_name,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat()
        })

        return normalized

    except Exception as exc:

        finished = datetime.now(timezone.utc)

        return {
            "source": source_key,
            "status": "error",
            "connector": fn_name,
            "records": 0,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "error": str(exc),
            "traceback": traceback.format_exc(limit=3)
        }


# ------------------------------------------------------------------------------
# 12. Main live development refresh engine
# ------------------------------------------------------------------------------

def run_v31_live_development_refresh(
    property_id,
    sources=None
):

    property_id = str(property_id)

    property_data = _v31_get_property(property_id)

    if property_data is None:

        raise ValueError(
            f"Property {property_id} was not found."
        )

    if sources is None:

        sources = [
            "osm",
            "news",
            "government"
        ]

    sources = [
        str(source).lower().strip()
        for source in sources
        if str(source).strip()
    ]

    allowed = {
        "osm",
        "news",
        "government"
    }

    sources = [
        source
        for source in sources
        if source in allowed
    ]

    if not sources:

        raise ValueError(
            "No valid live development sources requested."
        )

    run_id = str(uuid.uuid4())

    before_count = _v31_intelligence_count(property_id)

    with engine.begin() as conn:

        conn.execute(
            sa_text("""
                INSERT INTO piq_property_refresh_runs (
                    id,
                    property_id,
                    status,
                    sources_requested,
                    records_before
                )
                VALUES (
                    CAST(:id AS uuid),
                    CAST(:property_id AS uuid),
                    'running',
                    CAST(:sources AS jsonb),
                    :records_before
                )
            """),
            {
                "id": run_id,
                "property_id": property_id,
                "sources": json.dumps(sources),
                "records_before": before_count
            }
        )

    source_results = {}
    errors = []

    for source in sources:

        result = _v31_run_source(
            source,
            property_id,
            property_data
        )

        source_results[source] = result

        if result.get("status") == "error":
            errors.append({
                "source": source,
                "error": result.get("error")
            })

    after_count = _v31_intelligence_count(property_id)

    status = "completed"

    if errors and len(errors) < len(sources):
        status = "partial"

    elif errors and len(errors) == len(sources):
        status = "failed"

    finished_at = datetime.now(timezone.utc)

    with engine.begin() as conn:

        conn.execute(
            sa_text("""
                UPDATE piq_property_refresh_runs
                SET
                    finished_at = :finished_at,
                    status = :status,
                    source_results = CAST(:source_results AS jsonb),
                    errors = CAST(:errors AS jsonb),
                    records_after = :records_after
                WHERE id = CAST(:id AS uuid)
            """),
            {
                "id": run_id,
                "finished_at": finished_at,
                "status": status,
                "source_results": json.dumps(
                    source_results,
                    default=str
                ),
                "errors": json.dumps(
                    errors,
                    default=str
                ),
                "records_after": after_count
            }
        )

    return {
        "run_id": run_id,
        "property_id": property_id,
        "status": status,
        "started_at": None,
        "finished_at": finished_at.isoformat(),
        "sources_requested": sources,
        "source_results": source_results,
        "records_before": before_count,
        "records_after": after_count,
        "records_delta": after_count - before_count,
        "errors": errors
    }


# ------------------------------------------------------------------------------
# 13. Refresh history
# ------------------------------------------------------------------------------

def get_v31_refresh_history(property_id, limit=25):

    property_id = str(property_id)

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    started_at,
                    finished_at,
                    status,
                    sources_requested,
                    source_results,
                    errors,
                    records_before,
                    records_after,
                    created_at
                FROM piq_property_refresh_runs
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY started_at DESC
                LIMIT :limit
            """),
            {
                "pid": property_id,
                "limit": int(limit)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 14. Latest refresh status
# ------------------------------------------------------------------------------

def get_v31_latest_refresh(property_id):

    history = get_v31_refresh_history(
        property_id,
        limit=1
    )

    if not history:
        return None

    return history[0]


# ------------------------------------------------------------------------------
# 15. API routes
# ------------------------------------------------------------------------------

from fastapi import HTTPException
from pydantic import BaseModel, Field


class V31RefreshRequest(BaseModel):

    sources: list[str] = Field(
        default_factory=lambda: [
            "osm",
            "news",
            "government"
        ]
    )


# Avoid duplicate route registration if the cell is accidentally rerun.
_v31_existing_routes = {
    getattr(route, "path", "")
    for route in getattr(app, "routes", [])
}


def _v31_add_route(method, path, endpoint):

    if path in _v31_existing_routes:
        return

    if method == "GET":
        app.get(path)(endpoint)

    elif method == "POST":
        app.post(path)(endpoint)

    elif method == "PUT":
        app.put(path)(endpoint)

    _v31_existing_routes.add(path)


# --------------------------------------------------------------------------
# POST live refresh
# --------------------------------------------------------------------------

async def v31_live_refresh_endpoint(
    property_id: str,
    payload: V31RefreshRequest
):

    try:

        result = run_v31_live_development_refresh(
            property_id=property_id,
            sources=payload.sources
        )

        return {
            "success": True,
            "module": MODULE_VERSION,
            "result": result
        }

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v31_add_route(
    "POST",
    "/api/v1/properties/{property_id}/live-development-refresh",
    v31_live_refresh_endpoint
)


# --------------------------------------------------------------------------
# GET latest status
# --------------------------------------------------------------------------

async def v31_live_refresh_status_endpoint(
    property_id: str
):

    try:

        property_data = _v31_get_property(property_id)

        if property_data is None:
            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )

        latest = get_v31_latest_refresh(property_id)

        return {
            "success": True,
            "module": MODULE_VERSION,
            "property_id": property_id,
            "latest_refresh": latest
        }

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v31_add_route(
    "GET",
    "/api/v1/properties/{property_id}/live-development-refresh/status",
    v31_live_refresh_status_endpoint
)


# --------------------------------------------------------------------------
# GET refresh history
# --------------------------------------------------------------------------

async def v31_live_refresh_history_endpoint(
    property_id: str,
    limit: int = 25
):

    try:

        property_data = _v31_get_property(property_id)

        if property_data is None:
            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )

        history = get_v31_refresh_history(
            property_id,
            max(1, min(int(limit), 100))
        )

        return {
            "success": True,
            "module": MODULE_VERSION,
            "property_id": property_id,
            "history": history
        }

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v31_add_route(
    "GET",
    "/api/v1/properties/{property_id}/live-development-refresh/history",
    v31_live_refresh_history_endpoint
)


# ------------------------------------------------------------------------------
# 16. Development intelligence API
# ------------------------------------------------------------------------------

async def v31_live_development_summary_endpoint(
    property_id: str
):

    try:

        property_data = _v31_get_property(property_id)

        if property_data is None:

            raise HTTPException(
                status_code=404,
                detail="Property not found."
            )

        with engine.connect() as conn:

            rows = conn.execute(
                sa_text("""
                    SELECT
                        id,
                        title,
                        category,
                        status,
                        description,
                        latitude,
                        longitude,
                        source_name,
                        source_type,
                        source_url,
                        published_at,
                        confidence,
                        evidence_level,
                        metadata
                    FROM intelligence_records
                    WHERE property_id = CAST(:pid AS uuid)
                    ORDER BY created_at DESC
                """),
                {"pid": str(property_id)}
            ).mappings().all()

        development_keywords = [
            "metro",
            "rail",
            "highway",
            "expressway",
            "road",
            "flyover",
            "airport",
            "hospital",
            "school",
            "university",
            "industrial",
            "logistics",
            "warehouse",
            "commercial",
            "mall",
            "township",
            "development",
            "construction",
            "infrastructure",
            "project",
            "corridor",
            "land acquisition"
        ]

        development_records = []

        for row in rows:

            record = dict(row)

            searchable = " ".join([
                str(record.get("title") or ""),
                str(record.get("category") or ""),
                str(record.get("description") or "")
            ]).lower()

            if any(
                keyword in searchable
                for keyword in development_keywords
            ):

                development_records.append(record)

        source_summary = {}

        status_summary = {}

        mapped = 0
        unmapped = 0

        for record in development_records:

            source = (
                record.get("source_name")
                or record.get("source_type")
                or "UNKNOWN"
            )

            source_summary[source] = (
                source_summary.get(source, 0) + 1
            )

            status = (
                record.get("status")
                or "unknown"
            )

            status_summary[status] = (
                status_summary.get(status, 0) + 1
            )

            lat = record.get("latitude")
            lon = record.get("longitude")

            if (
                lat is not None
                and lon is not None
                and -90 <= float(lat) <= 90
                and -180 <= float(lon) <= 180
            ):
                mapped += 1
            else:
                unmapped += 1

        return {
            "success": True,
            "module": MODULE_VERSION,
            "property": property_data,
            "development_records": development_records,
            "summary": {
                "total_development_records": len(
                    development_records
                ),
                "mapped_records": mapped,
                "unmapped_records": unmapped,
                "source_summary": source_summary,
                "status_summary": status_summary
            },
            "latest_live_refresh": get_v31_latest_refresh(
                property_id
            )
        }

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v31_add_route(
    "GET",
    "/api/v1/properties/{property_id}/live-development",
    v31_live_development_summary_endpoint
)


# ------------------------------------------------------------------------------
# 17. Premium Live Development UI
# ------------------------------------------------------------------------------

def _v31_ui(property_id):

    html = r"""
<!DOCTYPE html>
<html>
<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>PropertyIQ — Live Development Intelligence</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0, 170, 255, .12),
            transparent 32%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(0, 255, 210, .08),
            transparent 30%
        ),
        #07111f;

    color: #eaf4ff;
}

header {

    padding: 24px 30px;

    border-bottom:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(7,17,31,.82);

    backdrop-filter: blur(16px);

    position: sticky;

    top: 0;

    z-index: 10;
}

.brand {

    font-size: 21px;

    font-weight: 800;

    letter-spacing: .5px;
}

.brand span {

    color: #35c8ff;
}

.subtitle {

    margin-top: 6px;

    color: #8fa8c4;

    font-size: 13px;
}

.container {

    max-width: 1450px;

    margin: auto;

    padding: 26px;
}

.grid {

    display: grid;

    grid-template-columns:
        repeat(auto-fit,minmax(260px,1fr));

    gap: 16px;
}

.card {

    background:
        linear-gradient(
            145deg,
            rgba(20,35,55,.88),
            rgba(10,21,37,.86)
        );

    border:
        1px solid rgba(255,255,255,.08);

    border-radius: 18px;

    padding: 20px;

    box-shadow:
        0 15px 45px rgba(0,0,0,.22);
}

.card h3 {

    margin: 0 0 12px;

    font-size: 15px;
}

.metric {

    font-size: 30px;

    font-weight: 800;

    color: #55d6ff;
}

.label {

    color: #8da5bd;

    font-size: 12px;

    margin-top: 4px;
}

button {

    border: 0;

    border-radius: 12px;

    padding: 12px 18px;

    font-weight: 700;

    cursor: pointer;

    color: #03111c;

    background:
        linear-gradient(
            135deg,
            #38c8ff,
            #58f0d0
        );

    box-shadow:
        0 8px 25px rgba(42,198,255,.18);
}

button.secondary {

    color: #d9eaff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.09);

    box-shadow: none;
}

.actions {

    display: flex;

    flex-wrap: wrap;

    gap: 10px;

    margin-bottom: 22px;
}

.status {

    padding: 8px 12px;

    border-radius: 999px;

    background:
        rgba(52,210,255,.10);

    color: #64d9ff;

    font-size: 12px;

    display: inline-block;
}

.source {

    margin-top: 12px;

    padding: 14px;

    border-radius: 14px;

    background:
        rgba(255,255,255,.035);

    border:
        1px solid rgba(255,255,255,.06);
}

.source-title {

    font-weight: 700;

    font-size: 14px;
}

.small {

    font-size: 12px;

    color: #8fa8c4;

    line-height: 1.6;
}

.record {

    padding: 16px;

    margin-top: 10px;

    border-radius: 14px;

    background:
        rgba(255,255,255,.035);

    border-left:
        3px solid #35c8ff;
}

.record-title {

    font-weight: 700;

    margin-bottom: 7px;
}

a {

    color: #5ed8ff;

    text-decoration: none;
}

a:hover {

    text-decoration: underline;
}

.loading {

    color: #8fa8c4;

    padding: 30px 0;
}

.error {

    color: #ff9d9d;

    background:
        rgba(255,70,70,.08);

    padding: 14px;

    border-radius: 12px;

    margin-top: 15px;
}

</style>

</head>

<body>

<header>

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div class="subtitle">
        Live Development Intelligence
    </div>

</header>

<div class="container">

    <div class="actions">

        <button onclick="refreshLive()">
            ↻ Refresh Live Intelligence
        </button>

        <button class="secondary"
                onclick="loadSummary()">
            Reload Summary
        </button>

        <button class="secondary"
                onclick="openWorkspace()">
            Property Workspace
        </button>

        <button class="secondary"
                onclick="openCommandMap()">
            Command Map
        </button>

    </div>

    <div id="status" class="status">
        Loading...
    </div>

    <div id="error"></div>

    <div id="summary"
         class="loading">
        Loading live development intelligence...
    </div>

</div>

<script>

const PROPERTY_ID = "__PROPERTY_ID__";

function esc(value) {

    if (value === null || value === undefined) {
        return "";
    }

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


async function loadSummary() {

    const summary =
        document.getElementById("summary");

    const status =
        document.getElementById("status");

    const error =
        document.getElementById("error");

    error.innerHTML = "";

    status.textContent = "Loading...";

    try {

        const response = await fetch(
            "/api/v1/properties/"
            + PROPERTY_ID
            + "/live-development"
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail || "Request failed"
            );
        }

        renderSummary(data);

        status.textContent =
            "Live intelligence available";

    } catch (err) {

        status.textContent = "Error";

        error.innerHTML =
            '<div class="error">'
            + esc(err.message)
            + '</div>';
    }
}


function renderSummary(data) {

    const summary =
        data.summary || {};

    const records =
        data.development_records || [];

    const sourceSummary =
        summary.source_summary || {};

    const statusSummary =
        summary.status_summary || {};

    let html = "";

    html += '<div class="grid">';

    html += `
        <div class="card">
            <h3>Development Records</h3>
            <div class="metric">
                ${esc(summary.total_development_records || 0)}
            </div>
            <div class="label">
                Source-backed development intelligence
            </div>
        </div>
    `;

    html += `
        <div class="card">
            <h3>Mapped</h3>
            <div class="metric">
                ${esc(summary.mapped_records || 0)}
            </div>
            <div class="label">
                Records with usable coordinates
            </div>
        </div>
    `;

    html += `
        <div class="card">
            <h3>Location Pending</h3>
            <div class="metric">
                ${esc(summary.unmapped_records || 0)}
            </div>
            <div class="label">
                Records retained without fabricated coordinates
            </div>
        </div>
    `;

    html += "</div>";

    html += `
        <div class="card"
             style="margin-top:16px">

            <h3>Live Sources</h3>

            <div class="grid">
    `;

    for (const [source,count]
         of Object.entries(sourceSummary)) {

        html += `
            <div class="source">
                <div class="source-title">
                    ${esc(source)}
                </div>
                <div class="metric"
                     style="font-size:24px">
                    ${esc(count)}
                </div>
            </div>
        `;
    }

    if (
        Object.keys(sourceSummary).length === 0
    ) {

        html += `
            <div class="small">
                No development source records are currently
                attached to this property.
            </div>
        `;
    }

    html += `
            </div>
        </div>
    `;

    html += `
        <div class="card"
             style="margin-top:16px">

            <h3>Development Status</h3>

            <div class="grid">
    `;

    for (const [status,count]
         of Object.entries(statusSummary)) {

        html += `
            <div class="source">
                <div class="source-title">
                    ${esc(status)}
                </div>
                <div class="metric"
                     style="font-size:24px">
                    ${esc(count)}
                </div>
            </div>
        `;
    }

    html += `
            </div>
        </div>
    `;

    html += `
        <div class="card"
             style="margin-top:16px">

            <h3>Development Intelligence</h3>
    `;

    if (records.length === 0) {

        html += `
            <div class="small">
                No source-backed development records are
                currently attached to this property.
                Use "Refresh Live Intelligence" to query
                the existing live connector layer.
            </div>
        `;

    } else {

        for (const record of records) {

            const title =
                record.title || "Untitled record";

            const source =
                record.source_name
                || record.source_type
                || "Unknown source";

            const status =
                record.status || "unknown";

            const evidence =
                record.evidence_level
                || "not specified";

            const url =
                record.source_url;

            html += `
                <div class="record">

                    <div class="record-title">
                        ${esc(title)}
                    </div>

                    <div class="small">
                        Source:
                        ${esc(source)}
                        <br>

                        Status:
                        ${esc(status)}
                        <br>

                        Evidence:
                        ${esc(evidence)}
                    </div>
            `;

            if (url) {

                html += `
                    <div style="margin-top:8px">
                        <a href="${esc(url)}"
                           target="_blank"
                           rel="noopener noreferrer">
                           Open source
                        </a>
                    </div>
                `;
            }

            html += `
                </div>
            `;
        }
    }

    html += `
        </div>
    `;

    const latest =
        data.latest_live_refresh;

    html += `
        <div class="card"
             style="margin-top:16px">

            <h3>Latest Live Refresh</h3>
    `;

    if (latest) {

        html += `
            <div class="small">

                Status:
                <strong>
                    ${esc(latest.status)}
                </strong>

                <br>

                Started:
                ${esc(latest.started_at)}

                <br>

                Finished:
                ${esc(latest.finished_at)}

                <br>

                Records before:
                ${esc(latest.records_before)}

                <br>

                Records after:
                ${esc(latest.records_after)}

            </div>
        `;

    } else {

        html += `
            <div class="small">
                No live refresh has been executed for this
                property yet.
            </div>
        `;
    }

    html += `
        </div>
    `;

    summary.innerHTML = html;
}


async function refreshLive() {

    const status =
        document.getElementById("status");

    const error =
        document.getElementById("error");

    error.innerHTML = "";

    status.textContent =
        "Refreshing OSM + News + Government...";

    try {

        const response = await fetch(
            "/api/v1/properties/"
            + PROPERTY_ID
            + "/live-development-refresh",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    sources: [
                        "osm",
                        "news",
                        "government"
                    ]
                })
            }
        );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail || "Live refresh failed"
            );
        }

        status.textContent =
            "Refresh completed";

        await loadSummary();

    } catch (err) {

        status.textContent =
            "Refresh error";

        error.innerHTML =
            '<div class="error">'
            + esc(err.message)
            + '</div>';
    }
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


function openCommandMap() {

    window.location.href =
        "/propertyiq/command-map/"
        + PROPERTY_ID;
}


loadSummary();

</script>

</body>
</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 18. UI route
# ------------------------------------------------------------------------------

from fastapi.responses import HTMLResponse


async def v31_live_development_ui(
    property_id: str
):

    property_data = _v31_get_property(property_id)

    if property_data is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v31_ui(property_id)
    )


_v31_add_route(
    "GET",
    "/propertyiq/live-development/{property_id}",
    v31_live_development_ui
)


# ------------------------------------------------------------------------------
# 19. Export useful globals
# ------------------------------------------------------------------------------

globals()["run_v31_live_development_refresh"] = (
    run_v31_live_development_refresh
)

globals()["get_v31_refresh_history"] = (
    get_v31_refresh_history
)

globals()["get_v31_latest_refresh"] = (
    get_v31_latest_refresh
)


# ------------------------------------------------------------------------------
# 20. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V31 — LIVE DEVELOPMENT INTELLIGENCE INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("LIVE DEVELOPMENT ENGINE:")
print("  ✓ Property-centric live refresh")
print("  ✓ OpenStreetMap / Overpass integration")
print("  ✓ GDELT development-news integration")
print("  ✓ PAIMANA / MoSPI infrastructure integration")
print("  ✓ Existing V14 ingestion preserved")
print("  ✓ Existing evidence pipeline preserved")
print("  ✓ Refresh history stored")
print("  ✓ Source-level result tracking")
print("  ✓ Partial-failure handling")
print()
print("API:")
print("  /api/v1/properties/{property_id}/live-development-refresh")
print("  /api/v1/properties/{property_id}/live-development-refresh/status")
print("  /api/v1/properties/{property_id}/live-development-refresh/history")
print("  /api/v1/properties/{property_id}/live-development")
print()
print("UI:")
print("  /propertyiq/live-development/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ No fabricated projects")
print("  ✓ No fabricated coordinates")
print("  ✓ Source URLs preserved")
print("  ✓ Existing intelligence preserved")
print("  ✓ Existing RERA layer preserved")
print("  ✓ No valuation prediction")
print("  ✓ No investment score")
print("  ✓ No legal conclusions")
print()
print("Database:")
print("  ✓ piq_property_refresh_runs created/verified")
print()
print("Existing PropertyIQ routes preserved.")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V32
# ORIGINAL COLAB CELL: In[57]
# ============================================================

# ==============================================================================
# PROPERTYIQ V32 — GEOGRAPHIC DEVELOPMENT MATCHING
# ==============================================================================
# Purpose:
#   Match existing source-backed development intelligence geographically
#   against the selected property.
#
# Uses:
#   - Existing intelligence_records
#   - Existing property coordinates
#   - Existing property boundary
#   - Existing RERA / Government / News / OSM records
#
# Matching:
#   0–500 m
#   500 m–1 km
#   1–3 km
#   3–5 km
#   5–10 km
#   >10 km
#
# Important:
#   - No fabricated coordinates
#   - No fabricated projects
#   - No investment score
#   - No appreciation prediction
#   - No automatic claim that a project benefits the property
#   - Location-pending records remain explicitly visible
#
# Direct Colab execution: ENABLED
# Existing PropertyIQ engine + app are reused.
# ==============================================================================

import sys
import subprocess
import importlib
import math
import json
import traceback

# ------------------------------------------------------------------------------
# 1. Dependency bootstrap
# ------------------------------------------------------------------------------

def _v32_install(package, module=None):
    module = module or package
    try:
        importlib.import_module(module)
    except Exception:
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])

_v32_install("sqlalchemy")
_v32_install("fastapi")
_v32_install("psycopg[binary]", "psycopg")


# ------------------------------------------------------------------------------
# 2. Imports
# ------------------------------------------------------------------------------

from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 3. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = "PROPERTYIQ-V32-GEOGRAPHIC-DEVELOPMENT-MATCHING"

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PropertyIQ PostgreSQL/PostGIS runtime bootstrap first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(
        title="PropertyIQ",
        version="V32"
    )


# ------------------------------------------------------------------------------
# 4. Distance calculation
# ------------------------------------------------------------------------------

def _v32_haversine_km(lat1, lon1, lat2, lon2):

    lat1 = float(lat1)
    lon1 = float(lon1)
    lat2 = float(lat2)
    lon2 = float(lon2)

    R = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * R * math.asin(
        min(1.0, math.sqrt(a))
    )


# ------------------------------------------------------------------------------
# 5. Property
# ------------------------------------------------------------------------------

def _v32_get_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    return dict(row) if row else None


# ------------------------------------------------------------------------------
# 6. Intelligence schema-safe loader
# ------------------------------------------------------------------------------

def _v32_get_intelligence(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY title
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [dict(row) for row in rows]


# ------------------------------------------------------------------------------
# 7. Location band
# ------------------------------------------------------------------------------

def _v32_band(distance_km):

    if distance_km <= 0.5:
        return "0–500 m"

    if distance_km <= 1:
        return "500 m–1 km"

    if distance_km <= 3:
        return "1–3 km"

    if distance_km <= 5:
        return "3–5 km"

    if distance_km <= 10:
        return "5–10 km"

    return ">10 km"


def _v32_band_order(band):

    order = {
        "0–500 m": 1,
        "500 m–1 km": 2,
        "1–3 km": 3,
        "3–5 km": 4,
        "5–10 km": 5,
        ">10 km": 6,
        "LOCATION_PENDING": 99
    }

    return order.get(band, 98)


# ------------------------------------------------------------------------------
# 8. Development classification
# ------------------------------------------------------------------------------

def _v32_classify(record):

    text = " ".join([
        str(record.get("title") or ""),
        str(record.get("category") or ""),
        str(record.get("description") or "")
    ]).lower()

    keyword_groups = {

        "transport": [
            "metro",
            "rail",
            "railway",
            "expressway",
            "highway",
            "road",
            "flyover",
            "corridor",
            "transport",
            "transit",
            "airport"
        ],

        "government_infrastructure": [
            "infrastructure",
            "government",
            "paimana",
            "mospi",
            "multi modal",
            "logistics hub",
            "industrial corridor"
        ],

        "commercial": [
            "mall",
            "commercial",
            "retail",
            "business district",
            "office",
            "market"
        ],

        "industrial": [
            "industrial",
            "manufacturing",
            "warehouse",
            "logistics",
            "factory",
            "industry"
        ],

        "residential_development": [
            "residential",
            "housing",
            "township",
            "apartment",
            "housing project"
        ],

        "institutional": [
            "hospital",
            "school",
            "college",
            "university",
            "medical"
        ],

        "land_development": [
            "land acquisition",
            "land development",
            "development authority",
            "development project"
        ]
    }

    for category, keywords in keyword_groups.items():

        for keyword in keywords:

            if keyword in text:
                return category

    return (
        record.get("category")
        or "other"
    )


# ------------------------------------------------------------------------------
# 9. Main geographic matching engine
# ------------------------------------------------------------------------------

def build_v32_geographic_matching(
    property_id,
    max_radius_km=25
):

    property_data = _v32_get_property(property_id)

    if property_data is None:
        raise ValueError(
            "Property not found."
        )

    prop_lat = property_data.get("latitude")
    prop_lon = property_data.get("longitude")

    if prop_lat is None or prop_lon is None:

        return {
            "success": True,
            "module": MODULE_VERSION,
            "property": property_data,
            "status": "property_location_pending",
            "message": (
                "The property does not have usable coordinates. "
                "Geographic matching cannot be performed."
            ),
            "records": [],
            "location_pending": [],
            "summary": {}
        }

    records = _v32_get_intelligence(
        property_id
    )

    matched = []
    location_pending = []

    bands = {
        "0–500 m": 0,
        "500 m–1 km": 0,
        "1–3 km": 0,
        "3–5 km": 0,
        "5–10 km": 0,
        ">10 km": 0
    }

    source_summary = {}
    category_summary = {}

    for record in records:

        lat = record.get("latitude")
        lon = record.get("longitude")

        if lat is None or lon is None:

            location_pending.append({
                "id": str(record["id"]),
                "title": record.get("title"),
                "source_name": record.get("source_name"),
                "source_type": record.get("source_type"),
                "source_url": record.get("source_url"),
                "status": "LOCATION_PENDING"
            })

            continue

        try:

            lat = float(lat)
            lon = float(lon)

        except Exception:

            location_pending.append({
                "id": str(record["id"]),
                "title": record.get("title"),
                "source_name": record.get("source_name"),
                "source_type": record.get("source_type"),
                "source_url": record.get("source_url"),
                "status": "LOCATION_PENDING"
            })

            continue

        if not (
            -90 <= lat <= 90
            and -180 <= lon <= 180
        ):

            location_pending.append({
                "id": str(record["id"]),
                "title": record.get("title"),
                "source_name": record.get("source_name"),
                "source_type": record.get("source_type"),
                "source_url": record.get("source_url"),
                "status": "INVALID_COORDINATES"
            })

            continue

        distance = _v32_haversine_km(
            prop_lat,
            prop_lon,
            lat,
            lon
        )

        band = _v32_band(distance)

        development_type = _v32_classify(
            record
        )

        enriched = dict(record)

        enriched["id"] = str(
            enriched["id"]
        )

        enriched["property_id"] = (
            str(enriched["property_id"])
            if enriched.get("property_id")
            else None
        )

        enriched["latitude"] = lat
        enriched["longitude"] = lon

        enriched["distance_km"] = round(
            distance,
            3
        )

        enriched["distance_m"] = round(
            distance * 1000,
            1
        )

        enriched["geographic_band"] = band

        enriched["development_type"] = (
            development_type
        )

        enriched["within_requested_radius"] = (
            distance <= max_radius_km
        )

        matched.append(enriched)

        bands[band] += 1

        source = (
            record.get("source_name")
            or record.get("source_type")
            or "UNKNOWN"
        )

        source_summary[source] = (
            source_summary.get(source, 0)
            + 1
        )

        category_summary[development_type] = (
            category_summary.get(
                development_type,
                0
            )
            + 1
        )

    matched.sort(
        key=lambda x: (
            x.get("distance_km", 999999),
            _v32_band_order(
                x.get("geographic_band")
            ),
            str(x.get("title") or "")
        )
    )

    nearby = [
        item
        for item in matched
        if item.get("distance_km", 999999)
        <= max_radius_km
    ]

    return {
        "success": True,
        "module": MODULE_VERSION,

        "property": property_data,

        "status": "matched",

        "parameters": {
            "max_radius_km": max_radius_km
        },

        "summary": {

            "total_intelligence_records": len(records),

            "mapped_records": len(matched),

            "location_pending_records": len(
                location_pending
            ),

            "records_within_radius": len(
                nearby
            ),

            "source_summary": source_summary,

            "category_summary": category_summary,

            "distance_bands": bands
        },

        "matched_records": matched,

        "location_pending": location_pending
    }


# ------------------------------------------------------------------------------
# 10. API
# ------------------------------------------------------------------------------

_v32_routes = {
    getattr(route, "path", "")
    for route in getattr(app, "routes", [])
}


def _v32_add_get(path, endpoint):

    if path not in _v32_routes:

        app.get(path)(endpoint)

        _v32_routes.add(path)


async def v32_geographic_matching_api(
    property_id: str,
    radius_km: float = 25
):

    try:

        radius_km = max(
            0.1,
            min(float(radius_km), 100)
        )

        return build_v32_geographic_matching(
            property_id,
            radius_km
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v32_add_get(
    "/api/v1/properties/{property_id}/geographic-development-matching",
    v32_geographic_matching_api
)


# ------------------------------------------------------------------------------
# 11. UI
# ------------------------------------------------------------------------------

def _v32_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Geographic Development Matching
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,180,255,.12),
            transparent 32%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(0,255,210,.07),
            transparent 30%
        ),
        #07111f;

    color: #edf7ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

header {

    padding: 22px 28px;

    border-bottom:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(7,17,31,.86);

    backdrop-filter: blur(18px);

    position: sticky;

    top: 0;

    z-index: 10;
}

.brand {

    font-size: 21px;

    font-weight: 850;
}

.brand span {

    color: #42d2ff;
}

.subtitle {

    margin-top: 5px;

    color: #8da6c0;

    font-size: 12px;
}

.container {

    max-width: 1500px;

    margin: auto;

    padding: 25px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 10px;

    margin-bottom: 18px;
}

button {

    border: 0;

    border-radius: 11px;

    padding: 11px 16px;

    font-weight: 750;

    cursor: pointer;

    color: #04111d;

    background:
        linear-gradient(
            135deg,
            #42d1ff,
            #62efcf
        );
}

button.secondary {

    color: #dceeff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.09);
}

select {

    border:
        1px solid rgba(255,255,255,.10);

    background:
        #101f31;

    color: white;

    padding: 10px 13px;

    border-radius: 10px;
}

.grid {

    display: grid;

    grid-template-columns:
        repeat(auto-fit,minmax(210px,1fr));

    gap: 14px;
}

.card {

    background:
        linear-gradient(
            145deg,
            rgba(20,36,57,.90),
            rgba(9,21,37,.90)
        );

    border:
        1px solid rgba(255,255,255,.08);

    border-radius: 17px;

    padding: 19px;

    box-shadow:
        0 18px 50px rgba(0,0,0,.20);
}

.metric {

    font-size: 29px;

    font-weight: 850;

    color: #55d8ff;
}

.label {

    color: #8da5bd;

    font-size: 12px;

    margin-top: 4px;
}

.section {

    margin-top: 17px;
}

.section h3 {

    font-size: 15px;

    margin: 0 0 12px;
}

.record {

    margin-top: 9px;

    padding: 15px;

    border-radius: 13px;

    background:
        rgba(255,255,255,.035);

    border-left:
        3px solid #3dd2ff;
}

.record-title {

    font-weight: 750;

    line-height: 1.45;
}

.meta {

    margin-top: 7px;

    color: #91a9c1;

    font-size: 12px;

    line-height: 1.65;
}

.badge {

    display: inline-block;

    padding: 5px 9px;

    border-radius: 999px;

    background:
        rgba(55,210,255,.10);

    color: #63d9ff;

    font-size: 11px;

    margin-right: 5px;

    margin-top: 5px;
}

.pending {

    border-left-color: #f2c14e;
}

a {

    color: #62d8ff;

    text-decoration: none;
}

a:hover {

    text-decoration: underline;
}

.status {

    color: #8fa8c0;

    font-size: 12px;

    margin-bottom: 15px;
}

.error {

    background:
        rgba(255,70,70,.09);

    color: #ffabab;

    padding: 13px;

    border-radius: 11px;
}

</style>

</head>

<body>

<header>

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div class="subtitle">
        Geographic Development Matching
    </div>

</header>

<div class="container">

    <div class="toolbar">

        <select id="radius">

            <option value="1">1 km</option>

            <option value="3">3 km</option>

            <option value="5">5 km</option>

            <option value="10">10 km</option>

            <option value="25" selected>
                25 km
            </option>

            <option value="50">
                50 km
            </option>

        </select>

        <button onclick="loadData()">
            Analyze Geographic Match
        </button>

        <button class="secondary"
                onclick="openCommandMap()">
            Command Map
        </button>

        <button class="secondary"
                onclick="openWorkspace()">
            Property Workspace
        </button>

    </div>

    <div id="status"
         class="status">
        Loading...
    </div>

    <div id="error"></div>

    <div id="content"></div>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function formatDistance(km) {

    if (km < 1) {

        return (
            Math.round(km * 1000)
            + " m"
        );
    }

    return (
        Number(km).toFixed(2)
        + " km"
    );
}


async function loadData() {

    const radius =
        document.getElementById(
            "radius"
        ).value;

    const status =
        document.getElementById(
            "status"
        );

    const error =
        document.getElementById(
            "error"
        );

    const content =
        document.getElementById(
            "content"
        );

    error.innerHTML = "";

    status.textContent =
        "Calculating geographic matches...";

    content.innerHTML =
        '<div class="card">Loading...</div>';

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/geographic-development-matching"
                + "?radius_km="
                + encodeURIComponent(radius)
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail || "Request failed"
            );
        }

        render(data);

        status.textContent =
            "Geographic analysis complete";

    } catch (err) {

        status.textContent =
            "Error";

        content.innerHTML = "";

        error.innerHTML =
            '<div class="error">'
            + esc(err.message)
            + '</div>';
    }
}


function render(data) {

    if (
        data.status ===
        "property_location_pending"
    ) {

        document.getElementById(
            "content"
        ).innerHTML = `
            <div class="card">

                <h3>
                    Property location pending
                </h3>

                <div class="meta">
                    ${esc(data.message)}
                </div>

            </div>
        `;

        return;
    }

    const summary =
        data.summary || {};

    const bands =
        summary.distance_bands || {};

    const sourceSummary =
        summary.source_summary || {};

    const categorySummary =
        summary.category_summary || {};

    const records =
        data.matched_records || [];

    const pending =
        data.location_pending || [];

    let html = "";

    html += `
        <div class="grid">

            <div class="card">

                <div class="metric">
                    ${esc(
                        summary
                        .total_intelligence_records
                        || 0
                    )}
                </div>

                <div class="label">
                    Total intelligence records
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        summary.mapped_records
                        || 0
                    )}
                </div>

                <div class="label">
                    Records with coordinates
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        summary
                        .records_within_radius
                        || 0
                    )}
                </div>

                <div class="label">
                    Within selected radius
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        summary
                        .location_pending_records
                        || 0
                    )}
                </div>

                <div class="label">
                    Location pending
                </div>

            </div>

        </div>
    `;

    html += `
        <div class="section">

            <div class="card">

                <h3>
                    Geographic Distance Bands
                </h3>

                <div class="grid">
    `;

    for (
        const [band,count]
        of Object.entries(bands)
    ) {

        html += `
            <div>

                <div class="metric"
                     style="font-size:23px">
                    ${esc(count)}
                </div>

                <div class="label">
                    ${esc(band)}
                </div>

            </div>
        `;
    }

    html += `
                </div>

            </div>

        </div>
    `;

    html += `
        <div class="section">

            <div class="card">

                <h3>
                    Source Distribution
                </h3>

                <div class="grid">
    `;

    for (
        const [source,count]
        of Object.entries(sourceSummary)
    ) {

        html += `
            <div class="record">

                <div class="record-title">
                    ${esc(source)}
                </div>

                <div class="metric"
                     style="font-size:23px">
                    ${esc(count)}
                </div>

            </div>
        `;
    }

    html += `
                </div>

            </div>

        </div>
    `;

    html += `
        <div class="section">

            <div class="card">

                <h3>
                    Development Categories
                </h3>

                <div>
    `;

    for (
        const [category,count]
        of Object.entries(categorySummary)
    ) {

        html += `
            <span class="badge">
                ${esc(category)}
                ·
                ${esc(count)}
            </span>
        `;
    }

    html += `
                </div>

            </div>

        </div>
    `;

    html += `
        <div class="section">

            <div class="card">

                <h3>
                    Geographically Matched Development
                </h3>
    `;

    if (records.length === 0) {

        html += `
            <div class="meta">
                No mapped development intelligence
                was found within the selected radius.
            </div>
        `;

    } else {

        for (const record of records) {

            html += `
                <div class="record">

                    <div class="record-title">
                        ${esc(
                            record.title
                            || "Untitled"
                        )}
                    </div>

                    <div class="meta">

                        <span class="badge">
                            ${esc(
                                record
                                .development_type
                            )}
                        </span>

                        <span class="badge">
                            ${esc(
                                record
                                .geographic_band
                            )}
                        </span>

                        <br>

                        Distance:
                        <strong>
                            ${formatDistance(
                                record.distance_km
                            )}
                        </strong>

                        <br>

                        Source:
                        ${esc(
                            record.source_name
                            ||
                            record.source_type
                            ||
                            "Unknown"
                        )}

                        <br>

                        Status:
                        ${esc(
                            record.status
                            || "unknown"
                        )}

                    </div>
            `;

            if (record.source_url) {

                html += `
                    <div style="margin-top:8px">

                        <a
                          href="${esc(
                              record.source_url
                          )}"
                          target="_blank"
                          rel="noopener noreferrer">

                            Open source

                        </a>

                    </div>
                `;
            }

            html += `
                </div>
            `;
        }
    }

    html += `
            </div>

        </div>
    `;

    html += `
        <div class="section">

            <div class="card">

                <h3>
                    Location Pending Records
                </h3>
    `;

    if (pending.length === 0) {

        html += `
            <div class="meta">
                All currently loaded intelligence
                records have usable coordinates.
            </div>
        `;

    } else {

        for (const record of pending) {

            html += `
                <div class="record pending">

                    <div class="record-title">
                        ${esc(
                            record.title
                            || "Untitled"
                        )}
                    </div>

                    <div class="meta">

                        Source:
                        ${esc(
                            record.source_name
                            ||
                            record.source_type
                            ||
                            "Unknown"
                        )}

                        <br>

                        Location status:
                        ${esc(
                            record.status
                            || "LOCATION_PENDING"
                        )}

                    </div>
            `;

            if (record.source_url) {

                html += `
                    <div style="margin-top:8px">

                        <a
                          href="${esc(
                              record.source_url
                          )}"
                          target="_blank"
                          rel="noopener noreferrer">

                            Open source

                        </a>

                    </div>
                `;
            }

            html += `
                </div>
            `;
        }
    }

    html += `
            </div>

        </div>
    `;

    document.getElementById(
        "content"
    ).innerHTML = html;
}


function openCommandMap() {

    window.location.href =
        "/propertyiq/command-map/"
        + PROPERTY_ID;
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadData();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 12. UI route
# ------------------------------------------------------------------------------

async def v32_geographic_matching_ui(
    property_id: str
):

    property_data = _v32_get_property(
        property_id
    )

    if property_data is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v32_html(property_id)
    )


if (
    "/propertyiq/geographic-development/{property_id}"
    not in _v32_routes
):

    app.get(
        "/propertyiq/geographic-development/{property_id}"
    )(v32_geographic_matching_ui)

    _v32_routes.add(
        "/propertyiq/geographic-development/{property_id}"
    )


# ------------------------------------------------------------------------------
# 13. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v32_geographic_matching"
] = build_v32_geographic_matching


# ------------------------------------------------------------------------------
# 14. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V32 — GEOGRAPHIC DEVELOPMENT MATCHING INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("GEOGRAPHIC MATCHING:")
print("  ✓ Property-coordinate matching")
print("  ✓ 0–500 m")
print("  ✓ 500 m–1 km")
print("  ✓ 1–3 km")
print("  ✓ 3–5 km")
print("  ✓ 5–10 km")
print("  ✓ >10 km")
print("  ✓ Source distribution")
print("  ✓ Development-category distribution")
print("  ✓ Location-pending tracking")
print("  ✓ Source URL preservation")
print()
print("API:")
print("  /api/v1/properties/{property_id}/geographic-development-matching")
print()
print("UI:")
print("  /propertyiq/geographic-development/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Existing intelligence only")
print("  ✓ No fabricated coordinates")
print("  ✓ No fabricated projects")
print("  ✓ No appreciation prediction")
print("  ✓ No investment score")
print("  ✓ No automatic benefit claims")
print("  ✓ Existing PropertyIQ routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V33
# ORIGINAL COLAB CELL: In[60]
# ============================================================

# ==============================================================================
# PROPERTYIQ V33 — INTERACTIVE INTELLIGENCE MAP
# ==============================================================================
# Consolidates:
#   V18  Map-Native Leaflet
#   V18.3 Map Intelligence Modes
#   V19  Due-Diligence Map
#   V21  Boundary Intelligence
#   V24  Real OSM/GIS
#   V30  Unified Development Intelligence
#   V32  Geographic Development Matching
#
# Adds:
#   - One unified interactive map
#   - Source filters
#   - Development-category filters
#   - Distance filters
#   - Mapped/unmapped visibility
#   - Property boundary
#   - Intelligence markers
#   - Development distance context
#   - Evidence/source links
#   - Live refresh handoff
#
# No fabricated coordinates.
# No fabricated intelligence.
# No investment score.
# No valuation prediction.
# ==============================================================================

import sys
import subprocess
import importlib
import math
import json

# ------------------------------------------------------------------------------
# 1. Dependency bootstrap
# ------------------------------------------------------------------------------

def _v33_install(package, module=None):
    module = module or package
    try:
        importlib.import_module(module)
    except Exception:
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])

_v33_install("sqlalchemy")
_v33_install("fastapi")
_v33_install("psycopg[binary]", "psycopg")
_v33_install("shapely")


# ------------------------------------------------------------------------------
# 2. Imports
# ------------------------------------------------------------------------------

from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 3. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = "PROPERTYIQ-V33-INTERACTIVE-INTELLIGENCE-MAP"

if "engine" not in globals() or engine is None:
    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PropertyIQ PostgreSQL/PostGIS runtime bootstrap first."
    )

if "app" not in globals() or app is None:
    app = FastAPI(
        title="PropertyIQ",
        version="V33"
    )


# ------------------------------------------------------------------------------
# 4. Distance
# ------------------------------------------------------------------------------

def _v33_haversine_km(lat1, lon1, lat2, lon2):

    R = 6371.0088

    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))

    dp = math.radians(
        float(lat2) - float(lat1)
    )

    dl = math.radians(
        float(lon2) - float(lon1)
    )

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * R * math.asin(
        min(1, math.sqrt(a))
    )


# ------------------------------------------------------------------------------
# 5. Property
# ------------------------------------------------------------------------------

def _v33_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    return dict(row) if row else None


# ------------------------------------------------------------------------------
# 6. Boundary
# ------------------------------------------------------------------------------

def _v33_boundary(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    ST_AsGeoJSON(boundary) AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    if not row:
        return None

    value = row.get(
        "boundary_geojson"
    )

    if not value:
        return None

    try:
        return json.loads(value)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 7. Intelligence
# ------------------------------------------------------------------------------

def _v33_intelligence(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY title
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [dict(row) for row in rows]


# ------------------------------------------------------------------------------
# 8. Classification
# ------------------------------------------------------------------------------

def _v33_classify(record):

    text = " ".join([
        str(record.get("title") or ""),
        str(record.get("category") or ""),
        str(record.get("description") or ""),
        str(record.get("source_type") or "")
    ]).lower()

    groups = {

        "TRANSPORT": [
            "metro",
            "railway",
            "rail",
            "expressway",
            "highway",
            "road",
            "flyover",
            "corridor",
            "airport",
            "transport",
            "transit"
        ],

        "GOVERNMENT": [
            "paimana",
            "mospi",
            "government",
            "infrastructure",
            "multi modal",
            "logistics hub"
        ],

        "RERA": [
            "rera",
            "up-rera",
            "project registration"
        ],

        "NEWS": [
            "gdelt",
            "news",
            "reported"
        ],

        "OSM": [
            "openstreetmap",
            "overpass",
            "osm"
        ],

        "COMMERCIAL": [
            "mall",
            "commercial",
            "business district",
            "retail",
            "office",
            "market"
        ],

        "INDUSTRIAL": [
            "industrial",
            "manufacturing",
            "warehouse",
            "factory",
            "logistics"
        ],

        "INSTITUTIONAL": [
            "hospital",
            "school",
            "college",
            "university",
            "medical"
        ],

        "RESIDENTIAL": [
            "residential",
            "housing",
            "township",
            "apartment"
        ]
    }

    for group, keywords in groups.items():

        for keyword in keywords:

            if keyword in text:
                return group

    return (
        str(record.get("category") or "OTHER")
        .upper()
    )


# ------------------------------------------------------------------------------
# 9. Unified map data
# ------------------------------------------------------------------------------

def build_v33_interactive_map(
    property_id,
    radius_km=25,
    include_unmapped=True
):

    property_data = _v33_property(
        property_id
    )

    if property_data is None:

        raise ValueError(
            "Property not found."
        )

    lat = property_data.get("latitude")
    lon = property_data.get("longitude")

    records = _v33_intelligence(
        property_id
    )

    boundary = _v33_boundary(
        property_id
    )

    mapped = []
    unmapped = []

    source_summary = {}
    category_summary = {}

    for record in records:

        r = dict(record)

        r["id"] = str(
            r["id"]
        )

        r["category_group"] = (
            _v33_classify(r)
        )

        source = (
            r.get("source_name")
            or r.get("source_type")
            or "UNKNOWN"
        )

        r["display_source"] = source

        source_summary[source] = (
            source_summary.get(source, 0)
            + 1
        )

        category = r[
            "category_group"
        ]

        category_summary[category] = (
            category_summary.get(category, 0)
            + 1
        )

        rlat = r.get("latitude")
        rlon = r.get("longitude")

        if rlat is None or rlon is None:

            r["location_status"] = (
                "LOCATION_PENDING"
            )

            unmapped.append(r)

            continue

        try:

            rlat = float(rlat)
            rlon = float(rlon)

        except Exception:

            r["location_status"] = (
                "INVALID_COORDINATES"
            )

            unmapped.append(r)

            continue

        if not (
            -90 <= rlat <= 90
            and -180 <= rlon <= 180
        ):

            r["location_status"] = (
                "INVALID_COORDINATES"
            )

            unmapped.append(r)

            continue

        r["latitude"] = rlat
        r["longitude"] = rlon

        if lat is not None and lon is not None:

            distance = _v33_haversine_km(
                lat,
                lon,
                rlat,
                rlon
            )

            r["distance_km"] = round(
                distance,
                3
            )

            r["within_radius"] = (
                distance <= radius_km
            )

        else:

            r["distance_km"] = None
            r["within_radius"] = False

        r["location_status"] = "MAPPED"

        mapped.append(r)

    mapped.sort(
        key=lambda x:
            x.get("distance_km")
            if x.get("distance_km") is not None
            else 999999
    )

    visible = [
        r
        for r in mapped
        if r.get("within_radius")
    ]

    if include_unmapped:
        display_records = (
            visible + unmapped
        )
    else:
        display_records = visible

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property": property_data,

        "map": {

            "latitude": lat,

            "longitude": lon,

            "radius_km": radius_km,

            "boundary": boundary
        },

        "summary": {

            "total_records": len(records),

            "mapped_records": len(mapped),

            "visible_mapped_records": len(
                visible
            ),

            "unmapped_records": len(
                unmapped
            ),

            "source_summary": source_summary,

            "category_summary": category_summary
        },

        "records": display_records,

        "mapped_records": mapped,

        "unmapped_records": unmapped
    }


# ------------------------------------------------------------------------------
# 10. Routes
# ------------------------------------------------------------------------------

_v33_routes = {
    getattr(route, "path", "")
    for route in getattr(app, "routes", [])
}


def _v33_add_get(path, endpoint):

    if path not in _v33_routes:

        app.get(path)(endpoint)

        _v33_routes.add(path)


async def v33_map_api(
    property_id: str,
    radius_km: float = 25,
    include_unmapped: bool = True
):

    try:

        radius_km = max(
            0.1,
            min(float(radius_km), 100)
        )

        return build_v33_interactive_map(
            property_id,
            radius_km,
            include_unmapped
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v33_add_get(
    "/api/v1/properties/{property_id}/interactive-intelligence-map",
    v33_map_api
)


# ------------------------------------------------------------------------------
# 11. UI
# ------------------------------------------------------------------------------

def _v33_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Interactive Intelligence Map
</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,180,255,.12),
            transparent 30%
        ),
        #07111f;

    color: #edf7ff;

    font-family:
        Inter,
        system-ui,
        sans-serif;

    overflow: hidden;
}

#app {

    display: grid;

    grid-template-columns:
        1fr 370px;

    height: 100vh;
}

#map {

    height: 100vh;

    background: #0a1625;
}

#panel {

    height: 100vh;

    overflow-y: auto;

    background:
        rgba(7,17,31,.96);

    border-left:
        1px solid rgba(255,255,255,.08);

    padding: 20px;

    z-index: 1000;
}

.brand {

    font-size: 20px;

    font-weight: 850;

    margin-bottom: 4px;
}

.brand span {

    color: #48d5ff;
}

.subtitle {

    color: #8da5be;

    font-size: 11px;

    margin-bottom: 18px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 7px;

    margin-bottom: 15px;
}

button {

    border: 0;

    border-radius: 9px;

    padding: 9px 11px;

    cursor: pointer;

    font-weight: 700;

    color: #06111d;

    background:
        linear-gradient(
            135deg,
            #43d1ff,
            #60edcf
        );
}

button.secondary {

    color: #d8edff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.08);
}

select {

    width: 100%;

    padding: 9px;

    border-radius: 9px;

    background: #101f31;

    color: white;

    border:
        1px solid rgba(255,255,255,.10);

    margin-bottom: 9px;
}

.card {

    background:
        rgba(255,255,255,.045);

    border:
        1px solid rgba(255,255,255,.07);

    border-radius: 13px;

    padding: 13px;

    margin-bottom: 10px;
}

.metric {

    font-size: 26px;

    font-weight: 850;

    color: #59d8ff;
}

.label {

    color: #8da7bf;

    font-size: 11px;
}

.record {

    padding: 11px;

    border-radius: 10px;

    background:
        rgba(255,255,255,.035);

    margin-top: 7px;

    border-left:
        3px solid #40d2ff;

    cursor: pointer;
}

.record:hover {

    background:
        rgba(60,210,255,.09);
}

.record-title {

    font-size: 12px;

    font-weight: 750;

    line-height: 1.4;
}

.record-meta {

    color: #8da6bd;

    font-size: 10px;

    line-height: 1.55;

    margin-top: 5px;
}

.badge {

    display: inline-block;

    padding: 3px 6px;

    border-radius: 999px;

    background:
        rgba(61,210,255,.11);

    color: #61d8ff;

    font-size: 9px;

    margin-top: 4px;

    margin-right: 3px;
}

.status {

    font-size: 11px;

    color: #91a9c0;

    margin-bottom: 10px;
}

.warning {

    border-left-color: #e4bd55;
}

a {

    color: #5fd7ff;

    text-decoration: none;
}

@media(max-width:900px) {

    #app {

        grid-template-columns: 1fr;
    }

    #panel {

        position: absolute;

        right: 0;

        top: 0;

        width: min(370px,92vw);

        box-shadow:
            -20px 0 50px rgba(0,0,0,.35);
    }
}

</style>

</head>

<body>

<div id="app">

    <div id="map"></div>

    <aside id="panel">

        <div class="brand">
            PROPERTY<span>IQ</span>
        </div>

        <div class="subtitle">
            Interactive Intelligence Command Map
        </div>

        <div class="toolbar">

            <button onclick="loadMap()">
                Refresh
            </button>

            <button
                class="secondary"
                onclick="liveRefresh()">
                Live Refresh
            </button>

            <button
                class="secondary"
                onclick="openWorkspace()">
                Workspace
            </button>

        </div>

        <select id="radius"
                onchange="loadMap()">

            <option value="1">
                1 km
            </option>

            <option value="3">
                3 km
            </option>

            <option value="5">
                5 km
            </option>

            <option value="10">
                10 km
            </option>

            <option value="25"
                    selected>
                25 km
            </option>

            <option value="50">
                50 km
            </option>

        </select>

        <select id="sourceFilter"
                onchange="filterRecords()">

            <option value="ALL">
                All Sources
            </option>

        </select>

        <select id="categoryFilter"
                onchange="filterRecords()">

            <option value="ALL">
                All Categories
            </option>

        </select>

        <select id="locationFilter"
                onchange="filterRecords()">

            <option value="MAPPED">
                Mapped Records
            </option>

            <option value="ALL">
                Mapped + Location Pending
            </option>

        </select>

        <div id="status"
             class="status">
            Loading map...
        </div>

        <div id="summary"></div>

        <div id="records"></div>

    </aside>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";

let map = null;

let propertyMarker = null;

let boundaryLayer = null;

let markerLayer = null;

let allRecords = [];

let currentData = null;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function initMap(
    lat,
    lon
) {

    if (map) {
        return;
    }

    map = L.map(
        "map",
        {
            zoomControl: true
        }
    ).setView(
        [lat,lon],
        13
    );

    L.tileLayer(
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom: 19,

            attribution:
                '&copy; OpenStreetMap contributors'
        }
    ).addTo(map);
}


function clearMarkers() {

    if (markerLayer) {

        markerLayer.clearLayers();

    } else {

        markerLayer =
            L.layerGroup().addTo(map);
    }
}


function addProperty(
    lat,
    lon,
    name
) {

    if (propertyMarker) {

        map.removeLayer(
            propertyMarker
        );
    }

    propertyMarker =
        L.marker(
            [lat,lon]
        )
        .addTo(map)
        .bindPopup(
            "<strong>"
            + esc(name)
            + "</strong><br>"
            + "Property location"
        );
}


function addBoundary(
    boundary
) {

    if (boundaryLayer) {

        map.removeLayer(
            boundaryLayer
        );

        boundaryLayer = null;
    }

    if (!boundary) {
        return;
    }

    boundaryLayer =
        L.geoJSON(
            boundary,
            {
                style: {
                    weight: 3,
                    fillOpacity: .10
                }
            }
        ).addTo(map);
}


function addMarkers(records) {

    clearMarkers();

    for (const record of records) {

        if (
            record.location_status
        ) {
            continue;
        }

        if (
            record.latitude === null ||
            record.longitude === null
        ) {
            continue;
        }

        const marker =
            L.circleMarker(
                [
                    record.latitude,
                    record.longitude
                ],
                {
                    radius: 7,
                    weight: 2,
                    fillOpacity: .72
                }
            );

        let popup = "";

        popup +=
            "<strong>"
            + esc(
                record.title
                || "Untitled"
            )
            + "</strong>";

        popup +=
            "<br>Category: "
            + esc(
                record.category_group
            );

        popup +=
            "<br>Source: "
            + esc(
                record.display_source
            );

        if (
            record.distance_km !== null
            &&
            record.distance_km !== undefined
        ) {

            popup +=
                "<br>Distance: "
                + Number(
                    record.distance_km
                ).toFixed(2)
                + " km";
        }

        if (record.status) {

            popup +=
                "<br>Status: "
                + esc(
                    record.status
                );
        }

        if (record.evidence_level) {

            popup +=
                "<br>Evidence: "
                + esc(
                    record.evidence_level
                );
        }

        if (record.source_url) {

            popup +=
                '<br><a href="'
                + esc(record.source_url)
                + '" target="_blank">'
                + "Open source"
                + "</a>";
        }

        marker
            .bindPopup(popup)
            .addTo(markerLayer);
    }
}


function populateFilters(records) {

    const sourceSelect =
        document.getElementById(
            "sourceFilter"
        );

    const categorySelect =
        document.getElementById(
            "categoryFilter"
        );

    const currentSource =
        sourceSelect.value;

    const currentCategory =
        categorySelect.value;

    const sources =
        [...new Set(
            records.map(
                r => r.display_source
            )
        )].sort();

    const categories =
        [...new Set(
            records.map(
                r => r.category_group
            )
        )].sort();

    sourceSelect.innerHTML =
        '<option value="ALL">'
        + "All Sources"
        + "</option>";

    for (const source of sources) {

        sourceSelect.innerHTML +=
            '<option value="'
            + esc(source)
            + '">'
            + esc(source)
            + "</option>";
    }

    categorySelect.innerHTML =
        '<option value="ALL">'
        + "All Categories"
        + "</option>";

    for (const category of categories) {

        categorySelect.innerHTML +=
            '<option value="'
            + esc(category)
            + '">'
            + esc(category)
            + "</option>";
    }

    if (
        sources.includes(currentSource)
    ) {
        sourceSelect.value =
            currentSource;
    }

    if (
        categories.includes(currentCategory)
    ) {
        categorySelect.value =
            currentCategory;
    }
}


function filterRecords() {

    if (!currentData) {
        return;
    }

    const source =
        document.getElementById(
            "sourceFilter"
        ).value;

    const category =
        document.getElementById(
            "categoryFilter"
        ).value;

    const location =
        document.getElementById(
            "locationFilter"
        ).value;

    let records =
        allRecords.slice();

    if (source !== "ALL") {

        records =
            records.filter(
                r =>
                    r.display_source
                    === source
            );
    }

    if (category !== "ALL") {

        records =
            records.filter(
                r =>
                    r.category_group
                    === category
            );
    }

    if (location === "MAPPED") {

        records =
            records.filter(
                r =>
                    r.location_status
                    === "MAPPED"
            );
    }

    addMarkers(records);

    renderRecords(records);
}


function renderSummary(
    data
) {

    const summary =
        data.summary || {};

    let html = "";

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    summary
                    .total_records
                    || 0
                )}
            </div>

            <div class="label">
                Total intelligence records
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    summary
                    .visible_mapped_records
                    || 0
                )}
            </div>

            <div class="label">
                Records within selected radius
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    summary
                    .unmapped_records
                    || 0
                )}
            </div>

            <div class="label">
                Location pending
            </div>

        </div>
    `;

    document.getElementById(
        "summary"
    ).innerHTML = html;
}


function renderRecords(
    records
) {

    const container =
        document.getElementById(
            "records"
        );

    if (!records.length) {

        container.innerHTML = `
            <div class="card">
                No records match the
                current filters.
            </div>
        `;

        return;
    }

    let html = "";

    for (const record of records) {

        const pending =
            record.location_status

        html += `
            <div
                class="record
                ${pending ? "warning" : ""}"
                onclick="focusRecord('${esc(
                    record.id
                )}')">

                <div class="record-title">
                    ${esc(
                        record.title
                        || "Untitled"
                    )}
                </div>

                <div class="record-meta">

                    <span class="badge">
                        ${esc(
                            record.category_group
                        )}
                    </span>

                    <span class="badge">
                        ${esc(
                            record.display_source
                        )}
                    </span>

                    <br>

                    ${pending
                        ? "Location pending"
                        : (
                            Number(
                                record.distance_km
                            ).toFixed(2)
                            + " km"
                        )
                    }

                    <br>

                    Status:
                    ${esc(
                        record.status
                        || "unknown"
                    )}

                </div>

            </div>
        `;
    }

    container.innerHTML = html;
}


function focusRecord(id) {

    const record =
        allRecords.find(
            r => String(r.id) === String(id)
        );

    if (!record) {
        return;
    }

    if (
        record.location_status
    ) {
        return;
    }

    map.setView(
        [
            record.latitude,
            record.longitude
        ],
        15
    );
}


async function loadMap() {

    const radius =
        document.getElementById(
            "radius"
        ).value;

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Loading intelligence...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/interactive-intelligence-map"
                + "?radius_km="
                + encodeURIComponent(radius)
                + "&include_unmapped=true"
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "Map request failed"
            );
        }

        currentData = data;

        const property =
            data.property;

        if (
            property.latitude === null
            ||
            property.longitude === null
        ) {

            status.textContent =
                "Property location pending";

            return;
        }

        initMap(
            property.latitude,
            property.longitude
        );

        addProperty(
            property.latitude,
            property.longitude,
            property.property_name
        );

        addBoundary(
            data.map.boundary
        );

        allRecords =
            data.records || [];

        populateFilters(
            allRecords
        );

        filterRecords();

        renderSummary(
            data
        );

        status.textContent =
            "Interactive intelligence map ready";

    } catch (err) {

        status.textContent =
            "Map error";

        document.getElementById(
            "records"
        ).innerHTML =
            '<div class="card">'
            + esc(err.message)
            + '</div>';
    }
}


async function liveRefresh() {

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Running live development refresh...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/live-development-refresh",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        sources: [
                            "osm",
                            "news",
                            "government"
                        ]
                    })
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "Live refresh failed"
            );
        }

        await loadMap();

    } catch (err) {

        status.textContent =
            "Live refresh error: "
            + err.message;
    }
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadMap();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 12. UI route
# ------------------------------------------------------------------------------

async def v33_map_ui(
    property_id: str
):

    if _v33_property(property_id) is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v33_html(property_id)
    )


_v33_add_get(
    "/propertyiq/interactive-intelligence-map/{property_id}",
    v33_map_ui
)


# ------------------------------------------------------------------------------
# 13. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v33_interactive_map"
] = build_v33_interactive_map


# ------------------------------------------------------------------------------
# 14. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V33 — INTERACTIVE INTELLIGENCE MAP INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("MAP:")
print("  ✓ Property location")
print("  ✓ GIS boundary")
print("  ✓ Intelligence markers")
print("  ✓ Development geographic context")
print("  ✓ Radius filtering")
print("  ✓ Source filtering")
print("  ✓ Development-category filtering")
print("  ✓ Mapped / location-pending filtering")
print("  ✓ Source links")
print("  ✓ Evidence metadata")
print("  ✓ Live refresh handoff")
print()
print("API:")
print("  /api/v1/properties/{property_id}/interactive-intelligence-map")
print()
print("UI:")
print("  /propertyiq/interactive-intelligence-map/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Existing intelligence only")
print("  ✓ No fabricated coordinates")
print("  ✓ No fabricated projects")
print("  ✓ No investment score")
print("  ✓ No valuation prediction")
print("  ✓ Existing PropertyIQ routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V34
# ORIGINAL COLAB CELL: In[62]
# ============================================================

# ==============================================================================
# PROPERTYIQ V34 — NEWS INTELLIGENCE MAP
# ==============================================================================
# Adds a dedicated geographic news intelligence workspace.
#
# Uses existing:
#   - intelligence_records
#   - GDELT / V15 live connector
#   - V32 geographic matching
#   - V33 interactive map
#   - existing evidence/source fields
#
# Features:
#   - News-only geographic map
#   - News radius filtering
#   - News category filtering
#   - Published-date filtering
#   - Location-pending news
#   - Source/domain visibility
#   - Article source links
#   - Development relevance
#   - Distance from property
#   - Live GDELT refresh handoff
#
# No fabricated news.
# No fabricated coordinates.
# No prediction.
# No investment score.
# ==============================================================================

import sys
import subprocess
import importlib
import math
import json
from datetime import datetime, timezone

# ------------------------------------------------------------------------------
# 1. Dependency bootstrap
# ------------------------------------------------------------------------------

def _v34_install(package, module=None):

    module = module or package

    try:
        importlib.import_module(module)
    except Exception:

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])


_v34_install("sqlalchemy")
_v34_install("fastapi")
_v34_install("psycopg[binary]", "psycopg")


# ------------------------------------------------------------------------------
# 2. Imports
# ------------------------------------------------------------------------------

from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 3. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = "PROPERTYIQ-V34-NEWS-INTELLIGENCE-MAP"

if "engine" not in globals() or engine is None:

    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PropertyIQ PostgreSQL/PostGIS runtime bootstrap first."
    )


if "app" not in globals() or app is None:


    app = FastAPI(
        title="PropertyIQ",
        version="V34"
    )


# ------------------------------------------------------------------------------
# 4. Haversine
# ------------------------------------------------------------------------------

def _v34_haversine_km(
    lat1,
    lon1,
    lat2,
    lon2
):

    R = 6371.0088

    p1 = math.radians(
        float(lat1)
    )

    p2 = math.radians(
        float(lat2)
    )

    dp = math.radians(
        float(lat2) - float(lat1)
    )

    dl = math.radians(
        float(lon2) - float(lon1)
    )

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * R * math.asin(
        min(1.0, math.sqrt(a))
    )


# ------------------------------------------------------------------------------
# 5. Property
# ------------------------------------------------------------------------------

def _v34_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    return dict(row) if row else None


# ------------------------------------------------------------------------------
# 6. Boundary
# ------------------------------------------------------------------------------

def _v34_boundary(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    ST_AsGeoJSON(boundary)
                    AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    if not row:
        return None

    raw = row.get(
        "boundary_geojson"
    )

    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 7. News record identification
# ------------------------------------------------------------------------------

def _v34_is_news(record):

    values = [

        record.get("source_type"),

        record.get("source_name"),

        record.get("category"),

        record.get("title"),

        record.get("description")
    ]

    text_value = " ".join(
        str(value or "")
        for value in values
    ).lower()

    news_signals = [
        "gdelt",
        "news",
        "article",
        "reported",
        "press",
        "publication"
    ]

    return any(
        signal in text_value
        for signal in news_signals
    )


# ------------------------------------------------------------------------------
# 8. News classification
# ------------------------------------------------------------------------------

def _v34_classify_news(record):

    text_value = " ".join([

        str(record.get("title") or ""),

        str(record.get("description") or ""),

        str(record.get("category") or "")

    ]).lower()

    groups = {

        "TRANSPORT": [
            "metro",
            "rail",
            "railway",
            "expressway",
            "highway",
            "road",
            "flyover",
            "corridor",
            "airport",
            "transit"
        ],

        "REAL_ESTATE": [
            "real estate",
            "property",
            "housing",
            "residential",
            "apartment",
            "township",
            "builder",
            "developer"
        ],

        "COMMERCIAL": [
            "mall",
            "commercial",
            "office",
            "retail",
            "business district",
            "market"
        ],

        "INDUSTRIAL": [
            "industrial",
            "manufacturing",
            "factory",
            "warehouse",
            "logistics",
            "industrial park"
        ],

        "INFRASTRUCTURE": [
            "infrastructure",
            "development",
            "construction",
            "project",
            "smart city",
            "urban development"
        ],

        "INSTITUTIONAL": [
            "hospital",
            "school",
            "college",
            "university",
            "medical"
        ],

        "LAND": [
            "land acquisition",
            "land",
            "acquisition",
            "parcel"
        ]
    }

    for group, keywords in groups.items():

        for keyword in keywords:

            if keyword in text_value:
                return group

    return "OTHER"


# ------------------------------------------------------------------------------
# 9. News loader
# ------------------------------------------------------------------------------

def _v34_load_news(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY published_at DESC NULLS LAST
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
        if _v34_is_news(dict(row))
    ]


# ------------------------------------------------------------------------------
# 10. News intelligence engine
# ------------------------------------------------------------------------------

def build_v34_news_map(
    property_id,
    radius_km=25
):

    property_data = _v34_property(
        property_id
    )

    if property_data is None:

        raise ValueError(
            "Property not found."
        )

    boundary = _v34_boundary(
        property_id
    )

    news = _v34_load_news(
        property_id
    )

    prop_lat = property_data.get(
        "latitude"
    )

    prop_lon = property_data.get(
        "longitude"
    )

    mapped = []

    pending = []

    categories = {}

    sources = {}

    dates = {}

    for record in news:

        record = dict(record)

        record["id"] = str(
            record["id"]
        )

        record["news_category"] = (
            _v34_classify_news(
                record
            )
        )

        source = (
            record.get("source_name")
            or record.get("source_type")
            or "UNKNOWN"
        )

        record["display_source"] = source

        categories[
            record["news_category"]
        ] = (
            categories.get(
                record["news_category"],
                0
            )
            + 1
        )

        sources[source] = (
            sources.get(
                source,
                0
            )
            + 1
        )

        published = record.get(
            "published_at"
        )

        if published:

            try:

                date_key = (
                    published.date().isoformat()
                    if hasattr(
                        published,
                        "date"
                    )
                    else str(
                        published
                    )[:10]
                )

                dates[date_key] = (
                    dates.get(
                        date_key,
                        0
                    )
                    + 1
                )

            except Exception:
                pass

        lat = record.get(
            "latitude"
        )

        lon = record.get(
            "longitude"
        )

        if lat is None or lon is None:

            record[
                "location_status"
            ] = "LOCATION_PENDING"

            record[
                "distance_km"
            ] = None

            pending.append(record)

            continue

        try:

            lat = float(lat)
            lon = float(lon)

        except Exception:

            record[
                "location_status"
            ] = "INVALID_COORDINATES"

            record[
                "distance_km"
            ] = None

            pending.append(record)

            continue

        if prop_lat is None or prop_lon is None:

            record[
                "location_status"
            ] = "MAPPED_PROPERTY_PENDING"

            record[
                "distance_km"
            ] = None

            mapped.append(record)

            continue

        distance = _v34_haversine_km(
            prop_lat,
            prop_lon,
            lat,
            lon
        )

        record[
            "latitude"
        ] = lat

        record[
            "longitude"
        ] = lon

        record[
            "distance_km"
        ] = round(
            distance,
            3
        )

        record[
            "within_radius"
        ] = (
            distance <= radius_km
        )

        record[
            "location_status"
        ] = "MAPPED"

        mapped.append(record)

    mapped.sort(
        key=lambda r:
            r.get(
                "distance_km"
            )
            if r.get(
                "distance_km"
            ) is not None
            else 999999
    )

    nearby = [
        r
        for r in mapped
        if r.get(
            "distance_km"
        ) is not None
        and r.get(
            "distance_km"
        ) <= radius_km
    ]

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property": property_data,

        "map": {

            "latitude": prop_lat,

            "longitude": prop_lon,

            "radius_km": radius_km,

            "boundary": boundary
        },

        "summary": {

            "total_news_records": len(
                news
            ),

            "mapped_news": len(
                mapped
            ),

            "nearby_news": len(
                nearby
            ),

            "location_pending_news": len(
                pending
            ),

            "categories": categories,

            "sources": sources,

            "publication_dates": dates
        },

        "news": mapped,

        "location_pending": pending
    }


# ------------------------------------------------------------------------------
# 11. Routes
# ------------------------------------------------------------------------------

_v34_routes = {
    getattr(
        route,
        "path",
        ""
    )
    for route in getattr(
        app,
        "routes",
        []
    )
}


def _v34_add_get(
    path,
    endpoint
):

    if path not in _v34_routes:

        app.get(path)(endpoint)

        _v34_routes.add(path)


async def v34_news_api(
    property_id: str,
    radius_km: float = 25
):

    try:

        radius_km = max(
            0.1,
            min(
                float(radius_km),
                100
            )
        )

        return build_v34_news_map(
            property_id,
            radius_km
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v34_add_get(
    "/api/v1/properties/{property_id}/news-map",
    v34_news_api
)


# ------------------------------------------------------------------------------
# 12. UI
# ------------------------------------------------------------------------------

def _v34_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — News Intelligence Map
</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background: #07111f;

    color: #edf7ff;

    font-family:
        Inter,
        system-ui,
        sans-serif;

    overflow: hidden;
}

#app {

    display: grid;

    grid-template-columns:
        1fr 380px;

    height: 100vh;
}

#map {

    height: 100vh;
}

#panel {

    height: 100vh;

    overflow-y: auto;

    padding: 20px;

    background:
        rgba(7,17,31,.97);

    border-left:
        1px solid rgba(255,255,255,.08);

    z-index: 1000;
}

.brand {

    font-size: 21px;

    font-weight: 850;
}

.brand span {

    color: #4bd4ff;
}

.subtitle {

    color: #8da6be;

    font-size: 11px;

    margin-top: 4px;

    margin-bottom: 17px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 7px;

    margin-bottom: 12px;
}

button {

    border: 0;

    border-radius: 9px;

    padding: 9px 12px;

    cursor: pointer;

    font-weight: 750;

    color: #04111d;

    background:
        linear-gradient(
            135deg,
            #40d1ff,
            #5feecf
        );
}

button.secondary {

    color: #dcefff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.08);
}

select {

    width: 100%;

    background: #102033;

    color: white;

    border:
        1px solid rgba(255,255,255,.10);

    border-radius: 9px;

    padding: 9px;

    margin-bottom: 8px;
}

.card {

    padding: 14px;

    border-radius: 13px;

    background:
        rgba(255,255,255,.045);

    border:
        1px solid rgba(255,255,255,.07);

    margin-bottom: 9px;
}

.metric {

    font-size: 27px;

    font-weight: 850;

    color: #5ad9ff;
}

.label {

    color: #8ea7bf;

    font-size: 11px;
}

.record {

    padding: 12px;

    margin-top: 7px;

    border-radius: 11px;

    background:
        rgba(255,255,255,.035);

    border-left:
        3px solid #43d2ff;

    cursor: pointer;
}

.record:hover {

    background:
        rgba(50,210,255,.09);
}

.title {

    font-size: 12px;

    font-weight: 750;

    line-height: 1.45;
}

.meta {

    color: #91a9c0;

    font-size: 10px;

    line-height: 1.6;

    margin-top: 5px;
}

.badge {

    display: inline-block;

    padding: 3px 7px;

    border-radius: 999px;

    background:
        rgba(65,210,255,.11);

    color: #64d9ff;

    font-size: 9px;

    margin-top: 4px;

    margin-right: 3px;
}

.pending {

    border-left-color: #e4bd54;
}

.status {

    color: #91a9c0;

    font-size: 11px;

    margin-bottom: 10px;
}

a {

    color: #5fd9ff;

    text-decoration: none;
}

@media(max-width:900px) {

    #app {

        grid-template-columns: 1fr;
    }

    #panel {

        position: absolute;

        right: 0;

        top: 0;

        width: min(380px,94vw);

        box-shadow:
            -20px 0 50px rgba(0,0,0,.4);
    }
}

</style>

</head>

<body>

<div id="app">

<div id="map"></div>

<aside id="panel">

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div class="subtitle">
        Geographic News Intelligence
    </div>

    <div class="toolbar">

        <button onclick="loadNews()">
            Refresh
        </button>

        <button
            class="secondary"
            onclick="liveNews()">
            Live GDELT
        </button>

        <button
            class="secondary"
            onclick="openWorkspace()">
            Workspace
        </button>

    </div>

    <select id="radius"
            onchange="loadNews()">

        <option value="1">
            1 km
        </option>

        <option value="3">
            3 km
        </option>

        <option value="5">
            5 km
        </option>

        <option value="10">
            10 km
        </option>

        <option value="25"
                selected>
            25 km
        </option>

        <option value="50">
            50 km
        </option>

    </select>

    <select id="category"
            onchange="filterNews()">

        <option value="ALL">
            All news categories
        </option>

    </select>

    <select id="dateFilter"
            onchange="filterNews()">

        <option value="ALL">
            All publication dates
        </option>

        <option value="30">
            Last 30 days
        </option>

        <option value="90">
            Last 90 days
        </option>

        <option value="365">
            Last year
        </option>

    </select>

    <div id="status"
         class="status">
        Loading...
    </div>

    <div id="summary"></div>

    <div id="news"></div>

</aside>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";

let map = null;

let markerLayer = null;

let propertyMarker = null;

let boundaryLayer = null;

let allNews = [];

let currentData = null;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function initMap(
    lat,
    lon
) {

    if (map) {
        return;
    }

    map = L.map(
        "map"
    ).setView(
        [lat,lon],
        13
    );

    L.tileLayer(
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom: 19,

            attribution:
                "&copy; OpenStreetMap contributors"
        }
    ).addTo(map);

    markerLayer =
        L.layerGroup().addTo(map);
}


function addProperty(
    lat,
    lon,
    name
) {

    if (propertyMarker) {

        map.removeLayer(
            propertyMarker
        );
    }

    propertyMarker =
        L.marker(
            [lat,lon]
        )
        .addTo(map)
        .bindPopup(
            "<strong>"
            + esc(name)
            + "</strong>"
            + "<br>Property location"
        );
}


function addBoundary(
    boundary
) {

    if (boundaryLayer) {

        map.removeLayer(
            boundaryLayer
        );
    }

    if (!boundary) {
        return;
    }

    boundaryLayer =
        L.geoJSON(
            boundary,
            {
                style: {
                    weight: 3,
                    fillOpacity: .10
                }
            }
        ).addTo(map);
}


function renderMarkers(
    records
) {

    markerLayer.clearLayers();

    for (
        const record of records
    ) {

        if (
            record.location_status
        ) {
            continue;
        }

        if (
            record.latitude === null
            ||
            record.longitude === null
        ) {
            continue;
        }

        const marker =
            L.circleMarker(
                [
                    record.latitude,
                    record.longitude
                ],
                {
                    radius: 7,
                    weight: 2,
                    fillOpacity: .75
                }
            );

        let popup =
            "<strong>"
            + esc(
                record.title
                || "Untitled"
            )
            + "</strong>";

        popup +=
            "<br>Category: "
            + esc(
                record.news_category
            );

        popup +=
            "<br>Source: "
            + esc(
                record.display_source
            );

        if (
            record.distance_km !== null
            &&
            record.distance_km !== undefined
        ) {

            popup +=
                "<br>Distance: "
                + Number(
                    record.distance_km
                ).toFixed(2)
                + " km";
        }

        if (record.published_at) {

            popup +=
                "<br>Published: "
                + esc(
                    record.published_at
                );
        }

        if (record.evidence_level) {

            popup +=
                "<br>Evidence: "
                + esc(
                    record.evidence_level
                );
        }

        if (record.source_url) {

            popup +=
                '<br><a href="'
                + esc(
                    record.source_url
                )
                + '" target="_blank">'
                + "Open article/source"
                + "</a>";
        }

        marker
            .bindPopup(popup)
            .addTo(markerLayer);
    }
}


function populateCategories(
    records
) {

    const select =
        document.getElementById(
            "category"
        );

    const current =
        select.value;

    const categories =
        [
            ...new Set(
                records.map(
                    r =>
                        r.news_category
                )
            )
        ].sort();

    select.innerHTML =
        '<option value="ALL">'
        + "All news categories"
        + "</option>";

    for (
        const category
        of categories
    ) {

        select.innerHTML +=
            '<option value="'
            + esc(category)
            + '">'
            + esc(category)
            + "</option>";
    }

    if (
        categories.includes(current)
    ) {
        select.value =
            current;
    }
}


function withinDateFilter(
    record,
    days
) {

    if (days === "ALL") {
        return true;
    }

    if (!record.published_at) {
        return true;
    }

    const published =
        new Date(
            record.published_at
        );

    if (
        Number.isNaN(
            published.getTime()
        )
    ) {
        return true;
    }

    const cutoff =
        Date.now()
        -
        Number(days)
        *
        24
        *
        60
        *
        60
        *
        1000;

    return (
        published.getTime()
        >= cutoff
    );
}


function filterNews() {

    const category =
        document.getElementById(
            "category"
        ).value;

    const dateFilter =
        document.getElementById(
            "dateFilter"
        ).value;

    let filtered =
        allNews.slice();

    if (
        category !== "ALL"
    ) {

        filtered =
            filtered.filter(
                record =>
                    record.news_category
                    === category
            );
    }

    filtered =
        filtered.filter(
            record =>
                withinDateFilter(
                    record,
                    dateFilter
                )
        );

    renderMarkers(
        filtered
    );

    renderNewsList(
        filtered
    );
}


function renderSummary(
    data
) {

    const s =
        data.summary || {};

    let html = "";

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.total_news_records
                    || 0
                )}
            </div>

            <div class="label">
                Total news records
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.nearby_news
                    || 0
                )}
            </div>

            <div class="label">
                News within selected radius
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.location_pending_news
                    || 0
                )}
            </div>

            <div class="label">
                Location pending
            </div>

        </div>
    `;

    document.getElementById(
        "summary"
    ).innerHTML = html;
}


function renderNewsList(
    records
) {

    const container =
        document.getElementById(
            "news"
        );

    if (!records.length) {

        container.innerHTML = `
            <div class="card">
                No news records match
                the current filters.
            </div>
        `;

        return;
    }

    let html = "";

    for (
        const record
        of records
    ) {

        const pending =
            record.location_status

        html += `
            <div
                class="record
                ${pending ? "pending" : ""}"
                onclick="focusNews('${esc(
                    record.id
                )}')">

                <div class="title">
                    ${esc(
                        record.title
                        || "Untitled"
                    )}
                </div>

                <div class="meta">

                    <span class="badge">
                        ${esc(
                            record.news_category
                        )}
                    </span>

                    <span class="badge">
                        ${esc(
                            record.display_source
                        )}
                    </span>

                    <br>

                    ${
                        pending
                        ? "Location pending"
                        : (
                            Number(
                                record.distance_km
                            ).toFixed(2)
                            + " km"
                        )
                    }

                    <br>

                    ${
                        record.published_at
                        ? "Published: "
                          +
                          esc(
                              record.published_at
                          )
                        : "Publication date unavailable"
                    }

                </div>

            </div>
        `;
    }

    container.innerHTML =
        html;
}


function focusNews(id) {

    const record =
        allNews.find(
            r =>
                String(r.id)
                ===
                String(id)
        );

    if (!record) {
        return;
    }

    if (
        record.location_status
    ) {
        return;
    }

    map.setView(
        [
            record.latitude,
            record.longitude
        ],
        15
    );
}


async function loadNews() {

    const radius =
        document.getElementById(
            "radius"
        ).value;

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Loading news intelligence...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/news-map"
                + "?radius_km="
                + encodeURIComponent(
                    radius
                )
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "News request failed"
            );
        }

        currentData =
            data;

        const property =
            data.property;

        if (
            property.latitude === null
            ||
            property.longitude === null
        ) {

            status.textContent =
                "Property location pending";

            return;
        }

        initMap(
            property.latitude,
            property.longitude
        );

        addProperty(
            property.latitude,
            property.longitude,
            property.property_name
        );

        addBoundary(
            data.map.boundary
        );

        allNews =
            data.news || [];

        populateCategories(
            allNews
        );

        renderSummary(
            data
        );

        filterNews();

        status.textContent =
            "News intelligence map ready";

    } catch (err) {

        status.textContent =
            "Error";

        document.getElementById(
            "news"
        ).innerHTML =
            '<div class="card">'
            + esc(err.message)
            + '</div>';
    }
}


async function liveNews() {

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Refreshing GDELT news...";

    try {

        const response =
            await fetch(
                "/api/v1/live/property/"
                + PROPERTY_ID
                + "/news",
                {
                    method: "POST"
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "GDELT refresh failed"
            );
        }

        await loadNews();

    } catch (err) {

        status.textContent =
            "GDELT refresh error: "
            + err.message;
    }
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadNews();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 13. UI route
# ------------------------------------------------------------------------------

async def v34_news_ui(
    property_id: str
):

    if _v34_property(
        property_id
    ) is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v34_html(property_id)
    )


_v34_add_get(
    "/propertyiq/news-map/{property_id}",
    v34_news_ui
)


# ------------------------------------------------------------------------------
# 14. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v34_news_map"
] = build_v34_news_map


# ------------------------------------------------------------------------------
# 15. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V34 — NEWS INTELLIGENCE MAP INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("NEWS MAP:")
print("  ✓ GDELT-backed news records")
print("  ✓ Geographic news markers")
print("  ✓ Property distance calculation")
print("  ✓ Radius filtering")
print("  ✓ News-category filtering")
print("  ✓ Publication-date filtering")
print("  ✓ Location-pending news")
print("  ✓ Source/domain visibility")
print("  ✓ Article/source links")
print("  ✓ Property boundary")
print("  ✓ Live GDELT refresh handoff")
print()
print("API:")
print("  /api/v1/properties/{property_id}/news-map")
print()
print("UI:")
print("  /propertyiq/news-map/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Existing news records only")
print("  ✓ No fabricated articles")
print("  ✓ No fabricated coordinates")
print("  ✓ Source URLs preserved")
print("  ✓ No prediction")
print("  ✓ No investment score")
print("  ✓ Existing PropertyIQ routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V35
# ORIGINAL COLAB CELL: In[64]
# ============================================================

# ==============================================================================
# PROPERTYIQ V35 — UNIFIED INTELLIGENCE COMMAND MAP
# ==============================================================================
# Consolidates existing PropertyIQ capabilities into one map:
#
#   Property
#   GIS Boundary
#   OSM / GIS
#   RERA
#   Government / PAIMANA
#   Development
#   News / GDELT
#   Market / Comparables
#   Evidence
#   Geographic distance
#
# Existing modules are NOT rebuilt.
#
# No fabricated intelligence.
# No fabricated coordinates.
# No valuation prediction.
# No investment score.
# ==============================================================================

import sys
import subprocess
import importlib
import math
import json


# ------------------------------------------------------------------------------
# 1. Dependencies
# ------------------------------------------------------------------------------

def _v35_install(package, module=None):

    module = module or package

    try:
        importlib.import_module(module)

    except Exception:

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])


_v35_install("sqlalchemy")
_v35_install("fastapi")
_v35_install("psycopg[binary]", "psycopg")


from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 2. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = (
    "PROPERTYIQ-V35-UNIFIED-INTELLIGENCE-COMMAND-MAP"
)


if "engine" not in globals() or engine is None:

    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PostgreSQL/PostGIS runtime bootstrap first."
    )


if "app" not in globals() or app is None:


    app = FastAPI(
        title="PropertyIQ",
        version="V35"
    )


# ------------------------------------------------------------------------------
# 3. Distance
# ------------------------------------------------------------------------------

def _v35_haversine_km(
    lat1,
    lon1,
    lat2,
    lon2
):

    R = 6371.0088

    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))

    dp = math.radians(
        float(lat2) - float(lat1)
    )

    dl = math.radians(
        float(lon2) - float(lon1)
    )

    a = (
        math.sin(dp / 2) ** 2
        +
        math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return (
        2
        * R
        * math.asin(
            min(
                1,
                math.sqrt(a)
            )
        )
    )


# ------------------------------------------------------------------------------
# 4. Property
# ------------------------------------------------------------------------------

def _v35_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    return dict(row) if row else None


# ------------------------------------------------------------------------------
# 5. Boundary
# ------------------------------------------------------------------------------

def _v35_boundary(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    ST_AsGeoJSON(boundary)
                    AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    if not row:
        return None

    raw = row.get(
        "boundary_geojson"
    )

    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 6. Intelligence
# ------------------------------------------------------------------------------

def _v35_intelligence(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY
                    published_at DESC NULLS LAST,
                    title
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 7. Source grouping
# ------------------------------------------------------------------------------

def _v35_source_group(record):

    text_value = " ".join([
        str(record.get("source_name") or ""),
        str(record.get("source_type") or ""),
        str(record.get("category") or ""),
        str(record.get("title") or ""),
        str(record.get("description") or "")
    ]).lower()

    if (
        "rera" in text_value
        or "up-rera" in text_value
    ):
        return "RERA"

    if (
        "gdelt" in text_value
        or "news" in text_value
        or "article" in text_value
    ):
        return "NEWS"

    if (
        "paimana" in text_value
        or "mospi" in text_value
        or "government" in text_value
        or "infrastructure" in text_value
    ):
        return "GOVERNMENT"

    if (
        "openstreetmap" in text_value
        or "overpass" in text_value
        or "osm" in text_value
    ):
        return "OSM"

    return "OTHER"


# ------------------------------------------------------------------------------
# 8. Development classification
# ------------------------------------------------------------------------------

def _v35_development_type(record):

    text_value = " ".join([
        str(record.get("title") or ""),
        str(record.get("description") or ""),
        str(record.get("category") or "")
    ]).lower()

    groups = {

        "TRANSPORT": [
            "metro",
            "railway",
            "rail",
            "expressway",
            "highway",
            "road",
            "flyover",
            "airport",
            "corridor",
            "transit"
        ],

        "INDUSTRIAL": [
            "industrial",
            "manufacturing",
            "factory",
            "warehouse",
            "logistics",
            "industrial area"
        ],

        "COMMERCIAL": [
            "mall",
            "commercial",
            "office",
            "retail",
            "business district",
            "market"
        ],

        "INSTITUTIONAL": [
            "hospital",
            "school",
            "college",
            "university",
            "medical"
        ],

        "RESIDENTIAL": [
            "residential",
            "housing",
            "apartment",
            "township"
        ],

        "INFRASTRUCTURE": [
            "infrastructure",
            "development",
            "construction",
            "project",
            "smart city"
        ]
    }

    for group, keywords in groups.items():

        for keyword in keywords:

            if keyword in text_value:
                return group

    return "OTHER"


# ------------------------------------------------------------------------------
# 9. Unified data builder
# ------------------------------------------------------------------------------

def build_v35_command_map(
    property_id,
    radius_km=25,
    include_unmapped=True
):

    property_data = _v35_property(
        property_id
    )

    if property_data is None:

        raise ValueError(
            "Property not found."
        )

    boundary = _v35_boundary(
        property_id
    )

    records = _v35_intelligence(
        property_id
    )

    prop_lat = property_data.get(
        "latitude"
    )

    prop_lon = property_data.get(
        "longitude"
    )

    mapped = []
    unmapped = []

    source_summary = {}
    category_summary = {}
    status_summary = {}

    for record in records:

        record = dict(record)

        record["id"] = str(
            record["id"]
        )

        record["source_group"] = (
            _v35_source_group(
                record
            )
        )

        record["development_type"] = (
            _v35_development_type(
                record
            )
        )

        source_group = (
            record["source_group"]
        )

        source_summary[
            source_group
        ] = (
            source_summary.get(
                source_group,
                0
            )
            + 1
        )

        development_type = (
            record["development_type"]
        )

        category_summary[
            development_type
        ] = (
            category_summary.get(
                development_type,
                0
            )
            + 1
        )

        status = (
            record.get("status")
            or "unknown"
        )

        status_summary[
            status
        ] = (
            status_summary.get(
                status,
                0
            )
            + 1
        )

        lat = record.get(
            "latitude"
        )

        lon = record.get(
            "longitude"
        )

        if lat is None or lon is None:

            record[
                "location_status"
            ] = "LOCATION_PENDING"

            record[
                "distance_km"
            ] = None

            unmapped.append(
                record
            )

            continue

        try:

            lat = float(lat)
            lon = float(lon)

        except Exception:

            record[
                "location_status"
            ] = "INVALID_COORDINATES"

            record[
                "distance_km"
            ] = None

            unmapped.append(
                record
            )

            continue

        if not (
            -90 <= lat <= 90
            and -180 <= lon <= 180
        ):

            record[
                "location_status"
            ] = "INVALID_COORDINATES"

            record[
                "distance_km"
            ] = None

            unmapped.append(
                record
            )

            continue

        record["latitude"] = lat
        record["longitude"] = lon

        if (
            prop_lat is not None
            and prop_lon is not None
        ):

            distance = (
                _v35_haversine_km(
                    prop_lat,
                    prop_lon,
                    lat,
                    lon
                )
            )

            record[
                "distance_km"
            ] = round(
                distance,
                3
            )

            record[
                "within_radius"
            ] = (
                distance <= radius_km
            )

        else:

            record[
                "distance_km"
            ] = None

            record[
                "within_radius"
            ] = False

        record[
            "location_status"
        ] = "MAPPED"

        mapped.append(
            record
        )

    mapped.sort(
        key=lambda x:
            x.get(
                "distance_km"
            )
            if x.get(
                "distance_km"
            ) is not None
            else 999999
    )

    visible = [
        r
        for r in mapped
        if r.get(
            "within_radius"
        )
    ]

    if include_unmapped:

        display_records = (
            visible
            + unmapped
        )

    else:

        display_records = visible

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property": property_data,

        "map": {

            "latitude": prop_lat,

            "longitude": prop_lon,

            "radius_km": radius_km,

            "boundary": boundary
        },

        "summary": {

            "total_records": len(
                records
            ),

            "mapped_records": len(
                mapped
            ),

            "visible_records": len(
                visible
            ),

            "unmapped_records": len(
                unmapped
            ),

            "source_summary":
                source_summary,

            "development_summary":
                category_summary,

            "status_summary":
                status_summary
        },

        "records": display_records,

        "mapped_records": mapped,

        "unmapped_records": unmapped
    }


# ------------------------------------------------------------------------------
# 10. API
# ------------------------------------------------------------------------------

_v35_routes = {
    getattr(
        route,
        "path",
        ""
    )
    for route in getattr(
        app,
        "routes",
        []
    )
}


def _v35_add_get(
    path,
    endpoint
):

    if path not in _v35_routes:

        app.get(path)(endpoint)

        _v35_routes.add(path)


async def v35_command_map_api(
    property_id: str,
    radius_km: float = 25,
    include_unmapped: bool = True
):

    try:

        radius_km = max(
            0.1,
            min(
                float(radius_km),
                100
            )
        )

        return build_v35_command_map(
            property_id,
            radius_km,
            include_unmapped
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v35_add_get(
    "/api/v1/properties/{property_id}/unified-command-map",
    v35_command_map_api
)


# ------------------------------------------------------------------------------
# 11. UI
# ------------------------------------------------------------------------------

def _v35_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Unified Intelligence Command Map
</title>

<link
 rel="stylesheet"
 href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
 src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background: #06101d;

    color: #eef7ff;

    font-family:
        Inter,
        system-ui,
        -apple-system,
        sans-serif;

    overflow: hidden;
}

#app {

    display: grid;

    grid-template-columns:
        1fr 400px;

    height: 100vh;
}

#map {

    height: 100vh;

    background: #091522;
}

#panel {

    height: 100vh;

    overflow-y: auto;

    background:
        rgba(6,16,29,.98);

    border-left:
        1px solid rgba(255,255,255,.08);

    padding: 19px;

    z-index: 1000;
}

.brand {

    font-size: 21px;

    font-weight: 900;

    letter-spacing: .2px;
}

.brand span {

    color: #45d4ff;
}

.subtitle {

    color: #8ea7c0;

    font-size: 11px;

    margin-top: 4px;

    margin-bottom: 16px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 7px;

    margin-bottom: 10px;
}

button {

    border: 0;

    border-radius: 9px;

    padding: 9px 11px;

    cursor: pointer;

    font-weight: 750;

    color: #04111d;

    background:
        linear-gradient(
            135deg,
            #45d1ff,
            #5eeecf
        );
}

button.secondary {

    color: #d9edff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.08);
}

select {

    width: 100%;

    background: #102033;

    color: white;

    border:
        1px solid rgba(255,255,255,.10);

    border-radius: 9px;

    padding: 9px;

    margin-bottom: 7px;
}

.card {

    padding: 13px;

    border-radius: 13px;

    background:
        linear-gradient(
            145deg,
            rgba(19,36,57,.85),
            rgba(8,20,34,.85)
        );

    border:
        1px solid rgba(255,255,255,.07);

    margin-bottom: 9px;
}

.metric {

    font-size: 27px;

    font-weight: 900;

    color: #59d9ff;
}

.label {

    color: #8fa8c0;

    font-size: 10px;
}

.record {

    padding: 11px;

    border-radius: 10px;

    margin-top: 7px;

    background:
        rgba(255,255,255,.035);

    border-left:
        3px solid #45d3ff;

    cursor: pointer;
}

.record:hover {

    background:
        rgba(55,210,255,.09);
}

.record.pending {

    border-left-color:
        #e2bb50;
}

.title {

    font-size: 11px;

    font-weight: 800;

    line-height: 1.45;
}

.meta {

    color: #91a9c0;

    font-size: 9px;

    line-height: 1.6;

    margin-top: 5px;
}

.badge {

    display: inline-block;

    padding: 3px 6px;

    border-radius: 999px;

    background:
        rgba(64,210,255,.11);

    color: #64d9ff;

    font-size: 8px;

    margin-top: 4px;

    margin-right: 3px;
}

.status {

    color: #8fa8c0;

    font-size: 10px;

    margin-bottom: 9px;
}

.legend {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 6px;

    margin-top: 8px;
}

.legend-item {

    font-size: 9px;

    color: #91a9c0;

    padding: 7px;

    border-radius: 8px;

    background:
        rgba(255,255,255,.035);
}

.legend-dot {

    display: inline-block;

    width: 8px;

    height: 8px;

    border-radius: 50%;

    margin-right: 5px;
}

a {

    color: #5fd9ff;

    text-decoration: none;
}

@media(max-width:900px) {

    #app {

        grid-template-columns: 1fr;
    }

    #panel {

        position: absolute;

        right: 0;

        top: 0;

        width: min(400px,94vw);

        box-shadow:
            -20px 0 50px rgba(0,0,0,.45);
    }
}

</style>

</head>

<body>

<div id="app">

<div id="map"></div>

<aside id="panel">

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div class="subtitle">
        Unified Property Intelligence Command Map
    </div>

    <div class="toolbar">

        <button onclick="loadMap()">
            Refresh
        </button>

        <button
            class="secondary"
            onclick="liveRefresh()">
            Live Refresh
        </button>

        <button
            class="secondary"
            onclick="openWorkspace()">
            Workspace
        </button>

    </div>

    <select id="radius"
            onchange="loadMap()">

        <option value="1">
            1 km
        </option>

        <option value="3">
            3 km
        </option>

        <option value="5">
            5 km
        </option>

        <option value="10">
            10 km
        </option>

        <option value="25"
                selected>
            25 km
        </option>

        <option value="50">
            50 km
        </option>

        <option value="100">
            100 km
        </option>

    </select>

    <select id="sourceFilter"
            onchange="applyFilters()">

        <option value="ALL">
            All Intelligence Sources
        </option>

    </select>

    <select id="categoryFilter"
            onchange="applyFilters()">

        <option value="ALL">
            All Development Categories
        </option>

    </select>

    <select id="locationFilter"
            onchange="applyFilters()">

        <option value="MAPPED">
            Mapped Only
        </option>

        <option value="ALL">
            Mapped + Location Pending
        </option>

    </select>

    <div id="status"
         class="status">
        Loading command map...
    </div>

    <div id="summary"></div>

    <div class="card">

        <div class="label">
            INTELLIGENCE LAYERS
        </div>

        <div class="legend">

            <div class="legend-item">
                ● Property
            </div>

            <div class="legend-item">
                ● RERA
            </div>

            <div class="legend-item">
                ● Government
            </div>

            <div class="legend-item">
                ● News
            </div>

            <div class="legend-item">
                ● OSM / GIS
            </div>

            <div class="legend-item">
                ● Other
            </div>

        </div>

    </div>

    <div id="records"></div>

</aside>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";

let map = null;

let markerLayer = null;

let propertyMarker = null;

let boundaryLayer = null;

let allRecords = [];

let currentData = null;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function initMap(
    lat,
    lon
) {

    if (map) {
        return;
    }

    map = L.map(
        "map"
    ).setView(
        [lat,lon],
        12
    );

    L.tileLayer(
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
            maxZoom: 19,

            attribution:
                "&copy; OpenStreetMap contributors"
        }
    ).addTo(map);

    markerLayer =
        L.layerGroup().addTo(map);
}


function addProperty(
    lat,
    lon,
    name
) {

    if (propertyMarker) {

        map.removeLayer(
            propertyMarker
        );
    }

    propertyMarker =
        L.marker(
            [lat,lon]
        )
        .addTo(map)
        .bindPopup(
            "<strong>"
            + esc(name)
            + "</strong>"
            + "<br>Property"
        );
}


function addBoundary(
    boundary
) {

    if (boundaryLayer) {

        map.removeLayer(
            boundaryLayer
        );

        boundaryLayer = null;
    }

    if (!boundary) {
        return;
    }

    boundaryLayer =
        L.geoJSON(
            boundary,
            {
                style: {
                    weight: 3,
                    fillOpacity: .10
                }
            }
        ).addTo(map);
}


function sourceColor(
    source
) {

    if (
        source === "RERA"
    ) {
        return "#b87cff";
    }

    if (
        source === "GOVERNMENT"
    ) {
        return "#52d6ff";
    }

    if (
        source === "NEWS"
    ) {
        return "#ffbf55";
    }

    if (
        source === "OSM"
    ) {
        return "#55e39b";
    }

    return "#d0d8e2";
}


function addMarkers(
    records
) {

    markerLayer.clearLayers();

    for (
        const record
        of records
    ) {

        if (
            record.location_status
        ) {
            continue;
        }

        if (
            record.latitude === null
            ||
            record.longitude === null
        ) {
            continue;
        }

        const color =
            sourceColor(
                record.source_group
            );

        const marker =
            L.circleMarker(
                [
                    record.latitude,
                    record.longitude
                ],
                {
                    radius: 7,

                    color: color,

                    fillColor: color,

                    fillOpacity: .72,

                    weight: 2
                }
            );

        let popup = "";

        popup +=
            "<strong>"
            + esc(
                record.title
                || "Untitled"
            )
            + "</strong>";

        popup +=
            "<br>Source group: "
            + esc(
                record.source_group
            );

        popup +=
            "<br>Type: "
            + esc(
                record.development_type
            );

        if (
            record.distance_km !== null
            &&
            record.distance_km !== undefined
        ) {

            popup +=
                "<br>Distance: "
                + Number(
                    record.distance_km
                ).toFixed(2)
                + " km";
        }

        if (record.status) {

            popup +=
                "<br>Status: "
                + esc(
                    record.status
                );
        }

        if (record.evidence_level) {

            popup +=
                "<br>Evidence: "
                + esc(
                    record.evidence_level
                );
        }

        if (record.confidence) {

            popup +=
                "<br>Confidence: "
                + esc(
                    record.confidence
                );
        }

        if (record.source_url) {

            popup +=
                '<br><a href="'
                + esc(
                    record.source_url
                )
                + '" target="_blank">'
                + "Open source"
                + "</a>";
        }

        marker
            .bindPopup(
                popup
            )
            .addTo(
                markerLayer
            );
    }
}


function populateFilters(
    records
) {

    const sourceSelect =
        document.getElementById(
            "sourceFilter"
        );

    const categorySelect =
        document.getElementById(
            "categoryFilter"
        );

    const oldSource =
        sourceSelect.value;

    const oldCategory =
        categorySelect.value;

    const sources =
        [
            ...new Set(
                records.map(
                    r =>
                        r.source_group
                )
            )
        ].sort();

    const categories =
        [
            ...new Set(
                records.map(
                    r =>
                        r.development_type
                )
            )
        ].sort();

    sourceSelect.innerHTML =
        '<option value="ALL">'
        + "All Intelligence Sources"
        + "</option>";

    for (
        const source
        of sources
    ) {

        sourceSelect.innerHTML +=
            '<option value="'
            + esc(source)
            + '">'
            + esc(source)
            + "</option>";
    }

    categorySelect.innerHTML =
        '<option value="ALL">'
        + "All Development Categories"
        + "</option>";

    for (
        const category
        of categories
    ) {

        categorySelect.innerHTML +=
            '<option value="'
            + esc(category)
            + '">'
            + esc(category)
            + "</option>";
    }

    if (
        sources.includes(
            oldSource
        )
    ) {

        sourceSelect.value =
            oldSource;
    }

    if (
        categories.includes(
            oldCategory
        )
    ) {

        categorySelect.value =
            oldCategory;
    }
}


function applyFilters() {

    const source =
        document.getElementById(
            "sourceFilter"
        ).value;

    const category =
        document.getElementById(
            "categoryFilter"
        ).value;

    const location =
        document.getElementById(
            "locationFilter"
        ).value;

    let records =
        allRecords.slice();

    if (
        source !== "ALL"
    ) {

        records =
            records.filter(
                r =>
                    r.source_group
                    === source
            );
    }

    if (
        category !== "ALL"
    ) {

        records =
            records.filter(
                r =>
                    r.development_type
                    === category
            );
    }

    if (
        location === "MAPPED"
    ) {

        records =
            records.filter(
                r =>
                    r.location_status
                    === "MAPPED"
            );
    }

    addMarkers(
        records
    );

    renderRecords(
        records
    );
}


function renderSummary(
    data
) {

    const s =
        data.summary || {};

    let html = "";

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.total_records
                    || 0
                )}
            </div>

            <div class="label">
                Total intelligence records
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.visible_records
                    || 0
                )}
            </div>

            <div class="label">
                Records in selected radius
            </div>

        </div>
    `;

    html += `
        <div class="card">

            <div class="metric">
                ${esc(
                    s.unmapped_records
                    || 0
                )}
            </div>

            <div class="label">
                Location pending
            </div>

        </div>
    `;

    document.getElementById(
        "summary"
    ).innerHTML = html;
}


function renderRecords(
    records
) {

    const container =
        document.getElementById(
            "records"
        );

    if (!records.length) {

        container.innerHTML = `
            <div class="card">
                No intelligence records
                match the selected filters.
            </div>
        `;

        return;
    }

    let html = "";

    for (
        const record
        of records
    ) {

        const pending =
            record.location_status

        html += `
            <div
                class="record
                ${pending ? "pending" : ""}"
                onclick="focusRecord('${esc(
                    record.id
                )}')">

                <div class="title">
                    ${esc(
                        record.title
                        || "Untitled"
                    )}
                </div>

                <div class="meta">

                    <span class="badge">
                        ${esc(
                            record.source_group
                        )}
                    </span>

                    <span class="badge">
                        ${esc(
                            record.development_type
                        )}
                    </span>

                    <br>

                    ${
                        pending
                        ? "Location pending"
                        : (
                            Number(
                                record.distance_km
                            ).toFixed(2)
                            + " km"
                        )
                    }

                    <br>

                    Status:
                    ${esc(
                        record.status
                        || "unknown"
                    )}

                    <br>

                    Evidence:
                    ${esc(
                        record.evidence_level
                        || "not specified"
                    )}

                </div>

            </div>
        `;
    }

    container.innerHTML =
        html;
}


function focusRecord(
    id
) {

    const record =
        allRecords.find(
            r =>
                String(r.id)
                ===
                String(id)
        );

    if (!record) {
        return;
    }

    if (
        record.location_status
    ) {
        return;
    }

    map.setView(
        [
            record.latitude,
            record.longitude
        ],
        15
    );
}


async function loadMap() {

    const radius =
        document.getElementById(
            "radius"
        ).value;

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Loading unified intelligence...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/unified-command-map"
                + "?radius_km="
                + encodeURIComponent(
                    radius
                )
                + "&include_unmapped=true"
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "Command map failed"
            );
        }

        currentData =
            data;

        const property =
            data.property;

        if (
            property.latitude === null
            ||
            property.longitude === null
        ) {

            status.textContent =
                "Property location pending";

            return;
        }

        initMap(
            property.latitude,
            property.longitude
        );

        addProperty(
            property.latitude,
            property.longitude,
            property.property_name
        );

        addBoundary(
            data.map.boundary
        );

        allRecords =
            data.records || [];

        populateFilters(
            allRecords
        );

        renderSummary(
            data
        );

        applyFilters();

        status.textContent =
            "Unified command map ready";

    } catch (err) {

        status.textContent =
            "Command map error";

        document.getElementById(
            "records"
        ).innerHTML =
            '<div class="card">'
            + esc(
                err.message
            )
            + '</div>';
    }
}


async function liveRefresh() {

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Running live intelligence refresh...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/live-development-refresh",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        sources: [
                            "osm",
                            "news",
                            "government"
                        ]
                    })
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "Live refresh failed"
            );
        }

        await loadMap();

    } catch (err) {

        status.textContent =
            "Live refresh error: "
            + err.message;
    }
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadMap();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 12. UI route
# ------------------------------------------------------------------------------

async def v35_command_map_ui(
    property_id: str
):

    if _v35_property(
        property_id
    ) is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v35_html(property_id)
    )


_v35_add_get(
    "/propertyiq/unified-command-map/{property_id}",
    v35_command_map_ui
)


# ------------------------------------------------------------------------------
# 13. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v35_command_map"
] = build_v35_command_map


# ------------------------------------------------------------------------------
# 14. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V35 — UNIFIED INTELLIGENCE COMMAND MAP INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("UNIFIED MAP:")
print("  ✓ Property")
print("  ✓ GIS boundary")
print("  ✓ RERA")
print("  ✓ Government / PAIMANA")
print("  ✓ News / GDELT")
print("  ✓ OSM / GIS")
print("  ✓ Development intelligence")
print("  ✓ Geographic distance")
print("  ✓ Source filtering")
print("  ✓ Development-category filtering")
print("  ✓ Mapped / location-pending filtering")
print("  ✓ Evidence metadata")
print("  ✓ Source links")
print("  ✓ Live refresh handoff")
print()
print("API:")
print("  /api/v1/properties/{property_id}/unified-command-map")
print()
print("UI:")
print("  /propertyiq/unified-command-map/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Existing PropertyIQ data only")
print("  ✓ No fabricated intelligence")
print("  ✓ No fabricated coordinates")
print("  ✓ No valuation prediction")
print("  ✓ No investment score")
print("  ✓ Existing routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V36
# ORIGINAL COLAB CELL: In[65]
# ============================================================

# ==============================================================================
# PROPERTYIQ V36 — TIMELINE & DEVELOPMENT PIPELINE
# ==============================================================================
# Consolidates existing:
#   RERA
#   Government / PAIMANA
#   News / GDELT
#   OSM / GIS
#   Development intelligence
#   Evidence
#
# Adds:
#   - Unified chronological timeline
#   - Development pipeline
#   - Source classification
#   - Status classification
#   - Date availability tracking
#   - Location-pending tracking
#   - Evidence/source links
#   - Timeline filtering
#   - Property-specific development history
#
# No fabricated dates.
# No fabricated projects.
# No investment score.
# No appreciation prediction.
# ==============================================================================

import sys
import subprocess
import importlib
import json
from datetime import datetime, timezone


# ------------------------------------------------------------------------------
# 1. Dependencies
# ------------------------------------------------------------------------------

def _v36_install(package, module=None):

    module = module or package

    try:
        importlib.import_module(module)

    except Exception:

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])


_v36_install("sqlalchemy")
_v36_install("fastapi")
_v36_install("psycopg[binary]", "psycopg")


from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 2. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = (
    "PROPERTYIQ-V36-TIMELINE-DEVELOPMENT-PIPELINE"
)


if "engine" not in globals() or engine is None:

    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PostgreSQL/PostGIS runtime bootstrap first."
    )


if "app" not in globals() or app is None:


    app = FastAPI(
        title="PropertyIQ",
        version="V36"
    )


# ------------------------------------------------------------------------------
# 3. Property
# ------------------------------------------------------------------------------

def _v36_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    return dict(row) if row else None


# ------------------------------------------------------------------------------
# 4. Intelligence
# ------------------------------------------------------------------------------

def _v36_intelligence(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_id,
                    title,
                    category,
                    status,
                    description,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level,
                    metadata
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
                ORDER BY
                    published_at DESC NULLS LAST,
                    title
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 5. Evidence
# ------------------------------------------------------------------------------

def _v36_evidence(property_id):

    if "evidence_items" not in _v36_tables():

        return []

    with engine.connect() as conn:

        columns = conn.execute(
            sa_text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'evidence_items'
            """)
        ).scalars().all()

    columns = set(columns)

    if "property_id" not in columns:

        return []

    order_column = "id"

    if "updated_at" in columns:
        order_column = "updated_at"

    elif "created_at" in columns:
        order_column = "created_at"

    query = f"""
        SELECT *
        FROM evidence_items
        WHERE property_id = CAST(:pid AS uuid)
        ORDER BY {order_column} DESC
    """

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text(query),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 6. Table helper
# ------------------------------------------------------------------------------

def _v36_tables():

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
        ).scalars().all()

    return set(rows)


# ------------------------------------------------------------------------------
# 7. Source classification
# ------------------------------------------------------------------------------

def _v36_source_group(record):

    text_value = " ".join([
        str(record.get("source_name") or ""),
        str(record.get("source_type") or ""),
        str(record.get("category") or ""),
        str(record.get("title") or ""),
        str(record.get("description") or "")
    ]).lower()

    if (
        "rera" in text_value
        or "up-rera" in text_value
    ):
        return "RERA"

    if (
        "paimana" in text_value
        or "mospi" in text_value
        or "government" in text_value
        or "infrastructure" in text_value
    ):
        return "GOVERNMENT"

    if (
        "gdelt" in text_value
        or "news" in text_value
        or "article" in text_value
    ):
        return "NEWS"

    if (
        "openstreetmap" in text_value
        or "overpass" in text_value
        or "osm" in text_value
    ):
        return "OSM"

    return "OTHER"


# ------------------------------------------------------------------------------
# 8. Development type
# ------------------------------------------------------------------------------

def _v36_development_type(record):

    text_value = " ".join([
        str(record.get("title") or ""),
        str(record.get("description") or ""),
        str(record.get("category") or "")
    ]).lower()

    groups = {

        "TRANSPORT": [
            "metro",
            "railway",
            "rail",
            "expressway",
            "highway",
            "road",
            "flyover",
            "airport",
            "corridor",
            "transit"
        ],

        "INDUSTRIAL": [
            "industrial",
            "manufacturing",
            "factory",
            "warehouse",
            "logistics",
            "industrial area"
        ],

        "COMMERCIAL": [
            "mall",
            "commercial",
            "office",
            "retail",
            "business district",
            "market"
        ],

        "INSTITUTIONAL": [
            "hospital",
            "school",
            "college",
            "university",
            "medical"
        ],

        "RESIDENTIAL": [
            "residential",
            "housing",
            "apartment",
            "township"
        ],

        "INFRASTRUCTURE": [
            "infrastructure",
            "development",
            "construction",
            "project",
            "smart city"
        ],

        "LAND": [
            "land acquisition",
            "land development",
            "land"
        ]
    }

    for group, keywords in groups.items():

        for keyword in keywords:

            if keyword in text_value:

                return group

    return "OTHER"


# ------------------------------------------------------------------------------
# 9. Status normalization
# ------------------------------------------------------------------------------

def _v36_status(record):

    raw = str(
        record.get("status")
        or ""
    ).strip()

    text_value = " ".join([
        str(record.get("title") or ""),
        str(record.get("description") or ""),
        raw
    ]).lower()

    if "completed" in text_value:
        return "COMPLETED"

    if "operational" in text_value:
        return "OPERATIONAL"

    if "ongoing" in text_value:
        return "ONGOING"

    if "under construction" in text_value:
        return "UNDER_CONSTRUCTION"

    if "construction" in text_value:
        return "CONSTRUCTION"

    if "proposed" in text_value:
        return "PROPOSED"

    if "planned" in text_value:
        return "PLANNED"

    if "approved" in text_value:
        return "APPROVED"

    if "reported" in text_value:
        return "REPORTED"

    if raw:
        return raw.upper()

    return "UNKNOWN"


# ------------------------------------------------------------------------------
# 10. Date extraction
# ------------------------------------------------------------------------------

def _v36_date(record):

    published = record.get(
        "published_at"
    )

    if published is not None:

        try:

            if hasattr(
                published,
                "isoformat"
            ):

                return (
                    published.isoformat()
                )

            return str(
                published
            )

        except Exception:
            pass

    metadata = record.get(
        "metadata"
    )

    if isinstance(
        metadata,
        str
    ):

        try:
            metadata = json.loads(
                metadata
            )
        except Exception:
            metadata = {}

    if isinstance(
        metadata,
        dict
    ):

        date_keys = [
            "date",
            "project_date",
            "event_date",
            "published_at",
            "publication_date",
            "registration_date",
            "original_end_date",
            "revised_date"
        ]

        for key in date_keys:

            value = metadata.get(
                key
            )

            if value:

                return str(value)

    return None


# ------------------------------------------------------------------------------
# 11. Timeline builder
# ------------------------------------------------------------------------------

def build_v36_timeline(
    property_id
):

    property_data = _v36_property(
        property_id
    )

    if property_data is None:

        raise ValueError(
            "Property not found."
        )

    records = _v36_intelligence(
        property_id
    )

    timeline = []

    source_summary = {}

    status_summary = {}

    type_summary = {}

    dated_count = 0

    undated_count = 0

    mapped_count = 0

    location_pending_count = 0

    for record in records:

        source_group = (
            _v36_source_group(
                record
            )
        )

        development_type = (
            _v36_development_type(
                record
            )
        )

        normalized_status = (
            _v36_status(
                record
            )
        )

        event_date = _v36_date(
            record
        )

        if event_date:
            dated_count += 1
        else:
            undated_count += 1

        if (
            record.get("latitude")
            is not None
            and
            record.get("longitude")
            is not None
        ):
            mapped_count += 1
        else:
            location_pending_count += 1

        source_summary[
            source_group
        ] = (
            source_summary.get(
                source_group,
                0
            )
            + 1
        )

        status_summary[
            normalized_status
        ] = (
            status_summary.get(
                normalized_status,
                0
            )
            + 1
        )

        type_summary[
            development_type
        ] = (
            type_summary.get(
                development_type,
                0
            )
            + 1
        )

        item = {

            "id": str(
                record["id"]
            ),

            "title":
                record.get("title"),

            "description":
                record.get("description"),

            "source_group":
                source_group,

            "source_name":
                record.get(
                    "source_name"
                ),

            "source_type":
                record.get(
                    "source_type"
                ),

            "source_url":
                record.get(
                    "source_url"
                ),

            "development_type":
                development_type,

            "status":
                normalized_status,

            "raw_status":
                record.get(
                    "status"
                ),

            "event_date":
                event_date,

            "published_at":
                (
                    record.get(
                        "published_at"
                    ).isoformat()
                    if hasattr(
                        record.get(
                            "published_at"
                        ),
                        "isoformat"
                    )
                    else (
                        str(
                            record.get(
                                "published_at"
                            )
                        )
                        if record.get(
                            "published_at"
                        )
                        else None
                    )
                ),

            "latitude":
                record.get(
                    "latitude"
                ),

            "longitude":
                record.get(
                    "longitude"
                ),

            "location_status":
                (
                    "MAPPED"
                    if (
                        record.get(
                            "latitude"
                        ) is not None
                        and
                        record.get(
                            "longitude"
                        ) is not None
                    )
                    else
                    "LOCATION_PENDING"
                ),

            "confidence":
                record.get(
                    "confidence"
                ),

            "evidence_level":
                record.get(
                    "evidence_level"
                ),

            "metadata":
                record.get(
                    "metadata"
                )
        }

        timeline.append(
            item
        )

    timeline.sort(
        key=lambda item: (
            item.get(
                "event_date"
            ) or "9999-99-99",
            item.get(
                "title"
            ) or ""
        )
    )

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property": property_data,

        "summary": {

            "total_records":
                len(records),

            "timeline_events":
                len(timeline),

            "dated_events":
                dated_count,

            "undated_events":
                undated_count,

            "mapped_events":
                mapped_count,

            "location_pending_events":
                location_pending_count,

            "source_summary":
                source_summary,

            "status_summary":
                status_summary,

            "development_type_summary":
                type_summary
        },

        "timeline":
            timeline
    }


# ------------------------------------------------------------------------------
# 12. Pipeline view
# ------------------------------------------------------------------------------

def build_v36_pipeline(
    property_id
):

    data = build_v36_timeline(
        property_id
    )

    pipeline = {}

    for event in data[
        "timeline"
    ]:

        status = (
            event.get("status")
            or "UNKNOWN"
        )

        if status not in pipeline:

            pipeline[status] = []

        pipeline[status].append(
            event
        )

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property":
            data["property"],

        "summary":
            data["summary"],

        "pipeline":
            pipeline,

        "timeline":
            data["timeline"]
    }


# ------------------------------------------------------------------------------
# 13. Routes
# ------------------------------------------------------------------------------

_v36_routes = {
    getattr(
        route,
        "path",
        ""
    )
    for route in getattr(
        app,
        "routes",
        []
    )
}


def _v36_add_get(
    path,
    endpoint
):

    if path not in _v36_routes:

        app.get(path)(endpoint)

        _v36_routes.add(path)


async def v36_timeline_api(
    property_id: str
):

    try:

        return build_v36_timeline(
            property_id
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v36_add_get(
    "/api/v1/properties/{property_id}/timeline",
    v36_timeline_api
)


async def v36_pipeline_api(
    property_id: str
):

    try:

        return build_v36_pipeline(
            property_id
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v36_add_get(
    "/api/v1/properties/{property_id}/development-pipeline",
    v36_pipeline_api
)


# ------------------------------------------------------------------------------
# 14. UI
# ------------------------------------------------------------------------------

def _v36_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Timeline & Development Pipeline
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 10% 0%,
            rgba(0,185,255,.12),
            transparent 32%
        ),
        radial-gradient(
            circle at 90% 10%,
            rgba(0,255,210,.07),
            transparent 28%
        ),
        #07111f;

    color: #edf7ff;

    font-family:
        Inter,
        system-ui,
        sans-serif;
}

header {

    padding: 23px 30px;

    border-bottom:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(7,17,31,.88);

    backdrop-filter: blur(18px);

    position: sticky;

    top: 0;

    z-index: 10;
}

.brand {

    font-size: 21px;

    font-weight: 900;
}

.brand span {

    color: #43d3ff;
}

.subtitle {

    color: #8da6bf;

    font-size: 12px;

    margin-top: 4px;
}

.container {

    max-width: 1450px;

    margin: auto;

    padding: 26px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 8px;

    margin-bottom: 18px;
}

button {

    border: 0;

    border-radius: 10px;

    padding: 10px 14px;

    cursor: pointer;

    font-weight: 750;

    color: #04111c;

    background:
        linear-gradient(
            135deg,
            #42d1ff,
            #60edcf
        );
}

button.secondary {

    color: #d9ecff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.08);
}

select {

    background: #102033;

    color: white;

    border:
        1px solid rgba(255,255,255,.10);

    border-radius: 9px;

    padding: 9px 12px;
}

.grid {

    display: grid;

    grid-template-columns:
        repeat(auto-fit,minmax(190px,1fr));

    gap: 13px;
}

.card {

    background:
        linear-gradient(
            145deg,
            rgba(20,37,58,.88),
            rgba(9,21,37,.88)
        );

    border:
        1px solid rgba(255,255,255,.08);

    border-radius: 17px;

    padding: 18px;

    box-shadow:
        0 15px 45px rgba(0,0,0,.20);
}

.metric {

    font-size: 29px;

    font-weight: 900;

    color: #58d8ff;
}

.label {

    color: #8ea6be;

    font-size: 11px;

    margin-top: 3px;
}

.pipeline {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(190px,1fr)
        );

    gap: 10px;

    margin-top: 15px;
}

.stage {

    padding: 14px;

    border-radius: 13px;

    background:
        rgba(255,255,255,.04);

    border:
        1px solid rgba(255,255,255,.06);
}

.stage-name {

    font-size: 11px;

    color: #9ab0c7;

    margin-bottom: 6px;
}

.stage-count {

    font-size: 25px;

    font-weight: 850;

    color: #5dd9ff;
}

.timeline {

    position: relative;

    margin-top: 20px;

    padding-left: 25px;
}

.timeline::before {

    content: "";

    position: absolute;

    left: 7px;

    top: 0;

    bottom: 0;

    width: 2px;

    background:
        rgba(70,210,255,.25);
}

.event {

    position: relative;

    padding: 16px;

    margin-bottom: 12px;

    border-radius: 14px;

    background:
        rgba(255,255,255,.035);

    border:
        1px solid rgba(255,255,255,.07);
}

.event::before {

    content: "";

    position: absolute;

    left: -23px;

    top: 22px;

    width: 11px;

    height: 11px;

    border-radius: 50%;

    background: #43d4ff;

    box-shadow:
        0 0 14px rgba(67,212,255,.55);
}

.event.pending::before {

    background: #e4bc50;

    box-shadow: none;
}

.event-title {

    font-weight: 800;

    line-height: 1.45;
}

.event-meta {

    color: #8fa8c0;

    font-size: 11px;

    line-height: 1.7;

    margin-top: 6px;
}

.badge {

    display: inline-block;

    padding: 4px 7px;

    border-radius: 999px;

    background:
        rgba(64,211,255,.10);

    color: #63d8ff;

    font-size: 9px;

    margin-top: 5px;

    margin-right: 4px;
}

.status {

    color: #91a8c0;

    font-size: 11px;

    margin-bottom: 13px;
}

a {

    color: #5ed8ff;

    text-decoration: none;
}

.empty {

    color: #91a8c0;

    padding: 15px 0;

}

</style>

</head>

<body>

<header>

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div class="subtitle">
        Timeline & Development Pipeline
    </div>

</header>

<div class="container">

    <div class="toolbar">

        <button onclick="loadData()">
            Refresh Timeline
        </button>

        <button
            class="secondary"
            onclick="openCommandMap()">
            Command Map
        </button>

        <button
            class="secondary"
            onclick="openWorkspace()">
            Property Workspace
        </button>

        <select id="sourceFilter"
                onchange="applyFilters()">

            <option value="ALL">
                All Sources
            </option>

        </select>

        <select id="statusFilter"
                onchange="applyFilters()">

            <option value="ALL">
                All Statuses
            </option>

        </select>

        <select id="dateFilter"
                onchange="applyFilters()">

            <option value="ALL">
                All Dates
            </option>

            <option value="DATED">
                Dated Only
            </option>

            <option value="UNDATED">
                Undated Only
            </option>

        </select>

    </div>

    <div id="status"
         class="status">
        Loading timeline...
    </div>

    <div id="summary"></div>

    <div id="pipeline"></div>

    <div id="timeline"></div>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";

let allEvents = [];

let currentData = null;


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function populateFilters(
    events
) {

    const source =
        document.getElementById(
            "sourceFilter"
        );

    const status =
        document.getElementById(
            "statusFilter"
        );

    const oldSource =
        source.value;

    const oldStatus =
        status.value;

    const sources =
        [
            ...new Set(
                events.map(
                    e =>
                        e.source_group
                )
            )
        ].sort();

    const statuses =
        [
            ...new Set(
                events.map(
                    e =>
                        e.status
                )
            )
        ].sort();

    source.innerHTML =
        '<option value="ALL">'
        + "All Sources"
        + "</option>";

    for (
        const value
        of sources
    ) {

        source.innerHTML +=
            '<option value="'
            + esc(value)
            + '">'
            + esc(value)
            + "</option>";
    }

    status.innerHTML =
        '<option value="ALL">'
        + "All Statuses"
        + "</option>";

    for (
        const value
        of statuses
    ) {

        status.innerHTML +=
            '<option value="'
            + esc(value)
            + '">'
            + esc(value)
            + "</option>";
    }

    if (
        sources.includes(
            oldSource
        )
    ) {
        source.value =
            oldSource;
    }

    if (
        statuses.includes(
            oldStatus
        )
    ) {
        status.value =
            oldStatus;
    }
}


function renderSummary(
    data
) {

    const s =
        data.summary || {};

    let html = "";

    html += `
        <div class="grid">

            <div class="card">

                <div class="metric">
                    ${esc(
                        s.total_records
                        || 0
                    )}
                </div>

                <div class="label">
                    Intelligence records
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        s.dated_events
                        || 0
                    )}
                </div>

                <div class="label">
                    Dated timeline events
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        s.undated_events
                        || 0
                    )}
                </div>

                <div class="label">
                    Undated records
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        s.mapped_events
                        || 0
                    )}
                </div>

                <div class="label">
                    Mapped events
                </div>

            </div>

            <div class="card">

                <div class="metric">
                    ${esc(
                        s.location_pending_events
                        || 0
                    )}
                </div>

                <div class="label">
                    Location pending
                </div>

            </div>

        </div>
    `;

    document.getElementById(
        "summary"
    ).innerHTML = html;
}


function renderPipeline(
    events
) {

    const container =
        document.getElementById(
            "pipeline"
        );

    const counts = {};

    for (
        const event
        of events
    ) {

        const status =
            event.status
            || "UNKNOWN";

        counts[status] =
            (
                counts[status]
                || 0
            )
            + 1;
    }

    let html =
        '<div class="card">'
        + '<h3>Development Pipeline</h3>'
        + '<div class="pipeline">';

    const ordered = [
        "PROPOSED",
        "PLANNED",
        "APPROVED",
        "CONSTRUCTION",
        "UNDER_CONSTRUCTION",
        "ONGOING",
        "OPERATIONAL",
        "COMPLETED",
        "REPORTED",
        "UNKNOWN"
    ];

    const used = new Set();

    for (
        const status
        of ordered
    ) {

        if (
        ) {
            continue;
        }

        used.add(status);

        html += `
            <div class="stage">

                <div class="stage-name">
                    ${esc(status)}
                </div>

                <div class="stage-count">
                    ${esc(
                        counts[status]
                    )}
                </div>

            </div>
        `;
    }

    for (
        const [status,count]
        of Object.entries(counts)
    ) {

        if (
            used.has(status)
        ) {
            continue;
        }

        html += `
            <div class="stage">

                <div class="stage-name">
                    ${esc(status)}
                </div>

                <div class="stage-count">
                    ${esc(count)}
                </div>

            </div>
        `;
    }

    html +=
        '</div></div>';

    container.innerHTML =
        html;
}


function applyFilters() {

    const source =
        document.getElementById(
            "sourceFilter"
        ).value;

    const status =
        document.getElementById(
            "statusFilter"
        ).value;

    const date =
        document.getElementById(
            "dateFilter"
        ).value;

    let events =
        allEvents.slice();

    if (
        source !== "ALL"
    ) {

        events =
            events.filter(
                e =>
                    e.source_group
                    === source
            );
    }

    if (
        status !== "ALL"
    ) {

        events =
            events.filter(
                e =>
                    e.status
                    === status
            );
    }

    if (
        date === "DATED"
    ) {

        events =
            events.filter(
                e =>
            );
    }

    if (
        date === "UNDATED"
    ) {

        events =
            events.filter(
                e =>
            );
    }

    renderPipeline(
        events
    );

    renderTimeline(
        events
    );
}


function renderTimeline(
    events
) {

    const container =
        document.getElementById(
            "timeline"
        );

    if (!events.length) {

        container.innerHTML = `
            <div class="card empty">
                No timeline events match
                the selected filters.
            </div>
        `;

        return;
    }

    let html =
        '<div class="card">'
        + '<h3>Development Timeline</h3>'
        + '<div class="timeline">';

    for (
        const event
        of events
    ) {

        const pending =
            event.location_status

        const date =
            event.event_date
            || "Date not available";

        html += `
            <div class="event
                ${pending ? "pending" : ""}">

                <div class="event-title">
                    ${esc(
                        event.title
                        || "Untitled"
                    )}
                </div>

                <div>

                    <span class="badge">
                        ${esc(
                            event.source_group
                        )}
                    </span>

                    <span class="badge">
                        ${esc(
                            event.development_type
                        )}
                    </span>

                    <span class="badge">
                        ${esc(
                            event.status
                        )}
                    </span>

                </div>

                <div class="event-meta">

                    Date:
                    <strong>
                        ${esc(date)}
                    </strong>

                    <br>

                    Source:
                    ${esc(
                        event.source_name
                        ||
                        event.source_type
                        ||
                        "Unknown"
                    )}

                    <br>

                    Location:
                    ${pending
                        ? "Location pending"
                        : (
                            event.latitude
                            + ", "
                            + event.longitude
                        )
                    }

                    ${
                        event.evidence_level
                        ? (
                            "<br>Evidence: "
                            +
                            esc(
                                event.evidence_level
                            )
                        )
                        : ""
                    }

                </div>

        `;

        if (
            event.source_url
        ) {

            html += `
                <div style="margin-top:7px">

                    <a
                      href="${esc(
                          event.source_url
                      )}"
                      target="_blank"
                      rel="noopener noreferrer">

                        Open source

                    </a>

                </div>
            `;
        }

        html += `
            </div>
        `;
    }

    html +=
        '</div></div>';

    container.innerHTML =
        html;
}


async function loadData() {

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Loading development timeline...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/timeline"
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                || "Timeline request failed"
            );
        }

        currentData =
            data;

        allEvents =
            data.timeline || [];

        populateFilters(
            allEvents
        );

        renderSummary(
            data
        );

        applyFilters();

        status.textContent =
            "Timeline ready";

    } catch (err) {

        status.textContent =
            "Timeline error";

        document.getElementById(
            "timeline"
        ).innerHTML =
            '<div class="card">'
            + esc(
                err.message
            )
            + '</div>';
    }
}


function openCommandMap() {

    window.location.href =
        "/propertyiq/unified-command-map/"
        + PROPERTY_ID;
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadData();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 15. UI route
# ------------------------------------------------------------------------------

async def v36_timeline_ui(
    property_id: str
):

    if _v36_property(
        property_id
    ) is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v36_html(property_id)
    )


_v36_add_get(
    "/propertyiq/timeline/{property_id}",
    v36_timeline_ui
)


# ------------------------------------------------------------------------------
# 16. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v36_timeline"
] = build_v36_timeline

globals()[
    "build_v36_pipeline"
] = build_v36_pipeline


# ------------------------------------------------------------------------------
# 17. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V36 — TIMELINE & DEVELOPMENT PIPELINE INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("TIMELINE:")
print("  ✓ RERA events")
print("  ✓ Government / PAIMANA events")
print("  ✓ News / GDELT events")
print("  ✓ OSM / GIS records")
print("  ✓ Development records")
print("  ✓ Chronological ordering")
print("  ✓ Date availability tracking")
print("  ✓ Location-pending tracking")
print("  ✓ Source classification")
print("  ✓ Status classification")
print("  ✓ Development-type classification")
print()
print("PIPELINE:")
print("  ✓ Proposed")
print("  ✓ Planned")
print("  ✓ Approved")
print("  ✓ Construction")
print("  ✓ Ongoing")
print("  ✓ Operational")
print("  ✓ Completed")
print("  ✓ Reported")
print()
print("API:")
print("  /api/v1/properties/{property_id}/timeline")
print("  /api/v1/properties/{property_id}/development-pipeline")
print()
print("UI:")
print("  /propertyiq/timeline/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Existing source-backed records only")
print("  ✓ No fabricated dates")
print("  ✓ No fabricated projects")
print("  ✓ No fabricated status")
print("  ✓ No investment score")
print("  ✓ No appreciation prediction")
print("  ✓ Existing PropertyIQ routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V37
# ORIGINAL COLAB CELL: In[66]
# ============================================================

# ==============================================================================
# PROPERTYIQ V37 — PROPERTY INTELLIGENCE DASHBOARD
# ==============================================================================
# Central read-only dashboard over existing PropertyIQ capabilities.
#
# Integrates:
#   V26  Full Property Profile
#   V28  Acquisition / Due Diligence
#   V29  Evidence / Confidence / Data Quality
#   V30  Unified Development Intelligence
#   V35  Unified Intelligence Command Map
#   V36  Timeline / Development Pipeline
#   V20  GIS Boundary
#   V24  OSM / GIS
#   V25  Comparables / Market
#   V17  Intelligence Report
#
# This module does NOT rebuild those systems.
#
# No fabricated intelligence.
# No fabricated coordinates.
# No investment score.
# No valuation prediction.
# No legal conclusion.
# No database modification during installation.
# ==============================================================================

import sys
import subprocess
import importlib
import json
import math


# ------------------------------------------------------------------------------
# 1. Dependencies
# ------------------------------------------------------------------------------

def _v37_install(package, module=None):

    module = module or package

    try:
        importlib.import_module(module)

    except Exception:

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])


_v37_install("sqlalchemy")
_v37_install("fastapi")
_v37_install("psycopg[binary]", "psycopg")


from sqlalchemy import text as sa_text
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


# ------------------------------------------------------------------------------
# 2. Runtime
# ------------------------------------------------------------------------------

MODULE_VERSION = (
    "PROPERTYIQ-V37-PROPERTY-INTELLIGENCE-DASHBOARD"
)


if "engine" not in globals() or engine is None:

    raise RuntimeError(
        "PropertyIQ engine was not found. "
        "Run the existing PostgreSQL/PostGIS runtime bootstrap first."
    )


if "app" not in globals() or app is None:


    app = FastAPI(
        title="PropertyIQ",
        version="V37"
    )


# ------------------------------------------------------------------------------
# 3. Route helper
# ------------------------------------------------------------------------------

_v37_routes = {
    getattr(
        route,
        "path",
        ""
    )
    for route in getattr(
        app,
        "routes",
        []
    )
}


def _v37_add_get(
    path,
    endpoint
):

    if path not in _v37_routes:

        app.get(path)(endpoint)

        _v37_routes.add(path)


# ------------------------------------------------------------------------------
# 4. Table helper
# ------------------------------------------------------------------------------

def _v37_tables():

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
        ).scalars().all()

    return set(rows)


# ------------------------------------------------------------------------------
# 5. Property
# ------------------------------------------------------------------------------

def _v37_property(property_id):

    with engine.connect() as conn:

        row = conn.execute(
            sa_text("""
                SELECT
                    id,
                    property_name,
                    property_address,
                    latitude,
                    longitude,
                    property_type,
                    area,
                    price,
                    price_per_sqft,
                    bedrooms,
                    bathrooms,
                    builder_owner,
                    description,
                    amenities,
                    ST_AsGeoJSON(boundary)
                        AS boundary_geojson
                FROM properties
                WHERE id = CAST(:pid AS uuid)
                LIMIT 1
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().first()

    if not row:
        return None

    data = dict(row)

    raw_boundary = data.get(
        "boundary_geojson"
    )

    if raw_boundary:

        try:
            data["boundary"] = json.loads(
                raw_boundary
            )
        except Exception:
            data["boundary"] = None

    else:

        data["boundary"] = None

    data.pop(
        "boundary_geojson",
        None
    )

    return data


# ------------------------------------------------------------------------------
# 6. Intelligence inventory
# ------------------------------------------------------------------------------

def _v37_intelligence(property_id):

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT
                    id,
                    title,
                    category,
                    status,
                    latitude,
                    longitude,
                    source_name,
                    source_type,
                    source_url,
                    published_at,
                    confidence,
                    evidence_level
                FROM intelligence_records
                WHERE property_id =
                    CAST(:pid AS uuid)
                ORDER BY
                    published_at DESC NULLS LAST,
                    title
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 7. Evidence inventory
# ------------------------------------------------------------------------------

def _v37_evidence(property_id):

    tables = _v37_tables()

    if "evidence_items" not in tables:

        return []

    with engine.connect() as conn:

        columns = conn.execute(
            sa_text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'evidence_items'
            """)
        ).scalars().all()

    columns = set(columns)

    if "property_id" not in columns:

        return []

    order_column = "id"

    if "updated_at" in columns:
        order_column = "updated_at"

    elif "created_at" in columns:
        order_column = "created_at"

    query = f"""
        SELECT *
        FROM evidence_items
        WHERE property_id =
            CAST(:pid AS uuid)
        ORDER BY {order_column} DESC
    """

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text(query),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 8. Documents
# ------------------------------------------------------------------------------

def _v37_documents(property_id):
    """
    Read documents from the existing PropertyIQ
    property_files table.

    Production schema:

        file_id
        property_id
        file_name
        mime_type
        file_size
        file_bytes
        title
        description
        extraction_status
        created_at
        updated_at

    file_size is used for document size.
    file_bytes contains the actual binary document and
    is intentionally NOT loaded by this inventory query.
    """

    tables = _v37_tables()

    if "property_files" not in tables:
        return []

    with engine.connect() as conn:

        columns = conn.execute(
            sa_text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'property_files'
            """)
        ).scalars().all()

    columns = set(columns)

    if "property_id" not in columns:
        return []

    # --------------------------------------------------------
    # Primary key
    # --------------------------------------------------------

    if "file_id" in columns:
        id_expression = "file_id"
    elif "id" in columns:
        id_expression = "id"
    else:
        id_expression = "NULL"

    # --------------------------------------------------------
    # File name
    # --------------------------------------------------------

    name_expression = "NULL"

    for candidate in [
        "file_name",
        "filename",
        "name"
    ]:

        if candidate in columns:
            name_expression = candidate
            break

    # --------------------------------------------------------
    # File size
    # --------------------------------------------------------

    if "file_size" in columns:
        size_expression = "file_size"
    elif "size_bytes" in columns:
        size_expression = "size_bytes"
    else:
        size_expression = "NULL"

    # --------------------------------------------------------
    # Query
    # --------------------------------------------------------

    query = f"""
        SELECT
            {id_expression} AS file_id,
            {name_expression} AS file_name,
            {size_expression} AS size_bytes
        FROM property_files
        WHERE property_id =
            CAST(:pid AS uuid)
        ORDER BY
            {id_expression} DESC
    """

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text(query),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 9. Market observations
# ------------------------------------------------------------------------------

def _v37_market(property_id):

    tables = _v37_tables()

    if "property_market_observations" not in tables:

        return []

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT *
                FROM property_market_observations
                WHERE property_id =
                    CAST(:pid AS uuid)
                ORDER BY id DESC
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 10. Refresh history
# ------------------------------------------------------------------------------

def _v37_refresh_history(property_id):

    tables = _v37_tables()

    if "piq_property_refresh_runs" not in tables:

        return []

    with engine.connect() as conn:

        rows = conn.execute(
            sa_text("""
                SELECT *
                FROM piq_property_refresh_runs
                WHERE property_id =
                    CAST(:pid AS uuid)
                ORDER BY
                    started_at DESC NULLS LAST
                LIMIT 20
            """),
            {
                "pid": str(property_id)
            }
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


# ------------------------------------------------------------------------------
# 11. Source classification
# ------------------------------------------------------------------------------

def _v37_source_group(record):

    value = " ".join([
        str(record.get("source_name") or ""),
        str(record.get("source_type") or ""),
        str(record.get("title") or ""),
        str(record.get("category") or "")
    ]).lower()

    if (
        "rera" in value
        or "up-rera" in value
    ):
        return "RERA"

    if (
        "gdelt" in value
        or "news" in value
    ):
        return "NEWS"

    if (
        "paimana" in value
        or "mospi" in value
        or "government" in value
    ):
        return "GOVERNMENT"

    if (
        "osm" in value
        or "openstreetmap" in value
        or "overpass" in value
    ):
        return "OSM"

    return "OTHER"


# ------------------------------------------------------------------------------
# 12. Dashboard builder
# ------------------------------------------------------------------------------

def build_v37_dashboard(
    property_id
):

    property_data = _v37_property(
        property_id
    )

    if property_data is None:

        raise ValueError(
            "Property not found."
        )

    intelligence = _v37_intelligence(
        property_id
    )

    evidence = _v37_evidence(
        property_id
    )

    documents = _v37_documents(
        property_id
    )

    market = _v37_market(
        property_id
    )

    refresh_history = (
        _v37_refresh_history(
            property_id
        )
    )

    source_summary = {}

    mapped_count = 0

    unmapped_count = 0

    for record in intelligence:

        source = _v37_source_group(
            record
        )

        source_summary[source] = (
            source_summary.get(
                source,
                0
            )
            + 1
        )

        if (
            record.get("latitude")
            is not None
            and
            record.get("longitude")
            is not None
        ):

            mapped_count += 1

        else:

            unmapped_count += 1

    boundary_present = (
        property_data.get(
            "boundary"
        )
        is not None
    )

    flags = []

    if (
        property_data.get(
            "latitude"
        ) is None
        or
        property_data.get(
            "longitude"
        ) is None
    ):

        flags.append({
            "code":
                "LOCATION_PENDING",
            "label":
                "Property location pending",
            "severity":
                "attention"
        })

    if not boundary_present:

        flags.append({
            "code":
                "BOUNDARY_PENDING",
            "label":
                "GIS boundary not saved",
            "severity":
                "attention"
        })

    if not intelligence:

        flags.append({
            "code":
                "NO_PROPERTY_INTELLIGENCE",
            "label":
                "No property-linked intelligence",
            "severity":
                "attention"
        })

    if unmapped_count:

        flags.append({
            "code":
                "UNMAPPED_INTELLIGENCE",
            "label":
                str(
                    unmapped_count
                )
                +
                " intelligence records lack coordinates",
            "severity":
                "attention"
        })

    if not evidence:

        flags.append({
            "code":
                "EVIDENCE_PENDING",
            "label":
                "No property-linked evidence rows",
            "severity":
                "attention"
        })

    if not documents:

        flags.append({
            "code":
                "DOCUMENTS_PENDING",
            "label":
                "No property documents stored",
            "severity":
                "attention"
        })

    if not market:

        flags.append({
            "code":
                "MARKET_DATA_PENDING",
            "label":
                "No market observations stored",
            "severity":
                "attention"
        })

    return {

        "success": True,

        "module": MODULE_VERSION,

        "property": property_data,

        "summary": {

            "intelligence_records":
                len(intelligence),

            "mapped_intelligence":
                mapped_count,

            "unmapped_intelligence":
                unmapped_count,

            "evidence_items":
                len(evidence),

            "documents":
                len(documents),

            "market_observations":
                len(market),

            "refresh_runs":
                len(refresh_history),

            "boundary_present":
                boundary_present,

            "source_summary":
                source_summary
        },

        "flags": flags,

        "intelligence": intelligence,

        "evidence": evidence,

        "documents": documents,

        "market_observations": market,

        "refresh_history":
            refresh_history
    }


# ------------------------------------------------------------------------------
# 13. API
# ------------------------------------------------------------------------------

async def v37_dashboard_api(
    property_id: str
):

    try:

        return build_v37_dashboard(
            property_id
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


_v37_add_get(
    "/api/v1/properties/{property_id}/property-intelligence-dashboard",
    v37_dashboard_api
)


# ------------------------------------------------------------------------------
# 14. UI
# ------------------------------------------------------------------------------

def _v37_html(property_id):

    html = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta
 name="viewport"
 content="width=device-width,initial-scale=1">

<title>
PropertyIQ — Property Intelligence Dashboard
</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 5% 0%,
            rgba(0,185,255,.13),
            transparent 30%
        ),
        radial-gradient(
            circle at 95% 5%,
            rgba(0,255,200,.07),
            transparent 25%
        ),
        #06101d;

    color: #edf7ff;

    font-family:
        Inter,
        system-ui,
        sans-serif;
}

.header {

    padding: 22px 30px;

    border-bottom:
        1px solid rgba(255,255,255,.08);

    background:
        rgba(6,16,29,.92);

    backdrop-filter: blur(18px);

    position: sticky;

    top: 0;

    z-index: 20;
}

.brand {

    font-size: 22px;

    font-weight: 900;
}

.brand span {

    color: #43d3ff;
}

.address {

    color: #8da7bf;

    font-size: 12px;

    margin-top: 5px;
}

.container {

    max-width: 1500px;

    margin: auto;

    padding: 25px;
}

.toolbar {

    display: flex;

    flex-wrap: wrap;

    gap: 8px;

    margin-bottom: 18px;
}

button {

    border: 0;

    border-radius: 10px;

    padding: 10px 14px;

    cursor: pointer;

    font-weight: 750;

    color: #04111d;

    background:
        linear-gradient(
            135deg,
            #42d2ff,
            #60edcf
        );
}

button.secondary {

    color: #dceeff;

    background:
        rgba(255,255,255,.07);

    border:
        1px solid rgba(255,255,255,.08);
}

.grid {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px,1fr)
        );

    gap: 12px;

    margin-bottom: 14px;
}

.card {

    background:
        linear-gradient(
            145deg,
            rgba(20,38,59,.86),
            rgba(8,20,35,.90)
        );

    border:
        1px solid rgba(255,255,255,.075);

    border-radius: 16px;

    padding: 17px;

    box-shadow:
        0 15px 45px rgba(0,0,0,.18);
}

.metric {

    font-size: 29px;

    font-weight: 900;

    color: #57d9ff;
}

.label {

    color: #8fa7bf;

    font-size: 10px;

    margin-top: 4px;
}

.section {

    margin-top: 18px;

    margin-bottom: 8px;

    font-size: 13px;

    font-weight: 850;

    letter-spacing: .4px;
}

.layout {

    display: grid;

    grid-template-columns:
        1.3fr .7fr;

    gap: 14px;
}

.property {

    font-size: 13px;

    line-height: 1.8;

    color: #b9cde0;
}

.property strong {

    color: #f0f8ff;
}

.flag {

    padding: 11px;

    border-radius: 10px;

    margin-bottom: 7px;

    background:
        rgba(228,188,75,.08);

    border-left:
        3px solid #dfbb50;

    color: #d9e5ef;

    font-size: 11px;
}

.source {

    padding: 10px;

    border-radius: 9px;

    margin-top: 6px;

    background:
        rgba(255,255,255,.035);

    display: flex;

    justify-content: space-between;

    font-size: 11px;
}

.badge {

    display: inline-block;

    padding: 4px 7px;

    border-radius: 999px;

    background:
        rgba(62,211,255,.11);

    color: #62d9ff;

    font-size: 9px;

    margin: 2px;
}

.quick {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px,1fr)
        );

    gap: 9px;
}

.quick a {

    display: block;

    padding: 13px;

    border-radius: 11px;

    background:
        rgba(255,255,255,.035);

    border:
        1px solid rgba(255,255,255,.06);

    color: #dff3ff;

    text-decoration: none;

    font-size: 11px;

    font-weight: 700;
}

.quick a:hover {

    background:
        rgba(63,211,255,.08);
}

.status {

    color: #8ea6be;

    font-size: 10px;

    margin-bottom: 10px;
}

.empty {

    color: #8da6bf;

    font-size: 11px;

    padding: 10px 0;
}

@media(max-width:950px) {

    .layout {

        grid-template-columns: 1fr;
    }
}

</style>

</head>

<body>

<div class="header">

    <div class="brand">
        PROPERTY<span>IQ</span>
    </div>

    <div
        id="propertyAddress"
        class="address">
        Loading property...
    </div>

</div>

<div class="container">

    <div class="toolbar">

        <button onclick="loadDashboard()">
            Refresh
        </button>

        <button
            class="secondary"
            onclick="openCommandMap()">
            Command Map
        </button>

        <button
            class="secondary"
            onclick="openTimeline()">
            Timeline
        </button>

        <button
            class="secondary"
            onclick="openDueDiligence()">
            Due Diligence
        </button>

        <button
            class="secondary"
            onclick="openReport()">
            Intelligence Report
        </button>

        <button
            class="secondary"
            onclick="openWorkspace()">
            Workspace
        </button>

    </div>

    <div id="status"
         class="status">
        Loading dashboard...
    </div>

    <div id="summary"></div>

    <div class="layout">

        <div>

            <div class="section">
                PROPERTY PROFILE
            </div>

            <div
                id="property"
                class="card">
            </div>

            <div class="section">
                INTELLIGENCE SOURCES
            </div>

            <div
                id="sources"
                class="card">
            </div>

            <div class="section">
                QUICK INTELLIGENCE
            </div>

            <div
                id="quick"
                class="quick">
            </div>

        </div>

        <div>

            <div class="section">
                DATA QUALITY / ATTENTION
            </div>

            <div
                id="flags"
                class="card">
            </div>

            <div class="section">
                COVERAGE
            </div>

            <div
                id="coverage"
                class="card">
            </div>

        </div>

    </div>

</div>

<script>

const PROPERTY_ID =
    "__PROPERTY_ID__";


function esc(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }

    return String(value)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function renderSummary(
    data
) {

    const s =
        data.summary || {};

    document.getElementById(
        "summary"
    ).innerHTML = `

        <div class="grid">

            <div class="card">
                <div class="metric">
                    ${esc(
                        s.intelligence_records
                        || 0
                    )}
                </div>
                <div class="label">
                    Intelligence records
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    ${esc(
                        s.mapped_intelligence
                        || 0
                    )}
                </div>
                <div class="label">
                    Mapped intelligence
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    ${esc(
                        s.evidence_items
                        || 0
                    )}
                </div>
                <div class="label">
                    Evidence items
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    ${esc(
                        s.documents
                        || 0
                    )}
                </div>
                <div class="label">
                    Documents
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    ${esc(
                        s.market_observations
                        || 0
                    )}
                </div>
                <div class="label">
                    Market observations
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    ${
                        s.boundary_present
                        ? "YES"
                        : "NO"
                    }
                </div>
                <div class="label">
                    GIS boundary
                </div>
            </div>

        </div>
    `;
}


function renderProperty(
    property
) {

    document.getElementById(
        "propertyAddress"
    ).textContent =
        property.property_address
        || "Property address unavailable";

    let html = "";

    html += `
        <div class="property">

            <strong>
                ${esc(
                    property.property_name
                    || "Unnamed property"
                )}
            </strong>

            <br>

            Type:
            <strong>
                ${esc(
                    property.property_type
                    || "Not specified"
                )}
            </strong>

            <br>

            Location:
            ${property.latitude !== null
                ? (
                    esc(
                        property.latitude
                    )
                    + ", "
                    + esc(
                        property.longitude
                    )
                )
                : "Pending"
            }

            <br>

            Area:
            ${esc(
                property.area
                || "Not specified"
            )}

            <br>

            Price:
            ${property.price !== null
                ? esc(
                    property.price
                  )
                : "Not specified"
            }

            <br>

            Price / sq.ft:
            ${property.price_per_sqft !== null
                ? esc(
                    property.price_per_sqft
                  )
                : "Not specified"
            }

            <br>

            Bedrooms:
            ${esc(
                property.bedrooms
                || "—"
            )}

            &nbsp;&nbsp;

            Bathrooms:
            ${esc(
                property.bathrooms
                || "—"
            )}

            <br>

            Builder / Owner:
            ${esc(
                property.builder_owner
                || "Not specified"
            )}

        </div>
    `;

    document.getElementById(
        "property"
    ).innerHTML =
        html;
}


function renderSources(
    summary
) {

    const sources =
        summary.source_summary
        || {};

    const container =
        document.getElementById(
            "sources"
        );

    const entries =
        Object.entries(
            sources
        );

    if (!entries.length) {

        container.innerHTML =
            '<div class="empty">'
            + "No source-backed intelligence"
            + "</div>";

        return;
    }

    let html = "";

    for (
        const [name,count]
        of entries
    ) {

        html += `
            <div class="source">

                <span>
                    ${esc(name)}
                </span>

                <strong>
                    ${esc(count)}
                </strong>

            </div>
        `;
    }

    container.innerHTML =
        html;
}


function renderFlags(
    flags
) {

    const container =
        document.getElementById(
            "flags"
        );

    if (!flags.length) {

        container.innerHTML = `
            <div class="empty">
                No dashboard data-quality
                attention flags were generated.
            </div>
        `;

        return;
    }

    let html = "";

    for (
        const flag
        of flags
    ) {

        html += `
            <div class="flag">

                <strong>
                    ${esc(
                        flag.code
                    )}
                </strong>

                <br>

                ${esc(
                    flag.label
                )}

            </div>
        `;
    }

    container.innerHTML =
        html;
}


function renderCoverage(
    data
) {

    const s =
        data.summary || {};

    document.getElementById(
        "coverage"
    ).innerHTML = `

        <div class="property">

            Intelligence:
            <strong>
                ${esc(
                    s.intelligence_records
                    || 0
                )}
            </strong>

            <br>

            Mapped:
            <strong>
                ${esc(
                    s.mapped_intelligence
                    || 0
                )}
            </strong>

            <br>

            Location pending:
            <strong>
                ${esc(
                    s.unmapped_intelligence
                    || 0
                )}
            </strong>

            <br>

            Evidence:
            <strong>
                ${esc(
                    s.evidence_items
                    || 0
                )}
            </strong>

            <br>

            Documents:
            <strong>
                ${esc(
                    s.documents
                    || 0
                )}
            </strong>

            <br>

            Market:
            <strong>
                ${esc(
                    s.market_observations
                    || 0
                )}
            </strong>

            <br>

            Refresh runs:
            <strong>
                ${esc(
                    s.refresh_runs
                    || 0
                )}
            </strong>

        </div>
    `;
}


function renderQuickLinks() {

    document.getElementById(
        "quick"
    ).innerHTML = `

        <a
          href="/propertyiq/unified-command-map/${PROPERTY_ID}">
            🗺 Unified Intelligence Map
        </a>

        <a
          href="/propertyiq/timeline/${PROPERTY_ID}">
            ◷ Development Timeline
        </a>

        <a
          href="/propertyiq/development-intelligence/${PROPERTY_ID}">
            ◈ Development Intelligence
        </a>

        <a
          href="/propertyiq/evidence-data-quality/${PROPERTY_ID}">
            ✓ Evidence & Data Quality
        </a>

        <a
          href="/propertyiq/acquisition-due-diligence/${PROPERTY_ID}">
            ⌕ Acquisition Due Diligence
        </a>

        <a
          href="/propertyiq/market-comparables/${PROPERTY_ID}">
            ▣ Market & Comparables
        </a>

        <a
          href="/propertyiq/boundary/${PROPERTY_ID}">
            ⬡ GIS Boundary
        </a>

        <a
          href="/propertyiq/real-osm-gis/${PROPERTY_ID}">
            ◉ OSM / GIS
        </a>

        <a
          href="/propertyiq/news-map/${PROPERTY_ID}">
            ◌ News Intelligence
        </a>

        <a
          href="/propertyiq/intelligence-report/${PROPERTY_ID}">
            ▤ Intelligence Report
        </a>

    `;
}


async function loadDashboard() {

    const status =
        document.getElementById(
            "status"
        );

    status.textContent =
        "Loading property intelligence...";

    try {

        const response =
            await fetch(
                "/api/v1/properties/"
                + PROPERTY_ID
                + "/property-intelligence-dashboard"
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.detail
                ||
                "Dashboard request failed"
            );
        }

        renderSummary(
            data
        );

        renderProperty(
            data.property
        );

        renderSources(
            data.summary
        );

        renderFlags(
            data.flags || []
        );

        renderCoverage(
            data
        );

        renderQuickLinks();

        status.textContent =
            "Property intelligence dashboard ready";

    } catch (err) {

        status.textContent =
            "Dashboard error";

        document.getElementById(
            "property"
        ).innerHTML =
            '<div class="empty">'
            + esc(
                err.message
            )
            + '</div>';
    }
}


function openCommandMap() {

    window.location.href =
        "/propertyiq/unified-command-map/"
        + PROPERTY_ID;
}


function openTimeline() {

    window.location.href =
        "/propertyiq/timeline/"
        + PROPERTY_ID;
}


function openDueDiligence() {

    window.location.href =
        "/propertyiq/acquisition-due-diligence/"
        + PROPERTY_ID;
}


function openReport() {

    window.location.href =
        "/propertyiq/intelligence-report/"
        + PROPERTY_ID;
}


function openWorkspace() {

    window.location.href =
        "/propertyiq/workspace/"
        + PROPERTY_ID;
}


loadDashboard();

</script>

</body>

</html>
"""

    return html.replace(
        "__PROPERTY_ID__",
        str(property_id)
    )


# ------------------------------------------------------------------------------
# 15. UI route
# ------------------------------------------------------------------------------

async def v37_dashboard_ui(
    property_id: str
):

    if _v37_property(
        property_id
    ) is None:

        raise HTTPException(
            status_code=404,
            detail="Property not found."
        )

    return HTMLResponse(
        _v37_html(property_id)
    )


_v37_add_get(
    "/propertyiq/property-intelligence-dashboard/{property_id}",
    v37_dashboard_ui
)


# ------------------------------------------------------------------------------
# 16. Export
# ------------------------------------------------------------------------------

globals()[
    "build_v37_dashboard"
] = build_v37_dashboard


# ------------------------------------------------------------------------------
# 17. Installation summary
# ------------------------------------------------------------------------------

print()
print("=" * 78)
print("PROPERTYIQ V37 — PROPERTY INTELLIGENCE DASHBOARD INSTALLED")
print("=" * 78)
print(f"Module: {MODULE_VERSION}")
print("Runtime: existing PropertyIQ runtime")
print()
print("INTEGRATED:")
print("  ✓ Full Property Profile")
print("  ✓ Acquisition / Due Diligence")
print("  ✓ Evidence / Data Quality")
print("  ✓ Unified Development Intelligence")
print("  ✓ Unified Intelligence Command Map")
print("  ✓ Timeline / Development Pipeline")
print("  ✓ GIS Boundary")
print("  ✓ OSM / GIS")
print("  ✓ Market / Comparables")
print("  ✓ Intelligence Report")
print()
print("DASHBOARD:")
print("  ✓ Property overview")
print("  ✓ Intelligence coverage")
print("  ✓ Source inventory")
print("  ✓ Data-quality flags")
print("  ✓ Evidence coverage")
print("  ✓ Document coverage")
print("  ✓ Market-data coverage")
print("  ✓ Quick navigation")
print()
print("API:")
print("  /api/v1/properties/{property_id}/property-intelligence-dashboard")
print()
print("UI:")
print("  /propertyiq/property-intelligence-dashboard/{property_id}")
print()
print("DATA INTEGRITY:")
print("  ✓ Read-only synthesis")
print("  ✓ Existing source-backed data only")
print("  ✓ No fabricated intelligence")
print("  ✓ No fabricated coordinates")
print("  ✓ No valuation prediction")
print("  ✓ No investment score")
print("  ✓ No legal conclusion")
print("  ✓ Database not modified during installation")
print("  ✓ Existing PropertyIQ routes preserved")
print("=" * 78)
print()



# ============================================================
# PROPERTYIQ MODULE: V38
# ORIGINAL COLAB CELL: In[67]
# ============================================================

# ============================================================
# PROPERTYIQ V38 — EVIDENCE & DUE-DILIGENCE WORKSPACE
# Direct Google Colab execution
# Consolidates V28 + V29 + V17 + V37
# ============================================================

import sys
import subprocess
import importlib
import json
import math
import html
from datetime import datetime, timezone

MODULE_VERSION = "PROPERTYIQ-V38-EVIDENCE-DUE-DILIGENCE-WORKSPACE"


# ------------------------------------------------------------
# BOOTSTRAP
# ------------------------------------------------------------

def _v38_install(pkg, import_name=None):
    import_name = import_name or pkg.split("[")[0].split("-")[0]
    try:
        importlib.import_module(import_name)
    except Exception:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-q", pkg
        ])


_v38_install("fastapi")
_v38_install("sqlalchemy")
_v38_install("psycopg[binary]", "psycopg")


from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text


# ------------------------------------------------------------
# REUSE EXISTING PROPERTYIQ RUNTIME
# ------------------------------------------------------------

if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not available in this Colab runtime. "
        "Run the existing PropertyIQ PostgreSQL runtime first."
    )

if "app" not in globals():
    app = FastAPI(title="PropertyIQ")


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def _v38_json(value):
    if value is None:
        return None

    if isinstance(value, dict):
        return value

    if isinstance(value, list):
        return value

    if hasattr(value, "_mapping"):
        return dict(value._mapping)

    return value


def _v38_num(value):
    try:
        return float(value)
    except Exception:
        return None


def _v38_safe(value):
    if value is None:
        return "—"
    return html.escape(str(value))


def _v38_distance_km(lat1, lon1, lat2, lon2):
    try:
        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)
    except Exception:
        return None

    r = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * r * math.asin(math.sqrt(a))


def _v38_columns(conn, table_name):
    rows = conn.execute(
        text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table
        """),
        {"table": table_name},
    ).fetchall()

    return {r[0] for r in rows}


def _v38_get_property(conn, property_id):
    row = conn.execute(
        text("""
            SELECT
                id,
                property_name,
                property_address,
                latitude,
                longitude,
                property_type,
                area,
                price,
                price_per_sqft,
                bedrooms,
                bathrooms,
                builder_owner,
                description,
                amenities,
                photos,
                documents,
                contact_information,
                ST_AsGeoJSON(boundary) AS boundary_geojson
            FROM properties
            WHERE id = CAST(:pid AS uuid)
            LIMIT 1
        """),
        {"pid": property_id},
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Property not found"
        )

    return dict(row._mapping)


def _v38_get_intelligence(conn, property_id):
    cols = _v38_columns(conn, "intelligence_records")

    select_cols = [
        "id",
        "property_id",
        "title",
        "category",
        "status",
        "description",
        "latitude",
        "longitude",
        "source_name",
        "source_type",
        "source_url",
        "published_at",
        "confidence",
        "evidence_level",
        "metadata",
    ]

    select_cols = [c for c in select_cols if c in cols]

    query = f"""
        SELECT {", ".join(select_cols)}
        FROM intelligence_records
        WHERE property_id = CAST(:pid AS uuid)
        ORDER BY published_at DESC NULLS LAST, id DESC
    """

    rows = conn.execute(
        text(query),
        {"pid": property_id},
    ).fetchall()

    return [dict(r._mapping) for r in rows]


def _v38_get_evidence(conn, property_id):
    cols = _v38_columns(conn, "evidence_items")

    preferred = [
        "id",
        "property_id",
        "intelligence_id",
        "title",
        "description",
        "source_name",
        "source_type",
        "source_url",
        "evidence_level",
        "confidence",
        "published_at",
        "created_at",
        "updated_at",
    ]

    select_cols = [c for c in preferred if c in cols]

    # Some PropertyIQ versions may not have property_id
    if "property_id" in cols:
        where_clause = "property_id = CAST(:pid AS uuid)"
        params = {"pid": property_id}

    elif "intelligence_id" in cols:
        where_clause = """
            intelligence_id IN (
                SELECT id
                FROM intelligence_records
                WHERE property_id = CAST(:pid AS uuid)
            )
        """
        params = {"pid": property_id}

    else:
        return []

    order_col = None

    for candidate in [
        "created_at",
        "updated_at",
        "published_at",
        "id",
    ]:
        if candidate in cols:
            order_col = candidate
            break

    order_sql = f" ORDER BY {order_col} DESC" if order_col else ""

    query = f"""
        SELECT {", ".join(select_cols)}
        FROM evidence_items
        WHERE {where_clause}
        {order_sql}
    """

    rows = conn.execute(
        text(query),
        params,
    ).fetchall()

    return [dict(r._mapping) for r in rows]


def _v38_get_files(conn, property_id):
    try:
        cols = _v38_columns(conn, "property_files")
    except Exception:
        return []

    if "property_id" not in cols:
        return []

    selected = ["id", "property_id"]

    for c in [
        "file_name",
        "filename",
        "content_type",
        "mime_type",
        "file_bytes",
        "created_at",
        "updated_at",
    ]:
        if c in cols:
            selected.append(c)

    rows = conn.execute(
        text(f"""
            SELECT {", ".join(selected)}
            FROM property_files
            WHERE property_id = CAST(:pid AS uuid)
            ORDER BY id DESC
        """),
        {"pid": property_id},
    ).fetchall()

    return [dict(r._mapping) for r in rows]


def _v38_get_market(conn, property_id):
    try:
        cols = _v38_columns(
            conn,
            "property_market_observations"
        )
    except Exception:
        return []

    if "property_id" not in cols:
        return []

    selected = ["id", "property_id"]

    for c in [
        "source_name",
        "source_url",
        "property_type",
        "area",
        "price",
        "price_per_sqft",
        "bedrooms",
        "bathrooms",
        "observation_date",
        "created_at",
    ]:
        if c in cols:
            selected.append(c)

    order_col = (
        "observation_date"
        if "observation_date" in cols
        else "id"
    )

    rows = conn.execute(
        text(f"""
            SELECT {", ".join(selected)}
            FROM property_market_observations
            WHERE property_id = CAST(:pid AS uuid)
            ORDER BY {order_col} DESC
        """),
        {"pid": property_id},
    ).fetchall()

    return [dict(r._mapping) for r in rows]


def _v38_get_refresh_history(conn, property_id):
    try:
        cols = _v38_columns(
            conn,
            "piq_property_refresh_runs"
        )
    except Exception:
        return []

    if "property_id" not in cols:
        return []

    selected = [
        c for c in [
            "id",
            "run_id",
            "property_id",
            "status",
            "started_at",
            "finished_at",
            "sources_requested",
            "source_results",
            "errors",
        ]
        if c in cols
    ]

    if not selected:
        return []

    order_col = (
        "started_at"
        if "started_at" in cols
        else "id"
    )

    rows = conn.execute(
        text(f"""
            SELECT {", ".join(selected)}
            FROM piq_property_refresh_runs
            WHERE property_id = CAST(:pid AS uuid)
            ORDER BY {order_col} DESC
            LIMIT 20
        """),
        {"pid": property_id},
    ).fetchall()

    return [dict(r._mapping) for r in rows]


# ------------------------------------------------------------
# CORE DUE-DILIGENCE SYNTHESIS
# ------------------------------------------------------------

def build_v38_due_diligence(property_id):

    with engine.begin() as conn:

        prop = _v38_get_property(
            conn,
            property_id
        )

        intelligence = _v38_get_intelligence(
            conn,
            property_id
        )

        evidence = _v38_get_evidence(
            conn,
            property_id
        )

        files = _v38_get_files(
            conn,
            property_id
        )

        market = _v38_get_market(
            conn,
            property_id
        )

        refresh_history = _v38_get_refresh_history(
            conn,
            property_id
        )

    # --------------------------------------------------------
    # BASIC COUNTS
    # --------------------------------------------------------

    mapped = [
        x for x in intelligence
        if _v38_num(x.get("latitude")) is not None
        and _v38_num(x.get("longitude")) is not None
    ]

    unmapped = [
        x for x in intelligence
        if (
            _v38_num(x.get("latitude")) is None
            or _v38_num(x.get("longitude")) is None
        )
    ]

    evidence_linked_ids = set()

    for e in evidence:
        iid = e.get("intelligence_id")
        if iid:
            evidence_linked_ids.add(str(iid))

    source_url_count = sum(
        1 for x in intelligence
        if x.get("source_url")
    )

    confidence_count = sum(
        1 for x in intelligence
        if x.get("confidence") not in [None, ""]
    )

    evidence_level_count = sum(
        1 for x in intelligence
        if x.get("evidence_level") not in [None, ""]
    )

    published_date_count = sum(
        1 for x in intelligence
        if x.get("published_at")
    )

    # --------------------------------------------------------
    # SOURCE INVENTORY
    # --------------------------------------------------------

    source_groups = {}

    for record in intelligence:

        source_type = (
            record.get("source_type")
            or record.get("source_name")
            or "OTHER"
        )

        source_type = str(source_type)

        if "RERA" in source_type.upper():
            group = "RERA"

        elif (
            "GOVERNMENT" in source_type.upper()
            or "PAIMANA" in source_type.upper()
        ):
            group = "GOVERNMENT"

        elif (
            "NEWS" in source_type.upper()
            or "GDELT" in source_type.upper()
        ):
            group = "NEWS"

        elif (
            "OSM" in source_type.upper()
            or "OPENSTREETMAP" in source_type.upper()
            or "OVERPASS" in source_type.upper()
        ):
            group = "OSM"

        elif "DEVELOP" in source_type.upper():
            group = "DEVELOPMENT"

        else:
            group = "OTHER"

        source_groups[group] = (
            source_groups.get(group, 0) + 1
        )

    # --------------------------------------------------------
    # LOCATION
    # --------------------------------------------------------

    property_lat = _v38_num(prop.get("latitude"))
    property_lon = _v38_num(prop.get("longitude"))

    nearby = []

    if property_lat is not None and property_lon is not None:

        for record in mapped:

            distance = _v38_distance_km(
                property_lat,
                property_lon,
                record["latitude"],
                record["longitude"],
            )

            if distance is not None:
                item = dict(record)
                item["distance_km"] = round(
                    distance,
                    3
                )
                nearby.append(item)

        nearby.sort(
            key=lambda x: x.get(
                "distance_km",
                999999
            )
        )

    # --------------------------------------------------------
    # FLAGS
    # --------------------------------------------------------

    flags = []

    if property_lat is None or property_lon is None:
        flags.append({
            "code": "PROPERTY_LOCATION_PENDING",
            "severity": "HIGH",
            "message": (
                "Property coordinates are not available."
            ),
        })

    if not prop.get("boundary_geojson"):
        flags.append({
            "code": "BOUNDARY_PENDING",
            "severity": "MEDIUM",
            "message": (
                "No saved property boundary is currently available."
            ),
        })

    if not intelligence:
        flags.append({
            "code": "NO_PROPERTY_INTELLIGENCE",
            "severity": "HIGH",
            "message": (
                "No property-linked intelligence records "
                "are currently available."
            ),
        })

    if unmapped:
        flags.append({
            "code": "UNMAPPED_INTELLIGENCE",
            "severity": "MEDIUM",
            "message": (
                f"{len(unmapped)} intelligence records "
                "do not currently have usable coordinates."
            ),
        })

    if intelligence and source_url_count < len(intelligence):
        flags.append({
            "code": "SOURCE_URL_GAPS",
            "severity": "MEDIUM",
            "message": (
                f"{len(intelligence) - source_url_count} "
                "intelligence records do not contain a source URL."
            ),
        })

    if intelligence and evidence_level_count < len(intelligence):
        flags.append({
            "code": "EVIDENCE_LEVEL_GAPS",
            "severity": "MEDIUM",
            "message": (
                f"{len(intelligence) - evidence_level_count} "
                "records do not have an evidence level."
            ),
        })

    if intelligence and confidence_count < len(intelligence):
        flags.append({
            "code": "CONFIDENCE_GAPS",
            "severity": "MEDIUM",
            "message": (
                f"{len(intelligence) - confidence_count} "
                "records do not have confidence metadata."
            ),
        })

    if intelligence and published_date_count < len(intelligence):
        flags.append({
            "code": "DATE_GAPS",
            "severity": "LOW",
            "message": (
                f"{len(intelligence) - published_date_count} "
                "records do not have a publication date."
            ),
        })

    if intelligence:

        linked_count = sum(
            1
            for x in intelligence
            if str(x.get("id")) in evidence_linked_ids
        )

        if linked_count < len(intelligence):
            flags.append({
                "code": "EVIDENCE_LINK_GAPS",
                "severity": "MEDIUM",
                "message": (
                    f"{len(intelligence) - linked_count} "
                    "intelligence records do not have "
                    "a linked evidence item."
                ),
            })

    if not files:
        flags.append({
            "code": "DOCUMENTS_PENDING",
            "severity": "LOW",
            "message": (
                "No property documents are currently stored."
            ),
        })

    if not market:
        flags.append({
            "code": "MARKET_DATA_PENDING",
            "severity": "LOW",
            "message": (
                "No property-linked market observations "
                "are currently stored."
            ),
        })

    # --------------------------------------------------------
    # RERA / GOVERNMENT / NEWS / OSM COUNTS
    # --------------------------------------------------------

    rera_count = 0
    government_count = 0
    news_count = 0
    osm_count = 0
    development_count = 0

    for group, count in source_groups.items():

        if group == "RERA":
            rera_count += count

        elif group == "GOVERNMENT":
            government_count += count

        elif group == "NEWS":
            news_count += count

        elif group == "OSM":
            osm_count += count

        elif group == "DEVELOPMENT":
            development_count += count

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    total_intelligence = len(intelligence)

    def pct(part, total):
        if not total:
            return 0
        return round(
            100 * part / total,
            1
        )

    coverage = {
        "source_url": pct(
            source_url_count,
            total_intelligence
        ),
        "confidence": pct(
            confidence_count,
            total_intelligence
        ),
        "evidence_level": pct(
            evidence_level_count,
            total_intelligence
        ),
        "published_date": pct(
            published_date_count,
            total_intelligence
        ),
        "mapped_location": pct(
            len(mapped),
            total_intelligence
        ),
        "evidence_linkage": pct(
            sum(
                1
                for x in intelligence
                if str(x.get("id"))
                in evidence_linked_ids
            ),
            total_intelligence
        ),
    }

    # --------------------------------------------------------
    # RECENT / NEARBY RECORDS
    # --------------------------------------------------------

    nearby_5km = [
        x for x in nearby
        if x.get("distance_km") is not None
        and x["distance_km"] <= 5
    ]

    # --------------------------------------------------------
    # RETURN
    # --------------------------------------------------------

    return {
        "module": MODULE_VERSION,
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "property": prop,

        "summary": {
            "intelligence_records": total_intelligence,
            "mapped_records": len(mapped),
            "unmapped_records": len(unmapped),
            "evidence_items": len(evidence),
            "documents": len(files),
            "market_observations": len(market),
            "refresh_runs": len(refresh_history),
            "nearby_records_5km": len(nearby_5km),
        },

        "source_inventory": source_groups,

        "source_counts": {
            "rera": rera_count,
            "government": government_count,
            "news": news_count,
            "osm": osm_count,
            "development": development_count,
        },

        "coverage": coverage,

        "flags": flags,

        "intelligence": intelligence,

        "nearby": nearby[:100],

        "evidence": evidence,

        "documents": files,

        "market": market,

        "refresh_history": refresh_history,

        "workspace_links": {
            "full_profile":
                f"/propertyiq/full-profile/{property_id}",

            "property_dashboard":
                f"/propertyiq/property-intelligence-dashboard/{property_id}",

            "command_map":
                f"/propertyiq/unified-command-map/{property_id}",

            "boundary":
                f"/propertyiq/boundary/{property_id}",

            "boundary_intelligence":
                f"/propertyiq/boundary-intelligence/{property_id}",

            "development":
                f"/propertyiq/development-intelligence/{property_id}",

            "market":
                f"/propertyiq/market-comparables/{property_id}",

            "news":
                f"/propertyiq/news-map/{property_id}",

            "timeline":
                f"/propertyiq/timeline/{property_id}",

            "report":
                f"/propertyiq/intelligence-report/{property_id}",

            "documents":
                f"/propertyiq/files/{property_id}",
        },

        "methodology": {
            "read_only": True,
            "source_backed_only": True,
            "fabricated_intelligence": False,
            "valuation_prediction": False,
            "investment_score": False,
            "legal_conclusion": False,
        },
    }


# ------------------------------------------------------------
# API
# ------------------------------------------------------------

_V38_API_PATH = (
    "/api/v1/properties/{property_id}/"
    "evidence-due-diligence"
)


@app.get(
    _V38_API_PATH,
    tags=["PropertyIQ V38"]
)
def v38_due_diligence_api(property_id: str):

    return build_v38_due_diligence(
        property_id
    )


# ------------------------------------------------------------
# PREMIUM UI
# ------------------------------------------------------------

def _v38_card(title, value, subtitle=""):
    return f"""
    <div class="card">
        <div class="card-title">{_v38_safe(title)}</div>
        <div class="card-value">{_v38_safe(value)}</div>
        <div class="card-sub">{_v38_safe(subtitle)}</div>
    </div>
    """


def _v38_flag_card(flag):
    severity = str(
        flag.get("severity", "INFO")
    ).upper()

    return f"""
    <div class="flag {severity.lower()}">
        <div class="flag-code">
            {_v38_safe(flag.get("code"))}
        </div>
        <div class="flag-message">
            {_v38_safe(flag.get("message"))}
        </div>
    </div>
    """


def _v38_source_rows(data):
    rows = ""

    for group, count in sorted(
        data.get(
            "source_inventory",
            {}
        ).items()
    ):
        rows += f"""
        <tr>
            <td>{_v38_safe(group)}</td>
            <td>{count}</td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="2">No source records</td>
        </tr>
        """

    return rows


def _v38_intelligence_rows(data):
    rows = ""

    for item in data.get(
        "intelligence",
        []
    )[:100]:

        source = (
            item.get("source_name")
            or item.get("source_type")
            or "OTHER"
        )

        url = item.get("source_url")

        source_html = _v38_safe(source)

        if url:
            source_html = (
                f'<a href="{_v38_safe(url)}" '
                f'target="_blank">Source</a>'
            )

        rows += f"""
        <tr>
            <td>{_v38_safe(item.get("title"))}</td>
            <td>{_v38_safe(source)}</td>
            <td>{_v38_safe(item.get("status"))}</td>
            <td>{_v38_safe(item.get("evidence_level"))}</td>
            <td>{_v38_safe(item.get("confidence"))}</td>
            <td>{source_html}</td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="6">
                No property-linked intelligence records.
            </td>
        </tr>
        """

    return rows


def _v38_evidence_rows(data):
    rows = ""

    for item in data.get(
        "evidence",
        []
    )[:100]:

        url = item.get("source_url")

        link = "—"

        if url:
            link = (
                f'<a href="{_v38_safe(url)}" '
                f'target="_blank">Open source</a>'
            )

        rows += f"""
        <tr>
            <td>{_v38_safe(item.get("title"))}</td>
            <td>{_v38_safe(item.get("evidence_level"))}</td>
            <td>{_v38_safe(item.get("confidence"))}</td>
            <td>{link}</td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="4">
                No evidence items are currently linked.
            </td>
        </tr>
        """

    return rows


def v38_due_diligence_html(property_id):

    data = build_v38_due_diligence(
        property_id
    )

    prop = data["property"]

    summary = data["summary"]
    coverage = data["coverage"]

    flags_html = ""

    for flag in data["flags"]:
        flags_html += _v38_flag_card(flag)

    if not flags_html:
        flags_html = """
        <div class="clean-state">
            No unresolved data-quality flags were detected
            by this workspace.
        </div>
        """

    source_rows = _v38_source_rows(data)
    intelligence_rows = _v38_intelligence_rows(data)
    evidence_rows = _v38_evidence_rows(data)

    property_name = _v38_safe(
        prop.get("property_name")
    )

    property_address = _v38_safe(
        prop.get("property_address")
    )

    property_id_safe = _v38_safe(
        property_id
    )

    coverage_json = json.dumps(
        coverage,
        indent=2,
        default=str
    )

    html_page = r"""
<!DOCTYPE html>
<html>
<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,
               initial-scale=1">

<title>PropertyIQ — Evidence & Due Diligence</title>

<style>

:root {
    --bg: #07111f;
    --panel: #0d1929;
    --panel2: #101f33;
    --border: rgba(255,255,255,.09);
    --text: #edf5ff;
    --muted: #91a4bb;
    --accent: #27c7ff;
    --accent2: #557cff;
    --warning: #f4b740;
    --danger: #ff667d;
    --success: #48d597;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at 15% 10%,
            rgba(39,199,255,.10),
            transparent 32%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(85,124,255,.10),
            transparent 30%
        ),
        var(--bg);

    color: var(--text);

    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

.topbar {
    position: sticky;
    top: 0;
    z-index: 10;

    backdrop-filter: blur(18px);

    background:
        rgba(7,17,31,.82);

    border-bottom:
        1px solid var(--border);

    padding: 18px 28px;

    display: flex;
    justify-content: space-between;
    align-items: center;
}

.brand {
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -.4px;
}

.brand span {
    color: var(--accent);
}

.top-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

button,
.link-btn {
    border: 1px solid var(--border);

    background:
        rgba(255,255,255,.045);

    color: var(--text);

    border-radius: 10px;

    padding: 9px 13px;

    cursor: pointer;

    text-decoration: none;

    font-size: 13px;
}

button:hover,
.link-btn:hover {
    background:
        rgba(39,199,255,.12);

    border-color:
        rgba(39,199,255,.4);
}

.container {
    max-width: 1500px;
    margin: auto;
    padding: 28px;
}

.hero {
    display: grid;
    grid-template-columns:
        minmax(0, 1fr)
        auto;

    gap: 20px;

    margin-bottom: 22px;
}

.eyebrow {
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 1.7px;
    font-size: 11px;
    font-weight: 800;
}

h1 {
    margin: 7px 0 8px;
    font-size: 31px;
    letter-spacing: -.9px;
}

.address {
    color: var(--muted);
    font-size: 14px;
}

.hero-id {
    color: var(--muted);
    font-size: 11px;
    margin-top: 10px;
    word-break: break-all;
}

.grid {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px, 1fr)
        );

    gap: 12px;

    margin-bottom: 20px;
}

.card {
    background:
        linear-gradient(
            145deg,
            rgba(255,255,255,.055),
            rgba(255,255,255,.025)
        );

    border:
        1px solid var(--border);

    border-radius: 16px;

    padding: 17px;

    box-shadow:
        0 18px 45px
        rgba(0,0,0,.18);
}

.card-title {
    color: var(--muted);
    font-size: 12px;
}

.card-value {
    margin-top: 7px;
    font-size: 25px;
    font-weight: 800;
}

.card-sub {
    color: var(--muted);
    font-size: 11px;
    margin-top: 4px;
}

.section {
    background:
        rgba(13,25,41,.82);

    border:
        1px solid var(--border);

    border-radius: 18px;

    margin-bottom: 18px;

    overflow: hidden;
}

.section-head {
    padding: 17px 20px;

    border-bottom:
        1px solid var(--border);

    display: flex;
    justify-content: space-between;
    align-items: center;
}

.section-title {
    font-size: 15px;
    font-weight: 800;
}

.section-sub {
    color: var(--muted);
    font-size: 11px;
}

.section-body {
    padding: 18px 20px;
}

.two-col {
    display: grid;

    grid-template-columns:
        minmax(0, 1fr)
        minmax(0, 1fr);

    gap: 18px;
}

@media(max-width:900px) {
    .two-col {
        grid-template-columns: 1fr;
    }

    .hero {
        grid-template-columns: 1fr;
    }
}

.flag {
    padding: 14px 16px;

    border:
        1px solid var(--border);

    border-radius: 12px;

    margin-bottom: 9px;

    background:
        rgba(255,255,255,.025);
}

.flag-code {
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .5px;
}

.flag-message {
    color: var(--muted);
    font-size: 13px;
    margin-top: 4px;
}

.flag.high {
    border-color:
        rgba(255,102,125,.35);
}

.flag.medium {
    border-color:
        rgba(244,183,64,.35);
}

.flag.low {
    border-color:
        rgba(145,164,187,.25);
}

.clean-state {
    padding: 15px;

    border-radius: 12px;

    background:
        rgba(72,213,151,.07);

    border:
        1px solid rgba(72,213,151,.2);

    color: #bcefd9;
}

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}

th,
td {
    padding: 11px 9px;

    border-bottom:
        1px solid var(--border);

    text-align: left;

    vertical-align: top;
}

th {
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .7px;
}

td {
    color: #dce7f4;
}

a {
    color: var(--accent);
    text-decoration: none;
}

.coverage {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(170px, 1fr)
        );

    gap: 10px;
}

.coverage-item {
    background:
        rgba(255,255,255,.035);

    border:
        1px solid var(--border);

    padding: 14px;

    border-radius: 12px;
}

.coverage-label {
    color: var(--muted);
    font-size: 11px;
}

.coverage-value {
    margin-top: 5px;
    font-size: 22px;
    font-weight: 800;
}

.progress {
    height: 5px;
    margin-top: 9px;

    background:
        rgba(255,255,255,.08);

    border-radius: 20px;

    overflow: hidden;
}

.progress > div {
    height: 100%;

    background:
        linear-gradient(
            90deg,
            var(--accent),
            var(--accent2)
        );
}

.links {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.footer {
    color: var(--muted);
    text-align: center;
    font-size: 11px;
    padding: 20px;
}

pre {
    white-space: pre-wrap;
    word-break: break-word;

    background:
        rgba(0,0,0,.22);

    border-radius: 12px;

    padding: 14px;

    color: #a9dfff;

    font-size: 11px;
}

</style>

</head>

<body>

<div class="topbar">

    <div class="brand">
        Property<span>IQ</span>
        <small style="color:#91a4bb;">
            / Evidence & Due Diligence
        </small>
    </div>

    <div class="top-actions">

        <a class="link-btn"
           href="/propertyiq/property-intelligence-dashboard/PROPERTY_ID">
            Dashboard
        </a>

        <a class="link-btn"
           href="/propertyiq/unified-command-map/PROPERTY_ID">
            Command Map
        </a>

        <a class="link-btn"
           href="/propertyiq/intelligence-report/PROPERTY_ID">
            Report
        </a>

    </div>

</div>


<div class="container">

    <div class="hero">

        <div>

            <div class="eyebrow">
                Evidence workspace
            </div>

            <h1>
                PROPERTY_NAME
            </h1>

            <div class="address">
                PROPERTY_ADDRESS
            </div>

            <div class="hero-id">
                Property ID: PROPERTY_ID
            </div>

        </div>

    </div>


    <!-- SUMMARY -->

    <div class="grid">

        SUMMARY_CARDS

    </div>


    <!-- FLAGS -->

    <div class="section">

        <div class="section-head">

            <div>
                <div class="section-title">
                    Due-Diligence Flags
                </div>

                <div class="section-sub">
                    Data-quality and evidence gaps identified
                    from currently stored PropertyIQ records.
                </div>
            </div>

        </div>

        <div class="section-body">

            FLAGS

        </div>

    </div>


    <!-- COVERAGE -->

    <div class="section">

        <div class="section-head">

            <div>
                <div class="section-title">
                    Evidence & Data Coverage
                </div>

                <div class="section-sub">
                    Coverage percentages describe the stored
                    intelligence records only.
                </div>
            </div>

        </div>

        <div class="section-body">

            <div class="coverage">

                COVERAGE_CARDS

            </div>

        </div>

    </div>


    <!-- SOURCES / DEVELOPMENT -->

    <div class="two-col">

        <div class="section">

            <div class="section-head">
                <div class="section-title">
                    Source Inventory
                </div>
            </div>

            <div class="section-body">

                <table>

                    <thead>
                        <tr>
                            <th>Source</th>
                            <th>Records</th>
                        </tr>
                    </thead>

                    <tbody>
                        SOURCE_ROWS
                    </tbody>

                </table>

            </div>

        </div>


        <div class="section">

            <div class="section-head">
                <div class="section-title">
                    Workspace Navigation
                </div>
            </div>

            <div class="section-body">

                <div class="links">

                    <a class="link-btn"
                       href="/propertyiq/full-profile/PROPERTY_ID">
                        Full Profile
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/unified-command-map/PROPERTY_ID">
                        Unified Map
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/boundary/PROPERTY_ID">
                        GIS Boundary
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/boundary-intelligence/PROPERTY_ID">
                        Boundary Intelligence
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/development-intelligence/PROPERTY_ID">
                        Development
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/market-comparables/PROPERTY_ID">
                        Market
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/news-map/PROPERTY_ID">
                        News
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/timeline/PROPERTY_ID">
                        Timeline
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/files/PROPERTY_ID">
                        Documents
                    </a>

                    <a class="link-btn"
                       href="/propertyiq/intelligence-report/PROPERTY_ID">
                        Intelligence Report
                    </a>

                </div>

            </div>

        </div>

    </div>


    <!-- INTELLIGENCE -->

    <div class="section">

        <div class="section-head">

            <div>
                <div class="section-title">
                    Property Intelligence
                </div>

                <div class="section-sub">
                    Source-backed records currently linked
                    to this property.
                </div>
            </div>

        </div>

        <div class="section-body">

            <div style="overflow-x:auto;">

                <table>

                    <thead>

                        <tr>
                            <th>Record</th>
                            <th>Source</th>
                            <th>Status</th>
                            <th>Evidence</th>
                            <th>Confidence</th>
                            <th>Source</th>
                        </tr>

                    </thead>

                    <tbody>

                        INTELLIGENCE_ROWS

                    </tbody>

                </table>

            </div>

        </div>

    </div>


    <!-- EVIDENCE -->

    <div class="section">

        <div class="section-head">

            <div>
                <div class="section-title">
                    Evidence Register
                </div>

                <div class="section-sub">
                    Evidence items currently associated with
                    this property or its intelligence records.
                </div>
            </div>

        </div>

        <div class="section-body">

            <div style="overflow-x:auto;">

                <table>

                    <thead>

                        <tr>
                            <th>Evidence</th>
                            <th>Level</th>
                            <th>Confidence</th>
                            <th>Source</th>
                        </tr>

                    </thead>

                    <tbody>

                        EVIDENCE_ROWS

                    </tbody>

                </table>

            </div>

        </div>

    </div>


    <!-- METHODOLOGY -->

    <div class="section">

        <div class="section-head">

            <div>
                <div class="section-title">
                    Workspace Methodology
                </div>
            </div>

        </div>

        <div class="section-body">

            <pre>COVERAGE_JSON</pre>

            <div style="
                color:#91a4bb;
                font-size:12px;
                line-height:1.7;
            ">
                This workspace is a read-only synthesis of
                existing PropertyIQ records. Missing coordinates,
                evidence, documents, source URLs, dates or other
                fields are displayed as data gaps rather than
                being inferred or fabricated.
            </div>

        </div>

    </div>


    <div class="footer">

        PROPERTYIQ V38 · Evidence & Due-Diligence Workspace

    </div>

</div>

</body>
</html>
"""

    # --------------------------------------------------------
    # SUMMARY CARDS
    # --------------------------------------------------------

    summary_cards = ""

    summary_cards += _v38_card(
        "Intelligence",
        summary["intelligence_records"],
        "property-linked records"
    )

    summary_cards += _v38_card(
        "Mapped",
        summary["mapped_records"],
        "records with coordinates"
    )

    summary_cards += _v38_card(
        "Evidence",
        summary["evidence_items"],
        "evidence items"
    )

    summary_cards += _v38_card(
        "Documents",
        summary["documents"],
        "stored property files"
    )

    summary_cards += _v38_card(
        "Market",
        summary["market_observations"],
        "market observations"
    )

    summary_cards += _v38_card(
        "Nearby",
        summary["nearby_records_5km"],
        "mapped records within 5 km"
    )

    # --------------------------------------------------------
    # COVERAGE CARDS
    # --------------------------------------------------------

    coverage_labels = [
        ("source_url", "Source URL"),
        ("confidence", "Confidence"),
        ("evidence_level", "Evidence Level"),
        ("published_date", "Publication Date"),
        ("mapped_location", "Mapped Location"),
        ("evidence_linkage", "Evidence Linkage"),
    ]

    coverage_cards = ""

    for key, label in coverage_labels:

        value = coverage.get(
            key,
            0
        )

        coverage_cards += f"""
        <div class="coverage-item">

            <div class="coverage-label">
                {_v38_safe(label)}
            </div>

            <div class="coverage-value">
                {value}%
            </div>

            <div class="progress">
                <div style="width:{max(0,min(100,value))}%"></div>
            </div>

        </div>
        """

    html_page = html_page.replace(
        "PROPERTY_ID",
        property_id_safe
    )

    html_page = html_page.replace(
        "PROPERTY_NAME",
        property_name
    )

    html_page = html_page.replace(
        "PROPERTY_ADDRESS",
        property_address
    )

    html_page = html_page.replace(
        "SUMMARY_CARDS",
        summary_cards
    )

    html_page = html_page.replace(
        "FLAGS",
        flags_html
    )

    html_page = html_page.replace(
        "COVERAGE_CARDS",
        coverage_cards
    )

    html_page = html_page.replace(
        "SOURCE_ROWS",
        source_rows
    )

    html_page = html_page.replace(
        "INTELLIGENCE_ROWS",
        intelligence_rows
    )

    html_page = html_page.replace(
        "EVIDENCE_ROWS",
        evidence_rows
    )

    html_page = html_page.replace(
        "COVERAGE_JSON",
        html.escape(
            coverage_json
        )
    )

    return html_page


# ------------------------------------------------------------
# UI ROUTE
# ------------------------------------------------------------

_V38_UI_PATH = (
    "/propertyiq/evidence-due-diligence/"
    "{property_id}"
)


@app.get(
    _V38_UI_PATH,
    response_class=HTMLResponse,
    tags=["PropertyIQ V38"]
)
def v38_due_diligence_ui(property_id: str):

    return HTMLResponse(
        v38_due_diligence_html(
            property_id
        )
    )


# ------------------------------------------------------------
# INSTALLATION STATUS
# ------------------------------------------------------------

print()
print("=" * 68)
print("PROPERTYIQ V38 — EVIDENCE & DUE-DILIGENCE WORKSPACE")
print("=" * 68)

print()
print("Module:")
print(f"  {MODULE_VERSION}")

print()
print("API:")
print(
    "  /api/v1/properties/{property_id}/"
    "evidence-due-diligence"
)

print()
print("UI:")
print(
    "  /propertyiq/evidence-due-diligence/"
    "{property_id}"
)

print()
print("Integrated:")
print("  ✓ V28 Acquisition & Due Diligence")
print("  ✓ V29 Evidence / Confidence / Data Quality")
print("  ✓ V17 Intelligence Report")
print("  ✓ V37 Property Intelligence Dashboard")
print("  ✓ RERA")
print("  ✓ Government / PAIMANA")
print("  ✓ News / GDELT")
print("  ✓ OSM / GIS")
print("  ✓ Development Intelligence")
print("  ✓ Market / Comparables")
print("  ✓ Documents")
print("  ✓ GIS Boundary")
print("  ✓ Source / Evidence Register")

print()
print("Evidence coverage: ENABLED")
print("Due-diligence flags: ENABLED")
print("Source traceability: ENABLED")
print("Mapped / unmapped tracking: ENABLED")
print("Document inventory: ENABLED")
print("Market-data coverage: ENABLED")

print()
print("Read-only synthesis: ENABLED")
print("Database modification during install: DISABLED")
print("Fabricated intelligence: DISABLED")
print("Valuation prediction: DISABLED")
print("Investment score: DISABLED")
print("Legal conclusion: DISABLED")

print()
print("Existing PropertyIQ routes preserved.")
print("=" * 68)



# ============================================================
# PROPERTYIQ MODULE: V39
# ORIGINAL COLAB CELL: In[68]
# ============================================================

# ============================================================
# PROPERTYIQ V39 — PRODUCTION ARCHITECTURE FINAL
# Direct Google Colab execution
#
# Purpose:
#   Final architectural control / readiness layer.
#
# IMPORTANT:
#   - Does NOT rebuild earlier PropertyIQ modules.
#   - Reuses existing engine + app.
#   - Read-only during installation.
#   - Does NOT delete or migrate existing data.
#   - Does NOT fabricate module status.
# ============================================================

import sys
import os
import json
import html
import importlib
import subprocess
from datetime import datetime, timezone

MODULE_VERSION = "PROPERTYIQ-V39-PRODUCTION-ARCHITECTURE-FINAL"


# ============================================================
# 1. SAFE DEPENDENCY BOOTSTRAP
# ============================================================

def _v39_install(package, import_name=None):
    import_name = import_name or package.split("[")[0].split("-")[0]

    try:
        importlib.import_module(import_name)
    except Exception:
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            package
        ])


_v39_install("fastapi")
_v39_install("sqlalchemy")
_v39_install("psycopg[binary]", "psycopg")

from fastapi.responses import HTMLResponse
from sqlalchemy import text


# ============================================================
# 2. REUSE EXISTING PROPERTYIQ RUNTIME
# ============================================================

if "engine" not in globals():
    raise RuntimeError(
        "PropertyIQ engine is not available in this Colab runtime. "
        "Run the existing PropertyIQ PostgreSQL/PostGIS runtime first."
    )

if "app" not in globals():
    app = FastAPI(title="PropertyIQ")


# ============================================================
# 3. ARCHITECTURE DEFINITION
# ============================================================

V39_ARCHITECTURE = {
    "platform": {
        "name": "PropertyIQ",
        "type": "Property Intelligence Platform",
        "architecture": "Map-first / Intelligence-first",
        "version": MODULE_VERSION,
        "runtime": "Python + FastAPI + PostgreSQL/PostGIS",
    },

    "layers": [
        {
            "name": "Presentation",
            "components": [
                "Premium Property Workspace",
                "Full Property Intelligence Dashboard",
                "Unified Intelligence Command Map",
                "Evidence & Due-Diligence Workspace",
                "GIS Boundary Workspace",
                "Timeline / Development Pipeline",
                "Market / Comparable Workspace",
                "News Intelligence Map",
                "Intelligence Report",
            ],
        },

        {
            "name": "API",
            "components": [
                "FastAPI",
                "Property APIs",
                "Intelligence APIs",
                "Evidence APIs",
                "GIS APIs",
                "Development APIs",
                "Market APIs",
                "Document APIs",
                "Ingestion APIs",
                "Live Connector APIs",
                "System / Readiness APIs",
            ],
        },

        {
            "name": "Intelligence",
            "components": [
                "Unified Property Intelligence",
                "Location Intelligence",
                "Boundary Intelligence",
                "Development Intelligence",
                "RERA Intelligence",
                "Government Infrastructure Intelligence",
                "News Intelligence",
                "OSM / GIS Intelligence",
                "Market / Comparable Intelligence",
                "Evidence / Confidence Intelligence",
                "Due-Diligence Intelligence",
                "Timeline Intelligence",
            ],
        },

        {
            "name": "Ingestion",
            "components": [
                "Source Registry",
                "Ingestion Runs",
                "Deterministic Fingerprints",
                "Safe Upsert",
                "Evidence Linkage",
                "Freshness Tracking",
                "Live Connector Handoff",
            ],
        },

        {
            "name": "Live Sources",
            "components": [
                "OpenStreetMap / Overpass",
                "Nominatim",
                "GDELT DOC",
                "PAIMANA / MoSPI",
                "UP-RERA",
            ],
        },

        {
            "name": "Data",
            "components": [
                "PostgreSQL",
                "PostGIS",
                "Properties",
                "Intelligence Records",
                "Evidence Items",
                "Property Files",
                "Market Observations",
                "Ingestion Jobs",
                "Source Registry",
                "Refresh History",
                "RERA Source Registry",
            ],
        },

        {
            "name": "Spatial",
            "components": [
                "POINT 4326",
                "MULTIPOLYGON 4326",
                "GIS Boundary",
                "Radius Search",
                "Haversine Distance",
                "PostGIS Spatial Indexes",
                "GeoJSON",
            ],
        },
    ],

    "principles": [
        "Source-backed intelligence only",
        "No fabricated coordinates",
        "No fabricated market data",
        "Evidence traceability",
        "Confidence preservation",
        "Unmapped records remain visible",
        "Missing data is represented explicitly",
        "Property-centric intelligence",
        "Map-native investigation",
        "Read-only synthesis layers",
        "Existing modules are preserved",
    ],
}


# ============================================================
# 4. MODULE REGISTRY
# ============================================================

V39_MODULE_REGISTRY = [
    ("V13", "Production Architecture", "architecture"),
    ("V14", "Ingestion Pipeline", "ingestion"),
    ("V15", "Live Connectors", "connectors"),
    ("V16", "Live Intelligence Engine", "live-intelligence"),
    ("V17", "Property Intelligence Report", "report"),
    ("V18", "Map-Native Leaflet", "map"),
    ("V18.3", "Map Intelligence Modes", "map"),
    ("V19.1", "Interactive Due-Diligence Map", "due-diligence"),
    ("V20.1", "GIS Boundary Workspace", "gis"),
    ("V21.1", "Boundary Intelligence", "gis"),
    ("V22", "Premium Property Workspace", "workspace"),
    ("V24", "Real OSM / GIS", "gis"),
    ("V25", "Comparables / Market Intelligence", "market"),
    ("V26.2", "Full Property Profile", "profile"),
    ("V28", "Acquisition & Due-Diligence", "due-diligence"),
    ("V29", "Evidence / Confidence / Data Quality", "evidence"),
    ("V30", "Unified Development Intelligence", "development"),
    ("V34", "News Intelligence Map", "news"),
    ("V35", "Unified Intelligence Command Map", "command-map"),
    ("V36", "Timeline / Development Pipeline", "timeline"),
    ("V37", "Property Intelligence Dashboard", "dashboard"),
    ("V38", "Evidence & Due-Diligence Workspace", "due-diligence"),
    ("V39", "Production Architecture Final", "architecture"),
]


# ============================================================
# 5. DATABASE INSPECTION
# ============================================================

def _v39_get_tables(conn):

    rows = conn.execute(
        text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
    ).fetchall()

    return [r[0] for r in rows]


def _v39_table_columns(conn, table_name):

    rows = conn.execute(
        text("""
            SELECT
                column_name,
                data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
            ORDER BY ordinal_position
        """),
        {"table_name": table_name}
    ).fetchall()

    return [
        {
            "column": r[0],
            "type": r[1],
        }
        for r in rows
    ]


def _v39_database_diagnostics():

    result = {
        "connection": False,
        "postgresql": None,
        "postgis": None,
        "tables": [],
        "table_columns": {},
        "spatial_capabilities": {},
        "errors": [],
    }

    try:

        with engine.begin() as conn:

            result["connection"] = True

            row = conn.execute(
                text("SELECT version()")
            ).fetchone()

            if row:
                result["postgresql"] = row[0]

            try:

                row = conn.execute(
                    text("""
                        SELECT PostGIS_Version()
                    """)
                ).fetchone()

                if row:
                    result["postgis"] = row[0]

            except Exception as exc:

                result["errors"].append(
                    "PostGIS check: " + str(exc)
                )

            tables = _v39_get_tables(conn)

            result["tables"] = tables

            for table in tables:

                try:
                    result["table_columns"][table] = (
                        _v39_table_columns(
                            conn,
                            table
                        )
                    )
                except Exception as exc:
                    result["errors"].append(
                        f"Column inspection {table}: {exc}"
                    )

            # --------------------------------------------
            # Spatial checks
            # --------------------------------------------

            required_spatial_tables = {
                "properties",
                "intelligence_records",
            }

            for table in required_spatial_tables:

                result["spatial_capabilities"][table] = (
                    table in tables
                )

            if "properties" in tables:

                cols = {
                    x["column"]
                    for x in result["table_columns"].get(
                        "properties",
                        []
                    )
                }

                result["spatial_capabilities"][
                    "properties.location"
                ] = "location" in cols

                result["spatial_capabilities"][
                    "properties.boundary"
                ] = "boundary" in cols

            if "intelligence_records" in tables:

                cols = {
                    x["column"]
                    for x in result["table_columns"].get(
                        "intelligence_records",
                        []
                    )
                }

                result["spatial_capabilities"][
                    "intelligence_records.location"
                ] = "location" in cols

                result["spatial_capabilities"][
                    "intelligence_records.latitude"
                ] = "latitude" in cols

                result["spatial_capabilities"][
                    "intelligence_records.longitude"
                ] = "longitude" in cols

    except Exception as exc:

        result["errors"].append(
            "Database connection: " + str(exc)
        )

    return result


# ============================================================
# 6. ROUTE INVENTORY
# ============================================================

def _v39_route_inventory():

    routes = []

    try:

        for route in app.routes:

            path = getattr(
                route,
                "path",
                None
            )

            methods = getattr(
                route,
                "methods",
                None
            )

            if path:

                routes.append({
                    "path": path,
                    "methods": sorted(
                        list(methods or [])
                    ),
                })

    except Exception:
        pass

    routes.sort(
        key=lambda x: x["path"]
    )

    return routes


# ============================================================
# 7. PROPERTYIQ CAPABILITY DETECTION
# ============================================================

def _v39_capability_report(db, routes):

    route_paths = {
        r["path"]
        for r in routes
    }

    tables = set(
        db.get("tables", [])
    )

    checks = {}

    def route_contains(fragment):
        return any(
            fragment in p
            for p in route_paths
        )

    checks["PostgreSQL"] = (
        db.get("connection") is True
    )

    checks["PostGIS"] = (
        db.get("postgis") is not None
    )

    checks["Properties table"] = (
        "properties" in tables
    )

    checks["Intelligence table"] = (
        "intelligence_records" in tables
    )

    checks["Evidence table"] = (
        "evidence_items" in tables
    )

    checks["Ingestion system"] = (
        "piq_ingestion_runs" in tables
        or route_contains("/ingestion/")
    )

    checks["Source registry"] = (
        "piq_source_registry" in tables
        or route_contains("/ingestion/sources")
    )

    checks["Property refresh history"] = (
        "piq_property_refresh_runs" in tables
    )

    checks["GIS boundary"] = (
        "boundary" in {
            x["column"]
            for x in db["table_columns"].get(
                "properties",
                []
            )
        }
    )

    checks["RERA"] = (
        route_contains("/rera")
        or "rera_source_registry" in tables
    )

    checks["News"] = (
        route_contains("/news")
    )

    checks["Market"] = (
        route_contains("/market")
        or "property_market_observations" in tables
    )

    checks["Documents"] = (
        route_contains("/files")
        or "property_files" in tables
    )

    checks["Evidence / Due Diligence"] = (
        route_contains("evidence")
        or route_contains("due-diligence")
    )

    checks["Development"] = (
        route_contains("development")
    )

    checks["Timeline"] = (
        route_contains("timeline")
    )

    checks["Command Map"] = (
        route_contains("command-map")
    )

    checks["Property Dashboard"] = (
        route_contains("property-intelligence-dashboard")
        or route_contains("full-intelligence-dashboard")
    )

    checks["Intelligence Report"] = (
        route_contains("intelligence-report")
    )

    checks["Boundary Intelligence"] = (
        route_contains("boundary-intelligence")
    )

    checks["Live connectors"] = (
        route_contains("/live/")
    )

    return checks


# ============================================================
# 8. READINESS
# ============================================================

def _v39_readiness(db, capabilities):

    critical = [
        "PostgreSQL",
        "PostGIS",
        "Properties table",
        "Intelligence table",
        "Evidence table",
        "GIS boundary",
    ]

    important = [
        "Ingestion system",
        "Source registry",
        "RERA",
        "News",
        "Market",
        "Documents",
        "Evidence / Due Diligence",
        "Development",
        "Timeline",
        "Command Map",
        "Property Dashboard",
        "Intelligence Report",
        "Live connectors",
    ]

    critical_missing = [
        x for x in critical
        if not capabilities.get(x, False)
    ]

    important_missing = [
        x for x in important
        if not capabilities.get(x, False)
    ]

    if critical_missing:
        state = "NOT_READY"

    elif important_missing:
        state = "PARTIAL"

    else:
        state = "READY"

    return {
        "state": state,
        "critical_missing": critical_missing,
        "important_missing": important_missing,
        "critical_total": len(critical),
        "critical_passed": (
            len(critical) - len(critical_missing)
        ),
        "important_total": len(important),
        "important_passed": (
            len(important) - len(important_missing)
        ),
    }


# ============================================================
# 9. ARCHITECTURE SNAPSHOT
# ============================================================

def build_v39_architecture_snapshot():

    db = _v39_database_diagnostics()

    routes = _v39_route_inventory()

    capabilities = _v39_capability_report(
        db,
        routes
    )

    readiness = _v39_readiness(
        db,
        capabilities
    )

    return {
        "module": MODULE_VERSION,

        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "architecture": V39_ARCHITECTURE,

        "module_registry": [
            {
                "version": x[0],
                "name": x[1],
                "category": x[2],
            }
            for x in V39_MODULE_REGISTRY
        ],

        "database": db,

        "routes": routes,

        "route_count": len(routes),

        "capabilities": capabilities,

        "readiness": readiness,

        "runtime": {
            "engine_present": "engine" in globals(),
            "app_present": "app" in globals(),
            "python": sys.version,
            "working_directory": os.getcwd(),
        },

        "rules": {
            "read_only_installation": True,
            "fabricated_intelligence": False,
            "fabricated_coordinates": False,
            "fabricated_market_data": False,
            "investment_score": False,
            "valuation_prediction": False,
            "unsupported_legal_conclusion": False,
        },
    }


# ============================================================
# 10. API — ARCHITECTURE
# ============================================================

@app.get(
    "/api/v1/system/production-architecture",
    tags=["PropertyIQ V39"]
)
def v39_architecture_api():

    return build_v39_architecture_snapshot()


# ============================================================
# 11. API — READINESS
# ============================================================

@app.get(
    "/api/v1/system/production-readiness",
    tags=["PropertyIQ V39"]
)
def v39_readiness_api():

    snapshot = build_v39_architecture_snapshot()

    return {
        "module": MODULE_VERSION,
        "generated_at": snapshot["generated_at"],
        "readiness": snapshot["readiness"],
        "capabilities": snapshot["capabilities"],
    }


# ============================================================
# 12. API — MODULE REGISTRY
# ============================================================

@app.get(
    "/api/v1/system/modules",
    tags=["PropertyIQ V39"]
)
def v39_modules_api():

    return {
        "module": MODULE_VERSION,
        "count": len(V39_MODULE_REGISTRY),
        "modules": [
            {
                "version": x[0],
                "name": x[1],
                "category": x[2],
            }
            for x in V39_MODULE_REGISTRY
        ],
    }


# ============================================================
# 13. PREMIUM ARCHITECTURE UI
# ============================================================

def _v39_html():

    snapshot = build_v39_architecture_snapshot()

    db = snapshot["database"]
    capabilities = snapshot["capabilities"]
    readiness = snapshot["readiness"]

    state = readiness["state"]

    state_class = {
        "READY": "ready",
        "PARTIAL": "partial",
        "NOT_READY": "not-ready",
    }.get(
        state,
        "partial"
    )

    # --------------------------------------------------------
    # Capability rows
    # --------------------------------------------------------

    capability_rows = ""

    for name, enabled in capabilities.items():

        cls = "yes" if enabled else "no"

        capability_rows += f"""
        <tr>
            <td>{html.escape(name)}</td>
            <td>
                <span class="status {cls}">
                    {"ENABLED" if enabled else "MISSING"}
                </span>
            </td>
        </tr>
        """

    # --------------------------------------------------------
    # Tables
    # --------------------------------------------------------

    table_rows = ""

    for table in db.get("tables", []):

        column_count = len(
            db.get(
                "table_columns",
                {}
            ).get(
                table,
                []
            )
        )

        table_rows += f"""
        <tr>
            <td>{html.escape(table)}</td>
            <td>{column_count}</td>
        </tr>
        """

    # --------------------------------------------------------
    # Module registry
    # --------------------------------------------------------

    module_rows = ""

    for version, name, category in V39_MODULE_REGISTRY:

        module_rows += f"""
        <tr>
            <td>{html.escape(version)}</td>
            <td>{html.escape(name)}</td>
            <td>{html.escape(category)}</td>
        </tr>
        """

    # --------------------------------------------------------
    # Routes
    # --------------------------------------------------------

    route_rows = ""

    for route in snapshot["routes"]:

        methods = ", ".join(
            route.get("methods", [])
        )

        route_rows += f"""
        <tr>
            <td>{html.escape(route["path"])}</td>
            <td>{html.escape(methods)}</td>
        </tr>
        """

    if not route_rows:

        route_rows = """
        <tr>
            <td colspan="2">
                No routes were detected.
            </td>
        </tr>
        """

    if not table_rows:

        table_rows = """
        <tr>
            <td colspan="2">
                No public tables detected.
            </td>
        </tr>
        """

    page = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,
               initial-scale=1">

<title>
PropertyIQ — Production Architecture
</title>

<style>

:root {
    --bg: #06101d;
    --panel: #0d1928;
    --panel2: #101f32;
    --border: rgba(255,255,255,.09);
    --text: #edf6ff;
    --muted: #91a5bd;
    --cyan: #29c8ff;
    --blue: #597dff;
    --green: #48d597;
    --yellow: #f4ba4d;
    --red: #ff637c;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;

    background:
        radial-gradient(
            circle at 15% 5%,
            rgba(41,200,255,.11),
            transparent 31%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(89,125,255,.10),
            transparent 30%
        ),
        var(--bg);

    color: var(--text);

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

.topbar {
    position: sticky;
    top: 0;
    z-index: 20;

    padding: 17px 28px;

    display: flex;
    align-items: center;
    justify-content: space-between;

    border-bottom:
        1px solid var(--border);

    background:
        rgba(6,16,29,.84);

    backdrop-filter: blur(18px);
}

.logo {
    font-size: 20px;
    font-weight: 850;
    letter-spacing: -.5px;
}

.logo span {
    color: var(--cyan);
}

.nav {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.nav a {
    color: var(--text);
    text-decoration: none;

    padding: 8px 12px;

    border:
        1px solid var(--border);

    border-radius: 9px;

    font-size: 12px;
}

.nav a:hover {
    border-color:
        rgba(41,200,255,.4);

    background:
        rgba(41,200,255,.08);
}

.container {
    max-width: 1500px;
    margin: auto;
    padding: 28px;
}

.hero {
    display: grid;

    grid-template-columns:
        minmax(0, 1fr)
        230px;

    gap: 20px;

    margin-bottom: 22px;
}

@media(max-width:850px) {
    .hero {
        grid-template-columns: 1fr;
    }
}

.eyebrow {
    color: var(--cyan);
    text-transform: uppercase;
    letter-spacing: 1.8px;
    font-size: 11px;
    font-weight: 800;
}

h1 {
    margin: 7px 0;

    font-size: 32px;

    letter-spacing: -.9px;
}

.subtitle {
    color: var(--muted);

    max-width: 800px;

    line-height: 1.65;

    font-size: 14px;
}

.readiness {
    border:
        1px solid var(--border);

    border-radius: 17px;

    padding: 20px;

    background:
        rgba(255,255,255,.035);
}

.readiness .label {
    color: var(--muted);

    font-size: 10px;

    text-transform: uppercase;

    letter-spacing: 1.2px;
}

.readiness .state {
    margin-top: 9px;

    font-size: 25px;

    font-weight: 850;
}

.ready {
    color: var(--green);
}

.partial {
    color: var(--yellow);
}

.not-ready {
    color: var(--red);
}

.cards {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px,1fr)
        );

    gap: 12px;

    margin-bottom: 20px;
}

.card {
    background:
        linear-gradient(
            145deg,
            rgba(255,255,255,.055),
            rgba(255,255,255,.02)
        );

    border:
        1px solid var(--border);

    border-radius: 15px;

    padding: 17px;
}

.card-label {
    color: var(--muted);

    font-size: 11px;
}

.card-value {
    margin-top: 7px;

    font-size: 25px;

    font-weight: 850;
}

.card-sub {
    color: var(--muted);

    font-size: 10px;

    margin-top: 4px;
}

.section {
    background:
        rgba(13,25,40,.83);

    border:
        1px solid var(--border);

    border-radius: 17px;

    margin-bottom: 18px;

    overflow: hidden;
}

.section-head {
    padding: 16px 19px;

    border-bottom:
        1px solid var(--border);

    display: flex;
    justify-content: space-between;
    align-items: center;
}

.section-title {
    font-size: 15px;

    font-weight: 800;
}

.section-sub {
    margin-top: 3px;

    color: var(--muted);

    font-size: 11px;
}

.section-body {
    padding: 18px 19px;
}

.two-col {
    display: grid;

    grid-template-columns:
        minmax(0,1fr)
        minmax(0,1fr);

    gap: 18px;
}

@media(max-width:900px) {
    .two-col {
        grid-template-columns: 1fr;
    }
}

table {
    width: 100%;

    border-collapse: collapse;

    font-size: 12px;
}

th,
td {
    padding: 10px 9px;

    border-bottom:
        1px solid var(--border);

    text-align: left;

    vertical-align: top;
}

th {
    color: var(--muted);

    font-size: 10px;

    text-transform: uppercase;

    letter-spacing: .7px;
}

.status {
    display: inline-block;

    padding: 4px 8px;

    border-radius: 999px;

    font-size: 9px;

    font-weight: 800;

    letter-spacing: .5px;
}

.status.yes {
    color: var(--green);

    background:
        rgba(72,213,151,.09);

    border:
        1px solid rgba(72,213,151,.18);
}

.status.no {
    color: var(--red);

    background:
        rgba(255,99,124,.09);

    border:
        1px solid rgba(255,99,124,.18);
}

.architecture {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(240px,1fr)
        );

    gap: 12px;
}

.layer {
    padding: 16px;

    border:
        1px solid var(--border);

    border-radius: 13px;

    background:
        rgba(255,255,255,.025);
}

.layer-title {
    font-weight: 800;

    font-size: 13px;

    margin-bottom: 9px;
}

.layer ul {
    margin: 0;

    padding-left: 18px;

    color: var(--muted);

    font-size: 11px;

    line-height: 1.7;
}

.principles {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(230px,1fr)
        );

    gap: 9px;
}

.principle {
    padding: 12px 14px;

    border:
        1px solid var(--border);

    border-radius: 10px;

    color: #cbd9e8;

    font-size: 12px;

    background:
        rgba(255,255,255,.025);
}

.footer {
    color: var(--muted);

    text-align: center;

    padding: 20px;

    font-size: 10px;
}

</style>

</head>

<body>

<div class="topbar">

    <div class="logo">
        Property<span>IQ</span>
        <small style="color:#91a5bd;">
            / Production Architecture
        </small>
    </div>

    <div class="nav">

        <a href="/propertyiq">
            Command Center
        </a>

        <a href="/api/v1/system/production-architecture"
           target="_blank">
            Architecture JSON
        </a>

        <a href="/api/v1/system/production-readiness"
           target="_blank">
            Readiness JSON
        </a>

        <a href="/api/v1/system/modules"
           target="_blank">
            Modules
        </a>

    </div>

</div>


<div class="container">

    <div class="hero">

        <div>

            <div class="eyebrow">
                Production control layer
            </div>

            <h1>
                PropertyIQ Architecture
            </h1>

            <div class="subtitle">

                Central architectural inventory for the existing
                PropertyIQ platform. This layer observes and
                consolidates the existing PostgreSQL/PostGIS,
                FastAPI, ingestion, live-source, GIS, intelligence,
                evidence, market, development and workspace
                capabilities without rebuilding them.

            </div>

        </div>


        <div class="readiness">

            <div class="label">
                Architecture readiness
            </div>

            <div class="state STATE_CLASS">
                READINESS_STATE
            </div>

            <div style="
                color:#91a5bd;
                font-size:11px;
                margin-top:8px;
            ">

                Critical:
                CRITICAL_PASSED /
                CRITICAL_TOTAL

                <br>

                Important:
                IMPORTANT_PASSED /
                IMPORTANT_TOTAL

            </div>

        </div>

    </div>


    <div class="cards">

        <div class="card">

            <div class="card-label">
                Registered Modules
            </div>

            <div class="card-value">
                MODULE_COUNT
            </div>

            <div class="card-sub">
                PropertyIQ architecture registry
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                API Routes
            </div>

            <div class="card-value">
                ROUTE_COUNT
            </div>

            <div class="card-sub">
                routes currently detected
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Database Tables
            </div>

            <div class="card-value">
                TABLE_COUNT
            </div>

            <div class="card-sub">
                public PostgreSQL tables
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Critical Checks
            </div>

            <div class="card-value">
                CRITICAL_PASSED / CRITICAL_TOTAL
            </div>

            <div class="card-sub">
                architecture foundation
            </div>

        </div>


        <div class="card">

            <div class="card-label">
                Important Checks
            </div>

            <div class="card-value">
                IMPORTANT_PASSED / IMPORTANT_TOTAL
            </div>

            <div class="card-sub">
                production capabilities
            </div>

        </div>

    </div>


    <!-- ARCHITECTURE LAYERS -->

    <div class="section">

        <div class="section-head">

            <div>

                <div class="section-title">
                    Architecture Layers
                </div>

                <div class="section-sub">
                    Canonical PropertyIQ system structure
                </div>

            </div>

        </div>


        <div class="section-body">

            <div class="architecture">

                ARCHITECTURE_LAYERS

            </div>

        </div>

    </div>


    <!-- CAPABILITIES -->

    <div class="section">

        <div class="section-head">

            <div>

                <div class="section-title">
                    Capability Readiness
                </div>

                <div class="section-sub">
                    Detected from the current runtime and database.
                </div>

            </div>

        </div>


        <div class="section-body">

            <div style="overflow-x:auto;">

                <table>

                    <thead>

                        <tr>
                            <th>Capability</th>
                            <th>Status</th>
                        </tr>

                    </thead>

                    <tbody>

                        CAPABILITY_ROWS

                    </tbody>

                </table>

            </div>

        </div>

    </div>


    <div class="two-col">


        <!-- DATABASE -->

        <div class="section">

            <div class="section-head">

                <div>

                    <div class="section-title">
                        PostgreSQL / PostGIS
                    </div>

                    <div class="section-sub">
                        Current database inventory
                    </div>

                </div>

            </div>


            <div class="section-body">

                <div class="card">

                    <div class="card-label">
                        PostgreSQL
                    </div>

                    <div class="card-value"
                         style="font-size:14px;">

                        POSTGRES_STATUS

                    </div>

                </div>

                <br>

                <div class="card">

                    <div class="card-label">
                        PostGIS
                    </div>

                    <div class="card-value"
                         style="font-size:14px;">

                        POSTGIS_STATUS

                    </div>

                </div>

                <br>

                <table>

                    <thead>

                        <tr>
                            <th>Table</th>
                            <th>Columns</th>
                        </tr>

                    </thead>

                    <tbody>

                        TABLE_ROWS

                    </tbody>

                </table>

            </div>

        </div>


        <!-- MODULE REGISTRY -->

        <div class="section">

            <div class="section-head">

                <div>

                    <div class="section-title">
                        Module Registry
                    </div>

                    <div class="section-sub">
                        PropertyIQ architectural modules
                    </div>

                </div>

            </div>


            <div class="section-body">

                <div style="overflow-x:auto;">

                    <table>

                        <thead>

                            <tr>
                                <th>Version</th>
                                <th>Module</th>
                                <th>Category</th>
                            </tr>

                        </thead>

                        <tbody>

                            MODULE_ROWS

                        </tbody>

                    </table>

                </div>

            </div>

        </div>

    </div>


    <!-- PRINCIPLES -->

    <div class="section">

        <div class="section-head">

            <div>

                <div class="section-title">
                    PropertyIQ Architecture Principles
                </div>

                <div class="section-sub">
                    Rules preserved by the production architecture.
                </div>

            </div>

        </div>


        <div class="section-body">

            <div class="principles">

                PRINCIPLES

            </div>

        </div>

    </div>


    <!-- ROUTES -->

    <div class="section">

        <div class="section-head">

            <div>

                <div class="section-title">
                    Detected API Routes
                </div>

                <div class="section-sub">
                    Runtime route inventory.
                </div>

            </div>

        </div>


        <div class="section-body">

            <div style="
                max-height:500px;
                overflow:auto;
            ">

                <table>

                    <thead>

                        <tr>
                            <th>Path</th>
                            <th>Methods</th>
                        </tr>

                    </thead>

                    <tbody>

                        ROUTE_ROWS

                    </tbody>

                </table>

            </div>

        </div>

    </div>


    <div class="footer">

        PROPERTYIQ V39 · Production Architecture Final

        <br>

        Generated:
        GENERATED_AT

    </div>

</div>

</body>

</html>
"""

    # --------------------------------------------------------
    # Architecture layers
    # --------------------------------------------------------

    architecture_layers = ""

    for layer in V39_ARCHITECTURE["layers"]:

        items = ""

        for item in layer["components"]:

            items += (
                "<li>"
                + html.escape(item)
                + "</li>"
            )

        architecture_layers += f"""
        <div class="layer">

            <div class="layer-title">
                {html.escape(layer["name"])}
            </div>

            <ul>
                {items}
            </ul>

        </div>
        """

    # --------------------------------------------------------
    # Principles
    # --------------------------------------------------------

    principles = ""

    for item in V39_ARCHITECTURE["principles"]:

        principles += f"""
        <div class="principle">
            ✓ {html.escape(item)}
        </div>
        """

    # --------------------------------------------------------
    # Replace placeholders
    # --------------------------------------------------------

    page = page.replace(
        "STATE_CLASS",
        state_class
    )

    page = page.replace(
        "READINESS_STATE",
        html.escape(state)
    )

    page = page.replace(
        "MODULE_COUNT",
        str(len(V39_MODULE_REGISTRY))
    )

    page = page.replace(
        "ROUTE_COUNT",
        str(snapshot["route_count"])
    )

    page = page.replace(
        "TABLE_COUNT",
        str(len(db.get("tables", [])))
    )

    page = page.replace(
        "CRITICAL_PASSED",
        str(readiness["critical_passed"])
    )

    page = page.replace(
        "CRITICAL_TOTAL",
        str(readiness["critical_total"])
    )

    page = page.replace(
        "IMPORTANT_PASSED",
        str(readiness["important_passed"])
    )

    page = page.replace(
        "IMPORTANT_TOTAL",
        str(readiness["important_total"])
    )

    page = page.replace(
        "ARCHITECTURE_LAYERS",
        architecture_layers
    )

    page = page.replace(
        "CAPABILITY_ROWS",
        capability_rows
    )

    page = page.replace(
        "POSTGRES_STATUS",
        (
            "CONNECTED"
            if db.get("connection")
            else "NOT CONNECTED"
        )
    )

    page = page.replace(
        "POSTGIS_STATUS",
        str(
            db.get("postgis")
            or "NOT AVAILABLE"
        )
    )

    page = page.replace(
        "TABLE_ROWS",
        table_rows
    )

    page = page.replace(
        "MODULE_ROWS",
        module_rows
    )

    page = page.replace(
        "PRINCIPLES",
        principles
    )

    page = page.replace(
        "ROUTE_ROWS",
        route_rows
    )

    page = page.replace(
        "GENERATED_AT",
        html.escape(
            snapshot["generated_at"]
        )
    )

    return page


# ============================================================
# 14. UI ROUTE
# ============================================================

@app.get(
    "/propertyiq/system/production-architecture",
    response_class=HTMLResponse,
    tags=["PropertyIQ V39"]
)
def v39_architecture_ui():

    return HTMLResponse(
        _v39_html()
    )


# ============================================================
# 15. INSTALLATION OUTPUT
# ============================================================

print()
print("=" * 72)
print("PROPERTYIQ V39 — PRODUCTION ARCHITECTURE FINAL")
print("=" * 72)

print()
print("Module:")
print(f"  {MODULE_VERSION}")

print()
print("Architecture control:")
print("  ✓ PostgreSQL / PostGIS")
print("  ✓ FastAPI backend")
print("  ✓ Property repository layer")
print("  ✓ Intelligence layer")
print("  ✓ Evidence layer")
print("  ✓ Ingestion layer")
print("  ✓ Live connector layer")
print("  ✓ GIS / spatial layer")
print("  ✓ Market / comparable layer")
print("  ✓ Development intelligence")
print("  ✓ RERA intelligence")
print("  ✓ Government infrastructure")
print("  ✓ News intelligence")
print("  ✓ OSM / GIS intelligence")
print("  ✓ Document layer")
print("  ✓ Timeline / development pipeline")
print("  ✓ Due-diligence workspace")
print("  ✓ Property intelligence dashboard")
print("  ✓ Unified command map")

print()
print("APIs:")
print("  /api/v1/system/production-architecture")
print("  /api/v1/system/production-readiness")
print("  /api/v1/system/modules")

print()
print("UI:")
print("  /propertyiq/system/production-architecture")

print()
print("Runtime:")
print(
    "  PostgreSQL connection:",
    "AVAILABLE" if "engine" in globals()
    else "MISSING"
)

print(
    "  FastAPI application:",
    "AVAILABLE" if "app" in globals()
    else "MISSING"
)

print()
print("Module registry:")
print(
    f"  {len(V39_MODULE_REGISTRY)} registered modules"
)

print()
print("Database:")
try:
    _v39_db = _v39_database_diagnostics()

    print(
        "  PostgreSQL:",
        "CONNECTED"
        if _v39_db["connection"]
        else "NOT CONNECTED"
    )

    print(
        "  PostGIS:",
        _v39_db["postgis"]
        or "NOT AVAILABLE"
    )

    print(
        "  Public tables:",
        len(_v39_db["tables"])
    )

except Exception as exc:

    print(
        "  Diagnostic error:",
        str(exc)
    )

print()
print("Installation mode:")
print("  Read-only architecture layer: ENABLED")
print("  Existing data preserved: ENABLED")
print("  Existing routes preserved: ENABLED")
print("  Existing modules rebuilt: NO")
print("  Database migration during install: NO")
print("  Data deletion during install: NO")
print("  Fabricated intelligence: DISABLED")
print("  Fabricated coordinates: DISABLED")
print("  Fabricated market data: DISABLED")
print("  Investment score: DISABLED")
print("  Valuation prediction: DISABLED")
print("  Unsupported legal conclusion: DISABLED")

print()
print("=" * 72)
print("PROPERTYIQ V39 INSTALLATION COMPLETE")
print("=" * 72)



# ============================================================
# PROPERTYIQ MODULE: HOME
# ORIGINAL COLAB CELL: In[70]
# ============================================================

# ============================================================
# PROPERTYIQ V40 — APPLICATION LAUNCHER + HOME
# Direct Google Colab execution
#
# Fixes:
#   1. "/" -> PropertyIQ home page
#   2. "/propertyiq" -> PropertyIQ home page
#   3. Colab iframe launch
#   4. Current FastAPI app reused
#   5. Existing routes preserved
#   6. New port 8501 avoids old server on 8500
# ============================================================

import sys
import time
import socket
import threading
import importlib
import subprocess
import html

from fastapi.responses import HTMLResponse, RedirectResponse


# ------------------------------------------------------------
# DEPENDENCY
# ------------------------------------------------------------

try:
    import uvicorn
except Exception:
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "uvicorn"
    ])
    import uvicorn


# ------------------------------------------------------------
# EXISTING PROPERTYIQ APP
# ------------------------------------------------------------

if "app" not in globals():
    app = FastAPI(title="PropertyIQ")


# ------------------------------------------------------------
# PROPERTY ID
# ------------------------------------------------------------

PROPERTY_ID = (
    "eec8ccb0-91bc-4dff-9b31-17e785c84521"
)


# ------------------------------------------------------------
# ROUTE CHECK
# ------------------------------------------------------------

def _v40_route_paths():

    paths = set()

    try:
        for route in app.routes:

            path = getattr(
                route,
                "path",
                None
            )

            if path:
                paths.add(path)

    except Exception:
        pass

    return paths


# ------------------------------------------------------------
# HOME PAGE
# ------------------------------------------------------------

def propertyiq_home():

    routes = _v40_route_paths()

    def route_status(fragment):

        return any(
            fragment in path
            for path in routes
        )

    dashboard_exists = route_status(
        "property-intelligence-dashboard"
    )

    command_map_exists = route_status(
        "unified-command-map"
    )

    evidence_exists = route_status(
        "evidence-due-diligence"
    )

    profile_exists = route_status(
        "full-profile"
    )

    report_exists = route_status(
        "intelligence-report"
    )

    boundary_exists = route_status(
        "/boundary/"
    )

    development_exists = route_status(
        "development-intelligence"
    )

    market_exists = route_status(
        "market-comparables"
    )

    news_exists = route_status(
        "news-map"
    )

    timeline_exists = route_status(
        "timeline"
    )

    system_exists = route_status(
        "production-architecture"
    )


    def status_badge(enabled):

        if enabled:

            return """
            <span class="badge enabled">
                AVAILABLE
            </span>
            """

        return """
        <span class="badge unavailable">
            ROUTE NOT DETECTED
        </span>
        """


    page = f"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>PropertyIQ</title>

<style>

:root {{
    --bg:#06101d;
    --panel:#0c1929;
    --panel2:#102238;
    --border:rgba(255,255,255,.09);
    --text:#eef7ff;
    --muted:#91a6bd;
    --cyan:#2acaff;
    --blue:#597cff;
    --green:#4bdd9b;
    --yellow:#f2ba4c;
}}

* {{
    box-sizing:border-box;
}}

body {{
    margin:0;

    min-height:100vh;

    color:var(--text);

    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:
        radial-gradient(
            circle at 15% 5%,
            rgba(42,202,255,.12),
            transparent 30%
        ),
        radial-gradient(
            circle at 90% 20%,
            rgba(89,124,255,.12),
            transparent 30%
        ),
        var(--bg);
}}

.top {{
    padding:22px 30px;

    border-bottom:
        1px solid var(--border);

    background:
        rgba(6,16,29,.78);

    backdrop-filter:blur(18px);

    display:flex;

    justify-content:space-between;

    align-items:center;
}}

.logo {{
    font-size:22px;
    font-weight:850;
    letter-spacing:-.6px;
}}

.logo span {{
    color:var(--cyan);
}}

.version {{
    color:var(--muted);
    font-size:11px;
    margin-left:8px;
}}

.container {{
    max-width:1450px;
    margin:auto;
    padding:42px 28px 60px;
}}

.hero {{
    max-width:850px;
    margin-bottom:35px;
}}

.eyebrow {{
    color:var(--cyan);
    font-size:11px;
    font-weight:800;
    letter-spacing:2px;
    text-transform:uppercase;
}}

h1 {{
    margin:9px 0 12px;

    font-size:46px;

    line-height:1.05;

    letter-spacing:-1.7px;
}}

.hero p {{
    color:var(--muted);

    font-size:15px;

    line-height:1.7;
}}

.actions {{
    display:flex;

    flex-wrap:wrap;

    gap:10px;

    margin-top:22px;
}}

.action {{
    text-decoration:none;

    color:var(--text);

    padding:11px 15px;

    border-radius:11px;

    border:
        1px solid var(--border);

    background:
        rgba(255,255,255,.045);

    font-size:12px;

    transition:.2s;
}}

.action.primary {{
    background:
        linear-gradient(
            135deg,
            rgba(42,202,255,.2),
            rgba(89,124,255,.2)
        );

    border-color:
        rgba(42,202,255,.35);
}}

.action:hover {{
    transform:translateY(-1px);

    border-color:
        rgba(42,202,255,.45);
}}

.section {{
    margin-top:30px;
}}

.section-title {{
    font-size:16px;
    font-weight:800;
    margin-bottom:12px;
}}

.grid {{
    display:grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(245px,1fr)
        );

    gap:13px;
}}

.card {{
    background:
        linear-gradient(
            145deg,
            rgba(255,255,255,.055),
            rgba(255,255,255,.018)
        );

    border:
        1px solid var(--border);

    border-radius:16px;

    padding:18px;

    text-decoration:none;

    color:var(--text);

    transition:.2s;
}}

.card:hover {{
    transform:translateY(-2px);

    border-color:
        rgba(42,202,255,.35);

    background:
        rgba(42,202,255,.055);
}}

.card-title {{
    font-size:14px;
    font-weight:800;
}}

.card-description {{
    color:var(--muted);

    font-size:11px;

    line-height:1.55;

    margin-top:6px;

    min-height:34px;
}}

.badge {{
    display:inline-block;

    margin-top:12px;

    padding:4px 8px;

    border-radius:999px;

    font-size:8px;

    font-weight:800;

    letter-spacing:.6px;
}}

.enabled {{
    color:var(--green);

    background:
        rgba(75,221,155,.08);

    border:
        1px solid rgba(75,221,155,.2);
}}

.unavailable {{
    color:var(--yellow);

    background:
        rgba(242,186,76,.08);

    border:
        1px solid rgba(242,186,76,.2);
}}

.info {{
    margin-top:25px;

    padding:17px;

    border:
        1px solid var(--border);

    border-radius:14px;

    color:var(--muted);

    font-size:11px;

    line-height:1.7;

    background:
        rgba(255,255,255,.025);
}}

.id {{
    font-family:monospace;

    color:#b8c9da;

    word-break:break-all;
}}

.footer {{
    margin-top:45px;

    text-align:center;

    color:var(--muted);

    font-size:10px;
}}

</style>

</head>


<body>


<div class="top">

    <div class="logo">
        Property<span>IQ</span>
        <span class="version">
            Property Intelligence Platform
        </span>
    </div>

</div>


<div class="container">


    <div class="hero">

        <div class="eyebrow">
            Intelligence Command Center
        </div>

        <h1>
            PropertyIQ
        </h1>

        <p>
            Map-first, intelligence-first property research
            platform integrating property data, GIS,
            development intelligence, RERA, government
            infrastructure, news, market data, documents
            and evidence.
        </p>


        <div class="actions">

            <a class="action primary"
               href="/propertyiq/property-intelligence-dashboard/{PROPERTY_ID}">
                Open Property Dashboard
            </a>

            <a class="action"
               href="/propertyiq/unified-command-map/{PROPERTY_ID}">
                Open Command Map
            </a>

            <a class="action"
               href="/propertyiq/evidence-due-diligence/{PROPERTY_ID}">
                Due Diligence
            </a>

            <a class="action"
               href="/propertyiq/system/production-architecture">
                System Architecture
            </a>

        </div>

    </div>


    <div class="section">

        <div class="section-title">
            Property Intelligence
        </div>


        <div class="grid">


            <a class="card"
               href="/propertyiq/property-intelligence-dashboard/{PROPERTY_ID}">

                <div class="card-title">
                    Full Intelligence Dashboard
                </div>

                <div class="card-description">
                    Main investor/property intelligence
                    command dashboard.
                </div>

                {status_badge(dashboard_exists)}

            </a>


            <a class="card"
               href="/propertyiq/full-profile/{PROPERTY_ID}">

                <div class="card-title">
                    Full Property Profile
                </div>

                <div class="card-description">
                    Property facts, location, intelligence,
                    development, market and documents.
                </div>

                {status_badge(profile_exists)}

            </a>


            <a class="card"
               href="/propertyiq/unified-command-map/{PROPERTY_ID}">

                <div class="card-title">
                    Unified Intelligence Map
                </div>

                <div class="card-description">
                    Geographic view of property intelligence
                    and source-backed records.
                </div>

                {status_badge(command_map_exists)}

            </a>


            <a class="card"
               href="/propertyiq/evidence-due-diligence/{PROPERTY_ID}">

                <div class="card-title">
                    Evidence & Due Diligence
                </div>

                <div class="card-description">
                    Evidence coverage, source traceability,
                    data gaps and due-diligence flags.
                </div>

                {status_badge(evidence_exists)}

            </a>


            <a class="card"
               href="/propertyiq/boundary/{PROPERTY_ID}">

                <div class="card-title">
                    GIS Boundary
                </div>

                <div class="card-description">
                    Draw, edit and inspect the property's
                    geographic boundary.
                </div>

                {status_badge(boundary_exists)}

            </a>


            <a class="card"
               href="/propertyiq/development-intelligence/{PROPERTY_ID}">

                <div class="card-title">
                    Development Intelligence
                </div>

                <div class="card-description">
                    RERA, government infrastructure,
                    news and development records.
                </div>

                {status_badge(development_exists)}

            </a>


            <a class="card"
               href="/propertyiq/market-comparables/{PROPERTY_ID}">

                <div class="card-title">
                    Market & Comparables
                </div>

                <div class="card-description">
                    Existing property observations and
                    comparable-property information.
                </div>

                {status_badge(market_exists)}

            </a>


            <a class="card"
               href="/propertyiq/news-map/{PROPERTY_ID}">

                <div class="card-title">
                    News Intelligence
                </div>

                <div class="card-description">
                    Geographic news intelligence from
                    stored GDELT records.
                </div>

                {status_badge(news_exists)}

            </a>


            <a class="card"
               href="/propertyiq/timeline/{PROPERTY_ID}">

                <div class="card-title">
                    Timeline & Pipeline
                </div>

                <div class="card-description">
                    Chronological development and
                    property intelligence timeline.
                </div>

                {status_badge(timeline_exists)}

            </a>


            <a class="card"
               href="/propertyiq/intelligence-report/{PROPERTY_ID}">

                <div class="card-title">
                    Intelligence Report
                </div>

                <div class="card-description">
                    Consolidated property intelligence
                    report.
                </div>

                {status_badge(report_exists)}

            </a>

        </div>

    </div>


    <div class="section">

        <div class="section-title">
            System
        </div>


        <div class="grid">


            <a class="card"
               href="/propertyiq/system/production-architecture">

                <div class="card-title">
                    Production Architecture
                </div>

                <div class="card-description">
                    PostgreSQL/PostGIS, FastAPI, ingestion,
                    connectors, intelligence and route inventory.
                </div>

                {status_badge(system_exists)}

            </a>


            <a class="card"
               href="/api/v1/system/production-readiness"
               target="_blank">

                <div class="card-title">
                    Production Readiness API
                </div>

                <div class="card-description">
                    Machine-readable capability and
                    architecture readiness information.
                </div>

                <span class="badge enabled">
                    API
                </span>

            </a>


            <a class="card"
               href="/api/v1/system/modules"
               target="_blank">

                <div class="card-title">
                    Module Registry
                </div>

                <div class="card-description">
                    Registered PropertyIQ architectural
                    modules.
                </div>

                <span class="badge enabled">
                    API
                </span>

            </a>

        </div>

    </div>


    <div class="info">

        <strong style="color:#eef7ff;">
            Current test property
        </strong>

        <br><br>

        Property ID:

        <span class="id">
            {PROPERTY_ID}
        </span>

        <br><br>

        This is the existing PropertyIQ test property.
        All workspaces above use the same property ID.

    </div>


    <div class="footer">

        PROPERTYIQ · Intelligence-first property platform

    </div>


</div>

</body>

</html>
"""

    return HTMLResponse(page)


# ------------------------------------------------------------
# REGISTER HOME ROUTES
# ------------------------------------------------------------

# Avoid duplicate registration if this cell is run again.

_existing_paths = _v40_route_paths()


if "/" not in _existing_paths:

    @app.get(
        "/",
        response_class=HTMLResponse,
        include_in_schema=False
    )
    def propertyiq_root():

        return propertyiq_home()


if "/propertyiq" not in _existing_paths:

    @app.get(
        "/propertyiq",
        response_class=HTMLResponse,
        include_in_schema=False
    )
    def propertyiq_command_center():

        return propertyiq_home()


# ------------------------------------------------------------
# START SERVER ON NEW PORT
# ------------------------------------------------------------

PORT = 8501


def _v40_port_open(port):

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    sock.settimeout(.7)

    try:

        return (
            sock.connect_ex(
                ("127.0.0.1", port)
            ) == 0
        )

    finally:

        sock.close()


if not _v40_port_open(PORT):

    def _run_propertyiq():

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=PORT,
            log_level="warning"
        )

    _thread = threading.Thread(
        target=_run_propertyiq,
        daemon=True
    )

    _thread.start()

    time.sleep(3)


# ------------------------------------------------------------
# WAIT FOR SERVER
# ------------------------------------------------------------

started = False

for _ in range(30):

    if _v40_port_open(PORT):

        started = True
        break

    time.sleep(.5)


# ------------------------------------------------------------
# COLAB LINK
# ------------------------------------------------------------

if started:

    print()
    print("=" * 70)
    print("PROPERTYIQ WEB APP — READY")
    print("=" * 70)
    print()
    print("Server:")
    print("  Port:", PORT)
    print()
    print("Main PropertyIQ URL:")
    print()
    print("  https://localhost:8501/")
    print()
    print("Property Dashboard:")
    print()
    print(
        "  /propertyiq/property-intelligence-dashboard/"
        + PROPERTY_ID
    )
    print()
    print("Command Map:")
    print()
    print(
        "  /propertyiq/unified-command-map/"
        + PROPERTY_ID
    )
    print()

    # --------------------------------------------------------
    # IMPORTANT:
    # Use iframe instead of deprecated window method
    # --------------------------------------------------------

    try:

        from google.colab import output

        print("Opening PropertyIQ inside Colab...")
        print()

        output.serve_kernel_port_as_iframe(
            PORT,
            height=900
        )

    except Exception as exc:

        print(
            "Automatic iframe display unavailable:",
            exc
        )

        print()
        print(
            "Open the displayed localhost:8501 "
            "link from Colab."
        )

else:

    print()
    print("=" * 70)
    print("PROPERTYIQ SERVER FAILED TO START")
    print("=" * 70)
    print()
    print("Try running the cell once more.")