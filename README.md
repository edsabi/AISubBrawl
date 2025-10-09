
## Sub Brawl — API‑driven real‑time submarine combat

**Sub Brawl** is a real‑time, server‑simulated submarine skirmish where *clients control everything over HTTP/Web APIs*. Spin up a server, authenticate, register a sub, and start maneuvering, pinging, and launching wire‑guided torpedoes.

This repo exposes:

* A Flask + SQLAlchemy game server with a deterministic physics loop
* An HTTP/JSON control surface for subs & torpedoes
* Server‑Sent Events (SSE) for live telemetry (contacts, pings, explosions, snapshots)
* Pluggable world + balance via `game_config.json`

> You can build your own UI/AI/CLI bot on top of the API. The included `ui.html` is a simple viewer.

---

## Table of contents

* [Features](#features)
* [Quickstart](#quickstart)
* [Configuration](#configuration)
* [Security & Auth](#security--auth)
* [Gameplay Concepts](#gameplay-concepts)
* [Performance Notes](#performance-notes)
* [Admin Utilities](#admin-utilities)
* [API Reference](#api-reference)
* [License](#license)

---

## Features

* **Real‑time loop** at `tick_hz` (default 10 Hz) with physics for heading, pitch, depth, buoyancy, battery, crush damage.
* **Torpedoes**: wire‑guided → free‑running, speed & depth control, proximity fuze, manual detonation, battery‑costed launch.
* **Sonar**: passive bearings for subs/torps; active pings with beamwidth/range tradeoffs and power draw; quality‑dependent returns.
* **SSE telemetry** per user: periodic snapshots and event fan‑out for contacts, echoes, pings, explosions.
* **Multi‑user** auth with API keys; optional **admin** vantage and performance endpoint.
* **SQLite WAL** tuned for low‑latency; thread‑safe world lock for consistency.

---

## Quickstart

### Prereqs

* Python 3.10+
* (Optional) virtualenv/uv/conda

### Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or, if you’re copying just server_world_db.py:
pip install flask flask_sqlalchemy sqlalchemy werkzeug
```

### Run the server

```bash
export FLASK_ENV=production
# (Optional) seed an admin user at boot
export SB_ADMIN_USER=admin
export SB_ADMIN_PASS=change-me
python server_world_db.py
```

The server listens on `0.0.0.0:5000`.

### First steps from a shell

```bash
# Sign up → receive an API key
curl -sX POST localhost:5000/signup \
  -H 'content-type: application/json' \
  -d '{"username":"captain","password":"secret"}'

# Save the key for convenience
API=... # paste value from the response

# Register a submarine (random safe spawn)
curl -sX POST "localhost:5000/register_sub?api_key=$API" | jq

# Stream live events (SSE)
curl -N -H "Authorization: Bearer $API" localhost:5000/stream
```

To control your sub, torpedoes, and sensors, see **[api.md](#api-reference)** (also included below for convenience).

---

## Configuration

At start, the server merges `game_config.json` over built‑in defaults (deep‑merge). Example knobs:

* `tick_hz` — simulation rate (Hz)
* `world.ring` — center & radius of the playable disc; `spawn_min_r/max_r`, `safe_spawn_separation`
* `sub` — kinematics, hydrodynamics, battery, snorkel limits, emergency blow, crush depth
* `torpedo` — speed limits, turn/descend rates, blast, lifetime, max_range, battery costs, fuze
* `sonar.passive` & `sonar.active` — ranges, noise models, jitter; `active_power` cost model

See the `/rules` endpoint for the effective runtime config.

---

## Security & Auth

* **API key** required for all gameplay endpoints. Supply as `Authorization: Bearer <key>` *or* `?api_key=` query param.
* Create a key via `/signup` or `/login`.
* Set `SB_ADMIN_USER`/`SB_ADMIN_PASS` to bootstrap an **admin** at process start (key is logged once).

> Do not expose admin logs publicly; the admin API shows all entities.

---

## Gameplay Concepts

* **Submarine control**: throttle (0..1), planes (‑1..1), rudder (±max deg), optional depth‑hold (`target_depth`). Snorkel auto‑recharges at ≤ `snorkel_depth` with hysteresis.
* **Emergency blow**: time‑limited upward velocity; consumes a rechargeable charge at snorkel.
* **Torpedoes**: launch cost scales with requested range; wire breaks with distance; heading/depth/speed commands while wired; auto proximity; manual `/detonate` always available.
* **Sensors**: passive contacts are noisy & intermittent; active pings cost battery, reveal you, and return noisy ranges/bearings/depths via delayed echoes.

State & events stream to your client via `/stream` as SSE: `snapshot`, `contact`, `echo`, `torpedo_contact`, `torpedo_ping`, `explosion`, `ping` (keepalive).

---

## Performance Notes

* SQLite runs in WAL mode with `busy_timeout=60000` and `synchronous=NORMAL`.
* The main loop batches physics outside the DB lock then commits once per tick.
* See `/perf` for timing breakdowns (`tick_ms`, `db_fetch_ms`, `physics_ms`, `db_commit_ms`) and queue counts.

---

## Admin Utilities

* `/admin/state` (admin only): dump of all subs/torpedoes including owners
* `/perf`: timing counters for profiling

---

## API Reference

For full details and examples, open **api.md** below.

* **Auth**: `/signup`, `/login`, `/stream` (SSE)
* **World**: `/public`, `/rules`, `/state`, `/register_sub`
* **Sub control**: `/control/<sub_id>`, `/snorkel/<sub_id>`, `/emergency_blow/<sub_id>`, `/ping/<sub_id>`, `/set_passive_array/<sub_id>`
* **Torpedoes**: `/launch_torpedo/<sub_id>`, `/set_torp_speed/<torp_id>`, `/set_torp_depth/<torp_id>`, `/set_torp_heading/<torp_id>`, `/torp_ping/<torp_id>`, `/torp_ping_toggle/<torp_id>`, `/detonate/<torp_id>`
* **Admin/Perf**: `/admin/state`, `/perf`

---

## License

MIT (or your preferred license). Replace this section accordingly.

---

# api.md

## Sub Brawl HTTP API

Base URL: `http://<host>:5000`

### Conventions

* All bodies are JSON. Responses include `{ "ok": true|false, ... }`.
* **Auth**: send `Authorization: Bearer <API_KEY>` or `?api_key=<API_KEY>`.
* Angles:

  * **Headings / bearings** in requests are in **degrees** where noted; internal state uses radians.
  * Server responses for headings/bearings may be in **radians** if not explicitly stated (see examples below).
* Distances in meters; speeds in m/s; depth in meters (positive down).

---

## Auth & Session

### `POST /signup`

Create a user and mint an API key.

```json
{ "username": "captain", "password": "secret" }
```

**200** → `{ "ok": true, "api_key": "…" }`

### `POST /login`

Return a fresh API key for existing user.

```json
{ "username": "captain", "password": "secret" }
```

**200** → `{ "ok": true, "api_key": "…" }`

### `GET /stream` (SSE)

Live per‑user event stream. Requires auth header or query.

* Events: `hello`, `snapshot`, `contact`, `echo`, `torpedo_contact`, `torpedo_ping`, `explosion`, `ping` (keepalive).
* Recommended: reconnect with exponential backoff; honor `retry:` field.

---

## World & Config

### `GET /public`

Public world info.
**200** → `{ "ring": {"x":0,"y":0,"r":6000}, "objectives": [{"id":"A",...}] }`

### `GET /rules`

Effective merged game config (defaults ⊕ `game_config.json`).

### `GET /state`

Your current subs and torpedoes.
**Auth required**
**200** → `{ "ok": true, "time": <epoch>, "subs": [...], "torpedoes": [...] }`

### `POST /register_sub`

Spawn a new submarine at a safe location.
**Auth required**
**200** → `{ "ok": true, "sub_id": "…", "spawn": [x,y,depth] }`

---

## Submarine Control

### `POST /control/<sub_id>`

Set control surfaces / targets.
Body fields (all optional):

* `target_depth: number|null` — enable (number) or clear (null) depth‑hold autopilot
* `throttle: 0..1` — propulsion demand
* `planes: -1..1` — manual dive/planes (disabled when depth‑hold active)
* `rudder_deg: -MAX..+MAX` — absolute rudder setpoint in **degrees** (max from `/rules` `sub.max_rudder_deg`)
* `rudder_nudge_deg: number` — relative nudge in **degrees**
  **200** → `{ "ok": true }`

### `POST /snorkel/<sub_id>`

Toggle or set snorkel state (must be ≤ `snorkel_depth`).
Body (optional):

* Omit body or `{ "toggle": true }` to toggle.
* `{ "on": true|false }` to force a state.
  **400** if too deep.

### `POST /emergency_blow/<sub_id>`

Trigger emergency blow if charge > 0. Adds upward m/s for duration; recharges at snorkel.

### `POST /ping/<sub_id>`

Active sonar ping from your sub.
Body:

```json
{
  "beamwidth_deg": 20,      // limited by /rules sonar.active.max_angle
  "max_range": 3000,        // <= sonar.active.max_range
  "center_bearing_deg": 0   // relative to own heading
}
```

Returns battery cost breakdown and schedules echo events on `/stream` once the sound travel time elapses. Also notifies others of your ping.

### `POST /set_passive_array/<sub_id>`

Electronically steer your passive array.

```json
{ "dir_deg": 123.4 }
```

---

## Torpedoes

### `POST /launch_torpedo/<sub_id>`

Fire a wire‑guided torpedo from the bow. Consumes battery based on desired range.
Body:

```json
{ "range": 1200 } // meters, clamped to /rules torpedo.max_range
```

**200** → `{ ok, torpedo_id, range, battery_cost, spawn: {x,y,depth} }`

### `POST /set_torp_speed/<torp_id>`

Command target speed (clamped between `min_speed` and `max_speed`).

```json
{ "speed": 16 }
```

### `POST /set_torp_depth/<torp_id>`

Set target depth (rate‑limited by `depth_rate_m_s`).

```json
{ "depth": 120 }
```

### `POST /set_torp_heading/<torp_id>`

Wire‑guided heading change (requires `control_mode == "wire"`).
Body (one of):

```json
{ "heading_deg": 45 } // absolute
{ "turn_deg": 15, "dt": 0.1 } // relative; optional dt to scale turn limit
```

### `POST /torp_ping/<torp_id>`

Active ping from torpedo (narrow, fixed 30° beam; clamped by torpedo sonar config).

```json
{ "max_range": 800 }
```

**200** → `{ ok, contacts: [ { bearing, range, depth }... ] }`

### `POST /torp_ping_toggle/<torp_id>`

Toggle auto‑pinging for a torpedo. When enabled, torp will periodically emit pings and stream `torpedo_ping` events.

### `POST /detonate/<torp_id>`

Manual detonation at current position. Graduated damage by distance. Removes the torpedo on success.

---

## Events (SSE payloads)

### `snapshot`

Periodic per‑user snapshot of all your entities.

```json
{
  "subs": [{ "id": "…", "x": 0, "y": 0, "depth": 120, "heading": 1.57, "speed": 6.0, "battery": 55.2, ... }],
  "torpedoes": [{ "id": "…", "x": 13, "y": 5, "depth": 120, "heading": 1.57, "speed": 12.0, ... }],
  "time": 173…
}
```

### `contact`

Passive contact from your sub *or* notification that someone else pinged.

```json
{ "type": "passive"|"active_ping_detected", "observer_sub_id": "…", "bearing": 0.78, "bearing_relative": -0.12, "range_class": "short|medium|long", "snr": 9.2, "time": 173… }
```

### `echo`

Your active ping echo with noisy range/bearing/depth and quality (0..1).

```json
{ "type":"active", "observer_sub_id":"…", "bearing":1.1, "range":950, "estimated_depth": 130, "quality": 0.82, "time": 173… }
```

### `torpedo_contact`

Torpedo passive contact on a submarine.

### `torpedo_ping`

Auto‑ping results from a torpedo when enabled.

### `explosion`

Damage event, typically after detonation or proximity fuze.

```json
{ "time":173…, "at":[x,y,depth], "torpedo_id":"…", "blast_radius": 60, "damage": 50, "distance": 92 }
```

---

## Error codes

* `401 Unauthorized` — missing/invalid API key
* `403 Forbidden` — valid key but not owner / not admin
* `404 Not found` — entity missing or not yours
* `400 Bad request` — parameter or gameplay rule violation (e.g., too deep to snorkel, not enough battery, wire lost)

---

## Admin & Perf

### `GET /admin/state` *(admin only)*

Full dump of all subs/torpedoes including owners.

### `GET /perf`

Perf counters + queue count.

---

## Examples — cURL cookbook (per endpoint)

> Assumes `HOST=http://localhost:5000` and `API=<your_api_key>` in your shell.

```bash
HOST=http://localhost:5000
API=REPLACE_ME
```

### Auth & Session

**Signup**

```bash
curl -sX POST "$HOST/signup" -H 'content-type: application/json' \
  -d '{"username":"captain","password":"secret"}' | jq
```

**Login**

```bash
curl -sX POST "$HOST/login" -H 'content-type: application/json' \
  -d '{"username":"captain","password":"secret"}' | jq
```

**SSE Stream**

```bash
# Using header
curl -N -H "Authorization: Bearer $API" "$HOST/stream"
# Or query param
curl -N "$HOST/stream?api_key=$API"
```

---

### World & Config

**Public info**

```bash
curl -s "$HOST/public" | jq
```

**Rules / Effective config**

```bash
curl -s "$HOST/rules" | jq
```

**Your state (subs & torps)**

```bash
curl -s -H "Authorization: Bearer $API" "$HOST/state" | jq
```

**Register a submarine**

```bash
curl -sX POST "$HOST/register_sub?api_key=$API" | jq
```

---

### Submarine Control

**/control — throttle, planes, rudder, depth‑hold**

```bash
SUB=REPLACE_WITH_SUB_ID
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"throttle":0.6, "planes":0.2, "rudder_deg":10, "target_depth":120}' \
  "$HOST/control/$SUB" | jq
```

**/snorkel — toggle**

```bash
curl -sX POST -H "Authorization: Bearer $API" "$HOST/snorkel/$SUB" | jq
```

**/snorkel — force on/off**

```bash
# Force ON (must be shallower than snorkel depth)
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"on":true}' "$HOST/snorkel/$SUB" | jq
# Force OFF
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"on":false}' "$HOST/snorkel/$SUB" | jq
```

**/emergency_blow**

```bash
curl -sX POST -H "Authorization: Bearer $API" "$HOST/emergency_blow/$SUB" | jq
```

**/ping — active sonar**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"beamwidth_deg":20, "max_range":2000, "center_bearing_deg":0}' \
  "$HOST/ping/$SUB" | jq
```

**/set_passive_array — steer the array**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"dir_deg":135}' "$HOST/set_passive_array/$SUB" | jq
```

---

### Torpedoes

**/launch_torpedo — fire wire‑guided torp**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"range":1200}' "$HOST/launch_torpedo/$SUB" | jq
# Save torpedo id
TID=$(curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"range":1200}' "$HOST/launch_torpedo/$SUB" | jq -r .torpedo_id)
```

**/set_torp_speed**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"speed":16}' "$HOST/set_torp_speed/$TID" | jq
```

**/set_torp_depth**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"depth":140}' "$HOST/set_torp_depth/$TID" | jq
```

**/set_torp_heading — absolute**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"heading_deg": 60}' "$HOST/set_torp_heading/$TID" | jq
```

**/set_torp_heading — relative turn with dt**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"turn_deg": 15, "dt": 0.1}' "$HOST/set_torp_heading/$TID" | jq
```

**/torp_ping — active torp ping**

```bash
curl -sX POST -H "Authorization: Bearer $API" -H 'content-type: application/json' \
  -d '{"max_range":800}' "$HOST/torp_ping/$TID" | jq
```

**/torp_ping_toggle — enable/disable auto‑ping**

```bash
curl -sX POST -H "Authorization: Bearer $API" "$HOST/torp_ping_toggle/$TID" | jq
```

**/detonate — manual detonation**

```bash
curl -sX POST -H "Authorization: Bearer $API" "$HOST/detonate/$TID" | jq
```

---

### Admin & Perf

**/admin/state — admin only**

```bash
# Requires the API key for a user marked is_admin=true
curl -s -H "Authorization: Bearer $API" "$HOST/admin/state" | jq
```

**/perf — server timings**

```bash
curl -s "$HOST/perf" | jq
```

---

## Notes & Gotchas

* Rudder and planes require **battery**; when empty, surfaces freeze and propulsion stops.
* Snorkel can only be engaged at/above the configured depth; auto‑off has hysteresis.
* Torpedo **wire control** is lost if distance to parent exceeds the torp’s `wire_length` (set from requested launch range).
* Manual `/detonate` always enforces **minimum safe distance** internally for proximity fuze; manual detonation is at your own risk.
* Headings returned in snapshots are radians; convert as needed in client UIs.

---

Happy hunting.
