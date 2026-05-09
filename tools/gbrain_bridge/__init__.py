"""sqlite-memory-mcp ↔ GBrain bridge — bidirectional Markdown adapter.

Convergent-evolution bridge between sqlite-memory-mcp's SQLite + KG store
and Garry Tan's GBrain (github.com/garrytan/gbrain) Markdown brain repo
layout. Pure SQL + filesystem; no LLM, no network. Runs offline.

Public surface:
    export_to_gbrain_brain_repo(conn, output_dir, *, project_filter=None)
    import_from_gbrain_brain_repo(conn, input_dir, *, project_default=None,
                                  skip_if_exists=True)
"""

from .gbrain_export import export_to_gbrain_brain_repo  # noqa: F401
from .gbrain_import import import_from_gbrain_brain_repo  # noqa: F401
