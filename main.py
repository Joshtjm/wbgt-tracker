
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import pytz
import json

app = Flask(__name__)
app.secret_key = 'secret_key'
socketio = SocketIO(app, async_mode='eventlet')

SG_TZ = pytz.timezone("Asia/Singapore")

WBGT_ZONES = {
    "white": {"work": 60, "rest": 15},
    "green": {"work": 45, "rest": 15},
    "yellow": {"work": 30, "rest": 15},
    "red": {"work": 30, "rest": 30},
    "black": {"work": 15, "rest": 30},
    "cut-off": {"work": 0, "rest": 30}
}

ROLES = ["Trainer", "Safety Officer", "Supervisor"]
users = {}
locations = {}
system_status = {"cut_off": False, "cut_off_end_time": None}

def is_authority(role):
    return role in ["Safety Officer", "Supervisor"]

def sg_now():
    return datetime.now(SG_TZ)

def calculate_end(start, minutes):
    return start + timedelta(minutes=minutes)

def save_locations():
    with open('locations.json', 'w') as f:
        json.dump(locations, f)

def load_locations():
    try:
        with open('locations.json', 'r') as f:
            return json.load(f)
    except:
        return {}

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form.get("username")
        role = request.form.get("role")
        
        if role in ["Safety Officer", "Supervisor"]:
            users[username] = {"role": role, "status": "monitoring"}
            return redirect(f"/monitor/{username}")
            
        users[username] = {"role": role, "status": "idle"}
        return redirect(f"/dashboard/{username}")
        
    return render_template("index.html", roles=ROLES)

@app.route("/dashboard/<username>")
def dashboard(username):
    if username not in users:
        return redirect("/")
    return render_template("dashboard.html", user=users[username], username=username, zones=WBGT_ZONES)

@app.route("/monitor/<username>")
def monitor(username):
    if username not in users or users[username]["role"] not in ["Safety Officer", "Supervisor"]:
        return redirect("/")
    return render_template("monitor.html", users=users, username=username, role=users[username]["role"], zones=WBGT_ZONES)

@app.route("/toggle_cut_off", methods=["POST"])
def toggle_cut_off():
    username = request.form.get("username")
    if username not in users or not is_authority(users[username]["role"]):
        return jsonify({"error": "Unauthorized"}), 401
    
    now = sg_now()
    if system_status["cut_off"]:
        system_status["cut_off"] = False
        system_status["cut_off_end_time"] = (now + timedelta(minutes=30)).strftime("%H:%M:%S")
    else:
        system_status["cut_off"] = True
        system_status["cut_off_end_time"] = None
        for user_id, user_data in users.items():
            if user_data["role"] == "Trainer":
                user_data["status"] = "resting"
                user_data["zone"] = "cut-off"
                user_data["start_time"] = now.strftime("%H:%M:%S")
                user_data["end_time"] = (now + timedelta(minutes=30)).strftime("%H:%M:%S")
    
    return jsonify({"success": True})

@app.route("/reset_logs", methods=["POST"])
def reset_logs():
    username = request.form.get("username")
    if username not in users or not is_authority(users[username]["role"]):
        return jsonify({"error": "Unauthorized"}), 401
    
    to_remove = []
    for user_id, user_data in users.items():
        if user_data["role"] == "Trainer":
            to_remove.append(user_id)
    
    for user_id in to_remove:
        del users[user_id]
    
    return jsonify({"success": True})

@app.route("/set_zone", methods=["POST"])
def set_zone():
    username = request.form.get("username")
    target_user = request.form.get("target_user", username)
    zone = request.form.get("zone")
    now = sg_now()
    
    if system_status["cut_off"] and not is_authority(users[username]["role"]):
        return jsonify({"error": "System is in cut-off mode"}), 403
        
    if system_status["cut_off_end_time"]:
        cut_off_end = datetime.strptime(system_status["cut_off_end_time"], "%H:%M:%S")
        cut_off_end = now.replace(hour=cut_off_end.hour, minute=cut_off_end.minute, second=cut_off_end.second)
        if now < cut_off_end and not is_authority(users[username]["role"]):
            return jsonify({"error": "Mandatory rest period is still active"}), 403
    
    if username not in users:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_role = users[username]["role"]
    if target_user != username and user_role not in ["Safety Officer", "Supervisor"]:
        return jsonify({"error": "Unauthorized"}), 401

    work_duration = WBGT_ZONES[zone]["work"]
    proposed_end = calculate_end(now, work_duration)

    if target_user in users and users[target_user].get("status") == "working":
        current_end_str = users[target_user]["end_time"]
        current_end_naive = datetime.strptime(current_end_str, "%H:%M:%S")
        current_end = now.replace(hour=current_end_naive.hour, minute=current_end_naive.minute, second=current_end_naive.second)
        proposed_end = min(current_end, proposed_end)

    users[target_user].update({
        "status": "working",
        "zone": zone,
        "start_time": now.strftime("%H:%M:%S"),
        "end_time": proposed_end.strftime("%H:%M:%S"),
        "location": request.form.get("location", None)
    })
    
    return jsonify({"success": True})

@app.route("/save_location", methods=["POST"])
def save_location():
    data = request.get_json()
    name = data.get("name")
    lat = data.get("lat")
    lng = data.get("lng")
    
    if name and lat and lng:
        locations[name] = {"lat": lat, "lng": lng}
        save_locations()
        return jsonify({"success": True})
    return jsonify({"error": "Invalid data"}), 400

@app.route("/get_locations")
def get_locations():
    return jsonify(load_locations())

if __name__ == "__main__":
    locations = load_locations()
    socketio.run(app, debug=True)
