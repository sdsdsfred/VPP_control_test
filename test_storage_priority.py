"""Test script to demonstrate the storage-first control strategy."""
import json
from vpp.simulator import generate_scenario, simulate_execution
from vpp.model import optimize_hourly
from vpp.rca import root_cause_and_adjust


def test_storage_priority():
    """Demonstrate the storage-first RCA strategy."""
    print("=" * 70)
    print("储能优先调控策略演示")
    print("=" * 70)
    
    # Create a short scenario
    scenario = generate_scenario(hours=6, n_load_groups=3, seed=42)
    
    soc = 50.0
    
    for h in range(6):
        actual_hour = scenario.actual_loads[h]
        lf_min = scenario.load_forecast_min[h]
        lf_max = scenario.load_forecast_max[h]
        forecast_mid = (lf_min + lf_max) / 2.0
        actual_total = actual_hour.sum()
        
        print(f"\n--- Hour {h} ---")
        print(f"Forecast: [{lf_min:.1f}, {lf_max:.1f}] (mid={forecast_mid:.1f})")
        print(f"Actual:   {actual_total:.1f} kW")
        print(f"Storage SoC before: {soc:.1f}%")
        
        # Simulate MILP control
        pv_min = scenario.pv_forecast_min[h]
        pv_max = scenario.pv_forecast_max[h]
        control = optimize_hourly(h, pv_min, pv_max, lf_min, lf_max, 5000.0, actual_hour, soc)
        
        # Simulate execution
        feedback = simulate_execution(control, actual_hour)
        achieved = feedback["achieved_loads"]
        
        # RCA with storage-first strategy
        adjust = root_cause_and_adjust(
            h, scenario, achieved, feedback,
            storage_soc=soc,
            recent_storage_cmd=None,
            storage_response_timeout=2
        )
        
        print(f"\nRCA Decision:")
        print(f"  Priority: {adjust.get('control_priority')}")
        print(f"  Reason: {adjust.get('reason')}")
        print(f"  Storage Target: {adjust.get('storage_target', 0.0):.1f} kW")
        
        if adjust.get('stored_diff', 0.0) > 0:
            print(f"  → Charging storage, diff={adjust.get('stored_diff', 0.0):.1f} kW")
        if adjust.get('excess_diff', 0.0) > 0:
            print(f"  → Discharging storage, excess={adjust.get('excess_diff', 0.0):.1f} kW")
        
        # Update SoC
        storage_action = adjust.get('storage_target', 0.0)
        soc += storage_action * 0.5
        soc = max(0.0, min(100.0, soc))
        print(f"Storage SoC after:  {soc:.1f}%")
        
        # Simulate storage action
        if storage_action != 0.0:
            feedback2 = simulate_execution(adjust, achieved)
            achieved_total = feedback2['achieved_loads'].sum()
            print(f"Achieved after adjustment: {achieved_total:.1f} kW")


if __name__ == '__main__':
    test_storage_priority()
