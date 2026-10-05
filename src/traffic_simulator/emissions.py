"""Fuel and CO2 of light motor vehicles from their speed and acceleration, step by step (no Qt).

The instantaneous power-based model of Biggs and Akçelik (1986), "An energy-related model of instantaneous fuel
consumption", Traffic Engineering & Control 27(6), 320–325, with the light-vehicle parameters of Bowyer, Akçelik and
Biggs (1985), "Guide to fuel consumption analyses for urban traffic management", ARRB Special Report 32:

    R_T = b1 + b2·v² + M·a/1000 + 9.81·M·G/100000            total tractive force, kN
    f   = α + β1·R_T·v + [β2·M·a²·v/1000]_(a>0)   for R_T > 0  fuel, mL/s
    f   = α                                       for R_T ≤ 0  (idling, coasting, braking)

with v in m/s, a in m/s², G the grade in % (0: the network has no grades). CO2 = fuel × 2.35 kg/L of petrol
(US EPA, 8,887 g CO2 a US gallon of gasoline). The parameters are for a 1980s Australian car of 1,200 kg, so the
litres read high for a modern car: compare designs with it, don't quote it as a fleet's consumption. It is a car
model: buses, trucks, motorcycles and bicycles are left out (`light_motor`), not given made-up parameters.
"""

from __future__ import annotations

ALPHA_ML_S = 0.375  # idle fuel rate, mL/s
BETA1_ML_KJ = 0.09  # fuel per unit of energy, mL/kJ
BETA2_ML_KJ_MS2 = 0.03  # fuel per unit of energy-acceleration, mL/(kJ·m/s²)
B1_KN = 0.333  # rolling resistance, kN
B2_KN_S2_M2 = 0.00108  # aerodynamic drag, kN/(m/s)²
MASS_KG = 1200.0
CO2_KG_PER_L = 2.35  # petrol (US EPA: 8,887 g CO2 / US gallon)
LIGHT_LENGTH_M = (2.5, 7.0)  # vehicle lengths the car model is used for (cars, taxis, vans, emergency vans)


def fuel_rate_ml_s(speed: float, accel: float, grade_percent: float = 0.0) -> float:
    """Fuel used per second at this speed (m/s) and acceleration (m/s²)."""
    tractive = B1_KN + B2_KN_S2_M2 * speed * speed + MASS_KG * accel / 1000 + 9.81 * MASS_KG * grade_percent / 100000
    if tractive <= 0:
        return ALPHA_ML_S
    rate = ALPHA_ML_S + BETA1_ML_KJ * tractive * speed
    if accel > 0:
        rate += BETA2_ML_KJ_MS2 * MASS_KG * accel * accel * speed / 1000
    return rate


def light_motor(length_m: float, desired_max_kmh: float) -> bool:
    """Whether the car model applies: a motor vehicle the size of a car or van (not a bike, motorcycle, bus or
    lorry)."""
    return LIGHT_LENGTH_M[0] <= length_m <= LIGHT_LENGTH_M[1] and desired_max_kmh > 30


def summary(fuel_ml: float, vehicle_km: float, occupancy: float = 0.0) -> dict:
    """Litres, kg of CO2, and per vehicle-km and person-km (`occupancy`: people a vehicle; 0 leaves that out)."""
    litres = fuel_ml / 1000
    return {"fuel_l": round(litres, 2), "co2_kg": round(litres * CO2_KG_PER_L, 2),
            "fuel_l_per_100km": round(litres / vehicle_km * 100, 2) if vehicle_km > 0 else None,
            "co2_g_per_km": round(litres * CO2_KG_PER_L * 1000 / vehicle_km, 1) if vehicle_km > 0 else None,
            "co2_g_per_person_km": round(litres * CO2_KG_PER_L * 1000 / (vehicle_km * occupancy), 1)
            if vehicle_km > 0 and occupancy > 0 else None}
