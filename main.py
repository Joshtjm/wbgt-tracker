import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from tinydb import TinyDB, Query
from datetime import datetime, timedelta
import threading
import pytz

app = Flask(__name__)
app.secret_key = 'secret_key'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
socketio = SocketIO(app, async_mode='eventlet')

SG_TZ = pytz.timezone("Asia/Singapore")
db = TinyDB("data.json")
user_table = db.table("users")
log_table = db.table("history")

WBGT_ZONES = {
    "white": {"min": 0.0, "max": 29.9, "work": 60, "rest": 15},
    "green": {"min": 30.0, "max": 30.9, "work": 45, "rest": 15},
    "yellow": {"min": 31.0, "max": 31.9, "work": 30, "rest": 15},
    "red": {"min": 32.0, "max": 32.9, "work": 30, "rest": 30},
    "black": {"min": 33.0, "max": 34.9, "work": 15, "rest": 30},
    "cutoff": {"min": 35.0, "max": 100.0, "work": 0, "rest": 60}
}

users = {}
undo_stack = {}
overwrite_flags = {}

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
            print(f"Alarm: {username} has not started rest.")
            threading.Timer(10, alarm_loop).start()
    threading.Timer(180, alarm_loop).start()

def store_state(username):
    if username in users:
        if username not in undo_stack:
            undo_stack[username] = []
        undo_stack[username].append(users[username].copy())

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
    if not data or "username" not in data:
        return jsonify({"error": "Missing username in request."}), 400
    username = data["username"]
    overwrite_flags[username] = True
    return jsonify({"message": "Overwrite confirmed."})

@app.route("/submit_zone", methods=["POST"])
def submit_zone():
    data = request.get_json(silent=True)
    if not data or "username" not in data or "zone" not in data:
        return jsonify({"error": "Missing required data."}), 400

    username = data["username"]
    zone = data["zone"]
    role = data.get("role", "user")
    lat = data.get("lat")
    lon = data.get("lon")
    location = f"{lat}, {lon}" if lat and lon else None
    now = sg_now()

    if username not in users:
        return jsonify({"error": "Session expired or user not found. Please log in again."}), 403

    if zone not in WBGT_ZONES:
        return jsonify({"error": "Invalid zone"}), 400

    group = users[username]["group"]
    work_minutes = WBGT_ZONES[zone]["work"]
    user = users.get(username)

    if zone == "cutoff":
        if role not in ["admin", "supervisor"]:
            return jsonify({"error": "Unauthorized cutoff."}), 403
        for u, info in users.items():
            if info.get("group") == group:
                users[u]["status"] = "resting"
                users[u]["zone"] = "cutoff"
                users[u]["start_time"] = now.strftime("%H:%M:%S")
                users[u]["end_time"] = calculate_end_time(now, 30).strftime("%H:%M:%S")
                users[u]["cutoff_active"] = True
                users[u]["cutoff_end"] = calculate_end_time(now, 30).strftime("%H:%M:%S")
                socketio.emit('status_update', {"user": u, "zone": "cutoff"})
        return jsonify({"message": f"CUTOFF enforced for group {group}. All users resting."})

    store_state(username)

    prev_end = parse_time_string(user.get("end_time")) if user else None
    new_end = calculate_end_time(now, work_minutes)

    if user["status"] == "working" and not overwrite_flags.get(username, False):
        return jsonify({"error": "Overwrite not confirmed. Press 📝 before ▶️."})

    if user["status"] == "working" and prev_end:
        end_time = min(prev_end, new_end)
        start_time = user["start_time"]
    else:
        end_time = new_end
        start_time = now.strftime("%H:%M:%S")

    users[username].update({
        "status": "working",
        "zone": zone,
        "start_time": start_time,
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
        if users[username]["status"] == "working":
            users[username]["status"] = "awaiting_rest"
            trigger_alarm(username)

    delay = (end_time - now).total_seconds()
    threading.Timer(delay, prompt_rest).start()

    return jsonify({"message": f"{username} started {zone.upper()} zone until {end_time.strftime('%H:%M:%S')} SG."})

@app.route("/deactivate_cutoff", methods=["POST"])
def deactivate_cutoff():
    data = request.get_json(silent=True)
    if not data or "username" not in data or "role" not in data:
        return jsonify({"error": "Missing data to deactivate cutoff."}), 400

    username = data["username"]
    role = data["role"]
    group = users[username]["group"]
    if role not in ["admin", "supervisor"]:
        return jsonify({"error": "Not authorized."}), 403

    for u, info in users.items():
        if info["group"] == group and info["cutoff_active"]:
            users[u]["cutoff_active"] = False
            users[u]["status"] = "idle"
            users[u]["zone"] = ""
            users[u]["start_time"] = sg_now().strftime("%H:%M:%S")
            users[u]["end_time"] = sg_now().strftime("%H:%M:%S")
            socketio.emit('status_update', {"user": u, "zone": "idle"})
    return jsonify({"message": f"Cutoff deactivated for group {group}."})

@app.route("/undo", methods=["POST"])
def undo():
    data = request.get_json(silent=True)
    if not data or "username" not in data:
        return jsonify({"error": "Missing username in undo request."}), 400

    username = data["username"]
    if username in undo_stack and undo_stack[username]:
        users[username] = undo_stack[username].pop()
        user_table.upsert(users[username], Query().username == username)
        return jsonify({"message": f"{username}'s last WBGT zone has been reverted."})
    return jsonify({"error": "No undo available."}), 400

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

if __name__ == "__main__":
    socketio.run(app, debug=True)
