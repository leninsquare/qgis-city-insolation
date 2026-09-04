from __future__ import annotations

import os

from qgis.core import QgsFieldProxyModel, QgsMapLayerProxyModel
from qgis.PyQt import QtCore, QtWidgets, uic


_DIRECTORY = os.path.dirname(__file__)
FORM_CLASS, _ = uic.loadUiType(
    os.path.join(_DIRECTORY, "module_insolation_dialog.ui")
)
YEAR_FORM_CLASS, _ = uic.loadUiType(
    os.path.join(_DIRECTORY, "module_insolation_year_dialog.ui")
)


class _InsolationDialogMixin:
    _default_output_name = "insolation.tif"

    def _configure_common_widgets(self) -> None:
        self.combobox_dem.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.combobox_dem.setAllowEmptyLayer(True)
        self.combobox_dem.setShowCrs(False)
        self.combobox_dem.setCurrentIndex(-1)

        self.combobox_buildings.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.combobox_buildings.setAllowEmptyLayer(True)
        self.combobox_buildings.setShowCrs(False)
        self.combobox_buildings.setCurrentIndex(-1)

        self.combobox_height.setFilters(QgsFieldProxyModel.Numeric)
        self.combobox_height.setAllowEmptyFieldName(True)

        self.combobox_buildings.layerChanged.connect(self._on_buildings_changed)
        self.use_buildings_checkbox.toggled.connect(
            self._on_use_buildings_toggled
        )
        self.tool_button.clicked.connect(self._select_output_file)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self._accept_if_valid)
        self._on_use_buildings_toggled(self.use_buildings_checkbox.isChecked())

    @property
    def dem_layer(self):
        return self.combobox_dem.currentLayer()

    @property
    def buildings_layer(self):
        if not self.use_buildings:
            return None
        return self.combobox_buildings.currentLayer()

    @property
    def height_field(self) -> str:
        if not self.use_buildings:
            return ""
        return self.combobox_height.currentField()

    @property
    def use_buildings(self) -> bool:
        return self.use_buildings_checkbox.isChecked()

    @property
    def output_path(self) -> str:
        return self.text_save.text().strip()

    def _on_buildings_changed(self, layer) -> None:
        self.combobox_height.setLayer(layer)
        if not self.use_buildings:
            self.combobox_height.setEnabled(False)
            self.height_help_label.setText(
                self.tr("Buildings are excluded; analysis uses elevation model only")
            )
            self.height_help_label.setVisible(True)
            return
        enabled = layer is not None
        self.combobox_height.setEnabled(enabled)
        self.height_help_label.setText(
            self.tr("Available after selecting a building layer")
        )
        self.height_help_label.setVisible(not enabled)

    def _on_use_buildings_toggled(self, checked: bool) -> None:
        self.combobox_buildings.setEnabled(checked)
        self.buildings_label.setText(
            self.tr("Buildings *") if checked else self.tr("Buildings")
        )
        self.height_label.setText(
            self.tr("Height field *") if checked else self.tr("Height field")
        )
        self._on_buildings_changed(self.combobox_buildings.currentLayer())

    def _select_output_file(self) -> None:
        start_path = self.output_path or self._default_output_name
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            self.tr("Save insolation result"),
            start_path,
            self.tr("GeoTIFF (*.tif *.tiff);;All files (*)"),
        )
        if path:
            if not os.path.splitext(path)[1]:
                path += ".tif"
            self.text_save.setText(path)

    def _missing_input_names(self) -> list[str]:
        missing = []
        if self.dem_layer is None:
            missing.append(self.tr("Elevation model"))
        if self.use_buildings:
            if self.buildings_layer is None:
                missing.append(self.tr("Buildings"))
            if not self.height_field:
                missing.append(self.tr("Height field"))
        return missing

    def _accept_if_valid(self) -> None:
        missing = self._missing_input_names()
        if missing:
            QtWidgets.QMessageBox.warning(
                self,
                self.tr("Required data is missing"),
                self.tr("Select the following: {fields}.").format(
                    fields=", ".join(missing)
                ),
            )
            return
        self.accept()


class ModuleInsolationDialog(
    _InsolationDialogMixin, QtWidgets.QDialog, FORM_CLASS
):
    moreInformationRequested = QtCore.pyqtSignal()
    yearCalculationAccepted = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.date_edit.setDate(QtCore.QDate.currentDate())
        self._configure_common_widgets()
        self.more_information_button.clicked.connect(
            self.moreInformationRequested.emit
        )
        self.calculate_year_button.clicked.connect(self._open_year_dialog)

    @property
    def calculation_date(self):
        return self.date_edit.date().toPyDate()

    def _open_year_dialog(self) -> None:
        dialog = ModuleInsolationYearDialog(self)
        dialog.use_buildings_checkbox.setChecked(self.use_buildings)

        if self.dem_layer is not None:
            dialog.combobox_dem.setLayer(self.dem_layer)
        if self.use_buildings and self.buildings_layer is not None:
            dialog.combobox_buildings.setLayer(self.buildings_layer)
            if self.height_field:
                dialog.combobox_height.setField(self.height_field)

        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.yearCalculationAccepted.emit(dialog)


class ModuleInsolationYearDialog(
    _InsolationDialogMixin, QtWidgets.QDialog, YEAR_FORM_CLASS
):
    _default_output_name = "insolation_year.tif"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._configure_common_widgets()

    @property
    def calculate_six_day_periods(self) -> bool:
        return self.calculate_six_day_periods_checkbox.isChecked()
