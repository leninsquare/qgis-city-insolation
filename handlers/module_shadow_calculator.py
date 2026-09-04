from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import Enum
from math import ceil, cos, radians, sin, tan
from os import PathLike, cpu_count
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Callable, Iterator, Optional, Union
from uuid import uuid4

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.io import DatasetReaderBase
from rasterio.warp import reproject

"""
try:
    from numba import njit, prange
except (ImportError, OSError) as error:
    raise ImportError(
        "Numba is required for the shadow calculation. QGIS could not load "
        "the bundled llvmlite binary; install a QGIS-compatible Numba build."
    ) from error
"""

from .module_insolation_handler import ModuleInsolationHandler, SunPosition

LOGGER = logging.getLogger(__name__)


RasterSource = Union[str, PathLike[str], DatasetReaderBase]
ProgressCallback = Callable[[float], None]
CancelCallback = Callable[[], bool]

#@njit(parallel=True, cache=True)
def horizon_shadow_scan(
    elevation: np.ndarray,
    pixel_size: float,
    elevation_slope: float,
    comparison_tolerance: float,
) -> np.ndarray:
    height, width = elevation.shape
    shadow = np.full((height, width), 255, dtype=np.uint8)

    for row in range(height):
        max_horizon = -np.inf
        for column in range(width):
            cell_elevation = elevation[row, column]
            if not np.isfinite(cell_elevation):
                continue

            distance = (column + 0.5) * pixel_size
            adjusted_height = (
                cell_elevation + elevation_slope * distance
            )
            if max_horizon > adjusted_height + comparison_tolerance:
                shadow[row, column] = 1
            else:
                shadow[row, column] = 0

            if adjusted_height > max_horizon:
                max_horizon = adjusted_height
    """
    for row in prange(height):
        max_horizon = -np.inf
        for column in range(width):
            cell_elevation = elevation[row, column]
            if not np.isfinite(cell_elevation):
                continue

            distance = (column + 0.5) * pixel_size
            adjusted_height = (
                cell_elevation + elevation_slope * distance
            )
            if max_horizon > adjusted_height + comparison_tolerance:
                shadow[row, column] = 1
            else:
                shadow[row, column] = 0

            if adjusted_height > max_horizon:
                max_horizon = adjusted_height
    """

    return shadow


@dataclass(frozen=True)
class PreparedSurface:
    elevation: np.ndarray
    valid_cells: np.ndarray
    bounds: Any
    transform: Affine
    crs: Any
    profile: dict


@dataclass(frozen=True)
class ShadowCalculationResult:
    raster_path: str
    positions: tuple[SunPosition, ...]


@dataclass(frozen=True)
class ShadowArrayCalculationResult:
    raster: np.ndarray
    profile: dict
    positions: tuple[SunPosition, ...]


class ShadowCalculationMode(str, Enum):
    SIX_INTERVALS = "six_intervals"
    SINGLE_NOON = "single_noon"


class ModuleShadowCalculator:
    INTERVAL_COUNT = 6
    OUTPUT_NODATA = 255
    HORIZON_TOLERANCE = 1.0e-5

    def __init__(
        self,
        sun_position_handler: Optional[ModuleInsolationHandler] = None,
        start_hour: int = 3,
        end_hour: int = 23,
        height_resampling: Resampling = Resampling.max,
        warp_threads: Optional[int] = None,
    ) -> None:
        if not 0 <= start_hour <= 23:
            raise ValueError("start_hour must be between 0 and 23.")
        if not 1 <= end_hour <= 24:
            raise ValueError("end_hour must be between 1 and 24.")
        if start_hour >= end_hour:
            raise ValueError("start_hour must be earlier than end_hour.")
        if warp_threads is not None and warp_threads < 1:
            raise ValueError("warp_threads must be at least one.")

        self._sun_position_handler = sun_position_handler or ModuleInsolationHandler()
        self._start_hour = start_hour
        self._end_hour = end_hour
        self._height_resampling = Resampling(height_resampling)
        self._warp_threads = warp_threads or max(1, cpu_count() or 1)

    def calculate(
        self,
        calculation_date: datetime,
        surface_raster: RasterSource,
        output_path: Optional[Union[str, PathLike[str]]] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
        mode: ShadowCalculationMode = ShadowCalculationMode.SIX_INTERVALS,
    ) -> ShadowCalculationResult:
        mode = ShadowCalculationMode(mode)
        LOGGER.info(
            "Starting shadow calculation for %s using mode=%s",
            calculation_date.date().isoformat(),
            mode.value,
        )
        array_result = self.calculate_array(
            calculation_date,
            surface_raster,
            mode=mode,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        result_path = str(output_path or self._temporary_output_path())
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(result_path, "w", **array_result.profile) as destination:
            destination.write(array_result.raster, 1)

        if progress_callback is not None:
            progress_callback(100.0)
        LOGGER.info("Shadow calculation completed: %s", result_path)
        return ShadowCalculationResult(result_path, array_result.positions)

    def calculate_array(
        self,
        calculation_date: datetime,
        surface_raster: RasterSource,
        mode: ShadowCalculationMode = ShadowCalculationMode.SIX_INTERVALS,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
    ) -> ShadowArrayCalculationResult:
        with self.open_raster(surface_raster) as dataset:
            prepared = self.prepare_surface(dataset)
        return self.calculate_prepared(
            calculation_date,
            prepared,
            mode=mode,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    def prepare_surface(self, dataset: DatasetReaderBase) -> PreparedSurface:
        self._validate_raster(dataset)
        LOGGER.debug(
            "Surface raster opened: size=%dx%d, crs=%s, bounds=%s",
            dataset.width,
            dataset.height,
            dataset.crs,
            dataset.bounds,
        )
        band = dataset.read(1, masked=True, out_dtype="float32")
        elevation = np.array(band.data, dtype=np.float32, copy=True, order="C")
        valid_cells = (
            ~np.ma.getmaskarray(band)
            & np.isfinite(elevation)
        )
        valid_cells = np.ascontiguousarray(valid_cells, dtype=np.bool_)
        elevation[~valid_cells] = np.nan

        return PreparedSurface(
            elevation=elevation,
            valid_cells=valid_cells,
            bounds=dataset.bounds,
            transform=dataset.transform,
            crs=dataset.crs,
            profile=dataset.profile.copy(),
        )

    def calculate_prepared(
        self,
        calculation_date: datetime,
        surface: PreparedSurface,
        mode: ShadowCalculationMode = ShadowCalculationMode.SIX_INTERVALS,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
    ) -> ShadowArrayCalculationResult:
        shadow_sums, positions = self.calculate_shadow_sums(
            calculation_date,
            surface,
            mode=mode,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        output = np.where(
            surface.valid_cells,
            shadow_sums,
            np.uint8(self.OUTPUT_NODATA),
        )
        profile = surface.profile.copy()
        profile.update(
            driver="GTiff",
            count=1,
            dtype="uint8",
            nodata=self.OUTPUT_NODATA,
            compress="deflate",
        )
        return ShadowArrayCalculationResult(output, profile, positions)

    def calculate_shadow_sums(
        self,
        calculation_date: datetime,
        surface: PreparedSurface,
        mode: ShadowCalculationMode = ShadowCalculationMode.SIX_INTERVALS,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
    ) -> tuple[np.ndarray, tuple[SunPosition, ...]]:
        mode = ShadowCalculationMode(mode)
        local_timezone = self.longitude_timezone(surface)
        local_date = calculation_date.replace(tzinfo=local_timezone)
        positions = tuple(
            self._sun_position_handler.calculate_sun_position(
                surface.bounds,
                instant,
                surface.crs,
            )
            for instant in self._calculation_times(local_date, mode)
        )
        LOGGER.debug(
            "Solar positions: %s",
            [
                {
                    "datetime": position.calculated_at.isoformat(),
                    "azimuth": position.azimuth,
                    "elevation": position.elevation,
                }
                for position in positions
            ],
        )

        shadow_sums = np.zeros(surface.elevation.shape, dtype=np.uint8)
        for index, position in enumerate(positions):
            self._raise_if_canceled(cancel_callback)
            self._accumulate_shadow_mask(
                surface,
                shadow_sums,
                position,
                progress_callback,
                cancel_callback,
                index,
                len(positions),
            )

        return shadow_sums, positions

    def longitude_timezone(self, surface: PreparedSurface) -> tzinfo:
        return self._sun_position_handler.longitude_timezone(
            surface.bounds,
            surface.crs,
        )

    def _calculation_times(
        self,
        calculation_date: datetime,
        mode: ShadowCalculationMode,
    ) -> tuple[datetime, ...]:
        if mode is ShadowCalculationMode.SINGLE_NOON:
            return (
                calculation_date.replace(
                    hour=12,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
            )
        return self._interval_midpoints(calculation_date)

    def _interval_midpoints(self, calculation_date: datetime) -> tuple[datetime, ...]:
        midnight = calculation_date.replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = midnight + timedelta(hours=self._start_hour)
        period_end = midnight + timedelta(hours=self._end_hour)
        interval = (period_end - period_start) / self.INTERVAL_COUNT
        return tuple(
            period_start + interval * (index + 0.5)
            for index in range(self.INTERVAL_COUNT)
        )

    def _accumulate_shadow_mask(
        self,
        surface: PreparedSurface,
        shadow_sums: np.ndarray,
        position: SunPosition,
        progress_callback: Optional[ProgressCallback],
        cancel_callback: Optional[CancelCallback],
        completed_intervals: int,
        total_intervals: int,
    ) -> None:
        if position.elevation <= 0.0:
            np.add(
                shadow_sums,
                1,
                out=shadow_sums,
                where=surface.valid_cells,
            )
            self._report_progress(
                progress_callback,
                completed_intervals,
                total_intervals,
                1.0,
            )
            return

        pixel_size = min(abs(surface.transform.a), abs(surface.transform.e))
        rotated_elevation, rotated_transform = self._rotate_surface(
            surface,
            position.azimuth,
            pixel_size,
        )
        self._raise_if_canceled(cancel_callback)
        self._report_progress(
            progress_callback,
            completed_intervals,
            total_intervals,
            0.45,
        )

        rotated_shadow = horizon_shadow_scan(
            rotated_elevation,
            pixel_size,
            tan(radians(position.elevation)),
            self.HORIZON_TOLERANCE,
        )
        self._raise_if_canceled(cancel_callback)
        self._report_progress(
            progress_callback,
            completed_intervals,
            total_intervals,
            0.75,
        )

        source_shadow = self._reproject_shadow(
            rotated_shadow,
            rotated_transform,
            surface,
        )
        np.add(
            shadow_sums,
            source_shadow,
            out=shadow_sums,
            where=surface.valid_cells,
        )
        self._report_progress(
            progress_callback,
            completed_intervals,
            total_intervals,
            1.0,
        )

    def _rotate_surface(
        self,
        surface: PreparedSurface,
        azimuth_degrees: float,
        pixel_size: float,
    ) -> tuple[np.ndarray, Affine]:
        height, width, rotated_transform = self._rotated_grid(
            surface.bounds,
            azimuth_degrees,
            pixel_size,
        )
        LOGGER.debug(
            "Rotating surface for azimuth %.3f: source=%dx%d, rotated=%dx%d",
            azimuth_degrees,
            surface.elevation.shape[1],
            surface.elevation.shape[0],
            width,
            height,
        )
        rotated_elevation = np.empty(
            (height, width),
            dtype=np.float32,
        )
        reproject(
            source=surface.elevation,
            destination=rotated_elevation,
            src_transform=surface.transform,
            src_crs=surface.crs,
            src_nodata=np.nan,
            dst_transform=rotated_transform,
            dst_crs=surface.crs,
            dst_nodata=np.nan,
            resampling=self._height_resampling,
            num_threads=self._warp_threads,
            init_dest_nodata=True,
        )
        return rotated_elevation, rotated_transform

    def _reproject_shadow(
        self,
        rotated_shadow: np.ndarray,
        rotated_transform: Affine,
        surface: PreparedSurface,
    ) -> np.ndarray:
        source_shadow = np.empty(surface.elevation.shape, dtype=np.uint8)
        reproject(
            source=rotated_shadow,
            destination=source_shadow,
            src_transform=rotated_transform,
            src_crs=surface.crs,
            src_nodata=self.OUTPUT_NODATA,
            dst_transform=surface.transform,
            dst_crs=surface.crs,
            dst_nodata=0,
            resampling=Resampling.nearest,
            num_threads=self._warp_threads,
            init_dest_nodata=True,
        )
        return source_shadow

    @staticmethod
    def _rotated_grid(
        bounds,
        azimuth_degrees: float,
        pixel_size: float,
    ) -> tuple[int, int, Affine]:
        if pixel_size <= 0.0:
            raise ValueError("pixel_size must be positive.")
        azimuth = radians(azimuth_degrees)
        sun_x = sin(azimuth)
        sun_y = cos(azimuth)

        column_x = -sun_x
        column_y = -sun_y
        row_x = column_y
        row_y = -column_x

        corners = (
            (bounds.left, bounds.bottom),
            (bounds.left, bounds.top),
            (bounds.right, bounds.bottom),
            (bounds.right, bounds.top),
        )
        column_coordinates = tuple(
            x * column_x + y * column_y
            for x, y in corners
        )
        row_coordinates = tuple(
            x * row_x + y * row_y
            for x, y in corners
        )
        column_min = min(column_coordinates)
        column_max = max(column_coordinates)
        row_min = min(row_coordinates)
        row_max = max(row_coordinates)

        width = max(1, int(ceil((column_max - column_min) / pixel_size)))
        height = max(1, int(ceil((row_max - row_min) / pixel_size)))
        origin_x = column_x * column_min + row_x * row_min
        origin_y = column_y * column_min + row_y * row_min
        transform = Affine(
            column_x * pixel_size,
            row_x * pixel_size,
            origin_x,
            column_y * pixel_size,
            row_y * pixel_size,
            origin_y,
        )
        return height, width, transform

    @staticmethod
    def _report_progress(
        callback: Optional[ProgressCallback],
        completed_intervals: int,
        total_intervals: int,
        interval_fraction: float,
    ) -> None:
        if callback is None:
            return
        total_progress = completed_intervals + interval_fraction
        callback(95.0 * total_progress / total_intervals)

    @staticmethod
    def _validate_raster(dataset: DatasetReaderBase) -> None:
        if dataset.count < 1 or dataset.width <= 0 or dataset.height <= 0:
            raise ValueError("surface_raster must contain a non-empty raster band.")
        if dataset.crs is None:
            raise ValueError("surface_raster must have a CRS.")
        crs = CRS.from_user_input(dataset.crs)
        if not crs.is_projected:
            raise ValueError("surface_raster must use a projected CRS.")
        if not crs.axis_info or crs.axis_info[0].unit_conversion_factor != 1.0:
            raise ValueError("surface_raster CRS horizontal units must be metres.")
        if (
            dataset.transform.a <= 0.0
            or dataset.transform.e >= 0.0
            or dataset.transform.b != 0.0
            or dataset.transform.d != 0.0
        ):
            raise ValueError("Only north-up, non-rotated rasters are supported.")

    @staticmethod
    def _temporary_output_path() -> Path:
        return Path(gettempdir()) / f"insolation_shadow_sum_{uuid4().hex}.tif"

    @staticmethod
    def _raise_if_canceled(callback: Optional[CancelCallback]) -> None:
        if callback is not None and callback():
            LOGGER.warning("Shadow calculation canceled")
            raise RuntimeError("Shadow calculation was canceled.")

    @staticmethod
    @contextmanager
    def open_raster(source: RasterSource) -> Iterator[DatasetReaderBase]:
        if isinstance(source, (str, PathLike)):
            with rasterio.open(source) as dataset:
                yield dataset
        else:
            yield source
