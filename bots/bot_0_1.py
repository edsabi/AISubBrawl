#!/usr/bin/env python3
import requests
import json
import time
import math
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum

class TacticalState(Enum):
    PATROL = "patrol"
    HUNT = "hunt"
    ATTACK = "attack"
    EVADE = "evade"
    RECHARGE = "recharge"

@dataclass
class Contact:
    bearing: float
    range_class: str
    snr: float
    last_seen: float
    estimated_range: Optional[float] = None

class SmartBatterySubmarine:
    def __init__(self, username="sub", password="secret", sub_id=None):
        self.base_url = "http://localhost:42066"
        self.api_key = None
        self.sub_id = sub_id
        self.name = username
        self.log_file = f"/tmp/{username}_smart.log"
        
        self.state = TacticalState.PATROL
        self.contacts = {}
        self.last_snapshot = {}
        self.running = False
        
        # Torpedo homing data
        self.torpedo_homing_states = {}
        self.torpedo_ping_contacts = {}
        self.torpedo_passive_contacts = {}
        
        # Clear log file
        with open(self.log_file, 'w') as f:
            f.write(f"=== {username} SMART BATTERY LOG ===\n")
        
        self._authenticate(username, password)
        
        if sub_id:
            self.sub_id = sub_id
            self.log_thinking("INIT", f"Using existing submarine ID: {self.sub_id}")
        else:
            self._find_or_register_submarine()
            
    def _find_or_register_submarine(self):
        # Check for existing submarines first
        response = requests.get(f"{self.base_url}/state", 
            headers={"Authorization": f"Bearer {self.api_key}"})
        result = response.json()
        
        if result.get("ok") and result.get("subs"):
            # Use existing submarine
            existing_sub = result["subs"][0]
            self.sub_id = existing_sub["id"]
            self.log_thinking("FOUND", f"Using existing submarine {self.sub_id[:8]} at ({existing_sub['x']:.0f}, {existing_sub['y']:.0f})")
        else:
            # No existing submarine, spawn new one
            self._register_new_submarine()
            
    def _register_new_submarine(self):
        response = requests.post(f"{self.base_url}/register_sub", 
            params={"api_key": self.api_key})
        result = response.json()
        if result.get("ok"):
            self.sub_id = result["sub_id"]
            spawn = result["spawn"]
            self.log_thinking("DEPLOYED", f"New submarine spawned at {spawn}")
        else:
            raise Exception("Failed to register new submarine")
        
    def log_thinking(self, decision, reasoning, data=None):
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {decision}: {reasoning}"
        if data:
            log_entry += f" | {data}"
        log_entry += "\n"
        
        with open(self.log_file, 'a') as f:
            f.write(log_entry)
        print(f"ðŸ”‹ {self.name}: {log_entry.strip()}")
    
    def _authenticate(self, username: str, password: str):
        # Try login first
        response = requests.post(f"{self.base_url}/login", 
            json={"username": username, "password": password})
        result = response.json()
        if result.get("ok"):
            self.api_key = result["api_key"]
            self.log_thinking("AUTH", f"Logged in as {username}")
        else:
            # Try signup if login fails
            response = requests.post(f"{self.base_url}/signup", 
                json={"username": username, "password": password})
            result = response.json()
            if result.get("ok"):
                self.api_key = result["api_key"]
                self.log_thinking("AUTH", f"Signed up as {username}")
            else:
                raise Exception(f"Auth failed: {result}")
    
    def _api_call(self, method: str, endpoint: str, data=None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if data:
            headers["Content-Type"] = "application/json"
        
        url = f"{self.base_url}{endpoint}"
        response = requests.request(method, url, headers=headers, json=data)
        return response.json() if response.content else {}
    
    def _stream_events(self):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        while self.running:
            try:
                self.log_thinking("STREAM_CONNECT", "Connecting to event stream")
                
                # Use shorter timeout and enable keepalive
                response = requests.get(
                    f"{self.base_url}/stream", 
                    headers=headers, 
                    stream=True, 
                    timeout=(10, 30),  # (connect_timeout, read_timeout)
                    params={'keepalive': '1'}
                )
                response.raise_for_status()
                
                self.log_thinking("STREAM_CONNECTED", "Event stream connected successfully")
                current_event = None
                line_count = 0
                
                for line in response.iter_lines(decode_unicode=True, chunk_size=1):
                    if not self.running:
                        break
                    
                    line_count += 1
                    
                    # Heartbeat every 100 lines to detect broken connections early
                    if line_count % 100 == 0:
                        try:
                            # Test if connection is still alive
                            response.raw._fp.fp.getpeername()
                        except:
                            self.log_thinking("STREAM_DEAD", "Connection dead - reconnecting")
                            break
                    
                    if not line or not line.strip():
                        continue
                        
                    line = line.strip()
                    
                    try:
                        if line.startswith('event: '):
                            current_event = line[7:]
                        elif line.startswith('data: '):
                            data_str = line[6:]
                            if data_str and data_str != '{}':  # Skip empty data
                                data = json.loads(data_str)
                                data['type'] = current_event
                                self._process_event(data)
                    except json.JSONDecodeError:
                        # Skip malformed JSON - don't crash
                        continue
                    except Exception as e:
                        # Log but continue processing
                        self.log_thinking("EVENT_ERROR", f"Event processing error: {e}")
                        continue
                        
            except (requests.exceptions.ConnectionError, 
                    requests.exceptions.ChunkedEncodingError,
                    BrokenPipeError, 
                    ConnectionResetError) as e:
                self.log_thinking("STREAM_BROKEN", f"Connection broken: {type(e).__name__} - auto-reconnecting in 3s")
                if self.running:
                    time.sleep(3)
                    
            except requests.exceptions.Timeout:
                self.log_thinking("STREAM_TIMEOUT", "Stream timeout - reconnecting immediately")
                # No sleep for timeouts - reconnect immediately
                
            except Exception as e:
                self.log_thinking("STREAM_ERROR", f"Unexpected stream error: {e} - reconnecting in 5s")
                if self.running:
                    time.sleep(5)
        
        self.log_thinking("STREAM_SHUTDOWN", "Event stream shutdown")
    
    def _process_event(self, event):
        event_type = event.get('type')
        
        # Debug logging for all events with details
        if event_type not in ['snapshot']:
            self.log_thinking("EVENT", f"Type: {event_type}, Data: {str(event)[:100]}")
        
        if event_type == 'snapshot':
            self.last_snapshot = event
        
        elif event_type in ['passive', 'active_ping_detected', 'contact']:
            bearing = event['bearing']
            contact_id = f"c_{bearing:.2f}"
            
            self.contacts[contact_id] = Contact(
                bearing=bearing,
                range_class=event.get('range_class', 'medium'),
                snr=event.get('snr', 5.0),
                last_seen=time.time()
            )
            
            self.log_thinking("CONTACT", f"Bearing {math.degrees(bearing):.0f}Â°, SNR {event.get('snr', 5.0):.1f}")
            
            # Follow up with directional active ping if battery allows
            if self.last_snapshot.get('subs') and event.get('snr', 0) > 6.0:
                sub = self.last_snapshot['subs'][0]
                if sub['battery'] > 30:  # Only ping if decent battery
                    # Calculate relative bearing for ping
                    relative_bearing = math.degrees(bearing - sub['heading'])
                    
                    self._api_call("POST", f"/ping/{self.sub_id}", {
                        "beamwidth_deg": 30,  # Narrow beam for precision
                        "max_range": 3000,
                        "center_bearing_deg": relative_bearing
                    })
                    self.log_thinking("ACTIVE_FOLLOW", f"Directional ping at {relative_bearing:.0f}Â° relative to investigate contact")
        
        elif event_type in ['active', 'echo']:
            bearing = event['bearing']
            contact_id = f"c_{bearing:.2f}"
            
            # Create or update contact from active echo
            if contact_id in self.contacts:
                self.contacts[contact_id].estimated_range = event['range']
            else:
                # Create new contact from active echo
                self.contacts[contact_id] = Contact(
                    bearing=bearing,
                    range_class="short" if event['range'] < 1000 else "medium" if event['range'] < 2500 else "long",
                    snr=10.0,  # Assume good SNR for active return
                    last_seen=time.time(),
                    estimated_range=event['range']
                )
            
            self.log_thinking("RANGE", f"Active echo: {event['range']:.0f}m at {math.degrees(bearing):.0f}Â°")
        
        elif event_type in ['torpedo_contact', 'torpedo_ping']:
            # Torpedo detected something
            if 'torpedo_id' in event:
                torp_id = event['torpedo_id']
                if event_type == 'torpedo_contact':
                    bearing = event.get('bearing', 0)
                    self.torpedo_passive_contacts[torp_id] = {
                        'bearing': bearing,
                        'time': time.time()
                    }
                    self.log_thinking("TORPEDO_CONTACT", f"Torpedo {torp_id[:8]} detected target at {math.degrees(bearing):.0f}Â°")
                    
                elif event_type == 'torpedo_ping':
                    contacts = event.get('contacts', [])
                    if contacts:
                        # Store ping contacts for homing
                        self.torpedo_ping_contacts[torp_id] = {
                            'contacts': contacts,
                            'time': time.time()
                        }
                        self.log_thinking("TORPEDO_SONAR", f"Torpedo {torp_id[:8]} sonar: {len(contacts)} contacts")
                        
                        # Start homing if not already active
                        if torp_id not in self.torpedo_homing_states:
                            self.torpedo_homing_states[torp_id] = True
                            threading.Thread(target=self._torpedo_homing_loop, args=(torp_id,)).start()
    
    def _analyze_situation(self):
        # Clean old contacts
        current_time = time.time()
        old_count = len(self.contacts)
        self.contacts = {k: v for k, v in self.contacts.items() 
                        if current_time - v.last_seen < 30}
        
        if len(self.contacts) < old_count:
            self.log_thinking("CONTACT_LOST", f"Lost {old_count - len(self.contacts)} contacts")
        
        # Get current submarine state
        if not self.last_snapshot.get('subs'):
            return
        
        sub = self.last_snapshot['subs'][0]
        battery = sub['battery']
        
        # WEIGHTED PRIORITY SYSTEM
        old_state = self.state
        
        # Calculate threat level using confirmed range when available
        immediate_threats = []
        close_contacts = []
        
        for contact in self.contacts.values():
            # Use confirmed range if available, otherwise estimate
            if contact.estimated_range:
                actual_range = contact.estimated_range
                range_source = "confirmed"
            else:
                actual_range = self._estimate_range(contact)
                range_source = "estimated"
            
            if actual_range < 1000:  # Immediate threat range
                immediate_threats.append(contact)
                self.log_thinking("THREAT_ANALYSIS", f"Immediate threat: {actual_range:.0f}m ({range_source})")
            elif actual_range < 2500:  # Close contact range
                close_contacts.append(contact)
                self.log_thinking("THREAT_ANALYSIS", f"Close contact: {actual_range:.0f}m ({range_source})")
            else:
                self.log_thinking("THREAT_ANALYSIS", f"Distant contact: {actual_range:.0f}m ({range_source})")
        
        # Priority 1: Critical battery (always surface recharge)
        if battery < 30:
            self.state = TacticalState.RECHARGE
            self.log_thinking("PRIORITY", f"Critical battery {battery:.1f}% - EMERGENCY SURFACE RECHARGE")
        
        # Priority 2: Immediate threats (interrupt anything if battery > 50%)
        elif immediate_threats and battery > 50:
            threat_range = self._estimate_range(immediate_threats[0])
            self.state = TacticalState.ATTACK
            self.log_thinking("PRIORITY", f"Immediate threat at {threat_range:.0f}m - ATTACK")
        
        # Priority 3: Hunt close contacts (with good battery for torpedo launch)
        elif close_contacts and battery > 60:
            contact_range = self._estimate_range(close_contacts[0])
            self.state = TacticalState.HUNT
            self.log_thinking("PRIORITY", f"Close contact at {contact_range:.0f}m - HUNT")
        
        # Priority 4: Patrol only with good battery (lowered threshold)
        elif battery >= 65:
            self.state = TacticalState.PATROL
            self.log_thinking("PRIORITY", f"Battery {battery:.1f}% good - PATROL")
        
        # Priority 5: Recharge to 100% when no contacts, or to 65% when contacts present
        elif not self.contacts or battery < 55 or (self.state == TacticalState.RECHARGE and battery < (100 if not self.contacts else 65)):
            self.state = TacticalState.RECHARGE
            target_battery = 100 if not self.contacts else 65
            self.log_thinking("PRIORITY", f"Battery {battery:.1f}% - RECHARGE TO {target_battery}% ({'no contacts' if not self.contacts else 'contacts present'})")
        
        # Priority 6: Conservative patrol (good battery but not excellent)
        else:
            self.state = TacticalState.PATROL
            self.log_thinking("PRIORITY", f"Battery {battery:.1f}% good - CONSERVATIVE PATROL")
        
        if old_state != self.state:
            self.log_thinking("STATE_CHANGE", f"{old_state.value} â†’ {self.state.value}")
    
    def _estimate_range(self, contact: Contact) -> float:
        # Use confirmed range from active echo if available
        if contact.estimated_range:
            return contact.estimated_range
        
        # Fallback to SNR-based estimation for passive-only contacts
        if contact.snr > 30:
            return 400
        elif contact.snr > 15:
            return 700
        elif contact.snr > 8:
            return 1200
        elif contact.snr > 5:
            return 2500
        else:
            return 4000  # Increased max estimate for distant contacts
    
    def _execute_patrol(self):
        if not self.last_snapshot.get('subs'):
            return
            
        sub = self.last_snapshot['subs'][0]
        battery = sub['battery']
        
        # Disable snorkel when not recharging
        if sub.get('is_snorkeling', False):
            self._api_call("POST", f"/snorkel/{self.sub_id}", {"on": False})
            self.log_thinking("DIVE", "Disabling snorkel for patrol")
        
        # Conservative patrol when battery moderate (40-80%), aggressive when high (80%+)
        if battery >= 80:
            # Full patrol mode - long range capability
            throttle = 0.25  
            rudder = 5      
            depth = 80
            ping_beam = 60
            ping_range = 5000  # Increased from 3000m for long-range detection
            mode = "Full patrol"
        else:
            # Conservative patrol - medium range
            throttle = 0.15   
            rudder = 2       
            depth = 150      
            ping_beam = 40   
            ping_range = 3500  # Increased from 2000m
            mode = "Conservative patrol"
        
        result = self._api_call("POST", f"/control/{self.sub_id}", {
            "throttle": throttle,
            "rudder_deg": rudder,
            "target_depth": depth,  # Always set target depth for diving
            "planes": -0.5  # NEGATIVE planes for diving
        })
        
        self.log_thinking("PATROLLING", f"{mode}, battery {battery:.1f}%, diving to {depth}m")
        
        # Ping logic: passive-triggered, extended search, or immediate response to strong contacts
        if self.contacts and battery > 40:  # Lowered from 50% to be more responsive
            # Get the strongest contact for targeted ping
            target_contact = max(self.contacts.values(), key=lambda c: c.snr)
            relative_bearing = math.degrees(target_contact.bearing - sub['heading'])
            
            # Use maximum range for strong contacts (SNR > 10)
            if target_contact.snr > 10:
                ping_range = 6000  # Maximum server range for strong contacts
                ping_beam = 20     # Narrow beam for precision
                self.log_thinking("STRONG_CONTACT", f"Strong contact SNR {target_contact.snr:.1f} - using max range ping")
            
            self._api_call("POST", f"/ping/{self.sub_id}", {
                "beamwidth_deg": ping_beam,
                "max_range": ping_range,
                "center_bearing_deg": relative_bearing
            })
            self.log_thinking("ACTIVE_PING", f"Investigating passive contact at {relative_bearing:.0f}Â° - beam {ping_beam}Â°, range {ping_range}m")
        elif not self.contacts and battery > 60 and time.time() % 45 < 2:  # More frequent search
            # Extended range search when no contacts detected (every 45 seconds)
            self._api_call("POST", f"/ping/{self.sub_id}", {
                "beamwidth_deg": 45,  # Wider beam for search
                "max_range": 6000,    # Maximum range
                "center_bearing_deg": 0
            })
            self.log_thinking("EXTENDED_PING", f"Extended search ping - no contacts detected - beam 45Â°, range 6000m")
        elif self.contacts:
            self.log_thinking("PASSIVE_ONLY", f"Tracking {len(self.contacts)} contacts passively - conserving battery")
    
    def _execute_hunt(self):
        if not self.contacts or not self.last_snapshot.get('subs'):
            return
        
        sub = self.last_snapshot['subs'][0]
        
        # Disable snorkel when hunting
        if sub.get('is_snorkeling', False):
            self._api_call("POST", f"/snorkel/{self.sub_id}", {"on": False})
            self.log_thinking("DIVE", "Disabling snorkel for hunt")
        
        target = max(self.contacts.values(), key=lambda c: c.snr)
        bearing_diff = target.bearing - sub['heading']
        
        # Normalize bearing difference
        while bearing_diff > math.pi:
            bearing_diff -= 2 * math.pi
        while bearing_diff < -math.pi:
            bearing_diff += 2 * math.pi
        
        rudder = max(-25, min(25, math.degrees(bearing_diff) * 2))
        
        result = self._api_call("POST", f"/control/{self.sub_id}", {
            "throttle": 0.5,  # Moderate speed to save battery
            "rudder_deg": rudder,
            "target_depth": 100,
            "planes": -0.3  # NEGATIVE planes for diving
        })
        
        self.log_thinking("HUNTING", f"Intercept to {math.degrees(target.bearing):.0f}Â°, battery {sub['battery']:.1f}%")
    
    def _execute_attack(self):
        if not self.contacts or not self.last_snapshot.get('subs'):
            return
        
        sub = self.last_snapshot['subs'][0]
        
        # Disable snorkel when attacking
        if sub.get('is_snorkeling', False):
            self._api_call("POST", f"/snorkel/{self.sub_id}", {"on": False})
            self.log_thinking("DIVE", "Disabling snorkel for attack")
        
        target = min(self.contacts.values(), key=lambda c: self._estimate_range(c))
        est_range = self._estimate_range(target)
        
        # Launch torpedo if in range and no existing torpedoes - with strict contact validation
        if (est_range < 2000 and sub['battery'] > 40 and  # Increased range from 900m to 2000m, lowered battery from 50%
            len(self.last_snapshot.get('torpedoes', [])) == 0 and
            len(self.contacts) > 0):  # Ensure we actually have contacts
            
            # Double-check contact is recent (within last 15 seconds - increased from 10)
            recent_contacts = [c for c in self.contacts.values() if time.time() - c.last_seen <= 15]
            if not recent_contacts:
                self.log_thinking("NO_RECENT_CONTACTS", "Skipping torpedo launch - no recent contacts")
                return
            
            # Check if we have active echo confirmation (precise range data)
            confirmed_contacts = [c for c in recent_contacts if c.estimated_range is not None]
            if not confirmed_contacts:
                self.log_thinking("NO_ECHO_CONFIRMATION", "Skipping torpedo launch - no active echo confirmation of range")
                return
            
            # Use confirmed contact with precise range
            target = min(confirmed_contacts, key=lambda c: c.estimated_range)
            confirmed_range = target.estimated_range
            
            if confirmed_range > 3000:  # Increased from 1200m to 3000m for long-range capability
                self.log_thinking("TARGET_TOO_FAR", f"Target at {confirmed_range:.0f}m beyond torpedo range (3000m) - not firing")
                return
            
            # Pre-launch targeting sequence
            target_bearing_deg = math.degrees(target.bearing)
            relative_bearing = target_bearing_deg - math.degrees(sub['heading'])
            
            # 1. Ping toward target for precise range/bearing
            self._api_call("POST", f"/ping/{self.sub_id}", {
                "beamwidth_deg": 20,  # Narrow beam for precision
                "max_range": min(confirmed_range + 500, 3000),
                "center_bearing_deg": relative_bearing
            })
            self.log_thinking("PRE_LAUNCH", f"Targeting ping at {relative_bearing:.0f}Â° relative, confirmed range {confirmed_range:.0f}m")
            
            # 2. Align submarine toward target
            bearing_diff = target.bearing - sub['heading']
            while bearing_diff > math.pi:
                bearing_diff -= 2 * math.pi
            while bearing_diff < -math.pi:
                bearing_diff += 2 * math.pi
            
            rudder = max(-30, min(30, math.degrees(bearing_diff) * 3))
            self._api_call("POST", f"/control/{self.sub_id}", {
                "rudder_deg": rudder,
                "throttle": 0.3  # Slow for precise aiming
            })
            self.log_thinking("PRE_LAUNCH", f"Aligning to target bearing {target_bearing_deg:.0f}Â°")
            
            # 3. Fire torpedo with confirmed range
            torpedo_range = min(confirmed_range + 200, 3000)  # Increased max from 1200m to 3000m
            result = self._api_call("POST", f"/launch_torpedo/{self.sub_id}", 
                {"range": torpedo_range})
            
            if result.get('ok'):
                torp_id = result['torpedo_id']
                # Initial guidance toward target
                self._api_call("POST", f"/set_torp_heading/{torp_id}", 
                    {"heading_deg": target_bearing_deg})
                # Set torpedo depth to target depth if known
                if target.estimated_range and target.estimated_range < 800:
                    # Assume target is at similar depth for close contacts
                    self._api_call("POST", f"/set_torp_depth/{torp_id}", 
                        {"depth": sub['depth']})
                # Enable torpedo active sonar for terminal homing
                result = self._api_call("POST", f"/torp_ping_toggle/{torp_id}")
                if result.get('active_sonar_enabled'):
                    self.log_thinking("FIRING", f"Torpedo {torp_id[:8]} launched at confirmed {confirmed_range:.0f}m target (range: {torpedo_range}m) with active sonar enabled")
                else:
                    # Toggle again if it was already on (to turn it on)
                    self._api_call("POST", f"/torp_ping_toggle/{torp_id}")
                    self.log_thinking("FIRING", f"Torpedo {torp_id[:8]} launched at confirmed {confirmed_range:.0f}m target (range: {torpedo_range}m) with active sonar enabled")
        
        # Manage existing torpedoes - homing is now handled by separate threads
        for torpedo in self.last_snapshot.get('torpedoes', []):
            torp_id = torpedo['id']
            # Ensure homing is active for all torpedoes
            if torp_id not in self.torpedo_homing_states:
                self.torpedo_homing_states[torp_id] = True
                threading.Thread(target=self._torpedo_homing_loop, args=(torp_id,)).start()
    
    def _torpedo_homing_loop(self, torp_id):
        """Continuous homing loop for a torpedo - mimics ui.html behavior"""
        while self.torpedo_homing_states.get(torp_id, False) and self.running:
            try:
                self._execute_torpedo_homing(torp_id)
                time.sleep(2)  # Homing update every 2 seconds like ui.html
            except Exception as e:
                self.log_thinking("HOMING_ERROR", f"Torpedo {torp_id[:8]} homing failed: {e}")
                break
        
        # Clean up when homing stops
        if torp_id in self.torpedo_homing_states:
            del self.torpedo_homing_states[torp_id]
    
    def _execute_torpedo_homing(self, torp_id):
        """Execute homing logic for a torpedo - mimics ui.html startTorpedoHoming"""
        current_time = time.time()
        target_bearing = None
        target_depth = None
        
        # Try active ping contact first (more accurate) - within 10 seconds
        if torp_id in self.torpedo_ping_contacts:
            ping_data = self.torpedo_ping_contacts[torp_id]
            if current_time - ping_data['time'] <= 10:
                contacts = ping_data['contacts']
                if contacts:
                    # Find closest contact
                    closest = min(contacts, key=lambda c: c.get('range', 9999))
                    
                    # Calculate target bearing relative to torpedo position
                    torpedo = next((t for t in self.last_snapshot.get('torpedoes', []) if t['id'] == torp_id), None)
                    if torpedo and closest:
                        # Convert relative bearing to absolute bearing
                        target_bearing = closest.get('bearing', 0)
                        target_depth = closest.get('depth')
                        
                        self.log_thinking("HOMING_PING", f"Torpedo {torp_id[:8]} using ping contact at {closest.get('range', 0):.0f}m")
        
        # Fall back to passive contact if no recent ping data
        if target_bearing is None and torp_id in self.torpedo_passive_contacts:
            passive_data = self.torpedo_passive_contacts[torp_id]
            if current_time - passive_data['time'] <= 10:
                target_bearing = passive_data['bearing']
                # No depth info from passive, maintain current depth
                torpedo = next((t for t in self.last_snapshot.get('torpedoes', []) if t['id'] == torp_id), None)
                target_depth = torpedo['depth'] if torpedo else None
                
                self.log_thinking("HOMING_PASSIVE", f"Torpedo {torp_id[:8]} using passive contact")
        
        # Execute homing commands if we have a target
        if target_bearing is not None:
            target_heading_deg = math.degrees(target_bearing)
            
            # Set torpedo heading
            self._api_call("POST", f"/set_torp_heading/{torp_id}", {
                "heading_deg": target_heading_deg,
                "dt": 1.0
            })
            
            # Set torpedo depth if available
            if target_depth is not None:
                self._api_call("POST", f"/set_torp_depth/{torp_id}", {
                    "depth": target_depth
                })
            
            # Set high speed for terminal attack
            self._api_call("POST", f"/set_torp_speed/{torp_id}", {"speed": 22})
            
            self.log_thinking("HOMING_UPDATE", f"Torpedo {torp_id[:8]} heading {target_heading_deg:.0f}Â°")
        else:
            # No torpedo sonar data - use submarine's contact data for guidance
            if self.contacts:
                # Find the strongest/closest contact from submarine
                best_contact = max(self.contacts.values(), key=lambda c: c.snr)
                if current_time - best_contact.last_seen <= 15:  # Recent contact
                    target_heading_deg = math.degrees(best_contact.bearing)
                    
                    self._api_call("POST", f"/set_torp_heading/{torp_id}", {
                        "heading_deg": target_heading_deg,
                        "dt": 1.0
                    })
                    
                    self._api_call("POST", f"/set_torp_speed/{torp_id}", {"speed": 20})
                    
                    self.log_thinking("HOMING_SUBMARINE", f"Torpedo {torp_id[:8]} guided by submarine contact at {target_heading_deg:.0f}Â°")
                else:
                    self.log_thinking("HOMING_SEARCH", f"Torpedo {torp_id[:8]} searching with active sonar")
            else:
                self.log_thinking("HOMING_SEARCH", f"Torpedo {torp_id[:8]} searching with active sonar")
        
        # Attack maneuvers - move submarine toward target
        if not self.contacts or not self.last_snapshot.get('subs'):
            return
            
        target = min(self.contacts.values(), key=lambda c: self._estimate_range(c))
        sub = self.last_snapshot['subs'][0]
        bearing_diff = target.bearing - sub['heading']
        while bearing_diff > math.pi:
            bearing_diff -= 2 * math.pi
        while bearing_diff < -math.pi:
            bearing_diff += 2 * math.pi
        
        rudder = max(-30, min(30, math.degrees(bearing_diff) * 2))
        
        self._api_call("POST", f"/control/{self.sub_id}", {
            "throttle": 0.6,  # Aggressive but not max
            "rudder_deg": rudder,
            "target_depth": 120
        })
        
        self.log_thinking("ATTACKING", f"Combat maneuvers, battery {sub['battery']:.1f}%")
    
    def _execute_recharge(self):
        if not self.last_snapshot.get('subs'):
            return
            
        sub = self.last_snapshot['subs'][0]
        
        # Emergency blow if too deep OR if sinking while trying to surface
        if (sub['depth'] > 30 and sub['battery'] < 50) or sub['depth'] > 100:
            self._api_call("POST", f"/emergency_blow/{self.sub_id}")
            self.log_thinking("EMERGENCY_BLOW", f"Emergency blow from {sub['depth']:.0f}m depth")
        
        # Tactical recharge - approach contacts while charging
        rudder_deg = 0
        if self.contacts:
            # Find strongest contact and approach it
            target = max(self.contacts.values(), key=lambda c: c.snr)
            bearing_diff = target.bearing - sub['heading']
            
            # Normalize bearing difference
            while bearing_diff > math.pi:
                bearing_diff -= 2 * math.pi
            while bearing_diff < -math.pi:
                bearing_diff += 2 * math.pi
            
            # Gentle turn toward target while recharging
            rudder_deg = max(-15, min(15, math.degrees(bearing_diff) * 1.5))
            self.log_thinking("TACTICAL_RECHARGE", f"Approaching target at {math.degrees(target.bearing):.0f}Â° while recharging")
        
        # Surface and maintain minimum speed for depth control
        self._api_call("POST", f"/control/{self.sub_id}", {
            "throttle": 0.2,    # Minimum throttle to maintain 2+ m/s (avoid sinking)
            "target_depth": 0,  # Surface target
            "rudder_deg": rudder_deg,  # Turn toward target or stay straight
            "planes": 1.0       # Surface planes
        })
        
        self.log_thinking("RECHARGING", f"Tactical recharge - battery {sub['battery']:.1f}%, depth {sub['depth']:.0f}m, speed {sub['speed']:.1f}m/s")
        
        # Enable snorkel when shallow enough (speed will be ~2+ m/s for depth control)
        if sub['depth'] <= 15:
            self._api_call("POST", f"/snorkel/{self.sub_id}", {"on": True})
            self.log_thinking("SNORKELING", f"Recharging at {sub['depth']:.0f}m depth, {sub['speed']:.1f}m/s")
        else:
            self.log_thinking("SURFACING", f"Rising to snorkel depth - current {sub['depth']:.0f}m")
    
    def run(self):
        self.running = True
        stream_thread = None
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        self.log_thinking("START", "Beginning smart battery management mission with auto-recovery")
        
        while self.running:
            try:
                # Start/restart stream thread if needed
                if stream_thread is None or not stream_thread.is_alive():
                    if stream_thread is not None:
                        self.log_thinking("STREAM_RESTART", "Restarting event stream thread")
                    stream_thread = threading.Thread(target=self._stream_events)
                    stream_thread.daemon = True
                    stream_thread.start()
                
                # Main decision loop with bulletproof error handling
                try:
                    self._analyze_situation()
                    
                    if self.state == TacticalState.PATROL:
                        self._execute_patrol()
                    elif self.state == TacticalState.HUNT:
                        self._execute_hunt()
                    elif self.state == TacticalState.ATTACK:
                        self._execute_attack()
                    elif self.state == TacticalState.RECHARGE:
                        self._execute_recharge()
                    
                    # Reset error counter on successful execution
                    consecutive_errors = 0
                        
                except BrokenPipeError:
                    self.log_thinking("PIPE_RECOVERED", "Broken pipe in main loop - continuing")
                    consecutive_errors += 1
                except ConnectionError:
                    self.log_thinking("CONNECTION_RECOVERED", "Connection error in main loop - continuing")
                    consecutive_errors += 1
                except requests.exceptions.RequestException as e:
                    self.log_thinking("REQUEST_RECOVERED", f"Request error in main loop: {e} - continuing")
                    consecutive_errors += 1
                except Exception as e:
                    self.log_thinking("EXECUTION_RECOVERED", f"Execution error in main loop: {e} - continuing")
                    consecutive_errors += 1
                
                # If too many consecutive errors, take a longer break
                if consecutive_errors >= max_consecutive_errors:
                    self.log_thinking("ERROR_COOLDOWN", f"Too many errors ({consecutive_errors}) - cooling down for 10s")
                    time.sleep(10)
                    consecutive_errors = 0
                else:
                    time.sleep(2)
                
            except KeyboardInterrupt:
                self.log_thinking("END", "Mission terminated by user")
                break
            except SystemExit:
                self.log_thinking("END", "System exit requested")
                break
            except Exception as e:
                self.log_thinking("CRITICAL_RECOVERED", f"Critical error recovered: {e} - continuing mission")
                time.sleep(5)
        
        self.running = False
        # Stop all torpedo homing
        self.torpedo_homing_states.clear()
        if stream_thread and stream_thread.is_alive():
            stream_thread.join(timeout=2)
        
        self.log_thinking("SHUTDOWN", "Mission ended - all systems shutdown")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python smart_battery_submarine.py <username> <password> [sub_id]")
        sys.exit(1)
    
    username = sys.argv[1]
    password = sys.argv[2]
    sub_id = sys.argv[3] if len(sys.argv) > 3 else None
    
    sub = SmartBatterySubmarine(username=username, password=password, sub_id=sub_id)
    sub.run()
