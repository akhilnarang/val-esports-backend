"""Select live, upcoming and recently completed matches for a client's stored favorites."""

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, constants, schemas, utils
from app.constants import MatchStatus
from app.exceptions import ServiceUnavailableError
from app.schemas.matches import Favorites
from app.services import match_refs, matches, riot_listing, riot_web_store, scrape_store
from app.services.subscription_store import SubscriptionStore
from app.utils import is_live


async def favorite_matches(
    session: AsyncSession, client_id: str, redis_client: Redis, include_results: bool = False
) -> list[schemas.ListedMatch]:
    """Return the next live and upcoming matches of each favorite, live first then by start time.

    Never fetches from VLR: matches come from the cron-filled match list cache, and a player counts as their
    current team from the players table, filled by the players cron (players it has not fetched yet are skipped).

    :param session: Request database session.
    :param client_id: Client UUID.
    :param redis_client: Redis client holding the match list cache.
    :param include_results: Also return completed matches that started in the last ``FAVORITE_RESULTS_WINDOW``,
        newest first, after the live and upcoming ones.
    :return: Deduplicated matches, at most ``FAVORITE_MATCHES_PER_ENTITY`` per favorite in each of the two groups.
    :raises NotFoundError: If the client is not registered.
    :raises ServiceUnavailableError: If the match list cache is empty.
    """
    favorites = await SubscriptionStore(session).get_favorites(client_id)
    if not (data := await cache.get("matches", client=redis_client)):
        # Only the cron fills the match list, so a miss never turns into per-request VLR fetches.
        raise ServiceUnavailableError("Match list is not available yet")
    # The feed shows each match as the match list does: Riot-only ones too, each under the same id.
    shown = await riot_listing.with_riot_only(
        await matches.with_broadcast_map(schemas.MatchListAdapter.validate_json(data), redis_client), session
    )
    active = sorted(
        (match for match in shown if is_live(match.status) or match.status == MatchStatus.UPCOMING),
        key=lambda match: (not is_live(match.status), match.time),
    )
    player_teams = await scrape_store.player_team_ids(session, favorites.players)
    team_ids = [*favorites.teams, *(team_id for team_id in player_teams.values() if team_id)]
    match_ids = await match_refs.aliases(session, favorites.matches)
    # An event favorited as a Riot-created one, by its tournament id, is its VLR event once VLR lists it.
    linked = await riot_web_store.events_by_tournament(
        session, [event_id for event_id in favorites.events if utils.is_riot_tournament_id(event_id)]
    )
    event_ids = {event_id: {event_id} | linked.get(event_id, set()) for event_id in favorites.events}
    # And back: a match clients know by its Riot id names its event by the tournament id.
    for event_id, tournament_id in await riot_web_store.tournaments_of_events(session, favorites.events):
        event_ids[event_id].add(tournament_id)
    selected = _select(active, favorites, team_ids, match_ids, event_ids)
    if include_results:
        since = datetime.now(UTC) - constants.FAVORITE_RESULTS_WINDOW
        results = sorted(
            (match for match in shown if match.status == MatchStatus.COMPLETED and match.time >= since),
            key=lambda match: match.time,
            reverse=True,
        )
        selected += _select(results, favorites, team_ids, match_ids, event_ids)
    return selected


def _select(
    pool: list[schemas.ListedMatch],
    favorites: Favorites,
    team_ids: list[str],
    match_ids: dict[str, set[str]],
    event_ids: dict[str, set[str]],
) -> list[schemas.ListedMatch]:
    """Keep the first matches of each favorite from an ordered pool.

    :param pool: Matches in display order.
    :param favorites: The client's favorites.
    :param team_ids: Favorited teams plus the current teams of favorited players.
    :param match_ids: Every id each favorited match is known by.
    :param event_ids: Every id each favorited event is known by.
    :return: Matches of the pool, in pool order, at most ``FAVORITE_MATCHES_PER_ENTITY`` per favorite.
    """
    groups = [
        *([match for match in pool if team_id in (match.team1.id, match.team2.id)] for team_id in team_ids),
        *([match for match in pool if match.event_id in event_ids[event_id]] for event_id in favorites.events),
        *([match for match in pool if match.id in match_ids[match_id]] for match_id in favorites.matches),
    ]
    selected = {match.id for group in groups for match in group[: constants.FAVORITE_MATCHES_PER_ENTITY]}
    return [match for match in pool if match.id in selected]
