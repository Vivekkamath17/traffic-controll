from __future__ import annotations

from traffic_controller.simulator import TrafficSimulator


class IntersectionNetwork:
    """
    Manages a grid of connected junctions.
    Vehicles exiting one junction can enter a connected junction.
    """

    def __init__(self):
        self.junctions: dict[str, TrafficSimulator] = {}
        self.connections: dict = {}
        # connections[(junction_id, exit_lane)] = (target_junction_id, entry_lane)

    def add_junction(self, junction_id: str, simulator: TrafficSimulator) -> None:
        self.junctions[junction_id] = simulator

    def connect(self, from_id: str, exit_lane: str, to_id: str, entry_lane: str) -> None:
        """
        Connect exit of one junction to entry of another.
        Example: connect('J1','E','J2','W')
        means vehicles leaving J1 heading East enter J2 from the West.
        """
        self.connections[(from_id, exit_lane)] = (to_id, entry_lane)

    def route_exiting_vehicles(self) -> None:
        """
        Call once per tick after all junctions have ticked.
        Takes exiting vehicles from each junction and injects them
        into connected junctions as new arrivals.
        """
        for junction_id, sim in self.junctions.items():
            for vehicle in sim.exiting_vehicles:
                key = (junction_id, vehicle.exit_lane)
                if key in self.connections:
                    target_id, entry_lane = self.connections[key]
                    target_sim = self.junctions[target_id]
                    # Inject as a new arrival in the target lane
                    target_sim.inject_arrival(entry_lane, 1)

    def tick_all(self) -> dict[str, dict[str, int]]:
        """Tick all junctions in parallel, then route vehicles."""
        arrivals_by_junction: dict[str, dict[str, int]] = {}
        for jid, sim in self.junctions.items():
            arrivals_by_junction[jid] = sim.tick()
        self.route_exiting_vehicles()
        return arrivals_by_junction

    def get_network_state(self) -> dict:
        return {
            jid: {
                "lanes": {
                    lid: {
                        "count": l.vehicle_count,
                        "wait": l.waiting_time,
                        "green": lid in (["N", "S"] if sim.state.current_phase == "NS" else ["E", "W"]),
                    }
                    for lid, l in sim.state.lanes.items()
                },
                "phase": sim.state.current_phase,
                "position": sim.grid_position,
            }
            for jid, sim in self.junctions.items()
        }
