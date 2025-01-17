import logging
from typing import Any, Dict, List, Mapping, SupportsFloat, Tuple

import torch
import torch.nn as nn

from multigrid.utils.typing import AgentID, ObsType
from rllib.algorithms.algorithm import Algorithm
from rllib.algorithms.ppo.ppo_config import PPOConfig
from rllib.core.algorithms.gae import GAE
from rllib.core.memory.trajectory_buffer import Trajectory, TrajectoryBuffer
from rllib.core.network.actor_critic_multi_input_network import (
    ActorCriticMultiInputNetwork,
)
from rllib.utils.ppo.calculations import compute_log_probs, ppo_loss
from rllib.utils.torch.processing import (
    leaf_value_to_torch,
    observations_seperate_to_torch,
    torch_stack_inner_list,
)
from utils.common.collections import zip_dict_list
from utils.core.wandb import LogMethod

"""
PPO Paper: https://arxiv.org/abs/1707.06347
"""

torch.autograd.set_detect_anomaly(True)


class PPO(Algorithm):
    _policy_net: ActorCriticMultiInputNetwork

    def __init__(self, config: PPOConfig):
        super().__init__(config)
        self._config = config
        self._policy_net = ActorCriticMultiInputNetwork(
            self.observation_space, self.action_space
        )

        # NOTE: Remove when debug is resolved
        for name, param in self._policy_net.named_parameters():
            if not param.requires_grad:
                logging.info(f"Parameter {name} requires grad: {param.requires_grad}")

        self._action_probs = {}
        self._values = {}
        self._trajectory_buffer = TrajectoryBuffer(self._config.batch_size)

        self._optimizer = torch.optim.AdamW(
            self._policy_net.parameters(), lr=config.learning_rate, amsgrad=True
        )

    def train_step(
        self,
        observations: dict[AgentID, ObsType],
        next_observations: dict[AgentID, ObsType],
        actions: dict[AgentID, int],
        rewards: dict[AgentID, SupportsFloat],
        terminations: dict[AgentID, bool],
        truncations: dict[AgentID, bool],
        infos: dict[AgentID, dict[str, Any]],
    ):
        dones = {}
        for key in terminations.keys():
            dones[key] = terminations[key] or truncations[key]
        assert len(self._values) != 0
        assert len(self._action_probs) != 0

        self._trajectory_buffer.add(
            states=observations,
            actions=actions,
            action_probs={
                key: value.clone() for key, value in self._action_probs.items()
            },
            values={key: value.clone() for key, value in self._values.items()},
            rewards=rewards,
            dones=dones,
        )
        self._values.clear()
        self._action_probs.clear()

        self._optimize_model()

    def log_episode(self):
        super().log_episode()
        self.log_model(
            self._policy_net, f"model_{self._episodes_done}", self._episodes_done
        )

    def predict(self, observation: Dict[AgentID, ObsType]) -> Dict[AgentID, int]:
        actions, action_probs, values = self._predict(observation, requires_grad=False)
        self._action_probs = {
            key: value.detach() for key, value in action_probs.items()
        }
        self._values = {key: value.clone() for key, value in values.items()}
        for value in actions.values():
            self.add_log(f"action_{value}", 1, LogMethod.CUMULATIVE)
        return actions

    def _predict(
        self, observation: Dict[AgentID, ObsType], requires_grad: bool = False
    ) -> Tuple[
        Dict[AgentID, int],
        Dict[AgentID, torch.Tensor],
        Dict[AgentID, torch.Tensor],
    ]:
        action_probabilities, policy_values = self._get_policy_values(
            list(observation.values()), requires_grad=requires_grad
        )
        actions = {}
        action_probs = {}
        values = {}
        for key, action_prob in zip(observation.keys(), action_probabilities):
            action_probs[key] = action_prob
            actions[key] = self._get_action(action_prob)
            values[key] = policy_values[key]
        return actions, action_probs, values

    def load_model(self, model: Mapping[str, Any]):
        self._policy_net.load_state_dict(model)
        self._policy_net.eval()

    @property
    def model(self) -> nn.Module:
        return self._policy_net

    def _get_action(self, action_prob: torch.Tensor) -> int:
        action = torch.multinomial(action_prob, num_samples=1).item()
        return action

    def _get_policy_values(
        self, observations: List[ObsType], requires_grad: bool = False
    ) -> List[Tuple[torch.Tensor, torch.Tensor]]:
        torch_observations = observations_seperate_to_torch(observations)
        if requires_grad:
            return self._policy_net(*torch_observations)
        with torch.no_grad():
            return self._policy_net(*torch_observations)

    def _optimize_model(self):
        if len(self._trajectory_buffer) != self._trajectory_buffer.maxlen:
            return

        for epoch in range(self._config.epochs):
            self._optimize_model_batch()
        self._trajectory_buffer.clear()

    def _optimize_model_batch(self):
        mini_batch_size = self._config.mini_batch_size
        buffer_list = list(self._trajectory_buffer)
        for i in range(0, len(buffer_list), mini_batch_size):
            batch = buffer_list[i : i + mini_batch_size]
            self._optimize_model_minibatch(batch)

    def _optimize_model_minibatch(self, trajectories: List[Trajectory]) -> float:
        batch = Trajectory(*zip(*trajectories))

        num_agents = len(batch.states[0].keys())

        action_prob_batch = zip_dict_list(batch.action_probs)
        state_batch = zip_dict_list(batch.states)
        action_batch = zip_dict_list(batch.actions)
        reward_batch = zip_dict_list(batch.rewards)
        value_batch = zip_dict_list(batch.values)
        dones = zip_dict_list(batch.dones)

        # NOTE: The code is a bit confusing. So if there is an error or the algorithm doesn't work. Start here.
        action_batch_torch = leaf_value_to_torch(action_batch)
        log_probs = compute_log_probs(
            torch_stack_inner_list(action_batch_torch),
            torch_stack_inner_list(action_prob_batch),
        )

        # TODO: This can be more efficient by passing all the states at once.
        new_action_probs = []
        new_values = []
        for state in batch.states:
            _, action_probs, value = self._predict(state, requires_grad=True)
            new_action_probs.append(action_probs)
            new_values.append(value)

        gae = GAE(
            num_agents, len(state_batch[0]), self._config.gamma, self._config.lambda_
        )

        new_value_batch = zip_dict_list(new_values)
        advantages = gae(dones, reward_batch, value_batch)
        average_advantages = float(torch.mean(torch_stack_inner_list(advantages)))
        self.add_log("advantages", average_advantages, LogMethod.AVERAGE)
        advantages_tensor = torch_stack_inner_list(advantages)
        normalized_advantages = (advantages_tensor - advantages_tensor.mean()) / (
            advantages_tensor.std() + 1e-8
        )

        new_action_probs_batch = zip_dict_list(new_action_probs)
        new_log_probs = compute_log_probs(
            torch_stack_inner_list(action_batch_torch),
            torch_stack_inner_list(new_action_probs_batch),
        )
        reward_batch = torch_stack_inner_list(leaf_value_to_torch(reward_batch))
        new_value_batch = torch_stack_inner_list(new_value_batch)

        policy_loss, value_loss, entropy_loss = ppo_loss(
            log_probs,
            new_log_probs,
            normalized_advantages,
            new_value_batch.view(-1),
            reward_batch.view(-1).to(torch.float32),
            self._config.epsilon,
        )

        self.add_log("policy_loss", policy_loss.item(), LogMethod.AVERAGE)
        self.add_log("value_loss", value_loss.item(), LogMethod.AVERAGE)
        self.add_log("entropy_loss", entropy_loss.item(), LogMethod.AVERAGE)

        loss = (
            policy_loss
            + value_loss * self._config.value_weight
            + entropy_loss * self._config.entropy_weight
        )

        self.add_log("loss", loss.item(), LogMethod.AVERAGE)

        self._optimizer.zero_grad()
        loss.backward()
        for name, param in self._policy_net.named_parameters():
            if param.grad is None:
                logging.info(f"{name}: {param.grad}")
        self._optimizer.step()

        return loss.item()
