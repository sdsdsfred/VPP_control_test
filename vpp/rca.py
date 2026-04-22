"""Root cause analysis and strategy adjustment."""
import numpy as np
from typing import Dict


def root_cause_and_adjust(
    hour_index: int, 
    scenario, 
    actual_loads_hour: np.ndarray, 
    control_feedback: Dict,
    storage_soc: float = 50.0,
    recent_storage_cmd: Dict = None,
    storage_response_timeout: int = 2
):
    """Enhanced RCA with storage-first strategy.
    
    Decision tree:
    1. If actual < forecast_mid: try to charge storage with the difference
    2. If actual > forecast_mid: try to discharge storage to cover difference
    3. If storage unavailable (low SoC or recent cmd not executed): trigger load control
    4. Load control as fallback
    
    Args:
        hour_index: current hour
        scenario: Scenario object with forecasts
        actual_loads_hour: achieved loads from last hour
        control_feedback: feedback from simulate_execution
        storage_soc: current storage state of charge (%)
        recent_storage_cmd: {'hour': h, 'target': cmd_value, 'executed': bool}
        storage_response_timeout: hours to wait for storage response before fallback to load control
    
    Returns:
        {"load_targets": [...], "storage_target": val, "reason": str, "control_priority": "storage"|"load"}
    """
    total_actual = actual_loads_hour.sum()
    forecast_min = scenario.load_forecast_min[hour_index]
    forecast_max = scenario.load_forecast_max[hour_index]
    forecast_mid = (forecast_min + forecast_max) / 2.0
    max_control_total = 1.10 * forecast_max
    
    # Determine if storage is responsive
    # (if recent command was issued but not executed, or SoC too low)
    storage_available = True
    if recent_storage_cmd is not None:
        hours_since_cmd = hour_index - recent_storage_cmd.get("hour", -999)
        if (0 < hours_since_cmd < storage_response_timeout 
            and not recent_storage_cmd.get("executed", False)):
            # Recent command not yet executed -> storage may be unresponsive
            storage_available = False
    
    # If SoC critically low, prefer load control to avoid battery issues
    if storage_soc < 20.0:
        storage_available = False
    
    # ========== Strategy 1: Actual below forecast_mid -> try to charge storage ==========
    if total_actual < forecast_mid * 0.98:  # small hysteresis to avoid oscillation
        diff = forecast_mid - total_actual
        
        if storage_available and storage_soc < 90.0:
            # Priority: charge storage with the difference
            charge_cmd = min(diff, 100.0)  # cap at 100 kW
            storage_charge = min(charge_cmd, (100.0 - storage_soc) * 0.5)  # crude SoC->power estimate
            return {
                "load_targets": actual_loads_hour, 
                "storage_target": storage_charge,
                "reason": "charge_storage",
                "control_priority": "storage",
                "stored_diff": float(diff),
            }
        elif not storage_available:
            # Storage not available -> fallback to load control (increase load via target reduction)
            # This keeps actual higher by reducing control aggressiveness
            return {
                "load_targets": actual_loads_hour, 
                "storage_target": 0.0,
                "reason": "load_increase_fallback_no_storage",
                "control_priority": "load",
            }
    
    # ========== Strategy 2: Actual above forecast_mid -> try to discharge storage ==========
    if total_actual > forecast_mid * 1.02:  # small hysteresis
        diff = total_actual - forecast_mid
        
        if storage_available and storage_soc > 10.0:
            # Priority: discharge storage to absorb excess load
            discharge_cmd = min(diff, 100.0)  # cap at 100 kW
            storage_discharge = -min(discharge_cmd, storage_soc * 0.5)  # negative for discharge
            return {
                "load_targets": actual_loads_hour, 
                "storage_target": storage_discharge,
                "reason": "discharge_storage",
                "control_priority": "storage",
                "excess_diff": float(diff),
            }
        else:
            # Storage not available -> trigger load control (reduce loads)
            denom = actual_loads_hour.sum()
            if denom <= 0:
                return {
                    "load_targets": actual_loads_hour,
                    "storage_target": 0.0,
                    "reason": "ok",
                    "control_priority": "none",
                }

            devi = actual_loads_hour - (forecast_mid * (actual_loads_hour / denom))
            idx = int(np.argmax(devi))
            reduction = 0.15 * actual_loads_hour[idx]  # request 15% reduction
            new_targets = actual_loads_hour.copy()
            new_targets[idx] = max(0.0, new_targets[idx] - reduction)

            # Safety bound: load-control total should not exceed forecast max by more than 10%.
            target_total = float(np.sum(new_targets))
            if target_total > max_control_total and target_total > 0:
                new_targets = new_targets * (max_control_total / target_total)

            return {
                "load_targets": new_targets, 
                "storage_target": 0.0,
                "reason": f"reduce_group_{idx}_no_discharge",
                "control_priority": "load",
            }
    
    # ========== Strategy 3: Actual within forecast_mid band -> no action ==========
    return {
        "load_targets": actual_loads_hour, 
        "storage_target": 0.0,
        "reason": "ok",
        "control_priority": "none",
    }
