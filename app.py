from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List
import math
import asyncio

from mlb_api import MLBApiClient
from ballpark_factors import get_hr_factor
from pitch_data import pitch_client


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

app = FastAPI(title="MLB HR Matchups")
templates = Jinja2Templates(directory="templates")

mlb_client = MLBApiClient()


def compute_recent_form(game_log_data: dict, target_date: date, days: int = 14) -> dict:
    if not game_log_data or "stats" not in game_log_data or not game_log_data["stats"]:
        return {"hr": 0, "avg": 0.0, "slg": 0.0, "games": 0, "hr_rate": 0.0}

    splits = game_log_data["stats"][0].get("splits", [])
    cutoff = target_date - timedelta(days=days)

    recent_hr = 0
    recent_ab = 0
    recent_pa = 0
    recent_hits = 0
    recent_tb = 0
    recent_games = 0

    for split in splits:
        game_date_str = split.get("date", "")
        if not game_date_str:
            continue
        try:
            game_date = datetime.strptime(game_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if game_date < cutoff or game_date > target_date:
            continue

        stat = split.get("stat", {})
        if not stat:
            continue

        recent_games += 1
        ab = stat.get("atBats", 0)
        pa = stat.get("plateAppearances", ab)
        hr = stat.get("homeRuns", 0)
        hits = stat.get("hits", 0)
        doubles = stat.get("doubles", 0)
        triples = stat.get("triples", 0)

        recent_ab += ab
        recent_pa += pa
        recent_hr += hr
        recent_hits += hits
        recent_tb += hits + doubles + (2 * triples) + (3 * hr)

    avg = round(recent_hits / recent_ab, 3) if recent_ab > 0 else 0.0
    slg = round(recent_tb / recent_ab, 3) if recent_ab > 0 else 0.0
    hr_rate = round(recent_hr / recent_pa, 3) if recent_pa > 0 else 0.0

    return {
        "hr": recent_hr,
        "avg": avg,
        "slg": slg,
        "games": recent_games,
        "hr_rate": hr_rate,
    }


@app.on_event("shutdown")
async def shutdown_event():
    await mlb_client.close()
    await pitch_client.close()


async def fetch_player_stats(player_entry: dict, season: int) -> Optional[dict]:
    player = player_entry["person"]
    position = player_entry.get("position", {}).get("abbreviation", "")

    if position not in ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "OF"]:
        return None

    stats_data = await mlb_client.get_player_stats(player["id"], season)

    if not stats_data or "stats" not in stats_data or not stats_data["stats"]:
        return None

    stat_group = stats_data["stats"][0]
    if "splits" not in stat_group or not stat_group["splits"]:
        return None

    split = stat_group["splits"][0]
    stat = split.get("stat", {})

    hr = stat.get("homeRuns", 0)
    if hr == 0:
        return None

    ab = stat.get("atBats", 0)
    pa = stat.get("plateAppearances", ab)
    avg = safe_float(stat.get("avg"))
    slg = safe_float(stat.get("slg"))
    ops = safe_float(stat.get("ops"))
    hr_rate = hr / pa if pa > 0 else 0.0
    iso = slg - avg

    return {
        "player_id": player["id"],
        "full_name": player["fullName"],
        "position": position,
        "home_runs": hr,
        "at_bats": ab,
        "plate_appearances": pa,
        "batting_avg": avg,
        "slugging_pct": slg,
        "ops": ops,
        "hr_rate": hr_rate,
        "iso": iso,
    }


def compute_matchup_score(batter_stats: dict, batter_arsenal: dict, pitch_mix: List[dict], barrel_data: dict, batted_ball_data: dict = None) -> float:
    pitch_usage_map = {p["pitch_code"]: p["usage_pct"] / 100.0 for p in pitch_mix}

    weighted_slg = 0.0
    weighted_iso = 0.0
    total_usage = 0.0

    for pitch_code, usage in pitch_usage_map.items():
        if pitch_code in batter_arsenal:
            b_slg = batter_arsenal[pitch_code]["slg"]
            b_iso = b_slg - batter_arsenal[pitch_code]["ba"]
            weighted_slg += b_slg * usage
            weighted_iso += b_iso * usage
            total_usage += usage

    if total_usage == 0:
        return batter_stats.get("slugging_pct", 0)

    barrel_pct = barrel_data.get("brl_pct", 0) if barrel_data else 0.0
    fb_pct = barrel_data.get("fb_pct", 0) if barrel_data else 0.0
    pull_pct = batted_ball_data.get("pull_pct", 0) if batted_ball_data else 0.0

    score = weighted_slg + (weighted_iso * 0.5) + (barrel_pct * 0.03) + (fb_pct * 0.005) + (pull_pct * 0.003)
    return round(score, 4)


async def fetch_game_matchups(game: dict, target_date: date, season: int) -> list:
    home_team = game["teams"]["home"]["team"]
    away_team = game["teams"]["away"]["team"]

    home_pitcher_entry = game["teams"]["home"].get("probablePitcher")
    away_pitcher_entry = game["teams"]["away"].get("probablePitcher")

    probable_home_pitcher = home_pitcher_entry.get("fullName") if home_pitcher_entry else None
    probable_away_pitcher = away_pitcher_entry.get("fullName") if away_pitcher_entry else None

    home_pitcher_id = home_pitcher_entry.get("id") if home_pitcher_entry else None
    away_pitcher_id = away_pitcher_entry.get("id") if away_pitcher_entry else None

    venue = game.get("venue", {}).get("name", "TBD")
    park_factor = get_hr_factor(venue)

    try:
        home_roster, away_roster = await asyncio.gather(
            mlb_client.get_roster(home_team["id"], season),
            mlb_client.get_roster(away_team["id"], season),
            return_exceptions=True
        )

        home_pitch_mix = None
        away_pitch_mix = None
        home_pitcher_splits = None
        away_pitcher_splits = None
        if home_pitcher_id:
            home_pitch_mix = await pitch_client.get_pitcher_pitch_mix(home_pitcher_id, season)
            home_pitcher_splits = await pitch_client.get_pitcher_handedness_splits(home_pitcher_id, season)
        if away_pitcher_id:
            away_pitch_mix = await pitch_client.get_pitcher_pitch_mix(away_pitcher_id, season)
            away_pitcher_splits = await pitch_client.get_pitcher_handedness_splits(away_pitcher_id, season)

        if not home_pitch_mix and not away_pitch_mix:
            return []

        home_pitcher_info = {
            "full_name": probable_home_pitcher,
            "pitcher_id": home_pitcher_id,
            "pitch_mix": home_pitch_mix,
            "handedness_splits": home_pitcher_splits,
        } if probable_home_pitcher else None

        away_pitcher_info = {
            "full_name": probable_away_pitcher,
            "pitcher_id": away_pitcher_id,
            "pitch_mix": away_pitch_mix,
            "handedness_splits": away_pitcher_splits,
        } if probable_away_pitcher else None

        if home_pitch_mix:
            for p in home_pitch_mix:
                p["is_weak_spot"] = p.get("hr_rate", 0) > 1.5 or p.get("slg_against", 0) > 0.500
        if away_pitch_mix:
            for p in away_pitch_mix:
                p["is_weak_spot"] = p.get("hr_rate", 0) > 1.5 or p.get("slg_against", 0) > 0.500

        async def get_top_n_batters(roster_data, opposing_pitcher_mix, opposing_pitcher_info, team_name, side, n=3):
            if isinstance(roster_data, Exception) or "roster" not in roster_data:
                return []
            if not opposing_pitcher_mix:
                return []

            position_players = [
                p for p in roster_data["roster"]
                if p.get("position", {}).get("abbreviation", "") in ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "OF"]
            ]

            opposing_weak_side = None
            if opposing_pitcher_info and opposing_pitcher_info.get("handedness_splits"):
                opposing_weak_side = opposing_pitcher_info["handedness_splits"].get("weak_side")

            semaphore = asyncio.Semaphore(10)

            async def process_batter(player_entry):
                async with semaphore:
                    player_id = player_entry["person"]["id"]
                    opposing_pitcher_id = opposing_pitcher_info.get("pitcher_id") if opposing_pitcher_info else None

                    stats_task = mlb_client.get_player_stats(player_id, season)
                    info_task = mlb_client.get_player_info(player_id)
                    gamelog_task = mlb_client.get_player_game_log(player_id, season)
                    arsenal_task = pitch_client.get_batter_pitch_arsenal(player_id, season)
                    barrels_task = pitch_client.get_batter_barrels(player_id, season)
                    batted_ball_task = pitch_client.get_batter_batted_ball_profile(player_id, season)
                    h2h_task = mlb_client.get_batter_vs_pitcher(player_id, opposing_pitcher_id) if opposing_pitcher_id else None

                    tasks = [stats_task, info_task, gamelog_task, arsenal_task, barrels_task, batted_ball_task]
                    if h2h_task:
                        tasks.append(h2h_task)

                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    stats_data, player_info, gamelog_data, arsenal, barrel_data, batted_ball_data = results[:6]
                    h2h_data = results[6] if (h2h_task and not isinstance(results[6], Exception)) else None

                    if isinstance(stats_data, Exception) or isinstance(arsenal, Exception) or isinstance(barrel_data, Exception):
                        return None

                    if not stats_data or "stats" not in stats_data or not stats_data["stats"]:
                        return None

                    stat_group = stats_data["stats"][0]
                    if "splits" not in stat_group or not stat_group["splits"]:
                        return None

                    split = stat_group["splits"][0]
                    stat = split.get("stat", {})

                    hr = stat.get("homeRuns", 0)
                    if hr == 0:
                        return None

                    ab = stat.get("atBats", 0)
                    pa = stat.get("plateAppearances", ab)
                    avg = safe_float(stat.get("avg"))
                    slg = safe_float(stat.get("slg"))
                    ops = safe_float(stat.get("ops"))
                    iso = slg - avg

                    batter_stats = {
                        "player_id": player_id,
                        "full_name": player_entry["person"]["fullName"],
                        "position": player_entry.get("position", {}).get("abbreviation", ""),
                        "home_runs": hr,
                        "at_bats": ab,
                        "plate_appearances": pa,
                        "batting_avg": avg,
                        "slugging_pct": slg,
                        "ops": ops,
                        "iso": iso,
                        "photo_url": f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{player_id}/headshot/67/current",
                    }

                    arsenal_dict = arsenal if isinstance(arsenal, dict) else {}
                    barrel_dict = barrel_data if isinstance(barrel_data, dict) else None
                    batted_ball_dict = batted_ball_data if isinstance(batted_ball_data, dict) else None
                    recent_form = compute_recent_form(gamelog_data if not isinstance(gamelog_data, Exception) else None, target_date)

                    bat_side = ""
                    platoon_advantage = False
                    if not isinstance(player_info, Exception) and player_info:
                        bat_side = player_info.get("bat_side", "")
                        if opposing_weak_side and bat_side == opposing_weak_side:
                            platoon_advantage = True

                    score = compute_matchup_score(batter_stats, arsenal_dict, opposing_pitcher_mix, barrel_dict, batted_ball_dict)

                    vs_pitch_breakdown = []
                    for pitch in opposing_pitcher_mix:
                        pc = pitch["pitch_code"]
                        if pc in arsenal_dict:
                            ab_data = arsenal_dict[pc]
                            pitch_hr_rate = pitch.get("hr_rate", 0)
                            pitch_slg_against = pitch.get("slg_against", 0)
                            is_weak_spot = pitch_hr_rate > 1.5 or pitch_slg_against > 0.500
                            vs_pitch_breakdown.append({
                                "pitch_code": pc,
                                "display_name": ab_data["display_name"],
                                "color": ab_data["color"],
                                "pa": ab_data["pa"],
                                "ba": ab_data["ba"],
                                "slg": ab_data["slg"],
                                "whiff_pct": ab_data["whiff_pct"],
                                "k_pct": ab_data["k_pct"],
                                "hard_hit_pct": ab_data["hard_hit_pct"],
                                "pitch_usage": pitch["usage_pct"],
                                "pitcher_hr_allowed": pitch.get("hr_allowed", 0),
                                "pitcher_slg_against": pitch_slg_against,
                                "pitcher_hr_rate": pitch_hr_rate,
                                "is_weak_spot": is_weak_spot,
                            })

                    return {
                        "batter": batter_stats,
                        "barrel_data": barrel_dict,
                        "batted_ball_data": batted_ball_dict,
                        "recent_form": recent_form,
                        "bat_side": bat_side,
                        "platoon_advantage": platoon_advantage,
                        "matchup_score": score,
                        "vs_pitch_breakdown": vs_pitch_breakdown,
                        "opposing_pitcher": opposing_pitcher_info,
                        "h2h": h2h_data if isinstance(h2h_data, dict) else None,
                    }

            tasks = [process_batter(p) for p in position_players]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            candidates = [r for r in results if r is not None and not isinstance(r, Exception)]
            candidates.sort(key=lambda x: x["matchup_score"], reverse=True)
            return candidates[:n]

        home_top = await get_top_n_batters(home_roster, away_pitcher_info.get("pitch_mix") if away_pitcher_info else None, away_pitcher_info, home_team["name"], "home", n=3)
        away_top = await get_top_n_batters(away_roster, home_pitcher_info.get("pitch_mix") if home_pitcher_info else None, home_pitcher_info, away_team["name"], "away", n=3)

        game_info = {
            "type": "game_info",
            "home_team": home_team["name"],
            "away_team": away_team["name"],
            "game_date": target_date.isoformat(),
            "game_time": game.get("gameDate", ""),
            "venue": venue,
            "park_factor": park_factor,
            "home_pitcher": home_pitcher_info,
            "away_pitcher": away_pitcher_info,
            "home_top_batters": home_top,
            "away_top_batters": away_top,
        }

        return [game_info]

    except Exception as e:
        print(f"Error in fetch_game_matchups: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/schedule")
async def api_schedule(game_date: Optional[str] = Query(None)):
    target_date = None
    if game_date:
        try:
            target_date = datetime.strptime(game_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = date.today()
    else:
        target_date = date.today()

    schedule_data = await mlb_client.get_schedule(target_date)

    games = []
    if "dates" in schedule_data and schedule_data["dates"]:
        for day_data in schedule_data["dates"]:
            for game in day_data.get("games", []):
                home_team = game["teams"]["home"]["team"]
                away_team = game["teams"]["away"]["team"]
                home_pitcher_entry = game["teams"]["home"].get("probablePitcher")
                away_pitcher_entry = game["teams"]["away"].get("probablePitcher")
                venue = game.get("venue", {}).get("name", "TBD")
                from ballpark_factors import get_hr_factor
                park_factor = get_hr_factor(venue)

                games.append({
                    "gamePk": game["gamePk"],
                    "home": {"name": home_team["name"], "id": home_team["id"]},
                    "away": {"name": away_team["name"], "id": away_team["id"]},
                    "home_pitcher": {"name": home_pitcher_entry.get("fullName"), "id": home_pitcher_entry.get("id")} if home_pitcher_entry else None,
                    "away_pitcher": {"name": away_pitcher_entry.get("fullName"), "id": away_pitcher_entry.get("id")} if away_pitcher_entry else None,
                    "venue": venue,
                    "park_factor": round(park_factor, 2),
                    "game_time": game.get("gameDate", ""),
                    "status": game["status"]["detailedState"],
                })

    return {"date": target_date.isoformat(), "games": games}


@app.get("/api/game/{game_pk}")
async def api_game(game_pk: int, game_date: Optional[str] = Query(None)):
    target_date = None
    if game_date:
        try:
            target_date = datetime.strptime(game_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = date.today()
    else:
        target_date = date.today()

    season = target_date.year
    if target_date.month < 3:
        season = target_date.year - 1

    schedule_data = await mlb_client.get_schedule(target_date)

    target_game = None
    if "dates" in schedule_data and schedule_data["dates"]:
        for day_data in schedule_data["dates"]:
            for game in day_data.get("games", []):
                if game["gamePk"] == game_pk:
                    target_game = game
                    break
            if target_game:
                break

    if not target_game:
        return {"error": "Game not found"}

    game_infos = await fetch_game_matchups(target_game, target_date, season)
    if not game_infos:
        return {"error": "No matchup data available"}

    return game_infos[0]


@app.get("/api/matchups")
async def api_matchups(game_date: Optional[str] = Query(None)):
    target_date = None
    if game_date:
        try:
            target_date = datetime.strptime(game_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = date.today()
    else:
        target_date = date.today()

    schedule_data = await mlb_client.get_schedule(target_date)

    games = []
    if "dates" in schedule_data and schedule_data["dates"]:
        for day_data in schedule_data["dates"]:
            for game in day_data.get("games", []):
                games.append({
                    "gamePk": game["gamePk"],
                    "home": game["teams"]["home"]["team"]["name"],
                    "away": game["teams"]["away"]["team"]["name"],
                    "status": game["status"]["detailedState"],
                })

    return {"date": target_date.isoformat(), "games": games}


@app.get("/api/pitch-mix/{pitcher_id}")
async def api_pitch_mix(pitcher_id: int):
    season = datetime.now().year
    if datetime.now().month < 3:
        season -= 1
    pitch_mix = await pitch_client.get_pitcher_pitch_mix(pitcher_id, season)
    return {"pitcher_id": pitcher_id, "pitch_mix": pitch_mix or []}
