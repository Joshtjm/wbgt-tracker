import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import pytz
import json

app = Flask(__name__)
app.secret_key = 'secret_key'
socketio = SocketIO(app, async_mode='eventlet', ping_timeout=60, ping_interval=25)

SG_TZ = pytz.timezone("Asia/Singapore")

WBGT_ZONES = {
    "white": {"work": 60, "rest": 15},
    "green": {"work": 45, "rest": 15},
    "yellow": {"work": 30, "rest": 15},
    "red": {"work": 30, "rest": 30},
    "black": {"work": 15, "rest": 30},
    "cut-off": {"work": 0, "rest": 30}
}

ROLES = ["Trainer", "Conducting Body"]
users = {}
locations = {}
history_log = []
system_status = {"cut_off": False, "cut_off_end_time": None}

def log_activity(username, action, zone=None):
    timestamp = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    history_log.append({
        "timestamp": timestamp,
        "username": username,
        "action": action,
        "zone": zone
    })

def is_authority(role):
    return role == "Conducting Body"

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

# Modify the index route to include password verification:
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        username = request.form.get("username")
        role = request.form.get("role")
        password = request.form.get("password", "")

        if role == "Conducting Body" and password != "password":
            return render_template("index.html", roles=ROLES, error="Invalid password for Conducting Body")

        if role == "Conducting Body":
            users[username] = {"role": role, "status": "monitoring"}
            return redirect(f"/monitor/{username}")

        users[username] = {"role": role, "status": "idle"}
        return redirect(f"/dashboard/{username}")

    return render_template("index.html", roles=ROLES)

@app.route("/dashboard/<username>")
def dashboard(username):
    if username not in users:
        return redirect("/")
    return render_template("dashboard.html", user=users[username], username=username, zones=WBGT_ZONES, system_status=system_status)

@app.route("/monitor/<username>")
def monitor(username):
    if username not in users or users[username]["role"] != "Conducting Body":
        return redirect("/")
    return render_template("monitor.html", users=users, username=username, role=users[username]["role"], zones=WBGT_ZONES, system_status=system_status)

@app.route("/toggle_cut_off", methods=["POST"])
def toggle_cut_off():
    username = request.form.get("username")
    if username not in users or not is_authority(users[username]["role"]):
        return jsonify({"error": "Unauthorized"}), 401

    now = sg_now()
    if system_status["cut_off"]:
        system_status["cut_off"] = False
        system_status["cut_off_end_time"] = (now + timedelta(minutes=30)).strftime("%H:%M:%S")
        for user_id, user_data in users.items():
            if user_data["role"] == "Trainer":
                user_data["status"] = "resting"
                user_data["zone"] = None
                user_data["start_time"] = now.strftime("%H:%M:%S")
                user_data["end_time"] = (now + timedelta(minutes=30)).strftime("%H:%M:%S")
    else:
        system_status["cut_off"] = True
        system_status["cut_off_end_time"] = None
        for user_id, user_data in users.items():
            if user_data["role"] == "Trainer":
                user_data["status"] = "idle"
                user_data["zone"] = None
                user_data["start_time"] = None
                user_data["end_time"] = None

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

@app.route("/clear_commands", methods=["POST"])
def clear_commands():
    username = request.form.get("username")
    if username not in users or not is_authority(users[username]["role"]):
        return jsonify({"error": "Unauthorized"}), 401

    global system_status
    system_status = {"cut_off": False, "cut_off_end_time": None}

    # Reset all trainers to idle state
    for user_id, user_data in users.items():
        if user_data["role"] == "Trainer":
            user_data.update({
                "status": "idle",
                "zone": None,
                "start_time": None,
                "end_time": None
            })

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
    if target_user != username and not is_authority(user_role):
        return jsonify({"error": "Unauthorized"}), 401
    if user_role == "Trainer" and target_user != username:
        return jsonify({"error": "Trainers can only set their own zone"}), 401

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

    return jsonify({
        "success": True,
        "start_time": users[target_user]["start_time"],
        "end_time": users[target_user]["end_time"]
    })

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

@app.route("/test_cycle", methods=["POST"])
def test_cycle():
    username = request.form.get("username")
    if username not in users:
        return jsonify({"error": "Unauthorized"}), 401

    now = sg_now()
    start_time = now.strftime("%H:%M:%S")
    end_time = (now + timedelta(seconds=10)).strftime("%H:%M:%S")

    users[username].update({
        "status": "working",
        "zone": "test",
        "start_time": start_time,
        "end_time": end_time,
        "work_completed": False,
        "pending_rest": False
    })

    print(f"Test cycle started for {username}: {start_time} to {end_time}")

    return jsonify({
        "success": True,
        "start_time": start_time,
        "end_time": end_time
    })

@app.route("/start_rest", methods=["POST"])
def start_rest():
    username = request.form.get("username")
    if username not in users:
        return jsonify({"error": f"User {username} not found"}), 404

    now = sg_now()
    user_data = users[username]

    # Print debug information
    print(f"Starting rest for user {username}. Current status: {user_data.get('status')}, zone: {user_data.get('zone')}")

    # Allow rest cycle to start regardless of current status
    zone = user_data.get("zone")
    if not zone:
        return jsonify({"error": "No active WBGT zone"}), 400

    # Set rest duration based on zone
    if zone == "test":
        rest_seconds = 20
        end_time = (now + timedelta(seconds=rest_seconds)).strftime("%H:%M:%S")
    else:
        rest_minutes = WBGT_ZONES[zone]["rest"]
        end_time = (now + timedelta(minutes=rest_minutes)).strftime("%H:%M:%S")

    start_time = now.strftime("%H:%M:%S")

    # Update user status to resting
    users[username].update({
        "status": "resting",
        "start_time": start_time,
        "end_time": end_time,
        "work_completed": False,
        "pending_rest": False
    })

    log_activity(username, "start_rest", zone)

    print(f"Rest cycle started for {username}: {start_time} to {end_time}")

    return jsonify({
        "success": True,
        "start_time": start_time,
        "end_time": end_time
    })

@app.route("/get_history")
def get_history():
    return jsonify(history_log)

@app.route("/get_updates")
def get_updates():
    now = sg_now()
    updates = check_user_cycles(now)
    return jsonify(updates)

def check_user_cycles(now):
    result = {"users": {}, "system_status": system_status, "history": history_log}

    for user_id, user_data in users.items():
        # Copy the user data to avoid modifying while iterating
        result["users"][user_id] = user_data.copy()

        # Check if user is in working status
        if user_data.get("status") == "working" and user_data.get("end_time"):
            end_time = datetime.strptime(user_data["end_time"], "%H:%M:%S")
            end_time = now.replace(hour=end_time.hour, minute=end_time.minute, second=end_time.second)

            # If work cycle has ended
            if now >= end_time:
                if not user_data.get("work_completed"):
                    zone = user_data.get("zone")
                    print(f"Work cycle completed for {user_id}, zone: {zone}")
                    log_activity(user_id, "completed_work", zone)

                    # Mark work as completed
                    user_data["work_completed"] = True
                    user_data["pending_rest"] = True

                    # Update result
                    result["users"][user_id]["work_completed"] = True
                    result["users"][user_id]["pending_rest"] = True

                    # Keep the zone and other data
                    # Don't change the status so we can still determine what zone they were in

                    # Emit work complete event
                    socketio.emit('work_complete', {'user': user_id})

        # Check if user is in resting status
        if user_data.get("status") == "resting" and user_data.get("end_time"):
            end_time = datetime.strptime(user_data["end_time"], "%H:%M:%S")
            end_time = now.replace(hour=end_time.hour, minute=end_time.minute, second=end_time.second)

            # If rest cycle has ended
            if now >= end_time:
                zone = user_data.get("zone")
                print(f"Rest cycle completed for {user_id}, zone: {zone}")
                log_activity(user_id, "completed_rest", zone)

                # Reset user data
                user_data.update({
                    "status": "idle",
                    "zone": None,
                    "start_time": None,
                    "end_time": None,
                    "work_completed": False,
                    "pending_rest": False
                })

                # Update result
                result["users"][user_id].update({
                    "status": "idle",
                    "zone": None,
                    "start_time": None,
                    "end_time": None,
                    "work_completed": False,
                    "pending_rest": False
                })

    return result

@app.route('/complete_cycle_early', methods=['POST'])
def complete_cycle_early():
    global history_log 
    
    username = request.form.get('username')
    if not username:
        return jsonify({'error': 'Username is required'}), 400

    # Update user status
    if username in users:
        users[username]['status'] = 'idle'

        # Get the current zone
        current_zone = users[username].get('zone')

        # Clear the timing information
        users[username]['start_time'] = None
        users[username]['end_time'] = None

        # Add to activity history
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history_log.append({  
            'timestamp': timestamp,
            'username': username,
            'action': 'early_completion',
            'zone': current_zone,
            'details': 'Early completion by user'
        })

        # Broadcast the update
        socketio.emit('history_update', {'history': history_log}) 

        return jsonify({'success': True})

    return jsonify({'error': 'User not found'}), 404
    
if __name__ == "__main__":
    locations = load_locations()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)