from multigrid.envs.cleanup import CleanUpEnv
from multigrid.base import MultiGridEnv
from multigrid.example.controller import Controller


def run_episode(controller: Controller, env: MultiGridEnv):
    while True:
        actions = controller.get_actions()
        next_observations, rewards, terminations, truncations, infos = env.step(actions)

        env.render()

        if all(terminations.values()) or all(truncations.values()):
            break


if __name__ == "__main__":
    agents = 2
    env = CleanUpEnv(
        width=10,
        height=10,
        max_steps=250,
        boxes=6,
        agents=1,
        success_termination_mode="any",
    )
    env = CleanUpEnv(
        boxes=5, agents=agents, width=11, height=11, success_termination_mode="any"
    )
    controller = Controller(agents, same_keys=True)
    while True:
        env.reset()
        run_episode(controller, env)
