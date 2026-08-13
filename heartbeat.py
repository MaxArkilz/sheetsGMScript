"""
SETUP (one-time):
  1. pip install gspread google-auth
  2. In Google Cloud Console: create a project -> enable "Google Sheets API"
     -> create a Service Account -> download its JSON key as credentials.json
     (keep this file next to this script, and out of version control).
  3. Open your Sheet, click Share, and share it with the service account's
     email address (looks like xxx@xxx.iam.gserviceaccount.com) as Editor.
  4. Fill in SHEET_ID below (the long id in the sheet's URL).

"""

import random
import time

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# ---- config ---------------------------------------------------------------

SHEET_ID = "1d55txSbBoXS4qjJ28L4NK9nylOJFoWGq0AReWvOyn6c"
TICK_SECONDS = 2                # how often the loop runs
REACTOR_DELTA_RANGE = (0, 5)    # per-tick change per bar; can go up OR down,
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

MAX_HISTORY = 20

LIFE_TEMP_DISK_RANGE = "Life Support Computer!H2:K30"
LIFE_STABILITY_DISK_RANGE = "Life Support Computer!L2:O30"
LIFE_REBOOT_RANGE = "Life Support Computer!F2"

MIN_GOOD_DATA_FOR_REBOOT = 8

TEMP_RISE_PER_CORRUPTED = 1.5
STABILITY_LOSS_PER_CORRUPTED = 1.0

MAX_TEMPERATURE = 100
MAX_STABILITY = 100
MIN_STABILITY = 0

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
REACTOR_FIRST_ROW = 3
REACTOR_LAST_ROW = REACTOR_FIRST_ROW + NUM_BARS - 1

REACTOR_VALUES_RANGE = f"Reactor!B{REACTOR_FIRST_ROW}:B{2 + NUM_BARS}"
REACTOR_ADJUST_RANGE = f"Reactor!H5:H{4 + NUM_BARS}"

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
 
    reactor_vals = retry(reactor_ws.get, f"B{REACTOR_FIRST_ROW}:B{REACTOR_LAST_ROW}")
    bars = []
    for i in range(NUM_BARS):
        row = reactor_vals[i] if i < len(reactor_vals) else []
        val = float(row[0]) if len(row) > 0 and row[0] != "" else 0
        bars.append(val)
    state["reactor_bars"] = bars
 
    temp_cell = retry(life_ws.acell, "B2").value
    state["temperature"] = (
        float(temp_cell) if temp_cell not in (None, "") else 0.0
    )

    stability_cell = retry(life_ws.acell, "C3").value
    state["stability"] = (
        float(stability_cell) if stability_cell not in (None, "") else 100.0
    )    
 
    # Find the next empty row in the Life Support history column (D),
    # so future ticks can write directly without an append lookup.
    history_col = retry(life_ws.col_values, 4)
    state["history_row"] = max(2, len(history_col) + 1)
 
    ensure_pilot_sheet(state)
 
    return state

def count_disk_data(block):
    """Return (corrupted_count, good_data_count) for a Sheets range."""
    corrupted_count = 0
    good_data_count = 0

    for row in block or []:
        for cell in row:
            value = str(cell).strip().upper()

            if value == CORRUPT_TEXT:
                corrupted_count += 1
            elif value == "GOOD DATA":
                good_data_count += 1

    return corrupted_count, good_data_count   

def tick(state):
    """One full tick: one batched read of player-editable cells, one
    batched write of every worksheet's updates."""
 
    # ---- single batched read across all three worksheets ----
    ranges = [
        REACTOR_ADJUST_RANGE,
        LIFE_REBOOT_RANGE,
        LIFE_TEMP_DISK_RANGE,
        LIFE_STABILITY_DISK_RANGE,
        "Cockpit!C2:C3",              
    ]
    result = retry(sh.values_batch_get, ranges)
 
    adjust_block = None
    reboot_flag = ""
    temp_disk = []
    stability_disk = []
    reboot_flag = None
    pilot_input = None
    for vr in result["valueRanges"]:
        rng = vr["range"]
        vals = vr.get("values", [])

        if rng.startswith("Reactor!"):
            adjust_block = vals
        elif rng.startswith("Life Support Computer!F2"):
            reboot_flag = vals[0][0] if vals and vals[0] else ""
        elif rng.startswith("Life Support Computer!H2:K30"):
            temp_disk = vals
        elif rng.startswith("Life Support Computer!L2:O30"):
            stability_disk = vals
        elif rng.startswith("Cockpit!"):
            pilot_input = vals
 
    writes = []  # list of {"range": "Sheet!A1", "values": [[...]]}
 
    # ---- Reactor ----
    reactor_adjustments = []
    for i in range(NUM_BARS):
        try:
            raw_value = adjust_block[i][0]
            adjustment = float(raw_value)
        except (IndexError, TypeError, ValueError):
            adjustment = 0.0  # neutral / no movement for blank or invalid cells

        # Optional: restrict player input to allowed directions.
        adjustment = max(-1.0, min(1.0, adjustment))
        reactor_adjustments.append(adjustment)

    for i in range(NUM_BARS):
        row_num = 3 + i

        # H5 controls bar 1, H6 controls bar 2, etc.
        adjust_direction = reactor_adjustments[i]

        delta = random.uniform(*REACTOR_DELTA_RANGE) * adjust_direction
        new_val = min(
            REACTOR_MELTDOWN_VALUE,
            max(0, state["reactor_bars"][i] + delta),
        )

        state["reactor_bars"][i] = new_val
        writes.append({
            "range": f"Reactor!B{row_num}",
            "values": [[round(new_val, 1)]],
        })

        if new_val >= REACTOR_MELTDOWN_VALUE:
            print(f"!! Reactor bar {i + 1} hit meltdown threshold")


    # ---- Life Support ----
    temp_corrupted, temp_good = count_disk_data(temp_disk)
    stability_corrupted, stability_good = count_disk_data(stability_disk)

    total_corrupted = temp_corrupted + stability_corrupted

    temp_reboot_ready = (
        temp_corrupted == 0
        and temp_good >= MIN_GOOD_DATA_FOR_REBOOT
    )

    stability_reboot_ready = (
        stability_corrupted == 0
        and stability_good >= MIN_GOOD_DATA_FOR_REBOOT
    )

    reboot_ready = temp_reboot_ready and stability_reboot_ready
    rebooted = str(reboot_flag).strip().upper() == "TRUE"

    # A reboot is accepted only after both disks are clean and sufficiently restored.
    if rebooted and reboot_ready:
        state["temperature"] = 0.0
        state["stability"] = MAX_STABILITY

        writes.extend([
            {"range": "Life Support Computer!F2", "values": [[False]]},
            {"range": "Life Support Computer!B5", "values": [[
                "REBOOT COMPLETE — systems nominal"
            ]]},
        ])

        print(">> Life Support rebooted successfully")

    else:
        # Reset the checkbox when it was attempted before both disks were valid.
        if rebooted:
            writes.extend([
                {"range": "Life Support Computer!F2", "values": [[False]]},
                {"range": "Life Support Computer!B5", "values": [[
                    "REBOOT DENIED — clear corruption and restore 8 GOOD DATA blocks per disk"
                ]]},
            ])
            print(
                ">> Reboot denied: "
                f"temp disk: {temp_corrupted} corrupt / {temp_good} good; "
                f"stability disk: {stability_corrupted} corrupt / {stability_good} good"
            )

        # No corrupt data means neither system gets worse this tick.
        if total_corrupted > 0:
            state["temperature"] = min(
                MAX_TEMPERATURE,
                state["temperature"] + total_corrupted * TEMP_RISE_PER_CORRUPTED,
            )

            state["stability"] = max(
                MIN_STABILITY,
                state["stability"] - total_corrupted * STABILITY_LOSS_PER_CORRUPTED,
            )

    # Current readouts
    writes.extend([
        {
            "range": "Life Support Computer!B2",
            "values": [[round(state["temperature"], 1)]],
        },
        {
            "range": "Life Support Computer!C3",
            "values": [[round(state["stability"], 1)]],
        },
    ])

    # Circular two-column history: D = temperature, E = stability.
    if state["history_row"] > MAX_HISTORY:
        state["history_row"] = 2

    writes.append({
        "range": f"Life Support Computer!D{state['history_row']}:E{state['history_row']}",
        "values": [[
            round(state["temperature"], 1),
            round(state["stability"], 1),
        ]],
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
            {"range": "Cockpit!B2", "values": [[state["pilot_heading"]]]},
            {"range": "Cockpit!B3", "values": [[state["pilot_speed"]]]},
            {"range": "Cockpit!C2:C3", "values": [[""], [""]]},
            {"range": "Cockpit!D2", "values": [[status]]},
            {"range": "Cockpit!B5", "values": [[state["pilot_time_left"]]]},
        ])
        print(f"Pilot round result: {status} (score {state['pilot_score']})")
    else:
        writes.append({"range": "Cockpit!B5", "values": [[round(state["pilot_time_left"], 1)]]})
 
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