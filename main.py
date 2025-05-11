
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)
app.secret_key = 'secret_key'
socketio = SocketIO(app, async_mode='eventlet')

SG_TZ = pytz.timezone("Asia/Singapore")

WBGT_ZONES = {
    "white": {"work": 60, "rest": 15},
    "green": {"work": 45, "rest": 15},
    "yellow": {"work": 30, "rest": 15},
    "red": {"work": 30, "rest": 30},
    "black": {"work": 15, "rest": 30}
}

users = {}
roles = {
    "safety_officer": ["john", "mary"],  # Example safety officers
    "supervisor": ["alex", "sarah"],     # Example supervisors
}

def sg_now():
    return datetime.now(SG_TZ)

def calculate_end(start, minutes):
    return start + timedelta(minutes=minutes)

def get_user_role(username):
    if username in roles["safety_officer"]:
        return "safety_officer"
    elif username in roles["supervisor"]:
        return "supervisor"
    return "user"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form.get("username")
        role = get_user_role(username)
        
        if role in ["safety_officer", "supervisor"]:
            return redirect(f"/monitor/{username}")
            
        zone = request.form.get("zone")
        now = sg_now()

        if username not in users:
            users[username] = {"status": "idle"}

        user = users[username]
        work_duration = WBGT_ZONES[zone]["work"]
        proposed_end = calculate_end(now, work_duration)

        if user["status"] == "working":
            current_end_str = user["end_time"]
            current_end_naive = datetime.strptime(current_end_str, "%H:%M:%S")
            current_end = now.replace(hour=current_end_naive.hour, minute=current_end_naive.minute, second=current_end_naive.second)
            proposed_end = min(current_end, proposed_end)

        users[username] = {
            "status": "working",
            "zone": zone,
            "start_time": now.strftime("%H:%M:%S"),
            "end_time": proposed_end.strftime("%H:%M:%S"),
            "role": role
        }
        return redirect(f"/dashboard/{username}")
    return render_template("index.html", zones=WBGT_ZONES)

@app.route("/dashboard/<username>")
def dashboard(username):
    if username not in users:
        return redirect("/")
    return render_template("dashboard.html", user=users[username], username=username, zones=WBGT_ZONES)

@app.route("/monitor/<username>")
def monitor(username):
    role = get_user_role(username)
    if role not in ["safety_officer", "supervisor"]:
        return redirect("/")
    return render_template("monitor.html", users=users, username=username, role=role)

if __name__ == "__main__":
    socketio.run(app, debug=True)
