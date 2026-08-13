"""
Bridge-sim heartbeat script.

Runs a persistent loop (NOT a cron job — cron can't go sub-minute) that
ticks the Reactor bars up and occasionally spikes/raises Life Support heat,
writing state straight into the shared Google Sheet via batched API calls.

SETUP (one-time):
  1. pip install gspread google-auth
  2. In Google Cloud Console: create a project -> enable "Google Sheets API"
     -> create a Service Account -> download its JSON key as credentials.json
     (keep this file next to this script, and out of version control).
  3. Open your Sheet, click Share, and share it with the service account's
     email address (looks like xxx@xxx.iam.gserviceaccount.com) as Editor.
  4. Fill in SHEET_ID below (the long id in the sheet's URL).

SHEET LAYOUT THIS SCRIPT EXPECTS:

  "Reactor" worksheet:
    A            B            C
    Bar name     Value(0-100) Reset(TRUE/FALSE checkbox)
    Bar 1        0            FALSE
    Bar 2        0            FALSE
    ...(5 rows)

  "Life Support" worksheet:
    B2 = current heat value
    D2:D2 = scrolling history log (script appends a new row each tick,
             point a native line chart at column D for the live graph)
    F2 = "CORRUPT" flag cell — script occasionally writes CORRUPTED into it
    G2 = reboot checkbox — player sets TRUE after clearing F2

  "Pilot" worksheet (auto-created with headers on first run if missing):
    Heading/speed matching under time pressure. B2/B3 = target heading (0-359)
    and speed (1-9), which the pilot must match by typing into C2/C3 before
    the countdown in B5 hits zero. B6 = running score. This is one option
    of a few discussed for the Pilot seat — swap tick_pilot() out for a
    different minigame (e.g. an obstacle-dodge grid) if this one doesn't
    feel right at the table.

Tune the constants below to taste; nothing here is precious.
"""

import random
import time
import datetime

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# ---- config ---------------------------------------------------------------

SHEET_ID = "1d55txSbBoXS4qjJ28L4NK9nylOJFoWGq0AReWvOyn6c"
TICK_SECONDS = 2                # how often the loop runs
REACTOR_DELTA_RANGE = (-2, 5)   # per-tick change per bar; can go up OR down,
                                 # each bar draws independently so they drift apart
REACTOR_MELTDOWN_VALUE = 100
HEAT_BASE_RISE = 1.5            # steady heat creep per tick
HEAT_SPIKE_CHANCE = 0.03        # per-tick chance of a spike
HEAT_SPIKE_AMOUNT = (15, 30)    # random range added on a spike
CORRUPT_CHANCE = 0.02           # per-tick chance of corrupting life support
CORRUPT_TEXT = "CORRUPTED"
 
PILOT_ROUND_SECONDS = 10        # time allowed per heading/speed target
HEADING_TOLERANCE = 15          # degrees, wraparound-aware
SPEED_TOLERANCE = 1

MAX_HISTORY = 250

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# ---- setup ------------------------------------------------------------

creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
reactor_ws = sh.worksheet("Reactor")
life_ws = sh.worksheet("Life Support")
pilot_ws = sh.worksheet("Pilot")

NUM_BARS = 5

REACTOR_VALUES_RANGE = f"Reactor!B2:C{1 + NUM_BARS}"
REACTOR_RESET_RANGE = f"Reactor!C2:C{1 + NUM_BARS}"

def retry(fn, *args, retries=5, **kwargs):
    """
    Ensure mass editing across sheets doesn't crash loop
    """
    delay = 1
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            if attempt == retries - 1:
                raise
            print(f"API error ({e}), retrying...")
            time.sleep(delay)
            delay *= 2

def ensure_pilot_sheet(state):
    """Set up headers and a first target if the sheet is freshly created/empty."""
    existing = pilot_ws.acell("B2").value
    if existing not in (None, ""):
        state["pilot_heading"] = int(float(existing))
        state["pilot_speed"] = int(float(pilot_ws.acell("B3").value or 0))
        state["pilot_score"] = float(pilot_ws.acell("B6").value or 0)
        state["pilot_time_left"] = float(pilot_ws.acell("B5").value or PILOT_ROUND_SECONDS)
        return
 
    state["pilot_heading"] = random.randint(0, 359)
    state["pilot_speed"] = random.randint(1, 9)
    state["pilot_score"] = 0
    state["pilot_time_left"] = PILOT_ROUND_SECONDS
 
    retry(pilot_ws.batch_update, [
        {"range": "A1:D1", "values": [["Metric", "Target", "Your Input", "Status"]]},
        {"range": "A2:D2", "values": [["Heading (0-359)", state["pilot_heading"], "", ""]]},
        {"range": "A3:D3", "values": [["Speed (1-9)", state["pilot_speed"], "", ""]]},
        {"range": "A5:B5", "values": [["Time Left (s)", state["pilot_time_left"]]]},
        {"range": "A6:B6", "values": [["Score", state["pilot_score"]]]},
    ])


def load_inital_state():
    """One-time reads at startup: current reactor bar values, current heat,
    and where the Life Support history log currently ends."""
    state = {}
 
    reactor_vals = retry(reactor_ws.get, f"B2:C{1 + NUM_BARS}")
    bars = []
    for i in range(NUM_BARS):
        row = reactor_vals[i] if i < len(reactor_vals) else []
        val = float(row[0]) if len(row) > 0 and row[0] != "" else 0
        bars.append(val)
    state["reactor_bars"] = bars
 
    heat_cell = retry(life_ws.acell, "B2").value
    state["heat"] = float(heat_cell) if heat_cell not in (None, "") else 0
 
    corrupt_cell = retry(life_ws.acell, "F2").value
    state["corrupted"] = bool(corrupt_cell)
 
    # Find the next empty row in the Life Support history column (D),
    # so future ticks can write directly without an append lookup.
    history_col = retry(life_ws.col_values, 4)
    state["history_row"] = max(2, len(history_col) + 1)
 
    ensure_pilot_sheet(state)
 
    return state


def tick(state):
    """One full tick: one batched read of player-editable cells, one
    batched write of every worksheet's updates."""
 
    # ---- single batched read across all three worksheets ----
    ranges = [
        REACTOR_RESET_RANGE,        # which bars the player reset this tick
        "Life Support!F2",          # corrupt flag (player clears this)
        "Life Support!G2",          # reboot checkbox (player sets this)
        "Pilot!C2:C3",              # player's typed heading/speed
    ]
    result = retry(sh.values_batch_get, ranges)
 
    reset_block = None
    corrupt_flag = None
    reboot_flag = None
    pilot_input = None
    for vr in result["valueRanges"]:
        rng = vr["range"]
        vals = vr.get("values", [])
        if rng.startswith("Reactor!"):
            reset_block = vals
        elif rng.startswith("Life Support!F2"):
            corrupt_flag = vals[0][0] if vals and vals[0] else ""
        elif rng.startswith("Life Support!G2"):
            reboot_flag = vals[0][0] if vals and vals[0] else ""
        elif rng.startswith("Pilot!"):
            pilot_input = vals
 
    writes = []  # list of {"range": "Sheet!A1", "values": [[...]]}
 
    # ---- Reactor ----
    for i in range(NUM_BARS):
        row_num = 2 + i
        reset = False
        if reset_block and i < len(reset_block) and reset_block[i]:
            reset = str(reset_block[i][0]).upper() == "TRUE"
        if reset:
            state["reactor_bars"][i] = 0
            writes.append({"range": f"Reactor!B{row_num}:C{row_num}", "values": [[0, False]]})
        else:
            delta = random.uniform(*REACTOR_DELTA_RANGE)
            new_val = min(REACTOR_MELTDOWN_VALUE, max(0, state["reactor_bars"][i] + delta))
            state["reactor_bars"][i] = new_val
            writes.append({"range": f"Reactor!B{row_num}", "values": [[round(new_val, 1)]]})
            if new_val >= REACTOR_MELTDOWN_VALUE:
                print(f"!! Reactor bar {i + 1} hit meltdown threshold")
 
    # ---- Life Support ----
    corrupted = bool(corrupt_flag)
    rebooted = str(reboot_flag).upper() == "TRUE"
 
    if rebooted and not corrupted:
        state["heat"] = 20
        writes.append({"range": "Life Support!G2", "values": [[False]]})
        print(">> Life Support rebooted successfully")
    else:
        state["heat"] += HEAT_BASE_RISE
        if random.random() < HEAT_SPIKE_CHANCE:
            state["heat"] += random.uniform(*HEAT_SPIKE_AMOUNT)
            print("!! Heat spike")
 
    if not corrupted and random.random() < CORRUPT_CHANCE:
        writes.append({"range": "Life Support!F2", "values": [[CORRUPT_TEXT]]})
        print("!! Life Support data corrupted — players must clear F2 then check G2")
 
    writes.append({"range": "Life Support!B2", "values": [[round(state["heat"], 1)]]})
 
    if state["history_row"] > MAX_HISTORY:
        state["history_row"] = 2  # wrap around instead of growing forever
    writes.append({
        "range": f"Life Support!D{state['history_row']}",
        "values": [[f"{datetime.datetime.now().isoformat(timespec='seconds')}  {round(state['heat'], 1)}"]],
    })
    state["history_row"] += 1
 
    # ---- Pilot ----
    player_heading = 0
    player_speed = 0
    if pilot_input:
        try:
            player_heading = float(pilot_input[0][0]) if len(pilot_input) > 0 and pilot_input[0] else 0
        except (ValueError, IndexError):
            pass
        try:
            player_speed = float(pilot_input[1][0]) if len(pilot_input) > 1 and pilot_input[1] else 0
        except (ValueError, IndexError):
            pass
 
    state["pilot_time_left"] -= TICK_SECONDS
 
    if state["pilot_time_left"] <= 0:
        hit = (_heading_diff(player_heading, state["pilot_heading"]) <= HEADING_TOLERANCE
               and abs(player_speed - state["pilot_speed"]) <= SPEED_TOLERANCE)
        status = "HIT" if hit else "MISS"
        if hit:
            state["pilot_score"] += 1
 
        state["pilot_heading"] = random.randint(0, 359)
        state["pilot_speed"] = random.randint(1, 9)
        state["pilot_time_left"] = PILOT_ROUND_SECONDS
 
        writes.extend([
            {"range": "Pilot!B2", "values": [[state["pilot_heading"]]]},
            {"range": "Pilot!B3", "values": [[state["pilot_speed"]]]},
            {"range": "Pilot!C2:C3", "values": [[""], [""]]},
            {"range": "Pilot!D2", "values": [[status]]},
            {"range": "Pilot!B5", "values": [[state["pilot_time_left"]]]},
        ])
        print(f"Pilot round result: {status} (score {state['pilot_score']})")
    else:
        writes.append({"range": "Pilot!B5", "values": [[round(state["pilot_time_left"], 1)]]})
 
    # ---- single batched write across all three worksheets ----
    retry(sh.values_batch_update, {
        "valueInputOption": "USER_ENTERED",
        "data": writes,
    })
 
 
def _heading_diff(a, b):
    """Smallest angular distance between two headings, wraparound-aware."""
    d = abs(a - b) % 360
    return min(d, 360 - d)
 
 
def main():
    print("Heartbeat running. Ctrl+C to stop.")
    state = load_inital_state()
    while True:
        try:
            tick(state)
        except Exception as e:
            # keep the loop alive through transient API hiccups
            print("tick error:", e)
        time.sleep(TICK_SECONDS)
 
 
if __name__ == "__main__":
    main()