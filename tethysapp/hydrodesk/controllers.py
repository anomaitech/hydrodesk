"""HydroDesk map view — a single generic MapLayout that renders any spatial
HydroType from the generic store. Here: monitoring_station, with click-to-plot.
This is the data-driven 'one code-time class, request-time content' pattern.
"""
import base64
import json
import re
import time
import urllib.parse
import urllib.request
import uuid as uuidlib

from sqlalchemy import select, func, delete
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
            select(m.HydroType.slug, m.HydroType.display_name, m.HydroType.geometry_kind)
            .order_by(m.HydroType.display_name)
        ).all()
        for slug, display_name, gkind in rows:
            count = session.execute(
                select(func.count()).select_from(m.HydroRecord)
                .where(m.HydroRecord.hydrotype_slug == slug)
            ).scalar()
            types.append({"slug": slug, "display_name": display_name,
                          "geometry_kind": gkind, "count": count or 0})
    return render(request, "hydrodesk/home.html", {"types": types, "total": len(types)})


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
_OUTPUT_FIELD_TYPES = ("Number", "Text", "Date", "Time-Series")
_OUTPUT_TYPE_TO_FIELD = {
    "number": "Number",
    "string": "Text",
    "text": "Text",
    "date": "Date",
    "series": "Time-Series",
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
    return {"kind": "value", "value": _json_path(data, out.get("path") or "")}


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


def _api_request_json(connector_name, url, method, headers, timeout):
    """Perform ONE cached HTTP request and return parsed JSON (or None).

    Cache key is (connector_name, url) so two records with different substituted
    ids cache independently, but repeated views of the same record reuse one fetch
    within the TTL. On any failure returns None (graceful), exactly like the NWIS
    fallback — a flaky API never 500s a detail page."""
    now = time.time()
    key = (connector_name, url)
    cached = _API_CACHE.get(key)
    if cached and (now - cached[0]) < _api_cache_ttl(connector_name):
        return cached[1]
    try:
        req = urllib.request.Request(url, method=(method or "GET").upper())
        for hk, hv in (headers or {}).items():
            req.add_header(hk, hv)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
        _API_CACHE[key] = (now, payload)
        return payload
    except Exception:
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

    now = time.time()
    cached_entry = _API_CACHE.get((name, url))
    was_cached = bool(cached_entry and (now - cached_entry[0]) < ttl)

    data = _api_request_json(name, url, method, headers, timeout)

    if data is None:
        return _soft_empty(redacted_url, was_cached)

    # JSON (legacy raw/scoped tree, Test flow only): scope by the connector's
    # output_path then return the parsed sub-tree. No outputs[] equivalent.
    if out_kind == "json":
        op = cfg.get("output_path") or ""
        scoped = _json_path(data, op) if op else data
        return {"kind": "json", "json": scoped, "url": redacted_url, "cached": was_cached}

    # Extract the SELECTED named output from the ONE parsed response. _extract_output
    # reuses the EXACT _NO_DATA pairwise filter for series so the sparkline is clean.
    extracted = _extract_output(data, out_entry)
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


def _label_for(target_field_schema, attrs):
    """Pick a human label for a linked record from its attributes.

    Prefers a ``name`` attribute (Frappe-style display key), else the value of
    the FIRST schema property (declaration order), else the first non-empty
    attribute. Always returns a plain string for safe linkification.
    """
    attrs = attrs or {}
    name = attrs.get("name")
    if name not in (None, ""):
        return str(name)
    props = (target_field_schema or {}).get("properties") or {}
    for key in props:  # first declared property
        v = attrs.get(key)
        if v not in (None, ""):
            return _format_cell(v)
    for v in attrs.values():  # any non-empty attribute
        if v not in (None, ""):
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
            if prop.get("x-layout") == "section":
                # Layout-only widget: starts a new titled section on the form.
                fields.append({"widget": "section", "name": name,
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
            elif t in ("number", "integer"):
                f["widget"] = "number"
                f["step"] = "any" if t == "number" else "1"
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

    geometry_kind = _resolve_geometry_kind(field_schema, geometry_kind)
    parent = _parent_ctx(request)

    if request.method == "POST":
        post = request.POST.dict()
        errors = []

        attributes, coerce_errors = _coerce_attributes(field_schema, post)
        for key, msg in coerce_errors.items():
            errors.append(f"{key} {msg}.")

        validated = attributes
        if not coerce_errors:
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
            if linked:  # back to the parent detail, where the new child now lists
                return redirect(reverse("hydrodesk:detail",
                                        kwargs={"slug": parent["slug"], "record_id": parent["id"]}))
            return redirect(reverse("hydrodesk:list", kwargs={"slug": slug}))

        # Re-render with the user's submitted values (including lon/lat) so the
        # form is not cleared on error.
        widgets = _build_widgets(field_schema, geometry_kind, values=post)
        return render(request, "hydrodesk/form.html",
                      _form_context(slug, display_name, type_found, widgets, errors,
                                    "new", parent=parent))

    # GET: blank form.
    widgets = _build_widgets(field_schema, geometry_kind)
    return render(request, "hydrodesk/form.html",
                  _form_context(slug, display_name, type_found, widgets, [], "new",
                                parent=parent))


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
    geometry_kind = _resolve_geometry_kind(field_schema, geometry_kind)
    detail_url = reverse("hydrodesk:detail", kwargs={"slug": slug, "record_id": record_id})
    list_url = reverse("hydrodesk:list", kwargs={"slug": slug})

    if request.method == "POST":
        post = request.POST.dict()
        errors = []
        attributes, coerce_errors = _coerce_attributes(field_schema, post)
        for key, msg in coerce_errors.items():
            errors.append(f"{key} {msg}.")
        validated = attributes
        if not coerce_errors:
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
                func.ST_X(m.HydroRecord.geom),
                func.ST_Y(m.HydroRecord.geom),
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
    return _format_cell(value)


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


def _render_linked_table(session, child_slug, parent_slug, parent_id, field):
    """Render a LINKED Table field: the child records of ``child_slug`` that point
    back to (parent_id, field), as a Frappe table whose columns are the child type's
    fields. The first cell links to each child's OWN detail page; a '+ Add' button
    creates a new child pre-linked to this parent. Children are full HydroRecords
    (own list/map/detail), so this is a true one-to-many."""
    meta = _load_hydrotype(session, child_slug) if session else None
    if meta is None:
        return format_html("<span class='frappe-muted frappe-text-sm'>Linked type "
                           "&ldquo;{}&rdquo; not found.</span>", child_slug or "")
    child_name, child_schema, _gk = meta

    add_btn = ""
    if parent_id:
        q = urllib.parse.urlencode({"parent_slug": parent_slug or "",
                                    "parent_id": str(parent_id),
                                    "parent_field": field or ""})
        add_url = reverse("hydrodesk:new", kwargs={"slug": child_slug}) + "?" + q
        add_btn = str(format_html(
            "<div style='margin-top:6px;'><a class='btn btn-default btn-sm' href='{}'>"
            "<i class='bi bi-plus-lg'></i> Add {}</a></div>", add_url, child_name))

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


def _render_api_field(connector_name, connector, value, attrs, refresh_url=None,
                      field_map=None, api_outputs=None):
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
            result = fetch_api(connector.config, attrs,
                               connector_name=connector_name,
                               field_map=field_map, output=oname)
            last_result = result
            if head_pill is None:
                head_pill = ('<span class="indicator-pill gray">cached</span>'
                             if result.get("cached")
                             else '<span class="indicator-pill blue">live</span>')
            if (result.get("kind") == "series") or ft == "Time-Series":
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
    result = fetch_api(connector.config, attrs, connector_name=connector_name,
                       field_map=field_map)
    kind = result.get("kind")
    cached = result.get("cached")
    pill = ('<span class="indicator-pill gray">cached</span>'
            if cached else '<span class="indicator-pill blue">live</span>')

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
    """A tiny muted footnote: the (secret-redacted) source URL the value came from."""
    url = result.get("url") or ""
    return str(format_html(
        "<div class='frappe-help' style='margin-top:3px;'>via connector "
        "<code>{}</code> &middot; <span style='word-break:break-all;'>{}</span></div>",
        connector.name, url))


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
        if prop.get("x-layout") == "section":
            fields.append({"is_section": True, "section": prop.get("title") or ""})
            seen.add(name)
            continue
        label = prop.get("title") or name.replace("_", " ").title()
        connector_name = prop.get("x-api-connector")
        if connector_name:
            connector = _load_connector(session, connector_name) if session else None
            fields.append({
                "label": label,
                "value": _render_api_field(
                    connector_name, connector, attrs.get(name), attrs,
                    refresh_url=refresh_urls.get(name),
                    field_map=prop.get("x-api-map"),
                    api_outputs=prop.get("x-api-outputs")),
            })
            seen.add(name)
            continue
        if prop.get("type") == "array" and prop.get("x-child-type"):
            fields.append({
                "label": label,
                "value": _render_linked_table(session, prop.get("x-child-type"),
                                              parent_slug, parent_id, name),
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

    with Session(engine) as session:
        meta = _load_hydrotype(session, slug)
        if meta is not None:
            display_name, field_schema, _ = meta

        row = session.execute(
            select(
                m.HydroRecord.attributes,
                func.ST_X(m.HydroRecord.geom),
                func.ST_Y(m.HydroRecord.geom),
            )
            .where(m.HydroRecord.hydrotype_slug == slug)
            .where(m.HydroRecord.id == record_id)
        ).first()

        if row is not None:
            record_found = True
            attributes, lon, lat = row[0], row[1], row[2]
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

    context = {
        "slug": slug,
        "record_id": str(record_id),
        "display_name": display_name,
        "record_found": record_found,
        "fields": fields,
        "has_geom": has_geom,
        "longitude": lon,
        "latitude": lat,
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
            # LINKED table: rows are REAL records of another HydroType (one-to-many).
            # The parent stores NO inline rows; children are separate HydroRecords
            # linked back via their _parent key and queried at render time. The field
            # is a (virtual) array whose shape lives in the child type's schema.
            return {"type": "array", "x-widget": "table", "x-child-type": payload}
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
      * a JSON OBJECT ``{"child_type": "<slug>"}`` -> ('link', '<slug>')  (records
        of another HydroType, a true one-to-many).
    A blank/invalid carrier degrades to ('columns', [])."""
    raw = (options or "").strip()
    if not raw:
        return ("columns", [])
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ("columns", [])
    if isinstance(parsed, dict) and (parsed.get("child_type") or "").strip():
        return ("link", parsed["child_type"].strip())
    if isinstance(parsed, list):
        return ("columns", [c for c in parsed
                            if isinstance(c, dict) and (c.get("label") or "").strip()])
    return ("columns", [])


def _builder_type_for(prop):
    """Inverse of _schema_for's TYPE choice: a stored property fragment -> the
    builder 'Type' select value (text/number/select/checkbox/date/textarea/email/
    url/link/api/table/tags). Used to pre-fill the builder when EDITING a type."""
    prop = prop or {}
    if prop.get("x-layout") == "section":
        return "section"
    if prop.get("x-api-connector"):
        return "api"
    if prop.get("x-child-type"):
        return "table"
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
    if builder_type == "table":
        if prop.get("x-child-type"):
            return json.dumps({"child_type": prop.get("x-child-type")})
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
        rows.append({
            "label": prop.get("title") or key.replace("_", " ").title(),
            "type": bt,
            "options": _builder_options_for(prop, bt),
            "required": key in required,
            "field_map": field_map,
            "api_outputs": list(prop.get("x-api-outputs") or []),
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
        has_any = (bool(label) or bool(options) or is_req or bool(field_map)
                   or bool(api_outputs))
        if has_any:
            submitted_count = i + 1
        rows.append({
            "label": label,
            "type": ftype,
            "options": options,
            "required": is_req,
            "field_map": field_map,
            "api_outputs": api_outputs,
        })
    return rows, submitted_count


def _builder_context(form_errors, type_name, geometry, rows, row_count,
                     mode="new", slug=None):
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
        "row_indexes": list(range(row_count)),
        # Pad rows so every rendered index has a dict to read on re-fill.
        "rows": rows + [{"label": "", "type": "text", "options": "",
                         "required": False, "field_map": {}, "api_outputs": [],
                         "field_map_json": "{}", "api_outputs_json": "[]"}]
                * max(0, row_count - len(rows)),
        # The mapping endpoint base for the per-row x-api-map JS (a placeholder
        # connector name the JS swaps with the typed name).
        "connector_inputs_url": reverse(
            "hydrodesk:connector_inputs", kwargs={"conn_name": "__NAME__"}),
        # JSON list endpoints powering the Configure modal's Link/API pickers.
        "types_json_url": reverse("hydrodesk:types_json"),
        "connectors_json_url": reverse("hydrodesk:connectors_json"),
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
    for idx, row in enumerate(rows):
        label = row["label"]
        if not label:
            continue  # blank label => skip the row entirely
        prop_name = _slugify_underscore(label)
        if not prop_name:
            form_errors.append(
                f"Row {idx + 1}: field label '{label}' yields an empty field name."
            )
            continue
        if prop_name in seen:  # two labels slugify to the same key -> would overwrite
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
                             list(row.get("api_outputs") or [])))
        seen.add(prop_name)
        prop = _schema_for(row["type"], row["options"])
        prop["title"] = label  # _build_widgets/_derive_columns use title as the label
        properties[prop_name] = prop
        # A layout marker (Section Break) holds no data, so it can never be required.
        if row["required"] and not prop.get("x-layout"):
            required.append(prop_name)

    # --- SECOND PASS: per-doctype x-api-map (the headline). Now that every
    # property exists we can validate each API field's connector-input -> field
    # mapping (a forward-reference to a later row is legal). Each submitted value
    # is the chosen target field SLUG, or '__const__:<value>' for a constant. A
    # required source==field input with no mapping is an error; an empty optional
    # mapping is silently dropped (the connector input's own default applies). ---
    valid_field_slugs = set(properties.keys())
    for row_no, prop_name, conn_name, raw_map, raw_outputs in api_rows:
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
            # Time-Series chart; a value output can NEVER be a Time-Series.
            if okind == "series":
                ft = "Time-Series"
            elif ft == "Time-Series":
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
                               mode="new")
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
                                   mode="edit", slug=slug)
        return render(request, "hydrodesk/new_type.html", context)

    # GET: reverse the stored schema back into editable builder rows.
    rows = _schema_to_builder_rows(field_schema)
    row_count = max(_BUILDER_MIN_ROWS, len(rows))
    geometry = geometry_kind or "none"
    context = _builder_context([], display_name, geometry, rows, row_count,
                               mode="edit", slug=slug)
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


def _connector_config_from_post(post):
    """Assemble a HydroConnector.config dict from the builder POST, with the auth
    block, paths, and templated headers/query. Returns (name, config, errors)."""
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

    inputs = _parse_inputs_rows(post)
    outputs = _parse_outputs_rows(post)

    config = {
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
    # Only attach inputs[] when the editor actually produced rows. An ABSENT key
    # keeps a connector on the legacy implicit token-scan (full back-compat); an
    # empty list would needlessly opt it into the resolver with zero tokens.
    if inputs:
        config["inputs"] = inputs
    # Mirror the inputs[] ABSENT-vs-EMPTY pattern for outputs[]: attach only when
    # the editor produced rows. An absent/empty outputs[] falls through to the
    # synthesized single primary output (from result_kind/output_path/x_path/y_path)
    # — full back-compat. The legacy path fields are KEPT above for that fallback.
    if outputs:
        config["outputs"] = outputs
    if not config["url_template"]:
        errors.append("URL Template is required.")
    return name, config, errors


def _connector_form_context(mode, name, config, form_errors, conn_id=None,
                            credentials=None):
    """Shared context for the connector builder (new + edit)."""
    auth = (config or {}).get("auth") or {}
    if mode == "edit":
        form_action = reverse("hydrodesk:connector_edit", kwargs={"conn_id": conn_id})
    else:
        form_action = reverse("hydrodesk:connectors")
    return {
        "mode": mode,
        "conn_id": conn_id,
        "form_action": form_action,
        "form_errors": form_errors,
        "name": name,
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
        name, config, form_errors = _connector_config_from_post(request.POST)
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
        name, config, form_errors = _connector_config_from_post(request.POST)
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
            name, config, _ = _connector_config_from_post(request.POST)
            attrs = json.loads(request.POST.get("attrs") or "{}")
    except (ValueError, TypeError) as exc:
        return JsonResponse({"ok": False, "error": f"bad request: {exc}"}, status=400)

    if not isinstance(attrs, dict):
        attrs = {}

    result = fetch_api(config, attrs, connector_name=config.get("name") or "test")

    # fetch_api already returns a secret-redacted URL and never the secret. For the
    # tree-picker we fetch the RAW JSON separately (value/series kinds only expose
    # the extracted leaf), so re-fetch raw using a json result_kind view.
    raw = None
    try:
        cfg_json = dict(config)
        cfg_json["result_kind"] = "json"
        cfg_json["output_path"] = ""
        # Drop outputs[] so the raw-JSON path triggers: fetch_api only returns the
        # whole tree when the connector declares NO outputs[] (else it extracts the
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
