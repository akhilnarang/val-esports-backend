"""Events Riot created: a competition VLR has no event for, served under its Riot tournament id.

The event is built from the stored Riot series and, when Riot answers, its standings entry; only
the standings are cached, for Riot's 60-second CDN window. Nothing here is listed by ``GET /events`` or searched.
"""

import asyncio
import logging
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, TypeAdapter
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app import cache, schemas, utils
from app.constants import (
    CACHE_TTL_RIOT_WEB_BRACKETS,
    RIOT_PLACEHOLDER_IMG,
    RIOT_WEB_LEDGER_SETTLE,
    RIOT_WEB_REQUEST_TIMEOUT,
    RIOT_WEB_SCHEDULE_LOOKBACK,
    RIOT_WEB_STANDINGS_KEY,
    RIOT_WEB_VODS_TIMEOUT,
    TBD,
    EventStatus,
    MatchStatus,
)
from app.core import connections
from app.core.config import settings
from app.db.models import MatchRecord, RiotWebMatch
from app.exceptions import NotFoundError, ServiceUnavailableError
from app.services import match_refs, riot_web, riot_web_store

logger = logging.getLogger(__name__)


async def vlr_event_for(tournament_id: str) -> str | None:
    """Find the VLR event a Riot tournament has been linked to.

    :param tournament_id: Riot tournament id.
    :return: The VLR event id when exactly one is known, else None.
    :raises ServiceUnavailableError: When the database is unavailable.
    """
    sessions = connections.subscription_sessions
    try:
        if sessions is None:
            raise OSError("database is unavailable")
        async with asyncio.timeout(RIOT_WEB_VODS_TIMEOUT):
            async with sessions() as session:
                found = (await riot_web_store.events_by_tournament(session, [tournament_id])).get(tournament_id, set())
    except OSError, TimeoutError, SQLAlchemyError:
        raise ServiceUnavailableError("Database is unavailable") from None
    return next(iter(found)) if len(found) == 1 else None


async def _standings(tournament_id: str, redis: Redis | None) -> TournamentSummary | None:
    """Read what the event page needs from Riot's standings, cached for Riot's CDN window, a miss too.

    :param tournament_id: Riot tournament id.
    :param redis: Request Redis client.
    :return: The tournament's name and dates, or None when the key is unset or Riot gave none.
    """
    if not settings.RIOT_WEB_API_KEY:
        return None
    key = RIOT_WEB_STANDINGS_KEY.format(tournament_id)
    if (raw := await cache.get(key, client=redis)) is not None:
        return _CACHED_SUMMARY.validate_json(raw)
    try:
        async with riot_web.client() as client:
            tournament = await riot_web.standings(client, tournament_id, timeout=RIOT_WEB_REQUEST_TIMEOUT)
    except Exception:
        logger.warning("could not fetch Riot standings for tournament %s", tournament_id, exc_info=True)
        tournament = None
    summary = (
        TournamentSummary(name=tournament.name, start_time=tournament.start_time, end_time=tournament.end_time)
        if tournament
        else None
    )
    await cache.set(key, _CACHED_SUMMARY.dump_json(summary).decode(), ttl=CACHE_TTL_RIOT_WEB_BRACKETS, client=redis)
    return summary


class TournamentSummary(BaseModel):
    """The part of a Riot tournament the event page shows."""

    name: str
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None


_CACHED_SUMMARY = TypeAdapter(TournamentSummary | None)


def _status(now: datetime, tournament: TournamentSummary | None, series: list[RiotWebMatch]) -> EventStatus:
    """Derive an event's status; it is never completed from settled series alone, as later stages may be unstored."""
    starts = [item.start_time for item in series]
    if tournament and tournament.start_time:
        starts.append(tournament.start_time)
    if now < min(starts):
        return EventStatus.UPCOMING
    if tournament and tournament.end_time:
        return EventStatus.COMPLETED if now >= tournament.end_time else EventStatus.ONGOING
    newest = max(item.start_time for item in series)
    if now - newest > RIOT_WEB_SCHEDULE_LOOKBACK:
        return EventStatus.COMPLETED
    if now - newest <= RIOT_WEB_LEDGER_SETTLE:
        return EventStatus.ONGOING
    return EventStatus.UNKNOWN


def _match(item: RiotWebMatch, riot_first: bool) -> schemas.events.Match:
    stored = riot_web_store.read_details(item)
    status = match_refs.status(item, None, stored) or MatchStatus.COMPLETED
    return schemas.events.Match(
        id=item.riot_id if riot_first else item.vlr_match_id or item.riot_id,  # the id the match list shows
        time=item.start_time.astimezone(UTC).time().isoformat(),
        date=item.start_time.astimezone(UTC).date(),
        status=status,
        teams=[
            schemas.events.MatchTeam(
                name=team["name"].strip(),
                region="",
                # Riot answers 0 wins for a series that has not started.
                score=stored.game_wins.get(team["name"].strip()) if stored and status != MatchStatus.UPCOMING else None,
            )
            for team in item.teams
        ],
        round="",
        stage="",
    )


async def event_with_details(tournament_id: str, redis: Redis | None) -> schemas.EventWithDetails:
    """Build the event page of a Riot tournament no VLR event is known for.

    Prize, location and standings are empty, because Riot gives none. Only a team mirrored to a VLR
    team is listed, since any other has no id the team route resolves. A series with a TBD team is
    left out, and so is one that has neither a VLR link nor its own row while it could still be linked.

    :param tournament_id: A Riot tournament id.
    :param redis: Request Redis client, for the cached standings.
    :return: The event, with ``id`` set to the tournament id.
    :raises NotFoundError: When no stored series belongs to the tournament.
    :raises ServiceUnavailableError: When the database is unavailable.
    """
    sessions = connections.subscription_sessions
    try:
        if sessions is None:
            raise OSError("database is unavailable")
        async with asyncio.timeout(RIOT_WEB_VODS_TIMEOUT):
            async with sessions() as session:
                series = await riot_web_store.series_for_tournament(session, tournament_id)
                names = {utils.normalize_name(team["name"]) for item in series for team in item.teams}
                usable = (
                    riot_web_store.teams_for(series[0], await riot_web_store.mirrored_teams(session)) if series else {}
                )
                mirrored = [team for key, team in usable.items() if key in names]
                # A series first listed from Riot has a row under its own id, and keeps that id.
                riot_first = set(
                    await session.scalars(
                        select(MatchRecord.id).where(MatchRecord.id.in_([item.riot_id for item in series]))
                    )
                )
    except OSError, TimeoutError, SQLAlchemyError:
        raise ServiceUnavailableError("Database is unavailable") from None
    if not series:
        raise NotFoundError("Event not found")
    tournament = await _standings(tournament_id, redis)
    now = datetime.now(UTC)
    league = series[0].league_name
    title = league
    if tournament:
        title = tournament.name if league.casefold() in tournament.name.casefold() else f"{league}: {tournament.name}"
    first = tournament.start_time if tournament and tournament.start_time else series[0].start_time
    last = tournament.end_time if tournament else series[-1].start_time
    return schemas.EventWithDetails.model_validate(
        {
            "id": tournament_id,
            "title": title,
            "subtitle": league,
            "dates": utils.format_event_dates(first.astimezone(UTC), last.astimezone(UTC) if last else None),
            "prize": "",
            "location": "",
            "status": _status(now, tournament, series),
            "img": RIOT_PLACEHOLDER_IMG,
            "teams": [
                {"id": team.id, "name": team.name or team.riot_name, "img": match_refs.team_img(team)}
                for team in mirrored
            ],
            "matches": [
                _match(item, item.riot_id in riot_first)
                for item in series
                if all(team["name"].strip().casefold() != TBD for team in item.teams)
                # A series with neither a link nor a row could still be linked, which would change its id.
                and (
                    item.vlr_match_id
                    or item.riot_id in riot_first
                    or item.start_time < now - RIOT_WEB_SCHEDULE_LOOKBACK
                )
            ],
        }
    )
