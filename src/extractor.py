"""
Stage 1 — Dataset Acquisition (Automated ETL).

Programmatic connection to the USGS LANDFIRE Processing Service. Submits an
asynchronous extraction job for a WGS84 bounding box, polls federal job queues,
and streams down a multi-band GeoTIFF containing fuel and terrain layers.
"""

from __future__ import annotations

import logging
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
import requests

from .spatial import point_to_bbox

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RasterResult:
  """Validated raster artifact returned by the extractor."""

  path: str
  source: str
  layer: str
  native_crs: str
  native_res_m: float
  band_mapping: Dict[str, str]
  acquired_at: str
  bounds: Tuple[float, float, float, float]


@dataclass(frozen=True)
class ExtractionResult:
  """Top-level extraction envelope consumed by the harmonizer."""

  source: str
  data: RasterResult
  metadata: Dict[str, Any]


class LandfireExtractor:
  """
  Fetch multi-band LANDFIRE landscape layers for a point-and-radius AOI.

  The extractor manages the full federal API lifecycle: job submission, status
  polling with retry tolerance, ZIP download, GeoTIFF validation, and metadata
  construction for downstream harmonization.
  """

  SOURCE_ID = "landfire"
  DEFAULT_PRODUCTS = (
    "LF2024_FBFM40",
    "LF2020_Elev",
    "LF2020_SlpD",
    "LF2020_Asp",
    "LF2024_CC",
    "LF2024_CH",
    "LF2024_CBH",
    "LF2024_CBD",
  )

  def __init__(
    self,
    email: Optional[str] = None,
    api_url: Optional[str] = None,
    products: Optional[List[str]] = None,
    projection: Optional[str] = None,
    poll_interval_s: int = 15,
    poll_timeout_s: int = 600,
  ) -> None:
    self.email = email or os.getenv("LANDFIRE_EMAIL", "")
    if not self.email:
      raise ValueError(
        "LANDFIRE_EMAIL is required. Register at https://lfps.usgs.gov/ "
        "and set the environment variable before running extraction."
      )

    self.api_url = (
      api_url
      or os.getenv("LANDFIRE_API_URL", "https://lfps.usgs.gov/api/job")
    ).rstrip("/")
    self.products = list(products or self._products_from_env())
    self.projection = projection or os.getenv("LANDFIRE_PROJECTION", "5070")
    self.poll_interval_s = poll_interval_s
    self.poll_timeout_s = poll_timeout_s

    logger.info("LandfireExtractor initialized for projection EPSG:%s", self.projection)

  @staticmethod
  def _products_from_env() -> List[str]:
    raw = os.getenv("LANDFIRE_PRODUCTS", "")
    if raw.strip():
      return [item.strip() for item in raw.split(",") if item.strip()]
    return list(LandfireExtractor.DEFAULT_PRODUCTS)

  def fetch(
    self,
    lat: float,
    lon: float,
    radius_km: float,
    output_dir: Optional[str] = None,
  ) -> ExtractionResult:
    """
    Run the full LANDFIRE extraction workflow for a location.

    Args:
      lat: WGS84 latitude of AOI center.
      lon: WGS84 longitude of AOI center.
      radius_km: Half-width of the square AOI in kilometers.
      output_dir: Optional destination for raw artifacts.

    Returns:
      ExtractionResult containing the validated multi-band GeoTIFF path.

    Raises:
      ValueError: If API responses are malformed.
      TimeoutError: If the federal queue does not complete within the timeout.
      requests.HTTPError: On unrecoverable HTTP failures.
    """
    geo_slug = f"lat{lat}_lon{lon}_rad{radius_km}"
    resolved_output_dir = output_dir or os.path.join(
      os.getenv("PIPELINE_RAW_DIR", "data/raw"),
      geo_slug,
    )

    try:
      job_id = self._submit_job(lat, lon, radius_km)
      download_url = self._poll_for_download_url(job_id)
      tif_path = self._download_and_extract(download_url, resolved_output_dir)
      raster_meta = self._validate_and_build_result(tif_path, lat, lon)
    except Exception as exc:
      logger.exception("LANDFIRE extraction failed for %s: %s", geo_slug, exc)
      raise

    return ExtractionResult(
      source=self.SOURCE_ID,
      data=raster_meta,
      metadata={
        "job_id": job_id,
        "output_dir": resolved_output_dir,
        "geo_slug": geo_slug,
      },
    )

  def validate(self, result: ExtractionResult) -> bool:
    """Return True when the extracted GeoTIFF is readable and non-empty."""
    path = result.data.path
    if not path or not os.path.exists(path):
      logger.error("validate: raster path does not exist: %s", path)
      return False

    try:
      with rasterio.open(path) as src:
        if src.count != len(self.products):
          logger.error(
            "validate: expected %s bands, found %s",
            len(self.products),
            src.count,
          )
          return False

        sample = src.read(1)
        nodata = src.nodata
        if np.all(sample == nodata) or np.all(sample == 0):
          logger.error("validate: raster contains no valid pixels")
          return False
    except Exception as exc:
      logger.error("validate: could not open raster — %s", exc)
      return False

    return True

  def _submit_job(self, lat: float, lon: float, radius_km: float) -> str:
    bbox = point_to_bbox(lat, lon, radius_km)
    payload = {
      "Email": self.email,
      "Layer_List": ";".join(self.products),
      "Area_of_Interest": " ".join(str(v) for v in bbox),
      "Output_Projection": self.projection,
    }

    logger.info("Submitting LANDFIRE job lat=%s lon=%s radius_km=%s", lat, lon, radius_km)
    response = requests.post(f"{self.api_url}/submit", json=payload, timeout=60)
    response.raise_for_status()

    job_id = response.json().get("jobId")
    if not job_id:
      raise ValueError(f"LANDFIRE submit response missing jobId: {response.text}")

    return str(job_id)

  def _poll_for_download_url(self, job_id: str) -> str:
    """Poll the federal queue until the job succeeds or fails."""
    start = time.time()
    last_status = "Unknown"

    while (time.time() - start) < self.poll_timeout_s:
      try:
        response = requests.get(
          f"{self.api_url}/status",
          params={"JobId": job_id},
          timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        last_status = str(payload.get("status", "Unknown"))

        if last_status == "Succeeded":
          output_url = payload.get("outputFile")
          if not output_url:
            raise ValueError(f"Succeeded job missing outputFile: {payload}")
          return str(output_url)

        if last_status in {"Failed", "Canceled"}:
          raise RuntimeError(
            f"LANDFIRE job {job_id} {last_status}: {payload.get('messages')}"
          )

        logger.info(
          "LANDFIRE status=%s elapsed=%ss",
          last_status,
          int(time.time() - start),
        )
      except requests.RequestException as exc:
        logger.warning("LANDFIRE poll transient failure: %s", exc)

      time.sleep(self.poll_interval_s)

    raise TimeoutError(
      f"LANDFIRE job {job_id} timed out after {self.poll_timeout_s}s "
      f"(last_status={last_status})"
    )

  def _download_and_extract(self, url: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    zip_path = Path(output_dir) / "bundle.zip"

    response = requests.get(url, timeout=300)
    response.raise_for_status()
    zip_path.write_bytes(response.content)

    with zipfile.ZipFile(zip_path, "r") as archive:
      archive.extractall(output_dir)

    zip_path.unlink(missing_ok=True)

    for filename in os.listdir(output_dir):
      if filename.endswith(".tif") and not filename.endswith(".xml"):
        return str(Path(output_dir) / filename)

    raise FileNotFoundError(f"No GeoTIFF found in LANDFIRE bundle: {output_dir}")

  def _validate_and_build_result(
    self,
    tif_path: str,
    lat: float,
    lon: float,
  ) -> RasterResult:
    with rasterio.open(tif_path) as src:
      if src.count != len(self.products):
        raise ValueError(
          f"Band mismatch: expected {len(self.products)}, got {src.count}"
        )

      return RasterResult(
        path=str(Path(tif_path).resolve()),
        source=self.SOURCE_ID,
        layer="multi",
        native_crs=src.crs.to_string() if src.crs else "unknown",
        native_res_m=float(src.res[0]),
        band_mapping={f"band_{index + 1}": product for index, product in enumerate(self.products)},
        acquired_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        bounds=tuple(src.bounds),
      )
