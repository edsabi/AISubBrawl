#!/usr/bin/env python3
import os, json, math, random, time, threading, queue, uuid, copy
from typing import Dict
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

# -------------------------- Defaults --------------------------
DEFAULT_CFG = {
    "tick_hz": 10,
    "world": {
        "ring": {"x": 0.0, "y": 0.0, "r": 6000.0},
        "spawn_min_r": 500.0,
        "spawn_max_r": 4500.0,
        "safe_spawn_separation": 800.0
    },
    "sub": {
        "max_speed": 6.0,
        "yaw_rate_deg_s": 20.0,
        "pitch_rate_deg_s": 12.0,
        "planes_effect": 1.0,
        "neutral_bias": 0.008,
        "depth_damping": 0.35,
        "snorkel_depth": 15.0,
        "emergency_blow": {
            "duration_s": 10.0,
            "upward_mps": 5.0,
            "recharge_per_s_at_snorkel": 0.06,
            "cooldown_s": 0.0
        },
        "battery": {
            "initial_min": 40.0,
            "initial_max": 80.0,
            "drain_per_throttle_per_s": 0.02,
            "recharge_per_s_snorkel": 0.25
        },
        "crush_depth": 500.0,
        "crush_dps_per_100m": 30.0
    },
    "torpedo": {
        "speed": 12.0,
        "turn_rate_deg_s": 30.0,
        "depth_rate_m_s": 6.0,
        "blast_radius": 60.0,
        "lifetime_s": 240.0,
        "default_wire": 600.0
    },
    "sonar": {
        "passive": {
            "base_snr": 8.0,
            "speed_noise_gain": 0.6,
            "snorkel_bonus": 8.0,
            "bearing_jitter_deg": 3.0,
            "report_interval_s": [2.0, 4.0]
        },
        "active": {
            "max_range": 6000.0,
            "sound_speed": 1500.0,
            "rng_sigma_m": 40.0,
            "brg_sigma_deg": 1.5
        }
    }
}

def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst

CFG_PATH = 'game_config.json'
if os.path.exists(CFG_PATH):
    with open(CFG_PATH, 'r') as f:
        user_cfg = json.load(f)
    GAME_CFG = copy.deepcopy(DEFAULT_CFG)
    deep_merge(GAME_CFG, user_cfg)
    print("[CONFIG] Loaded & merged:", CFG_PATH)
else:
    GAME_CFG = copy.deepcopy(DEFAULT_CFG)
    print("[CONFIG] Using defaults")

TICK_HZ = GAME_CFG.get("tick_hz", DEFAULT_CFG["tick_hz"])
TICK = 1.0 / TICK_HZ

# -------------------------- App / DB --------------------------
app = Flask(__name__)
# important: allow cross-thread use + timeout
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///sub_brawl.sqlite3?check_same_thread=false'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "connect_args": {"check_same_thread": False, "timeout": 60},
}

db = SQLAlchemy(app)

WORLD_LOCK = threading.RLock()

# -------------------------- Models --------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    pw_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class SubModel(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=100.0)
    heading = db.Column(db.Float, default=0.0)       # radians, 0=east CCW+
    pitch = db.Column(db.Float, default=0.0)         # + = bow up
    rudder_angle = db.Column(db.Float, default=0.0)  # radians
    rudder_cmd = db.Column(db.Float, default=0.0)    # radians target
    planes = db.Column(db.Float, default=0.0)        # -1..1 manual
    throttle = db.Column(db.Float, default=0.2)      # 0..1
    target_depth = db.Column(db.Float, default=None)
    speed = db.Column(db.Float, default=0.0)
    battery = db.Column(db.Float, default=50.0)
    is_snorkeling = db.Column(db.Boolean, default=False)
    blow_active = db.Column(db.Boolean, default=False)
    blow_charge = db.Column(db.Float, default=1.0)
    blow_end = db.Column(db.Float, default=0.0)
    health = db.Column(db.Float, default=100.0)
    passive_dir = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.Float, default=lambda: time.time())
    last_report = db.Column(db.Float, default=0.0)

class TorpedoModel(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    parent_sub = db.Column(db.String(36), db.ForeignKey('sub_model.id'), nullable=False)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=100.0)
    target_depth = db.Column(db.Float, default=None)
    heading = db.Column(db.Float, default=0.0)
    speed = db.Column(db.Float, default=lambda: GAME_CFG.get("torpedo", {}).get("speed", DEFAULT_CFG["torpedo"]["speed"]))
    created_at = db.Column(db.Float, default=lambda: time.time())
    control_mode = db.Column(db.String(16), default='wire')  # wire | free
    wire_length = db.Column(db.Float, default=lambda: GAME_CFG.get("torpedo", {}).get("default_wire", DEFAULT_CFG["torpedo"]["default_wire"]))
    updated_at = db.Column(db.Float, default=lambda: time.time())

with app.app_context():
    db.create_all()
    # SQLite concurrency tuning: WAL + sane sync + long busy timeout
    try:
        with db.engine.connect() as con:
            con.execute(text("PRAGMA journal_mode=WAL;"))
            con.execute(text("PRAGMA synchronous=NORMAL;"))
            con.execute(text("PRAGMA busy_timeout=60000;"))
        print("[DB] SQLite WAL enabled, busy_timeout=60000ms, synchronous=NORMAL")
    except Exception as e:
        print("[DB] PRAGMA set failed:", e)

# -------------------------- Helpers --------------------------
def clamp(v, lo, hi): return max(lo, min(hi, v))
def wrap_angle(a):
    while a >= math.pi: a -= 2*math.pi
    while a < -math.pi: a += 2*math.pi
    return a
def distance(x1,y1,x2,y2): return math.hypot(x2-x1, y2-y1)

# -------------------------- Auth --------------------------
def make_key():
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]

def get_user_from_api():
    k = None
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        k = auth.split(' ', 1)[1].strip()
    if not k:
        k = request.args.get('api_key', '').strip()
    if not k:
        return None
    ak = ApiKey.query.filter_by(key=k).first()
    if not ak: return None
    return User.query.get(ak.user_id)

def require_key(fn):
    def w(*a, **kw):
        u = get_user_from_api()
        if not u:
            return jsonify({"ok": False, "error": "Invalid api key"}), 401
        request.user = u
        return fn(*a, **kw)
    w.__name__ = fn.__name__
    return w




# -------------------------- SSE per-user --------------------------
USER_QUEUES: Dict[int, "queue.Queue[str]"] = {}
USER_LAST: Dict[int, float] = {}

def _uq(user_id: int) -> "queue.Queue[str]":
    if user_id not in USER_QUEUES:
        USER_QUEUES[user_id] = queue.Queue(maxsize=1000)
        USER_LAST[user_id] = time.time()
    return USER_QUEUES[user_id]

def send_private(user_id: int, event: str, obj: dict):
    try:
        q = _uq(user_id)
        payload = f"event: {event}\ndata: {json.dumps(obj)}\n\n"
        q.put_nowait(payload)
    except queue.Full:
        pass

def send_snapshot(user_id: int):
    # lock DB read to avoid racing with writer
    with WORLD_LOCK:
        subs = SubModel.query.filter_by(owner_id=user_id).all()
        torps = TorpedoModel.query.filter_by(owner_id=user_id).all()
    def sub_pub(s: SubModel):
        return dict(
            id=s.id, x=s.x, y=s.y, depth=s.depth, heading=s.heading, pitch=s.pitch,
            rudder_angle=s.rudder_angle, rudder_cmd=s.rudder_cmd, planes=s.planes,
            speed=s.speed, battery=s.battery, is_snorkeling=s.is_snorkeling,
            blow_active=s.blow_active, blow_charge=s.blow_charge, health=s.health,
            target_depth=s.target_depth, throttle=s.throttle
        )
    def torp_pub(t: TorpedoModel):
        return dict(id=t.id, x=t.x, y=t.y, depth=t.depth, heading=t.heading,
                    speed=t.speed, mode=t.control_mode, wire_length=t.wire_length)
    q = _uq(user_id)
    obj = {"subs":[sub_pub(s) for s in subs], "torpedoes":[torp_pub(t) for t in torps], "time": time.time()}
    payload = f"event: snapshot\ndata: {json.dumps(obj)}\n\n"
    try: q.put_nowait(payload)
    except queue.Full: pass



# -------------------------- World / spawn --------------------------
WORLD_RING = GAME_CFG.get("world", {}).get("ring", DEFAULT_CFG["world"]["ring"])
OBJECTIVES = [
    {"id":"A","x":1500.0,"y":-800.0,"r":250.0},
    {"id":"B","x":-1200.0,"y":1300.0,"r":250.0},
]

def random_spawn_pos():
    cfgw = GAME_CFG.get("world", DEFAULT_CFG["world"])
    cx, cy, R = WORLD_RING["x"], WORLD_RING["y"], WORLD_RING["r"]
    rmin = cfgw.get("spawn_min_r", DEFAULT_CFG["world"]["spawn_min_r"])
    rmax = cfgw.get("spawn_max_r", DEFAULT_CFG["world"]["spawn_max_r"])
    sep  = cfgw.get("safe_spawn_separation", DEFAULT_CFG["world"]["safe_spawn_separation"])
    for _ in range(50):
        ang = random.uniform(-math.pi, math.pi)
        r = random.uniform(rmin, rmax)
        x = cx + math.cos(ang)*r
        y = cy + math.sin(ang)*r
        ok = True
        with WORLD_LOCK:
            for s in SubModel.query.all():
                if distance(x, y, s.x, s.y) < sep:
                    ok = False; break
        if ok: return x, y
    return cx, cy

# -------------------------- Game logic --------------------------
PENDING_PINGS = []  # active sonar echoes waiting to return

def update_sub(s: SubModel, dt: float, now: float):
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    max_spd      = scfg["max_speed"]
    yaw_rate_rad = math.radians(scfg["yaw_rate_deg_s"])          # rad/s @ full rudder
    pitch_rate   = math.radians(scfg["pitch_rate_deg_s"])
    planes_eff   = scfg["planes_effect"]
    neutral      = scfg["neutral_bias"]
    crush_depth  = scfg["crush_depth"]
    crush_dps    = scfg["crush_dps_per_100m"]

    # === RUDDER: normalized command -> servo angle (radians) ===
    # Configurable limits
    max_rudder_deg  = scfg.get("max_rudder_deg", 30.0)           # hardware limit
    rudder_rate_deg = scfg.get("rudder_rate_deg_s", 60.0)        # servo slew rate
    MAX_RUDDER_RAD  = math.radians(max_rudder_deg)
    MAX_RUDDER_STEP = math.radians(rudder_rate_deg) * dt

    # Ensure command is normalized
    s.rudder_cmd = max(-1.0, min(1.0, float(s.rudder_cmd or 0.0)))

    # Target servo angle (radians) from normalized command
    target_rudder_angle = s.rudder_cmd * MAX_RUDDER_RAD

    # Current servo angle defaults to 0 if None
    if s.rudder_angle is None:
        s.rudder_angle = 0.0

    # Slew the servo toward target (no wrap needed; small range)
    error = target_rudder_angle - s.rudder_angle
    if error > 0:
        s.rudder_angle += min(error, MAX_RUDDER_STEP)
    else:
        s.rudder_angle += max(error, -MAX_RUDDER_STEP)

    # Clamp to physical stop
    s.rudder_angle = max(-MAX_RUDDER_RAD, min(MAX_RUDDER_RAD, s.rudder_angle))

    # === Heading integration (CCW positive) ===
    # Scale yaw linearly with actual rudder deflection fraction
    rudder_frac = 0.0 if MAX_RUDDER_RAD == 0 else (s.rudder_angle / MAX_RUDDER_RAD)  # -1..+1
    s.heading = wrap_angle(s.heading + yaw_rate_rad * rudder_frac * dt)

    # ... keep the rest of your update (speed, planes->pitch, buoyancy, damage, etc.)

    # Manual planes map to pitch target (positive planes -> bow up)
    target_pitch = clamp(s.planes * planes_eff, -1.0, 1.0) * math.radians(20.0)
    s.pitch += clamp(target_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)

    # Speed from throttle
    s.speed = clamp(s.throttle, 0.0, 1.0) * max_spd

    # Vertical dynamics (v_down positive = deeper)
    v_down = neutral * (1.0 - s.throttle)  # natural sink if slow

    # Emergency blow (rise = reduce v_down)
    ebcfg = scfg["emergency_blow"]
    if s.blow_active and now < s.blow_end and s.blow_charge > 0.0:
        v_down -= ebcfg["upward_mps"]
        s.blow_charge = clamp(s.blow_charge - (dt / ebcfg["duration_s"]), 0.0, 1.0)
    else:
        s.blow_active = False

    # Depth-hold autopilot (only when planesÃ¢â€°Ë†0)
    autopilot_on = (s.target_depth is not None) and (abs(s.planes) < 0.05)
    if autopilot_on:
        err_m = s.target_depth - s.depth   # + if need deeper (down)
        ap_pitch = clamp(-err_m * math.radians(0.5), -math.radians(25), math.radians(25))
        s.pitch += clamp(ap_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)
        v_down += clamp(err_m * 0.02, -1.5, 1.5)

    # Hydrodynamic lift: bow-up reduces depth => subtract from v_down
    LIFT = 0.45
    v_down -= math.sin(s.pitch) * max(0.0, s.speed) * LIFT

    # Apply vertical
    s.depth = max(0.0, s.depth + v_down * dt)

    # XY
    s.x += math.cos(s.heading) * s.speed * dt
    s.y += math.sin(s.heading) * s.speed * dt

    # Battery
    bcfg = scfg["battery"]
    s.battery = clamp(s.battery - s.throttle * bcfg["drain_per_throttle_per_s"] * dt, 0.0, 100.0)
    if s.is_snorkeling and s.depth <= scfg["snorkel_depth"]:
        s.battery = clamp(s.battery + bcfg["recharge_per_s_snorkel"] * dt, 0.0, 100.0)
        s.blow_charge = clamp(s.blow_charge + ebcfg["recharge_per_s_at_snorkel"] * dt, 0.0, 1.0)
    else:
        s.is_snorkeling = False

    # Low battery safety
    if s.battery <= 0.0:
        s.throttle = min(s.throttle, 0.1)

    # Crush damage
    if s.depth > crush_depth:
        over = s.depth - crush_depth
        dps = (over / 100.0) * crush_dps
        s.health -= dps * dt
        if s.health <= 0.0: s.health = 0.0

def distance3d(ax, ay, az, bx, by, bz):
    dx = ax - bx; dy = ay - by; dz = az - bz
    return math.sqrt(dx*dx + dy*dy + dz*dz)

def explode_torpedo(t: 'TorpedoModel'):
    """Apply damage & delete the torpedo."""
    blast = GAME_CFG["torpedo"]["blast_radius"]
    # Kill everything within spherical radius
    victims = []
    for s in SubModel.query.all():
        d = distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth)
        if d <= blast:
            s.health = 0.0
            victims.append(s.id)
            send_private(s.owner_id, 'explosion', {
                "time": time.time(),
                "at": [t.x, t.y, t.depth],
                "torpedo_id": t.id,
                "blast_radius": blast
            })
    db.session.delete(t)
    return victims

def update_torpedo(t: 'TorpedoModel', dt: float, now: float):
    cfg = GAME_CFG["torpedo"]
    turn_rate = math.radians(cfg.get("turn_rate_deg_per_s", 10.0))
    depth_rate = cfg.get("depth_rate_m_per_s", 2.0)
    max_range = cfg.get("max_range", 5000.0)
    prox = cfg.get("proximity_fuze_m", 0.0)
    speed = float(getattr(t, "speed", cfg.get("speed", 20.0)))

    # Initialize start point once (for max_range tracking)
    if getattr(t, "start_x", None) is None:
        t.start_x = t.x
        t.start_y = t.y
        if getattr(t, "created_at", None) is None:
            t.created_at = now

    # --- Wire control cutoff by geometry ---
    if t.control_mode == 'wire' and t.parent_sub:
        parent = SubModel.query.get(t.parent_sub)
        if parent is not None:
            dist_parent = distance(t.x, t.y, parent.x, parent.y)
            if dist_parent > float(t.wire_length or cfg.get("wire_length", 1000.0)):
                t.control_mode = 'dumb'  # lost the wire

    # --- Heading guidance ---
    # Expect your wire commands to set either t.target_heading (abs) or t.turn_cmd (delta per request)
    if getattr(t, "target_heading", None) is not None:
        da = wrap_angle(t.target_heading - t.heading)
        step = clamp(da, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
    elif getattr(t, "pending_turn", None) is not None:
        # Optional one-shot "turn by X deg" behavior if you store it
        want = math.radians(t.pending_turn)
        step = clamp(want, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
        t.pending_turn = (math.degrees(want - step)) if abs(want - step) > 1e-4 else None

    # --- Depth guidance (toward target_depth) ---
    if getattr(t, "target_depth", None) is not None:
        dz = t.target_depth - t.depth
        t.depth += clamp(dz, -depth_rate*dt, depth_rate*dt)

    # --- Integrate position ---
    t.x += math.cos(t.heading) * speed * dt
    t.y += math.sin(t.heading) * speed * dt

    # --- Range self-destruct ---
    traveled = distance(t.x, t.y, t.start_x, t.start_y)
    if traveled > max_range:
        db.session.delete(t)
        return

    # --- Arming delay & proximity fuze (optional but nice) ---
    if prox > 0.0 and (now - t.created_at) >= cfg.get("arming_delay_s", 1.0):
        for s in SubModel.query.all():
            if s.health <= 0: 
                continue
            if distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth) <= prox:
                explode_torpedo(t)
                return





def process_wire_links():
    for t in TorpedoModel.query.all():
        parent = SubModel.query.get(t.parent_sub)
        if not parent:
            t.control_mode = 'free'; continue
        d = distance(t.x, t.y, parent.x, parent.y)
        if d > t.wire_length:
            t.control_mode = 'free'

def process_explosions():
    blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
    for t in TorpedoModel.query.all():
        for s in SubModel.query.all():
            if s.owner_id == t.owner_id and s.id == t.parent_sub:
                continue
            if distance(t.x, t.y, s.x, s.y) <= blast and abs(t.depth - s.depth) < blast:
                send_private(s.owner_id, 'explosion', {"time": time.time(), "at": [t.x, t.y]})
                s.health = 0.0
                db.session.delete(t)
                break

def schedule_passive_contacts(now: float):
    pcfg = GAME_CFG.get("sonar", {}).get("passive", DEFAULT_CFG["sonar"]["passive"])
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    subcfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    for obs in SubModel.query.all():
        if now - obs.last_report < random.uniform(*pcfg["report_interval_s"]):
            continue
        for tgt in SubModel.query.all():
            if tgt.id == obs.id and tgt.owner_id == obs.owner_id:
                continue
            speed_noise = pcfg["speed_noise_gain"] * (tgt.speed / subcfg["max_speed"])
            snorkel_bonus = pcfg["snorkel_bonus"] if tgt.is_snorkeling else 0.0
            base = pcfg["base_snr"] + speed_noise + snorkel_bonus
            rng = distance(obs.x, obs.y, tgt.x, tgt.y)
            if rng > acfg["max_range"]:
                continue
            snr = base - (rng / 1000.0) * 2.0 - (tgt.depth / 200.0)
            if snr < 5.0:
                continue
            brg = math.atan2(tgt.y - obs.y, tgt.x - obs.x)
            rel = wrap_angle(brg - obs.heading)
            jitter = math.radians(pcfg["bearing_jitter_deg"] if tgt.depth < 50.0 else 1.0)
            brg += random.uniform(-jitter, jitter); brg = wrap_angle(brg); rel = wrap_angle(brg - obs.heading)
            rc = "short" if rng < 1200 else "medium" if rng < 3000 else "long"
            send_private(obs.owner_id, 'contact', {
                "type": "passive",
                "observer_sub_id": obs.id,
                "bearing": brg,
                "bearing_relative": rel,
                "range_class": rc,
                "snr": snr,
                "time": now
            })
            obs.last_report = now
            break

def schedule_active_ping(obs: SubModel, beam_deg: float, max_range: float, now: float, center_world: float=None):
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    max_range = min(max_range, acfg["max_range"])
    if center_world is None:
        center_world = obs.heading

    for tgt in SubModel.query.all():
        if tgt.id == obs.id and tgt.owner_id == obs.owner_id:
            continue
        dx = tgt.x - obs.x; dy = tgt.y - obs.y
        rng3d = math.sqrt(dx*dx + dy*dy + (tgt.depth - obs.depth)**2)
        if rng3d > max_range: 
            continue
        brg_world = math.atan2(dy, dx)
        rel = abs(wrap_angle(brg_world - center_world))
        if rel > math.radians(beam_deg / 2.0): 
            continue
        echo_lvl = 18.0 - (rng3d / 400.0) + (8.0 if tgt.is_snorkeling else 0.0)
        c = acfg["sound_speed"]
        eta = now + 2.0 * (rng3d / c)
        PENDING_PINGS.append({
            'eta': eta,
            'rng': rng3d,
            'bearing': brg_world,
            'echo_level': echo_lvl,
            'observer_sub_id': obs.id,
            'observer_user_id': obs.owner_id,
            'observer_depth': float(obs.depth),
            'target_depth': float(tgt.depth)
        })

def process_active_pings(now: float):
    due = [p for p in PENDING_PINGS if p['eta'] <= now]
    if not due: return
    PENDING_PINGS[:] = [p for p in PENDING_PINGS if p['eta'] > now]
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    for p in due:
        lvl = float(p['echo_level'])
        q = 1.0 / (1.0 + math.exp(-(lvl - 10.0)/6.0))
        bearing_noise = math.radians(acfg["brg_sigma_deg"]) * (1.0 - q)
        range_noise   = max(5.0, acfg["rng_sigma_m"] * (1.0 - q))
        est_brg = wrap_angle(float(p['bearing']) + random.uniform(-bearing_noise, bearing_noise))
        est_rng = max(1.0, float(p['rng']) + random.uniform(-range_noise, range_noise))
        obs_depth = float(p.get('observer_depth', 0.0))
        tgt_depth = float(p.get('target_depth', obs_depth))
        dz = tgt_depth - obs_depth
        horiz = max(1e-3, math.sqrt(max(0.0, float(p['rng'])**2 - dz**2)))
        vertical_angle = math.atan2(dz, horiz)
        sigma = max(3.0, 30.0 * (1.0 - q))
        est_depth = tgt_depth + random.gauss(0.0, sigma)
        obs = SubModel.query.get(p['observer_sub_id'])
        obs_heading = float(obs.heading) if obs else 0.0
        send_private(p['observer_user_id'], 'echo', {
            'type': 'active',
            'observer_sub_id': p['observer_sub_id'],
            'bearing': est_brg,
            'bearing_relative': wrap_angle(est_brg - obs_heading),
            'range': est_rng,
            'quality': q,
            'vertical_angle': vertical_angle,
            'estimated_depth': est_depth,
            'time': now
        })

def game_loop():
    with app.app_context():
        last = time.time()
        while True:
            start = time.time()
            dt = start - last
            last = start
            with WORLD_LOCK:
                for s in SubModel.query.all():
                    if s.health <= 0.0:
                        db.session.delete(s)
                        continue
                    update_sub(s, dt, start)
                db.session.commit()
                for t in TorpedoModel.query.all():
                    update_torpedo(t, dt, start)
                process_wire_links()
                process_explosions()
                db.session.commit()
                schedule_passive_contacts(start)
                process_active_pings(start)
                # periodic snapshots to each user (throttled)
                for uid in list(USER_QUEUES.keys()):
                    if time.time() - USER_LAST.get(uid, 0) > 1.0:
                        send_snapshot(uid); USER_LAST[uid] = time.time()
            spent = time.time() - start
            time.sleep(max(0.0, TICK - spent))

# -------------------------- Routes --------------------------
@app.get('/')
def ui_home():
    return send_from_directory('.', 'ui.html')

@app.get('/public')
def public_info():
    return jsonify({"ring": WORLD_RING, "objectives": OBJECTIVES})

@app.post('/signup')
def signup():
    d = request.get_json(force=True)
    u = d.get('username','').strip(); p = d.get('password','')
    if not u or not p: return jsonify({"ok": False, "error": "username and password required"}), 400
    if User.query.filter_by(username=u).first():
        return jsonify({"ok": False, "error": "username taken"}), 400
    user = User(username=u, pw_hash=generate_password_hash(p))
    db.session.add(user); db.session.commit()
    key = make_key()
    db.session.add(ApiKey(key=key, user_id=user.id)); db.session.commit()
    return jsonify({"ok": True, "api_key": key})

@app.post('/login')
def login():
    d = request.get_json(force=True)
    u = d.get('username','').strip(); p = d.get('password','')
    user = User.query.filter_by(username=u).first()
    if not user or not check_password_hash(user.pw_hash, p):
        return jsonify({"ok": False, "error": "invalid credentials"}), 401
    key = make_key()
    db.session.add(ApiKey(key=key, user_id=user.id)); db.session.commit()
    return jsonify({"ok": True, "api_key": key})

@app.get('/rules')
def rules():
    return jsonify(GAME_CFG)

@app.get('/state')
@require_key
def state():
    with WORLD_LOCK:
        subs = SubModel.query.filter_by(owner_id=request.user.id).all()
        torps = TorpedoModel.query.filter_by(owner_id=request.user.id).all()
    def sub_pub(s: SubModel):
        return dict(
            id=s.id, x=s.x, y=s.y, depth=s.depth, heading=s.heading, pitch=s.pitch,
            rudder_angle=s.rudder_angle, rudder_cmd=s.rudder_cmd, planes=s.planes,
            speed=s.speed, battery=s.battery, is_snorkeling=s.is_snorkeling,
            blow_active=s.blow_active, blow_charge=s.blow_charge, health=s.health,
            target_depth=s.target_depth, throttle=s.throttle
        )
    def torp_pub(t: TorpedoModel):
        return dict(id=t.id, x=t.x, y=t.y, depth=t.depth, heading=t.heading,
                    speed=t.speed, mode=t.control_mode, wire_length=t.wire_length)
    return jsonify({
        "ok": True,
        "subs": [sub_pub(s) for s in subs],
        "torpedoes": [torp_pub(t) for t in torps]
    })

@app.post('/register_sub')
@require_key
def register_sub():
    with WORLD_LOCK:
        x, y = random_spawn_pos()
        bcfg = GAME_CFG.get("sub", {}).get("battery", DEFAULT_CFG["sub"]["battery"])
        bmin = bcfg.get("initial_min", DEFAULT_CFG["sub"]["battery"]["initial_min"])
        bmax = bcfg.get("initial_max", DEFAULT_CFG["sub"]["battery"]["initial_max"])
        s = SubModel(
            owner_id=request.user.id,
            x=x, y=y,
            depth=random.uniform(80.0, 180.0),
            heading=random.uniform(-math.pi, math.pi),
            pitch=0.0,
            rudder_angle=0.0,
            rudder_cmd=0.0,
            planes=0.0,
            throttle=0.2,
            speed=0.0,
            battery=random.uniform(bmin, bmax),
            is_snorkeling=False,
            blow_active=False,
            blow_charge=1.0,
            health=100.0,
            passive_dir=0.0
        )
        db.session.add(s); db.session.commit()
        return jsonify({"ok": True, "sub_id": s.id, "spawn": [s.x, s.y, s.depth]})


@app.post('/control/<sub_id>')
@require_key
def control(sub_id):
    d = request.get_json(force=True) or {}
    MAX_RUDDER_DEG = GAME_CFG.get("sub", {}).get("max_rudder_deg", 30.0)

    def clamp(v, lo, hi):  # if you already have clamp imported, remove this local one
        return lo if v < lo else hi if v > hi else v

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        # target depth: number or null to clear
        if "target_depth" in d:
            td = d["target_depth"]
            s.target_depth = None if td is None else float(td)

        # throttle: 0..1
        if "throttle" in d:
            s.throttle = clamp(float(d["throttle"]), 0.0, 1.0)

        # planes: -1..1 (positive = planes up if that's your convention)
        if "planes" in d:
            s.planes = clamp(float(d["planes"]), -1.0, 1.0)

        # RUDDER:
        # Convention: positive degrees = LEFT/port = CCW = heading increases.
        # Store both a readable angle and a normalized command for physics.
        # If you don't have these fields yet, add them to the model: rudder_angle_deg (Float), rudder_cmd (Float).
        if "rudder_deg" in d:
            rdeg = clamp(float(d["rudder_deg"]), -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_angle_deg = rdeg
            s.rudder_cmd = rdeg / MAX_RUDDER_DEG  # -1..+1

        if "rudder_nudge_deg" in d:
            nudge = float(d["rudder_nudge_deg"])
            # Start from current angle if present; else derive from cmd
            curr_deg = getattr(s, "rudder_angle_deg", (s.rudder_cmd if hasattr(s, "rudder_cmd") else 0.0) * MAX_RUDDER_DEG)
            new_deg = clamp(curr_deg + nudge, -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_angle_deg = new_deg
            s.rudder_cmd = new_deg / MAX_RUDDER_DEG

        db.session.commit()
        return jsonify({"ok": True})




@app.post('/snorkel/<sub_id>')
@require_key
def snorkel(sub_id):
    d = request.get_json(force=True)
    on = bool(d.get('on', True))
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        if on and s.depth > scfg["snorkel_depth"]:
            return jsonify({"ok": False, "error": "too deep to snorkel"}), 400
        s.is_snorkeling = on
        db.session.commit()
        return jsonify({"ok": True, "is_snorkeling": s.is_snorkeling})

@app.post('/emergency_blow/<sub_id>')
@require_key
def emergency_blow(sub_id):
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        if s.blow_charge <= 0.0:
            return jsonify({"ok": False, "error": "no charge"}), 400
        s.blow_active = True
        s.blow_end = time.time() + GAME_CFG.get("sub", DEFAULT_CFG["sub"])["emergency_blow"]["duration_s"]
        db.session.commit()
        return jsonify({"ok": True})



@app.post('/launch_torpedo/<sub_id>')
@require_key
def launch_torpedo(sub_id):
    d = request.get_json(force=True)
    wire = float(d.get('wire_length', GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["default_wire"]))
    tube = int(d.get('tube', 0))  # 0=center, negative=port, positive=starboard

    # how far ahead of the subÃ¢â‚¬â„¢s center to spawn (nose distance)
    NOSE_OFFSET = 12.0  # meters forward
    # lateral spacing between tubes (per index step)
    TUBE_SPACING = 2.0  # meters left/right

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404

        # compute spawn point in world coords from subÃ¢â‚¬â„¢s local frame
        # forward unit = (cosH, sinH), right unit = (-sinH, cosH)
        cosH, sinH = math.cos(s.heading), math.sin(s.heading)
        fwd_x, fwd_y = cosH, sinH
        right_x, right_y = -sinH, cosH

        lateral = tube * TUBE_SPACING  # +starboard, -port
        spawn_x = s.x + fwd_x * NOSE_OFFSET + right_x * lateral
        spawn_y = s.y + fwd_y * NOSE_OFFSET + right_y * lateral
        spawn_depth = s.depth

        t = TorpedoModel(
            owner_id=request.user.id,
            parent_sub=s.id,
            x=spawn_x, y=spawn_y, depth=spawn_depth,
            heading=s.heading,  # straight out the bow
            speed=GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["speed"],
            control_mode='wire',
            wire_length=wire
        )

        # (optional) arm delay to avoid instant self-kill within 1s
        t.created_at = time.time()
        db.session.add(t)
        db.session.commit()

        return jsonify({"ok": True, "torpedo_id": t.id, "wire_length": t.wire_length, "tube": tube,
                        "spawn": {"x": spawn_x, "y": spawn_y, "depth": spawn_depth}})



@app.post('/set_torp_depth/<torp_id>')
@require_key
def set_torp_depth(torp_id):
    data = request.get_json() or {}
    new_depth = float(data.get("depth", 0))
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not your torpedo"}), 403

        # set TARGET only
        t.target_depth = new_depth
        db.session.commit()
        return jsonify({"ok": True, "depth": t.depth, "target_depth": t.target_depth})




@app.post('/set_torp_heading/<torp_id>')
@require_key
def set_torp_heading(torp_id):
    d = request.get_json(force=True)
    dt = float(d.get('dt', TICK))
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        if t.control_mode != 'wire':
            return jsonify({'ok': False, 'error': 'wire lost'}), 400
        max_turn = math.radians(GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["turn_rate_deg_s"]) * dt
        if 'heading_deg' in d:
            desired = math.radians(float(d['heading_deg']))
            err = wrap_angle(desired - t.heading)
            t.heading = wrap_angle(t.heading + clamp(err, -max_turn, max_turn))
        elif 'turn_deg' in d:
            turn = math.radians(float(d['turn_deg']))
            t.heading = wrap_angle(t.heading + clamp(turn, -max_turn, max_turn))
        else:
            return jsonify({'ok': False, 'error': 'heading_deg or turn_deg required'}), 400
        t.updated_at = time.time()
        db.session.commit()
        return jsonify({'ok': True, 'torpedo': dict(id=t.id, heading=t.heading)})

@app.post('/set_passive_array/<sub_id>')
@require_key
def set_passive_array(sub_id):
    d = request.get_json(force=True)
    deg = float(d.get('dir_deg', 0.0))
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        s.passive_dir = math.radians(deg)
        db.session.commit()
        return jsonify({"ok": True})


@app.post('/ping/<sub_id>')
@require_key
def ping(sub_id):
    d = request.get_json(force=True)
    beam = float(d.get('beamwidth_deg', 20.0))
    max_r = float(d.get('max_range', GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])["max_range"]))
    center_rel_deg = d.get('center_bearing_deg', 0.0)

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        power_cfg = GAME_CFG.get("sonar", {}).get("active_power", {"cost_per_ping":1.0,"cost_per_degree":0.01,"min_battery":5.0})
        cost = power_cfg["cost_per_ping"] + (beam * power_cfg["cost_per_degree"])
        if s.battery < power_cfg["min_battery"]:
            return jsonify({"ok": False, "error": "battery too low"}), 400
        if s.battery < cost:
            return jsonify({"ok": False, "error": "not enough battery"}), 400

        # Deduct battery
        s.battery = clamp(s.battery - cost, 0.0, 100.0)

        # Cooldown to prevent ping spam (optional)
        if hasattr(s, 'ping_cooldown') and s.ping_cooldown > time.time():
            return jsonify({"ok": False, "error": "ping recharging"}), 400
        s.ping_cooldown = time.time() + 5.0  # seconds cooldown

        # Beam-centered active sonar
        center_world = wrap_angle(s.heading + math.radians(float(center_rel_deg)))
        schedule_active_ping(s, beam, max_r, time.time(), center_world=center_world)

        # Notify all other subs they heard this ping
        for other in SubModel.query.all():
            if other.id == s.id:
                continue
            snr = 5.0 * (beam / 90.0) - (distance(s.x, s.y, other.x, other.y) / 800.0)
            if snr > 1.0:
                send_private(other.owner_id, 'contact', {
                    "type": "active_ping_detected",
                    "observer_sub_id": other.id,
                    "bearing": math.atan2(s.y - other.y, s.x - other.x),
                    "snr": snr,
                    "time": time.time()
                })

        db.session.commit()
        return jsonify({
            "ok": True,
            "battery_cost": round(cost, 2),
            "battery_remaining": round(s.battery, 2)
        })


@app.get('/stream')
def stream():
    user = get_user_from_api()
    if not user:
        return Response("unauthorized\n", status=401)
    q = _uq(user.id)

    @stream_with_context
    def gen():
        with app.app_context():
            try:
                yield ":" + (" " * 2048) + "\n"
                yield "retry: 2000\n"
                yield "event: hello\ndata: {}\n\n"
                send_snapshot(user.id)
                while True:
                    try:
                        chunk = q.get(timeout=15.0)
                        yield chunk
                    except queue.Empty:
                        now = time.time()
                        yield f"event: ping\ndata: {{\"t\": {now} }}\n\n"
            except GeneratorExit:
                return
            except Exception as e:
                try:
                    yield f"event: error\ndata: {{\"msg\": \"{str(e)}\"}}\n\n"
                except Exception:
                    pass

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)

# -------------------------- Admin --------------------------
@app.get('/admin/state')
@require_key
def admin_state():
    if not request.user.is_admin:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    with WORLD_LOCK:
        subs = [dict(
            id=s.id,x=s.x,y=s.y,depth=s.depth,heading=s.heading,pitch=s.pitch,
            rudder_angle=s.rudder_angle,rudder_cmd=s.rudder_cmd,planes=s.planes,
            speed=s.speed,battery=s.battery,is_snorkeling=s.is_snorkeling,
            blow_active=s.blow_active,blow_charge=s.blow_charge,health=s.health,
            target_depth=s.target_depth, throttle=s.throttle, owner_id=s.owner_id
        ) for s in SubModel.query.all()]
        torps = [dict(
            id=t.id,x=t.x,y=t.y,depth=t.depth,heading=t.heading,speed=t.speed,
            mode=t.control_mode,wire_length=t.wire_length,owner_id=t.owner_id,parent_sub=t.parent_sub
        ) for t in TorpedoModel.query.all()]
    return jsonify({"ok": True, "subs": subs, "torpedoes": torps})

# -------------------------- Boot --------------------------
def ensure_admin():
    admin_user = os.environ.get('SB_ADMIN_USER')
    admin_pass = os.environ.get('SB_ADMIN_PASS')
    if not admin_user or not admin_pass:
        return
    u = User.query.filter_by(username=admin_user).first()
    if not u:
        u = User(username=admin_user, pw_hash=generate_password_hash(admin_pass), is_admin=True)
        db.session.add(u); db.session.commit()
        k = make_key(); db.session.add(ApiKey(key=k, user_id=u.id)); db.session.commit()
        print(f"[ADMIN] created @{admin_user}  API={k}")
    elif not u.is_admin:
        u.is_admin = True; db.session.commit()

@app.post('/detonate/<torp_id>')
@require_key
def detonate_torp(torp_id):
    """
    Owner can command a torpedo to detonate immediately.
    All subs (except the torpedo's parent sub) within blast_radius
    and within vertical tolerance are killed (health=0) and notified.
    """
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        # Only owner may detonate their torpedo
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not allowed"}), 403

        blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
        # Collect affected subs
        affected = []
        for s in SubModel.query.all():
            # Optionally skip the firing sub itself (or include if you want friendly-fire)
            if s.id == t.parent_sub:
                # include friendly fire if desired; here we include (owner is allowed)
                pass
            horiz = distance(t.x, t.y, s.x, s.y)
            vert = abs(t.depth - s.depth)
            # simple 3D sphere test using blast radius
            if math.sqrt(horiz*horiz + vert*vert) <= blast:
                s.health = 0.0
                affected.append(s)
        # notify affected users and any observers
        for s in affected:
            send_private(s.owner_id, 'explosion', {
                "time": time.time(),
                "at": [t.x, t.y, t.depth],
                "torpedo_id": t.id,
                "blast_radius": blast
            })
        # finally remove the torpedo
        db.session.delete(t)
        db.session.commit()
        return jsonify({"ok": True, "affected": len(affected)})



if __name__ == '__main__':
    with app.app_context():
        ensure_admin()
    th = threading.Thread(target=game_loop, daemon=True)
    th.start()
    # For dev: threaded server is fine now that we tune SQLite + lock snapshots
    app.run(host='0.0.0.0', port=5000, threaded=True)
