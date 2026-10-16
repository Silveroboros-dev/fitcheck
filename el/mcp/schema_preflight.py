"""Read-only production schema gate for the MCP service.

Schema migration is an operator action. A serving replica may check its
database, but must never apply DDL during startup or a traffic rollback.
"""

from importlib.resources import files

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from el.domain.db import make_engine


class SchemaNotReady(RuntimeError):
    """The connected database is not at this image's Alembic head."""


def require_schema_head(engine: Engine | None = None) -> None:
    """Read the DB revision and fail before serving if it differs from code."""
    config = Config()
    config.set_main_option("script_location", str(files("migrations")))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    if not expected:
        raise SchemaNotReady("image has no Alembic head")

    own_engine = engine is None
    bound_engine = engine if engine is not None else make_engine()
    try:
        with bound_engine.connect() as connection:
            if "alembic_version" not in inspect(connection).get_table_names():
                raise SchemaNotReady("database has no Alembic version")
            actual = {
                row[0]
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            }
        if actual != expected:
            raise SchemaNotReady("database revision differs from image head")
    finally:
        if own_engine:
            bound_engine.dispose()
