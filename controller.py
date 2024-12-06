import copy
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import gymnasium as gym
import matplotlib.pyplot as plt
import numpy as np
import torch
from cma import CMAEvolutionStrategy
from matplotlib.animation import FuncAnimation
from torch import nn
from torchvision import transforms
from tqdm import tqdm

from memory import MDN_RNN
from vision import ConvVAE


class Controller(nn.Module):
    def __init__(
        self, latent_dimension: int = 32, hidden_units: int = 256, continuos=True
    ):
        super().__init__()
        self.continuos = continuos
        self.fc = nn.Linear(latent_dimension + hidden_units, 3 if continuos else 1)

    def forward(
        self, latent_observation: torch.Tensor, hidden_state: torch.Tensor
    ) -> torch.Tensor:

        return torch.tanh(
            self.fc(torch.cat((latent_observation, hidden_state), dim=-1))
        )

    def __call__(
        self, latent_observation: torch.Tensor, hidden_state: torch.Tensor
    ) -> torch.Tensor:
        return self.forward(latent_observation, hidden_state)

    # def act(self, observation: np.ndarray, vision: ConvVAE, memory: MDN_RNN):
    #     observation = transforms.Compose(
    #         [
    #             transforms.ToPILImage(),
    #             transforms.Resize((64, 64)),
    #             transforms.ToTensor(),
    #         ]
    #     )(observation)
    #     latent_observation = self.vision.get_latent(observation.unsqueeze(0))
    #     latent_observation = latent_observation.unsqueeze(0)
    #     action = self.controller(latent_observation, hidden_state)
    #     numpy_action = action.detach().cpu().numpy().ravel()

    def get_weights(self):
        return (
            nn.utils.parameters_to_vector(self.parameters())
            .detach()
            .cpu()
            .numpy()
            .ravel()
        )

    def set_weights(self, weights: np.ndarray):
        nn.utils.vector_to_parameters(
            torch.tensor(weights, dtype=torch.float32), self.parameters()
        )

    @staticmethod
    def from_pretrained(
        model_path: Path = Path("models/controller_continuos.pt"),
    ) -> "MDN_RNN":
        if not model_path.exists():
            raise FileNotFoundError(f"Couldn't find the Mdn-RNN model at {model_path}")
        loaded_data = torch.load(model_path, weights_only=True)
        controller = MDN_RNN(continuos="continuos" in model_path.name)
        controller.load_state_dict(loaded_data)
        return controller


class ControllerTrainer:
    def __init__(
        self,
        controller: Controller,
        vision: ConvVAE,
        memory: MDN_RNN,
        population_size=4,
        env_name="CarRacing-v2",
        render=False,
    ):
        self.controller = controller
        self.vision = vision.eval()
        self.memory = memory.eval()
        self.population_size = population_size
        self.env_name = env_name
        self.n_rows, self.n_cols = self._get_rows_and_cols()
        self.controllers = [
            copy.deepcopy(controller) for _ in range(self.population_size)
        ]
        self.render = render
        self.__transformation = transforms.Compose(
            [
                transforms.ToPILImage(),
                transforms.Resize((64, 64)),
                transforms.ToTensor(),
            ]
        )

    def _get_rows_and_cols(self):
        sqrt_num = math.sqrt(self.population_size)
        n_rows = math.floor(sqrt_num)
        n_cols = math.ceil(self.population_size / n_rows)
        while n_rows * n_cols < self.population_size:
            n_rows += 1
            n_cols = math.ceil(self.population_size / n_rows)
        return n_rows, n_cols

    def _rollout(self, index: int):
        environment = gym.make(self.env_name, render_mode="rgb_array")
        observation, _ = environment.reset()
        hidden_state, cell_state = self.memory.init_hidden()
        cumulative_reward = 0
        frames = []  # Collect frames for visualization
        cnt = 0
        while True:
            cnt += 1
            frames.append(observation)
            observation: torch.Tensor = self.__transformation(observation)
            latent_observation = self.vision.get_latent(observation.unsqueeze(0))
            latent_observation = latent_observation.unsqueeze(0)
            action = self.controller(latent_observation, hidden_state)
            numpy_action = action.detach().cpu().numpy().ravel()
            next_observation, reward, done, _, _ = environment.step(numpy_action)
            cumulative_reward += float(reward)
            if done:
                break
            _mu, _pi, _sigma, hidden_state, cell_state = self.memory.forward(
                latent_observation,
                action,
                hidden_state,
                cell_state,
            )
            observation = next_observation
        environment.close()
        return frames, cumulative_reward

    def animate_rollouts(self, all_frames):
        """Animate the collected frames after all rollouts are complete."""
        fig, ax = plt.subplots(self.n_rows, self.n_cols, figsize=(10, 8))
        ax = ax.flatten()
        images = []
        for axis in ax:
            img = axis.imshow(np.zeros((64, 64, 3), dtype=np.uint8), vmin=0, vmax=255)
            axis.axis("off")
            images.append(img)

        def update(frame):
            for i, img in enumerate(images):
                if i < len(all_frames) and frame < len(
                    all_frames[i]
                ):  # Ensure valid indices
                    img.set_data(all_frames[i][frame])
            return images

        max_frames = max(len(frames) for frames in all_frames)
        anim = FuncAnimation(fig, update, frames=max_frames, interval=50, blit=True)
        plt.show()

    def train(
        self,
        max_epochs=1,
        save_path: Path = Path("models"),
    ):
        initial_epoch = 0
        if save_path.exists():
            checkpoints_path = sorted(
                save_path.glob("controller_epoch_*"),
                key=lambda p: int(p.stem.split("_")[-1]),
            )
            if len(checkpoints_path) > 0:
                last_checkpoint_path = checkpoints_path[-1]
                loaded_data = torch.load(last_checkpoint_path, weight_only=True)
                self.controller.load_state_dict(loaded_data)
                initial_epoch = int(last_checkpoint_path.stem.split("_")[-1])
        save_path.parent.mkdir(parents=True, exist_ok=True)
        initial_solution = self.controller.get_weights()
        print(f"Initial solution size: {len(initial_solution)}")
        solver = CMAEvolutionStrategy(
            initial_solution, 0.2, {"popsize": self.population_size}
        )
        for epoch in tqdm(
            range(initial_epoch, max_epochs + initial_epoch),
            total=max_epochs,
            desc="Calculating solutions with CMAES",
            leave=False,
        ):
            solutions = solver.ask()
            with ProcessPoolExecutor(max_workers=self.population_size) as executor:
                results = list(
                    executor.map(
                        self._rollout,
                        range(self.population_size),
                    )
                )

            if self.render:
                all_frames = [result[0] for result in results]
                self.animate_rollouts(all_frames)
            fitlist = [result[1] for result in results]
            solver.tell(solutions, fitlist)
            bestsol, bestfit, *_ = solver.result
            print(f"Best fitness in epoch {epoch}: {bestfit}")
            if bestfit > 900:
                print(f"Best solution found : {bestfit}")
                break

        self.controller.set_weights(bestsol)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.controller.state_dict(), save_path)
        print(f"Model saved to {save_path}")


if __name__ == "__main__":
    vision = ConvVAE.from_pretrained().to("cpu")
    memory = MDN_RNN.from_pretrained().to("cpu")
    controller = Controller().to("cpu")
    controller_trainer = ControllerTrainer(
        controller, vision, memory, population_size=11
    )
    controller_trainer.train(3)
