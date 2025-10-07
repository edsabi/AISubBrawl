# AISubBrawl - Submarine Battle Game

A real-time multiplayer submarine warfare simulation with sonar, torpedoes, and tactical maneuvering.

## Features

- **Real-time multiplayer**: Multiple players control submarines simultaneously
- **Realistic submarine physics**: Speed, depth, battery management, emergency blow systems
- **Sonar systems**: Passive listening and active pinging with realistic detection mechanics
- **Wire-guided torpedoes**: Launch and control torpedoes with depth and heading commands
- **Interactive map**: Pan, zoom, and track submarine movements
- **Tactical gameplay**: Stealth, positioning, and timing are key to survival

## Quick Start

1. **Start the server**:
   ```bash
   cd sub
   python3 server_world_db.py
   ```

2. **Open the game**: Navigate to `http://localhost:5000` in your browser

3. **Create account**: Register with a username and password

4. **Spawn submarine**: Click "Spawn Sub" to enter the battlefield

## Controls

### Submarine Maneuvering
- **Throttle**: 0-100% engine power
- **Rudder**: -100 to +100 for turning
- **Planes**: -100 to +100 for diving/surfacing
- **Target Depth**: Set desired depth for autopilot
- **Emergency Blow**: Surface quickly (limited charges)
- **Snorkel**: Recharge battery at shallow depth

### Sonar Operations
- **Passive Array**: Listen for enemy submarines (set bearing)
- **Active Ping**: Send sonar pulse to detect contacts (reveals your position)

### Torpedo Warfare
- **Launch**: Fire torpedo from selected tube with wire length
- **Wire Guidance**: Control torpedo heading and depth
- **Quick Turns**: L30, L15, L5, R5, R15, R30 buttons for rapid course changes
- **Detonate**: Manual detonation when close to target

### Map Navigation
- **Mouse**: Click and drag to pan the map
- **Arrow Buttons**: Use directional pad for precise movement
- **Zoom**: + and - buttons to zoom in/out
- **Center**: ◎ button to center on your submarine
- **Reset**: ⟲ button to reset view

## Game Mechanics

### Submarine Systems
- **Battery**: Drains with throttle use, recharges while snorkeling
- **Crush Depth**: Take damage below 500m depth
- **Speed vs Stealth**: Higher speeds make more noise
- **Buoyancy**: Natural tendency to sink when stopped

### Sonar Detection
- **Passive**: Detect other submarines without revealing position
- **Active**: Get precise range/bearing but alerts enemies
- **Noise**: Engine throttle affects detectability

### Torpedo Mechanics
- **Wire Control**: Maintain connection within wire length
- **Autonomous**: Torpedo goes "dumb" if wire breaks
- **Blast Radius**: 60m damage radius
- **Fuel Limited**: Torpedoes have maximum range

## Configuration

Edit `game_config.json` to customize:
- World size and spawn areas
- Submarine performance parameters
- Torpedo specifications
- Sonar detection ranges
- Game tick rate

## Technical Details

- **Backend**: Python Flask with SQLAlchemy
- **Database**: SQLite with real-time updates
- **Frontend**: HTML5 Canvas with Server-Sent Events
- **Physics**: 10Hz game loop with realistic submarine dynamics

## Tips for New Players

1. **Start slow**: Learn basic maneuvering before engaging
2. **Listen first**: Use passive sonar to locate enemies
3. **Manage battery**: Don't run at full throttle constantly
4. **Use depth**: Vertical maneuvering is crucial for tactics
5. **Wire management**: Keep torpedo wire length appropriate for engagement range
6. **Stealth approach**: Lower speeds reduce detection range

## Multiplayer Strategy

- **Positioning**: Use terrain and depth layers tactically
- **Timing**: Coordinate attacks and defensive maneuvers
- **Communication**: Share sonar contacts with teammates
- **Resource management**: Balance speed, stealth, and battery life
- **Escape routes**: Always plan your exit strategy

Dive deep, stay quiet, and hunt smart! 🚢⚓
