from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List
import math
import asyncio
import json

from mlb_api import MLBApiClient
from ballpark_factors import get_hr_factor
from pitch_data import pitch_client
from espn_api import espn_client
from mma_api import mma_client


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

_game_cache: Dict[int, tuple[Any, datetime]] = {}
_game_cache_ttl = timedelta(minutes=30)
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
    await espn_client.close()
    await mma_client.close()


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
    if not pitch_mix:
        barrel_pct = barrel_data.get("brl_pct", 0) if barrel_data else 0.0
        fb_pct = barrel_data.get("fb_pct", 0) if barrel_data else 0.0
        pull_pct = batted_ball_data.get("pull_pct", 0) if batted_ball_data else 0.0
        score = batter_stats.get("slugging_pct", 0) + (barrel_pct * 0.03) + (fb_pct * 0.005) + (pull_pct * 0.003)
        return round(score, 4)

    pitch_usage_map = {p["pitch_code"]: p["usage_pct"] / 100.0 for p in pitch_mix}

    weighted_slg = 0.0
    weighted_iso = 0.0
    total_usage = 0.0

    for pitch_code, usage in pitch_usage_map.items():
        if pitch_code in batter_arsenal and usage >= 0.10:
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

        if (isinstance(home_roster, Exception) or "roster" not in home_roster) and home_team.get("id"):
            home_roster = await espn_client.get_roster(home_team["id"], season) or home_roster
        if (isinstance(away_roster, Exception) or "roster" not in away_roster) and away_team.get("id"):
            away_roster = await espn_client.get_roster(away_team["id"], season) or away_roster

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

            position_players = [
                p for p in roster_data["roster"]
                if p.get("position", {}).get("abbreviation", "") in ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "OF"]
            ]

            opposing_weak_side = None
            if opposing_pitcher_info and opposing_pitcher_info.get("handedness_splits"):
                opposing_weak_side = opposing_pitcher_info["handedness_splits"].get("weak_side")

            semaphore = asyncio.Semaphore(15)

            async def process_batter(player_entry):
                async with semaphore:
                    player_id = player_entry["person"]["id"]

                    stats_task = mlb_client.get_player_stats(player_id, season)
                    info_task = mlb_client.get_player_info(player_id)
                    arsenal_task = pitch_client.get_batter_pitch_arsenal(player_id, season)
                    barrels_task = pitch_client.get_batter_barrels(player_id, season)
                    batted_ball_task = pitch_client.get_batter_batted_ball_profile(player_id, season)

                    tasks = [stats_task, info_task, arsenal_task, barrels_task, batted_ball_task]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    stats_data, player_info, arsenal, barrel_data, batted_ball_data = results

                    gamelog_data = await mlb_client.get_player_game_log(player_id, season)
                    if isinstance(gamelog_data, Exception) or not gamelog_data:
                        gamelog_data = await espn_client.get_athlete_gamelog(player_id, season)
                        if isinstance(gamelog_data, Exception):
                            gamelog_data = None

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
                            is_rare = pitch.get("usage_pct", 0) < 10
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
                                "is_rare": is_rare,
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
                    }

            import time
            start_time = time.time()
            print(f"Fetching {len(position_players)} batters for {team_name}...")

            tasks = [process_batter(p) for p in position_players]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            candidates = [r for r in results if r is not None and not isinstance(r, Exception)]
            candidates.sort(key=lambda x: x["matchup_score"], reverse=True)
            elapsed = round(time.time() - start_time, 1)
            print(f"Done: {len(candidates)} candidates from {team_name} in {elapsed}s")
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
                    "home": {"name": home_team["name"], "id": home_team["id"], "logo": f"https://www.mlbstatic.com/team-logos/team-cap-on-dark/{home_team['id']}.svg"},
                    "away": {"name": away_team["name"], "id": away_team["id"], "logo": f"https://www.mlbstatic.com/team-logos/team-cap-on-dark/{away_team['id']}.svg"},
                    "home_pitcher": {"name": home_pitcher_entry.get("fullName"), "id": home_pitcher_entry.get("id"), "photo": f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{home_pitcher_entry.get('id')}/headshot/67/current"} if home_pitcher_entry else None,
                    "away_pitcher": {"name": away_pitcher_entry.get("fullName"), "id": away_pitcher_entry.get("id"), "photo": f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{away_pitcher_entry.get('id')}/headshot/67/current"} if away_pitcher_entry else None,
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

    cached = _game_cache.get(game_pk)
    if cached:
        data, timestamp = cached
        if datetime.now() - timestamp < _game_cache_ttl:
            return data
        del _game_cache[game_pk]

    game_infos = await fetch_game_matchups(target_game, target_date, season)
    if not game_infos:
        return {"error": "No matchup data available"}

    _game_cache[game_pk] = (game_infos[0], datetime.now())
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


@app.get("/api/top-matchups")
async def api_top_matchups(game_date: Optional[str] = Query(None)):
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

    all_games = []
    if "dates" in schedule_data and schedule_data["dates"]:
        for day_data in schedule_data["dates"]:
            for game in day_data.get("games", []):
                all_games.append(game)

    all_batters = []
    game_tasks = [fetch_game_matchups(game, target_date, season) for game in all_games]
    game_results = await asyncio.gather(*game_tasks, return_exceptions=True)

    for i, game in enumerate(all_games):
        game_infos = game_results[i]
        if isinstance(game_infos, Exception) or not game_infos:
            continue
        home_team = game["teams"]["home"]["team"]
        away_team = game["teams"]["away"]["team"]
        info = game_infos[0]
        game_pk = game["gamePk"]
        _game_cache[game_pk] = (info, datetime.now())
        for b in info.get("home_top_batters", []):
            b["team"] = home_team["name"]
            b["gamePk"] = game["gamePk"]
            b["game_label"] = f"{away_team['name']} @ {home_team['name']}"
            all_batters.append(b)
        for b in info.get("away_top_batters", []):
            b["team"] = away_team["name"]
            b["gamePk"] = game["gamePk"]
            b["game_label"] = f"{away_team['name']} @ {home_team['name']}"
            all_batters.append(b)

    all_batters.sort(key=lambda x: x["matchup_score"], reverse=True)
    top_3 = all_batters[:3]

    return {"date": target_date.isoformat(), "top_matchups": top_3}


@app.get("/api/pitch-mix/{pitcher_id}")
async def api_pitch_mix(pitcher_id: int):
    season = datetime.now().year
    if datetime.now().month < 3:
        season -= 1
    pitch_mix = await pitch_client.get_pitcher_pitch_mix(pitcher_id, season)
    return {"pitcher_id": pitcher_id, "pitch_mix": pitch_mix or []}


# ── MMA / UFC Endpoints ──


async def fetch_event_matchups_summary(event_data: dict) -> dict:
    event_id = event_data.get("id")
    summary_data = await mma_client.get_event_summary(event_id)
    parsed = mma_client.parse_event_summary(summary_data)

    event_header = parsed.get("header", {})
    fights = parsed.get("fights", [])

    enriched_fights = []
    for fight in fights:
        fighter_a = fight["fighters"][0] if len(fight["fighters"]) > 0 else None
        fighter_b = fight["fighters"][1] if len(fight["fighters"]) > 1 else None

        odds_data = await mma_client.get_competition_odds(
            f"{MMA_CORE_BASE}/events/{event_id}/competitions/{fight['id']}/odds"
        ) if fight.get("id") else {}

        odds = {}
        if odds_data and "items" in odds_data:
            for item in odds_data.get("items", []):
                if item.get("provider", {}).get("name") == "Caesars":
                    odds = item
                    break
            if not odds and len(odds_data.get("items", [])) > 0:
                odds = odds_data["items"][0]

        def odds_for_fighter(fighter_data, odds_info):
            if not odds_info or not fighter_data:
                return None
            for detail in odds_info.get("details", []):
                if detail.get("athlete", {}).get("id") == fighter_data.get("id"):
                    return detail.get("moneyline", detail.get("overUnder"))
            return None

        enriched = {
            "id": fight["id"],
            "description": fight.get("description", ""),
            "status": fight["status"],
            "status_detail": fight.get("status_detail", ""),
            "weight_class": fight.get("weight_class", "TBD"),
            "winner": fight.get("winner"),
        }

        if fighter_a:
            enriched["fighter_a"] = {
                "id": fighter_a["id"],
                "name": fighter_a["name"],
                "headshot": fighter_a.get("headshot", ""),
                "record": fighter_a.get("record", ""),
                "weight": fighter_a.get("weight", ""),
                "height": fighter_a.get("height", ""),
                "stance": fighter_a.get("stance", ""),
                "result": fighter_a.get("result", "PENDING"),
                "odds": odds_for_fighter({"id": fighter_a["id"]}, odds),
                "$ref": fighter_a.get("$ref", ""),
            }

        if fighter_b:
            enriched["fighter_b"] = {
                "id": fighter_b["id"],
                "name": fighter_b["name"],
                "headshot": fighter_b.get("headshot", ""),
                "record": fighter_b.get("record", ""),
                "weight": fighter_b.get("weight", ""),
                "height": fighter_b.get("height", ""),
                "stance": fighter_b.get("stance", ""),
                "result": fighter_b.get("result", "PENDING"),
                "odds": odds_for_fighter({"id": fighter_b["id"]}, odds),
                "$ref": fighter_b.get("$ref", ""),
            }

        enriched_fights.append(enriched)

    return {
        "event_id": int(event_id) if event_id else 0,
        "event_name": event_header.get("name", ""),
        "event_date": event_header.get("date", ""),
        "venue": event_header.get("venue", ""),
        "location": event_header.get("location", ""),
        "status": event_header.get("status", ""),
        "notes_headline": event_header.get("notes_headline", ""),
        "fights": enriched_fights,
    }


@app.get("/api/mma/scoreboard")
async def api_mma_scoreboard(date_str: Optional[str] = Query(None), limit: int = Query(50)):
    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = None

    data = await mma_client.get_scoreboard(target_date, limit)

    events = []
    for evt in data.get("events", []):
        competitions = evt.get("competitions", [])
        main_event = ""
        main_fighters = []
        venue_info = evt.get("venue", {})

        for comp in competitions:
            comp_desc = comp.get("description", "")
            competitors = comp.get("competitors", [])
            if len(competitors) >= 2:
                main_fighters = [
                    competitors[0].get("athlete", {}).get("shortName", competitors[0].get("athlete", {}).get("displayName", "")),
                    competitors[1].get("athlete", {}).get("shortName", competitors[1].get("athlete", {}).get("displayName", "")),
                ]
                main_event = comp_desc
                break

        status_type = evt.get("status", {}).get("type", {}).get("name", "")
        status_detail = evt.get("status", {}).get("detail", "")

        events.append({
            "id": int(evt.get("id", 0)),
            "name": evt.get("name", evt.get("shortName", "")),
            "date": evt.get("date", ""),
            "date_str": evt.get("date", "")[:10] if evt.get("date") else "",
            "venue": venue_info.get("fullName", venue_info.get("name", "TBD")),
            "location": f"{venue_info.get('address', {}).get('city', '')}, {venue_info.get('address', {}).get('state', '')}".strip(", "),
            "status": status_detail or status_type,
            "main_event": main_event,
            "fighters": main_fighters,
            "link": evt.get("link", ""),
        })

    events.sort(key=lambda e: e.get("date", ""), reverse=False)
    return {"events": events}


@app.get("/api/mma/event/{event_id}")
async def api_mma_event(event_id: int, event_date: Optional[str] = Query(None)):
    target_date = None
    if event_date:
        try:
            target_date = datetime.strptime(event_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = None
    else:
        target_date = date.today()

    scoreboard = await mma_client.get_scoreboard(target_date, limit=100)

    target_event = None
    events_data = scoreboard.get("events", [])
    for evt in events_data:
        eid = evt.get("id")
        try:
            if int(eid) == event_id:
                target_event = evt
                break
        except (ValueError, TypeError):
            if str(eid) == str(event_id):
                target_event = evt
                break

    if not target_event and target_date != date.today():
        scoreboard = await mma_client.get_scoreboard(None, limit=100)
        events_data = scoreboard.get("events", [])
        for evt in events_data:
            try:
                if int(evt.get("id", 0)) == event_id:
                    target_event = evt
                    break
            except (ValueError, TypeError):
                if str(evt.get("id", "")) == str(event_id):
                    target_event = evt
                    break

    if not target_event:
        return {"error": "Event not found", "event_id": event_id}

    event_name = target_event.get("name", "")
    event_date_str = target_event.get("date", "")
    venue_info = target_event.get("venue", {})
    status_info = target_event.get("status", {})

    athlete_ids = []
    fights = []
    for comp in target_event.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        fight = {
            "id": comp.get("id", ""),
            "description": comp.get("description", ""),
            "weight_class": comp.get("type", {}).get("text") or comp.get("type", {}).get("abbreviation", "TBD"),
            "status": comp.get("status", {}).get("type", {}).get("name", "SCHEDULED"),
            "status_detail": comp.get("status", {}).get("type", {}).get("detail", ""),
            "winner": None,
        }

        fighters = []
        for c in competitors:
            athlete = c.get("athlete", {})
            fid = c.get("id") or athlete.get("id")

            record_summary = ""
            records = c.get("records", [])
            for rec in records:
                if rec.get("type") == "total":
                    record_summary = rec.get("summary", "")
                    break

            result = "PENDING"
            if c.get("winner"):
                result = "WINNER"
                fight["winner"] = fid

            fighters.append({
                "id": fid,
                "name": athlete.get("displayName") or athlete.get("fullName", "TBD"),
                "shortName": athlete.get("shortName", ""),
                "record": record_summary,
                "result": result,
                "order": c.get("order", 0),
                "flag": athlete.get("flag", {}).get("href", ""),
            })
            if fid:
                athlete_ids.append(fid)

        fight["fighter_a"] = fighters[0] if len(fighters) > 0 else None
        fight["fighter_b"] = fighters[1] if len(fighters) > 1 else None
        fights.append(fight)

    headshot_map = {}
    if athlete_ids:
        unique_ids = list(set(athlete_ids))
        tasks = []
        semaphore = asyncio.Semaphore(15)

        async def fetch_athlete_photo(aid):
            async with semaphore:
                try:
                    ref = f"{MMA_CORE_BASE}/athletes/{aid}"
                    data = await mma_client.get_athlete(ref)
                    if data and "headshot" in data:
                        return aid, data["headshot"].get("href", "") if isinstance(data["headshot"], dict) else ""
                    return aid, f"https://a.espncdn.com/i/headshots/mma/players/full/{aid}.png"
                except Exception:
                    return aid, f"https://a.espncdn.com/i/headshots/mma/players/full/{aid}.png"

        photo_results = await asyncio.gather(*[fetch_athlete_photo(aid) for aid in unique_ids], return_exceptions=True)
        for result in photo_results:
            if isinstance(result, tuple) and len(result) == 2:
                headshot_map[result[0]] = result[1]

    for fight in fights:
        for key in ("fighter_a", "fighter_b"):
            f = fight.get(key)
            if f and f.get("id"):
                f["headshot"] = headshot_map.get(f["id"], f"https://a.espncdn.com/i/headshots/mma/players/full/{f['id']}.png")

    return {
        "event_id": event_id,
        "event_name": event_name,
        "event_date": event_date_str,
        "venue": venue_info.get("fullName", venue_info.get("name", "TBD")),
        "location": f"{venue_info.get('address', {}).get('city', '')}, {venue_info.get('address', {}).get('country', '')}".strip(", "),
        "status": status_info.get("type", {}).get("detail", status_info.get("type", {}).get("name", "")),
        "fights": fights,
    }


@app.get("/api/mma/fighter/{fighter_id}")
async def api_mma_fighter(fighter_id: int):
    ref = f"{MMA_CORE_BASE}/athletes/{fighter_id}"
    data = await mma_client.get_fighter_detail(ref)

    if not data:
        return {"error": "Fighter not found", "fighter_id": fighter_id}

    return data


@app.get("/api/mma/rankings")
async def api_mma_rankings():
    data = await mma_client.get_rankings()

    rankings_list = []
    for ranking in data.get("rankings", []):
        items = []
        for rank_item in ranking.get("ranks", []):
            items.append({
                "rank": rank_item.get("current", ""),
                "name": rank_item.get("name", ""),
                "headshot": rank_item.get("headshot", ""),
                "record": rank_item.get("record", ""),
                "id": rank_item.get("athlete_id"),
            })
        if items:
            rankings_list.append({
                "weightClass": ranking.get("name", ranking.get("shortName", "")),
                "fighters": items,
            })

    return {"rankings": rankings_list}


@app.get("/api/mma/news")
async def api_mma_news(limit: int = Query(15)):
    data = await mma_client.get_news(limit)

    articles = []
    for article in data.get("articles", [])[:limit]:
        articles.append({
            "headline": article.get("headline", ""),
            "description": article.get("description", ""),
            "link": article.get("links", {}).get("web", {}).get("href", ""),
            "published": article.get("published", ""),
            "image": "",
        })
        images = article.get("images", [])
        if images:
            articles[-1]["image"] = images[0].get("url", "")

    return {"articles": articles, "count": len(articles)}


MMA_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"
