from __future__ import annotations

import logging
from math import isfinite
from os import PathLike
from pathlib import Path
from tempfile import gettempdir
from typing import Callable, Optional, Union
from uuid import uuid4

import fiona
import numpy as np
import rasterio
from fiona.transform import transform_geom
from pyproj import CRS
from rasterio.features import rasterize

LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]
CancelCallback = Callable[[], bool]
VectorSource = Union[
    str,
    PathLike[str],
    tuple[Union[str, PathLike[str]], Optional[str]],
]


class ModuleSurfaceModelBuilder:
    OUTPUT_NODATA = -3.402823466e38

    def build(
        self,
        terrain_raster: Union[str, PathLike[str]],
        buildings_layer: Optional[VectorSource],
        height_field: str,
        output_path: Optional[Union[str, PathLike[str]]] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
    ) -> str:
        terrain_path = str(terrain_raster)
        buildings_path = None
        buildings_layer_name = None
        if buildings_layer is not None:
            buildings_path, buildings_layer_name = self._vector_source(
                buildings_layer
            )
        result_path = str(output_path or self._temporary_output_path())

        LOGGER.info(
            "Starting surface model build: terrain=%s, buildings=%s, field=%s",
            terrain_path,
            buildings_path,
            height_field,
        )
        with rasterio.open(terrain_path) as terrain_dataset:
            self._validate_terrain(terrain_dataset)
            LOGGER.debug(
                "Terrain raster opened: size=%dx%d, crs=%s, bounds=%s",
                terrain_dataset.width,
                terrain_dataset.height,
                terrain_dataset.crs,
                terrain_dataset.bounds,
            )
            terrain = terrain_dataset.read(
                1,
                masked=True,
                out_dtype="float32",
            )
            valid_terrain = (
                ~np.ma.getmaskarray(terrain)
                & np.isfinite(terrain.data)
            )
            if buildings_path is None:
                shapes = []
                if progress_callback is not None:
                    progress_callback(70.0)
            else:
                shapes = self._building_shapes(
                    buildings_path,
                    buildings_layer_name,
                    height_field,
                    terrain_dataset.crs,
                    progress_callback,
                    cancel_callback,
                )

            surface = terrain.filled(0.0)
            if shapes:
                building_heights = rasterize(
                    shapes,
                    out_shape=(terrain_dataset.height, terrain_dataset.width),
                    transform=terrain_dataset.transform,
                    fill=0.0,
                    all_touched=False,
                    dtype="float32",
                )
                surface += building_heights
            surface[~valid_terrain] = self.OUTPUT_NODATA

            profile = terrain_dataset.profile.copy()
            profile.update(
                driver="GTiff",
                count=1,
                dtype="float32",
                nodata=self.OUTPUT_NODATA,
            )
            if output_path is None:
                profile.pop("compress", None)
            else:
                profile["compress"] = "deflate"
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            self._raise_if_canceled(cancel_callback)
            with rasterio.open(result_path, "w", **profile) as destination:
                destination.write(surface, 1)

        if progress_callback is not None:
            progress_callback(100.0)
        LOGGER.info("Surface model build completed: %s", result_path)
        return result_path

    def _building_shapes(
        self,
        buildings_path: str,
        buildings_layer_name: Optional[str],
        height_field: str,
        target_crs,
        progress_callback: Optional[ProgressCallback],
        cancel_callback: Optional[CancelCallback],
    ) -> list[tuple[dict, float]]:
        shapes: list[tuple[dict, float]] = []
        skipped_features = 0
        open_options = {}
        if buildings_layer_name:
            open_options["layer"] = buildings_layer_name
        with fiona.open(buildings_path, **open_options) as buildings:
            if not height_field or height_field not in buildings.schema["properties"]:
                raise ValueError("height_field must name an existing building field.")
            if not buildings.crs:
                raise ValueError("buildings_layer must have a CRS.")

            source_crs = CRS.from_user_input(buildings.crs)
            destination_crs = CRS.from_user_input(target_crs)
            feature_count = len(buildings)
            for index, feature in enumerate(buildings):
                self._raise_if_canceled(cancel_callback)
                geometry = feature.get("geometry")
                if not geometry:
                    skipped_features += 1
                    continue
                if geometry["type"] not in ("Polygon", "MultiPolygon"):
                    raise ValueError(
                        "buildings_layer must contain only polygon geometries."
                    )

                building_height = self._valid_height(
                    feature["properties"].get(height_field)
                )
                if building_height is None:
                    skipped_features += 1
                    continue
                if source_crs != destination_crs:
                    geometry = transform_geom(
                        source_crs.to_wkt(),
                        destination_crs.to_wkt(),
                        geometry,
                    )
                shapes.append((geometry, building_height))

                if progress_callback is not None and feature_count:
                    progress_callback(70.0 * (index + 1) / feature_count)

        shapes.sort(key=lambda item: item[1])
        LOGGER.info(
            "Prepared %d building geometries; skipped %d invalid features",
            len(shapes),
            skipped_features,
        )
        return shapes

    @staticmethod
    def _valid_height(value) -> Optional[float]:
        try:
            height = float(value)
        except (TypeError, ValueError):
            return None
        if not isfinite(height) or height < 0.0:
            return None
        return height

    @staticmethod
    def _validate_terrain(dataset) -> None:
        if dataset.count < 1 or dataset.width <= 0 or dataset.height <= 0:
            raise ValueError("terrain_raster must contain a non-empty raster band.")
        if dataset.crs is None:
            raise ValueError("terrain_raster must have a CRS.")
        crs = CRS.from_user_input(dataset.crs)
        if not crs.is_projected:
            raise ValueError("terrain_raster must use a projected CRS.")
        if not crs.axis_info or crs.axis_info[0].unit_conversion_factor != 1.0:
            raise ValueError("terrain_raster CRS horizontal units must be metres.")
        if (
            dataset.transform.a <= 0.0
            or dataset.transform.e >= 0.0
            or dataset.transform.b != 0.0
            or dataset.transform.d != 0.0
        ):
            raise ValueError("Only north-up, non-rotated rasters are supported.")

    @staticmethod
    def _temporary_output_path() -> Path:
        return Path(gettempdir()) / f"digital_surface_model_{uuid4().hex}.tif"

    @staticmethod
    def _vector_source(source: VectorSource) -> tuple[str, Optional[str]]:
        if isinstance(source, tuple):
            if len(source) != 2:
                raise ValueError(
                    "Vector source tuple must contain path and layer name."
                )
            path, layer_name = source
            return str(path), str(layer_name) if layer_name else None
        return str(source), None

    @staticmethod
    def _raise_if_canceled(callback: Optional[CancelCallback]) -> None:
        if callback is not None and callback():
            LOGGER.warning("Surface model calculation canceled")
            raise RuntimeError("Surface model calculation was canceled.")
