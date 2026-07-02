"""Lightweight spatial helpers for bounding-box construction and CRS selection."""

from __future__ import annotations

import math
from typing import NamedTuple, Tuple


class CRSSuggestion(NamedTuple):
  """Human-readable CRS recommendation for a geographic area of interest."""

  epsg: str
  label: str


def point_to_bbox(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
  """
  Build a WGS84 bounding box from a center point and radius.

  Uses a degree-offset approximation suitable for regional tile-scale queries.
  """
  if not (-90.0 <= lat <= 90.0):
    raise ValueError(f"latitude must be in [-90, 90], got {lat}")
  if not (-180.0 <= lon <= 180.0):
    raise ValueError(f"longitude must be in [-180, 180], got {lon}")
  if radius_km <= 0:
    raise ValueError(f"radius_km must be positive, got {radius_km}")

  lat_delta = radius_km / 111.0
  lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

  return (
    lon - lon_delta,
    lat - lat_delta,
    lon + lon_delta,
    lat + lat_delta,
  )


def suggest_crs(bbox: Tuple[float, float, float, float]) -> CRSSuggestion:
  """
  Recommend a projected CRS for harmonization based on AOI extent.

  Small AOIs receive a local UTM zone; CONUS receives EPSG:5070 to match
  LANDFIRE products; all other regions fall back to EPSG:6933.
  """
  xmin, ymin, xmax, ymax = bbox
  center_lon = (xmin + xmax) / 2.0
  center_lat = (ymin + ymax) / 2.0
  width_km = (xmax - xmin) * 111.320 * math.cos(math.radians(center_lat))

  if width_km < 500:
    zone = int((center_lon + 180) / 6) + 1
    if center_lat >= 0:
      return CRSSuggestion(epsg=f"EPSG:{32600 + zone}", label=f"UTM Zone {zone}N")
    return CRSSuggestion(epsg=f"EPSG:{32700 + zone}", label=f"UTM Zone {zone}S")

  if -125 <= xmin and xmax <= -66 and 24 <= ymin and ymax <= 50:
    return CRSSuggestion(epsg="EPSG:5070", label="Albers Equal Area CONUS")

  return CRSSuggestion(epsg="EPSG:6933", label="Equal-Area Cylindrical (global)")


def snap_geo_center(lat: float, lon: float, radius_km: float) -> Tuple[float, float]:
  """
  Snap a click coordinate to a coarse cache grid.

  Nearby requests share the same LANDFIRE download when radii overlap.
  """
  step_deg = (radius_km / 2.0) / 111.0
  snapped_lat = round(round(lat / step_deg) * step_deg, 5)
  snapped_lon = round(round(lon / step_deg) * step_deg, 5)
  return snapped_lat, snapped_lon
