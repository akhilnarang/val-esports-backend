from fastapi import APIRouter

from app import schemas
from app.services import match_refs, team

router = APIRouter()


@router.get("/{team_id}", response_model=schemas.Team)
async def get_team_by_id(team_id: str) -> schemas.Team:
    data = await team.get_team_data(team_id)
    await match_refs.keep_ids([*data.upcoming, *data.completed])
    return data
