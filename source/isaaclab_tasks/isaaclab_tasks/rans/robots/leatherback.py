# Copyright (c) 2022-2025, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

import numpy as np
import torch
from gymnasium import spaces, vector

from isaaclab.assets import Articulation
from isaaclab.scene import InteractiveScene
from isaaclab.sensors import ContactSensor

from isaaclab_tasks.rans import LeatherbackRobotCfg

from .robot_core import RobotCore


class LeatherbackRobot(RobotCore):
    def __init__(
        self,
        scene: InteractiveScene | None = None,
        robot_cfg: LeatherbackRobotCfg = LeatherbackRobotCfg(),
        robot_uid: int = 0,
        num_envs: int = 1,
        decimation: int = 4,
        device: str = "cuda",
    ):
        super().__init__(scene=scene, robot_uid=robot_uid, num_envs=num_envs, decimation=decimation, device=device)
        self._robot_cfg = robot_cfg
        self._dim_robot_obs = self._robot_cfg.observation_space
        self._dim_robot_act = self._robot_cfg.action_space
        self._dim_gen_act = self._robot_cfg.gen_space

        # Buffers
        self.initialize_buffers()

    def initialize_buffers(self, env_ids=None):
        super().initialize_buffers(env_ids)
        self._previous_actions = torch.zeros(
            (self._num_envs, self._dim_robot_act),
            device=self._device,
            dtype=torch.float32,
        )
        self._throttle_action = torch.zeros((self._num_envs, 1), device=self._device, dtype=torch.float32)
        self._steering_action = torch.zeros((self._num_envs, 1), device=self._device, dtype=torch.float32)

    def run_setup(self, robot: Articulation):
        super().run_setup(robot)
        self._throttle_dof_idx, _ = self._robot.find_joints(self._robot_cfg.throttle_dof_name)
        self._steering_dof_idx, _ = self._robot.find_joints(self._robot_cfg.steering_dof_name)

    def create_logs(self):
        super().create_logs()

        self.scalar_logger.add_log("robot_state", "AVG/throttle_action", "mean")
        self.scalar_logger.add_log("robot_state", "AVG/steering_action", "mean")
        self.scalar_logger.add_log("robot_state", "AVG/action_rate", "mean")
        self.scalar_logger.add_log("robot_state", "AVG/joint_acceleration", "mean")
        self.scalar_logger.add_log("robot_reward", "AVG/action_rate", "mean")
        self.scalar_logger.add_log("robot_reward", "AVG/joint_acceleration", "mean")

    def get_observations(self) -> torch.Tensor:
        return self._unaltered_actions

    def compute_rewards(self):
        # TODO: DT should be factored in?

        # Compute
        action_rate = torch.sum(torch.square(self._unaltered_actions - self._previous_unaltered_actions), dim=1)
        joint_accelerations = torch.sum(torch.square(self.joint_acc), dim=1)

        # Log data
        self.scalar_logger.log("robot_state", "AVG/action_rate", action_rate)
        self.scalar_logger.log("robot_state", "AVG/joint_acceleration", joint_accelerations)
        self.scalar_logger.log("robot_reward", "AVG/action_rate", action_rate)
        self.scalar_logger.log("robot_reward", "AVG/joint_acceleration", joint_accelerations)

        return (
            action_rate * self._robot_cfg.rew_action_rate_scale
            + joint_accelerations * self._robot_cfg.rew_joint_accel_scale
        )

    def get_dones(self) -> tuple[torch.Tensor, torch.Tensor]:
        task_failed = torch.zeros(self._num_envs, dtype=torch.int32, device=self._device)
        task_done = torch.zeros(self._num_envs, dtype=torch.int32, device=self._device)
        return task_failed, task_done

    def reset(
        self,
        env_ids: torch.Tensor,
        gen_actions: torch.Tensor | None = None,
        env_seeds: torch.Tensor | None = None,
    ):
        super().reset(env_ids, gen_actions, env_seeds)
        self._previous_actions[env_ids] = 0

    def set_initial_conditions(self, env_ids: torch.Tensor | None = None):
        throttle_reset = torch.zeros(
            (len(env_ids), self._throttle_action.shape[1]),
            device=self._device,
            dtype=torch.float32,
        )
        steering_reset = torch.zeros(
            (len(env_ids), self._steering_action.shape[1]),
            device=self._device,
            dtype=torch.float32,
        )
        self._robot.set_joint_velocity_target(throttle_reset, joint_ids=self._throttle_dof_idx, env_ids=env_ids)
        self._robot.set_joint_position_target(steering_reset, joint_ids=self._steering_dof_idx, env_ids=env_ids)

    def process_actions(self, actions: torch.Tensor) -> None:
        """Process the actions for the robot.

        Expects the actions to be in the range [-1, 1].

        - First, clip the actions to the action space limits. This is done to avoid violating the robot's limits.
        - Second, apply the action randomizers to the actions. This is done to add noise to the actions, apply
          different scaling factors to the actions, etc.
        - Third, format the actions to send to the actuators.

        Args:
            actions (torch.Tensor): The actions to process."""

        # Enforce action limits at the robot level
        actions.clip_(min=-1.0, max=1.0)
        # Store the unaltered actions, by default the robot should only observe the unaltered actions.
        self._previous_unaltered_actions = self._unaltered_actions.clone()
        self._unaltered_actions = actions.clone()

        # Apply action randomizers
        for randomizer in self.randomizers:
            randomizer.actions(dt=self.scene.physics_dt, actions=actions)

        self._previous_actions = self._actions.clone()
        self._actions = actions
        self._throttle_action = (
            self._actions[:, 0].repeat_interleave(4).reshape((-1, 4)) * self._robot_cfg.throttle_scale
        )
        self._steering_action = (
            self._actions[:, 1].repeat_interleave(2).reshape((-1, 2)) * self._robot_cfg.steering_scale
        )

        # Log data
        self.scalar_logger.log("robot_state", "AVG/throttle_action", self._throttle_action[:, 0])
        self.scalar_logger.log("robot_state", "AVG/steering_action", self._steering_action[:, 0])

    def compute_physics(self):
        pass  # Model motor + ackermann steering here

    def apply_actions(self):
        # Compute the physics
        super().apply_actions()
        for randomizer in self.randomizers:
            randomizer.update(dt=self.scene.physics_dt, actions=self._actions)

        self._robot.set_joint_velocity_target(self._throttle_action, joint_ids=self._throttle_dof_idx)
        self._robot.set_joint_position_target(self._steering_action, joint_ids=self._steering_dof_idx)

    def activateSensors(self, sensor_type: str, filter: list):
        if sensor_type == "contacts":
            self._robot_cfg.contact_sensor_active = True
            if len(filter) > 0:
                self._robot_cfg.chassis_contact_forces.filter_prim_paths_expr = filter

    def register_sensors(self) -> None:
        # Contact sensor
        if self._robot_cfg.contact_sensor_active:
            self.scene.sensors["robot_contacts"] = ContactSensor(self._robot_cfg.chassis_contact_forces)
            self.contacts: ContactSensor = self.scene["robot_contacts"]

    def configure_gym_env_spaces(self):
        single_action_space = spaces.Box(low=-1.0, high=1.0, shape=(2,), dtype=np.float32)
        action_space = vector.utils.batch_space(single_action_space, self._num_envs)

        return single_action_space, action_space

    @property
    def eval_data_keys(self) -> list[str]:
        return ["position", "heading", "linear_velocity", "angular_velocity", "throttle", "steering"]

    @property
    def eval_data(self) -> dict:
        return {
            "position": self.root_pos_w,
            "heading": self.heading_w,
            "linear_velocity": self.root_lin_vel_b,
            "angular_velocity": self.root_ang_vel_b,
            "throttle": self._throttle_action,
            "steering": self._steering_action,
        }
