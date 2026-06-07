"""HydroForge generic metadata store — the FIXED schema (the EAV/JSONB resolution).

Verification refuted runtime per-type tables in Tethys (persistent-store classes
bind to a declarative_base at import time; `tethys syncstores` only materializes
classes already in deployed code). So a *new HydroType* is a ROW in `hydrotype`,
NOT a new table. Records live in `hydro_record` with free attributes in JSONB and
geometry in a PostGIS column. The physical schema never changes => defining a new
type needs zero DDL, zero syncstores, zero restart. This file is the entire
storage surface of the engine.
"""
import uuid
from sqlalchemy import (Column, String, Integer, Text, DateTime, SmallInteger,
                        Float, ForeignKey, func, Index)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base
from geoalchemy2 import Geometry

Base = declarative_base()


class HydroType(Base):
    """The DocType analog. A new type = one INSERTed row here."""
    __tablename__ = 'hydrotype'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    field_schema = Column(JSONB, nullable=False)        # JSON Schema for `attributes`
    geometry_kind = Column(Text)                        # 'point'|'line'|'polygon'|None
    timeseries_policy = Column(Text, default='inline')  # 'inline'|'table'|'netcdf'
    permissions = Column(JSONB, default=dict)
    workflow = Column(Text)
    content_hash = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class HydroRecord(Base):
    """Every record of every type lives here; fields in JSONB, geometry in PostGIS."""
    __tablename__ = 'hydro_record'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hydrotype_slug = Column(String, ForeignKey('hydrotype.slug'), nullable=False, index=True)
    attributes = Column(JSONB, nullable=False, default=dict)
    # Single geometry column for ALL types (any point/line/polygon); the
    # geoalchemy2 model creates the column + its GiST spatial index on create_all.
    geom = Column(Geometry(geometry_type='GEOMETRY', srid=4326))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by = Column(String)


# GIN index on the JSONB attributes => filter/sort on any field without per-type columns.
Index('ix_hydro_record_attrs_gin', HydroRecord.attributes, postgresql_using='gin')


class HydroTimeseries(Base):
    """Bulk series tier (kept OUT of the JSONB record per the verified perf finding)."""
    __tablename__ = 'hydro_timeseries'
    record_id = Column(UUID(as_uuid=True), ForeignKey('hydro_record.id'), primary_key=True)
    variable = Column(Text, primary_key=True)
    ts = Column(DateTime(timezone=True), primary_key=True)
    value = Column(Float)
    qc = Column(SmallInteger)


class WorkflowAuditLog(Base):
    __tablename__ = 'workflow_audit_log'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id = Column(UUID(as_uuid=True), index=True)
    from_state = Column(Text)
    to_state = Column(Text)
    actor = Column(String)
    at = Column(DateTime(timezone=True), server_default=func.now())


class HydroCredential(Base):
    """Named secret store for the dynamic API extractor.

    A row is the SENSITIVE half of an API integration: a human ``name`` (the value
    a HydroConnector references via its auth config) plus a single opaque ``secret``
    string. The interpretation of ``secret`` is owned by the connector's
    ``auth.scheme`` — an api-key string, a bearer token, or ``user:pass`` for basic.

    Secrets live ONLY here, keyed by ``name``. Connectors and a HydroType's
    field_schema reference the NAME only (two levels of name-only indirection), so
    nothing portable (registry export) ever carries a secret. The CRUD UI masks the
    secret in the list and never echoes it back into the test-flow JSON.
    """
    __tablename__ = 'hydro_credential'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, unique=True, nullable=False)   # the referenced label
    secret = Column(Text)                                # opaque; meaning per scheme
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class HydroConnector(Base):
    """A data-driven REST/JSON API integration ("Connector").

    Generalizes the single hardcoded NWIS fetch into one configurable row: a URL
    template with ``{field}`` placeholders (filled from a record's attributes at
    fetch time), method, headers/query, an auth block that names a HydroCredential,
    a result_kind (value|series|json) and dot/index extraction paths. The entire
    config rides in a single JSONB ``config`` column so adding a new connector is a
    pure INSERT (schema-stable, like a HydroType). The ``name`` is what the API
    field type's Options column references.

    config keys: url_template, method, headers{}, query{}, auth{scheme, credential,
    placement, param}, result_kind, output_path, x_path, y_path, ttl_seconds,
    timeout. NEVER stores a secret — only the credential NAME.
    """
    __tablename__ = 'hydro_connector'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, unique=True, nullable=False)   # referenced by API field
    config = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def init_hydro_db(engine, first_time):
    """Tethys persistent-store initializer. Enables PostGIS, then creates the
    fixed schema (the geom column + its GiST index come from the geoalchemy2
    model on create_all)."""
    from sqlalchemy import text
    # engine.begin() opens a transaction that auto-commits on exit — works on
    # both SQLAlchemy 1.4 (Tethys env) and 2.0 (no .commit() needed).
    with engine.begin() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS postgis'))
    Base.metadata.create_all(engine)
