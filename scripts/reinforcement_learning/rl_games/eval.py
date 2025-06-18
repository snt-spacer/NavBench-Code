# Copyright (c) 2022-2025, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

"""Script to play a checkpoint if an RL agent from RL-Games."""

"""Launch Isaac Sim Simulator first."""

import argparse
import sys

from isaaclab.app import AppLauncher

# add argparse arguments
parser = argparse.ArgumentParser(description="Play a checkpoint of an RL agent from RL-Games.")
parser.add_argument("--video", action="store_true", default=False, help="Record videos during training.")
parser.add_argument("--video_length", type=int, default=200, help="Length of the recorded video (in steps).")
parser.add_argument(
    "--disable_fabric", action="store_true", default=False, help="Disable fabric and use USD I/O operations."
)
parser.add_argument("--num_envs", type=int, default=None, help="Number of environments to simulate.")
parser.add_argument("--task", type=str, default=None, help="Name of the task.")
parser.add_argument("--checkpoint", type=str, default=None, help="Path to model checkpoint.")
parser.add_argument(
    "--use_last_checkpoint",
    action="store_true",
    help="When no checkpoint provided, use the last saved model. Otherwise use the best saved model.",
)
parser.add_argument(
    "--algorithm",
    type=str,
    default="PPO",
    help="The RL algorithm used for training the skrl agent.",
)
parser.add_argument(
    "--horizon",
    type=int,
    default=512,
    help="The maximum number of steps in an episode.",
)

# append AppLauncher cli args
AppLauncher.add_app_launcher_args(parser)
# parse the arguments
args_cli, hydra_args = parser.parse_known_args()
# clear out sys.argv for Hydra
sys.argv = [sys.argv[0]] + hydra_args

# always enable cameras to record video
if args_cli.video:
    args_cli.enable_cameras = True

# launch omniverse app
app_launcher = AppLauncher(args_cli)
simulation_app = app_launcher.app

"""Rest everything follows."""

import gymnasium as gym
import math
import numpy as np
import os

from rl_games.common import env_configurations, vecenv
from rl_games.common.player import BasePlayer
from rl_games.torch_runner import Runner

from isaaclab.envs import (
    DirectMARLEnv,
    DirectMARLEnvCfg,
    DirectRLEnvCfg,
    ManagerBasedRLEnvCfg,
    multi_agent_to_single_agent,
)
from isaaclab.utils.assets import retrieve_file_path
from isaaclab.utils.dict import print_dict

from isaaclab_rl.rl_games import RlGamesGpuEnv, RlGamesVecEnvWrapper

from isaaclab_tasks.rans.utils.performance_evaluator_v2 import PerformanceEvaluatorV2
from isaaclab_tasks.rans.utils.plot_eval_multi import plot_episode_data_virtual
from isaaclab_tasks.utils import get_checkpoint_path
from isaaclab_tasks.utils.hydra import hydra_task_config

algorithm = args_cli.algorithm.lower()
agent_cfg_entry_point = "rl_games_cfg_entry_point" if algorithm in ["ppo"] else f"rl_games_{algorithm}_cfg_entry_point"


@hydra_task_config(args_cli.task, agent_cfg_entry_point)
def main(env_cfg: ManagerBasedRLEnvCfg | DirectRLEnvCfg | DirectMARLEnvCfg, agent_cfg: dict):

    # override configurations with non-hydra CLI arguments
    env_cfg.scene.num_envs = args_cli.num_envs if args_cli.num_envs is not None else env_cfg.scene.num_envs
    env_cfg.sim.device = args_cli.device if args_cli.device is not None else env_cfg.sim.device

    """Play with RL-Games agent."""
    # specify directory for logging experiments
    log_root_path = os.path.join("logs", "rl_games", agent_cfg["params"]["config"]["name"])
    log_root_path = os.path.abspath(log_root_path)
    print(f"[INFO] Loading experiment from directory: {log_root_path}")
    # find checkpoint
    if args_cli.checkpoint is None:
        # specify directory for logging runs
        run_dir = agent_cfg["params"]["config"].get("full_experiment_name", ".*")
        # specify name of checkpoint
        if args_cli.use_last_checkpoint:
            checkpoint_file = ".*"
        else:
            # this loads the best checkpoint
            checkpoint_file = f"{agent_cfg['params']['config']['name']}.pth"
        # get path to previous checkpoint
        resume_path = get_checkpoint_path(log_root_path, run_dir, checkpoint_file, other_dirs=["nn"])
    else:
        resume_path = retrieve_file_path(args_cli.checkpoint)
    log_dir = os.path.dirname(os.path.dirname(resume_path))

    # wrap around environment for rl-games
    rl_device = agent_cfg["params"]["config"]["device"]
    clip_obs = agent_cfg["params"]["env"].get("clip_observations", math.inf)
    clip_actions = agent_cfg["params"]["env"].get("clip_actions", math.inf)
    # create isaac environment
    env = gym.make(args_cli.task, cfg=env_cfg, render_mode="rgb_array" if args_cli.video else None)

    # wrap for video recording
    if args_cli.video:
        video_kwargs = {
            "video_folder": os.path.join(log_root_path, log_dir, "videos", "play"),
            "step_trigger": lambda step: step == 0,
            "video_length": args_cli.video_length,
            "disable_logger": True,
        }
        print("[INFO] Recording videos during training.")
        print_dict(video_kwargs, nesting=4)
        env = gym.wrappers.RecordVideo(env, **video_kwargs)

    # convert to single-agent instance if required by the RL algorithm
    if isinstance(env.unwrapped, DirectMARLEnv):
        env = multi_agent_to_single_agent(env)

    # wrap around environment for rl-games
    env = RlGamesVecEnvWrapper(env, rl_device, clip_obs, clip_actions)

    # register the environment to rl-games registry
    # note: in agents configuration: environment name must be "rlgpu"
    vecenv.register(
        "IsaacRlgWrapper", lambda config_name, num_actors, **kwargs: RlGamesGpuEnv(config_name, num_actors, **kwargs)
    )
    env_configurations.register("rlgpu", {"vecenv_type": "IsaacRlgWrapper", "env_creator": lambda **kwargs: env})

    # load previously trained model
    agent_cfg["params"]["load_checkpoint"] = True
    agent_cfg["params"]["load_path"] = resume_path
    print(f"[INFO]: Loading model checkpoint from: {agent_cfg['params']['load_path']}")

    if "Single" in args_cli.task:
        task_name = env.env.cfg.task_name
        robot_name = env.env.cfg.robot_name
    else:
        task_name = args_cli.task.split("-")[2]
        robot_name = agent_cfg["params"]["config"]["name"].split("_")[0]  # make first letter capital
        robot_name = robot_name[0].upper() + robot_name[1:]

    print_all_agents = False
    # set number of actors into agent config
    agent_cfg["params"]["config"]["num_actors"] = env.unwrapped.num_envs
    # create runner from rl-games
    runner = Runner()
    runner.load(agent_cfg)

    # obtain the agent from the runner
    agent: BasePlayer = runner.create_player()
    agent.restore(resume_path)
    agent.reset()

    # Declare dictionary to store obs, actions, and rewards
    ep_data = {"act": [], "obs": [], "rews": [], "dones": []}

    horizon = args_cli.horizon if args_cli.horizon is not None else 512
    # reset environment
    obs = env.reset()
    if isinstance(obs, dict):
        obs = obs["obs"]
    timestep = 0
    # required: enables the flag for batched observations
    _ = agent.get_batch_size(obs, 1)

    # simulate environment
    # note: We simplified the logic in rl-games player.py (:func:`BasePlayer.run()`) function in an
    #   attempt to have complete control over environment stepping. However, this removes other
    #   operations such as masking that is used for multi-agent learning by RL-Games.
    # while simulation_app.is_running():

    print("Evaluation started over ", horizon, " steps for ", args_cli.num_envs, " environments.")
    for _ in range(horizon):
        # run everything in inference mode
        # with torch.inference_mode():
        # convert obs to agent format
        obs = agent.obs_to_torch(obs)
        # agent stepping
        actions = agent.get_action(obs, is_deterministic=True)
        # env stepping
        obs, rews, dones, _ = env.step(actions)

        ep_data["act"].append(actions.cpu().numpy())
        ep_data["obs"].append(obs.cpu().numpy())
        ep_data["rews"].append(rews.cpu().numpy())
        ep_data["dones"].append(dones.cpu().numpy())

        if args_cli.video:
            timestep += 1
            # Exit the play loop after recording one video
            if timestep == args_cli.video_length:
                break

    # Convert data to numpy arrays
    ep_data["obs"], ep_data["rews"], ep_data["act"] = map(np.array, (ep_data["obs"], ep_data["rews"], ep_data["act"]))

    save_dir = os.path.join(log_root_path, log_dir, f"eval_{args_cli.num_envs}_envs", task_name)
    print("Saving plots in ", save_dir)

    # Plot the episode data
    if print_all_agents:
        print("Plotting data for all agents.")
        plot_episode_data_virtual(
            ep_data,
            save_dir=save_dir,
            task=task_name,
            all_agents=print_all_agents,
        )
    # Run performance evaluation
    # evaluator = PerformanceEvaluator(task_name, env.env.cfg.robot_name, ep_data, horizon)
    # evaluator = PerformanceMetrics(task_name, robot_name, ep_data, horizon, plot_metrics=False, save_path=save_dir)
    # evaluator.compute_basic_metrics()

    # results = evaluator.evaluate()
    # print_dict(results, nesting=4)

    combo_id = f"{robot_name}_{task_name}_rl_games"  # lib is 'skrl' or 'rlgames'
    evaluator = PerformanceEvaluatorV2(task_name, robot_name, "rl_games", ep_data, horizon, combo_id, seed=0)
    metrics = evaluator.evaluate()
    evaluator.save_csv()  # writes per-run CSV
    evaluator.export_timeseries_metrics()
    print_dict(metrics, nesting=4)

    # close the simulator
    env.close()


if __name__ == "__main__":
    # run the main function
    main()
    # close sim app
    simulation_app.close()
