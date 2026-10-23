"""Archive games from Riot's esports match API, and link them to the VLR maps they were."""

import logging
import time
from collections import defaultdict
from http import HTTPStatus

import httpx2
from sentry_sdk import get_current_scope
from sqlalchemy import select

from app import schemas
from app.constants import RIOT_RUN_BUDGET
from app.core import connections
from app.core.config import settings
from app.db.models import RiotMatch
from app.schemas.riot import Candidate, Game, GameLink
from app.services import matches, riot

logger = logging.getLogger(__name__)


async def riot_matches_cron(ctx: dict) -> None:
    """Archive each completed game Riot lists, then link the unlinked ones to VLR maps.

    Riot lists a game for about 12 hours and keeps no history, so every completed game is stored whole as soon as it
    is seen, whether or not VLR has a map for it yet. Linking then runs for the games still unlinked, and a later run
    tries again while Riot still lists them; without ``LLM_API_KEY`` the archive still runs and only linking is
    skipped. A game the model turned down, or that competes for a map, is remembered and not asked about again. A VLR
    map that several listed games link to, such as a voided game and its replay, gets none of them. Checking stops
    when the run's time budget is spent or Riot rate-limits it, and what was found so far is still stored.

    :param ctx: arq context holding the shared Redis client.
    :return: None.
    """
    get_current_scope().set_transaction_name("Riot Matches Cron")
    sessions = connections.subscription_sessions
    redis = ctx["redis"]
    if sessions is None:
        return
    deadline = time.monotonic() + RIOT_RUN_BUDGET.total_seconds()
    pages: dict[str, schemas.MatchWithDetails] = {}
    claims: dict[tuple[str, int], list[tuple[Game, Candidate, GameLink]]] = defaultdict(list)
    async with riot.client() as client:
        try:
            game_ids = await riot.recent_game_ids(client)
        except httpx2.HTTPError as error:
            logger.warning("could not list Riot's recent games: %s", error)
            return
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(RiotMatch.riot_id, RiotMatch.data, RiotMatch.vlr_match_id).where(
                        RiotMatch.riot_id.in_(game_ids)
                    )
                )
            ).all()
        archived = {row.riot_id for row in rows}
        stored = {row.riot_id: row.data for row in rows if row.vlr_match_id is None}
        games: dict[str, Game] = {}
        for riot_id in game_ids:
            if riot_id in stored:
                try:
                    game = riot.parse_game(riot_id, stored[riot_id])
                except Exception:
                    logger.warning("could not read stored Riot game %s; will retry", riot_id, exc_info=True)
                    game = None
                if game is not None and game.linkable:
                    games[riot_id] = game
                    continue
                # An incomplete copy is fetched again while Riot lists the game; a failed fetch keeps it.
            elif riot_id in archived:
                continue
            if time.monotonic() > deadline:
                logger.info("Riot run out of time; later games wait for the next run")
                break
            try:
                game = riot.parse_game(riot_id, await riot.game(client, riot_id))
            except httpx2.HTTPStatusError as error:
                if error.response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    logger.warning("Riot rate-limited the run; stopping until the next one")
                    break
                logger.warning("could not fetch Riot game %s; will retry", riot_id, exc_info=True)
                continue
            except Exception:
                logger.warning("could not read Riot game %s; will retry", riot_id, exc_info=True)
                continue
            if game is None:
                # Riot may fill in the rounds later, so a later run retries it while Riot still lists it.
                continue
            try:
                async with sessions.begin() as session:
                    await riot.archive(session, game)
            except Exception:
                logger.warning("could not archive Riot game %s; will retry", riot_id, exc_info=True)
                continue
            games[riot_id] = game

        if not settings.LLM_API_KEY:
            logger.info("riot archive: %d games stored; no LLM_API_KEY, so none were linked", len(games))
            return
        # Rejections only gate linking: a game rejected before it was archived must still be archived.
        skipped = await riot.rejected(redis, list(games))
        if not games.keys() - skipped:
            return
        listing = await matches.cached_match_list(redis)

        for riot_id, game in games.items():
            if riot_id in skipped:
                continue
            if time.monotonic() > deadline:
                logger.info("Riot run out of time; later games wait for the next run")
                break
            try:
                found = await riot.find_link(game, listing, redis, pages)
            except Exception:
                logger.warning("could not link Riot game %s; will retry", riot_id, exc_info=True)
                continue
            if found is not None:
                candidate, link = found
                claims[(candidate.match_id, candidate.map_number)].append((game, candidate, link))

    for (match_id, map_number), claimed in claims.items():
        if len(claimed) > 1:
            riot_ids = [game.riot_id for game, _, _ in claimed]
            logger.warning("Riot games %s all link to VLR %s map %s; linking none", riot_ids, match_id, map_number)
            for riot_id in riot_ids:
                await riot.reject(redis, riot_id)
            continue
        [(game, candidate, link)] = claimed
        try:
            async with sessions.begin() as session:
                linked = await riot.link(session, game, candidate, link)
        except Exception:
            logger.warning("could not link Riot game %s; will retry", game.riot_id, exc_info=True)
            continue
        if linked:
            logger.info("linked Riot game %s to VLR %s map %s", game.riot_id, match_id, map_number)
        else:
            logger.warning(
                "Riot game %s or VLR %s map %s is already linked; not linking it", game.riot_id, match_id, map_number
            )
            await riot.reject(redis, game.riot_id)
