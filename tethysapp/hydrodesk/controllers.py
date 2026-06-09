"""HydroDesk map view — a single generic MapLayout that renders any spatial
HydroType from the generic store. Here: monitoring_station, with click-to-plot.
This is the data-driven 'one code-time class, request-time content' pattern.
"""
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib

from sqlalchemy import select, func, delete, cast, Float, Text, or_
from sqlalchemy.orm import Session
from geoalchemy2 import WKTElement

from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.text import slugify
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from tethys_sdk.routing import controller
from tethys_sdk.layouts import MapLayout

from .app import App
from . import model as m
from . import registry

logger = logging.getLogger(__name__)

# Upper bound on a decompressed shapefile upload (sum of the .shp/.shx/.dbf members),
# a guard against zip-decompression bombs exhausting memory.
_SHP_MAX_BYTES = 200 * 1024 * 1024

# The installed JSON-Schema validator in the Tethys env is `fastjsonschema`
# (2.21.2). Prefer it; fall back to the classic `jsonschema` package only if
# fastjsonschema is unavailable, so the import never hard-fails at server start.
try:
    import fastjsonschema  # noqa: F401

    def _validate_attributes(field_schema, attributes):
        """Validate ``attributes`` against ``field_schema``.

        Returns ``(validated_dict, None)`` on success (the validated dict may
        carry schema ``default`` values injected by the validator) or
        ``(None, message)`` on the first validation error.
        """
        try:
            validated = fastjsonschema.compile(field_schema or {})(attributes)
            return validated, None
        except fastjsonschema.JsonSchemaException as exc:
            return None, exc.message
except Exception:  # pragma: no cover - exercised only if fastjsonschema absent
    import jsonschema
    from jsonschema import Draft7Validator

    def _validate_attributes(field_schema, attributes):
        validator = Draft7Validator(field_schema or {})
        errors = sorted(validator.iter_errors(attributes), key=lambda e: e.path)
        if errors:
            err = errors[0]
            loc = ".".join(str(p) for p in err.path)
            msg = f"{loc}: {err.message}" if loc else err.message
            return None, msg
        return attributes, None


@controller(name="home", url="", title="Home")
def desk_home(request):
    """Frappe-style Desk home (the app index): every DocType (HydroType) as a
    card + the '+ New HydroType' builder. Replaces the old ReactPy map landing."""
    engine = App.get_persistent_store_database("hydro_db")
    types = []
    with Session(engine) as session:
        rows = session.execute(
            select(m.HydroType.slug, m.HydroType.display_name,
                   m.HydroType.geometry_kind, m.HydroType.field_schema)
            .order_by(m.HydroType.display_name)
        ).all()
        for slug, display_name, gkind, fs in rows:
            if not _user_can(request, fs, "read"):
                continue  # hide types the user has no read permission for
            count = session.execute(
                select(func.count()).select_from(m.HydroRecord)
                .where(m.HydroRecord.hydrotype_slug == slug)
            ).scalar()
            types.append({"slug": slug, "display_name": display_name,
                          "geometry_kind": gkind, "count": count or 0})
    return render(request, "hydrodesk/home.html",
                  {"types": types, "total": len(types),
                   "can_build": _can_build(request)})


def _records_geojson(slug):
    """Load every record of a HydroType from the generic store as a GeoJSON
    FeatureCollection (lon/lat EPSG:4326 — MapLayout reprojects for display)."""
    engine = App.get_persistent_store_database("hydro_db")
    features = []
    with Session(engine) as session:
        rows = session.execute(
            select(
                m.HydroRecord.id,
                m.HydroRecord.attributes,
                func.ST_AsGeoJSON(m.HydroRecord.geom),
            )
            .where(m.HydroRecord.hydrotype_slug == slug)
            .where(m.HydroRecord.geom.isnot(None))
        ).all()
    for rid, attrs, geom in rows:
        props = dict(attrs or {})
        props["id"] = str(rid)
        features.append(
            {"type": "Feature", "geometry": json.loads(geom), "properties": props}
        )
    return {"type": "FeatureCollection", "features": features}


# --- Vetted, cached NWIS fetch. Honors the design's cached-fetch principle:
#     in-process TTL cache + request timeout + graceful fallback; never raw polling. ---
_NWIS_CACHE = {}
_NWIS_TTL = 900  # seconds (15 min) — well above the 60s rate floor


def fetch_nwis_discharge(site, period="P90D"):
    """Return (dates, values_cfs, site_name) of USGS daily mean discharge
    (parameter 00060) for a site, cached. Empty + None on failure/no-data."""
    now = time.time()
    cached = _NWIS_CACHE.get(site)
    if cached and (now - cached[0]) < _NWIS_TTL:
        return cached[1]
    url = (
        "https://waterservices.usgs.gov/nwis/dv/?format=json"
        f"&sites={site}&parameterCd=00060&period={period}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.load(resp)
        series = payload.get("value", {}).get("timeSeries", [])
        if not series:
            result = ([], [], None)
        else:
            s0 = series[0]
            name = s0.get("sourceInfo", {}).get("siteName")
            pairs = [
                (v["dateTime"][:10], float(v["value"]))
                for v in s0["values"][0]["value"]
                if v.get("value") not in (None, "", "-999999")
            ]
            result = ([d for d, _ in pairs], [v for _, v in pairs], name)
    except Exception:
        result = ([], [], None)
    _NWIS_CACHE[site] = (now, result)
    return result


# ===========================================================================
# DYNAMIC API EXTRACTOR — the generic, data-driven generalization of the single
# hardcoded NWIS fetch above. A HydroConnector row declares a URL template (with
# {field} placeholders), method, headers/query, an auth block that NAMES a
# HydroCredential, a result_kind, and dot/index extraction paths. ``fetch_api``
# substitutes the record's attributes into the template, injects auth from the
# named credential, performs ONE cached HTTP request (TTL + 15s timeout + graceful
# fallback, mirroring fetch_nwis_discharge / _NWIS_CACHE), parses JSON, and walks
# the configured path(s) to extract the value / (x,y) series / raw JSON.
#
# Design contract (from the probe): adding an API integration is DATA (a connector
# row), never DDL. Secrets live ONLY in hydro_credential, are resolved lazily into
# local scope at request time, and are never logged, never written into the
# connector row, never returned by the test flow.
# ===========================================================================

# In-process TTL cache, keyed by (connector_name, resolved_url) — same shape as
# _NWIS_CACHE. Each entry is (fetched_at_epoch, parsed_json). Caching the RAW
# parsed JSON (not the extracted value) lets value/series/json reuse one fetch and
# lets the test flow share the cache with the live detail-view path.
_API_CACHE = {}
_API_DEFAULT_TTL = 900  # seconds (15 min), matching _NWIS_TTL
_API_DEFAULT_TIMEOUT = 15
_NO_DATA = (None, "", "-999999")  # USGS no-data sentinel + empties

# A path segment is a LIST INDEX iff the current node is a list AND the segment is
# an optionally-signed integer (so '-1' is the last/latest element, native Python).
_INT_SEG = re.compile(r"-?\d+$")


def _json_path(data, dotpath):
    """Walk a dotted/index path into parsed JSON and return the leaf, or None.

    Splitting on '.', each segment indexes a list when the current node is a list
    and the segment is an integer (``value.timeSeries.0.values.0.value.-1.value``)
    or keys a dict otherwise. Negative integers use native Python indexing so
    ``.-1`` is the latest reading. Empty segments are skipped. Any miss (KeyError /
    IndexError / wrong node type) degrades to ``None`` — a bad path is a soft miss,
    never a 500 — mirroring the graceful fallback in fetch_nwis_discharge."""
    if not dotpath:
        return data
    obj = data
    try:
        for seg in str(dotpath).split("."):
            if seg == "":
                continue
            if isinstance(obj, list) and _INT_SEG.match(seg):
                obj = obj[int(seg)]
            elif isinstance(obj, dict):
                obj = obj[seg]
            else:
                return None
        return obj
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _json_path_series(data, dotpath):
    """Collect an ARRAY by mapping the path tail over a list at a '*' segment.

    ``value.timeSeries.0.values.0.value.*.dateTime`` walks to the readings list at
    '*', then pulls ``dateTime`` from every element — materializing the x (or y)
    array in one pass. With no '*' the path is walked as a plain scalar and wrapped
    in a single-element list. Misses yield [] (soft)."""
    if not dotpath:
        return []
    segs = [s for s in str(dotpath).split(".") if s != ""]
    if "*" not in segs:
        val = _json_path(data, dotpath)
        return [] if val is None else [val]
    star = segs.index("*")
    head, tail = ".".join(segs[:star]), ".".join(segs[star + 1:])
    node = _json_path(data, head) if head else data
    if not isinstance(node, list):
        return []
    out = []
    for el in node:
        out.append(_json_path(el, tail) if tail else el)
    return out


# ===========================================================================
# OUTPUTS — the multi-output catalog. A connector's config.outputs[] is a list of
# named outputs, each either:
#   {'name', 'kind':'value',  'path', 'type', 'unit'?, 'primary'?}             OR
#   {'name', 'kind':'series', 'array_path', 'variables':[{name,path}], ...}
# A SCALAR leaf is ONE 'value' output. A JSON ARRAY of records is ONE 'series'
# output that captures ALL of an element's variables: clicking the array node in
# the Test tree builds one ``variables[]`` entry per sub-key (each a '*'-wildcard
# path), so an array of 20+ points becomes ONE multi-column series (a column per
# variable) — NEVER N outputs. The detail view renders that series as a SINGLE
# table whose column headers ARE the variable names.
#
# LEGACY SHAPE: older series outputs carry {'x_path','y_path'} (two variables)
# instead of variables[]; _series_variables() normalizes both into a column list,
# so the renderer/extractor never branch on shape.
#
# BACK-COMPAT: outputs[] is OPTIONAL. _connector_outputs synthesizes ONE primary
# output from the legacy result_kind/output_path/x_path/y_path when outputs[] is
# absent OR empty — mirroring the inputs[] absent-vs-empty pattern. An empty
# outputs[] therefore behaves as 'use the legacy single output', not 'zero outputs'.
# ===========================================================================


def _last_seg(path):
    """Last meaningful segment of a dotted path (skipping ``*`` and empties).
    ``a.b.*.dateTime`` -> 'dateTime'; ``data.*`` -> 'data'; '' -> ''."""
    segs = [s for s in str(path or "").split(".") if s and s != "*"]
    return segs[-1] if segs else ""


def _series_variables(out):
    """Normalize a series output into an ordered column list ``[{name, path}]``.

    Prefers the modern ``variables[]`` (one entry per captured sub-key). Falls
    back to the legacy ``x_path``/``y_path`` pair (-> two columns named after their
    leaf segments) so old connectors keep rendering. Entries without a path are
    dropped; a missing name is derived from the path's last segment."""
    out = out or {}
    norm = []
    for v in (out.get("variables") or []):
        if isinstance(v, dict) and (v.get("path") or "").strip():
            norm.append({"name": (v.get("name") or _last_seg(v["path"]) or "var"),
                         "path": v["path"].strip()})
    if norm:
        return norm
    xp, yp = (out.get("x_path") or "").strip(), (out.get("y_path") or "").strip()
    if xp:
        norm.append({"name": _last_seg(xp) or "time", "path": xp})
    if yp:
        norm.append({"name": _last_seg(yp) or "value", "path": yp})
    return norm


def _array_path_from_vars(variables):
    """Derive the shared array path (prefix up to and including ``*``) from a
    series' variables, for display/seed. ``a.b.*.value`` & ``a.b.*.t`` -> 'a.b.*'."""
    for v in (variables or []):
        p = (v.get("path") or "") if isinstance(v, dict) else ""
        if ".*" in p:
            return p.split(".*")[0] + ".*"
        if p:
            return p
    return ""

# The doctype-side render modes a ticked output can use, keyed by output.type. A
# 'series' output FORCES 'Time-Series' (it can't render as a scalar); value outputs
# never offer 'Time-Series'.
_OUTPUT_FIELD_TYPES = ("Number", "Text", "Date", "Time-Series", "Image")
# A Python-script output can also be stored as raw JSON (an object/array kept as-is) —
# the alternative to "Table" for a dict output.
_SCRIPT_OUTPUT_FIELD_TYPES = ("Number", "Text", "Date", "Time-Series", "JSON")
_OUTPUT_TYPE_TO_FIELD = {
    "number": "Number",
    "string": "Text",
    "text": "Text",
    "date": "Date",
    "series": "Time-Series",
    "image": "Image",
}


def _connector_outputs(cfg):
    """Return the connector's outputs[] catalog, synthesizing the legacy single
    output when none is declared (BACK-COMPAT).

    If ``cfg['outputs']`` is present AND non-empty, the declared list is returned
    verbatim. Otherwise ONE primary output is synthesized from the legacy
    result_kind/output_path/x_path/y_path so every existing connector/preset still
    exposes exactly one output:
      result_kind=='series' -> [{name:'series', kind:'series', x_path, y_path,
                                  type:'series', primary:True}]
      result_kind=='value'  -> [{name:'value',  kind:'value',  path:output_path,
                                  type:'string', primary:True}]
      result_kind=='json'   -> [{name:'json',   kind:'value',  path:output_path,
                                  type:'string', primary:True}]
    """
    cfg = cfg or {}
    declared = cfg.get("outputs")
    if declared:
        out = []
        for o in declared:
            if isinstance(o, dict) and (o.get("name") or "").strip():
                out.append(o)
        if out:
            return out
    # NetCDF / THREDDS connectors: synthesize a series (the variable along its time
    # dimension) + a latest value, keyed off the configured variable + x_dim.
    knd = (cfg.get("kind") or "rest").lower()
    if knd in ("netcdf", "thredds"):
        # One or MORE variables (the Variable field is a comma list). Synthesize: a
        # combined 'table' series (all variables as columns along x_dim) when there
        # are several, plus per-variable a series + a 'latest' value. The doctype
        # field checklist then picks which outputs to show.
        var_list = [v for v in (cfg.get("variables")
                                or ([cfg.get("variable")] if cfg.get("variable") else []))
                    if (v or "").strip()]
        if not var_list:
            return []
        x_dim = (cfg.get("x_dim") or "time").strip()
        unit = (cfg.get("unit") or "").strip()
        derived = [d for d in (cfg.get("derived") or [])
                   if isinstance(d, dict) and (d.get("name") or "").strip()
                   and (d.get("formula") or "").strip()]
        outs = []
        # A combined table when there are several variables OR any derived columns
        # (a computed column needs the table to carry every variable for the row-wise
        # expression). It's primary unless there's a single plain variable.
        table = (len(var_list) > 1) or bool(derived)
        for i, var in enumerate(var_list):
            outs.append({"name": var, "kind": "series", "type": "series",
                         "var": var, "x_dim": x_dim, "unit": unit,
                         "primary": (not table and i == 0)})
            outs.append({"name": var + "_latest", "kind": "value", "type": "number",
                         "var": var, "unit": unit})
        if table:
            outs.insert(0, {"name": "table", "kind": "series", "type": "series",
                            "primary": True, "vars": var_list, "derived": derived,
                            "x_dim": x_dim, "unit": unit})
        return outs
    if knd == "csv":
        # CSV: the whole table is ONE series (every column becomes a variable, so it
        # reuses the existing multi-column table render); plus a 'latest' value of
        # the chosen value column. The columns are discovered at fetch time.
        return [
            {"name": "table", "kind": "series", "type": "series", "primary": True},
            {"name": "latest", "kind": "value", "type": "number",
             "value_column": (cfg.get("value_column") or "").strip()},
        ]
    if knd == "wms":
        # WMS: a GetMap image centred on the record's point (the primary output),
        # plus a best-effort GetFeatureInfo scalar at that point.
        return [
            {"name": "map", "kind": "image", "type": "image", "primary": True},
            {"name": "featureinfo", "kind": "value", "type": "string"},
        ]
    if knd == "wcs":
        # WCS: the coverage VALUE near the record's point (spatial mean of a small
        # subset, the primary), plus a time series when the coverage has a time axis.
        var = (cfg.get("variable") or "").strip()
        unit = (cfg.get("unit") or "").strip()
        return [
            {"name": "value", "kind": "value", "type": "number", "primary": True,
             "var": var, "unit": unit},
            {"name": "series", "kind": "series", "type": "series",
             "var": var, "unit": unit},
        ]
    if knd == "gee":
        # Earth Engine: sample an Image at the record's point (value), or reduce an
        # ImageCollection over a date range at the point (series).
        unit = (cfg.get("unit") or "").strip()
        return [
            {"name": "value", "kind": "value", "type": "number", "primary": True,
             "unit": unit},
            {"name": "series", "kind": "series", "type": "series", "unit": unit},
        ]
    result_kind = (cfg.get("result_kind") or "value").lower()
    if result_kind == "series":
        return [{
            "name": "series", "kind": "series",
            "x_path": cfg.get("x_path") or "", "y_path": cfg.get("y_path") or "",
            "type": "series", "primary": True,
        }]
    # 'value' and 'json' both extract a scalar leaf via output_path.
    return [{
        "name": ("json" if result_kind == "json" else "value"),
        "kind": "value", "path": cfg.get("output_path") or "",
        "type": "string", "primary": True,
    }]


def _primary_output(outputs):
    """Pick the primary output from a catalog: the first ``primary:True`` entry, or
    the first output, or None for an empty list."""
    outputs = outputs or []
    for o in outputs:
        if isinstance(o, dict) and o.get("primary"):
            return o
    return outputs[0] if outputs else None


def _find_output(outputs, name):
    """Find an output by name in a catalog, or None."""
    for o in (outputs or []):
        if isinstance(o, dict) and (o.get("name") or "").strip() == name:
            return o
    return None


def _extract_output(data, out):
    """Extract ONE named output from the SINGLE already-fetched parsed JSON ``data``.

    ``out`` is one outputs[] entry. A 'value' output walks ``out['path']`` to a
    scalar leaf via _json_path; a 'series' output maps EACH of its variables
    (``_series_variables``) over the array at their shared '*' segment via
    _json_path_series, materializing one aligned COLUMN per variable. The result
    carries ``columns:[{name, values}]`` (rendered as one table whose headers are
    the variable names) plus back-compat ``x``/``y`` (first column = x, the
    'value'-ish column = y) for the sparkline. This is the reusable post-fetch
    extraction the detail renderer calls N times against the SAME cached ``data``
    (one HTTP hit for all ticked outputs)."""
    out = out or {}
    kind = (out.get("kind") or "value").lower()
    if kind == "series":
        variables = _series_variables(out)
        columns = [{"name": v["name"], "values": _json_path_series(data, v["path"])}
                   for v in variables]
        n = max((len(c["values"]) for c in columns), default=0)
        # Back-compat x/y for the sparkline: x = first column; y = the column named
        # 'value' (or the 2nd) with USGS no-data sentinels dropped PAIRWISE so the
        # chart never plots a -999999 spike.
        xs = columns[0]["values"] if columns else []
        yi = next((i for i, c in enumerate(columns)
                   if (c["name"] or "").lower() == "value"), 1 if len(columns) > 1 else None)
        ys = columns[yi]["values"] if (yi is not None and yi < len(columns)) else []
        pairs = [(x, y) for x, y in zip(xs, ys) if y not in _NO_DATA]
        return {
            "kind": "series", "columns": columns, "n": n,
            "x": [x for x, _ in pairs], "y": [y for _, y in pairs],
        }
    return {"kind": "value", "value": _coerce_output(_json_path(data, out.get("path") or ""),
                                                     out.get("type"))}


def _coerce_output(value, typ):
    """Coerce an extracted value by its declared output type so a number that arrived
    as a JSON string (e.g. '1.5') becomes a float for charts/math. Strings/dates/objects
    pass through unchanged; an un-coercible value is left as-is (never raises)."""
    typ = (typ or "").lower()
    if value is None or not isinstance(value, str):
        return value
    if typ in ("number", "float", "double"):
        try:
            return float(value)
        except ValueError:
            return value
    if typ in ("integer", "int"):
        try:
            return int(float(value))
        except ValueError:
            return value
    return value


def _render_body(template, attrs):
    """Substitute {field} tokens in a request BODY template from attrs — WITHOUT URL
    encoding (a JSON/GraphQL body, not a URL). A missing token becomes ''. Structurally
    injection-safe (only bare {name} tokens, looked up in a plain dict)."""
    if not template:
        return None

    def _sub(match):
        v = attrs.get(match.group(1))
        return "" if v in (None, "") else str(v)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, str(template))


def _merge_extracted(pages):
    """Merge per-page _extract_output results from a paginated fetch. Series pages are
    concatenated column-wise (aligned by column name); value/json pages keep the LAST
    non-empty. ``pages`` is the list of extracted dicts in page order."""
    pages = [p for p in pages if p]
    if not pages:
        return {"kind": "value", "value": None}
    if any(p.get("kind") == "series" for p in pages):
        names, cols = [], {}
        for p in pages:
            for c in (p.get("columns") or []):
                nm = c.get("name")
                if nm not in cols:
                    cols[nm] = []
                    names.append(nm)
                cols[nm].extend(c.get("values") or [])
        columns = [{"name": nm, "values": cols[nm]} for nm in names]
        n = max((len(c["values"]) for c in columns), default=0)
        xs = columns[0]["values"] if columns else []
        yi = next((i for i, c in enumerate(columns)
                   if (c["name"] or "").lower() == "value"), 1 if len(columns) > 1 else None)
        ys = columns[yi]["values"] if (yi is not None and yi < len(columns)) else []
        pairs = [(x, y) for x, y in zip(xs, ys) if y not in _NO_DATA]
        return {"kind": "series", "columns": columns, "n": n,
                "x": [x for x, _ in pairs], "y": [y for _, y in pairs]}
    last = next((p for p in reversed(pages) if p.get("value") is not None
                 or p.get("json") is not None), pages[-1])
    return last


def _render_template(template, attrs):
    """Substitute ``{field}`` placeholders from ``attrs`` (URL-encoding the value),
    leaving an unknown placeholder as an EMPTY string instead of raising.

    Uses a manual regex pass rather than ``str.format(**attrs)`` for two reasons:
    (1) missing keys must degrade to '' (not KeyError), and (2) format-string
    injection like ``{0.__class__}`` / attribute access is structurally impossible
    because only bare ``{name}`` tokens are recognized and looked up in a plain
    dict. Values are quoted with urllib so a site id with spaces/specials is safe in
    a URL or query string."""
    if not template:
        return ""

    def _sub(match):
        key = match.group(1)
        val = attrs.get(key)
        if val in (None, ""):
            return ""
        return urllib.parse.quote(str(val), safe="")

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, str(template))


def _render_value(template, attrs):
    """Like _render_template but WITHOUT URL-encoding — for header values where the
    substituted text is used verbatim (e.g. a templated bearer or X-Api-Key)."""
    if not template:
        return ""

    def _sub(match):
        key = match.group(1)
        val = attrs.get(key)
        return "" if val in (None, "") else str(val)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, str(template))


def _inject_auth(auth, secret, headers, query):
    """Apply auth to ``headers``/``query`` (mutated in place) using ``secret``
    resolved from the named credential. Called AFTER templating, just before the
    HTTP call. The secret stays in local scope only — never stored or echoed.

    none      -> no-op.
    api_key   -> placement 'header': headers[param]=secret; 'query': query[param]=secret.
    bearer    -> headers['Authorization'] = 'Bearer ' + secret (placement forced header).
    basic     -> secret is 'user:pass'; headers['Authorization'] = 'Basic ' + b64.
    """
    scheme = (auth or {}).get("scheme") or "none"
    if scheme == "none" or not secret:
        return
    placement = (auth or {}).get("placement") or "header"
    param = (auth or {}).get("param") or "Authorization"
    if scheme == "api_key":
        if placement == "query":
            query[param] = secret
        else:
            headers[param] = secret
    elif scheme == "bearer":
        headers["Authorization"] = "Bearer " + secret
    elif scheme == "basic":
        token = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token


def _build_url(url_template, query, attrs):
    """Render the URL template + append the (templated) query dict as a query
    string. Query values are templated then URL-encoded by urlencode; query keys
    that are auth params (already a secret) pass through unencoded-value-safe via
    urlencode too."""
    url = _render_template(url_template, attrs)
    rendered_query = {}
    for k, v in (query or {}).items():
        rendered_query[k] = _render_value(str(v), attrs)
    if rendered_query:
        sep = "&" if ("?" in url) else "?"
        url = url + sep + urllib.parse.urlencode(rendered_query)
    return url


def _resolve_secret(session, credential_name):
    """Fetch the secret string for a credential NAME from hydro_credential, or
    None. The value lives only in the returned local; callers must not persist it."""
    if not credential_name:
        return None
    row = session.execute(
        select(m.HydroCredential.secret)
        .where(m.HydroCredential.name == credential_name)
    ).first()
    return row[0] if row is not None else None


def _xml_to_dict(elem):
    """Normalise an XML ElementTree element into a JSON-shaped dict so the SAME
    _json_path / _json_path_series extractor works on XML APIs (WaterML/SOAP/Atom).
    Namespaces are stripped; attributes become '@name' keys; repeated child tags
    become a list; a leaf element becomes its (stripped) text."""
    tag = elem.tag.split("}")[-1]
    kids = list(elem)
    if not kids:
        text = (elem.text or "").strip()
        if elem.attrib:
            node = {"@" + k.split("}")[-1]: v for k, v in elem.attrib.items()}
            node["#text"] = text
            return {tag: node}
        return {tag: text}
    node = {}
    for c in kids:
        for k, v in _xml_to_dict(c).items():
            if k in node:
                if not isinstance(node[k], list):
                    node[k] = [node[k]]
                node[k].append(v)
            else:
                node[k] = v
    for k, v in elem.attrib.items():
        node["@" + k.split("}")[-1]] = v
    return {tag: node}


# Side-channel for the LAST request error per connector (status/reason), so fetch_api
# can surface "API error: 404 Not Found" instead of a silent no-data. Filled by
# _api_request_json, read once by fetch_api (mirrors the _API_TTL_BY_NAME pattern).
_API_LAST_ERROR = {}


def _api_request_json(connector_name, url, method, headers, timeout,
                      body=None, content_type=None, accept=None):
    """Perform ONE cached HTTP request and return a parsed dict/list tree (or None).

    Parses JSON; falls back to XML (normalised to a dict tree) when the body isn't
    JSON or the Content-Type is XML — so the same path extractor covers both. Sends a
    request BODY (bytes) when given, with its Content-Type. Records the last HTTP error
    in _API_LAST_ERROR for the caller to surface. On any failure returns None (graceful)
    — a flaky API never 500s a detail page. Cache key folds in the method + body so a
    POST query caches distinctly from a GET."""
    now = time.time()
    key = (connector_name, url, (method or "GET").upper(), body or "")
    cached = _API_CACHE.get(key)
    if cached and (now - cached[0]) < _api_cache_ttl(connector_name):
        return cached[1]
    _API_LAST_ERROR.pop(connector_name, None)
    try:
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=(method or "GET").upper())
        for hk, hv in (headers or {}).items():
            req.add_header(hk, hv)
        if data is not None and content_type:
            req.add_header("Content-Type", content_type)
        if accept:
            req.add_header("Accept", accept)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
        text = raw.decode("utf-8-sig", "replace")
        payload = None
        if "xml" not in ctype:
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                payload = None
        if payload is None:                       # not JSON -> try XML -> dict tree
            try:
                import xml.etree.ElementTree as ET
                payload = _xml_to_dict(ET.fromstring(text))
            except Exception:
                payload = None
        if payload is None:
            return None
        _API_CACHE[key] = (now, payload)
        return payload
    except urllib.error.HTTPError as exc:
        _API_LAST_ERROR[connector_name] = "%s %s" % (exc.code, exc.reason)
        return None
    except Exception as exc:
        _API_LAST_ERROR[connector_name] = type(exc).__name__
        return None


# Per-connector TTL is read from its config at fetch time; this tiny registry lets
# _api_request_json honor it without threading the connector through. It is filled
# by fetch_api right before the request.
_API_TTL_BY_NAME = {}


def _api_cache_ttl(connector_name):
    return _API_TTL_BY_NAME.get(connector_name, _API_DEFAULT_TTL)


def _connector_config(connector):
    """Return the config dict from a HydroConnector row (or a passed-in dict)."""
    if connector is None:
        return {}
    if isinstance(connector, dict):
        return connector
    return connector.config or {}


def _resolve_inputs(cfg, record_attrs, field_map=None):
    """Resolve a connector's ``inputs[]`` into a flat ``{input.name: value}`` dict.

    This is the SINGLE source of truth for the sourced-inputs model; both
    ``fetch_api`` (the real fetch) and ``_bust_api_cache_for_field`` (the Refresh
    cache-eviction) call it so they always derive the SAME URL (and therefore the
    same cache key). Returns ``(attrs, missing_required)`` where ``attrs`` is the
    flat token dict and ``missing_required`` is the list of required input names
    that resolved to empty (the caller turns that into a soft-empty result).

    RESOLUTION ORDER per input (first non-empty wins):
      1. runtime override  — record_attrs[input.name] (a value supplied at fetch
         time directly under the input's own name; the Test panel + ?runtime use).
      2. mapped record field — record_attrs[<resolved field key>], where the field
         key is field_map[input.name] (the per-doctype x-api-map remap) when
         present, else the connector input's own ``field``.
      3. constant value    — the input's literal ``value`` (source const/constant).
      4. default           — the input's ``default`` fallback.
      5. ""                — empty string (a missing required input is recorded).

    ``source`` is advisory: source=='field' privileges step 2, source in
    {'constant','const','value'} privileges step 3, source=='runtime' privileges
    step 1, source=='default' privileges step 4 — but the chain above is applied
    uniformly so a field-sourced input still honors a runtime override and falls
    back to its default. ``source=='secret'`` is resolved through the auth/secret
    path elsewhere (never materialized here), so it is treated as no value.
    """
    rec = record_attrs or {}
    field_map = field_map or {}
    attrs = {}
    missing_required = []
    for inp in (cfg.get("inputs") or []):
        if not isinstance(inp, dict):
            continue
        name = (inp.get("name") or "").strip()
        if not name:
            continue
        source = (inp.get("source") or "field").strip().lower()

        # x-api-map remap: a per-doctype override for THIS input's field key.
        # x-api-map entry may be {'source':'field','field':'<key>'} or
        # {'source':'const','value':'<v>'}; a bare string is treated as a field key.
        mapped_field = None
        mapped_const = None
        if name in field_map:
            mv = field_map[name]
            if isinstance(mv, dict):
                msrc = (mv.get("source") or "field").strip().lower()
                if msrc in ("const", "constant", "value"):
                    mapped_const = mv.get("value")
                else:
                    mapped_field = mv.get("field") or mv.get("value")
            elif mv:
                mapped_field = str(mv)

        field_key = mapped_field or inp.get("field") or name

        # 1) runtime override (a value supplied directly under the input name).
        val = rec.get(name)
        # 2) mapped/declared record field.
        if val in (None, "") and source != "secret":
            val = rec.get(field_key)
        # 3) constant (x-api-map const overrides the connector's own constant).
        if val in (None, ""):
            if mapped_const not in (None, ""):
                val = mapped_const
            elif source in ("constant", "const", "value"):
                val = inp.get("value")
            elif inp.get("value") not in (None, ""):
                # A field/runtime input may still carry a constant value as a
                # secondary fallback (e.g. format=json declared as source=field).
                val = inp.get("value")
        # 4) default fallback.
        if val in (None, ""):
            val = inp.get("default")

        if val in (None, ""):
            val = ""
            if inp.get("required"):
                missing_required.append(name)
        attrs[name] = val
    return attrs, missing_required


# ===========================================================================
# NetCDF / THREDDS (OPeNDAP) connectors. netCDF4 reads a local .nc file OR a remote
# OPeNDAP/dodsC URL directly; siphon resolves a THREDDS catalog to an OPeNDAP URL.
# A connector's variable + x_dim synthesize a series (the variable along its time
# dimension) + a latest value, returned in the SAME value/series shape the doctype
# render already consumes. All failures degrade to a soft-empty result (never raise).
# ===========================================================================

def _netcdf_to_list(arr):
    """Convert a (possibly masked) numpy array to a plain Python list, mapping
    masked entries and NaNs to None (so the renderer shows an em-dash)."""
    import numpy as np
    a = np.ma.filled(np.ma.asarray(arr).astype("float64"), np.nan)
    out = []
    for x in np.atleast_1d(a).ravel().tolist():
        if x is None or (isinstance(x, float) and x != x):   # None / NaN
            out.append(None)
        else:
            out.append(round(float(x), 4))                   # trim float noise
    return out


def _netcdf_coord_values(cvar):
    """Coordinate-variable values as a list; CF time axes (units '… since …') are
    decoded to ISO datetime strings via num2date."""
    import netCDF4
    import numpy as np
    units = getattr(cvar, "units", None)
    cal = getattr(cvar, "calendar", "standard")
    vals = cvar[:]
    if units and "since" in str(units).lower():
        try:
            dts = netCDF4.num2date(vals, str(units), cal)
            return [str(d) for d in np.atleast_1d(dts).ravel().tolist()]
        except Exception:
            pass
    return [str(v) for v in _netcdf_to_list(vals)]


def _nearest_index(coord_var, target, circular=False):
    """Index of the value nearest ``target`` in a 1-D coordinate variable, or None.
    ``circular`` (for LONGITUDE) uses wrap-around distance mod 360, so a target in any
    convention (−180…180 or 0…360) matches the right cell regardless of the dataset's
    convention — e.g. a record's −160°W finds the dataset's 200°E."""
    import numpy as np
    try:
        vals = np.ma.asarray(coord_var[:]).astype("float64")
        if vals.ndim != 1 or not vals.size:
            return None
        if circular:
            diff = np.abs(((vals - float(target) + 180.0) % 360.0) - 180.0)
        else:
            diff = np.abs(vals - float(target))
        return int(np.argmin(diff))
    except Exception:
        return None


def _range_slice(coord_var, lo, hi, circular=False):
    """A slice over a 1-D coord covering [lo, hi] (inclusive), or slice(None) when the
    bounds are missing or nothing falls inside. ``circular`` (LONGITUDE) normalises the
    bounds and coords mod 360 so a Pacific window matches whatever convention the
    dataset uses (a window that wraps the 0/360 seam is approximated by min..max)."""
    import numpy as np
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return slice(None)
    try:
        vals = np.ma.asarray(coord_var[:]).astype("float64")
        if circular:
            cn, lon, hin = vals % 360.0, lo % 360.0, hi % 360.0
            mask = ((cn >= lon) & (cn <= hin)) if lon <= hin else ((cn >= lon) | (cn <= hin))
        else:
            if lo > hi:
                lo, hi = hi, lo
            mask = (vals >= lo) & (vals <= hi)
        idx = np.where(mask)[0]
        if idx.size:
            return slice(int(idx.min()), int(idx.max()) + 1)
    except Exception:
        pass
    return slice(None)


def _bbox_bounds(cfg, is_lon, target):
    """Resolve a bbox axis's (lo, hi): the connector's fixed lon/lat-min/max when set,
    else the RECORD'S point ± bbox_buffer degrees — so the box is DYNAMIC per record
    (centred on each record's geometry). (None, None) when neither is available."""
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    lo = _num(cfg.get("lon_min" if is_lon else "lat_min"))
    hi = _num(cfg.get("lon_max" if is_lon else "lat_max"))
    if lo is not None and hi is not None:
        return lo, hi
    if target is not None:
        buf = _wms_num(cfg.get("bbox_buffer")) or 5.0
        return target - buf, target + buf
    return None, None


def _norm_lon(x):
    """Wrap longitude(s) into (-180, 180] so a polygon's coords and the grid agree."""
    import numpy as np
    return ((np.asarray(x, dtype="float64") + 180.0) % 360.0) - 180.0


def _parse_polygon_text(s):
    """Parse a polygon from WKT (``POLYGON((lon lat, ...),(hole...))``) or GeoJSON
    (Polygon/MultiPolygon) into rings ``[[(lon,lat), ...], ...]``. None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            g = json.loads(s)
            t = (g.get("type") or "").lower()
            c = g.get("coordinates")
            if t == "polygon" and c:
                return [[(float(p[0]), float(p[1])) for p in ring] for ring in c]
            if t == "multipolygon" and c:
                return [[(float(p[0]), float(p[1])) for p in ring]
                        for poly in c for ring in poly]
        except Exception:
            return None
        return None
    m = re.search(r"POLYGON\s*\((.*)\)\s*$", s, re.I | re.S)
    if not m:
        return None
    rings = []
    for ring_txt in re.findall(r"\(([^()]*)\)", m.group(1)):
        pts = []
        for pair in ring_txt.split(","):
            xy = pair.split()
            if len(xy) >= 2:
                try:
                    pts.append((float(xy[0]), float(xy[1])))
                except ValueError:
                    pass
        if pts:
            rings.append(pts)
    return rings or None


def _polygon_rings(cfg):
    """The connector's polygon region as rings ``[[(lon,lat), ...], ...]`` — from a
    SHAPEFILE path (pyshp, first polygon) or a pasted WKT/GeoJSON ``polygon``. None
    when neither is set/readable."""
    sp = (cfg.get("shapefile") or "").strip()
    if sp:
        try:
            import shapefile
            for shp in shapefile.Reader(sp).shapes():
                pts = shp.points or []
                parts = list(shp.parts) + [len(pts)]
                rings = [[(p[0], p[1]) for p in pts[parts[i]:parts[i + 1]]]
                         for i in range(len(parts) - 1)]
                rings = [r for r in rings if len(r) >= 3]
                if rings:
                    return rings
        except Exception:
            pass
    return _parse_polygon_text(cfg.get("polygon"))


def _open_shapefile_reader(data):
    """Open a pyshp ``Reader`` from raw uploaded bytes — a zip bundle (.shp/.shx/.dbf)
    or a bare .shp — with a DECOMPRESSION-BOMB guard (refuse if the members would
    inflate past ``_SHP_MAX_BYTES``). None if pyshp is missing or the bytes are
    unreadable/too large."""
    import io
    import zipfile
    try:
        import shapefile  # pyshp
    except Exception:
        return None
    if not data or len(data) > _SHP_MAX_BYTES:
        return None
    try:
        buf = io.BytesIO(data)
        if zipfile.is_zipfile(buf):
            buf.seek(0)
            zf = zipfile.ZipFile(buf)

            def _pick(ext):
                for n in zf.namelist():
                    base = n.rsplit("/", 1)[-1]
                    if base.lower().endswith(ext) and not base.startswith("."):
                        return n
                return None

            shp_n, shx_n, dbf_n = _pick(".shp"), _pick(".shx"), _pick(".dbf")
            if not shp_n:
                return None
            total = 0                                  # uncompressed size of the members
            for nm in (shp_n, shx_n, dbf_n):
                if nm:
                    try:
                        total += zf.getinfo(nm).file_size
                    except KeyError:
                        pass
            if total > _SHP_MAX_BYTES:
                logger.warning("shapefile upload rejected: decompresses to %d bytes (> %d)",
                               total, _SHP_MAX_BYTES)
                return None
            kw = {"shp": io.BytesIO(zf.read(shp_n))}
            if shx_n:
                kw["shx"] = io.BytesIO(zf.read(shx_n))
            if dbf_n:
                kw["dbf"] = io.BytesIO(zf.read(dbf_n))
            return shapefile.Reader(**kw)
        # A bare .shp carries the geometry on its own (shx is only an index).
        return shapefile.Reader(shp=io.BytesIO(data))
    except Exception:
        return None


def _shapefile_upload_to_geojson(file_obj):
    """IMPORT an uploaded shapefile — a zipped bundle (.shp+.shx+.dbf[+.prj]) or a
    bare .shp — and return its FIRST polygon as a GeoJSON string. We convert at
    upload time so the geometry is stored INLINE in the connector's ``polygon``
    config (a pure-JSON row): no server file to keep, and the existing polygon
    machinery (_parse_polygon_text -> _polygon_rings -> _netcdf_polygon_ys) handles
    it unchanged. None if pyshp is missing or nothing polygonal is found."""
    import json as _json
    try:
        data = file_obj.read()
    except Exception:
        return None
    reader = _open_shapefile_reader(data)
    if reader is None:
        return None
    try:
        for shp in reader.iterShapes():
            gj = getattr(shp, "__geo_interface__", None)
            if gj and (gj.get("type") or "") in ("Polygon", "MultiPolygon"):
                return _json.dumps(gj)
    except Exception:
        return None
    return None


def _shapefile_to_featurecollection(file_obj):
    """IMPORT a multi-polygon shapefile (a GRID / CELL / ZONE layer — fishnet, model
    grid, HUC subbasins, admin units) and return a GeoJSON **FeatureCollection** string
    keeping EVERY polygon + its .dbf attributes as feature properties. Unlike
    _shapefile_upload_to_geojson (which keeps only the first polygon, for a single
    region), this preserves the per-cell structure so each polygon can become its own
    zonal column. None if pyshp is missing or no polygons are found."""
    import json as _json

    def _jsonable(val):
        if isinstance(val, (str, int, float, bool)) or val is None:
            return val
        try:
            return val.isoformat()          # dates/datetimes from the .dbf
        except Exception:
            return str(val)

    try:
        data = file_obj.read()
    except Exception:
        return None
    reader = _open_shapefile_reader(data)
    if reader is None:
        return None
    try:
        fields = [f[0] for f in reader.fields[1:]] if reader.fields else []
        try:
            recs = list(reader.shapeRecords())
        except Exception:
            recs = None
        feats = []
        if recs:
            for sr in recs:
                gj = getattr(sr.shape, "__geo_interface__", None)
                if not gj or (gj.get("type") or "") not in ("Polygon", "MultiPolygon"):
                    continue
                try:
                    props = {k: _jsonable(v) for k, v in dict(sr.record.as_dict()).items()}
                except Exception:
                    try:
                        props = {k: _jsonable(v) for k, v in zip(fields, list(sr.record))}
                    except Exception:
                        props = {}
                feats.append({"type": "Feature", "geometry": gj, "properties": props})
        else:
            for shp in reader.iterShapes():
                gj = getattr(shp, "__geo_interface__", None)
                if gj and (gj.get("type") or "") in ("Polygon", "MultiPolygon"):
                    feats.append({"type": "Feature", "geometry": gj, "properties": {}})
        if not feats:
            return None
        return _json.dumps({"type": "FeatureCollection", "features": feats})
    except Exception:
        return None


def _geom_rings(geom):
    """A GeoJSON geometry dict -> flat rings ``[[(lon,lat), ...], ...]`` (a Polygon's
    exterior+holes, or ALL rings of a MultiPolygon). [] when not polygonal."""
    if not isinstance(geom, dict):
        return []
    t = (geom.get("type") or "").lower()
    c = geom.get("coordinates")
    rings = []
    try:
        if t == "polygon" and c:
            for ring in c:
                rings.append([(float(p[0]), float(p[1])) for p in ring])
        elif t == "multipolygon" and c:
            for poly in c:
                for ring in poly:
                    rings.append([(float(p[0]), float(p[1])) for p in ring])
    except (TypeError, ValueError, IndexError):
        return []
    return [r for r in rings if len(r) >= 3]


def _ring_centroid_label(rings):
    """A short ``'lat,lon'`` label = the average of a zone's first (exterior) ring."""
    pts = rings[0] if rings else []
    if not pts:
        return "zone"
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return "%.2f,%.2f" % (lat, lon)


# .dbf attribute names that commonly identify a zone/cell, tried (case-insensitively)
# when no explicit label field is set.
_ZONE_LABEL_KEYS = ("name", "label", "id", "zone", "zone_id", "zone_name",
                    "huc", "huc12", "huc8", "huc10", "gridid", "grid_id",
                    "cellid", "cell_id", "fid", "objectid", "gridcode")


def _zone_label_from_props(props, label_field, idx, rings):
    """Pick a human label for one zone: the chosen ``label_field`` (case-insensitive),
    else a well-known id attribute, else the ring centroid, else ``zone_<n>``."""
    if isinstance(props, dict) and props:
        if label_field:
            for k in props:
                if k.lower() == label_field.lower() and props[k] not in (None, ""):
                    return str(props[k])
        low = {k.lower(): k for k in props}
        for key in _ZONE_LABEL_KEYS:
            if key in low and props[low[key]] not in (None, ""):
                return str(props[low[key]])
    return _ring_centroid_label(rings) if rings else ("zone_%d" % (idx + 1))


def _zones_from_geojson(s, label_field=None):
    """Parse a GeoJSON string into per-zone ``[(label, rings), ...]`` — a
    FeatureCollection (one zone per feature, labelled from properties), a single
    Feature, a MultiPolygon (one zone per constituent polygon), or a Polygon. None
    when ``s`` isn't usable GeoJSON."""
    s = (s or "").strip()
    if not s.startswith("{"):
        return None
    try:
        g = json.loads(s)
    except (ValueError, TypeError):
        return None
    t = (g.get("type") or "").lower()
    zones = []
    if t == "featurecollection":
        for i, feat in enumerate(g.get("features") or []):
            rings = _geom_rings((feat or {}).get("geometry") or {})
            if rings:
                zones.append((_zone_label_from_props((feat or {}).get("properties"),
                                                     label_field, i, rings), rings))
    elif t == "feature":
        rings = _geom_rings(g.get("geometry") or {})
        if rings:
            zones.append((_zone_label_from_props(g.get("properties"), label_field, 0, rings), rings))
    elif t == "multipolygon":
        for i, poly in enumerate(g.get("coordinates") or []):
            rings = []
            for ring in poly:
                try:
                    rings.append([(float(p[0]), float(p[1])) for p in ring])
                except (TypeError, ValueError, IndexError):
                    pass
            rings = [r for r in rings if len(r) >= 3]
            if rings:
                zones.append((_ring_centroid_label(rings), rings))
    elif t == "polygon":
        rings = _geom_rings(g)
        if rings:
            zones.append((_ring_centroid_label(rings), rings))
    return zones or None


def _zone_polygons(cfg, attrs=None):
    """The connector's ZONES as ``[(label, rings), ...]`` — each entry is ONE polygon
    (a cell/zone) with its exterior+hole rings.

    When ``zones_source == 'record'`` the zones are DYNAMIC: they come from the
    triggering record's OWN geometry (a Polygon -> one zone; a MultiPolygon -> one
    zone per part), injected as the GeoJSON ``attrs['_geojson']`` by the detail view —
    so each record reduces over its own shape (the per-zone analog of the dynamic
    bbox/point). Otherwise the zones are FIXED on the connector, source priority:
    inline ``zones`` FeatureCollection (imported shapefile / pasted) -> ``polygon``
    text (FeatureCollection/MultiPolygon/Polygon/WKT) -> a server ``shapefile`` path.
    None when nothing usable is configured."""
    cfg, attrs = cfg or {}, attrs or {}
    label_field = (cfg.get("zone_label") or "").strip() or None
    if (cfg.get("zones_source") or "").strip().lower() == "record":
        # DYNAMIC: ONLY the record's geometry is the zone set (no .dbf attrs -> centroid
        # / 'zone_N' labels). _geojson is a bare GeoJSON geometry (Polygon/MultiPolygon),
        # injected by the detail view. A non-polygon / geometry-less record yields None
        # here -> a soft-empty series (NOT a silent fall-back to some connector-level
        # polygon, which would mislabel shared zones as this record's own). For the Test
        # panel, connector_test injects the pasted polygon as a stand-in _geojson so this
        # same record path runs.
        return _zones_from_geojson(attrs.get("_geojson"), label_field)
    z = _zones_from_geojson(cfg.get("zones"), label_field)
    if z:
        return z
    z = _zones_from_geojson(cfg.get("polygon"), label_field)
    if z:
        return z
    rings = _parse_polygon_text(cfg.get("polygon"))     # WKT single polygon
    if rings:
        return [(_ring_centroid_label(rings), rings)]
    return _zones_from_shapefile_path(cfg.get("shapefile"), label_field)


def _zones_from_shapefile_path(sp, label_field=None):
    """Read a server-side .shp PATH into per-zone ``[(label, rings), ...]`` (pyshp).
    None when the path is blank/unreadable or holds no polygons."""
    sp = (sp or "").strip()
    if not sp:
        return None
    try:
        import shapefile
        reader = shapefile.Reader(sp)
        fields = [f[0] for f in reader.fields[1:]] if reader.fields else []
        zones = []
        try:
            recs = list(reader.shapeRecords())
        except Exception:
            recs = None
        if recs:
            for i, sr in enumerate(recs):
                rings = _geom_rings(getattr(sr.shape, "__geo_interface__", {}) or {})
                if not rings:
                    continue
                try:
                    props = dict(sr.record.as_dict())
                except Exception:
                    try:
                        props = dict(zip(fields, list(sr.record)))
                    except Exception:
                        props = {}
                zones.append((_zone_label_from_props(props, label_field, i, rings), rings))
        else:
            for i, shp in enumerate(reader.iterShapes()):
                rings = _geom_rings(getattr(shp, "__geo_interface__", {}) or {})
                if rings:
                    zones.append((_ring_centroid_label(rings), rings))
        return zones or None
    except Exception:
        return None


def _shapefile_zones(cfg, attrs=None):
    """Resolve the SHAPEFILE spatial filter's polygons as ``[(label, rings), ...]``.
    Source priority: the record's uploaded Shapefile field (``attrs['_shapefile']``) ->
    the record's geometry (``attrs['_geojson']``) -> a pasted/inline ``zones``/``polygon``
    on the connector (the Test panel) -> a server ``shapefile`` path. None if none usable."""
    cfg, attrs = cfg or {}, attrs or {}
    label_field = (cfg.get("zone_label") or "").strip() or None
    for src in (attrs.get("_shapefile"), attrs.get("_geojson"),
                cfg.get("zones"), cfg.get("polygon")):
        z = _zones_from_geojson(src, label_field)
        if z:
            return z
    rings = _parse_polygon_text(cfg.get("polygon"))
    if rings:
        return [(_ring_centroid_label(rings), rings)]
    return _zones_from_shapefile_path(cfg.get("shapefile"), label_field)


def _shapefile_union_rings(cfg, attrs):
    """All polygons of the resolved shapefile flattened into one rings list — the
    region as a whole (the even-odd ray-cast unions disjoint parts). None if empty."""
    zones = _shapefile_zones(cfg, attrs)
    if not zones:
        return None
    rings = [r for _label, zrings in zones for r in zrings]
    return rings or None


def _points_in_polygon(lon2d, lat2d, rings):
    """Boolean mask of which (lon,lat) grid points fall inside the polygon (numpy
    ray-cast; rings XOR'd so holes are excluded). Longitudes are normalised to
    (-180,180] on both sides — a polygon crossing the antimeridian isn't supported."""
    import numpy as np
    x = _norm_lon(lon2d)
    y = np.asarray(lat2d, dtype="float64")
    inside = np.zeros(x.shape, dtype=bool)
    for ring in rings:
        rx = _norm_lon([p[0] for p in ring])
        ry = np.asarray([p[1] for p in ring], dtype="float64")
        n = len(ring)
        j = n - 1
        w = np.zeros(x.shape, dtype=bool)
        for i in range(n):
            cond = (((ry[i] > y) != (ry[j] > y)) &
                    (x < (rx[j] - rx[i]) * (y - ry[i]) / ((ry[j] - ry[i]) + 1e-300) + rx[i]))
            w ^= cond
            j = i
        inside ^= w
    return inside


def _netcdf_polygon_ys(ds, v, xd, lat_dim, lon_dim, rings):
    """Series along ``xd`` = the masked mean over the grid cells INSIDE ``rings``.
    Subsets to the polygon's bbox first (cheap), builds a lat×lon point-in-polygon
    mask, applies it, and means the lat/lon axes — for any dimension order."""
    import numpy as np
    latc, lonc = ds.variables.get(lat_dim), ds.variables.get(lon_dim)
    if latc is None or lonc is None:
        return None
    allx = [p[0] for r in rings for p in r]
    ally = [p[1] for r in rings for p in r]
    lat_sl = _range_slice(latc, min(ally), max(ally), circular=False)
    lon_sl = _range_slice(lonc, min(allx), max(allx), circular=True)
    lat_sub = np.asarray(latc[:], dtype="float64")[lat_sl]
    lon_sub = np.asarray(lonc[:], dtype="float64")[lon_sl]
    if not lat_sub.size or not lon_sub.size:
        return None
    LON, LAT = np.meshgrid(lon_sub, lat_sub)        # (nlat, nlon)
    mask_in = _points_in_polygon(LON, LAT, rings)   # True INSIDE
    if not mask_in.any():
        return None
    dims = list(v.dimensions)
    index = [lat_sl if d == lat_dim else lon_sl if d == lon_dim else slice(None)
             for d in dims]
    arr = np.ma.asarray(v[tuple(index)])
    sliced = [d for d in dims if d in (xd, lat_dim, lon_dim) or True]  # all kept (slices)
    lat_ax, lon_ax = dims.index(lat_dim), dims.index(lon_dim)
    arr = np.moveaxis(arr, [lat_ax, lon_ax], [-2, -1])   # (..., nlat, nlon)
    outside = np.broadcast_to(~mask_in, arr.shape)
    arr = np.ma.masked_array(arr, mask=(np.ma.getmaskarray(arr) | outside))
    reduced = np.ma.mean(arr, axis=(-2, -1))             # leading dims (xd + others)
    remaining = [d for d in dims if d not in (lat_dim, lon_dim)]
    if xd not in remaining:
        return _netcdf_to_list(reduced)
    xpos = remaining.index(xd)
    other = tuple(i for i in range(reduced.ndim) if i != xpos)
    return _netcdf_to_list(np.ma.mean(reduced, axis=other) if other else reduced)


def _netcdf_cells_columns(ds, v, x_dim, cfg=None, attrs=None, max_cells=24):
    """PER-CELL columns: each grid cell in the bbox region becomes its OWN column —
    the variable's series at that cell, labelled 'lat,lon' — so you see every cell
    instead of their mean. The region is the bbox (fixed bounds, or DYNAMIC = the
    record's point ± buffer); if it holds more than ``max_cells`` cells, evenly-spaced
    cells are sampled so a big region doesn't explode. Returns [{name,values}] columns
    (x_dim first) or None when unavailable."""
    import math
    import numpy as np
    cfg, attrs = cfg or {}, attrs or {}
    dims = list(v.dimensions)
    xd = x_dim if x_dim in dims else (dims[0] if dims else "")
    lat_dim = (cfg.get("lat_dim") or "").strip()
    lon_dim = (cfg.get("lon_dim") or "").strip()
    if lat_dim not in dims or lon_dim not in dims:
        return None
    latc, lonc = ds.variables.get(lat_dim), ds.variables.get(lon_dim)
    if latc is None or lonc is None:
        return None
    tlat = _wms_num(cfg.get("lat"), attrs.get("_lat"), attrs.get("latitude"), attrs.get("lat"))
    tlon = _wms_num(cfg.get("lon"), attrs.get("_lon"), attrs.get("longitude"), attrs.get("lon"))
    lat_lo, lat_hi = _bbox_bounds(cfg, False, tlat)
    lon_lo, lon_hi = _bbox_bounds(cfg, True, tlon)
    lat_sl = _range_slice(latc, lat_lo, lat_hi, False) if lat_lo is not None else slice(None)
    lon_sl = _range_slice(lonc, lon_lo, lon_hi, True) if lon_lo is not None else slice(None)
    lat_vals = np.asarray(latc[:], dtype="float64")[lat_sl]
    lon_vals = np.asarray(lonc[:], dtype="float64")[lon_sl]
    if not lat_vals.size or not lon_vals.size:
        return None
    # Cap: pick ~sqrt(max_cells) evenly-spaced indices on each axis.
    per = max(1, int(math.sqrt(max(1, max_cells))))
    li = np.unique(np.linspace(0, lat_vals.size - 1, min(lat_vals.size, per)).round().astype(int))
    ji = np.unique(np.linspace(0, lon_vals.size - 1, min(lon_vals.size, per)).round().astype(int))
    index = [lat_sl if d == lat_dim else lon_sl if d == lon_dim else slice(None) for d in dims]
    sub = np.ma.asarray(v[tuple(index)])
    lat_ax, lon_ax = dims.index(lat_dim), dims.index(lon_dim)
    sub = np.moveaxis(sub, [lat_ax, lon_ax], [-2, -1])      # (leading..., nlat, nlon)
    remaining = [d for d in dims if d not in (lat_dim, lon_dim)]
    if xd in remaining:
        xpos = remaining.index(xd)
        other = tuple(i for i in range(len(remaining)) if i != xpos)
        if other:
            sub = np.ma.mean(sub, axis=other)              # collapse any non-x leading dims
        if xpos != 0 and sub.ndim >= 3:
            sub = np.moveaxis(sub, xpos, 0)
    cvar = ds.variables.get(xd)
    xs = _netcdf_coord_values(cvar) if cvar is not None else [str(i) for i in range(sub.shape[0])]
    cols = [{"name": xd or "index", "values": xs}]
    for i in li:
        for j in ji:
            cols.append({"name": "%.1f,%.1f" % (float(lat_vals[i]), float(lon_vals[j])),
                         "values": _netcdf_to_list(sub[:, i, j])})
    return cols


def _netcdf_zones_columns(ds, v, x_dim, cfg=None, attrs=None, max_zones=60):
    """PER-ZONE columns (zonal statistics): each polygon in a multi-polygon source (an
    imported grid/cell/zone shapefile, a pasted FeatureCollection/MultiPolygon, or a
    server shapefile path) becomes its OWN column = the variable's masked MEAN over the
    grid cells inside that polygon, along x_dim, labelled by the polygon's attribute.
    Downloads the union bounding box ONCE and masks each zone against it (so N zones
    cost one fetch, not N). Returns [{name,values}] (x_dim first) or None."""
    import numpy as np
    cfg, attrs = cfg or {}, attrs or {}
    dims = list(v.dimensions)
    xd = x_dim if x_dim in dims else (dims[0] if dims else "")
    lat_dim = (cfg.get("lat_dim") or "").strip()
    lon_dim = (cfg.get("lon_dim") or "").strip()
    if lat_dim not in dims or lon_dim not in dims:
        return None
    latc, lonc = ds.variables.get(lat_dim), ds.variables.get(lon_dim)
    if latc is None or lonc is None:
        return None
    zones = _zone_polygons(cfg, attrs)
    if not zones:
        return None
    cap = max(1, int(max_zones or 60))
    if len(zones) > cap:                                   # surface the dropped count
        logger.warning("netcdf zones: keeping %d of %d polygons (zones_max=%d); %d dropped",
                       cap, len(zones), cap, len(zones) - cap)
        zones = zones[:cap]
    allx = [p[0] for _l, rings in zones for r in rings for p in r]
    ally = [p[1] for _l, rings in zones for r in rings for p in r]
    if not allx or not ally:
        return None
    lat_sl = _range_slice(latc, min(ally), max(ally), circular=False)
    lon_sl = _range_slice(lonc, min(allx), max(allx), circular=True)
    lat_sub = np.asarray(latc[:], dtype="float64")[lat_sl]
    lon_sub = np.asarray(lonc[:], dtype="float64")[lon_sl]
    if not lat_sub.size or not lon_sub.size:
        return None
    LON, LAT = np.meshgrid(lon_sub, lat_sub)            # (nlat, nlon)
    index = [lat_sl if d == lat_dim else lon_sl if d == lon_dim else slice(None)
             for d in dims]
    arr0 = np.ma.asarray(v[tuple(index)])
    lat_ax, lon_ax = dims.index(lat_dim), dims.index(lon_dim)
    arr0 = np.moveaxis(arr0, [lat_ax, lon_ax], [-2, -1])   # (..., nlat, nlon)
    base_mask = np.ma.getmaskarray(arr0)
    remaining = [d for d in dims if d not in (lat_dim, lon_dim)]
    cols = []
    seen = {xd or "index": 1}                              # x column already takes this name

    def _uniq(name):
        # Two zones can share a label (same .dbf value, or centroids that round equal);
        # a downstream consumer keying columns by name would clobber one. Disambiguate.
        name = name or "zone"
        if name not in seen:
            seen[name] = 1
            return name
        seen[name] += 1
        return "%s #%d" % (name, seen[name])

    for label, rings in zones:
        nm = _uniq(label)
        mask_in = _points_in_polygon(LON, LAT, rings)      # True INSIDE this zone
        if not mask_in.any():
            cols.append({"name": nm, "values": []})
            continue
        outside = np.broadcast_to(~mask_in, arr0.shape)
        arr = np.ma.masked_array(arr0, mask=(base_mask | outside))
        reduced = np.ma.mean(arr, axis=(-2, -1))           # collapse lat/lon
        if xd not in remaining:
            ys = _netcdf_to_list(reduced)
        else:
            xpos = remaining.index(xd)
            other = tuple(i for i in range(reduced.ndim) if i != xpos)
            ys = _netcdf_to_list(np.ma.mean(reduced, axis=other) if other else reduced)
        cols.append({"name": nm, "values": ys})
    n = max((len(c["values"]) for c in cols), default=0)
    if not n:
        return None
    for c in cols:                                          # pad empty zones to align
        if len(c["values"]) < n:
            c["values"] = list(c["values"]) + [None] * (n - len(c["values"]))
    cvar = ds.variables.get(xd)
    xs = _netcdf_coord_values(cvar) if cvar is not None else None
    if xs is None or len(xs) < n:
        xs = [str(i) for i in range(n)]
    else:
        xs = xs[:n]
    return [{"name": xd or "index", "values": xs}] + cols


def _netcdf_shapefile_cells(ds, v, x_dim, cfg=None, attrs=None, max_cells=200):
    """ALL CELLS inside the shapefile, NO aggregation: every grid cell whose centre
    falls inside the resolved shapefile region becomes its OWN column ('lat,lon'),
    the variable's series at that cell. Caps to ``max_cells`` evenly-spaced inside
    cells. Returns [{name,values}] (x_dim first) or None."""
    import numpy as np
    cfg, attrs = cfg or {}, attrs or {}
    dims = list(v.dimensions)
    xd = x_dim if x_dim in dims else (dims[0] if dims else "")
    lat_dim = (cfg.get("lat_dim") or "").strip()
    lon_dim = (cfg.get("lon_dim") or "").strip()
    if lat_dim not in dims or lon_dim not in dims:
        return None
    latc, lonc = ds.variables.get(lat_dim), ds.variables.get(lon_dim)
    if latc is None or lonc is None:
        return None
    rings = _shapefile_union_rings(cfg, attrs)
    if not rings:
        return None
    allx = [p[0] for r in rings for p in r]
    ally = [p[1] for r in rings for p in r]
    lat_sl = _range_slice(latc, min(ally), max(ally), circular=False)
    lon_sl = _range_slice(lonc, min(allx), max(allx), circular=True)
    lat_sub = np.asarray(latc[:], dtype="float64")[lat_sl]
    lon_sub = np.asarray(lonc[:], dtype="float64")[lon_sl]
    if not lat_sub.size or not lon_sub.size:
        return None
    LON, LAT = np.meshgrid(lon_sub, lat_sub)               # (nlat, nlon)
    mask = _points_in_polygon(LON, LAT, rings)
    ii, jj = np.where(mask)
    if not len(ii):
        return None
    if len(ii) > max(1, int(max_cells or 200)):            # cap: evenly-spaced inside cells
        sel = np.unique(np.linspace(0, len(ii) - 1, int(max_cells)).round().astype(int))
        ii, jj = ii[sel], jj[sel]
    index = [lat_sl if d == lat_dim else lon_sl if d == lon_dim else slice(None)
             for d in dims]
    sub = np.ma.asarray(v[tuple(index)])
    lat_ax, lon_ax = dims.index(lat_dim), dims.index(lon_dim)
    sub = np.moveaxis(sub, [lat_ax, lon_ax], [-2, -1])     # (leading..., nlat, nlon)
    remaining = [d for d in dims if d not in (lat_dim, lon_dim)]
    if xd in remaining:
        xpos = remaining.index(xd)
        other = tuple(i for i in range(len(remaining)) if i != xpos)
        if other:
            sub = np.ma.mean(sub, axis=other)
        if xpos != 0 and sub.ndim >= 3:
            sub = np.moveaxis(sub, xpos, 0)
    cvar = ds.variables.get(xd)
    xs = _netcdf_coord_values(cvar) if cvar is not None else [str(i) for i in range(sub.shape[0])]
    cols = [{"name": xd or "index", "values": xs}]
    for i, j in zip(ii, jj):
        cols.append({"name": "%.2f,%.2f" % (float(lat_sub[i]), float(lon_sub[j])),
                     "values": _netcdf_to_list(sub[:, int(i), int(j)])})
    return cols


def _netcdf_reduce_ys(ds, v, x_dim, cfg=None, attrs=None):
    """Reduce a variable to a 1-D series along ``x_dim``, applying the connector's
    SPATIAL FILTER to the lat/lon axes before averaging any remaining non-x axes:
      spatial 'point'   -> the grid cell NEAREST (lon, lat);
      spatial 'bbox'    -> the mean over a lat/lon window (fixed, or DYNAMIC = the
                           record's point ± buffer);
      spatial 'polygon' -> the mean over the cells inside a shapefile/WKT region;
      'mean' / default  -> the whole-grid masked mean (size-guarded).
    (lon, lat) come from the config, or — when blank — the record's geometry
    (_lon/_lat) / a longitude|latitude field. Returns the ys list (None for masked)."""
    import numpy as np
    cfg, attrs = cfg or {}, attrs or {}
    dims = list(v.dimensions)
    xd = x_dim if x_dim in dims else (dims[0] if dims else "")
    if not dims or not [d for d in dims if d != xd]:
        return _netcdf_to_list(v[:])    # already 1-D along x (or scalar)
    mode = (cfg.get("spatial") or "mean").lower()
    lat_dim = (cfg.get("lat_dim") or "").strip()
    lon_dim = (cfg.get("lon_dim") or "").strip()
    tlat = _wms_num(cfg.get("lat"), attrs.get("_lat"), attrs.get("latitude"), attrs.get("lat"))
    tlon = _wms_num(cfg.get("lon"), attrs.get("_lon"), attrs.get("longitude"), attrs.get("lon"))

    if mode == "polygon" and lat_dim in dims and lon_dim in dims:
        rings = _polygon_rings(cfg)
        if rings:
            ys = _netcdf_polygon_ys(ds, v, xd, lat_dim, lon_dim, rings)
            if ys is not None:
                return ys
        # no usable polygon -> fall through to whole-grid mean

    use_point = (mode == "point" and lat_dim and lon_dim
                 and tlat is not None and tlon is not None)
    use_bbox = (mode == "bbox" and (lat_dim or lon_dim))
    # Whole-grid mean of a huge variable: sample a mid-grid point (legacy guard);
    # point/bbox slice the lat/lon axes first so they never download the whole grid.
    if not (use_point or use_bbox) and v.size and v.size > 5_000_000:
        idx = tuple(slice(None) if d == xd else (v.shape[k] // 2)
                    for k, d in enumerate(dims))
        return _netcdf_to_list(v[idx])
    index = []
    for k, d in enumerate(dims):
        if d == xd:
            index.append(slice(None))
        elif use_point and d in (lat_dim, lon_dim):
            coord = ds.variables.get(d)
            is_lon = (d == lon_dim)
            ix = (_nearest_index(coord, tlon if is_lon else tlat, circular=is_lon)
                  if coord is not None else None)
            index.append(ix if ix is not None else v.shape[k] // 2)
        elif use_bbox and d in (lat_dim, lon_dim):
            coord = ds.variables.get(d)
            is_lon = (d == lon_dim)
            lo, hi = _bbox_bounds(cfg, is_lon, tlon if is_lon else tlat)
            index.append(_range_slice(coord, lo, hi, circular=is_lon)
                         if coord is not None else slice(None))
        else:
            index.append(slice(None))
    sub = np.ma.asarray(v[tuple(index)])
    sliced_dims = [d for d, ix in zip(dims, index) if isinstance(ix, slice)]
    if xd in sliced_dims and sub.ndim:
        xpos = sliced_dims.index(xd)
        other = tuple(i for i in range(sub.ndim) if i != xpos)
        return _netcdf_to_list(np.ma.mean(sub, axis=other) if other else sub)
    return _netcdf_to_list(sub)


def _netcdf_series_xy(ds, v, x_dim, cfg=None, attrs=None):
    """(xs, ys) for a variable along ``x_dim``, applying the connector's spatial
    filter; xs are the x_dim coordinate values (CF time decoded), masked steps dropped."""
    ys = _netcdf_reduce_ys(ds, v, x_dim, cfg, attrs)
    cvar = ds.variables.get(x_dim) if x_dim else None
    xs = (_netcdf_coord_values(cvar) if cvar is not None
          else [str(i) for i in range(len(ys))])
    pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
    return [x for x, _ in pairs], [y for _, y in pairs]


def _netcdf_var_along(ds, v, x_dim, cfg=None, attrs=None):
    """Spatially-filtered 1-D series (None for masked, NOT dropped) — keeps several
    variables row-aligned for a combined table."""
    return _netcdf_reduce_ys(ds, v, x_dim, cfg, attrs)


def _netcdf_multivar_columns(ds, var_names, x_dim, derived=None, cfg=None, attrs=None):
    """Build aligned table columns for several variables along ``x_dim``: the x_dim
    coordinate first, then one column per existing variable (each reduced over its
    non-x dims), then any DERIVED columns — a row-wise expression over the real
    variables (e.g. ``sqrt(UWND**2 + VWND**2)``) via the safe formula evaluator.
    Columns are truncated to the shortest so rows stay aligned."""
    per = []
    for vn in var_names:
        v = ds.variables.get(vn)
        if v is not None:
            per.append((vn, _netcdf_var_along(ds, v, x_dim, cfg, attrs)))
    if not per:
        return []
    n = min(len(ys) for _vn, ys in per)
    cvar = ds.variables.get(x_dim) if x_dim else None
    xs = _netcdf_coord_values(cvar) if cvar is not None else None
    if xs is None or len(xs) < n:
        xs = [str(i) for i in range(n)]
    cols = [{"name": x_dim or "index", "values": xs[:n]}]
    for vn, ys in per:
        cols.append({"name": vn, "values": ys[:n]})
    # Derived columns: evaluate each formula per row over the real variable values.
    for d in (derived or []):
        dname = (d.get("name") or "").strip()
        formula = (d.get("formula") or "").strip()
        if not dname or not formula:
            continue
        out = []
        for i in range(n):
            rowvars = {}
            for _vn, ys in per:
                yv = ys[i] if i < len(ys) else None
                if isinstance(yv, (int, float)) and not isinstance(yv, bool):
                    rowvars[_vn] = yv
            out.append(_safe_eval(formula, rowvars))
        cols.append({"name": dname, "values": out})
    return cols


def _parse_date_loose(val):
    """A time-coordinate / field value -> a ``datetime.date`` (None if unparseable).
    Accepts date/datetime objects and ISO-ish strings ('2020-01-15', with or without a
    'T'/space time part); cftime/num2date values stringify to this shape."""
    import datetime as _dt
    if val is None:
        return None
    if isinstance(val, _dt.datetime):
        return val.date()
    if isinstance(val, _dt.date):
        return val
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(val))
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _resolve_date(spec, attrs):
    """A ``time_start``/``time_end`` spec -> a ``datetime.date``. A ``{field}`` token
    resolves from the record ``attrs`` (so each record sets its own window); otherwise
    the spec is a literal date. None when blank/unresolved."""
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"\{(.+)\}", spec)
    if m:
        spec = str((attrs or {}).get(m.group(1).strip()) or "").strip()
    return _parse_date_loose(spec)


def _apply_time_range(result, cfg=None, attrs=None):
    """Filter a SERIES result's rows to the date window [time_start, time_end] (resolved
    per-record) when ``time_source == 'range'``. Mode-agnostic — keeps only timesteps
    whose x_dim (time) value parses to a date inside the window; an undecodable time
    axis is left untouched (graceful, never raises)."""
    cfg, attrs = cfg or {}, attrs or {}
    if not result or result.get("kind") != "series":
        return result
    if (cfg.get("time_source") or "").strip().lower() != "range":
        return result
    cols = result.get("columns") or []
    if not cols:
        return result
    start = _resolve_date(cfg.get("time_start"), attrs)
    end = _resolve_date(cfg.get("time_end"), attrs)
    if not start and not end:
        return result
    if start and end and start > end:           # tolerate an inverted window
        start, end = end, start
    tvals = cols[0].get("values") or []
    keep, any_parsed = [], False
    for tv in tvals:
        d = _parse_date_loose(tv)
        if d is None:
            keep.append(True)
            continue
        any_parsed = True
        keep.append((start is None or d >= start) and (end is None or d <= end))
    if not any_parsed or all(keep):
        return result
    new_cols = [{"name": c.get("name"),
                 "values": [val for k, val in zip(keep, c.get("values") or []) if k]}
                for c in cols]
    n = len(new_cols[0]["values"]) if new_cols else 0
    out = dict(result)
    out.update({"columns": new_cols, "n": n,
                "x": new_cols[0]["values"] if new_cols else [],
                "y": new_cols[1]["values"] if len(new_cols) > 1 else []})
    return out


def _netcdf_extract(ds, out_entry, cfg=None, attrs=None):
    """Extract ONE output from an open netCDF4 Dataset. A 'table' output (carrying a
    ``vars`` list) -> one multi-column series (x_dim + each variable); a 'series'
    output -> the variable along its x_dim (spatially filtered per the connector's
    spatial config); a 'value' output -> the last point of that series. ``cfg``/
    ``attrs`` carry the spatial filter (point/bbox) + the record's lon/lat."""
    out_entry = out_entry or {}
    multi = out_entry.get("vars")
    if multi:
        cols = _netcdf_multivar_columns(ds, multi, (out_entry.get("x_dim") or "").strip(),
                                        out_entry.get("derived"), cfg, attrs)
        if not cols:
            return None
        n = max((len(c["values"]) for c in cols), default=0)
        xs = cols[0]["values"] if cols else []
        ys = cols[1]["values"] if len(cols) > 1 else []
        return {"kind": "series", "columns": cols, "n": n, "x": xs, "y": ys}
    var_name = (out_entry.get("var") or out_entry.get("name") or "").strip()
    v = ds.variables.get(var_name)
    if v is None:
        return None
    dims = list(v.dimensions)
    x_dim = (out_entry.get("x_dim") or "").strip() or (dims[0] if dims else "")
    if x_dim not in dims:
        x_dim = dims[0] if dims else ""
    is_series = (out_entry.get("kind") or "value").lower() == "series"
    # SHAPEFILE filter (the two record-driven modes): 'mean' = the masked mean inside
    # the resolved shapefile (a series, or its last point for a value output); 'cells'
    # = every grid cell inside the shapefile as its own column (no aggregation). The
    # region comes from THIS record's uploaded Shapefile field (_shapefile) / geometry.
    if (cfg or {}).get("spatial") == "shapefile":
        agg = ((cfg or {}).get("shapefile_agg") or "mean").lower()
        lat_dim = (cfg or {}).get("lat_dim") or ""
        lon_dim = (cfg or {}).get("lon_dim") or ""
        if agg == "cells" and is_series:
            cols = _netcdf_shapefile_cells(ds, v, x_dim, cfg, attrs,
                                           int((cfg or {}).get("cells_max") or 200))
            if cols:
                n = max((len(c["values"]) for c in cols), default=0)
                return {"kind": "series", "columns": cols, "n": n,
                        "x": cols[0]["values"], "y": cols[1]["values"] if len(cols) > 1 else []}
            return {"kind": "series", "n": 0, "x": [], "y": [],
                    "columns": [{"name": x_dim or "index", "values": []}]}
        rings = _shapefile_union_rings(cfg, attrs)
        ys = (_netcdf_polygon_ys(ds, v, x_dim, lat_dim, lon_dim, rings)
              if (rings and lat_dim in dims and lon_dim in dims) else None)
        if is_series:
            if ys is None:
                return {"kind": "series", "n": 0, "x": [], "y": [],
                        "columns": [{"name": x_dim or "index", "values": []}]}
            cvar = ds.variables.get(x_dim)
            xs = _netcdf_coord_values(cvar) if cvar is not None else [str(i) for i in range(len(ys))]
            xs = xs[:len(ys)]
            return {"kind": "series",
                    "columns": [{"name": x_dim or "index", "values": xs},
                                {"name": var_name, "values": ys}],
                    "n": len(ys), "x": xs, "y": ys}
        return {"kind": "value", "value": (ys[-1] if ys else None)}
    # PER-CELL / PER-ZONE: a SERIES output becomes one column per grid cell / per polygon
    # (NOT the mean). When the columns can't be built (e.g. all zones fall outside the
    # grid, or no zones are configured) return a soft-EMPTY series rather than falling
    # through to the whole-grid mean — silently mislabelling a global mean as the
    # requested per-cell/zone output would be worse than showing no data.
    if is_series and (cfg or {}).get("spatial") in ("cells", "zones"):
        if (cfg or {}).get("spatial") == "cells":
            cols = _netcdf_cells_columns(ds, v, x_dim, cfg, attrs,
                                         int((cfg or {}).get("cells_max") or 24))
        else:
            cols = _netcdf_zones_columns(ds, v, x_dim, cfg, attrs,
                                         int((cfg or {}).get("zones_max") or 60))
        if cols:
            n = max((len(c["values"]) for c in cols), default=0)
            return {"kind": "series", "columns": cols, "n": n,
                    "x": cols[0]["values"], "y": cols[1]["values"] if len(cols) > 1 else []}
        return {"kind": "series", "n": 0, "x": [], "y": [],
                "columns": [{"name": x_dim or "index", "values": []}]}
    xs, ys = _netcdf_series_xy(ds, v, x_dim, cfg, attrs)
    if is_series:
        return {"kind": "series",
                "columns": [{"name": x_dim or "index", "values": xs},
                            {"name": var_name, "values": ys}],
                "n": len(ys), "x": xs, "y": ys}
    return {"kind": "value", "value": (ys[-1] if ys else None)}


def _resolve_thredds_url(cfg, attrs):
    """Resolve a THREDDS connector's OPeNDAP access URL via siphon: open the
    catalog_url, find the dataset whose name contains ``dataset`` (or the first),
    and return its OPeNDAP URL. '' on any failure."""
    try:
        from siphon.catalog import TDSCatalog
    except Exception:
        return ""
    try:
        cat_url = _render_template(cfg.get("catalog_url") or "", attrs)
        if not cat_url:
            return ""
        cat = TDSCatalog(cat_url)
        want = (cfg.get("dataset") or "").strip().lower()
        chosen = None
        names = list(cat.datasets)
        if want:
            for nm in names:
                if want in nm.lower():
                    chosen = cat.datasets[nm]
                    break
        if chosen is None and names:
            chosen = cat.datasets[names[0]]
        if chosen is None:
            return ""
        au = chosen.access_urls or {}
        for key in ("OPENDAP", "OpenDAP", "OPeNDAP", "dap", "DODS"):
            if au.get(key):
                return _strip_file_scheme(au[key])
        if au:
            return _strip_file_scheme(next(iter(au.values())))
        # Fallback: siphon yields no access_urls for some minimal/non-standard catalogs
        # -> build the OPeNDAP URL from the dataset's url_path + an OPENDAP service base
        # (recursing compound services).
        built = _thredds_opendap_from_services(cat, chosen)
        return _strip_file_scheme(built)
    except Exception:
        return ""


def _strip_file_scheme(url):
    """A ``file:///path`` URL -> the bare local path (netCDF4 opens a path, not file://)."""
    url = (url or "").strip()
    if url.startswith("file://"):
        url = url[len("file://"):]
    return url


def _thredds_opendap_from_services(cat, ds):
    """Build a THREDDS dataset's OPeNDAP URL from its ``url_path`` + an OPENDAP service
    base, recursing compound services. '' when no OPENDAP service / url_path is found."""
    url_path = getattr(ds, "url_path", None)
    if not url_path:
        return ""

    def _walk(services):
        for s in services or []:
            if (getattr(s, "service_type", "") or "").upper() == "OPENDAP" \
                    and getattr(s, "base", None) is not None:
                return s.base
            found = _walk(getattr(s, "services", None))
            if found is not None:
                return found
        return None

    base = _walk(getattr(cat, "services", None))
    return ((base or "") + url_path) if base is not None else ""


def _netcdf_describe(cfg, attrs):
    """For the Test panel: open the dataset and return a {variables, dimensions}
    summary (variable -> its dims/shape/units) so the user can see what to put in
    the Variable field. None on any failure (the panel shows the error status)."""
    import socket
    cfg = cfg or {}
    if cfg.get("inputs"):
        rattrs, _ = _resolve_inputs(cfg, attrs, None)
    else:
        rattrs = dict(attrs or {})
    if (cfg.get("kind") or "netcdf").lower() == "thredds":
        url = _resolve_thredds_url(cfg, rattrs)
    else:
        url = _render_template(cfg.get("dataset_url") or "", rattrs)
    if not url:
        return None
    timeout = int(cfg.get("timeout") or 30)
    old_to = socket.getdefaulttimeout()
    try:
        import netCDF4
        socket.setdefaulttimeout(timeout)
        ds = netCDF4.Dataset(url)
        try:
            dims = {k: len(v) for k, v in ds.dimensions.items()}
            variables = {}
            for vn, v in ds.variables.items():
                variables[vn] = {
                    "dimensions": list(v.dimensions),
                    "shape": [int(s) for s in v.shape],
                    "units": getattr(v, "units", ""),
                    "long_name": getattr(v, "long_name", ""),
                }
            return {"resolved_url": url, "dimensions": dims, "variables": variables}
        finally:
            ds.close()
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old_to)


def _netcdf_soft_empty(out_kind, url, cached):
    """A no-data result of the requested kind (no dataset access needed)."""
    if (out_kind or "value") == "series":
        return {"kind": "series", "columns": [], "n": 0, "x": [], "y": [],
                "url": url, "cached": cached}
    return {"kind": "value", "value": None, "url": url, "cached": cached}


def _fetch_netcdf(cfg, record_attrs, output=None, field_map=None,
                  connector_name="connector"):
    """Fetch one output from a NetCDF/THREDDS connector. Resolves the dataset URL
    (THREDDS catalog via siphon, else the dataset_url with {field} substitution),
    opens it via netCDF4 (socket timeout), and extracts the selected output. Caches
    per (name, url, output) like the REST path; never raises."""
    import socket
    cfg = cfg or {}
    kind = (cfg.get("kind") or "netcdf").lower()
    # resolve inputs for {field} substitution (declared inputs[] or raw attrs)
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs, missing_required = dict(record_attrs or {}), []

    # which output (synthesized series/value from the variable)
    catalog = _connector_outputs(cfg)
    if isinstance(output, dict):
        out_entry = output
    else:
        out_entry = _find_output(catalog, output) if output else _primary_output(catalog)
    out_kind = (out_entry.get("kind") if out_entry else "value")

    if missing_required:
        return _netcdf_soft_empty(out_kind, "", False)

    if kind == "thredds":
        url = _resolve_thredds_url(cfg, attrs)
    else:
        url = _render_template(cfg.get("dataset_url") or "", attrs)
    if not url or out_entry is None:
        return _netcdf_soft_empty(out_kind, url, False)

    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or 30)
    # Include the output's variable signature so the same output NAME (notably the
    # combined 'table') doesn't return a stale result when the connector's variable
    # list changes (the name alone would collide across different variable sets).
    _sig = ",".join((out_entry.get("vars") or [out_entry.get("var") or ""]))
    _sig += "|" + ";".join((d.get("name", "") + "=" + d.get("formula", ""))
                           for d in (out_entry.get("derived") or []))
    # Spatial filter signature: the same url+var resolves to a different series per
    # location/region, so the spatial mode + its RESOLVED parameters must key the
    # cache (a dynamic bbox uses the record's lon/lat, so two records mustn't collide).
    _sp = (cfg.get("spatial") or "mean").lower()
    _rlon = _wms_num(cfg.get("lon"), attrs.get("_lon"), attrs.get("longitude"), attrs.get("lon"))
    _rlat = _wms_num(cfg.get("lat"), attrs.get("_lat"), attrs.get("latitude"), attrs.get("lat"))
    if _sp == "point":
        _sig += "|pt:%s,%s" % (_rlon, _rlat)
    elif _sp == "bbox":
        _sig += "|bx:%s,%s,%s,%s|loc:%s,%s" % (
            cfg.get("lon_min"), cfg.get("lon_max"), cfg.get("lat_min"), cfg.get("lat_max"),
            _rlon, _rlat)
    elif _sp == "polygon":
        _sig += "|pg:%s|%s" % ((cfg.get("polygon") or "")[:120], cfg.get("shapefile") or "")
    elif _sp == "cells":
        _sig += "|cells:%s,%s,%s,%s|loc:%s,%s|%s" % (
            cfg.get("lon_min"), cfg.get("lon_max"), cfg.get("lat_min"), cfg.get("lat_max"),
            _rlon, _rlat, cfg.get("cells_max"))
    elif _sp == "zones":
        import hashlib
        # DYNAMIC zones key on the RECORD's geometry (each record differs); fixed zones
        # key on the connector's zones source. Either way fold a digest in so distinct
        # geometries never collide (the recurring per-record cache-collision trap).
        if (cfg.get("zones_source") or "").strip().lower() == "record":
            _zsrc = "rec:" + str(attrs.get("_geojson") or "")
        else:
            _zsrc = (cfg.get("zones") or cfg.get("polygon") or cfg.get("shapefile") or "")
        _zh = hashlib.md5(_zsrc.encode("utf-8", "replace")).hexdigest()[:12]
        _sig += "|zones:%s,%s,%s" % (_zh, cfg.get("zone_label") or "", cfg.get("zones_max"))
    elif _sp == "shapefile":
        import hashlib
        # The region is per-record (the uploaded shapefile / geometry), so key on a
        # digest of the RESOLVED shapefile source + the aggregation mode.
        _ssrc = str(attrs.get("_shapefile") or attrs.get("_geojson")
                    or cfg.get("zones") or cfg.get("polygon") or cfg.get("shapefile") or "")
        _sh = hashlib.md5(_ssrc.encode("utf-8", "replace")).hexdigest()[:12]
        _sig += "|shp:%s,%s,%s" % (_sh, (cfg.get("shapefile_agg") or "mean"), cfg.get("cells_max"))
    else:
        _sig += "|mean"
    # A per-record DATE RANGE subsets the time axis -> two records differ; fold the
    # resolved window into the key so they don't share a cached series.
    if (cfg.get("time_source") or "").strip().lower() == "range":
        _sig += "|t:%s,%s" % (_resolve_date(cfg.get("time_start"), attrs),
                              _resolve_date(cfg.get("time_end"), attrs))
    cache_key = (connector_name,
                 url + "::" + (out_entry.get("name") or "") + "::" + _sig)
    now = time.time()
    hit = _API_CACHE.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        res = dict(hit[1]); res["url"] = url; res["cached"] = True
        return res

    import netCDF4
    old_to = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        ds = netCDF4.Dataset(_strip_file_scheme(url))   # file:///path -> local path
        try:
            extracted = _netcdf_extract(ds, out_entry, cfg, attrs)
            extracted = _apply_time_range(extracted, cfg, attrs)   # per-record date window
        finally:
            ds.close()
    except Exception:
        return _netcdf_soft_empty(out_kind, url, False)
    finally:
        socket.setdefaulttimeout(old_to)

    if extracted is None:
        return _netcdf_soft_empty(out_kind, url, False)
    _API_CACHE[cache_key] = (now, extracted)
    res = dict(extracted); res["url"] = url; res["cached"] = False
    return res


# --- CSV connector (a tabular file -> one multi-column series + a latest value) ---
def _csv_coerce(raw):
    """Parse a CSV cell: numeric strings -> int/float (nicer table + sparkline),
    blanks -> None, everything else kept as the trimmed string."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if (f.is_integer() and abs(f) < 1e15) else round(f, 6)


def _read_csv_columns(url, delimiter=",", has_header=True, timeout=15, max_rows=5000):
    """Fetch (http/https) or open (local path) a CSV; return (columns, n_rows,
    truncated) where columns=[{name, values}] aligned by row. Reads at most
    ``max_rows`` data rows so a huge file can't exhaust memory; ``truncated`` is True
    when more rows existed than were read. Decoded utf-8-SIG so an Excel BOM never
    sticks to the first header. Numeric cells are coerced to numbers."""
    import csv as _csvmod
    import io
    if re.match(r"^https?://", url, re.I):
        req = urllib.request.Request(url, headers={"User-Agent": "HydroDesk/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8-sig", "replace")
    else:
        with open(url, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            text = fh.read()
    reader = _csvmod.reader(io.StringIO(text), delimiter=(delimiter or ","),
                            skipinitialspace=True)
    rows = []
    limit = max_rows + (1 if has_header else 0)
    truncated = False
    for row in reader:
        if len(rows) >= limit:
            truncated = True   # at least one more row exists past the cap
            break
        rows.append(row)
    if not rows:
        return [], 0, False
    if has_header:
        names = [(str(h).strip() or ("col%d" % j)) for j, h in enumerate(rows[0])]
        data = rows[1:]
    else:
        ncols0 = max((len(r) for r in rows), default=0)
        names = ["col%d" % j for j in range(ncols0)]
        data = rows
    cols = [{"name": nm, "values": []} for nm in names]
    for r in data:
        for j in range(len(names)):
            cols[j]["values"].append(_csv_coerce(r[j] if j < len(r) else None))
    return cols, len(data), truncated


def _fetch_csv(cfg, record_attrs, output=None, field_map=None,
               connector_name="connector"):
    """Fetch one output from a CSV connector. The whole table is ONE series (each
    column a variable); a 'value' output returns the latest non-null of the chosen
    value column (else the last column). {field} tokens in csv_url are filled from
    the record. Caches the parsed table per (name, url). Never raises."""
    cfg = cfg or {}
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs, missing_required = dict(record_attrs or {}), []
    catalog = _connector_outputs(cfg)
    out_entry = (output if isinstance(output, dict)
                 else (_find_output(catalog, output) if output
                       else _primary_output(catalog)))
    out_kind = (out_entry.get("kind") if out_entry else "series")

    def _empty(url, cached):
        if out_kind == "value":
            return {"kind": "value", "value": None, "url": url, "cached": cached}
        return {"kind": "series", "columns": [], "n": 0, "x": [], "y": [],
                "url": url, "cached": cached}

    if missing_required:
        return _empty("", False)
    url = _render_template(cfg.get("csv_url") or "", attrs)
    if not url or out_entry is None:
        return _empty(url, False)

    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or 15)
    cache_key = (connector_name, "csv::" + url)
    now = time.time()
    hit = _API_CACHE.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        cols, truncated, cached = hit[1][0], hit[1][1], True
    else:
        try:
            cols, _n, truncated = _read_csv_columns(
                url, delimiter=cfg.get("delimiter") or ",",
                has_header=cfg.get("has_header", True), timeout=timeout)
        except Exception:
            return _empty(url, False)
        _API_CACHE[cache_key] = (now, (cols, truncated))
        cached = False

    if (out_entry.get("kind") or "series").lower() == "value":
        vcol = (out_entry.get("value_column") or "").strip()
        col = next((c for c in cols if c["name"] == vcol), None) if vcol else None
        if col is None and cols:
            col = cols[-1]
        vals = (col or {}).get("values") or []
        latest = next((v for v in reversed(vals) if v is not None), None)
        return {"kind": "value", "value": latest, "url": url, "cached": cached}

    n = max((len(c["values"]) for c in cols), default=0)
    xs = cols[0]["values"] if cols else []
    yi = next((i for i, c in enumerate(cols)
               if (c["name"] or "").lower() in ("value", "y")),
              1 if len(cols) > 1 else 0)
    ys = cols[yi]["values"] if cols else []
    return {"kind": "series", "columns": cols, "n": n, "x": xs, "y": ys,
            "url": url, "cached": cached, "truncated": truncated}


def _csv_describe(cfg, attrs):
    """Test panel: resolve the CSV and return its columns + a small preview."""
    try:
        if cfg.get("inputs"):
            rattrs, _ = _resolve_inputs(cfg, attrs, None)
        else:
            rattrs = dict(attrs or {})
        url = _render_template(cfg.get("csv_url") or "", rattrs)
        if not url:
            return None
        cols, n, truncated = _read_csv_columns(
            url, delimiter=cfg.get("delimiter") or ",",
            has_header=cfg.get("has_header", True),
            timeout=int(cfg.get("timeout") or 15), max_rows=50)
        return {"resolved_url": url, "columns": [c["name"] for c in cols],
                "rows_in_preview": n, "more_rows_exist": truncated,
                "preview": {c["name"]: (c["values"][:5]) for c in cols}}
    except Exception:
        return None


# --- WMS connector (a map service -> a GetMap image at the record's point) ---
def _wms_num(*candidates):
    """First candidate that parses as a float, else None."""
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            return float(c)
        except (TypeError, ValueError):
            continue
    return None


def _wms_point(cfg, attrs):
    """The map centre lon/lat: resolved inputs / geom-injected _lon/_lat / a
    longitude|latitude field / a config default; (None, None) when unknown."""
    lon = _wms_num(attrs.get("lon"), attrs.get("_lon"),
                   attrs.get("longitude"), cfg.get("default_lon"))
    lat = _wms_num(attrs.get("lat"), attrs.get("_lat"),
                   attrs.get("latitude"), cfg.get("default_lat"))
    return lon, lat


def _wms_getmap_url(cfg, attrs):
    """Build a deterministic WMS GetMap URL centred on the record's point (point ±
    bbox_buffer degrees). Honors the 1.3.0 vs 1.1.1 axis-order + CRS/SRS param
    difference. Returns (url, (width,height), (lon,lat)); ('', None, None) when the
    service URL is missing."""
    base = _render_template(cfg.get("wms_url") or "", attrs)
    if not base:
        return "", None, None
    version = (cfg.get("wms_version") or "1.3.0").strip()
    layers = (cfg.get("layers") or "").strip()
    fmt = (cfg.get("image_format") or "image/png").strip()
    styles = (cfg.get("styles") or "").strip()
    crs = (cfg.get("crs") or "EPSG:4326").strip()
    buf = _wms_num(cfg.get("bbox_buffer")) or 0.5
    try:
        width, height = int(cfg.get("width") or 512), int(cfg.get("height") or 384)
    except (TypeError, ValueError):
        width, height = 512, 384
    lon, lat = _wms_point(cfg, attrs)
    if lon is None or lat is None:
        minx, miny, maxx, maxy = -180.0, -90.0, 180.0, 90.0
    else:
        minx, miny, maxx, maxy = lon - buf, lat - buf, lon + buf, lat + buf
    if version.startswith("1.3"):
        crs_param = "CRS"
        # EPSG:4326 in WMS 1.3.0 uses lat,lon (y,x) BBOX axis order.
        bbox = ("%s,%s,%s,%s" % (miny, minx, maxy, maxx)
                if crs.upper() == "EPSG:4326"
                else "%s,%s,%s,%s" % (minx, miny, maxx, maxy))
    else:
        crs_param = "SRS"
        bbox = "%s,%s,%s,%s" % (minx, miny, maxx, maxy)
    params = [
        ("service", "WMS"), ("request", "GetMap"), ("version", version),
        ("layers", layers), ("styles", styles), (crs_param, crs),
        ("bbox", bbox), ("width", str(width)), ("height", str(height)),
        ("format", fmt), ("transparent", "TRUE"),
    ]
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params), (width, height), (lon, lat)


def _fetch_wms(cfg, record_attrs, output=None, field_map=None,
               connector_name="connector"):
    """Fetch one output from a WMS connector. 'image' (primary) builds a GetMap URL
    centred on the record's point — NO server fetch, the <img> loads it client-side.
    'value' does a best-effort GetFeatureInfo at the centre pixel and pulls the first
    numeric property. Never raises."""
    cfg = cfg or {}
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs, missing_required = dict(record_attrs or {}), []
    catalog = _connector_outputs(cfg)
    out_entry = (output if isinstance(output, dict)
                 else (_find_output(catalog, output) if output
                       else _primary_output(catalog)))
    out_kind = (out_entry.get("kind") if out_entry else "image")

    getmap, dims, _pt = _wms_getmap_url(cfg, attrs)
    if missing_required or not getmap or out_entry is None:
        if out_kind == "value":
            return {"kind": "value", "value": None, "url": getmap, "cached": False}
        return {"kind": "image", "url": "", "cached": False}

    if (out_entry.get("kind") or "image").lower() != "value":
        return {"kind": "image", "url": getmap, "cached": False}

    # GetFeatureInfo (value): query the centre pixel, parse JSON best-effort.
    width, height = dims or (512, 384)
    version = (cfg.get("wms_version") or "1.3.0").strip()
    fi = getmap.replace("request=GetMap", "request=GetFeatureInfo")
    extra = [("query_layers", (cfg.get("layers") or "").strip()),
             ("info_format", "application/json")]
    extra += ([("i", str(width // 2)), ("j", str(height // 2))]
              if version.startswith("1.3")
              else [("x", str(width // 2)), ("y", str(height // 2))])
    fi_url = fi + "&" + urllib.parse.urlencode(extra)
    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or 20)
    cache_key = (connector_name, "wmsfi::" + fi_url)
    now = time.time()
    hit = _API_CACHE.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        return {"kind": "value", "value": hit[1], "url": getmap, "cached": True}
    val = None
    try:
        req = urllib.request.Request(fi_url, headers={"User-Agent": "HydroDesk/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
        feats = (payload or {}).get("features") or []
        if feats:
            props = (feats[0] or {}).get("properties") or {}
            for v in props.values():
                try:
                    val = float(v)
                    break
                except (TypeError, ValueError):
                    if val is None and v not in (None, ""):
                        val = v   # fall back to the first non-empty property
    except Exception:
        val = None
    _API_CACHE[cache_key] = (now, val)
    return {"kind": "value", "value": val, "url": getmap, "cached": False}


def _wms_describe(cfg, attrs):
    """Test panel: the sample GetMap URL + (best-effort) the service's layers via
    owslib GetCapabilities, so the user can pick a layer and see its WGS84 bbox."""
    out = {}
    try:
        if cfg.get("inputs"):
            rattrs, _ = _resolve_inputs(cfg, attrs, None)
        else:
            rattrs = dict(attrs or {})
        getmap, _dims, pt = _wms_getmap_url(cfg, rattrs)
        out["sample_getmap_url"] = getmap
        if pt and pt[0] is not None:
            out["centre"] = {"lon": pt[0], "lat": pt[1]}
    except Exception:
        pass
    try:
        from owslib.wms import WebMapService
        base = _render_template(cfg.get("wms_url") or "", attrs or {})
        wms = WebMapService(base, version=(cfg.get("wms_version") or "1.3.0"))
        try:
            out["service_title"] = wms.identification.title
        except Exception:
            pass
        layers = []
        for nm in list(wms.contents)[:80]:
            ly = wms[nm]
            layers.append({"name": nm, "title": getattr(ly, "title", ""),
                           "bbox_wgs84": list(getattr(ly, "boundingBoxWGS84", []) or [])})
        out["layers"] = layers
    except Exception as exc:
        out["capabilities_error"] = str(exc)[:200]
    return out or None


# --- WCS connector (a coverage service -> the value at the record's point) ---
def _wcs_getcoverage_url(cfg, attrs):
    """Build a WCS 2.0.1 GetCoverage URL for a Lat/Long subset, requesting NetCDF (read
    locally via netCDF4 — no raster lib needed). The spatial subset is the per-record
    SHAPEFILE's bounding box when one is mapped (spatial='shapefile' / _shapefile),
    otherwise the record's point ± buffer; a per-record DATE RANGE (time_source='range')
    becomes a time subset on the time axis. Returns (url, (lon,lat)); ('', (lon,lat))
    when the region or service is missing. Axis labels default to Lat/Long (configurable)."""
    base = _render_template(cfg.get("wcs_url") or "", attrs)
    cov = (cfg.get("coverage") or "").strip()
    version = (cfg.get("wcs_version") or "2.0.1").strip()
    fmt = (cfg.get("wcs_format") or "application/netcdf").strip()
    lon_axis = (cfg.get("lon_axis") or "Long").strip()
    lat_axis = (cfg.get("lat_axis") or "Lat").strip()
    rings = (_shapefile_union_rings(cfg, attrs)
             if ((cfg.get("spatial") or "").lower() == "shapefile" or attrs.get("_shapefile"))
             else None)
    if rings:                                   # the shapefile's bounding box
        allx = [p[0] for r in rings for p in r]
        ally = [p[1] for r in rings for p in r]
        lon_lo, lon_hi, lat_lo, lat_hi = min(allx), max(allx), min(ally), max(ally)
        lon, lat = (lon_lo + lon_hi) / 2.0, (lat_lo + lat_hi) / 2.0
    else:                                       # the record's point ± buffer (legacy)
        lon, lat = _wms_point(cfg, attrs)
        buf = _wms_num(cfg.get("bbox_buffer")) or 0.25
        if lon is not None and lat is not None:
            lon_lo, lon_hi, lat_lo, lat_hi = lon - buf, lon + buf, lat - buf, lat + buf
        else:
            lon_lo = lon_hi = lat_lo = lat_hi = None
    if not base or not cov or lon is None or lat is None or lon_lo is None:
        return "", (lon, lat)
    params = [
        ("service", "WCS"), ("version", version), ("request", "GetCoverage"),
        ("coverageId", cov),
        ("subset", "%s(%s,%s)" % (lat_axis, lat_lo, lat_hi)),
        ("subset", "%s(%s,%s)" % (lon_axis, lon_lo, lon_hi)),
        ("format", fmt),
    ]
    start = _resolve_date(cfg.get("time_start"), attrs)
    end = _resolve_date(cfg.get("time_end"), attrs)
    if (cfg.get("time_source") or "").lower() == "range" and (start or end):
        tax = (cfg.get("time_axis") or "ansi").strip()
        if start and end and start > end:
            start, end = end, start
        params.append(("subset", '%s("%s","%s")' % (tax, start or end, end or start)))
    else:
        extra = (cfg.get("extra_subset") or "").strip()
        if extra:  # e.g. ansi("2014-07-01") for a coverage with a time axis
            params.append(("subset", extra))
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params), (lon, lat)


def _wcs_extract(ds, out_entry):
    """Extract one output from an open (in-memory) netCDF Dataset of a WCS coverage:
    'value' -> the masked spatial MEAN of the data variable over the whole subset;
    'series' -> that variable along a time-like dim (other dims averaged)."""
    import numpy as np
    var_name = (out_entry.get("var") or "").strip()
    v = ds.variables.get(var_name) if var_name else None
    if v is None:  # pick the first data variable (>=1 dim, not a coordinate)
        for vn, vv in ds.variables.items():
            if vn not in ds.dimensions and len(vv.dimensions) >= 1:
                v, var_name = vv, vn
                break
    if v is None:
        return None
    dims = list(v.dimensions)
    kind = (out_entry.get("kind") or "value").lower()
    time_dim = next((d for d in dims
                     if d.lower() in ("time", "ansi", "t", "date", "unix")), None)
    if kind == "series" and time_dim:
        xs, ys = _netcdf_series_xy(ds, v, time_dim)
        return {"kind": "series",
                "columns": [{"name": time_dim, "values": xs},
                            {"name": var_name, "values": ys}],
                "n": len(ys), "x": xs, "y": ys}
    arr = np.ma.asarray(v[:])
    m = float(np.ma.mean(arr)) if arr.size else None
    if m is None or m != m:   # empty or NaN
        return {"kind": "value", "value": None}
    return {"kind": "value", "value": round(m, 4)}


def _fetch_wcs(cfg, record_attrs, output=None, field_map=None,
               connector_name="connector"):
    """Fetch one output from a WCS connector: GetCoverage a small NetCDF subset
    around the record's point, read it in-memory with netCDF4, and return the
    coverage value (spatial mean) or a time series. Caches per (name, url). Never
    raises — degrades to a soft-empty of the requested kind."""
    cfg = cfg or {}
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs, missing_required = dict(record_attrs or {}), []
    catalog = _connector_outputs(cfg)
    out_entry = (output if isinstance(output, dict)
                 else (_find_output(catalog, output) if output
                       else _primary_output(catalog)))
    out_kind = (out_entry.get("kind") if out_entry else "value")

    def _empty(url, cached):
        if out_kind == "series":
            return {"kind": "series", "columns": [], "n": 0, "x": [], "y": [],
                    "url": url, "cached": cached}
        return {"kind": "value", "value": None, "url": url, "cached": cached}

    url, _pt = _wcs_getcoverage_url(cfg, attrs)
    if missing_required or not url or out_entry is None:
        return _empty(url, False)

    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or 30)
    cache_key = (connector_name, "wcs::" + url + "::" + (out_entry.get("name") or ""))
    now = time.time()
    hit = _API_CACHE.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        res = dict(hit[1]); res["url"] = url; res["cached"] = True
        return res
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HydroDesk/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception:
        return _empty(url, False)
    try:
        import netCDF4
        ds = netCDF4.Dataset("inmem.nc", mode="r", memory=data)
        try:
            extracted = _wcs_extract(ds, out_entry)
        finally:
            ds.close()
    except Exception:
        return _empty(url, False)
    if extracted is None:
        return _empty(url, False)
    _API_CACHE[cache_key] = (now, extracted)
    res = dict(extracted); res["url"] = url; res["cached"] = False
    return res


def _wcs_describe(cfg, attrs):
    """Test panel: the sample GetCoverage URL + (best-effort) the service's coverage
    IDs via owslib GetCapabilities, so the user can pick a coverage."""
    out = {}
    try:
        if cfg.get("inputs"):
            rattrs, _ = _resolve_inputs(cfg, attrs, None)
        else:
            rattrs = dict(attrs or {})
        url, pt = _wcs_getcoverage_url(cfg, rattrs)
        out["sample_getcoverage_url"] = url
        if pt and pt[0] is not None:
            out["centre"] = {"lon": pt[0], "lat": pt[1]}
    except Exception:
        pass
    try:
        from owslib.wcs import WebCoverageService
        base = _render_template(cfg.get("wcs_url") or "", attrs or {})
        wcs = WebCoverageService(base, version=(cfg.get("wcs_version") or "2.0.1"))
        try:
            out["service_title"] = wcs.identification.title
        except Exception:
            pass
        out["coverages"] = list(wcs.contents)[:120]
    except Exception as exc:
        out["capabilities_error"] = str(exc)[:200]
    return out or None


# --- Google Earth Engine connector (sample an asset at the record's point) ---
def _gee_init(cfg):
    """Initialise Earth Engine from the connector's service-account credential
    (stored in the secure HydroCredential store as the key JSON). Returns the ``ee``
    module on success, or (None, reason) — NEVER raises. The 'ee' package and a
    credential must both be present; otherwise the connector degrades to no-data."""
    try:
        import ee
    except Exception:
        return None, "the earthengine-api package is not installed"
    cred_name = (cfg.get("gee_credential") or "").strip()
    if not cred_name:
        return None, "no service-account credential is set"
    key = None
    try:
        with Session(App.get_persistent_store_database("hydro_db")) as session:
            key = _resolve_secret(session, cred_name)
    except Exception:
        key = None
    if not key:
        return None, "the service-account credential could not be resolved"
    try:
        info = json.loads(key)
        email = info.get("client_email") or (cfg.get("gee_service_account") or "")
        project = (cfg.get("gee_project") or info.get("project_id") or "").strip()
        creds = ee.ServiceAccountCredentials(email, key_data=key)
        ee.Initialize(creds, project=project or None)
        return ee, ""
    except Exception as exc:
        return None, "Earth Engine init failed: %s" % (str(exc)[:140])


_GEE_DEMO_NOTE = ("synthetic preview — Earth Engine isn't configured; install "
                  "earthengine-api + add a service-account credential for live data")


def _gee_synthetic(cfg, attrs, out_entry):
    """A DETERMINISTIC synthetic Earth-Engine result for the demo/preview mode: an
    NDVI-like value (0..1) that depends on the record's REGION centroid (its mapped
    shapefile) and DATE window, so different records visibly differ — without a live
    Earth Engine. Returns the same value/series shape a real fetch would, plus a note."""
    import math
    import datetime as _dt
    rings = _shapefile_union_rings(cfg, attrs)
    if rings:
        allx = [p[0] for r in rings for p in r]
        ally = [p[1] for r in rings for p in r]
        lon, lat = (min(allx) + max(allx)) / 2.0, (min(ally) + max(ally)) / 2.0
    else:
        lon, lat = _wms_point(cfg, attrs)
    if lon is None or lat is None:
        lon, lat = 0.0, 0.0

    def _ndvi(doy):
        base = 0.55 - 0.012 * abs(lat - 35.0) + 0.05 * math.sin((lon + 120.0) / 20.0)
        return round(max(0.0, min(1.0, base + 0.18 * math.sin(2 * math.pi * doy / 365.0))), 4)

    band = (cfg.get("gee_band") or "value").strip() or "value"
    start = _resolve_date(cfg.get("time_start"), attrs)
    end = _resolve_date(cfg.get("time_end"), attrs)
    if (out_entry.get("kind") or "value").lower() == "series" and start and end:
        if start > end:
            start, end = end, start
        step = max(1, (end - start).days // 12)
        xs, ys, d = [], [], start
        while d <= end:
            xs.append(d.isoformat())
            ys.append(_ndvi(d.timetuple().tm_yday))
            d = d + _dt.timedelta(days=step)
        return {"kind": "series", "columns": [{"name": "time", "values": xs},
                                              {"name": band, "values": ys}],
                "n": len(ys), "x": xs, "y": ys, "url": "", "cached": False,
                "note": _GEE_DEMO_NOTE}
    doy = (start or end).timetuple().tm_yday if (start or end) else 180
    return {"kind": "value", "value": _ndvi(doy), "url": "", "cached": False,
            "note": _GEE_DEMO_NOTE}


def _gee_geometry(ee, cfg, attrs):
    """An ``ee.Geometry`` for the record: its mapped SHAPEFILE as a MultiPolygon when
    one is present (region reduction), otherwise its point. Returns (geom, is_region);
    (None, False) when neither resolves."""
    if (cfg.get("spatial") or "").lower() == "shapefile" or (attrs or {}).get("_shapefile"):
        zones = _shapefile_zones(cfg, attrs)
        if zones:
            mp = [[[[float(p[0]), float(p[1])] for p in ring] for ring in zrings]
                  for _label, zrings in zones]
            try:
                return ee.Geometry.MultiPolygon(mp), True
            except Exception:
                pass
    lon, lat = _wms_point(cfg, attrs)
    if lon is not None and lat is not None:
        return ee.Geometry.Point([lon, lat]), False
    return None, False


def _fetch_gee(cfg, record_attrs, output=None, field_map=None,
               connector_name="connector"):
    """Fetch one output from an Earth Engine connector. 'value' reduces an Image over
    the record's REGION (its mapped shapefile) or point; 'series' reduces an
    ImageCollection over a date range across that region/point. The date range and the
    region come from the per-record mapping (x-nc-map) when set, else the connector's
    gee_start/gee_end + point. Requires the 'ee' package + a service-account credential;
    without them it returns a soft-empty (NEVER raises). Caches per (name, asset, region,
    dates, output)."""
    cfg = cfg or {}
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs, missing_required = dict(record_attrs or {}), []
    catalog = _connector_outputs(cfg)
    out_entry = (output if isinstance(output, dict)
                 else (_find_output(catalog, output) if output
                       else _primary_output(catalog)))
    out_kind = (out_entry.get("kind") if out_entry else "value")

    def _empty(cached):
        if out_kind == "series":
            return {"kind": "series", "columns": [], "n": 0, "x": [], "y": [],
                    "url": "", "cached": cached}
        return {"kind": "value", "value": None, "url": "", "cached": cached}

    lon, lat = _wms_point(cfg, attrs)
    asset = (cfg.get("gee_asset") or "").strip()
    # A per-record shapefile gives a REGION even when there's no point.
    _have_region = ((cfg.get("spatial") or "").lower() == "shapefile" or attrs.get("_shapefile"))
    if missing_required or out_entry is None or not asset or (lon is None and not _have_region):
        return _empty(False)

    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    scale = int(cfg.get("gee_scale") or 30)
    band = (cfg.get("gee_band") or "").strip()
    # The date window + region are per-record, so key the cache on them.
    _gstart = _resolve_date(cfg.get("time_start"), attrs) or (cfg.get("gee_start") or "").strip()
    _gend = _resolve_date(cfg.get("time_end"), attrs) or (cfg.get("gee_end") or "").strip()
    import hashlib as _hl
    _rgn = _hl.md5(str(attrs.get("_shapefile") or "%s,%s" % (lon, lat)).encode("utf-8", "replace")).hexdigest()[:12]
    cache_key = (connector_name, "gee::%s::%s::%s,%s::%s::%s"
                 % (asset, _rgn, _gstart, _gend, out_entry.get("name") or "", band))
    now = time.time()
    hit = _API_CACHE.get(cache_key)
    if hit and (now - hit[0]) < ttl:
        res = dict(hit[1]); res["cached"] = True
        return res

    ee, reason = _gee_init(cfg)
    if ee is None:
        # SYNTHETIC PREVIEW (opt-in): when Earth Engine isn't available, a connector
        # with gee_demo=true returns deterministic synthetic data that still varies by
        # the record's REGION + DATE window — so the per-record mapping is demonstrable
        # before real earthengine-api + a service-account credential are configured.
        if cfg.get("gee_demo"):
            res = _gee_synthetic(cfg, attrs, out_entry)
            _API_CACHE[cache_key] = (now, res)
            return dict(res)
        res = _empty(False); res["note"] = reason
        return res
    try:
        geom, is_region = _gee_geometry(ee, cfg, attrs)
        if geom is None:
            return _empty(False)
        start, end = str(_gstart or ""), str(_gend or "")
        if (out_entry.get("kind") or "value").lower() == "series":
            col = ee.ImageCollection(asset).filterBounds(geom)
            if start and end:
                col = col.filterDate(start, end)
            if band:
                col = col.select(band)
            if is_region:
                # A region series = the per-image MEAN over the polygon.
                reducer = (cfg.get("gee_reducer") or "mean").strip().lower()
                red = {"mean": ee.Reducer.mean, "median": ee.Reducer.median,
                       "max": ee.Reducer.max, "min": ee.Reducer.min}.get(reducer, ee.Reducer.mean)()

                def _img_mean(img):
                    d = img.reduceRegion(red, geom, scale)
                    return ee.Feature(None, {"time": img.date().millis(),
                                             "value": d.values().get(0)})
                feats = (col.map(_img_mean).getInfo() or {}).get("features", [])
                import datetime as _dt
                xs, ys = [], []
                for ft in feats:
                    pr = (ft or {}).get("properties") or {}
                    t = pr.get("time")
                    xs.append(_dt.datetime.utcfromtimestamp(t / 1000.0).isoformat()
                              if isinstance(t, (int, float)) else str(t))
                    ys.append(pr.get("value"))
                extracted = {"kind": "series",
                             "columns": [{"name": "time", "values": xs},
                                         {"name": band or "value", "values": ys}],
                             "n": len(ys), "x": xs, "y": ys}
                _API_CACHE[cache_key] = (now, extracted)
                res = dict(extracted); res["cached"] = False
                return res
            rows = col.getRegion(geom, scale).getInfo() or []
            if len(rows) < 2:
                extracted = {"kind": "series", "columns": [], "n": 0, "x": [], "y": []}
            else:
                header = rows[0]
                ti = header.index("time") if "time" in header else 3
                vi = (header.index(band) if band in header else len(header) - 1)
                import datetime as _dt
                xs, ys = [], []
                for r in rows[1:]:
                    t = r[ti]
                    xs.append(_dt.datetime.utcfromtimestamp(t / 1000.0).isoformat()
                              if isinstance(t, (int, float)) else str(t))
                    ys.append(r[vi])
                extracted = {"kind": "series",
                             "columns": [{"name": "time", "values": xs},
                                         {"name": band or "value", "values": ys}],
                             "n": len(ys), "x": xs, "y": ys}
        else:
            img = ee.Image(asset)
            if band:
                img = img.select(band)
            # Over a polygon the default is a MEAN; a point keeps 'first' (the pixel).
            reducer = (cfg.get("gee_reducer") or ("mean" if is_region else "first")).strip().lower()
            red = {"mean": ee.Reducer.mean, "median": ee.Reducer.median,
                   "first": ee.Reducer.first, "max": ee.Reducer.max,
                   "min": ee.Reducer.min}.get(reducer, ee.Reducer.first)()
            d = img.reduceRegion(red, geom, scale).getInfo() or {}
            val = None
            if band and band in d:
                val = d[band]
            elif d:
                val = next(iter(d.values()))
            extracted = {"kind": "value", "value": val}
    except Exception as exc:
        res = _empty(False); res["note"] = "Earth Engine query failed: %s" % (str(exc)[:140])
        return res
    _API_CACHE[cache_key] = (now, extracted)
    res = dict(extracted); res["cached"] = False
    return res


def _gee_describe(cfg, attrs):
    """Test panel: report the configured asset/point and whether Earth Engine is
    usable (package present + credential resolves), without running a query."""
    rattrs = dict(attrs or {})
    if cfg.get("inputs"):
        try:
            rattrs, _ = _resolve_inputs(cfg, attrs, None)
        except Exception:
            pass
    lon, lat = _wms_point(cfg, rattrs)
    out = {"asset": (cfg.get("gee_asset") or "").strip(),
           "band": (cfg.get("gee_band") or "").strip(),
           "scale": cfg.get("gee_scale") or 30,
           "point": ({"lon": lon, "lat": lat} if lon is not None else None)}
    ee, reason = _gee_init(cfg)
    out["earth_engine_ready"] = ee is not None
    if ee is None:
        out["status"] = reason
        return out
    out["status"] = "Earth Engine initialised; ready to sample the asset."
    # Discover the asset's BAND names so the Test panel can offer them as clickable
    # chips (an Image's bands, or the first image of a collection). Best-effort.
    asset = (cfg.get("gee_asset") or "").strip()
    if asset:
        try:
            try:
                bands = ee.Image(asset).bandNames().getInfo()
            except Exception:
                bands = ee.ImageCollection(asset).first().bandNames().getInfo()
            if isinstance(bands, list):
                out["bands"] = [str(b) for b in bands]
        except Exception:
            pass
    return out


def fetch_api(connector_config, record_attrs, connector_name="connector",
              field_map=None, output=None):
    """The generic API fetch — the centerpiece extractor.

    ``connector_config`` is a HydroConnector.config dict (or a HydroConnector row,
    or a (config, name) is supplied via ``connector_name``). ``record_attrs`` are
    the triggering record's attributes, used to fill {field} placeholders.

    ``field_map`` is the per-doctype x-api-map: a ``{connector_input_name:
    {source,field|value}}`` dict that REMAPS each connector input to one of THIS
    doctype's attribute keys (or a constant), so the same connector can bind to
    different doctypes. It is applied by ``_resolve_inputs`` only when the
    connector declares ``inputs[]``; for legacy (no-inputs) connectors it is
    ignored and the implicit token scan against ``record_attrs`` runs verbatim
    (full back-compat).

    ``output`` selects WHICH named output of the connector's outputs[] catalog to
    extract from the single cached response. It may be an output NAME (string), an
    output DICT (one outputs[] entry), or None. When None the connector's PRIMARY
    output is used (synthesized from legacy result_kind/output_path/x_path/y_path
    for a connector that declares no outputs[] — full back-compat). The detail
    renderer calls fetch_api once per ticked output with the SAME (name,url) cache
    key, so N outputs share ONE HTTP hit.

    Returns a dict the caller renders:
      {'kind': 'value',  'value': <scalar or None>}
      {'kind': 'series', 'x': [...], 'y': [...]}
      {'kind': 'json',   'json': <parsed>}
    plus 'url' (SECRET-REDACTED) and 'cached' (bool) in every case. On any failure
    a soft empty result of the requested kind is returned (never raises)."""
    cfg = _connector_config(connector_config)
    name = connector_name or cfg.get("name") or "connector"

    # NON-REST kinds dispatch to their own fetcher (same value/series/image shape).
    knd = (cfg.get("kind") or "rest").lower()
    if knd in ("netcdf", "thredds"):
        return _fetch_netcdf(cfg, record_attrs, output=output,
                             field_map=field_map, connector_name=name)
    if knd == "csv":
        return _fetch_csv(cfg, record_attrs, output=output,
                          field_map=field_map, connector_name=name)
    if knd == "wms":
        return _fetch_wms(cfg, record_attrs, output=output,
                          field_map=field_map, connector_name=name)
    if knd == "wcs":
        return _fetch_wcs(cfg, record_attrs, output=output,
                          field_map=field_map, connector_name=name)
    if knd == "gee":
        return _fetch_gee(cfg, record_attrs, output=output,
                          field_map=field_map, connector_name=name)

    # OUTPUT SELECTION (the multi-output model). Resolve the requested output to a
    # concrete outputs[] entry up front: an explicit dict is used verbatim; a name
    # is looked up in the synthesized catalog; None selects the primary. The chosen
    # entry's kind drives the post-fetch extraction. When the connector declares no
    # outputs[] this resolves to the synthesized single primary (legacy behavior).
    legacy_result_kind = (cfg.get("result_kind") or "value").lower()
    # 'json' is a legacy-only result kind (raw/scoped tree) used by the connector
    # Test flow; it has no outputs[] equivalent. Honor it ONLY when no specific
    # output was requested AND the connector declares no outputs[] catalog — i.e.
    # the pure legacy single-output path. Otherwise resolve a concrete output.
    legacy_json = (output is None and not cfg.get("outputs")
                   and legacy_result_kind == "json")
    out_entry = None
    if not legacy_json:
        if isinstance(output, dict):
            out_entry = output
        else:
            catalog = _connector_outputs(cfg)
            out_entry = (_find_output(catalog, output) if output
                         else _primary_output(catalog))
    out_kind = "json" if legacy_json else (
        (out_entry.get("kind") or "value").lower() if out_entry
        else legacy_result_kind)

    # INPUT RESOLUTION (the sourced-inputs model). If the connector declares
    # inputs[], resolve them through the shared resolver (applying the per-doctype
    # x-api-map field_map); the resulting flat {input.name: value} dict fills the
    # {token} placeholders downstream EXACTLY where record_attrs used to. If
    # inputs[] is absent, fall back to today's implicit token scan against
    # record_attrs verbatim so every existing connector/preset keeps working.
    missing_required = []
    if cfg.get("inputs"):
        attrs, missing_required = _resolve_inputs(cfg, record_attrs, field_map)
    else:
        attrs = dict(record_attrs or {})
    # Per-record REST: expose {bbox} (the record's shapefile bbox) + {datetime} (its
    # date window) as URL tokens when the connector opts in (spatial=shapefile / range).
    # Compute them from the ORIGINAL record_attrs (which carries _shapefile + the mapped
    # date fields) since _resolve_inputs returns only the declared inputs.
    if _supports_record_params(cfg):
        _ext = _inject_rest_spatiotemporal(cfg, record_attrs)
        for _k in ("bbox", "datetime"):
            if _ext.get(_k):
                attrs[_k] = _ext[_k]

    url_template = cfg.get("url_template") or ""
    method = (cfg.get("method") or "GET").upper()
    ttl = int(cfg.get("ttl_seconds") or _API_DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or _API_DEFAULT_TIMEOUT)
    auth = cfg.get("auth") or {}

    _API_TTL_BY_NAME[name] = ttl

    def _soft_empty(url, cached):
        """A no-data result of the SELECTED output's kind (no network needed)."""
        if out_kind == "series":
            return {"kind": "series", "columns": [], "n": 0, "x": [], "y": [],
                    "url": url, "cached": cached}
        if out_kind == "json":
            return {"kind": "json", "json": None, "url": url, "cached": cached}
        return {"kind": "value", "value": None, "url": url, "cached": cached}

    # A required input that resolved to empty => soft-empty result of the requested
    # kind, WITHOUT any network call (the URL would be missing a key segment, e.g.
    # an empty {sites} or {identifier}). Mirrors the graceful no-data path so a
    # half-filled record never errors the detail page.
    if missing_required:
        return _soft_empty("", False)

    # Build headers/query from templated config, then inject auth from the secret.
    headers = {k: _render_value(str(v), attrs) for k, v in (cfg.get("headers") or {}).items()}
    query = dict(cfg.get("query") or {})

    # Resolve the secret lazily (own session is fine; connector lookup already done).
    secret = None
    cred_name = (auth or {}).get("credential")
    if cred_name:
        try:
            with Session(App.get_persistent_store_database("hydro_db")) as session:
                secret = _resolve_secret(session, cred_name)
        except Exception:
            secret = None

    _inject_auth(auth, secret, headers, query)
    url = _build_url(url_template, query, attrs)

    # Redact the secret out of the URL for any echoed/displayed form.
    redacted_url = url
    if secret:
        redacted_url = redacted_url.replace(urllib.parse.quote(str(secret), safe=""), "***")
        redacted_url = redacted_url.replace(str(secret), "***")

    # Optional request BODY (templated like the URL, but NOT URL-encoded) -> POST/PUT
    # APIs (GraphQL, POST-search, OGC 'filter'). A dict body is JSON-encoded verbatim.
    body = None
    body_tpl = cfg.get("body_template")
    if body_tpl is None and cfg.get("body") is not None:
        body_tpl = cfg.get("body")
    if isinstance(body_tpl, (dict, list)):
        body = json.dumps(body_tpl)
    elif body_tpl:
        body = _render_body(str(body_tpl), attrs)
    content_type = cfg.get("body_content_type") or ("application/json" if body else None)
    accept = cfg.get("accept")

    now = time.time()
    _m = (method or "GET").upper()
    cached_entry = _API_CACHE.get((name, url, _m, body or ""))
    was_cached = bool(cached_entry and (now - cached_entry[0]) < ttl)

    # PAGINATION: follow a next-link (paginate.next_path) up to max_pages, then merge.
    pg = cfg.get("paginate") or {}
    next_path = (pg.get("next_path") or "").strip()
    max_pages = max(1, min(int(pg.get("max_pages") or 1), 50))
    pages, page_url, seen = [], url, set()
    for _ in range(max_pages if next_path else 1):
        d = _api_request_json(name, page_url, _m, headers, timeout,
                              body=body, content_type=content_type, accept=accept)
        if d is None:
            break
        pages.append(d)
        if not next_path:
            break
        nxt = _json_path(d, next_path)
        if nxt in (None, ""):
            break
        nxt = str(nxt)
        if nxt.startswith("/") or not nxt.startswith("http"):
            nxt = urllib.parse.urljoin(page_url, nxt)
        if nxt in seen or nxt == page_url:
            break
        seen.add(page_url)
        page_url = nxt

    if not pages:
        res = _soft_empty(redacted_url, was_cached)
        err = _API_LAST_ERROR.get(name)
        if err:
            res["note"] = "API error: " + err
        return res

    # JSON (legacy raw/scoped tree, Test flow only): scope page 1 by the output_path.
    if out_kind == "json":
        op = cfg.get("output_path") or ""
        scoped = _json_path(pages[0], op) if op else pages[0]
        return {"kind": "json", "json": scoped, "url": redacted_url, "cached": was_cached}

    # Extract the SELECTED named output from each page, then merge (series concat / value
    # keep-last). _extract_output reuses the _NO_DATA pairwise filter for the sparkline.
    extracted = _merge_extracted([_extract_output(p, out_entry) for p in pages])
    extracted["url"] = redacted_url
    extracted["cached"] = was_cached
    return extracted


# --- Connector PRESETS (verified live URLs + extraction paths from the probe).
# Each prefills a connector config for the builder; auth-less for the public
# water APIs. Keys mirror HydroConnector.config exactly. ---
CONNECTOR_PRESETS = {
    "nwis_iv": {
        "label": "USGS NWIS — Instantaneous Values",
        "config": {
            "url_template": "https://waterservices.usgs.gov/nwis/iv/?sites={sites}&parameterCd={parameterCd}&format={format}&siteStatus={siteStatus}&period={period}",
            "method": "GET",
            "headers": {},
            "query": {},
            "auth": {"scheme": "none", "credential": "", "placement": "header", "param": ""},
            "inputs": [
                {"name": "sites", "label": "Site Number", "type": "string",
                 "source": "field", "field": "nwis_site_id", "value": "",
                 "default": "09380000", "required": True, "in": "url"},
                {"name": "parameterCd", "label": "Parameter Code", "type": "string",
                 "source": "field", "field": "parameter_cd", "value": "",
                 "default": "00060", "required": False, "in": "url"},
                {"name": "format", "label": "Format", "type": "string",
                 "source": "constant", "field": "", "value": "json",
                 "default": "json", "required": False, "in": "url"},
                {"name": "siteStatus", "label": "Site Status", "type": "string",
                 "source": "constant", "field": "", "value": "all",
                 "default": "all", "required": False, "in": "url"},
                {"name": "period", "label": "Period", "type": "date",
                 "source": "runtime", "field": "", "value": "",
                 "default": "PT2H", "required": False, "in": "url"},
            ],
            "result_kind": "value",
            "output_path": "value.timeSeries.0.values.0.value.-1.value",
            "x_path": "value.timeSeries.0.values.0.value.*.dateTime",
            "y_path": "value.timeSeries.0.values.0.value.*.value",
            "outputs": [
                {"name": "discharge", "kind": "series",
                 "array_path": "value.timeSeries.0.values.0.value.*",
                 "variables": [
                     {"name": "dateTime", "path": "value.timeSeries.0.values.0.value.*.dateTime"},
                     {"name": "value", "path": "value.timeSeries.0.values.0.value.*.value"},
                     {"name": "qualifiers", "path": "value.timeSeries.0.values.0.value.*.qualifiers"}],
                 "type": "series", "unit": "ft3/s", "primary": True},
                {"name": "latest", "kind": "value",
                 "path": "value.timeSeries.0.values.0.value.-1.value",
                 "type": "number", "unit": "ft3/s"},
            ],
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "nwis_dv": {
        "label": "USGS NWIS — Daily Values",
        "config": {
            "url_template": "https://waterservices.usgs.gov/nwis/dv/?sites={sites}&parameterCd={parameterCd}&statCd={statCd}&format={format}&period={period}",
            "method": "GET",
            "headers": {},
            "query": {},
            "auth": {"scheme": "none", "credential": "", "placement": "header", "param": ""},
            "inputs": [
                {"name": "sites", "label": "Site Number", "type": "string",
                 "source": "field", "field": "nwis_site_id", "value": "",
                 "default": "09380000", "required": True, "in": "url"},
                {"name": "parameterCd", "label": "Parameter Code", "type": "string",
                 "source": "field", "field": "parameter_cd", "value": "",
                 "default": "00060", "required": False, "in": "url"},
                {"name": "statCd", "label": "Statistic Code", "type": "string",
                 "source": "constant", "field": "", "value": "00003",
                 "default": "00003", "required": False, "in": "url"},
                {"name": "format", "label": "Format", "type": "string",
                 "source": "constant", "field": "", "value": "json",
                 "default": "json", "required": False, "in": "url"},
                {"name": "period", "label": "Period", "type": "date",
                 "source": "runtime", "field": "", "value": "",
                 "default": "P90D", "required": False, "in": "url"},
            ],
            "result_kind": "value",
            "output_path": "value.timeSeries.0.values.0.value.-1.value",
            "x_path": "value.timeSeries.0.values.0.value.*.dateTime",
            "y_path": "value.timeSeries.0.values.0.value.*.value",
            "outputs": [
                {"name": "discharge", "kind": "series",
                 "array_path": "value.timeSeries.0.values.0.value.*",
                 "variables": [
                     {"name": "dateTime", "path": "value.timeSeries.0.values.0.value.*.dateTime"},
                     {"name": "value", "path": "value.timeSeries.0.values.0.value.*.value"},
                     {"name": "qualifiers", "path": "value.timeSeries.0.values.0.value.*.qualifiers"}],
                 "type": "series", "unit": "ft3/s", "primary": True},
                {"name": "latest", "kind": "value",
                 "path": "value.timeSeries.0.values.0.value.-1.value",
                 "type": "number", "unit": "ft3/s"},
            ],
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "nwps_stageflow": {
        "label": "NOAA NWPS — Gauge Stage/Flow",
        "config": {
            "url_template": "https://api.water.noaa.gov/nwps/v1/gauges/{identifier}/stageflow/{product}",
            "method": "GET",
            "headers": {},
            "query": {},
            "auth": {"scheme": "none", "credential": "", "placement": "header", "param": ""},
            "inputs": [
                {"name": "identifier", "label": "NWS LID", "type": "string",
                 "source": "field", "field": "nws_lid", "value": "",
                 "default": "", "required": True, "in": "path"},
                {"name": "product", "label": "Product", "type": "enum",
                 "source": "runtime", "field": "", "value": "",
                 "default": "", "required": False, "in": "path",
                 "options": ["observed", "forecast"]},
            ],
            "result_kind": "value",
            "output_path": "observed.data.-1.primary",
            "x_path": "observed.data.*.validTime",
            "y_path": "observed.data.*.primary",
            "outputs": [
                {"name": "stageflow", "kind": "series",
                 "array_path": "observed.data.*",
                 "variables": [
                     {"name": "validTime", "path": "observed.data.*.validTime"},
                     {"name": "primary", "path": "observed.data.*.primary"},
                     {"name": "secondary", "path": "observed.data.*.secondary"}],
                 "type": "series", "primary": True},
                {"name": "latest", "kind": "value",
                 "path": "observed.data.-1.primary", "type": "number"},
            ],
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "wqp_station": {
        "label": "Water Quality Portal — Station",
        "config": {
            "url_template": "https://www.waterqualitydata.us/data/Station/search?siteid={siteid}&mimeType={mimeType}&providers={providers}",
            "method": "GET",
            "headers": {},
            "query": {},
            "auth": {"scheme": "none", "credential": "", "placement": "header", "param": ""},
            "inputs": [
                {"name": "siteid", "label": "WQP Site ID", "type": "string",
                 "source": "field", "field": "wqp_site_id", "value": "",
                 "default": "USGS-09380000", "required": True, "in": "url"},
                {"name": "mimeType", "label": "MIME Type", "type": "string",
                 "source": "constant", "field": "", "value": "geojson",
                 "default": "geojson", "required": False, "in": "url"},
                {"name": "providers", "label": "Providers", "type": "enum",
                 "source": "runtime", "field": "", "value": "",
                 "default": "NWIS", "required": False, "in": "url",
                 "options": ["NWIS", "STORET"]},
            ],
            "result_kind": "value",
            "output_path": "features.0.properties.MonitoringLocationName",
            "x_path": "",
            "y_path": "",
            "outputs": [
                {"name": "station_name", "kind": "value",
                 "path": "features.0.properties.MonitoringLocationName",
                 "type": "string", "primary": True},
            ],
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "generic": {
        "label": "Generic REST (api-key header)",
        "config": {
            "url_template": "https://api.example.com/v1/sites/{value}/latest",
            "method": "GET",
            "headers": {},
            "query": {},
            "auth": {"scheme": "api_key", "credential": "", "placement": "header", "param": "X-Api-Key"},
            "inputs": [
                {"name": "value", "label": "Value", "type": "string",
                 "source": "field", "field": "value", "value": "",
                 "default": "", "required": True, "in": "url"},
            ],
            "result_kind": "value",
            "output_path": "data.0.measurements.-1.value",
            "x_path": "data.0.measurements.*.time",
            "y_path": "data.0.measurements.*.value",
            "outputs": [
                {"name": "series", "kind": "series",
                 "x_path": "data.0.measurements.*.time",
                 "y_path": "data.0.measurements.*.value",
                 "type": "series", "primary": True},
                {"name": "latest", "kind": "value",
                 "path": "data.0.measurements.-1.value", "type": "number"},
            ],
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "netcdf_opendap": {
        "label": "NetCDF — OPeNDAP (example: monthly SST)",
        "config": {
            "kind": "netcdf",
            "dataset_url": "http://test.opendap.org/dap/data/nc/coads_climatology.nc",
            "variable": "SST",
            "x_dim": "TIME",
            "unit": "degC",
            "ttl_seconds": 900,
            "timeout": 40,
        },
    },
    "thredds_catalog": {
        "label": "THREDDS catalog (siphon → OPeNDAP)",
        "config": {
            "kind": "thredds",
            "catalog_url": "https://thredds.example.org/thredds/catalog/path/catalog.xml",
            "dataset": "",
            "variable": "streamflow",
            "x_dim": "time",
            "ttl_seconds": 900,
            "timeout": 40,
        },
    },
    "csv_demo": {
        "label": "CSV — remote table (example)",
        "config": {
            "kind": "csv",
            "csv_url": "https://people.sc.fsu.edu/~jburkardt/data/csv/airtravel.csv",
            "delimiter": ",",
            "has_header": True,
            "value_column": "",
            "ttl_seconds": 900,
            "timeout": 15,
        },
    },
    "wms_usgs_topo": {
        "label": "WMS — USGS National Map (Topo)",
        "config": {
            "kind": "wms",
            "wms_url": "https://basemap.nationalmap.gov/arcgis/services/USGSTopo/MapServer/WMSServer",
            "layers": "0",
            "wms_version": "1.3.0",
            "image_format": "image/png",
            "styles": "",
            "crs": "EPSG:4326",
            "bbox_buffer": 0.2,
            "width": 512,
            "height": 384,
            "default_lon": -111.65,
            "default_lat": 40.23,
            "ttl_seconds": 900,
            "timeout": 20,
        },
    },
    "wcs_rasdaman": {
        "label": "WCS — rasdaman demo (AvgLandTemp)",
        "config": {
            "kind": "wcs",
            "wcs_url": "https://ows.rasdaman.org/rasdaman/ows",
            "coverage": "AvgLandTemp",
            "wcs_version": "2.0.1",
            "wcs_format": "application/netcdf",
            "variable": "",
            "lat_axis": "Lat",
            "lon_axis": "Long",
            "extra_subset": "ansi(\"2014-07-01\")",
            "bbox_buffer": 0.5,
            "default_lon": -111.65,
            "default_lat": 40.23,
            "ttl_seconds": 900,
            "timeout": 40,
        },
    },
    "gee_srtm": {
        "label": "GEE — SRTM elevation (needs earthengine-api + credential)",
        "config": {
            "kind": "gee",
            "gee_asset": "USGS/SRTMGL1_003",
            "gee_band": "elevation",
            "gee_reducer": "first",
            "gee_scale": 30,
            "gee_project": "",
            "gee_credential": "",
            "default_lon": -111.65,
            "default_lat": 40.23,
            "ttl_seconds": 3600,
            "timeout": 30,
        },
    },
}


@controller(name="map", url="map")
class HydroDeskMap(MapLayout):
    app = App
    map_title = "HydroDesk"
    map_subtitle = "Monitoring Stations — HydroForge generic store"
    default_map_extent = [-114.2, 36.9, -108.9, 42.1]  # Utah bbox (lon/lat)
    max_zoom = 18
    min_zoom = 5
    show_properties_popup = True
    plot_slide_sheet = True

    def compose_layers(self, request, map_view, *args, **kwargs):
        geojson = _records_geojson("monitoring_station")
        stations_layer = self.build_geojson_layer(
            geojson=geojson,
            layer_name="monitoring_station",
            layer_title="Monitoring Stations",
            layer_variable="stations",
            visible=True,
            selectable=True,
            plottable=True,
        )
        map_view.layers.append(stations_layer)
        layer_group = self.build_layer_group(
            id="hydrodesk-layers",
            display_name="HydroDesk",
            layers=[stations_layer],
        )
        return [layer_group]

    def get_plot_for_layer_feature(self, request, layer_name, feature_id,
                                   layer_data, feature_props, *args, **kwargs):
        """Real USGS NWIS daily-mean discharge for the clicked station."""
        label = feature_props.get("name", str(feature_id))
        site = feature_props.get("nwis_site_id")
        dates, values, nwis_name = ([], [], None)
        if site:
            dates, values, nwis_name = fetch_nwis_discharge(site)
        if dates and values:
            sub = f"USGS {site}" + (f" &middot; {nwis_name}" if nwis_name else "")
            data = [{
                "name": "Discharge (cfs)", "mode": "lines",
                "x": dates, "y": values, "line": {"color": "#0984e3"},
            }]
            layout = {
                "title": f"{label}<br><sub>{sub} &middot; daily mean, last 90 days</sub>",
                "xaxis": {"title": "Date"},
                "yaxis": {"title": "Discharge (cfs)"},
            }
        else:
            data = [{"name": "No data", "x": [], "y": []}]
            layout = {"title": f"{label} &mdash; no NWIS daily discharge available"}
        return label, data, layout


# ---------------------------------------------------------------------------
# Generic Frappe-style auto List View. Driven ENTIRELY by metadata: the
# HydroType row's display_name + field_schema. Works for ANY type by slug.
# No per-type code; columns derive from field_schema.properties at request time.
# ---------------------------------------------------------------------------

# When list_columns is absent from the stored row (it is NOT a column on the
# hydrotype table — it only lives in the source JSON spec), we still want a
# sensible, stable column order. We surface up to this many derived columns so
# the table stays readable for wide schemas; the rest remain in the record but
# off the list (classic Frappe behavior).
_MAX_LIST_COLUMNS = 6


def _derive_columns(field_schema):
    """Derive list columns from a JSON-Schema ``field_schema``.

    Returns a list of (key, header) tuples. ``key`` indexes into a record's
    ``attributes`` JSONB; ``header`` is the property's 'title' (falling back to
    a humanized key). Order follows the schema's property declaration order
    (Python dicts preserve insertion order), with any 'required' properties
    floated to the front so the most identifying fields lead the table.
    """
    schema = field_schema or {}
    required = schema.get("required") or []

    # DESIGN CHOICE (logged): API fields are OMITTED from the list view. Rendering
    # an API column would either show the raw substitution key (useless) or trigger
    # one external fetch PER ROW (N+1 network calls on every list render). The live
    # value belongs on the detail view, where exactly one fetch per field happens
    # (TTL-cached). So we drop x-api-connector properties from the derived columns;
    # they remain in the record and on the detail view.
    # Table (child-grid) AND linked (x-child-type) fields are also omitted: a list
    # of rows / linked records is not a meaningful single cell — they render in full
    # on the detail view. Field order follows the schema's x-order (JSONB scrambles).
    cols = [(k, p) for k, p in _ordered_props(schema)
            if not (p or {}).get("x-api-connector")
            and not (p or {}).get("x-child-type")
            and not (p or {}).get("x-layout")
            and not (p or {}).get("x-field") == "script"     # holds no value; outputs do
            and not (p or {}).get("x-field") == "shapefile"  # big GeoJSON blob, not a cell
            and not ((p or {}).get("type") == "array"
                     and (p or {}).get("x-widget") == "table")]
    pmap = dict(cols)
    # Stable order: required fields first (in x-order), then the rest (in x-order).
    ordered_keys = [k for k, _ in cols if k in required]
    ordered_keys += [k for k, _ in cols if k not in required]

    columns = []
    for key in ordered_keys[:_MAX_LIST_COLUMNS]:
        prop = pmap.get(key) or {}
        header = prop.get("title") or key.replace("_", " ").title()
        columns.append((key, header))
    return columns


def _format_cell(value):
    """Render a JSONB attribute value as a flat string for a table cell."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_cell(v) for v in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


# ===========================================================================
# OUTBOUND DATA API — exposes EVERY doctype's records as JSON/GeoJSON so other
# apps (e.g. TethysDash) can build dashboards. Metadata-driven: one endpoint
# serves any slug, with attribute filters, bbox, paging, ordering, and field
# selection to keep payloads small. Read access = a logged-in user with the
# doctype's read permission OR a valid read token (the 'api_read_token'
# credential), so external consumers can pull with ?token=/X-API-Token.
# ===========================================================================
_API_RESERVED = {"limit", "offset", "order", "fields", "format", "bbox", "q", "token",
                 "tables", "include"}
_API_OPS = {"gt", "lt", "gte", "lte", "ne", "contains", "in", "any"}


def _api_read_token(request):
    """The read token presented on the request (?token= or X-API-Token), or ''."""
    return (request.GET.get("token")
            or request.headers.get("X-API-Token")
            or request.headers.get("Authorization", "").replace("Bearer ", "")
            or "").strip()


def _api_authorized(request, field_schema):
    """True if the caller may read this doctype over the API: a logged-in user with
    read permission, OR a request token matching the stored 'api_read_token' secret
    (the explicit publish token for external consumers like TethysDash)."""
    if _user_can(request, field_schema, "read"):
        return True
    tok = _api_read_token(request)
    if not tok:
        return False
    try:
        import hmac
        with Session(App.get_persistent_store_database("hydro_db")) as session:
            secret = _resolve_secret(session, "api_read_token")
        return bool(secret) and hmac.compare_digest(tok, str(secret))
    except Exception:
        return False


def _api_apply_filters(stmt, params):
    """Apply attribute filters / bbox / order to a HydroRecord SELECT from the query
    params. Any param that isn't reserved is an attribute filter; 'key__op' selects an
    operator (gt/lt/gte/lte/ne/contains/in). Returns (stmt, errors)."""
    errors = []
    attrs = m.HydroRecord.attributes
    for key, val in params.items():
        if key in _API_RESERVED:
            continue
        field, _, op = key.partition("__")
        op = op or "eq"
        if op not in _API_OPS and op != "eq":
            errors.append("unknown operator '%s'" % op)
            continue
        col = attrs[field].astext
        try:
            if op == "eq":
                stmt = stmt.where(col == val)
            elif op == "ne":
                stmt = stmt.where(col != val)
            elif op == "contains":
                stmt = stmt.where(col.ilike("%%%s%%" % val))
            elif op == "in":
                stmt = stmt.where(col.in_([v.strip() for v in val.split(",") if v.strip()]))
            elif op == "any":
                # TABLE-aware filter: keep records whose inline-table field has ANY row
                # matching a (possibly compound) predicate — 'quality:Good',
                # 'discharge>100', or 'temp>20 AND quality:Good' / '(a OR b) AND c'.
                ast = _parse_row_predicate(val)
                if ast is None:
                    errors.append("table filter '%s' must be col:value / col>value, "
                                  "optionally joined by AND/OR" % key)
                    continue
                sfield = re.sub(r"[^A-Za-z0-9_]", "", field)
                jpath = '$.%s[*] ? (%s)' % (sfield, _predicate_to_jsonpath(ast))
                stmt = stmt.where(func.jsonb_path_exists(m.HydroRecord.attributes, jpath))
            else:  # numeric range
                num = cast(col, Float)
                stmt = stmt.where({"gt": num > float(val), "lt": num < float(val),
                                   "gte": num >= float(val), "lte": num <= float(val)}[op])
        except (ValueError, TypeError):
            errors.append("bad value for '%s'" % key)
    bbox = (params.get("bbox") or "").strip()
    if bbox:
        try:
            x0, y0, x1, y1 = [float(v) for v in bbox.split(",")]
            stmt = stmt.where(func.ST_Intersects(
                m.HydroRecord.geom, func.ST_MakeEnvelope(x0, y0, x1, y1, 4326)))
        except (ValueError, TypeError):
            errors.append("bbox must be minlon,minlat,maxlon,maxlat")
    q = (params.get("q") or "").strip()
    if q:  # free-text across all attributes (the whole JSONB blob cast to text)
        stmt = stmt.where(cast(m.HydroRecord.attributes, Text).ilike("%%%s%%" % q))
    order = (params.get("order") or "").strip()
    if order:
        desc = order.startswith("-")
        ofield = order.lstrip("-")
        ocol = attrs[ofield].astext
        stmt = stmt.order_by(ocol.desc() if desc else ocol.asc())
    return stmt, errors


# --- Connector-output fields in the API ----------------------------------------
# Some fields don't STORE their value in the record's attributes — they render a
# LIVE connector result per record (x-api-connector + x-nc-map, e.g. a Time-Series
# table fetched from THREDDS/NetCDF/REST/GEE/WCS). The stored attributes hold only
# the connector INPUTS (region, dates, site id). The Data API can MATERIALIZE these
# on request (?include=field) and filter on their rows (field__any=col OP value),
# reusing the exact detail-view path (_load_connector + _apply_nc_map + fetch_api).

def _connector_output_fields(field_schema):
    """{field_name: prop} for every field that renders a live connector output."""
    return {name: prop for name, prop in _ordered_props(field_schema or {})
            if (prop or {}).get("x-api-connector")}


def _materialize_connector_series(session, prop, attrs, cache):
    """Fetch ONE connector-output field for ONE record and return its series as a
    list of row dicts ([{col: val, ...}, ...]), or None. Picks the first Time-Series
    output. ``cache`` memoizes loaded HydroConnector rows across records in a request."""
    connector_name = (prop or {}).get("x-api-connector")
    if not connector_name:
        return None
    if connector_name in cache:
        connector = cache[connector_name]
    else:
        connector = _load_connector(session, connector_name)
        cache[connector_name] = connector
    if connector is None:
        return None
    cfg, mapped = _apply_nc_map(connector.config or {}, attrs, prop.get("x-nc-map"))
    field_map = prop.get("x-api-map")
    for entry in (prop.get("x-api-outputs") or [{}]):
        entry = entry if isinstance(entry, dict) else {}
        oname = (entry.get("output") or "").strip() or None
        try:
            result = fetch_api(cfg, mapped, connector_name=connector_name,
                               field_map=field_map, output=oname)
        except Exception:
            continue
        if (result or {}).get("kind") == "series":
            cols = result.get("columns") or []
            n = result.get("n") or max((len(c.get("values") or []) for c in cols), default=0)
            rows = []
            for i in range(n):
                rows.append({c.get("name"): (c.get("values") or [None] * (i + 1))[i]
                             if i < len(c.get("values") or []) else None
                             for c in cols})
            return rows
    return None


def _parse_table_expr(expr):
    """Parse ONE row condition 'col:value' / 'col>val' into (col, op, value_str), or
    None when malformed. The atom of a (possibly compound) __any predicate."""
    me = re.match(r"^\s*([A-Za-z0-9_]+)\s*(>=|<=|!=|>|<|=|:)\s*(.+?)\s*$", str(expr))
    return (me.group(1), me.group(2), me.group(3)) if me else None


# A __any value may be a COMPOUND predicate over a table row: conditions (col OP val)
# joined by AND/OR (or && / ||), with optional parentheses, e.g.
#   temp>20 AND quality:Good     |     (temp>30 OR temp<5) AND quality:Good
# It is parsed ONCE into a small AST — ('cond',col,op,val) | ('and',a,b) | ('or',a,b)
# — then compiled to a Postgres jsonpath predicate (inline tables) OR evaluated in
# Python per row (live connector series). Idents are sanitized and values are
# numeric-or-quoted at compile time, so the generated jsonpath is injection-safe.
# Caveat: AND/OR are operators, so a bare value containing the standalone word "and"
# / "or" must use the && / || forms (the UI builder assembles symbol-free rows).
_PRED_SPLIT_RE = re.compile(r"\s*(\(|\)|&&|\|\||\bAND\b|\bOR\b)\s*", re.IGNORECASE)


def _tokenize_predicate(expr):
    """Flat token list: ('PAREN','('/')'), ('OP','AND'/'OR'), ('COND',(col,op,val)).
    Returns None if any condition atom is malformed."""
    tokens = []
    for i, part in enumerate(_PRED_SPLIT_RE.split(str(expr))):
        if i % 2 == 1:                                   # a captured separator
            up = part.upper()
            up = {"&&": "AND", "||": "OR"}.get(up, up)
            tokens.append(("OP", up) if up in ("AND", "OR") else ("PAREN", up))
        else:
            s = part.strip()
            if not s:
                continue
            cond = _parse_table_expr(s)
            if cond is None:
                return None
            tokens.append(("COND", cond))
    return tokens or None


def _parse_row_predicate(expr):
    """Parse a compound row predicate into an AST, or None if malformed. AND binds
    tighter than OR; parentheses group. Bounded recursive descent (no eval)."""
    tokens = _tokenize_predicate(expr)
    if not tokens:
        return None

    def parse_or(i):
        node, i = parse_and(i)
        while i < len(tokens) and tokens[i] == ("OP", "OR"):
            rhs, i = parse_and(i + 1)
            node = ("or", node, rhs)
        return node, i

    def parse_and(i):
        node, i = parse_term(i)
        while i < len(tokens) and tokens[i] == ("OP", "AND"):
            rhs, i = parse_term(i + 1)
            node = ("and", node, rhs)
        return node, i

    def parse_term(i):
        if i >= len(tokens):
            raise ValueError("unexpected end")
        kind, payload = tokens[i]
        if (kind, payload) == ("PAREN", "("):
            node, i = parse_or(i + 1)
            if i >= len(tokens) or tokens[i] != ("PAREN", ")"):
                raise ValueError("unbalanced parens")
            return node, i + 1
        if kind == "COND":
            return ("cond",) + payload, i + 1
        raise ValueError("expected a condition")

    try:
        node, i = parse_or(0)
    except ValueError:
        return None
    return node if i == len(tokens) else None


def _predicate_to_jsonpath(ast):
    """Compile a predicate AST to the body of a jsonpath `? ( ... )` filter. Idents
    sanitized to [A-Za-z0-9_]; values numeric-or-quoted — so it is injection-safe."""
    kind = ast[0]
    if kind == "cond":
        _, col, op, val = ast
        col = re.sub(r"[^A-Za-z0-9_]", "", col)
        jop = {":": "==", "=": "==", "!=": "!=", ">": ">", "<": "<",
               ">=": ">=", "<=": "<="}[op]
        try:
            float(val)
            lit = val
        except ValueError:
            lit = '"%s"' % val.replace("\\", "").replace('"', "")
        return "@.%s %s %s" % (col, jop, lit)
    return "(%s %s %s)" % (_predicate_to_jsonpath(ast[1]),
                           "&&" if kind == "and" else "||",
                           _predicate_to_jsonpath(ast[2]))


def _cell_cmp(cell, op, valstr):
    """Compare one row cell against ``valstr`` with ``op``. Numeric when both parse as
    float; else string (== / != exact, ':' = case-insensitive contains). A null cell
    never matches (mirrors jsonpath's treatment of a missing key)."""
    if cell is None:
        return False
    try:
        cf, vf = float(cell), float(valstr)
    except (TypeError, ValueError):
        cs = str(cell)
        if op == "!=":
            return cs != valstr
        if op == "=":
            return cs == valstr
        if op == ":":
            return valstr.lower() in cs.lower()
        return False                                     # >,<,>=,<= on non-numeric
    return {">": cf > vf, "<": cf < vf, ">=": cf >= vf, "<=": cf <= vf,
            "!=": cf != vf, ":": cf == vf, "=": cf == vf}[op]


def _eval_predicate(ast, row):
    """Evaluate a predicate AST against one row dict (pure recursive walk)."""
    kind = ast[0]
    if kind == "cond":
        _, col, op, val = ast
        return _cell_cmp(row.get(col) if isinstance(row, dict) else None, op, val)
    if kind == "and":
        return _eval_predicate(ast[1], row) and _eval_predicate(ast[2], row)
    if kind == "or":
        return _eval_predicate(ast[1], row) or _eval_predicate(ast[2], row)
    return False


def _rows_match_any(rows, ast):
    """True if ANY row in ``rows`` satisfies the predicate AST (live-series __any)."""
    return any(isinstance(r, dict) and _eval_predicate(ast, r) for r in (rows or []))


def _connector_series_columns(cfg, output_name=None):
    """Best-effort STATIC column names for a connector's series output (no fetch),
    from its config: declared variables[], or netcdf/thredds x_dim + variable(s) (+
    derived). Returns [] when the columns are only knowable at fetch (e.g. CSV)."""
    outs = _connector_outputs(cfg or {})
    chosen = next((o for o in outs if output_name and o.get("name") == output_name), None)
    if chosen is None:
        chosen = (next((o for o in outs if o.get("kind") == "series" and o.get("primary")), None)
                  or next((o for o in outs if o.get("kind") == "series"), None))
    if not chosen:
        return []
    if chosen.get("variables"):
        return [v["name"] for v in _series_variables(chosen)]
    xd = (chosen.get("x_dim") or "time")
    if chosen.get("vars"):
        return [xd] + list(chosen["vars"]) + \
               [d["name"] for d in (chosen.get("derived") or []) if d.get("name")]
    if chosen.get("var"):
        return [xd, chosen["var"]]
    return []


@controller(name="api_records", url="api/{slug}/records", title="Records API",
            login_required=False)
def api_records(request, slug=None):
    """Outbound JSON/GeoJSON of a doctype's records (any slug). Query params: attribute
    filters (field / field__gt/lt/gte/lte/ne/contains/in), table-row filter (field__any=
    col OP val), bbox=, q=, order=, fields=, limit= (<=1000), offset=, format=json|geojson,
    include= (materialize live connector-output fields), token=. Read-gated by perm/token."""
    MAT_CAP = 50   # per-request live-fetch ceiling (each materialized field = 1+ HTTP hit)
    engine = App.get_persistent_store_database("hydro_db")
    page, total, notes = [], 0, []
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is None:
            return JsonResponse({"ok": False, "error": "unknown doctype '%s'" % slug}, status=404)
        display_name, field_schema, geometry_kind = meta
        if not _api_authorized(request, field_schema):
            return JsonResponse({"ok": False, "error": "read access denied (login or a valid token required)"}, status=401)
        try:
            limit = max(1, min(1000, int(request.GET.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(request.GET.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        fmt = (request.GET.get("format") or "json").strip().lower()
        want_fields = [f.strip() for f in (request.GET.get("fields") or "").split(",") if f.strip()]
        # tables=false / 0 -> drop inline-table (array) fields for a lean list payload.
        drop_tables = (request.GET.get("tables") or "").strip().lower() in ("0", "false", "no")

        # Connector-output fields aren't stored in attributes — they're fetched live.
        # ?include=field[,field] | all  materializes them; field__any=col OP val filters
        # on their rows (a Python post-filter, since the data isn't in JSONB).
        conn_fields = _connector_output_fields(field_schema)
        inc_raw = (request.GET.get("include") or "").strip()
        if inc_raw.lower() in ("all", "*", "connectors"):
            include = set(conn_fields)
        else:
            include = {x.strip() for x in inc_raw.split(",") if x.strip()} & set(conn_fields)

        # Split connector __any filters out of the SQL params (they can't be a jsonpath).
        conn_filters, sql_get = [], {}
        for k, v in request.GET.items():
            fld, _, op = k.partition("__")
            if fld in conn_fields and op == "any":
                ast = _parse_row_predicate(v)
                if ast is None:
                    return JsonResponse({"ok": False, "error":
                        "table filter '%s' must be col:value / col>value, optionally "
                        "joined by AND/OR" % k}, status=400)
                conn_filters.append((fld, ast))
                include.add(fld)          # filtering a connector field implies fetching it
            else:
                sql_get[k] = v

        base = select(m.HydroRecord.id, m.HydroRecord.attributes,
                      func.ST_AsGeoJSON(m.HydroRecord.geom)).where(
                          m.HydroRecord.hydrotype_slug == slug)
        filtered, ferr = _api_apply_filters(base, sql_get)
        if ferr:
            return JsonResponse({"ok": False, "error": "; ".join(ferr)}, status=400)

        conn_cache = {}

        def _materialize(rec_attrs):
            """{field: rows} for the requested connector-output fields of one record."""
            out = {}
            for fld in include:
                rows = _materialize_connector_series(session, conn_fields[fld], rec_attrs, conn_cache)
                if rows is not None:
                    out[fld] = rows
            return out

        # Both the count and the page can fail on a numeric filter over mixed-type data
        # (cast(Float) on a non-numeric attribute) -> 400, never a 500.
        try:
            if conn_filters:
                # Connector-row filters are applied in Python after a live fetch, so they
                # can't be counted/paged in SQL. Scan up to MAT_CAP SQL-filtered records,
                # materialize, filter, then page the survivors.
                sql_total = session.execute(
                    select(func.count()).select_from(filtered.subquery())).scalar() or 0
                kept = []
                for rid, attrs, geom in session.execute(filtered.limit(MAT_CAP)).all():
                    mats = _materialize(attrs)
                    if all(_rows_match_any(mats.get(fld), ast)
                           for fld, ast in conn_filters):
                        kept.append((rid, attrs, geom, mats))
                total = len(kept)
                if sql_total > MAT_CAP:
                    notes.append("connector-field filter applied to the first %d of %d "
                                 "records (live fetch); narrow the stored-field filters "
                                 "to cover more." % (MAT_CAP, sql_total))
                page = kept[offset:offset + limit]
            else:
                total = session.execute(
                    select(func.count()).select_from(filtered.subquery())).scalar() or 0
                eff_limit = limit
                if include and limit > MAT_CAP:
                    eff_limit = MAT_CAP
                    notes.append("limit capped at %d because ?include= fetches live "
                                 "connector data per record." % MAT_CAP)
                page = [(rid, attrs, geom, (_materialize(attrs) if include else {}))
                        for rid, attrs, geom in
                        session.execute(filtered.limit(eff_limit).offset(offset)).all()]
        except Exception as exc:
            return JsonResponse({"ok": False, "error": "query failed: %s" % str(exc)[:160]}, status=400)

    def _project(attrs, mats):
        a = {k: v for k, v in (attrs or {}).items() if not k.startswith("_")}  # hide reserved
        if drop_tables:                                # lean payload: omit inline tables
            a = {k: v for k, v in a.items() if not isinstance(v, list)
                 or (v and not isinstance(v[0], dict))}
        for fld, rows in (mats or {}).items():         # embed materialized connector series
            a[fld] = rows
        if want_fields:
            a = {k: v for k, v in a.items() if k in want_fields}
        return a

    if fmt == "geojson":
        feats = []
        for rid, attrs, geom, mats in page:
            props = _project(attrs, mats)
            props["id"] = str(rid)
            feats.append({"type": "Feature", "properties": props,
                          "geometry": json.loads(geom) if geom else None})
        out = {"type": "FeatureCollection", "features": feats,
               "numberReturned": len(feats), "numberMatched": total}
        if notes:
            out["notes"] = notes
        return JsonResponse(out)
    records = []
    for rid, attrs, geom, mats in page:
        rec = {"id": str(rid)}
        rec.update(_project(attrs, mats))
        if geom and not want_fields:
            rec["geometry"] = json.loads(geom)
        records.append(rec)
    out = {"ok": True, "doctype": slug, "display_name": display_name,
           "count": len(records), "matched": total,
           "limit": limit, "offset": offset, "records": records}
    if notes:
        out["notes"] = notes
    return JsonResponse(out)


@controller(name="api_catalog", url="api/catalog", title="API catalog",
            login_required=False)
def api_catalog(request):
    """A machine-readable catalog of every doctype's API endpoint + its fields, so a
    consumer (or the Explorer UI) can discover what's available. Read-gated overall by
    login OR the api_read_token; per-doctype read permission still applies on /records."""
    engine = App.get_persistent_store_database("hydro_db")
    base = request.build_absolute_uri("/apps/hydrodesk/api/").rstrip("/")
    out = []
    conn_cache = {}   # connector name -> row, so series columns are resolved once
    with Session(engine) as session:
        rows = session.execute(select(
            m.HydroType.slug, m.HydroType.display_name, m.HydroType.geometry_kind,
            m.HydroType.field_schema).order_by(m.HydroType.display_name)).all()
        for sl, dn, gk, fs in rows:
            if not _api_authorized(request, fs or {}):
                continue
            count = session.execute(
                select(func.count()).select_from(m.HydroRecord)
                .where(m.HydroRecord.hydrotype_slug == sl)).scalar() or 0
            fields = []
            for k, p in _ordered_props(fs or {}):
                p = p or {}
                if p.get("x-layout"):
                    continue
                fd = {"key": k, "title": p.get("title") or k, "type": p.get("type", "string")}
                if p.get("x-widget") == "table":   # an inline table / linked child-grid
                    fd["is_table"] = True
                    fd["columns"] = list(((p.get("items") or {}).get("properties") or {}).keys())
                    if p.get("x-child-type"):
                        fd["linked_to"] = p["x-child-type"]
                if p.get("x-api-connector"):        # live connector output (not stored)
                    cname = p["x-api-connector"]
                    fd["is_connector_output"] = True
                    fd["connector"] = cname
                    outs = [o for o in (p.get("x-api-outputs") or []) if isinstance(o, dict)]
                    fd["outputs"] = [{"output": o.get("output"), "label": o.get("label"),
                                      "field_type": o.get("field_type")} for o in outs]
                    # A Time-Series output becomes an inline table once materialized
                    # (?include=); derive its columns from the connector (no fetch).
                    ts = [o for o in outs if (o.get("field_type") or "") == "Time-Series"]
                    if ts:
                        fd["is_table"] = True
                        if cname not in conn_cache:
                            conn_cache[cname] = _load_connector(session, cname)
                        conn = conn_cache[cname]
                        if conn is not None:
                            cols = _connector_series_columns(conn.config or {},
                                                             ts[0].get("output") or None)
                            if cols:
                                fd["columns"] = cols
                fields.append(fd)
            out.append({"doctype": sl, "display_name": dn, "geometry": gk,
                        "count": count, "fields": fields,
                        "records_url": "%s/%s/records" % (base, sl),
                        "geojson_url": "%s/%s/records?format=geojson" % (base, sl)})
    return JsonResponse({"ok": True, "collections": out})


@controller(name="api_explorer", url="api", title="Data API")
def api_explorer(request):
    """The 'API development' UI: browse every doctype as a data endpoint, build a
    filtered query, preview the JSON, and copy the ready URL (with the read token) to
    paste into another app (e.g. TethysDash). Gated to builders."""
    if not _can_build(request):
        return _denied(request, "manage", "Data API")
    with Session(App.get_persistent_store_database("hydro_db")) as session:
        token = _resolve_secret(session, "api_read_token")
    base = request.build_absolute_uri(reverse("hydrodesk:api_catalog")).rsplit("/", 1)[0]
    return render(request, "hydrodesk/api_explorer.html", {
        "api_base": base,
        "catalog_url": reverse("hydrodesk:api_catalog"),
        "has_token": bool(token),
        "token": token or "",
        "credentials_url": reverse("hydrodesk:credentials"),
    })


@controller(name="list", url="list/{slug}", title="Records")
def hydrotype_list(request, slug="monitoring_station"):
    """Generic list view for one HydroType, addressed by its ``slug``.

    Loads the HydroType metadata row (display_name + field_schema) and every
    hydro_record of that slug from the generic store, derives table columns from
    field_schema.properties, and renders a Frappe-Desk-like list page inside the
    Tethys app chrome. Entirely metadata-driven — not specific to any one type.
    """
    engine = App.get_persistent_store_database("hydro_db")

    display_name = slug
    field_schema = {}
    columns = []
    rows = []

    with Session(engine) as session:
        # 1) Load the type's metadata (the "DocType") by slug.
        type_row = session.execute(
            select(m.HydroType.display_name, m.HydroType.field_schema)
            .where(m.HydroType.slug == slug)
        ).first()

        if type_row is not None:
            display_name = type_row[0] or slug
            field_schema = type_row[1] or {}

        if not _user_can(request, field_schema, "read"):
            return _denied(request, "view", display_name)

        columns = _derive_columns(field_schema)

        # 2) Load every record of this type; build a flat row per record.
        record_rows = session.execute(
            select(m.HydroRecord.id, m.HydroRecord.attributes)
            .where(m.HydroRecord.hydrotype_slug == slug)
            .order_by(m.HydroRecord.created_at)
        ).all()

        # The property fragment per column drives typed (Link/email/url) cells.
        props = (field_schema or {}).get("properties") or {}
        column_props = [(key, props.get(key) or {}) for key, _ in columns]

        # Batch-resolve every Link column up front: collect the UUIDs referenced
        # by each target slug across ALL rows, do ONE query per target, and build
        # a {target_slug: {uuid: label}} map (avoids the per-cell N+1).
        link_targets = {}  # target_slug -> set of uuid strings
        for rec_id, attributes in record_rows:
            attrs = attributes or {}
            for key, prop in column_props:
                target = prop.get("x-link-type")
                if target:
                    val = attrs.get(key)
                    if val not in (None, ""):
                        link_targets.setdefault(target, set()).add(str(val))
        link_label_maps = {
            target: _resolve_link_labels(session, target, uuids)
            for target, uuids in link_targets.items()
        }

    def _cell(prop, value):
        """Render one list cell, linkifying Link/email/url; plain otherwise.
        Uses the pre-batched link_label_maps for Link labels (no extra query)."""
        target = prop.get("x-link-type")
        if target and value not in (None, ""):
            uuid = str(value)
            label = link_label_maps.get(target, {}).get(uuid, uuid[:8])
            return _link_anchor(target, uuid, label)
        return _format_typed_cell(prop, value)  # email/url linkify, else plain

    for rec_id, attributes in record_rows:
        attrs = attributes or {}
        cells = [_cell(prop, attrs.get(key)) for key, prop in column_props]
        rows.append({"id": str(rec_id), "cells": cells})

    # Pre-sort deterministically on the first column so the static render is
    # already ordered for a headless screenshot (no client-side JS required).
    if columns:
        rows.sort(key=lambda r: (r["cells"][0] or "").lower())

    context = {
        "slug": slug,
        "display_name": display_name,
        "type_found": type_row is not None,
        "columns": [header for _, header in columns],
        "rows": rows,
        "record_count": len(rows),
    }
    return render(request, "hydrodesk/list.html", context)


# ---------------------------------------------------------------------------
# Generic Frappe-style auto Form View + Detail View. Both are driven ENTIRELY
# by the HydroType row's field_schema + geometry_kind — no per-type code.
# ---------------------------------------------------------------------------


def _load_hydrotype(session, slug):
    """Load (display_name, field_schema, geometry_kind) for a slug, or None."""
    row = session.execute(
        select(
            m.HydroType.display_name,
            m.HydroType.field_schema,
            m.HydroType.geometry_kind,
        ).where(m.HydroType.slug == slug)
    ).first()
    if row is None:
        return None
    return (row[0] or slug, row[1] or {}, row[2])


def _user_can(request, field_schema, action):
    """True if the request's user may perform ``action`` ('read' | 'write') on
    records of a type. Superusers/staff always may; an EMPTY allow-list for an
    action means everyone (any logged-in user); otherwise the user must belong to
    one of the allowed groups (roles). Permissions live in
    ``field_schema['x-permissions'] = {'read': [...groups], 'write': [...groups]}``.
    Write covers create/edit/delete."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    allowed = ((field_schema or {}).get("x-permissions") or {}).get(action) or []
    if not allowed:
        return True
    try:
        groups = set(user.groups.values_list("name", flat=True))
    except Exception:
        groups = set()
    return bool(groups & set(allowed))


def _denied(request, action, display_name):
    """Render the 'no permission' page (HTTP 403) for a record action on a type."""
    from django.http import HttpResponseForbidden
    html = render(request, "hydrodesk/denied.html", {
        "action": action, "display_name": display_name,
        "home_url": reverse("hydrodesk:home"),
    }).content
    return HttpResponseForbidden(html)


def _can_build(request):
    """True if the user may MANAGE the SCHEMA — create / edit / delete / duplicate
    HydroTypes and reach the DocType builder. Schema editing is privileged: superuser
    or staff only. (Per-record access is governed separately by _user_can +
    x-permissions; this gate is about who can change the metadata model itself.)"""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or user.is_staff)


def _label_for(target_field_schema, attrs):
    """Pick a human TITLE/label for a record from its attributes — the single
    'record title' function used by link labels, link pickers, and the detail
    header.

    Order: (1) the type's designated TITLE FIELD (``x-title-field``) when set and
    non-empty; (2) a Frappe-style ``name`` attribute; (3) the first non-empty real
    field in declaration order (skipping layout/api/child-table fields, which have
    no scalar value); (4) any non-empty non-reserved attribute. Always a plain
    string for safe linkification.
    """
    fs = target_field_schema or {}
    attrs = attrs or {}
    tf = fs.get("x-title-field")
    if tf and str(attrs.get(tf) or "").strip():
        return _format_cell(attrs.get(tf))
    if str(attrs.get("name") or "").strip():
        return str(attrs.get("name"))
    for key, prop in _ordered_props(fs):
        prop = prop or {}
        if prop.get("x-layout") or prop.get("x-api-connector") or prop.get("x-child-type"):
            continue
        v = attrs.get(key)
        if v not in (None, "") and not isinstance(v, (list, dict)):
            return _format_cell(v)
    for k, v in attrs.items():
        if not str(k).startswith("_") and v not in (None, ""):
            return _format_cell(v)
    return ""


def _link_options(session, target_slug):
    """Return [(record_uuid_str, label)] for every record of ``target_slug``,
    ordered by creation, for a Link field's <select>. Empty list if the target
    type is absent or has no records (caller renders a blank-only select)."""
    if not target_slug:
        return []
    meta = _load_hydrotype(session, target_slug)
    target_schema = meta[1] if meta else {}
    rows = session.execute(
        select(m.HydroRecord.id, m.HydroRecord.attributes)
        .where(m.HydroRecord.hydrotype_slug == target_slug)
        .order_by(m.HydroRecord.created_at)
    ).all()
    out = []
    for rid, attrs in rows:
        label = _label_for(target_schema, attrs) or str(rid)[:8]
        out.append((str(rid), label))
    return out


def _resolve_link_labels(session, target_slug, uuids):
    """Batch-resolve {uuid_str: label} for a set of linked record UUIDs of one
    target slug (one query, avoiding the N+1 on the list view). Missing UUIDs are
    simply absent from the returned map; the caller falls back to a short uuid."""
    uuids = [u for u in uuids if u]
    if not target_slug or not uuids:
        return {}
    meta = _load_hydrotype(session, target_slug)
    target_schema = meta[1] if meta else {}
    rows = session.execute(
        select(m.HydroRecord.id, m.HydroRecord.attributes)
        .where(m.HydroRecord.hydrotype_slug == target_slug)
        .where(m.HydroRecord.id.in_(uuids))
    ).all()
    return {str(rid): (_label_for(target_schema, attrs) or str(rid)[:8])
            for rid, attrs in rows}


def _link_anchor(target_slug, uuid, label):
    """Build a safe clickable <a> to a linked record's detail page. ``label`` is
    escaped by format_html, so a malicious record name cannot inject markup."""
    href = reverse("hydrodesk:detail",
                   kwargs={"slug": target_slug, "record_id": uuid})
    return format_html('<a href="{}">{}</a>', href, label or str(uuid)[:8])


def _resolve_geometry_kind(field_schema, geometry_kind):
    """Resolve the geometry kind from the HydroType column, falling back to a
    value carried inside field_schema. Returns None for a NON-SPATIAL type (so no
    lon/lat inputs are forced) — the builder stores 'point'/'line'/'polygon'
    explicitly when the type IS spatial, so an absent value genuinely means
    'no geometry'. Callers that want a lon/lat fallback for an UNKNOWN slug pass
    geometry_kind='point' explicitly."""
    if geometry_kind:
        return geometry_kind
    return (field_schema or {}).get("geometry_kind") or None


def _ordered_props(field_schema):
    """Return a field_schema's properties as ordered ``(key, prop)`` pairs.

    Honors the top-level ``x-order`` key list (the order source of truth, since
    JSONB does not preserve object-key order); properties missing from x-order are
    appended in dict order so a legacy/hand-edited schema still renders fully."""
    schema = field_schema or {}
    props = schema.get("properties") or {}
    order = [k for k in (schema.get("x-order") or []) if k in props]
    order += [k for k in props if k not in order]
    return [(k, props.get(k) or {}) for k in order]


def _table_item_columns(prop):
    """Return a Table field's child columns as ordered ``(key, child_prop)`` pairs.

    Honors the ``items['x-order']`` key list (the order source of truth, since JSONB
    does not preserve object-key order); any properties missing from x-order are
    appended in dict order so an older/hand-edited schema still renders fully."""
    items = (prop or {}).get("items") or {}
    cprops = items.get("properties") or {}
    order = [k for k in (items.get("x-order") or []) if k in cprops]
    order += [k for k in cprops if k not in order]
    return [(k, cprops.get(k) or {}) for k in order]


def _child_input_kind(cprop):
    """Map a Table child-column's JSON-Schema fragment -> the grid cell input kind
    the form template renders (text / number / date / checkbox / select)."""
    cprop = cprop or {}
    if cprop.get("enum"):
        return "select"
    t = cprop.get("type")
    if t == "boolean":
        return "checkbox"
    if t in ("number", "integer"):
        return "number"
    if t == "string" and cprop.get("format") == "date":
        return "date"
    return "text"


def _build_widgets(field_schema, geometry_kind=None, values=None, session=None):
    """Map each JSON-Schema property -> a widget descriptor the form template
    renders: text / select(enum) / number / checkbox / comma-text(array) / the
    typed string controls date / email / url / textarea / link-select. When
    ``geometry_kind == 'point'`` two extra Longitude/Latitude number widgets are
    appended; these are NOT schema properties (not validated against attributes).

    ``values`` (a partial attributes/POST mapping) re-fills inputs on re-render.
    A Link property (``x-link-type``) needs the live store to list the target
    type's records; ``session`` is reused if given, else a short-lived one is
    opened (every form GET/POST/re-render hits this path).
    """
    schema = field_schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    values = values or {}

    # Open a session only if a Link field actually needs the store. This keeps
    # link-free forms zero-DB while still working when no session is threaded in.
    own_session = None
    if session is None and any((p or {}).get("x-link-type") for p in props.values()):
        own_session = Session(App.get_persistent_store_database("hydro_db"))
        session = own_session

    fields = []
    try:
        for name, prop in _ordered_props(schema):  # honor x-order (JSONB scrambles)
            prop = prop or {}
            if prop.get("x-layout") == "tab":
                # Layout-only widget: starts a new top-level TAB on the form. Everything
                # until the next tab break belongs to this tab (JS builds the tab bar).
                fields.append({"widget": "tab", "name": name,
                               "label": prop.get("title") or "Tab"})
                continue
            if prop.get("x-layout") == "section":
                # Layout-only widget: starts a new titled section on the form.
                fields.append({"widget": "section", "name": name,
                               "label": prop.get("title") or ""})
                continue
            if prop.get("x-layout") == "column":
                # Layout-only widget: starts a new column within the section.
                fields.append({"widget": "column", "name": name,
                               "label": prop.get("title") or ""})
                continue
            t = prop.get("type")
            label = prop.get("title") or name.replace("_", " ").title()
            f = {
                "name": name,
                "label": label,
                "required": name in required,
                "help": prop.get("description", ""),
                "value": values.get(name, prop.get("default", "")),
                "show_if": prop.get("x-show-if"),
            }
            link_target = prop.get("x-link-type")
            api_connector = prop.get("x-api-connector")
            x_widget = prop.get("x-widget")
            fmt = prop.get("format")
            if t == "string" and prop.get("enum"):
                f["widget"] = "select"
                f["options"] = list(prop["enum"])
            elif t == "string" and api_connector:
                # API field: rendered READ-ONLY on the record form — it stores NO
                # input. The live value is fetched from the named connector only on
                # the detail/list views (resolved from the record's attrs). The form
                # shows just a muted note so the field is visible but inert.
                f["widget"] = "api"
                f["connector"] = api_connector
                # Display-only: which of THIS type's fields feed the connector
                # (the x-api-map). The record form stores no input for an API field.
                f["map"] = prop.get("x-api-map") or {}
            elif t == "string" and link_target:
                # Foreign key: a <select> of existing target-type records,
                # value=record UUID, label=name/first-field. Own its widget kind
                # because options are (uuid, label) PAIRS, not flat enum strings.
                f["widget"] = "link-select"
                f["link_target"] = link_target
                f["options"] = _link_options(session, link_target) if session else []
            elif t == "string" and x_widget == "textarea":
                f["widget"] = "textarea"
            elif t == "string" and fmt == "date":
                f["widget"] = "date"
            elif t == "string" and fmt == "email":
                f["widget"] = "email"
            elif t == "string" and fmt == "uri":
                f["widget"] = "url"
            elif t == "string" and prop.get("x-field") == "phone":
                f["widget"] = "phone"
            elif t == "string" and prop.get("x-field") == "color":
                f["widget"] = "color"
            elif prop.get("x-field") == "shapefile":
                # Per-record shapefile UPLOAD: a file input. Show how many polygons are
                # already stored (a file input can't pre-fill, so this is the cue that
                # a re-upload would replace an existing one).
                f["widget"] = "shapefile"
                f["zone_count"] = _geojson_feature_count(values.get(name) or "")
                f["has_value"] = f["zone_count"] > 0
            elif prop.get("x-field") == "formula":
                # Computed field: READ-ONLY on the form (filled on save from the other
                # fields); just shows its expression so it's visible but inert.
                f["widget"] = "formula"
                f["formula"] = prop.get("x-formula") or ""
            elif prop.get("x-field") == "script":
                # Python-script field: READ-ONLY badge on the form (outputs are computed
                # on save). The source itself isn't surfaced into the form context.
                f["widget"] = "script"
                f["script_outputs"] = prop.get("x-script-outputs") or []
            elif t in ("number", "integer"):
                f["widget"] = "number"
                f["step"] = "any" if t == "number" else "1"
                # Rating is an integer 0..max, shown as a small bounded number input.
                if prop.get("x-field") == "rating":
                    f["min"] = 0
                    f["max"] = int(prop.get("maximum") or 5)
            elif t == "boolean":
                f["widget"] = "checkbox"
                f["checked"] = bool(values.get(name, prop.get("default", False)))
            elif t == "array" and prop.get("x-child-type"):
                # LINKED table: rows are records of another HydroType. Not edited
                # inline on this form — they are added/managed on the saved record's
                # detail page (where the parent id exists to link them).
                f["widget"] = "linked-table"
                f["child_type"] = prop.get("x-child-type")
            elif t == "array" and x_widget == "table":
                # Child grid: an editable table of row objects. Columns come from the
                # item schema; rows come from the stored list. The form posts the
                # whole grid as ONE JSON blob under the field name (a hidden carrier
                # the grid JS keeps in sync) — _coerce_attributes parses it back.
                f["widget"] = "table"
                f["columns"] = [{
                    "key": ck,
                    "label": (cp or {}).get("title") or ck.replace("_", " ").title(),
                    "input": _child_input_kind(cp or {}),
                    "options": list((cp or {}).get("enum") or []),
                } for ck, cp in _table_item_columns(prop)]
                v = values.get(name)
                f["rows"] = v if isinstance(v, list) else []
                f["rows_json"] = json.dumps(f["rows"])
                f["columns_json"] = json.dumps(f["columns"])
            elif t == "array":
                f["widget"] = "text"
                v = values.get(name)
                if isinstance(v, (list, tuple)):
                    f["value"] = ", ".join(str(x) for x in v)
                f["help"] = (f["help"] + " (comma-separated)").strip()
            else:  # string + anything unknown -> plain text
                f["widget"] = "text"
            fields.append(f)
    finally:
        if own_session is not None:
            own_session.close()

    if _resolve_geometry_kind(schema, geometry_kind) == "point":
        fields.append({
            "name": "longitude", "label": "Longitude", "widget": "number",
            "step": "any", "required": True, "is_geom": True,
            "help": "Decimal degrees (-180 to 180)",
            "value": values.get("longitude", ""),
        })
        fields.append({
            "name": "latitude", "label": "Latitude", "widget": "number",
            "step": "any", "required": True, "is_geom": True,
            "help": "Decimal degrees (-90 to 90)",
            "value": values.get("latitude", ""),
        })
    return fields


def _coerce_attributes(field_schema, post):
    """Coerce a request.POST.dict() (all strings; unchecked checkboxes absent)
    into a typed dict ready for JSON-Schema validation. Returns (attributes,
    errors) where errors maps a field name -> message for un-coercible values.

    Booleans are set from key-presence (must run before the skip-if-missing
    guard); empty optional values are dropped so the 'required' list — not a
    spurious float('') — governs presence.
    """
    props = (field_schema or {}).get("properties") or {}
    out, errors = {}, {}
    for key, prop in props.items():
        prop = prop or {}
        t = prop.get("type")
        if prop.get("x-layout") or prop.get("x-child-type"):
            continue  # layout marker (no data) / LINKED table (separate records)
        if prop.get("x-field") in ("formula", "script") or prop.get("x-computed"):
            continue  # computed on save (never user-provided) — a POST can't forge them
        if prop.get("x-field") == "shapefile":
            continue  # uploaded file -> handled from request.FILES, not POST text
        if t == "boolean":
            out[key] = key in post
            continue
        if key not in post or str(post[key]).strip() == "":
            continue
        raw = str(post[key]).strip()
        if t == "integer":
            try:
                out[key] = int(raw)
            except ValueError:
                errors[key] = "must be an integer"
        elif t == "number":
            try:
                out[key] = float(raw)
            except ValueError:
                errors[key] = "must be a number"
        elif t == "array" and prop.get("x-widget") == "table":
            # The grid posts ONE JSON blob (list of row objects). Parse + coerce each
            # cell per the child schema; drop blank rows so empty grids store [].
            try:
                arr = json.loads(raw)
            except (ValueError, TypeError):
                errors[key] = "invalid table data"
                continue
            item_props = (prop.get("items") or {}).get("properties") or {}
            rows_out = []
            for row in (arr if isinstance(arr, list) else []):
                if not isinstance(row, dict):
                    continue
                cells = {}
                for ck, cp in item_props.items():
                    ct = (cp or {}).get("type")
                    cv = row.get(ck)
                    if ct == "boolean":
                        cells[ck] = (cv if isinstance(cv, bool)
                                     else str(cv).strip().lower() in ("true", "on", "1", "yes"))
                        continue
                    if cv is None or str(cv).strip() == "":
                        continue
                    sval = str(cv).strip()
                    if ct == "number":
                        try:
                            cells[ck] = float(sval)
                        except ValueError:
                            errors[key] = f"{(cp or {}).get('title', ck)}: must be a number"
                    elif ct == "integer":
                        try:
                            cells[ck] = int(sval)
                        except ValueError:
                            errors[key] = f"{(cp or {}).get('title', ck)}: must be an integer"
                    else:
                        cells[ck] = sval
                # Keep a row only if it has at least one non-empty/true cell.
                if any(v not in (None, "", False) for v in cells.values()):
                    rows_out.append(cells)
            out[key] = rows_out
        elif t == "array":
            out[key] = [s.strip() for s in raw.split(",") if s.strip()]
        else:  # string (+ enum) stays a string
            out[key] = raw
    return out, errors


def _shapefile_fields(field_schema):
    """The keys of all Shapefile-type (x-field=='shapefile') properties, in order."""
    return [k for k, p in _ordered_props(field_schema or {})
            if (p or {}).get("x-field") == "shapefile"]


def _geojson_feature_count(s):
    """How many polygons a stored shapefile-field GeoJSON string holds (0 if none /
    unparseable) — a FeatureCollection's features, a MultiPolygon's parts, else 1."""
    s = (s or "").strip()
    if not s.startswith("{"):
        return 0
    try:
        g = json.loads(s)
    except (ValueError, TypeError):
        return 0
    t = (g.get("type") or "").lower()
    if t == "featurecollection":
        return len(g.get("features") or [])
    if t == "multipolygon":
        return len(g.get("coordinates") or [])
    if t in ("polygon", "feature"):
        return 1
    return 0


def _apply_shapefile_uploads(field_schema, files, attributes, existing=None):
    """Fold per-record SHAPEFILE uploads into ``attributes``. For each Shapefile field:
    a newly uploaded file (request.FILES) is converted to an inline GeoJSON
    FeatureCollection and stored under the field key; with no new upload the prior
    value is preserved (edit). Returns a list of error strings (bad uploads)."""
    files = files or {}
    existing = existing or {}
    errors = []
    for key in _shapefile_fields(field_schema):
        up = files.get(key)
        if up is not None:
            gj = _shapefile_to_featurecollection(up)
            if gj:
                attributes[key] = gj
            else:
                errors.append("%s: could not read a shapefile (expected a .zip bundle "
                              "of .shp/.shx/.dbf, or a .shp)." % key)
        elif existing.get(key):
            attributes[key] = existing[key]            # keep the stored geometry on edit
    return errors


# --- Formula fields: a SAFE arithmetic evaluator (no eval/exec). Only numbers, the
#     record's own field values (by key), +-*/ ** % //, unary +/-, and a small set of
#     whitelisted math functions are allowed; anything else -> None. ---
_FORMULA_FUNCS = None


def _formula_funcs():
    global _FORMULA_FUNCS
    if _FORMULA_FUNCS is None:
        import math
        _FORMULA_FUNCS = {
            "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "round": round,
            "floor": math.floor, "ceil": math.ceil, "log": math.log,
            "log10": math.log10, "exp": math.exp, "pow": pow,
        }
    return _FORMULA_FUNCS


def _safe_eval(expr, variables):
    """Evaluate an arithmetic ``expr`` over ``variables`` (a {name: value} map of the
    record's fields). Returns a rounded float, or None when the expression is empty,
    references a non-numeric/absent field, or uses anything not on the allow-list."""
    import ast
    import operator as op
    expr = (expr or "").strip()
    if not expr:
        return None
    bin_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
               ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod,
               ast.FloorDiv: op.floordiv}
    un_ops = {ast.UAdd: op.pos, ast.USub: op.neg}
    funcs = _formula_funcs()

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric constant")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in bin_ops:
            return bin_ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in un_ops:
            return un_ops[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name):
            val = variables.get(node.id)
            if isinstance(val, bool) or val is None:
                raise ValueError("field %s is not a number" % node.id)
            return float(val)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in funcs and not node.keywords):
            return funcs[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("disallowed expression")

    try:
        result = ev(ast.parse(expr, mode="eval"))
        return round(float(result), 6)
    except Exception:
        return None


def _compute_formulas(field_schema, attrs):
    """Fill every formula field (x-field == 'formula') by evaluating its x-formula
    expression over the record's other field values. Modifies ``attrs`` in place: a
    successful result is stored; an un-computable one is dropped (so a stale/forged
    value never survives). Runs after coercion, before validation.

    Order-aware: evaluates in declaration (x-order) order and re-runs up to N passes
    until stable, so a formula that depends on ANOTHER formula resolves regardless of
    which is declared first (a small fixpoint; cycles simply settle/drop)."""
    formulas = [(k, p) for k, p in _ordered_props(field_schema)
                if (p or {}).get("x-field") == "formula"]
    if not formulas:
        return attrs
    for _pass in range(len(formulas)):
        changed = False
        for key, prop in formulas:
            val = _safe_eval(prop.get("x-formula") or "", attrs)
            if val is None:
                if key in attrs:
                    attrs.pop(key, None)
                    changed = True
            elif attrs.get(key) != val:
                attrs[key] = val
                changed = True
        if not changed:
            break
    return attrs


# ===========================================================================
# PYTHON SCRIPT field (x-field == 'script') — runs a sandboxed Python script over
# the record's fields and stores its declared, typed OUTPUTS on save (so they are
# fast + filterable in the Data API). Sandbox: RestrictedPython + CURATED PROXIES
# (np/pd carry only a vetted whitelist; raw modules are never exposed, so a script
# can't walk pd.compat.os... to the host), an attribute denylist (file/pickle/
# network/code-exec), an underscore/format compile block, and a module-root guard.
# Heavy imports (RestrictedPython, numpy, pandas) are LAZY so app startup is unaffected.
# Inputs bind by NAME: a free variable matching a record field uses that field's value.
# ===========================================================================
# Pure-compute stdlib roots a script may use. '_strptime'/'time'/'calendar' are
# datetime.strptime's lazy transitive imports — none expose file/network, the module-root
# guard blocks pivoting through them to os/sys, and any time.sleep DoS is bounded by the
# SIGALRM timeout. The script can't name '_strptime' (underscore is compile-blocked).
_SCRIPT_ALLOWED_ROOTS = {"math", "statistics", "datetime", "json", "re",
                         "_strptime", "time", "calendar"}
_SCRIPT_PROXY_ROOTS = {"numpy", "pandas"}
_SCRIPT_DENY_ATTRS = {
    "eval", "query", "to_pickle", "to_csv", "to_excel", "to_hdf", "to_sql",
    "to_parquet", "to_feather", "to_clipboard", "to_stata", "to_gbq", "to_xml",
    "to_latex", "to_markdown", "to_html", "to_json", "to_string", "tofile",
    "dump", "load", "open", "system", "popen",
    # NOTE: json.loads / json.dumps (string <-> JSON) are SAFE and allowed — pickle/
    # marshal are not importable and numpy's load/save aren't on the proxy, so 'loads'/
    # 'dumps' can only ever resolve to json's (no code-exec path). file 'load'/'dump'
    # stay blocked (they'd need a file object the sandbox can't obtain anyway).
    "ctypes", "data_as", "getfield", "setfield", "setflags", "newbyteorder",
}
_SCRIPT_NP_FUNCS = (
    "array asarray arange linspace logspace zeros ones full eye identity mean "
    "average std var median percentile quantile min max amin amax argmin argmax "
    "sum prod cumsum cumprod diff gradient abs absolute sqrt cbrt square exp expm1 "
    "log log2 log10 log1p sin cos tan arcsin arccos arctan arctan2 deg2rad rad2deg "
    "floor ceil round clip where select sign mod remainder power dot vdot cross "
    "outer inner concatenate stack vstack hstack column_stack split reshape ravel "
    "transpose flip roll sort argsort unique isnan isinf isfinite nan_to_num "
    "nanmean nanstd nansum nanmin nanmax interp polyfit polyval histogram bincount "
    "corrcoef cov array_equal allclose isclose pi e nan inf newaxis float64 int64 "
    "bool_ "
    # --- additions vetted from the hydrology audit (all pure-compute; no I/O/eval) ---
    "trapz trapezoid zeros_like ones_like full_like empty empty_like maximum minimum "
    "fmax fmin convolve correlate searchsorted digitize ptp count_nonzero nonzero "
    "flatnonzero nanpercentile nanquantile nanmedian nanvar nancumsum nanargmax "
    "nanargmin nanprod tanh sinh cosh radians degrees hypot meshgrid tile repeat "
    "expand_dims squeeze moveaxis swapaxes take indices apply_along_axis pad append "
    "insert delete array_split ediff1d add subtract multiply divide true_divide "
    "floor_divide isin intersect1d union1d setdiff1d diag atleast_1d").split()
_SCRIPT_NP_SUBNS = {
    "random": "rand randn randint normal uniform choice seed random_sample permutation shuffle".split(),
    "linalg": "norm inv solve det eig eigvals svd lstsq pinv qr matrix_rank".split(),
    "fft": "fft ifft rfft irfft fftfreq rfftfreq".split(),
}
_SCRIPT_PD_FUNCS = (
    "DataFrame Series concat merge date_range to_datetime to_numeric to_timedelta "
    "Timestamp Timedelta NaT isna notna cut qcut Categorical pivot_table melt "
    "crosstab factorize "
    # audit additions for calendar-correct water-year work. NOTE: only the CLASSES/
    # functions — NOT pd.offsets (a module, which would reopen pd.offsets.np.<io> pivots).
    "DateOffset Period period_range Grouper").split()


def _script_ns(module, names, subns=None):
    """A SimpleNamespace exposing only ``names`` from ``module`` (+ sub-namespaces)."""
    import types
    o = types.SimpleNamespace()
    for n in names:
        if hasattr(module, n):
            setattr(o, n, getattr(module, n))
    for sub, subnames in (subns or {}).items():
        if hasattr(module, sub):
            setattr(o, sub, _script_ns(getattr(module, sub), subnames))
    return o


def _build_script_globals():
    """Build the sandbox global namespace (curated proxies + guarded builtins). Lazy:
    imports RestrictedPython/numpy/pandas only on first script run. Fresh per run, so a
    script can't pollute the proxies for the next run."""
    import types, builtins
    import numpy, pandas, math, statistics, datetime, json, re
    from RestrictedPython import safe_builtins
    from RestrictedPython.Guards import (safer_getattr, guarded_iter_unpack_sequence,
                                         guarded_unpack_sequence, full_write_guard)
    from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter

    np_proxy = _script_ns(numpy, _SCRIPT_NP_FUNCS, _SCRIPT_NP_SUBNS)
    if not hasattr(np_proxy, "trapz") and hasattr(numpy, "trapezoid"):
        np_proxy.trapz = numpy.trapezoid       # numpy>=2.0 renamed trapz -> trapezoid
    pd_proxy = _script_ns(pandas, _SCRIPT_PD_FUNCS)
    modules = {"np": np_proxy, "numpy": np_proxy, "pd": pd_proxy, "pandas": pd_proxy,
               "math": math, "statistics": statistics, "datetime": datetime,
               "json": json, "re": re}

    missing = object()                                  # sentinel for "attr not present"

    def _guard_getattr(obj, name, default=None):
        if name in _SCRIPT_DENY_ATTRS or name.startswith("read_"):
            raise AttributeError("attribute '%s' is not allowed in a script" % name)
        val = safer_getattr(obj, name, missing)         # blocks _-names, format/format_map
        if val is missing:
            # A clear error beats the cryptic "'NoneType' object is not callable" you'd
            # get if a not-whitelisted np/pd function silently resolved to None.
            raise AttributeError("'%s' is not available in the script sandbox" % name)
        if isinstance(val, types.ModuleType):
            root = (getattr(val, "__name__", "") or "").split(".")[0]
            if root not in _SCRIPT_ALLOWED_ROOTS:
                raise AttributeError("reaching module '%s' is not allowed"
                                     % getattr(val, "__name__", "?"))
        return val

    def _guard_import(name, *a, **k):
        root = name.split(".")[0]
        if root in _SCRIPT_PROXY_ROOTS:
            return modules[root]                       # import numpy -> the PROXY
        if root in _SCRIPT_ALLOWED_ROOTS:
            return __import__(name, *a, **k)
        raise ImportError("import of '%s' is not allowed in a script" % name)

    # Augmented assignment (a += b, s -= r) compiles to _inplacevar_('+=', a, b); without
    # it RestrictedPython raises NameError on EVERY += — breaking accumulator/mass-balance
    # loops. Pure arithmetic, no I/O.
    _INPLACE = {"+=": lambda a, b: a + b, "-=": lambda a, b: a - b,
                "*=": lambda a, b: a * b, "/=": lambda a, b: a / b,
                "//=": lambda a, b: a // b, "%=": lambda a, b: a % b,
                "**=": lambda a, b: a ** b, "&=": lambda a, b: a & b,
                "|=": lambda a, b: a | b, "^=": lambda a, b: a ^ b,
                ">>=": lambda a, b: a >> b, "<<=": lambda a, b: a << b,
                "@=": lambda a, b: a @ b}

    def _inplacevar(op, x, y):
        fn = _INPLACE.get(op)
        if fn is None:
            raise NotImplementedError("operator %s not supported" % op)
        return fn(x, y)

    # Item/slice assignment (arr[i]=x, df['c']=…) — full_write_guard blocks it on numpy/
    # pandas objects, which kills the canonical preallocate-then-fill recursive-filter
    # idiom. Permit it ONLY for these in-memory compute types; attribute writes still can't
    # reach a dunder (compile-blocked) or a module (proxy/denylist-gated), so no escape.
    def _script_write(obj):
        if isinstance(obj, (numpy.ndarray, numpy.generic,
                            pandas.Series, pandas.DataFrame, pandas.Index)):
            return obj
        return full_write_guard(obj)

    b = dict(safe_builtins)
    b["__import__"] = _guard_import
    for fn in ("min", "max", "sum", "abs", "round", "len", "range", "sorted",
               "enumerate", "zip", "map", "filter", "all", "any", "bool", "int",
               "float", "str", "list", "dict", "tuple", "set", "frozenset",
               "reversed", "divmod", "pow", "isinstance"):
        b[fn] = getattr(builtins, fn)
    g = {"__builtins__": b, "_getattr_": _guard_getattr,
         "_getitem_": default_guarded_getitem, "_getiter_": default_guarded_getiter,
         "_write_": _script_write, "_inplacevar_": _inplacevar,
         "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
         "_unpack_sequence_": guarded_unpack_sequence}
    g.update(modules)
    return g


def _run_python_script(src, inputs=None, timeout=5):
    """Compile + run ``src`` in the sandbox with ``inputs`` bound, returning the dict
    of script-assigned variables. Raises on a blocked op / syntax / runtime error /
    timeout. Best-effort CPU timeout via SIGALRM (main thread only)."""
    import threading, signal
    from RestrictedPython import compile_restricted
    code = compile_restricted(src, "<hydrodesk-script>", "exec")
    g = _build_script_globals()
    injected = set(g.keys())
    g.update(inputs or {})
    injected |= set((inputs or {}).keys())
    use_alarm = (hasattr(signal, "SIGALRM")
                 and threading.current_thread() is threading.main_thread())
    if use_alarm:
        def _on_timeout(signum, frame):
            raise TimeoutError("script exceeded %ss" % timeout)
        old = signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        exec(code, g)
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    return {k: v for k, v in g.items() if k not in injected and not k.startswith("_")}


def _script_free_vars(src):
    """The script's free variables = names read before assignment, minus builtins and
    the pre-bound module names. These are the script's INPUTS (bound by name to record
    fields). A best-effort over/under-approximation that drives the builder + binding."""
    import ast
    try:
        tree = ast.parse(src or "")
    except SyntaxError:
        return []
    bound, loaded = set(), []
    builtin_names = set(dir(__import__("builtins")))
    premod = {"np", "numpy", "pd", "pandas", "math", "statistics", "datetime", "json", "re"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (bound.add(node.id) if isinstance(node.ctx, ast.Store) else loaded.append(node.id))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
    seen, out = set(), []
    for n in loaded:
        if n not in bound and n not in builtin_names and n not in premod and n not in seen:
            seen.add(n); out.append(n)
    return out


def _script_jsonify(v):
    """Coerce a script output (numpy/pandas/datetime/...) into a JSON-storable value."""
    try:
        import numpy as _np
        if isinstance(v, _np.generic):
            v = v.item()
        elif isinstance(v, _np.ndarray):
            return [_script_jsonify(x) for x in v.tolist()]
    except Exception:
        pass
    try:
        import pandas as _pd
        if isinstance(v, _pd.Series):
            return [_script_jsonify(x) for x in v.tolist()]
        if isinstance(v, _pd.DataFrame):
            return [{str(k): _script_jsonify(x) for k, x in row.items()}
                    for row in v.to_dict(orient="records")]
        if isinstance(v, _pd.Timestamp):
            return v.isoformat()
    except Exception:
        pass
    import datetime as _dt
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, float):
        import math as _m
        return None if (_m.isnan(v) or _m.isinf(v)) else v
    if v is None or isinstance(v, (int, str, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_script_jsonify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _script_jsonify(x) for k, x in v.items()}
    return str(v)


def _coerce_script_output(value, ftype):
    """JSON-ify a script output and coerce by its declared field type."""
    v = _script_jsonify(value)
    ft = (ftype or "").lower()
    if ft in ("number", "float", "int", "integer"):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return v
    if ft in ("json", "object"):
        return v                            # store the JSON value as-is (object/array/scalar)
    if ft in ("time-series", "table", "series"):
        # A table is a list of row dicts. A dict of SCALARS -> key/value rows
        # ([{key,value}, ...]); a dict with nested values -> a single (one-row) table;
        # a list passes through; a bare scalar -> one row {value}.
        if isinstance(v, dict):
            if not v:
                return []
            if all(val is None or isinstance(val, (int, float, str, bool))
                   for val in v.values()):
                return [{"key": k, "value": val} for k, val in v.items()]
            return [v]
        if isinstance(v, list):
            return v
        return [] if v is None else [{"value": v}]
    if ft in ("text", "string", "date"):
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, (dict, list)):     # store JSON, never a Python repr string
            return json.dumps(v)
        return str(v)
    return v


def _script_output_json_type(field_type):
    """The JSON-Schema 'type' for a computed-output property, from its UI field type."""
    return {"Number": "number", "Text": "string", "Date": "string",
            "Time-Series": "array", "Image": "string",
            "JSON": "object"}.get(field_type, "string")


def _infer_script_output_field_type(value):
    """Best-effort (field_type, kind) for a script result value — used to pre-select a
    variable's type in the builder's Test-run output picker. The backend re-validates the
    user's final choice, so a wrong guess is harmless."""
    try:
        import numpy as _np
        if isinstance(value, _np.generic):
            value = value.item()
        elif isinstance(value, _np.ndarray):
            return ("Time-Series", "series")
    except Exception:
        pass
    try:
        import pandas as _pd
        if isinstance(value, (_pd.DataFrame, _pd.Series)):
            return ("Time-Series", "series")
        if isinstance(value, _pd.Timestamp):
            return ("Date", "date")
    except Exception:
        pass
    import datetime as _dt
    if isinstance(value, bool):
        return ("Text", "boolean")
    if isinstance(value, (int, float)):
        return ("Number", "number")
    if isinstance(value, (_dt.date, _dt.datetime)):
        return ("Date", "date")
    if isinstance(value, (list, tuple)):
        return ("Time-Series", "series")
    if isinstance(value, dict):
        return ("Time-Series", "table")        # a dict -> a one-row table (cols = keys)
    return ("Text", "string")


def _materialize_connector_for_script(session, prop, attrs, cache):
    """A connector-output field's value as a SCRIPT INPUT: the fetched series (a list of
    row dicts) for a Time-Series output, else the scalar value. None if unresolvable.

    The fetch runs HERE, in trusted app code (the same connector path the detail view
    uses) — the sandboxed script only ever receives the already-fetched plain data, never
    network access. Reuses _materialize_connector_series + fetch_api (TTL-cached)."""
    rows = _materialize_connector_series(session, prop, attrs, cache)
    if rows is not None:
        return rows
    connector_name = (prop or {}).get("x-api-connector")
    if not connector_name:
        return None
    connector = cache.get(connector_name)
    if connector is None and connector_name not in cache:
        connector = _load_connector(session, connector_name)
        cache[connector_name] = connector
    if connector is None:
        return None
    cfg, mapped = _apply_nc_map(connector.config or {}, attrs, prop.get("x-nc-map"))
    for entry in (prop.get("x-api-outputs") or [{}]):
        oname = ((entry.get("output") or "").strip() or None) if isinstance(entry, dict) else None
        try:
            result = fetch_api(cfg, mapped, connector_name=connector_name,
                               field_map=prop.get("x-api-map"), output=oname)
        except Exception:
            continue
        if (result or {}).get("kind") == "value":
            return _script_jsonify(result.get("value"))
    return None


def _connector_input_problem(prop, attrs):
    """A human message if a connector field's mapped nc-map inputs (region/dates) are
    PRESENT-but-unparseable in ``attrs`` — None when they look OK. Guards the bridge
    against the connector layer silently substituting a fallback (a widened date window
    on a bad date, an empty series on bad-region JSON) and the script persisting a
    silently-wrong value. Only flags malformed-when-present inputs (an absent mapped
    input legitimately falls back to the connector's own default)."""
    nc = prop.get("x-nc-map") or {}
    shp = nc.get("shapefile")
    if shp and attrs.get(shp) not in (None, ""):
        raw = attrs.get(shp)
        if isinstance(raw, str):
            try:
                json.loads(raw)
            except (ValueError, TypeError):
                return "region field '%s' is not valid GeoJSON" % shp
    import datetime as _dt
    for k in ("start", "end"):
        f = nc.get(k)
        if f and attrs.get(f) not in (None, ""):
            s = str(attrs.get(f)).strip()
            try:
                _dt.date.fromisoformat(s[:10])      # accepts YYYY-MM-DD / ISO datetimes
            except ValueError:
                return "%s date field '%s' = %r is not a valid date" % (k, f, attrs.get(f))
    return None


def _compute_scripts(field_schema, attrs, session=None):
    """Run every Python-script field (x-field == 'script') and store its declared outputs
    in ``attrs``. Runs after _compute_formulas.

    Inputs bind by NAME from the record's other fields. A free var that names a LIVE
    connector-output field (x-api-connector) is MATERIALIZED via the connector layer (the
    same fetch the detail view uses) and bound as its fetched series — so the sandboxed
    script can compute over live NetCDF/REST/THREDDS/etc. data at save time WITHOUT itself
    touching the network (it receives the already-fetched plain data). ``session`` is
    required for that fetch; without it a connector free var is NOT bound at all (the
    script sees a NameError — more conservative than binding None: no fetch, no value).
    A script that errors records ``_<key>_error``; a connector input whose mapped
    region/dates are present-but-unparseable is bound None + flagged in ``_<key>_warning``
    (so a silently-substituted fallback series is never persisted as if correct). Neither
    ever blocks the save."""
    scripts = [(k, p) for k, p in _ordered_props(field_schema)
               if (p or {}).get("x-field") == "script"]
    if not scripts:
        return attrs
    conn_fields = _connector_output_fields(field_schema) if session is not None else {}
    conn_cache = {}
    for key, prop in scripts:
        src = (prop.get("x-script") or "").strip()
        errkey, warnkey = "_%s_error" % key, "_%s_warning" % key
        if not src:
            attrs.pop(errkey, None)
            attrs.pop(warnkey, None)
            continue
        inputs, warnings = {}, []
        for v in _script_free_vars(src):
            if v in conn_fields:                        # a LIVE connector-output field
                problem = _connector_input_problem(conn_fields[v], attrs)
                if problem:
                    # The connector would silently substitute a fallback (e.g. a widened
                    # window) — bind None instead so no wrong value is computed + persisted.
                    inputs[v] = None
                    warnings.append("input '%s': %s" % (v, problem))
                else:
                    inputs[v] = _materialize_connector_for_script(
                        session, conn_fields[v], attrs, conn_cache)
            elif v in attrs:
                inputs[v] = attrs.get(v)
        if warnings:
            attrs[warnkey] = "; ".join(warnings)[:400]
        else:
            attrs.pop(warnkey, None)
        try:
            out = _run_python_script(src, inputs)
        except Exception as exc:
            attrs[errkey] = ("%s: %s" % (type(exc).__name__, exc))[:240]
            continue
        attrs.pop(errkey, None)
        for o in (prop.get("x-script-outputs") or []):
            if not isinstance(o, dict):
                continue
            name = (o.get("name") or "").strip()
            if name and name in out:
                attrs[name] = _coerce_script_output(out[name],
                                                    o.get("field_type") or o.get("type"))
    return attrs


# ===========================================================================
# MODEL RUN -> Tethys Job Manager. A doctype binds to a HydroModel (field_schema
# 'x-model'); a record's "Run" creates a Tethys BasicJob and executes, in a per-run
# workspace: pre Python -> shell command -> post Python, writing the model's typed
# outputs back to the record (filterable in the Data API like script/connector fields).
# PRIVILEGED + TRUSTED: the command + pre/post Python run UNSANDBOXED as the app (the
# pre/post must read/write files) -> a model is SUPERUSER-defined only; running one is
# gated to the doctype's write permission. (BasicJob + a daemon worker thread, since no
# Condor/Dask scheduler is configured; the Job Manager gives status tracking + the Jobs
# table for free.)
# ===========================================================================

def _load_model(session, name):
    """Load a HydroModel by name (the value a doctype's x-model references), or None."""
    if not name:
        return None
    return session.execute(
        select(m.HydroModel).where(m.HydroModel.name == name)).scalar_one_or_none()


class _ModelData:
    """In-process access to HydroDesk data for a model's pre/post Python (TRUSTED). Lets a
    model import its inputs from your data — other doctypes' records, a live connector
    output (NetCDF/REST/WCS series or value) for THIS record, or a raster/coverage/DEM
    file fetched into the run workspace."""

    def __init__(self, slug, record_id):
        self.slug = slug
        self.record_id = record_id

    @staticmethod
    def _eng():
        return App.get_persistent_store_database("hydro_db")

    def records(self, slug, limit=5000, **equals):
        """All records of a doctype as attr dicts (id included). Optional ==field filters,
        e.g. data.records('gage_site', region='Provo')."""
        with Session(self._eng()) as s:
            stmt = select(m.HydroRecord.id, m.HydroRecord.attributes).where(
                m.HydroRecord.hydrotype_slug == slug)
            for k, v in (equals or {}).items():
                stmt = stmt.where(m.HydroRecord.attributes[k].astext == str(v))
            rows = s.execute(stmt.limit(int(limit))).all()
        out = []
        for rid, a in rows:
            d = {k: v for k, v in (a or {}).items() if not k.startswith("_")}
            d["id"] = str(rid)
            out.append(d)
        return out

    def get(self, slug, record_id):
        """One record's attr dict (or None)."""
        import uuid as _uuid
        try:
            rid = _uuid.UUID(str(record_id))
        except (ValueError, TypeError):
            return None
        with Session(self._eng()) as s:
            r = s.execute(select(m.HydroRecord.attributes).where(
                m.HydroRecord.id == rid, m.HydroRecord.hydrotype_slug == slug)).first()
        return ({k: v for k, v in (r[0] or {}).items() if not k.startswith("_")} if r else None)

    def series(self, field):
        """Materialize a connector-output field (x-api-connector) of THIS record's doctype
        LIVE — the parsed NetCDF/REST/WCS series (list of row dicts) or scalar value."""
        import uuid as _uuid
        with Session(self._eng()) as s:
            meta = _load_hydrotype(s, self.slug)
            if not meta:
                return None
            prop = ((meta[1] or {}).get("properties") or {}).get(field) or {}
            if not prop.get("x-api-connector"):
                return None
            try:
                rid = _uuid.UUID(str(self.record_id))
            except (ValueError, TypeError):
                return None
            r = s.execute(select(m.HydroRecord.attributes).where(
                m.HydroRecord.id == rid)).first()
            attrs = dict(r[0]) if r else {}
            return _materialize_connector_for_script(s, prop, attrs, {})

    def fetch(self, url, out_path, timeout=120):
        """Download a URL — a DEM tile, a WCS GetCoverage request, any raster/file — to
        ``out_path`` (under the run workspace) and return the path. TRUSTED network: the
        model author (superuser) constructs the URL, typically from record fields (bbox,
        region, dates)."""
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "HydroDesk-Model/1.0"})
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            data = resp.read()
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path


def _model_namespace(attrs, workspace, slug=None, record_id=None):
    """The TRUSTED namespace for a model's pre/post Python: full stdlib + numpy/pandas +
    file access (by convention scoped to ``workspace``) + a ``data`` accessor over HydroDesk
    data. NOT the sandbox — model authoring is superuser-only, so the pre/post can stage
    input files (incl. fetched DEM/coverage rasters) and parse outputs."""
    import os, io, csv, math, subprocess, pathlib, datetime, shutil, tempfile
    ns = {"record": dict(attrs or {}), "workspace": workspace,
          "data": _ModelData(slug, record_id), "os": os, "io": io,
          "json": json, "csv": csv, "math": math, "re": re, "subprocess": subprocess,
          "pathlib": pathlib, "datetime": datetime, "shutil": shutil, "tempfile": tempfile,
          "open": open, "print": print, "len": len, "range": range, "float": float,
          "int": int, "str": str, "list": list, "dict": dict, "sorted": sorted,
          "sum": sum, "min": min, "max": max, "abs": abs, "round": round, "enumerate": enumerate}
    try:
        import numpy as _np
        ns["np"] = _np; ns["numpy"] = _np
    except Exception:
        pass
    try:
        import pandas as _pd
        ns["pd"] = _pd; ns["pandas"] = _pd
    except Exception:
        pass
    return ns


def _render_model_command(template, attrs, workspace):
    """Substitute {field} (from attrs) and {workspace} in a command template. Field
    values are shlex.quote'd so record data can never break out of the (trusted,
    superuser-authored) command; {workspace} is a path we control."""
    import shlex

    def sub(mo):
        k = mo.group(1)
        if k == "workspace":
            return shlex.quote(workspace)
        v = attrs.get(k)
        return shlex.quote("" if v is None else str(v))

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template or "")


def _write_model_outputs(slug, record_id, updates):
    """Merge a model run's outputs into the record's attributes — a FRESH session (this
    runs in a worker thread)."""
    if not updates:
        return
    import uuid as _uuid
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        rid = record_id if isinstance(record_id, _uuid.UUID) else _uuid.UUID(str(record_id))
        rec = session.execute(
            select(m.HydroRecord).where(m.HydroRecord.id == rid)).scalar_one_or_none()
        if rec is None:
            return
        a = dict(rec.attributes or {})
        a.update(updates)
        rec.attributes = a                 # reassign so JSONB change is tracked
        session.add(rec)
        session.commit()


def _run_model_async(job_id, model_config, attrs, slug, record_id):
    """Worker thread: pre -> command -> post -> write outputs -> job status. Never raises
    out — records ERR on the job and a status_message."""
    import subprocess, tempfile, shutil
    from django.utils import timezone
    from django.db import connections as _dj_conns
    from tethys_compute.models import BasicJob
    try:
        job = BasicJob.objects.get(id=job_id)
    except Exception:
        return
    workspace = tempfile.mkdtemp(prefix="hd_model_")
    job._status = "RUN"
    job.start_time = timezone.now()
    job.save()
    try:
        pre = (model_config.get("pre_script") or "").strip()
        if pre:                                  # 1) stage inputs
            exec(compile(pre, "<hydrodesk-model-pre>", "exec"),
                 _model_namespace(attrs, workspace, slug, record_id))
        proc = None                              # 2) run the command
        cmd = _render_model_command(model_config.get("command") or "", attrs, workspace)
        if cmd.strip():
            timeout = max(1, min(3600, int(model_config.get("timeout") or 300)))
            proc = subprocess.run(cmd, shell=True, cwd=workspace, capture_output=True,
                                  text=True, timeout=timeout)
        post_ns = _model_namespace(attrs, workspace, slug, record_id)   # 3) parse outputs
        post_ns.update({"stdout": (proc.stdout if proc else ""),
                        "stderr": (proc.stderr if proc else ""),
                        "returncode": (proc.returncode if proc else None)})
        post = (model_config.get("post_script") or "").strip()
        if post:
            exec(compile(post, "<hydrodesk-model-post>", "exec"), post_ns)
        updates = {}
        for o in (model_config.get("outputs") or []):
            if not isinstance(o, dict):
                continue
            name = (o.get("name") or "").strip()
            if name and name in post_ns:
                updates[name] = _coerce_script_output(post_ns[name], o.get("field_type"))
        if proc is not None and proc.returncode != 0 and not updates:
            raise RuntimeError("command exited %d: %s"
                               % (proc.returncode, (proc.stderr or "")[:300]))
        _write_model_outputs(slug, record_id, updates)
        job._status = "COM"
        job.completion_time = timezone.now()
        rc = proc.returncode if proc else "n/a"
        job.status_message = ("exit %s; %d output(s) written" % (rc, len(updates)))[:2000]
        job.save()
    except Exception as exc:
        job._status = "ERR"
        job.completion_time = timezone.now()
        job.status_message = ("%s: %s" % (type(exc).__name__, exc))[:2000]
        job.save()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            _dj_conns.close_all()
        except Exception:
            pass


def _start_model_run(slug, record_id, model_name, model_config, attrs, user):
    """Create a Tethys BasicJob + spawn the run worker thread; return the saved job."""
    import threading
    from tethys_compute.models import BasicJob
    jm = App.get_job_manager()
    job = jm.create_job(name=("%s @ %s" % (model_name, str(record_id)[:8]))[:1024],
                        user=user, job_type=BasicJob,
                        description="HydroDesk model '%s' on %s" % (model_name, slug))
    job._status = "PEN"
    job.extended_properties = {"hydrodesk": {"slug": slug, "record_id": str(record_id),
                                             "model": model_name}}
    job.save()
    threading.Thread(target=_run_model_async,
                     args=(job.id, dict(model_config or {}), dict(attrs or {}),
                           slug, str(record_id)),
                     daemon=True).start()
    return job


@controller(name="run_model", url="run-model/{slug}/{record_id}", title="Run Model")
def run_model(request, slug=None, record_id=None):
    """Trigger a doctype's bound model (x-model) for one record -> a BasicJob. Gated to
    the doctype's WRITE permission (running a model mutates the record)."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)
    import uuid as _uuid
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is None:
            return JsonResponse({"ok": False, "error": "unknown doctype"}, status=404)
        _dn, field_schema, _gk = meta
        if not _user_can(request, field_schema, "write"):
            return JsonResponse({"ok": False, "error": "write permission required"}, status=403)
        model_name = (field_schema or {}).get("x-model")
        if not model_name:
            return JsonResponse({"ok": False, "error": "this doctype has no model bound"}, status=400)
        model = _load_model(session, model_name)
        if model is None:
            return JsonResponse({"ok": False, "error": "model '%s' not found" % model_name}, status=404)
        try:
            rid = _uuid.UUID(str(record_id))
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "bad record id"}, status=400)
        rec = session.execute(
            select(m.HydroRecord).where(m.HydroRecord.id == rid)).scalar_one_or_none()
        if rec is None:
            return JsonResponse({"ok": False, "error": "record not found"}, status=404)
        attrs = dict(rec.attributes or {})
        model_config = dict(model.config or {})
    job = _start_model_run(slug, record_id, model_name, model_config, attrs, request.user)
    return JsonResponse({"ok": True, "job_id": job.id, "status": job.status})


@controller(name="model_job_status", url="model-job/{job_id}", title="Model Job Status")
def model_job_status(request, job_id=None):
    """Poll a model run's BasicJob status (for the record's Run badge)."""
    from tethys_compute.models import BasicJob
    try:
        job = BasicJob.objects.get(id=int(job_id))
    except (BasicJob.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "job not found"}, status=404)
    if not (request.user.is_staff or request.user.is_superuser
            or getattr(job, "user_id", None) == request.user.id):
        return JsonResponse({"ok": False, "error": "not authorized"}, status=403)
    status = job.status
    return JsonResponse({"ok": True, "job_id": job.id, "status": status,
                         "message": job.status_message or "",
                         "done": status in ("Complete", "Error", "Aborted")})


def _parse_point(post):
    """Parse and range-check Longitude/Latitude from POST -> (WKTElement, None)
    or (None, message). Axis order is POINT(lon lat), srid 4326 to match the
    geom column. A swapped lon/lat passes float() so ranges are enforced."""
    try:
        lon = float(post["longitude"])
        lat = float(post["latitude"])
    except (KeyError, ValueError, TypeError):
        return None, "Longitude and Latitude are required numbers."
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None, "Longitude must be -180..180 and Latitude -90..90."
    return WKTElement(f"POINT({lon} {lat})", srid=4326), None


def _naming_parts(field_schema):
    """Parse a type's naming series (x-naming, e.g. 'INV-#####') into the target
    field + fixed prefix/suffix + counter width + a matcher. None when there's no
    pattern, no '#' run, or no title field to receive the value."""
    fs = field_schema or {}
    pattern = (fs.get("x-naming") or "").strip()
    field = (fs.get("x-title-field") or "").strip()
    if not pattern or not field:
        return None
    mh = re.search(r"#+", pattern)
    if not mh:
        return None
    return {
        "field": field,
        "prefix": pattern[:mh.start()],
        "suffix": pattern[mh.end():],
        "width": len(mh.group(0)),
        "rx": re.compile("^" + re.escape(pattern[:mh.start()]) + r"(\d+)"
                         + re.escape(pattern[mh.end():]) + "$"),
    }


def _naming_max(session, slug, parts):
    """Highest counter currently used by this type's records for ``parts`` (0 if none)
    — scanned from the title field, so naming is stateless (no counter row)."""
    mx = 0
    rows = session.execute(
        select(m.HydroRecord.attributes)
        .where(m.HydroRecord.hydrotype_slug == slug)
    ).scalars().all()
    for attrs in rows:
        hit = parts["rx"].match(str((attrs or {}).get(parts["field"]) or ""))
        if hit:
            try:
                mx = max(mx, int(hit.group(1)))
            except ValueError:
                pass
    return mx


def _format_name(parts, number):
    """Render a counter into the pattern: prefix + zero-padded number + suffix."""
    return parts["prefix"] + str(number).zfill(parts["width"]) + parts["suffix"]


def _next_name(session, slug, field_schema):
    """The next naming-series value (e.g. 'INV-00001'), or None when the type has no
    naming series. = max(existing) + 1."""
    parts = _naming_parts(field_schema)
    if not parts:
        return None
    return _format_name(parts, _naming_max(session, slug, parts) + 1)


def _form_context(slug, display_name, type_found, widgets, errors, mode,
                  record_id=None, parent=None):
    """Shared context for the create AND edit forms (both render form.html).
    ``mode`` is 'new' or 'edit'; for edit the form posts to the edit URL and
    cancels back to the record's detail. ``parent`` (a {slug,id,field} dict) is set
    when creating a child of a LINKED Table field: the parent context is carried as
    hidden inputs and Cancel/return go back to the parent's detail."""
    is_edit = mode == "edit"
    parent = parent or {}
    has_parent = bool(parent.get("slug") and parent.get("id"))
    list_url = reverse("hydrodesk:list", kwargs={"slug": slug})
    if is_edit:
        form_action = reverse("hydrodesk:edit", kwargs={"slug": slug, "record_id": record_id})
        cancel_url = reverse("hydrodesk:detail", kwargs={"slug": slug, "record_id": record_id})
    else:
        form_action = reverse("hydrodesk:new", kwargs={"slug": slug})
        cancel_url = list_url
    parent_detail_url = ""
    if has_parent:
        parent_detail_url = reverse("hydrodesk:detail",
                                    kwargs={"slug": parent["slug"], "record_id": parent["id"]})
        if not is_edit:
            cancel_url = parent_detail_url     # adding a child -> back to the parent
    return {
        "slug": slug,
        "display_name": display_name,
        "type_found": type_found,
        "widgets": widgets,
        "form_errors": errors,
        "list_url": list_url,
        "mode": mode,
        "form_action": form_action,
        "cancel_url": cancel_url,
        "page_title": ("Edit " if is_edit else "New ") + str(display_name),
        "indicator_label": "Editing" if is_edit else "Not Saved",
        "indicator_class": "blue" if is_edit else "orange",
        # Linked-child context (hidden inputs in form.html; empty when not a child).
        "parent_slug": parent.get("slug", ""),
        "parent_id": parent.get("id", ""),
        "parent_field": parent.get("field", ""),
        "parent_link_field": parent.get("link_field", ""),
        "parent_detail_url": parent_detail_url,
    }


def _parent_ctx(request):
    """Read the linked-child parent context from the request (POST when posting,
    else GET): the parent's slug, record id, and the parent-type FIELD the new
    record links under. All empty for a normal (non-child) create."""
    src = request.POST if request.method == "POST" else request.GET
    return {
        "slug": (src.get("parent_slug") or "").strip(),
        "id": (src.get("parent_id") or "").strip(),
        "field": (src.get("parent_field") or "").strip(),
        # Reverse-link mode: the child's OWN Link field that should be pre-filled
        # with the parent id (instead of stamping a hidden _parent record).
        "link_field": (src.get("parent_link_field") or "").strip(),
    }


def _valid_parent_link(session, parent, child_slug):
    """Confirm the parent context names a real parent record whose ``field`` is a
    LINKED Table (x-child-type) pointing at ``child_slug``. Guards against forging
    a link to an arbitrary record/field. Returns True only when all checks pass."""
    if not (parent.get("slug") and parent.get("id") and parent.get("field")):
        return False
    meta = _load_hydrotype(session, parent["slug"])
    if meta is None:
        return False
    _dn, pschema, _gk = meta
    prop = ((pschema or {}).get("properties") or {}).get(parent["field"]) or {}
    if prop.get("x-child-type") != child_slug:
        return False
    exists = session.execute(
        select(m.HydroRecord.id).where(
            m.HydroRecord.hydrotype_slug == parent["slug"],
            m.HydroRecord.id == parent["id"])
    ).first()
    return exists is not None


@controller(name="new", url="new/{slug}", title="New Record")
def hydrotype_new(request, slug="monitoring_station"):
    """Generic auto-form for creating a HydroRecord of one HydroType.

    GET  -> render an auto-form (one widget per field_schema property, plus
            Longitude/Latitude when the type is a point geometry).
    POST -> coerce -> validate (fastjsonschema) -> parse/range-check geometry ->
            insert a HydroRecord and redirect to the list (Post/Redirect/Get).
            On any error, re-render the form with messages + entered values.
    """
    engine = App.get_persistent_store_database("hydro_db")

    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)

    if meta is None:
        # Unknown type: render a bare (non-spatial) form — never invent a point
        # geometry. A type is spatial only when it explicitly says so.
        display_name, field_schema, geometry_kind = slug, {}, None
        type_found = False
    else:
        display_name, field_schema, geometry_kind = meta
        type_found = True

    if not _user_can(request, field_schema, "write"):
        return _denied(request, "create", display_name)
    geometry_kind = _resolve_geometry_kind(field_schema, geometry_kind)
    parent = _parent_ctx(request)

    if request.method == "POST":
        post = request.POST.dict()
        errors = []

        attributes, coerce_errors = _coerce_attributes(field_schema, post)
        for key, msg in coerce_errors.items():
            errors.append(f"{key} {msg}.")
        # Per-record SHAPEFILE uploads (request.FILES) -> inline GeoJSON in attributes.
        errors.extend(_apply_shapefile_uploads(field_schema, request.FILES, attributes))

        validated = attributes
        if not coerce_errors:
            _compute_formulas(field_schema, attributes)   # fill computed fields
            _compute_scripts(field_schema, attributes, session)  # sandboxed scripts (+ live connector inputs)
            validated, vmsg = _validate_attributes(field_schema, attributes)
            if vmsg:
                errors.append(vmsg)

        geom = None
        if geometry_kind == "point":
            geom, gmsg = _parse_point(post)
            if gmsg:
                errors.append(gmsg)

        if not errors:
            with Session(engine) as session:
                # Stamp the _parent link when this is a valid linked child so the
                # parent's detail can query it back. Validated separately so a forged
                # parent context can never attach a record to an arbitrary parent.
                attrs_to_store = dict(validated)
                # Naming series: fill the title field with the next series value
                # (e.g. INV-00001) when the user left it blank.
                tf = field_schema.get("x-title-field")
                if (field_schema.get("x-naming") and tf
                        and not str(attrs_to_store.get(tf) or "").strip()):
                    nm = _next_name(session, slug, field_schema)
                    if nm:
                        attrs_to_store[tf] = nm
                linked = _valid_parent_link(session, parent, slug)
                if linked:
                    attrs_to_store["_parent"] = {
                        "slug": parent["slug"], "id": parent["id"],
                        "field": parent["field"]}
                record = m.HydroRecord(
                    hydrotype_slug=slug,
                    attributes=attrs_to_store,
                    geom=geom,
                    created_by=getattr(request.user, "username", None),
                )
                session.add(record)
                session.commit()
            # Reverse-link create (the child's own Link field carries the relation):
            # return to the parent detail, where the child now appears in the
            # reverse linked-table.
            reverse_link = bool(parent.get("link_field") and parent.get("id")
                                and parent.get("slug"))
            if linked or reverse_link:  # back to the parent detail
                return redirect(reverse("hydrodesk:detail",
                                        kwargs={"slug": parent["slug"], "record_id": parent["id"]}))
            return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))

        # Re-render with the user's submitted values (including lon/lat) so the
        # form is not cleared on error.
        widgets = _build_widgets(field_schema, geometry_kind, values=post)
        return render(request, "hydrodesk/form.html",
                      _form_context(slug, display_name, type_found, widgets, errors,
                                    "new", parent=parent))

    # GET: blank form. In reverse-link mode, pre-fill the child's own Link field
    # with the parent record id so the new child already points back at it.
    prefill = None
    if parent.get("link_field") and parent.get("id"):
        prefill = {parent["link_field"]: parent["id"]}
    widgets = _build_widgets(field_schema, geometry_kind, values=prefill)
    return render(request, "hydrodesk/form.html",
                  _form_context(slug, display_name, type_found, widgets, [], "new",
                                parent=parent))


def _importable_fields(field_schema):
    """The scalar fields a flat CSV can populate, in declaration order. Skips layout
    markers, API fields (computed/not stored), linked Tables (separate records) and
    inline Tables (not flat-CSV). Returns [(key, prop, title)]."""
    out = []
    for key, prop in _ordered_props(field_schema):
        prop = prop or {}
        if prop.get("x-layout") or prop.get("x-api-connector") or prop.get("x-child-type"):
            continue
        if prop.get("x-field") in ("formula", "script") or prop.get("x-computed"):
            continue                              # computed, never user-provided
        if prop.get("type") == "array" and prop.get("x-widget") == "table":
            continue
        out.append((key, prop, prop.get("title") or key.replace("_", " ").title()))
    return out


@controller(name="import_records", url="import/{slug}", title="Import CSV")
def import_records(request, slug="monitoring_station"):
    """Bulk-create HydroRecords of one HydroType from an uploaded CSV, auto-mapped by
    header name. GET shows the upload form + the expected headers; POST parses the
    file, matches each header to a field (by key / slugified title, case-insensitive),
    coerces + validates each row with the SAME engine as the record form, inserts the
    good rows, and reports per-row errors so a few bad rows never block the good ones.
    Point types read latitude/longitude (or lat/lon) columns for the geometry."""
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
    if meta is None:
        return redirect(reverse("hydrodesk:home"))
    display_name, field_schema, geometry_kind = meta
    if not _user_can(request, field_schema, "write"):
        return _denied(request, "import", display_name)
    geometry_kind = _resolve_geometry_kind(field_schema, geometry_kind)
    importable = _importable_fields(field_schema)
    required = (field_schema or {}).get("required") or []

    ctx = {
        "slug": slug,
        "display_name": display_name,
        "list_url": reverse("hydrodesk:list", kwargs={"slug": slug}),
        "form_action": reverse("hydrodesk:import_records", kwargs={"slug": slug}),
        "is_point": geometry_kind == "point",
        "fields": [{"key": k, "title": t, "required": k in required}
                   for k, _p, t in importable],
        "done": False,
    }

    if request.method != "POST":
        return render(request, "hydrodesk/import_records.html", ctx)

    upload = request.FILES.get("csv_file")
    if upload is None:
        ctx["error"] = "Choose a CSV file to import."
        return render(request, "hydrodesk/import_records.html", ctx)

    import csv as _csvmod
    import io
    try:
        text = upload.read().decode("utf-8-sig", "replace")
    except Exception:
        ctx["error"] = "Could not read the file as UTF-8 text."
        return render(request, "hydrodesk/import_records.html", ctx)

    reader = _csvmod.DictReader(io.StringIO(text), skipinitialspace=True)
    headers = reader.fieldnames or []
    if not headers:
        ctx["error"] = "The CSV has no header row."
        return render(request, "hydrodesk/import_records.html", ctx)

    # Map each importable field -> a CSV header (by field key or slugified title).
    norm = {}
    for h in headers:
        norm.setdefault(_slugify_underscore(h or ""), h)
    col_for = {}
    for key, prop, title in importable:
        for cand in (_slugify_underscore(key), _slugify_underscore(title)):
            if cand in norm:
                col_for[key] = norm[cand]
                break
    lat_col = next((norm[c] for c in ("latitude", "lat") if c in norm), None)
    lon_col = next((norm[c] for c in ("longitude", "lon", "lng") if c in norm), None)
    matched = sorted(set(col_for.values()))
    ignored = [h for h in headers if h not in matched and h not in (lat_col, lon_col)]

    created, row_errors = 0, []
    MAX_ROWS = 5000
    with Session(engine) as session:
        # Naming series: number imported rows sequentially from the current max, so a
        # batch gets INV-00001, INV-00002, … (the in-memory counter avoids re-scanning
        # for each row, and uncommitted rows aren't yet queryable).
        name_parts = _naming_parts(field_schema)
        name_counter = _naming_max(session, slug, name_parts) if name_parts else 0
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            if (i - 1) > MAX_ROWS:
                row_errors.append({"row": i, "msg": f"stopped at the {MAX_ROWS}-row limit"})
                break
            # Build a form-like post dict from the mapped columns; a boolean field is
            # True only when its cell is truthy (presence => True in _coerce_attributes).
            row_post = {}
            for key, prop, _t in importable:
                col = col_for.get(key)
                if col is None:
                    continue
                cell = (raw.get(col) or "").strip()
                if prop.get("type") == "boolean":
                    if cell.lower() in ("true", "1", "yes", "y", "t", "on"):
                        row_post[key] = "on"
                elif cell != "":
                    row_post[key] = cell
            attributes, coerce_errors = _coerce_attributes(field_schema, row_post)
            if coerce_errors:
                row_errors.append({"row": i, "msg": "; ".join(
                    f"{k} {v}" for k, v in coerce_errors.items())})
                continue
            _compute_formulas(field_schema, attributes)   # fill computed fields
            _compute_scripts(field_schema, attributes, session)  # sandboxed scripts (+ live connector inputs)
            validated, vmsg = _validate_attributes(field_schema, attributes)
            if vmsg:
                row_errors.append({"row": i, "msg": vmsg})
                continue
            geom = None
            if geometry_kind == "point":
                geom, gmsg = _parse_point({
                    "longitude": (raw.get(lon_col) or "") if lon_col else "",
                    "latitude": (raw.get(lat_col) or "") if lat_col else ""})
                if gmsg:
                    row_errors.append({"row": i, "msg": gmsg})
                    continue
            attrs_to_store = dict(validated)
            if name_parts and not str(attrs_to_store.get(name_parts["field"]) or "").strip():
                name_counter += 1
                attrs_to_store[name_parts["field"]] = _format_name(name_parts, name_counter)
            session.add(m.HydroRecord(
                hydrotype_slug=slug, attributes=attrs_to_store, geom=geom,
                created_by=getattr(request.user, "username", None)))
            created += 1
        session.commit()

    ctx.update({
        "done": True,
        "created": created,
        "error_count": len(row_errors),
        "row_errors": row_errors[:200],
        "matched_columns": matched,
        "ignored_columns": ignored,
        "point_mapped": bool(geometry_kind == "point" and lat_col and lon_col),
        "point_missing": bool(geometry_kind == "point" and not (lat_col and lon_col)),
    })
    return render(request, "hydrodesk/import_records.html", ctx)


@controller(name="edit", url="record/{slug}/{record_id}/edit", title="Edit Record")
def hydrotype_edit(request, slug="monitoring_station", record_id=None):
    """Edit an existing HydroRecord. GET pre-fills the form from the record;
    POST validates and UPDATEs the row in place, then redirects to the detail."""
    engine = App.get_persistent_store_database("hydro_db")

    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
    if meta is None:
        display_name, field_schema, geometry_kind = slug, {}, None
        type_found = False
    else:
        display_name, field_schema, geometry_kind = meta
        type_found = True
    if not _user_can(request, field_schema, "write"):
        return _denied(request, "edit", display_name)
    geometry_kind = _resolve_geometry_kind(field_schema, geometry_kind)
    detail_url = reverse("hydrodesk:detail", kwargs={"slug": slug, "record_id": record_id})
    list_url = reverse("hydrodesk:list", kwargs={"slug": slug})

    if request.method == "POST":
        post = request.POST.dict()
        errors = []
        # Current stored attributes (to preserve an uploaded shapefile when this edit
        # doesn't re-upload one — a file input never pre-fills).
        with Session(engine) as session:
            existing_attrs = session.execute(
                select(m.HydroRecord.attributes)
                .where(m.HydroRecord.hydrotype_slug == slug)
                .where(m.HydroRecord.id == record_id)
            ).scalar_one_or_none() or {}
        attributes, coerce_errors = _coerce_attributes(field_schema, post)
        for key, msg in coerce_errors.items():
            errors.append(f"{key} {msg}.")
        errors.extend(_apply_shapefile_uploads(field_schema, request.FILES, attributes,
                                               existing=existing_attrs))
        validated = attributes
        if not coerce_errors:
            _compute_formulas(field_schema, attributes)   # fill computed fields
            _compute_scripts(field_schema, attributes, session)  # sandboxed scripts (+ live connector inputs)
            validated, vmsg = _validate_attributes(field_schema, attributes)
            if vmsg:
                errors.append(vmsg)
        geom, gmsg = (None, None)
        if geometry_kind == "point":
            geom, gmsg = _parse_point(post)
            if gmsg:
                errors.append(gmsg)
        if not errors:
            with Session(engine) as session:
                rec = session.execute(
                    select(m.HydroRecord)
                    .where(m.HydroRecord.hydrotype_slug == slug)
                    .where(m.HydroRecord.id == record_id)
                ).scalar_one_or_none()
                if rec is None:
                    return redirect(list_url)
                # Preserve reserved keys (e.g. the _parent link) that aren't part of
                # the editable schema, so editing a child never unlinks it.
                preserved = {k: v for k, v in (rec.attributes or {}).items()
                             if k.startswith("_")}
                rec.attributes = {**validated, **preserved}
                if geometry_kind == "point":
                    rec.geom = geom
                session.commit()
            return redirect(detail_url)
        widgets = _build_widgets(field_schema, geometry_kind, values=post)
        return render(request, "hydrodesk/form.html",
                      _form_context(slug, display_name, type_found, widgets, errors, "edit", record_id))

    # GET: load the record and pre-fill the widgets.
    with Session(engine) as session:
        row = session.execute(
            select(
                m.HydroRecord.attributes,
                # ST_PointOnSurface (not ST_X/ST_Y on the raw geom — those raise on a
                # polygon/line) -> a representative lon/lat guaranteed ON the geometry,
                # valid for ANY type so editing a polygon/line record doesn't 500.
                func.ST_X(func.ST_PointOnSurface(m.HydroRecord.geom)),
                func.ST_Y(func.ST_PointOnSurface(m.HydroRecord.geom)),
            )
            .where(m.HydroRecord.hydrotype_slug == slug)
            .where(m.HydroRecord.id == record_id)
        ).first()
    if row is None:
        return redirect(list_url)
    values = dict(row[0] or {})
    if row[1] is not None and row[2] is not None:
        values["longitude"], values["latitude"] = row[1], row[2]
    widgets = _build_widgets(field_schema, geometry_kind, values=values)
    return render(request, "hydrodesk/form.html",
                  _form_context(slug, display_name, type_found, widgets, [], "edit", record_id))


@controller(name="delete", url="record/{slug}/{record_id}/delete", title="Delete Record")
def hydrotype_delete(request, slug="monitoring_station", record_id=None):
    """Delete a HydroRecord (POST only), then redirect to the list."""
    if request.method == "POST":
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            meta = _load_hydrotype(session, slug)
            if meta is not None and not _user_can(request, meta[1], "write"):
                return _denied(request, "delete", meta[0])
            rec = session.execute(
                select(m.HydroRecord)
                .where(m.HydroRecord.hydrotype_slug == slug)
                .where(m.HydroRecord.id == record_id)
            ).scalar_one_or_none()
            if rec is not None:
                session.delete(rec)
                session.commit()
    return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))


@controller(name="records_bulk_delete", url="list/{slug}/bulk-delete",
            title="Delete Records")
def records_bulk_delete(request, slug="monitoring_station"):
    """Bulk-delete the SELECTED HydroRecords of one type (POST only), then redirect
    back to the list. The list view posts the ticked rows as repeated ``ids`` form
    fields; each is validated as a UUID (malformed ids are dropped) and only records
    of THIS slug are removed (a forged id from another type can't be touched).
    Records of OTHER types are never affected (orphans an _parent link at most)."""
    if request.method == "POST":
        ids = []
        for raw in request.POST.getlist("ids"):
            try:
                ids.append(uuidlib.UUID(str(raw)))
            except (ValueError, AttributeError, TypeError):
                continue
        if ids:
            engine = App.get_persistent_store_database("hydro_db")
            with Session(engine) as session:
                session.execute(delete(m.HydroRecord).where(
                    m.HydroRecord.hydrotype_slug == slug,
                    m.HydroRecord.id.in_(ids)))
                session.commit()
    return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))


def _format_typed_cell(prop, value, session=None):
    """Render one attribute value with type awareness, returning either a plain
    string (auto-escaped by the template) OR a safe-HTML anchor.

    - Link (x-link-type): resolve the stored UUID -> the target record's label
      and emit a clickable <a> to that record's detail (safe HTML).
    - Email (format:email): a mailto: link.
    - URL (format:uri): a clickable link.
    Anything else falls through to the plain _format_cell string. ``session`` is
    used only to resolve a Link label; without it a Link shows the short uuid.
    """
    prop = prop or {}
    if value in (None, ""):
        return _format_cell(value)
    link_target = prop.get("x-link-type")
    if link_target:
        uuid = str(value)
        label = uuid[:8]
        if session is not None:
            resolved = _resolve_link_labels(session, link_target, [uuid])
            label = resolved.get(uuid, label)
        return _link_anchor(link_target, uuid, label)
    fmt = prop.get("format")
    if fmt == "email":
        return format_html('<a href="mailto:{0}">{0}</a>', str(value))
    if fmt == "uri":
        return format_html('<a href="{0}" target="_blank" rel="noopener">{0}</a>', str(value))
    xfield = prop.get("x-field")
    if xfield == "currency":
        return _fmt_currency(value)
    if xfield == "percent":
        return _fmt_percent(value)
    if xfield == "duration":
        return _fmt_duration(value)
    if xfield == "phone":
        dial = re.sub(r"[^0-9+]", "", str(value))
        return format_html('<a href="tel:{}">{}</a>', dial, str(value))
    if xfield == "color":
        return format_html(
            '<span style="display:inline-block;width:12px;height:12px;'
            'border:1px solid #ccc;border-radius:2px;background:{};'
            'vertical-align:middle;margin-right:6px;"></span>{}',
            str(value), str(value))
    if xfield == "rating":
        return _fmt_rating(value, prop.get("maximum") or 5)
    if xfield == "shapefile":
        n = _geojson_feature_count(value)
        return format_html('<span class="frappe-muted"><i class="bi bi-bounding-box"></i> '
                           'shapefile &mdash; {} polygon{}</span>',
                           n, "" if n == 1 else "s")
    return _format_cell(value)


def _fmt_currency(value, symbol="$"):
    """A number -> '$1,234.56' (thousands-grouped, 2 dp). Non-numeric -> plain."""
    try:
        return "%s%s" % (symbol, "{:,.2f}".format(float(value)))
    except (TypeError, ValueError):
        return _format_cell(value)


def _fmt_percent(value):
    """A number -> '45%' / '45.5%' (trailing-zero-trimmed)."""
    try:
        return "%s%%" % ("%g" % float(value))
    except (TypeError, ValueError):
        return _format_cell(value)


def _fmt_duration(value):
    """A number of SECONDS -> a compact '2d 3h 30m 15s' (largest non-zero units)."""
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return _format_cell(value)
    neg = total < 0
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if mins:
        parts.append("%dm" % mins)
    if secs or not parts:
        parts.append("%ds" % secs)
    return ("-" if neg else "") + " ".join(parts)


def _fmt_rating(value, maximum=5):
    """An integer 0..maximum -> filled/empty stars (plain text, auto-escaped)."""
    try:
        mx = int(maximum or 5)
        n = max(0, min(int(round(float(value))), mx))
    except (TypeError, ValueError):
        return _format_cell(value)
    return "★" * n + "☆" * (mx - n)


def _render_table_field(prop, value):
    """Render a Table (child-grid) field's rows as a read-only Frappe DataTable.

    Columns come from the item schema (in declaration order); rows from the stored
    list of objects. Reuses the .hd-series-table look. Booleans show a check/dash,
    lists join with commas, empties show an em-dash. Every header/cell is escaped
    (stored attribute data is user content)."""
    prop = prop or {}
    cols = [(ck, (cp or {}).get("title") or ck.replace("_", " ").title(),
             (cp or {}).get("type")) for ck, cp in _table_item_columns(prop)]
    rows = value if isinstance(value, list) else []
    if not cols:
        return _format_cell(value)
    if not rows:
        return mark_safe("<span class='frappe-muted frappe-text-sm'>no rows</span>")

    th = "".join(str(format_html("<th>{}</th>", h)) for _, h, _ in cols)
    body = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = ""
        for ck, _, ct in cols:
            cv = row.get(ck)
            if ct == "boolean":
                disp = "✓" if cv else "—"
            elif cv in (None, ""):
                disp = "—"
            elif ct in ("number", "integer") and isinstance(cv, (int, float)) \
                    and not isinstance(cv, bool):
                disp = "%g" % cv          # 7490.0 -> 7490, 4.21 -> 4.21
            elif isinstance(cv, (list, tuple)):
                disp = ", ".join(str(x) for x in cv)
            else:
                disp = cv
            cells += str(format_html("<td>{}</td>", str(disp)))
        body += "<tr>" + cells + "</tr>"
    n = len([r for r in rows if isinstance(r, dict)])
    foot = str(format_html("<div class='hd-series-foot'>{} row{}</div>",
                           n, "" if n == 1 else "s"))
    table = ("<table class='hd-series-table'><thead><tr>" + th
             + "</tr></thead><tbody>" + body + "</tbody></table>")
    return mark_safe("<div class='hd-series-wrap'><div class='hd-series-scroll'>"
                     + table + "</div>" + foot + "</div>")


def _child_records(session, child_slug, parent_id, field):
    """Return the child HydroRecords linked to (parent_id, field) for a LINKED Table
    field, oldest-first. Children carry ``attributes['_parent'] = {slug,id,field}``;
    we filter on the JSONB id + field so a child only shows under its own parent
    field (a type can be a child of several different parent fields)."""
    if not (session and child_slug and parent_id):
        return []
    pid = str(parent_id)
    return list(session.execute(
        select(m.HydroRecord).where(
            m.HydroRecord.hydrotype_slug == child_slug,
            m.HydroRecord.attributes["_parent"]["id"].astext == pid,
            m.HydroRecord.attributes["_parent"]["field"].astext == field,
        ).order_by(m.HydroRecord.created_at)
    ).scalars().all())


def _child_records_by_link(session, child_slug, link_field, parent_id):
    """Return the records of ``child_slug`` whose LINK field ``link_field`` points at
    ``parent_id`` (the REVERSE of a Link field — e.g. every Sales Invoice whose
    'customer' equals this Customer). Oldest-first."""
    if not (session and child_slug and link_field and parent_id):
        return []
    pid = str(parent_id)
    return list(session.execute(
        select(m.HydroRecord).where(
            m.HydroRecord.hydrotype_slug == child_slug,
            m.HydroRecord.attributes[link_field].astext == pid,
        ).order_by(m.HydroRecord.created_at)
    ).scalars().all())


def _render_linked_table(session, child_slug, parent_slug, parent_id, field,
                         child_link=None):
    """Render a LINKED Table field as a Frappe table of child records (columns = the
    child type's fields, first cell links to each child's detail).

    Two relationship modes:
      * ``child_link`` set -> REVERSE of a Link field: show records of ``child_slug``
        whose Link field ``child_link`` equals this record (e.g. a Customer's
        invoices). '+ Add' pre-selects that Link to this record.
      * else -> the _parent-owned model: children created via '+ Add' carry a
        ``_parent`` back-reference (a true private one-to-many)."""
    meta = _load_hydrotype(session, child_slug) if session else None
    if meta is None:
        return format_html("<span class='frappe-muted frappe-text-sm'>Linked type "
                           "&ldquo;{}&rdquo; not found.</span>", child_slug or "")
    child_name, child_schema, _gk = meta

    add_btn = ""
    if parent_id:
        params = {"parent_slug": parent_slug or "", "parent_id": str(parent_id),
                  "parent_field": field or ""}
        if child_link:
            params["parent_link_field"] = child_link
        add_url = reverse("hydrodesk:new", kwargs={"slug": child_slug}) + "?" \
            + urllib.parse.urlencode(params)
        link_url = reverse("hydrodesk:link_existing", kwargs={
            "parent_slug": parent_slug, "parent_id": str(parent_id), "field": field})
        add_btn = str(format_html(
            "<div style='margin-top:6px;display:flex;gap:6px;'>"
            "<a class='btn btn-default btn-sm' href='{}'>"
            "<i class='bi bi-plus-lg'></i> Add {}</a>"
            "<a class='btn btn-default btn-sm' href='{}'>"
            "<i class='bi bi-link-45deg'></i> Link existing</a></div>",
            add_url, child_name, link_url))

    if child_link:
        children = _child_records_by_link(session, child_slug, child_link, parent_id)
    else:
        children = _child_records(session, child_slug, parent_id, field)
    cols = _derive_columns(child_schema)  # [(key, header)]
    if not children:
        return mark_safe(
            "<div class='hd-series-wrap' style='padding:9px 12px;'>"
            + str(format_html("<span class='frappe-muted frappe-text-sm'>"
                              "No {} records yet.</span>", child_name))
            + "</div>" + add_btn)

    th = "".join(str(format_html("<th>{}</th>", h)) for _, h in cols) or "<th>Record</th>"
    body = ""
    for rec in children:
        cattrs = rec.attributes or {}
        detail = reverse("hydrodesk:detail",
                         kwargs={"slug": child_slug, "record_id": str(rec.id)})
        if cols:
            cells = ""
            for i, (ck, _h) in enumerate(cols):
                raw = _format_cell(cattrs.get(ck))
                if i == 0:
                    cell = format_html("<a href='{}'>{}</a>", detail,
                                       raw or str(rec.id)[:8])
                else:
                    cell = format_html("{}", raw)
                cells += "<td>" + str(cell) + "</td>"
        else:
            cells = str(format_html("<td><a href='{}'>{}</a></td>",
                                    detail, str(rec.id)[:8]))
        body += "<tr>" + cells + "</tr>"
    n = len(children)
    foot = str(format_html("<div class='hd-series-foot'>{} {} record{}</div>",
                           n, child_name, "" if n == 1 else "s"))
    table = ("<table class='hd-series-table'><thead><tr>" + th
             + "</tr></thead><tbody>" + body + "</tbody></table>")
    return mark_safe("<div class='hd-series-wrap'><div class='hd-series-scroll'>"
                     + table + "</div>" + foot + "</div>" + add_btn)


@controller(name="link_existing",
            url="link-existing/{parent_slug}/{parent_id}/{field}",
            title="Link existing")
def link_existing(request, parent_slug=None, parent_id=None, field=None):
    """Attach an EXISTING child record to a parent's linked Table (vs. '+ New').
    GET lists candidate records of the child type (those not already linked here);
    POST re-points the chosen child at this parent — by setting its Link field
    (reverse-link mode) or its hidden ``_parent`` (the _parent-owned mode) — then
    returns to the parent detail. Gated by WRITE on the CHILD type (it edits the
    child record)."""
    engine = App.get_persistent_store_database("hydro_db")
    home = redirect(reverse("hydrodesk:home"))
    back = redirect(reverse("hydrodesk:detail",
                            kwargs={"slug": parent_slug, "record_id": parent_id}))
    with Session(engine) as session:
        pmeta = _load_hydrotype(session, parent_slug)
        if pmeta is None:
            return home
        p_display, p_schema, _gk = pmeta
        prop = ((p_schema or {}).get("properties") or {}).get(field) or {}
        child_slug = prop.get("x-child-type")
        child_link = prop.get("x-child-link")
        if not child_slug:   # not a linked Table field -> nothing to attach
            return back
        cmeta = _load_hydrotype(session, child_slug)
        if cmeta is None:
            return back
        c_display, c_schema, _cgk = cmeta
        if not _user_can(request, c_schema, "write"):
            return _denied(request, "link", c_display)

        if request.method == "POST":
            child_id = (request.POST.get("child_id") or "").strip()
            rec = session.execute(
                select(m.HydroRecord).where(
                    m.HydroRecord.hydrotype_slug == child_slug,
                    m.HydroRecord.id == child_id)
            ).scalar_one_or_none() if child_id else None
            if rec is not None:
                attrs = dict(rec.attributes or {})
                if child_link:
                    attrs[child_link] = str(parent_id)            # reverse-link mode
                else:
                    attrs["_parent"] = {"slug": parent_slug,      # _parent-owned mode
                                        "id": str(parent_id), "field": field}
                rec.attributes = attrs
                session.add(rec)
                session.commit()
            return back

        # GET: candidate records = all of the child type NOT already linked here.
        if child_link:
            already = _child_records_by_link(session, child_slug, child_link, parent_id)
        else:
            already = _child_records(session, child_slug, parent_id, field)
        already_ids = {str(r.id) for r in already}
        options = [{"id": rid, "label": lab}
                   for rid, lab in _link_options(session, child_slug)
                   if rid not in already_ids]
        parent_label = _label_for(p_schema, _record_attrs(session, parent_slug, parent_id)) \
            or str(parent_id)[:8]
        new_params = {"parent_slug": parent_slug or "", "parent_id": str(parent_id),
                      "parent_field": field or ""}
        if child_link:
            new_params["parent_link_field"] = child_link
        return render(request, "hydrodesk/link_existing.html", {
            "parent_display": p_display,
            "parent_label": parent_label,
            "child_display": c_display,
            "field": field,
            "options": options,
            "form_action": reverse("hydrodesk:link_existing", kwargs={
                "parent_slug": parent_slug, "parent_id": parent_id, "field": field}),
            "parent_detail_url": reverse("hydrodesk:detail", kwargs={
                "slug": parent_slug, "record_id": parent_id}),
            "new_url": reverse("hydrodesk:new", kwargs={"slug": child_slug})
            + "?" + urllib.parse.urlencode(new_params),
        })


def _record_attrs(session, slug, record_id):
    """The attributes dict of one record (or {}) — for a label lookup."""
    row = session.execute(
        select(m.HydroRecord.attributes).where(
            m.HydroRecord.hydrotype_slug == slug,
            m.HydroRecord.id == record_id)
    ).first()
    return (row[0] if row else {}) or {}


def _load_connector(session, connector_name):
    """Load a HydroConnector row by NAME (the value carried in x-api-connector), or
    None. The connector references its credential by name too, so no secret is
    touched here — fetch_api resolves the secret lazily at request time."""
    if not connector_name:
        return None
    return session.execute(
        select(m.HydroConnector)
        .where(m.HydroConnector.name == connector_name)
    ).scalar_one_or_none()


def _bust_api_cache_for_field(field_schema, field_name, attrs, session):
    """Evict the _API_CACHE entry for one API field's resolved URL so the next
    fetch_api re-hits the network. Resolves the connector + rebuilds the same URL
    fetch_api would, then drops (connector_name, url) from the cache. Safe no-op if
    the field/connector/url cannot be resolved.

    CRITICAL: this MUST resolve inputs the SAME way fetch_api does (via the shared
    ``_resolve_inputs`` helper, applying the field's x-api-map) or the rebuilt URL
    won't match the cache key (name, url) and Refresh silently no-ops."""
    props = (field_schema or {}).get("properties") or {}
    prop = props.get(field_name) or {}
    connector_name = prop.get("x-api-connector")
    if not connector_name:
        return
    connector = _load_connector(session, connector_name)
    if connector is None:
        return
    cfg = connector.config or {}
    # Resolve the inputs exactly like fetch_api (with this field's x-api-map) so the
    # rebuilt {token} substitution — and therefore the cache key URL — matches.
    if cfg.get("inputs"):
        resolved, _missing = _resolve_inputs(cfg, attrs, prop.get("x-api-map"))
    else:
        resolved = dict(attrs or {})
    try:
        url = _build_url(cfg.get("url_template") or "", dict(cfg.get("query") or {}),
                         resolved)
    except Exception:
        return
    _API_CACHE.pop((connector_name, url), None)


def _render_series_block(xs, ys):
    """Render a series (x=time, y=value) as an inline SVG sparkline + a compact
    last-N table. This IS the inline chart; a series is NEVER shown as 20+ rows
    (the table is capped at the last 8 points). Every cell is escaped (external
    content). Returns a safe HTML string."""
    spark = _sparkline_svg(ys)
    n = min(8, len(xs))
    rows_html = ""
    for x, y in list(zip(xs, ys))[-n:]:
        rows_html += format_html(
            "<tr><td style='padding:1px 10px 1px 0;color:var(--fr-text-muted);'>{}</td>"
            "<td style='padding:1px 0;'>{}</td></tr>", str(x), str(y))
    table = mark_safe(
        "<table class='frappe-text-sm' style='border-collapse:collapse;margin-top:6px;'>"
        + rows_html + "</table>") if rows_html else mark_safe(
        "<span class='frappe-muted frappe-text-sm'>no series data</span>")
    return spark + "<div>" + str(table) + "</div>"


def _render_columns_table(result, cap=12):
    """Render a multi-variable series as ONE table whose column headers ARE the
    variable names (the user's "click a node -> all its variables as columns").

    ``result`` is a series result from _extract_output: ``columns:[{name, values}]``
    aligned by row index. Shows the latest ``cap`` rows (newest last) with a muted
    "showing latest N of M" note when truncated; USGS no-data sentinels render as an
    em dash. A leading sparkline of the 'value'-ish column gives an at-a-glance
    shape. Every header and cell is escaped (untrusted external content)."""
    columns = (result or {}).get("columns") or []
    # Legacy fallback: a result with only x/y -> synthesize two columns.
    if not columns and (result or {}).get("x") is not None:
        columns = [{"name": "time", "values": result.get("x") or []},
                   {"name": "value", "values": result.get("y") or []}]
    n = max((len(c.get("values") or []) for c in columns), default=0)
    if not columns or n == 0:
        return mark_safe("<span class='frappe-muted frappe-text-sm'>no series data</span>")

    # Sparkline over the 'value'-named column (or the 2nd, or the 1st numeric).
    spark_idx = next((i for i, c in enumerate(columns)
                      if (c.get("name") or "").lower() == "value"),
                     1 if len(columns) > 1 else 0)
    spark = ("<div class='hd-series-spark'>"
             + _sparkline_svg(columns[spark_idx].get("values") or []) + "</div>")

    th = "".join(str(format_html("<th>{}</th>", c.get("name") or ""))
                 for c in columns)
    start = max(0, n - cap)
    body = ""
    for i in range(start, n):
        cells = ""
        for c in columns:
            vals = c.get("values") or []
            v = vals[i] if i < len(vals) else None
            disp = "—" if (v in _NO_DATA or v == -999999) else v
            cells += str(format_html("<td>{}</td>", str(disp)))
        body += "<tr>" + cells + "</tr>"
    foot = ""
    if start > 0:
        foot = str(format_html(
            "<div class='hd-series-foot'>showing latest {} of {} rows</div>",
            n - start, n))
    # The source was capped on read (e.g. a CSV past max_rows): say so explicitly so
    # the "of N" count is never mistaken for the whole file.
    if (result or {}).get("truncated"):
        foot += str(format_html(
            "<div class='hd-series-foot'>source truncated to the first {} rows</div>",
            n))
    table = ("<table class='hd-series-table'><thead><tr>" + th
             + "</tr></thead><tbody>" + body + "</tbody></table>")
    return mark_safe(
        spark + "<div class='hd-series-wrap'><div class='hd-series-scroll'>"
        + table + "</div>" + foot + "</div>")


def _render_value_block(val, field_type="Text"):
    """Render a scalar output by its doctype field_type (Number/Text/Date) as a
    safe formatted span. Number tries a numeric format; Date/Text show the value
    verbatim. The value is always escaped via format_html (external content)."""
    if val in (None, ""):
        return str(format_html(
            "<span class='frappe-muted frappe-text-sm'>no value</span>"))
    shown = val
    if (field_type or "").lower() == "number":
        try:
            f = float(val)
            shown = ("%g" % f)
        except (TypeError, ValueError):
            shown = val
    return str(format_html("<span style='font-weight:600;'>{}</span>", str(shown)))


def _render_image_block(result, label="map"):
    """Render an 'image' output (e.g. a WMS GetMap) as a lazy <img> linking to the
    full image. The URL is built from connector config + record attrs; format_html
    escapes it in both the href and src attributes (no injection)."""
    url = (result or {}).get("url") or ""
    if not url:
        return mark_safe(
            "<span class='frappe-muted frappe-text-sm'>no map (set a point / layer)</span>")
    return format_html(
        "<div class='hd-api-image'>"
        "<a href='{}' target='_blank' rel='noopener'>"
        "<img src='{}' alt='{}' loading='lazy' "
        "style='max-width:100%;height:auto;display:block;"
        "border:1px solid var(--fr-border-color,#d1d8dd);border-radius:6px;'>"
        "</a></div>",
        url, url, label or "map")


# Connector kinds that accept a per-record parameter mapping (x-nc-map): a record's
# Shapefile field -> the region, and Date fields -> the time window. NetCDF/THREDDS,
# Earth Engine, and WCS are region+time grids; REST gets the region as a {bbox} token
# and the window as a {datetime} token (for OGC API Features / any bbox+datetime API).
_RECORD_PARAM_KINDS = ("netcdf", "thredds", "gee", "wcs", "rest")


def _inject_rest_spatiotemporal(cfg, attrs):
    """For a per-record REST connector, expose two URL tokens computed from the record:
    ``{bbox}``  = the shapefile's bounding box 'minlon,minlat,maxlon,maxlat' (OGC order),
    ``{datetime}`` = the date window as an RFC3339 interval 'start/end' (open-ended with
    '..'). Only fills a token when the source resolves; never overwrites with blanks."""
    attrs = dict(attrs or {})
    rings = _shapefile_union_rings(cfg, attrs)
    if rings:
        allx = [p[0] for r in rings for p in r]
        ally = [p[1] for r in rings for p in r]
        attrs["bbox"] = "%g,%g,%g,%g" % (min(allx), min(ally), max(allx), max(ally))
    start = _resolve_date(cfg.get("time_start"), attrs)
    end = _resolve_date(cfg.get("time_end"), attrs)
    if start and end and start > end:
        start, end = end, start
    if start or end:
        attrs["datetime"] = "%sT00:00:00Z/%sT23:59:59Z" % (start or "..", end or "..")
    return attrs


def _supports_record_params(cfg):
    """True when a connector CAN take a per-record shapefile/date mapping — its kind is
    a region+time source AND it is configured to use one (spatial='shapefile' or a date
    range). Drives the builder mapping section, the x-nc-map gate, and the params bar."""
    cfg = cfg or {}
    if (cfg.get("kind") or "rest").lower() not in _RECORD_PARAM_KINDS:
        return False
    return ((cfg.get("spatial") or "").lower() == "shapefile"
            or (cfg.get("time_source") or "").lower() == "range")


def _apply_nc_map(cfg, attrs, nc_map):
    """Apply an API field's per-record parameter mapping (x-nc-map) for ANY region+time
    connector kind. Returns (cfg, attrs) where the record's mapped Shapefile field is
    exposed as _shapefile (the spatial source every kind reads) and the connector's date
    window is pointed at the record's mapped start/end date fields. No-op when empty."""
    nc_map = nc_map or {}
    if not nc_map:
        return cfg, attrs
    cfg = dict(cfg or {})
    attrs = dict(attrs or {})
    if nc_map.get("shapefile"):
        attrs["_shapefile"] = attrs.get(nc_map["shapefile"]) or attrs.get("_shapefile")
    if nc_map.get("start"):
        cfg["time_source"] = "range"
        cfg["time_start"] = "{%s}" % nc_map["start"]
    if nc_map.get("end"):
        cfg["time_source"] = "range"
        cfg["time_end"] = "{%s}" % nc_map["end"]
    return cfg, attrs


def _nc_params_bar(slug, record_id, field, nc_map, attrs):
    """A small EDIT-IN-PLACE bar shown above a netcdf API field's table: the active
    region (polygon count) + date window, and an 'Adjust & refresh' button that opens
    the params modal (data-* carry the field's current values + the update URL)."""
    nc_map = nc_map or {}
    if not (slug and record_id and nc_map):
        return ""
    try:
        url = reverse("hydrodesk:api_params",
                      kwargs={"slug": slug, "record_id": record_id, "field": field})
    except Exception:
        return ""
    start_f, end_f, shp_f = nc_map.get("start"), nc_map.get("end"), nc_map.get("shapefile")
    start_v = str((attrs or {}).get(start_f) or "") if start_f else ""
    end_v = str((attrs or {}).get(end_f) or "") if end_f else ""
    n = _geojson_feature_count((attrs or {}).get(shp_f)) if shp_f else 0
    summary = []
    if shp_f:
        summary.append("region: %d polygon%s" % (n, "" if n == 1 else "s"))
    if start_f or end_f:
        summary.append("window: %s … %s" % (start_v or "—", end_v or "—"))
    return format_html(
        '<div class="hd-ncparams" data-url="{}" data-start="{}" data-end="{}" '
        'data-haveshp="{}" data-havedate="{}" '
        'style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:13px;">'
        '<span class="frappe-muted">{}</span>'
        '<button type="button" class="btn btn-link btn-sm hd-ncparams-btn" '
        'style="padding:0 4px;">Adjust &amp; refresh</button></div>',
        url, start_v, end_v, "1" if shp_f else "", "1" if (start_f or end_f) else "",
        " · ".join(summary) or "query parameters")


def _render_api_field(connector_name, connector, value, attrs, refresh_url=None,
                      field_map=None, api_outputs=None, nc_map=None):
    """Render an API field's LIVE result as safe HTML for the detail view.

    Resolves the connector (already loaded), and renders by the field's
    ``api_outputs`` (the ticked x-api-outputs subset: a list of
    {output, label, field_type}). For EACH ticked output it calls fetch_api once
    with the SAME (name,url) cache key — so N outputs share ONE HTTP hit — selecting
    that named output, and renders by its field_type:
      Number/Text/Date -> the existing formatted scalar span (under its label)
      Time-Series       -> the inline SVG sparkline + last-N table (NEVER 20+ rows)

    BACK-COMPAT: if ``api_outputs`` is absent/empty, render the connector's single
    PRIMARY output exactly as before (value scalar / series sparkline / raw JSON).

    EVERY piece of fetched, untrusted external content is escaped (format_html /
    escape) so a malicious API response can never inject markup. The credential
    secret is never shown (fetch_api redacts the URL)."""
    if connector is None:
        return format_html(
            '<span class="frappe-muted frappe-text-sm">'
            'No connector named &ldquo;{}&rdquo; found.</span>', connector_name or "")

    # Honor the netcdf per-record parameter mapping (x-nc-map): the record's mapped
    # shapefile + date fields drive the fetch (cloned cfg/attrs; never mutates the row).
    _cfg, attrs = _apply_nc_map(connector.config, attrs, nc_map)

    # The cached/live pill + Refresh link are computed from the FIRST fetch and
    # shown once for the whole field (all outputs share one cached `data`).
    refresh = ""
    if refresh_url:
        refresh = format_html(
            ' <a class="btn btn-link btn-sm" href="{}" '
            'style="padding:0 4px;">Refresh</a>', refresh_url)

    # ---- Multi-output render (the ticked x-api-outputs subset). ----
    if api_outputs:
        blocks = []
        head_pill = None
        last_result = None
        for entry in api_outputs:
            if not isinstance(entry, dict):
                continue
            oname = (entry.get("output") or "").strip()
            label = (entry.get("label") or oname or "").strip()
            ft = (entry.get("field_type") or "Text").strip()
            result = fetch_api(_cfg, attrs,
                               connector_name=connector_name,
                               field_map=field_map, output=oname)
            last_result = result
            if head_pill is None:
                head_pill = ('<span class="indicator-pill gray">cached</span>'
                             if result.get("cached")
                             else '<span class="indicator-pill blue">live</span>')
            if (result.get("kind") == "image") or ft == "Image":
                body = _render_image_block(result, label)
            elif (result.get("kind") == "series") or ft == "Time-Series":
                body = _render_columns_table(result)
            else:
                body = _render_value_block(result.get("value"), ft)
            blocks.append(
                "<div class='hd-api-output' style='margin-bottom:8px;'>"
                + str(format_html(
                    "<div class='frappe-text-sm' "
                    "style='color:var(--fr-text-muted);font-weight:600;'>{}</div>",
                    label))
                + "<div>" + str(body) + "</div></div>")
        note = (_api_source_note(last_result, connector) if last_result else "")
        return mark_safe(
            "<div class='hd-api-field'>" + str(mark_safe(head_pill or ""))
            + str(refresh) + "".join(blocks) + note + "</div>")

    # ---- BACK-COMPAT single-output render (no x-api-outputs ticked). ----
    result = fetch_api(_cfg, attrs, connector_name=connector_name,
                       field_map=field_map)
    kind = result.get("kind")
    cached = result.get("cached")
    pill = ('<span class="indicator-pill gray">cached</span>'
            if cached else '<span class="indicator-pill blue">live</span>')

    if kind == "image":
        body = _render_image_block(result)
        return mark_safe(
            "<div class='hd-api-field'>" + str(body) + " "
            + str(mark_safe(pill)) + str(refresh)
            + _api_source_note(result, connector) + "</div>")

    if kind == "series":
        body = _render_columns_table(result)
        return mark_safe(
            "<div class='hd-api-field'>" + str(body) + " "
            + str(mark_safe(pill)) + str(refresh)
            + _api_source_note(result, connector) + "</div>")

    if kind == "json":
        payload = result.get("json")
        try:
            pretty = json.dumps(payload, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            pretty = str(payload)
        body = format_html(
            "<details class='hd-api-json'><summary class='frappe-text-sm'>"
            "View JSON {}{}</summary><pre style='background:var(--gray-100);"
            "padding:10px;border-radius:6px;overflow:auto;max-height:320px;'>"
            "{}</pre></details>", mark_safe(pill), refresh, pretty)
        return mark_safe(str(body) + _api_source_note(result, connector))

    # value
    val = result.get("value")
    if val in (None, ""):
        shown = format_html(
            "<span class='frappe-muted frappe-text-sm'>no value {}{}</span>",
            mark_safe(pill), refresh)
    else:
        shown = format_html(
            "<span style='font-weight:600;'>{}</span> {}{}",
            str(val), mark_safe(pill), refresh)
    return mark_safe(str(shown) + _api_source_note(result, connector))


def _api_source_note(result, connector):
    """A tiny muted footnote: the (secret-redacted) source URL the value came from,
    plus the result's ``note`` when present (e.g. a synthetic-preview / degradation
    message) so the user always knows when data isn't a live fetch."""
    url = result.get("url") or ""
    note = (result or {}).get("note") or ""
    base = format_html(
        "<div class='frappe-help' style='margin-top:3px;'>via connector "
        "<code>{}</code>{}</div>",
        connector.name,
        format_html(" &middot; <span style='word-break:break-all;'>{}</span>", url) if url else "")
    if note:
        base = format_html("{}<div class='frappe-help' style='margin-top:2px;color:#b35900;'>"
                           "<i class='bi bi-info-circle'></i> {}</div>", base, note)
    return str(base)


def _sparkline_svg(values):
    """Tiny inline SVG sparkline from a numeric series (best-effort float()).

    Returns a safe <svg> string. Non-numeric/empty -> an empty muted span. The SVG
    is generated from numbers only (no external content interpolated), so it is
    safe by construction."""
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(nums) < 2:
        return "<span class='frappe-muted frappe-text-sm'>&mdash;</span>"
    w, h, pad = 160, 32, 2
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    step = (w - 2 * pad) / (len(nums) - 1)
    pts = []
    for i, v in enumerate(nums):
        x = pad + i * step
        y = h - pad - ((v - lo) / span) * (h - 2 * pad)
        pts.append("%.1f,%.1f" % (x, y))
    return (
        "<svg width='%d' height='%d' viewBox='0 0 %d %d' "
        "style='vertical-align:middle;' xmlns='http://www.w3.org/2000/svg'>"
        "<polyline fill='none' stroke='#2490ef' stroke-width='1.5' points='%s'/>"
        "</svg>" % (w, h, w, h, " ".join(pts))
    )


def _detail_fields(field_schema, attributes, session=None, refresh_urls=None,
                   parent_slug=None, parent_id=None):
    """Build an ordered list of {label, value} pairs for the detail view.

    Schema properties first (in declaration order, labels from title/humanized
    key), then any extra attribute keys not described by the schema. Typed
    properties (Link/email/url) render as safe-HTML links via _format_typed_cell;
    API properties (x-api-connector) call fetch_api and render the LIVE result
    (value/series/json). detail.html ({{ f.value }}) passes mark_safe/format_html
    output through. ``refresh_urls`` maps a field name -> its Refresh URL.
    """
    schema = field_schema or {}
    attrs = attributes or {}
    refresh_urls = refresh_urls or {}

    fields = []
    seen = set()
    for name, prop in _ordered_props(schema):  # honor x-order (JSONB scrambles)
        prop = prop or {}
        if prop.get("x-layout") == "tab":
            fields.append({"is_tab": True, "tab": prop.get("title") or "Tab"})
            seen.add(name)
            continue
        if prop.get("x-layout") == "section":
            fields.append({"is_section": True, "section": prop.get("title") or ""})
            seen.add(name)
            continue
        if prop.get("x-layout") == "column":
            seen.add(name)   # column breaks don't affect the read-only detail view
            continue
        label = prop.get("title") or name.replace("_", " ").title()
        connector_name = prop.get("x-api-connector")
        if connector_name:
            connector = _load_connector(session, connector_name) if session else None
            api_val = _render_api_field(
                connector_name, connector, attrs.get(name), attrs,
                refresh_url=refresh_urls.get(name),
                field_map=prop.get("x-api-map"),
                api_outputs=prop.get("x-api-outputs"),
                nc_map=prop.get("x-nc-map"))
            if (prop.get("x-nc-map") and connector       # edit-in-place params bar
                    and _supports_record_params(connector.config or {})):
                bar = _nc_params_bar(parent_slug, parent_id, name,
                                     prop.get("x-nc-map"), attrs)
                if bar:
                    api_val = mark_safe(str(bar) + str(api_val))
            fields.append({"label": label, "value": api_val})
            seen.add(name)
            continue
        if prop.get("type") == "array" and prop.get("x-child-type"):
            fields.append({
                "label": label,
                "value": _render_linked_table(session, prop.get("x-child-type"),
                                              parent_slug, parent_id, name,
                                              child_link=prop.get("x-child-link")),
            })
            seen.add(name)
            continue
        if prop.get("type") == "array" and prop.get("x-widget") == "table":
            fields.append({
                "label": label,
                "value": _render_table_field(prop, attrs.get(name)),
            })
            seen.add(name)
            continue
        fields.append({
            "label": label,
            "value": _format_typed_cell(prop, attrs.get(name), session),
        })
        seen.add(name)
    for name, value in attrs.items():
        if name in seen or name.startswith("_"):
            continue  # _-prefixed keys are reserved (e.g. _parent link) -> hidden
        fields.append({
            "label": name.replace("_", " ").title(),
            "value": _format_cell(value),
        })
    return fields


@controller(name="api_params", url="record/{slug}/{record_id}/api-params/{field}",
            title="Update query")
def api_params_update(request, slug=None, record_id=None, field=None):
    """EDIT-IN-PLACE for a netcdf API field: update the record's mapped date/shapefile
    fields (the ``x-nc-map`` targets) from the params modal, then redirect to the detail
    with ``?refresh=<field>`` so the query re-runs with the new parameters. POST only,
    write-gated, multipart (the shapefile may be re-uploaded)."""
    engine = App.get_persistent_store_database("hydro_db")
    detail_url = reverse("hydrodesk:detail", kwargs={"slug": slug, "record_id": record_id})
    if request.method != "POST":
        return redirect(detail_url)
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is None:
            return redirect(reverse("hydrodesk:home"))
        _dn, field_schema, _ = meta
        if not _user_can(request, field_schema, "write"):
            return _denied(request, "edit", _dn)
        prop = ((field_schema.get("properties") or {}).get(field)) or {}
        nc_map = prop.get("x-nc-map") or {}
        # Only a real netcdf API field carries x-nc-map; refuse anything else.
        if not (nc_map and prop.get("x-api-connector")):
            return redirect(detail_url)
        rec = session.execute(
            select(m.HydroRecord)
            .where(m.HydroRecord.hydrotype_slug == slug)
            .where(m.HydroRecord.id == record_id)
        ).scalar_one_or_none()
        if rec is None:
            return redirect(detail_url)
        attrs = dict(rec.attributes or {})
        for key in ("start", "end"):
            fld = nc_map.get(key)
            if fld:
                v = (request.POST.get("p_" + key) or "").strip()
                # Store only a parseable date; ignore garbage so the window can't be
                # silently disabled by an un-decodable value.
                if v and _parse_date_loose(v) is not None:
                    attrs[fld] = v
        shp_fld = nc_map.get("shapefile")
        if shp_fld and request.FILES.get("p_shapefile") is not None:
            gj = _shapefile_to_featurecollection(request.FILES["p_shapefile"])
            if gj:
                attrs[shp_fld] = gj
        rec.attributes = attrs
        session.commit()
    return redirect(detail_url + "?refresh=" + urllib.parse.quote(field or ""))


@controller(name="detail", url="record/{slug}/{record_id}", title="Record")
def hydrotype_detail(request, slug="monitoring_station", record_id=None):
    """Read-style detail view for one HydroRecord: a definition list of its
    fields (labels from field_schema titles) plus its lon/lat when it has a
    point geometry, with Edit (-> new form) and Back-to-list links."""
    engine = App.get_persistent_store_database("hydro_db")

    display_name = slug
    field_schema = {}
    fields = []
    record_found = False
    lon = lat = None
    geom_geojson = None

    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is not None:
            display_name, field_schema, _ = meta

        if not _user_can(request, field_schema, "read"):
            return _denied(request, "view", display_name)

        row = session.execute(
            select(
                m.HydroRecord.attributes,
                # A representative point ON the geometry (not ST_X/ST_Y on the raw geom
                # — those raise on a polygon/line; not ST_Centroid — that can fall in a
                # gap between disjoint MultiPolygon parts). Valid for ANY geometry type:
                # the record's lon/lat for map centring + the dynamic point/bbox filters.
                func.ST_X(func.ST_PointOnSurface(m.HydroRecord.geom)),
                func.ST_Y(func.ST_PointOnSurface(m.HydroRecord.geom)),
                func.ST_AsGeoJSON(m.HydroRecord.geom),
            )
            .where(m.HydroRecord.hydrotype_slug == slug)
            .where(m.HydroRecord.id == record_id)
        ).first()

        if row is not None:
            record_found = True
            attributes, lon, lat, geom_geojson = row[0], row[1], row[2], row[3]
            # Expose the record's geometry to connectors under reserved keys (hidden
            # from the field list): _lon/_lat (the centroid — a WMS map centred here,
            # the dynamic point/bbox filters) and _geojson (the FULL geometry — the
            # DYNAMIC per-zone filter reduces over the record's own polygons).
            if lon is not None and lat is not None:
                attributes = dict(attributes or {})
                attributes.setdefault("_lon", lon)
                attributes.setdefault("_lat", lat)
            if geom_geojson:
                attributes = dict(attributes or {})
                attributes.setdefault("_geojson", geom_geojson)
            # A per-record SHAPEFILE field -> expose its GeoJSON as the reserved
            # _shapefile key (the shapefile spatial-filter source) and, when the record
            # has no PostGIS geometry, draw it on the detail map.
            for _sfk in _shapefile_fields(field_schema):
                _sfv = (attributes or {}).get(_sfk)
                if _sfv:
                    attributes = dict(attributes or {})
                    attributes.setdefault("_shapefile", _sfv)
                    if not geom_geojson:
                        geom_geojson = _sfv
                    break
            # A '?refresh=<field>' request busts the API cache for that field's
            # connector before the synchronous fetch in _detail_fields, so the
            # Refresh link forces a fresh pull (then degrades gracefully if the
            # API is down).
            refresh_field = request.GET.get("refresh")
            if refresh_field:
                _bust_api_cache_for_field(field_schema, refresh_field, attributes,
                                          session)
            # Per-API-field Refresh URLs (point back at this detail with ?refresh=).
            base = reverse("hydrodesk:detail",
                           kwargs={"slug": slug, "record_id": record_id})
            refresh_urls = {
                fname: base + "?refresh=" + urllib.parse.quote(fname)
                for fname, fprop in ((field_schema or {}).get("properties") or {}).items()
                if (fprop or {}).get("x-api-connector")
            }
            # Build fields inside the open session so Link labels + API fetches
            # resolve against the live store.
            fields = _detail_fields(field_schema, attributes, session,
                                    refresh_urls=refresh_urls,
                                    parent_slug=slug, parent_id=record_id)

    has_geom = lon is not None and lat is not None
    # The record's human TITLE (designated title field / name / first field), shown
    # as the detail H1 instead of a bare UUID. Falls back to a short id.
    record_title = (_label_for(field_schema, attributes)
                    or (str(record_id)[:8] if record_found else "")) if record_found else ""

    context = {
        "slug": slug,
        "record_id": str(record_id),
        "display_name": display_name,
        "record_title": record_title,
        "record_found": record_found,
        "fields": fields,
        "has_geom": has_geom,
        "longitude": lon,
        "latitude": lat,
        "geom_geojson": geom_geojson,
        "list_url": reverse("hydrodesk:list", kwargs={"slug": slug}),
        "edit_url": reverse("hydrodesk:edit", kwargs={"slug": slug, "record_id": record_id}),
        "delete_url": reverse("hydrodesk:delete", kwargs={"slug": slug, "record_id": record_id}),
    }
    return render(request, "hydrodesk/detail.html", context)


# ---------------------------------------------------------------------------
# DocType Builder — "+ New HydroType". The headline runtime-definition feature:
# define a new type's fields in a UI form -> assemble a JSON-Schema field_schema
# + spec -> INSERT one hydrotype row via registry.import_hydrotype -> redirect to
# the new type's (now-existing) generic List view. A new HydroType is NOT a new
# DB table — it is a single row in the `hydrotype` table. No DDL, no syncstores.
# ---------------------------------------------------------------------------

# Scan well past the 7 fixed builder rows so JS-added rows (index >= 7) are read
# without gaps in numbering mattering. Rows key on a NON-EMPTY label.
_BUILDER_MAX_ROWS = 64
_BUILDER_MIN_ROWS = 7


def _slugify_underscore(text):
    """django.utils.text.slugify emits HYPHENS ('site-name'); registry.validate_spec
    requires slug.replace('_','').isalnum() (underscores/alnum only). Convert
    hyphens -> underscores so the assembled slug/field keys pass validation."""
    return slugify(text or "").replace("-", "_")


def _schema_for(field_type, options):
    """Map a builder row 'Type' select -> a JSON-Schema property fragment that the
    existing read-side (_build_widgets / _coerce_attributes / _derive_columns)
    already consumes: Text->string, Number->number, Select->string+enum,
    Checkbox->boolean, Date->string+format:date, Tags->array of strings,
    Long Text->string+x-widget:textarea, Email->string+format:email,
    URL->string+format:uri, Link->string+x-link-type:<target slug>.

    The new string-based types all stay JSON-Schema ``type: 'string'`` so they
    coerce as raw strings and validate cleanly; the input control is selected
    downstream off the ``format`` / ``x-widget`` / ``x-link-type`` hint. The
    ``x-`` keys are ignored by check_schema/fastjsonschema (custom extensions)."""
    ft = (field_type or "text").strip().lower()
    if ft == "section":
        # LAYOUT-ONLY: a Section Break. Holds no data — it groups the fields that
        # follow it under a heading (its label, set as 'title' by the caller). The
        # x-layout key is ignored by validation and skipped by the data paths.
        return {"x-layout": "section"}
    if ft == "column":
        # LAYOUT-ONLY: a Column Break. Splits the current section into side-by-side
        # columns on the form (its label, if any, is an optional column heading).
        return {"x-layout": "column"}
    if ft == "tab":
        # LAYOUT-ONLY: a Tab Break. Starts a new top-level TAB (its label is the tab
        # title); the form/detail render a tab bar and show one tab at a time.
        return {"x-layout": "tab"}
    if ft == "number":
        return {"type": "number"}
    if ft == "checkbox":
        return {"type": "boolean"}
    if ft == "date":
        return {"type": "string", "format": "date"}
    if ft == "tags":
        return {"type": "array", "items": {"type": "string"}}
    if ft == "textarea":  # Long Text -> multiline <textarea>
        return {"type": "string", "x-widget": "textarea"}
    if ft == "email":
        return {"type": "string", "format": "email"}
    if ft == "url":
        return {"type": "string", "format": "uri"}
    # --- formatted scalar types (batch 2): a standard JSON type + an x-field display
    # hint. They coerce/validate like number/integer/string; the input widget +
    # detail/list formatting dispatch off x-field (custom key, ignored by validation).
    if ft == "currency":
        return {"type": "number", "x-field": "currency"}
    if ft == "percent":
        return {"type": "number", "x-field": "percent"}
    if ft == "duration":  # stored as a number of SECONDS
        return {"type": "number", "x-field": "duration"}
    if ft == "phone":
        return {"type": "string", "x-field": "phone"}
    if ft == "color":
        return {"type": "string", "x-field": "color"}
    if ft == "shapefile":  # per-record uploaded shapefile -> stored as inline GeoJSON
        # The record form shows a file input (.zip/.shp); on save it is converted to a
        # GeoJSON FeatureCollection string in attributes. A connector with
        # spatial='shapefile' reduces the NetCDF over THIS record's uploaded region.
        return {"type": "string", "x-field": "shapefile"}
    if ft == "rating":  # integer 0..5, shown as stars
        return {"type": "integer", "x-field": "rating", "minimum": 0, "maximum": 5}
    if ft == "formula":  # computed from other fields; Options column = the expression
        return {"type": "number", "x-field": "formula",
                "x-formula": (options or "").strip()}
    if ft == "script":  # sandboxed Python; Options column = the source. The declared
        # outputs (x-script-outputs + one computed property each) are added by the third
        # pass in _assemble_type_spec; this base property holds no record value itself.
        return {"x-field": "script", "x-script": (options or "").strip(),
                "x-script-outputs": []}
    if ft == "link":  # foreign key: Options column carries the target HydroType slug
        return {"type": "string", "x-link-type": (options or "").strip()}
    if ft == "api":  # live field: Options column carries the CONNECTOR NAME
        # Mirrors the Link branch exactly — a type:'string' property whose stored
        # value is the substitution key (a site/reach id) and whose x-api-connector
        # custom key (silently ignored by check_schema/fastjsonschema) names the
        # connector resolved at detail-render time. Kept raw (not slugified) so it
        # matches the HydroConnector.name the user typed verbatim.
        return {"type": "string", "x-api-connector": (options or "").strip()}
    if ft == "table":  # child grid OR linked records of another HydroType
        mode, payload = _parse_table_config(options)
        if mode == "link":
            # LINKED table: rows are REAL records of another HydroType. The parent
            # stores NO inline rows. ``x-child-link`` (optional) = the child's Link
            # field pointing back (reverse-link mode, e.g. a Customer's invoices);
            # absent = children carry a ``_parent`` back-reference (private one-to-many).
            prop = {"type": "array", "x-widget": "table",
                    "x-child-type": payload.get("child_type")}
            child_link = _slugify_underscore(payload.get("child_link") or "")
            if child_link:
                prop["x-child-link"] = child_link
            return prop
        # INLINE columns: a JSON ARRAY of row objects stored on the parent record's
        # attributes (the generic JSONB store holds the rows). Options carries
        # [{label,type,options}] -> the per-column item schema. Child cell types are
        # limited to the simple scalar set (no nested table / link / api).
        item_props = {}
        order = []
        for col in payload:
            ckey = _slugify_underscore(col.get("label") or "")
            if not ckey or ckey in item_props:
                continue
            ctype = (col.get("type") or "text").strip().lower()
            if ctype in ("table", "link", "api", "tags"):
                ctype = "text"  # child cells are simple scalars only
            cprop = _schema_for(ctype, col.get("options") or "")
            cprop["title"] = col.get("label") or ckey
            item_props[ckey] = cprop
            order.append(ckey)
        # x-order PRESERVES column order: properties is stored as JSONB, which does
        # NOT keep object-key insertion order (it reorders by key length then bytes),
        # so an explicit ordered key list is the source of truth for column order.
        return {"type": "array", "x-widget": "table",
                "items": {"type": "object", "properties": item_props,
                          "x-order": order}}
    if ft == "select":
        enum = [o.strip() for o in (options or "").split(",") if o.strip()]
        prop = {"type": "string"}
        if enum:  # drop empty enum -> an [] enum would make every value invalid
            prop["enum"] = enum
        return prop
    return {"type": "string"}  # text + anything unknown


def _parse_table_config(options):
    """Parse a Table field's Options carrier into ``(mode, payload)``.

    The builder stores either:
      * a JSON LIST ``[{label,type,options}]`` -> ('columns', [cols])  (inline grid)
      * a JSON OBJECT ``{"child_type": "<slug>", "child_link": "<field>"?}`` ->
        ('link', {child_type, child_link}). ``child_link`` (optional) names the
        child's Link field that points back here (reverse-link / connections mode);
        absent = the _parent-owned mode.
    A blank/invalid carrier degrades to ('columns', [])."""
    raw = (options or "").strip()
    if not raw:
        return ("columns", [])
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ("columns", [])
    if isinstance(parsed, dict) and (parsed.get("child_type") or "").strip():
        return ("link", {"child_type": parsed["child_type"].strip(),
                         "child_link": (parsed.get("child_link") or "").strip()})
    if isinstance(parsed, list):
        return ("columns", [c for c in parsed
                            if isinstance(c, dict) and (c.get("label") or "").strip()])
    return ("columns", [])


def _coerce_default(prop, raw):
    """Coerce a builder-entered default string to the property's JSON type, or None
    when blank/un-coercible (so a bad default is simply dropped, never stored)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    t = (prop or {}).get("type")
    if t == "boolean":
        return str(raw).lower() in ("true", "on", "1", "yes")
    if t == "number":
        try:
            return float(raw)
        except ValueError:
            return None
    if t == "integer":
        try:
            return int(raw)
        except ValueError:
            return None
    if t == "array":
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


def _builder_type_for(prop):
    """Inverse of _schema_for's TYPE choice: a stored property fragment -> the
    builder 'Type' select value (text/number/select/checkbox/date/textarea/email/
    url/link/api/table/tags). Used to pre-fill the builder when EDITING a type."""
    prop = prop or {}
    if prop.get("x-layout") == "tab":
        return "tab"
    if prop.get("x-layout") == "section":
        return "section"
    if prop.get("x-layout") == "column":
        return "column"
    if prop.get("x-api-connector"):
        return "api"
    if prop.get("x-child-type"):
        return "table"
    if prop.get("x-field") in ("currency", "percent", "duration", "phone",
                               "color", "rating", "formula", "shapefile", "script"):
        return prop.get("x-field")  # formatted/computed types round-trip by x-field
    t = prop.get("type")
    if t == "array":
        return "table" if prop.get("x-widget") == "table" else "tags"
    if prop.get("x-link-type"):
        return "link"
    if t == "boolean":
        return "checkbox"
    if t in ("number", "integer"):
        return "number"
    if t == "string":
        if prop.get("x-widget") == "textarea":
            return "textarea"
        fmt = prop.get("format")
        if fmt == "date":
            return "date"
        if fmt == "email":
            return "email"
        if fmt == "uri":
            return "url"
        if prop.get("enum"):
            return "select"
    return "text"


def _builder_options_for(prop, builder_type):
    """Inverse of _schema_for's OPTIONS carrier: reconstruct the field_options string
    the builder modal expects for a given property + builder type — a comma list for
    Select, the target slug for Link, the connector name for API, and the columns
    JSON / {child_type} JSON for a Table."""
    prop = prop or {}
    if builder_type == "select":
        return ", ".join(str(x) for x in (prop.get("enum") or []))
    if builder_type == "link":
        return prop.get("x-link-type") or ""
    if builder_type == "api":
        return prop.get("x-api-connector") or ""
    if builder_type == "formula":
        return prop.get("x-formula") or ""
    if builder_type == "script":
        return prop.get("x-script") or ""
    if builder_type == "table":
        if prop.get("x-child-type"):
            obj = {"child_type": prop.get("x-child-type")}
            if prop.get("x-child-link"):
                obj["child_link"] = prop.get("x-child-link")
            return json.dumps(obj)
        cols = []
        for ck, cp in _table_item_columns(prop):
            cbt = _builder_type_for(cp)
            copts = ", ".join(str(x) for x in (cp.get("enum") or [])) if cbt == "select" else ""
            cols.append({"label": cp.get("title") or ck, "type": cbt, "options": copts})
        return json.dumps(cols)
    return ""


def _schema_to_builder_rows(field_schema):
    """Reverse a stored field_schema into editable builder rows (the inverse of the
    new_hydrotype assembly). Each row mirrors _parse_builder_rows' shape
    (label/type/options/required/field_map/api_outputs) so the builder pre-fills when
    editing. Honors x-order. The per-doctype x-api-map is reversed to the builder's
    ``{input: <slug> | __const__:<value>}`` seed shape."""
    required = set((field_schema or {}).get("required") or [])
    rows = []
    for key, prop in _ordered_props(field_schema):
        prop = prop or {}
        bt = _builder_type_for(prop)
        field_map = {}
        for inp, mp in (prop.get("x-api-map") or {}).items():
            if not isinstance(mp, dict):
                continue
            src = (mp.get("source") or "").lower()
            if src in ("const", "constant", "value"):
                field_map[inp] = "__const__:" + str(mp.get("value", ""))
            elif mp.get("field"):
                field_map[inp] = mp.get("field")
        # Default value -> string carrier (bool -> 'true'/'false').
        dflt = prop.get("default")
        if isinstance(dflt, bool):
            dflt = "true" if dflt else "false"
        elif dflt is None:
            dflt = ""
        else:
            dflt = str(dflt)
        si = prop.get("x-show-if") or {}
        nc_map = prop.get("x-nc-map") or {}
        rows.append({
            "label": prop.get("title") or key.replace("_", " ").title(),
            "type": bt,
            "options": _builder_options_for(prop, bt),
            "required": key in required,
            "field_map": field_map,
            # Outputs round-trip through the SAME api_outputs carrier; a script's
            # x-script-outputs use the 'name' key, so normalize them to 'output' here.
            "api_outputs": list(prop.get("x-api-outputs")
                or [{"output": o.get("name"), "label": o.get("label"),
                     "field_type": o.get("field_type")}
                    for o in (prop.get("x-script-outputs") or []) if isinstance(o, dict)]),
            "nc_map": nc_map,
            "nc_map_json": json.dumps(nc_map) if nc_map else "",
            "default": dflt,
            "showif": si,
            "showif_json": json.dumps(si) if si.get("field") else "",
        })
    return rows


def _parse_builder_rows(post):
    """Reconstruct the indexed field-definition rows from request.POST.

    Inputs are named field_label_<i>/field_type_<i>/field_required_<i>/
    field_options_<i>. A row is 'real' only when its label is non-empty. Returns
    (rows, submitted_count) where rows is a list of dicts preserving submitted
    values for re-render, and submitted_count is the highest index seen + 1 (so
    the re-render keeps every row the user filled, even JS-added ones).

    For an API row, the per-doctype x-api-map selections (one POST key per
    connector input, named ``field_map_<i>_<input_name>`` carrying the chosen
    target-field SLUG, or ``__const__:<value>`` for a constant) are collected into
    ``row['field_map']`` so a validation error re-render keeps the user's mapping
    work (the JS would otherwise lose it)."""
    rows = []
    submitted_count = 0
    for i in range(_BUILDER_MAX_ROWS):
        label = (post.get(f"field_label_{i}") or "").strip()
        ftype = (post.get(f"field_type_{i}") or "text").strip().lower()
        options = (post.get(f"field_options_{i}") or "").strip()
        # Unchecked checkboxes send NOTHING in POST -> presence == checked.
        is_req = post.get(f"field_required_{i}") is not None
        # Collect any field_map_<i>_<input> selections submitted for this row.
        prefix = f"field_map_{i}_"
        field_map = {}
        for key in post.keys():
            if key.startswith(prefix):
                input_name = key[len(prefix):]
                val = (post.get(key) or "").strip()
                if input_name and val:
                    field_map[input_name] = val
        # Collect this row's ticked OUTPUTS subset (x-api-outputs). Carried as a
        # single JSON blob in a hidden ``field_outputs_<i>`` input: a list of
        # {output, label, field_type}. Stashed into row['api_outputs'] so a
        # validation-error re-render survives (same reason field_map is preserved).
        api_outputs = []
        raw_outputs = (post.get(f"field_outputs_{i}") or "").strip()
        if raw_outputs:
            try:
                parsed = json.loads(raw_outputs)
                if isinstance(parsed, list):
                    api_outputs = parsed
            except (ValueError, TypeError):
                api_outputs = []
        # NetCDF API field: the per-record parameter mapping (x-nc-map) carried as a
        # single JSON blob field_ncmap_<i> = {shapefile,start,end} -> field slugs.
        nc_map = {}
        raw_ncmap = (post.get(f"field_ncmap_{i}") or "").strip()
        if raw_ncmap:
            try:
                parsed_nc = json.loads(raw_ncmap)
                if isinstance(parsed_nc, dict):
                    nc_map = {k: str(v).strip() for k, v in parsed_nc.items()
                              if k in ("shapefile", "start", "end") and str(v).strip()}
            except (ValueError, TypeError):
                nc_map = {}
        # Common per-field options: a default value + a structured "show only if"
        # (depends-on) carried as field_default_<i> / field_showif_<i> (JSON).
        default_val = (post.get(f"field_default_{i}") or "").strip()
        showif = {}
        raw_showif = (post.get(f"field_showif_{i}") or "").strip()
        if raw_showif:
            try:
                parsed_si = json.loads(raw_showif)
                if isinstance(parsed_si, dict) and (parsed_si.get("field") or "").strip():
                    showif = {"field": str(parsed_si["field"]).strip(),
                              "value": str(parsed_si.get("value") or "")}
            except (ValueError, TypeError):
                showif = {}
        has_any = (bool(label) or bool(options) or is_req or bool(field_map)
                   or bool(api_outputs) or bool(nc_map) or bool(default_val) or bool(showif))
        if has_any:
            submitted_count = i + 1
        rows.append({
            "label": label,
            "type": ftype,
            "options": options,
            "required": is_req,
            "field_map": field_map,
            "api_outputs": api_outputs,
            "nc_map": nc_map,
            "default": default_val,
            "showif": showif,
        })
    return rows, submitted_count


def _builder_perm_groups(perms):
    """Every portal role (Django Group) with its current read/write selection, for
    the builder's Permissions section. Empty list if there are no groups."""
    try:
        from django.contrib.auth.models import Group
        names = list(Group.objects.values_list("name", flat=True).order_by("name"))
    except Exception:
        names = []
    perms = perms or {}
    read_set = set(perms.get("read") or [])
    write_set = set(perms.get("write") or [])
    return [{"name": g, "read": g in read_set, "write": g in write_set} for g in names]


def _builder_context(form_errors, type_name, geometry, rows, row_count,
                     mode="new", slug=None, title_field="", perms=None,
                     naming_series=""):
    """Build the template context. row_indexes drives the fixed rows; rows holds
    re-fill values keyed by index (template loops row_indexes and reads rows[i]).
    ``mode`` ('new'|'edit') + ``slug`` drive the form action, title, and submit
    label so the SAME builder template serves both create and edit."""
    # Attach a JSON string of each row's x-api-map selections AND its ticked
    # x-api-outputs so the per-row panel can re-seed both after a validation-error
    # re-render (parallel plumbing to field_map_json).
    for row in rows:
        if isinstance(row, dict):
            row["field_map_json"] = json.dumps(row.get("field_map") or {})
            row["api_outputs_json"] = json.dumps(row.get("api_outputs") or [])
            row["nc_map_json"] = json.dumps(row.get("nc_map") or {}) if row.get("nc_map") else ""
            si = row.get("showif") or {}
            row["showif_json"] = json.dumps(si) if si.get("field") else ""
    is_edit = mode == "edit"
    form_action = (reverse("hydrodesk:edit_type", kwargs={"slug": slug})
                   if is_edit else reverse("hydrodesk:new_type"))
    return {
        "form_errors": form_errors,
        "type_name": type_name,
        "geometry": geometry,
        "mode": mode,
        "edit_slug": slug if is_edit else "",
        "form_action": form_action,
        "page_title": ("Edit HydroType" if is_edit else "New HydroType"),
        "submit_label": ("Save changes" if is_edit else "Create type"),
        "title_field": title_field or "",
        "naming_series": naming_series or "",
        "perm_groups": _builder_perm_groups(perms),
        "row_indexes": list(range(row_count)),
        # Pad rows so every rendered index has a dict to read on re-fill.
        "rows": rows + [{"label": "", "type": "text", "options": "",
                         "required": False, "field_map": {}, "api_outputs": [],
                         "field_map_json": "{}", "api_outputs_json": "[]",
                         "default": "", "showif": {}, "showif_json": ""}]
                * max(0, row_count - len(rows)),
        # The mapping endpoint base for the per-row x-api-map JS (a placeholder
        # connector name the JS swaps with the typed name).
        "connector_inputs_url": reverse(
            "hydrodesk:connector_inputs", kwargs={"conn_name": "__NAME__"}),
        # JSON list endpoints powering the Configure modal's Link/API pickers.
        "types_json_url": reverse("hydrodesk:types_json"),
        "connectors_json_url": reverse("hydrodesk:connectors_json"),
        # The Python-Script field's Test-run endpoint (runs the sandbox on sample inputs).
        "script_test_url": reverse("hydrodesk:script_test"),
        # Convenience deep-links for the modal's empty-state hints.
        "connectors_url": reverse("hydrodesk:connectors"),
        "new_type_url": reverse("hydrodesk:new_type"),
    }


def _assemble_type_spec(post, force_slug=None):
    """Parse the builder POST into a HydroType spec (the shared create+edit core).

    Returns ``(spec, form_errors, type_name, geometry, rows, row_count, slug)``.
    ``spec`` is None when there are form_errors. ``force_slug`` (edit mode) pins the
    slug to the existing type so renaming the display name never orphans records;
    in create mode the slug is derived from the type name."""
    type_name = (post.get("type_name") or "").strip()
    geometry = (post.get("geometry") or "none").strip().lower()
    rows, submitted_count = _parse_builder_rows(post)
    row_count = max(_BUILDER_MIN_ROWS, submitted_count)

    form_errors = []

    # --- derive + guard the slug (hyphens -> underscores for validate_spec) ---
    if force_slug:
        slug = force_slug                 # edit: slug is immutable
        if not type_name:
            form_errors.append("Type Name is required.")
    else:
        slug = _slugify_underscore(type_name)
        if not type_name:
            form_errors.append("Type Name is required.")
        elif not slug:  # all-non-Latin / punctuation-only name slugifies to ''
            form_errors.append("Type Name must contain letters or digits.")

    # --- assemble properties from the real (non-blank-label) rows ---
    properties, required, seen = {}, [], set()
    # API rows captured for the x-api-map / x-api-outputs SECOND PASS: a mapping may
    # target a field defined in a LATER row, and the outputs subset must be validated
    # against the connector's catalog, so we can only finalize after all properties
    # exist. Each entry: (display_row_no, prop_name, connector_name, raw_field_map,
    # raw_api_outputs).
    api_rows = []
    # Python-script rows captured for the THIRD PASS (after every property exists): each
    # is (display_row_no, prop_name, raw_outputs) — the ticked outputs + their types.
    script_rows = []
    layout_seq = 0
    for idx, row in enumerate(rows):
        label = row["label"]
        rtype = (row["type"] or "text").strip().lower()
        is_layout = rtype in ("section", "column", "tab")
        if not label and not is_layout:
            continue  # a blank non-layout row => skip it entirely
        if is_layout:
            # Layout breaks (Section / Column) hold no data and are usually UNLABELED
            # (a column break especially), so they get a synthetic unique key rather
            # than being dropped for a blank label.
            layout_seq += 1
            prop_name = _slugify_underscore(label) if label else ""
            if not prop_name or prop_name in seen:
                prop_name = f"{rtype}_break_{layout_seq}"
                while prop_name in seen:
                    layout_seq += 1
                    prop_name = f"{rtype}_break_{layout_seq}"
        else:
            prop_name = _slugify_underscore(label)
            if not prop_name:
                form_errors.append(
                    f"Row {idx + 1}: field label '{label}' yields an empty field name."
                )
                continue
            if prop_name in seen:  # two labels slugify to the same key -> overwrite
                form_errors.append(
                    f"Row {idx + 1}: duplicate field name '{prop_name}'."
                )
                continue
        # A Link field's Options column carries the target HydroType slug; an
        # empty target makes the field unusable, so reject it early.
        if (row["type"] or "").strip().lower() == "link" and not (row["options"] or "").strip():
            form_errors.append(
                f"Row {idx + 1}: a Link field needs a target type slug in Options."
            )
            continue
        # An API field's Options column carries the CONNECTOR NAME (matched
        # verbatim to HydroConnector.name); reject an empty/unknown connector so
        # the field resolves to a real live source. Stronger than the Link guard.
        if (row["type"] or "").strip().lower() == "api":
            conn_name = (row["options"] or "").strip()
            if not conn_name:
                form_errors.append(
                    f"Row {idx + 1}: an API field needs a Connector name in Options."
                )
                continue
            if not _connector_name_exists(conn_name):
                form_errors.append(
                    f"Row {idx + 1}: no Connector named '{conn_name}' exists "
                    f"(create it under Connectors first)."
                )
                continue
            # Defer x-api-map + x-api-outputs validation to the second pass (targets
            # may be later rows; outputs need the connector catalog). Stash the
            # row's connector + submitted mapping selections + ticked outputs.
            api_rows.append((idx + 1, prop_name, conn_name,
                             dict(row.get("field_map") or {}),
                             list(row.get("api_outputs") or []),
                             dict(row.get("nc_map") or {})))
        if (row["type"] or "").strip().lower() == "script":
            src = (row["options"] or "").strip()
            if not src:
                form_errors.append(
                    f"Row {idx + 1}: a Python Script field needs a script in its config.")
                continue
            try:                              # reject a syntax error up front
                from RestrictedPython import compile_restricted
                compile_restricted(src, "<hydrodesk-script>", "exec")
            except SyntaxError as _se:
                form_errors.append(f"Row {idx + 1}: script has a syntax error: {str(_se)[:90]}")
                continue
            except Exception:
                pass                          # non-syntax issues surface at run time
            script_rows.append((idx + 1, prop_name, list(row.get("api_outputs") or [])))
        seen.add(prop_name)
        prop = _schema_for(row["type"], row["options"])
        prop["title"] = label  # _build_widgets/_derive_columns use title as the label
        properties[prop_name] = prop
        # A layout marker (Section Break) holds no data, so it can never be required.
        if row["required"] and not prop.get("x-layout"):
            required.append(prop_name)
        # Common per-field options (skip layout markers): a typed default value and
        # a structured depends-on (x-show-if: shows the field only when another
        # field matches). The controlling field is stored as a slug; the form JS
        # resolves it at render (fail-open if it no longer exists).
        if not prop.get("x-layout"):
            dv = _coerce_default(prop, row.get("default") or "")
            if dv is not None:
                prop["default"] = dv
            si = row.get("showif") or {}
            if (si.get("field") or "").strip():
                prop["x-show-if"] = {"field": _slugify_underscore(si["field"]),
                                     "value": str(si.get("value") or "")}

    # --- SECOND PASS: per-doctype x-api-map (the headline). Now that every
    # property exists we can validate each API field's connector-input -> field
    # mapping (a forward-reference to a later row is legal). Each submitted value
    # is the chosen target field SLUG, or '__const__:<value>' for a constant. A
    # required source==field input with no mapping is an error; an empty optional
    # mapping is silently dropped (the connector input's own default applies). ---
    valid_field_slugs = set(properties.keys())
    for row_no, prop_name, conn_name, raw_map, raw_outputs, raw_ncmap in api_rows:
        # x-nc-map: the netcdf per-record parameter mapping (shapefile/start/end ->
        # field slugs). ONLY for a netcdf connector, and each target must be the right
        # TYPE (shapefile param -> a Shapefile field; start/end -> a Date field) — the
        # builder UI enforces this, but never trust the client POST.
        nc_map = {}
        _nc_cfg = _connector_config_by_name(conn_name) or {}
        if _supports_record_params(_nc_cfg):
            for _k in ("shapefile", "start", "end"):
                _sel = (raw_ncmap.get(_k) or "").strip()
                if not _sel:
                    continue
                _slug = _sel if _sel in valid_field_slugs else _slugify_underscore(_sel)
                if _slug not in valid_field_slugs:
                    continue
                _tp = properties.get(_slug) or {}
                if _k == "shapefile" and _tp.get("x-field") != "shapefile":
                    form_errors.append(
                        f"Row {row_no}: API field '{prop_name}' shapefile parameter "
                        f"must map to a Shapefile field.")
                    continue
                if _k in ("start", "end") and _tp.get("format") != "date":
                    form_errors.append(
                        f"Row {row_no}: API field '{prop_name}' {_k} parameter "
                        f"must map to a Date field.")
                    continue
                nc_map[_k] = _slug
        if nc_map:
            properties[prop_name]["x-nc-map"] = nc_map
        field_inputs = _connector_field_inputs(conn_name)
        parsed_map = {}
        for input_name, required_flag in field_inputs:
            sel = (raw_map.get(input_name) or "").strip()
            if not sel:
                if required_flag:
                    form_errors.append(
                        f"Row {row_no}: API field '{prop_name}' must map the "
                        f"required connector input '{input_name}' to a field."
                    )
                continue
            if sel.startswith("__const__:"):
                parsed_map[input_name] = {
                    "source": "const", "value": sel[len("__const__:"):]}
                continue
            # A label may have been submitted instead of a slug; slugify to match
            # the stored attribute key (record_attrs is keyed by slug, not label).
            target_slug = sel if sel in valid_field_slugs else _slugify_underscore(sel)
            if target_slug not in valid_field_slugs:
                form_errors.append(
                    f"Row {row_no}: API field '{prop_name}' maps input "
                    f"'{input_name}' to unknown field '{sel}'."
                )
                continue
            parsed_map[input_name] = {"source": "field", "field": target_slug}
        if parsed_map:
            properties[prop_name]["x-api-map"] = parsed_map

        # --- x-api-outputs: validate the ticked output subset against the
        # connector's catalog (the headline OUTPUTS feature). Each raw entry is
        # {output, label, field_type}. An output name must exist in the connector's
        # _connector_outputs(); field_type defaults from the output.type
        # (number->Number, string->Text, date->Date, series->Time-Series) and a
        # 'series' output is FORCED to Time-Series (it cannot render as a scalar);
        # a value output is never allowed Time-Series. If NONE are ticked, fall back
        # to the connector's PRIMARY output so a record still renders something. ---
        cfg = _connector_config_by_name(conn_name)
        catalog = _connector_outputs(cfg)
        by_name = {(o.get("name") or "").strip(): o for o in catalog
                   if isinstance(o, dict) and (o.get("name") or "").strip()}
        x_outputs = []
        for entry in (raw_outputs or []):
            if not isinstance(entry, dict):
                continue
            oname = (entry.get("output") or "").strip()
            spec = by_name.get(oname)
            if spec is None:
                form_errors.append(
                    f"Row {row_no}: API field '{prop_name}' ticks unknown output "
                    f"'{oname}' for connector '{conn_name}'."
                )
                continue
            okind = (spec.get("kind") or "value").lower()
            otype = (spec.get("type") or
                     ("series" if okind == "series" else "string")).lower()
            default_ft = ("Time-Series" if okind == "series"
                          else _OUTPUT_TYPE_TO_FIELD.get(otype, "Text"))
            ft = (entry.get("field_type") or "").strip() or default_ft
            if ft not in _OUTPUT_FIELD_TYPES:
                ft = default_ft
            # Guard the asymmetric type rule: a series output can ONLY render as a
            # Time-Series chart; an image output can ONLY render as an Image; a value
            # output can NEVER be a Time-Series or Image.
            if okind == "series":
                ft = "Time-Series"
            elif okind == "image":
                ft = "Image"
            elif ft in ("Time-Series", "Image"):
                ft = default_ft
            label = (entry.get("label") or "").strip() or oname
            x_outputs.append({"output": oname, "label": label, "field_type": ft})
        if not x_outputs:
            # No subset ticked => default to the connector's primary output so the
            # record detail still renders a live value/chart (back-compat with the
            # single-output behavior).
            primary = _primary_output(catalog)
            if primary is not None:
                pkind = (primary.get("kind") or "value").lower()
                ptype = (primary.get("type") or
                         ("series" if pkind == "series" else "string")).lower()
                pft = ("Time-Series" if pkind == "series"
                       else _OUTPUT_TYPE_TO_FIELD.get(ptype, "Text"))
                x_outputs = [{
                    "output": (primary.get("name") or "").strip(),
                    "label": properties[prop_name].get("title") or prop_name,
                    "field_type": pft,
                }]
        if x_outputs:
            properties[prop_name]["x-api-outputs"] = x_outputs

    # --- THIRD PASS: Python-script fields. Build x-script-outputs (keyed 'name' — the
    # key _compute_scripts reads) from the ticked outputs, and emit one read-only
    # COMPUTED property per output (typed) so the outputs are first-class fields
    # (columns, Data API catalog, filtering). Added BEFORE x-order is built below. ---
    for row_no, prop_name, raw_outputs in script_rows:
        x_outputs = []
        for o in raw_outputs:
            if not isinstance(o, dict):
                continue
            oname = _slugify_underscore(o.get("output") or o.get("name") or "")
            if not oname:
                continue
            ft = (o.get("field_type") or "Text").strip()
            if ft not in _SCRIPT_OUTPUT_FIELD_TYPES:
                ft = "Text"
            label = (o.get("label") or oname.replace("_", " ").title()).strip()
            x_outputs.append({"name": oname, "label": label, "field_type": ft})
            if oname not in properties:        # one read-only computed property per output
                properties[oname] = {"type": _script_output_json_type(ft),
                                     "x-computed": True, "x-computed-by": prop_name,
                                     "title": label}
                seen.add(oname)
        properties[prop_name]["x-script-outputs"] = x_outputs

    if not properties:
        form_errors.append("Add at least one field (a row with a Label).")

    spec = None
    if not form_errors:
        # x-order PRESERVES field declaration order: properties is stored as JSONB,
        # which does NOT keep object-key order (reorders by key length then bytes),
        # so an explicit ordered key list is the source of truth for field order.
        field_schema = {"type": "object", "properties": properties,
                        "x-order": list(properties.keys())}
        if required:
            field_schema["required"] = required
        # Record TITLE field: the field whose value names each record (shown as the
        # detail H1, link labels, pickers). Stored only when it resolves to a real
        # (non-layout) property of this type.
        title_field = _slugify_underscore(post.get("title_field") or "")
        if title_field in properties and not (properties[title_field] or {}).get("x-layout"):
            field_schema["x-title-field"] = title_field
        # Naming series: a pattern (e.g. "INV-#####") whose '#' run is the zero-padded
        # auto-increment counter. On create, the next value fills the title field when
        # it's left blank. Stored only when the pattern has a '#' run AND a title field
        # exists to receive it.
        naming = (post.get("naming_series") or "").strip()
        if naming and "#" in naming and field_schema.get("x-title-field"):
            field_schema["x-naming"] = naming
        # Role permissions: read/write group allow-lists. Stored only when non-empty
        # (an absent action = open to every logged-in user; superuser/staff bypass).
        getlist = getattr(post, "getlist", None)
        read_g = [g.strip() for g in (getlist("perm_read") if getlist else []) if g.strip()]
        write_g = [g.strip() for g in (getlist("perm_write") if getlist else []) if g.strip()]
        if read_g or write_g:
            perms = {}
            if read_g:
                perms["read"] = read_g
            if write_g:
                perms["write"] = write_g
            field_schema["x-permissions"] = perms
        spec = {
            "slug": slug,
            "display_name": type_name,
            "version": 1,
            "field_schema": field_schema,
            "geometry_kind": None if geometry in ("none", "", "None") else geometry,
            "timeseries_policy": "inline",
        }
    return spec, form_errors, type_name, geometry, rows, row_count, slug


@controller(name="new_type", url="new-type", title="New HydroType")
def new_hydrotype(request):
    """DocType Builder: define a NEW HydroType in a UI form and create it.

    GET  -> render the builder with a FIXED set of 7 blank field rows.
    POST -> _assemble_type_spec -> registry.import_hydrotype(overwrite=False) (rejects
            a duplicate slug). On success redirect to the new type's List view; on
            error re-render with messages + entries.
    """
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    if request.method != "POST":
        blank = [{"label": "", "type": "text", "options": "", "required": False}
                 for _ in range(_BUILDER_MIN_ROWS)]
        context = _builder_context([], "", "none", blank, _BUILDER_MIN_ROWS, mode="new")
        return render(request, "hydrodesk/new_type.html", context)

    spec, form_errors, type_name, geometry, rows, row_count, slug = \
        _assemble_type_spec(request.POST)
    if spec is not None:
        engine = App.get_persistent_store_database("hydro_db")
        try:
            with Session(engine) as session:
                _, created = registry.import_hydrotype(session, spec, overwrite=False)
        except ValueError as exc:  # malformed spec surfaced by validate_spec
            form_errors.append(str(exc))
        else:
            if created:
                return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))
            form_errors.append(f"A type with slug '{slug}' already exists.")

    context = _builder_context(form_errors, type_name, geometry, rows, row_count,
                               mode="new",
                               title_field=request.POST.get("title_field", ""),
                               naming_series=request.POST.get("naming_series", ""),
                               perms={"read": request.POST.getlist("perm_read"),
                                      "write": request.POST.getlist("perm_write")})
    return render(request, "hydrodesk/new_type.html", context)


@controller(name="edit_type", url="edit-type/{slug}", title="Edit HydroType")
def edit_hydrotype(request, slug="monitoring_station"):
    """DocType Builder in EDIT mode: load an existing HydroType's definition into
    the builder, let the user change its name/geometry/fields, and UPDATE it.

    The slug is IMMUTABLE (records reference it), so renaming the display name keeps
    the same slug. GET pre-fills the builder from the stored field_schema (reverse
    of _schema_for); POST upserts via import_hydrotype(overwrite=True) and bumps the
    version. NOTE: renaming a field's LABEL changes its derived key, so data stored
    under the old key on existing records is no longer shown (a rename-field caveat).
    """
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        cur_version = session.execute(
            select(m.HydroType.version).where(m.HydroType.slug == slug)
        ).scalar()
    if meta is None:
        return redirect(reverse("hydrodesk:home"))
    display_name, field_schema, geometry_kind = meta

    if request.method == "POST":
        spec, form_errors, type_name, geometry, rows, row_count, _slug = \
            _assemble_type_spec(request.POST, force_slug=slug)
        if spec is not None:
            spec["version"] = (cur_version or 1) + 1   # bump on every edit
            try:
                with Session(engine) as session:
                    registry.import_hydrotype(session, spec, overwrite=True)
            except ValueError as exc:
                form_errors.append(str(exc))
            else:
                return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))
        context = _builder_context(form_errors, type_name, geometry, rows, row_count,
                                   mode="edit", slug=slug,
                                   title_field=request.POST.get("title_field", ""),
                               naming_series=request.POST.get("naming_series", ""),
                                   perms={"read": request.POST.getlist("perm_read"),
                                          "write": request.POST.getlist("perm_write")})
        return render(request, "hydrodesk/new_type.html", context)

    # GET: reverse the stored schema back into editable builder rows.
    rows = _schema_to_builder_rows(field_schema)
    row_count = max(_BUILDER_MIN_ROWS, len(rows))
    geometry = geometry_kind or "none"
    context = _builder_context([], display_name, geometry, rows, row_count,
                               mode="edit", slug=slug,
                               title_field=(field_schema or {}).get("x-title-field", ""),
                               naming_series=(field_schema or {}).get("x-naming", ""),
                               perms=(field_schema or {}).get("x-permissions"))
    return render(request, "hydrodesk/new_type.html", context)


def _dependent_types(session, slug):
    """Other HydroTypes whose schema REFERENCES ``slug`` — via a Link field
    (x-link-type) or a linked Table field (x-child-type). Returned as
    ``[{slug, display_name, field, kind}]`` so the delete confirmation can warn that
    those references will dangle (they degrade gracefully, but the operator should
    know)."""
    out = []
    rows = session.execute(select(
        m.HydroType.slug, m.HydroType.display_name, m.HydroType.field_schema)).all()
    for s, dn, fs in rows:
        if s == slug:
            continue
        for k, prop in ((fs or {}).get("properties") or {}).items():
            prop = prop or {}
            if prop.get("x-link-type") == slug:
                out.append({"slug": s, "display_name": dn,
                            "field": prop.get("title") or k, "kind": "Link"})
            elif prop.get("x-child-type") == slug:
                out.append({"slug": s, "display_name": dn,
                            "field": prop.get("title") or k, "kind": "Table"})
    return out


@controller(name="delete_type", url="delete-type/{slug}", title="Delete HydroType")
def delete_hydrotype(request, slug="monitoring_station"):
    """Delete a whole HydroType (DocType) AND all of its records.

    GET  -> a confirmation page showing the record count + any other types that
            reference this one (their references will dangle, handled gracefully).
    POST -> delete every HydroRecord of this slug, then the HydroType row, and
            redirect Home. Records of OTHER types are never touched (a linked child
            of a different type is left intact, just unparented).
    """
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is None:
            return redirect(reverse("hydrodesk:home"))
        display_name, _field_schema, _gk = meta

        if request.method == "POST":
            session.execute(
                delete(m.HydroRecord).where(m.HydroRecord.hydrotype_slug == slug))
            ht = session.execute(
                select(m.HydroType).where(m.HydroType.slug == slug)
            ).scalar_one_or_none()
            if ht is not None:
                session.delete(ht)
            session.commit()
            return redirect(reverse("hydrodesk:home"))

        record_count = session.execute(
            select(func.count()).select_from(m.HydroRecord)
            .where(m.HydroRecord.hydrotype_slug == slug)
        ).scalar() or 0
        dependents = _dependent_types(session, slug)

    return render(request, "hydrodesk/delete_type.html", {
        "slug": slug,
        "display_name": display_name,
        "record_count": record_count,
        "dependents": dependents,
        "form_action": reverse("hydrodesk:delete_type", kwargs={"slug": slug}),
        "cancel_url": reverse("hydrodesk:edit_type", kwargs={"slug": slug}),
        "home_url": reverse("hydrodesk:home"),
        "page_title": "Delete " + str(display_name),
    })


def _unique_slug(session, base):
    """Return a slug not already used by a HydroType, appending _2, _3, … on a
    collision. Used by Duplicate so the clone never clobbers an existing type."""
    base = base or "type"
    candidate = base
    n = 2
    while session.execute(
            select(m.HydroType.slug).where(m.HydroType.slug == candidate)).first():
        candidate = f"{base}_{n}"
        n += 1
    return candidate


@controller(name="doctypes", url="doctypes", title="DocTypes")
def doctypes_list(request):
    """Management list of every HydroType (DocType): name, slug, geometry, field
    count, record count, with per-row Open/Edit/Duplicate/Delete and select +
    bulk-delete. The home page shows the same types as cards; this is the tabular
    management surface."""
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    engine = App.get_persistent_store_database("hydro_db")
    types = []
    with Session(engine) as session:
        rows = session.execute(select(
            m.HydroType.slug, m.HydroType.display_name, m.HydroType.geometry_kind,
            m.HydroType.field_schema, m.HydroType.version,
        ).order_by(m.HydroType.display_name)).all()
        for slug, dn, gk, fs, ver in rows:
            n_fields = sum(1 for _k, p in _ordered_props(fs)
                           if not (p or {}).get("x-layout"))
            count = session.execute(
                select(func.count()).select_from(m.HydroRecord)
                .where(m.HydroRecord.hydrotype_slug == slug)).scalar() or 0
            types.append({
                "slug": slug, "display_name": dn, "geometry_kind": gk or "—",
                "field_count": n_fields, "record_count": count, "version": ver or 1,
            })
    return render(request, "hydrodesk/doctypes.html", {
        "types": types, "total": len(types),
        "bulk_delete_url": reverse("hydrodesk:doctypes_bulk_delete"),
    })


@controller(name="doctypes_bulk_delete", url="doctypes/bulk-delete",
            title="Delete DocTypes")
def doctypes_bulk_delete(request):
    """Bulk-delete the SELECTED HydroTypes AND all their records (POST only). The
    list posts ticked rows as repeated ``slugs``; records of the deleted types go
    too. Records of OTHER types are never touched."""
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    if request.method == "POST":
        slugs = [s.strip() for s in request.POST.getlist("slugs") if s.strip()]
        if slugs:
            engine = App.get_persistent_store_database("hydro_db")
            with Session(engine) as session:
                session.execute(delete(m.HydroRecord)
                                .where(m.HydroRecord.hydrotype_slug.in_(slugs)))
                session.execute(delete(m.HydroType)
                                .where(m.HydroType.slug.in_(slugs)))
                session.commit()
    return redirect(reverse("hydrodesk:doctypes"))


@controller(name="duplicate_type", url="doctypes/{slug}/duplicate",
            title="Duplicate DocType")
def duplicate_hydrotype(request, slug="monitoring_station"):
    """Clone a HydroType's full definition under a new name/slug ('<name> Copy'),
    then open it in the edit builder. No records are copied (POST only)."""
    if not _can_build(request):
        return _denied(request, "manage", "DocTypes")
    if request.method != "POST":
        return redirect(reverse("hydrodesk:doctypes"))
    engine = App.get_persistent_store_database("hydro_db")
    with Session(engine) as session:
        src = session.execute(
            select(m.HydroType).where(m.HydroType.slug == slug)).scalar_one_or_none()
        if src is None:
            return redirect(reverse("hydrodesk:doctypes"))
        new_name = f"{src.display_name} Copy"
        new_slug = _unique_slug(session, _slugify_underscore(new_name))
        session.add(m.HydroType(
            slug=new_slug, display_name=new_name, version=1,
            field_schema=src.field_schema, geometry_kind=src.geometry_kind,
            timeseries_policy=src.timeseries_policy, workflow=src.workflow))
        session.commit()
    return redirect(reverse("hydrodesk:edit_type", kwargs={"slug": new_slug}))


# ===========================================================================
# CREDENTIALS — a small CRUD over the hydro_credential secrets store. The secret
# is WRITE-ONLY in the UI: it is never echoed back (the list shows a mask, the
# edit form shows a '••••' placeholder and only overwrites when a new value is
# typed). Secrets never reach the test-flow JSON or any rendered HTML.
# ===========================================================================

def _connector_name_exists(name):
    """True iff a HydroConnector with this exact name exists (builder API-field
    guard). Own short-lived session; safe False on any store error."""
    if not name:
        return False
    try:
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            row = session.execute(
                select(m.HydroConnector.id)
                .where(m.HydroConnector.name == name)
            ).first()
        return row is not None
    except Exception:
        return False


def _connector_config_by_name(name):
    """Load a connector's config dict by NAME (own short-lived session), or {} on
    any miss/error. Used by the x-api-outputs second pass to resolve the connector's
    outputs catalog for validation + the primary-output fallback."""
    if not name:
        return {}
    try:
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            conn = _load_connector(session, name)
            return (conn.config or {}) if conn is not None else {}
    except Exception:
        return {}


def _connector_field_inputs(name):
    """Return [(input_name, required_bool)] for a connector's source=='field'
    inputs (the ones an API DocType field must map via x-api-map). For a legacy
    no-inputs connector, synthesize from the url_template/headers/query {tokens}
    (each treated as a required field input). Empty list on any miss."""
    if not name:
        return []
    try:
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            conn = _load_connector(session, name)
            cfg = (conn.config or {}) if conn is not None else None
    except Exception:
        cfg = None
    if cfg is None:
        return []
    declared = cfg.get("inputs") or []
    out = []
    if declared:
        for inp in declared:
            if not isinstance(inp, dict):
                continue
            if (inp.get("source") or "field").strip().lower() != "field":
                continue
            iname = (inp.get("name") or "").strip()
            if iname:
                out.append((iname, bool(inp.get("required"))))
    else:
        seen = set()
        blobs = [cfg.get("url_template") or ""]
        blobs += [str(v) for v in (cfg.get("query") or {}).values()]
        blobs += [str(v) for v in (cfg.get("headers") or {}).values()]
        for blob in blobs:
            for tok in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", blob):
                if tok not in seen:
                    seen.add(tok)
                    out.append((tok, True))
    return out


def _mask_secret(secret):
    """Render a secret as a masked hint for the list view — never the real value.
    Shows only that a secret is set (and a length-ish dot run), never any chars."""
    if not secret:
        return ""
    return "•" * 8


@controller(name="credentials", url="credentials", title="Credentials")
def credentials(request):
    """List + create named credentials (the secrets store).

    GET  -> render the list (secrets MASKED) + an inline 'add' form.
    POST -> create a credential (name + secret), then redirect (PRG). A duplicate
            name is reported as a form error.
    """
    engine = App.get_persistent_store_database("hydro_db")
    form_errors = []

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        secret = request.POST.get("secret") or ""
        if not name:
            form_errors.append("Name is required.")
        if not form_errors:
            with Session(engine) as session:
                exists = session.execute(
                    select(m.HydroCredential.id)
                    .where(m.HydroCredential.name == name)
                ).first()
                if exists is not None:
                    form_errors.append(f"A credential named '{name}' already exists.")
                else:
                    session.add(m.HydroCredential(name=name, secret=secret))
                    session.commit()
                    return redirect(reverse("hydrodesk:credentials"))

    rows = []
    with Session(engine) as session:
        for cid, name, secret, created in session.execute(
            select(m.HydroCredential.id, m.HydroCredential.name,
                   m.HydroCredential.secret, m.HydroCredential.created_at)
            .order_by(m.HydroCredential.name)
        ).all():
            rows.append({
                "id": str(cid),
                "name": name,
                "masked": _mask_secret(secret),
                "has_secret": bool(secret),
                "created_at": created,
            })

    return render(request, "hydrodesk/credentials.html", {
        "rows": rows,
        "form_errors": form_errors,
        "record_count": len(rows),
        "form_name": request.POST.get("name", "") if form_errors else "",
    })


@controller(name="credential_delete", url="credentials/{cred_id}/delete",
            title="Delete Credential")
def credential_delete(request, cred_id=None):
    """Delete a credential (POST only), then redirect to the list."""
    if request.method == "POST":
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            cred = session.execute(
                select(m.HydroCredential)
                .where(m.HydroCredential.id == cred_id)
            ).scalar_one_or_none()
            if cred is not None:
                session.delete(cred)
                session.commit()
    return redirect(reverse("hydrodesk:credentials"))


# ===========================================================================
# CONNECTORS — list + builder (create/edit) over hydro_connector, plus a JSON
# test endpoint that fetches a connector with sample field values and returns the
# RAW JSON (secret-redacted) for the clickable path-picker tree.
# ===========================================================================

# Connector config keys parsed from the builder form. Auth/headers/query/paths
# all live under config; name is the row's unique key.
_CONNECTOR_RESULT_KINDS = ("value", "series", "json")
_CONNECTOR_AUTH_SCHEMES = ("none", "api_key", "bearer", "basic")
_CONNECTOR_METHODS = ("GET", "POST")

# Sourced-inputs editor knobs (the Inputs table on the connector builder).
_CONNECTOR_MAX_INPUTS = 32
_CONNECTOR_INPUT_TYPES = ("string", "number", "date", "enum")
_CONNECTOR_INPUT_SOURCES = ("field", "constant", "runtime", "default")
_CONNECTOR_INPUT_PLACEMENTS = ("url", "query", "header", "path")

# Outputs editor knobs (the Outputs table on the connector builder). An output is
# a 'value' (a scalar leaf at ``path``) or a 'series' (an array collapsed to two
# variables x+y via the '*' wildcard). ``type`` is the connector-side hint that
# seeds the doctype checklist's render-mode default.
_CONNECTOR_MAX_OUTPUTS = 32
_CONNECTOR_OUTPUT_KINDS = ("value", "series")
_CONNECTOR_OUTPUT_TYPES = ("string", "number", "date", "series")


def _parse_inputs_rows(post):
    """Reconstruct the connector ``inputs[]`` from the builder POST.

    Inputs editor rows are named input_name_<i>/input_label_<i>/input_type_<i>/
    input_source_<i>/input_field_<i>/input_value_<i>/input_default_<i>/
    input_required_<i>/input_in_<i>/input_options_<i> (the last only for enum). A
    row is 'real' only when its ``name`` token is non-empty. Returns a list of
    input dicts in the connector's inputs[] shape (an EMPTY list when the user
    cleared the editor, which fetch_api treats as 'no inputs' => back-compat
    token-scan)."""
    inputs = []
    for i in range(_CONNECTOR_MAX_INPUTS):
        name = (post.get(f"input_name_{i}") or "").strip()
        if not name:
            continue
        itype = (post.get(f"input_type_{i}") or "string").strip().lower()
        if itype not in _CONNECTOR_INPUT_TYPES:
            itype = "string"
        source = (post.get(f"input_source_{i}") or "field").strip().lower()
        if source not in _CONNECTOR_INPUT_SOURCES:
            source = "field"
        placement = (post.get(f"input_in_{i}") or "url").strip().lower()
        if placement not in _CONNECTOR_INPUT_PLACEMENTS:
            placement = "url"
        inp = {
            "name": name,
            "label": (post.get(f"input_label_{i}") or "").strip(),
            "type": itype,
            "source": source,
            "field": (post.get(f"input_field_{i}") or "").strip(),
            "value": (post.get(f"input_value_{i}") or "").strip(),
            "default": (post.get(f"input_default_{i}") or "").strip(),
            "required": post.get(f"input_required_{i}") is not None,
            "in": placement,
        }
        if itype == "enum":
            opts = [o.strip() for o in
                    (post.get(f"input_options_{i}") or "").split(",") if o.strip()]
            if opts:
                inp["options"] = opts
        inputs.append(inp)
    return inputs


def _inputs_for_form(config):
    """Serialize a connector's stored inputs[] back into rows the builder template
    re-renders (each row carries an ``options_csv`` string for the enum editor)."""
    rows = []
    for inp in ((config or {}).get("inputs") or []):
        if not isinstance(inp, dict):
            continue
        rows.append({
            "name": inp.get("name", ""),
            "label": inp.get("label", ""),
            "type": inp.get("type", "string"),
            "source": inp.get("source", "field"),
            "field": inp.get("field", ""),
            "value": inp.get("value", ""),
            "default": inp.get("default", ""),
            "required": bool(inp.get("required")),
            "in": inp.get("in", "url"),
            "options_csv": ", ".join(inp.get("options") or []),
        })
    return rows


def _parse_outputs_rows(post):
    """Reconstruct the connector ``outputs[]`` from the builder POST.

    Outputs editor rows are named output_name_<i>/output_kind_<i>/output_path_<i>/
    output_xpath_<i>/output_ypath_<i>/output_type_<i>/output_unit_<i>/
    output_primary_<i>. A row is 'real' only when its ``name`` token is non-empty.
    A 'series' row carries x_path+y_path (the array collapsed to two variables); a
    'value' row carries a single ``path``. Exactly ONE primary is kept (the radio's
    selection, named ``output_primary`` carrying the chosen index; if absent the
    first row is primary). Returns a list of output dicts in the connector's
    outputs[] shape (an EMPTY list when the editor is cleared, which the engine
    treats as 'use the legacy single output' => back-compat)."""
    rows = []
    primary_idx = (post.get("output_primary") or "").strip()
    for i in range(_CONNECTOR_MAX_OUTPUTS):
        name = (post.get(f"output_name_{i}") or "").strip()
        if not name:
            continue
        kind = (post.get(f"output_kind_{i}") or "value").strip().lower()
        if kind not in _CONNECTOR_OUTPUT_KINDS:
            kind = "value"
        otype = (post.get(f"output_type_{i}") or "").strip().lower()
        if otype not in _CONNECTOR_OUTPUT_TYPES:
            otype = "series" if kind == "series" else "string"
        out = {
            "name": name,
            "kind": kind,
            "type": otype,
            "unit": (post.get(f"output_unit_{i}") or "").strip(),
        }
        if kind == "series":
            # The modern shape: an array node captured as N variables (columns).
            # output_arraypath_<i> = the '*'-wildcard array path; output_vars_<i> =
            # a JSON list of {name, path} (one per captured sub-key).
            out["array_path"] = (post.get(f"output_arraypath_{i}") or "").strip()
            variables = []
            raw_vars = (post.get(f"output_vars_{i}") or "").strip()
            if raw_vars:
                try:
                    parsed = json.loads(raw_vars)
                except (ValueError, TypeError):
                    parsed = None
                for v in (parsed or []):
                    if isinstance(v, dict) and (v.get("path") or "").strip():
                        variables.append({
                            "name": (v.get("name") or _last_seg(v["path"]) or "var"),
                            "path": v["path"].strip()})
            # Legacy carriers (older saved forms) -> fold into variables.
            if not variables:
                xp = (post.get(f"output_xpath_{i}") or "").strip()
                yp = (post.get(f"output_ypath_{i}") or "").strip()
                if xp:
                    variables.append({"name": _last_seg(xp) or "time", "path": xp})
                if yp:
                    variables.append({"name": _last_seg(yp) or "value", "path": yp})
            out["variables"] = variables
            if not out["array_path"]:
                out["array_path"] = _array_path_from_vars(variables)
            out["type"] = "series"  # a series output always renders as a table
        else:
            out["path"] = (post.get(f"output_path_{i}") or "").strip()
        out["primary"] = (primary_idx == str(i))
        rows.append((i, out))
    # If the user never picked a primary radio (or it pointed at a removed row),
    # promote the first real row so a back-compat single-output read still works.
    if rows and not any(o["primary"] for _, o in rows):
        rows[0][1]["primary"] = True
    return [o for _, o in rows]


def _outputs_for_form(config):
    """Serialize a connector's outputs[] back into rows the builder re-renders.

    Seeds the Outputs editor from the stored outputs[] when present; otherwise
    SYNTHESIZES the single legacy output (via _connector_outputs) so a legacy
    connector opens with one editable output row instead of an empty table."""
    rows = []
    for out in _connector_outputs(config or {}):
        if not isinstance(out, dict):
            continue
        kind = (out.get("kind") or "value").lower()
        variables = _series_variables(out) if kind == "series" else []
        rows.append({
            "name": out.get("name", ""),
            "kind": kind,
            "path": out.get("path", "") if kind == "value" else "",
            # Series carriers: the array path + a JSON list of captured variables
            # (legacy x/y is normalized into variables by _series_variables).
            "array_path": (out.get("array_path") or _array_path_from_vars(variables)
                           if kind == "series" else ""),
            "vars_json": json.dumps(variables) if kind == "series" else "",
            "type": out.get("type", "series" if kind == "series" else "string"),
            "unit": out.get("unit", ""),
            "primary": bool(out.get("primary")),
        })
    # Guarantee at least one primary so the radio always has a selection.
    if rows and not any(r["primary"] for r in rows):
        rows[0]["primary"] = True
    return rows


def _parse_json_field(raw, default):
    """Parse a small JSON object from a form textarea, tolerating blank -> default.
    Returns (value, error_or_None). Non-object JSON is rejected."""
    raw = (raw or "").strip()
    if not raw:
        return dict(default), None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return None, f"invalid JSON ({exc})"
    if not isinstance(val, dict):
        return None, "must be a JSON object ({...})"
    return val, None


def _parse_record_params(post, config, files=None):
    """Shared per-record region/time fields for the geospatial connectors (WCS, GEE, REST):
    a checkbox 'pr_region' opts the region into the record's shapefile (spatial=shapefile);
    'pr_time_source'=range + pr_time_start/pr_time_end (literals or {field} tokens) define
    the per-record date window. A 'Test region' (an uploaded shapefile OR a pasted polygon)
    is stored as config['polygon'] — a STAND-IN the Test button uses when there's no record
    (a real record's shapefile always takes priority in the resolver). Lets the x-nc-map
    builder mapping drive these kinds too."""
    files = files or {}
    if (post.get("pr_region") or "").strip().lower() in ("1", "on", "true", "shapefile", "yes"):
        config["spatial"] = "shapefile"
    config["time_source"] = (post.get("pr_time_source") or "none").strip().lower()
    config["time_start"] = (post.get("pr_time_start") or "").strip()
    config["time_end"] = (post.get("pr_time_end") or "").strip()
    # Test-region stand-in geometry: an uploaded shapefile (-> inline GeoJSON) wins; else
    # a pasted WKT/GeoJSON polygon. Stored in 'polygon' (the resolver's fallback source).
    up = files.get("pr_shapefile_file")
    pasted = (post.get("pr_polygon") or "").strip()
    if up is not None:
        gj = _shapefile_to_featurecollection(up)
        if gj:
            config["polygon"] = gj
        elif pasted:
            config["polygon"] = pasted
    elif pasted:
        config["polygon"] = pasted


def _connector_config_from_post(post, files=None):
    """Assemble a HydroConnector.config dict from the builder POST, with the auth
    block, paths, and templated headers/query. Returns (name, config, errors).

    ``files`` is request.FILES — an uploaded shapefile (zip or .shp) is IMPORTED
    here (converted to inline GeoJSON in config['polygon']) for the polygon mode."""
    files = files or {}
    errors = []
    name = (post.get("name") or "").strip()
    if not name:
        errors.append("Connector Name is required.")

    method = (post.get("method") or "GET").strip().upper()
    if method not in _CONNECTOR_METHODS:
        method = "GET"
    result_kind = (post.get("result_kind") or "value").strip().lower()
    if result_kind not in _CONNECTOR_RESULT_KINDS:
        result_kind = "value"
    auth_scheme = (post.get("auth_scheme") or "none").strip().lower()
    if auth_scheme not in _CONNECTOR_AUTH_SCHEMES:
        auth_scheme = "none"

    headers, herr = _parse_json_field(post.get("headers"), {})
    if herr:
        errors.append(f"Headers {herr}.")
        headers = {}
    query, qerr = _parse_json_field(post.get("query"), {})
    if qerr:
        errors.append(f"Query {qerr}.")
        query = {}

    try:
        ttl = int((post.get("ttl_seconds") or "900").strip() or 900)
    except ValueError:
        ttl = 900
    try:
        timeout = int((post.get("timeout") or "15").strip() or 15)
    except ValueError:
        timeout = 15

    kind = (post.get("kind") or "rest").strip().lower()
    if kind not in ("rest", "netcdf", "thredds", "csv", "wms", "wcs", "gee"):
        kind = "rest"

    inputs = _parse_inputs_rows(post)
    outputs = _parse_outputs_rows(post)

    config = {
        "kind": kind,
        "url_template": (post.get("url_template") or "").strip(),
        "method": method,
        "headers": headers,
        "query": query,
        "auth": {
            "scheme": auth_scheme,
            "credential": (post.get("auth_credential") or "").strip(),
            "placement": (post.get("auth_placement") or "header").strip().lower(),
            "param": (post.get("auth_param") or "").strip(),
        },
        "result_kind": result_kind,
        "output_path": (post.get("output_path") or "").strip(),
        "x_path": (post.get("x_path") or "").strip(),
        "y_path": (post.get("y_path") or "").strip(),
        "ttl_seconds": ttl,
        "timeout": timeout,
    }
    if kind in ("netcdf", "thredds"):
        # NetCDF / THREDDS source fields. The Variable field accepts a COMMA list of
        # variables; outputs are synthesized at fetch time — a combined table (all
        # variables along x_dim) when there are several, plus a series + a latest
        # value per variable. ``inputs`` (the shared editor) substitute {field} into
        # the dataset_url / catalog_url.
        config["dataset_url"] = (post.get("dataset_url") or "").strip()
        var_list = [v.strip() for v in (post.get("variable") or "").split(",")
                    if v.strip()]
        config["variables"] = var_list
        config["variable"] = var_list[0] if var_list else ""   # back-compat / describe
        config["x_dim"] = (post.get("x_dim") or "time").strip()
        config["unit"] = (post.get("nc_unit") or "").strip()
        # Derived (computed) variables: one "name = formula" per line, evaluated
        # row-wise over the real variables and added as extra table columns.
        derived = []
        for line in (post.get("nc_derived") or "").splitlines():
            name, sep, formula = line.partition("=")
            name, formula = name.strip(), formula.strip()
            if sep and name and formula:
                derived.append({"name": name, "formula": formula})
        config["derived"] = derived
        # Spatial filter: how the lat/lon axes are reduced. 'mean' (whole grid),
        # 'point' (nearest cell to lon/lat — blank lon/lat uses the record geometry),
        # 'bbox' (mean over a window — fixed, or DYNAMIC = the record's point ± buffer),
        # or 'polygon' (mean over the cells inside a shapefile/WKT region).
        # The builder offers two shapefile modes encoded in nc_spatial; split them into
        # spatial='shapefile' + shapefile_agg. Legacy modes (point/bbox/cells/zones) are
        # still accepted so a previously-saved connector edits/round-trips cleanly.
        spatial = (post.get("nc_spatial") or "shapefile_mean").strip().lower()
        if spatial in ("shapefile_mean", "shapefile_cells"):
            config["shapefile_agg"] = "cells" if spatial == "shapefile_cells" else "mean"
            spatial = "shapefile"
        else:
            config["shapefile_agg"] = (post.get("nc_shapefile_agg") or "mean").strip().lower()
        if spatial not in ("mean", "point", "bbox", "polygon", "cells", "zones", "shapefile"):
            spatial = "shapefile"
        config["spatial"] = spatial
        # Time filter: a date range (start/end may be literals or {field} tokens).
        config["time_source"] = (post.get("nc_time_source") or "none").strip().lower()
        config["time_start"] = (post.get("nc_time_start") or "").strip()
        config["time_end"] = (post.get("nc_time_end") or "").strip()
        config["lat_dim"] = (post.get("nc_lat_dim") or "").strip()
        config["lon_dim"] = (post.get("nc_lon_dim") or "").strip()
        try:
            config["cells_max"] = max(1, min(200, int(post.get("nc_cells_max") or 24)))
        except (TypeError, ValueError):
            config["cells_max"] = 24
        try:
            config["zones_max"] = max(1, min(500, int(post.get("nc_zones_max") or 60)))
        except (TypeError, ValueError):
            config["zones_max"] = 60
        for k in ("lat", "lon", "lat_min", "lat_max", "lon_min", "lon_max"):
            raw = (post.get("nc_" + k) or "").strip()
            if raw:
                try:
                    config[k] = float(raw)
                except ValueError:
                    pass
        config["polygon"] = (post.get("nc_polygon") or "").strip()      # WKT / GeoJSON
        config["shapefile"] = (post.get("nc_shapefile") or "").strip()  # server .shp path
        # 'zones' carries a GeoJSON FeatureCollection of MANY polygons (one zonal column
        # each); it round-trips through a hidden field so an edit without a re-upload
        # keeps it, and so the Test button (JSON fetch) can read it back.
        config["zones"] = (post.get("nc_zones") or "").strip()
        config["zone_label"] = (post.get("nc_zone_label") or "").strip()  # .dbf attr to label by
        # Where the zones come from: 'shapefile' (FIXED, imported/pasted/path) or
        # 'record' (DYNAMIC — each record's own geometry, the dynamic-input mode).
        config["zones_source"] = (post.get("nc_zones_source") or "shapefile").strip().lower()
        if config["zones_source"] not in ("shapefile", "record"):
            config["zones_source"] = "shapefile"
        # IMPORT an uploaded shapefile (zip or .shp). In 'zones' mode keep EVERY polygon
        # as a FeatureCollection (a grid/cell layer); otherwise keep the first polygon as
        # an inline GeoJSON region. Either way the connector stays a pure-JSON row.
        up = files.get("nc_shapefile_file")
        if up is not None:
            if spatial == "zones":
                fc = _shapefile_to_featurecollection(up)
                if fc:
                    config["zones"] = fc
                else:
                    errors.append("Could not read polygons from the uploaded shapefile "
                                  "(expected a .zip bundle of .shp/.shx/.dbf, or a .shp).")
            else:
                gj = _shapefile_upload_to_geojson(up)
                if gj:
                    config["polygon"] = gj
                else:
                    errors.append("Could not read a polygon from the uploaded shapefile "
                                  "(expected a .zip bundle of .shp/.shx/.dbf, or a .shp).")
        if spatial in ("point", "bbox", "polygon", "cells", "zones", "shapefile") and not (config["lat_dim"] and config["lon_dim"]):
            errors.append("A shapefile spatial filter needs the Lat dim and Lon dim names.")
        if spatial == "polygon" and not (config["polygon"] or config["shapefile"]):
            errors.append("A polygon spatial filter needs a WKT/GeoJSON polygon, "
                          "an uploaded shapefile, or a shapefile path.")
        if (spatial == "zones" and config["zones_source"] != "record"
                and not (config["zones"] or config["polygon"] or config["shapefile"])):
            errors.append("A per-zone spatial filter needs an imported multi-polygon "
                          "shapefile, a pasted FeatureCollection/MultiPolygon, a shapefile "
                          "path, or set Zones-from to the record's geometry.")
        if kind == "thredds":
            config["catalog_url"] = (post.get("catalog_url") or "").strip()
            config["dataset"] = (post.get("dataset") or "").strip()
        if not var_list:
            errors.append("A NetCDF/THREDDS connector needs at least one Variable name.")
        if kind == "netcdf" and not config["dataset_url"]:
            errors.append("A NetCDF connector needs a Dataset URL (.nc file or OPeNDAP/dodsC URL).")
        if kind == "thredds" and not config.get("catalog_url"):
            errors.append("A THREDDS connector needs a Catalog URL.")
    elif kind == "csv":
        # CSV source fields. The whole table is synthesized as one series + a latest
        # value (of value_column); no outputs[] editor needed.
        config["csv_url"] = (post.get("csv_url") or "").strip()
        config["delimiter"] = (post.get("delimiter") or ",").strip() or ","
        config["has_header"] = (post.get("has_header") or "true").strip().lower() != "false"
        config["value_column"] = (post.get("value_column") or "").strip()
        if not config["csv_url"]:
            errors.append("A CSV connector needs a CSV URL or file path.")
    elif kind == "wms":
        # WMS source fields. A GetMap image (centred on the record's point) +
        # a best-effort GetFeatureInfo value are synthesized.
        config["wms_url"] = (post.get("wms_url") or "").strip()
        config["layers"] = (post.get("layers") or "").strip()
        config["wms_version"] = (post.get("wms_version") or "1.3.0").strip()
        config["image_format"] = (post.get("image_format") or "image/png").strip()
        config["styles"] = (post.get("styles") or "").strip()
        config["crs"] = (post.get("crs") or "EPSG:4326").strip()
        try:
            config["bbox_buffer"] = float(post.get("bbox_buffer") or 0.5)
        except (TypeError, ValueError):
            config["bbox_buffer"] = 0.5
        try:
            config["width"] = int(post.get("wms_width") or 512)
        except (TypeError, ValueError):
            config["width"] = 512
        try:
            config["height"] = int(post.get("wms_height") or 384)
        except (TypeError, ValueError):
            config["height"] = 384
        # Optional map-centre fallback for NON-spatial doctypes (a point type uses
        # the record geom instead). Stored only when supplied so config stays clean.
        for k, src in (("default_lon", "default_lon"), ("default_lat", "default_lat")):
            raw = (post.get(src) or "").strip()
            if raw:
                try:
                    config[k] = float(raw)
                except ValueError:
                    pass
        if not config["wms_url"]:
            errors.append("A WMS connector needs a WMS service URL.")
        if not config["layers"]:
            errors.append("A WMS connector needs at least one layer name.")
    elif kind == "wcs":
        # WCS source fields. The coverage value (spatial mean near the point) + a
        # time series (if the coverage has a time axis) are synthesized.
        config["wcs_url"] = (post.get("wcs_url") or "").strip()
        config["coverage"] = (post.get("coverage") or "").strip()
        config["wcs_version"] = (post.get("wcs_version") or "2.0.1").strip()
        config["wcs_format"] = (post.get("wcs_format") or "application/netcdf").strip()
        config["variable"] = (post.get("wcs_variable") or "").strip()
        config["lon_axis"] = (post.get("lon_axis") or "Long").strip()
        config["lat_axis"] = (post.get("lat_axis") or "Lat").strip()
        config["extra_subset"] = (post.get("extra_subset") or "").strip()
        config["unit"] = (post.get("wcs_unit") or "").strip()
        config["time_axis"] = (post.get("pr_time_axis") or "ansi").strip()
        _parse_record_params(post, config, files)   # per-record shapefile region + date range
        try:
            config["bbox_buffer"] = float(post.get("wcs_bbox_buffer") or 0.25)
        except (TypeError, ValueError):
            config["bbox_buffer"] = 0.25
        for k in ("default_lon", "default_lat"):
            raw = (post.get(k) or "").strip()
            if raw:
                try:
                    config[k] = float(raw)
                except ValueError:
                    pass
        if not config["wcs_url"]:
            errors.append("A WCS connector needs a WCS service URL.")
        if not config["coverage"]:
            errors.append("A WCS connector needs a Coverage ID.")
    elif kind == "gee":
        # Earth Engine source fields. Samples an Image (value) or ImageCollection
        # (series) at the record's point. Needs the 'ee' package + a service-account
        # credential; degrades to no-data when either is absent.
        config["gee_asset"] = (post.get("gee_asset") or "").strip()
        config["gee_band"] = (post.get("gee_band") or "").strip()
        config["gee_reducer"] = (post.get("gee_reducer") or "first").strip().lower()
        config["gee_project"] = (post.get("gee_project") or "").strip()
        config["gee_credential"] = (post.get("gee_credential") or "").strip()
        config["gee_start"] = (post.get("gee_start") or "").strip()
        config["gee_end"] = (post.get("gee_end") or "").strip()
        config["unit"] = (post.get("gee_unit") or "").strip()
        config["gee_demo"] = (post.get("gee_demo") or "").strip().lower() in ("1", "on", "true", "yes")
        _parse_record_params(post, config, files)   # per-record shapefile region + date range
        try:
            config["gee_scale"] = int(post.get("gee_scale") or 30)
        except (TypeError, ValueError):
            config["gee_scale"] = 30
        for k in ("default_lon", "default_lat"):
            raw = (post.get(k) or "").strip()
            if raw:
                try:
                    config[k] = float(raw)
                except ValueError:
                    pass
        if not config["gee_asset"]:
            errors.append("A GEE connector needs an Earth Engine asset ID.")
    else:
        _parse_record_params(post, config, files)   # per-record {bbox}/{datetime} (OGC REST)
        # Generality knobs: a request BODY (POST/PUT/GraphQL), an Accept header, and
        # pagination (follow a next-link). All optional + stored only when set.
        body_tpl = (post.get("body_template") or "").strip()
        if body_tpl:
            config["body_template"] = body_tpl
            config["body_content_type"] = (post.get("body_content_type") or "application/json").strip()
        accept = (post.get("accept") or "").strip()
        if accept:
            config["accept"] = accept
        next_path = (post.get("paginate_next_path") or "").strip()
        if next_path:
            try:
                mp = max(1, min(50, int(post.get("paginate_max_pages") or 10)))
            except (TypeError, ValueError):
                mp = 10
            config["paginate"] = {"next_path": next_path, "max_pages": mp}
        if not config["url_template"]:
            errors.append("URL Template is required.")

    # Only attach inputs[] when the editor actually produced rows (REST). An ABSENT
    # key keeps a connector on the legacy implicit token-scan (full back-compat).
    if inputs:
        config["inputs"] = inputs
    # Attach outputs[] only when the editor produced rows; otherwise the primary
    # output is synthesized (REST: from result_kind/paths; NetCDF: from variable).
    if outputs and kind == "rest":
        config["outputs"] = outputs
    return name, config, errors


def _connector_form_context(mode, name, config, form_errors, conn_id=None,
                            credentials=None):
    """Shared context for the connector builder (new + edit)."""
    auth = (config or {}).get("auth") or {}
    if mode == "edit":
        form_action = reverse("hydrodesk:connector_edit", kwargs={"conn_id": conn_id})
    else:
        form_action = reverse("hydrodesk:connectors")
    # Map the stored spatial mode to the simplified builder select. spatial='shapefile'
    # -> one of the two shapefile options; any other stored mode renders as a 'legacy'
    # option so editing an older connector preserves it.
    _sp_raw = (config or {}).get("spatial", "")
    if _sp_raw == "shapefile":
        _sp_select = "shapefile_cells" if (config or {}).get("shapefile_agg") == "cells" else "shapefile_mean"
        _sp_legacy = ""
    elif _sp_raw in ("", "shapefile_mean", "shapefile_cells"):
        _sp_select = _sp_raw or "shapefile_mean"
        _sp_legacy = ""
    else:
        _sp_select = _sp_raw
        _sp_legacy = _sp_raw
    return {
        "mode": mode,
        "conn_id": conn_id,
        "form_action": form_action,
        "form_errors": form_errors,
        "name": name,
        "kind": (config or {}).get("kind", "rest"),
        "dataset_url": (config or {}).get("dataset_url", ""),
        "variable": (", ".join((config or {}).get("variables") or [])
                     or (config or {}).get("variable", "")),
        "x_dim": (config or {}).get("x_dim", "time"),
        "nc_unit": (config or {}).get("unit", ""),
        "nc_derived": "\n".join(
            (d.get("name", "") + " = " + d.get("formula", ""))
            for d in ((config or {}).get("derived") or [])
            if isinstance(d, dict)),
        "nc_spatial": _sp_select,
        "nc_spatial_legacy": _sp_legacy,
        "nc_time_source": (config or {}).get("time_source", "none"),
        "nc_time_start": (config or {}).get("time_start", ""),
        "nc_time_end": (config or {}).get("time_end", ""),
        "nc_lat_dim": (config or {}).get("lat_dim", ""),
        "nc_lon_dim": (config or {}).get("lon_dim", ""),
        "nc_lat": (config or {}).get("lat", "") if (config or {}).get("spatial") == "point" else "",
        "nc_lon": (config or {}).get("lon", "") if (config or {}).get("spatial") == "point" else "",
        "nc_lat_min": (config or {}).get("lat_min", ""),
        "nc_lat_max": (config or {}).get("lat_max", ""),
        "nc_lon_min": (config or {}).get("lon_min", ""),
        "nc_lon_max": (config or {}).get("lon_max", ""),
        "nc_polygon": (config or {}).get("polygon", ""),
        "nc_shapefile": (config or {}).get("shapefile", ""),
        "nc_zones": (config or {}).get("zones", ""),
        "nc_zone_label": (config or {}).get("zone_label", ""),
        "nc_zones_max": (config or {}).get("zones_max", 60),
        "nc_zones_source": (config or {}).get("zones_source", "shapefile"),
        "nc_cells_max": (config or {}).get("cells_max", 24),
        "catalog_url": (config or {}).get("catalog_url", ""),
        "dataset": (config or {}).get("dataset", ""),
        # CSV
        "csv_url": (config or {}).get("csv_url", ""),
        "delimiter": (config or {}).get("delimiter", ","),
        "has_header": (config or {}).get("has_header", True),
        "value_column": (config or {}).get("value_column", ""),
        # WMS
        "wms_url": (config or {}).get("wms_url", ""),
        "layers": (config or {}).get("layers", ""),
        "wms_version": (config or {}).get("wms_version", "1.3.0"),
        "image_format": (config or {}).get("image_format", "image/png"),
        "styles": (config or {}).get("styles", ""),
        "crs": (config or {}).get("crs", "EPSG:4326"),
        "bbox_buffer": (config or {}).get("bbox_buffer", 0.5),
        "wms_width": (config or {}).get("width", 512),
        "wms_height": (config or {}).get("height", 384),
        "default_lon": (config or {}).get("default_lon", ""),
        "default_lat": (config or {}).get("default_lat", ""),
        # WCS
        "wcs_url": (config or {}).get("wcs_url", ""),
        "coverage": (config or {}).get("coverage", ""),
        "wcs_version": (config or {}).get("wcs_version", "2.0.1"),
        "wcs_format": (config or {}).get("wcs_format", "application/netcdf"),
        "wcs_variable": (config or {}).get("variable", "") if (config or {}).get("kind") == "wcs" else "",
        "lon_axis": (config or {}).get("lon_axis", "Long"),
        "lat_axis": (config or {}).get("lat_axis", "Lat"),
        "extra_subset": (config or {}).get("extra_subset", ""),
        "wcs_unit": (config or {}).get("unit", "") if (config or {}).get("kind") == "wcs" else "",
        "wcs_bbox_buffer": (config or {}).get("bbox_buffer", 0.25) if (config or {}).get("kind") == "wcs" else 0.25,
        # GEE
        "gee_asset": (config or {}).get("gee_asset", ""),
        "gee_band": (config or {}).get("gee_band", ""),
        "gee_reducer": (config or {}).get("gee_reducer", "first"),
        "gee_project": (config or {}).get("gee_project", ""),
        "gee_credential": (config or {}).get("gee_credential", ""),
        "gee_start": (config or {}).get("gee_start", ""),
        "gee_end": (config or {}).get("gee_end", ""),
        "gee_scale": (config or {}).get("gee_scale", 30),
        "gee_unit": (config or {}).get("unit", "") if (config or {}).get("kind") == "gee" else "",
        "gee_demo": (config or {}).get("gee_demo", False),
        # Per-record region & time (WCS/GEE/REST): round-trip the shared pr_* controls.
        "pr_region": (config or {}).get("spatial") == "shapefile"
        if (config or {}).get("kind") in ("wcs", "gee", "rest") else False,
        "pr_time_source": (config or {}).get("time_source", "none")
        if (config or {}).get("kind") in ("wcs", "gee", "rest") else "none",
        "pr_time_start": (config or {}).get("time_start", "") if (config or {}).get("kind") in ("wcs", "gee", "rest") else "",
        "pr_time_end": (config or {}).get("time_end", "") if (config or {}).get("kind") in ("wcs", "gee", "rest") else "",
        "pr_polygon": (config or {}).get("polygon", "") if (config or {}).get("kind") in ("wcs", "gee", "rest") else "",
        "time_axis": (config or {}).get("time_axis", "ansi"),
        # REST generality knobs (body / pagination / Accept).
        "body_template": (config or {}).get("body_template", ""),
        "body_content_type": (config or {}).get("body_content_type", "application/json"),
        "accept": (config or {}).get("accept", ""),
        "paginate_next_path": ((config or {}).get("paginate") or {}).get("next_path", ""),
        "paginate_max_pages": ((config or {}).get("paginate") or {}).get("max_pages", 10),
        "url_template": (config or {}).get("url_template", ""),
        "method": (config or {}).get("method", "GET"),
        "headers": json.dumps((config or {}).get("headers") or {}, indent=2),
        "query": json.dumps((config or {}).get("query") or {}, indent=2),
        "auth_scheme": auth.get("scheme", "none"),
        "auth_credential": auth.get("credential", ""),
        "auth_placement": auth.get("placement", "header"),
        "auth_param": auth.get("param", ""),
        "result_kind": (config or {}).get("result_kind", "value"),
        "output_path": (config or {}).get("output_path", ""),
        "x_path": (config or {}).get("x_path", ""),
        "y_path": (config or {}).get("y_path", ""),
        "ttl_seconds": (config or {}).get("ttl_seconds", 900),
        "timeout": (config or {}).get("timeout", 15),
        "inputs": _inputs_for_form(config),
        "input_types": _CONNECTOR_INPUT_TYPES,
        "input_sources": _CONNECTOR_INPUT_SOURCES,
        "input_placements": _CONNECTOR_INPUT_PLACEMENTS,
        "outputs": _outputs_for_form(config),
        "output_kinds": _CONNECTOR_OUTPUT_KINDS,
        "output_types": _CONNECTOR_OUTPUT_TYPES,
        "credentials": credentials or [],
        "presets": [{"key": k, "label": v["label"],
                     "config": json.dumps(v["config"])}
                    for k, v in CONNECTOR_PRESETS.items()],
        "test_url": reverse("hydrodesk:connector_test"),
        "page_title": ("Edit Connector" if mode == "edit" else "New Connector"),
    }


def _credential_names(session):
    """Return the list of credential names (for the auth <select> in the builder)."""
    return [r[0] for r in session.execute(
        select(m.HydroCredential.name).order_by(m.HydroCredential.name)
    ).all()]


@controller(name="connectors", url="connectors", title="Connectors")
def connectors(request):
    """List connectors + create a new one.

    GET  -> if '?new' render the builder form; else render the connector list.
    POST -> assemble config from the builder, INSERT a new hydro_connector row,
            redirect to the list (PRG). Re-render with errors on failure.
    """
    engine = App.get_persistent_store_database("hydro_db")

    if request.method == "POST":
        name, config, form_errors = _connector_config_from_post(request.POST, request.FILES)
        if not form_errors:
            with Session(engine) as session:
                exists = session.execute(
                    select(m.HydroConnector.id)
                    .where(m.HydroConnector.name == name)
                ).first()
                if exists is not None:
                    form_errors.append(f"A connector named '{name}' already exists.")
                else:
                    session.add(m.HydroConnector(name=name, config=config))
                    session.commit()
                    return redirect(reverse("hydrodesk:connectors"))
        with Session(engine) as session:
            creds = _credential_names(session)
        return render(request, "hydrodesk/connector_form.html",
                      _connector_form_context("new", name, config, form_errors,
                                              credentials=creds))

    # GET — builder if ?new, else the list.
    if "new" in request.GET:
        with Session(engine) as session:
            creds = _credential_names(session)
        blank = {"method": "GET", "result_kind": "value", "ttl_seconds": 900,
                 "timeout": 15, "auth": {"scheme": "none", "placement": "header"}}
        return render(request, "hydrodesk/connector_form.html",
                      _connector_form_context("new", "", blank, [], credentials=creds))

    rows = []
    with Session(engine) as session:
        for cid, name, config, created in session.execute(
            select(m.HydroConnector.id, m.HydroConnector.name,
                   m.HydroConnector.config, m.HydroConnector.created_at)
            .order_by(m.HydroConnector.name)
        ).all():
            cfg = config or {}
            rows.append({
                "id": str(cid),
                "name": name,
                "url_template": cfg.get("url_template", ""),
                "result_kind": cfg.get("result_kind", "value"),
                "auth_scheme": (cfg.get("auth") or {}).get("scheme", "none"),
                "edit_url": reverse("hydrodesk:connector_edit", kwargs={"conn_id": str(cid)}),
                "delete_url": reverse("hydrodesk:connector_delete", kwargs={"conn_id": str(cid)}),
            })

    return render(request, "hydrodesk/connectors.html", {
        "rows": rows,
        "record_count": len(rows),
        "new_url": reverse("hydrodesk:connectors") + "?new",
    })


@controller(name="connector_edit", url="connectors/{conn_id}/edit",
            title="Edit Connector")
def connector_edit(request, conn_id=None):
    """Edit an existing connector. GET pre-fills the builder; POST UPDATEs config."""
    engine = App.get_persistent_store_database("hydro_db")

    if request.method == "POST":
        name, config, form_errors = _connector_config_from_post(request.POST, request.FILES)
        if not form_errors:
            with Session(engine) as session:
                conn = session.execute(
                    select(m.HydroConnector)
                    .where(m.HydroConnector.id == conn_id)
                ).scalar_one_or_none()
                if conn is None:
                    return redirect(reverse("hydrodesk:connectors"))
                # Guard a rename collision with another connector.
                clash = session.execute(
                    select(m.HydroConnector.id)
                    .where(m.HydroConnector.name == name)
                    .where(m.HydroConnector.id != conn_id)
                ).first()
                if clash is not None:
                    form_errors.append(f"A connector named '{name}' already exists.")
                else:
                    conn.name = name
                    conn.config = config
                    session.commit()
                    return redirect(reverse("hydrodesk:connectors"))
        with Session(engine) as session:
            creds = _credential_names(session)
        return render(request, "hydrodesk/connector_form.html",
                      _connector_form_context("edit", name, config, form_errors,
                                              conn_id=conn_id, credentials=creds))

    with Session(engine) as session:
        conn = session.execute(
            select(m.HydroConnector)
            .where(m.HydroConnector.id == conn_id)
        ).scalar_one_or_none()
        if conn is None:
            return redirect(reverse("hydrodesk:connectors"))
        name, config = conn.name, conn.config or {}
        creds = _credential_names(session)
    return render(request, "hydrodesk/connector_form.html",
                  _connector_form_context("edit", name, config, [],
                                          conn_id=conn_id, credentials=creds))


@controller(name="connector_delete", url="connectors/{conn_id}/delete",
            title="Delete Connector")
def connector_delete(request, conn_id=None):
    """Delete a connector (POST only), then redirect to the list."""
    if request.method == "POST":
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            conn = session.execute(
                select(m.HydroConnector)
                .where(m.HydroConnector.id == conn_id)
            ).scalar_one_or_none()
            if conn is not None:
                session.delete(conn)
                session.commit()
    return redirect(reverse("hydrodesk:connectors"))


@controller(name="connector_test", url="connectors/test", title="Test Connector")
def connector_test(request):
    """JSON test endpoint for the connector builder's 'Test' button.

    POST (CSRF-protected, sent via X-CSRFToken from the builder page) with a JSON
    body of {config:{...}, attrs:{...}} OR a form-encoded config + 'attrs' JSON.
    Performs ONE fetch_api with the trial config and sample field values, then
    returns the RAW parsed JSON so the client can render a clickable tree. The
    response REDACTS the resolved URL and NEVER includes the credential secret.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    # Accept either a JSON body (preferred from the builder fetch) or form fields.
    config = {}
    attrs = {}
    try:
        if request.content_type and "application/json" in request.content_type:
            body = json.loads(request.body.decode("utf-8") or "{}")
            config = body.get("config") or {}
            attrs = body.get("attrs") or {}
        else:
            name, config, _ = _connector_config_from_post(request.POST, request.FILES)
            attrs = json.loads(request.POST.get("attrs") or "{}")
    except (ValueError, TypeError) as exc:
        return JsonResponse({"ok": False, "error": f"bad request: {exc}"}, status=400)

    if not isinstance(attrs, dict):
        attrs = {}

    # DYNAMIC zones (zones_source='record') read the record's geometry from
    # attrs['_geojson']; the Test panel has no record, so use the pasted polygon/zones
    # as a STAND-IN geometry here (this is the only place that fallback is allowed —
    # real records never borrow connector-level zones).
    if (isinstance(config, dict)
            and (config.get("spatial") or "").lower() == "zones"
            and (config.get("zones_source") or "").lower() == "record"
            and not attrs.get("_geojson")):
        stand_in = config.get("polygon") or config.get("zones")
        if stand_in:
            attrs = dict(attrs)
            attrs["_geojson"] = stand_in

    result = fetch_api(config, attrs, connector_name=config.get("name") or "test")

    # RAW tree for the Test panel. REST: re-fetch the whole JSON (the tree-picker
    # needs it to pick paths). NetCDF/THREDDS: describe the dataset (its variables +
    # dimensions) so the user can see what to put in the Variable field.
    raw = None
    knd = (config.get("kind") or "rest").lower()
    if knd in ("netcdf", "thredds"):
        raw = _netcdf_describe(config, attrs)
    elif knd == "csv":
        raw = _csv_describe(config, attrs)
    elif knd == "wms":
        raw = _wms_describe(config, attrs)
    elif knd == "wcs":
        raw = _wcs_describe(config, attrs)
    elif knd == "gee":
        raw = _gee_describe(config, attrs)
    else:
        try:
            cfg_json = dict(config)
            cfg_json["result_kind"] = "json"
            cfg_json["output_path"] = ""
            # Drop outputs[] so the raw-JSON path triggers (else it extracts the
            # primary output). The Test tree needs the FULL response to pick from.
            cfg_json.pop("outputs", None)
            raw_result = fetch_api(cfg_json, attrs, connector_name=config.get("name") or "test")
            raw = raw_result.get("json")
        except Exception:
            raw = None

    return JsonResponse({
        "ok": True,
        "url": result.get("url"),          # secret-redacted
        "kind": result.get("kind"),
        "value": result.get("value") if result.get("kind") == "value" else None,
        "x": result.get("x") if result.get("kind") == "series" else None,
        "y": result.get("y") if result.get("kind") == "series" else None,
        "raw": raw,                         # full JSON for the clickable tree
    })


@controller(name="connector_inputs", url="connectors/{conn_name}/inputs",
            title="Connector Inputs")
def connector_inputs(request, conn_name=None):
    """Lightweight JSON: a connector's source=='field' inputs, for the DocType
    builder's per-doctype mapping UI (x-api-map).

    Returns ``{ok, name, inputs:[{name,label,source,field,required,type}]}`` where
    ``inputs`` is the subset of the connector's declared inputs[] whose source is
    'field' (the ones a doctype must MAP to one of its own fields). For a legacy
    connector that declares NO inputs[], the implicit {tokens} in its url_template
    are surfaced as synthetic field inputs so the mapping UI still works. NEVER
    echoes any secret (only input metadata). Degrades to ok:False/empty on a
    missing connector — the new_hydrotype server guard is authoritative."""
    name = (conn_name or "").strip()
    if not name:
        return JsonResponse({"ok": False, "name": "", "inputs": []})
    engine = App.get_persistent_store_database("hydro_db")
    cfg = None
    try:
        with Session(engine) as session:
            conn = _load_connector(session, name)
            if conn is not None:
                cfg = conn.config or {}
    except Exception:
        cfg = None
    if cfg is None:
        return JsonResponse({"ok": False, "name": name, "inputs": []})

    field_inputs = []
    declared = cfg.get("inputs") or []
    if declared:
        for inp in declared:
            if not isinstance(inp, dict):
                continue
            if (inp.get("source") or "field").strip().lower() != "field":
                continue
            iname = (inp.get("name") or "").strip()
            if not iname:
                continue
            field_inputs.append({
                "name": iname,
                "label": inp.get("label") or iname,
                "source": "field",
                "field": inp.get("field") or iname,
                "required": bool(inp.get("required")),
                "type": inp.get("type", "string"),
            })
    else:
        # Back-compat: synthesize field inputs from the {tokens} in the template +
        # headers/query values so a legacy connector is still mappable.
        seen = set()
        blobs = [cfg.get("url_template") or ""]
        blobs += [str(v) for v in (cfg.get("query") or {}).values()]
        blobs += [str(v) for v in (cfg.get("headers") or {}).values()]
        for blob in blobs:
            for tok in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", blob):
                if tok in seen:
                    continue
                seen.add(tok)
                field_inputs.append({
                    "name": tok, "label": tok, "source": "field",
                    "field": tok, "required": True, "type": "string",
                })

    # The outputs catalog (synthesized for legacy connectors) so the doctype modal
    # renders the OUTPUT CHECKLIST from this same fetch — one checkbox per output
    # (a series output is ONE checkbox). The modal already calls this endpoint on
    # connector-select, so no second round-trip is needed.
    outputs = _outputs_for_checklist(_connector_outputs(cfg))
    return JsonResponse({"ok": True, "name": name, "inputs": field_inputs,
                         "outputs": outputs})


@controller(name="script_test", url="script-test", title="Test Script")
def script_test(request):
    """Builder-only JSON endpoint for the Python-Script field's Test-run button.

    POST a JSON body ``{source:<str>, inputs:{<field>: <sample value>, ...}}`` (CSRF
    via X-CSRFToken). Runs the sandboxed script ONCE and returns its assigned variables
    with an inferred field type + a JSON preview, plus the AST-detected free variables
    (the script's INPUTS). Never persists anything. EXECUTES user code, so it is gated
    to builders; the same RestrictedPython sandbox + SIGALRM timeout as the save path."""
    if not _can_build(request):
        return JsonResponse({"ok": False, "error": "permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)
    try:
        body = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, TypeError) as exc:
        return JsonResponse({"ok": False, "error": "bad request: %s" % exc}, status=400)
    source = (body.get("source") or "").strip()
    inputs = body.get("inputs") if isinstance(body.get("inputs"), dict) else {}
    if not source:
        return JsonResponse({"ok": False, "error": "script is empty"}, status=400)
    free = _script_free_vars(source)
    bound = {v: inputs.get(v) for v in free}          # only bind the detected free vars
    try:
        out = _run_python_script(source, bound)       # sandboxed, 5s SIGALRM timeout
    except Exception as exc:
        return JsonResponse({"ok": False,
                             "error": ("%s: %s" % (type(exc).__name__, exc))[:240],
                             "inputs": [{"name": v} for v in free]})
    variables = []
    for name, val in out.items():
        ftype, kind = _infer_script_output_field_type(val)
        try:
            preview = json.dumps(_script_jsonify(val))
        except (TypeError, ValueError):
            preview = str(val)
        if len(preview) > 200:
            preview = preview[:200] + "…"
        variables.append({"name": name, "kind": kind, "field_type": ftype,
                          "preview": preview})
    return JsonResponse({"ok": True, "inputs": [{"name": v} for v in free],
                         "variables": variables})


@controller(name="types_json", url="types.json", title="HydroTypes JSON")
def types_json(request):
    """Lightweight JSON: every existing HydroType as ``{slug, display_name}`` for
    the DocType builder's Link-field target picker.

    Returns ``{ok, types:[{slug, display_name} ...]}`` ordered by display name.
    The builder's Link Configure tab populates a target <select> (value=slug)
    from this instead of the user hand-typing the slug — the chosen slug is then
    written verbatim into the row's hidden ``field_options_<i>`` (so _schema_for
    keeps storing it as x-link-type). NEVER 500s: degrades to an empty list on a
    store error (the picker keeps its allow-typing fallback)."""
    out = []
    try:
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            rows = session.execute(
                select(m.HydroType.slug, m.HydroType.display_name)
                .order_by(m.HydroType.display_name)
            ).all()
        out = [{"slug": s, "display_name": d} for s, d in rows]
    except Exception:
        out = []
    return JsonResponse({"ok": True, "types": out})


@controller(name="connectors_json", url="connectors.json", title="Connectors JSON")
def connectors_json(request):
    """Lightweight JSON: every existing HydroConnector as ``{name, result_kind,
    url_template}`` for the DocType builder's API-field connector picker.

    Returns ``{ok, connectors:[{name, result_kind, url_template} ...]}`` ordered
    by name. The builder's API Configure tab populates a connector <select>
    (value=name) from this; the chosen name is written verbatim (NOT slugified)
    into the row's hidden ``field_options_<i>`` so _schema_for keeps storing it as
    x-api-connector and new_hydrotype's _connector_name_exists guard matches it.
    NEVER 500s: degrades to an empty list on a store error (the picker keeps its
    allow-typing fallback)."""
    out = []
    try:
        engine = App.get_persistent_store_database("hydro_db")
        with Session(engine) as session:
            rows = session.execute(
                select(m.HydroConnector.name, m.HydroConnector.config)
                .order_by(m.HydroConnector.name)
            ).all()
        for name, config in rows:
            cfg = config or {}
            out.append({
                "name": name,
                "result_kind": cfg.get("result_kind", "value"),
                "url_template": cfg.get("url_template", ""),
                # NetCDF query shape — so the doctype API-field modal can offer the
                # parameter mapping (shapefile field, date-range fields) above the
                # outputs checklist when the connector needs them.
                "kind": cfg.get("kind", "rest"),
                "spatial": cfg.get("spatial", ""),
                "time_source": cfg.get("time_source", "none"),
                # The outputs catalog (synthesized for legacy connectors). This is
                # the SOURCE the doctype modal's OUTPUT CHECKLIST reads — one
                # checkbox per output (a series output is ONE checkbox, not two).
                "outputs": _outputs_for_checklist(_connector_outputs(cfg)),
            })
    except Exception:
        out = []
    return JsonResponse({"ok": True, "connectors": out})


def _outputs_for_checklist(outputs):
    """Project a connector's outputs[] to the minimal shape the doctype OUTPUT
    CHECKLIST needs: ``[{name, kind, type, unit, primary, default_field_type,
    columns}]``. A series output is ONE entry (never split into variables);
    ``columns`` lists its captured variable names so the modal can show what the
    Table will contain. ``default_field_type`` is the doctype render-mode default
    derived from the connector-side ``type`` hint (number->Number, string->Text,
    date->Date, series->Time-Series)."""
    out = []
    for o in (outputs or []):
        if not isinstance(o, dict):
            continue
        name = (o.get("name") or "").strip()
        if not name:
            continue
        kind = (o.get("kind") or "value").lower()
        otype = (o.get("type") or ("series" if kind == "series" else "string")).lower()
        default_ft = ("Time-Series" if kind == "series"
                      else _OUTPUT_TYPE_TO_FIELD.get(otype, "Text"))
        out.append({
            "name": name,
            "kind": kind,
            "type": otype,
            "unit": o.get("unit", ""),
            "primary": bool(o.get("primary")),
            "default_field_type": default_ft,
            # The series' Table columns (variable names) for the modal hint.
            "columns": [v["name"] for v in _series_variables(o)] if kind == "series" else [],
        })
    return out
