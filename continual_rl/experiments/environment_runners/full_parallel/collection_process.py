from torch.multiprocessing import Queue
import cloudpickle
from continual_rl.experiments.environment_runners.environment_runner_batch import EnvironmentRunnerBatch


class CollectionProcess():
    def __init__(self, policy, timesteps_per_collection, render_collection_freq=None):
        self.incoming_queue = Queue()  # Requests to process
        self.outgoing_queue = Queue()  # Pass data back to caller

        # Only supporting 1 parallel env for now
        self._episode_runner = EnvironmentRunnerBatch(policy, num_parallel_envs=1,
                                                      timesteps_per_collection=timesteps_per_collection,
                                                      render_collection_freq=render_collection_freq)

    def process_queue(self):
        while True:
            next_message = self.incoming_queue.get()
            action_id, content = next_message

            if action_id == "kill":
                break

            elif action_id == "start_episode":
                time_batch_size, env_spec, preprocessor, task_id, episode_renderer, early_stopping_condition = content

                env_spec = cloudpickle.loads(env_spec)
                preprocessor = cloudpickle.loads(preprocessor)
                episode_renderer = cloudpickle.loads(episode_renderer)
                early_stopping_condition = cloudpickle.loads(early_stopping_condition)

                results = self._episode_runner.collect_data(time_batch_size, env_spec, preprocessor, task_id,
                                                            episode_renderer, early_stopping_condition)
                self.outgoing_queue.put(results)
