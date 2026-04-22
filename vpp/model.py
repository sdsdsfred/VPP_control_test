"""MILP model for VPP scheduling using pulp."""
import pulp
import numpy as np
from typing import Dict


def optimize_hourly(
    hour_index: int,
    pv_min: float,
    pv_max: float,
    load_forecast_min: float,
    load_forecast_max: float,
    market_cap_remaining: float,
    actual_loads_hour: np.ndarray,
    soc: float,
    soc_min: float = 0.0,
    soc_max: float = 100.0,
    battery_power_cap: float = 200.0,
):
    """Build and solve a simple MILP for one hour.

    Returns control targets: load_targets (per group) and storage_target (kW, positive=charge)
    """
    n_groups = len(actual_loads_hour)
    prob = pulp.LpProblem(f"vpp_hour_{hour_index}", pulp.LpMinimize)

    # Decision vars: load for each group (kW), bounded 0..some large number
    load_vars = [pulp.LpVariable(f"load_{i}", lowBound=0) for i in range(n_groups)]

    # Storage action: positive = charge (consume), negative = discharge (supply back)
    s = pulp.LpVariable("storage", lowBound=-battery_power_cap, upBound=battery_power_cap)

    # Objective: minimize absolute deviation from actual (to reduce control aggression)
    # plus penalty for violating forecast bounds.
    dev_pos = [pulp.LpVariable(f"dev_pos_{i}", lowBound=0) for i in range(n_groups)]
    dev_neg = [pulp.LpVariable(f"dev_neg_{i}", lowBound=0) for i in range(n_groups)]

    # load_i - actual_i = dev_pos_i - dev_neg_i, so |load_i - actual_i| = dev_pos_i + dev_neg_i
    for i in range(n_groups):
        prob += load_vars[i] - float(actual_loads_hour[i]) == dev_pos[i] - dev_neg[i]

    deviation_cost = pulp.lpSum(dev_pos) + pulp.lpSum(dev_neg)

    # Penalty for exceeding forecast bounds (soft constraints via slack variables)
    slack_pos = pulp.LpVariable("slack_pos", lowBound=0)
    slack_neg = pulp.LpVariable("slack_neg", lowBound=0)

    prob += 1.0 * deviation_cost + 100.0 * (slack_pos + slack_neg)

    # Constraints:
    # total load across groups plus charging must be between forecast min/max +/- slack
    total_load = pulp.lpSum(load_vars)
    prob += total_load + s >= load_forecast_min - slack_neg
    prob += total_load + s <= load_forecast_max + slack_pos

    # PV limits: assume PV reduces net load, but here we only ensure we don't ask for negative net demand beyond PV max
    # (This is a simplified coupling; in detailed models you'd model net demand = load - pv)
    prob += total_load >= 0

    # Market daily cap: enforce per-hour budget simplification by ensuring we don't exceed remaining cap for this hour
    # (Simplified: consume at most market_cap_remaining in this hour)
    prob += total_load <= market_cap_remaining

    # Load-control safety bound: hourly load-target should not exceed forecast max by more than 10%.
    prob += total_load <= 1.10 * load_forecast_max

    # Solve
    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    load_targets = np.array([v.value() if v.value() is not None else actual_loads_hour[i] for i, v in enumerate(load_vars)])
    storage_target = s.value() if s.value() is not None else 0.0

    return {"load_targets": load_targets, "storage_target": storage_target, "status": pulp.LpStatus[prob.status]}
