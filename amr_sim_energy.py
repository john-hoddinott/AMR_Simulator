from amr_sim_models import AMR, Lift, PayloadType

G = 9.81


def loaded_power_w(amr: AMR, payload: PayloadType) -> float:
    if amr.payload_capacity_kg <= 0:
        return amr.motor_power_w
    load_fraction = min(1.0, max(0.0, payload.weight_kg / amr.payload_capacity_kg))
    return amr.motor_power_w * (1.0 + 0.35 * load_fraction)


def idle_move_power_w(amr: AMR) -> float:
    return amr.motor_power_w * 0.75


def energy_for_travel_kwh(power_w: float, duration_sec: float) -> float:
    return (power_w / 1000.0) * (duration_sec / 3600.0)


def total_route_energy_kwh(
    amr: AMR, payload: PayloadType, to_pickup_sec: float, loaded_sec: float
) -> float:
    return energy_for_travel_kwh(
        idle_move_power_w(amr), to_pickup_sec
    ) + energy_for_travel_kwh(loaded_power_w(amr, payload), loaded_sec)


def requires_recharge_before_route(amr: AMR, required_energy_kwh: float) -> bool:
    remaining_after_route = amr.battery_energy_kwh() - required_energy_kwh
    return remaining_after_route < amr.min_reserve_energy_kwh()


# Lift energy


def energy_for_power_kwh(power_w: float, duration_sec: float) -> float:
    return (power_w / 1000.0) * (duration_sec / 3600.0)


def lift_travel_energy_kwh(
    lift: Lift,
    payload: PayloadType,
    floor_height_m: float,
    floor_delta: int,
    loaded: bool,
) -> float:
    if floor_delta == 0:
        return 0.0

    travel_m = abs(floor_delta) * floor_height_m

    payload_mass = payload.weight_kg if loaded else 0.0
    car_plus_load_kg = lift.car_mass_kg + payload_mass
    counterweight_kg = lift.car_mass_kg * lift.counterweight_ratio

    imbalance_kg = car_plus_load_kg - counterweight_kg
    mechanical_j = abs(imbalance_kg) * G * travel_m

    electrical_j = mechanical_j / max(lift.travel_efficiency, 1e-9)

    # crude regeneration credit for descending with a heavier car side
    descending_loaded = floor_delta < 0 and imbalance_kg > 0
    descending_empty = floor_delta > 0 and imbalance_kg < 0
    if descending_loaded or descending_empty:
        electrical_j *= max(0.0, 1.0 - lift.regen_efficiency)

    return electrical_j / 3_600_000.0


def lift_door_energy_kwh(
    lift: Lift, door_time_sec: float, door_cycles: int = 1
) -> float:
    return energy_for_power_kwh(lift.door_power_w, door_time_sec * door_cycles)


def lift_standby_energy_kwh(lift: Lift, wait_time_sec: float) -> float:
    return energy_for_power_kwh(lift.standby_power_w, wait_time_sec)


def total_lift_energy_kwh(
    lift: Lift,
    payload: PayloadType,
    floor_height_m: float,
    reposition_floor_delta: int,
    loaded_floor_delta: int,
    wait_time_sec: float,
    door_time_sec: float,
    boarding_time_sec: float = 0.0,
) -> float:
    stationary_time_sec = (
        max(0.0, wait_time_sec)
        + max(0.0, door_time_sec) * 2
        + max(0.0, boarding_time_sec) * 2
    )
    return (
        lift_travel_energy_kwh(
            lift, payload, floor_height_m, reposition_floor_delta, loaded=False
        )
        + lift_travel_energy_kwh(
            lift, payload, floor_height_m, loaded_floor_delta, loaded=True
        )
        + lift_door_energy_kwh(lift, door_time_sec, door_cycles=2)
        + lift_standby_energy_kwh(lift, stationary_time_sec)
    )
