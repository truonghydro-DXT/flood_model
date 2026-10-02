"""
Basemap OSM Standard cho DEM 3D.

Ưu tiên https://tile.openstreetmap.org — tương thích Windows Server 2019:
TLS 1.2+, proxy env, certifi (nếu có), retry, mirror dự phòng.
"""
from __future__ import annotations

import hashlib
import math
import os
import ssl
import time
import threading
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from PIL import Image

_USER_AGENT = os.environ.get(
    'BASEMAP_USER_AGENT',
    'geotiff-viewer/1.0 (Windows Server GIS; local intranet use; '
    'https://www.openstreetmap.org/copyright)',
)

# Chi OSM / mirror cong cong — khong dung Carto (doi API key).
_TILE_URLS = (
    'https://tile.openstreetmap.de/{z}/{x}/{y}.png',
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    'https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',
)

_TILE_CACHE_TTL_SEC = int(os.environ.get('BASEMAP_TILE_TTL', str(7 * 24 * 3600)))
_MOSAIC_CACHE_TTL_SEC = int(os.environ.get('BASEMAP_MOSAIC_TTL', str(3600)))
_CACHE_VERSION = 'osm-free-v4'
_MAX_TILES = int(os.environ.get('BASEMAP_MAX_TILES', '36'))
_HTTP_TIMEOUT = float(os.environ.get('BASEMAP_HTTP_TIMEOUT', '6'))
# BASEMAP_SSL_VERIFY=0 nếu corporate SSL inspection làm hỏng cert chain
_SSL_VERIFY = os.environ.get('BASEMAP_SSL_VERIFY', '1') != '0'
_MEM_CACHE_MAX = int(os.environ.get('BASEMAP_MEM_CACHE', '384'))

_TILE_LOCK = threading.Lock()
_GREY = (230, 230, 230)
_SSL_CONTEXT = None
_SSL_CONTEXT_LOCK = threading.Lock()
_HTTP_OPENER = None
_PREFERRED_TMPL = None
_PREFERRED_LOCK = threading.Lock()
_MEM_CACHE: dict[tuple, bytes] = {}
_MEM_ORDER: list[tuple] = []
_MEM_LOCK = threading.Lock()
_INFLIGHT: dict[tuple, threading.Event] = {}
_INFLIGHT_DATA: dict[tuple, bytes] = {}
_INFLIGHT_LOCK = threading.Lock()
_GREY_PNG = b''


def _cache_root(base_dir: str) -> Path:
    root = Path(os.environ.get('BASEMAP_CACHE_ROOT', ''))
    if not str(root):
        root = Path(base_dir) / 'tiles' / 'cache' / 'basemap'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _is_fresh(path: Path, ttl_sec: int) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    return (time.time() - path.stat().st_mtime) <= ttl_sec


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context ổn định trên Windows Server (TLS1.2+, CA bundle)."""
    if not _SSL_VERIFY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ctx = ssl.create_default_context()
    # Ép TLS 1.2+ (Win Server 2019 / Schannel đôi khi mặc định yếu)
    if hasattr(ssl, 'TLSVersion'):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    else:
        ctx.options |= getattr(ssl, 'OP_NO_SSLv2', 0)
        ctx.options |= getattr(ssl, 'OP_NO_SSLv3', 0)
        ctx.options |= getattr(ssl, 'OP_NO_TLSv1', 0)
        ctx.options |= getattr(ssl, 'OP_NO_TLSv1_1', 0)

    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass

    return ctx


def _get_ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = _build_ssl_context()
        return _SSL_CONTEXT


def lonlat_to_tile_xy(lon: float, lat: float, z: int):
    n = 2.0 ** z
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_xy_to_lonlat_bounds(z: int, x: int, y: int):
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0

    def tile_y_to_lat(ty):
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n))))

    north = tile_y_to_lat(y)
    south = tile_y_to_lat(y + 1)
    return west, south, east, north


def _tile_cache_path(cache_root: Path, z: int, x: int, y: int, *, create: bool = False) -> Path:
    d = cache_root / 'tiles' / _CACHE_VERSION / 'osm' / str(z) / str(x)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / f'{y}.png'


def _grey_png() -> bytes:
    global _GREY_PNG
    if _GREY_PNG:
        return _GREY_PNG
    buf = BytesIO()
    Image.new('RGB', (256, 256), _GREY).save(buf, format='PNG', optimize=False)
    _GREY_PNG = buf.getvalue()
    return _GREY_PNG


def _looks_like_png(data: bytes | None) -> bool:
    return bool(data) and len(data) > 80 and data[:8] == b'\x89PNG\r\n\x1a\n'


def _mem_get(key: tuple) -> bytes | None:
    with _MEM_LOCK:
        data = _MEM_CACHE.get(key)
        if data is None:
            return None
        try:
            _MEM_ORDER.remove(key)
        except ValueError:
            pass
        _MEM_ORDER.append(key)
        return data


def _mem_put(key: tuple, data: bytes) -> None:
    if not data:
        return
    with _MEM_LOCK:
        if key not in _MEM_CACHE:
            _MEM_ORDER.append(key)
        _MEM_CACHE[key] = data
        while len(_MEM_ORDER) > _MEM_CACHE_MAX:
            old = _MEM_ORDER.pop(0)
            _MEM_CACHE.pop(old, None)


def _http_opener() -> urllib.request.OpenerDirector:
    global _HTTP_OPENER
    if _HTTP_OPENER is None:
        _HTTP_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=_get_ssl_context()),
            urllib.request.HTTPHandler(),
        )
    return _HTTP_OPENER


def _http_get(url: str, timeout: float = None) -> bytes:
    timeout = _HTTP_TIMEOUT if timeout is None else timeout
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': _USER_AGENT,
            'Accept': 'image/png,image/*;q=0.8,*/*;q=0.5',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        method='GET',
    )
    with _http_opener().open(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_with_retry(url: str, attempts: int = 2) -> bytes:
    last_err = None
    for i in range(attempts):
        try:
            return _http_get(url)
        except Exception as e:
            last_err = e
            if i + 1 < attempts:
                time.sleep(0.15 * (i + 1))
    raise last_err


def _download_tile_bytes(z: int, x: int, y: int) -> bytes | None:
    """Tai tile: dung nguon dang tot, neu fail thi thu mirror khac."""
    global _PREFERRED_TMPL
    with _PREFERRED_LOCK:
        preferred = _PREFERRED_TMPL
    urls = list(_TILE_URLS)
    if preferred in urls:
        urls.remove(preferred)
        urls.insert(0, preferred)
    last_err = None
    for tmpl in urls:
        url = tmpl.format(z=z, x=x, y=y)
        try:
            data = _http_get_with_retry(url, attempts=1 if tmpl == urls[0] else 2)
            if _looks_like_png(data):
                with _PREFERRED_LOCK:
                    _PREFERRED_TMPL = tmpl
                return data
        except Exception as e:
            last_err = e
            with _PREFERRED_LOCK:
                if _PREFERRED_TMPL == tmpl:
                    _PREFERRED_TMPL = None
            continue
    if last_err is not None:
        traceback.print_exc()
    return None


def probe_osm_connectivity() -> dict:
    """Kiểm tra kết nối OSM (dùng chẩn đoán trên Win Server)."""
    result = {
        'ok': False,
        'ssl_verify': _SSL_VERIFY,
        'tried': [],
        'working_url': None,
        'error': None,
    }
    # Một tile Hà Nội z=10
    z, x, y = 10, 820, 470
    for tmpl in _TILE_URLS:
        url = tmpl.format(z=z, x=x, y=y)
        entry = {'url': url, 'ok': False, 'bytes': 0, 'error': None}
        try:
            data = _http_get_with_retry(url, attempts=2)
            entry['ok'] = True
            entry['bytes'] = len(data)
            result['tried'].append(entry)
            result['ok'] = True
            result['working_url'] = url
            return result
        except Exception as e:
            entry['error'] = f'{type(e).__name__}: {e}'
            result['tried'].append(entry)
            result['error'] = entry['error']
    return result


def _cache_key(cache_root: Path, z: int, x: int, y: int) -> tuple:
    return (str(cache_root), z, x, y)


def _write_tile_cache(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with _TILE_LOCK:
            tmp.write_bytes(data)
            tmp.replace(path)
    except OSError:
        pass


def fetch_tile_png_bytes(z: int, x: int, y: int, cache_root: Path) -> bytes:
    """PNG goc: RAM → dia → HTTP. Khong decode PIL tren duong 2D."""
    n = 2 ** max(z, 0)
    if z < 0 or z > 19 or x < 0 or x >= n or y < 0 or y >= n:
        return _grey_png()

    key = _cache_key(cache_root, z, x, y)
    hit = _mem_get(key)
    if hit:
        return hit

    path = _tile_cache_path(cache_root, z, x, y, create=False)
    if _is_fresh(path, _TILE_CACHE_TTL_SEC):
        try:
            data = path.read_bytes()
            if _looks_like_png(data):
                _mem_put(key, data)
                return data
        except OSError:
            pass

    with _INFLIGHT_LOCK:
        wait = _INFLIGHT.get(key)
        owner = wait is None
        if owner:
            wait = threading.Event()
            _INFLIGHT[key] = wait
    if not owner:
        wait.wait(timeout=12)
        return _mem_get(key) or _INFLIGHT_DATA.get(key) or _grey_png()

    try:
        data = _download_tile_bytes(z, x, y)
        if not _looks_like_png(data):
            data = _grey_png()
        else:
            _write_tile_cache(path, data)
            _mem_put(key, data)
        _INFLIGHT_DATA[key] = data
        return data
    finally:
        wait.set()
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
            _INFLIGHT_DATA.pop(key, None)


def _load_native_tile(z: int, x: int, y: int, cache_root: Path) -> Image.Image | None:
    """Tải tile OSM gốc (z≤19): cache → HTTP → None."""
    n = 2 ** z
    if z < 0 or z > 19 or x < 0 or x >= n or y < 0 or y >= n:
        return None
    data = fetch_tile_png_bytes(z, x, y, cache_root)
    try:
        with Image.open(BytesIO(data)) as im:
            return im.convert('RGB')
    except OSError:
        return None


def fetch_tile_image(z: int, x: int, y: int, cache_root: Path) -> Image.Image:
    """Tải tile: cache → OSM.org → mirror → ô xám. Overzoom z 20–22 từ tile z=19."""
    grey = Image.new('RGB', (256, 256), _GREY)
    if z < 0 or z > 22:
        return grey

    if z > 19:
        scale = 2 ** (z - 19)
        native_x = x // scale
        native_y = y // scale
        local_x = x % scale
        local_y = y % scale
        parent = _load_native_tile(19, native_x, native_y, cache_root)
        if parent is None:
            return grey
        cell = 256 / scale
        left = int(round(local_x * cell))
        top = int(round(local_y * cell))
        right = int(round((local_x + 1) * cell))
        bottom = int(round((local_y + 1) * cell))
        left = max(0, min(left, 255))
        top = max(0, min(top, 255))
        right = max(left + 1, min(right, 256))
        bottom = max(top + 1, min(bottom, 256))
        crop = parent.crop((left, top, right, bottom))
        resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.BICUBIC)
        return crop.resize((256, 256), resample)

    data = fetch_tile_png_bytes(z, x, y, cache_root)
    try:
        with Image.open(BytesIO(data)) as im:
            return im.convert('RGB')
    except OSError:
        return grey


def choose_tile_range(west, south, east, north, max_tiles=_MAX_TILES):
    for z in range(18, 5, -1):
        fx0, fy0 = lonlat_to_tile_xy(west, north, z)
        fx1, fy1 = lonlat_to_tile_xy(east, south, z)
        tx0, ty0 = int(math.floor(fx0)), int(math.floor(fy0))
        tx1, ty1 = int(math.floor(fx1)), int(math.floor(fy1))
        cols = tx1 - tx0 + 1
        rows = ty1 - ty0 + 1
        if cols > 0 and rows > 0 and cols * rows <= max_tiles:
            return z, tx0, ty0, tx1, ty1
    fx0, fy0 = lonlat_to_tile_xy(west, north, 8)
    fx1, fy1 = lonlat_to_tile_xy(east, south, 8)
    return (
        8,
        int(math.floor(fx0)),
        int(math.floor(fy0)),
        int(math.floor(fx1)),
        int(math.floor(fy1)),
    )


def _mosaic_cache_key(west, south, east, north, out_size, z, x0, y0, x1, y1) -> str:
    raw = (
        f'{west:.6f},{south:.6f},{east:.6f},{north:.6f}|'
        f'{out_size}|{z}|{x0},{y0},{x1},{y1}|{_CACHE_VERSION}'
    )
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]


def build_mosaic_png(west, south, east, north, out_size, base_dir: str):
    """Trả (png_bytes, mosaic_west, mosaic_south, mosaic_east, mosaic_north)."""
    cache_root = _cache_root(base_dir)
    out_size = max(256, min(int(out_size), 2048))

    z, x0, y0, x1, y1 = choose_tile_range(west, south, east, north, max_tiles=_MAX_TILES)
    key = _mosaic_cache_key(west, south, east, north, out_size, z, x0, y0, x1, y1)
    png_path = cache_root / 'mosaic' / _CACHE_VERSION / f'{key}.png'
    png_path.parent.mkdir(parents=True, exist_ok=True)

    mosaic_west, _, _, mosaic_north = tile_xy_to_lonlat_bounds(z, x0, y0)
    _, mosaic_south, mosaic_east, _ = tile_xy_to_lonlat_bounds(z, x1, y1)

    if _is_fresh(png_path, _MOSAIC_CACHE_TTL_SEC):
        return png_path.read_bytes(), mosaic_west, mosaic_south, mosaic_east, mosaic_north

    tile_size = 256
    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    mosaic = Image.new('RGB', (cols * tile_size, rows * tile_size), _GREY)

    coords = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]
    results = {}
    # Win Server: ít worker hơn để tránh firewall/rate-limit
    workers = min(3, max(1, len(coords)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_tile_image, z, tx, ty, cache_root): (tx, ty)
            for tx, ty in coords
        }
        for fut in as_completed(futures):
            tx, ty = futures[fut]
            try:
                results[(tx, ty)] = fut.result()
            except Exception:
                traceback.print_exc()
                results[(tx, ty)] = Image.new('RGB', (tile_size, tile_size), _GREY)

    for (tx, ty), tile in results.items():
        mosaic.paste(tile, ((tx - x0) * tile_size, (ty - y0) * tile_size))

    cw, ch = mosaic.size
    if max(cw, ch) > out_size:
        resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', None)
        if resample is None:
            resample = getattr(Image, 'LANCZOS', Image.BICUBIC)
        if cw >= ch:
            nh = max(1, int(round(ch * (out_size / cw))))
            mosaic = mosaic.resize((out_size, nh), resample)
        else:
            nw = max(1, int(round(cw * (out_size / ch))))
            mosaic = mosaic.resize((nw, out_size), resample)

    buf = BytesIO()
    mosaic.save(buf, format='PNG', optimize=False)
    data = buf.getvalue()
    try:
        png_path.write_bytes(data)
    except OSError:
        pass

    return data, mosaic_west, mosaic_south, mosaic_east, mosaic_north
