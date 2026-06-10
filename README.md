# HydroDesk

A **Frappe-style, no-code metadata engine** for the [Tethys Platform](https://www.tethysplatform.org/) — so non-coding hydrologists (and anyone else) can define record types and get instant List, Form, Detail, and Map views, plus live API integrations, **without writing code or running database migrations**.

> Packaged as the Tethys app `tethysapp-hydrodesk`; repo [`anomaitech/hydrodesk`](https://github.com/anomaitech/hydrodesk).

## What it does

Define a **HydroType** (the DocType analog) in a Frappe-style builder; everything below is generated from that one definition.

- **Runtime types, no migrations.** A new type is a single row in a generic EAV/JSONB store (`hydrotype` + `hydro_record`, geometry in PostGIS) — no DDL, no `syncstores`, no reinstall, no restart.
- **Rich field types:** Text, Number, Int, Select, Checkbox, Date, Long Text, Email, URL, Tags, **Link** (foreign key to another type), **API** (live external data), and **Table** (child grids).
- **Child / linked tables:**
  - *Inline table* — repeating rows stored on the record (e.g. an invoice's line items).
  - *Linked table* — each row is a real record of **another type** (a true one-to-many, with its own detail/list/map).
- **Live API connectors:** configurable URL-template connectors with inputs, an output catalog, auth via named **credentials** (secrets stored separately, never in the shared schema), and a Test → clickable-JSON-tree output picker. Series outputs render as a single multi-column table.
- **Form layout:** **Section Breaks** group fields into titled sections.
- **Full lifecycle:** create / **edit** / **delete** a type; per-record CRUD; list **bulk-delete** with select-all.
- **Auto views:** every type gets a List, Form, Detail, and (for spatial types) a Map view — all driven by the metadata.
- **Shareable:** export/import a type as portable JSON (secrets never travel).

## Architecture

| Piece | Role |
|---|---|
| `tethysapp/hydrodesk/model.py` | Generic JSONB/PostGIS store (`HydroType`, `HydroRecord`, `HydroConnector`, `HydroCredential`, …) |
| `tethysapp/hydrodesk/controllers.py` | Metadata-driven controllers: builder, CRUD, detail/list/map render, API fetch engine |
| `tethysapp/hydrodesk/registry.py` | Export / import a type as portable JSON |
| `tethysapp/hydrodesk/templates/` | Frappe-Desk themed, server-rendered UI |

A HydroType is described by a JSON-Schema `field_schema` (with `x-` extension keys for widgets, links, API connectors, child tables, and layout). Records validate against it (`fastjsonschema`) and store their values as JSONB — so adding a type is an `INSERT`, not a schema change.

## Install (development)

```bash
# in a Tethys Platform environment
tethys install -d            # installs this app from pyproject.toml
tethys syncstores hydrodesk  # provisions the PostGIS persistent store
tethys manage start          # then open /apps/hydrodesk/
```

Requires a spatial persistent-store service (PostgreSQL + PostGIS).

## Status

Active development (CIROH / Tethys DevCon 2026). Built and validated inside a real Tethys 4.5.2 install.
