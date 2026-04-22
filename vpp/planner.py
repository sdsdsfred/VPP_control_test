"""VPP D-2/D-1/D day planning with revenue-maximizing MILP and rolling re-plan.

Decision flow
-------------
D-2  : fix purchase schedule for D-day — buy_price[h], buy_qty[h] per hour (48h horizon).
D-1 23:00 : initial MILP dispatch plan for D-day (24h), output per-hour targets for
            load adjustment, storage charge/discharge, and PV curtailment.
D-1 rolling : after each actual D-1 hour passes, replan with locked actuals and update
              remaining-horizon targets.  Repeat MILP → execute → revenue until convergence.
D-day : execute final optimized plan.

Optimization objective (per planning call)
------------------------------------------
  maximize  Σ_h [ sell_price[h] * sold_back[h] ]
          - Σ_h [ buy_price[h]  * buy_qty[h]   ]   (sunk cost, included for reporting)
          - Σ_h [ PENALTY * shortfall[h]        ]

Energy balance (per hour h, 1-hour interval, all in kWh/kW·h)
--------------------------------------------------------------
  buy_qty[h] + pv_used[h] + s_discharge[h]
      = actual_demand[h] + s_charge[h] + sold_back[h] - shortfall[h]

where:
  actual_demand[h] = estimated_demand[h] + load_adj[h]
  pv_used[h]       = pv_available[h] - pv_curtail[h]

Rules encoded by constraints
-----------------------------
  If estimated[h] > buy_qty[h]  → optimizer prefers discharge or reduce load (avoids penalty)
  If estimated[h] <= buy_qty[h] → optimizer prefers charge storage or sell back (maximises revenue)

SoC continuity
--------------
  soc[h+1] = soc[h] + s_charge[h] * charge_eff - s_discharge[h] / discharge_eff
  soc_min <= soc[h] <= soc_max
"""

from __future__ import annotations

import numpy as np
import pulp
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PriceSchedule:
    """Hourly price and purchase schedule fixed on D-2."""
    hours: int
    buy_price: np.ndarray    # yuan/kWh, shape (hours,)
    buy_qty: np.ndarray      # kWh contracted per hour, shape (hours,)
    sell_price: np.ndarray   # yuan/kWh sell-back, shape (hours,)


@dataclass
class DispatchPlan:
    """Optimal per-hour dispatch targets returned by MILP."""
    hours: int
    load_adj: np.ndarray           # kW adjustment (+ increase, – decrease), shape (hours,)
    storage_charge: np.ndarray     # kW charged (>= 0), shape (hours,)
    storage_discharge: np.ndarray  # kW discharged (>= 0), shape (hours,)
    pv_curtail: np.ndarray         # kW PV curtailed, shape (hours,)
    sold_back: np.ndarray          # kWh sold back to grid, shape (hours,)
    shortfall: np.ndarray          # kWh shortfall (ideally 0), shape (hours,)
    soc_trajectory: np.ndarray     # SoC kWh at end of each hour, shape (hours,)
    buy_cost: float                # total purchase cost (sunk, yuan)
    sell_revenue: float            # projected sell-back revenue (yuan)
    penalty: float                 # shortfall penalty (yuan)
    net_profit: float              # sell_revenue - buy_cost - penalty (yuan)
    converged: bool = False
    iterations: int = 0
    milp_status: str = ""


@dataclass
class HourlyActual:
    """Recorded actual for a single hour (used while locking D-1 knowns)."""
    hour_index: int        # index within the planning horizon (0-based)
    actual_demand: float   # kWh actual metered demand


# ---------------------------------------------------------------------------
# D-2 purchase schedule generator
# ---------------------------------------------------------------------------

def generate_price_schedule(
    n_hours: int = 48,
    base_buy_price: float = 0.65,
    base_sell_price: float = 0.40,
    base_demand: float = 800.0,
    seed: Optional[int] = None,
) -> PriceSchedule:
    """Simulate a D-2 purchase schedule with time-of-use pricing.

    Uses a 24h TOU structure (peak / shoulder / valley) repeated over n_hours.
    n_hours=48 covers D-1 + D day (0..47h).
    """
    rng = np.random.default_rng(seed)

    buy_price = np.zeros(n_hours)
    sell_price = np.zeros(n_hours)
    buy_qty = np.zeros(n_hours)

    for h in range(n_hours):
        hod = h % 24  # hour-of-day
        # TOU tier
        if hod in range(8, 12) or hod in range(18, 22):
            tou_buy = 1.30   # peak
        elif hod < 8 or hod >= 23:
            tou_buy = 0.70   # valley
        else:
            tou_buy = 1.00   # shoulder

        buy_price[h] = base_buy_price * tou_buy * (1 + rng.normal(0, 0.02))
        # Sell price set to 1.1x of buy price (forecast price)
        sell_price[h] = buy_price[h] * 1.1

        # Typical demand curve (sinusoidal daily pattern)
        demand_factor = 0.8 + 0.4 * np.sin(np.pi * hod / 12.0)
        buy_qty[h] = base_demand * demand_factor * (1 + rng.normal(0, 0.03))

    return PriceSchedule(
        hours=n_hours,
        buy_price=np.clip(buy_price,  0.10, 2.00),
        buy_qty=np.clip(buy_qty,      100.0, 3000.0),
        sell_price=np.clip(sell_price, 0.05, 1.50),
    )


# ---------------------------------------------------------------------------
# Core MILP optimiser
# ---------------------------------------------------------------------------

def optimize_dispatch(
    prices: PriceSchedule,
    estimated_demand: np.ndarray,       # kWh estimated per hour, shape (hours,)
    pv_available: np.ndarray,           # kWh PV available per hour, shape (hours,)
    soc_init: float,                    # kWh initial SoC
    soc_min: float = 10.0,              # kWh minimum SoC
    soc_max: float = 100.0,             # kWh battery capacity upper bound
    battery_power_kw: float = 100.0,    # kW max charge/discharge rate
    load_min_ratio: float = 0.80,       # min actual_demand = estimated * ratio
    load_max_ratio: float = 1.10,       # max actual_demand = estimated * ratio
    penalty_per_kwh: float = 2.0,       # yuan/kWh shortfall penalty
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
    actuals: Optional[List[HourlyActual]] = None,  # locked known actual hours
    horizon_start: int = 0,             # first free (optimisable) hour index
) -> DispatchPlan:
    """MILP: maximise (sell_revenue - buy_cost - shortfall_penalty) over all hours.

    Hours with index < horizon_start that appear in `actuals` have their load_adj
    locked to match the known actual demand.

    Returns a DispatchPlan with per-hour targets and projected financials.
    """
    h_total = len(estimated_demand)
    assert len(pv_available) == h_total, "pv_available must match estimated_demand length"
    assert prices.hours >= h_total, "PriceSchedule must cover all planning hours"

    prob = pulp.LpProblem("vpp_revenue", pulp.LpMaximize)

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    # load_adj: built after actuals_map so locked hours can use wider bounds
    s_charge    = [pulp.LpVariable(f"sc_{h}", lowBound=0, upBound=battery_power_kw) for h in range(h_total)]
    s_discharge = [pulp.LpVariable(f"sd_{h}", lowBound=0, upBound=battery_power_kw) for h in range(h_total)]
    pv_curtail  = [pulp.LpVariable(f"pc_{h}", lowBound=0, upBound=float(pv_available[h])) for h in range(h_total)]
    sold_back   = [pulp.LpVariable(f"sb_{h}", lowBound=0) for h in range(h_total)]
    shortfall   = [pulp.LpVariable(f"sf_{h}", lowBound=0) for h in range(h_total)]

    # SoC at end of each hour; soc[0] = after hour 0
    soc = [pulp.LpVariable(f"soc_{h}", lowBound=soc_min, upBound=soc_max) for h in range(h_total + 1)]
    prob += soc[0] == float(soc_init)

    # ------------------------------------------------------------------
    # Build actuals lookup for locking
    # ------------------------------------------------------------------
    actuals_map: dict = {}
    if actuals:
        for a in actuals:
            actuals_map[a.hour_index] = float(a.actual_demand)

    # ------------------------------------------------------------------
    # Per-hour load_adj variables — locked hours get wider bounds so the
    # equality constraint is always feasible regardless of actual demand
    # ------------------------------------------------------------------
    load_adj = []
    for h in range(h_total):
        ed_h = float(estimated_demand[h])
        if h < horizon_start and h in actuals_map:
            # Locked hour: bounds widen to cover exact actual value, then pinned by equality
            lock_delta = actuals_map[h] - ed_h
            lb = min(lock_delta, ed_h * (load_min_ratio - 1.0))
            ub = max(lock_delta, ed_h * (load_max_ratio - 1.0))
        else:
            lb = ed_h * (load_min_ratio - 1.0)
            ub = ed_h * (load_max_ratio - 1.0)
        load_adj.append(pulp.LpVariable(f"la_{h}", lowBound=lb, upBound=ub))

    # ------------------------------------------------------------------
    # Per-hour constraints
    # ------------------------------------------------------------------
    for h in range(h_total):
        ed_h = float(estimated_demand[h])
        pv_h = float(pv_available[h])
        bq_h = float(prices.buy_qty[h])

        # Pin locked hours to known actual demand
        if h < horizon_start and h in actuals_map:
            prob += load_adj[h] == actuals_map[h] - ed_h

        actual_demand_h = ed_h + load_adj[h]   # kWh
        pv_used_h       = pv_h - pv_curtail[h]

        # Energy balance: supply = demand
        prob += (bq_h + pv_used_h + s_discharge[h]
                 == actual_demand_h + s_charge[h] + sold_back[h] - shortfall[h])

        # SoC continuity
        prob += soc[h + 1] == (soc[h]
                               + s_charge[h]    * charge_efficiency
                               - s_discharge[h] / discharge_efficiency)

        # Prevent simultaneous charge + discharge from exceeding rated power
        prob += s_charge[h] + s_discharge[h] <= battery_power_kw

    # ------------------------------------------------------------------
    # Objective
    # ------------------------------------------------------------------
    buy_cost_expr  = pulp.lpSum(float(prices.buy_price[h]) * float(prices.buy_qty[h]) for h in range(h_total))
    sell_rev_expr  = pulp.lpSum(float(prices.sell_price[h]) * sold_back[h]            for h in range(h_total))
    penalty_expr   = pulp.lpSum(float(penalty_per_kwh)     * shortfall[h]             for h in range(h_total))

    prob += sell_rev_expr - buy_cost_expr - penalty_expr

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[prob.status]

    def val(v, default=0.0):
        return float(v.value()) if v.value() is not None else default

    plan_load_adj      = np.array([val(load_adj[h])      for h in range(h_total)])
    plan_s_charge      = np.array([val(s_charge[h])      for h in range(h_total)])
    plan_s_discharge   = np.array([val(s_discharge[h])   for h in range(h_total)])
    plan_pv_curtail    = np.array([val(pv_curtail[h])    for h in range(h_total)])
    plan_sold_back     = np.array([val(sold_back[h])     for h in range(h_total)])
    plan_shortfall     = np.array([val(shortfall[h])     for h in range(h_total)])
    plan_soc           = np.array([val(soc[h + 1])       for h in range(h_total)])

    buy_cost   = float(np.sum(prices.buy_price[:h_total] * prices.buy_qty[:h_total]))
    sell_rev   = float(np.sum(prices.sell_price[:h_total] * plan_sold_back))
    penalty    = float(penalty_per_kwh * np.sum(plan_shortfall))

    return DispatchPlan(
        hours=h_total,
        load_adj=plan_load_adj,
        storage_charge=plan_s_charge,
        storage_discharge=plan_s_discharge,
        pv_curtail=plan_pv_curtail,
        sold_back=plan_sold_back,
        shortfall=plan_shortfall,
        soc_trajectory=plan_soc,
        buy_cost=buy_cost,
        sell_revenue=sell_rev,
        penalty=penalty,
        net_profit=sell_rev - buy_cost - penalty,
        milp_status=status,
    )


# ---------------------------------------------------------------------------
# Execution simulator (add realistic noise to represent device response)
# ---------------------------------------------------------------------------

def execute_dispatch(
    plan: DispatchPlan,
    estimated_demand: np.ndarray,
    pv_available: np.ndarray,
    prices: PriceSchedule,
    noise_std: float = 0.02,
    penalty_per_kwh: float = 2.0,
    rng: Optional[np.random.Generator] = None,
) -> Tuple[float, float, float, np.ndarray]:
    """Simulate physical execution of a dispatch plan with small noise.

    Returns
    -------
    sell_revenue : float   actual sell-back revenue (yuan)
    penalty      : float   actual shortfall penalty (yuan)
    net_profit   : float   sell_revenue - buy_cost - penalty
    actual_demand_arr : ndarray  kWh per hour actually consumed
    """
    if rng is None:
        rng = np.random.default_rng()

    h = plan.hours
    noise = 1.0 + rng.normal(0, noise_std, h)

    actual_demand    = np.maximum(0.0, (estimated_demand + plan.load_adj) * noise)
    actual_s_charge  = np.maximum(0.0, plan.storage_charge    * (1.0 + rng.normal(0, noise_std, h)))
    actual_s_disch   = np.maximum(0.0, plan.storage_discharge  * (1.0 + rng.normal(0, noise_std, h)))
    actual_pv_curtail= np.maximum(0.0, plan.pv_curtail         * (1.0 + rng.normal(0, noise_std, h)))
    pv_used          = np.maximum(0.0, pv_available - actual_pv_curtail)

    # Actual sold_back / shortfall from energy balance
    net_supply = prices.buy_qty[:h] + pv_used + actual_s_disch - actual_s_charge
    actual_sold_back = np.maximum(0.0, net_supply - actual_demand)
    actual_shortfall = np.maximum(0.0, actual_demand - net_supply)

    buy_cost   = float(np.sum(prices.buy_price[:h] * prices.buy_qty[:h]))
    sell_rev   = float(np.sum(prices.sell_price[:h] * actual_sold_back))
    penalty    = float(penalty_per_kwh * np.sum(actual_shortfall))
    net_profit = sell_rev - buy_cost - penalty

    return sell_rev, penalty, net_profit, actual_demand


# ---------------------------------------------------------------------------
# Convergence loop: MILP → execute → replan until revenue converges
# ---------------------------------------------------------------------------

def convergence_loop(
    prices: PriceSchedule,
    estimated_demand: np.ndarray,
    pv_available: np.ndarray,
    soc_init: float,
    actuals: Optional[List[HourlyActual]] = None,
    horizon_start: int = 0,
    max_iter: int = 10,
    profit_tol: float = 0.005,         # relative convergence tolerance
    execute_noise_std: float = 0.02,
    rng: Optional[np.random.Generator] = None,
    verbose: bool = True,
    **dispatch_kwargs,
) -> DispatchPlan:
    """Iterative planning loop until revenue converges.

    Each iteration:
      1. Solve MILP → DispatchPlan
      2. Simulate execution with noise → actual revenue
      3. If |ΔRevenue| / |Revenue| < profit_tol → converged
      4. Else: update free-hour estimates from execution feedback, repeat.

    Parameters
    ----------
    profit_tol : relative change in net_profit used as convergence criterion.
    **dispatch_kwargs : passed through to optimize_dispatch (e.g. soc_min, soc_max, ...).
    """
    if rng is None:
        rng = np.random.default_rng()

    # Work on copies to avoid mutating caller arrays
    est = estimated_demand.copy()

    prev_profit: Optional[float] = None
    plan: Optional[DispatchPlan] = None

    for iteration in range(1, max_iter + 1):
        plan = optimize_dispatch(
            prices, est, pv_available, soc_init,
            actuals=actuals, horizon_start=horizon_start,
            **dispatch_kwargs,
        )

        # Simulate execution
        sell_rev, penalty, net_profit, actual_demand = execute_dispatch(
            plan, est, pv_available, prices,
            noise_std=execute_noise_std, rng=rng,
        )

        if verbose:
            print(
                f"    [iter {iteration:2d}] MILP={plan.milp_status:8s} "
                f"sell={sell_rev:.1f}  penalty={penalty:.1f}  "
                f"net_profit={net_profit:.1f}"
            )

        # Check convergence
        if prev_profit is not None:
            denom = max(abs(prev_profit), 1.0)
            if abs(net_profit - prev_profit) / denom < profit_tol:
                plan.converged = True
                plan.iterations = iteration
                plan.net_profit = net_profit
                plan.sell_revenue = sell_rev
                plan.penalty = penalty
                if verbose:
                    print(f"    [Converged] after {iteration} iterations, net_profit={net_profit:.2f} yuan")
                return plan

        prev_profit = net_profit

        # Update free-hour estimates with execution results for next iteration
        for h in range(horizon_start, len(est)):
            est[h] = float(actual_demand[h])

    # Not converged within max_iter — return best plan
    if plan is not None:
        plan.iterations = max_iter
        plan.net_profit = prev_profit if prev_profit is not None else plan.net_profit
    return plan  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# D-2 / D-1 rolling strategy driver
# ---------------------------------------------------------------------------

def rolling_strategy(
    prices: PriceSchedule,
    estimated_demand_d1: np.ndarray,   # 24h estimated demand for D-1 day
    estimated_demand_d:  np.ndarray,   # 24h estimated demand for D day
    pv_available_d1: np.ndarray,       # 24h PV for D-1 day
    pv_available_d:  np.ndarray,       # 24h PV for D day
    actual_demand_d1: np.ndarray,      # 24h realised demand on D-1 day (revealed hour by hour)
    soc_init: float = 100.0,
    publisher=None,
    delay_seconds: float = 0.0,
    max_iter: int = 10,
    seed: Optional[int] = None,
    **dispatch_kwargs,
) -> List[DispatchPlan]:
    """Execute the full D-2 → D-1 → D-day planning and rolling replan cycle.

    Phase 1 (D-2 → D-1 23:00)
    --------------------------
    Optimise over 48h horizon: [D-1 0h .. D-day 23h] using estimated demand.
    Run convergence_loop for initial plan.

    Phase 2 (D-1 rolling, hours 0..23)
    ------------------------------------
    After each D-1 hour h passes, re-run convergence_loop:
      - Lock hours [0..h] to actual_demand_d1[h]
      - Horizon = h+1 (next free hour in the 48h window)

    Phase 3 (D-day)
    ---------------
    Execute final plan for D-day hours 24..47.

    Returns list of DispatchPlan objects (one per phase + replan).

    Publisher events (if publisher callable provided)
    -------------------------------------------------
    type='initial_plan'  : D-2 initial 48h plan
    type='replan'        : D-1 rolling replan after each actual hour
    type='d_day_hour'    : D-day per-hour execution result
    """
    import time

    rng = np.random.default_rng(seed)
    all_plans: List[DispatchPlan] = []

    # Combined 48h arrays (D-1 first, D day second)
    estimated_48 = np.concatenate([estimated_demand_d1, estimated_demand_d])
    pv_48        = np.concatenate([pv_available_d1,     pv_available_d])

    # ------------------------------------------------------------------
    # Phase 1: D-2 initial plan (D-1 23:00)
    # ------------------------------------------------------------------
    print("=" * 60)
    print("Phase 1: D-2 initial 48h planning (at D-1 23:00)")
    print("=" * 60)

    plan_init = convergence_loop(
        prices, estimated_48, pv_48, soc_init,
        actuals=None, horizon_start=0,
        max_iter=max_iter, rng=rng,
        verbose=True, **dispatch_kwargs,
    )
    all_plans.append(plan_init)

    if publisher:
        _emit(publisher, {
            "type": "initial_plan",
            "phase": "D-2 initial plan",
            "hours": 48,
            "load_adj":          plan_init.load_adj.tolist(),
            "storage_charge":    plan_init.storage_charge.tolist(),
            "storage_discharge": plan_init.storage_discharge.tolist(),
            "pv_curtail":        plan_init.pv_curtail.tolist(),
            "sold_back":         plan_init.sold_back.tolist(),
            "soc_trajectory":    plan_init.soc_trajectory.tolist(),
            "buy_cost":          plan_init.buy_cost,
            "sell_revenue":      plan_init.sell_revenue,
            "penalty":           plan_init.penalty,
            "net_profit":        plan_init.net_profit,
            "converged":         plan_init.converged,
            "iterations":        plan_init.iterations,
        })

    _print_plan_summary(plan_init, "Initial 48h plan")

    # ------------------------------------------------------------------
    # Phase 2: D-1 rolling replan (hours 0..23 on D-1 day)
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print("Phase 2: D-1 rolling replan")
    print("=" * 60)

    actuals: List[HourlyActual] = []
    current_soc = soc_init
    latest_plan = plan_init

    for h in range(24):
        # Reveal actual for D-1 hour h
        real_demand = float(actual_demand_d1[h])
        actuals.append(HourlyActual(hour_index=h, actual_demand=real_demand))

        # Update SoC from last plan's storage action for this hour
        net_storage = float(plan_init.storage_charge[h]) - float(plan_init.storage_discharge[h])
        current_soc = float(np.clip(current_soc + net_storage * 0.5, 0.0, 200.0))

        print(f"\n  D-1 hour {h:02d} passed — actual_demand={real_demand:.1f} kWh | SoC={current_soc:.1f} kWh")
        print(f"  Re-planning hours [{h+1}..47] with {len(actuals)} locked actuals")

        # Replan with all actuals so far locked
        plan_replan = convergence_loop(
            prices, estimated_48, pv_48, current_soc,
            actuals=actuals, horizon_start=h + 1,
            max_iter=max_iter, rng=rng,
            verbose=True, **dispatch_kwargs,
        )
        latest_plan = plan_replan
        all_plans.append(plan_replan)

        if publisher:
            _emit(publisher, {
                "type": "replan",
                "d1_hour": h,
                "actual_demand_d1": real_demand,
                "horizon_start": h + 1,
                "load_adj":          plan_replan.load_adj.tolist(),
                "storage_charge":    plan_replan.storage_charge.tolist(),
                "storage_discharge": plan_replan.storage_discharge.tolist(),
                "pv_curtail":        plan_replan.pv_curtail.tolist(),
                "sold_back":         plan_replan.sold_back.tolist(),
                "soc_trajectory":    plan_replan.soc_trajectory.tolist(),
                "buy_cost":          plan_replan.buy_cost,
                "sell_revenue":      plan_replan.sell_revenue,
                "penalty":           plan_replan.penalty,
                "net_profit":        plan_replan.net_profit,
                "converged":         plan_replan.converged,
                "iterations":        plan_replan.iterations,
                "soc": current_soc,
            })

        _print_plan_summary(plan_replan, f"Replan after D-1 h={h:02d}")

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    # ------------------------------------------------------------------
    # Phase 3: D-day execution
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print("Phase 3: D-day execution (hours 24..47)")
    print("=" * 60)

    final_load_adj      = latest_plan.load_adj[24:]
    final_s_charge      = latest_plan.storage_charge[24:]
    final_s_discharge   = latest_plan.storage_discharge[24:]
    final_pv_curtail    = latest_plan.pv_curtail[24:]

    total_sell = 0.0
    total_shortfall_kwh = 0.0

    for h_rel in range(24):
        h_abs = 24 + h_rel   # index in 48h plan
        hod   = h_rel

        # Compute target dispatch magnitudes for this D-day hour
        la  = float(final_load_adj[h_rel])
        sc  = float(final_s_charge[h_rel])
        sd  = float(final_s_discharge[h_rel])
        pc  = float(final_pv_curtail[h_rel])
        ed  = float(estimated_demand_d[h_rel])
        pv  = float(pv_available_d[h_rel])

        # Action descriptions
        load_action = f"increase {la:+.1f} kW"   if la >= 0 else f"decrease {la:.1f} kW"
        if sc > sd:
            storage_action = f"charge {sc:.1f} kW"
        elif sd > sc:
            storage_action = f"discharge {sd:.1f} kW"
        else:
            storage_action = "idle"
        pv_action = f"curtail {pc:.1f} kW" if pc > 0 else "full"

        print(
            f"  D-day h={hod:02d} | load: {load_action:20s} | "
            f"storage: {storage_action:22s} | PV: {pv_action}"
        )

        # Revenue contribution from this hour (simplified)
        net_supply  = prices.buy_qty[h_abs] + (pv - pc) + sd - sc
        actual_dem  = ed + la
        sb          = max(0.0, net_supply - actual_dem)
        sf          = max(0.0, actual_dem - net_supply)
        total_sell += float(prices.sell_price[h_abs]) * sb
        total_shortfall_kwh += sf

        # Update SoC
        current_soc = float(np.clip(current_soc + sc * 0.95 - sd / 0.95, 0.0, 200.0))

        if publisher:
            _emit(publisher, {
                "type": "d_day_hour",
                "d_day_hour": hod,
                "load_adj": la,
                "storage_charge": sc,
                "storage_discharge": sd,
                "pv_curtail": pc,
                "estimated_demand": ed,
                "pv_available": pv,
                "sold_back": sb,
                "shortfall": sf,
                "soc": current_soc,
                "sell_revenue_hour": float(prices.sell_price[h_abs]) * sb,
                "buy_price": float(prices.buy_price[h_abs]),
                "buy_qty": float(prices.buy_qty[h_abs]),
                "sell_price": float(prices.sell_price[h_abs]),
            })

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    total_penalty = 2.0 * total_shortfall_kwh
    buy_cost_d = float(np.sum(prices.buy_price[24:48] * prices.buy_qty[24:48]))
    print(f"\n  D-day summary: sell_revenue={total_sell:.2f}  buy_cost={buy_cost_d:.2f}  "
          f"penalty={total_penalty:.2f}  net= {total_sell - buy_cost_d - total_penalty:.2f} yuan")

    return all_plans


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _print_plan_summary(plan: DispatchPlan, label: str) -> None:
    """Print a compact per-hour dispatch table."""
    print(f"\n  >>> {label}  [converged={plan.converged}, iter={plan.iterations}, "
          f"status={plan.milp_status}]")
    print(f"      buy_cost={plan.buy_cost:.1f}  sell_rev={plan.sell_revenue:.1f}  "
          f"penalty={plan.penalty:.1f}  net={plan.net_profit:.1f} yuan")
    print(f"  {'h':>3} {'est_demand+adj':>15} {'s_chg':>7} {'s_dis':>7} "
          f"{'sold_back':>10} {'shortfall':>10} {'SoC':>7}")
    for h in range(plan.hours):
        print(
            f"  {h:3d} "
            f"{plan.load_adj[h]:+14.1f}  "
            f"{plan.storage_charge[h]:7.1f}  "
            f"{plan.storage_discharge[h]:7.1f}  "
            f"{plan.sold_back[h]:10.1f}  "
            f"{plan.shortfall[h]:10.1f}  "
            f"{plan.soc_trajectory[h]:7.1f}"
        )


def _emit(publisher, event: dict) -> None:
    """Safe call of publisher, silently ignoring errors."""
    try:
        publisher(event)
    except Exception:
        pass
