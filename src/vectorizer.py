"""
Frontend Layer Optimization — Simulation Vectorization.

Converts Cell2Fire binary burn grids into lightweight GeoJSON time-series
layers suitable for Mapbox GL JS rendering.
"""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
from affine import Affine
from rasterio.features import shapes
from rasterio.transform import from_bounds
from rasterio.warp import transform as warp_transform_coords
from rasterio.warp import transform_geom

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", message=".*SettingWithCopyWarning.*", category=Warning)
warnings.filterwarnings(
  "ignore",
  message="A value is trying to be set on a copy of a slice.*",
  category=Warning,
)

Coordinate = Union[float, Sequence[float]]
GeometryDict = Dict[str, Any]


class SimulationVectorizer:
  """
  Vectorize simulation burn grids for frontend consumption.

  The vectorizer keeps all heavy GIS work server-side. Outputs are simplified,
  reprojected to WGS84, and coordinate precision is truncated to five decimal
  places (~1 meter) to minimize payload size for 60fps Mapbox animations.
  """

  DEFAULT_SRC_CRS = "EPSG:5070"
  DEFAULT_DST_CRS = "EPSG:4326"
  COORD_PRECISION = 5

  def convert_simulation_grids(
    self,
    simulation_dir: str,
    raw_data_dir: str,
    output_path: Optional[str] = None,
    instance_path: Optional[str] = None,
    sim_date: Optional[str] = None,
    start_hour: Optional[int] = None,
    simplify_tolerance: float = 0.0001,
  ) -> Dict[str, Any]:
    """
    Convert all ForestGrid CSV timesteps into a GeoJSON time-series envelope.

    Args:
      simulation_dir: Directory containing Cell2Fire `Grids/Grids1` outputs.
      raw_data_dir: Directory containing harmonized `metadata.json`.
      output_path: Optional path to persist the JSON response.
      instance_path: Optional directory containing `Weather.csv`.
      sim_date: Optional `YYYY-MM-DD` fire date for timestamp synthesis.
      start_hour: Optional UTC start hour paired with `sim_date`.
      simplify_tolerance: Douglas-Peucker tolerance in decimal degrees.

    Returns:
      Dictionary with `simulation` summary metadata and `geojson` features.
    """
    sim_path = Path(simulation_dir)
    raw_path = Path(raw_data_dir)
    metadata_path = raw_path / "metadata.json"

    if not metadata_path.exists():
      raise FileNotFoundError(f"Metadata not found: {metadata_path}")

    grids_dir = sim_path / "Grids" / "Grids1"
    grid_files = sorted(grids_dir.glob("ForestGrid*.csv"))
    if not grid_files:
      raise FileNotFoundError(f"No grid files found in {grids_dir}")

    metadata = self._load_metadata(str(metadata_path))
    cell_size = float(metadata["spatial_info"]["resolution"])
    bounds = metadata["spatial_info"]["bounds"]
    weather_by_step = self._load_weather_by_step(instance_path) if instance_path else {}

    logger.info(
      "Vectorizing %s grid files (%s weather)",
      len(grid_files),
      "with" if weather_by_step else "without",
    )

    from shapely.geometry import mapping, shape
    from shapely.validation import make_valid

    all_features: List[Dict[str, Any]] = []
    step_properties: List[Dict[str, Any]] = []
    previous_cumulative = None

    for grid_file in grid_files:
      step = int(grid_file.stem.replace("ForestGrid", ""))
      grid = self._load_grid_csv(str(grid_file))
      rows, cols = grid.shape
      transform = self._create_transform(bounds, rows, cols)
      geometries = self._vectorize_burn_area(grid, transform)

      if not geometries:
        continue

      total_cells = int(grid.size)
      burned_cells = int(np.sum(grid == 1))
      burned_area_m2 = burned_cells * (cell_size ** 2)
      burned_area_acres = burned_area_m2 * 0.000247105

      smoothed_5070 = self._smooth_and_union_geometries(geometries, cell_size)
      cumulative_shape = shape(smoothed_5070)
      if not cumulative_shape.is_valid:
        cumulative_shape = make_valid(cumulative_shape)

      if previous_cumulative is not None:
        incremental_shape = cumulative_shape.difference(previous_cumulative)
        if not incremental_shape.is_valid:
          incremental_shape = make_valid(incremental_shape)
        incremental_5070 = smoothed_5070 if incremental_shape.is_empty else mapping(incremental_shape)
      else:
        incremental_5070 = smoothed_5070

      previous_cumulative = cumulative_shape

      incremental_4326 = self._truncate_geometry(
        self._reproject_geometry(incremental_5070),
        self.COORD_PRECISION,
      )
      cumulative_4326 = self._truncate_geometry(
        self._reproject_geometry(smoothed_5070),
        self.COORD_PRECISION,
      )
      perimeter_4326 = self._truncate_geometry(
        self._extract_perimeter(cumulative_4326),
        self.COORD_PRECISION,
      )

      if simplify_tolerance > 0:
        incremental_4326 = self._simplify_geometry(incremental_4326, simplify_tolerance)
        perimeter_4326 = self._simplify_geometry(perimeter_4326, simplify_tolerance)

      elapsed_min = step * 5
      timestamp = None
      if sim_date and start_hour is not None:
        step_dt = datetime.strptime(sim_date, "%Y-%m-%d").replace(
          hour=start_hour,
          tzinfo=timezone.utc,
        ) + timedelta(minutes=elapsed_min)
        timestamp = step_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

      all_features.append(
        {
          "id": step * 1000,
          "type": "Feature",
          "properties": {
            "step": step,
            "type": "burned_area",
            "burned": True,
            "acres": round(burned_area_acres, 2),
          },
          "geometry": incremental_4326,
        }
      )
      all_features.append(
        {
          "id": step * 1000 + 1,
          "type": "Feature",
          "properties": {
            "step": step,
            "type": "perimeter",
          },
          "geometry": perimeter_4326,
        }
      )

      step_properties.append(
        {
          "step": step,
          "elapsed_min": elapsed_min,
          "timestamp": timestamp,
          "burned_area_ha": round(burned_area_m2 * 0.0001, 2),
          "burned_area_acres": round(burned_area_acres, 2),
          "percent_burned": round(burned_cells / total_cells * 100, 2),
          "weather": weather_by_step.get(step),
        }
      )

    if (
      len(step_properties) >= 2
      and step_properties[-1].get("burned_area_acres")
      == step_properties[-2].get("burned_area_acres")
    ):
      dropped_step = step_properties[-1]["step"]
      step_properties = step_properties[:-1]
      all_features = [feature for feature in all_features if feature["properties"]["step"] != dropped_step]

    result = {
      "simulation": {
        "id": sim_path.name,
        "total_steps": len(step_properties),
        "steps": step_properties,
        "bounds_wgs84": self.get_bounds_wgs84(metadata),
        "cell_size_m": cell_size,
        "sim_date": sim_date,
        "start_hour": start_hour,
      },
      "geojson": {
        "type": "FeatureCollection",
        "features": all_features,
      },
    }

    if output_path:
      Path(output_path).write_text(json.dumps(result))
      logger.info("Saved vectorized simulation to %s", output_path)

    return result

  def get_bounds_wgs84(self, metadata: Dict[str, Any]) -> List[float]:
    """Project harmonized bounds to WGS84."""
    bounds = metadata["spatial_info"]["bounds"]
    xmin, ymin, xmax, ymax = bounds
    xs = [xmin, xmax, xmax, xmin]
    ys = [ymin, ymin, ymax, ymax]
    xs_wgs84, ys_wgs84 = warp_transform_coords(self.DEFAULT_SRC_CRS, self.DEFAULT_DST_CRS, xs, ys)
    return [min(xs_wgs84), min(ys_wgs84), max(xs_wgs84), max(ys_wgs84)]

  def _load_weather_by_step(self, instance_path: Optional[str]) -> Dict[int, Dict[str, float]]:
    if not instance_path:
      return {}

    weather_path = Path(instance_path) / "Weather.csv"
    if not weather_path.exists():
      logger.warning("Weather.csv not found at %s", weather_path)
      return {}

    try:
      dataframe = pd.read_csv(weather_path)
      weather_by_step: Dict[int, Dict[str, float]] = {}
      for index, row in dataframe.iterrows():
        weather_by_step[int(index)] = {
          "temp_c": round(float(row["TMP"]), self.COORD_PRECISION),
          "rh": round(float(row["RH"]), self.COORD_PRECISION),
          "ws_kmh": round(float(row["WS"]), self.COORD_PRECISION),
          "wd_deg": round(float(row["WD"]), self.COORD_PRECISION),
          "ffmc": round(float(row["FFMC"]), self.COORD_PRECISION),
        }
      return weather_by_step
    except Exception as exc:
      logger.warning("Failed to read Weather.csv: %s", exc)
      return {}

  @staticmethod
  def _load_grid_csv(csv_path: str) -> np.ndarray:
    return np.loadtxt(csv_path, delimiter=",", dtype=np.uint8)

  @staticmethod
  def _load_metadata(metadata_path: str) -> Dict[str, Any]:
    with open(metadata_path, "r", encoding="utf-8") as handle:
      return json.load(handle)

  @staticmethod
  def _create_transform(bounds: List[float], rows: int, cols: int) -> Affine:
    xmin, ymin, xmax, ymax = bounds
    return from_bounds(xmin, ymin, xmax, ymax, cols, rows)

  def _vectorize_burn_area(
    self,
    grid: np.ndarray,
    transform: Affine,
    burn_value: int = 1,
  ) -> List[GeometryDict]:
    mask = grid == burn_value
    geometries: List[GeometryDict] = []
    for geometry, value in shapes(grid, mask=mask, transform=transform):
      if value == burn_value:
        geometries.append(geometry)
    return geometries

  def _reproject_geometry(
    self,
    geometry: GeometryDict,
    src_crs: str = DEFAULT_SRC_CRS,
    dst_crs: str = DEFAULT_DST_CRS,
  ) -> GeometryDict:
    return transform_geom(src_crs, dst_crs, geometry)

  def _simplify_geometry(self, geometry: GeometryDict, tolerance: float) -> GeometryDict:
    from shapely.geometry import mapping, shape
    from shapely.validation import make_valid

    shapely_geom = shape(geometry)
    if not shapely_geom.is_valid:
      shapely_geom = make_valid(shapely_geom)
    simplified = shapely_geom.simplify(tolerance, preserve_topology=True)
    return mapping(simplified)

  def _smooth_and_union_geometries(
    self,
    geometries: List[GeometryDict],
    cell_size: float,
  ) -> GeometryDict:
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    from shapely.validation import make_valid

    polygons = [shape(geometry) for geometry in geometries]
    merged = unary_union(polygons)
    if not merged.is_valid:
      merged = make_valid(merged)

    smoothed = merged.buffer(cell_size * 0.4).buffer(-cell_size * 0.3)
    smoothed = smoothed.simplify(cell_size * 0.3, preserve_topology=True)
    if not smoothed.is_valid:
      smoothed = make_valid(smoothed)
    if smoothed.is_empty:
      return mapping(merged)
    return mapping(smoothed)

  def _extract_perimeter(self, geometry: GeometryDict) -> GeometryDict:
    from shapely.geometry import mapping, shape

    return mapping(shape(geometry).boundary)

  def _truncate_geometry(self, geometry: GeometryDict, precision: int) -> GeometryDict:
    """Reduce coordinate precision to keep frontend payloads lightweight."""

    def _truncate_value(value: Coordinate) -> Coordinate:
      if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
          return [_truncate_value(item) for item in value]
        return [round(float(item), precision) for item in value]
      return round(float(value), precision)

    truncated = dict(geometry)
    if "coordinates" in truncated:
      truncated["coordinates"] = _truncate_value(truncated["coordinates"])
    return truncated
