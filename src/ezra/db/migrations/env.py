"""Alembic environment. Called programmatically by ezra.db.migrate() with an
open connection, or from the alembic CLI with -x url=..."""

from alembic import context
from sqlalchemy import create_engine

from ezra.db.models import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        context.configure(connection=conn, target_metadata=target_metadata,
                          render_as_batch=conn.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
        return
    url = context.get_x_argument(as_dictionary=True).get("url") or config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as c:
        context.configure(connection=c, target_metadata=target_metadata,
                          render_as_batch=c.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


run()
