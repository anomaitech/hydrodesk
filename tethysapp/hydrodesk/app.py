from tethys_sdk.components import ComponentBase
from tethys_sdk.app_settings import PersistentStoreDatabaseSetting


class App(ComponentBase):
    """
    Tethys app class for Hydrodesk.

    A Frappe-style metadata engine: a HydroType (the DocType analog) is one row
    in a fixed generic store; every UI surface (Home, List, Map, Form, Detail,
    DocType Builder) is a server-rendered controller generic over the type slug.
    """

    name = "Hydrodesk"
    description = "Frappe-style metadata engine for hydrology (HydroForge)"
    package = "hydrodesk"  # WARNING: Do not change this value
    index = "home"
    icon = f"{package}/images/icon.png"
    root_url = "hydrodesk"
    color = "#2f3640"
    tags = ""
    enable_feedback = False
    feedback_emails = []
    exit_url = "/apps/"
    default_layout = "NavHeader"
    nav_links = "auto"

    def persistent_store_settings(self):
        """The single fixed generic store. A new HydroType is a ROW, not a table,
        so this one spatial store backs every type with zero per-type schema."""
        return (
            PersistentStoreDatabaseSetting(
                name="hydro_db",
                description="HydroForge generic metadata store (EAV/JSONB + PostGIS)",
                initializer="hydrodesk.model.init_hydro_db",
                spatial=True,
                required=True,
            ),
        )
