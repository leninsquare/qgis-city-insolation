from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from tempfile import gettempdir
from typing import Optional, Union
from uuid import uuid4

import numpy as np
import rasterio

from .module_shadow_calculator import (
    CancelCallback,
    ModuleShadowCalculator,
    ProgressCallback,
    RasterSource,
    ShadowCalculationMode,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnualShadowCalculationResult:
    raster_path: str
    year: int
    days_calculated: int


class ModuleAnnualShadowCalculator:
    OUTPUT_NODATA = 65535

    def __init__(
        self,
        shadow_calculator: Optional[ModuleShadowCalculator] = None,
    ) -> None:
        self._shadow_calculator = shadow_calculator or ModuleShadowCalculator()

    def calculate(
        self,
        surface_raster: RasterSource,
        output_path: Optional[Union[str, PathLike[str]]] = None,
        six_periods_per_day: bool = False,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_callback: Optional[CancelCallback] = None,
    ) -> AnnualShadowCalculationResult:
        with self._shadow_calculator.open_raster(surface_raster) as dataset:
            prepared_surface = self._shadow_calculator.prepare_surface(dataset)
            local_timezone = self._shadow_calculator.longitude_timezone(
                prepared_surface
            )
            year = datetime.now(local_timezone).year
            first_day = datetime(year, 1, 1, 12, tzinfo=local_timezone)
            next_year = datetime(year + 1, 1, 1, 12, tzinfo=local_timezone)
            days_count = (next_year.date() - first_day.date()).days
            mode = (
                ShadowCalculationMode.SIX_INTERVALS
                if six_periods_per_day
                else ShadowCalculationMode.SINGLE_NOON
            )
            LOGGER.info(
                "Starting annual shadow calculation: year=%d, days=%d, "
                "longitude_time_offset=%s, mode=%s, samples=%d",
                year,
                days_count,
                local_timezone.utcoffset(None),
                mode.value,
                days_count * (6 if six_periods_per_day else 1),
            )
            valid_cells = prepared_surface.valid_cells
            annual_sum = np.zeros(prepared_surface.elevation.shape, dtype=np.uint16)
            output_profile = prepared_surface.profile.copy()
            for day_index in range(days_count):
                self._raise_if_canceled(cancel_callback)
                calculation_date = first_day + timedelta(days=day_index)
                daily_sum, _ = self._shadow_calculator.calculate_shadow_sums(
                    calculation_date,
                    prepared_surface,
                    mode=mode,
                    cancel_callback=cancel_callback,
                )
                np.add(
                    annual_sum,
                    daily_sum,
                    out=annual_sum,
                    where=valid_cells,
                )

                if progress_callback is not None:
                    progress_callback(95.0 * (day_index + 1) / days_count)
                LOGGER.debug(
                    "Annual shadow day calculated: %s (%d/%d)",
                    calculation_date.date().isoformat(),
                    day_index + 1,
                    days_count,
                )

        output = np.where(
            valid_cells,
            annual_sum,
            np.uint16(self.OUTPUT_NODATA),
        )
        output_profile.update(
            driver="GTiff",
            count=1,
            dtype="uint16",
            nodata=self.OUTPUT_NODATA,
            compress="deflate",
        )
        result_path = str(output_path or self._temporary_output_path(year))
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        self._raise_if_canceled(cancel_callback)
        with rasterio.open(result_path, "w", **output_profile) as destination:
            destination.write(output, 1)

        if progress_callback is not None:
            progress_callback(100.0)
        LOGGER.info("Annual shadow calculation completed: %s", result_path)
        return AnnualShadowCalculationResult(result_path, year, days_count)

    @staticmethod
    def _temporary_output_path(year: int) -> Path:
        return (
            Path(gettempdir())
            / f"annual_shadow_sum_{year}_{uuid4().hex}.tif"
        )

    @staticmethod
    def _raise_if_canceled(callback: Optional[CancelCallback]) -> None:
        if callback is not None and callback():
            LOGGER.warning("Annual shadow calculation canceled")
            raise RuntimeError("Annual shadow calculation was canceled.")
