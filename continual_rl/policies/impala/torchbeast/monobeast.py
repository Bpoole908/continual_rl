# Copyright (c) Facebook, Inc. and its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Taken from https://raw.githubusercontent.com/facebookresearch/torchbeast/3f3029cf3d6d488b8b8f952964795f451a49048f/torchbeast/monobeast.py
# and modified

import logging
import os
import pprint
import time
import timeit
import traceback
import typing
import copy
import psutil
import numpy as np
import queue
import cloudpickle
from torch.multiprocessing import Pool
import threading
import json
import shutil

import torch
import multiprocessing as py_mp
from torch import multiprocessing as mp
from torch import nn
from torch.nn import functional as F

from continual_rl.policies.impala.torchbeast.core import environment
from continual_rl.policies.impala.torchbeast.core import file_writer
from continual_rl.policies.impala.torchbeast.core import prof
from continual_rl.policies.impala.torchbeast.core import vtrace
from continual_rl.utils.utils import Utils


Buffers = typing.Dict[str, typing.List[torch.Tensor]]


class LearnerThreadState():
    STARTING, RUNNING, STOP_REQUESTED, STOPPED = range(4)

    def __init__(self):
        """
        This class is a helper class to manage communication of state between threads. For now I'm assuming just
        setting state is atomic enough to not require further thread safety.
        """
        self.state = self.STARTING
        self.lock = threading.Lock()

    def wait_for(self, desired_state_list, timeout=300):
        time_passed = 0
        delta = 0.1  # seconds

        while self.state not in desired_state_list and time_passed < timeout:
            #print(f"Waiting on state(s) {desired_state_list} but in state {self.state}")
            time.sleep(delta)
            time_passed += delta

        if time_passed > timeout:
            print(f"Gave up on waiting due to timeout. Desired list: {desired_state_list}, current state: {self.state}")  # TODO: not print


class Monobeast():
    def __init__(self, model_flags, observation_space, action_spaces, policy_class):
        self._model_flags = model_flags

        # The latest full episode's set of observations generated by actor with actor_index == 0
        self._videos_to_log = py_mp.Manager().Queue(maxsize=1)

        # Moved some of the original Monobeast code into a setup function, to make class objects
        self.buffers, self.actor_model, self.learner_model, self.optimizer, self.plogger, self.logger, self.checkpointpath \
            = self.setup(model_flags, observation_space, action_spaces, policy_class)
        self._scheduler_state_dict = None  # Filled if we load()
        self._scheduler = None  # Task-specific, so created there

        # Keep track of our threads/processes so we can clean them up.
        self._learner_thread_states = []
        self._actor_processes = []

        # If we're reloading a task, we need to start from where we left off. This gets populated by load, if
        # applicable
        self.last_timestep_returned = 0

    # Functions designed to be overridden by subclasses of Monobeast
    def on_act_unroll_complete(self, task_flags, actor_index, agent_output, env_output, new_buffers):
        """
        Called after every unroll in every process running act(). Note that this happens in separate processes, and
        data will need to be shepherded accordingly.
        """
        pass

    def get_batch_for_training(self, batch):
        """
        Create a new batch based on the old, with any modifications desired. (E.g. augmenting with entries from
        a replay buffer.) This is run in each learner thread.
        """
        return batch

    def custom_loss(self, task_flags, model, initial_agent_state):
        """
        Create a new loss. This is added to the existing losses before backprop. Any returned stats will be added
        to the logged stats. If a stat's key ends in "_loss", it'll automatically be plotted as well.
        This is run in each learner thread.
        :return: (loss, dict of stats)
        """
        return 0, {}

    # Core Monobeast functionality
    def setup(self, model_flags, observation_space, action_spaces, policy_class):
        os.environ["OMP_NUM_THREADS"] = "1"
        logging.basicConfig(
            format=(
                "[%(levelname)s:%(process)d %(module)s:%(lineno)d %(asctime)s] " "%(message)s"
            ),
            level=0,
        )

        logger = Utils.create_logger(os.path.join(model_flags.savedir, "impala_logs.log"))
        plogger = Utils.create_logger(os.path.join(model_flags.savedir, "impala_results.log"))

        checkpointpath = os.path.join(model_flags.savedir, "model.tar")

        if model_flags.num_buffers is None:  # Set sensible default for num_buffers.
            model_flags.num_buffers = max(2 * model_flags.num_actors, model_flags.batch_size)
        if model_flags.num_actors >= model_flags.num_buffers:
            raise ValueError("num_buffers should be larger than num_actors")
        if model_flags.num_buffers < model_flags.batch_size:
            raise ValueError("num_buffers should be larger than batch_size")

        model_flags.device = None
        if not model_flags.disable_cuda and torch.cuda.is_available():
            logger.info("Using CUDA.")
            model_flags.device = torch.device("cuda")
        else:
            logger.info("Not using CUDA.")
            model_flags.device = torch.device("cpu")

        model = policy_class(observation_space, action_spaces, model_flags.use_lstm)
        buffers = self.create_buffers(model_flags, observation_space.shape, model.num_actions)

        model.share_memory()

        learner_model = policy_class(
            observation_space, action_spaces, model_flags.use_lstm
        ).to(device=model_flags.device)

        if model_flags.optimizer == "rmsprop":
            optimizer = torch.optim.RMSprop(
                learner_model.parameters(),
                lr=model_flags.learning_rate,
                momentum=model_flags.momentum,
                eps=model_flags.epsilon,
                alpha=model_flags.alpha,
            )
        elif model_flags.optimizer == "adam":
            optimizer = torch.optim.Adam(
                learner_model.parameters(),
                lr=model_flags.learning_rate,
            )
        else:
            raise ValueError(f'Unsupported optimizer type {model_flags.optimizer}')

        return buffers, model, learner_model, optimizer, plogger, logger, checkpointpath

    def compute_baseline_loss(self, advantages):
        return 0.5 * torch.sum(advantages ** 2)

    def compute_entropy_loss(self, logits):
        """Return the entropy loss, i.e., the negative entropy of the policy."""
        policy = F.softmax(logits, dim=-1)
        log_policy = F.log_softmax(logits, dim=-1)
        return torch.sum(policy * log_policy)

    def compute_policy_gradient_loss(self, logits, actions, advantages):
        cross_entropy = F.nll_loss(
            F.log_softmax(torch.flatten(logits, 0, 1), dim=-1),
            target=torch.flatten(actions, 0, 1),
            reduction="none",
        )
        cross_entropy = cross_entropy.view_as(advantages)
        return torch.sum(cross_entropy * advantages.detach())

    def act(
            self,
            model_flags,
            task_flags,
            actor_index: int,
            free_queue: py_mp.Queue,
            full_queue: py_mp.Queue,
            model: torch.nn.Module,
            buffers: Buffers,
            initial_agent_state_buffers,
    ):
        try:
            self.logger.info("Actor %i started.", actor_index)
            timings = prof.Timings()  # Keep track of how fast things are.

            gym_env, seed = Utils.make_env(task_flags.env_spec, create_seed=True)
            self.logger.info(f"Environment and libraries setup with seed {seed}")

            # Parameters involved in rendering behavior video
            observations_to_render = []  # Only populated by actor 0

            env = environment.Environment(gym_env)
            env_output = env.initial()
            agent_state = model.initial_state(batch_size=1)
            agent_output, unused_state = model(env_output, task_flags.action_space_id, agent_state)
            while True:
                index = free_queue.get()
                if index is None:
                    break

                # Write old rollout end.
                for key in env_output:
                    buffers[key][index][0, ...] = env_output[key]
                for key in agent_output:
                    buffers[key][index][0, ...] = agent_output[key]
                for i, tensor in enumerate(agent_state):
                    initial_agent_state_buffers[index][i][...] = tensor

                # Do new rollout.
                for t in range(model_flags.unroll_length):
                    timings.reset()

                    with torch.no_grad():
                        agent_output, agent_state = model(env_output, task_flags.action_space_id, agent_state)

                    timings.time("model")

                    env_output = env.step(agent_output["action"])

                    timings.time("step")

                    for key in env_output:
                        buffers[key][index][t + 1, ...] = env_output[key]
                    for key in agent_output:
                        buffers[key][index][t + 1, ...] = agent_output[key]

                    # Save off video if appropriate
                    if actor_index == 0:
                        if env_output['done'].squeeze():
                            # If we have a video in there, replace it with this new one
                            try:
                                self._videos_to_log.get(timeout=1)
                            except queue.Empty:
                                pass
                            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, RuntimeError) as e:
                                # Sometimes it seems like the videos_to_log socket fails. Since video logging is not
                                # mission-critical, just let it go.
                                self.logger.warning(
                                    f"Video logging socket seems to have failed with error {e}. Aborting video log.")
                                pass

                            self._videos_to_log.put(copy.deepcopy(observations_to_render))
                            observations_to_render.clear()

                        observations_to_render.append(env_output['frame'].squeeze(0).squeeze(0)[-1])

                    timings.time("write")

                new_buffers = {key: buffers[key][index] for key in buffers.keys()}
                self.on_act_unroll_complete(task_flags, actor_index, agent_output, env_output, new_buffers)
                full_queue.put(index)

            if actor_index == 0:
                self.logger.info("Actor %i: %s", actor_index, timings.summary())

        except KeyboardInterrupt:
            pass  # Return silently.
        except Exception as e:
            self.logger.error(f"Exception in worker process {actor_index}: {e}")
            traceback.print_exc()
            print()
            raise e

    def get_batch(
            self,
            flags,
            free_queue: py_mp.Queue,
            full_queue: py_mp.Queue,
            buffers: Buffers,
            initial_agent_state_buffers,
            timings,
            lock,
    ):
        with lock:
            timings.time("lock")
            indices = [full_queue.get() for _ in range(flags.batch_size)]
            timings.time("dequeue")
        batch = {
            key: torch.stack([buffers[key][m] for m in indices], dim=1) for key in buffers
        }
        initial_agent_state = (
            torch.cat(ts, dim=1)
            for ts in zip(*[initial_agent_state_buffers[m] for m in indices])
        )
        timings.time("batch")
        for m in indices:
            free_queue.put(m)
        timings.time("enqueue")

        batch = {k: t.to(device=flags.device, non_blocking=True) for k, t in batch.items()}
        initial_agent_state = tuple(
            t.to(device=flags.device, non_blocking=True) for t in initial_agent_state
        )
        timings.time("device")
        return batch, initial_agent_state

    def compute_loss(self, model_flags, task_flags, learner_model, batch, initial_agent_state, with_custom_loss=True):
        # Note the action_space_id isn't really used - it's used to generate an action, but we use the action that
        # was already computed and executed
        learner_outputs, unused_state = learner_model(batch, task_flags.action_space_id, initial_agent_state)

        # Take final value function slice for bootstrapping.
        bootstrap_value = learner_outputs["baseline"][-1]

        # Move from obs[t] -> action[t] to action[t] -> obs[t].
        batch = {key: tensor[1:] for key, tensor in batch.items()}
        learner_outputs = {key: tensor[:-1] for key, tensor in learner_outputs.items()}

        rewards = batch["reward"]
        if model_flags.reward_clipping == "abs_one":
            clipped_rewards = torch.clamp(rewards, -1, 1)
        elif model_flags.reward_clipping == "none":
            clipped_rewards = rewards

        discounts = (~batch["done"]).float() * model_flags.discounting

        vtrace_returns = vtrace.from_logits(
            behavior_policy_logits=batch["policy_logits"],
            target_policy_logits=learner_outputs["policy_logits"],
            actions=batch["action"],
            discounts=discounts,
            rewards=clipped_rewards,
            values=learner_outputs["baseline"],
            bootstrap_value=bootstrap_value,
        )

        pg_loss = self.compute_policy_gradient_loss(
            learner_outputs["policy_logits"],
            batch["action"],
            vtrace_returns.pg_advantages,
        )
        baseline_loss = model_flags.baseline_cost * self.compute_baseline_loss(
            vtrace_returns.vs - learner_outputs["baseline"]
        )
        entropy_loss = model_flags.entropy_cost * self.compute_entropy_loss(
            learner_outputs["policy_logits"]
        )

        total_loss = pg_loss + baseline_loss + entropy_loss
        stats = {
            "pg_loss": pg_loss.item(),
            "baseline_loss": baseline_loss.item(),
            "entropy_loss": entropy_loss.item(),
        }

        if with_custom_loss: # auxilary terms for continual learning
            custom_loss, custom_stats = self.custom_loss(task_flags, learner_model, initial_agent_state)
            total_loss += custom_loss
            stats.update(custom_stats)

        return total_loss, stats, pg_loss, baseline_loss

    def learn(
            self,
            model_flags,
            task_flags,
            actor_model,
            learner_model,
            batch,
            initial_agent_state,
            optimizer,
            scheduler,
            lock,
    ):
        """Performs a learning (optimization) step."""
        with lock:
            # Only log the real batch of new data, not the manipulated version for training, so save it off
            batch_for_logging = copy.deepcopy(batch)

            # Prepare the batch for training (e.g. augmenting with more data)
            batch = self.get_batch_for_training(batch)

            total_loss, stats, _, _ = self.compute_loss(model_flags, task_flags, learner_model, batch, initial_agent_state)

            # The episode_return may be nan if we're using an EpisodicLifeEnv (for Atari), where episode_return is nan
            # until the end of the game, where a real return is produced.
            batch_done_flags = batch_for_logging["done"] * ~torch.isnan(batch_for_logging["episode_return"])
            episode_returns = batch_for_logging["episode_return"][batch_done_flags]
            stats.update({
                "episode_returns": tuple(episode_returns.cpu().numpy()),
                "mean_episode_return": torch.mean(episode_returns).item(),
                "total_loss": total_loss.item(),
            })

            optimizer.zero_grad()
            total_loss.backward()

            norm = nn.utils.clip_grad_norm_(learner_model.parameters(), model_flags.grad_norm_clipping)
            stats["total_norm"] = norm.item()

            optimizer.step()
            if scheduler is not None:
                scheduler.step()
            actor_model.load_state_dict(learner_model.state_dict())
            return stats

    def create_buffer_specs(self, unroll_length, obs_shape, num_actions):
        T = unroll_length
        specs = dict(
            frame=dict(size=(T + 1, *obs_shape), dtype=torch.uint8),
            reward=dict(size=(T + 1,), dtype=torch.float32),
            done=dict(size=(T + 1,), dtype=torch.bool),
            episode_return=dict(size=(T + 1,), dtype=torch.float32),
            episode_step=dict(size=(T + 1,), dtype=torch.int32),
            policy_logits=dict(size=(T + 1, num_actions), dtype=torch.float32),
            baseline=dict(size=(T + 1,), dtype=torch.float32),
            last_action=dict(size=(T + 1,), dtype=torch.int64),
            action=dict(size=(T + 1,), dtype=torch.int64),
        )
        return specs

    def create_buffers(self, flags, obs_shape, num_actions) -> Buffers:
        specs = self.create_buffer_specs(flags.unroll_length, obs_shape, num_actions)
        buffers: Buffers = {key: [] for key in specs}
        for _ in range(flags.num_buffers):
            for key in buffers:
                buffers[key].append(torch.empty(**specs[key]).share_memory_())
        return buffers

    def create_learn_threads(self, batch_and_learn, stats_lock, thread_free_queue, thread_full_queue):
        learner_thread_states = [LearnerThreadState() for _ in range(self._model_flags.num_learner_threads)]
        batch_lock = threading.Lock()
        learn_lock = threading.Lock()
        threads = []
        for i in range(self._model_flags.num_learner_threads):
            thread = threading.Thread(
                target=batch_and_learn, name="batch-and-learn-%d" % i, args=(i, stats_lock, learner_thread_states[i], batch_lock, learn_lock, thread_free_queue, thread_full_queue)
            )
            thread.start()
            threads.append(thread)
        return threads, learner_thread_states

    def cleanup(self):
        # Pause the learner so we don't keep churning out results when we're done (or something died)
        self.logger.info("Cleaning up learners")
        for thread_state in self._learner_thread_states:
            thread_state.state = LearnerThreadState.STOP_REQUESTED

        self.logger.info("Cleaning up actors")
        for actor_index, actor in enumerate(self._actor_processes):
            try:
                actor_process = psutil.Process(actor.pid)
                actor_process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def resume_actor_processes(self, ctx, task_flags, actor_processes, free_queue, full_queue, initial_agent_state_buffers):
        # Copy, so iterator and what's being updated are separate
        actor_processes_copy = actor_processes.copy()
        for actor_index, actor in enumerate(actor_processes_copy):
            actor_process = None
            allowed_statuses = ["running", "sleeping", "disk-sleep"]

            try:
                actor_process = psutil.Process(actor.pid)
                actor_process.resume()
                recreate_actor = not actor_process.is_running() or actor_process.status() not in allowed_statuses
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                recreate_actor = True

            if recreate_actor:
                if actor_process is not None:
                    actor_process.kill()

                self.logger.warn(
                    f"Actor with pid {actor.pid} in actor index {actor_index} was unable to be restarted. Recreating...")
                new_actor = ctx.Process(
                    target=self.act,
                    args=(
                        self._model_flags,
                        task_flags,
                        actor_index,
                        free_queue,
                        full_queue,
                        self.actor_model,
                        self.buffers,
                        initial_agent_state_buffers,
                    ),
                )
                new_actor.start()
                actor_processes[actor_index] = new_actor

    def save(self, output_path):
        if self._model_flags.disable_checkpoint:
            return

        model_file_path = os.path.join(output_path, "model.tar")

        # Back up previous model (sometimes they can get corrupted)
        if os.path.exists(model_file_path):
            shutil.copyfile(model_file_path, os.path.join(output_path, "model_bak.tar"))

        # Save the model
        self.logger.info(f"Saving model to {output_path}")

        checkpoint_data = {
                "model_state_dict": self.actor_model.state_dict(),
                "optimizer_state_dict": self.optimizer.state_dict(),
            }
        if self._scheduler is not None:
            checkpoint_data["scheduler_state_dict"] = self._scheduler.state_dict()

        torch.save(checkpoint_data, model_file_path)

        # Save metadata
        metadata_path = os.path.join(output_path, "impala_metadata.json")
        metadata = {"last_timestep_returned": self.last_timestep_returned}
        with open(metadata_path, "w+") as metadata_file:
            json.dump(metadata, metadata_file)

    def load(self, output_path):
        model_file_path = os.path.join(output_path, "model.tar")
        if os.path.exists(model_file_path):
            self.logger.info(f"Loading model from {output_path}")
            checkpoint = torch.load(model_file_path, map_location="cpu")
            #self.learner_model = self.learner_model.to("cpu")  # So we can load the model in conveniently

            self.actor_model.load_state_dict(checkpoint["model_state_dict"])
            self.learner_model.load_state_dict(checkpoint["model_state_dict"])
            self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])  # TODO: something is awry with devices in this loading
            self._scheduler_state_dict = checkpoint["scheduler_state_dict"]

            #self.learner_model = self.learner_model.to(device=self._model_flags.device)
        else:
            self.logger.info("No model to load, starting from scratch")

        # Load metadata
        metadata_path = os.path.join(output_path, "impala_metadata.json")
        if os.path.exists(metadata_path):
            self.logger.info(f"Loading impala metdata from {metadata_path}")
            with open(metadata_path, "r") as metadata_file:
                metadata = json.load(metadata_file)

            self.last_timestep_returned = metadata["last_timestep_returned"]

    def train(self, task_flags):  # pylint: disable=too-many-branches, too-many-statements
        T = self._model_flags.unroll_length
        B = self._model_flags.batch_size

        def lr_lambda(epoch):
            return 1 - min(epoch * T * B, task_flags.total_steps) / task_flags.total_steps

        if self._model_flags.scheduler:
            self._scheduler = torch.optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda)
        else:
            self._scheduler = None

        if self._scheduler is not None and self._scheduler_state_dict is not None:
            self.logger.info("Loading scheduler state dict")
            self._scheduler.load_state_dict(self._scheduler_state_dict)
            self._scheduler_state_dict = None

        # Add initial RNN state.
        initial_agent_state_buffers = []
        for _ in range(self._model_flags.num_buffers):
            state = self.actor_model.initial_state(batch_size=1)
            for t in state:
                t.share_memory_()
            initial_agent_state_buffers.append(state)

        # Setup actor processes and kick them off
        self._actor_processes = []
        ctx = mp.get_context("fork")

        # See: https://stackoverflow.com/questions/47085458/why-is-multiprocessing-queue-get-so-slow for why Manager
        free_queue = py_mp.Manager().Queue()
        full_queue = py_mp.Manager().Queue()

        for i in range(self._model_flags.num_actors):
            actor = ctx.Process(
                target=self.act,
                args=(
                    self._model_flags,
                    task_flags,
                    i,
                    free_queue,
                    full_queue,
                    self.actor_model,
                    self.buffers,
                    initial_agent_state_buffers,
                ),
            )
            actor.start()
            self._actor_processes.append(actor)

        stat_keys = [
            "total_loss",
            "mean_episode_return",
            "pg_loss",
            "baseline_loss",
            "entropy_loss",
        ]
        self.logger.info("# Step\t%s", "\t".join(stat_keys))

        step, collected_stats = self.last_timestep_returned, {}
        stats_lock = threading.Lock()

        def batch_and_learn(i, lock, thread_state, batch_lock, learn_lock, thread_free_queue, thread_full_queue):
            """Thread target for the learning process."""
            try:
                nonlocal step, collected_stats
                timings = prof.Timings()

                while True:
                    # If we've requested a stop, indicate it and end the thread
                    with thread_state.lock:
                        if thread_state.state == LearnerThreadState.STOP_REQUESTED:
                            thread_state.state = LearnerThreadState.STOPPED
                            return

                        thread_state.state = LearnerThreadState.RUNNING

                    timings.reset()
                    batch, agent_state = self.get_batch(
                        self._model_flags,
                        thread_free_queue,
                        thread_full_queue,
                        self.buffers,
                        initial_agent_state_buffers,
                        timings,
                        batch_lock,
                    )
                    stats = self.learn(
                        self._model_flags, task_flags, self.actor_model, self.learner_model, batch, agent_state, self.optimizer, self._scheduler, learn_lock
                    )
                    timings.time("learn")
                    with lock:
                        step += T * B
                        to_log = dict(step=step)
                        to_log.update({k: stats[k] for k in stat_keys})
                        self.plogger.info(to_log)

                        # We might collect stats more often than we return them to the caller, so collect them all
                        for key in stats.keys():
                            if key not in collected_stats:
                                collected_stats[key] = []

                            if isinstance(stats[key], tuple) or isinstance(stats[key], list):
                                collected_stats[key].extend(stats[key])
                            else:
                                collected_stats[key].append(stats[key])
            except Exception as e:
                self.logger.error(f"Learner thread failed with exception {e}")
                raise e

            if i == 0:
                self.logger.info("Batch and learn: %s", timings.summary())

            thread_state.state = LearnerThreadState.STOPPED

        for m in range(self._model_flags.num_buffers):
            free_queue.put(m)

        threads, self._learner_thread_states = self.create_learn_threads(batch_and_learn, stats_lock, free_queue, full_queue)

        timer = timeit.default_timer
        try:
            while True:
                start_step = step
                start_time = timer()
                time.sleep(self._model_flags.seconds_between_yields)

                # Copy right away, because there's a race where stats can get re-set and then certain things set below
                # will be missing (eg "step")
                with stats_lock:
                    stats_to_return = copy.deepcopy(collected_stats)
                    collected_stats.clear()

                sps = (step - start_step) / (timer() - start_time)

                # Aggregate our collected values. Do it with mean so it's not sensitive to the number of times
                # learning occurred in the interim
                mean_return = np.array(stats_to_return.get("episode_returns", [np.nan])).mean()
                stats_to_return["mean_episode_return"] = mean_return

                # Make a copy of the keys so we're not updating it as we iterate over it
                for key in list(stats_to_return.keys()).copy():
                    if key.endswith("loss") or key == "total_norm":
                        # Replace with the number we collected and the mean value, otherwise the logs are very verbose
                        stats_to_return[f"{key}_count"] = len(np.array(stats_to_return.get(key, [])))
                        stats_to_return[key] = np.array(stats_to_return.get(key, [np.nan])).mean()

                self.logger.info(
                    "Steps %i @ %.1f SPS. Mean return %f. Stats:\n%s",
                    step,
                    sps,
                    mean_return,
                    pprint.pformat(stats_to_return),
                )
                stats_to_return["step"] = step
                stats_to_return["step_delta"] = step - self.last_timestep_returned

                try:
                    video = self._videos_to_log.get(block=False)
                    stats_to_return["video"] = video
                except queue.Empty:
                    pass
                except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, RuntimeError) as e:
                    # Sometimes it seems like the videos_to_log socket fails. Since video logging is not
                    # mission-critical, just let it go.
                    self.logger.warning(f"Video logging socket seems to have failed with error {e}. Aborting video log.")
                    pass

                # This block sets us up to yield our results in batches, pausing everything while yielded.
                if self.last_timestep_returned != step:
                    self.last_timestep_returned = step

                    # Tell the learn thread to pause. Do this before the actors in case we need to do a last batch
                    self.logger.info("Stopping learners")
                    for thread_id, thread_state in enumerate(self._learner_thread_states):
                        wait = False
                        with thread_state.lock:
                            if thread_state.state != LearnerThreadState.STOPPED and threads[thread_id].is_alive():
                                thread_state.state = LearnerThreadState.STOP_REQUESTED
                                wait = True

                        # Wait for it to stop, otherwise we have training overlapping with eval, and possibly
                        # the thread creation below
                        if wait:
                            thread_state.wait_for([LearnerThreadState.STOPPED], timeout=30)

                    # The actors will keep going unless we pause them, so...do that.
                    if self._model_flags.pause_actors_during_yield:
                        for actor in self._actor_processes:
                            psutil.Process(actor.pid).suspend()

                    # Make sure the queue is empty (otherwise things can get dropped in the shuffle)
                    # (Not 100% sure relevant but:) https://stackoverflow.com/questions/19257375/python-multiprocessing-queue-put-not-working-for-semi-large-data
                    while not free_queue.empty():  # TODO: debugging
                        try:
                            free_queue.get(block=False)
                        except queue.Empty:
                            # Race between empty check and get, I guess
                            break

                    while not full_queue.empty():  # TODO: debugging
                        try:
                            full_queue.get(block=False)
                        except queue.Empty:
                            # Race between empty check and get, I guess
                            break

                    yield stats_to_return

                    # Ensure everything is set back up to train
                    self.actor_model.train()
                    self.learner_model.train()

                    # Resume the actors. If one is dead, replace it with a new one
                    if self._model_flags.pause_actors_during_yield:
                        self.resume_actor_processes(ctx, task_flags, self._actor_processes, free_queue, full_queue,
                                                    initial_agent_state_buffers)

                    # Resume the learners by creating new ones
                    self.logger.info("Restarting learners")
                    threads, self._learner_thread_states = self.create_learn_threads(batch_and_learn, stats_lock, free_queue, full_queue)
                    self.logger.info("Restart complete")

                    for m in range(self._model_flags.num_buffers):
                        free_queue.put(m)
                    self.logger.info("Free queue re-populated")

            # # We've finished the task, so reset the appropriate counter
            # self.last_timestep_returned = 0

        except KeyboardInterrupt:
            return  # Try joining actors then quit.
        else:
            for thread in threads:
                thread.join()
            self.logger.info("Learning finished after %d steps.", step)
        finally:
            self.cleanup()

    @staticmethod
    def _collect_test_episode(pickled_args):
        task_flags, logger, model = cloudpickle.loads(pickled_args)

        gym_env, seed = Utils.make_env(task_flags.env_spec, create_seed=True)
        logger.info(f"Environment and libraries setup with seed {seed}")
        env = environment.Environment(gym_env)
        observation = env.initial()
        done = False
        step = 0
        returns = []

        while not done:
            if task_flags.mode == "test_render":
                env.gym_env.render()
            agent_outputs = model(observation, task_flags.action_space_id)
            policy_outputs, _ = agent_outputs
            observation = env.step(policy_outputs["action"])
            step += 1
            done = observation["done"].item() and not torch.isnan(observation["episode_return"])

            # NaN if the done was "fake" (e.g. Atari). We want real scores here so wait for the real return.
            if done:
                returns.append(observation["episode_return"].item())
                logger.info(
                    "Episode ended after %d steps. Return: %.1f",
                    observation["episode_step"].item(),
                    observation["episode_return"].item(),
                )

        env.close()
        return step, returns

    def test(self, task_flags, num_episodes: int = 10):
        if not self._model_flags.no_eval_mode:
            self.actor_model.eval()

        async_objs = []
        returns = []
        step = 0

        with Pool(processes=num_episodes) as pool:
            for episode_id in range(num_episodes):
                pickled_args = cloudpickle.dumps((task_flags, self.logger, self.actor_model))
                async_obj = pool.apply_async(self._collect_test_episode, (pickled_args,))
                async_objs.append(async_obj)

            for async_obj in async_objs:
                episode_step, episode_returns = async_obj.get()
                step += episode_step
                returns.extend(episode_returns)

        self.logger.info(
            "Average returns over %i episodes: %.1f", num_episodes, sum(returns) / len(returns)
        )
        stats = {"episode_returns": returns, "step": step, "num_episodes": num_episodes}

        yield stats
