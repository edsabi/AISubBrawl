# Submarine Battle Game API Documentation

## Authentication

All API endpoints (except public ones) require an `api_key` header for authentication.

```
Headers:
  api_key: <your_api_key>
  Content-Type: application/json
```

## Public Endpoints

### GET `/`
Serves the main game UI (HTML page).

```bash
curl http://localhost:5000/
```

### GET `/public`
Returns public game information.

```bash
curl http://localhost:5000/public
```

**Response:**
```json
{
  "players": 5,
  "submarines": 3,
  "torpedoes": 2
}
```

## Authentication Endpoints

### POST `/signup`
Create a new user account.

```bash
curl -X POST http://localhost:5000/signup \
  -H "Content-Type: application/json" \
  -d '{"username": "player1", "password": "secret123"}'
```

**Response:**
```json
{
  "ok": true,
  "api_key": "abc123...",
  "user_id": 1
}
```

### POST `/login`
Login to existing account.

```bash
curl -X POST http://localhost:5000/login \
  -H "Content-Type: application/json" \
  -d '{"username": "player1", "password": "secret123"}'
```

**Response:**
```json
{
  "ok": true,
  "api_key": "abc123...",
  "user_id": 1
}
```

## Game State Endpoints

### GET `/rules`
Returns game configuration and rules.

```bash
curl http://localhost:5000/rules \
  -H "api_key: your_api_key_here"
```

**Response:**
```json
{
  "tick_hz": 10,
  "world": {
    "ring": {"x": 0.0, "y": 0.0, "r": 6000.0}
  },
  "sub": {
    "max_speed": 6.0,
    "crush_depth": 500.0
  }
}
```

### GET `/state`
Returns current user's submarines and torpedoes.

```bash
curl http://localhost:5000/state \
  -H "api_key: your_api_key_here"
```

**Response:**
```json
{
  "subs": [
    {
      "id": "sub123",
      "x": 1000.0,
      "y": 500.0,
      "depth": 100.0,
      "heading": 1.57,
      "speed": 3.0,
      "battery": 75.0,
      "health": 100.0
    }
  ],
  "torpedoes": [
    {
      "id": "torp456",
      "x": 1100.0,
      "y": 520.0,
      "depth": 80.0,
      "heading": 1.6,
      "mode": "wire"
    }
  ]
}
```

## Submarine Management

### POST `/register_sub`
Spawn a new submarine.

```bash
curl -X POST http://localhost:5000/register_sub \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"name": "USS Hunter"}'
```

**Response:**
```json
{
  "ok": true,
  "sub_id": "sub123",
  "spawn": {"x": 1000.0, "y": 500.0, "depth": 50.0}
}
```

## Submarine Control

### POST `/control/<sub_id>`
Control submarine movement and systems.

```bash
curl -X POST http://localhost:5000/control/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"throttle": 0.8, "rudder": 0.3, "planes": -0.2, "target_depth": 150.0}'
```

**Response:**
```json
{
  "ok": true
}
```

**Parameters:**
- `throttle`: 0.0-1.0 (engine power)
- `rudder`: -1.0 to 1.0 (turning)
- `planes`: -1.0 to 1.0 (diving/surfacing)
- `target_depth`: meters (autopilot depth)

### POST `/snorkel/<sub_id>`
Toggle snorkel mode for battery charging.

```bash
curl -X POST http://localhost:5000/snorkel/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"active": true}'
```

### POST `/emergency_blow/<sub_id>`
Activate emergency surface system.

```bash
curl -X POST http://localhost:5000/emergency_blow/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"activate": true}'
```

## Sonar Operations

### POST `/set_passive_array/<sub_id>`
Set passive sonar listening direction.

```bash
curl -X POST http://localhost:5000/set_passive_array/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"dir_deg": 45.0}'
```

### POST `/ping/<sub_id>`
Send active sonar ping.

```bash
curl -X POST http://localhost:5000/ping/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"bearing_deg": 90.0, "range_m": 2000.0}'
```

**Response:**
```json
{
  "ok": true,
  "contacts": [
    {
      "bearing": 92.3,
      "range": 1850.0,
      "strength": 0.8
    }
  ]
}
```

## Torpedo Operations

### POST `/launch_torpedo/<sub_id>`
Launch a torpedo from submarine.

```bash
curl -X POST http://localhost:5000/launch_torpedo/sub123 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"tube": 0, "wire_length": 600.0}'
```

**Response:**
```json
{
  "ok": true,
  "torpedo_id": "torp456",
  "wire_length": 600.0,
  "spawn": {"x": 1010.0, "y": 505.0, "depth": 100.0}
}
```

### POST `/set_torp_depth/<torp_id>`
Set torpedo target depth.

```bash
curl -X POST http://localhost:5000/set_torp_depth/torp456 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"depth": 120.0}'
```

### POST `/set_torp_heading/<torp_id>`
Control torpedo heading.

```bash
# Relative turn
curl -X POST http://localhost:5000/set_torp_heading/torp456 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"turn_deg": 15.0, "dt": 1.0}'

# Absolute heading
curl -X POST http://localhost:5000/set_torp_heading/torp456 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"heading_deg": 90.0, "dt": 1.0}'
```

**Parameters:**
- `turn_deg`: Relative turn amount (positive = left)
- `heading_deg`: Absolute heading target
- `dt`: Time delta for turn rate

### POST `/detonate/<torp_id>`
Manually detonate torpedo.

```bash
curl -X POST http://localhost:5000/detonate/torp456 \
  -H "api_key: your_api_key_here" \
  -H "Content-Type: application/json"
```

**Response:**
```json
{
  "ok": true,
  "affected": 1,
  "torpedo_id": "torp456",
  "blast_radius": 60.0
}
```

## Real-time Updates

### GET `/stream`
Server-Sent Events stream for real-time game updates.

```bash
curl http://localhost:5000/stream?api_key=your_api_key_here \
  -H "Accept: text/event-stream"
```

**Event Types:**

#### snapshot
Current game state update (sent every second):
```json
{
  "subs": [...],
  "torpedoes": [...],
  "time": 1696789123.456
}
```

#### echo
Active sonar return:
```json
{
  "observer_sub_id": "sub123",
  "bearing": 45.2,
  "range": 1200.0,
  "quality": 0.9,
  "time": 1696789123.456
}
```

#### explosion
Torpedo detonation:
```json
{
  "time": 1696789123.456,
  "at": [1000.0, 500.0, 100.0],
  "torpedo_id": "torp456",
  "blast_radius": 60.0
}
```

#### contact
Passive sonar detection:
```json
{
  "observer_sub_id": "sub123",
  "bearing": 30.5,
  "bearing_relative": 15.2,
  "range": 800.0,
  "quality": 0.7,
  "time": 1696789123.456
}
```

## Admin Endpoints

### GET `/admin/state`
Admin-only endpoint for full game state (requires admin privileges).

```bash
curl http://localhost:5000/admin/state \
  -H "api_key: admin_api_key_here"
```

## Error Responses

All endpoints return error responses in this format:

```json
{
  "ok": false,
  "error": "Description of the error"
}
```

Common HTTP status codes:
- `400`: Bad Request (invalid parameters)
- `401`: Unauthorized (missing/invalid api_key)
- `403`: Forbidden (not your submarine/torpedo)
- `404`: Not Found (submarine/torpedo doesn't exist)
- `500`: Internal Server Error

## Rate Limiting

- Control commands: No specific limit
- Sonar pings: Limited by game mechanics (cooldown periods)
- Torpedo launches: Limited by submarine tube count and reload times

## Coordinate System

- **X/Y**: World coordinates in meters
- **Heading**: Radians, 0 = East, π/2 = North
- **Depth**: Meters below surface (positive = deeper)
- **Angles**: Degrees in API, radians internally
