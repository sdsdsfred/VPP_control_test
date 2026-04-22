"""Web visualization server using Flask + Socket.IO.

This server serves a simple static page and streams simulation events via Socket.IO.
"""
import sys
import os
from threading import Lock

# ensure project root is on path so `main` / `vpp` packages are importable when launching from web/ dir
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO
from vpp.simulator import scenario_from_input

# Use threading mode for demo to avoid eventlet/gevent runtime complications
HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")

app = Flask(__name__, static_folder=STATIC_DIR)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# store last sent day forecasts so we can provide them to clients that connect mid-day
last_day_forecasts = {}
# keep a short history of recent hourly events so clients that connect late can get recent execution data
last_hourly_events = []
MAX_HOURLY_REPLAY = 48
MAX_DISPATCH_HISTORY = 200

# externally injected scenario from REST API
runtime_lock = Lock()
injected_scenario = None
simulation_running = False
latest_dispatch_output = None
dispatch_history = []
current_market_daily_cap = None
latest_correction_strategy = None
latest_storage_state = None
storage_feedback_by_hour = {}
latest_storage_feedback = None
load_feedback_by_hour = {}
latest_load_feedback = None
realtime_pv_by_hour = {}
latest_realtime_pv = None


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


def publisher(event: dict):
    """Publish an event to all connected clients."""
    global latest_dispatch_output, current_market_daily_cap, latest_correction_strategy, latest_storage_state
    try:
        # emit to all connected clients (no broadcast keyword to keep compatibility)
        socketio.emit("update", event)
        # small debug log
        try:
            print(f"[web] emit hour={event.get('hour')} status={event.get('control_status')}")
        except Exception:
            print("[web] emit event (no hour info)")
    except Exception as e:
        print("[web] failed to emit event", e)
    # capture day forecasts for replay to late-joining clients
    try:
        if event.get('day_index'):
            d = int(event['day_index'])
            last_day_forecasts[d] = {
                'day_index': d,
                'day_load_min': event.get('day_load_min'),
                'day_load_max': event.get('day_load_max'),
                'day_pv_mid': event.get('day_pv_mid')
            }
    except Exception:
        pass

    # capture hourly events for replay
    try:
        if event.get('hour') is not None:
            last_hourly_events.append(event)
            if len(last_hourly_events) > MAX_HOURLY_REPLAY:
                last_hourly_events.pop(0)
    except Exception:
        pass

    # capture dispatch outputs for REST API retrieval
    try:
        if event.get('hour') is not None:
            if event.get('market_daily_cap') is not None:
                with runtime_lock:
                    current_market_daily_cap = float(event.get('market_daily_cap'))

            dispatch_record = {
                'hour': int(event.get('hour')),
                'load_targets': event.get('load_targets') or [],
                'storage_target': float(event.get('storage_target', 0.0)),
                'control_status': event.get('control_status'),
                'adjust_reason': event.get('adjust_reason'),
            }
            with runtime_lock:
                latest_dispatch_output = dispatch_record
                dispatch_history.append(dispatch_record)
                if len(dispatch_history) > MAX_DISPATCH_HISTORY:
                    dispatch_history.pop(0)
    except Exception:
        pass

    # capture correction strategy for REST API retrieval
    try:
        if event.get('correction_strategy') is not None:
            strategy = event.get('correction_strategy')
            strategy_record = {
                'hour': int(event.get('hour')),
                'reduction_ratio': float(strategy.get('reduction_ratio', 0.0)),
                'target_hour_indices': strategy.get('target_hour_indices', []),
                'target_hour_loads': strategy.get('target_hour_loads', []),
            }
            with runtime_lock:
                latest_correction_strategy = strategy_record
    except Exception:
        pass

    # capture storage state for REST API retrieval
    try:
        if event.get('hour') is not None:
            storage_record = {
                'hour': int(event.get('hour')),
                'storage_soc': float(event.get('storage_soc', 50.0)),
                'storage_target': float(event.get('storage_target', 0.0)),
                'storage_target_base': float(event.get('storage_target_base', 0.0)),
                'storage_priority': event.get('storage_priority', 'none'),
                'stored_diff': float(event.get('stored_diff', 0.0)),
                'excess_diff': float(event.get('excess_diff', 0.0)),
                'adjust_reason': event.get('adjust_reason', ''),
                'storage_guidance_reason': event.get('storage_guidance_reason', ''),
                'storage_lookahead_hour': event.get('storage_lookahead_hour'),
                'storage_lookahead_forecast_gap': float(event.get('storage_lookahead_forecast_gap', 0.0)),
                'storage_recent_actual_gap': float(event.get('storage_recent_actual_gap', 0.0)),
                'storage_effective_gap': float(event.get('storage_effective_gap', 0.0)),
                'storage_projected_soc': float(event.get('storage_projected_soc', 0.0)),
            }
            with runtime_lock:
                latest_storage_state = storage_record
    except Exception:
        pass


def start_background_simulation(seed=1, delay_seconds=2.0, scenario=None):
    global simulation_running
    # run the demo simulation; it will call publisher for each hour
    # debug: show sys.path and cwd before import
    try:
        import sys as _sys, os as _os
        print("[web] start_background_simulation cwd=", _os.getcwd())
        print("[web] start_background_simulation sys.path[:3]=", _sys.path[:3])
    except Exception:
        pass

    # import here to avoid import-time issues with sys.path when running as a script
    # main.py lives at project root
    from main import run_demo

    def storage_feedback_getter(hour, target):
        """Return external module estimated storage action for this hour if provided."""
        with runtime_lock:
            rec = storage_feedback_by_hour.pop(int(hour), None)
        if rec is None:
            return None
        try:
            return float(rec.get('estimated_storage_target'))
        except Exception:
            return None

    def load_feedback_getter(hour, target):
        """Return external module estimated load adjust for this hour if provided."""
        with runtime_lock:
            rec = load_feedback_by_hour.pop(int(hour), None)
        if rec is None:
            return None
        try:
            return float(rec.get('estimated_load_adjust'))
        except Exception:
            return None

    def realtime_pv_getter(hour, default_value):
        """Return external realtime PV override for this hour if provided."""
        with runtime_lock:
            rec = realtime_pv_by_hour.get(int(hour))
        if rec is None:
            return None
        try:
            return float(rec.get('realtime_pv'))
        except Exception:
            return None

    try:
        # run demo with a small delay to pace events for UI
        run_demo(
            publisher=publisher,
            seed=seed,
            delay_seconds=delay_seconds,
            scenario=scenario,
            storage_feedback_getter=storage_feedback_getter,
            load_feedback_getter=load_feedback_getter,
            realtime_pv_getter=realtime_pv_getter,
        )
    finally:
        with runtime_lock:
            simulation_running = False


def start_background_strategy(seed=1, delay_seconds=2.0, soc_init=30.0, base_demand=800.0):
    """Background task running the D-2/D-1/D-day rolling strategy demo."""
    global simulation_running
    from main import run_strategy_demo

    def storage_feedback_getter(hour, target):
        with runtime_lock:
            rec = storage_feedback_by_hour.pop(int(hour), None)
        if rec is None:
            return None
        try:
            return float(rec.get('estimated_storage_target'))
        except Exception:
            return None

    def load_feedback_getter(hour, target):
        with runtime_lock:
            rec = load_feedback_by_hour.pop(int(hour), None)
        if rec is None:
            return None
        try:
            return float(rec.get('estimated_load_adjust'))
        except Exception:
            return None

    def realtime_pv_getter(hour, default_value):
        with runtime_lock:
            rec = realtime_pv_by_hour.get(int(hour))
        if rec is None:
            return None
        try:
            return float(rec.get('realtime_pv'))
        except Exception:
            return None

    try:
        run_strategy_demo(
            publisher=strategy_publisher,
            seed=seed,
            delay_seconds=delay_seconds,
            soc_init=soc_init,
            base_demand=base_demand,
            storage_feedback_getter=storage_feedback_getter,
            load_feedback_getter=load_feedback_getter,
            realtime_pv_getter=realtime_pv_getter,
        )
    finally:
        with runtime_lock:
            simulation_running = False


@app.route('/api/input/scenario', methods=['POST'])
def api_set_scenario():
    """Accept externally supplied forecast/actual payload for the next simulation run."""
    global injected_scenario, latest_realtime_pv
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'JSON body is required'}), 400

    try:
        scenario = scenario_from_input(payload)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    with runtime_lock:
        injected_scenario = scenario
        # Optional: preload realtime PV schedule together with scenario payload.
        pv_schedule = payload.get('realtime_pv_schedule')
        if pv_schedule is not None:
            if not isinstance(pv_schedule, list) or len(pv_schedule) != int(scenario.hours):
                return jsonify({'ok': False, 'error': f'realtime_pv_schedule must be a list with length={int(scenario.hours)}'}), 400
            realtime_pv_by_hour.clear()
            for i, v in enumerate(pv_schedule):
                realtime_pv_by_hour[int(i)] = {'hour': int(i), 'realtime_pv': float(v)}
            if len(pv_schedule) > 0:
                latest_realtime_pv = {'hour': int(len(pv_schedule) - 1), 'realtime_pv': float(pv_schedule[-1])}
            else:
                latest_realtime_pv = None

    return jsonify({
        'ok': True,
        'hours': int(scenario.hours),
        'n_load_groups': int(scenario.n_load_groups),
        'market_daily_cap': float(scenario.market_daily_cap),
        'has_market_hourly_cap': bool(getattr(scenario, 'market_hourly_cap', None) is not None),
        'has_realtime_pv_schedule': bool(len(realtime_pv_by_hour) > 0),
    })


@app.route('/api/input/scenario', methods=['GET'])
def api_get_scenario_status():
    with runtime_lock:
        scenario = injected_scenario

    if scenario is None:
        return jsonify({'ok': True, 'has_scenario': False})

    return jsonify({
        'ok': True,
        'has_scenario': True,
        'hours': int(scenario.hours),
        'n_load_groups': int(scenario.n_load_groups),
        'market_daily_cap': float(scenario.market_daily_cap),
        'has_market_hourly_cap': bool(getattr(scenario, 'market_hourly_cap', None) is not None),
        'has_realtime_pv_schedule': bool(len(realtime_pv_by_hour) > 0),
    })


@app.route('/api/input/storage-feedback', methods=['POST'])
def api_set_storage_feedback():
    """Receive storage module estimated response after target command.

    JSON body:
    {
      "hour": 25,
      "target_storage": -18.0,            # optional, signed kW
      "estimated_storage_target": -15.2   # required, signed kW
    }
    """
    global latest_storage_feedback
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'JSON body is required'}), 400

    if 'hour' not in payload or 'estimated_storage_target' not in payload:
        return jsonify({'ok': False, 'error': 'hour and estimated_storage_target are required'}), 400

    try:
        hour = int(payload.get('hour'))
        estimated = float(payload.get('estimated_storage_target'))
        target = float(payload.get('target_storage', estimated))
    except Exception:
        return jsonify({'ok': False, 'error': 'hour/target/estimated must be numeric'}), 400

    rec = {
        'hour': hour,
        'target_storage': target,
        'estimated_storage_target': estimated,
    }
    with runtime_lock:
        storage_feedback_by_hour[hour] = rec
        latest_storage_feedback = rec

    return jsonify({'ok': True, 'data': rec})


@app.route('/api/input/storage-feedback/latest', methods=['GET'])
def api_get_storage_feedback_latest():
    with runtime_lock:
        rec = latest_storage_feedback
    if rec is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})
    return jsonify({'ok': True, 'has_data': True, 'data': rec})


@app.route('/api/input/load-feedback', methods=['POST'])
def api_set_load_feedback():
    """Receive load-control module estimated executable adjustment.

    JSON body:
    {
      "hour": 25,
      "target_load_adjust": -22.0,
      "estimated_load_adjust": -18.5
    }
    """
    global latest_load_feedback
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'JSON body is required'}), 400

    if 'hour' not in payload or 'estimated_load_adjust' not in payload:
        return jsonify({'ok': False, 'error': 'hour and estimated_load_adjust are required'}), 400

    try:
        hour = int(payload.get('hour'))
        estimated = float(payload.get('estimated_load_adjust'))
        target = float(payload.get('target_load_adjust', estimated))
    except Exception:
        return jsonify({'ok': False, 'error': 'hour/target/estimated must be numeric'}), 400

    rec = {
        'hour': hour,
        'target_load_adjust': target,
        'estimated_load_adjust': estimated,
    }
    with runtime_lock:
        load_feedback_by_hour[hour] = rec
        latest_load_feedback = rec

    return jsonify({'ok': True, 'data': rec})


@app.route('/api/input/load-feedback/latest', methods=['GET'])
def api_get_load_feedback_latest():
    with runtime_lock:
        rec = latest_load_feedback
    if rec is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})
    return jsonify({'ok': True, 'has_data': True, 'data': rec})


@app.route('/api/input/realtime-pv', methods=['POST'])
def api_set_realtime_pv():
    """Receive realtime PV for a specific hour.

    JSON body:
    {
      "hour": 25,
      "realtime_pv": 120.5
    }
    """
    global latest_realtime_pv
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'JSON body is required'}), 400

    if 'hour' not in payload or 'realtime_pv' not in payload:
        return jsonify({'ok': False, 'error': 'hour and realtime_pv are required'}), 400

    try:
        hour = int(payload.get('hour'))
        realtime_pv = float(payload.get('realtime_pv'))
    except Exception:
        return jsonify({'ok': False, 'error': 'hour and realtime_pv must be numeric'}), 400

    rec = {
        'hour': hour,
        'realtime_pv': realtime_pv,
    }
    with runtime_lock:
        realtime_pv_by_hour[hour] = rec
        latest_realtime_pv = rec

    return jsonify({'ok': True, 'data': rec})


@app.route('/api/input/realtime-pv/latest', methods=['GET'])
def api_get_realtime_pv_latest():
    with runtime_lock:
        rec = latest_realtime_pv
    if rec is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})
    return jsonify({'ok': True, 'has_data': True, 'data': rec})


@app.route('/api/input/realtime-pv/schedule', methods=['POST'])
def api_set_realtime_pv_schedule():
    """Set realtime PV schedule for simulation-stage time-sliced reads.

    JSON body:
    {
      "start_hour": 0,
      "values": [100.0, 110.0, 95.0]
    }
    """
    global latest_realtime_pv
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'JSON body is required'}), 400

    if 'values' not in payload:
        return jsonify({'ok': False, 'error': 'values is required'}), 400

    values = payload.get('values')
    if not isinstance(values, list):
        return jsonify({'ok': False, 'error': 'values must be a list'}), 400

    try:
        start_hour = int(payload.get('start_hour', 0))
        parsed = [float(x) for x in values]
    except Exception:
        return jsonify({'ok': False, 'error': 'start_hour and values must be numeric'}), 400

    with runtime_lock:
        realtime_pv_by_hour.clear()
        for i, v in enumerate(parsed):
            h = int(start_hour + i)
            realtime_pv_by_hour[h] = {'hour': h, 'realtime_pv': float(v)}
        if parsed:
            latest_realtime_pv = {'hour': int(start_hour + len(parsed) - 1), 'realtime_pv': float(parsed[-1])}
        else:
            latest_realtime_pv = None

    return jsonify({
        'ok': True,
        'count': len(parsed),
        'start_hour': int(start_hour),
        'end_hour': int(start_hour + len(parsed) - 1) if parsed else int(start_hour),
    })


@app.route('/api/input/realtime-pv/schedule', methods=['GET'])
def api_get_realtime_pv_schedule_status():
    with runtime_lock:
        hours = sorted(realtime_pv_by_hour.keys())
    if not hours:
        return jsonify({'ok': True, 'has_data': False, 'count': 0, 'start_hour': None, 'end_hour': None})
    return jsonify({
        'ok': True,
        'has_data': True,
        'count': int(len(hours)),
        'start_hour': int(hours[0]),
        'end_hour': int(hours[-1]),
    })


@app.route('/api/simulation/start', methods=['POST'])
def api_start_simulation():
    """Start simulation. By default, consume injected scenario if available."""
    global simulation_running, current_market_daily_cap, latest_realtime_pv
    body = request.get_json(silent=True) or {}

    seed = int(body.get('seed', 1))
    delay_seconds = float(body.get('delay_seconds', 2.0))
    use_injected = bool(body.get('use_injected', True))

    with runtime_lock:
        if simulation_running:
            return jsonify({'ok': False, 'error': 'simulation is already running'}), 409

        scenario = injected_scenario if use_injected else None
        if scenario is not None:
            current_market_daily_cap = float(scenario.market_daily_cap)
        else:
            current_market_daily_cap = None
        storage_feedback_by_hour.clear()
        load_feedback_by_hour.clear()
        simulation_running = True

    socketio.start_background_task(
        start_background_simulation,
        seed,
        delay_seconds,
        scenario,
    )

    source = 'injected' if scenario is not None else 'generated'
    return jsonify({'ok': True, 'started': True, 'source': source})


# -- D-2/D-1/D-day strategy state ---------------------------------------------
latest_strategy_plan: dict = {}
strategy_plan_history: list = []
MAX_STRATEGY_HISTORY = 30


def strategy_publisher(event: dict):
    """Publisher for strategy demo events (initial_plan / replan / d_day_hour)."""
    global latest_strategy_plan
    try:
        socketio.emit("strategy_update", event)
        # Keep main chart in sync during strategy mode by mirroring hour events.
        if event.get("hour") is not None:
            socketio.emit("update", event)
        evt_type = event.get("type", "?")
        if evt_type == "strategy_hour":
            h = event.get("hour")
            if isinstance(h, int):
                if h < 24:
                    evt_type = "d2_hour_result"
                elif h < 48:
                    evt_type = "d1_hour_result"
                else:
                    evt_type = "dday_hour_result"
        print(f"[web] strategy emit type={evt_type}")
    except Exception as e:
        print("[web] strategy emit failed:", e)

    try:
        with runtime_lock:
            latest_strategy_plan = dict(event)
            strategy_plan_history.append(dict(event))
            if len(strategy_plan_history) > MAX_STRATEGY_HISTORY:
                strategy_plan_history.pop(0)
    except Exception:
        pass


@app.route('/api/strategy/start', methods=['POST'])
def api_start_strategy():
    """Start the D-2/D-1/D-day rolling strategy demo."""
    global simulation_running, latest_realtime_pv
    body = request.get_json(silent=True) or {}

    seed         = int(body.get('seed', 42))
    delay_seconds = float(body.get('delay_seconds', 2.0))
    soc_init     = float(body.get('soc_init', 30.0))
    base_demand  = float(body.get('base_demand', 800.0))

    with runtime_lock:
        if simulation_running:
            return jsonify({'ok': False, 'error': 'a simulation is already running'}), 409
        storage_feedback_by_hour.clear()
        load_feedback_by_hour.clear()
        simulation_running = True

    socketio.start_background_task(
        start_background_strategy,
        seed,
        delay_seconds,
        soc_init,
        base_demand,
    )

    return jsonify({'ok': True, 'started': True, 'mode': 'strategy'})


@app.route('/api/strategy/latest', methods=['GET'])
def api_strategy_latest():
    with runtime_lock:
        plan = dict(latest_strategy_plan)
    return jsonify({'ok': True, 'has_data': bool(plan), 'data': plan})


@app.route('/api/strategy/history', methods=['GET'])
def api_strategy_history():
    limit = request.args.get('limit', default=MAX_STRATEGY_HISTORY, type=int)
    with runtime_lock:
        items = strategy_plan_history[-limit:]
    return jsonify({'ok': True, 'count': len(items), 'items': items})


@app.route('/api/simulation/status', methods=['GET'])
def api_simulation_status():
    with runtime_lock:
        running = simulation_running
        has_scenario = injected_scenario is not None
    return jsonify({'ok': True, 'running': running, 'has_injected_scenario': has_scenario})


@app.route('/api/scenario/current-cap', methods=['GET'])
def api_current_scenario_cap():
    """Return market_daily_cap for current running scenario (if available)."""
    with runtime_lock:
        running = simulation_running
        cap = current_market_daily_cap

    if cap is None:
        return jsonify({'ok': True, 'running': running, 'has_cap': False, 'market_daily_cap': None})

    return jsonify({'ok': True, 'running': running, 'has_cap': True, 'market_daily_cap': float(cap)})


@app.route('/api/output/dispatch/latest', methods=['GET'])
def api_dispatch_latest():
    with runtime_lock:
        record = latest_dispatch_output

    if record is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})

    return jsonify({'ok': True, 'has_data': True, 'data': record})


@app.route('/api/output/dispatch/history', methods=['GET'])
def api_dispatch_history():
    limit = request.args.get('limit', default=24, type=int)
    if limit is None or limit <= 0:
        limit = 24
    if limit > MAX_DISPATCH_HISTORY:
        limit = MAX_DISPATCH_HISTORY

    with runtime_lock:
        items = dispatch_history[-limit:]

    return jsonify({'ok': True, 'count': len(items), 'items': items})


@app.route('/api/output/correction/latest', methods=['GET'])
def api_correction_latest():
    """返回最新的负荷修正策略。"""
    with runtime_lock:
        strategy = latest_correction_strategy

    if strategy is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})

    return jsonify({'ok': True, 'has_data': True, 'data': strategy})


@app.route('/api/output/storage/latest', methods=['GET'])
def api_storage_latest():
    """返回最新的储能状态和调控策略。"""
    with runtime_lock:
        state = latest_storage_state

    if state is None:
        return jsonify({'ok': True, 'has_data': False, 'data': None})

    return jsonify({'ok': True, 'has_data': True, 'data': state})

@socketio.on('connect')
def handle_connect():
    try:
        print(f"[web] client connected sid={request.sid}")
    except Exception:
        print("[web] client connected")
    # replay any stored day forecasts to the newly connected client
    try:
        for d, evt in last_day_forecasts.items():
            try:
                # send only the day-forecast payload back to this client (use room= for python-socketio)
                socketio.emit('update', evt, room=request.sid)
                print(f"[web] replayed day={d} forecast to sid={request.sid}")
            except Exception as e:
                print(f"[web] replay to sid failed, broadcasting instead: {e}")
                try:
                    socketio.emit('update', evt)
                except Exception:
                    pass
    except Exception:
        pass

    # replay recent hourly events to new client so energy cap / execution metrics are available immediately
    try:
        replay_items = list(last_hourly_events)
        if replay_items:
            print(f"[web] replayed {len(replay_items)} recent hourly events to sid={request.sid}")
            for evt in replay_items:
                try:
                    socketio.emit('update', evt, room=request.sid)
                except Exception:
                    pass
    except Exception:
        pass

@socketio.on('disconnect')
def handle_disconnect():
    try:
        print(f"[web] client disconnected sid={request.sid}")
    except Exception:
        print("[web] client disconnected")

def run_server(host="127.0.0.1", port=8000, seed=1, autostart=False):
    global simulation_running
    # keep compatibility: auto-run generated simulation unless disabled
    if autostart:
        with runtime_lock:
            simulation_running = True
        socketio.start_background_task(start_background_simulation, seed, 0.6, None)
    # run web server (blocking)
    socketio.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server(autostart=False)
