import httpx
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any
import math
import asyncio

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"


def safe_float(value, default=0.0) -> float:
    if value is None:
        return default
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


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

            if "stats" not in data or not data["stats"]:
                return None

            stats_entry = data["stats"][0]
            if "splits" not in stats_entry or not stats_entry["splits"]:
                return None

            splits = stats_entry["splits"]

            total_ab = 0
            total_hits = 0
            total_hr = 0
            total_k = 0
            total_bb = 0
            total_1b = 0
            total_2b = 0
            total_3b = 0

            for split in splits:
                stat = split.get("stat", {})
                ab = stat.get("atBats", 0)
                hits = stat.get("hits", 0)
                hr = stat.get("homeRuns", 0)
                k = stat.get("strikeOuts", 0)
                bb = stat.get("baseOnBalls", 0)
                doubles = stat.get("doubles", 0)
                triples = stat.get("triples", 0)

                total_ab += ab
                total_hits += hits
                total_hr += hr
                total_k += k
                total_bb += bb
                total_1b += (hits - doubles - triples - hr)
                total_2b += doubles
                total_3b += triples

            if total_ab == 0:
                return {
                    "at_bats": 0, "hits": 0, "home_runs": 0,
                    "avg": 0.0, "slg": 0.0, "obp": 0.0,
                    "strike_outs": 0, "base_on_balls": 0,
                }

            total_tb = total_1b + (2 * total_2b) + (3 * total_3b) + (4 * total_hr)
            avg = total_hits / total_ab
            slg = total_tb / total_ab

            result = {
                "at_bats": total_ab,
                "hits": total_hits,
                "home_runs": total_hr,
                "avg": round(avg, 3),
                "slg": round(slg, 3),
                "obp": round((total_hits + total_bb) / (total_ab + total_bb), 3) if (total_ab + total_bb) > 0 else 0.0,
                "strike_outs": total_k,
                "base_on_balls": total_bb,
            }
            self._set_cache(cache_key, result)
            return result
        except Exception as e:
            print(f"H2H error for batter {batter_id} vs pitcher {pitcher_id}: {e}")
            return None
