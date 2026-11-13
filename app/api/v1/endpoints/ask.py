from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.agent.ratelimit import client_ip, enforce_rate_limit
from app.agent.runner import run_ask
from app.api.deps import RedisDep
from app.schemas import AskRequest, AskResponse

router = APIRouter()


@router.post("/")
async def ask(
    request: Request,
    body: AskRequest,
    redis_client: RedisDep,
    x_forwarded_for: Annotated[str | None, Header()] = None,
) -> AskResponse:
    """Natural-language Q&A over vlr.gg data.

    The rate-limit client IP comes from the X-Forwarded-For header (assumes a
    trusted reverse proxy in production), falling back to the socket peer address.
    """
    ip = client_ip(x_forwarded_for, request.client.host if request.client else None)
    await enforce_rate_limit(redis_client, ip)
    return await run_ask(body.query, redis_client=redis_client)
