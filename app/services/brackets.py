"""Riot's bracket layout for a VLR event, fetched on demand and converted on read.

Riot's ``getStandings`` answer is a layout, not a round tree: ``columns`` are topological layers,
``cells`` are display groups that may hold several matches or mix bracket sides, and a slot's
``origin`` is the only progression edge. This module returns that layout faithfully and does not
infer rounds, sides, formats or byes, because every one of those has counter-examples.
"""

import asyncio
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError

from app import cache
from app.constants import (
    CACHE_TTL_RIOT_WEB_BRACKETS,
    RIOT_BRACKET_SECTION,
    RIOT_MATCH_ORIGIN,
    RIOT_TBD_TEAM_ID,
    RIOT_WEB_BRACKETS_KEY,
    RIOT_WEB_REQUEST_TIMEOUT,
    RIOT_WIN_OUTCOME,
    BracketSourceKind,
    BracketSourceResult,
    BracketStatus,
    MatchStatus,
    RiotMatchState,
)
from app.core import connections
from app.core.config import settings
from app.db.models import RiotWebMatch
from app.schemas.brackets import (
    BracketCell,
    BracketColumn,
    BracketMatch,
    BracketSection,
    BracketSlot,
    BracketSource,
    BracketTeam,
    EventBrackets,
)
from app.schemas.riot_web import StandingsMatch, StandingsOrigin, StandingsTeam, Tournament
from app.services import riot_web, riot_web_store

logger = logging.getLogger(__name__)

_STATUS = {
    RiotMatchState.UNSTARTED: MatchStatus.UPCOMING,
    RiotMatchState.IN_PROGRESS: MatchStatus.LIVE,
    RiotMatchState.COMPLETED: MatchStatus.COMPLETED,
}


def _fold(value: str | None) -> str:
    """Fold a team name to a comparison key.

    :param value: Riot's team name.
    :return: Lowercase ASCII words, or an empty string when there is no value.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def _vlr_team_ids(rows: Sequence[tuple[str, str | None]]) -> dict[str, str]:
    """Index each Riot name to the VLR team it was matched to.

    Only the name is indexed: Riot codes repeat across rosters (Game Changers' ``VISA KRÜ Blaze``
    and the main ``KRÜ Esports`` both carry ``KRU``), so a code lookup links the wrong team. A name
    that two VLR teams carry is dropped: the app links to a team, so a guess would be wrong.

    :param rows: (VLR team id, Riot name) rows.
    :return: Folded Riot name mapped to its one VLR team id.
    """
    seen: dict[str, set[str]] = {}
    for team_id, riot_name in rows:
        if folded := _fold(riot_name):
            seen.setdefault(folded, set()).add(team_id)
    return {key: next(iter(ids)) for key, ids in seen.items() if len(ids) == 1}


def _team(team: StandingsTeam, vlr_teams: dict[str, str]) -> BracketTeam | None:
    """Build a display team; Riot's TBD team id is ``"0"``.

    ``vlr_team_id`` is set only when the mirrored Riot name resolves to one VLR team, so a Riot
    team the id mirror never matched keeps ``None`` rather than a guess.

    :param team: Riot's team entry.
    :param vlr_teams: Folded Riot name mapped to its VLR team id.
    :return: The team, or None for a TBD slot.
    """
    if team.id == RIOT_TBD_TEAM_ID:
        return None
    image = str(team.image) if team.image else None
    if image and image.startswith("http://"):
        image = f"https://{image.removeprefix('http://')}"
    return BracketTeam(
        id=team.id,
        name=team.name.strip(),
        code=team.code.strip(),
        img=image,
        vlr_team_id=vlr_teams.get(_fold(team.name)),
    )


def _source(origin: StandingsOrigin | None, structural_ids: dict[str, str]) -> BracketSource | None:
    """Turn a slot's entry edge into a match or seed source.

    :param origin: Riot's per-team ``origin``.
    :param structural_ids: Section-local ``structuralId`` to Riot match id.
    :return: The source, or None when Riot gave none.
    """
    if origin is None:
        return None
    if origin.type == RIOT_MATCH_ORIGIN:
        return BracketSource(
            kind=BracketSourceKind.MATCH,
            match_id=structural_ids.get(origin.structural_id),
            result=BracketSourceResult.WINNER if origin.slot == 1 else BracketSourceResult.LOSER,
        )
    return BracketSource(kind=BracketSourceKind.SEED, seed=origin.slot)


def _match(
    match: StandingsMatch,
    structural_ids: dict[str, str],
    link: RiotWebMatch | None,
    vlr_teams: dict[str, str],
) -> BracketMatch:
    """Convert one Riot series, keeping its two slots in display order.

    :param match: Riot's match entry.
    :param structural_ids: Section-local ``structuralId`` to Riot match id.
    :param link: The stored series, when the schedule holds it.
    :param vlr_teams: Folded Riot name mapped to its VLR team id.
    :return: The public match.
    """
    status = _STATUS.get(match.state, MatchStatus.TBD)
    settled = status in (MatchStatus.COMPLETED, MatchStatus.LIVE)
    slots = [
        BracketSlot(
            team=_team(team, vlr_teams),
            source=_source(team.origin, structural_ids),
            score=team.result.game_wins if settled and team.result else None,
            winner=(team.result.outcome == RIOT_WIN_OUTCOME) if settled and team.result else None,
        )
        for team in match.teams
    ]
    return BracketMatch(
        id=match.id,
        vlr_match_id=link.vlr_match_id if link else None,
        label=match.description or None,
        status=status,
        start_time=link.start_time if link else None,
        best_of=link.best_of if link else None,
        slots=slots,
    )


def _owned(owners: set[str], tournament_events: set[str], event_id: str, *, single_section: bool) -> bool:
    """Decide whether one section is the requested event's to show.

    :param owners: VLR events named by the section's linked matches.
    :param tournament_events: VLR events every linked match in the tournament names.
    :param event_id: VLR event the request is for.
    :param single_section: Whether the tournament has exactly one bracket section and dropped none.
    :return: Whether the section belongs to the request.
    """
    if owners:
        return owners == {event_id}
    # A section with no linked matches can only inherit the tournament's own mapping when that
    # mapping is unambiguous. A tournament shared by several VLR events (Riot's Game Changers
    # LATAM, for example) has sibling sections that have not linked yet, so its event-wide map
    # would otherwise serve one event another's bracket.
    return single_section and tournament_events == {event_id}


def sections_from(
    tournament: Tournament,
    links: dict[str, RiotWebMatch],
    *,
    event_id: str,
    match_events: dict[str, str],
    tournament_events: set[str],
    vlr_teams: dict[str, str],
) -> list[BracketSection]:
    """Convert the bracket sections owned by one event, preserving column, cell and match order.

    ``group`` and ``crossGroup`` sections are tables VLR's own standings already cover. A section
    with no ``match`` origin (Swiss, for example) renders with seed sources only.

    A section is owned by the VLR event its linked matches name; unlinked matches stay inside it. A
    section with no linked matches is kept only when the tournament is known to map to this event
    alone, has no other bracket section, and dropped none as unreadable (see :func:`_owned`), so the
    same Riot tournament can serve several VLR events without showing one of them another's bracket.

    :param tournament: Riot's layout, already validated.
    :param links: Riot series id mapped to its stored row.
    :param event_id: VLR event the request is for.
    :param match_events: Riot series id mapped to the VLR event its link names.
    :param tournament_events: VLR events every linked match in the tournament names.
    :param vlr_teams: Folded Riot name mapped to its VLR team id.
    :return: One section per Riot ``type: bracket`` section owned by the event, in Riot's order.
    """
    single_section = sum(
        1 for stage in tournament.stages for section in stage.sections if section.type == RIOT_BRACKET_SECTION
    ) == 1 and not any(stage.had_invalid_section for stage in tournament.stages)
    sections = []
    for stage in tournament.stages:
        for section in stage.sections:
            if section.type != RIOT_BRACKET_SECTION:
                continue
            owners = {
                match_events[match.id]
                for column in section.columns
                for cell in column.cells
                for match in cell.matches
                if match.id in match_events
            }
            if not _owned(owners, tournament_events, event_id, single_section=single_section):
                logger.info(
                    "withholding Riot bracket section %s for event %s: links name %s, tournament links name %s",
                    section.id,
                    event_id,
                    sorted(owners),
                    sorted(tournament_events),
                )
                continue
            structural_ids = {
                match.structural_id: match.id
                for column in section.columns
                for cell in column.cells
                for match in cell.matches
            }
            sections.append(
                BracketSection(
                    id=section.id,
                    tournament=tournament.name,
                    stage=stage.name,
                    name=section.name,
                    columns=[
                        BracketColumn(
                            cells=[
                                BracketCell(
                                    name=cell.name,
                                    matches=[
                                        _match(match, structural_ids, links.get(match.id), vlr_teams)
                                        for match in cell.matches
                                    ],
                                )
                                for cell in column.cells
                            ]
                        )
                        for column in section.columns
                    ],
                )
            )
    return sections


@dataclass(frozen=True)
class _Brackets:
    """What one request could learn, and whether a source went unanswered."""

    sections: list[BracketSection]
    fetched_at: datetime | None  # when the returned sections' layouts were fetched
    incomplete: bool  # a source went unanswered, so no sections may mean "unknown", not "none"


async def _sections(event_id: str, redis: Redis | None) -> _Brackets:
    """Fetch each linked tournament's layout, then convert the ones that parse.

    All mapped tournaments are fetched at once, so an event with several 5-second timeouts does
    not serialize them. Nothing is stored: the converted response is the only cache.

    :param event_id: VLR event id.
    :param redis: Request Redis client.
    :return: Converted sections with their fetch time, and whether any source was unanswered.
    """
    sessions = connections.subscription_sessions
    if sessions is None:
        return _Brackets([], None, True)

    try:
        async with sessions() as session:
            tournament_ids = await riot_web_store.tournaments_for_event(session, event_id)
    except SQLAlchemyError:
        logger.warning("could not read Riot tournaments for event %s", event_id, exc_info=True)
        return _Brackets([], None, True)
    if not tournament_ids:
        # No link names Riot for this event, so whether Riot has a bracket is unknown, not "none".
        return _Brackets([], None, True)

    now = datetime.now(UTC)
    async with riot_web.client() as client:
        fetched = await asyncio.gather(
            *(
                riot_web.standings(client, tournament_id, timeout=RIOT_WEB_REQUEST_TIMEOUT)
                for tournament_id in tournament_ids
            ),
            return_exceptions=True,
        )

    tournaments: dict[str, Tournament] = {}
    incomplete = False
    for tournament_id, result in zip(tournament_ids, fetched, strict=True):
        if isinstance(result, httpx2.HTTPStatusError):
            if redis is not None:
                await riot_web.report_rejection(redis, result)
            logger.warning("could not fetch Riot standings for tournament %s: %s", tournament_id, result)
            incomplete = True
            continue
        if isinstance(result, BaseException):
            logger.warning("could not fetch Riot standings for tournament %s", tournament_id, exc_info=result)
            incomplete = True
            continue
        if result is not None:
            if any(stage.had_invalid_section for stage in result.stages):
                incomplete = True
            tournaments[tournament_id] = result

    if not tournaments:
        return _Brackets([], None, incomplete)

    ordered = sorted(
        tournaments.items(),
        key=lambda item: item[1].start_time or datetime.max.replace(tzinfo=UTC),
    )
    match_ids = [
        match.id
        for tournament in tournaments.values()
        for stage in tournament.stages
        for section in stage.sections
        if section.type == RIOT_BRACKET_SECTION
        for column in section.columns
        for cell in column.cells
        for match in cell.matches
    ]
    if not match_ids:
        return _Brackets([], None, incomplete)
    try:
        async with sessions() as session:
            links = await riot_web_store.matches_by_riot_id(session, match_ids)
            match_events = await riot_web_store.events_by_riot_id(session, match_ids)
            tournament_events = await riot_web_store.events_by_tournament(session, tournament_ids)
            vlr_teams = _vlr_team_ids(await riot_web_store.riot_team_fields(session))
    except SQLAlchemyError:
        logger.warning("could not read Riot series links for event %s", event_id, exc_info=True)
        return _Brackets([], None, True)

    sections: list[BracketSection] = []
    for tournament_id, tournament in ordered:
        converted = sections_from(
            tournament,
            links,
            event_id=event_id,
            match_events=match_events,
            tournament_events=tournament_events.get(tournament_id, set()),
            vlr_teams=vlr_teams,
        )
        if converted:
            sections.extend(converted)
    return _Brackets(sections, now if sections else None, incomplete)


def _status(brackets: _Brackets) -> BracketStatus:
    """Name what the response's sections do and do not cover.

    :param brackets: What the request could learn.
    :return: The status the client should render.
    """
    if brackets.incomplete:
        return BracketStatus.STALE if brackets.sections else BracketStatus.UNAVAILABLE
    return BracketStatus.OK if brackets.sections else BracketStatus.NO_BRACKET


async def event_brackets(event_id: str, client: Redis | None = None) -> EventBrackets:
    """Return a VLR event's Riot brackets.

    An event with no Riot tournament, an unset key, or an unreachable Riot answers with no
    sections rather than an error: a mapping gap is not a client error. ``status`` says whether
    that empty answer means Riot has no bracket (``no_bracket``) or the answer is unknown
    (``unavailable``), and whether returned sections are incomplete (``stale``). Every answer is
    cached for Riot's 60-second CDN window, an incomplete one too, so a failing Riot is asked at
    most once a minute per event rather than on every request.

    :param event_id: VLR event id.
    :param client: Request Redis client.
    :return: The public bracket response.
    """
    if not settings.RIOT_WEB_API_KEY:
        return EventBrackets(event_id=event_id, status=BracketStatus.UNAVAILABLE)

    key = RIOT_WEB_BRACKETS_KEY.format(event_id)
    if raw := await cache.get(key, client=client):
        try:
            return EventBrackets.model_validate_json(raw)
        except ValidationError:
            # A short-lived converted payload: an old shape is a miss, never a 5xx.
            logger.warning("cached Riot brackets for event %s no longer parse; rebuilding", event_id)
    brackets = await _sections(event_id, client)
    result = EventBrackets(
        event_id=event_id,
        sections=brackets.sections,
        available=bool(brackets.sections),
        status=_status(brackets),
        fetched_at=brackets.fetched_at,
    )
    await cache.set(key, result.model_dump_json(), ttl=CACHE_TTL_RIOT_WEB_BRACKETS, client=client)
    return result
