"""
SETUP (one-time):
  1. pip install gspread google-auth
  2. In Google Cloud Console: create a project -> enable "Google Sheets API"
     -> create a Service Account -> download its JSON key as credentials.json
     (keep this file next to this script, and out of version control).
  3. Open your Sheet, click Share, and share it with the service account's
     email address (looks like xxx@xxx.iam.gserviceaccount.com) as Editor.
  4. Fill in SHEET_ID below (the long id in the sheet's URL).

  Worksheet (tab) names must exactly match SHEET_NAMES below. Every batched
  range string in this file is built from those names, so if you rename a
  tab, update SHEET_NAMES and nothing else needs to change.
"""

import random
import time
import string

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# ---- config ---------------------------------------------------------------

SHEET_ID = "1d55txSbBoXS4qjJ28L4NK9nylOJFoWGq0AReWvOyn6c"
TICK_SECONDS = 2                # how often the loop runs

REACTOR_SHEET = "Reactor"
LIFE_SHEET = "Life Support Computer"
PILOT_SHEET = "Cockpit"

# Reactor
REACTOR_DELTA_RANGE = (0, 5)    # per-tick change per bar; can go up OR down,
                                 # each bar draws independently so they drift apart
REACTOR_MELTDOWN_VALUE = 100
NUM_BARS = 5
REACTOR_FIRST_ROW = 3
REACTOR_LAST_ROW = REACTOR_FIRST_ROW + NUM_BARS - 1

REACTOR_VALUES_RANGE = f"{REACTOR_SHEET}!B{REACTOR_FIRST_ROW}:B{REACTOR_LAST_ROW}"
REACTOR_ADJUST_RANGE = f"{REACTOR_SHEET}!H5:H{4 + NUM_BARS}"

# Life support

CORRUPT_CHANCE = 0.7
CORRUPT_TEXT = "CORRUPTED"
GOOD_DATA = "GOOD DATA"
MIN_GOOD_DATA_FOR_REBOOT = 8
TEMP_RISE_PER_CORRUPTED = 2.5
STABILITY_LOSS_PER_CORRUPTED = 2.0
TEMP_FALL_WHEN_CLEAN = 3.0       # passive cooldown per tick while both disks are clean
STABILITY_GAIN_WHEN_CLEAN = 4.0  # passive stabilization per tick while both disks are clean
MAX_TEMPERATURE = 100
MAX_STABILITY = 100
MIN_STABILITY = 0

MAX_HISTORY = 20

LIFE_TEMP_DISK_RANGE = f"{LIFE_SHEET}!H2:K30"
LIFE_STABILITY_DISK_RANGE = f"{LIFE_SHEET}!L2:O30"
LIFE_REBOOT_TEMP = f"{LIFE_SHEET}!F2"
LIFE_REBOOT_STABILITY = f"{LIFE_SHEET}!G2"
LIFE_TEMP_CELL = f"{LIFE_SHEET}!B2"
LIFE_STABILITY_CELL = f"{LIFE_SHEET}!C2"
LIFE_STATUS_CELL = f"{LIFE_SHEET}!B5"

# Disk grids that corruption can be injected into (see LIFE_TEMP_DISK_RANGE /
# LIFE_STABILITY_DISK_RANGE below for the matching read range).
DISK_FIRST_ROW = 2
DISK_LAST_ROW = 30
TEMP_DISK_COLS = ["H", "I", "J", "K"]
STABILITY_DISK_COLS = ["L", "M", "N", "O"]

# Pilot
PILOT_ROUND_SECONDS = 8        # time allowed per heading/speed target
HEADING_TOLERANCE = 15          # degrees, wraparound-aware
SPEED_TOLERANCE = 1

PILOT_INPUT_RANGE = f"{PILOT_SHEET}!C2:C3"
PILOT_HEADING_CELL = f"{PILOT_SHEET}!B2"
PILOT_SPEED_CELL = f"{PILOT_SHEET}!B3"
PILOT_INPUT_CLEAR_RANGE = f"{PILOT_SHEET}!C2:C3"
PILOT_STATUS_CELL = f"{PILOT_SHEET}!D2"
PILOT_TIME_CELL = f"{PILOT_SHEET}!B5"
SILAS_HULL_INTEGRITY_CELL = f"{PILOT_SHEET}!E3"

# --- Repair (Silas) config ---
REPAIR_CODE_LENGTH = 5
REPAIR_INTERVAL_SECONDS = 12       # how long a code stays valid before rotating
REPAIR_HEAL_MIN = 3
REPAIR_HEAL_MAX = 7
MAX_HULL_INTEGRITY = 100
MIN_HULL_INTEGRITY = 0
REPAIR_CODE_CHARS = string.ascii_uppercase + string.digits

REPAIR_CODE_CELL = f"{PILOT_SHEET}!C9"     # displays the current code to match
REPAIR_INPUT_RANGE = f"{PILOT_SHEET}!D9"   # player types their guess here
REPAIR_STATUS_CELL = f"{PILOT_SHEET}!C11"  # shows "REPAIR SUCCESSFUL" or "CODE EXPIRED"
REPAIR_TIME_CELL = f"{PILOT_SHEET}!D11"    # shows how many seconds remain before the code rotates

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ---- connection -------------------------------------------------------

def connect():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return (
        sh,
        sh.worksheet(REACTOR_SHEET),
        sh.worksheet(LIFE_SHEET),
        sh.worksheet(PILOT_SHEET),
    )

def retry(fn, *args, retries=5, **kwargs):
    """Ensure mass editing across sheets doesn't crash loop."""
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

def _heading_diff(a, b):
    """Smallest angular distance between two headings, wraparound-aware."""
    d = abs(a - b) % 360
    return min(d, 360 - d)

def count_disk_data(block):
    """Return (corrupted_count, good_data_count) for a Sheets range."""
    corrupted_count = 0
    good_data_count = 0

    for row in block or []:
        for cell in row:
            value = str(cell).strip().upper()

            if value == CORRUPT_TEXT:
                corrupted_count += 1
            elif value == GOOD_DATA:
                good_data_count += 1

    return corrupted_count, good_data_count

def maybe_corrupt_disk(cols, chance):
    """Roll CORRUPT_CHANCE; on success return a write that corrupts one random
    cell in the given disk's column range. Returns None on a miss."""
    if random.random() >= chance:
        return None
 
    col = random.choice(cols)
    row = random.randint(DISK_FIRST_ROW, DISK_LAST_ROW)
    return {"range": f"{LIFE_SHEET}!{col}{row}", "values": [[CORRUPT_TEXT]]}


# ---- startup ------------------------------------------------------------

def ensure_pilot_sheet(pilot_ws, state):
    """Set up headers and a first target if the sheet is freshly created/empty."""
    existing = pilot_ws.acell("B2").value
    if existing not in (None, ""):
        state["pilot_heading"] = int(float(existing))
        state["pilot_speed"] = int(float(pilot_ws.acell("B3").value or 0))
        state["pilot_score"] = float(pilot_ws.acell("B6").value or 100)
        state["pilot_time_left"] = float(pilot_ws.acell("B5").value or PILOT_ROUND_SECONDS)
        return

    state["pilot_heading"] = random.randint(0, 359)
    state["pilot_speed"] = random.randint(1, 9)
    state["pilot_score"] = 100
    state["pilot_time_left"] = PILOT_ROUND_SECONDS

    retry(pilot_ws.batch_update, [
        {"range": "A1:D1", "values": [["Metric", "Target", "Your Input", "Status"]]},
        {"range": "A2:D2", "values": [["Heading (0-359)", state["pilot_heading"], "", ""]]},
        {"range": "A3:D3", "values": [["Speed (1-9)", state["pilot_speed"], "", ""]]},
        {"range": "A5:B5", "values": [["Time Left (s)", state["pilot_time_left"]]]},
        {"range": "A6:B6", "values": [["Score", state["pilot_score"]]]},
    ])

def load_reactor_bars(reactor_ws):
    reactor_vals = retry(reactor_ws.get, f"B{REACTOR_FIRST_ROW}:B{REACTOR_LAST_ROW}")
    bars = []
    for i in range(NUM_BARS):
        row = reactor_vals[i] if i < len(reactor_vals) else []
        val = float(row[0]) if len(row) > 0 and row[0] != "" else 0
        bars.append(val)
    return bars

def load_life_support(life_ws):
    temp_cell = retry(life_ws.acell, "B2").value
    temperature = float(temp_cell) if temp_cell not in (None, "") else 0.0

    stability_cell = retry(life_ws.acell, "C2").value
    stability = float(stability_cell) if stability_cell not in (None, "") else 100.0

    # Find the next empty row in the Life Support history column (D),
    # so future ticks can write directly without an append lookup.
    history_col = retry(life_ws.col_values, 4)
    history_row = max(2, len(history_col) + 1)

    return temperature, stability, history_row

def load_initial_state(reactor_ws, life_ws, pilot_ws):
    """One-time reads at startup: current reactor bar values, current heat,
    and where the Life Support history log currently ends."""
    state = {}

    state["reactor_bars"] = load_reactor_bars(reactor_ws)
    state["temperature"], state["stability"], state["history_row"] = load_life_support(life_ws)

    ensure_pilot_sheet(pilot_ws, state)

    return state


# ---- tick: read ------------------------------------------------------------

def read_tick_inputs(sh):
    ranges = [
        REACTOR_ADJUST_RANGE,
        LIFE_REBOOT_TEMP,
        LIFE_REBOOT_STABILITY,
        LIFE_TEMP_DISK_RANGE,
        LIFE_STABILITY_DISK_RANGE,
        PILOT_INPUT_RANGE,
        REPAIR_INPUT_RANGE,
    ]
    result = retry(sh.values_batch_get, ranges)
    value_ranges = result["valueRanges"]

    adjust_vals = value_ranges[0].get("values", [])
    reboot_temp_vals = value_ranges[1].get("values", [])
    reboot_stability_vals = value_ranges[2].get("values", [])
    temp_vals = value_ranges[3].get("values", [])
    stability_vals = value_ranges[4].get("values", [])
    pilot_vals = value_ranges[5].get("values", [])
    repair_vals = value_ranges[6].get("values", [])

    return {
        "adjust_block": adjust_vals,
        "reboot_flag_temp": reboot_temp_vals[0][0] if reboot_temp_vals and reboot_temp_vals[0] else "",
        "reboot_flag_stability": reboot_stability_vals[0][0] if reboot_stability_vals and reboot_stability_vals[0] else "",
        "temp_disk": temp_vals,
        "stability_disk": stability_vals,
        "pilot_input": pilot_vals,
        "repair_input": repair_vals,
    }


# ---- tick: per-system updates ------------------------------------------

def update_reactor(state, adjust_block):
    """Advance each reactor bar based on player-set direction. Returns writes."""
    writes = []
    reactor_adjustments = []

    for i in range(NUM_BARS):
        try:
            raw_value = adjust_block[i][0]
            adjustment = float(raw_value)
        except (IndexError, TypeError, ValueError):
            adjustment = 0.0  # neutral / no movement for blank or invalid cells

        # Restrict player input to allowed directions/magnitude.
        adjustment = max(-1.0, min(1.0, adjustment))
        reactor_adjustments.append(adjustment)

    for i in range(NUM_BARS):
        row_num = 3 + i  # H5 controls bar 1, H6 controls bar 2, etc.
        delta = random.uniform(*REACTOR_DELTA_RANGE) * reactor_adjustments[i]
        new_val = min(REACTOR_MELTDOWN_VALUE, max(0, state["reactor_bars"][i] + delta))

        state["reactor_bars"][i] = new_val
        writes.append({
            "range": f"{REACTOR_SHEET}!B{row_num}",
            "values": [[round(new_val, 1)]],
        })

        if (new_val >= REACTOR_MELTDOWN_VALUE) and i is not NUM_BARS:
            print(f"!! Reactor bar {i + 1} hit meltdown threshold")

    return writes

def update_life_support(state, reboot_flag_temp, reboot_flag_stability, temp_disk, stability_disk):
    """Apply corruption damage or process a reboot attempt. Temperature and
    stability are fully independent: each is driven only by its own disk
    and its own reboot checkbox."""
    writes = []

    temp_corrupted, temp_good = count_disk_data(temp_disk)
    stability_corrupted, stability_good = count_disk_data(stability_disk)

    temp_reboot_ready = temp_corrupted == 0 and temp_good >= MIN_GOOD_DATA_FOR_REBOOT
    stability_reboot_ready = stability_corrupted == 0 and stability_good >= MIN_GOOD_DATA_FOR_REBOOT

    temp_rebooted = str(reboot_flag_temp).strip().upper() == "TRUE"
    stability_rebooted = str(reboot_flag_stability).strip().upper() == "TRUE"

    temp_just_rebooted = False
    stability_just_rebooted = False
    status_parts = []
    denied_parts = []

    # --- Temperature channel (temp_disk + LIFE_REBOOT_TEMP only) ---
    if temp_rebooted:
        writes.append({"range": LIFE_REBOOT_TEMP, "values": [[False]]})
        if temp_reboot_ready:
            state["temperature"] = 50.0
            temp_just_rebooted = True
            status_parts.append("temperature nominal")
            print(">> Temp disk rebooted successfully")
        else:
            denied_parts.append(f"temp disk: {temp_corrupted} corrupt / {temp_good} good")
            print(">> Temp reboot denied: not ready")

    if not temp_just_rebooted:
        if temp_corrupted > 0:
            state["temperature"] = min(
                MAX_TEMPERATURE,
                state["temperature"] + temp_corrupted * TEMP_RISE_PER_CORRUPTED,
            )
        else:
            recovery_scale = temp_good / 10
            state["temperature"] = max(
                0.0,
                state["temperature"] - TEMP_FALL_WHEN_CLEAN * recovery_scale,
            )

    # --- Stability channel (stability_disk + LIFE_REBOOT_STABILITY only) ---
    if stability_rebooted:
        writes.append({"range": LIFE_REBOOT_STABILITY, "values": [[False]]})
        if stability_reboot_ready:
            state["stability"] = 50.0
            stability_just_rebooted = True
            status_parts.append("stability nominal")
            print(">> Stability disk rebooted successfully")
        else:
            denied_parts.append(f"stability disk: {stability_corrupted} corrupt / {stability_good} good")
            print(">> Stability reboot denied: not ready")

    if not stability_just_rebooted:
        if stability_corrupted > 0:
            state["stability"] = max(
                MIN_STABILITY,
                state["stability"] - stability_corrupted * STABILITY_LOSS_PER_CORRUPTED,
            )
        else:
            recovery_scale = stability_good / 10
            state["stability"] = min(
                MAX_STABILITY,
                state["stability"] + STABILITY_GAIN_WHEN_CLEAN * recovery_scale,
            )

    # --- Status cell ---
    if status_parts or denied_parts:
        if status_parts and not denied_parts:
            msg = "REBOOT COMPLETE — " + ", ".join(status_parts)
        elif denied_parts and not status_parts:
            msg = "REBOOT DENIED — clear corruption and restore 8 GOOD DATA blocks per disk"
        else:
            msg = "REBOOT PARTIAL — " + ", ".join(status_parts) + "; denied: " + ", ".join(denied_parts)
        writes.append({"range": LIFE_STATUS_CELL, "values": [[msg]]})

    # --- Corruption spread (independent per disk, skipped only for the
    # disk that just successfully rebooted) ---
    if not temp_just_rebooted:
        corrupt_write = maybe_corrupt_disk(TEMP_DISK_COLS, CORRUPT_CHANCE)
        if corrupt_write:
            writes.append(corrupt_write)

    if not stability_just_rebooted:
        corrupt_write = maybe_corrupt_disk(STABILITY_DISK_COLS, CORRUPT_CHANCE)
        if corrupt_write:
            writes.append(corrupt_write)

    if state["temperature"] >= MAX_TEMPERATURE / 1.5:
        print(">> Temperature approaching critical threshold! Roll CON Checks.")

    if state["stability"] <= MIN_STABILITY / 1.5:
        print(">> Stability approaching critical threshold! Roll DEX Checks.")
    
    # Current readouts
    writes.extend([
        {"range": LIFE_TEMP_CELL, "values": [[round(state["temperature"], 1)]]},
        {"range": LIFE_STABILITY_CELL, "values": [[round(state["stability"], 1)]]},
    ])

    # Circular two-column history: D = temperature, E = stability.
    if state["history_row"] > MAX_HISTORY:
        state["history_row"] = 2

    writes.append({
        "range": f"{LIFE_SHEET}!D{state['history_row']}:E{state['history_row']}",
        "values": [[round(state["temperature"], 1), round(state["stability"], 1)]],
    })
    state["history_row"] += 1

    return writes

def update_pilot(state, pilot_input):
    """Advance the round timer and score a completed round. Returns writes."""
    writes = []

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
        miss = (
            _heading_diff(player_heading, state["pilot_heading"]) <= HEADING_TOLERANCE
            and abs(player_speed - state["pilot_speed"]) <= SPEED_TOLERANCE
        )
        status = "MISS" if miss else "HIT"
        if not miss and state["pilot_score"] > MIN_HULL_INTEGRITY:
            Damage = random.randint(5, 15)
            state["pilot_score"] -= Damage
            print(f"Silas hit! Hull integrity -{Damage} (now {state['pilot_score']})")
        if state["pilot_score"] < MIN_HULL_INTEGRITY:
            print(">> Hull integrity depleted! SILAS GOING DOWN!")

        state["pilot_heading"] = random.randint(0, 359)
        state["pilot_speed"] = random.randint(1, 9)
        state["pilot_time_left"] = PILOT_ROUND_SECONDS

        writes.extend([
            {"range": PILOT_HEADING_CELL, "values": [[state["pilot_heading"]]]},
            {"range": PILOT_SPEED_CELL, "values": [[state["pilot_speed"]]]},
            {"range": PILOT_INPUT_CLEAR_RANGE, "values": [[""], [""]]},
            {"range": PILOT_STATUS_CELL, "values": [[status]]},
            {"range": PILOT_TIME_CELL, "values": [[state["pilot_time_left"]]]},
            {"range": SILAS_HULL_INTEGRITY_CELL, "values": [[state["pilot_score"]]]},
        ])
        print(f"Pilot round result: {status} (score {state['pilot_score']})")
    else:
        writes.append({"range": PILOT_TIME_CELL, "values": [[round(state["pilot_time_left"], 1)]]})

    return writes

def _generate_repair_code():
    return "".join(random.choices(REPAIR_CODE_CHARS, k=REPAIR_CODE_LENGTH))

def update_repair(state, repair_input):
    """Advance the repair-code timer. If the player typed a matching code
    before it rotates, grant hull healing. Returns writes."""
    writes = []

    player_guess = ""
    if repair_input and repair_input[0]:
        player_guess = str(repair_input[0][0]).strip().upper()

    current_code = state.get("repair_code")
    if not current_code:
        # First tick: seed a code with no prior guess possible.
        current_code = _generate_repair_code()
        state["repair_code"] = current_code
        state["repair_time_left"] = REPAIR_INTERVAL_SECONDS
        writes.extend([
            {"range": REPAIR_CODE_CELL, "values": [[current_code]]},
            {"range": REPAIR_TIME_CELL, "values": [[state["repair_time_left"]]]},
        ])
        return writes

    solved = player_guess == current_code

    if solved:
        heal = random.randint(REPAIR_HEAL_MIN, REPAIR_HEAL_MAX)
        state["pilot_score"] = min(MAX_HULL_INTEGRITY, state["pilot_score"] + heal)
        print(f"Repair code matched! Hull repaired +{heal} (now {state['pilot_score']})")

        new_code = _generate_repair_code()
        state["repair_code"] = new_code
        state["repair_time_left"] = REPAIR_INTERVAL_SECONDS

        writes.extend([
            {"range": REPAIR_CODE_CELL, "values": [[new_code]]},
            {"range": REPAIR_INPUT_RANGE, "values": [[""]]},
            {"range": REPAIR_STATUS_CELL, "values": [["REPAIR SUCCESSFUL"]]},
            {"range": REPAIR_TIME_CELL, "values": [[state["repair_time_left"]]]},
            {"range": SILAS_HULL_INTEGRITY_CELL, "values": [[state["pilot_score"]]]},
        ])
        return writes

    state["repair_time_left"] -= TICK_SECONDS

    if state["repair_time_left"] <= 0:
        new_code = _generate_repair_code()
        state["repair_code"] = new_code
        state["repair_time_left"] = REPAIR_INTERVAL_SECONDS

        writes.extend([
            {"range": REPAIR_CODE_CELL, "values": [[new_code]]},
            {"range": REPAIR_INPUT_RANGE, "values": [[""]]},
            {"range": REPAIR_STATUS_CELL, "values": [["CODE EXPIRED"]]},
            {"range": REPAIR_TIME_CELL, "values": [[state["repair_time_left"]]]},
        ])
        print(">> Repair code expired unmatched, rotating")
    else:
        writes.append({"range": REPAIR_TIME_CELL, "values": [[round(state["repair_time_left"], 1)]]})

    return writes

# ---- tick: orchestration ------------------------------------------------

def tick(sh, state):
    """One full tick: one batched read of player-editable cells, one
    batched write of every worksheet's updates."""
    inputs = read_tick_inputs(sh)

    writes = []
    # writes += update_reactor(state, inputs["adjust_block"])
    # writes += update_life_support(state, inputs["reboot_flag_temp"], inputs["reboot_flag_stability"], inputs["temp_disk"], inputs["stability_disk"])
    writes += update_pilot(state, inputs["pilot_input"])
    writes += update_repair(state, inputs["repair_input"])

    retry(sh.values_batch_update, {
        "valueInputOption": "USER_ENTERED",
        "data": writes,
    })

def main():
    print("Heartbeat running. Ctrl+C to stop.")
    sh, reactor_ws, life_ws, pilot_ws = connect()
    state = load_initial_state(reactor_ws, life_ws, pilot_ws)
    while True:
        try:
            tick(sh, state)
        except Exception as e:
            # keep the loop alive through transient API hiccups
            print("tick error:", e)
        time.sleep(TICK_SECONDS)

if __name__ == "__main__":
    main()