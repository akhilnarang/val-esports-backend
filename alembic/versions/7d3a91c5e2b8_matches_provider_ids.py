"""Give matches a VLR id and a Riot id beside their primary key, so a Riot-only match can exist."""

import sqlalchemy as sa

from alembic import op

revision = "7d3a91c5e2b8"
down_revision = "625f004fc4c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``vlr_id`` and ``riot_id``, backfill ``vlr_id`` from ``id``, and widen the ids to hold Riot's.

    ``matches.id`` and its foreign keys take a Riot series id, ``events.id`` and ``matches.event_id`` a
    Riot tournament id, and a favorite either.

    :return: None.
    """
    op.add_column("matches", sa.Column("vlr_id", sa.String(10), nullable=True))
    op.add_column("matches", sa.Column("riot_id", sa.Text(), nullable=True))
    op.execute("UPDATE matches SET vlr_id = id")
    op.create_unique_constraint("uq_matches_vlr_id", "matches", ["vlr_id"])
    op.create_unique_constraint("uq_matches_riot_id", "matches", ["riot_id"])
    op.create_check_constraint("ck_matches_has_provider_id", "matches", "vlr_id IS NOT NULL OR riot_id IS NOT NULL")
    op.alter_column("matches", "id", existing_type=sa.String(10), type_=sa.String(20))
    op.alter_column("maps", "match_id", existing_type=sa.String(10), type_=sa.String(20))
    op.alter_column("ranking_results", "match_id", existing_type=sa.String(10), type_=sa.String(20))
    op.alter_column("events", "id", existing_type=sa.String(10), type_=sa.String(20))
    op.alter_column("matches", "event_id", existing_type=sa.String(10), type_=sa.String(20), existing_nullable=False)
    op.alter_column("favorites", "entity_id", existing_type=sa.String(10), type_=sa.String(20))


def downgrade() -> None:
    """Narrow the columns again, refusing while a match exists that the old schema cannot hold.

    :return: None.
    :raises RuntimeError: When a match, event or favorite under a Riot id exists.
    """
    connection = op.get_bind()
    stranded = (
        connection.execute(sa.text("SELECT 1 FROM matches WHERE vlr_id IS NULL OR id <> vlr_id LIMIT 1")).first()
        or connection.execute(sa.text("SELECT 1 FROM events WHERE length(id) > 10 LIMIT 1")).first()
        or connection.execute(sa.text("SELECT 1 FROM favorites WHERE length(entity_id) > 10 LIMIT 1")).first()
    )
    if stranded:
        raise RuntimeError(
            "Matches, events or favorites under a Riot id exist and cannot be stored in the old schema. "
            "Delete them deliberately before downgrading."
        )
    op.alter_column("favorites", "entity_id", existing_type=sa.String(20), type_=sa.String(10))
    op.drop_constraint("ck_matches_has_provider_id", "matches", type_="check")
    op.drop_constraint("uq_matches_riot_id", "matches", type_="unique")
    op.drop_constraint("uq_matches_vlr_id", "matches", type_="unique")
    op.alter_column("matches", "event_id", existing_type=sa.String(20), type_=sa.String(10), existing_nullable=False)
    op.alter_column("events", "id", existing_type=sa.String(20), type_=sa.String(10))
    op.alter_column("ranking_results", "match_id", existing_type=sa.String(20), type_=sa.String(10))
    op.alter_column("maps", "match_id", existing_type=sa.String(20), type_=sa.String(10))
    op.alter_column("matches", "id", existing_type=sa.String(20), type_=sa.String(10))
    op.drop_column("matches", "riot_id")
    op.drop_column("matches", "vlr_id")
