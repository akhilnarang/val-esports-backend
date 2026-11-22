"""Add the Riot series VLR does not list to the match list, so a Riot-only match shows up in it.

The list stays what VLR scrapes and caches; this runs on each request after the cache read, so the
cached schema does not change. A series is added only while unlinked and near now, once the schedule
cron gave it a ``matches`` row, and only when its status is known. Its id is that row's id, its Riot
series id, which the match keeps: once VLR lists it, VLR's entry is shown under the same id, and under
the same event id, its Riot tournament id. Both ids open ``GET /matches/{id}``.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas, utils
from app.constants import RIOT_WEB_LISTING_WINDOW, RIOT_WEB_VODS_TIMEOUT, TBD, MatchStatus
from app.core import connections
from app.core.config import settings
from app.db.models import MatchRecord, RiotWebMatch
from app.schemas.matches import MatchTeam
from app.services import match_refs, riot_web_link, riot_web_store

logger = logging.getLogger(__name__)


def _insert(listed: list[schemas.ListedMatch], item: schemas.ListedMatch) -> None:
    """Place a Riot item among VLR's, which are upcoming by ascending time, then completed by descending time.

    :param listed: The list being built; VLR's items keep their order.
    :param item: The item to add.
    """
    completed = item.status == MatchStatus.COMPLETED
    for index, other in enumerate(listed):
        if (other.status == MatchStatus.COMPLETED) != completed:
            continue
        if (other.time < item.time) if completed else (other.time > item.time):
            listed.insert(index, item)
            return
    if completed:
        listed.append(item)
        return
    index = next((i for i, other in enumerate(listed) if other.status == MatchStatus.COMPLETED), len(listed))
    listed.insert(index, item)


async def with_riot_only(
    listed: list[schemas.ListedMatch], session: AsyncSession | None = None
) -> list[schemas.ListedMatch]:
    """Add the Riot-only series near now to a VLR listing.

    :param listed: The cached or freshly scraped VLR listing.
    :param session: The request's session, if it has one; else a short one, given a second at most.
    :return: The listing with the Riot-only series added; the input unchanged when none apply or the
        database cannot be read, because the list must not fail for an extra.
    """
    # The key is the one switch for everything from Riot's site: unset, the list is VLR's alone.
    if not settings.RIOT_WEB_API_KEY or (session is None and connections.subscription_sessions is None):
        return listed
    now = datetime.now(UTC)
    try:
        async with (
            asyncio.timeout(None if session else RIOT_WEB_VODS_TIMEOUT),
            connections.reading_session(session) as db,
        ):
            rows = (
                await db.execute(
                    select(RiotWebMatch, MatchRecord)
                    .join(MatchRecord, MatchRecord.id == RiotWebMatch.riot_id)
                    .where(
                        RiotWebMatch.vlr_match_id.is_(None),
                        RiotWebMatch.start_time.between(now - RIOT_WEB_LISTING_WINDOW, now + RIOT_WEB_LISTING_WINDOW),
                    )
                    .order_by(RiotWebMatch.start_time)
                )
            ).all()
            linked = await match_refs.shown_ids(db, [item.id for item in listed])
            mirrored = await riot_web_store.mirrored_teams(db) if rows else {}
    except Exception:
        logger.warning("could not read the Riot-only matches for the list", exc_info=True)
        return listed
    # A VLR entry linked to its own series cannot be another one, so it does not hold back a rematch.
    claimed = set(linked)
    shown_as = {vlr_id: ids for vlr_id, ids in linked.items() if ids[0]}
    added = []
    # VLR lists a match before its link is stored: its entry is the match, under the id it already has.
    # The series nearest in time takes an entry first, as the link will.
    fits = []
    for series, ledger in rows:
        names = [team["name"].strip() for team in series.teams]
        if len(names) != 2 or any(name.casefold() == TBD for name in names):
            continue
        candidates = riot_web_link.vlr_candidates(names, series.league_name, series.start_time, listed)
        gap = min((abs(match.time - series.start_time) for match in candidates), default=None)
        fits.append((gap, series, ledger, names, candidates))
    unlisted = []
    for _, series, ledger, names, candidates in sorted(
        fits, key=lambda fit: (timedelta.max if fit[0] is None else fit[0], fit[1].start_time)
    ):
        free = [match for match in candidates if match.id not in claimed]
        if len(free) == 1:
            claimed.add(free[0].id)
            shown_as[free[0].id] = (series.riot_id, match_refs.event_id(series, ledger))
        else:
            unlisted.append((series, ledger, names))
    for series, ledger, names in unlisted:
        stored = riot_web_store.read_details(series)
        usable = riot_web_store.teams_for(series, mirrored)
        status = match_refs.status(series, ledger, stored)
        if status is None:
            continue
        # Riot answers 0 wins for a series that has not started; VLR's list has no score there.
        played = stored is not None and status != MatchStatus.UPCOMING
        added.append(
            schemas.ListedMatch(
                id=series.riot_id,
                team1=MatchTeam(
                    name=names[0],
                    id=team.id if (team := usable.get(utils.normalize_name(names[0]))) else None,
                    score=stored.game_wins.get(names[0]) if played else None,
                ),
                team2=MatchTeam(
                    name=names[1],
                    id=team.id if (team := usable.get(utils.normalize_name(names[1]))) else None,
                    score=stored.game_wins.get(names[1]) if played else None,
                ),
                status=status,
                time=series.start_time,
                event=series.league_name,
                series=(ledger.stage if ledger else None) or "",
                # Empty, like the match page, while no tournament is known: the oldest Android app fails the
                # whole list on a null here.
                event_id=match_refs.event_id(series, ledger) or "",
            )
        )
    result = [
        item.model_copy(update={"id": shown_as[item.id][0], "event_id": shown_as[item.id][1] or item.event_id})
        if item.id in shown_as
        else item
        for item in listed
    ]
    for item in added:
        _insert(result, item)
    return result
