"""HydroType registry — export / import (SHARE) DocTypes as portable JSON.

A HydroType (the Frappe-DocType analog) is fully described by a small JSON spec:
slug, display_name, field_schema (a JSON Schema), geometry_kind, timeseries_policy,
workflow, version. Because every record lives in the generic EAV/JSONB store,
importing a shared doctype is just INSERTing ONE row into `hydrotype` — no schema
migration, no `syncstores`, no reinstall, no restart. That is what makes doctypes
shareable across portals: a single self-contained `.hydrotype.json` file.
"""
import jsonschema
from sqlalchemy import select, func

from . import model as m

# The keys that travel when a doctype is shared (everything that defines the type;
# nothing portal-specific or record data).
#
# SECURITY (API extractor): exporting a HydroType ships its ``field_schema`` only.
# An API field carries ONLY the connector NAME (the ``x-api-connector`` key); the
# connector row, the credential row, and any secret live in separate tables that
# the export path never reads. So a shared doctype is secret-free BY CONSTRUCTION:
# the operator on the target portal re-creates the named Connector + Credential
# (re-entering the secret). No 'secret' key can appear in an exported spec because
# nothing here serializes hydro_connector / hydro_credential.
SHAREABLE_KEYS = (
    "slug", "display_name", "version", "field_schema",
    "geometry_kind", "timeseries_policy", "workflow",
)
DOCTYPE_FORMAT = "hydrodesk/doctype@1.0"


def export_hydrotype(session, slug):
    """Serialize a HydroType row to a portable, shareable spec dict (or None)."""
    ht = session.execute(
        select(m.HydroType).where(m.HydroType.slug == slug)
    ).scalar_one_or_none()
    if ht is None:
        return None
    spec = {"_format": DOCTYPE_FORMAT}
    for key in SHAREABLE_KEYS:
        spec[key] = getattr(ht, key)
    return spec


def validate_spec(spec):
    """Validate a shared doctype spec. Raises ValueError on any problem."""
    if not isinstance(spec, dict):
        raise ValueError("Doctype spec must be a JSON object.")
    for key in ("slug", "display_name", "field_schema"):
        if not spec.get(key):
            raise ValueError(f"Doctype spec is missing required key: '{key}'.")
    slug = spec["slug"]
    if not isinstance(slug, str) or not slug.replace("_", "").isalnum():
        raise ValueError("slug must be alphanumeric/underscore.")
    field_schema = spec["field_schema"]
    if not isinstance(field_schema, dict) or field_schema.get("type") != "object":
        raise ValueError("field_schema must be a JSON Schema object (type: 'object').")
    # The field_schema must itself be a valid JSON Schema.
    try:
        jsonschema.Draft202012Validator.check_schema(field_schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(f"field_schema is not a valid JSON Schema: {exc.message}")
    return True


def import_hydrotype(session, spec, overwrite=False):
    """Upsert a shared doctype spec into the `hydrotype` table.

    Returns (hydrotype_row, created). If the slug already exists and overwrite is
    False, the existing row is returned untouched (created=False).
    """
    validate_spec(spec)
    slug = spec["slug"]
    existing = session.execute(
        select(m.HydroType).where(m.HydroType.slug == slug)
    ).scalar_one_or_none()

    if existing is not None and not overwrite:
        return existing, False

    target = existing or m.HydroType(slug=slug)
    target.display_name = spec["display_name"]
    target.version = spec.get("version", 1)
    target.field_schema = spec["field_schema"]
    target.geometry_kind = spec.get("geometry_kind")
    target.timeseries_policy = spec.get("timeseries_policy", "inline")
    target.workflow = spec.get("workflow")
    if existing is None:
        session.add(target)
    session.commit()
    return target, existing is None


def list_hydrotypes(session):
    """Return [(slug, display_name, record_count)] for every registered doctype."""
    rows = session.execute(
        select(m.HydroType.slug, m.HydroType.display_name)
        .order_by(m.HydroType.display_name)
    ).all()
    out = []
    for slug, display_name in rows:
        count = session.execute(
            select(func.count()).select_from(m.HydroRecord)
            .where(m.HydroRecord.hydrotype_slug == slug)
        ).scalar()
        out.append((slug, display_name, count or 0))
    return out
