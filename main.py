import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import pytz
import json

# Near the top of your file:
from flask_cors import CORS

app = Flask(__name__)
app.secret_key = 'secret_key'
CORS(app)  # Add CORS support

# Initialize SocketIO with cors_allowed_origins="*"
socketio = SocketIO(app, 
                   async_mode='eventlet', 
                   cors_allowed_origins="*",
                   ping_timeout=60, 
                   ping_interval=25)

@socketio.on('connect')
def handle_connect():
    print("Client connected:", request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    print("Client disconnected:", request.sid)

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
    def _save():
        with open('locations.json', 'w') as f:
            json.dump(locations, f)
    eventlet.spawn(_save)

def load_locations():
    try:
        def _load():
            with open('locations.json', 'r') as f:
                return json.load(f)
        return eventlet.spawn(_load).wait()
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

@app.route('/toggle_cut_off', methods=['POST'])
def toggle_cut_off():
    global system_status, history_log, users
    previous_state = system_status.get("cut_off", False)
    system_status["cut_off"] = not previous_state
    
    try:
        # If activating cut-off
        if system_status["cut_off"]:
            for username, user in users.items():
                if user.get('role') == 'Trainer':
                    # Set all trainers to idle status
                    previous_status = user.get('status')
                    previous_zone = user.get('zone')
                    
                    # Clear all user status and timing information
                    user['status'] = 'idle'
                    user['start_time'] = None
                    user['end_time'] = None
                    
                    # Important: Also clear the zone!
                    user['zone'] = None
                    
                    # Only add to history if they were active
                    if previous_status in ['working', 'resting']:
                        # Add to history
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        history_log.append({
                            'timestamp': timestamp,
                            'username': username,
                            'action': 'cut_off_activated',
                            'zone': previous_zone,
                            'details': 'Activity stopped by cut-off activation'
                        })
            
            # Reset any mandatory rest period
            system_status.pop("cut_off_end_time", None)
            system_status["mandatory_rest"] = False
                    
        else:
            # If deactivating cut-off, set mandatory rest period
            current_time = datetime.now(SG_TZ) if 'SG_TZ' in globals() else datetime.now()
            end_time = current_time + timedelta(minutes=30)
            system_status["cut_off_end_time"] = end_time.strftime('%Y-%m-%d %H:%M:%S')
            
            # Also set a flag to prevent trainers from ending this rest early
            system_status["mandatory_rest"] = True
            
            # Add to history
            timestamp = current_time.strftime('%Y-%m-%d %H:%M:%S')
            history_log.append({
                'timestamp': timestamp,
                'action': 'mandatory_rest',
                'details': 'Mandatory 30-minute rest period initiated'
            })
            
            # Schedule the end of mandatory rest
            def end_mandatory_rest():
                system_status["mandatory_rest"] = False
                system_status.pop("cut_off_end_time", None)
                try:
                    socketio.emit('system_status_update', system_status)
                    print("Mandatory rest period ended")
                except Exception as e:
                    print(f"Error emitting update at end of mandatory rest: {e}")
            
            # Use eventlet to schedule the end of mandatory rest
            import eventlet
            eventlet.spawn_after(30 * 60, end_mandatory_rest)
        
        # Debug log
        print(f"System status after toggle: {system_status}")
        print(f"User statuses: {[{k: {'status': v.get('status'), 'zone': v.get('zone')}} for k, v in users.items()]}")
        
        # Emit system status update to all clients - use try/except to handle connection issues
        try:
            socketio.emit('system_status_update', system_status)
            socketio.emit('user_update', {'users': users})
            socketio.emit('history_update', {'history': history_log})
        except Exception as e:
            print(f"Error emitting updates: {e}")
        
        return jsonify({"status": "success", "cut_off": system_status["cut_off"]})
    
    except Exception as e:
        print(f"Error in toggle_cut_off: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/clear_commands', methods=['POST'])
def clear_commands():
    global users, system_status, history_log
    
    # Clear the cut-off status
    system_status["cut_off"] = False
    
    # Clear the mandatory rest period
    if "cut_off_end_time" in system_status:
        system_status.pop("cut_off_end_time", None)
    
    # Clear the mandatory rest flag
    system_status["mandatory_rest"] = False
    
    # Clear all user activities
    for username, user in users.items():
        if user.get('role') == 'Trainer':
            user['status'] = 'idle'
            user['start_time'] = None
            user['end_time'] = None
            user['work_completed'] = False
            user['pending_rest'] = False
    
    # Add to history log
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    history_log.append({
        'timestamp': timestamp,
        'action': 'clear_commands',
        'details': 'All commands cleared by conducting body'
    })
    
    # Emit updates to all clients
    try:
        socketio.emit('system_status_update', system_status)
        socketio.emit('user_update', {'users': users})
        socketio.emit('history_update', {'history': history_log})
    except Exception as e:
        print(f"Error emitting updates in clear_commands: {e}")
    
    return jsonify({"status": "success"})

@app.route("/set_zone", methods=["POST"])
def set_zone():
    username = request.form.get("username")
    target_user = request.form.get("target_user", username)
    zone = request.form.get("zone")
    now = sg_now()

    # Check for cut-off mode
    if system_status.get("cut_off", False) and not is_authority(users.get(username, {}).get("role", "")):
        return jsonify({"error": "System is in cut-off mode"}), 403

    # Safe check for cut_off_end_time
    if system_status.get("cut_off_end_time"):
        try:
            cut_off_end = datetime.strptime(system_status["cut_off_end_time"], "%Y-%m-%d %H:%M:%S")
            if now < cut_off_end and not is_authority(users.get(username, {}).get("role", "")):
                return jsonify({"error": "Mandatory rest period is still active"}), 403
        except ValueError:
            # If there's an issue with date format, just log it and continue
            print(f"Error parsing cut_off_end_time: {system_status.get('cut_off_end_time')}")

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

    # Add to history log
    timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
    history_log.append({
        'timestamp': timestamp,
        'username': username,
        'action': 'set_zone',
        'zone': zone,
        'details': f'Zone set to {zone} for user {target_user}'
    })
    
    # Emit updates
    try:
        socketio.emit('user_update', {'users': users})
        socketio.emit('history_update', {'history': history_log})
    except Exception as e:
        print(f"Error emitting updates in set_zone: {e}")

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

@app.route('/get_system_status', methods=['GET'])
def get_system_status():
    return jsonify(system_status)

@app.route('/reset_logs', methods=['POST'])
def reset_logs():
    global history_log
    
    try:
        # Clear history log
        history_log = []
        
        # Add a record of the reset action
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history_log.append({
            'timestamp': timestamp,
            'action': 'reset_logs',
            'details': 'User logs reset by conducting body'
        })
        
        # Emit updates
        socketio.emit('history_update', {'history': history_log})
        
        return jsonify({"status": "success", "message": "Logs reset successfully"})
    except Exception as e:
        print(f"Error in reset_logs: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/complete_cycle_early', methods=['POST'])
def complete_cycle_early():
    global history_log
    
    username = request.form.get('username')
    if not username:
        return jsonify({'error': 'Username is required'}), 400

    # Update user status
    if username in users:
        # Check if this is a mandatory rest cycle
        is_resting = users[username].get('status') == 'resting'
        is_mandatory_rest = system_status.get('cut_off_end_time') is not None
        
        if is_resting and is_mandatory_rest:
            return jsonify({'error': 'Cannot end mandatory rest cycles early'}), 403
            
        # Proceed with early completion
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
    # Load locations
    locations = load_locations()
    
    # Check if running on Render
    import os
    if os.environ.get('RENDER') == 'true':
        # In production on Render, don't call socketio.run()
        # Gunicorn will manage the app
        pass
    else:
        # In local development
        socketio.run(app, host="0.0.0.0", port=5000, debug=True)