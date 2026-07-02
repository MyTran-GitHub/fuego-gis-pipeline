"""
Stage 2 — Raster Harmonization.

Splits multi-band LANDFIRE GeoTIFFs into named layers, aligns grids to a
reference CRS/transform, and exports ESRI ASCII matrices for cell-based
simulation engines.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

from .extractor import ExtractionResult, RasterResult
from .schemas import TerrainMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarmonizationResult:
  """Paths and metadata produced by raster harmonization."""

  instance_path: str
  layers: Dict[str, str]
  ascii_layers: Dict[str, str]
  metadata: TerrainMetadata


class RasterHarmonizer:
  """
  Transform raw LANDFIRE extraction output into simulation-ready raster grids.

  Responsibilities:
    1. Split the multi-band GeoTIFF into semantic single-band layers.
    2. Reproject ancillary rasters onto the reference fuel grid when needed.
    3. Export aligned integer ASCII grids required by Cell2Fire.
  """

  NAME_MAP: Mapping[str, str] = {
    "LF2024_FBFM40": "fuels",
    "LF2020_Elev": "elevation",
    "LF2020_SlpD": "slope",
    "LF2020_Asp": "aspect",
    "LF2024_CC": "canopy_cover",
    "LF2024_CH": "canopy_height",
    "LF2024_CBH": "canopy_base_height",
    "LF2024_CBD": "canopy_bulk_density",
    "240FBFM40": "fuels",
    "ELEV2020": "elevation",
    "SLPD2020": "slope",
    "ASP2020": "aspect",
    "240CC": "canopy_cover",
    "LF2022_FBFM40": "fuels",
    "LF2022_CC": "canopy_cover",
  }

  ASC_MAP: Mapping[str, str] = {
    "fuels.tif": "Forest.asc",
    "elevation.tif": "elevation.asc",
    "slope.tif": "slope.asc",
    "aspect.tif": "saz.asc",
  }

  def harmonize(
    self,
    extraction: ExtractionResult,
    output_dir: str,
    write_metadata: bool = True,
  ) -> HarmonizationResult:
    """
    Split, align, and export harmonized terrain layers.

    Args:
      extraction: Result from `LandfireExtractor.fetch()`.
      output_dir: Directory for harmonized TIF/ASC artifacts.
      write_metadata: Persist metadata.json for downstream vectorization.

    Returns:
      HarmonizationResult with layer paths and spatial metadata.
    """
    os.makedirs(output_dir, exist_ok=True)
    layer_results = self._split_multiband_tif(extraction, output_dir)
    ascii_layers = self._export_ascii_layers(output_dir)
    metadata = self._build_metadata(layer_results["fuels"])

    if write_metadata:
      metadata_path = Path(output_dir) / "metadata.json"
      metadata_path.write_text(json.dumps(metadata, indent=2))

    return HarmonizationResult(
      instance_path=output_dir,
      layers={name: result.path for name, result in layer_results.items()},
      ascii_layers=ascii_layers,
      metadata=metadata,
    )

  def reproject_to_reference(
    self,
    source_path: str,
    reference_path: str,
    output_path: str,
    resampling: Resampling = Resampling.bilinear,
  ) -> str:
    """
    Reproject `source_path` onto the CRS, transform, and dimensions of
    `reference_path`.

    Nearest-neighbor resampling is appropriate for categorical fuel layers;
    bilinear resampling is preferred for continuous elevation/slope surfaces.
    """
    if not os.path.exists(source_path):
      raise FileNotFoundError(source_path)
    if not os.path.exists(reference_path):
      raise FileNotFoundError(reference_path)

    with rasterio.open(reference_path) as reference:
      dst_crs = reference.crs
      dst_transform = reference.transform
      dst_width = reference.width
      dst_height = reference.height
      dst_nodata = reference.nodata if reference.nodata is not None else 0

    with rasterio.open(source_path) as source:
      destination = np.zeros((dst_height, dst_width), dtype=source.dtypes[0])
      reproject(
        source=rasterio.band(source, 1),
        destination=destination,
        src_transform=source.transform,
        src_crs=source.crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=resampling,
        dst_nodata=dst_nodata,
      )

      profile = source.profile.copy()
      profile.update(
        crs=dst_crs,
        transform=dst_transform,
        width=dst_width,
        height=dst_height,
        count=1,
        nodata=dst_nodata,
        compress="lzw",
      )

      with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(destination, 1)

    logger.info("Reprojected %s -> %s", os.path.basename(source_path), output_path)
    return output_path

  def _split_multiband_tif(
    self,
    extraction: ExtractionResult,
    output_dir: str,
  ) -> Dict[str, RasterResult]:
    raster = extraction.data
    raw_path = raster.path
    band_mapping = raster.band_mapping
    outputs: Dict[str, RasterResult] = {}

    logger.info("Splitting %s bands into %s", len(band_mapping), output_dir)

    with rasterio.open(raw_path) as src:
      for band_key, product_code in band_mapping.items():
        band_index = int(band_key.split("_")[1])
        layer_name = self.NAME_MAP.get(product_code, product_code.lower())
        out_path = os.path.join(output_dir, f"{layer_name}.tif")

        profile = src.profile.copy()
        profile.update(count=1)

        with rasterio.open(out_path, "w", **profile) as dst:
          dst.write(src.read(band_index), 1)

        outputs[layer_name] = RasterResult(
          path=str(Path(out_path).resolve()),
          source="landfire",
          layer=layer_name,
          native_crs=raster.native_crs,
          native_res_m=raster.native_res_m,
          band_mapping={"band_1": product_code},
          acquired_at=raster.acquired_at,
          bounds=raster.bounds,
        )

    if "fuels" not in outputs:
      raise ValueError("Harmonization requires a fuels layer in the LANDFIRE bundle")

    return outputs

  def _export_ascii_layers(self, instance_path: str) -> Dict[str, str]:
    ascii_paths: Dict[str, str] = {}

    for tif_name, asc_name in self.ASC_MAP.items():
      tif_path = os.path.join(instance_path, tif_name)
      asc_path = os.path.join(instance_path, asc_name)

      if not os.path.exists(tif_path):
        logger.warning("Skipping ASC export; missing %s", tif_name)
        continue

      self._tif_to_asc(tif_path, asc_path)
      ascii_paths[asc_name] = asc_path

    return ascii_paths

  def _tif_to_asc(self, tif_path: str, asc_path: str) -> None:
    """
    Write a GeoTIFF as ESRI ASCII with integer-only headers.

    Cell2Fire's C++ parser uses `std::stoi()` and requires single-space
    separation between cell values.
    """
    with rasterio.open(tif_path) as src:
      data = src.read(1)
      transform = src.transform
      nodata = src.nodata if src.nodata is not None else -9999

      header = (
        f"ncols {src.width}\n"
        f"nrows {src.height}\n"
        f"xllcorner {int(transform[2])}\n"
        f"yllcorner {int(transform[5] + src.height * transform[4])}\n"
        f"cellsize {int(transform[0])}\n"
        f"NODATA_value {int(nodata)}\n"
      )

      with open(asc_path, "w", encoding="utf-8") as handle:
        handle.write(header)
        for row in data:
          line = " ".join(
            str(int(value)) if value != nodata else str(int(nodata))
            for value in row
          )
          handle.write(line + "\n")

    logger.info("Converted %s -> %s", os.path.basename(tif_path), os.path.basename(asc_path))

  def _build_metadata(self, fuels_layer: RasterResult) -> TerrainMetadata:
    with rasterio.open(fuels_layer.path) as src:
      epsg = src.crs.to_epsg() if src.crs else 5070
      return {
        "spatial_info": {
          "epsg": int(epsg) if epsg is not None else 5070,
          "bounds": list(src.bounds),
          "resolution": float(src.res[0]),
        }
      }
