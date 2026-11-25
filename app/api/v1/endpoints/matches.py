import asyncio
import time

from fastapi import APIRouter

from app import cache, schemas
from app.api import deps
from app.core.config import settings
from app.schemas.riot import MatchExtra
from app.services import insights, match_refs, match_vods, matches, riot, riot_listing

router = APIRouter()


@router.get("/")
async def get_matches(client: deps.RedisDep) -> list[schemas.ListedMatch]:
    if data := await cache.get("matches", client=client):
        listed = schemas.MatchListAdapter.validate_json(data)
    else:
        listed = await matches.match_list(redis_client=client)
    return await riot_listing.with_riot_only(await matches.with_broadcast_map(listed, client))


@router.get("/{id}")
async def get_match_by_id(id: deps.MatchRef, client: deps.RedisDep) -> schemas.MatchDetailsResponse:
    """Get a match by its VLR id or its Riot series id."""
    ref = await match_refs.resolve(id)
    if ref.vlr_id is None:
        return match_refs.riot_only_response(ref)
    deadline = time.monotonic() + settings.INSIGHTS_REQUEST_BUDGET_SECONDS
    details = await matches.cached_match_by_id(ref.vlr_id, client)
    response, vods = await asyncio.gather(
        insights.attach(ref.vlr_id, details, deadline), match_vods.map_vods(ref.vlr_id, details)
    )
    response.map_vods = vods
    if ref.series is not None and ref.ledger is not None and ref.ledger.id == ref.series.riot_id:
        # Clients know this match by its Riot id, and its event by the tournament id, as the list shows it.
        event_id = match_refs.event_id(ref.series, ref.ledger) or response.event.id
        response.event = response.event.model_copy(update={"id": event_id})
    return response


@router.get("/{id}/extra")
async def get_match_extra(id: deps.MatchRef, session: deps.DatabaseSessionDep) -> MatchExtra:
    """Return round-by-round Riot data for the match's maps that are linked to Riot games."""
    return await riot.match_extra(session, await match_refs.vlr_id_of(id, session))
