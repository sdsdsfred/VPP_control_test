"""Run a simple simulation loop demonstrating MILP control + RCA."""
import numpy as np

from vpp.simulator import generate_scenario, simulate_execution
from vpp.model import optimize_hourly
from vpp.rca import root_cause_and_adjust
from vpp.planner import (
    generate_price_schedule,
    optimize_dispatch,
    HourlyActual,
)


def _price_profile_for_hour(hour: int) -> tuple[float, float]:
    """Return (realtime_price, contract_price) for hour-of-day.

    Contract price follows the step profile in the user-provided figure.
    """
    hod = int(hour % 24)

    # Signed/contract price step curve (yuan/MWh style) from the figure.
    # Hours use 0-based indexing here.
    if hod <= 6:          # 1-7
        contract = 150.0
    elif hod <= 8:        # 8-9
        contract = 390.0
    elif hod <= 10:       # 10-11
        contract = 630.0
    elif hod <= 14:       # 12-15
        contract = 390.0
    elif hod <= 20:       # 16-21
        contract = 630.0
    else:                 # 22-24
        contract = 150.0

    # Realtime price profile shaped like the blue curve in the figure.
    realtime_curve = [
        270.0, 220.0, 210.0, 220.0, 225.0, 270.0,
        330.0, 310.0, 225.0, 185.0, 170.0, 155.0,
        135.0, 150.0, 185.0, 220.0, 300.0, 310.0,
        335.0, 335.0, 320.0, 302.0, 278.0, 270.0,
    ]
    realtime = float(realtime_curve[hod])
    return realtime, contract


def _estimate_future_soc(plan, current_hour: int, target_hour: int, current_soc: float) -> float:
    """Estimate SoC at the end of target_hour using the current dispatch plan."""
    if target_hour < current_hour:
        return float(current_soc)
    if target_hour >= len(plan.soc_trajectory):
        target_hour = len(plan.soc_trajectory) - 1
    if target_hour < 0:
        return float(current_soc)
    return float(plan.soc_trajectory[target_hour])


def _apply_lookahead_storage_policy(
    *,
    hour: int,
    hours: int,
    estimated_demand: np.ndarray,
    prices,
    plan,
    current_soc: float,
    battery_power_kw: float,
    charge_efficiency: float,
    soc_max: float,
    actual_done: np.ndarray,
):
    """Adjust current-hour storage target using hour+24 demand/purchase outlook.

    Rule set:
    - If hour+24 forecast likely exceeds its contracted purchase, start charging early.
    - If hour+24 forecast is below contracted purchase and projected SoC reaches full,
      reduce current-hour charging to avoid saturating the battery too early.
    """
    lookahead_hour = hour + 24
    base_target = float(plan.storage_charge[hour] - plan.storage_discharge[hour])
    guidance = {
        "enabled": False,
        "lookahead_hour": None,
        "forecast_gap": 0.0,
        "recent_actual_gap": 0.0,
        "effective_gap": 0.0,
        "projected_soc": float(current_soc),
        "storage_target_base": base_target,
        "storage_target_adjusted": base_target,
        "reason": "out_of_horizon",
    }

    if lookahead_hour >= hours:
        return base_target, guidance

    future_forecast = float(estimated_demand[lookahead_hour])
    future_buy_qty = float(prices.buy_qty[lookahead_hour])
    forecast_gap = future_forecast - future_buy_qty
    recent_actual_gap = 0.0
    if hour > 0:
        recent_actual_gap = float(actual_done[hour - 1] - prices.buy_qty[hour - 1])

    effective_gap = forecast_gap + 0.35 * recent_actual_gap
    projected_soc = _estimate_future_soc(plan, hour, lookahead_hour - 1, current_soc)
    remaining_hours = max(1, lookahead_hour - hour)
    adjusted_target = base_target
    max_charge_now = max(0.0, (soc_max - current_soc) / max(charge_efficiency, 1e-6))

    guidance.update({
        "enabled": True,
        "lookahead_hour": int(lookahead_hour),
        "forecast_gap": float(forecast_gap),
        "recent_actual_gap": float(recent_actual_gap),
        "effective_gap": float(effective_gap),
        "projected_soc": float(projected_soc),
    })

    if effective_gap > 0.0:
        reserve_need = min(float(soc_max - current_soc), float(effective_gap))
        charge_step = reserve_need / float(remaining_hours)
        charge_step = min(float(battery_power_kw), max(0.0, charge_step / max(charge_efficiency, 1e-6)))
        adjusted_target = min(float(battery_power_kw), max(base_target, charge_step))
        guidance["reason"] = "precharge_for_h_plus_24_deficit"
    elif forecast_gap < 0.0 and projected_soc >= soc_max - 1.0 and base_target > 0.0:
        reducible_energy = min(projected_soc - (soc_max - 1.0), abs(forecast_gap))
        reduce_step = min(base_target, max(0.0, reducible_energy / max(charge_efficiency, 1e-6)))
        adjusted_target = max(0.0, base_target - reduce_step)
        guidance["reason"] = "trim_charge_for_h_plus_24_surplus"
    else:
        guidance["reason"] = "keep_milp_storage_target"

    if adjusted_target > 0.0:
        adjusted_target = min(adjusted_target, max_charge_now)

    guidance["storage_target_adjusted"] = float(adjusted_target)
    return float(adjusted_target), guidance


def run_demo(
    publisher=None,
    seed=1,
    delay_seconds: float = 2.0,
    scenario=None,
    storage_feedback_getter=None,
    load_feedback_getter=None,
    realtime_pv_getter=None,
):
    """Run the demo; if publisher is provided it will be called with an event dict each hour.

    publisher(event_dict) where event_dict contains keys: hour, pv_min, pv_max,
    load_forecast_min, load_forecast_max, actual_total, achieved_total, control_status, adjust_reason
    """
    # If no external input is supplied, keep default random 72-hour scenario.
    if scenario is None:
        scenario = generate_scenario(hours=72, n_load_groups=4, seed=seed)

    # Purchased energy is constrained by D-2 fixed hourly market caps.
    forecast_mid = (scenario.load_forecast_min + scenario.load_forecast_max) / 2.0
    market_hourly_cap = getattr(scenario, "market_hourly_cap", None)
    if market_hourly_cap is None or len(market_hourly_cap) != scenario.hours:
        market_hourly_cap = forecast_mid.copy()
    procured_energy_total = float(np.sum(market_hourly_cap))
    n_days = (scenario.hours + 23) // 24
    procured_energy_by_day = []
    for d in range(n_days):
        s = d * 24
        e = min((d + 1) * 24, scenario.hours)
        procured_energy_by_day.append(float(np.sum(market_hourly_cap[s:e])))

    soc = 30.0
    market_remaining = procured_energy_total
    total_energy_consumed = 0.0
    daily_energy_consumed = [0.0 for _ in range(n_days)]
    
    # Storage command history for detecting unresponsive storage
    storage_commands_history = []
    recent_storage_cmd = None

    for h in range(scenario.hours):
        realtime_price, contract_price = _price_profile_for_hour(h)
        actual_hour = np.array(scenario.actual_loads[h], dtype=float, copy=True)
        pv_min = scenario.pv_forecast_min[h]
        pv_max = scenario.pv_forecast_max[h]
        pv_rt = None
        if callable(realtime_pv_getter):
            pv_rt = realtime_pv_getter(h, (float(pv_min) + float(pv_max)) / 2.0)
            if pv_rt is not None:
                pv_min = float(pv_rt)
                pv_max = float(pv_rt)
        lf_min = scenario.load_forecast_min[h]
        lf_max = scenario.load_forecast_max[h]

        day_idx = h // 24
        hourly_cap = float(market_hourly_cap[h])
        if h < 24:
            actual_total = float(np.sum(actual_hour))
            if actual_total > 1e-9:
                actual_hour *= hourly_cap / actual_total
            else:
                actual_hour = np.full_like(actual_hour, hourly_cap / max(1, len(actual_hour)), dtype=float)
        daily_remaining = max(0.0, procured_energy_by_day[day_idx] - daily_energy_consumed[day_idx])
        print(
            f"Hour {h}: forecast load [{lf_min:.1f}, {lf_max:.1f}], "
            f"total actual {actual_hour.sum():.1f}, storage_soc={soc:.1f}%, "
            f"hourly_cap={hourly_cap:.1f}, day{day_idx + 1}_remaining={daily_remaining:.1f}"
        )

        # initial MILP compute
        control = optimize_hourly(h, pv_min, pv_max, lf_min, lf_max, hourly_cap, actual_hour, soc)
        print("  MILP status:", control["status"])

        # send to execution simulator
        feedback = simulate_execution(control, actual_hour)
        achieved = feedback["achieved_loads"]
        print(f"  Achieved total after MILP: {achieved.sum():.1f}")

        # root cause analysis with storage state
        adjust = root_cause_and_adjust(
            h, scenario, achieved, feedback,
            storage_soc=soc,
            recent_storage_cmd=recent_storage_cmd,
            storage_response_timeout=2
        )
        final_targets = control
        correction_strategy = {
            'triggered': False,
            'reason': 'no_overuse',
            'lookahead_hours': 0,
            'reduction_ratio': 0.0,
            'target_hour_indices': [],
            'target_hour_loads': [],
        }
        storage_target_requested = 0.0
        storage_target_estimated = 0.0
        if adjust.get("reason") != "ok":
            print(f"  RCA triggered: {adjust.get('reason')} (priority={adjust.get('control_priority')}) -> issuing new targets")
            feedback2 = simulate_execution(adjust, achieved)
            print(f"  Achieved after RCA action: {feedback2['achieved_loads'].sum():.1f}")
            achieved_total = feedback2['achieved_loads'].sum()
            
            # Update storage SoC based on this hour's action
            storage_action = float(adjust.get('storage_target', 0.0))
            storage_target_requested = storage_action
            if callable(storage_feedback_getter):
                fb = storage_feedback_getter(h, storage_action)
                if fb is not None:
                    storage_action = float(fb)
            storage_target_estimated = storage_action

            soc += storage_action * 0.5  # crude model: 1 kW charge ~= 0.5% SoC per hour
            soc = max(0.0, min(100.0, soc))  # clamp to [0, 100]
            
            # Track this storage command
            if storage_action != 0.0:
                cmd_record = {
                    'hour': h,
                    'target': float(storage_action),
                    'executed': True,  # simulator always executes
                }
                storage_commands_history.append(cmd_record)
                recent_storage_cmd = cmd_record
                print(f"    Storage action issued: {storage_action:.1f} kW")
            
            adjust_reason = adjust.get('reason')
            final_targets = adjust
        else:
            achieved_total = achieved.sum()
            adjust_reason = adjust.get('reason')

        # Apply load-control module executable feedback (signed load adjustment)
        # to this hour's realized total so subsequent decisions use real executable outcome.
        load_adjust_requested = 0.0
        load_adjust_estimated = 0.0
        try:
            target_arr = final_targets.get('load_targets', []) if isinstance(final_targets, dict) else []
            if target_arr:
                load_adjust_requested = float(sum(target_arr) - float(actual_hour.sum()))
        except Exception:
            load_adjust_requested = 0.0

        if callable(load_feedback_getter):
            fb_load = load_feedback_getter(h, load_adjust_requested)
            if fb_load is not None:
                load_adjust_estimated = float(fb_load)
                achieved_total = max(0.0, float(actual_hour.sum()) + load_adjust_estimated)
            else:
                load_adjust_estimated = float(load_adjust_requested)
        else:
            load_adjust_estimated = float(load_adjust_requested)

        total_energy_consumed += float(achieved_total)
        daily_energy_consumed[day_idx] += float(achieved_total)

        # Rolling correction: if cumulative actual exceeds cumulative forecast mid,
        # lower the next 2 hours' expected/actual levels to keep total within budget.
        cumulative_forecast_to_now = float(forecast_mid[: h + 1].sum())
        cumulative_overuse = max(0.0, total_energy_consumed - cumulative_forecast_to_now)

        if cumulative_overuse > 0 and h < scenario.hours - 1:
            next_start = h + 1
            next_end = min(h + 3, scenario.hours)
            idxs = list(range(next_start, next_end))
            future_mid_sum = float(forecast_mid[idxs].sum()) if idxs else 0.0

            if future_mid_sum > 0:
                reduction_ratio = min(0.30, cumulative_overuse / future_mid_sum)
                if reduction_ratio > 0:
                    for i in idxs:
                        scenario.actual_loads[i] = scenario.actual_loads[i] * (1.0 - reduction_ratio)
                        scenario.load_forecast_min[i] = scenario.load_forecast_min[i] * (1.0 - reduction_ratio)
                        scenario.load_forecast_max[i] = scenario.load_forecast_max[i] * (1.0 - reduction_ratio)
                        forecast_mid[i] = (scenario.load_forecast_min[i] + scenario.load_forecast_max[i]) / 2.0

                    correction_strategy = {
                        'triggered': True,
                        'reason': 'cumulative_overuse',
                        'lookahead_hours': len(idxs),
                        'reduction_ratio': float(reduction_ratio),
                        'target_hour_indices': [int(i) for i in idxs],
                        'target_hour_loads': [float(forecast_mid[i]) for i in idxs],
                    }
                    print(
                        f"  Energy correction: overuse={cumulative_overuse:.1f}, "
                        f"next_{len(idxs)}h reduction={reduction_ratio * 100:.1f}%"
                    )

        # notify publisher if present
        if publisher:
            event = {
                'hour': h,
                'pv_min': float(pv_min),
                'pv_max': float(pv_max),
                'load_forecast_min': float(lf_min),
                'load_forecast_max': float(lf_max),
                'actual_total': float(actual_hour.sum()),
                'achieved_total': float(achieved_total),
                'control_status': control.get('status'),
                'adjust_reason': adjust_reason,
                # expose dispatch targets via event for downstream API/web consumers
                'load_targets': [float(x) for x in final_targets.get('load_targets', [])],
                'storage_target': float(storage_target_estimated),
                'storage_target_requested': float(storage_target_requested),
                'market_hourly_cap': float(hourly_cap),
                'load_adjust_requested': float(load_adjust_requested),
                'load_adjust_estimated': float(load_adjust_estimated),
                'market_daily_cap': float(scenario.market_daily_cap),
                'total_energy_consumed': float(total_energy_consumed),
                'energy_procured_total': float(procured_energy_total),
                'energy_procured_today': float(procured_energy_by_day[day_idx]),
                'energy_remaining_today': float(max(0.0, procured_energy_by_day[day_idx] - daily_energy_consumed[day_idx])),
                'cumulative_forecast_mid': float(cumulative_forecast_to_now),
                'cumulative_overuse': float(cumulative_overuse),
                'correction_strategy': correction_strategy,
                # storage state info
                'storage_soc': float(soc),
                'storage_priority': adjust.get('control_priority', 'none'),
                'stored_diff': float(adjust.get('stored_diff', 0.0)),
                'excess_diff': float(adjust.get('excess_diff', 0.0)),
                'realtime_price': float(realtime_price),
                'contract_price': float(contract_price),
                'realtime_pv': float(pv_rt) if pv_rt is not None else None,
            }

            # if this is the start of a day (hour 0,24,48) include the full day's forecast arrays
            if h % 24 == 0:
                day_idx = (h // 24) + 1
                start = (day_idx - 1) * 24
                end = start + 24
                # guard against out-of-range if scenario shorter
                end = min(end, scenario.hours)
                # full-day forecast min/max and pv mid values
                day_load_min = [float(x) for x in scenario.load_forecast_min[start:end]]
                day_load_max = [float(x) for x in scenario.load_forecast_max[start:end]]
                # pv mid (average of min/max)
                pv_mid = []
                for i in range(start, end):
                    pv_lo = float(scenario.pv_forecast_min[i])
                    pv_hi = float(scenario.pv_forecast_max[i])
                    pv_mid.append((pv_lo + pv_hi) / 2.0)

                event['day_index'] = int(day_idx)
                event['day_load_min'] = day_load_min
                event['day_load_max'] = day_load_max
                event['day_pv_mid'] = pv_mid
                event['day_market_hour_cap'] = [float(x) for x in market_hourly_cap[start:end]]

            try:
                publisher(event)
            except Exception:
                # ensure simulation continues even if publisher fails
                pass

        # update remaining purchased energy budget for the next hour
        market_remaining = max(0.0, float(procured_energy_total) - total_energy_consumed)

        # optional delay to pace events for realtime visualization
        if delay_seconds and delay_seconds > 0:
            import time
            time.sleep(delay_seconds)



if __name__ == "__main__":
    run_demo()


# ---------------------------------------------------------------------------
# New strategy demo: D-2/D-1/D day rolling plan with revenue optimisation
# ---------------------------------------------------------------------------

def run_strategy_demo(
    publisher=None,
    seed: int = 42,
    delay_seconds: float = 2.0,
    soc_init: float = 30.0,
    base_demand: float = 800.0,
    storage_feedback_getter=None,
    load_feedback_getter=None,
    realtime_pv_getter=None,
):
    """Run 72h strategy display flow:

    0-24h  : direct simulation and draw curves in forecast/real-time panel.
    25-72h : each hour re-dispatch before draw using completed actual load history,
             then execute 1-hour strategy result and draw.
    """
    import numpy as np
    import time

    rng = np.random.default_rng(seed)

    hours = 72

    # Build price / buy schedule for full 72h.
    prices = generate_price_schedule(
        n_hours=hours,
        base_demand=base_demand,
        seed=seed,
    )
    # Demo tuning: make the synthetic market profile profitable by default.
    prices.buy_qty = np.clip(prices.buy_qty * 0.78, 80.0, 3000.0)
    prices.sell_price = np.clip(np.maximum(prices.sell_price, prices.buy_price * 2.6), 0.05, 3.50)
    hod = np.arange(hours) % 24
    day_factor = np.array([0.96, 1.00, 1.04])
    day_idx = np.arange(hours) // 24

    demand_base = base_demand * (0.72 + 0.26 * np.sin(np.pi * hod / 12.0) + 0.12 * np.sin(2 * np.pi * hod / 24.0))
    estimated_demand = np.maximum(50.0, demand_base * day_factor[day_idx] * (1.0 + rng.normal(0, 0.03, hours)))

    pv_base = (base_demand * 0.82) * np.maximum(0.0, np.sin(np.pi * (hod - 6) / 12.0))
    pv_available = np.maximum(0.0, pv_base * (1.0 + rng.normal(0, 0.04, hours)))

    forecast_min = estimated_demand * 0.90
    forecast_max = estimated_demand * 1.10

    actual_done = np.zeros(hours)
    locked_actuals = []
    soc = float(soc_init)
    recent_bias = 0.0  # Rolling average bias to correct future demand toward forecast
    total_buy_cost = 0.0
    total_sell = 0.0
    total_penalty = 0.0
    battery_power_kw = 100.0
    charge_efficiency = 0.95
    discharge_efficiency = 0.95
    soc_min = 10.0
    soc_max = 100.0
    shortfall_guard_ratio = 0.06  # Conservative demand uplift for redispatch robustness.

    print("D-2 Purchase schedule generated: 72h")
    print(f"  buy_price  : min={float(np.min(prices.buy_price)):.3f}  max={float(np.max(prices.buy_price)):.3f}")
    print(f"  buy_qty    : min={float(np.min(prices.buy_qty)):.0f}   max={float(np.max(prices.buy_qty)):.0f}  kWh")
    print(f"  sell_price : min={float(np.min(prices.sell_price)):.3f} max={float(np.max(prices.sell_price)):.3f}")
    print("")
    print(f"D-1 estimated demand  : total={float(np.sum(estimated_demand[24:48])):.0f} kWh")
    print(f"D-day estimated demand: total={float(np.sum(estimated_demand[48:72])):.0f} kWh")
    print("=" * 60)
    print("Strategy display flow: 0-24h direct simulation, 25-72h hourly redispatch")
    print("=" * 60)

    for h in range(hours):
        if h == 0:
            print("=" * 60)
            print("Stage A: D-2 direct simulation (hours 0..23)")
            print("=" * 60)
        elif h == 24:
            print("  Stage B: D-1 hourly dispatch and 1h execution result (hours 24..47)")
            print("=" * 60)
        elif h == 48:
            print("  Stage C: D-day hourly dispatch and 1h execution result (hours 48..71)")
            print("=" * 60)

        realtime_price, contract_price = _price_profile_for_hour(h)
        pv_realtime = None
        pv_available_h = float(pv_available[h])
        hour_converged = False
        hour_iterations = 0
        if callable(realtime_pv_getter):
            pv_realtime = realtime_pv_getter(h, pv_available_h)
            if pv_realtime is not None:
                pv_available_h = float(max(0.0, pv_realtime))

        if h < 24:
            # Stage 1: historical control basis is assumed perfect for the first 24h.
            # Use market_hour_cap as actual load so the physical curve tracks the cap in 0-24h.
            load_adj = 0.0
            load_adj_requested = 0.0
            base_supply_h = float(prices.buy_qty[h]) + float(pv_available_h)
            demand_ref = float(prices.buy_qty[h])

            max_charge_now = min(
                float(battery_power_kw),
                max(0.0, (soc_max - soc) / max(charge_efficiency, 1e-6)),
            )
            max_discharge_now = min(
                float(battery_power_kw),
                max(0.0, (soc - soc_min) * discharge_efficiency),
            )
            deficit_h = max(0.0, demand_ref - base_supply_h)
            surplus_h = max(0.0, base_supply_h - demand_ref)

            # Prefer discharging to avoid shortfall; absorb surplus when possible.
            storage_discharge = min(deficit_h, max_discharge_now)
            storage_charge = min(surplus_h, max_charge_now)
            pv_curtail = 0.0
            demand_exec = demand_ref
            status = "DIRECT"
            reason = "stage_0_24_direct_perfect_midpoint"
        else:
            # Stage 2: before drawing each hour, re-dispatch using completed actual history.
            # In 25-72h, compute rolling average of recent demand bias and use to correct forecast.
            if h >= 24:
                # Look back up to 6 hours (instead of 4) to estimate systematic bias more reliably
                lookback = min(6, h - 24)
                if lookback > 0:
                    bias_sum = 0.0
                    for i in range(lookback):
                        prev_h = h - lookback + i
                        prev_actual = float(actual_done[prev_h])
                        prev_est = float(estimated_demand[prev_h])
                        bias_sum += (prev_actual - prev_est) / max(1.0, prev_est)
                    recent_bias = bias_sum / float(lookback)
                else:
                    recent_bias = 0.0

            est_for_dispatch = estimated_demand.copy()
            guard_end = min(hours, h + 6)
            est_for_dispatch[h:guard_end] = est_for_dispatch[h:guard_end] * (1.0 + shortfall_guard_ratio)

            plan = optimize_dispatch(
                prices=prices,
                estimated_demand=est_for_dispatch,
                pv_available=np.where(np.arange(hours) == h, pv_available_h, pv_available),
                soc_init=soc,
                actuals=locked_actuals,
                horizon_start=h,
                soc_min=soc_min,
                soc_max=soc_max,
                battery_power_kw=battery_power_kw,
                load_min_ratio=0.80,
                load_max_ratio=1.10,
                penalty_per_kwh=6.0,
                charge_efficiency=charge_efficiency,
                discharge_efficiency=discharge_efficiency,
            )
            load_adj = float(plan.load_adj[h])
            load_adj_requested = float(load_adj)
            storage_target_plan = float(plan.storage_charge[h] - plan.storage_discharge[h])
            storage_target_plan, storage_guidance = _apply_lookahead_storage_policy(
                hour=h,
                hours=hours,
                estimated_demand=estimated_demand,
                prices=prices,
                plan=plan,
                current_soc=soc,
                battery_power_kw=battery_power_kw,
                charge_efficiency=charge_efficiency,
                soc_max=soc_max,
                actual_done=actual_done,
            )
            storage_charge = max(0.0, float(storage_target_plan))
            storage_discharge = max(0.0, float(-storage_target_plan))
            pv_curtail = float(plan.pv_curtail[h])

            # Load-control executable feedback overrides planned load adjustment.
            if callable(load_feedback_getter):
                fb_load = load_feedback_getter(h, load_adj_requested)
                if fb_load is not None:
                    load_adj = float(fb_load)

            # Further reduce noise in 25-72h (0.005 for even better forecast match) and apply bias correction
            noise_scale = 0.005
            demand_base_adjusted = (estimated_demand[h] + load_adj) * (1.0 + recent_bias)
            demand_exec = max(0.0, float(demand_base_adjusted * (1.0 + rng.normal(0, noise_scale))))
            status = str(plan.milp_status)
            reason = f"stage_25_72_hourly_redispatch:{storage_guidance['reason']}"
            hour_converged = str(plan.milp_status) == "Optimal"
            hour_iterations = int(plan.iterations) if int(getattr(plan, "iterations", 0)) > 0 else 1

        if h < 24:
            storage_guidance = {
                "enabled": False,
                "lookahead_hour": None,
                "forecast_gap": 0.0,
                "recent_actual_gap": 0.0,
                "effective_gap": 0.0,
                "projected_soc": float(soc),
                "storage_target_base": 0.0,
                "storage_target_adjusted": 0.0,
                "reason": "stage_0_24_direct",
            }

        pv_used = max(0.0, float(pv_available_h - pv_curtail))
        storage_target_requested = storage_charge - storage_discharge  # positive=charge, negative=discharge
        storage_target = storage_target_requested
        if callable(storage_feedback_getter):
            fb = storage_feedback_getter(h, storage_target_requested)
            if fb is not None:
                storage_target = float(fb)

        # Enforce executable storage bounds from current SoC to avoid overflow/over-discharge.
        max_charge_exec = max(0.0, (soc_max - soc) / max(charge_efficiency, 1e-6))
        max_discharge_exec = max(0.0, (soc - soc_min) * discharge_efficiency)
        storage_target = float(np.clip(storage_target, -max_discharge_exec, max_charge_exec))

        # Convert signed storage target to effective charge/discharge used by execution.
        storage_charge_eff = max(0.0, float(storage_target))
        storage_discharge_eff = max(0.0, float(-storage_target))

        net_supply = float(prices.buy_qty[h]) + pv_used + storage_discharge_eff - storage_charge_eff
        sold_back = max(0.0, net_supply - demand_exec)
        shortfall = max(0.0, demand_exec - net_supply)

        # Real-time recourse: if shortfall remains, discharge extra energy within executable limits.
        if shortfall > 0.0:
            max_discharge_by_soc = max(0.0, (soc - soc_min) * discharge_efficiency)
            remaining_discharge_headroom = max(0.0, min(float(battery_power_kw), max_discharge_by_soc) - storage_discharge_eff)
            emergency_discharge = min(shortfall, remaining_discharge_headroom)
            if emergency_discharge > 0.0:
                storage_discharge_eff += float(emergency_discharge)
                net_supply = float(prices.buy_qty[h]) + pv_used + storage_discharge_eff - storage_charge_eff
                sold_back = max(0.0, net_supply - demand_exec)
                shortfall = max(0.0, demand_exec - net_supply)

        buy_cost_h = float(prices.buy_price[h] * prices.buy_qty[h])
        sell_h = float(prices.sell_price[h] * sold_back)
        penalty_h = 2.0 * shortfall

        # SoC hard cap at 100%. If execution drives SoC above 100, penalize overflow.
        soc_next_raw = soc + storage_charge_eff * charge_efficiency - storage_discharge_eff / discharge_efficiency
        soc_excess = max(0.0, soc_next_raw - soc_max)
        penalty_soc_h = float(realtime_price) * soc_excess * 1.1

        total_buy_cost += buy_cost_h
        total_sell += sell_h
        total_penalty += penalty_h + penalty_soc_h
        net_h = float(sell_h - buy_cost_h - penalty_h - penalty_soc_h)

        soc = max(soc_min, min(soc_max, soc_next_raw))
        if h == 23:
            # At the end of the first 24h, enforce a fixed 30% pre-stock for next stage.
            soc = 30.0
        actual_done[h] = demand_exec
        locked_actuals.append(HourlyActual(hour_index=h, actual_demand=float(demand_exec)))
        estimated_demand[h] = demand_exec  # keep horizon aligned to completed actual load

        if h < 24:
            print(
                f"  D-2 h={h:02d} | demand={float(demand_exec):.1f} pv={float(pv_used):.1f} "
                f"sold={float(sold_back):.1f} shortfall={float(shortfall):.1f} net={net_h:+.2f}"
            )
        elif h < 48:
            print(
                f"  D-1 h={h:02d} | dispatch(la={float(load_adj):+.1f}, sc={float(storage_charge_eff):.1f}, "
                f"sd={float(storage_discharge_eff):.1f}, pc={float(pv_curtail):.1f}) -> "
                f"run(demand={float(demand_exec):.1f}, sold={float(sold_back):.1f}, shortfall={float(shortfall):.1f}) "
                f"net={net_h:+.2f}"
            )
        else:
            print(
                f"  D-day h={h:02d} | dispatch(la={float(load_adj):+.1f}, sc={float(storage_charge_eff):.1f}, "
                f"sd={float(storage_discharge_eff):.1f}, pc={float(pv_curtail):.1f}) -> "
                f"run(demand={float(demand_exec):.1f}, sold={float(sold_back):.1f}, shortfall={float(shortfall):.1f}) "
                f"net={net_h:+.2f}"
            )

        if storage_guidance["enabled"] and storage_guidance["reason"] != "keep_milp_storage_target":
            print(
                f"    lookahead h+24={int(storage_guidance['lookahead_hour']):02d} "
                f"gap={float(storage_guidance['effective_gap']):+.1f} "
                f"soc_proj={float(storage_guidance['projected_soc']):.1f} "
                f"storage={float(storage_guidance['storage_target_base']):+.1f}->{float(storage_guidance['storage_target_adjusted']):+.1f} "
                f"reason={storage_guidance['reason']}"
            )

        event = {
            "type": "strategy_hour",
            "hour": int(h),
            "stage": "direct" if h < 24 else "redispatch",
            "load_adj": float(load_adj),
            "load_adjust_requested": float(load_adj_requested),
            "load_adjust_estimated": float(load_adj),
            "storage_charge": float(storage_charge_eff),
            "storage_discharge": float(storage_discharge_eff),
            "pv_curtail": float(pv_curtail),
            "pv_min": float(max(0.0, pv_used * 0.95)),
            "pv_max": float(pv_used * 1.05),
            "load_forecast_min": float(forecast_min[h]),
            "load_forecast_max": float(forecast_max[h]),
            "actual_total": float(demand_exec),
            "achieved_total": float(demand_exec),
            "control_status": status,
            "adjust_reason": reason,
            "load_targets": [float(demand_exec)],
            "storage_target": float(storage_target),
            "storage_target_requested": float(storage_target_requested),
            "storage_target_base": float(storage_guidance["storage_target_base"]),
            "storage_guidance_reason": str(storage_guidance["reason"]),
            "storage_lookahead_hour": storage_guidance["lookahead_hour"],
            "storage_lookahead_forecast_gap": float(storage_guidance["forecast_gap"]),
            "storage_recent_actual_gap": float(storage_guidance["recent_actual_gap"]),
            "storage_effective_gap": float(storage_guidance["effective_gap"]),
            "storage_projected_soc": float(storage_guidance["projected_soc"]),
            "storage_soc": float(soc),
            "storage_priority": "none" if h < 24 else "storage",
            "stored_diff": float(max(0.0, storage_charge_eff)),
            "excess_diff": float(max(0.0, storage_discharge_eff)),
            "market_daily_cap": float(np.sum(estimated_demand[(h // 24) * 24 : (h // 24 + 1) * 24])),
            "market_hourly_cap": float(prices.buy_qty[h]),
            "energy_procured_total": float(np.sum(prices.buy_qty[: hours])),
            "energy_procured_today": float(np.sum(prices.buy_qty[(h // 24) * 24 : (h // 24 + 1) * 24])),
            "energy_remaining_today": float(max(0.0, np.sum(prices.buy_qty[(h // 24) * 24 : (h // 24 + 1) * 24]) - np.sum(actual_done[(h // 24) * 24 : h + 1]))),
            "total_energy_consumed": float(np.sum(actual_done[: h + 1])),
            "cumulative_forecast_mid": float(np.sum((forecast_min[: h + 1] + forecast_max[: h + 1]) / 2.0)),
            "cumulative_overuse": float(max(0.0, np.sum(actual_done[: h + 1]) - np.sum((forecast_min[: h + 1] + forecast_max[: h + 1]) / 2.0))),
            "buy_price": float(prices.buy_price[h]),
            "buy_qty": float(prices.buy_qty[h]),
            "sell_price": float(prices.sell_price[h]),
            "realtime_price": float(realtime_price),
            "contract_price": float(contract_price),
            "realtime_pv": float(pv_available_h),
            "sell_revenue_hour": float(sell_h),
            "penalty_shortfall_hour": float(penalty_h),
            "penalty_soc_hour": float(penalty_soc_h),
            "soc_excess_energy": float(soc_excess),
            "buy_cost": float(total_buy_cost),
            "sell_revenue": float(total_sell),
            "penalty": float(total_penalty),
            "net_profit": float(total_sell - total_buy_cost - total_penalty),
            "converged": bool(hour_converged),
            "iterations": int(hour_iterations),
        }

        if h % 24 == 0:
            di = (h // 24) + 1
            s = (di - 1) * 24
            e = min(s + 24, hours)
            event["day_index"] = int(di)
            event["day_load_min"] = [float(x) for x in forecast_min[s:e]]
            event["day_load_max"] = [float(x) for x in forecast_max[s:e]]
            event["day_pv_mid"] = [float(x) for x in pv_available[s:e]]
            event["day_market_hour_cap"] = [float(x) for x in prices.buy_qty[s:e]]

        if publisher:
            try:
                publisher(event)
            except Exception:
                pass

        if delay_seconds and delay_seconds > 0:
            time.sleep(delay_seconds)

    print(f"\nTotal strategy hours executed: {hours}")
    print(f"Net profit: {total_sell - total_buy_cost - total_penalty:.2f} yuan")
    return []

