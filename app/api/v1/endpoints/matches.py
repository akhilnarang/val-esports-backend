import asyncio
import time

from fastapi import APIRouter

from app import cache, schemas
from app.api import deps
from app.core.config import settings
from app.schemas.riot import MatchExtra
from app.services import insights, match_vods, matches, riot

router = APIRouter()


@router.get("/")
async def get_matches(client: deps.RedisDep) -> list[schemas.ListedMatch]:
    if data := await cache.get("matches", client=client):
        listed = schemas.MatchListAdapter.validate_json(data)
    else:
        listed = await matches.match_list(redis_client=client)
    return await matches.with_broadcast_map(listed, client)


@router.get("/{id}")
async def get_match_by_id(id: deps.MatchId, client: deps.RedisDep) -> schemas.MatchDetailsResponse:
    deadline = time.monotonic() + settings.INSIGHTS_REQUEST_BUDGET_SECONDS
    details = await matches.cached_match_by_id(id, client)
    response, vods = await asyncio.gather(insights.attach(id, details, deadline), match_vods.map_vods(id, details))
    response.map_vods = vods
    return response


@router.get("/{id}/extra")
async def get_match_extra(id: deps.MatchId, session: deps.DatabaseSessionDep) -> MatchExtra:
    """Return round-by-round Riot data for the match's maps that are linked to Riot games."""
    return await riot.match_extra(session, id)
