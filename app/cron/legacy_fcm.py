"""Legacy "match starting soon" FCM alerts on the original `match-`, `event-`, and `team-` topics.

Released app versions render every message on these topics as a notification, so live
scores go directly to registration tokens (see app/services/fcm.py) and these stay unchanged.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from firebase_admin import messaging
from sentry_sdk import capture_exception, get_current_scope
from sqlalchemy import select

from app import utils
from app.constants import MatchStatus
from app.core import connections
from app.core.config import settings
from app.db.models import RiotWebMatch
from app.services import matches
from app.services.fcm import get_app

logger = logging.getLogger(__name__)


async def _riot_ids(match_ids: list[str]) -> dict[str, tuple[str, str | None]]:
    """Find the Riot series and tournament ids of VLR matches, which a favorite may be stored under.

    :param match_ids: VLR match ids.
    :return: VLR match id mapped to its Riot series id and tournament id; empty when the database
        cannot be read, because the alert must still go out.
    """
    sessions = connections.subscription_sessions
    if sessions is None:
        return {}
    try:
        async with sessions() as session:
            rows = await session.execute(
                select(RiotWebMatch.vlr_match_id, RiotWebMatch.riot_id, RiotWebMatch.tournament_id).where(
                    RiotWebMatch.vlr_match_id.in_(match_ids)
                )
            )
            return {
                vlr_id: (riot_id, tournament_id if utils.is_riot_tournament_id(tournament_id) else None)
                for vlr_id, riot_id, tournament_id in rows
            }
    except Exception:
        logger.warning("could not read the Riot ids of upcoming matches", exc_info=True)
        return {}


async def fcm_notification_cron(ctx: dict) -> None:
    """
    Function to notify users about upcoming matches
    :param ctx: Context dict
    :return: Nothing
    """
    get_current_scope().set_transaction_name("FCM Notification Cron")
    client = ctx["redis"]

    # Get the current time, so that we can filter for matches starting in the next 15 minutes
    current_time = datetime.now(tz=ZoneInfo(settings.TIMEZONE))
    upcoming_matches = [
        match
        for match in await matches.get_upcoming_matches(redis_client=client)
        if match.status == MatchStatus.UPCOMING and 0 < (match.time - current_time).total_seconds() < 900
    ]

    if not upcoming_matches:
        logger.info("No notifications to send")
        return

    # Fetch all match details concurrently (skip failures gracefully)
    all_match_details = await asyncio.gather(
        *[matches.match_by_id(match.id, redis_client=client) for match in upcoming_matches],
        return_exceptions=True,
    )

    riot_ids = await _riot_ids([match.id for match in upcoming_matches])

    # Build notification messages, skipping any failed lookups
    messages = []
    for match, match_details in zip(upcoming_matches, all_match_details):
        if isinstance(match_details, BaseException):
            capture_exception(match_details)
            logger.warning(f"Failed to fetch match details for {match.id}: {match_details}")
            continue

        logger.info(f"Sending notification for {match=}")

        team1_id, team2_id = (team.id for team in match_details.teams)
        time_to_start = int((match.time - current_time).total_seconds() // 60)

        payload = {
            "title": f"{match.team1.name} vs {match.team2.name}",
            "body": f"Match is starting in {time_to_start} minutes",
            "timestamp": match.time.isoformat(),
            "match_id": match.id,
        }
        if streams := match_details.videos.streams:
            payload |= {"stream_url": streams[0].url.unicode_string()}

        riot_id, tournament_id = riot_ids.get(match.id, (None, None))
        # A match or event the user favorited by its Riot id is subscribed under that id. The payload
        # keeps the VLR id, which every released Android app can take. FCM allows five topics a condition.
        usual = (
            f"'event-{match_details.event.id}' in topics || 'match-{match.id}' in topics || "
            f"'team-{team1_id}' in topics || 'team-{team2_id}' in topics"
        )
        conditions = [usual + (f" || 'match-{riot_id}' in topics" if riot_id else "")]
        if tournament_id:
            conditions.append(
                f"'event-{tournament_id}' in topics && !('event-{match_details.event.id}' in topics || "
                f"'match-{riot_id}' in topics || 'team-{team1_id}' in topics || 'team-{team2_id}' in topics)"
            )
        messages.extend(
            messaging.Message(
                data=payload, condition=condition, android=messaging.AndroidConfig(ttl=timedelta(minutes=30))
            )
            for condition in conditions
        )

    # Don't bother sending if there's nothing to send
    if not messages:
        return

    await messaging.send_each_async(messages=messages, dry_run=False, app=get_app())
    logger.info("Sent notification")
