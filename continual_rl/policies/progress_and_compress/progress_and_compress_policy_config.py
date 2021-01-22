from continual_rl.policies.ewc.ewc_policy_config import OnlineEWCPolicyConfig


class ProgressAndCompressPolicyConfig(OnlineEWCPolicyConfig):

    def __init__(self):
        super().__init__()
        self.num_train_steps_of_compress = 1000
        self.ewc_per_task_min_frames = 0  # Cadence of compress (incl EWC) cycles is handled differently in P&C
