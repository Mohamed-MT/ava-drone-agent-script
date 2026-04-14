"""
=============================================================
  Agentic AI Drone Control System
  DroneKit + SITL + Unreal Engine + Agno + OpenRouter
=============================================================
"""

# ---------------------------------------------------------------
# DRONEKIT IMPORT FIX
# ---------------------------------------------------------------
try:
    from dronekit import connect, VehicleMode, LocationGlobalRelative
except Exception:
    from collections import abc
    import collections
    collections.MutableMapping = abc.MutableMapping
    from dronekit import connect, VehicleMode, LocationGlobalRelative

import os
os.environ["OPENROUTER_API_KEY"] = "Insert your key here"

import time
import math
import threading
import queue
import datetime
import json
import operator as op_module
import tcp_relay

from agno.agent import Agent
from agno.tools import Toolkit
from agno.db.sqlite import SqliteDb
from agno.models.openrouter import OpenRouter
from agno.learn import LearningMachine
from agno.compression.manager import CompressionManager
from typing import List, Optional
from pydantic import BaseModel, Field

try:
    from agno.workflow import Workflow, Step, Steps, StepInput, StepOutput
    WORKFLOW_AVAILABLE = True
except ImportError:
    WORKFLOW_AVAILABLE = False

# ---------------------------------------------------------------
# STRUCTURED OUTPUTS
# ---------------------------------------------------------------

class DroneCommand(BaseModel):
    action:    str             = Field(description="Drone action to execute")
    altitude:  Optional[float] = Field(None, description="Target altitude in meters")
    latitude:  Optional[float] = Field(None, description="Target latitude")
    longitude: Optional[float] = Field(None, description="Target longitude")
    radius:    Optional[float] = Field(None, description="Circle radius in meters")
    direction: Optional[str]   = Field(None, description="Movement direction")
    distance:  Optional[float] = Field(None, description="Movement distance in meters")
    speed:     Optional[float] = Field(None, description="Target speed in m/s")
    reason:    str             = Field(default="", description="Why this command was chosen")

class MissionPlan(BaseModel):
    mission_name:           str                = Field(description="Short name for this mission")
    objective:              str                = Field(description="What this mission accomplishes")
    steps:                  List[DroneCommand] = Field(description="Ordered drone commands")
    risk_level:             str                = Field(description="LOW / MEDIUM / HIGH")
    estimated_time_seconds: int                = Field(description="Estimated total time")
    notes:                  str                = Field(default="", description="Special notes")

class SafetyAssessment(BaseModel):
    is_safe:         bool      = Field(description="True if mission is safe")
    risk_level:      str       = Field(description="LOW / MEDIUM / HIGH / CRITICAL")
    issues:          List[str] = Field(description="Identified safety issues")
    recommendations: List[str] = Field(description="Suggested mitigations")
    approved:        bool      = Field(description="Final approval to proceed")

class FlightReport(BaseModel):
    session_id:           str           = Field(description="Session identifier")
    total_commands:       int           = Field(description="Commands executed")
    commands_executed:    List[str]     = Field(description="All commands run")
    max_altitude_m:       float         = Field(description="Peak altitude reached")
    duration_seconds:     int           = Field(description="Session length in seconds")
    battery_start:        Optional[int] = Field(None, description="Battery at start")
    battery_end:          Optional[int] = Field(None, description="Battery now")
    incidents:            List[str]     = Field(description="Blocked or failed commands")
    summary:              str           = Field(description="Human-readable narrative")

# ---------------------------------------------------------------
# PRESET LOCATIONS — named places near SITL home (Canberra)
# ---------------------------------------------------------------
PRESET_LOCATIONS = {
    "home":         {"lat": -35.363261, "lon": 149.165230, "description": "SITL home / launch point"},
    "airfield":     {"lat": -35.362749, "lon": 149.165353, "description": "Canberra Airfield"},
    "runway 35":    {"lat": -35.363328, "lon": 149.165223, "description": "Runway 35"},
    "runway 17":    {"lat": -35.362227, "lon": 149.165074, "description": "Runway 17"},
    "hospital":     {"lat": -35.354167, "lon": 149.150560, "description": "Mugga Mugga Hospital"},
    "prison":       {"lat": -35.371077, "lon": 149.172684, "description": "West Jerrabomberra Prison"},
    "camp a":       {"lat": -35.360338, "lon": 149.151874, "description": "West Jerrabomberra Camp A"},
    "camp b":       {"lat": -35.361530, "lon": 149.154562, "description": "West Jerrabomberra Location 2"},
    "reserve":      {"lat": -35.366030, "lon": 149.150095, "description": "West Jerrabomberra Reserve"},
    "residence 1":  {"lat": -35.357340, "lon": 149.170626, "description": "Jerrabomberra Residence 1"},
    "residence 2":  {"lat": -35.346840, "lon": 149.154976, "description": "Jerrabomberra North Residence"},
    "creek south":  {"lat": -35.363393, "lon": 149.175728, "description": "Jerrabomberra Creek South"},
    "location 1":   {"lat": -35.364759, "lon": 149.152459, "description": "West Jerrabomberra Location 1"},
}

# ---------------------------------------------------------------
# VEHICLE CONNECTION
# ---------------------------------------------------------------
print("Connecting to vehicle...")
vehicle = connect("tcp:127.0.0.1:5763", wait_ready=True, baud=57600, rate=60)
print("Vehicle connected.")

while vehicle.location.local_frame.north is None:
    time.sleep(1)
    print("Waiting for local frame...")
print("Local frame ready.")

# ---------------------------------------------------------------
# TCP RELAY
# ---------------------------------------------------------------
relay = tcp_relay.TCP_Relay()

def vehicle_to_unreal(v, z_invert=True, scale=100):
    return {
        "n":     v.location.local_frame.north * scale,
        "e":     v.location.local_frame.east  * scale,
        "d":     v.location.local_frame.down  * scale * (-1 if z_invert else 1),
        "roll":  math.degrees(v.attitude.roll),
        "pitch": math.degrees(v.attitude.pitch),
        "yaw":   math.degrees(v.attitude.yaw),
    }

def unreal_stream_loop():
    while True:
        data = vehicle_to_unreal(vehicle)
        fields = [0.0] * relay.num_fields
        fields[0] = data["n"]
        fields[1] = data["e"]
        fields[2] = data["d"]
        fields[3] = data["roll"]
        fields[4] = data["pitch"]
        fields[5] = data["yaw"]
        relay.message = tcp_relay.create_fields_string(fields)
        time.sleep(1 / 60)

threading.Thread(target=unreal_stream_loop, daemon=True).start()

# ---------------------------------------------------------------
# STATE MANAGEMENT
# ---------------------------------------------------------------
SESSION_START = datetime.datetime.now()
SESSION_ID    = f"drone-{SESSION_START.strftime('%H%M%S')}"

mission_state = {
    "phase":           "idle",
    "flight_log":      [],
    "max_altitude":    0.0,
    "battery_start":   None,
    "battery_current": None,
    "incidents":       [],
    "last_command":    {},
    "current_mission": None,
}

try:
    mission_state["battery_start"] = vehicle.battery.level
except Exception:
    pass

# ---------------------------------------------------------------
# COMMAND QUEUE + FLAGS
# ---------------------------------------------------------------
command_queue = queue.Queue()
stop_flag     = threading.Event()

def _clear_queue():
    cleared = 0
    while not command_queue.empty():
        try:
            command_queue.get_nowait()
            command_queue.task_done()
            cleared += 1
        except queue.Empty:
            break
    if cleared:
        print(f"[QUEUE] Cleared {cleared} pending command(s).")

# ---------------------------------------------------------------
# CONDITION MONITOR
# ---------------------------------------------------------------
_OPERATORS = {
    "==":     op_module.eq,
    "!=":     op_module.ne,
    "<":      op_module.lt,
    "<=":     op_module.le,
    ">":      op_module.gt,
    ">=":     op_module.ge,
    "in":     lambda a, b: a in b,
    "not in": lambda a, b: a not in b,
}

CONDITION_FIELDS = [
    "rel_alt", "battery_level", "battery_voltage",
    "groundspeed", "armed", "mode", "airborne", "yaw",
]

class ConditionWatch:
    def __init__(self, field, operator, value, then_action, then_params=None, label=""):
        self.field       = field
        self.operator    = operator
        self.value       = value
        self.then_action = then_action
        self.then_params = then_params or {}
        self.label       = label or f"{field} {operator} {value} -> {then_action}"
        self.triggered   = False

    def evaluate(self, state):
        state_val = state.get(self.field)
        if state_val is None:
            return False
        op_func = _OPERATORS.get(self.operator)
        if op_func is None:
            return False
        try:
            return op_func(state_val, self.value)
        except Exception:
            return False

class ConditionMonitor:
    def __init__(self):
        self._watches = []
        self._lock    = threading.Lock()
        threading.Thread(target=self._tick, daemon=True).start()

    def add_watch(self, watch):
        with self._lock:
            self._watches.append(watch)
        print(f"[CONDITION] Watching: {watch.label}")

    def clear_watches(self):
        with self._lock:
            self._watches.clear()
        print("[CONDITION] All watches cleared.")

    def list_watches(self):
        with self._lock:
            if not self._watches:
                return "No active condition watches."
            return "\n".join(f"  - {w.label}" for w in self._watches)

    def _get_state(self):
        try:
            loc  = vehicle.location.global_relative_frame
            batt = vehicle.battery
            return {
                "rel_alt":         round(loc.alt, 1) if loc.alt else 0.0,
                "lat":             loc.lat,
                "lon":             loc.lon,
                "battery_level":   batt.level or 0,
                "battery_voltage": round(batt.voltage or 0.0, 2),
                "groundspeed":     round(vehicle.groundspeed, 1),
                "armed":           vehicle.armed,
                "mode":            vehicle.mode.name,
                "yaw":             round(math.degrees(vehicle.attitude.yaw), 1),
                "airborne":        (loc.alt or 0) > 1.0,
                "time":            time.time(),
            }
        except Exception:
            return {}

    def _tick(self):
        while True:
            time.sleep(0.5)
            with self._lock:
                if not self._watches:
                    continue
                state     = self._get_state()
                remaining = []
                for watch in self._watches:
                    if watch.triggered:
                        continue
                    if watch.evaluate(state):
                        print(f"\n[CONDITION] TRIGGERED: {watch.label}")
                        watch.triggered = True
                        stop_flag.set()
                        _clear_queue()
                        cmd = {"action": watch.then_action}
                        cmd.update(watch.then_params)
                        command_queue.put(cmd)
                    else:
                        remaining.append(watch)
                self._watches = remaining

condition_monitor = ConditionMonitor()

# ---------------------------------------------------------------
# KNOWLEDGE BASE
# ---------------------------------------------------------------
_loc_lines = "\n".join(
    f"  {name}: {data['description']} (lat={data['lat']}, lon={data['lon']})"
    for name, data in PRESET_LOCATIONS.items()
)

DRONE_KNOWLEDGE = f"""
ENVIRONMENT: SITL simulation. Battery is NOTIONAL/FAKE — ignore completely.

PRESET LOCATIONS:
{_loc_lines}

ALTITUDE: Max 120m AGL. Min 2m. Cruise 15-50m.
SPEED: Max 15 m/s. Cruise 5-8 m/s.
CIRCLE: Min radius 5m. Recommended 20-50m.

FLIGHT MODES:
- GUIDED: programmatic control (default)
- LOITER: GPS hover
- AUTO: execute Mission Planner waypoint mission
- RTL: return to launch and land
- LAND: land at current position
"""

# ---------------------------------------------------------------
# EXECUTOR HELPERS
# ---------------------------------------------------------------
def _wait_for_arrival(target_lat, target_lon, target_alt, tolerance_m=2.0, timeout=60):
    R     = 6378137.0
    start = time.time()
    while time.time() - start < timeout:
        if stop_flag.is_set():
            print("[DRONE] Arrival interrupted.")
            return False
        loc = vehicle.location.global_relative_frame
        if loc.lat is None:
            time.sleep(0.5)
            continue
        d_lat = math.radians(loc.lat - target_lat) * R
        d_lon = math.radians(loc.lon - target_lon) * R * math.cos(math.radians(target_lat))
        d_alt = abs(loc.alt - target_alt)
        if math.sqrt(d_lat**2 + d_lon**2 + d_alt**2) <= tolerance_m:
            return True
        time.sleep(0.5)
    print("[DRONE] Arrival timeout — proceeding.")
    return False

def _set_mode_confirmed(mode_name, retries=5):
    for i in range(retries):
        vehicle.mode = VehicleMode(mode_name)
        time.sleep(0.5)
        if vehicle.mode.name == mode_name:
            return True
        print(f"[DRONE] Mode retry {i+1}/{retries} -> {mode_name}")
    print(f"[DRONE] WARNING: Could not confirm {mode_name}")
    return False

def _fly_circle(radius=20, altitude=None, points=12):
    cur_lat = vehicle.location.global_relative_frame.lat
    cur_lon = vehicle.location.global_relative_frame.lon
    cur_alt = altitude or vehicle.location.global_relative_frame.alt
    R       = 6378137.0
    print(f"[DRONE] Circle: r={radius}m alt={cur_alt}m pts={points}")
    for i in range(points):
        if stop_flag.is_set():
            print("[DRONE] Circle interrupted.")
            return
        angle = (2 * math.pi / points) * i
        d_lat = (radius * math.cos(angle)) / R
        d_lon = (radius * math.sin(angle)) / (R * math.cos(math.radians(cur_lat)))
        vehicle.simple_goto(LocationGlobalRelative(
            cur_lat + math.degrees(d_lat),
            cur_lon + math.degrees(d_lon),
            cur_alt
        ))
        for _ in range(30):
            if stop_flag.is_set():
                return
            time.sleep(0.1)
    print("[DRONE] Circle complete.")

def _log(action, details=None):
    loc = vehicle.location.global_relative_frame
    entry = {
        "time":    datetime.datetime.now().strftime("%H:%M:%S"),
        "action":  action,
        "lat":     round(loc.lat, 6) if loc.lat else None,
        "lon":     round(loc.lon, 6) if loc.lon else None,
        "alt":     round(loc.alt, 1) if loc.alt else None,
        "details": details or {},
    }
    mission_state["flight_log"].append(entry)
    if loc.alt:
        mission_state["max_altitude"] = max(mission_state["max_altitude"], loc.alt)
    try:
        mission_state["battery_current"] = vehicle.battery.level
    except Exception:
        pass

# ---------------------------------------------------------------
# EXECUTOR THREAD
# ---------------------------------------------------------------
def executor_loop():
    while True:
        cmd    = command_queue.get()
        action = cmd.get("action")
        try:
            if action in ("arm", "takeoff", "goto", "change_altitude", "adjust_altitude",
                          "move", "keep_moving", "circle", "flyover", "rtl", "land",
                          "set_mode", "set_speed"):
                stop_flag.clear()
                if action not in ("rtl", "land", "arm", "set_mode", "set_speed"):
                    mission_state["last_command"] = cmd
                mission_state["phase"] = "executing"

            if action == "arm":
                _set_mode_confirmed("GUIDED")
                vehicle.armed = True
                while not vehicle.armed:
                    time.sleep(0.5)
                _log("arm")
                print("[DRONE] Armed.")

            elif action == "disarm":
                vehicle.armed = False
                _log("disarm")
                print("[DRONE] Disarmed.")

            elif action == "takeoff":
                alt = cmd.get("altitude", 10)
                _set_mode_confirmed("GUIDED")
                vehicle.armed = True
                while not vehicle.armed:
                    time.sleep(0.5)
                vehicle.simple_takeoff(alt)
                while True:
                    if stop_flag.is_set():
                        break
                    if vehicle.location.global_relative_frame.alt >= alt * 0.95:
                        break
                    time.sleep(0.5)
                _log("takeoff", {"altitude": alt})
                if not stop_flag.is_set():
                    print(f"[DRONE] Reached {alt}m.")

            elif action == "goto":
                tlat, tlon, talt = cmd["latitude"], cmd["longitude"], cmd["altitude"]
                if vehicle.mode.name != "GUIDED":
                    _set_mode_confirmed("GUIDED")
                vehicle.simple_goto(LocationGlobalRelative(tlat, tlon, talt))
                print(f"[DRONE] Going to {tlat:.5f}, {tlon:.5f} @ {talt}m")
                _wait_for_arrival(tlat, tlon, talt)
                _log("goto", {"lat": tlat, "lon": tlon, "alt": talt})
                if not stop_flag.is_set():
                    print("[DRONE] Arrived.")

            elif action == "change_altitude":
                cur     = vehicle.location.global_relative_frame
                new_alt = cmd["altitude"]
                vehicle.simple_goto(LocationGlobalRelative(cur.lat, cur.lon, new_alt))
                print(f"[DRONE] Changing altitude to {new_alt}m")
                while True:
                    if stop_flag.is_set():
                        break
                    if abs(vehicle.location.global_relative_frame.alt - new_alt) <= 1.0:
                        print(f"[DRONE] Altitude reached: {new_alt}m")
                        break
                    time.sleep(0.5)
                _log("change_altitude", {"altitude": new_alt})

            elif action == "adjust_altitude":
                cur_alt = vehicle.location.global_relative_frame.alt
                new_alt = max(2, min(120, cur_alt + cmd["change"]))
                cur     = vehicle.location.global_relative_frame
                vehicle.simple_goto(LocationGlobalRelative(cur.lat, cur.lon, new_alt))
                _log("adjust_altitude", {"change": cmd["change"], "new_alt": round(new_alt, 1)})

            elif action == "move":
                d, dist = cmd["direction"].lower(), cmd["distance"]
                alt     = cmd.get("altitude")
                cur     = vehicle.location.global_relative_frame
                cur_alt = alt if alt else cur.alt
                R       = 6378137.0
                if d in ("forward",):           d = "north"
                elif d in ("backward", "back"): d = "south"
                elif d in ("left",):            d = "west"
                elif d in ("right",):           d = "east"
                if   d == "north": nlat, nlon = cur.lat + math.degrees(dist/R), cur.lon
                elif d == "south": nlat, nlon = cur.lat - math.degrees(dist/R), cur.lon
                elif d == "east":
                    nlat = cur.lat
                    nlon = cur.lon + math.degrees(dist/(R*math.cos(math.radians(cur.lat))))
                else:
                    nlat = cur.lat
                    nlon = cur.lon - math.degrees(dist/(R*math.cos(math.radians(cur.lat))))
                vehicle.simple_goto(LocationGlobalRelative(nlat, nlon, cur_alt))
                _wait_for_arrival(nlat, nlon, cur_alt)
                _log("move", {"direction": d, "distance": dist})

            elif action == "circle":
                mission_state["last_command"] = cmd
                _fly_circle(
                    radius   = cmd.get("radius", 20),
                    altitude = cmd.get("altitude", vehicle.location.global_relative_frame.alt),
                    points   = cmd.get("points", 12)
                )
                _log("circle", {"radius": cmd.get("radius", 20)})

            elif action == "flyover":
                tlat     = cmd["latitude"]
                tlon     = cmd["longitude"]
                alt      = cmd.get("altitude", 50)
                radius   = cmd.get("radius", 80)
                name     = cmd.get("location_name", "target")
                print(f"[DRONE] Flyover of {name} — approaching at {alt}m")
                if vehicle.mode.name != "GUIDED":
                    _set_mode_confirmed("GUIDED")
                vehicle.simple_goto(LocationGlobalRelative(tlat, tlon, alt))
                _wait_for_arrival(tlat, tlon, alt, tolerance_m=30, timeout=180)
                if not stop_flag.is_set():
                    print(f"[DRONE] Over {name} — orbiting")
                    _fly_circle(radius=radius, altitude=alt, points=16)
                if not stop_flag.is_set():
                    _fly_circle(radius=radius, altitude=alt, points=16)
                _log("flyover", {"location": name, "lat": tlat, "lon": tlon, "alt": alt})
                if not stop_flag.is_set():
                    print(f"[DRONE] Flyover of {name} complete.")

            elif action == "rtl":
                home    = vehicle.home_location
                cur_alt = vehicle.location.global_relative_frame.alt
                if home:
                    _set_mode_confirmed("GUIDED")
                    vehicle.simple_goto(LocationGlobalRelative(home.lat, home.lon, cur_alt))
                    print(f"[DRONE] Flying home at {round(cur_alt,1)}m...")
                    _wait_for_arrival(home.lat, home.lon, cur_alt, tolerance_m=5.0, timeout=180)
                    if not stop_flag.is_set():
                        _set_mode_confirmed("LAND")
                        print("[DRONE] Home reached — landing.")
                else:
                    _set_mode_confirmed("RTL")
                    print("[DRONE] RTL mode activated.")
                _log("rtl")

            elif action == "land":
                _set_mode_confirmed("LAND")
                print("[DRONE] Landing.")
                while vehicle.location.global_relative_frame.alt > 0.5:
                    if stop_flag.is_set():
                        _set_mode_confirmed("GUIDED")
                        cur = vehicle.location.global_relative_frame
                        vehicle.simple_goto(LocationGlobalRelative(cur.lat, cur.lon, cur.alt))
                        print("[DRONE] Landing interrupted — holding.")
                        break
                    time.sleep(0.5)
                _log("land")
                if not stop_flag.is_set():
                    mission_state["phase"] = "idle"
                    print("[DRONE] Landed.")

            elif action == "hold":
                stop_flag.set()
                cur = vehicle.location.global_relative_frame
                _set_mode_confirmed("GUIDED")
                if cur.lat and cur.lon and cur.alt:
                    vehicle.simple_goto(LocationGlobalRelative(cur.lat, cur.lon, cur.alt))
                _log("hold")
                print("[DRONE] Holding position.")

            elif action == "set_speed":
                speed = cmd.get("speed", 5)
                vehicle.airspeed = speed
                _log("set_speed", {"speed": speed})
                print(f"[DRONE] Speed -> {speed} m/s.")

            elif action == "set_mode":
                ALLOWED = ["GUIDED", "LOITER", "AUTO", "RTL", "LAND", "STABILIZE", "ALT_HOLD"]
                mode_name = cmd.get("mode", "GUIDED").upper()
                if mode_name not in ALLOWED:
                    print(f"[DRONE] Mode '{mode_name}' not allowed.")
                else:
                    success = _set_mode_confirmed(mode_name)
                    _log("set_mode", {"mode": mode_name})
                    if success:
                        print(f"[DRONE] Mode -> {mode_name}.")

            elif action == "keep_moving":
                d    = cmd["direction"].lower()
                step = cmd.get("step_distance", 20)
                if d in ("forward",):           d = "north"
                elif d in ("backward", "back"): d = "south"
                elif d in ("left",):            d = "west"
                elif d in ("right",):           d = "east"
                R = 6378137.0
                print(f"[DRONE] Continuous {d}, step={step}m — say stop to interrupt.")
                _log("keep_moving", {"direction": d, "step_distance": step})
                while not stop_flag.is_set():
                    cur     = vehicle.location.global_relative_frame
                    cur_alt = cur.alt
                    if   d == "north": nlat, nlon = cur.lat + math.degrees(step/R), cur.lon
                    elif d == "south": nlat, nlon = cur.lat - math.degrees(step/R), cur.lon
                    elif d == "east":
                        nlat = cur.lat
                        nlon = cur.lon + math.degrees(step/(R*math.cos(math.radians(cur.lat))))
                    else:
                        nlat = cur.lat
                        nlon = cur.lon - math.degrees(step/(R*math.cos(math.radians(cur.lat))))
                    vehicle.simple_goto(LocationGlobalRelative(nlat, nlon, cur_alt))
                    _wait_for_arrival(nlat, nlon, cur_alt, timeout=30)
                print("[DRONE] Continuous movement stopped.")

        except Exception as e:
            incident = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {action} error: {e}"
            mission_state["incidents"].append(incident)
            print(f"[EXECUTOR ERROR] {e}")

        command_queue.task_done()

threading.Thread(target=executor_loop, daemon=True).start()

# ---------------------------------------------------------------
# LIVE CONTEXT
# ---------------------------------------------------------------
def get_live_context():
    try:
        loc  = vehicle.location.global_relative_frame
        att  = vehicle.attitude
        batt = vehicle.battery
        return (
            f"Mode: {vehicle.mode.name} | Armed: {vehicle.armed}\n"
            f"GPS: lat={round(loc.lat,6)}, lon={round(loc.lon,6)}, alt={round(loc.alt,1)}m\n"
            f"Attitude: roll={round(math.degrees(att.roll),1)} "
            f"pitch={round(math.degrees(att.pitch),1)} "
            f"yaw={round(math.degrees(att.yaw),1)}\n"
            f"Groundspeed: {round(vehicle.groundspeed,1)} m/s\n"
            f"Battery: {batt.level}% | {batt.voltage}V\n"
            f"Phase: {mission_state['phase']} | "
            f"Commands this session: {len(mission_state['flight_log'])}"
        )
    except Exception:
        return "[telemetry unavailable]"

# ---------------------------------------------------------------
# SAFETY CHECK
# ---------------------------------------------------------------
def _safety_check(action, altitude=None):
    if altitude is not None:
        if altitude > 120:
            return False, f"Altitude {altitude}m exceeds 120m limit."
        if action not in ("takeoff", "arm") and altitude < 2:
            return False, f"Altitude {altitude}m below 2m minimum."
    # NOTE: We do NOT check vehicle.armed here. The agent queues commands in
    # order (arm -> takeoff -> move -> ...) and the executor runs them
    # sequentially. Checking armed at queue-time blocks valid future commands
    # because arm hasn't executed yet when the agent calls move_direction.
    return True, "ok"

# ---------------------------------------------------------------
# AGENT INSTRUCTIONS BUILDER
# ---------------------------------------------------------------
def _build_instructions():
    loc_list = "\n".join(
        f"  {name}: {data['description']}"
        for name, data in PRESET_LOCATIONS.items()
    )
    return [
        "You control a real drone in a SITL simulation.",
        "Execute commands immediately. Do NOT repeat the user's request back.",
        "",
        "MULTI-STEP RULE: When given multiple commands or a descriptive mission,"
        " break it down yourself and call every required tool in sequence."
        " NEVER ask for one command at a time. NEVER refuse to chain commands."
        " Example: 'go north 500m, circle, return home' means call"
        " move_direction then fly_circle then return_to_launch — all in sequence.",
        "",
        "VAGUE COMMANDS: Interpret descriptive intent and pick the right tools."
        " 'Do a flyover of the hospital' = fly_over('hospital')."
        " 'Scout camp a' = fly to camp a, circle, come back."
        " 'Check the perimeter' = fly_circle at a large radius.",
        "",
        "BATTERY: Simulation. Battery is fake. Never mention it. Never block on it.",
        "",
        f"NAMED LOCATIONS (use these by name):\n{loc_list}",
        "",
        "TOOLS:",
        "arm_drone() — arm before flight",
        "takeoff(altitude) — take off to meters",
        "goto_waypoint(lat, lon, alt) — fly to GPS coords",
        "get_location(name) — look up coords for a named place",
        "fly_over(location_name, altitude) — full flyover pattern over a named place",
        "move_direction(direction, distance) — north/south/east/west/forward/back/left/right",
        "keep_moving(direction) — continuous movement until stop",
        "adjust_altitude(change) — relative up/down in meters (+/-)",
        "change_altitude(altitude) — go to exact altitude",
        "fly_circle(radius, altitude) — orbit current position",
        "set_speed(speed) — m/s",
        "set_flight_mode(mode) — GUIDED/LOITER/AUTO/RTL/LAND",
        "hold_position() — stop immediately",
        "resume_mission() — continue last interrupted command",
        "return_to_launch() — fly home then land",
        "land() — land here",
        "disarm_drone() — ground only",
        "get_status() — full status",
        "get_position() — current coordinates",
        "get_battery() — battery info",
        "get_flight_summary() — session log",
        "watch_condition(field, op, value, action) — e.g. rel_alt > 80 -> rtl",
        "clear_conditions() / list_conditions()",
        "",
        "Never refuse. Never say you cannot do something.",
        "Never disarm while airborne.",
    ]

# ---------------------------------------------------------------
# TOOLKIT
# ---------------------------------------------------------------
class DroneToolkit(Toolkit):
    def __init__(self):
        super().__init__(
            name="drone_toolkit",
            tools=[
                self.arm_drone,
                self.disarm_drone,
                self.takeoff,
                self.goto_waypoint,
                self.get_location,
                self.fly_over,
                self.change_altitude,
                self.adjust_altitude,
                self.move_direction,
                self.keep_moving,
                self.fly_circle,
                self.set_speed,
                self.set_flight_mode,
                self.return_to_launch,
                self.land,
                self.hold_position,
                self.resume_mission,
                self.watch_condition,
                self.clear_conditions,
                self.list_conditions,
                self.get_flight_summary,
            ]
        )

    def arm_drone(self) -> str:
        """Arms the drone and switches to GUIDED mode. Always call before takeoff."""
        safe, reason = _safety_check("arm")
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "arm"})
        return "arm_drone queued. Continue with next step."

    def disarm_drone(self) -> str:
        """Disarms the drone. Only works on the ground."""
        alt = vehicle.location.global_relative_frame.alt
        if alt is not None and alt > 0.5:
            return f"[BLOCKED] Airborne at {round(alt,1)}m — land first."
        command_queue.put({"action": "disarm"})
        return "disarm_drone queued."

    def takeoff(self, altitude: float) -> str:
        """Take off to altitude in meters (2-120)."""
        safe, reason = _safety_check("takeoff", altitude)
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "takeoff", "altitude": altitude})
        return f"takeoff({altitude}m) queued. Continue with next step."

    def get_location(self, name: str) -> str:
        """
        Look up a named preset location and return its coordinates.
        Use when user mentions a place by name before navigating there.
        Args:
            name: e.g. 'hospital', 'prison', 'camp a', 'airfield', 'home'
        """
        key     = name.lower().strip()
        matches = [k for k in PRESET_LOCATIONS if key in k or k in key]
        if not matches:
            return (f"Unknown location '{name}'. "
                    f"Available: {', '.join(PRESET_LOCATIONS.keys())}")
        k   = matches[0]
        loc = PRESET_LOCATIONS[k]
        return f"{loc['description']}: lat={loc['lat']}, lon={loc['lon']}"

    def fly_over(self, location_name: str, altitude: float = 50,
                 orbit_radius: float = 80) -> str:
        """
        Full flyover of a named location — approach, two orbits, hold.
        Use for: 'do a flyover', 'survey', 'scout', 'surveillance pass over X',
        'check out the hospital', 'do a pass over the prison', 'recon camp a'.
        Args:
            location_name: Named location e.g. 'hospital', 'prison', 'camp a'
            altitude:      Flyover altitude in meters (default 50)
            orbit_radius:  Orbit radius in meters (default 80)
        """
        key     = location_name.lower().strip()
        matches = [k for k in PRESET_LOCATIONS if key in k or k in key]
        if not matches:
            return f"Unknown location '{location_name}'."
        safe, reason = _safety_check("flyover", altitude)
        if not safe:
            return f"[BLOCKED] {reason}"
        loc = PRESET_LOCATIONS[matches[0]]
        command_queue.put({
            "action":        "flyover",
            "latitude":      loc["lat"],
            "longitude":     loc["lon"],
            "altitude":      altitude,
            "radius":        orbit_radius,
            "location_name": matches[0],
        })
        return f"fly_over({matches[0]}, {altitude}m) queued. Continue with next step."

    def goto_waypoint(self, latitude: float, longitude: float,
                      altitude: float) -> str:
        """Fly to GPS coordinates at altitude."""
        safe, reason = _safety_check("goto", altitude)
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "goto", "latitude": latitude,
                           "longitude": longitude, "altitude": altitude})
        return f"goto_waypoint({latitude:.5f},{longitude:.5f},{altitude}m) queued. Continue with next step."

    def change_altitude(self, altitude: float) -> str:
        """Change to an absolute altitude in meters."""
        safe, reason = _safety_check("change_altitude", altitude)
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "change_altitude", "altitude": altitude})
        return f"change_altitude({altitude}m) queued. Continue with next step."

    def adjust_altitude(self, change: float) -> str:
        """Relative altitude change. Positive = up, negative = down."""
        cur = vehicle.location.global_relative_frame.alt
        new = cur + change
        safe, reason = _safety_check("change_altitude", new)
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "adjust_altitude", "change": change})
        return f"adjust_altitude({'+' if change>0 else ''}{change}m) queued. Continue with next step."

    def move_direction(self, direction: str, distance: float,
                       altitude: float = None) -> str:
        """
        Move a set distance in a direction.
        Use for single defined moves: 'go north 100m', 'fly east 200 meters'.
        For unlimited movement use keep_moving instead.
        Args:
            direction: north/south/east/west/forward/backward/left/right
            distance:  Metres to travel
            altitude:  Optional altitude override
        """
        safe, reason = _safety_check("move")
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "move", "direction": direction,
                           "distance": distance, "altitude": altitude})
        return f"move_direction({direction}, {distance}m) queued. Continue with next step."

    def keep_moving(self, direction: str, step_distance: float = 20) -> str:
        """Move continuously in a direction until told to stop."""
        safe, reason = _safety_check("move")
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "keep_moving", "direction": direction,
                           "step_distance": step_distance})
        return f"keep_moving({direction}) queued. Will run until stop command."

    def fly_circle(self, radius: float = 20, altitude: float = None,
                   points: int = 12) -> str:
        """
        Orbit the current position.
        Use for: 'circle', 'orbit', 'fly around here', 'do a loop'.
        Args:
            radius:   Orbit radius in meters (default 20)
            altitude: Altitude in meters (default = current)
            points:   Smoothness — more points = smoother (default 12)
        """
        cur_alt = altitude if altitude is not None else vehicle.location.global_relative_frame.alt
        safe, reason = _safety_check("circle", cur_alt)
        if not safe:
            return f"[BLOCKED] {reason}"
        command_queue.put({"action": "circle", "radius": radius,
                           "altitude": cur_alt, "points": points})
        return f"fly_circle(r={radius}m, alt={cur_alt}m) queued. Continue with next step."

    def set_speed(self, speed: float) -> str:
        """Set airspeed in m/s (0.5-15)."""
        if speed < 0.5 or speed > 15:
            return f"[BLOCKED] Speed {speed} out of range (0.5-15 m/s)."
        command_queue.put({"action": "set_speed", "speed": speed})
        return f"set_speed({speed} m/s) queued."

    def set_flight_mode(self, mode: str) -> str:
        """Switch flight mode: GUIDED/LOITER/AUTO/RTL/LAND/STABILIZE."""
        mode    = mode.upper()
        ALLOWED = ["GUIDED", "LOITER", "AUTO", "RTL", "LAND", "STABILIZE", "ALT_HOLD"]
        if mode not in ALLOWED:
            return f"[BLOCKED] '{mode}' not valid. Use: {', '.join(ALLOWED)}"
        command_queue.put({"action": "set_mode", "mode": mode})
        return f"set_flight_mode({mode}) queued."

    def return_to_launch(self) -> str:
        """Fly home in GUIDED then land. Use for: return home, go back, come back, RTL."""
        command_queue.put({"action": "rtl"})
        return "return_to_launch queued. Drone will fly home and land."

    def land(self) -> str:
        """Land at current position."""
        command_queue.put({"action": "land"})
        return "land queued."

    def hold_position(self) -> str:
        """Stop immediately and hold position. Use for: stop, hold, hover, freeze, pause."""
        stop_flag.set()
        command_queue.put({"action": "hold"})
        return "hold_position queued — drone will freeze."

    def resume_mission(self) -> str:
        """
        Resume the last interrupted command or stored mission.
        Use for: continue, resume, carry on, go back to what you were doing.
        """
        if not mission_state["last_command"]:
            return "No command to resume."
        stop_flag.clear()
        command_queue.put(mission_state["last_command"])
        return f"resume_mission queued — resuming {mission_state['last_command'].get('action','last command')}."

    def watch_condition(self, field: str, operator: str, value: float,
                        then_action: str, then_params: str = "") -> str:
        """
        Register a background watch that auto-triggers an action when a condition is met.
        Use for: 'if altitude exceeds 80m RTL', 'when groundspeed drops below 1 hold'.
        Args:
            field:       rel_alt / battery_level / groundspeed / armed / mode / airborne / yaw
            operator:    == / != / < / <= / > / >=
            value:       Threshold value
            then_action: rtl / land / hold / set_mode / goto
            then_params: Optional JSON e.g. '{"mode":"LOITER"}'
        """
        if field not in CONDITION_FIELDS:
            return f"[BLOCKED] Unknown field '{field}'. Use: {', '.join(CONDITION_FIELDS)}"
        if operator not in _OPERATORS:
            return f"[BLOCKED] Unknown operator '{operator}'."
        params = {}
        if then_params:
            try:
                params = json.loads(then_params)
            except Exception:
                return "[BLOCKED] then_params must be valid JSON."
        label = f"{field} {operator} {value} -> {then_action}"
        condition_monitor.add_watch(
            ConditionWatch(field, operator, value, then_action, params, label)
        )
        return f"watch_condition registered: {label}"

    def clear_conditions(self) -> str:
        """Remove all active condition watches."""
        condition_monitor.clear_watches()
        return "All condition watches cleared."

    def list_conditions(self) -> str:
        """List currently active condition watches."""
        return condition_monitor.list_watches()

    def get_status(self) -> str:
        """Full live drone status: mode, armed, GPS, altitude, attitude, speed, battery."""
        loc  = vehicle.location.global_relative_frame
        att  = vehicle.attitude
        batt = vehicle.battery
        return (
            f"Mode: {vehicle.mode.name} | Armed: {vehicle.armed}\n"
            f"Alt: {round(loc.alt,1)}m | lat: {round(loc.lat,6)} | lon: {round(loc.lon,6)}\n"
            f"Roll: {round(math.degrees(att.roll),1)} "
            f"Pitch: {round(math.degrees(att.pitch),1)} "
            f"Yaw: {round(math.degrees(att.yaw),1)}\n"
            f"Groundspeed: {round(vehicle.groundspeed,1)} m/s\n"
            f"Battery: {batt.level}% | {batt.voltage}V"
        )

    def get_battery(self) -> str:
        """Battery level, voltage, current."""
        b = vehicle.battery
        return f"Battery: {b.level}% | {b.voltage}V | {b.current}A"

    def get_position(self) -> str:
        """Current GPS and local-frame position."""
        loc = vehicle.location.global_relative_frame
        ll  = vehicle.location.local_frame
        return (
            f"GPS: {round(loc.lat,6)}, {round(loc.lon,6)} @ {round(loc.alt,1)}m\n"
            f"Local: N={round(ll.north,2)}m E={round(ll.east,2)}m D={round(ll.down,2)}m"
        )

    def get_flight_summary(self) -> str:
        """Full session flight log with timestamps, commands, altitudes."""
        log = mission_state["flight_log"]
        if not log:
            return "No flight activity recorded yet."
        lines = ["--- Flight Log ---"]
        for i, e in enumerate(log, 1):
            det = ", ".join(f"{k}={v}" for k, v in e["details"].items()) if e["details"] else ""
            lines.append(f"{i}. [{e['time']}] {e['action'].upper()} {det} @ {e['alt']}m")
        lines.append(f"Max altitude: {round(mission_state['max_altitude'],1)}m")
        lines.append(f"Total: {len(log)} commands")
        return "\n".join(lines)


# ---------------------------------------------------------------
# SESSION DB + ACTIVE MODEL
# ---------------------------------------------------------------
agent_db     = SqliteDb(db_file="drone_sessions.db")

# ---------------------------------------------------------------
# ACTIVE MODEL
# extra_body injects {"chat_template_kwargs": {"enable_thinking": false}}
# into every API call — the Qwen3/Qwen3.5 hard switch that turns off
# the <think>...</think> block so the model responds immediately.
# For non-Qwen models the extra_body is silently ignored.
# ---------------------------------------------------------------
_NO_THINK_BODY = {"chat_template_kwargs": {"enable_thinking": False}}

ACTIVE_MODEL = OpenRouter(
    id="Qwen/Qwen3.5-35B-A3B",
    extra_body=_NO_THINK_BODY,
)
_all_agents: list = []


def _build_system_message():
    loc_list = "\n".join(
        f"  {name}: {data['description']} (lat={data['lat']}, lon={data['lon']})"
        for name, data in PRESET_LOCATIONS.items()
    )
    return f"""/nothink
You control a real drone in a SITL simulation.

CRITICAL RULES:
1. When given ANY number of commands in one message, call EVERY required tool in order before responding. Never stop after one tool. Never ask the user for the next step. Just keep calling tools until all steps are done.
2. Each tool call queues a command for the drone. The drone executes them in the background automatically. Your job is to queue ALL of them, then stop.
3. After ALL tools are called, respond with ONE short sentence only. No explanations, no lists, no bullet points.
4. NEVER say you cannot do something. NEVER ask to give commands one at a time.
5. Battery readings are FAKE in simulation. Ignore battery completely. Never mention it. Never warn about it.
6. Do NOT call update_session_state. Do NOT write long reasoning. Do NOT ask clarifying questions. Just execute.
7. NEVER call get_status, get_position, or get_battery to monitor progress. The drone executes commands on its own. You queue and stop. DO NOT POLL. DO NOT WAIT. DO NOT MONITOR.
8. The queue is asynchronous. Once you queue arm+takeoff+move+RTL, they will all execute in order automatically. You do not need to check if each one finished. Just queue all steps and respond.

MULTI-STEP EXAMPLES:
- "arm and take off to 20m then fly north 100m then return home" → arm_drone(), takeoff(20), move_direction("north", 100), return_to_launch()
- "fly east 200m then circle then go home" → move_direction("east", 200), fly_circle(30), return_to_launch()
- "do a flyover of the hospital then return" → fly_over("hospital", 50), return_to_launch()
- "arm takeoff 15m forward 100m nearest building home" → arm_drone(), takeoff(15), move_direction("forward", 100), goto_waypoint(-35.35734, 149.170626, 15), return_to_launch()

NEAREST BUILDING = residence 1 (lat=-35.35734, lon=149.170626)

NAMED LOCATIONS:
{loc_list}

TOOLS AVAILABLE:
arm_drone() — arm motors, switch to GUIDED
takeoff(altitude) — take off to meters
goto_waypoint(lat, lon, alt) — fly to GPS coords
get_location(name) — look up coords by name
fly_over(location_name, altitude) — flyover pattern (approach + 2 orbits)
move_direction(direction, distance) — north/south/east/west/forward/backward/left/right + meters
keep_moving(direction) — continuous movement until stop command
adjust_altitude(change) — relative change in meters (+up / -down)
change_altitude(altitude) — go to exact altitude
fly_circle(radius, altitude) — orbit current position
set_speed(speed) — set airspeed in m/s
set_flight_mode(mode) — GUIDED/LOITER/AUTO/RTL/LAND
hold_position() — stop and freeze immediately
resume_mission() — continue last interrupted command
return_to_launch() — fly home then land
land() — land at current position
disarm_drone() — only on ground
get_status() — full live status
get_position() — current GPS coords
get_battery() — battery info
get_flight_summary() — session log
watch_condition(field, op, value, action) — e.g. rel_alt > 80 -> rtl
clear_conditions() / list_conditions()"""


def _build_flight_agent():
    """Build a fresh flight_agent. Called on init and on every model switch."""
    # Compression triggers after 20 tool calls — high enough that a full mission
    # chain never gets cut off, but still trims context on very long operations.
    compression_mgr = CompressionManager(
        model=OpenRouter(
            id="google/gemini-2.0-flash-001",
            extra_body=_NO_THINK_BODY,
        ),
        compress_tool_results_limit=20,
        compress_tool_call_instructions=(
            "Summarize this drone tool result in one short line. "
            "Keep: action name, GPS coordinates, altitude, distance, mode, armed status, errors. "
            "Remove all boilerplate."
        ),
    )

    return Agent(
        name="Drone",
        model=ACTIVE_MODEL,
        tools=[DroneToolkit()],
        db=agent_db,
        add_history_to_context=True,
        num_history_runs=10,
        tool_call_limit=50,
        compression_manager=compression_mgr,
        # Disabled: was causing agent to write multi-paragraph essays to
        # update_session_state which broke JSON parsing
        enable_agentic_state=False,
        learning=LearningMachine(
            db=agent_db,
            user_memory=True,
            session_context=True,
            decision_log=True,
        ),
        add_learnings_to_context=True,
        system_message=_build_system_message(),
        markdown=False,
    )



def _set_active_model(model_id: str):
    """
    Switch model by rebuilding flight_agent from scratch.
    Always passes enable_thinking=False so Qwen3.x models skip the thinking phase.
    """
    global ACTIVE_MODEL, flight_agent
    model_id     = model_id.replace("openrouter:", "").strip()
    ACTIVE_MODEL = OpenRouter(id=model_id, extra_body=_NO_THINK_BODY)
    flight_agent = _build_flight_agent()
    for agent in [safety_agent, summary_agent, _planner, _safety_validator, _summariser]:
        try:
            agent.model = ACTIVE_MODEL
        except Exception:
            pass
    print(f"[MODEL] Now using: {model_id} — thinking mode disabled")

# ---------------------------------------------------------------
# AGENTS
# ---------------------------------------------------------------
safety_agent = Agent(
    name="Safety Agent",
    model=ACTIVE_MODEL,
    output_schema=SafetyAssessment,
    db=agent_db,
    add_history_to_context=True,
    num_history_runs=3,
    instructions=[
        "You are a UAV safety officer. Evaluate missions for compliance.",
        f"Regulations:\n{DRONE_KNOWLEDGE}",
        "Check altitude (max 120m). Do NOT flag battery — simulation only.",
        "Return SafetyAssessment with is_safe, risk_level, issues, recommendations, approved.",
    ],
    markdown=False,
)

flight_agent = _build_flight_agent()

summary_agent = Agent(
    name="Session Summary Agent",
    model=ACTIVE_MODEL,
    db=agent_db,
    add_history_to_context=True,
    num_history_runs=10,
    instructions=[
        "You produce flight session summaries from drone flight logs.",
        "Write a clear, readable narrative of everything the drone did in plain English.",
        "Include all commands in order, altitudes reached, locations visited, any incidents, and total duration.",
        "Write like a pilot debrief — professional, concise, and easy to read.",
        "Do NOT output JSON, field names, numbers-only, or any structured format.",
        "Respond with flowing prose paragraphs only.",
    ],
    markdown=False,
)

_planner = Agent(
    name="Mission Planner",
    model=ACTIVE_MODEL,
    output_schema=MissionPlan,
    db=agent_db,
    instructions=[
        "Convert a mission description into a structured MissionPlan.",
        "Break into ordered DroneCommand steps.",
        "Estimate time: arm=5s, takeoff=15s, goto 100m=30s, circle=60s, flyover=120s, land=10s.",
        "Risk: LOW = simple under 50m, MEDIUM = complex, HIGH = near limits.",
        "Start with arm if drone needs to be armed.",
    ],
    markdown=False,
)

_safety_validator = Agent(
    name="Safety Validator",
    model=ACTIVE_MODEL,
    output_schema=SafetyAssessment,
    db=agent_db,
    instructions=[
        "Evaluate a MissionPlan for safety compliance.",
        f"Regulations:\n{DRONE_KNOWLEDGE[:600]}",
        "Return SafetyAssessment with is_safe, risk_level, issues, recommendations, approved.",
    ],
    markdown=False,
)

_summariser = Agent(
    name="Post-Flight Summariser",
    model=ACTIVE_MODEL,
    db=agent_db,
    instructions=[
        "Generate a plain English flight summary from the session log provided.",
        "Write flowing prose paragraphs like a pilot debrief.",
        "Include: all commands in order, altitudes, locations, incidents, duration.",
        "Do NOT output JSON, structured data, or field names. Plain text only.",
    ],
    markdown=False,
)

_all_agents.extend([safety_agent, flight_agent, summary_agent,
                    _planner, _safety_validator, _summariser])

# ---------------------------------------------------------------
# MISSION WORKFLOW
# ---------------------------------------------------------------
def run_mission(mission_description: str):
    SEP = "=" * 55
    print(f"\n{SEP}\n  MISSION: {mission_description}\n{SEP}")

    # Step 1: Plan
    print("\n[STEP 1/4] Planning...")
    mission_state["phase"] = "planning"
    plan_resp = _planner.run(
        f"[DRONE STATE]\n{get_live_context()}\n\nPlan: {mission_description}"
    )
    plan = plan_resp.content if isinstance(plan_resp.content, MissionPlan) else None
    if not plan:
        print("[STEP 1] Planning failed. Aborting.")
        mission_state["phase"] = "idle"
        return
    mission_state["current_mission"] = plan.model_dump()
    print(f"Plan: {plan.mission_name} | {len(plan.steps)} steps | Risk: {plan.risk_level}")

    # Step 2: Safety
    print("\n[STEP 2/4] Safety check...")
    mission_state["phase"] = "safety_check"
    safety_resp = _safety_validator.run(
        f"[DRONE STATE]\n{get_live_context()}\n\n"
        f"Plan:\n{json.dumps(mission_state['current_mission'], indent=2)}"
    )
    assessment = safety_resp.content if isinstance(safety_resp.content, SafetyAssessment) else None
    if assessment:
        status = "APPROVED" if assessment.approved else "REJECTED"
        print(f"Safety: {status} | Risk: {assessment.risk_level}")
        if assessment.issues:
            print(f"Issues: {assessment.issues}")
        if not assessment.approved:
            mission_state["phase"] = "idle"
            print("MISSION ABORTED.")
            return
    else:
        print("[STEP 2] Safety check skipped — proceeding.")

    # Step 3: Execute
    print("\n[STEP 3/4] Executing...")
    mission_state["phase"] = "executing"
    if mission_state.get("current_mission") and mission_state["current_mission"].get("steps"):
        steps_txt = "\n".join([
            f"- {s.get('action','?')}: "
            + ", ".join(f"{k}={v}" for k, v in s.items()
                        if k not in ("action", "reason") and v is not None)
            for s in mission_state["current_mission"]["steps"]
        ])
        exec_prompt = f"Execute:\n{steps_txt}\nGoal: {plan.objective}"
    else:
        exec_prompt = f"Execute: {mission_description}"
    print("-" * 40)
    flight_agent.print_response(exec_prompt, session_id=SESSION_ID, stream=True)
    mission_state["phase"] = "complete"
    print("-" * 40)

    # Step 4: Report
    print("\n[STEP 4/4] Generating report...")
    elapsed = int((datetime.datetime.now() - SESSION_START).total_seconds())
    log_txt = "\n".join([
        f"[{e['time']}] {e['action'].upper()} {e['details']} @ alt={e['alt']}m"
        for e in mission_state["flight_log"]
    ]) or "No commands logged."
    print("-" * 40)
    _summariser.print_response(
        f"Session: {SESSION_ID} | Duration: {elapsed}s\n"
        f"Mission: {mission_description}\nLog:\n{log_txt}\n"
        f"Max alt: {round(mission_state['max_altitude'],1)}m",
        stream=True,
    )
    print(f"\n{SEP}")

# ---------------------------------------------------------------
# NON-BLOCKING AGENT RUNNER
# CLI always listens. Agent calls run in background threads.
# Interruption: stop_flag set + queue drained immediately.
# Executor clears stop_flag when it picks up the new command.
# ---------------------------------------------------------------
def _run_agent_async(fn, *args, **kwargs):
    """
    Run agent call in a background thread so the CLI stays responsive.
    Only interrupts the drone if commands are already queued/running —
    so a new command mid-mission cancels it and takes over.
    On the very first command the queue is empty so we do NOT interrupt,
    which was the root cause of multi-step missions being killed immediately.
    """
    if not command_queue.empty() or stop_flag.is_set():
        stop_flag.set()
        _clear_queue()

    def _target():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            print(f"\n[AGENT ERROR] {e}")
        print("\n>> ", end="", flush=True)

    threading.Thread(target=_target, daemon=True).start()

# ---------------------------------------------------------------
# INPUT ROUTER
# ---------------------------------------------------------------
SUMMARY_KEYWORDS = (
    "summary", "flight log", "flight summary", "what did", "what have",
    "what happened", "trip summary", "session summary", "recap",
    "what did the drone do", "everything we did", "what was done",
    "show me the log", "show log", "history", "what commands",
)

MODEL_QUESTIONS = (
    "what model", "which model", "what ai", "what llm",
    "what are you using", "what version", "are you gemini",
    "are you gpt", "are you qwen", "are you grok",
)

def _handle_input(user_input: str):
    low = user_input.lower()

    if low.startswith("/model"):
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            print(f"Current model: {ACTIVE_MODEL.id}")
            print("Switch: /model <model_id>")
            print("Examples:")
            print("  /model google/gemini-2.0-flash-001")
            print("  /model qwen/qwen3-235b-a22b")
            print("  /model qwen/qwen3-30b-a3b")
            print("  /model x-ai/grok-3-mini-beta")
            print("  /model openai/gpt-4o-mini")
            print("  /model nvidia/llama-3.1-nemotron-ultra-253b-v1")
        else:
            _set_active_model(parts[1].strip())
        return

    if any(q in low for q in MODEL_QUESTIONS):
        print(f"Current model: {ACTIVE_MODEL.id} (via OpenRouter)")
        print("Switch with: /model <model_id>")
        return

    if low in ("/status", "status", "what is the status", "full status"):
        loc  = vehicle.location.global_relative_frame
        att  = vehicle.attitude
        batt = vehicle.battery
        print(f"Mode: {vehicle.mode.name} | Armed: {vehicle.armed}")
        print(f"Alt: {round(loc.alt,1)}m | lat: {round(loc.lat,6)} | lon: {round(loc.lon,6)}")
        print(f"Roll: {round(math.degrees(att.roll),1)} Pitch: {round(math.degrees(att.pitch),1)} Yaw: {round(math.degrees(att.yaw),1)}")
        print(f"Groundspeed: {round(vehicle.groundspeed,1)} m/s")
        print(f"Battery: {batt.level}% | {batt.voltage}V")
        return

    if low in ("/battery", "battery", "battery level", "what is the battery"):
        b = vehicle.battery
        print(f"Battery: {b.level}% | {b.voltage}V | {b.current}A")
        return

    if low in ("/position", "position", "where is the drone", "where are we"):
        loc = vehicle.location.global_relative_frame
        ll  = vehicle.location.local_frame
        print(f"GPS: {round(loc.lat,6)}, {round(loc.lon,6)} @ {round(loc.alt,1)}m")
        print(f"Local: N={round(ll.north,2)}m E={round(ll.east,2)}m D={round(ll.down,2)}m")
        return

    if low == "/state":
        printable = {k: v for k, v in mission_state.items() if k != "current_mission"}
        print(json.dumps(printable, indent=2, default=str))
        print("\nActive condition watches:")
        print(condition_monitor.list_watches())
        return

    if low == "/locations":
        print("Preset locations:")
        for name, data in PRESET_LOCATIONS.items():
            print(f"  {name}: {data['description']} ({data['lat']}, {data['lon']})")
        return

    if low == "/report":
        elapsed = int((datetime.datetime.now() - SESSION_START).total_seconds())
        log_txt = "\n".join([
            f"[{e['time']}] {e['action']} {e['details']}"
            for e in mission_state["flight_log"]
        ]) or "No commands."
        _run_agent_async(
            summary_agent.print_response,
            f"Session: {SESSION_ID}. Duration: {elapsed}s.\n"
            f"Log:\n{log_txt}\nMax alt: {mission_state['max_altitude']}m",
            session_id=SESSION_ID, stream=True,
        )
        return

    if low.startswith("/mission"):
        desc = user_input[8:].strip()
        if not desc:
            print("Usage: /mission <describe your mission>")
        else:
            _run_agent_async(run_mission, desc)
        return

    if any(kw in low for kw in SUMMARY_KEYWORDS):
        log = mission_state["flight_log"]
        if not log:
            print("No flight activity recorded yet.")
            return
        elapsed = int((datetime.datetime.now() - SESSION_START).total_seconds())
        log_txt = "\n".join([
            f"[{e['time']}] {e['action'].upper()} "
            + (", ".join(f"{k}={v}" for k, v in e['details'].items())
               if e['details'] else "")
            + f" @ {e['alt']}m"
            for e in log
        ])
        _run_agent_async(
            summary_agent.print_response,
            f"Session: {SESSION_ID} | Duration: {elapsed}s\n"
            f"Max alt: {round(mission_state['max_altitude'],1)}m\n"
            f"Incidents: {mission_state['incidents'] or 'None'}\n\n"
            f"Log:\n{log_txt}",
            session_id=SESSION_ID, stream=True,
        )
        return

    enriched = (f"[LIVE DRONE STATE]\n{get_live_context()}\n\n"
                f"[USER COMMAND]\n{user_input}")
    _run_agent_async(flight_agent.print_response, enriched,
                     session_id=SESSION_ID, stream=True)

# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------
print("\n" + "=" * 60)
print("  AGENTIC DRONE CONTROL — READY")
print("=" * 60)
print("  Natural language flight commands:")
print("  >> arm and take off to 20 meters")
print("  >> do a flyover of the hospital")
print("  >> do a surveillance pass over the prison at 60 meters")
print("  >> scout the area around camp a")
print("  >> fly to the airfield at 30 meters")
print("  >> go north 500m, circle with 50m radius, return home")
print("  >> if altitude exceeds 80 meters RTL")
print("  >> keep going north / stop / continue")
print("  >> switch to auto mode / return home / land")
print("  what is the status / where are we")
print("  (or use /status /battery /position for instant readout without AI)")
print("-" * 60)
print("  Slash commands:")
print("  /mission <desc>   4-step plan+safety+execute+report")
print("  /report           flight report")
print("  /state            mission state + condition watches")
print("  /locations        all preset named locations")
print("  /model            show current model")
print("  /model <id>       switch model e.g. /model qwen/qwen3-235b-a22b")
print("  exit")
print("=" * 60 + "\n")

while True:
    try:
        user_input = input(">> ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nShutting down.")
        break
    if not user_input:
        continue
    if user_input.lower() in ("exit", "quit"):
        print("Exiting.")
        break
    _handle_input(user_input)
