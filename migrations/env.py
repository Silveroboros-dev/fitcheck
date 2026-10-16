from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from alembic.script.revision import RangeNotAncestorError
from sqlalchemy import engine_from_config, pool

from el.domain.db import database_url
from el.domain.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A programmatic bootstrap supplies its already-open connection so a fresh
# database is migrated exactly once without touching process-wide settings.
connection_override = config.attributes.get("connection")
if connection_override is None:
    config.set_main_option("sqlalchemy.url", database_url())

target_metadata = Base.metadata
_LIVE_PREFLIGHT_REVISION = "4f7c2e9a6b13"
_LIVE_PREFLIGHT_ERROR = (
    "cannot downgrade 4f7c2e9a6b13: offline downgrade SQL generation is "
    "unavailable because a live data preflight is required"
)


def _offline_range_requires_live_preflight() -> bool:
    script = ScriptDirectory.from_config(config)
    starting_revision = context.get_starting_revision_argument()
    destination_revision = context.get_revision_argument()
    try:
        revisions = script.iterate_revisions(
            starting_revision,
            destination_revision,
            select_for_downgrade=True,
        )
        return any(
            revision.revision == _LIVE_PREFLIGHT_REVISION
            for revision in revisions
        )
    except RangeNotAncestorError:
        # The supplied range is an upgrade, so no downgrade preflight applies.
        return False


def run_migrations_offline() -> None:
    if _offline_range_requires_live_preflight():
        raise RuntimeError(_LIVE_PREFLIGHT_ERROR)
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if connection_override is not None:
        context.configure(
            connection=connection_override,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
