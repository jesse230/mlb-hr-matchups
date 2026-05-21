import httpx
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
import asyncio

MMA_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
MMA_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"


class MMAApiClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=20.0,
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

    async def get_scoreboard(self, game_date: Optional[date] = None, limit: int = 50) -> dict:
        cache_key = f"mma_scoreboard_{game_date.isoformat() if game_date else 'all'}_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        params = {"limit": limit}
        if game_date:
            params["dates"] = game_date.strftime("%Y%m%d")

        try:
            response = await self.client.get(
                f"{MMA_SITE_BASE}/scoreboard",
                params=params
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA scoreboard error: {e}")
            return {}

    async def get_event_summary(self, event_id: int) -> dict:
        cache_key = f"mma_event_summary_{event_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{MMA_SITE_BASE}/summary",
                params={"event": event_id}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA event summary error: {e}")
            return {}

    async def get_news(self, limit: int = 20) -> dict:
        cache_key = f"mma_news_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{MMA_SITE_BASE}/news",
                params={"limit": limit}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA news error: {e}")
            return {}

    async def get_rankings(self) -> dict:
        cache_key = "mma_rankings"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{MMA_CORE_BASE}/rankings"
            )
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            ranking_refs = [item.get("$ref") for item in items if item.get("$ref")]

            ranking_tasks = [self._fetch_ranking_detail(ref) for ref in ranking_refs]
            ranking_details = await asyncio.gather(*ranking_tasks, return_exceptions=True)

            all_athlete_refs = []
            ranked_lists = []
            for detail in ranking_details:
                if isinstance(detail, Exception) or not detail:
                    continue
                ranks = []
                for rank_item in detail.get("ranks", []):
                    athlete_ref = rank_item.get("athlete", {}).get("$ref", "")
                    athlete_id = self._extract_id_from_ref(athlete_ref)
                    ranks.append({
                        "current": rank_item.get("current", 0),
                        "trend": rank_item.get("trend", ""),
                        "athlete_id": athlete_id,
                        "athlete_ref": athlete_ref,
                    })
                    if athlete_ref:
                        all_athlete_refs.append(athlete_ref)
                ranked_lists.append({
                    "name": detail.get("name", detail.get("shortName", "")),
                    "shortName": detail.get("shortName", ""),
                    "ranks": ranks,
                })

            athlete_map = await self._fetch_athletes_batch(all_athlete_refs)

            for rlist in ranked_lists:
                for rank in rlist["ranks"]:
                    ref = rank.get("athlete_ref", "")
                    if ref and ref in athlete_map:
                        a = athlete_map[ref]
                        rank["name"] = a.get("displayName") or a.get("fullName", "")
                        rank["headshot"] = a.get("headshot", {}).get("href", "") if isinstance(a.get("headshot"), dict) else ""
                        rank["record"] = a.get("record", {}).get("summary", "") if isinstance(a.get("record"), dict) else ""

            result = {"rankings": ranked_lists}
            self._set_cache(cache_key, result)
            return result
        except Exception as e:
            print(f"MMA rankings error: {e}")
            return {}

    async def _fetch_ranking_detail(self, ref: str) -> Optional[dict]:
        try:
            response = await self.client.get(ref)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Ranking detail error for {ref}: {e}")
            return None

    async def _fetch_athletes_batch(self, refs: List[str]) -> Dict[str, dict]:
        if not refs:
            return {}

        unique_refs = list(dict.fromkeys(refs))
        semaphore = asyncio.Semaphore(20)

        async def fetch_one(ref):
            async with semaphore:
                cache_key = f"mma_athlete_{ref}"
                cached = self._get_cached(cache_key)
                if cached:
                    return ref, cached
                try:
                    response = await self.client.get(ref)
                    response.raise_for_status()
                    data = response.json()
                    self._set_cache(cache_key, data)
                    return ref, data
                except Exception:
                    return ref, {}

        tasks = [fetch_one(ref) for ref in unique_refs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        athlete_map = {}
        for result in results:
            if isinstance(result, tuple) and len(result) == 2:
                ref, data = result
                if data:
                    athlete_map[ref] = data
        return athlete_map

    def _extract_id_from_ref(self, ref: str) -> Optional[int]:
        if not ref:
            return None
        try:
            parts = ref.rstrip("/").split("/")
            athlete_idx = -1
            for i, p in enumerate(parts):
                if p == "athletes" and i + 1 < len(parts):
                    athlete_idx = i + 1
                    break
            if athlete_idx >= 0:
                raw = parts[athlete_idx].split("?")[0]
                return int(raw)
        except (ValueError, IndexError):
            pass
        return None

    async def get_events(self, limit: int = 50) -> dict:
        cache_key = f"mma_events_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(
                f"{MMA_CORE_BASE}/events",
                params={"limit": limit}
            )
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA events error: {e}")
            return {}

    async def get_event_detail(self, event_ref: str) -> dict:
        cache_key = f"mma_event_detail_{event_ref}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(event_ref)
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA event detail error: {e}")
            return {}

    async def get_competition_detail(self, event_ref: str) -> dict:
        cache_key = f"mma_competition_{event_ref}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(event_ref)
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA competition error: {e}")
            return {}

    async def get_athlete(self, athlete_ref: str) -> dict:
        cache_key = f"mma_athlete_{athlete_ref}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(athlete_ref)
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA athlete error: {e}")
            return {}

    async def get_competition_odds(self, odds_ref: str) -> dict:
        cache_key = f"mma_odds_{odds_ref}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await self.client.get(odds_ref)
            response.raise_for_status()
            data = response.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            print(f"MMA odds error: {e}")
            return {}

    def parse_event_summary(self, summary_data: dict) -> dict:
        """Parse site API event summary into structured fight data."""
        if not summary_data:
            return {"header": {}, "fights": []}

        header = summary_data.get("header", {})
        competitions = header.get("competitions", [])
        athletes = summary_data.get("athletes", [])
        notes = summary_data.get("notes", {})

        athlete_map = {}
        for a in athletes:
            athlete_map[a.get("id")] = {
                "id": a.get("id"),
                "fullName": a.get("fullName"),
                "displayName": a.get("displayName"),
                "headshot": a.get("headshot", {}).get("href", ""),
                "flag": a.get("flag", {}).get("href", ""),
                "weight": a.get("weight"),
                "height": a.get("height"),
                "stance": a.get("stance"),
                "record": a.get("record", {}),
                "stats": a.get("statistics", {}),
            }

        fights = []
        for comp in competitions:
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            comp_id = comp.get("id")
            status = comp.get("status", {}).get("type", {}).get("name", "SCHEDULED")

            fight = {
                "id": comp_id,
                "description": comp.get("description", ""),
                "status": status,
                "status_detail": comp.get("status", {}).get("detail", ""),
                "weight_class": comp.get("weightClass", {}).get("text", ""),
                "order": comp.get("order", 0),
                "winner": None,
                "fighters": [],
            }

            for c in competitors:
                fighter_id = c.get("id")
                fighter = athlete_map.get(fighter_id, {})
                score = c.get("score")

                result = "PENDING"
                if c.get("winner"):
                    result = "WINNER"
                    fight["winner"] = fighter_id

                fight["fighters"].append({
                    "id": fighter_id,
                    "name": fighter.get("displayName") or c.get("athlete", {}).get("displayName", "Unknown"),
                    "fullName": fighter.get("fullName") or c.get("athlete", {}).get("fullName", "Unknown"),
                    "headshot": fighter.get("headshot") or c.get("athlete", {}).get("headshot", {}).get("href", ""),
                    "record": fighter.get("record") or c.get("record", ""),
                    "weight": fighter.get("weight") or c.get("athlete", {}).get("weight"),
                    "height": fighter.get("height") or c.get("athlete", {}).get("height"),
                    "stance": fighter.get("stance") or c.get("athlete", {}).get("stance"),
                    "stats": fighter.get("stats") or c.get("athlete", {}).get("statistics", {}),
                    "score": score,
                    "result": result,
                    "$ref": c.get("athlete", {}).get("$ref", ""),
                })
                for f in fight["fighters"]:
                    if not f.get("$ref"):
                        f["$ref"] = f"https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes/{fighter_id}"

            fights.append(fight)

        fights.sort(key=lambda f: f.get("order", 0))

        event_header = {
            "id": header.get("id"),
            "name": header.get("name", ""),
            "date": header.get("date", ""),
            "venue": header.get("venue", ""),
            "location": header.get("location", ""),
            "status": header.get("status", {}).get("type", {}).get("name", ""),
            "broadcasts": header.get("broadcasts", []),
            "notes_headline": notes.get("headline", ""),
        }

        return {"header": event_header, "fights": fights}

    async def get_fighter_detail(self, athlete_ref: str) -> dict:
        """Fetch detailed fighter stats from the core API."""
        data = await self.get_athlete(athlete_ref)
        if not data:
            return {}

        record = data.get("record", {})
        stats = data.get("statistics", {})

        return {
            "id": data.get("id"),
            "fullName": data.get("fullName"),
            "displayName": data.get("displayName"),
            "headshot": data.get("headshot", {}).get("href", ""),
            "weight": data.get("weight"),
            "height": data.get("height"),
            "reach": data.get("reach"),
            "stance": data.get("stance"),
            "dateOfBirth": data.get("dateOfBirth"),
            "record": {
                "wins": record.get("wins", 0),
                "losses": record.get("losses", 0),
                "draws": record.get("draws", 0),
                "summary": record.get("summary", ""),
            },
            "stats_summary": stats.get("summary", ""),
            "stats": [
                {"name": s.get("name", ""), "displayValue": s.get("displayValue", ""), "abbreviation": s.get("abbreviation", "")}
                for s in stats.get("splits", {}).get("categories", [])[:20]
            ] if stats.get("splits") else [],
        }


mma_client = MMAApiClient()
