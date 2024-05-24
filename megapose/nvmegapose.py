# Standard Library
import argparse
import json
import os
from pathlib import Path
from typing import List, Tuple, Union

# Third Party
import numpy as np
from bokeh.io import export_png
from bokeh.plotting import gridplot
from PIL import Image
from torchvision import transforms
import torch

# MegaPose
from megapose.datasets.object_dataset import RigidObject, RigidObjectDataset
from megapose.datasets.scene_dataset import CameraData, ObjectData
from megapose.inference.types import (
    DetectionsType,
    ObservationTensor,
    PoseEstimatesType,
)
from megapose.inference.utils import make_detections_from_object_data
from megapose.lib3d.transform import Transform
from megapose.panda3d_renderer import Panda3dLightData
from megapose.panda3d_renderer.panda3d_scene_renderer import Panda3dSceneRenderer
from megapose.utils.conversion import convert_scene_observation_to_panda3d
from megapose.utils.load_model import NAMED_MODELS, load_named_model
from megapose.utils.logging import get_logger
from megapose.visualization.bokeh_plotter import BokehPlotter
from megapose.visualization.utils import make_contour_overlay

logger = get_logger(__name__)


class Megapose:
    def __init__(self, device, convert):
        self.device = device
        self.Convert_YCB = convert
        self.model_name = "megapose-1.0-RGB-multi-hypothesis-icp"
        self.model_info = NAMED_MODELS[self.model_name]
        self.camera_data = CameraData.from_json((Path("./data/ycbv_camera_data.json")).read_text())
        self.models_path = Path("./models/megapose-models")
        self.cad_path = Path("./bop_datasets/ycbv/models")
        self.object_dataset = self.make_ycb_object_dataset(self.cad_path)
        logger.info(f"Loading model {self.model_name}.")
        self.pose_estimator = load_named_model(self.model_name, self.models_path, self.object_dataset).to(self.device)
        self.renders_path = Path("./data/ycbv_generated")
        renders = self.load_renders(self.renders_path)
        self.pose_estimator.attach_renders(renders)

    def inference(self, rgb, depth, label, bbox):
        """
        :param rgb: np array of the RGB image, np.uint8 type
        :param depth: np array of the depth image, np.float32 type or None
        :param label: object name in string format
        :param bbox: bounding box of the object [xmin, ymin, xmax, ymax] format
        :return: prediction result in RT
        """
        # make sure the size of camera input and images are same
        assert rgb.shape[:2] == self.camera_data.resolution
        assert depth.shape[:2] == self.camera_data.resolution
        observation = ObservationTensor.from_numpy(rgb, depth, self.camera_data.K).to_cuda(device=self.device)

        object_data = [ObjectData(label=label, bbox_modal=bbox)]
        detections = make_detections_from_object_data(object_data).to(self.device)

        output, _ = self.pose_estimator.run_inference_pipeline(
            observation, detections=detections, run_detector=False, **self.model_info["inference_parameters"]
        )

        return output

    def output_visualization(self, example_dir: Path) -> None:
        rgb, _, camera_data = self.load_observation(example_dir, load_depth=False)
        camera_data.TWC = Transform(np.eye(4))
        object_datas = self.load_object_data(example_dir / "outputs" / "object_data.json")
        object_dataset = self.make_object_dataset(example_dir)

        renderer = Panda3dSceneRenderer(object_dataset)

        camera_data, object_datas = convert_scene_observation_to_panda3d(camera_data, object_datas)
        light_datas = [
            Panda3dLightData(
                light_type="ambient",
                color=((1.0, 1.0, 1.0, 1)),
            ),
        ]
        renderings = renderer.render_scene(
            object_datas,
            [camera_data],
            light_datas,
            render_depth=False,
            render_binary_mask=False,
            render_normals=False,
            copy_arrays=True,
        )[0]

        plotter = BokehPlotter()

        fig_rgb = plotter.plot_image(rgb)
        fig_mesh_overlay = plotter.plot_overlay(rgb, renderings.rgb)
        contour_overlay = make_contour_overlay(
            rgb, renderings.rgb, dilate_iterations=1, color=(0, 255, 0)
        )["img"]
        fig_contour_overlay = plotter.plot_image(contour_overlay)
        fig_all = gridplot([[fig_rgb, fig_contour_overlay, fig_mesh_overlay]], toolbar_location=None)
        vis_dir = example_dir / "visualizations"
        vis_dir.mkdir(exist_ok=True)
        export_png(fig_mesh_overlay, filename=vis_dir / "mesh_overlay.png")
        export_png(fig_contour_overlay, filename=vis_dir / "contour_overlay.png")
        export_png(fig_all, filename=vis_dir / "all_results.png")
        logger.info(f"Wrote visualizations to {vis_dir}.")
        return

    def load_observation(self, example_dir: Path, load_depth: bool = False) -> Tuple[
        np.ndarray, Union[None, np.ndarray], CameraData]:
        camera_data = CameraData.from_json((example_dir / "ycbv_camera_data.json").read_text())

        rgb = np.array(Image.open(example_dir / "image_rgb.png"), dtype=np.uint8)
        assert rgb.shape[:2] == camera_data.resolution

        depth = None
        if load_depth:
            depth = np.array(Image.open(example_dir / "image_depth.png"), dtype=np.float32) / 10000
            assert depth.shape[:2] == camera_data.resolution

        return rgb, depth, camera_data

    def load_observation_tensor(self,
            example_dir: Path,
            load_depth: bool = False,
    ) -> ObservationTensor:
        rgb, depth, camera_data = self.load_observation(example_dir, load_depth)
        observation = ObservationTensor.from_numpy(rgb, depth, camera_data.K)
        return observation

    def load_object_data(self, data_path: Path) -> List[ObjectData]:
        object_data = json.loads(data_path.read_text())
        object_data = [ObjectData.from_json(d) for d in object_data]
        return object_data

    def load_detections(self, example_dir: Path) -> DetectionsType:
        input_object_data = self.load_object_data(example_dir / "inputs/object_data.json")
        detections = make_detections_from_object_data(input_object_data).to(self.device)
        return detections

    def make_object_dataset(self, cad_model_dir: Path) -> RigidObjectDataset:
        rigid_objects = []
        mesh_units = "m"
        object_dirs = cad_model_dir.iterdir()
        print("Loading all CAD models from {}, default unit {}, this may take a long time".
              format(cad_model_dir, mesh_units))
        for object_dir in object_dirs:
            label = object_dir.name
            mesh_path = None
            for fn in object_dir.glob("*"):
                if fn.suffix in {".obj", ".ply"}:
                    assert not mesh_path, f"there multiple meshes in the {label} directory"
                    mesh_path = fn
            assert mesh_path, f"couldnt find a obj or ply mesh for {label}"
            rigid_objects.append(RigidObject(label=label, mesh_path=mesh_path, mesh_units=mesh_units))
            # TODO: fix mesh units
        rigid_object_dataset = RigidObjectDataset(rigid_objects)
        return rigid_object_dataset

    def make_ycb_object_dataset(self, cad_model_dir: Path) -> RigidObjectDataset:
        rigid_objects = []
        mesh_units = "mm"
        object_plys = sorted(cad_model_dir.rglob('*.ply'))
        print("Loading all CAD models from {}, default unit {}, this may take a long time".
              format(cad_model_dir, mesh_units))
        for num, object_ply in enumerate(object_plys):
            label = self.Convert_YCB.convert_number(num + 1)
            rigid_objects.append(RigidObject(label=label, mesh_path=object_ply, mesh_units=mesh_units))
        rigid_object_dataset = RigidObjectDataset(rigid_objects)
        return rigid_object_dataset

    def save_predictions(self,
            example_dir: Path,
            pose_estimates: PoseEstimatesType,
    ) -> None:
        labels = pose_estimates.infos["label"]
        poses = pose_estimates.poses.cpu().numpy()
        object_data = [
            ObjectData(label=label, TWO=Transform(pose)) for label, pose in zip(labels, poses)
        ]
        object_data_json = json.dumps([x.to_json() for x in object_data])
        output_fn = example_dir / "object_data.json"
        output_fn.parent.mkdir(exist_ok=True)
        output_fn.write_text(object_data_json)
        logger.info(f"Wrote predictions: {output_fn}")
        return

    def load_renders(self, renders_path):
        # Dictionary to hold the tensors for each sub-folder
        folder_tensors = {}

        # Transform to convert images to tensors and normalize by 255
        transform = transforms.Compose([
            transforms.ToTensor(),  # Converts to [C, H, W] and scales pixel values to [0, 1]
        ])

        # Iterate over each sub-folder in the root directory
        for sub_folder in os.listdir(renders_path):
            sub_folder_path = renders_path / sub_folder

            if os.path.isdir(sub_folder_path):
                # Initialize list to hold pairs of rgb and normal images
                png_files = sorted(list(sub_folder_path.rglob('*.png')))
                images_list = []
                num_files = int(len(png_files) / 2)

                for i in range(num_files):
                    # Load rgb image
                    padded_num = "{:03d}".format(i)
                    rgb_path = os.path.join(sub_folder_path, f'rgb_{padded_num}.png')
                    rgb_image = Image.open(rgb_path)
                    rgb_tensor = transform(rgb_image)

                    # Load normal image
                    normal_path = os.path.join(sub_folder_path, f'normal_{padded_num}.png')
                    normal_image = Image.open(normal_path)
                    normal_tensor = transform(normal_image)

                    # Concatenate the rgb and normal tensors along the channel dimension
                    combined_tensor = torch.cat((rgb_tensor, normal_tensor), dim=0)
                    images_list.append(combined_tensor)

                # Stack all image tensors to create a single tensor of shape [576, 6, H, W]
                folder_tensor = torch.stack(images_list)
                folder_tensors[sub_folder] = folder_tensor

        return folder_tensors
