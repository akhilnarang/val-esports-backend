"""Resolve the id in ``GET /matches/{id}``, which may be a VLR id or a Riot series id.

A match that has a VLR id is served by the VLR page, so every id of a merged match answers with the
body clients already know. A Riot-only match has no VLR page: its response is built from the stored
Riot series, with the schema's neutral values for what Riot does not carry. That response has no VLR
cross-check, so Riot's ``game_wins`` and game states are its only evidence, and it is never cached.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pydantic import HttpUrl, TypeAdapter, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import utils
from app.constants import (
    MATCH_ID_REGEX,
    RIOT_PLACEHOLDER_IMG,
    RIOT_WEB_LIVE_MAX,
    RIOT_WEB_VODS_TIMEOUT,
    MatchStatus,
    RiotGameState,
)
from app.core import connections
from app.core.config import settings
from app.db.models import MatchRecord, RiotWebMatch, Team
from app.exceptions import NotFoundError, ServiceUnavailableError
from app.schemas.matches import Event, MapVods, MatchDetailsResponse, MatchVideos, TeamWithImage
from app.schemas.riot_web import StoredDetails
from app.services import match_vods, riot_web_store

logger = logging.getLogger(__name__)

_HTTP_URL = TypeAdapter(HttpUrl)


class HasId(Protocol):
    """Anything that carries a match id."""

    id: str


@dataclass(frozen=True, slots=True)
class MatchRef:
    """What an id in the match route resolved to."""

    vlr_id: str | None
    series: RiotWebMatch | None
    ledger: MatchRecord | None
    teams: dict[str, Team] = field(default_factory=dict)  # folded Riot team name to its mirrored VLR team


def _is_vlr_id(ref_id: str) -> bool:
    return re.fullmatch(MATCH_ID_REGEX, ref_id) is not None


async def vlr_id_of(ref_id: str, session: AsyncSession | None = None) -> str:
    """Name the VLR match an id of any kind stands for, for the routes and tables keyed by VLR page id.

    :param ref_id: A VLR id or a Riot series id.
    :param session: The request's session, if it has one.
    :return: The VLR id.
    :raises NotFoundError: When the match has no VLR id (a Riot-only match) or no row holds the id.
    """
    if (vlr_id := (await resolve(ref_id, session)).vlr_id) is None:
        raise NotFoundError("Match has no VLR page")
    return vlr_id


async def aliases(session: AsyncSession, ids: Sequence[str]) -> dict[str, set[str]]:
    """Collect every id each given match id is known by, so a favorite made under one id finds the match under another.

    :param session: Caller-owned database session.
    :param ids: VLR ids or Riot series ids.
    :return: For each given id, itself plus the ledger and series ids of the same match.
    """
    known = {ref_id: {ref_id} for ref_id in ids}
    if not known:
        return known
    # Two passes, so an id found through the series also reaches the ledger row that holds it, and back.
    for _ in range(2):
        found = set().union(*known.values())
        rows = [
            *(
                await session.execute(
                    select(MatchRecord.id, MatchRecord.vlr_id, MatchRecord.riot_id).where(
                        or_(MatchRecord.id.in_(found), MatchRecord.vlr_id.in_(found), MatchRecord.riot_id.in_(found))
                    )
                )
            ).all(),
            *(
                await session.execute(
                    select(RiotWebMatch.riot_id, RiotWebMatch.vlr_match_id).where(
                        or_(RiotWebMatch.riot_id.in_(found), RiotWebMatch.vlr_match_id.in_(found))
                    )
                )
            ).all(),
        ]
        for row in rows:
            row_ids = {value for value in row if value}
            for ids_of in known.values():
                if ids_of & row_ids:
                    ids_of |= row_ids
    return known


async def shown_ids(session: AsyncSession, vlr_ids: Sequence[str]) -> dict[str, tuple[str | None, str | None]]:
    """Find the ids clients know each Riot-linked VLR match and its event by.

    A match first listed from Riot has a ``matches`` row whose id is its Riot series id. It keeps
    that id when VLR lists it, and its event keeps the Riot tournament id.

    :param session: Caller-owned database session.
    :param vlr_ids: VLR match ids.
    :return: Each VLR id a series is linked to, mapped to that series' id and tournament id when the
        match was first listed under them, else to a pair of None.
    """
    if not vlr_ids:
        return {}
    rows = await session.execute(
        select(RiotWebMatch.vlr_match_id, MatchRecord.id, RiotWebMatch.tournament_id)
        .outerjoin(MatchRecord, MatchRecord.id == RiotWebMatch.riot_id)
        .where(RiotWebMatch.vlr_match_id.in_(vlr_ids))
    )
    return {
        vlr_id: (riot_id, tournament_id if utils.is_riot_tournament_id(tournament_id) else None)
        if riot_id
        else (None, None)
        for vlr_id, riot_id, tournament_id in rows
    }


async def keep_ids(matches: Sequence[HasId]) -> None:
    """Relabel VLR-scraped matches, in place, with the ids clients first saw them under.

    Runs after the cache read, so cached payloads keep VLR's ids. When the database cannot be read
    the ids stay VLR's, which still open the match; with ``RIOT_WEB_API_KEY`` unset they stay VLR's too.

    :param matches: Matches of a VLR page, such as an event's or a team's.
    """
    if not matches or connections.subscription_sessions is None or not settings.RIOT_WEB_API_KEY:
        return
    try:
        async with asyncio.timeout(RIOT_WEB_VODS_TIMEOUT), connections.reading_session() as db:
            shown = await shown_ids(db, [match.id for match in matches])
    except Exception:
        logger.warning("could not read the ids matches are shown by", exc_info=True)
        return
    for match in matches:
        if riot_id := shown.get(match.id, (None, None))[0]:
            match.id = riot_id


async def resolve(ref_id: str, session: AsyncSession | None = None) -> MatchRef:
    """Look an id up by its shape: 1-10 digits is a VLR id, more digits a Riot series id.

    A VLR id needs no lookup: its match is the VLR page, whether or not a row holds it. A Riot id is
    looked up in the request's session, or in one short session of its own (closed before any VLR
    call, and given a second at most).

    :param ref_id: The path id.
    :param session: The request's session, if it has one.
    :return: The resolution; ``vlr_id`` is None for a Riot-only match.
    :raises NotFoundError: When no row holds a Riot id.
    :raises ServiceUnavailableError: When the database is unavailable.
    """
    if _is_vlr_id(ref_id):
        return MatchRef(ref_id, None, None)
    try:
        async with (
            asyncio.timeout(None if session else RIOT_WEB_VODS_TIMEOUT),
            connections.reading_session(session) as db,
        ):
            series = await db.get(RiotWebMatch, ref_id)
            # The mirror of the series link, else the row a VLR match took this id from before the mirror ran.
            ledger = await db.scalar(
                select(MatchRecord)
                .where(or_(MatchRecord.riot_id == ref_id, MatchRecord.id == ref_id))
                .order_by(MatchRecord.riot_id.is_(None))
                .limit(1)
            )
            needs_teams = series is not None and not series.vlr_match_id and not (ledger and ledger.vlr_id)
            teams = riot_web_store.teams_for(series, await riot_web_store.mirrored_teams(db)) if needs_teams else {}
    except OSError, TimeoutError, SQLAlchemyError:
        raise ServiceUnavailableError("Database is unavailable") from None
    # The series link is the source of truth; the ledger's mirror of it can be stale.
    vlr_id = (series.vlr_match_id if series else None) or (ledger.vlr_id if ledger else None)
    if vlr_id is None and series is None:
        raise NotFoundError("Match not found")
    return MatchRef(vlr_id, series, ledger, teams)


def event_id(series: RiotWebMatch, ledger: MatchRecord | None) -> str | None:
    """Name the event a Riot series belongs to.

    :param series: The stored series.
    :param ledger: Its ledger row, if any.
    :return: Its Riot tournament id when clients know the match by its Riot series id, so the event
        keeps one id too. Otherwise the VLR event it is linked to, else the ledger's event, else its
        Riot tournament id, and None while no tournament is known.
    """
    tournament = series.tournament_id if utils.is_riot_tournament_id(series.tournament_id) else None
    if tournament and ledger is not None and ledger.id == series.riot_id:
        return tournament
    return series.vlr_event_id or (ledger.event_id if ledger else None) or tournament


def team_img(team: Team | None) -> str:
    """Pick a mirrored team's first logo that is a valid URL, else the placeholder."""
    for value in (team.riot_logo, team.logo) if team else ():
        if (url := utils.resolve_http_url(value)) is not None:
            try:
                _HTTP_URL.validate_python(url)
            except ValidationError:
                continue
            return url
    return RIOT_PLACEHOLDER_IMG


def status(series: RiotWebMatch, ledger: MatchRecord | None, stored: StoredDetails | None) -> MatchStatus | None:
    """Derive a Riot series' status from its start, its ledger row and Riot's game wins.

    :param series: The stored series.
    :param ledger: Its ledger row, which carries a score only once the series is completed.
    :param stored: Its stored details.
    :return: Upcoming, live or completed; None once it is too old to still be live but no win decides it.
    """
    now = datetime.now(UTC)
    if series.start_time > now:
        return MatchStatus.UPCOMING
    needed = (series.best_of or 1) // 2 + 1
    scored = ledger is not None and ledger.team_a_score is not None
    if scored or (stored is not None and any(wins >= needed for wins in stored.game_wins.values())):
        return MatchStatus.COMPLETED
    if now - series.start_time <= RIOT_WEB_LIVE_MAX:
        return MatchStatus.LIVE
    return None


def riot_only_response(ref: MatchRef) -> MatchDetailsResponse:
    """Build the match response of a series VLR has no page for.

    Teams carry Riot's names and series scores; a team mirrored to a VLR team also carries its id and
    logo, any other gets no id and a placeholder logo. Bans, veto, previous encounters, streams, VOD
    links, per-map data and insights are empty, so ``current_map`` is None. ``event.id`` is the VLR
    event the series is attached to, else the ledger's event, else the Riot tournament id (the id of
    its Riot-created event), and the empty string only while no tournament is known. ``total_maps`` is Riot's best-of; ``map_count`` is the games Riot marks completed.

    :param ref: A resolution whose ``vlr_id`` is None and whose ``series`` is set.
    :return: The same model the VLR-backed route returns.
    """
    series, ledger = ref.series, ref.ledger
    assert series is not None
    stored = riot_web_store.read_details(series)
    series_status = status(series, ledger, stored)
    # Riot answers 0 wins for a series that has not started; a VLR page has no score there.
    played = stored is not None and series_status != MatchStatus.UPCOMING
    teams = []
    for item in series.teams:
        name = item["name"].strip()
        mirrored = ref.teams.get(utils.normalize_name(name))
        teams.append(
            TeamWithImage(
                name=name,
                score=stored.game_wins.get(name) if played else None,
                id=mirrored.id if mirrored else None,
                img=team_img(mirrored),
            )
        )
    return MatchDetailsResponse(
        teams=teams,
        bans=[],
        event=Event(
            id=event_id(series, ledger) or "",
            img=RIOT_PLACEHOLDER_IMG,
            series=series.league_name,
            stage=(ledger.stage if ledger else None) or "",
            date=series.start_time,
            patch=ledger.patch if ledger else None,
            status=series_status,
        ),
        videos=MatchVideos(streams=[], vods=[]),
        map_count=sum(game.state == RiotGameState.COMPLETED for game in stored.games) if stored else 0,
        total_maps=series.best_of or 1,
        data=[],
        previous_encounters=[],
        map_vods=[MapVods(map_number=number, links=links) for number, links in match_vods.vods_by_map(stored).items()]
        if stored
        else [],
    )
