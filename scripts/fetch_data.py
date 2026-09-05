#!/usr/bin/env python3
"""
Fetches Premier League data and writes data/data.json for the dashboard.

Sources:
  - Fantasy Premier League API (free, no key): teams, fixtures, results,
    player stats, mini-league standings
  - The Odds API (free key): Sky Bet + Paddy Power match odds

Env vars (set as GitHub secret / variable):
  ODDS_API_KEY    - from the-odds-api.com (optional; odds skipped if missing)
  FPL_LEAGUE_ID   - your FPL classic mini-league ID (optional)
"""
import json, os, sys, urllib.request, datetime, re

FPL = "https://fantasy.premierleague.com/api"
DEFAULT_LEAGUE_ID = "1022213"   # "Devils in the Sky" - override with FPL_LEAGUE_ID if needed
UA = {"User-Agent": "Mozilla/5.0 (pl-predictor hobby project)"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

# Club names differ between the FPL feed ("Man City") and the odds feed
# ("Manchester City"). Fuzzy word-overlap matching is NOT safe: "Man City" and
# "Manchester United" share "Manchester"; "Coventry City" and "Manchester City"
# share "City". That silently paired the wrong fixtures. Exact canonical keys only.
CANON = {
    "arsenal": "arsenal",
    "aston villa": "villa",
    "bournemouth": "bournemouth", "afc bournemouth": "bournemouth",
    "brentford": "brentford",
    "brighton": "brighton", "brighton and hove albion": "brighton",
    "burnley": "burnley",
    "chelsea": "chelsea",
    "coventry": "coventry", "coventry city": "coventry",
    "crystal palace": "palace",
    "everton": "everton",
    "fulham": "fulham",
    "hull": "hull", "hull city": "hull",
    "ipswich": "ipswich", "ipswich town": "ipswich",
    "leeds": "leeds", "leeds united": "leeds",
    "leicester": "leicester", "leicester city": "leicester",
    "liverpool": "liverpool",
    "luton": "luton", "luton town": "luton",
    "man city": "mancity", "manchester city": "mancity",
    "man utd": "manutd", "man united": "manutd", "manchester united": "manutd",
    "newcastle": "newcastle", "newcastle united": "newcastle",
    "nottm forest": "forest", "notts forest": "forest", "nottingham forest": "forest",
    "sheffield utd": "sheffutd", "sheffield united": "sheffutd",
    "southampton": "southampton",
    "spurs": "spurs", "tottenham": "spurs", "tottenham hotspur": "spurs",
    "sunderland": "sunderland",
    "west brom": "westbrom", "west bromwich albion": "westbrom",
    "west ham": "westham", "west ham united": "westham",
    "wolves": "wolves", "wolverhampton wanderers": "wolves",
}

def canon(name):
    n = (name or "").lower().replace("&", "and")
    n = re.sub(r"\b(fc|afc)\b", " ", n)
    n = re.sub(r"[^a-z ]", "", n)
    n = " ".join(n.split())
    return CANON.get(n, n)

def main():
    out = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    # ---------- FPL core ----------
    boot = get(f"{FPL}/bootstrap-static/")
    fpl_fixtures = get(f"{FPL}/fixtures/")

    teams = {}
    for t in boot["teams"]:
        teams[t["id"]] = {"name": t["name"], "code": t["code"],
                          "short": t.get("short_name", "")}
    out["teams"] = teams

    # current gameweek
    cur = next((e["id"] for e in boot["events"] if e.get("is_current")), None)
    if cur is None:
        cur = next((e["id"] for e in boot["events"] if e.get("is_next")), 1)
    out["currentGW"] = cur

    fixtures = []
    for f in fpl_fixtures:
        if not f.get("event"):
            continue  # unscheduled
        # FPL only sets `finished` once bonus points are confirmed, hours after
        # the whistle. `finished_provisional` flips at full time and the score
        # never changes after it, so treat either as done - but only if we
        # actually have a scoreline.
        done = bool(f["finished"] or f.get("finished_provisional"))
        if f["team_h_score"] is None or f["team_a_score"] is None:
            done = False
        fixtures.append({
            "id": f["id"], "gw": f["event"], "kickoff": f["kickoff_time"],
            "h": f["team_h"], "a": f["team_a"],
            "hs": f["team_h_score"], "as": f["team_a_score"],
            "finished": done,
        })
    out["fixtures"] = fixtures

    # ---------- form (last 5 finished per team) ----------
    form = {tid: [] for tid in teams}
    for f in sorted([x for x in fixtures if x["finished"]], key=lambda x: x["kickoff"] or ""):
        hs, as_ = f["hs"], f["as"]
        if hs is None:
            continue
        form[f["h"]].append("W" if hs > as_ else "D" if hs == as_ else "L")
        form[f["a"]].append("W" if as_ > hs else "D" if hs == as_ else "L")
    out["form"] = {tid: v[-5:] for tid, v in form.items()}

    # ---------- player stats ----------
    els = boot["elements"]
    def photo(e): return e["photo"].split(".")[0]
    def top(key, n=8, fmt=lambda v: v):
        rows = sorted(els, key=lambda e: -float(e.get(key) or 0))[:n]
        return [{"n": e["web_name"], "t": e["team"], "v": fmt(e.get(key)), "p": photo(e)}
                for e in rows if float(e.get(key) or 0) > 0]
    stats = {
        "scorers": top("goals_scored"),
        "assists": top("assists"),
        "cleanSheets": [r for r in top("clean_sheets", 20)
                        if next(e for e in els if e["web_name"] == r["n"] and e["team"] == r["t"])
                        ["element_type"] in (1, 2)][:8],
        "inForm": top("form", 8),
        # works before a ball is kicked, unlike goals/assists
        "mostPicked": [{"n": e["web_name"], "t": e["team"],
                        "v": f'{e["selected_by_percent"]}%', "p": photo(e)}
                       for e in sorted(els, key=lambda e: -float(e.get("selected_by_percent") or 0))[:8]],
    }
    # player to watch: best form; pre-season (no form yet) fall back to most-selected
    watch_pool = sorted(els, key=lambda e: (-float(e.get("form") or 0), -int(e.get("total_points") or 0)))
    preseason = not watch_pool or float(watch_pool[0].get("form") or 0) <= 0
    if preseason:
        watch_pool = sorted(els, key=lambda e: -float(e.get("selected_by_percent") or 0))
    if watch_pool:
        w = watch_pool[0]
        nxt = next((f for f in fixtures if not f["finished"] and w["team"] in (f["h"], f["a"])), None)
        opp = ""
        if nxt:
            opp_id = nxt["a"] if nxt["h"] == w["team"] else nxt["h"]
            opp = ("vs " if nxt["h"] == w["team"] else "at ") + teams[opp_id]["name"]
        stats["watch"] = {"n": w["web_name"], "t": w["team"], "p": photo(w),
                          "form": w.get("form"), "pts": w.get("total_points"),
                          "why": (f"Most-picked player in FPL ({w.get('selected_by_percent')}% of teams). Next: {opp}"
                                  if preseason else
                                  f"Hottest player in the league right now. Next: {opp}") if opp else ""}
    out["stats"] = stats

    # ---------- odds ----------
    # Runs hourly now so results land quickly, but the odds API allows only 500
    # calls a month. Refresh odds every 6th hour and carry the previous ones over
    # otherwise - and also on failure, so a bad response never wipes the board.
    previous = {}
    try:
        with open("data/data.json") as fh:
            previous = json.load(fh)
    except Exception:
        pass
    carried = previous.get("odds") or {}

    odds_out = {}
    key = os.environ.get("ODDS_API_KEY", "").strip()
    hour = datetime.datetime.now(datetime.timezone.utc).hour
    refresh_odds = (hour % 6 == 0) or os.environ.get("ODDS_FORCE") == "1"
    if key and not refresh_odds:
        print(f"Hour {hour:02d} - keeping existing odds ({len(carried)} fixtures) to save quota")
        odds_out = carried
    elif key:
        try:
            url = (f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/"
                   f"?apiKey={key}&regions=uk&markets=h2h&oddsFormat=decimal")
            events = get(url)
            unmatched = []
            for ev in events:
                eh, ea = canon(ev["home_team"]), canon(ev["away_team"])
                ek = ev.get("commence_time")
                match = None
                for f in fixtures:
                    if f["finished"] or not f["kickoff"]:
                        continue
                    if canon(teams[f["h"]]["name"]) != eh or canon(teams[f["a"]]["name"]) != ea:
                        continue
                    if ek:
                        try:
                            d1 = datetime.datetime.fromisoformat(ek.replace("Z", "+00:00"))
                            d2 = datetime.datetime.fromisoformat(f["kickoff"].replace("Z", "+00:00"))
                            if abs((d1 - d2).total_seconds()) > 6 * 3600:
                                continue
                        except Exception:
                            pass
                    match = f
                    break
                if not match:
                    unmatched.append(f'{ev["home_team"]} v {ev["away_team"]}')
                    continue
                entry = {}
                for bk in ev.get("bookmakers", []):
                    bkey = bk["key"].lower()
                    tag = "skybet" if "sky" in bkey else "paddypower" if "paddy" in bkey else None
                    if not tag:
                        continue
                    mkt = next((m for m in bk.get("markets", []) if m["key"] == "h2h"), None)
                    if not mkt:
                        continue
                    prices = {o["name"]: o["price"] for o in mkt["outcomes"]}
                    h = prices.get(ev["home_team"]); d = prices.get("Draw"); a = prices.get(ev["away_team"])
                    if h and d and a:
                        entry[tag] = [f"{h:.2f}", f"{d:.2f}", f"{a:.2f}"]
                if entry:
                    odds_out[str(match["id"])] = entry
            print(f"Odds matched for {len(odds_out)} fixtures")
            if unmatched:
                print("Odds events with no fixture match: " + "; ".join(unmatched), file=sys.stderr)
            if not odds_out and carried:
                print("Odds call returned nothing - keeping previous odds")
                odds_out = carried
        except Exception as e:
            print(f"WARNING: odds fetch failed, keeping previous odds: {e}", file=sys.stderr)
            odds_out = carried
    else:
        print("No ODDS_API_KEY set - skipping odds")
        odds_out = carried
    out["odds"] = odds_out

    # ---------- FPL mini-league ----------
    out["fplLeague"] = None
    lid = os.environ.get("FPL_LEAGUE_ID", "").strip() or DEFAULT_LEAGUE_ID
    if lid:
        try:
            lg = get(f"{FPL}/leagues-classic/{lid}/standings/")
            rows = [{"entry": s["entry"], "team": s["entry_name"],
                     "player": s["player_name"], "total": s["total"], "rank": s["rank"]}
                    for s in lg["standings"]["results"]]
            # Before GW1 is scored, members appear under new_entries with no points yet.
            known = {r["entry"] for r in rows}
            for s in lg.get("new_entries", {}).get("results", []):
                if s["entry"] not in known:
                    rows.append({"entry": s["entry"], "team": s["entry_name"],
                                 "player": f'{s["player_first_name"]} {s["player_last_name"]}',
                                 "total": 0, "rank": None})
            out["fplLeague"] = {"name": lg["league"]["name"], "standings": rows}
            print(f"League '{lg['league']['name']}': {len(rows)} teams")
        except Exception as e:
            print(f"WARNING: league fetch failed: {e}", file=sys.stderr)
    else:
        print("No FPL_LEAGUE_ID set - skipping mini-league")

    os.makedirs("data", exist_ok=True)
    with open("data/data.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"Wrote data/data.json ({len(fixtures)} fixtures, GW{cur})")

if __name__ == "__main__":
    main()
