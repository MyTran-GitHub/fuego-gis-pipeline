"""Shared typed schemas for pipeline inputs and outputs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, TypedDict


class WeatherPoint(TypedDict):
  """Single hourly weather observation used by the simulation input generator."""

  lat: float
  lon: float
  timestamp: str
  temp: float
  rh: float
  ws: float
  wd: float
  precip: float
  pressure: Optional[float]


class SpatialInfo(TypedDict):
  """Spatial envelope consumed by simulation engines and the vectorizer."""

  epsg: int
  bounds: List[float]
  resolution: float


class TerrainMetadata(TypedDict):
  """Terrain metadata written alongside harmonized rasters."""

  spatial_info: SpatialInfo


class PipelineRequest(TypedDict, total=False):
  """Standard parameters accepted by backend orchestration services."""

  location: Tuple[float, float]
  radius_km: float
  weather_profile: List[WeatherPoint]
  ignition_lat: float
  ignition_lon: float
  output_dir: str


class PipelineResult(TypedDict):
  """Structured response returned to FastAPI orchestration layers."""

  instance_path: str
  terrain_layers: Dict[str, str]
  weather_csv: str
  ignitions_csv: str
  metadata: TerrainMetadata
  geojson: Optional[Dict[str, Any]]
