import httpx
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any
import asyncio

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"


class MLBApiClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=MLB_API_BASE,
            timeout=15.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"Accept": "application/json"}
        )
        self._cache: Dict[str, tuple[Any, datetime]] = {}
        self._cache_ttl = timedelta(minutes=30)

    async def close(self):
        await self.client.aclose()

    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self._cache:
            data, timestamp = self._cache[key]
            if datetime.now() - timestamp < self._cache_ttl:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Any):
        self._cache[key] = (data, datetime.now())

    async def get_schedule(self, game_date: Optional[date] = None) -> dict:
        target_date = game_date or date.today()
        cache_key = f"schedule_{target_date.isoformat()}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        response = await self.client.get(
            "/schedule",
            params={
                "date": target_date.isoformat(),
                "sportId": 1,
                "hydrate": "team,linescore,probablePitcher"
            }
        )
        response.raise_for_status()
        data = response.json()
        self._set_cache(cache_key, data)
        return data

    async def get_roster(self, team_id: int, season: Optional[int] = None) -> dict:
        current_season = season or datetime.now().year
        cache_key = f"roster_{team_id}_{current_season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        response = await self.client.get(
            f"/teams/{team_id}/roster",
            params={"season": current_season, "rosterType": "active"}
        )
        response.raise_for_status()
        data = response.json()
        self._set_cache(cache_key, data)
        return data

    async def get_player_stats(self, player_id: int, season: Optional[int] = None) -> Optional[dict]:
        current_season = season or datetime.now().year
        cache_key = f"stats_{player_id}_{current_season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"/people/{player_id}/stats",
                params={
                    "stats": "season",
                    "season": current_season,
                    "sportId": 1
                }
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_player_info(self, player_id: int) -> Optional[dict]:
        cache_key = f"player_info_{player_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(f"/people/{player_id}")
            response.raise_for_status()
            data = response.json()
            if "people" in data and data["people"]:
                person = data["people"][0]
                result = {
                    "full_name": person.get("fullName", ""),
                    "bat_side": person.get("batSide", {}).get("code", ""),
                    "pitch_hand": person.get("pitchHand", {}).get("code", ""),
                    "position": person.get("primaryPosition", {}).get("abbreviation", ""),
                }
                self._set_cache(cache_key, result)
                return result
            return None
        except Exception:
            return None

    async def get_player_game_log(self, player_id: int, season: int, game_type: str = "R") -> Optional[dict]:
        cache_key = f"gamelog_{player_id}_{season}_{game_type}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"/people/{player_id}/stats",
                params={
                    "stats": "gameLog",
                    "season": season,
                    "sportId": 1,
                    "gameType": game_type
                }
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_team_batting_stats(self, team_id: int, season: Optional[int] = None) -> Optional[dict]:
        current_season = season or datetime.now().year
        cache_key = f"team_batting_{team_id}_{current_season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"/teams/{team_id}/stats",
                params={
                    "season": current_season,
                    "sportId": 1,
                    "stats": "batting"
                }
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_batter_vs_pitcher(self, batter_id: int, pitcher_id: int) -> Optional[dict]:
        cache_key = f"h2h_{batter_id}_{pitcher_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"/people/{batter_id}/stats",
                params={
                    "stats": "vsPlayer",
                    "opponentId": pitcher_id,
                    "sportId": 1
                }
            )
            response.raise_for_status()
            data = response.json()
            if "stats" in data and data["stats"] and "splits" in data["stats"][0]:
                splits = data["stats"][0]["splits"]
                if splits:
                    stat = splits[0].get("stat", {})
                    result = {
                        "at_bats": stat.get("atBats", 0),
                        "hits": stat.get("hits", 0),
                        "home_runs": stat.get("homeRuns", 0),
                        "avg": stat.get("avg", 0.0),
                        "slg": stat.get("slg", 0.0),
                        "obp": stat.get("obp", 0.0),
                        "strike_outs": stat.get("strikeOuts", 0),
                        "base_on_balls": stat.get("baseOnBalls", 0),
                    }
                    self._set_cache(cache_key, result)
                    return result
            return None
        except Exception:
            return None
