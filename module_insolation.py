from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from typing import Optional

from qgis.core import (
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsUnitTypes,
    Qgis,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFontDatabase, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QMessageBox,
    QProgressDialog,
)

from .module_insolation_dialog import (
    ModuleInsolationDialog,
    ModuleInsolationYearDialog,
)

LOGGER = logging.getLogger(__name__)


class ModuleInsolationPlugin:
    MENU_NAME = "&Urban Insolation"
    MAX_EXTENT_AREA_SQUARE_KM = 2600.0
    MAX_RASTER_CELLS = 100_000_000
    POLAR_CIRCLE_LATITUDE = 66.5622
    FONT_PATHS = (
        "assets/fonts/anek_latin/AnekLatin[wdth,wght].ttf",
        "assets/fonts/monomaniac_one/MonomaniacOne-Regular.ttf",
    )

    def __init__(self, iface) -> None:
        self.iface = iface
        self.plugin_dir = Path(__file__).resolve().parent
        self.actions: list[QAction] = []
        self._surface_builder = None
        self._shadow_calculator = None
        self._annual_shadow_calculator = None
        self._configure_bundled_dependencies()
        self._font_ids = self._load_bundled_fonts()

    def initGui(self) -> None:
        icon = QIcon(str(self.plugin_dir / "icons" / "insolation.svg"))
        daily_action = QAction(
            icon,
            self.tr("Urban insolation"),
            self.iface.mainWindow(),
        )
        daily_action.setObjectName("module_insolation_daily_action")
        daily_action.triggered.connect(self.open_main_dialog)

        annual_action = QAction(
            icon,
            self.tr("Annual urban insolation"),
            self.iface.mainWindow(),
        )
        annual_action.setObjectName("module_insolation_annual_action")
        annual_action.triggered.connect(self.open_year_dialog)

        self.actions = [daily_action, annual_action]
        for action in self.actions:
            self.iface.addPluginToRasterMenu(self.MENU_NAME, action)
        self.iface.addToolBarIcon(daily_action)
        LOGGER.info("Urban Insolation plugin GUI initialized")

    def unload(self) -> None:
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.MENU_NAME, action)
            self.iface.removeToolBarIcon(action)
            action.deleteLater()
        self.actions.clear()
        for font_id in self._font_ids:
            QFontDatabase.removeApplicationFont(font_id)
        self._font_ids.clear()
        LOGGER.info("Urban Insolation plugin unloaded")

    def open_main_dialog(self) -> None:
        dialog = ModuleInsolationDialog(self.iface.mainWindow())
        dialog.moreInformationRequested.connect(self._show_information)
        dialog.yearCalculationAccepted.connect(self._run_annual_from_dialog)
        if dialog.exec_() == dialog.Accepted:
            self._run_daily_from_dialog(dialog)

    def open_year_dialog(self) -> None:
        dialog = ModuleInsolationYearDialog(self.iface.mainWindow())
        if dialog.exec_() == dialog.Accepted:
            self._run_annual_from_dialog(dialog)

    def _run_daily_from_dialog(self, dialog: ModuleInsolationDialog) -> None:
        progress = self._create_progress_dialog(
            self.tr("Calculating daily insolation…")
        )
        surface_path = None
        try:
            self._validate_spatial_extent(dialog.dem_layer)
            self._load_calculators()
            calculation_date = self._as_datetime(dialog.calculation_date)
            progress.show()
            QApplication.processEvents()
            surface_source = self._raster_source(dialog.dem_layer)
            if dialog.buildings_layer is not None:
                surface_path = self._surface_builder.build(
                    terrain_raster=surface_source,
                    buildings_layer=self._vector_source(dialog.buildings_layer),
                    height_field=dialog.height_field,
                    progress_callback=self._progress_callback(progress, 0.0, 20.0),
                    cancel_callback=self._cancel_callback(progress),
                )
                surface_source = surface_path
            else:
                progress.setValue(20)
            result = self._shadow_calculator.calculate(
                calculation_date=calculation_date,
                surface_raster=surface_source,
                output_path=dialog.output_path or None,
                progress_callback=self._progress_callback(progress, 20.0, 80.0),
                cancel_callback=self._cancel_callback(progress),
            )
            self._add_result_layer(result.raster_path, self.tr("Daily shadows"))
            self._show_success(result.raster_path)
        except Exception as error:
            self._handle_calculation_error(error, progress)
        finally:
            progress.close()
            self._remove_intermediate(surface_path)

    def _run_annual_from_dialog(self, dialog: ModuleInsolationYearDialog) -> None:
        progress = self._create_progress_dialog(
            self.tr("Calculating annual insolation…")
        )
        surface_path = None
        try:
            self._validate_spatial_extent(dialog.dem_layer)
            self._load_calculators()
            progress.show()
            QApplication.processEvents()
            surface_source = self._raster_source(dialog.dem_layer)
            if dialog.buildings_layer is not None:
                surface_path = self._surface_builder.build(
                    terrain_raster=surface_source,
                    buildings_layer=self._vector_source(dialog.buildings_layer),
                    height_field=dialog.height_field,
                    progress_callback=self._progress_callback(progress, 0.0, 10.0),
                    cancel_callback=self._cancel_callback(progress),
                )
                surface_source = surface_path
            else:
                progress.setValue(10)
            result = self._annual_shadow_calculator.calculate(
                surface_raster=surface_source,
                output_path=dialog.output_path or None,
                six_periods_per_day=dialog.calculate_six_day_periods,
                progress_callback=self._progress_callback(progress, 10.0, 90.0),
                cancel_callback=self._cancel_callback(progress),
            )
            self._add_result_layer(
                result.raster_path,
                self.tr("Annual shadows {year}").format(year=result.year),
            )
            self._show_success(result.raster_path)
        except Exception as error:
            self._handle_calculation_error(error, progress)
        finally:
            progress.close()
            self._remove_intermediate(surface_path)

    def _load_calculators(self) -> None:
        if self._surface_builder is not None:
            return
        try:
            from .handlers.module_annual_shadow_calculator import (
                ModuleAnnualShadowCalculator,
            )
            from .handlers.module_shadow_calculator import ModuleShadowCalculator
            from .handlers.module_surface_model_builder import ModuleSurfaceModelBuilder
        except ImportError as error:
            raise RuntimeError(
                "Calculation dependencies are unavailable. Install packages from "
                "requirements.txt into the QGIS Python environment."
            ) from error

        self._surface_builder = ModuleSurfaceModelBuilder()
        self._shadow_calculator = ModuleShadowCalculator()
        self._annual_shadow_calculator = ModuleAnnualShadowCalculator(
            self._shadow_calculator
        )

    def _configure_bundled_dependencies(self) -> None:
        platform_directory = None
        if sys.platform.startswith("win"):
            platform_directory = "windows"
        elif sys.platform == "darwin":
            platform_directory = "macos"
        if platform_directory is None:
            return
        tools_path = self.plugin_dir / "tools" / platform_directory
        if tools_path.is_dir() and str(tools_path) not in sys.path:
            sys.path.insert(0, str(tools_path))

    def _load_bundled_fonts(self) -> list[int]:
        font_ids = []
        for relative_path in self.FONT_PATHS:
            path = self.plugin_dir / relative_path
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id < 0:
                LOGGER.warning("Could not load bundled font: %s", path)
                continue
            font_ids.append(font_id)
            LOGGER.debug(
                "Bundled font loaded: %s, families=%s",
                path,
                QFontDatabase.applicationFontFamilies(font_id),
            )
        return font_ids

    def _raster_source(self, layer) -> str:
        decoded = QgsProviderRegistry.instance().decodeUri(
            layer.providerType(),
            layer.source(),
        )
        path = decoded.get("path") or decoded.get("url")
        if not path:
            path = layer.source().split("|")[0]
        return str(path)

    def _vector_source(self, layer):
        if layer is None:
            return None
        decoded = QgsProviderRegistry.instance().decodeUri(
            layer.providerType(),
            layer.source(),
        )
        path = decoded.get("path") or decoded.get("database")
        if not path:
            path = layer.source().split("|")[0]
        layer_name = decoded.get("layerName") or decoded.get("layer")
        return (str(path), str(layer_name)) if layer_name else str(path)

    def _create_progress_dialog(self, label: str) -> QProgressDialog:
        progress = QProgressDialog(
            label,
            self.tr("Cancel"),
            0,
            100,
            self.iface.mainWindow(),
        )
        progress.setWindowTitle(self.tr("Urban insolation"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        return progress

    @staticmethod
    def _progress_callback(progress: QProgressDialog, start: float, span: float):
        def update(value: float) -> None:
            target = round(start + span * max(0.0, min(100.0, value)) / 100.0)
            if target == progress.value():
                return
            progress.setValue(target)
            QApplication.processEvents()

        return update

    @staticmethod
    def _cancel_callback(progress: QProgressDialog):
        def is_canceled() -> bool:
            return progress.wasCanceled()

        return is_canceled

    def _add_result_layer(self, path: str, name: str) -> None:
        layer = QgsRasterLayer(path, name)
        if not layer.isValid():
            raise RuntimeError(f"The result raster could not be opened: {path}")
        self._apply_purples_style(layer)
        QgsProject.instance().addMapLayer(layer)

    @staticmethod
    def _apply_purples_style(layer: QgsRasterLayer) -> None:
        provider = layer.dataProvider()
        statistics = provider.bandStatistics(
            1,
            QgsRasterBandStats.Min | QgsRasterBandStats.Max,
        )
        minimum = statistics.minimumValue
        maximum = statistics.maximumValue
        if not isfinite(minimum) or not isfinite(maximum):
            return

        shader_function = QgsColorRampShader()
        shader_function.setColorRampType(QgsColorRampShader.Interpolated)
        values = (
            (minimum, "#fcfbfd"),
            ((minimum + maximum) / 2.0, "#bcbddc"),
            (maximum, "#3f007d"),
        )
        if minimum == maximum:
            values = values[:1]
        shader_function.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(
                    value,
                    QColor(color),
                    f"{value:g}",
                )
                for value, color in values
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(shader_function)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(minimum)
        renderer.setClassificationMax(maximum)
        layer.setRenderer(renderer)
        layer.setCustomProperty("insolation/colorRamp", "Purples")
        layer.triggerRepaint()

    def _validate_spatial_extent(self, raster_layer) -> None:
        pixel_count = raster_layer.width() * raster_layer.height()
        if pixel_count > self.MAX_RASTER_CELLS:
            raise ValueError(
                self.tr(
                    "The raster is too detailed for this extent: "
                    "{pixels:,} cells. The maximum is {maximum:,}; "
                    "increase the pixel size."
                ).format(
                    pixels=pixel_count,
                    maximum=self.MAX_RASTER_CELLS,
                )
            )

        project = QgsProject.instance()
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(
            raster_layer.crs(),
            project.transformContext(),
        )
        distance_area.setEllipsoid("WGS84")
        measured_area = abs(
            distance_area.measureArea(
                QgsGeometry.fromRect(raster_layer.extent())
            )
        )
        square_kilometres = getattr(
            QgsUnitTypes,
            "AreaSquareKilometers",
            None,
        )
        if square_kilometres is None:
            square_kilometres = Qgis.AreaUnit.SquareKilometers
        area_square_km = distance_area.convertAreaMeasurement(
            measured_area,
            square_kilometres,
        )
        LOGGER.info("Calculation extent area: %.3f km²", area_square_km)
        if area_square_km > self.MAX_EXTENT_AREA_SQUARE_KM:
            raise ValueError(
                self.tr(
                    "The calculation area is too large: {area:.2f} km². "
                    "The maximum allowed area is {maximum:.0f} km²."
                ).format(
                    area=area_square_km,
                    maximum=self.MAX_EXTENT_AREA_SQUARE_KM,
                )
            )

        wgs84 = QgsCoordinateReferenceSystem.fromEpsgId(4326)
        coordinate_transform = QgsCoordinateTransform(
            raster_layer.crs(),
            wgs84,
            project.transformContext(),
        )
        geographic_extent = coordinate_transform.transformBoundingBox(
            raster_layer.extent()
        )
        crosses_north = (
            geographic_extent.yMaximum() > self.POLAR_CIRCLE_LATITUDE
        )
        crosses_south = (
            geographic_extent.yMinimum() < -self.POLAR_CIRCLE_LATITUDE
        )
        if crosses_north or crosses_south:
            hemispheres = []
            if crosses_north:
                hemispheres.append(self.tr("north of the Arctic Circle"))
            if crosses_south:
                hemispheres.append(self.tr("south of the Antarctic Circle"))
            LOGGER.warning(
                "Calculation extent reaches a polar region: latitude %.4f..%.4f",
                geographic_extent.yMinimum(),
                geographic_extent.yMaximum(),
            )
            QMessageBox.warning(
                self.iface.mainWindow(),
                self.tr("Polar region warning"),
                self.tr(
                    "The calculation extent reaches {regions}. Accuracy may be "
                    "reduced because of polar day and polar night conditions."
                ).format(regions=self.tr(" and ").join(hemispheres)),
            )

    def _handle_calculation_error(
        self,
        error: Exception,
        progress: QProgressDialog,
    ) -> None:
        if progress.wasCanceled():
            LOGGER.info("Calculation canceled by user")
            QMessageBox.information(
                self.iface.mainWindow(),
                self.tr("Urban insolation"),
                self.tr("Calculation canceled."),
            )
            return
        LOGGER.exception("Insolation calculation failed")
        critical_level = getattr(Qgis, "Critical", None)
        if critical_level is None:
            critical_level = Qgis.MessageLevel.Critical
        QgsMessageLog.logMessage(str(error), "Urban Insolation", critical_level)
        QMessageBox.critical(
            self.iface.mainWindow(),
            self.tr("Calculation failed"),
            str(error),
        )

    def _show_success(self, output_path: str) -> None:
        QMessageBox.information(
            self.iface.mainWindow(),
            self.tr("Calculation completed"),
            self.tr("Result saved to:\n{path}").format(path=output_path),
        )

    def _show_information(self) -> None:
        QMessageBox.information(
            self.iface.mainWindow(),
            self.tr("Urban insolation"),
            self.tr(
                "The plugin builds a surface model from terrain and optional "
                "buildings, then calculates accumulated terrain shadows."
            ),
        )

    @staticmethod
    def _as_datetime(value) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        parsed = date.fromisoformat(str(value))
        return datetime(parsed.year, parsed.month, parsed.day)

    @staticmethod
    def _remove_intermediate(path: Optional[str]) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove intermediate surface raster: %s", path)

    @staticmethod
    def tr(message: str) -> str:
        return QApplication.translate("ModuleInsolationPlugin", message)
