from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from tinydb import TinyDB, Query
from datetime import datetime, timedelta
import threading
import pytz

app = Flask(__name__)
app.secret_key = 'secret_key'
socketio = SocketIO(app, async_mode='threading')


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
supervisors = {"admin", "supervisor1"}
undo_stack = {}

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        role = request.form.get("role")
        group = request.form.get("group") or "default"

        session["username"] = username
        session["role"] = role
        session["group"] = group

        now = sg_now()
        end = calculate_end_time(now, WBGT_ZONES["green"]["work"])
        users[username] = {
            "status": "idle",
            "zone": "",
            "start_time": now.strftime("%H:%M:%S"),
            "end_time": end.strftime("%H:%M:%S"),
            "alarm_triggered": False,
            "location": None,
            "role": role,
            "username": username
        }

        user_table.upsert({
            "username": username,
            "role": role,
            "group": group
        }, Query().username == username)

        return redirect(f"/dashboard/{username}")
    return render_template("login.html")

@app.route("/")
def home():
    if "username" in session:
        return redirect(f"/dashboard/{session['username']}")
    return redirect("/login")

def sg_now():
    return datetime.now(SG_TZ)

def calculate_end_time(start, work_minutes):
    return start + timedelta(minutes=work_minutes)

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

@app.route("/dashboard/<username>")
def dashboard(username):
    if "username" not in session or session["username"] != username:
        return redirect("/login")

    if username not in users:
        now = sg_now()
        end = calculate_end_time(now, WBGT_ZONES["green"]["work"])
        users[username] = {
            "status": "idle",
            "zone": "",
            "start_time": now.strftime("%H:%M:%S"),
            "end_time": end.strftime("%H:%M:%S"),
            "alarm_triggered": False,
            "location": None,
            "role": session.get("role", "user"),
            "username": username
        }
    return render_template("dashboard.html", username=username, user=users[username], zones=WBGT_ZONES)

@app.route("/submit_zone", methods=["POST"])
def submit_zone():
    data = request.json
    username = data.get("username")
    zone = data.get("zone")
    role = data.get("role", "user")
    lat = data.get("lat")
    lon = data.get("lon")
    location = f"{lat}, {lon}" if lat and lon else None
    now = sg_now()

    if zone == "cutoff" and role not in ["admin", "supervisor"]:
        return jsonify({"error": "Unauthorized cutoff."}), 403

    if zone not in WBGT_ZONES:
        return jsonify({"error": "Invalid zone"}), 400

    work_minutes = WBGT_ZONES[zone]["work"]
    user = users.get(username)

    store_state(username)

    if user and user["status"] == "working":
        prev_end = datetime.strptime(user["end_time"], "%H:%M:%S")
        new_end = calculate_end_time(now, work_minutes)
        end_time = min(prev_end, new_end)
    else:
        end_time = calculate_end_time(now, work_minutes)

    users[username] = {
        "status": "working",
        "zone": zone,
        "start_time": now.strftime("%H:%M:%S"),
        "end_time": end_time.strftime("%H:%M:%S"),
        "alarm_triggered": False,
        "location": location,
        "role": role,
        "username": username
    }

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

@app.route("/undo", methods=["POST"])
def undo():
    data = request.json
    username = data.get("username")
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

@app.route("/admin/groups", methods=["GET", "POST"])
def admin_groups():
    if session.get("role") != "admin":
        return redirect("/")

    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            pass  # group is virtual
        elif action == "assign":
            username = request.form.get("username")
            group = request.form.get("group")
            user_table.update({"group": group}, Query().username == username)

    all_users = user_table.all()
    grouped_users = {}
    for user in all_users:
        grp = user.get("group", "ungrouped")
        if grp not in grouped_users:
            grouped_users[grp] = []
        grouped_users[grp].append(user)

    return render_template("admin_groups.html", grouped_users=grouped_users)

@app.route("/supervisor/dashboard")
def supervisor_dashboard():
    if session.get("role") not in ["supervisor", "admin"]:
        return redirect("/")

    group = session.get("group")
    all_users = user_table.all()
    group_users = []

    for user in all_users:
        if user.get("group") == group:
            live = users.get(user["username"], {})
            group_users.append({
                "username": user["username"],
                "zone": live.get("zone", "--"),
                "status": live.get("status", "idle"),
                "start_time": live.get("start_time", "--:--"),
                "end_time": live.get("end_time", "--:--")
            })

    return render_template("supervisor_dashboard.html", group_users=group_users)

if __name__ == "__main__":
    socketio.run(app, debug=True)
