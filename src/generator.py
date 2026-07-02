"""
Stage 3 — Simulation Input Generation.

Converts harmonized terrain layers and hourly weather profiles into the
specialized CSV inputs required by cell-based fire spread engines.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import transform as warp_transform

from .schemas import WeatherPoint

logger = logging.getLogger(__name__)

DEFAULT_DURATION_HOURS = int(os.getenv("SIMULATION_DURATION_HOURS", "6"))
INTERVAL_MINUTES = 5
ROWS_PER_HOUR = 60 // INTERVAL_MINUTES
CELL2FIRE_MAX_WEATHER_ROWS = 150
FFMC_SEED = 85.0

_FWI_CONSTANTS = {
  "DMC": 60.0,
  "DC": 500.0,
  "ISI": 15.0,
  "BUI": 95.0,
  "FWI": 40.0,
}


def hourly_ffmc(
  temp: float,
  rh: float,
  ws: float,
  precip: float,
  ffmc_prev: float,
  time_step: float = 1.0,
) -> float:
  """
  Compute hourly Fine Fuel Moisture Code using Van Wagner (1977).

  Chained across simulation timesteps so each row's FFMC becomes the next
  row's moisture baseline.
  """
  moisture = 147.27723 * (101.0 - ffmc_prev) / (59.5 + ffmc_prev)

  if precip > 0.0:
    rain_factor = precip
    rain_moisture = moisture + 42.5 * rain_factor * math.exp(-100.0 / (251.0 - moisture)) * (
      1.0 - math.exp(-6.93 / rain_factor)
    )
    if moisture > 150.0:
      rain_moisture += 0.0015 * ((moisture - 150.0) ** 2) * (rain_factor ** 0.5)
    moisture = min(rain_moisture, 250.0)

  equilibrium_dry = (
    0.942 * (rh ** 0.679)
    + 11.0 * math.exp((rh - 100.0) / 10.0)
    + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh))
  )

  if moisture > equilibrium_dry:
    drying_coeff = (
      0.424 * (1.0 - (rh / 100.0) ** 1.7)
      + 0.0694 * (ws ** 0.5) * (1.0 - (rh / 100.0) ** 8)
    )
    drying_rate = drying_coeff * 0.0579 * math.exp(0.0365 * temp)
    moisture = equilibrium_dry + (moisture - equilibrium_dry) * (10.0 ** (-drying_rate * time_step))
  else:
    equilibrium_wet = (
      0.618 * (rh ** 0.753)
      + 10.0 * math.exp((rh - 100.0) / 10.0)
      + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh))
    )
    if moisture < equilibrium_wet:
      wetting_coeff = (
        0.424 * (1.0 - ((100.0 - rh) / 100.0) ** 1.7)
        + 0.0694 * (ws ** 0.5) * (1.0 - ((100.0 - rh) / 100.0) ** 8)
      )
      wetting_rate = wetting_coeff * 0.0579 * math.exp(0.0365 * temp)
      moisture = equilibrium_wet - (equilibrium_wet - moisture) * (10.0 ** (-wetting_rate * time_step))
    else:
      moisture = moisture

  ffmc = 59.5 * (250.0 - moisture) / (147.27723 + moisture)
  return max(ffmc, 0.0)


def _lerp(a: float, b: float, t: float) -> float:
  return a + (b - a) * t


def _lerp_angle(a: float, b: float, t: float) -> float:
  """Interpolate compass bearings along the shortest arc."""
  diff = (b - a + 180.0) % 360.0 - 180.0
  return (a + diff * t) % 360.0


class SimulationInputGenerator:
  """
  Synthesize Weather.csv and Ignitions.csv from harmonized terrain inputs.

  Weather generation interpolates hourly anchors to 5-minute simulation rows
  and computes chained FFMC values. Ignition generation maps a WGS84 click
  coordinate to a 1-indexed fuel-grid cell, with nearest-burnable fallback.
  """

  def __init__(
    self,
    fuel_lut_path: Optional[str] = None,
    duration_hours: int = DEFAULT_DURATION_HOURS,
  ) -> None:
    default_lut = Path(__file__).resolve().parent.parent / "data" / "fbfm40_lookup.csv"
    self.fuel_lut_path = fuel_lut_path or os.getenv("FUEL_LUT_PATH", str(default_lut))
    self.duration_hours = duration_hours

    if not os.path.exists(self.fuel_lut_path):
      raise FileNotFoundError(
        f"Fuel lookup table not found at {self.fuel_lut_path}. "
        "Set FUEL_LUT_PATH to a valid FBFM40 lookup CSV."
      )

  def generate(
    self,
    instance_path: str,
    weather_profile: Sequence[WeatherPoint],
    ignition_lat: Optional[float] = None,
    ignition_lon: Optional[float] = None,
    copy_fuel_tables: bool = True,
  ) -> Dict[str, str]:
    """
    Write all simulation CSV inputs into `instance_path`.

    Returns:
      Dictionary with `weather_csv` and `ignitions_csv` absolute paths.
    """
    os.makedirs(instance_path, exist_ok=True)
    weather_csv = self.write_weather_csv(weather_profile, instance_path)
    ignitions_csv = self.create_ignition_file(
      instance_path,
      lat=ignition_lat,
      lon=ignition_lon,
    )

    if copy_fuel_tables:
      self.copy_fuel_rules(instance_path)

    return {
      "weather_csv": weather_csv,
      "ignitions_csv": ignitions_csv,
    }

  def write_weather_csv(
    self,
    points: Sequence[WeatherPoint],
    instance_path: str,
  ) -> str:
    """Interpolate hourly weather anchors to Cell2Fire's 5-minute Weather.csv."""
    if not points or len(points) < 2:
      raise ValueError(
        "weather_profile requires at least 2 hourly WeatherPoint anchors "
        f"to interpolate; received {len(points) if points else 0}."
      )

    rows = self._interpolate_points(list(points))
    max_rows = self.duration_hours * ROWS_PER_HOUR

    if len(rows) > CELL2FIRE_MAX_WEATHER_ROWS:
      logger.warning(
        "Truncating weather rows from %s to %s (Cell2Fire hard limit)",
        len(rows),
        CELL2FIRE_MAX_WEATHER_ROWS,
      )
      rows = rows[:CELL2FIRE_MAX_WEATHER_ROWS]
    elif len(rows) > max_rows:
      rows = rows[:max_rows]

    dataframe = self._build_dataframe(rows)
    csv_path = os.path.join(instance_path, "Weather.csv")
    dataframe.to_csv(csv_path, index=False)

    logger.info(
      "Wrote Weather.csv rows=%s ws=[%.1f, %.1f] ffmc=[%.1f, %.1f]",
      len(dataframe),
      dataframe["WS"].min(),
      dataframe["WS"].max(),
      dataframe["FFMC"].min(),
      dataframe["FFMC"].max(),
    )
    return csv_path

  def create_ignition_file(
    self,
    instance_path: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
  ) -> str:
    """Map a WGS84 ignition coordinate to a 1-indexed fuel-grid cell."""
    fuels_tif = os.path.join(instance_path, "fuels.tif")
    output_path = os.path.join(instance_path, "Ignitions.csv")

    if not os.path.exists(fuels_tif):
      raise FileNotFoundError(f"fuels.tif missing in {instance_path}")

    fuel_lut = pd.read_csv(self.fuel_lut_path)
    non_burnable = set(fuel_lut[fuel_lut["fuel_type"] == "NF"]["grid_value"].tolist())
    non_burnable.update([-9999, 0, -1])
    valid_codes = set(fuel_lut["grid_value"].tolist())

    with rasterio.open(fuels_tif) as src:
      fuel_data = src.read(1)
      height, width = src.height, src.width

      if lat is not None and lon is not None:
        xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
        col, row = ~src.transform * (xs[0], ys[0])
        target_row = int(max(0, min(round(row), height - 1)))
        target_col = int(max(0, min(round(col), width - 1)))
      else:
        target_row = height // 2
        target_col = width // 2

      ignition_row, ignition_col = target_row, target_col
      target_value = fuel_data[target_row, target_col]

      if target_value in non_burnable or target_value not in valid_codes:
        burnable_mask = np.isin(fuel_data, list(valid_codes - non_burnable))
        burnable_coords = np.argwhere(burnable_mask)
        if len(burnable_coords) == 0:
          raise ValueError("No burnable cells available for ignition")

        distances = np.abs(burnable_coords - [target_row, target_col]).max(axis=1)
        ignition_row, ignition_col = burnable_coords[distances.argmin()]

    ignition_cell = (ignition_row * width) + ignition_col + 1
    pd.DataFrame({"Year": [1], "Ncell": [ignition_cell]}).to_csv(output_path, index=False)
    logger.info(
      "Created Ignitions.csv cell=%s row=%s col=%s",
      ignition_cell,
      ignition_row,
      ignition_col,
    )
    return output_path

  def copy_fuel_rules(self, instance_path: str) -> None:
    """Copy fuel lookup tables required by Cell2Fire preprocessing."""
    for filename in ("fbp_lookup_table.csv", "FuelRules.csv", "spain_lookup_table.csv"):
      shutil.copy(self.fuel_lut_path, os.path.join(instance_path, filename))

  def _interpolate_points(self, points: List[WeatherPoint]) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    ffmc_prev = FFMC_SEED
    time_step_hours = INTERVAL_MINUTES / 60.0

    for index in range(len(points)):
      start = points[index]
      end = points[index + 1] if index < len(points) - 1 else start

      for step in range(ROWS_PER_HOUR):
        t = step / ROWS_PER_HOUR
        temp = _lerp(start["temp"], end["temp"], t)
        rh = _lerp(start["rh"], end["rh"], t)
        ws = _lerp(start["ws"], end["ws"], t)
        wd = _lerp_angle(start["wd"], end["wd"], t)
        precip = start["precip"]

        ffmc_val = hourly_ffmc(
          temp=temp,
          rh=rh,
          ws=ws,
          precip=precip,
          ffmc_prev=ffmc_prev,
          time_step=time_step_hours,
        )
        ffmc_prev = ffmc_val

        anchor_dt = datetime.fromisoformat(start["timestamp"].rstrip("Z"))
        row_dt = anchor_dt + timedelta(minutes=step * INTERVAL_MINUTES)

        rows.append(
          {
            "temp": round(temp, 2),
            "rh": round(max(rh, 0.0), 2),
            "ws": round(max(ws, 0.0), 2),
            "wd": round(wd % 360.0, 2),
            "precip": precip,
            "ffmc": round(ffmc_val, 1),
            "datetime": row_dt.strftime("%Y-%m-%d %H:%M:%S"),
          }
        )

    return rows

  def _build_dataframe(self, rows: List[Dict[str, float]]) -> pd.DataFrame:
    count = len(rows)
    return pd.DataFrame(
      {
        "Scenario": ["default"] * count,
        "datetime": [row["datetime"] for row in rows],
        "APCP": [row["precip"] for row in rows],
        "TMP": [row["temp"] for row in rows],
        "RH": [row["rh"] for row in rows],
        "WS": [row["ws"] for row in rows],
        "WD": [row["wd"] for row in rows],
        "FFMC": [row["ffmc"] for row in rows],
        "DMC": [_FWI_CONSTANTS["DMC"]] * count,
        "DC": [_FWI_CONSTANTS["DC"]] * count,
        "ISI": [_FWI_CONSTANTS["ISI"]] * count,
        "BUI": [_FWI_CONSTANTS["BUI"]] * count,
        "FWI": [_FWI_CONSTANTS["FWI"]] * count,
      }
    )
