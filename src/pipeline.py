"""
Backend integration facade.

Provides a single entry point that FastAPI services can call with simple,
standardized parameters while keeping CPU-heavy GIS work isolated from HTTP
routing, authentication, and caching concerns.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .extractor import LandfireExtractor
from .generator import SimulationInputGenerator
from .harmonizer import RasterHarmonizer
from .schemas import PipelineResult, TerrainMetadata, WeatherPoint
from .spatial import snap_geo_center
from .vectorizer import SimulationVectorizer

logger = logging.getLogger(__name__)


class GisPipeline:
  """
  Orchestrate terrain extraction, harmonization, and simulation input synthesis.

  Typical FastAPI usage:

      pipeline = GisPipeline()
      result = pipeline.prepare_simulation_inputs(
          location=(38.25, -120.25),
          radius_km=3.0,
          weather_profile=hourly_points,
      )
  """

  def __init__(
    self,
    raw_dir: Optional[str] = None,
    instance_dir: Optional[str] = None,
  ) -> None:
    self.raw_dir = raw_dir or os.getenv("PIPELINE_RAW_DIR", "data/raw")
    self.instance_dir = instance_dir or os.getenv("PIPELINE_INSTANCE_DIR", "data/instances")
    self.extractor = LandfireExtractor()
    self.harmonizer = RasterHarmonizer()
    self.generator = SimulationInputGenerator()
    self.vectorizer = SimulationVectorizer()

  def prepare_simulation_inputs(
    self,
    location: Tuple[float, float],
    radius_km: float,
    weather_profile: Sequence[WeatherPoint],
    ignition_lat: Optional[float] = None,
    ignition_lon: Optional[float] = None,
    force_refresh: bool = False,
  ) -> PipelineResult:
    """
    Run Stages 1–3 and return simulation-ready artifacts plus metadata.

    Args:
      location: `(lat, lon)` ignition-centered AOI in WGS84.
      radius_km: Square AOI radius in kilometers.
      weather_profile: Hourly weather anchors for Weather.csv synthesis.
      ignition_lat: Optional explicit ignition latitude.
      ignition_lon: Optional explicit ignition longitude.
      force_refresh: Re-download LANDFIRE even when cached terrain exists.

    Returns:
      Structured artifact paths for backend orchestration layers.
    """
    lat, lon = location
    snap_lat, snap_lon = snap_geo_center(lat, lon, radius_km)
    geo_slug = f"lat{snap_lat}_lon{snap_lon}_rad{radius_km}"

    raw_path = os.path.join(self.raw_dir, geo_slug)
    instance_path = os.path.join(self.instance_dir, geo_slug)
    fuels_tif = os.path.join(instance_path, "fuels.tif")

    if force_refresh or not os.path.exists(fuels_tif):
      logger.info("Running LANDFIRE extraction for %s", geo_slug)
      extraction = self.extractor.fetch(
        snap_lat,
        snap_lon,
        radius_km,
        output_dir=raw_path,
      )
      if not self.extractor.validate(extraction):
        raise RuntimeError("LANDFIRE extraction failed validation")

      harmonized = self.harmonizer.harmonize(extraction, instance_path)
      metadata = harmonized.metadata
    else:
      logger.info("Using cached harmonized terrain for %s", geo_slug)
      metadata = self._load_metadata(instance_path)

    csv_paths = self.generator.generate(
      instance_path=instance_path,
      weather_profile=weather_profile,
      ignition_lat=ignition_lat or lat,
      ignition_lon=ignition_lon or lon,
    )

    return {
      "instance_path": instance_path,
      "terrain_layers": self._collect_layer_paths(instance_path),
      "weather_csv": csv_paths["weather_csv"],
      "ignitions_csv": csv_paths["ignitions_csv"],
      "metadata": metadata,
      "geojson": None,
    }

  def vectorize_simulation_output(
    self,
    simulation_dir: str,
    raw_data_dir: str,
    instance_path: Optional[str] = None,
    sim_date: Optional[str] = None,
    start_hour: Optional[int] = None,
    output_path: Optional[str] = None,
  ) -> Dict[str, Any]:
    """Convert post-simulation grids into frontend-ready GeoJSON."""
    return self.vectorizer.convert_simulation_grids(
      simulation_dir=simulation_dir,
      raw_data_dir=raw_data_dir,
      instance_path=instance_path,
      sim_date=sim_date,
      start_hour=start_hour,
      output_path=output_path,
    )

  def _load_metadata(self, instance_path: str) -> TerrainMetadata:
    metadata_path = Path(instance_path) / "metadata.json"
    if metadata_path.exists():
      return json.loads(metadata_path.read_text())

    fuels_tif = Path(instance_path) / "fuels.tif"
    if not fuels_tif.exists():
      raise FileNotFoundError(f"No terrain metadata or fuels.tif in {instance_path}")

    from .extractor import RasterResult

    placeholder = RasterResult(
      path=str(fuels_tif),
      source="landfire",
      layer="fuels",
      native_crs="EPSG:5070",
      native_res_m=30.0,
      band_mapping={"band_1": "LF2024_FBFM40"},
      acquired_at="",
      bounds=(0.0, 0.0, 0.0, 0.0),
    )
    return self.harmonizer._build_metadata(placeholder)  # noqa: SLF001

  @staticmethod
  def _collect_layer_paths(instance_path: str) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    for filename in os.listdir(instance_path):
      if filename.endswith(".tif") or filename.endswith(".asc"):
        key = Path(filename).stem
        paths[key] = os.path.join(instance_path, filename)
    return paths
