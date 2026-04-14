# ava-drone-agent-script
Agentic AI drone control system using DroneKit, SITL, Agno, and OpenRouter — natural language flight commands with multi-step mission execution


# AVA Drone Agent

A natural language drone control system built on top of DroneKit and the Agno agent framework. You talk to it like a person — it breaks down your commands, queues them in order, and the drone executes them autonomously. The agent understands multi-step missions, conditional logic, named locations, and learns from each session.

---

## Setup

For environment setup, DroneKit installation, and SITL configuration, follow the guide here:  
**(https://github.com/igsxf22/flight_manual/blob/main/win_install_dronekit_2026.md)**

Once your environment is ready, install the Python dependencies:

```bash
pip install agno pydantic openai
```

Then open `drone_agent.py` and set your OpenRouter API key on line 20:

```python
os.environ["OPENROUTER_API_KEY"] = "your-key-here"
```

Run the script:

```bash
python drone_agent.py
```

---

## How It Works

The script is built around one core idea: every command you type gets translated by an AI agent into a sequence of tool calls, each of which puts a command into a thread-safe queue. A separate executor thread runs in the background, pulling commands from that queue and sending them to the drone one at a time. The AI and the drone operate independently — the agent finishes deciding what to do and returns control to you immediately, while the drone keeps executing in the background.

```
You type a command
      |
      v
AI Agent (Agno + OpenRouter)
      |
      v
DroneToolkit — queues commands into command_queue
      |
      v
Executor Thread — runs commands sequentially
      |
      v
DroneKit — sends MAVLink messages to SITL/drone
      |
      v
Unreal Engine — receives position via TCP relay at 60Hz
```

---

## Script Sections

### DroneKit Connection
The script opens a single TCP connection to the vehicle at `127.0.0.1:5763` on startup. All tools and the executor share this one connection — there is no reconnection logic needed because everything runs in the same process.

### TCP Relay — Unreal Engine Stream
A background thread runs at 60Hz, reading the drone's local-frame position and attitude and sending it to Unreal Engine over a TCP socket. This is what drives the 3D visualization. It runs completely independently of the agent and the executor.

### State Management
A dictionary called `mission_state` is the single source of truth shared across every part of the script — the tools, the executor, the agent, and the CLI. It tracks the flight log, max altitude reached, battery readings, any blocked or failed commands, the last executed command (for resume), and the current mission phase.

### Command Queue and Executor
The executor thread runs in an infinite loop, pulling commands from `command_queue` one at a time and executing them against the vehicle. Each command is a plain Python dictionary with an `action` key and any relevant parameters. The executor handles all blocking behavior — waiting for the drone to reach altitude, arrive at a waypoint, complete a circle — so the agent never has to wait.

A `stop_flag` threading event allows any active movement to be interrupted cleanly. When the flag is set, whatever loop the executor is in (`_wait_for_arrival`, `_fly_circle`, `keep_moving`) checks it on every 0.5s tick and exits early.

### Condition Monitor
A second background thread ticks every 0.5s and evaluates any registered condition watches against a live snapshot of vehicle state. When a condition is met it clears the queue, sets the stop flag, and queues the trigger action. This is what allows commands like "switch to AUTO when altitude reaches 15 meters" or "RTL if altitude exceeds 80 meters."

Available fields to watch: `rel_alt`, `battery_level`, `battery_voltage`, `groundspeed`, `armed`, `mode`, `airborne`, `yaw`.

### DroneToolkit
All flight commands are organized into an Agno `Toolkit` class. Each method maps to one executor action. Tools return a short "queued" string immediately — they do not wait for the drone to finish — which tells the AI to keep calling the next tool without pausing. This is what enables full mission chaining in a single turn.

Available tools:

| Tool | What it does |
|---|---|
| `arm_drone` | Arms motors, switches to GUIDED |
| `takeoff(altitude)` | Takes off to meters |
| `goto_waypoint(lat, lon, alt)` | Flies to GPS coordinates |
| `get_location(name)` | Looks up a named preset location |
| `fly_over(location, altitude)` | Approach + two orbit passes over a named location |
| `move_direction(direction, distance)` | Moves north/south/east/west/forward/back |
| `keep_moving(direction)` | Continuous movement until stopped |
| `adjust_altitude(change)` | Relative altitude change (+up / -down) |
| `change_altitude(altitude)` | Go to exact altitude |
| `fly_circle(radius, altitude)` | Orbit current position |
| `set_speed(speed)` | Set airspeed in m/s |
| `set_flight_mode(mode)` | Switch to GUIDED / LOITER / AUTO / RTL / LAND |
| `hold_position` | Stop and freeze immediately |
| `resume_mission` | Continue last interrupted command |
| `return_to_launch` | Fly home in GUIDED then land |
| `land` | Land at current position |
| `disarm_drone` | Disarm motors (ground only) |
| `watch_condition` | Register a live condition trigger |
| `clear_conditions` | Remove all active condition watches |
| `list_conditions` | Show active condition watches |
| `get_flight_summary` | Return the full session flight log |

### Preset Locations
A dictionary of named places near the default SITL spawn point in Canberra, Australia. The agent resolves location names automatically — you can say "fly to the hospital" or "do a flyover of the prison" without providing coordinates.

| Name | Description |
|---|---|
| home | SITL launch point |
| airfield | Canberra Airfield |
| hospital | Mugga Mugga Hospital |
| prison | West Jerrabomberra Prison |
| camp a / camp b | West Jerrabomberra camps |
| reserve | West Jerrabomberra Reserve |
| residence 1 / residence 2 | Jerrabomberra residences |
| creek south | Jerrabomberra Creek South |
| runway 35 / runway 17 | Airfield runways |

### AI Agent
The flight agent is built with Agno and connects to any model on OpenRouter. It receives live drone telemetry prepended to every prompt so it always knows the current state. It is configured with:

- `tool_call_limit=50` — can call up to 50 tools in a single turn, enabling full mission chains
- `compression_manager` — compresses tool result history after every 20 calls to keep context lean
- `LearningMachine` — captures user preferences, session context, and decisions across sessions
- `system_message` — explicit rules including the multi-step execution pattern, no-polling rule, and SITL battery note

The model can be switched live from the CLI without restarting the script.

### Non-Blocking CLI
The CLI loop runs on the main thread and only calls `input()`. Every agent call runs in a background daemon thread, so the prompt is always available. Typing a new command while the drone is mid-mission will interrupt the current operation and start the new one — the queue is cleared and the stop flag is set only if something is already running.

---

## Usage

### Flight Commands

Talk to it naturally. The agent understands intent and chains tools automatically:

```
>> arm and take off to 20 meters
>> fly north 500 meters then circle then return home
>> arm, take off to 15m, fly north 100m, fly to the hospital, return home and land
>> do a flyover of the prison at 60 meters
>> scout camp a
>> keep going east
>> stop
>> continue
>> switch to auto mode
>> if altitude exceeds 80 meters RTL
>> when groundspeed drops below 1 RTL
```

### Slash Commands

These run instantly without involving the AI:

```
/status          live drone status (no AI call)
/battery         battery level and voltage
/position        current GPS coordinates
/locations       all preset named locations with coordinates
/model           show the current AI model
/model <id>      switch model live
/state           full mission_state dump + active condition watches
/report          generate a plain-English flight summary
/mission <desc>  run the 4-step Plan → Safety → Execute → Report workflow
exit             shut down
```

### Switching Models

```
/model google/gemini-2.0-flash-001
/model qwen/qwen3-235b-a22b
/model qwen/qwen3-30b-a3b
/model openai/gpt-4o-mini
/model x-ai/grok-3-mini-beta
```

Models that support thinking mode (Qwen3, Qwen3.5) have it disabled automatically via the `enable_thinking: false` API parameter.

---

## Notes

- Battery in SITL reads 0% but the drone flies normally — this is expected and the agent is instructed to ignore it
- Default SITL connection: `tcp:127.0.0.1:5763`
- Unreal Engine relay: `127.0.0.1:1234`
- Session data and learned preferences are stored in `drone_sessions.db` (SQLite, created automatically)
- The script requires `tcp_relay.py` to be in the same directory
