#!/usr/bin/env python3
import os, json, math, random, time, threading, queue, uuid, copy
from typing import Dict, List, Tuple
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError
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
        "snorkel_off_hysteresis": 2.0,  # <-- new: prevents flapping at the limit
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
        "default_wire": 600.0,
        "max_range": 5000.0,
        "proximity_fuze_m": 0.0,
        "arming_delay_s": 1.0
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
        },
        "active_power": {
            "cost_per_ping": 1.0,
            "cost_per_degree": 0.01,
            "min_battery": 5.0
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
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=100.0)
    heading = db.Column(db.Float, default=0.0)       # radians, 0=east CCW+
    pitch = db.Column(db.Float, default=0.0)         # + = bow up
    rudder_angle = db.Column(db.Float, default=0.0)  # radians (servo)
    rudder_cmd = db.Column(db.Float, default=0.0)    # -1..+1
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
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
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
    try:
        with db.engine.connect() as con:
            con.execute(text("PRAGMA journal_mode=WAL;"))
            con.execute(text("PRAGMA synchronous=NORMAL;"))
            con.execute(text("PRAGMA busy_timeout=60000;"))
            con.execute(text("PRAGMA temp_store=MEMORY;"))
    except Exception as e:
        print("[DB] PRAGMA set failed:", e)
    print("[DB] SQLite WAL enabled, busy_timeout=60000ms, synchronous=NORMAL")

# -------------------------- Helpers --------------------------
def clamp(v, lo, hi): return max(lo, min(hi, v))
def wrap_angle(a):
    while a >= math.pi: a -= 2*math.pi
    while a < -math.pi: a += 2*math.pi
    return a
def distance(x1,y1,x2,y2): return math.hypot(x2-x1, y2-y1)
def distance3d(ax, ay, az, bx, by, bz):
    dx = ax - bx; dy = ay - by; dz = az - bz
    return math.sqrt(dx*dx + dy*dy + dz*dz)

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

def _sub_pub(s: SubModel):
    return dict(
        id=s.id, x=s.x, y=s.y, depth=s.depth, heading=s.heading, pitch=s.pitch,
        rudder_angle=s.rudder_angle, rudder_cmd=s.rudder_cmd, planes=s.planes,
        speed=s.speed, battery=s.battery, is_snorkeling=s.is_snorkeling,
        blow_active=s.blow_active, blow_charge=s.blow_charge, health=s.health,
        target_depth=s.target_depth, throttle=s.throttle
    )

def _torp_pub(t: TorpedoModel):
    return dict(id=t.id, x=t.x, y=t.y, depth=t.depth, heading=t.heading,
                speed=t.speed, mode=t.control_mode, wire_length=t.wire_length)

def send_snapshot(user_id: int):
    with WORLD_LOCK:
        subs = SubModel.query.filter_by(owner_id=user_id).all()
        torps = TorpedoModel.query.filter_by(owner_id=user_id).all()
    q = _uq(user_id)
    obj = {
        "subs":[_sub_pub(s) for s in subs],
        "torpedoes":[_torp_pub(t) for t in torps],
        "time": time.time()
    }
    payload = f"event: snapshot\ndata: {json.dumps(obj)}\n\n"
    try: q.put_nowait(payload)
    except queue.Full: pass

def send_snapshot_mem(user_id: int, subs: List[SubModel], torps: List[TorpedoModel]):
    q = _uq(user_id)
    obj = {
        "subs":[_sub_pub(s) for s in subs if s.owner_id == user_id],
        "torpedoes":[_torp_pub(t) for t in torps if t.owner_id == user_id],
        "time": time.time()
    }
    try: q.put_nowait(f"event: snapshot\ndata: {json.dumps(obj)}\n\n")
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
    yaw_rate_rad = math.radians(scfg["yaw_rate_deg_s"])
    pitch_rate   = math.radians(scfg["pitch_rate_deg_s"])
    planes_eff   = scfg["planes_effect"]
    neutral      = scfg["neutral_bias"]
    crush_depth  = scfg["crush_depth"]
    crush_dps    = scfg["crush_dps_per_100m"]

    # RUDDER servo
    max_rudder_deg  = scfg.get("max_rudder_deg", 30.0)
    rudder_rate_deg = scfg.get("rudder_rate_deg_s", 60.0)
    MAX_RUDDER_RAD  = math.radians(max_rudder_deg)
    MAX_RUDDER_STEP = math.radians(rudder_rate_deg) * dt

    s.rudder_cmd = clamp(float(s.rudder_cmd or 0.0), -1.0, 1.0)
    target_rudder_angle = s.rudder_cmd * MAX_RUDDER_RAD
    if s.rudder_angle is None:
        s.rudder_angle = 0.0
    error = target_rudder_angle - s.rudder_angle
    s.rudder_angle += clamp(error, -MAX_RUDDER_STEP, MAX_RUDDER_STEP)
    s.rudder_angle = clamp(s.rudder_angle, -MAX_RUDDER_RAD, MAX_RUDDER_RAD)

    # Heading integration
    rudder_frac = 0.0 if MAX_RUDDER_RAD == 0 else (s.rudder_angle / MAX_RUDDER_RAD)
    s.heading = wrap_angle(s.heading + yaw_rate_rad * rudder_frac * dt)

    # Planes -> pitch
    target_pitch = clamp(s.planes * planes_eff, -1.0, 1.0) * math.radians(20.0)
    s.pitch += clamp(target_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)

    # Speed
    s.speed = clamp(s.throttle, 0.0, 1.0) * max_spd

    # Vertical dynamics
    v_down = neutral * (1.0 - s.throttle)

    # Emergency blow
    ebcfg = scfg["emergency_blow"]
    if s.blow_active and now < s.blow_end and s.blow_charge > 0.0:
        v_down -= ebcfg["upward_mps"]
        s.blow_charge = clamp(s.blow_charge - (dt / ebcfg["duration_s"]), 0.0, 1.0)
    else:
        s.blow_active = False

    # Depth hold autopilot
    autopilot_on = (s.target_depth is not None) and (abs(s.planes) < 0.05)
    if autopilot_on:
        err_m = s.target_depth - s.depth
        ap_pitch = clamp(-err_m * math.radians(0.5), -math.radians(25), math.radians(25))
        s.pitch += clamp(ap_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)
        v_down += clamp(err_m * 0.02, -1.5, 1.5)

    # Hydrodynamic lift
    LIFT = 0.45
    v_down -= math.sin(s.pitch) * max(0.0, s.speed) * LIFT

    # Apply vertical
    s.depth = max(0.0, s.depth + v_down * dt)

    # XY
    s.x += math.cos(s.heading) * s.speed * dt
    s.y += math.sin(s.heading) * s.speed * dt

    # Battery & snorkel recharge + auto-off with hysteresis
    bcfg = scfg["battery"]
    s.battery = max(0.0, min(100.0, s.battery - s.throttle * bcfg["drain_per_throttle_per_s"] * dt))

    snorkel_depth = scfg.get("snorkel_depth", 15.0)
    off_hyst = scfg.get("snorkel_off_hysteresis", 2.0)
    if s.is_snorkeling and s.depth <= snorkel_depth:
        s.battery = clamp(s.battery + bcfg["recharge_per_s_snorkel"] * dt, 0.0, 100.0)
        s.blow_charge = clamp(s.blow_charge + ebcfg["recharge_per_s_at_snorkel"] * dt, 0.0, 1.0)
    # Auto-off once we exceed snorkel depth + hysteresis
    if s.is_snorkeling and s.depth > (snorkel_depth + off_hyst):
        s.is_snorkeling = False

    # Low battery safety
    if s.battery <= 0.0:
        s.throttle = min(s.throttle, 0.1)

    # Crush damage
    if s.depth > crush_depth:
        over = s.depth - crush_depth
        dps = (over / 100.0) * crush_dps
        s.health = max(0.0, s.health - dps * dt)

def explode_torpedo_in_mem(t: TorpedoModel, subs: List[SubModel], pending_events: List[Tuple[int,str,dict]]):
    blast = GAME_CFG["torpedo"]["blast_radius"]
    for s in subs:
        d = distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth)
        if d <= blast and s.health > 0.0:
            s.health = 0.0
            pending_events.append((
                s.owner_id, 'explosion',
                {"time": time.time(), "at": [t.x, t.y, t.depth], "torpedo_id": t.id, "blast_radius": blast}
            ))

def update_torpedo(t: TorpedoModel, dt: float, now: float):
    cfg = GAME_CFG["torpedo"]
    turn_rate = math.radians(cfg.get("turn_rate_deg_s", DEFAULT_CFG["torpedo"]["turn_rate_deg_s"]))
    depth_rate = float(cfg.get("depth_rate_m_s", DEFAULT_CFG["torpedo"]["depth_rate_m_s"]))
    max_range = float(cfg.get("max_range", DEFAULT_CFG["torpedo"]["max_range"]))
    prox = float(cfg.get("proximity_fuze_m", DEFAULT_CFG["torpedo"]["proximity_fuze_m"]))
    speed = float(getattr(t, "speed", cfg.get("speed", DEFAULT_CFG["torpedo"]["speed"])))

    if getattr(t, "start_x", None) is None:
        t.start_x = t.x
        t.start_y = t.y
        if getattr(t, "created_at", None) is None:
            t.created_at = now

    # Wire control cutoff by geometry
    if t.control_mode == 'wire' and t.parent_sub:
        parent = SubModel.query.get(t.parent_sub)  # occasional lookup
        if parent is not None:
            dist_parent = distance(t.x, t.y, parent.x, parent.y)
            if dist_parent > float(t.wire_length or cfg.get("default_wire", 1000.0)):
                t.control_mode = 'free'

    # Heading guidance
    if getattr(t, "target_heading", None) is not None:
        da = wrap_angle(t.target_heading - t.heading)
        step = clamp(da, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
    elif getattr(t, "pending_turn", None) is not None:
        want = math.radians(t.pending_turn)
        step = clamp(want, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
        rem = want - step
        t.pending_turn = (math.degrees(rem)) if abs(rem) > 1e-4 else None

    # Depth guidance
    if getattr(t, "target_depth", None) is not None:
        dz = t.target_depth - t.depth
        t.depth += clamp(dz, -depth_rate*dt, depth_rate*dt)

    # Integrate position
    t.x += math.cos(t.heading) * speed * dt
    t.y += math.sin(t.heading) * speed * dt

    # Range self-destruct
    traveled = distance(t.x, t.y, t.start_x, t.start_y)
    if traveled > max_range:
        t._expired = True
        return

    # Proximity fuze check flag (blast handled later)
    t._check_prox = prox > 0.0 and (now - t.created_at) >= cfg.get("arming_delay_s", 1.0)

def process_wire_links_mem(torps: List[TorpedoModel], subs: List[SubModel]):
    for t in torps:
        if t.control_mode != 'wire':
            continue
        parent = next((s for s in subs if s.id == t.parent_sub), None)
        if not parent:
            t.control_mode = 'free'; continue
        d = distance(t.x, t.y, parent.x, parent.y)
        if d > float(t.wire_length or GAME_CFG["torpedo"].get("default_wire", 1000.0)):
            t.control_mode = 'free'

def process_explosions_mem(torps: List[TorpedoModel], subs: List[SubModel], pending_events: List[Tuple[int,str,dict]]):
    blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
    for t in torps:
        if getattr(t, "_expired", False):
            t._delete = True
            continue
        if getattr(t, "_check_prox", False):
            for s in subs:
                if s.health <= 0: 
                    continue
                if distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth) <= blast:
                    explode_torpedo_in_mem(t, subs, pending_events)
                    t._delete = True
                    break

def schedule_passive_contacts(now: float, subs: List[SubModel], pending_events: List[Tuple[int,str,dict]]):
    pcfg = GAME_CFG.get("sonar", {}).get("passive", DEFAULT_CFG["sonar"]["passive"])
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    subcfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    for obs in subs:
        if now - obs.last_report < random.uniform(*pcfg["report_interval_s"]):
            continue
        for tgt in subs:
            if tgt.owner_id == obs.owner_id and tgt.id == obs.id:
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
            jitter = math.radians(pcfg["bearing_jitter_deg"] if tgt.depth < 50.0 else 1.0)
            brg = wrap_angle(brg + random.uniform(-jitter, jitter))
            rel = wrap_angle(brg - obs.heading)
            rc = "short" if rng < 1200 else "medium" if rng < 3000 else "long"
            pending_events.append((obs.owner_id,'contact',{
                "type": "passive",
                "observer_sub_id": obs.id,
                "bearing": brg,
                "bearing_relative": rel,
                "range_class": rc,
                "snr": snr,
                "time": now
            }))
            obs.last_report = now
            break

def schedule_active_ping(obs: SubModel, beam_deg: float, max_range: float, now: float, center_world: float=None):
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    max_range = min(max_range, acfg["max_range"])
    if center_world is None:
        center_world = obs.heading

    for tgt in SubModel.query.all():  # rare read
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
        obs = SubModel.query.get(p['observer_sub_id'])
        obs_heading = float(obs.heading) if obs else 0.0
        send_private(p['observer_user_id'], 'echo', {
            'type': 'active',
            'observer_sub_id': p['observer_sub_id'],
            'bearing': est_brg,
            'bearing_relative': wrap_angle(est_brg - obs_heading),
            'range': est_rng,
            'quality': q,
            'time': now
        })

# -------------------------- PERF --------------------------
_perf = {"tick_ms": 0.0, "db_fetch_ms": 0.0, "physics_ms": 0.0, "db_commit_ms": 0.0}
def _ms(t0): return (time.time() - t0) * 1000.0

# -------------------------- Main loop --------------------------
def _apply_fields(dst, src, fields):
    for f in fields:
        setattr(dst, f, getattr(src, f))

def game_loop():
    with app.app_context():
        last_ts = time.time()
        while True:
            loop_start = time.time()
            dt = max(0.0, min(loop_start - last_ts, 0.25))  # clamp dt
            last_ts = loop_start
            try:
                # 1) Snapshot
                t0 = time.time()
                with WORLD_LOCK:
                    subs: List[SubModel] = SubModel.query.all()
                    torps: List[TorpedoModel] = TorpedoModel.query.all()
                _perf["db_fetch_ms"] = _ms(t0)

                # 2) Physics outside lock
                t1 = time.time()
                pending_events: List[Tuple[int,str,dict]] = []
                dead_sub_ids = set()

                for s in subs:
                    if s.health <= 0.0:
                        dead_sub_ids.add(s.id); continue
                    update_sub(s, dt, loop_start)

                for t in torps:
                    update_torpedo(t, dt, loop_start)

                process_wire_links_mem(torps, subs)
                process_explosions_mem(torps, subs, pending_events)
                schedule_passive_contacts(loop_start, subs, pending_events)
                process_active_pings(loop_start)
                _perf["physics_ms"] = _ms(t1)

                # 3) Commit once, resilient to concurrent deletes
                t2 = time.time()
                with WORLD_LOCK:
                    try:
                        # delete dead subs
                        for sid in list(dead_sub_ids):
                            srow = db.session.get(SubModel, sid)
                            if srow: db.session.delete(srow)

                        # delete marked/expired torps
                        for t in torps:
                            if getattr(t, "_delete", False) or getattr(t, "_expired", False):
                                trow = db.session.get(TorpedoModel, t.id)
                                if trow: db.session.delete(trow)

                        # update subs that still exist
                        sub_fields = ["x","y","depth","heading","pitch","rudder_angle","rudder_cmd",
                                      "planes","throttle","target_depth","speed","battery","is_snorkeling",
                                      "blow_active","blow_charge","blow_end","health","passive_dir","last_report"]
                        for s in subs:
                            if s.id in dead_sub_ids: 
                                continue
                            srow = db.session.get(SubModel, s.id)
                            if srow:
                                _apply_fields(srow, s, sub_fields)

                        # update torps that still exist
                        torp_fields = ["x","y","depth","target_depth","heading","speed",
                                       "control_mode","wire_length","updated_at","created_at"]
                        for t in torps:
                            if getattr(t, "_delete", False) or getattr(t, "_expired", False):
                                continue
                            trow = db.session.get(TorpedoModel, t.id)
                            if trow:
                                _apply_fields(trow, t, torp_fields)

                        db.session.commit()

                    except StaleDataError as se:
                        print("[GAME_LOOP] StaleDataError during commit:", se, flush=True)
                        db.session.rollback()
                    except Exception as e:
                        print("[GAME_LOOP] Commit error:", repr(e), flush=True)
                        db.session.rollback()
                _perf["db_commit_ms"] = _ms(t2)

                # 4) Fan-out (no lock)
                for uid, ev, obj in pending_events:
                    send_private(uid, ev, obj)
                now = time.time()
                for uid in list(USER_QUEUES.keys()):
                    if now - USER_LAST.get(uid, 0) > 1.0:
                        send_snapshot_mem(uid, subs, torps)
                        USER_LAST[uid] = now

            except Exception as e:
                print("[GAME_LOOP] ERROR:", repr(e), flush=True)
                try:
                    db.session.rollback()
                except Exception:
                    pass

            _perf["tick_ms"] = _ms(loop_start)
            sleep_left = TICK - (time.time() - loop_start)
            if sleep_left > 0:
                time.sleep(sleep_left)

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
    return jsonify({
        "ok": True,
        "time": time.time(),
        "subs": [_sub_pub(s) for s in subs],
        "torpedoes": [_torp_pub(t) for t in torps]
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

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        if "target_depth" in d:
            td = d["target_depth"]
            s.target_depth = None if td is None else float(td)

        if "throttle" in d:
            s.throttle = clamp(float(d["throttle"]), 0.0, 1.0)

        if "planes" in d:
            s.planes = clamp(float(d["planes"]), -1.0, 1.0)

        if "rudder_deg" in d:
            rdeg = clamp(float(d["rudder_deg"]), -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_cmd = rdeg / MAX_RUDDER_DEG

        if "rudder_nudge_deg" in d:
            nudge = float(d["rudder_nudge_deg"])
            curr_deg = (s.rudder_cmd or 0.0) * MAX_RUDDER_DEG
            new_deg = clamp(curr_deg + nudge, -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_cmd = new_deg / MAX_RUDDER_DEG

        db.session.commit()
        return jsonify({"ok": True})

# --- UPDATED: snorkel route with toggle + depth enforcement ---
@app.post('/snorkel/<sub_id>')
@require_key
def snorkel(sub_id):
    d = (request.get_json(silent=True) or {})
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    snorkel_depth = scfg.get("snorkel_depth", 15.0)

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        # Toggle if no explicit "on" passed, or toggle:true provided
        do_toggle = bool(d.get("toggle", False)) or ("on" not in d)

        if do_toggle:
            if not s.is_snorkeling:
                if s.depth > snorkel_depth:
                    return jsonify({"ok": False, "error": "too deep to snorkel"}), 400
                s.is_snorkeling = True
            else:
                s.is_snorkeling = False
        else:
            want_on = bool(d.get("on", True))
            if want_on:
                if s.depth > snorkel_depth:
                    return jsonify({"ok": False, "error": "too deep to snorkel"}), 400
                s.is_snorkeling = True
            else:
                s.is_snorkeling = False

        db.session.commit()
        return jsonify({"ok": True, "is_snorkeling": s.is_snorkeling, "depth": s.depth, "limit": snorkel_depth})

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
    tube = int(d.get('tube', 0))

    NOSE_OFFSET = 12.0
    TUBE_SPACING = 2.0

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404

        cosH, sinH = math.cos(s.heading), math.sin(s.heading)
        right_x, right_y = -sinH, cosH

        lateral = tube * TUBE_SPACING
        spawn_x = s.x + cosH * NOSE_OFFSET + right_x * lateral
        spawn_y = s.y + sinH * NOSE_OFFSET + right_y * lateral
        spawn_depth = s.depth

        t = TorpedoModel(
            owner_id=request.user.id,
            parent_sub=s.id,
            x=spawn_x, y=spawn_y, depth=spawn_depth,
            heading=s.heading,
            speed=GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["speed"],
            control_mode='wire',
            wire_length=wire
        )
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

        s.battery = clamp(s.battery - cost, 0.0, 100.0)

        if hasattr(s, 'ping_cooldown') and s.ping_cooldown > time.time():
            return jsonify({"ok": False, "error": "ping recharging"}), 400
        s.ping_cooldown = time.time() + 5.0

        center_world = wrap_angle(s.heading + math.radians(float(center_rel_deg)))
        schedule_active_ping(s, beam, max_r, time.time(), center_world=center_world)

        # Notify others
        others = SubModel.query.all()
        for other in others:
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

@app.get('/perf')
def perf():
    return jsonify(dict(ok=True, **_perf, queues=len(USER_QUEUES)))

# -------------------------- Admin & boot (Flask 3.x safe) --------------------------
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

from threading import Lock
_loop_started = False
_loop_lock = Lock()

def _start_loop_once():
    global _loop_started
    if _loop_started: return
    with _loop_lock:
        if _loop_started: return
        with app.app_context():
            ensure_admin()
        th = threading.Thread(target=game_loop, daemon=True)
        th.start()
        _loop_started = True
        print("[GAME] loop started")

@app.before_request
def _ensure_loop():
    if not _loop_started:
        _start_loop_once()

@app.post('/detonate/<torp_id>')
@require_key
def detonate_torp(torp_id):
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not allowed"}), 403
        blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
        affected = []
        for s in SubModel.query.all():
            horiz = distance(t.x, t.y, s.x, s.y)
            vert = abs(t.depth - s.depth)
            if math.sqrt(horiz*horiz + vert*vert) <= blast:
                s.health = 0.0
                affected.append(s)
                send_private(s.owner_id, 'explosion', {
                    "time": time.time(),
                    "at": [t.x, t.y, t.depth],
                    "torpedo_id": t.id,
                    "blast_radius": blast
                })
        db.session.delete(t)
        db.session.commit()
        return jsonify({"ok": True, "affected": len(affected)})

if __name__ == '__main__':
    with app.app_context():
        ensure_admin()
    th = threading.Thread(target=game_loop, daemon=True)
    th.start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
