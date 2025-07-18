# Copyright (c) 2022-2025, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

import torch

from isaaclab.scene import InteractiveScene
from isaaclab.utils import configclass

from isaaclab_tasks.rans.domain_randomization import RandomizationCore, RandomizationCoreCfg, Registerable
from isaaclab_tasks.rans.utils import PerEnvSeededRNG


@configclass
class NoisyActionsCfg(RandomizationCoreCfg):
    randomization_modes: list = ["none"]
    """The randomization modes to apply to the actions."""

    slices: list[tuple[int, int] | int] = []
    """The slices of the actions to apply the noise to."""

    max_delta: list[float] = []
    """The maximum delta to apply to the actions when using a uniform distribution. Default is [].
    The length of the list must be the same as the number of slices."""

    std: list[float] = []
    """The standard deviation of the normal distribution used to sample the delta from. Default is [].
    The length of the list must be the same as the number of slices. The mean of the distribution is always set
    to 0."""

    # Used for clipping
    clip_actions: list[tuple[float, float]] | None = None
    """The minimum and maximum values for the actions. Default is None (no clipping).
    The length of the list must be the same as the number of slices, unless it's set to none."""

    # Define the size of the generative space associated with the randomization
    gen_space: int = 1  # DO NOT EDIT


class NoisyActions(Registerable, RandomizationCore):
    _cfg: NoisyActionsCfg

    def __init__(
        self,
        cfg: NoisyActionsCfg,
        rng: PerEnvSeededRNG,
        scene: InteractiveScene,
        num_envs: int = 1,
        device: str = "cuda",
        **kwargs,
    ):
        """Action noise randomization class.

        Args:
            cfg: The configuration for the randomization.
            rng: The random number generator.
            scene: The scene to get the assets from.
            num_envs: The number of environments.
            device: The device on which the tensors are stored."""

        super().__init__(cfg, rng, scene, num_envs, device)

    @property
    def data(self) -> dict[str, torch.Tensor]:
        """The maximum amount of noise applicable to that env.

        Returns:
            dict[str, torch.Tensor]: The maximum amount of noise applicable to that env."""

        if "uniform" in self._cfg.randomization_modes:
            return {"action_noise": torch.stack(self._max_action_noise)}
        elif "normal" in self._cfg.randomization_modes:
            return {"action_noise": torch.stack(self._std_action_noise)}
        else:
            return {}

    @property
    def data_shape(self) -> dict[str, tuple[int]]:
        """The shape of the data.

        Returns:
            dict[str, tuple[int]]: The shape of the data."""

        if "uniform" in self._cfg.randomization_modes or "normal" in self._cfg.randomization_modes:
            return {"action_noise:", (len(self._cfg.slices),)}
        else:
            return {}

    def check_cfg(self) -> None:
        """Check the configuration of the randomization."""

        super().check_cfg()

        # Check the slices
        assert len(self._cfg.slices) > 0, "The slices must be defined."
        # Check the min max values
        if self._cfg.clip_actions is not None:
            assert len(self._cfg.slices) == len(
                self._cfg.clip_actions
            ), "The length of 'clip_actions' must be the same as 'slices'."
        # Check the randomization modes
        if "uniform" in self._cfg.randomization_modes:
            assert "normal" not in self._cfg.randomization_modes, "The 'uniform' and 'normal' modes cannot be combined."
            assert len(self._cfg.max_delta) == len(
                self._cfg.slices
            ), "The length of 'max_delta' must be the same as 'slices'."
        if "normal" in self._cfg.randomization_modes:
            assert (
                "uniform" not in self._cfg.randomization_modes
            ), "The 'uniform' and 'normal' modes cannot be combined."
            assert len(self._cfg.std) == len(self._cfg.slices), "The length of 'std' must be the same as 'slices'."

    def pre_setup(self) -> None:
        """Setup the action noise randomization."""

        super().pre_setup()

    def fn_on_setup_uniform(self, **kwargs) -> None:
        """Setup the uniform randomization."""

        # Set up the max_action_noise buffer
        self._max_action_noise = []
        for max_delta in self._cfg.max_delta:
            self._max_action_noise.append(torch.zeros((self._num_envs,), device=self._device))

    def fn_on_setup_normal(self, **kwargs) -> None:
        """Setup the normal randomization."""

        # Set up the std_action_noise buffer
        self._mean_action_noise = []
        self._std_action_noise = []
        for std in self._cfg.std:
            self._std_action_noise.append(torch.zeros((self._num_envs,), device=self._device))
            self._mean_action_noise.append(torch.zeros((self._num_envs,), device=self._device))

    def default_reset(self, env_ids: torch.Tensor | None, **kwargs) -> None:
        """The default reset function for the randomization."""

        # Do nothing
        return

    def default_update(self, **kwargs) -> None:
        """The default update function for the randomization."""

        # Do nothing
        return

    def fn_on_reset_uniform(
        self, env_ids: torch.Tensor | None = None, gen_actions: torch.Tensor | None = None, **kwargs
    ) -> None:
        """Randomizes the ranges used to apply noise to the actions. The update function applies the uniform noise by
        adding it to the actions. "max_delta" is used to determine the upper bound of the noise, while the lower bound
        is set to the negative of the computed upper bound. For each slice of the actions, the function uses the same
        generative action to determine the upper bound of the noise. This means that the same generative action will be
        used for all the slices.

        Args:
            env_ids: The ids of the environments.
            gen_actions: The actions taken by the agent."""

        if gen_actions is None:
            gen_actions = self._rng.sample_uniform_torch(0, 1, (1,), env_ids)

        if env_ids is None:
            env_ids = self._ALL_INDICES

        for i, max_delta in enumerate(self._cfg.max_delta):
            self._max_action_noise[i][env_ids] = max_delta * gen_actions

    def fn_on_reset_normal(
        self, env_ids: torch.Tensor | None = None, gen_actions: torch.Tensor | None = None, **kwargs
    ) -> None:
        """Randomizes the standard deviation used to apply the normal noise to the actions. The update function applies
        the normal noise centered in 0 by adding it to the actions. For each slice of the actions, the reset uses the
        same generative action to determine the upper bound of the noise. This means that the same generative action
        will be used for all the slices.

        Args:
            env_ids: The ids of the environments.
            gen_actions: The actions taken by the agent."""

        if gen_actions is None:
            gen_actions = self._rng.sample_uniform_torch(0, 1, (1,), env_ids)

        if env_ids is None:
            env_ids = self._ALL_INDICES

        for i, std in enumerate(self._cfg.std):
            self._std_action_noise[i][env_ids] = std * gen_actions

    def fn_on_actions_uniform(self, actions: torch.Tensor | None = None, **kwargs) -> None:
        """Apply the uniform noise to the actions. This function modifies the actions in place.

        Args:
            actions: The actions taken by the agent."""

        for i, slice in enumerate(self._cfg.slices):
            if isinstance(slice, int):
                actions[:, slice] += self._rng.sample_uniform_torch(
                    -self._max_action_noise[i], self._max_action_noise[i], (1,)
                )
                if self._cfg.clip_actions is not None:
                    actions[:, slice].clip_(self._cfg.clip_actions[i][0], self._cfg.clip_actions[i][1])
            else:
                actions[:, slice[0] : slice[1]] += self._rng.sample_uniform_torch(
                    -self._max_action_noise[i], self._max_action_noise[i], (slice[1] - slice[0],)
                )
                if self._cfg.clip_actions is not None:
                    actions[:, slice[0] : slice[1]].clip_(self._cfg.clip_actions[i][0], self._cfg.clip_actions[i][1])

    def fn_on_actions_normal(self, actions: torch.Tensor | None = None, **kwargs) -> None:
        """Apply the normal noise to the actions. This function modifies the actions in place.

        Args:
            actions: The actions taken by the agent."""

        for i, slice in enumerate(self._cfg.slices):
            if isinstance(slice, int):
                actions[:, slice] += self._rng.sample_normal_torch(
                    self._mean_action_noise[i], self._std_action_noise[i], (1,)
                )
                if self._cfg.clip_actions is not None:
                    actions[:, slice].clip_(self._cfg.clip_actions[i][0], self._cfg.clip_actions[i][1])
            else:
                actions[:, slice[0] : slice[1]] += self._rng.sample_normal_torch(
                    self._mean_action_noise[i], self._std_action_noise[i], (slice[1] - slice[0],)
                )
                if self._cfg.clip_actions is not None:
                    actions[:, slice[0] : slice[1]].clip_(self._cfg.clip_actions[i][0], self._cfg.clip_actions[i][1])

    def apply_randomization(self, ids: torch.Tensor | None = None) -> None:
        """Everything is done in place. Nothing to update.

        Args:
            ids: The ids of the environments."""

        return
