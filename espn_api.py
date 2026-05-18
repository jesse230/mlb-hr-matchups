import httpx
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
import asyncio

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
ESPN_CORE = "https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb"


class ESPNApiClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
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

    async def get_scoreboard(self, game_date: date) -> dict:
        cache_key = f"espn_scoreboard_{game_date.isoformat()}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        date_str = game_date.strftime("%Y%m%d")
        try:
            response = await self.client.get(
                f"{ESPN_BASE}/scoreboard",
                params={"dates": date_str}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return {}

    async def get_roster(self, team_id: int, season: Optional[int] = None) -> Optional[dict]:
        cache_key = f"espn_roster_{team_id}_{season or 'current'}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(f"{ESPN_BASE}/teams/{team_id}/roster")
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_athlete_overview(self, athlete_id: int) -> Optional[dict]:
        cache_key = f"espn_overview_{athlete_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(f"{ESPN_CORE}/athletes/{athlete_id}/overview")
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_athlete_stats(self, athlete_id: int, season: int) -> Optional[dict]:
        cache_key = f"espn_stats_{athlete_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{ESPN_CORE}/athletes/{athlete_id}/stats",
                params={"season": season, "seasontype": "2"}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_athlete_gamelog(self, athlete_id: int, season: int) -> Optional[dict]:
        cache_key = f"espn_gamelog_{athlete_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{ESPN_CORE}/athletes/{athlete_id}/gamelog",
                params={"season": season, "seasontype": "2"}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None

    async def get_team_leaders(self, team_id: int, season: int) -> Optional[dict]:
        cache_key = f"espn_leaders_{team_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{ESPN_BASE}/teams/{team_id}/leaders",
                params={"season": season}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception:
            return None


espn_client = ESPNApiClient()
