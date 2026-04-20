from __future__ import annotations


class QLearningAgent:
    """
    Tabular Q-learning agent that learns from the controller's decisions.
    Runs alongside rule-based algorithms and gradually builds a Q-table.
    Policy can be toggled between rule_based and learned at runtime.
    """

    def __init__(self, alpha: float = 0.1, gamma: float = 0.95, epsilon: float = 0.6):
        self.q_table: dict = {}      # {state_key: {action_name: q_value}}
        self.alpha = alpha
        self.gamma = gamma
        self.epsilon = epsilon
        self.total_updates = 0
        self.last_state_key = None
        self.last_action = None

    def _discretize(self, traffic_state) -> tuple:
        # Bucket vehicle counts: 0-5=0, 6-12=1, 13-20=2, 21+=3
        buckets = []
        for lane_id in ["N", "S", "E", "W"]:
            c = traffic_state.lanes[lane_id].vehicle_count
            buckets.append(0 if c <= 5 else 1 if c <= 12 else 2 if c <= 20 else 3)
        buckets.append(0 if traffic_state.current_phase == "NS" else 1)
        return tuple(buckets)

    def _get_q(self, state_key, action_name) -> float:
        return self.q_table.get(state_key, {}).get(action_name, 0.0)

    def select_action(self, traffic_state, context_hint=None):
        """
        Epsilon-greedy Q-learning policy.
        context_hint: optional Action from legacy algorithms — used ONLY
        during early exploration (epsilon > 0.5) to seed the Q-table faster.
        Once epsilon drops below 0.5, context_hint is ignored entirely.
        """
        state_key = self._discretize(traffic_state)
        self.last_state_key = state_key

        import random
        from traffic_controller.models import Action

        # Early training: blend hint with exploration
        if self.epsilon > 0.5 and context_hint is not None:
            if random.random() < 0.6:
                self.last_action = context_hint.name
                return context_hint

        # Epsilon-greedy
        if random.random() < self.epsilon:
            chosen = random.choice([Action.KEEP_PHASE, Action.SWITCH_PHASE])
        else:
            actions = [Action.KEEP_PHASE, Action.SWITCH_PHASE]
            chosen = max(actions, key=lambda a: self._get_q(state_key, a.name))

        self.last_action = chosen.name
        return chosen

    def update(self, new_traffic_state, reward: float):
        """
        Call this after every tick with the reward signal.
        Updates Q(last_state, last_action) using standard Q-learning.
        """
        if self.last_state_key is None or self.last_action is None:
            return

        new_key = self._discretize(new_traffic_state)
        best_next = max(self._get_q(new_key, a) for a in ["KEEP_PHASE", "SWITCH_PHASE"])

        if self.last_state_key not in self.q_table:
            self.q_table[self.last_state_key] = {}

        old_q = self._get_q(self.last_state_key, self.last_action)
        new_q = old_q + self.alpha * (reward + self.gamma * best_next - old_q)
        self.q_table[self.last_state_key][self.last_action] = new_q
        self.total_updates += 1

        # Decay epsilon every 100 updates
        if self.total_updates % 100 == 0:
            self.epsilon = max(0.01, self.epsilon * 0.99)

    def calculate_reward(self, prev_state, new_state, emergency_cleared: bool) -> float:
        # Base: reduction in total weighted wait
        prev_w = sum(l.vehicle_count * l.waiting_time for l in prev_state.lanes.values())
        new_w = sum(l.vehicle_count * l.waiting_time for l in new_state.lanes.values())
        reward = (prev_w - new_w) * 0.01

        # Bonus: vehicles served this tick (green lane count drop)
        for lid in ["N", "S", "E", "W"]:
            drop = prev_state.lanes[lid].vehicle_count - new_state.lanes[lid].vehicle_count
            if drop > 0:
                reward += drop * 2.0

        # Penalty: any lane waiting > 90s
        for lane in new_state.lanes.values():
            if lane.waiting_time > 90:
                reward -= (lane.waiting_time - 90) * 0.5

        # Penalty: unnecessary phase switch
        green = ["N", "S"] if new_state.current_phase == "NS" else ["E", "W"]
        red = ["E", "W"] if new_state.current_phase == "NS" else ["N", "S"]
        green_count = sum(new_state.lanes[l].vehicle_count for l in green)
        red_count = sum(new_state.lanes[l].vehicle_count for l in red)
        if new_state.current_phase != prev_state.current_phase and green_count > red_count:
            reward -= 15.0

        if emergency_cleared:
            reward += 300.0
        return reward

    def get_stats(self) -> dict:
        return {
            "q_table_size": len(self.q_table),
            "epsilon": round(self.epsilon, 4),
            "total_updates": self.total_updates,
            "training_phase": "exploring" if self.epsilon > 0.3 else "exploiting",
        }
