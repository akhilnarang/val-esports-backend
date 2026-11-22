import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from app import constants
from app.constants import RIOT_PLACEHOLDER_IMG, MatchStatus
from app.exceptions import ScrapingError
from app.services import matches

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_match_details_follow_the_public_response_contract(http_response):
    response = http_response("https://www.vlr.gg/12345", (FIXTURE_DIR / "match_12345.html").read_bytes())

    with patch("httpx2.AsyncClient.get", return_value=response):
        result = await matches.match_by_id("12345", AsyncMock())

    assert [(team.name, team.score, team.tag) for team in result.teams] == [("Team A", 2, "A"), ("Team B", 1, "B")]
    assert result.event.id == "2283"
    assert result.event.series == "Event Series"
    assert result.event.patch == "13.05"
    assert result.map_count == 2
    assert [(item.number, item.map, [team.score for team in item.teams], item.winner) for item in result.data] == [
        (1, "Lotus", [13, 10], "Team A"),
        (3, "Split", [None, None], None),
    ]
    member = result.data[0].members[0]
    assert (member.id, member.name, member.team) == ("2114", "Kinguyen", "Team A")
    assert (member.agents[0].title, member.rating, member.kills) == ("Raze", 1.42, 29)
    # The halftime "Side swap" spacer column must not shift or break the round timeline.
    assert [
        (round_.round_number, round_.round_score, round_.winner, round_.side) for round_ in result.data[0].rounds
    ] == [
        (1, "0-1", "team2", "attack"),
        (2, "1-1", "team1", "defense"),
        (13, "2-1", "", ""),
    ]
    assert [(stream.name, str(stream.url)) for stream in result.videos.streams] == [
        ("VCT", "https://www.youtube.com/@ValorantEsports/live"),
        ("VAL KR", "https://play.sooplive.co.kr/valorant"),
    ]
    assert [(vod.name, str(vod.url)) for vod in result.videos.vods] == [("Map 1", "https://youtu.be/abc123?t=132")]
    assert [(v.team, v.action, v.map) for v in result.veto] == [(None, "unknown", "Map ban: Bind, Haven")]
    assert [
        (v.team, v.action, v.map)
        for v in matches.parse_veto(["Paper Rex ban Bind", "FNC pick Lotus", "Sunset remains"])
    ] == [
        ("Paper Rex", "ban", "Bind"),
        ("FNC", "pick", "Lotus"),
        (None, "remains", "Sunset"),
    ]

    decider = (FIXTURE_DIR / "match_12345.html").read_text().replace("2 N/A", "2 Haven").encode()
    with patch("httpx2.AsyncClient.get", return_value=http_response("https://www.vlr.gg/12345", decider)):
        decider_result = await matches.match_by_id("12345", AsyncMock())

    assert [(item.number, item.map) for item in decider_result.data] == [(1, "Lotus"), (3, "Split")]

    upcoming = (
        (FIXTURE_DIR / "match_12345.html").read_text().replace("1 Lotus", "1 TBD").replace("3 Split", "3 TBD").encode()
    )
    with patch("httpx2.AsyncClient.get", return_value=http_response("https://www.vlr.gg/12345", upcoming)):
        upcoming_result = await matches.match_by_id("12345", AsyncMock())

    assert upcoming_result.data == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeline", "winners"),
    [
        ([(1, "1-0"), (2, "1-0"), (3, "1-1")], ["team1", "", "team2"]),
        ([(1, "1-0"), (2, "3-0"), (3, "3-1")], ["team1", "", "team2"]),
        ([(1, "1-0"), (3, "2-0"), (4, "2-1")], ["team1", "", "team2"]),
        ([(1, "1-0"), (1, "2-0"), (2, "2-1")], ["team1", "", "team2"]),
    ],
    ids=["duplicate-score", "score-jump", "missing-round", "duplicate-round"],
)
async def test_round_winners_require_a_consecutive_single_round_score_change(timeline, winners, http_response):
    soup = BeautifulSoup((FIXTURE_DIR / "match_12345.html").read_bytes(), "lxml")
    row = soup.select_one('.vm-stats-game[data-game-id="283202"] .vlr-rounds-row')
    for column in row.select(".vlr-rounds-row-col:not(.mod-teams)"):
        column.decompose()
    for number, score in timeline:
        column = soup.new_tag("div", attrs={"class": "vlr-rounds-row-col", "title": score})
        round_number = soup.new_tag("div", attrs={"class": "rnd-num"})
        round_number.string = str(number)
        column.append(round_number)
        row.append(column)
    response = http_response("https://www.vlr.gg/12345", str(soup).encode())

    with patch("httpx2.AsyncClient.get", return_value=response):
        result = await matches.match_by_id("12345", AsyncMock())

    assert [round_.round_number for round_ in result.data[0].rounds] == [number for number, _ in timeline]
    assert [round_.winner for round_ in result.data[0].rounds] == winners


def test_display_strings_follow_accept_language(monkeypatch, http_response):
    """Localized labels come from the device locale header; no header keeps the legacy English payload."""
    from fastapi import FastAPI
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.testclient import TestClient

    from app import i18n
    from app.api import deps
    from app.api.v1.endpoints.matches import router

    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=0)
    app.middleware("http")(i18n.localize_response)
    app.include_router(router, prefix="/api/v1/matches")
    store = {}
    redis = AsyncMock()
    redis.get.side_effect = store.get
    redis.set.side_effect = lambda key, value, ttl: store.__setitem__(key, value)
    app.dependency_overrides[deps.get_redis_client] = lambda: redis
    monkeypatch.setattr("app.cache.cache.settings.ENABLE_CACHE", True)

    response = http_response("https://www.vlr.gg/12345", (FIXTURE_DIR / "match_12345.html").read_bytes())
    with (
        patch("httpx2.AsyncClient.get", return_value=response) as vlr_get,
        patch("app.services.matches.get_team_data", AsyncMock(return_value=[])),
    ):
        client = TestClient(app)
        english = client.get("/api/v1/matches/12345")
        hindi = client.get("/api/v1/matches/12345", headers={"Accept-Language": "hi-IN,hi;q=0.9,en;q=0.8"})
        unknown = client.get("/api/v1/matches/12345", headers={"Accept-Language": "xx"})

    assert english.status_code == hindi.status_code == 200
    assert vlr_get.call_count == 1
    assert english.headers["content-language"] == "en" and hindi.headers["content-language"] == "hi"
    en_event, hi_event = english.json()["event"], hindi.json()["event"]
    assert (en_event["status"], en_event["status_label"]) == ("completed", "Completed")
    assert (hi_event["status"], hi_event["status_label"]) == ("completed", "समाप्त")
    assert (english.json()["veto"][0]["action_label"], hindi.json()["veto"][0]["action_label"]) == ("Unknown", "अज्ञात")
    assert unknown.json() == english.json() and unknown.headers["content-language"] == "en"

    assert i18n.resolve("pt") == "pt-BR" and i18n.resolve("de;q=0.5, ko;q=0.8") == "ko"
    assert i18n.resolve("de;q=0, ko") == "ko"
    assert i18n.resolve("de ;Q=0.1, ko;q=0.5") == "ko" and i18n.resolve("de;q=2, ko;q=1") == "ko"
    assert unknown.headers["vary"] == "Accept-Encoding, Accept-Language"


def test_unreachable_vlr_returns_503(monkeypatch):
    """A DNS failure or blocked IP is an upstream outage, so clients must see 503, not an unhandled 500."""
    import httpx2
    from fastapi.testclient import TestClient

    from app.core.config import settings

    monkeypatch.setattr(settings, "API_KEYS", {"test": "test-key"})
    from app.main import app

    with patch(
        "httpx2.AsyncClient.get", AsyncMock(side_effect=httpx2.ConnectError("[Errno -2] Name or service not known"))
    ):
        response = TestClient(app).get("/api/v1/matches/12345", headers={"Authorization": "Bearer test-key"})

    assert (response.status_code, response.json()) == (503, {"detail": "VLR.gg is unreachable"})


@pytest.mark.asyncio
async def test_match_list_keeps_each_upcoming_date_group(monkeypatch, http_response):
    responses = {
        constants.UPCOMING_MATCHES_URL: http_response(
            constants.UPCOMING_MATCHES_URL, (FIXTURE_DIR / "matches.html").read_bytes()
        ),
        constants.PAST_MATCHES_URL: http_response(
            constants.PAST_MATCHES_URL, (FIXTURE_DIR / "matches_results.html").read_bytes()
        ),
    }
    monkeypatch.setattr(matches.settings, "ENABLE_ID_MAPPING", False)

    with patch("httpx2.AsyncClient.get", side_effect=lambda url, *_args, **_kwargs: responses[url]):
        result = await matches.match_list(AsyncMock())

    names = {match.id: (match.team1.name, match.team2.name) for match in result}
    assert names["684611"] == ("FULL SENSE", "FUT Esports")
    assert names["684612"] == ("LEVIATÁN", "Global Esports")
    assert names["684610"] == ("Team Vitality", "Dragon Ranger Gaming")


def test_match_list_shows_the_broadcast_map_on_its_live_match(monkeypatch):
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app import schemas
    from app.api import deps
    from app.api.v1.endpoints.matches import router
    from app.schemas.matches import Event, MatchData, MatchVideos, MatchWithDetails, Team, TeamWithImage, VideoScore

    detail = MatchWithDetails(
        teams=[
            TeamWithImage(id="1", name="Alpha", tag="ALP", score=0, img="https://cdn.vlr.gg/a.png"),
            TeamWithImage(id="2", name="Beta", tag="BET", score=1, img="https://cdn.vlr.gg/b.png"),
        ],
        bans=[],
        event=Event(id="99", img="https://cdn.vlr.gg/e.png", series="Series", stage="Playoffs", status="live"),
        videos=MatchVideos(streams=[], vods=[]),
        map_count=2,
        total_maps=3,
        data=[
            MatchData(
                number=1,
                map="Ascent",
                teams=[Team(name="Beta", score=13), Team(name="Alpha", score=9)],
                members=[],
                rounds=[],
                winner="Beta",
            ),
            MatchData(
                number=2,
                map="Bind",
                teams=[Team(name="Alpha", score=3), Team(name="Beta", score=4)],
                members=[],
                rounds=[],
                live=True,
            ),
        ],
        previous_encounters=[],
    )
    listed = [
        # VLR lists Beta first here, so the map score must follow the listing's team order, not the details'.
        schemas.Match(
            id="123",
            team1=schemas.MatchTeam(name="Beta", id="2", score=1),
            team2=schemas.MatchTeam(name="Alpha", id="1", score=0),
            status="live",
            time="2026-10-16T10:00:00Z",
            event="Event",
            series="Series",
            event_id="99",
        ),
        schemas.Match(
            id="456",
            team1=schemas.MatchTeam(name="Alpha", id="1"),
            team2=schemas.MatchTeam(name="Gamma", id="3"),
            status="upcoming",
            time="2026-10-17T10:00:00Z",
            event="Event",
            series="Series",
            event_id="99",
        ),
    ]

    def video(status, observed_at):
        return VideoScore.model_validate(
            {
                "status": status,
                "observed_at": observed_at,
                "map_number": 2,
                "teams": [{"code": "ALP", "name": "Alpha", "score": 5}, {"code": "BET", "name": "Beta", "score": 7}],
            }
        ).model_dump_json()

    stored = {
        "matches": schemas.MatchListAdapter.dump_json(listed),
        constants.PUSH_DETAILS_KEY: json.dumps({"123": detail.model_dump(mode="json")}),
        constants.VIDEO_SCORE_KEY: video("ok", int(time.time())),
    }
    # store_score writes a map's round history once a read of that map is verified for the match.
    verified = {constants.VIDEO_ROUNDS_KEY.format("123", 2)}
    redis_down = False

    def exists(key):
        if redis_down:
            raise RedisConnectionError("Redis is down")
        return int(key in verified)

    redis = AsyncMock()
    redis.get.side_effect = stored.get
    redis.exists.side_effect = exists
    monkeypatch.setattr("app.cache.cache.settings.ENABLE_CACHE", True)
    monkeypatch.setattr(matches.settings, "ENABLE_LIVE_PUSH", True)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/matches")
    app.dependency_overrides[deps.get_redis_client] = lambda: redis

    with TestClient(app) as client:
        fresh = client.get("/api/v1/matches/").json()
        # During a pause or a break the tracker re-sends the last score it read with an error status.
        stored[constants.VIDEO_SCORE_KEY] = video("error", int(time.time()) - constants.VIDEO_STALE_SECONDS - 1)
        paused = client.get("/api/v1/matches/").json()
        # Another live match keeps the lookup running, so only the ended match's own status hides its map.
        stored["matches"] = schemas.MatchListAdapter.dump_json(
            [
                listed[0].model_copy(update={"status": MatchStatus.COMPLETED}),
                listed[1].model_copy(update={"status": MatchStatus.LIVE}),
            ]
        )
        ended = client.get("/api/v1/matches/").json()
        # The same teams' earlier series left its last score, but no read was verified for this match.
        stored["matches"] = schemas.MatchListAdapter.dump_json(listed)
        rounds_key = verified.pop()
        rematch = client.get("/api/v1/matches/").json()
        verified.add(rounds_key)
        redis_down = True
        during_outage = client.get("/api/v1/matches/")

    bind = {"name": "Bind", "number": 2, "scores": [7, 5]}
    assert [match["current_map"] for match in fresh] == [bind, None]
    assert [match["team1"]["score"] for match in fresh] == [1, None]
    # The last known score stays while the match is live, however long the tracker has not read.
    assert [match["current_map"] for match in paused] == [bind, None]
    assert [match["current_map"] for match in ended] == [None, None]
    assert [match["current_map"] for match in rematch] == [None, None]
    # The map score is an extra: a Redis outage drops it instead of failing the list.
    assert during_outage.status_code == 200
    assert [match["current_map"] for match in during_outage.json()] == [None, None]


@pytest.mark.asyncio
async def test_completed_matches_clamp_pages_and_keep_results_in_order(monkeypatch, http_get):
    monkeypatch.setattr(matches.settings, "ENABLE_ID_MAPPING", False)
    pages = {
        constants.PAST_MATCHES_URL: (FIXTURE_DIR / "matches_results_page1.html").read_bytes(),
        matches.completed_matches_url(2): (FIXTURE_DIR / "matches_results_page2.html").read_bytes(),
    }

    with patch("httpx2.AsyncClient.get", side_effect=http_get(pages)) as get:
        result = await matches.get_completed_matches(AsyncMock(), pages=9999)

    assert len(result) == 100
    assert len({match.id for match in result}) == 100
    assert result[0].id == "670476"
    assert result[50].id == "684615"
    assert get.call_count <= constants.MAX_PAGINATION_PAGES


@pytest.mark.asyncio
async def test_completed_matches_do_not_return_partial_results(monkeypatch, http_get):
    monkeypatch.setattr(matches.settings, "ENABLE_ID_MAPPING", False)
    pages = {constants.PAST_MATCHES_URL: (FIXTURE_DIR / "matches_results_page1.html").read_bytes()}
    failures = {matches.completed_matches_url(2): 502}

    with patch("httpx2.AsyncClient.get", side_effect=http_get(pages, failures=failures)), pytest.raises(ScrapingError):
        await matches.get_completed_matches(AsyncMock(), pages=2)


def test_live_update_api_stores_token_and_favorites(monkeypatch, make_database):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.api import deps
    from app.api.v1.endpoints.live_updates import router
    from app.core import connections
    from app.db.migrations import upgrade_to_head

    database_url = make_database()
    asyncio.run(upgrade_to_head(database_url))
    # TestClient and asyncio.run each use a fresh event loop, and asyncpg connections cannot outlive theirs.
    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(connections, "subscription_sessions", sessions)
    monkeypatch.setattr(deps.settings, "ENABLE_LIVE_PUSH", True)
    monkeypatch.setattr(deps.settings, "API_KEYS", {"test": "secret"})

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/live-updates")
    redis = AsyncMock()
    redis.get.return_value = None
    app.dependency_overrides[deps.get_redis_client] = lambda: redis
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}
    base = "/api/v1/live-updates/clients/11111111-1111-4111-8111-111111111111"

    try:
        assert client.get(f"{base}/favorites", headers=headers).status_code == 404
        token_response = client.put(f"{base}/token", headers=headers, json={"token": "AABB"})
        assert token_response.status_code == 204
        assert token_response.headers["Cache-Control"] == "no-store"
        fcm_token = {"token": "dGVzdA_x-1:APA91bH", "platform": "android"}
        assert client.put(f"{base}/token", headers=headers, json=fcm_token).status_code == 204

        from app.db.models import Client

        async def live_updates_setting():
            async with sessions() as session:
                return (await session.get(Client, "11111111-1111-4111-8111-111111111111")).live_updates

        assert asyncio.run(live_updates_setting()) is None
        assert (
            client.put(f"{base}/token", headers=headers, json={**fcm_token, "live_updates": False}).status_code == 204
        )
        assert client.put(f"{base}/token", headers=headers, json=fcm_token).status_code == 204
        assert asyncio.run(live_updates_setting()) is False
        with patch("app.services.push.deliver_instant_start", AsyncMock()) as instant_start:
            turned_off = client.post(f"{base}/matches/123/live-activity", headers=headers)
        assert turned_off.status_code == 409
        instant_start.assert_not_awaited()
        assert client.put(f"{base}/token", headers=headers, json={"token": fcm_token["token"]}).status_code == 422
        assert (
            client.put(f"{base}/token", headers=headers, json={"token": "AABB", "live_updates": True}).status_code
            == 204
        )
        assert (
            client.put(
                f"{base}/favorites",
                headers=headers,
                json={"teams": ["1"], "matches": ["2"], "players": ["3"], "events": ["4", "112053399716844250"]},
            ).status_code
            == 204
        )
        favorites_response = client.get(f"{base}/favorites", headers=headers)
        assert favorites_response.headers["Cache-Control"] == "no-store"
        assert favorites_response.json() == {
            "teams": ["1"],
            "matches": ["2"],
            "players": ["3"],
            "events": ["4", "112053399716844250"],
        }
        assert client.put(f"{base}/favorites", headers=headers, json={"teams": ["5", "1"]}).status_code == 204
        # A zero-padded ID would overflow VARCHAR(10) in PostgreSQL and 500 instead of 422.
        assert client.put(f"{base}/favorites", headers=headers, json={"teams": ["00000000001"]}).status_code == 422
        assert client.get(f"{base}/favorites", headers=headers).json()["teams"] == ["1", "5"]
        removal = {"teams": ["1"], "matches": ["2"], "events": ["9"]}
        assert client.request("DELETE", f"{base}/favorites", headers=headers, json=removal).status_code == 204
        assert client.get(f"{base}/favorites", headers=headers).json() == {
            "teams": ["5"],
            "matches": [],
            "players": ["3"],
            "events": ["4", "112053399716844250"],
        }
        unknown = "/api/v1/live-updates/clients/33333333-3333-4333-8333-333333333333/favorites"
        assert client.request("DELETE", unknown, headers=headers, json=removal).status_code == 404

        mock_apns = AsyncMock()
        mock_apns.create_channel.return_value = "channel-live-123"
        monkeypatch.setattr(connections, "apns_client", mock_apns)

        from app.schemas.matches import Event, MatchData, MatchVideos, MatchWithDetails, Round, Team, TeamWithImage

        live_detail = MatchWithDetails(
            teams=[
                TeamWithImage(id="1", name="Alpha", tag="ALP", score=1, img="https://cdn.vlr.gg/a.png"),
                TeamWithImage(id="2", name="Beta", tag="BET", score=0, img="https://cdn.vlr.gg/b.png"),
            ],
            bans=[],
            event=Event(
                id="99",
                img="https://cdn.vlr.gg/e.png",
                series="Series",
                stage="Playoffs: Grand Final",
                status="live",
            ),
            videos=MatchVideos(streams=[], vods=[]),
            map_count=1,
            total_maps=3,
            data=[
                MatchData(
                    number=1,
                    map="Ascent",
                    teams=[Team(name="Beta", score=14), Team(name="Alpha", score=12)],
                    members=[],
                    rounds=[
                        Round(round_number=1, round_score="1-0", winner="team1", side="attack", win_type="Elimination"),
                        Round(
                            round_number=2, round_score="1-1", winner="team2", side="defense", win_type="Elimination"
                        ),
                        Round(round_number=3, round_score="2-1", winner="team1", side="attack", win_type="Elimination"),
                    ],
                    winner="Beta",
                ),
                MatchData(
                    number=2,
                    map="Bind",
                    teams=[Team(name="Alpha", score=12), Team(name="Beta", score=11)],
                    members=[],
                    rounds=[
                        Round(round_number=1, round_score="0-1", winner="team2", side="defense", win_type="Elimination")
                    ],
                    live=True,
                ),
            ],
            previous_encounters=[],
        )
        completed_detail = live_detail.model_copy(
            update={"event": live_detail.event.model_copy(update={"status": "completed"})}
        )
        assert live_detail.model_dump()["current_map"] == {"name": "Bind", "scores": [12, 11], "number": 2}
        assert completed_detail.model_dump()["current_map"] is None

        with patch("app.services.matches.match_by_id", AsyncMock(return_value=live_detail)):
            start_response = client.post(f"{base}/matches/123/live-activity", headers=headers)
            assert start_response.status_code == 204
            assert start_response.headers["Cache-Control"] == "no-store"
            mock_apns.create_channel.assert_awaited_once()
            mock_apns.send_start.assert_awaited_once()
            token_arg, channel_arg, state_arg = mock_apns.send_start.call_args[0]
            assert (token_arg, channel_arg) == ("aabb", "channel-live-123")
            assert (state_arg.total_maps, state_arg.current_map.number) == (3, 2)

            retrigger = client.post(f"{base}/matches/123/live-activity", headers=headers)
            assert retrigger.status_code == 204
            assert mock_apns.create_channel.await_count == 1
            assert mock_apns.send_start.await_count == 2

        with patch("app.services.matches.match_by_id", AsyncMock(return_value=completed_detail)):
            assert client.post(f"{base}/matches/456/live-activity", headers=headers).status_code == 400

        from app.services.apns import APNsError
        from app.services.subscription_store import SubscriptionStore

        mock_apns.send_start.side_effect = APNsError(410, "Unregistered")
        with patch("app.services.matches.match_by_id", AsyncMock(return_value=live_detail)):
            failed_ios = client.post(f"{base}/matches/789/live-activity", headers=headers)
        assert (failed_ios.status_code, failed_ios.json()) == (503, {"detail": "APNs start failed: Unregistered"})
        assert client.post(f"{base}/matches/123/live-activity", headers=headers).status_code == 404

        async def saved_channel():
            async with sessions() as session:
                return await SubscriptionStore(session).get_match("789")

        assert asyncio.run(saved_channel()).channel_id == "channel-live-123"

        unknown = "/api/v1/live-updates/clients/99999999-9999-4999-8999-999999999999"
        assert client.post(f"{unknown}/matches/123/live-activity", headers=headers).status_code == 404

        from datetime import timedelta

        from firebase_admin import messaging

        from app.services.push import Routing

        android_client = "/api/v1/live-updates/clients/22222222-2222-4222-8222-222222222222"
        assert (
            client.put(
                f"{android_client}/token", headers=headers, json={"token": "tok:123", "platform": "android"}
            ).status_code
            == 204
        )
        fcm_app = MagicMock()
        send_fcm = AsyncMock(return_value=messaging.BatchResponse([messaging.SendResponse({"name": "sent"}, None)]))
        with (
            patch("app.services.matches.match_by_id", AsyncMock(return_value=live_detail)),
            patch("app.core.config.settings.GOOGLE_APPLICATION_CREDENTIALS", ""),
        ):
            unavailable = client.post(f"{android_client}/matches/123/live-activity", headers=headers)
            assert (unavailable.status_code, unavailable.json()) == (503, {"detail": "FCM client is not configured"})

        with (
            patch("app.services.matches.match_by_id", AsyncMock(return_value=live_detail)),
            patch("app.core.config.settings.GOOGLE_APPLICATION_CREDENTIALS", "/fake/creds.json"),
            patch("app.services.fcm.get_app", return_value=fcm_app),
            patch("firebase_admin.messaging.send_each_async", send_fcm),
        ):
            android_resp = client.post(f"{android_client}/matches/123/live-activity", headers=headers)
            assert android_resp.status_code == 204
            send_fcm.assert_awaited_once()
            message = send_fcm.call_args.kwargs["messages"][0]
            assert send_fcm.call_args.kwargs == {"messages": [message], "dry_run": False, "app": fcm_app}
            assert message.token == "tok:123"
            assert message.notification is None and message.condition is None
            assert message.android.collapse_key == "match-123"
            assert message.android.priority == "high" and message.android.ttl == timedelta(seconds=120)
            assert message.data["type"] == "match-live-v1"
            state_data = json.loads(message.data["state"])
            assert (state_data["match_id"], state_data["total_maps"], state_data["current_map"]["number"]) == (
                "123",
                3,
                2,
            )
            assert state_data["map_winners"] == ["2", None, None]
            assert state_data["map_round_winners"] == [
                {"map_number": 1, "winners": [1, 0, 1], "scores": [12, 14]},
                {"map_number": 2, "winners": [1], "scores": [12, 11]},
                {"map_number": 3, "winners": [], "scores": []},
            ]
            # Map 1's confirmed win floors Beta's series score, which the header lags at 0.
            assert [(team["id"], team["score"]) for team in state_data["teams"]] == [("1", 1), ("2", 1)]
            assert state_data["stage"] == "Playoffs: Grand Final"

            # A match favorited under its Riot id, before VLR listed it, is routed by its VLR id.
            async def link_series():
                from app.db.models import RiotWebMatch

                async with sessions.begin() as session:
                    session.add(
                        RiotWebMatch(
                            riot_id="115576361461208999",
                            start_time=datetime.now(UTC),
                            league_name="Champions",
                            teams=[{"name": "Alpha"}, {"name": "Beta"}],
                            vlr_match_id="123",
                        )
                    )

            asyncio.run(link_series())
            favorites = {"matches": ["115576361461208999"]}
            assert client.put(f"{android_client}/favorites", headers=headers, json=favorites).status_code == 204
            assert client.get(f"{android_client}/favorites", headers=headers).json()["matches"] == favorites["matches"]
            assert client.put(f"{android_client}/token", headers=headers, json={"token": "CCDD"}).status_code == 204

            async def pending_ios_starts():
                async with sessions() as session:
                    return await SubscriptionStore(session).pending_starts(Routing("123", None, [], []))

            assert asyncio.run(pending_ios_starts()) == [("22222222-2222-4222-8222-222222222222", "ccdd")]

            assert (
                client.put(
                    f"{android_client}/token", headers=headers, json={"token": "tok:123", "platform": "android"}
                ).status_code
                == 204
            )

            send_fcm.return_value = messaging.BatchResponse(
                [messaging.SendResponse(None, messaging.UnregisteredError("registration token expired"))]
            )
            failed = client.post(f"{android_client}/matches/123/live-activity", headers=headers)
            assert (failed.status_code, failed.json()) == (503, {"detail": "FCM start failed"})
            assert client.post(f"{android_client}/matches/123/live-activity", headers=headers).status_code == 404

            # Once the match has the row that makes its Riot id the one clients know, iOS is sent that
            # id. Android is still sent the VLR id, the only one released Android apps accept.
            async def keep_riot_id():
                from app.db.models import EventRecord, MatchRecord

                async with sessions.begin() as session:
                    session.add(EventRecord(id="77", name="Event", circuit="other"))
                    await session.flush()
                    session.add(
                        MatchRecord(
                            id="115576361461208999",
                            riot_id="115576361461208999",
                            event_id="77",
                            played_on=datetime.now(UTC).date(),
                            source="riot_web",
                            ingested_at=0,
                        )
                    )

            asyncio.run(keep_riot_id())
            android_token = {"token": "tok:123", "platform": "android"}
            assert client.put(f"{android_client}/token", headers=headers, json=android_token).status_code == 204
            send_fcm.reset_mock()
            send_fcm.return_value = messaging.BatchResponse([messaging.SendResponse({"name": "sent"}, None)])
            riot_start = f"{android_client}/matches/115576361461208999/live-activity"
            assert client.post(riot_start, headers=headers).status_code == 204
            assert '"match_id":"123"' in send_fcm.call_args.kwargs["messages"][0].data["state"]
            assert client.put(f"{android_client}/token", headers=headers, json={"token": "CCDD"}).status_code == 204
            mock_apns.send_start.side_effect = None
            assert client.post(riot_start, headers=headers).status_code == 204
            assert mock_apns.send_start.call_args.args[2].match_id == "115576361461208999"

        assert client.delete(f"{base}/token", headers=headers).status_code == 204
    finally:
        asyncio.run(engine.dispose())


RIOT_WEB_VODS_FIXTURE = FIXTURE_DIR / "riot_web" / "eventdetails_champions2026_lowerfinal.json"


def _vlr_details(numbers, scores):
    """Build the VLR details of the Champions lower final with the given played maps and series score."""
    from app import schemas

    return schemas.MatchWithDetails.model_validate(
        {
            "teams": [
                {
                    "name": "100 Thieves",
                    "img": "https://cdn.test/100t.png",
                    "score": scores[0],
                    "id": "105748037960121143",
                },
                {
                    "name": "Team Vitality",
                    "img": "https://cdn.test/vit.png",
                    "score": scores[1],
                    "id": "106013381894847385",
                },
            ],
            "bans": [],
            "event": {
                "id": "2283",
                "img": "https://cdn.test/champions.png",
                "series": "Champions 2026",
                "stage": "Lower Final",
            },
            "videos": {"streams": [], "vods": [{"name": "Map 1", "url": "https://youtu.be/MZjaOtjtRlU?t=1"}]},
            "map_count": len(numbers),
            "total_maps": 5,
            "data": [
                {
                    "number": number,
                    "map": f"Map {number}",
                    "teams": [
                        {"name": "100 Thieves", "score": 13},
                        {"name": "Team Vitality", "score": 9},
                    ],
                    "members": [],
                    "rounds": [],
                    "winner": "100 Thieves",
                }
                for number in numbers
            ],
            "previous_encounters": [],
        }
    )


@pytest.mark.asyncio
async def test_match_vods_start_each_vlr_map_at_its_riot_offset(monkeypatch, make_database):
    """Stored Riot offsets ride along with VLR's own VODs, ordered and only when the maps agree."""
    import httpx2
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.endpoints.matches import get_match_by_id
    from app.constants import VodProvider
    from app.core import connections
    from app.core.config import settings
    from app.cron import riot_web as riot_web_cron
    from app.db.migrations import upgrade_to_head
    from app.db.models import RiotWebMatch
    from app.schemas.riot_web import StoredDetails
    from app.services import match_vods, riot_web_store

    database_url = make_database()
    await upgrade_to_head(database_url)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(connections, "subscription_sessions", sessions)
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", "test-key")

    now = datetime.now(UTC)
    async with sessions.begin() as session:
        session.add(
            RiotWebMatch(
                riot_id="115576361461208325",
                start_time=now,
                league_name="Champions",
                teams=[{"name": "100 Thieves"}, {"name": "Team Vitality"}],
                vlr_match_id="12345",
            )
        )
        # A linked series that has not started cannot have games yet, so it is not fetched.
        session.add(
            RiotWebMatch(
                riot_id="115576361461208326",
                start_time=now + timedelta(days=7),
                league_name="Champions",
                teams=[{"name": "100 Thieves"}, {"name": "Team Vitality"}],
                vlr_match_id="12346",
            )
        )

    payload = json.loads(RIOT_WEB_VODS_FIXTURE.read_text())
    games = payload["data"]["event"]["match"]["games"]
    # English moved last, and an id that is not a YouTube id, must both sort/filter out.
    games[0]["vods"].append(games[0]["vods"].pop(0))
    games[0]["vods"].insert(0, {"parameter": "x&y=1", "locale": "en-US", "provider": "youtube", "startMillis": 1000})
    # A Twitch VOD listed first still sorts behind YouTube within English.
    games[1]["vods"].insert(
        0, {"parameter": "2896791742", "locale": "en-US", "provider": "twitch", "startMillis": 2685000}
    )
    # A completed map can legitimately have no VOD at all.
    games[2]["vods"] = []

    requested = []

    async def router(client, url, *, params=None, **_kwargs):
        request = client.build_request("GET", url, params=params)
        requested.append(request)
        return httpx2.Response(200, json=payload, request=request)

    with patch("httpx2.AsyncClient.get", autospec=True, side_effect=router):
        await riot_web_cron.riot_web_details_cron({"redis": AsyncMock()})

    assert [
        (
            request.url.path,
            request.url.params.get("id"),
            request.url.params.get("hl"),
            request.url.params.get("sport"),
            request.headers["x-api-key"],
        )
        for request in requested
    ] == [("/persisted/val/getEventDetails", "115576361461208325", "en-US", "val", "test-key")]

    async with sessions() as session:
        row = await session.scalar(select(RiotWebMatch).where(RiotWebMatch.riot_id == "115576361461208325"))
        stored = StoredDetails.model_validate(row.details)
        tournament_id, fetched_at = row.tournament_id, row.details_fetched_at
    assert (tournament_id, fetched_at is not None) == ("115576361459045501", True)
    assert stored.game_wins == {"100 Thieves": 1, "Team Vitality": 3}
    assert [(game.number, game.state, len(game.vods)) for game in stored.games] == [
        (1, "completed", 10),
        (2, "completed", 10),
        (3, "completed", 0),
        (4, "completed", 9),
        (5, "unneeded", 0),
    ]

    details = _vlr_details([1, 2, 3, 4], [1, 3])
    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)):
        response = await get_match_by_id("12345", AsyncMock())

    assert [(item.map_number, [link.locale for link in item.links]) for item in response.map_vods] == [
        (1, ["en-US", "es-MX", "fr-FR", "id-ID", "ja-JP", "pt-BR", "th-TH", "tr-TR", "vi-VN"]),
        (2, ["en-US", "en-US", "es-MX", "fr-FR", "id-ID", "ja-JP", "pt-BR", "th-TH", "tr-TR", "vi-VN"]),
        (4, ["en-US", "es-MX", "fr-FR", "id-ID", "ja-JP", "pt-BR", "th-TH", "tr-TR", "vi-VN"]),
    ]
    assert {link.provider for link in response.map_vods[0].links} == {VodProvider.YOUTUBE}
    assert response.model_dump(mode="json")["map_vods"][0]["links"][0] == {
        "provider": "youtube",
        "locale": "en-US",
        "start_seconds": 1,
        "url": "https://www.youtube.com/watch?v=MZjaOtjtRlU&t=1s",
    }
    assert [str(link.url) for link in response.map_vods[1].links[:2]] == [
        "https://www.youtube.com/watch?v=MZjaOtjtRlU&t=2685s",
        "https://www.twitch.tv/videos/2896791742?t=0h44m45s",
    ]
    # Riot is an overlay: VLR's own video list is untouched.
    assert [str(vod.url) for vod in response.videos.vods] == ["https://youtu.be/MZjaOtjtRlU?t=1"]

    # A later fetch that answers event: null, or has no readable games, must not wipe the stored offsets.
    async with sessions.begin() as session:
        await riot_web_store.save_details(session, "115576361461208325", None, datetime.now(UTC))
        await riot_web_store.save_details(
            session,
            "115576361461208325",
            StoredDetails(tournament_id="115576361459045502"),
            datetime.now(UTC),
        )
    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)):
        preserved = await get_match_by_id("12345", AsyncMock())
    assert preserved.map_vods == response.map_vods
    async with sessions() as session:
        tournament_id = await session.scalar(
            select(RiotWebMatch.tournament_id).where(RiotWebMatch.riot_id == "115576361461208325")
        )
    # The games list is empty, but the tournament id it names is still recorded.
    assert tournament_id == "115576361459045502"

    # A refresh that lost a game to an unreadable entry keeps the fuller stored series, or every map's VODs go.
    async with sessions.begin() as session:
        full = await riot_web_store.details_for_vlr(session, "12345")
        assert full is not None
        await riot_web_store.save_details(
            session, "115576361461208325", full.model_copy(update={"games": full.games[1:]}), datetime.now(UTC)
        )
    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)):
        assert (await get_match_by_id("12345", AsyncMock())).map_vods == response.map_vods

    # The numbers are Riot's only map key, so any disagreement with VLR drops every offset.
    assert await match_vods.map_vods("12345", _vlr_details([1, 2, 3], [1, 3])) == []
    assert await match_vods.map_vods("12345", _vlr_details([1, 2, 3, 4], [2, 2])) == []
    # A reversed series score is a different result even though the multiset of wins matches.
    assert await match_vods.map_vods("12345", _vlr_details([1, 2, 3, 4], [3, 1])) == []

    # The route takes any of a match's ids. A linked Riot series id serves the same VLR body.
    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)) as cached:
        by_riot_id = await get_match_by_id("115576361461208325", AsyncMock())
    assert by_riot_id.model_dump(mode="json") == preserved.model_dump(mode="json")
    cached.assert_awaited_once_with("12345", ANY)

    from fastapi import HTTPException

    from app.db.models import EventRecord, MatchRecord, Team

    riot_only = "115576361461208400"
    async with sessions.begin() as session:
        session.add(EventRecord(id="2283", name="Champions 2026", circuit="vct"))
        session.add(
            Team(id="7", source="vlr", riot_name="Alpha", logo="", riot_logo=None, first_seen_at=0, last_fetched_at=0)
        )
        session.add(
            RiotWebMatch(
                riot_id=riot_only,
                start_time=now - timedelta(hours=2),
                league_name="Champions",
                best_of=3,
                teams=[{"name": "Alpha"}, {"name": "Beta"}],
                details=StoredDetails(
                    tournament_id="1",
                    game_wins={"Alpha": 2, "Beta": 0},
                    games=[
                        {
                            "number": 1,
                            "state": "completed",
                            "vods": [
                                {"provider": "youtube", "locale": "en-US", "parameter": "MZjaOtjtRlU", "start_ms": 5000}
                            ],
                        },
                        {"number": 2, "state": "completed"},
                        {"number": 3, "state": "unneeded"},
                    ],
                ).model_dump(mode="json"),
            )
        )
        await session.flush()
        for match_id, vlr_id, riot_id in (
            (riot_only, None, riot_only),
            ("12345", "12345", None),
        ):
            session.add(
                MatchRecord(
                    id=match_id,
                    vlr_id=vlr_id,
                    riot_id=riot_id,
                    event_id="2283",
                    stage="Grand Final",
                    played_on=now.date(),
                    source="riot_web",
                    ingested_at=0,
                )
            )
    with patch("app.services.matches.cached_match_by_id", AsyncMock()) as cached:
        body = await get_match_by_id(riot_only, AsyncMock())
        with pytest.raises(HTTPException) as unknown:
            await get_match_by_id("115576361461208499", AsyncMock())
    cached.assert_not_awaited()
    assert unknown.value.status_code == 404
    assert [(team.name, team.score, team.id) for team in body.teams] == [("Alpha", 2, "7"), ("Beta", 0, None)]
    # A mirrored team whose own logo columns are unusable, and an unmirrored one, show the placeholder.
    assert {str(team.img) for team in body.teams} == {RIOT_PLACEHOLDER_IMG}
    assert (body.event.id, body.event.series, body.event.stage, body.event.status) == (
        "2283",
        "Champions",
        "Grand Final",
        "completed",
    )
    assert (body.total_maps, body.map_count, body.data, body.insights) == (3, 2, [], None)
    assert [(item.map_number, len(item.links)) for item in body.map_vods] == [(1, 1)]

    # An unledgered series names the Riot-created event of its tournament.
    async with sessions.begin() as session:
        session.add(
            RiotWebMatch(
                riot_id="115576361461208500",
                start_time=now + timedelta(hours=2),
                league_name="Champions",
                teams=[{"name": "Alpha"}, {"name": "Beta"}],
                tournament_id="115576361459045501",
            )
        )
    with patch("app.services.matches.cached_match_by_id", AsyncMock()):
        by_riot_id = await get_match_by_id("115576361461208500", AsyncMock())
    assert by_riot_id.event.id == "115576361459045501"


@pytest.mark.asyncio
async def test_map_vods_are_absent_without_stored_riot_data(monkeypatch, make_database):
    """With no fetched series, or the key unset, the match response is what VLR alone returns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.endpoints.matches import get_match_by_id
    from app.core import connections
    from app.core.config import settings
    from app.db.migrations import upgrade_to_head
    from app.services import match_vods

    database_url = make_database()
    await upgrade_to_head(database_url)
    engine = create_async_engine(database_url)
    monkeypatch.setattr(connections, "subscription_sessions", async_sessionmaker(engine, expire_on_commit=False))

    details = _vlr_details([1, 2, 3, 4], [1, 3])
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", "test-key")
    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)):
        response = await get_match_by_id("12345", AsyncMock())
    assert response.map_vods == []

    # With the key unset, a cache hit must not cost a database round trip.
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", None)
    opened = []

    def open_session():
        opened.append(True)
        raise RuntimeError("no database session may be opened while RIOT_WEB_API_KEY is unset")

    monkeypatch.setattr(connections, "subscription_sessions", open_session)
    assert await match_vods.map_vods("12345", details) == []
    assert opened == []

    with patch("app.services.matches.cached_match_by_id", AsyncMock(return_value=details)):
        disabled = await get_match_by_id("12345", AsyncMock())

    assert disabled.model_dump(mode="json") == response.model_dump(mode="json")
    assert [(team.name, team.score) for team in disabled.teams] == [("100 Thieves", 1), ("Team Vitality", 3)]
    assert [str(vod.url) for vod in disabled.videos.vods] == ["https://youtu.be/MZjaOtjtRlU?t=1"]
    assert [item.number for item in disabled.data] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_match_extra_serves_linked_riot_games_by_vlr_team(monkeypatch, make_database):
    """Riot's stored game for a VLR map comes back with VLR's team slots, player names and radar locations."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.endpoints.matches import get_match_extra
    from app.core import connections
    from app.db.migrations import upgrade_to_head
    from app.db.models import EventRecord, MatchRecord, RiotMatch
    from app.schemas.riot import MatchExtra

    database_url = make_database()
    await upgrade_to_head(database_url)
    sessions = async_sessionmaker(create_async_engine(database_url), expire_on_commit=False)
    monkeypatch.setattr(connections, "subscription_sessions", sessions)
    # A real Riot payload, trimmed to two rounds; Riot's Red side is VLR's team2 on this map.
    data = json.loads((FIXTURE_DIR / "riot" / "game_754737_map3.json").read_text())
    link = {"red_team": "team2", "blue_team": "team1", "same_game": True, "confidence": 0.99, "reason": "rosters"}
    started = datetime(2026, 10, 18, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            RiotMatch(riot_id="linked", vlr_match_id="754737", map_number=3, started_at=started, data=data, link=link)
        )
        session.add(RiotMatch(riot_id="unlinked", started_at=started, data=data))
        session.add(EventRecord(id="1", name="E", circuit="vct"))
        await session.flush()
        session.add(
            MatchRecord(
                id="115576361461208777",
                vlr_id="754737",
                event_id="1",
                played_on=started.date(),
                source="vlr",
                ingested_at=0,
            )
        )

    async with sessions() as session:
        extra = (await get_match_extra("754737", session)).model_dump(mode="json")
        assert await get_match_extra("1", session) == MatchExtra(maps=[])
        # A row Riot created and VLR adopted keeps its Riot id, which opens the same games.
        by_own_id = (await get_match_extra("115576361461208777", session)).model_dump(mode="json")
        assert by_own_id == extra
    [game] = extra["maps"]
    assert {key: game[key] for key in ("map_number", "map", "duration_millis")} == {
        "map_number": 3,
        "map": "Sunset",
        "duration_millis": 2318352,
    }
    # Observers are not players, and each side is named by the VLR slot the link gave it.
    assert len(game["players"]) == 10
    derke = next(p for p in game["players"] if p["name"] == "VIT Derke")
    assert (derke["team"], derke["kills"], derke["deaths"], derke["assists"]) == ("team2", 14, 12, 2)
    first = game["rounds"][0]
    assert (first["number"], first["winner"], first["plant_site"], first["planter"]) == (1, "team2", "B", "VIT PROFEK")
    # Kills are in the order they happened, by name.
    assert [(k["killer"], k["victim"]) for k in first["kills"][:2]] == [
        ("LOUD tkzin", "VIT Jamppi"),
        ("LOUD DaviH", "VIT Chronicle"),
    ]
    assert [k["time_millis"] for k in first["kills"]] == sorted(k["time_millis"] for k in first["kills"])
    derke = next(p for p in first["players"] if p["name"] == "VIT Derke")
    assert (derke["damage"], derke["loadout_value"], derke["armor_id"]) == (324, 600, None)
    assert game["radar_url"] == "https://files.akhilnarang.dev/cdn/valorant/sunset-radar.webp"
    # The opening kill: everyone alive stands on the radar, the victim apart, and the killer faces the victim.
    opening = first["kills"][0]
    assert opening["victim_location"] == {"x": 0.164, "y": 0.476}
    standing = {p["name"]: p for p in opening["player_locations"]}
    assert len(standing) == 9 and opening["victim"] not in standing
    assert all(0 <= p["location"]["x"] <= 1 and 0 <= p["location"]["y"] <= 1 for p in standing.values())
    killer = standing[opening["killer"]]
    towards = math.atan2(
        opening["victim_location"]["y"] - killer["location"]["y"],
        opening["victim_location"]["x"] - killer["location"]["x"],
    )
    assert killer["facing"] == pytest.approx(towards, abs=0.02)
    # Riot's default ceremony is no highlight; the planter stands on the plant spot, and no defuse happened.
    assert first["ceremony"] is None
    assert (first["plant"]["time_millis"], first["plant"]["location"]) == (43877, {"x": 0.06, "y": 0.385})
    planter = next(p for p in first["plant"]["player_locations"] if p["name"] == first["planter"])
    assert planter["location"] == first["plant"]["location"]
    assert first["defuse"] is None


@pytest.mark.asyncio
async def test_match_list_adds_unlisted_riot_series_in_order_without_duplicating_vlr(monkeypatch, make_database):
    """A Riot series VLR does not list appears in its place; one VLR lists, or that is far off, does not."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import schemas
    from app.core import connections
    from app.core.config import settings
    from app.db.migrations import upgrade_to_head
    from app.db.models import RiotWebMatch, Team
    from app.services import ranking_store, riot_listing

    database_url = make_database()
    await upgrade_to_head(database_url)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(connections, "subscription_sessions", sessions)
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", "test-key")
    now = datetime.now(UTC)

    def vlr(match_id, hours, status, team1, team2):
        return schemas.ListedMatch(
            id=match_id,
            team1=schemas.MatchTeam(name=team1),
            team2=schemas.MatchTeam(name=team2),
            status=status,
            time=now + timedelta(hours=hours),
            event="Champions",
            series="Final",
        )

    def riot(riot_id, hours, teams, **fields):
        return RiotWebMatch(
            riot_id=riot_id,
            start_time=now + timedelta(hours=hours),
            league_name="Challengers BR",
            teams=[{"name": name} for name in teams],
            tournament_id="112053399716844250",
            **fields,
        )

    async with sessions.begin() as session:
        session.add(Team(id="7", source="vlr", name="Alpha", riot_name="Alpha", first_seen_at=0, last_fetched_at=0))
        session.add_all(
            [
                # Nobody lists it. Riot's early details answer 0 wins each, which is not a score yet.
                riot(
                    "115576361461208401",
                    5,
                    ["Alpha", "Beta"],
                    details={"tournament_id": "1", "game_wins": {"Alpha": 0, "Beta": 0}, "games": []},
                ),
                riot("115576361461208407", 3, ["Lambda", "Mu"], vlr_match_id="903"),  # linked: VLR's own entry
                # VLR lists it before the link is stored: VLR's entry is the match.
                riot("115576361461208402", 7, ["Gamma", "Delta"]),
                riot(
                    "115576361461208403",
                    -30,
                    ["Eps", "Zeta"],
                    details={"tournament_id": "1", "game_wins": {"Eps": 2, "Zeta": 0}, "games": []},
                ),
                riot("115576361461208404", 6, ["TBD", "Beta"]),
                riot("115576361461208405", 24 * 5, ["Eta", "Theta"]),  # too far
                riot("115576361461208406", 8, ["Iota", "Kappa"], vlr_match_id="999"),
                # A rematch of a match VLR lists and has linked: it is listed itself, not taken for VLR's entry.
                riot("115576361461208408", 2, ["One", "Two"], vlr_match_id="900"),
                riot("115576361461208409", 4, ["One", "Two"]),
                # VLR lists it with a team still undecided: VLR's entry and id come first.
                riot("115576361461208411", 6, ["Nu", "Xi"]),
            ]
        )
    listed = [
        vlr("900", 2, MatchStatus.UPCOMING, "One", "Two"),
        vlr("903", 3, MatchStatus.UPCOMING, "Lambda", "Mu"),
        vlr("905", 6, MatchStatus.UPCOMING, "TBD", "Xi"),
        vlr("901", 7, MatchStatus.UPCOMING, "Gamma", "Delta"),
        vlr("902", 9, MatchStatus.UPCOMING, "Three", "Four"),
        vlr("999", -40, MatchStatus.COMPLETED, "Iota", "Kappa"),
    ]

    # The schedule cron gives each series VLR does not list its row, which is what lists it.
    async with sessions.begin() as session:
        await ranking_store.keep_listed(session, listed, now)
    result = await riot_listing.with_riot_only(listed)

    assert [(item.id, item.status) for item in result] == [
        ("900", MatchStatus.UPCOMING),
        ("903", MatchStatus.UPCOMING),
        ("115576361461208409", MatchStatus.UPCOMING),
        ("115576361461208401", MatchStatus.UPCOMING),
        ("905", MatchStatus.UPCOMING),
        ("901", MatchStatus.UPCOMING),
        ("902", MatchStatus.UPCOMING),
        ("115576361461208403", MatchStatus.COMPLETED),
        ("999", MatchStatus.COMPLETED),
    ]
    upcoming, completed = result[3], result[7]
    assert (upcoming.team1.id, upcoming.team2.id, upcoming.event, upcoming.event_id) == (
        "7",
        None,
        "Challengers BR",
        "112053399716844250",  # the Riot-created event of its tournament: always an event that opens
    )
    assert (upcoming.team1.score, upcoming.team2.score) == (None, None)
    assert [(team.name, team.score) for team in (completed.team1, completed.team2)] == [("Eps", 2), ("Zeta", 0)]

    # VLR then lists the match: its entry keeps the Riot id, before the link is stored and after.
    # A later series of the same teams does not take the entry from the one at the same time.
    listed.insert(2, vlr("904", 5, MatchStatus.UPCOMING, "Alpha", "Beta"))
    async with sessions.begin() as session:
        session.add(riot("115576361461208410", 8, ["Alpha", "Beta"]))
        await session.flush()
        await ranking_store.keep_listed(session, [], now)
    for link in (None, "904"):
        if link:
            async with sessions.begin() as session:
                (await session.get(RiotWebMatch, "115576361461208401")).vlr_match_id = link
        relisted = await riot_listing.with_riot_only(listed)
        shown = [(item.id, item.team1.name) for item in relisted]
        assert shown[3] == ("115576361461208401", "Alpha") and "115576361461208410" in dict(shown)
        assert relisted[3].event_id == "112053399716844250"  # its event keeps the tournament id too

    # With the Riot key unset, and when the database cannot be read, the list is VLR's alone.
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", None)
    assert await riot_listing.with_riot_only(listed) == listed
    monkeypatch.setattr(settings, "RIOT_WEB_API_KEY", "test-key")
    monkeypatch.setattr(connections, "subscription_sessions", None)
    assert await riot_listing.with_riot_only(listed) == listed
    await engine.dispose()
