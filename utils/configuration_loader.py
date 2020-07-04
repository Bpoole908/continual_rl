import os
import subprocess
import copy
import json
import datetime
from available_policies import get_available_policies
from experiment_specs import get_available_experiments


class ConfigurationLoader(object):
    """
    Loads a configuration from a raw dictionary into the appropriate experiment spec and policy config objects.
    Also sets up the output directory where the new experiment will be stored.
    """

    @classmethod
    def _get_policy_and_experiment_from_raw_config(cls, raw_config, experiment_output_dir):
        """
        The config dictionary will tell us which policy to use, which will allow us to populate the correct config file.
        """
        # Load the available policies and experiments
        available_policies = get_available_policies()
        available_experiments = get_available_experiments(experiment_output_dir)

        # Extract the spec of the experiment we will be running
        experiment_id = raw_config.pop("experiment")
        experiment_spec = available_experiments[experiment_id]

        # Extract the configuration of the policy we will be running
        policy_id = raw_config.pop("policy")
        policy_config_class = available_policies[policy_id].config
        policy_config = policy_config_class.load_single_experiment_from_config(raw_config)

        # Make the output dir accessible on the config itself, so more things can be put there as necessary.
        policy_config.set_experiment_output_dir(experiment_output_dir)

        return experiment_spec, policy_config

    @classmethod
    def _get_script_dir_commit_hash(cls):
        current_working_dir = os.getcwd()
        script_dir = os.path.realpath(__file__)
        print("Script file path: {}".format(script_dir))
        os.chdir(os.path.dirname(script_dir))

        commit = subprocess.check_output(["git", "describe", "--always"]).strip()

        os.chdir(current_working_dir)
        return commit

    @classmethod
    def _write_json_log_file(cls, experiment_json, output_path):
        experiment_json = copy.deepcopy(experiment_json)
        experiment_json["commit"] = str(cls._get_script_dir_commit_hash())
        experiment_json["timestamp"] = str(datetime.datetime.utcnow())

        output_file_path = os.path.join(output_path, "experiment.json")

        with open(output_file_path, "w") as output_file:
            output_file.write(json.dumps(experiment_json))

    @classmethod
    def load_next_experiment_from_config(cls, output_dir, config_path):
        """
        Reads the configuration dictionary from the config_path, and loads the next entry to run.
        Returns None if there is nothing further to load.
        """
        # Instead of dumping directly into the output directory, we'll make a folder with the same name as the
        # experiment file. This allows for multiple experiment sets
        json_experiment_name = os.path.basename(os.path.splitext(config_path)[0])
        output_directory = os.path.join(output_dir, json_experiment_name)

        with open(config_path) as json_file:
            json_raw = json_file.read()
            experiments = json.loads(json_raw)

        return cls.load_next_experiment_from_dicts(output_directory, experiments, subdirectory_from_timestamp=False)

    @classmethod
    def load_next_experiment_from_dicts(cls, experiment_base_directory, experiments, subdirectory_from_timestamp=True):
        """
        Given a list of experiments (i.e. a list of dictionaries), load the next one. Its results will be saved in
        experiment_output_directory.
        If we create subdirectories, the results will be output_dir/0, output_dir/1, etc, with one per separate
        configuration entry.

        Each experiment configuration dictionary must have a "policy" entry and an "experiment" entry, at minimum.
        """
        try:
            os.makedirs(experiment_base_directory)
        except FileExistsError:
            pass

        if subdirectory_from_timestamp:
            assert len(experiments) == 0, "Multiple experiments available, but only one was expected."

            # Run this experiment no matter what, in a folder based on the timestamp
            next_experiment_id = 0
            experiment = experiments[next_experiment_id]
            output_name = f"{experiment['policy']}_{experiment['experiment']}_{datetime.datetime.utcnow()}"
            experiment_output_dir = os.path.join(experiment_base_directory, output_name)
        else:
            # Load up the first experiment we haven't yet started
            # Start by grabbing all the folders (representing old experiments) that currently exist
            # These will be, '0', '1', '2', '3', etc.
            existing_experiments = os.listdir(experiment_base_directory)
            next_experiment_id = None

            # Find the first experiment (ie numbered folder) that doesn't yet exist
            # We do it this way so that if folders '0' and '2' exist, we will run '1' now.
            for experiment_id in range(len(experiments)):
                if not str(experiment_id) in existing_experiments:
                    next_experiment_id = experiment_id
                    break
            experiment_output_dir = os.path.join(experiment_base_directory, str(next_experiment_id))

        experiment_spec = None
        policy_config = None

        # Inflate the configuration from the raw json
        if next_experiment_id is not None:
            os.makedirs(experiment_output_dir)

            experiment_json = experiments[next_experiment_id]

            # Log some metadata information into an "experiments.json" file in the output directory
            cls._write_json_log_file(experiment_json, experiment_output_dir)

            experiment_spec, policy_config = cls._get_policy_and_experiment_from_raw_config(raw_config=experiment_json,
                                                                                            experiment_output_dir=experiment_output_dir)

            print("Starting job in location: {}".format(experiment_output_dir))

        return experiment_spec, policy_config
