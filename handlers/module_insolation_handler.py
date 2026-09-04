from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from math import acos, asin, atan2, cos, degrees, isfinite, radians, sin, tan
from typing import Any, Union

from pyproj import CRS, Transformer

LOGGER = logging.getLogger(__name__)

Extent = Union[tuple[float, float, float, float], Any]
CRSLike = Union[str, int, CRS]


@dataclass(frozen=True)
class SunPosition:
    longitude: float
    latitude: float
    calculated_at: datetime
    azimuth: float
    elevation: float
    zenith: float


class ModuleInsolationHandler:
    def longitude_timezone(self, extent: Extent, extent_crs: CRSLike) -> tzinfo:
        longitude, _ = self._geographic_center(extent, extent_crs)
        result = timezone(timedelta(hours=longitude / 15.0))
        LOGGER.debug(
            "Longitude timezone calculated: longitude=%.6f, offset=%s",
            longitude,
            result.utcoffset(None),
        )
        return result

    def calculate_sun_position(
        self,
        extent: Extent,
        calculated_at: datetime,
        extent_crs: CRSLike,
    ) -> SunPosition:
        if calculated_at.tzinfo is None or calculated_at.utcoffset() is None:
            raise ValueError("calculated_at must be timezone-aware.")

        longitude, latitude = self._geographic_center(extent, extent_crs)
        LOGGER.debug(
            "Calculating Sun position for longitude=%.6f, latitude=%.6f, "
            "crs=%s, datetime=%s",
            longitude,
            latitude,
            extent_crs,
            calculated_at.isoformat(),
        )
        instant_utc = calculated_at.astimezone(timezone.utc)
        azimuth, elevation, zenith = self._solar_angles(
            longitude,
            latitude,
            instant_utc,
        )
        result = SunPosition(
            longitude=longitude,
            latitude=latitude,
            calculated_at=instant_utc,
            azimuth=azimuth,
            elevation=elevation,
            zenith=zenith,
        )
        LOGGER.debug(
            "Sun position calculated: longitude=%.6f, latitude=%.6f, "
            "azimuth=%.3f, elevation=%.3f",
            result.longitude,
            result.latitude,
            result.azimuth,
            result.elevation,
        )
        return result

    def _geographic_center(
        self,
        extent: Extent,
        extent_crs: CRSLike,
    ) -> tuple[float, float]:
        left, bottom, right, top = self._extent_values(extent)
        centre_x = (left + right) / 2.0
        centre_y = (bottom + top) / 2.0
        source_crs = CRS.from_user_input(extent_crs)
        transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        longitude, latitude = transformer.transform(centre_x, centre_y)
        if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
            raise ValueError("The transformed extent centre is outside WGS 84 bounds.")
        return longitude, latitude

    @staticmethod
    def _extent_values(extent: Extent) -> tuple[float, float, float, float]:
        try:
            if all(
                hasattr(extent, name)
                for name in ("left", "bottom", "right", "top")
            ):
                values = (extent.left, extent.bottom, extent.right, extent.top)
            else:
                values = tuple(extent)
            if len(values) != 4:
                raise ValueError
            left, bottom, right, top = map(float, values)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "extent must contain left, bottom, right and top coordinates."
            ) from error
        if not all(isfinite(value) for value in (left, bottom, right, top)):
            raise ValueError("extent coordinates must be finite.")
        if not left < right or not bottom < top:
            raise ValueError("extent must be non-empty and ordered.")
        return left, bottom, right, top

    @staticmethod
    def _solar_angles(
        longitude: float,
        latitude: float,
        instant_utc: datetime,
    ) -> tuple[float, float, float]:
        julian_day = instant_utc.timestamp() / 86400.0 + 2440587.5
        julian_century = (julian_day - 2451545.0) / 36525.0

        geom_mean_longitude = (
            280.46646
            + julian_century * (36000.76983 + julian_century * 0.0003032)
        ) % 360.0
        geom_mean_anomaly = 357.52911 + julian_century * (
            35999.05029 - 0.0001537 * julian_century
        )
        orbit_eccentricity = 0.016708634 - julian_century * (
            0.000042037 + 0.0000001267 * julian_century
        )

        anomaly_rad = radians(geom_mean_anomaly)
        equation_of_centre = (
            sin(anomaly_rad)
            * (1.914602 - julian_century * (0.004817 + 0.000014 * julian_century))
            + sin(2.0 * anomaly_rad) * (0.019993 - 0.000101 * julian_century)
            + sin(3.0 * anomaly_rad) * 0.000289
        )
        true_longitude = geom_mean_longitude + equation_of_centre
        apparent_longitude = true_longitude - 0.00569 - 0.00478 * sin(
            radians(125.04 - 1934.136 * julian_century)
        )

        mean_obliquity = (
            23.0
            + (
                26.0
                + (
                    21.448
                    - julian_century
                    * (46.815 + julian_century * (0.00059 - 0.001813 * julian_century))
                )
                / 60.0
            )
            / 60.0
        )
        corrected_obliquity = mean_obliquity + 0.00256 * cos(
            radians(125.04 - 1934.136 * julian_century)
        )
        declination = asin(
            sin(radians(corrected_obliquity)) * sin(radians(apparent_longitude))
        )

        y = tan(radians(corrected_obliquity) / 2.0) ** 2
        mean_longitude_rad = radians(geom_mean_longitude)
        equation_of_time = 4.0 * degrees(
            y * sin(2.0 * mean_longitude_rad)
            - 2.0 * orbit_eccentricity * sin(anomaly_rad)
            + 4.0
            * orbit_eccentricity
            * y
            * sin(anomaly_rad)
            * cos(2.0 * mean_longitude_rad)
            - 0.5 * y * y * sin(4.0 * mean_longitude_rad)
            - 1.25 * orbit_eccentricity**2 * sin(2.0 * anomaly_rad)
        )

        utc_minutes = (
            instant_utc.hour * 60.0
            + instant_utc.minute
            + instant_utc.second / 60.0
            + instant_utc.microsecond / 60_000_000.0
        )
        true_solar_time = (utc_minutes + equation_of_time + 4.0 * longitude) % 1440.0
        hour_angle = true_solar_time / 4.0 - 180.0
        if hour_angle < -180.0:
            hour_angle += 360.0

        latitude_rad = radians(latitude)
        hour_angle_rad = radians(hour_angle)
        cosine_zenith = (
            sin(latitude_rad) * sin(declination)
            + cos(latitude_rad) * cos(declination) * cos(hour_angle_rad)
        )
        zenith = degrees(acos(max(-1.0, min(1.0, cosine_zenith))))
        elevation = 90.0 - zenith
        azimuth = (
            degrees(
                atan2(
                    sin(hour_angle_rad),
                    cos(hour_angle_rad) * sin(latitude_rad)
                    - tan(declination) * cos(latitude_rad),
                )
            )
            + 180.0
        ) % 360.0

        return azimuth, elevation, zenith
