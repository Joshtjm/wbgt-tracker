import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from tinydb import TinyDB, Query
from datetime import datetime, timedelta
import pytz

# === Flask Setup ===
app = Flask(__name__)
app.secret_key = 'secret_key'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
socketio = SocketIO(app, async_mode='eventlet')

# === Global Constants ===
SG_TZ = pytz.timezone("Asia/Singapore")
db = TinyDB("data.json")
user_table = db.table("users")
log_table = db.table("history")

WBGT_ZONES = {
    "white":  {"min": 0.0, "max": 29.9, "work": 60, "rest": 15},
    "green":  {"min": 30.0, "max": 30.9, "work": 45, "rest": 15},
    "yellow": {"min": 31.0, "max": 31.9, "work": 30, "rest": 15},
    "red":    {"min": 32.0, "max": 32.9, "work": 30, "rest": 30},
    "black":  {"min": 33.0, "max": 34.9, "work": 15, "rest": 30},
    "cutoff": {"min": 35.0, "max": 100.0, "work": 0,  "rest": 60}
}

users = {}
undo_stack = {}
overwrite_flags = {}

# === Helper Functions ===
def sg_now():
    return datetime.now(SG_TZ)

def calculate_end_time(start, minutes):
    return start + timedelta(minutes=minutes)

def parse_time_string(time_str):
    try:
        naive = datetime.strptime(time_str, "%H:%M:%S")
        return SG_TZ.localize(naive)
    except Exception:
        return None

def trigger_alarm(username):
    def alarm_loop():
        if username in users and users[username]["status"] == "awaiting_rest":
            print(f"[ALARM] {username} has not started rest.")
            socketio.emit("status_update", {"user": username, "zone": "awaiting_rest"})
            eventlet.spawn_after(10, alarm_loop)
    eventlet.spawn_after(180, alarm_loop)

def store_state(username):
    if username in users:
        if username not in undo_stack:
            undo_stack[username] = []
        undo_stack[username].append(users[username].copy())

# === Routes ===
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        role = request.form.get("role")
        group = request.form.get("group") or "default"

        session["username"] = username
        session["role"] = role
        session["group"] = group
        session.permanent = True

        user_table.upsert({
            "username": username,
            "role": role,
            "group": group
        }, Query().username == username)

        now = sg_now()
        users[username] = {
            "status": "idle",
            "zone": "",
            "start_time": now.strftime("%H:%M:%S"),
            "end_time": now.strftime("%H:%M:%S"),
            "alarm_triggered": False,
            "location": None,
            "role": role,
            "username": username,
            "group": group,
            "cutoff_active": False,
            "cutoff_end": None
        }

        return redirect(f"/dashboard/{username}")
    return render_template("login.html")

@app.route("/")
def home():
    return redirect("/login")

@app.route("/dashboard/<username>")
def dashboard(username):
    if username not in users:
        return redirect("/login")
    return render_template("dashboard.html", username=username, user=users[username], zones=WBGT_ZONES)

@app.route("/confirm_overwrite", methods=["POST"])
def confirm_overwrite():
    data = request.get_json(silent=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "Missing username"}), 400
    overwrite_flags[username] = True
    return jsonify({"message": "Overwrite confirmed."})

@app.route("/submit_zone", methods=["POST"])
def submit_zone():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    username = data.get("username")
    zone = data.get("zone")
    role = data.get("role", "user")
    lat = data.get("lat")
    lon = data.get("lon")
    location = f"{lat}, {lon}" if lat and lon else None
    now = sg_now()

    if username not in users:
        return jsonify({"error": "Session expired. Please log in again."}), 403

    if zone not in WBGT_ZONES:
        return jsonify({"error": "Invalid WBGT zone"}), 400

    user = users[username]
    group = user["group"]
    work_minutes = WBGT_ZONES[zone]["work"]

    # Handle Cutoff
    if zone == "cutoff":
        if role not in ["admin", "supervisor"]:
            return jsonify({"error": "Unauthorized cutoff"}), 403
        for u, info in users.items():
            if info.get("group") == group:
                users[u].update({
                    "status": "resting",
                    "zone": "cutoff",
                    "start_time": now.strftime("%H:%M:%S"),
                    "end_time": calculate_end_time(now, 30).strftime("%H:%M:%S"),
                    "cutoff_active": True,
                    "cutoff_end": calculate_end_time(now, 30).strftime("%H:%M:%S")
                })
                socketio.emit('status_update', {"user": u, "zone": "cutoff"})
        return jsonify({"message": f"Cutoff enforced for group {group}."})

    store_state(username)

    # If already working and overwrite not confirmed
    if user["status"] == "working" and not overwrite_flags.get(username, False):
        return jsonify({"error": "Overwrite not confirmed. Press 📝 before ▶️."})

    start_time = now
    end_time = calculate_end_time(now, work_minutes)

    users[username].update({
        "status": "working",
        "zone": zone,
        "start_time": start_time.strftime("%H:%M:%S"),
        "end_time": end_time.strftime("%H:%M:%S"),
        "alarm_triggered": False,
        "location": location,
        "role": role,
        "cutoff_active": False,
        "cutoff_end": None
    })

    overwrite_flags[username] = False

    user_table.upsert(users[username], Query().username == username)
    log_table.insert({
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "username": username,
        "zone": zone,
        "duration": work_minutes,
        "status": "work"
    })

    socketio.emit('status_update', {"user": username, "zone": zone})

    def prompt_rest():
        if users.get(username, {}).get("status") == "working":
            users[username]["status"] = "awaiting_rest"
            trigger_alarm(username)

    eventlet.spawn_after((end_time - now).total_seconds(), prompt_rest)

    return jsonify({"message": f"{username} started {zone.upper()} zone until {end_time.strftime('%H:%M:%S')} SG."})

@app.route("/deactivate_cutoff", methods=["POST"])
def deactivate_cutoff():
    data = request.get_json(silent=True)
    username = data.get("username")
    role = data.get("role")

    if not username or not role:
        return jsonify({"error": "Missing data"}), 400
    if role not in ["admin", "supervisor"]:
        return jsonify({"error": "Not authorized"}), 403

    group = users[username]["group"]
    for u, info in users.items():
        if info["group"] == group and info["cutoff_active"]:
            users[u].update({
                "cutoff_active": False,
                "status": "idle",
                "zone": "",
                "start_time": sg_now().strftime("%H:%M:%S"),
                "end_time": sg_now().strftime("%H:%M:%S")
            })
            socketio.emit('status_update', {"user": u, "zone": "idle"})
    return jsonify({"message": f"Cutoff deactivated for group {group}."})

@app.route("/undo", methods=["POST"])
def undo():
    data = request.get_json(silent=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "Missing username"}), 400
    if username in undo_stack and undo_stack[username]:
        users[username] = undo_stack[username].pop()
        user_table.upsert(users[username], Query().username == username)
        return jsonify({"message": "Last zone reverted."})
    return jsonify({"error": "No undo available"}), 400

# === Views ===
@app.route("/log")
def log():
    return render_template("zones.html", users=users)

@app.route("/readings")
def readings():
    return render_template("readings.html", users=users)

@app.route("/history")
def history_view():
    return render_template("history.html", history=log_table.all())

@app.route("/locations")
def locations():
    return render_template("locations.html")

@app.route("/settings")
def settings():
    return "<h2>Settings (Coming Soon)</h2>"

# === Run App ===
if __name__ == "__main__":
    socketio.run(app, debug=True)
