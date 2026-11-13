from fastapi import APIRouter

from app.api.deps import RedisDep
from app.schemas import TeamCache

router = APIRouter()


@router.get("/team_cache")
async def get_team_cache(client: RedisDep) -> list[TeamCache]:
    return [TeamCache(name=k.decode(), id=v.decode()) for k, v in (await client.hgetall("team")).items()]  # type: ignore


@router.put("/team_cache")
async def set_team_cache(teams: list[TeamCache], client: RedisDep) -> list[TeamCache]:
    await client.hset("team", mapping={team.name: team.id for team in teams})  # type: ignore
    return [TeamCache(name=k.decode(), id=v.decode()) for k, v in (await client.hgetall("team")).items()]  # type: ignore
