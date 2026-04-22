"""Simulator: generate forecasts, actuals and simulate execution feedback."""
import numpy as np
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Scenario:
    hours: int
    n_load_groups: int
    pv_forecast_min: np.ndarray  # size hours
    pv_forecast_max: np.ndarray
    load_forecast_min: np.ndarray
    load_forecast_max: np.ndarray
    market_daily_cap: float
    market_hourly_cap: Optional[np.ndarray]
    actual_loads: np.ndarray  # shape (hours, n_load_groups)


def _clamp_hourly_actual_ratio(
    actual_loads: np.ndarray,
    load_min: np.ndarray,
    load_max: np.ndarray,
    low_ratio: float = 0.8,
    high_ratio: float = 1.2,
) -> np.ndarray:
    """Clamp each hour's actual total to [low_ratio, high_ratio] * forecast_mid.

    Keeps per-hour group composition by proportional scaling.
    """
    hours = int(actual_loads.shape[0])
    for h in range(hours):
        forecast_mid = float((load_min[h] + load_max[h]) / 2.0)
        if forecast_mid <= 0:
            continue

        actual_total = float(np.sum(actual_loads[h]))
        lower = low_ratio * forecast_mid
        upper = high_ratio * forecast_mid
        clamped_total = min(max(actual_total, lower), upper)

        if actual_total > 0 and abs(clamped_total - actual_total) > 1e-9:
            actual_loads[h] = actual_loads[h] * (clamped_total / actual_total)

    return actual_loads


def _enforce_daily_actual_floor(
    actual_loads: np.ndarray,
    load_min: np.ndarray,
    load_max: np.ndarray,
    min_daily_ratio: float = 0.98,
    hourly_high_ratio: float = 1.2,
) -> np.ndarray:
    """Ensure daily actual total >= min_daily_ratio * daily forecast-mid total.

    Increases hours with available headroom first, while respecting hourly upper bound.
    """
    hours, n_groups = actual_loads.shape
    n_days = (hours + 23) // 24

    for d in range(n_days):
        s = d * 24
        e = min((d + 1) * 24, hours)

        day_mid = (load_min[s:e] + load_max[s:e]) / 2.0
        day_target = float(min_daily_ratio * np.sum(day_mid))
        if day_target <= 0:
            continue

        day_totals = np.sum(actual_loads[s:e], axis=1)
        day_actual = float(np.sum(day_totals))
        if day_actual >= day_target:
            continue

        gap = day_target - day_actual
        headroom = np.maximum(hourly_high_ratio * day_mid - day_totals, 0.0)
        headroom_sum = float(np.sum(headroom))
        if headroom_sum <= 0:
            continue

        # Allocate gap by available hourly headroom.
        add = np.minimum(headroom, gap * (headroom / headroom_sum))
        leftover = gap - float(np.sum(add))

        # Greedy fill any remaining gap into hours that still have headroom.
        if leftover > 1e-9:
            for i in np.argsort(-headroom):
                room = float(headroom[i] - add[i])
                if room <= 1e-9:
                    continue
                extra = min(room, leftover)
                add[i] += extra
                leftover -= extra
                if leftover <= 1e-9:
                    break

        # Apply per-hour additions by proportional group scaling.
        for i in range(e - s):
            inc = float(add[i])
            if inc <= 0:
                continue
            hidx = s + i
            old_total = float(np.sum(actual_loads[hidx]))
            new_total = old_total + inc
            if old_total > 0:
                actual_loads[hidx] = actual_loads[hidx] * (new_total / old_total)
            else:
                actual_loads[hidx] = np.full(n_groups, new_total / n_groups, dtype=float)

    return actual_loads


def scenario_from_input(payload: Dict[str, Any]) -> Scenario:
    """Build Scenario from API input payload.

    Expected fields:
    - pv_forecast_min: list[float] length = hours
    - pv_forecast_max: list[float] length = hours
    - load_forecast_min: list[float] length = hours
    - load_forecast_max: list[float] length = hours
    - actual_loads: list[list[float]] shape = (hours, n_load_groups)
    Optional fields:
    - market_daily_cap: float
    - market_hourly_cap: list[float] length = hours
    """
    required = [
        "pv_forecast_min",
        "pv_forecast_max",
        "load_forecast_min",
        "load_forecast_max",
        "actual_loads",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    pv_min = np.asarray(payload["pv_forecast_min"], dtype=float)
    pv_max = np.asarray(payload["pv_forecast_max"], dtype=float)
    load_min = np.asarray(payload["load_forecast_min"], dtype=float)
    load_max = np.asarray(payload["load_forecast_max"], dtype=float)
    actual_loads = np.asarray(payload["actual_loads"], dtype=float)

    if actual_loads.ndim != 2:
        raise ValueError("actual_loads must be a 2D array of shape (hours, n_load_groups)")

    hours = int(actual_loads.shape[0])
    n_load_groups = int(actual_loads.shape[1])

    if hours <= 0 or n_load_groups <= 0:
        raise ValueError("hours and n_load_groups must be positive")

    for name, arr in [
        ("pv_forecast_min", pv_min),
        ("pv_forecast_max", pv_max),
        ("load_forecast_min", load_min),
        ("load_forecast_max", load_max),
    ]:
        if arr.ndim != 1 or arr.shape[0] != hours:
            raise ValueError(f"{name} must be a 1D array with length equal to hours={hours}")

    # Enforce hourly and daily constraints for injected actuals.
    # 1) each hour within +/-20% of forecast midpoint
    # 2) each day actual total >= 98% of daily forecast-mid total
    actual_loads = _clamp_hourly_actual_ratio(actual_loads, load_min, load_max, 0.8, 1.2)
    actual_loads = _enforce_daily_actual_floor(actual_loads, load_min, load_max, 0.98, 1.2)

    forecast_mid = (load_min + load_max) / 2.0

    market_hourly_cap_raw = payload.get("market_hourly_cap")
    market_daily_cap_raw = payload.get("market_daily_cap")

    if market_hourly_cap_raw is not None:
        market_hourly_cap = np.asarray(market_hourly_cap_raw, dtype=float)
        if market_hourly_cap.ndim != 1 or market_hourly_cap.shape[0] != hours:
            raise ValueError(f"market_hourly_cap must be a 1D array with length equal to hours={hours}")
        market_hourly_cap = np.clip(market_hourly_cap, 0.0, None)
        # Keep legacy field as day-1 sum for compatibility with existing consumers.
        market_daily_cap = float(np.sum(market_hourly_cap[: min(24, hours)]))
    else:
        # Backward compatibility: derive hourly cap from daily cap proportionally by forecast-mid.
        if market_daily_cap_raw is None:
            market_daily_cap = float(np.sum(load_max[: min(24, hours)]))
        else:
            market_daily_cap = float(market_daily_cap_raw)

        market_hourly_cap = np.zeros(hours, dtype=float)
        n_days = (hours + 23) // 24
        for d in range(n_days):
            s = d * 24
            e = min((d + 1) * 24, hours)
            day_mid = forecast_mid[s:e]
            day_sum = float(np.sum(day_mid))
            if day_sum > 0:
                market_hourly_cap[s:e] = day_mid / day_sum * market_daily_cap
            else:
                market_hourly_cap[s:e] = market_daily_cap / max(1, e - s)

    return Scenario(
        hours=hours,
        n_load_groups=n_load_groups,
        pv_forecast_min=pv_min,
        pv_forecast_max=pv_max,
        load_forecast_min=load_min,
        load_forecast_max=load_max,
        market_daily_cap=market_daily_cap,
        market_hourly_cap=market_hourly_cap,
        actual_loads=actual_loads,
    )


def generate_scenario(hours=24, n_load_groups=3, seed=0) -> Scenario:
    rng = np.random.RandomState(seed)
    n_days = (hours + 23) // 24

    # Per-day multipliers so each day's profile differs.
    day_load_factors = np.clip(rng.normal(loc=1.0, scale=0.06, size=n_days), 0.88, 1.12)
    day_pv_factors = np.clip(rng.normal(loc=1.0, scale=0.10, size=n_days), 0.80, 1.20)
    day_actual_bias = np.clip(rng.normal(loc=1.0, scale=0.03, size=n_days), 0.94, 1.06)

    # PV forecasts (per hour) in kW.
    # Daily profile: 00:00-05:00 and 20:00-24:00 -> 0,
    # 06:00-14:00 rising, 15:00-20:00 falling.
    base_pv = np.zeros(hours, dtype=float)
    pv_uncert = np.zeros(hours, dtype=float)
    pv_peak = 260.0
    daylight_uncert = 20.0

    for h in range(hours):
        hod = h % 24
        day_idx = h // 24
        if 0 <= hod <= 5 or hod >= 20:
            pv_mid = 0.0
        elif 6 <= hod <= 14:
            pv_mid = (hod - 6) / (14 - 6) * pv_peak
        else:  # 15 <= hod <= 19
            pv_mid = (20 - hod) / (20 - 14) * pv_peak

        # Apply day-level PV variation.
        pv_mid *= day_pv_factors[day_idx]

        # Keep some stochasticity while preserving the requested trend.
        if pv_mid > 0:
            pv_mid = max(0.0, pv_mid + rng.normal(0, 8.0))
            uncert = max(5.0, daylight_uncert + rng.normal(0, 3.0))
        else:
            uncert = 0.0

        base_pv[h] = pv_mid
        pv_uncert[h] = uncert

    pv_min = np.maximum(base_pv - pv_uncert, 0)
    pv_max = base_pv + pv_uncert

    # Load forecasts (hourly) total across groups in kW.
    # Keep forecast interval fluctuation below 10% (here target ~9%).
    base_load = np.zeros(hours, dtype=float)
    load_uncert = np.zeros(hours, dtype=float)
    for h in range(hours):
        hod = h % 24
        day_idx = h // 24

        # Requested load-forecast profile:
        # 00:00-06:00 low valley, 22:00-24:00 low valley,
        # and a higher peak window at 14:00-18:00.
        if 0 <= hod <= 5:
            # Deep night valley that gradually declines.
            day_shape = 0.86 - 0.02 * hod
        elif 6 <= hod <= 13:
            # Morning recovery toward normal level.
            day_shape = 0.78 + (hod - 6) / (13 - 6) * (1.00 - 0.78)
        elif 14 <= hod <= 18:
            # Elevated afternoon peak (max around 16:00).
            day_shape = 1.08 + 0.08 * (1.0 - abs(hod - 16) / 2.0)
        elif 19 <= hod <= 21:
            # Post-peak decline.
            day_shape = 0.98 - 0.03 * (hod - 19)
        else:  # 22 <= hod <= 23
            # Late-night drop.
            day_shape = 0.74 - 0.03 * (hod - 22)

        noise = rng.normal(0, 0.01)
        load_mid = max(300.0, 920.0 * day_shape * day_load_factors[day_idx] * (1.0 + noise))
        base_load[h] = load_mid
        load_uncert[h] = load_mid * 0.045  # total band ~= 9%

    load_min = np.maximum(base_load - load_uncert, 0)
    load_max = base_load + load_uncert

    # Split forecasts into groups by proportion
    proportions = rng.dirichlet(alpha=np.ones(n_load_groups))

    # Actual per group: keep actual total within about +/-3% around forecast mid.
    actual_loads = np.zeros((hours, n_load_groups))
    for h in range(hours):
        day_idx = h // 24
        hod = h % 24
        forecast_mid = (load_min[h] + load_max[h]) / 2

        # Requirement: around 14:00 actual load can exceed forecast by >15%.
        if hod == 14:
            actual_ratio = np.clip(rng.normal(loc=1.16, scale=0.015), 1.15, 1.20)
        else:
            # Reduced noise (0.01 instead of 0.02) and tighter range (0.96-1.04) for better forecast match
            actual_ratio = np.clip(rng.normal(loc=day_actual_bias[day_idx], scale=0.01), 0.96, 1.04)

        total = forecast_mid * actual_ratio
        # distribute
        group_vals = total * proportions * rng.normal(1.0, 0.1, size=n_load_groups)
        group_vals = np.clip(group_vals, 0, None)
        gsum = float(np.sum(group_vals))
        if gsum > 0:
            group_vals = group_vals * (total / gsum)
        actual_loads[h] = group_vals

    # Make day1/day2 physical actuals close to day3 (hour-by-hour), when day3 exists.
    # This preserves group proportions by proportional scaling and adds small jitter.
    if n_days >= 3:
        ref_day = 2  # day3 in 0-based index
        for d in (0, 1):
            for hod in range(24):
                src_idx = d * 24 + hod
                ref_idx = ref_day * 24 + hod
                if src_idx >= hours or ref_idx >= hours:
                    continue

                ref_total = float(np.sum(actual_loads[ref_idx]))
                if ref_total <= 0:
                    continue

                target_total = ref_total * float(np.clip(rng.normal(loc=1.0, scale=0.015), 0.97, 1.03))
                src_total = float(np.sum(actual_loads[src_idx]))
                if src_total > 0:
                    actual_loads[src_idx] = actual_loads[src_idx] * (target_total / src_total)
                else:
                    actual_loads[src_idx] = np.full(n_load_groups, target_total / n_load_groups, dtype=float)

    # Apply global consistency constraints for generated scenario as well.
    actual_loads = _clamp_hourly_actual_ratio(actual_loads, load_min, load_max, 0.8, 1.2)
    actual_loads = _enforce_daily_actual_floor(actual_loads, load_min, load_max, 0.98, 1.2)

    # D-2 determined hourly market cap (kWh per hour), with daily aggregate retained for compatibility.
    forecast_mid = (load_min + load_max) / 2.0
    market_hourly_cap = np.clip(forecast_mid * 0.92, 0.0, None)
    market_daily_cap = float(np.sum(market_hourly_cap[: min(24, hours)]))

    return Scenario(
        hours=hours,
        n_load_groups=n_load_groups,
        pv_forecast_min=pv_min,
        pv_forecast_max=pv_max,
        load_forecast_min=load_min,
        load_forecast_max=load_max,
        market_daily_cap=market_daily_cap,
        market_hourly_cap=market_hourly_cap,
        actual_loads=actual_loads,
    )


def simulate_execution(control_targets: Dict, actual_loads_hour: np.ndarray) -> Dict:
    """Simulate applying control targets to actual loads and return new actuals and a feedback report.

    control_targets: {
        'load_targets': array of target load per group (kW),
        'storage_target': charge(+)/discharge(-) kW
    }
    """
    # apply load targets by scaling actuals towards target (simple proportional controller)
    load_targets = control_targets.get("load_targets")
    if load_targets is None:
        load_targets = actual_loads_hour

    # simulate achieved = actual + alpha*(target - actual)
    alpha = 0.7
    achieved = actual_loads_hour + alpha * (load_targets - actual_loads_hour)

    # storage: we just report that storage acted as commanded
    storage_action = control_targets.get("storage_target", 0.0)

    feedback = {
        "achieved_loads": achieved,
        "storage_action": storage_action,
    }
    return feedback
