from pydantic import BaseModel
from typing import Optional
from datetime import date


class PlayerHRStats(BaseModel):
    player_id: int
    full_name: str
    position: str
    home_runs: int
    at_bats: int
    plate_appearances: int
    batting_avg: float
    slugging_pct: float
    ops: float
    hr_rate: float

    @property
    def hr_per_game(self) -> float:
        return self.home_runs / 162.0 if self.home_runs else 0.0


class PitcherInfo(BaseModel):
    player_id: int
    full_name: str
    era: Optional[float] = None
    home_runs_allowed: Optional[int] = None
    innings_pitched: Optional[float] = None


class HRMatchup(BaseModel):
    game_date: date
    home_team: str
    away_team: str
    game_time: str
    venue: str
    batter: PlayerHRStats
    opposing_pitcher: Optional[PitcherInfo] = None


class GameInfo(BaseModel):
    game_pk: int
    game_date: date
    game_time: str
    home_team: str
    home_team_id: int
    away_team: str
    away_team_id: int
    venue: str
    status: str
    probable_home_pitcher: Optional[str] = None
    probable_away_pitcher: Optional[str] = None
