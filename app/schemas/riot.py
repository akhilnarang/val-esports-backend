"""Riot's esports match API payloads, and the shapes that link its games to VLR maps."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

VlrTeamSlot = Literal["team1", "team2"]


class _RiotPayload(BaseModel):
    """Reads Riot's camelCase fields and ignores the rest of its payload."""

    model_config = ConfigDict(alias_generator=to_camel)


class RecentGames(_RiotPayload):
    match_ids: list[str]


class RiotMatchInfo(_RiotPayload):
    map_id: str
    is_completed: bool
    game_start_millis: int


class RiotPlayer(_RiotPayload):
    game_name: str
    tag_line: str
    team_id: str


class RiotTeam(_RiotPayload):
    team_id: str
    rounds_won: int = 0


class RiotGame(_RiotPayload):
    match_info: RiotMatchInfo
    players: list[RiotPlayer]
    teams: list[RiotTeam]
    round_results: list[dict] = Field(default_factory=list)


class Side(BaseModel):
    """One side of a Riot game."""

    rounds_won: int
    players: list[str]


class Game(BaseModel):
    """A completed Riot game, with whatever linking could read from it."""

    riot_id: str
    map: str | None
    started_at: datetime
    red: Side | None
    blue: Side | None
    data: dict

    @property
    def linkable(self) -> bool:
        """Whether linking has a known map and both sides' players to compare.

        :return: True when the game can be compared with a VLR map.
        """
        return (
            self.map is not None
            and self.red is not None
            and self.blue is not None
            and bool(self.red.players)
            and bool(self.blue.players)
        )


class VlrTeam(BaseModel):
    """A team on one VLR map."""

    name: str
    tag: str | None
    map_score: int
    players: list[str]


class Candidate(BaseModel):
    """A VLR map whose name and final score fit a Riot game; teams are in the map panel's order."""

    match_id: str
    map_number: int
    team1: VlrTeam
    team2: VlrTeam


class GameLink(BaseModel):
    """The model's verdict on whether a Riot game is a VLR map, and which side is which team."""

    red_team: VlrTeamSlot | Literal["neither"]
    blue_team: VlrTeamSlot | Literal["neither"]
    same_game: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


class ExtraPlayer(BaseModel):
    """One player's whole-map line from Riot; IDs are Riot's content UUIDs."""

    name: str
    team: VlrTeamSlot
    agent_id: str
    kills: int
    deaths: int
    assists: int
    score: int


class ExtraLocation(BaseModel):
    """A spot on the map's radar, as fractions of its width and height from its top left corner.

    A value outside 0 to 1 is off the radar, such as Iso's Kill Contract arena or a fall past the map's edge.
    """

    x: float
    y: float


class ExtraPlayerLocation(BaseModel):
    """Where a player stood at one moment of a round, facing ``facing`` radians on the radar (0 right, π/2 down)."""

    name: str
    location: ExtraLocation
    facing: float


class ExtraKill(BaseModel):
    """One kill in a round, in the order it happened.

    ``player_locations`` is where Riot placed the living as it landed, without the victim, who fell at
    ``victim_location``, which is None when Riot recorded none.
    """

    killer: str
    victim: str
    assistants: list[str]
    time_millis: int
    damage_type: str
    damage_item: str | None
    victim_location: ExtraLocation | None
    player_locations: list[ExtraPlayerLocation]


class ExtraSpike(BaseModel):
    """A plant or defuse: when it finished, where (None when Riot recorded none), and where Riot placed the living."""

    time_millis: int
    location: ExtraLocation | None
    player_locations: list[ExtraPlayerLocation]


class ExtraRoundPlayer(BaseModel):
    """One player's damage and buy in a round."""

    name: str
    score: int
    damage: int
    headshots: int
    bodyshots: int
    legshots: int
    loadout_value: int
    spent: int
    remaining: int
    weapon_id: str | None
    armor_id: str | None


class ExtraRound(BaseModel):
    """One round of a map, numbered from 1.

    ``ceremony`` is Riot's highlight for it (Clutch, Ace, TeamAce, Flawless, Closer or Thrifty), or None.
    """

    number: int
    winner: VlrTeamSlot
    winner_role: str
    result: str
    ceremony: str | None
    plant_site: str | None
    planter: str | None
    defuser: str | None
    plant: ExtraSpike | None
    defuse: ExtraSpike | None
    kills: list[ExtraKill]
    players: list[ExtraRoundPlayer]


class ExtraMap(BaseModel):
    """A VLR map's Riot game, with sides named by VLR's team slots and every location on the radar at ``radar_url``."""

    map_number: int
    map: str
    radar_url: str
    started_at: datetime
    duration_millis: int
    patch: str
    players: list[ExtraPlayer]
    rounds: list[ExtraRound]


class MatchExtra(BaseModel):
    """What ``GET /api/v1/matches/{id}/extra`` returns: one entry per map linked to a Riot game."""

    maps: list[ExtraMap]
