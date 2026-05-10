import pandas as pd
import math
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
from pybaseball import statcast_pitcher, statcast_batter_pitch_arsenal, statcast_batter_exitvelo_barrels, statcast_batter
import asyncio


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


PITCH_TYPE_NAMES = {
    "FF": ("4-Seam Fastball", "#e94560"),
    "SL": ("Slider", "#4ecca3"),
    "CU": ("Curveball", "#45b7d1"),
    "CH": ("Changeup", "#f39c12"),
    "SI": ("Sinker", "#9b59b6"),
    "FC": ("Cutter", "#e67e22"),
    "FS": ("Splitter", "#e74c3c"),
    "KC": ("Knuckle Curve", "#3498db"),
    "EP": ("Eephus", "#95a5a6"),
    "SC": ("Screwball", "#1abc9c"),
    "ST": ("Sweeper", "#8e44ad"),
}


def get_pitch_display(pitch_code: str) -> tuple:
    return PITCH_TYPE_NAMES.get(pitch_code, (pitch_code, "#888888"))


class PitchMixClient:
    def __init__(self):
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = timedelta(hours=12)
        self._season_data: Dict[str, pd.DataFrame] = {}

    async def close(self):
        pass

    def _get_cached(self, key: str) -> Optional:
        if key in self._cache:
            data, timestamp = self._cache[key]
            if datetime.now() - timestamp < self._cache_ttl:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = (data, datetime.now())

    async def _get_season_batter_arsenal(self, season: int) -> Optional[pd.DataFrame]:
        cache_key = f"season_arsenal_{season}"
        if cache_key in self._season_data:
            return self._season_data[cache_key]
        try:
            df = await asyncio.to_thread(
                statcast_batter_pitch_arsenal,
                season,
                minPA=5
            )
            if df is not None and not df.empty:
                self._season_data[cache_key] = df
            return df
        except Exception as e:
            print(f"Error fetching season arsenal {season}: {e}")
            return None

    async def _get_season_barrels(self, season: int) -> Optional[pd.DataFrame]:
        cache_key = f"season_barrels_{season}"
        if cache_key in self._season_data:
            return self._season_data[cache_key]
        try:
            df = await asyncio.to_thread(
                statcast_batter_exitvelo_barrels,
                season,
                minBBE=5
            )
            if df is not None and not df.empty:
                self._season_data[cache_key] = df
            return df
        except Exception as e:
            print(f"Error fetching season barrels {season}: {e}")
            return None

    async def get_batter_batted_ball_profile(self, batter_id: int, season: int) -> Optional[dict]:
        cache_key = f"batter_batted_ball_{batter_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            df = await asyncio.to_thread(
                statcast_batter,
                f"{season}-03-01",
                f"{season}-11-30",
                batter_id
            )
        except Exception as e:
            print(f"Error fetching batted ball for batter {batter_id}: {e}")
            return None

        if df is None or df.empty:
            return None

        bbe = df[df["bb_type"].notna()].copy()
        if bbe.empty:
            return None

        total_bbe = len(bbe)

        rhh = bbe[bbe["stand"] == "R"]
        lhb = bbe[bbe["stand"] == "L"]

        pull_count = 0
        if not rhh.empty:
            pull_count += len(rhh[rhh["hc_x"] > 125.44])
        if not lhb.empty:
            pull_count += len(lhb[lhb["hc_x"] < 125.44])

        pull_rate = (pull_count / total_bbe) * 100 if total_bbe > 0 else 0

        fly_balls = bbe[bbe["launch_angle"] >= 10]
        fb_rate = (len(fly_balls) / total_bbe) * 100 if total_bbe > 0 else 0

        result = {
            "pull_pct": round(pull_rate, 1),
            "fb_pct_la": round(fb_rate, 1),
            "bbe_count": total_bbe,
        }

        self._set_cache(cache_key, result)
        return result
        return None

    async def get_batter_pitch_arsenal(self, batter_id: int, season: int) -> Optional[Dict[str, dict]]:
        cache_key = f"batter_arsenal_{batter_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        full_data = await self._get_season_batter_arsenal(season)
        if full_data is None or full_data.empty:
            return None

        batter_rows = full_data[full_data["player_id"] == batter_id]
        if batter_rows.empty:
            return None

        result = {}
        for _, row in batter_rows.iterrows():
            pitch_code = row.get("pitch_type", "")
            if not pitch_code or pitch_code not in PITCH_TYPE_NAMES:
                continue

            display_name, color = get_pitch_display(pitch_code)
            result[pitch_code] = {
                "pitch_code": pitch_code,
                "display_name": display_name,
                "color": color,
                "pa": int(row.get("pa", 0)),
                "ba": safe_float(row.get("ba")),
                "slg": safe_float(row.get("slg")),
                "woba": safe_float(row.get("woba")),
                "whiff_pct": round(safe_float(row.get("whiff_percent")), 1),
                "k_pct": round(safe_float(row.get("k_percent")), 1),
                "hard_hit_pct": round(safe_float(row.get("hard_hit_percent")), 1),
                "est_slg": safe_float(row.get("est_slg")),
            }

        if result:
            self._set_cache(cache_key, result)
            return result
        return None

    async def get_batter_barrels(self, batter_id: int, season: int) -> Optional[dict]:
        cache_key = f"batter_barrels_{batter_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        full_data = await self._get_season_barrels(season)
        if full_data is None or full_data.empty:
            return None

        batter_rows = full_data[full_data["player_id"] == batter_id]
        if batter_rows.empty:
            return None

        row = batter_rows.iloc[0]
        result = {
            "barrels": int(row.get("barrels", 0)),
            "brl_pct": round(safe_float(row.get("brl_percent")), 1),
            "brl_pa": round(safe_float(row.get("brl_pa")), 1),
            "avg_hit_speed": round(safe_float(row.get("avg_hit_speed")), 1),
            "max_hit_speed": round(safe_float(row.get("max_hit_speed")), 1),
            "avg_hr_distance": round(safe_float(row.get("avg_hr_distance")), 1),
            "ev95plus": int(row.get("ev95plus", 0)),
            "ev95_pct": round(safe_float(row.get("ev95percent")), 1),
            "fb_pct": round(safe_float(row.get("fbld")), 1),
            "gb_pct": round(safe_float(row.get("gb")), 1),
        }

        self._set_cache(cache_key, result)
        return result

    async def get_pitcher_handedness_splits(self, pitcher_id: int, season: int) -> Optional[dict]:
        cache_key = f"pitcher_splits_{pitcher_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        df = await self._get_pitcher_season_data(pitcher_id, season)
        if df is None or df.empty or "stand" not in df.columns:
            return None

        result = {}
        for stand in ["L", "R"]:
            side_df = df[df["stand"] == stand]
            if side_df.empty:
                result[stand] = {"pa": 0, "hr": 0, "slg": 0.0, "hr_rate": 0.0}
                continue

            pa = len(side_df)
            hr = len(side_df[side_df["events"] == "home_run"]) if "events" in side_df.columns else 0

            ab = len(side_df[(side_df["bb_type"].notna()) | (side_df["events"].isin(["single", "double", "triple", "home_run", "strikeout", "field_error"]))]) if "events" in side_df.columns else pa
            total_bases = 0
            if "events" in side_df.columns:
                for _, row in side_df.iterrows():
                    evt = str(row.get("events", ""))
                    if "single" in evt:
                        total_bases += 1
                    elif "double" in evt:
                        total_bases += 2
                    elif "triple" in evt:
                        total_bases += 3
                    elif "home_run" in evt:
                        total_bases += 4

            slg = round(total_bases / ab, 3) if ab > 0 else 0.0
            hr_rate = round((hr / pa) * 100, 2) if pa > 0 else 0.0

            result[stand] = {
                "pa": pa,
                "hr": hr,
                "slg": slg,
                "hr_rate": hr_rate,
            }

        weak_side = None
        l_hr_rate = result["L"]["hr_rate"]
        r_hr_rate = result["R"]["hr_rate"]
        l_slg = result["L"]["slg"]
        r_slg = result["R"]["slg"]

        if l_hr_rate > 0 and r_hr_rate > 0:
            if l_hr_rate > r_hr_rate * 1.3 or l_slg > r_slg * 1.2:
                weak_side = "L"
            elif r_hr_rate > l_hr_rate * 1.3 or r_slg > l_slg * 1.2:
                weak_side = "R"
        elif l_hr_rate > 0:
            weak_side = "L"
        elif r_hr_rate > 0:
            weak_side = "R"

        result["weak_side"] = weak_side

        if result["L"]["pa"] > 0 or result["R"]["pa"] > 0:
            self._set_cache(cache_key, result)
            return result
        return None

    async def get_batter_batted_ball_profile(self, batter_id: int, season: int) -> Optional[dict]:
        cache_key = f"batter_batted_ball_{batter_id}_{season}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        df = await self._get_season_batted_ball(season)
        if df is None or df.empty:
            return None

        batter_df = df[df["player_id"] == batter_id]
        if batter_df.empty:
            return None

        bbe = batter_df[batter_df["bb_type"].notna()].copy()
        if bbe.empty:
            return None

        total_bbe = len(bbe)

        rhh = bbe[bbe["stand"] == "R"]
        lhb = bbe[bbe["stand"] == "L"]

        pull_count = 0
        if not rhh.empty:
            pull_count += len(rhh[rhh["hc_x"] > 125.44])
        if not lhb.empty:
            pull_count += len(lhb[lhb["hc_x"] < 125.44])

        pull_rate = (pull_count / total_bbe) * 100 if total_bbe > 0 else 0

        fly_balls = bbe[bbe["launch_angle"] >= 10]
        fb_rate = (len(fly_balls) / total_bbe) * 100 if total_bbe > 0 else 0

        result = {
            "pull_pct": round(pull_rate, 1),
            "fb_pct_la": round(fb_rate, 1),
            "bbe_count": total_bbe,
        }

        self._set_cache(cache_key, result)
        return result


pitch_client = PitchMixClient()
