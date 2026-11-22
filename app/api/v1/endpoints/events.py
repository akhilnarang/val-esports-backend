import contextlib

from fastapi import APIRouter

from app import cache, schemas, utils
from app.api import deps
from app.exceptions import ServiceUnavailableError
from app.services import brackets, events, match_refs, riot_events

router = APIRouter()


@router.get("/")
async def list_events(client: deps.RedisDep) -> list[schemas.Event]:
    if data := await cache.get("events", client=client):
        return schemas.EventListAdapter.validate_json(data)
    return await events.get_events(client)


@router.get("/{id}/brackets")
async def event_brackets(id: deps.EventId, client: deps.RedisDep) -> schemas.EventBrackets:
    """Return Riot's bracket layout for an event, with a status saying what it does not cover."""
    requested = id
    if utils.is_riot_tournament_id(id):
        # Without the database the tournament's own brackets still answer, marked unavailable when unread.
        with contextlib.suppress(ServiceUnavailableError):
            id = await riot_events.vlr_event_for(id) or id
    shown = await brackets.event_brackets(id, client)
    shown.event_id = requested  # a client files the answer under the id it asked for
    return shown


@router.get("/{id}")
async def event_by_id(id: str, client: deps.RedisDep) -> schemas.EventWithDetails:
    """Return an event by VLR event id or Riot tournament id."""
    requested = id
    if utils.is_riot_tournament_id(id):
        if (vlr_id := await riot_events.vlr_event_for(id)) is None:
            return await riot_events.event_with_details(id, client)
        id = vlr_id
    event = await events.get_event_by_id(id, client)
    await match_refs.keep_ids(event.matches)
    event.id = requested  # a client files the event under the id it asked for, also once VLR lists it
    return event
