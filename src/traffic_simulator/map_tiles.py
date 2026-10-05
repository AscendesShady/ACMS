"""OpenStreetMap's tiles behind the map, placed in the scene's metres (no Qt, standard library only).

A map background is georeferenced by one WGS84 point: the scene point `anchor` (metres, x east, y
down) sits at (lat, lon). Around it the scene is a local plane: Web Mercator metres (EPSG:3857)
scaled by cos(lat) of that point, which is true scale there and within a fraction of a percent over
a network's few kilometres.

Tiles follow the OSM Tile Usage Policy (operations.osmfoundation.org/policies/tiles): an identifying
User-Agent, at most two downloads at a time, every tile cached on disk and reused for 7 days, no
bulk downloading (only the tiles in view), and "© OpenStreetMap contributors" shown on the map.

Place names are looked up with OSM's Nominatim (`PlaceSearch`), following its Usage Policy
(operations.osmfoundation.org/policies/nominatim): the same identifying User-Agent, at most one
request a second, results cached, and only a search the user asks for (never as they type).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from math import atan, cos, degrees, exp, floor, log, log2, pi, radians, sin, tan
from pathlib import Path

EARTH_RADIUS_M = 6378137.0  # Web Mercator's sphere
MAX_LATITUDE = 85.05112878  # Web Mercator's edge
TILE_PX = 256
MAX_ZOOM = 19
MAX_TILES = 80  # in view at once; beyond, a coarser zoom
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "ACMS/0.1 (Agentic Context-Aware Mobility System; road network editor)"
ATTRIBUTION = "© OpenStreetMap contributors"
FRESH_S = 7 * 24 * 3600  # a cached tile is used this long before it is fetched again
DOWNLOADS = 2  # at a time
SEARCH_URL = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit={limit}&q={query}"
SEARCH_LIMIT = 5  # places offered for one search
SEARCH_GAP_S = 1.0  # Nominatim's policy: at most one request a second


def mercator(lat: float, lon: float) -> tuple[float, float]:
    """Web Mercator metres (x east, y north)."""
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    return EARTH_RADIUS_M * radians(lon), EARTH_RADIUS_M * log(tan(pi / 4 + radians(lat) / 2))


def wgs84(mx: float, my: float) -> tuple[float, float]:
    """(lat, lon) of Web Mercator metres."""
    return degrees(2 * atan(exp(my / EARTH_RADIUS_M)) - pi / 2), degrees(mx / EARTH_RADIUS_M)


class Frame:
    """The scene's local plane at a georeference: `anchor` (scene metres) sits at (lat, lon), and the
    map is turned `rotation` degrees clockwise on the screen about it (0: north up). Local map
    coordinates are metres east and south of the anchor before that turn."""

    def __init__(self, lat: float, lon: float, anchor_x: float = 0.0, anchor_y: float = 0.0,
                 rotation: float = 0.0) -> None:
        self.mx, self.my = mercator(lat, lon)
        self.scale = cos(radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))))  # scene metres per Mercator metre
        self.ax, self.ay, self.rotation = anchor_x, anchor_y, rotation
        self._cos, self._sin = cos(radians(rotation)), sin(radians(rotation))

    def to_scene(self, mx: float, my: float) -> tuple[float, float]:
        east, south = (mx - self.mx) * self.scale, -(my - self.my) * self.scale
        return self.ax + east * self._cos - south * self._sin, self.ay + east * self._sin + south * self._cos

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        """Metres east and south of the anchor, on the map before its turn."""
        dx, dy = x - self.ax, y - self.ay
        return dx * self._cos + dy * self._sin, -dx * self._sin + dy * self._cos

    def to_mercator(self, x: float, y: float) -> tuple[float, float]:
        east, south = self.to_local(x, y)
        return self.mx + east / self.scale, self.my - south / self.scale

    def to_wgs84(self, x: float, y: float) -> tuple[float, float]:
        return wgs84(*self.to_mercator(x, y))

    def zoom(self, pixels_per_metre: float) -> int:
        """The tile zoom whose pixels come nearest the screen's at this scale (0 to MAX_ZOOM)."""
        world_px = 2 * pi * EARTH_RADIUS_M * self.scale * pixels_per_metre  # the world's width on screen
        return max(0, min(MAX_ZOOM, round(log2(max(world_px / TILE_PX, 1.0)))))

    def tile_rect(self, z: int, x: int, y: int) -> tuple[float, float, float]:
        """(left, top, size) of tile x, y at zoom z in local map metres (the scene's, when the map
        isn't turned): drawn translated to the anchor and turned by the rotation."""
        span = 2 * pi * EARTH_RADIUS_M / 2 ** z
        mx, my = -pi * EARTH_RADIUS_M + x * span, pi * EARTH_RADIUS_M - y * span
        return (mx - self.mx) * self.scale, -(my - self.my) * self.scale, span * self.scale

    def tiles(self, left: float, top: float, right: float, bottom: float, z: int) -> tuple[int, list]:
        """The tiles covering the scene rectangle (turned with the map, its corners' bounds) at zoom
        z, coarser until there are at most MAX_TILES: (zoom used, [(x, y)])."""
        corners = [self.to_mercator(x, y) for x, y in ((left, top), (right, top), (left, bottom), (right, bottom))]
        x0, x1 = min(x for x, _ in corners), max(x for x, _ in corners)
        y0, y1 = max(y for _, y in corners), min(y for _, y in corners)  # north, south
        while True:
            span = 2 * pi * EARTH_RADIUS_M / 2 ** z
            count = 2 ** z
            first_x, last_x = floor((x0 + pi * EARTH_RADIUS_M) / span), floor((x1 + pi * EARTH_RADIUS_M) / span)
            first_y, last_y = floor((pi * EARTH_RADIUS_M - y0) / span), floor((pi * EARTH_RADIUS_M - y1) / span)
            xs = range(max(first_x, 0), min(last_x, count - 1) + 1)
            ys = range(max(first_y, 0), min(last_y, count - 1) + 1)
            if len(xs) * len(ys) <= MAX_TILES or z == 0:
                return z, [(x, y) for y in ys for x in xs]
            z -= 1


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


class TileStore:
    """Tiles on disk (`folder`/z/x/y.png), fetched at most DOWNLOADS at a time when missing or older
    than FRESH_S; `ready(z, x, y)` is called from the download thread when one arrives. `download`
    (url -> bytes) stands in for the network in tests."""

    def __init__(self, folder: Path, ready=None, download=_download) -> None:
        self.folder, self.ready, self.download = Path(folder), ready or (lambda z, x, y: None), download
        self._pool = ThreadPoolExecutor(max_workers=DOWNLOADS, thread_name_prefix="osm tiles")
        self._asked: set[tuple[int, int, int]] = set()
        self._failed: dict[tuple[int, int, int], float] = {}  # tile -> when, so a bad one isn't asked every frame
        self._lock = threading.Lock()

    def path(self, z: int, x: int, y: int) -> Path:
        return self.folder / str(z) / str(x) / f"{y}.png"

    def get(self, z: int, x: int, y: int) -> bytes | None:
        """The tile's image if it is on disk (fetching a newer one in the background when it is
        stale), else None and a download starts."""
        path = self.path(z, x, y)
        data = None
        try:
            data = path.read_bytes()
            if time.time() - path.stat().st_mtime < FRESH_S:
                return data
        except OSError:
            pass
        self._ask(z, x, y)
        return data

    def _ask(self, z: int, x: int, y: int) -> None:
        key = (z, x, y)
        with self._lock:
            if key in self._asked or time.time() - self._failed.get(key, -1e9) < 60:
                return
            self._asked.add(key)
        self._pool.submit(self._fetch, key)

    def _fetch(self, key: tuple[int, int, int]) -> None:
        z, x, y = key
        try:
            data = self.download(TILE_URL.format(z=z, x=x, y=y))
            path = self.path(z, x, y)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.ready(z, x, y)
        except Exception:  # offline, refused, a full disk: tried again in a minute
            with self._lock:
                self._failed[key] = time.time()
        finally:
            with self._lock:
                self._asked.discard(key)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class PlaceSearch:
    """Place names to WGS84 points with OSM's Nominatim, one search at a time on its own thread.
    `ready(query, found)` is called from that thread: `found` is a list of (name, lat, lon), best
    first (places beyond Web Mercator's ±85° left out), or a message saying why it failed. A query
    already answered comes from the cache at once. `download` (url -> bytes) stands in for the
    network in tests."""

    def __init__(self, ready=None, download=_download) -> None:
        self.ready, self.download = ready or (lambda query, found: None), download
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="osm search")
        self._found: dict[str, list[tuple[str, float, float]]] = {}
        self._last = -SEARCH_GAP_S  # when the last request went, time.monotonic()

    def search(self, query: str) -> bool:
        """Look a place up; False for an empty query."""
        query = " ".join(query.split())
        if not query:
            return False
        if query.lower() in self._found:
            self.ready(query, self._found[query.lower()])
        else:
            self._pool.submit(self._fetch, query)
        return True

    def _fetch(self, query: str) -> None:
        time.sleep(max(0.0, self._last + SEARCH_GAP_S - time.monotonic()))  # one thread: requests queue up here
        self._last = time.monotonic()
        try:
            places = json.loads(self.download(SEARCH_URL.format(limit=SEARCH_LIMIT, query=urllib.parse.quote(query))))
            found = [(str(place["display_name"]), float(place["lat"]), float(place["lon"])) for place in places]
        except OSError as exc:
            self.ready(query, f"Couldn't reach the place search ({exc}).")
            return
        except (ValueError, KeyError, TypeError):
            self.ready(query, "The place search sent an answer that couldn't be read.")
            return
        found = [place for place in found if abs(place[1]) <= MAX_LATITUDE and abs(place[2]) <= 180][:SEARCH_LIMIT]
        self._found[query.lower()] = found
        self.ready(query, found)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
