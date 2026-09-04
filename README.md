# Urban Insolation Analysis

Urban Insolation Analysis is an experimental QGIS plugin for calculating accumulated terrain and building shadows. It can evaluate a selected day or every day of the current year and adds the resulting GeoTIFF directly to the QGIS project.

The output represents the number of calculation samples during which a cell is in shadow. It is not a direct measurement of solar energy or insolation duration in hours.

## Features

- Builds a digital surface model from a DEM and optional building polygons.
- Reads building height from a selected numeric attribute.
- Calculates the Sun position at the geographic centre of the raster.
- Uses local longitude time instead of the computer's time zone.
- Calculates six shadow samples for a selected day.
- Calculates one noon sample per day for the current year.
- Optionally calculates six samples per day for the annual result.
- Uses a directional horizon scan instead of tracing a separate ray from every cell.
- Shows progress and supports cancellation.
- Warns about polar regions where polar day and polar night may reduce accuracy.
- Applies the `Purples` colour ramp to result layers automatically.

## Requirements

- QGIS 3.28 or newer
- Python 3 supplied with QGIS
- NumPy 1.24 or newer
- Rasterio 1.3 or newer
- PyProj 3.5 or newer
- Fiona 1.9 or newer

The required versions are listed in `requirements.txt`. Dependencies must be installed in the Python environment used by QGIS, not only in the system Python environment. Numba is optional and is not required for the default calculation path.

## Installation

### Install from a ZIP archive

1. Create a ZIP archive containing the `insolation_analysis` directory.
2. Make sure `metadata.txt` is located directly inside that directory.
3. In QGIS, open **Plugins → Manage and Install Plugins**.
4. Select **Install from ZIP**.
5. Select the archive and install the plugin.
6. Enable **Urban Insolation Analysis** in the plugin manager.

### Install from source

Copy or clone the project into the active QGIS profile's `python/plugins` directory so that the final path ends with:

```text
python/plugins/insolation_analysis/metadata.txt
```

Restart QGIS and enable the plugin in the plugin manager.

If dependencies are missing, install them with the Python executable used by QGIS:

```bash
<qgis-python> -m pip install -r requirements.txt
```

## Input data

### Elevation model

The DEM must:

- contain at least one raster band;
- use a projected CRS;
- use metres as its horizontal CRS unit;
- be north-up and non-rotated;
- contain elevation values in metres;
- define its CRS and valid raster extent.

Horizontal distances and elevation values are compared directly during shadow calculation, so both must use metres.

### Buildings

Buildings are optional. When enabled, provide:

- a polygon layer with a valid CRS;
- a numeric height field;
- height values representing building height above the terrain, in metres.

Building heights are rasterised to the DEM grid and added to the terrain elevation. Disable **Use buildings** to calculate terrain shadows only.

## Daily calculation

1. Open **Raster → Urban Insolation → Urban insolation**, or use the plugin toolbar button.
2. Select the elevation model.
3. Optionally select a building layer and its height field.
4. Select a date.
5. Select an output GeoTIFF path or leave it empty to create a temporary result.
6. Click **Run**.

The interval from 03:00 to 23:00 is divided into six equal periods. Shadows are evaluated at their midpoints:

```text
04:40, 08:00, 11:20, 14:40, 18:00, 21:20
```

Times are interpreted as local longitude time at the centre of the raster. Samples for which the Sun is below the horizon are counted as shadow.

Daily output values range from `0` to `6`:

- `0` means the cell was not in shadow in any sample;
- `6` means the cell was in shadow in every sample;
- `255` is NoData.

## Annual calculation

Open **Raster → Urban Insolation → Annual urban insolation**, or open the annual window from the main dialog.

The current year is determined using local longitude time at the centre of the raster.

By default, one shadow sample is calculated at 12:00 for each day:

- common year: up to `365` shadow samples;
- leap year: up to `366` shadow samples.

Enable **Calculate for 6 day periods** to use the same six daily periods as the daily calculation:

- common year: `365 × 6 = 2190` samples;
- leap year: `366 × 6 = 2196` samples.

Annual output uses `65535` as NoData. The six-period option requires approximately six times as much shadow computation as the default annual mode.

## Local longitude time

The plugin does not use the operating system's civil time zone or daylight-saving rules. The time offset is calculated from the longitude of the raster centre:

```text
UTC offset in hours = longitude / 15
```

Examples:

| Longitude | Local longitude offset |
|---:|---:|
| 30° E | UTC+02:00 |
| 37.5° E | UTC+02:30 |
| 75° W | UTC−05:00 |

The resulting local time is converted to UTC before the solar-position calculation. Longitude is then used in the solar-time equation.

## Output styling

Result rasters are added to the current QGIS project with a linear `Purples` colour ramp:

| Raster value | Colour |
|---|---|
| Minimum | `#fcfbfd` |
| Midpoint | `#bcbddc` |
| Maximum | `#3f007d` |

The original GeoTIFF values are not modified by the display style.

## Validation and limits

- Maximum extent area: `2600 km²`.
- Maximum raster size: `100,000,000` cells.
- Extents crossing north of the Arctic Circle or south of the Antarctic Circle produce a warning.
- Polar calculations may be less representative because of polar day and polar night.
- Large rasters and annual six-period calculations can require significant processing time and memory.

For city-scale work, choose a raster resolution appropriate to the required level of detail before starting an annual calculation.

## How the calculation works

1. The DEM and optional building heights are combined into a digital surface model.
2. The raster centre is transformed to WGS 84.
3. Local longitude time and the Sun's azimuth and elevation are calculated for every sample.
4. The surface is rotated to align raster rows with the Sun direction.
5. A directional horizon scan classifies each valid cell as shadow (`1`) or not shadow (`0`).
6. The binary masks are summed and written to a GeoTIFF.

## Project structure

| Path | Purpose |
|---|---|
| `module_insolation.py` | QGIS plugin lifecycle, dialogs, validation and result styling |
| `module_insolation_dialog.py` | Dialog behaviour and input validation |
| `handlers/module_insolation_handler.py` | Solar position and local longitude time |
| `handlers/module_surface_model_builder.py` | Digital surface model generation |
| `handlers/module_shadow_calculator.py` | Daily shadow calculation |
| `handlers/module_annual_shadow_calculator.py` | Annual accumulation |
| `assets/fonts` | Bundled interface fonts and OFL licences |
| `tests` | Calculation tests |

## Testing

Run the tests with the Python executable supplied by QGIS:

```bash
<qgis-python> -B tests/test_shadow_calculator.py
```

Depending on the QGIS installation, `PROJ_DATA` and `GDAL_DATA` may need to point to the corresponding QGIS resource directories.

## Optional Numba acceleration

The default version runs `horizon_shadow_scan` as normal Python code and does not import Numba or llvmlite. This keeps the plugin portable because their binary files must match the operating system, processor architecture, Python version and QGIS build.

For local acceleration, install compatible Numba and llvmlite packages into the directory for the current platform:

```text
insolation_analysis/
└── tools/
    ├── macos/
    └── windows/
```

Use the Python interpreter supplied by the target QGIS installation. For example, on Windows:

```bash
<QGIS_PYTHON> -m pip install --target insolation_analysis/tools/windows numba llvmlite
```

Use `tools/macos` instead of `tools/windows` on macOS. Never copy these packages from another operating system, processor architecture, Python version or QGIS installation.

Installing the packages alone does not enable acceleration. To enable it in `handlers/module_shadow_calculator.py`:

1. Import `njit` and `prange` from `numba`.
2. Apply `@njit(parallel=True, cache=True)` to `horizon_shadow_scan`.
3. Replace the outer row `range` with `prange`.

On signed macOS QGIS builds, external llvmlite binaries may be rejected by system library validation. In that case, keep the default Python implementation or use binaries built specifically for that QGIS distribution.

## Third-party fonts

The optional UI fonts are distributed under the SIL Open Font License 1.1:

- `Anek Latin` — copyright 2021 The Anek Project Authors; [license text](assets/fonts/anek_latin/OFL.txt).
- `Monomaniac One` — copyright 2020 The Monomaniac Project Authors; [license text](assets/fonts/monomaniac_one/OFL.txt).

The unmodified font files may be used, embedded and redistributed with the plugin as long as the copyright notices and OFL license texts remain included. The font files may not be sold by themselves. Modified font versions must remain under OFL 1.1 and comply with its naming conditions. The OFL applies to the font software, not to documents created with it or automatically to the plugin's own source code.

The plugin can run without these font files. If a bundled font cannot be loaded, Qt uses the fallback families declared in the interface stylesheets, such as Arial or another available sans-serif system font.
