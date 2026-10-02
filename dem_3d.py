"""API DEM 3D: luoi cao do, anh texture, tile OSM."""

from __future__ import annotations

import base64
import traceback
from io import BytesIO

import numpy as np
from flask import Blueprint, Response, jsonify, make_response, request, send_file
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform, transform_bounds

from flood_model.gis import open_dem, resolve_dem_path
from flood_model.paths import PACKAGE_DIR
from flood_model.urls import public_path

TILE_CACHE_ROOT = PACKAGE_DIR / "tiles" / "cache"


def _downsample_shape(src, max_size: int) -> tuple[int, int]:
    max_size = max(32, min(int(max_size), 512))
    scale = max(src.width, src.height) / float(max_size)
    out_w = max(2, int(round(src.width / max(scale, 1.0))))
    out_h = max(2, int(round(src.height / max(scale, 1.0))))
    return out_w, out_h


def _is_dem_raster(src) -> bool:
    return src.count == 1


def _raster_metadata(src) -> dict:
    bounds = src.bounds
    left, bottom, right, top = bounds.left, bounds.bottom, bounds.right, bounds.top
    if src.crs and (src.crs.to_epsg() == 4326 or src.crs.is_geographic):
        center_lon = (left + right) / 2
        center_lat = (bottom + top) / 2
    elif src.crs:
        transformed = transform(src.crs, "EPSG:4326", [left, right], [bottom, top])
        lon, lat = transformed[0], transformed[1]
        center_lon = sum(lon) / 2
        center_lat = sum(lat) / 2
    else:
        raise RuntimeError("Raster CRS khong xac dinh")

    bounds_wgs84 = None
    try:
        if src.crs:
            w, s, e, n = transform_bounds(
                src.crs, "EPSG:4326", left, bottom, right, top, densify_pts=5
            )
            bounds_wgs84 = {"west": w, "south": s, "east": e, "north": n}
    except Exception:
        bounds_wgs84 = None

    return {
        "crs": str(src.crs),
        "bounds": {"left": left, "bottom": bottom, "right": right, "top": top},
        "bounds_wgs84": bounds_wgs84,
        "center": [center_lat, center_lon],
        "band_count": src.count,
        "is_dem": _is_dem_raster(src),
        "width": src.width,
        "height": src.height,
    }


def create_dem3d_blueprint(
    name: str = "dem3d",
) -> Blueprint:
    bp = Blueprint(name, __name__)

    def _fail(message: object, status: int = 500):
        return jsonify({"success": False, "ok": False, "message": str(message), "error": str(message)}), status

    @bp.route("/api/geotiff/dem/file/<file_id>/terrain", methods=["GET"])
    def api_terrain(file_id: str):
        try:
            raster_path = resolve_dem_path(file_id)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)

        max_size = request.args.get("max_size", default=256, type=int)
        try:
            with open_dem(raster_path) as src:
                out_w, out_h = _downsample_shape(src, max_size)
                is_dem = _is_dem_raster(src)
                elev = src.read(
                    1,
                    out_shape=(out_h, out_w),
                    resampling=Resampling.bilinear,
                ).astype(np.float32)

                nodata = src.nodata
                mask = np.ones(elev.shape, dtype=bool)
                if nodata is not None:
                    mask &= elev != nodata
                mask &= np.isfinite(elev)
                try:
                    if src.count >= 4:
                        alpha = src.read(
                            4,
                            out_shape=(out_h, out_w),
                            resampling=Resampling.nearest,
                        )
                        mask &= alpha > 0
                    elif src.nodata is None and hasattr(src, "dataset_mask"):
                        dm = src.dataset_mask(
                            out_shape=(out_h, out_w),
                            resampling=Resampling.nearest,
                        )
                        mask &= dm > 0
                except Exception:
                    pass

                if mask.any():
                    zmin = float(np.min(elev[mask]))
                    zmax = float(np.max(elev[mask]))
                else:
                    zmin, zmax = 0.0, 1.0
                elev_out = elev.copy()
                elev_out[~mask] = zmin
                meta = _raster_metadata(src)
                payload = {
                    "success": True,
                    "file_id": file_id,
                    "is_dem": is_dem,
                    "width": out_w,
                    "height": out_h,
                    "zmin": zmin,
                    "zmax": zmax,
                    "nodata": nodata,
                    "bounds": meta.get("bounds"),
                    "bounds_wgs84": meta.get("bounds_wgs84"),
                    "center": meta.get("center"),
                    "crs": meta.get("crs"),
                    "band_count": src.count,
                    "elevations_b64": base64.b64encode(elev_out.astype("<f4").tobytes()).decode("ascii"),
                    "mask_b64": base64.b64encode(mask.astype(np.uint8).tobytes()).decode("ascii"),
                    "texture_url": public_path(
                        f"/api/geotiff/preview/file/{file_id}.png?max_size={max(out_w, out_h)}"
                    ),
                }
                return jsonify(payload)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi doc DEM: {exc}", 500)

    @bp.route("/api/geotiff/preview/file/<file_id>.png", methods=["GET"])
    def api_preview(file_id: str):
        try:
            raster_path = resolve_dem_path(file_id)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)

        max_size = request.args.get("max_size", default=512, type=int)
        try:
            with open_dem(raster_path) as src:
                out_w, out_h = _downsample_shape(src, max_size)
                if src.count >= 3:
                    rgb = src.read(
                        [1, 2, 3],
                        out_shape=(3, out_h, out_w),
                        resampling=Resampling.bilinear,
                    )
                    arr = np.transpose(np.clip(rgb, 0, 255), (1, 2, 0)).astype(np.uint8)
                    if src.count >= 4:
                        alpha = src.read(
                            4,
                            out_shape=(out_h, out_w),
                            resampling=Resampling.nearest,
                        )
                        rgba = np.dstack([arr, np.clip(alpha, 0, 255).astype(np.uint8)])
                        image = Image.fromarray(rgba, mode="RGBA")
                    else:
                        image = Image.fromarray(arr, mode="RGB")
                else:
                    band = src.read(
                        1,
                        out_shape=(out_h, out_w),
                        resampling=Resampling.bilinear,
                    ).astype(np.float32)
                    nodata = src.nodata
                    valid = np.isfinite(band)
                    if nodata is not None:
                        valid &= band != nodata
                    if valid.any():
                        vmin = float(np.min(band[valid]))
                        vmax = float(np.max(band[valid]))
                    else:
                        vmin, vmax = 0.0, 1.0
                    if vmax > vmin:
                        norm = (band - vmin) / (vmax - vmin)
                    else:
                        norm = np.zeros_like(band)
                    grey = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
                    image = Image.fromarray(grey, mode="L")
                buf = BytesIO()
                image.save(buf, format="PNG")
                buf.seek(0)
                return send_file(buf, mimetype="image/png", max_age=60)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi preview: {exc}", 500)

    @bp.route("/api/basemap/osm/health", methods=["GET"])
    def api_osm_health():
        try:
            from flood_model.basemap import probe_osm_connectivity

            info = probe_osm_connectivity()
            status = 200 if info.get("ok") else 503
            return jsonify({"success": bool(info.get("ok")), **info}), status
        except Exception as exc:
            traceback.print_exc()
            return _fail(str(exc), 500)

    @bp.route("/api/basemap/osm/<int:z>/<int:x>/<int:y>.png", methods=["GET"])
    def api_osm_tile(z: int, x: int, y: int):
        try:
            from flood_model.basemap import fetch_tile_png_bytes

            cache_root = TILE_CACHE_ROOT / "basemap"
            cache_root.mkdir(parents=True, exist_ok=True)
            data = fetch_tile_png_bytes(z, x, y, cache_root)
            resp = Response(data, mimetype="image/png")
            resp.headers["Cache-Control"] = "public, max-age=604800, stale-while-revalidate=86400"
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Khong tai duoc tile basemap: {exc}", 502)

    @bp.route("/api/basemap/osm/mosaic.png", methods=["GET"])
    def api_osm_mosaic():
        try:
            west = float(request.args["west"])
            south = float(request.args["south"])
            east = float(request.args["east"])
            north = float(request.args["north"])
            out_size = int(request.args.get("size", 1024))
        except (KeyError, TypeError, ValueError):
            return _fail("Thieu hoac sai tham so west,south,east,north", 400)
        if east <= west or north <= south:
            return _fail("Bbox khong hop le", 400)
        try:
            from flood_model.basemap import build_mosaic_png

            data, mw, ms, me, mn = build_mosaic_png(
                west, south, east, north, out_size, str(PACKAGE_DIR)
            )
            resp = make_response(data)
            resp.headers["Content-Type"] = "image/png"
            resp.headers["Cache-Control"] = "public, max-age=86400"
            resp.headers["X-Osm-West"] = f"{mw:.10f}"
            resp.headers["X-Osm-South"] = f"{ms:.10f}"
            resp.headers["X-Osm-East"] = f"{me:.10f}"
            resp.headers["X-Osm-North"] = f"{mn:.10f}"
            resp.headers["Access-Control-Expose-Headers"] = (
                "X-Osm-West, X-Osm-South, X-Osm-East, X-Osm-North"
            )
            return resp
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mosaic basemap: {exc}", 500)

    return bp
