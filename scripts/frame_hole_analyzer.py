#!/usr/bin/env python3
"""Offline hole/slot detection for PNGTemplates and PolaroidFrame assets.

Ports the core rules from iOS ``SlotRegionAnalyzer`` / ``MultiHoleAnalyzer`` so
GitHub Actions can bake ``slots`` into OTA manifests and the app can skip runtime scans.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

DESIGN_SIZE = 500.0
ALPHA_THRESHOLD = 0.30
MAX_ANALYSIS_LONG_SIDE = 512
MIN_AREA_FRACTION = 0.005
HOLE_EXPANSION_PIXELS = 3
SLOT_GRAY_HEX = 0xCCCCCC
SLOT_GRAY_TOLERANCE = 14.0
CHROMA_KEY_RGB = (0.0, 1.0, 0.0)
CHROMA_TOLERANCE = 0.06

DEFAULT_CUE_PALETTE: list[dict[str, Any]] = [
    {"hex": "FF00FF", "tolerance": 14, "shape": "organic"},
    {"hex": "00FFFF", "tolerance": 14, "shape": "organic"},
    {"hex": "FFFF00", "tolerance": 14, "shape": "organic"},
    {"hex": "FF8000", "tolerance": 14, "shape": "organic"},
    {"hex": "FF0000", "tolerance": 14, "shape": "organic"},
    {"hex": "0000FF", "tolerance": 14, "shape": "organic"},
]

DEFAULT_GREY_RANGE = {
    "minLuminance": 160,
    "maxLuminance": 230,
    "maxChannelSpread": 18,
    "shape": "organic",
}


@dataclass
class AnalyzeConfig:
    detection_mode: str = "auto"  # auto | transparency | colorCue
    ignore_edge_touching_holes: bool = False
    min_area_fraction: float = MIN_AREA_FRACTION
    max_analysis_long_side: int = MAX_ANALYSIS_LONG_SIDE
    default_shape: str = "organic"  # rect | organic
    slot_cues: list[dict[str, Any]] = field(default_factory=list)
    grey_range: dict[str, Any] | None = None
    hole_expansion_pixels: int = HOLE_EXPANSION_PIXELS


def _parse_hex(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


def _pixel_matches_hex(r: float, g: float, b: float, hex_val: int, tolerance: float) -> bool:
    tr = (hex_val >> 16) & 0xFF
    tg = (hex_val >> 8) & 0xFF
    tb = hex_val & 0xFF
    return abs(r - tr) <= tolerance and abs(g - tg) <= tolerance and abs(b - tb) <= tolerance


def _load_rgba(path: Path, max_long_side: int) -> tuple[Any, int, int] | None:
    if Image is None:
        return None
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        return None
    w0, h0 = img.size
    if w0 < 3 or h0 < 3:
        return None
    scale = min(1.0, max_long_side / max(w0, h0))
    w = max(2, int(round(w0 * scale)))
    h = max(2, int(round(h0 * scale)))
    if (w, h) != (w0, h0):
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    return img, w, h


def _build_transparency_mask(pixels, w: int, h: int) -> list[bool]:
    count = w * h
    mask = [False] * count
    for i in range(count):
        o = i * 4
        r, g, b, a = float(pixels[o]), float(pixels[o + 1]), float(pixels[o + 2]), float(pixels[o + 3]) / 255.0
        is_transparent = a < ALPHA_THRESHOLD
        is_slot_gray = _pixel_matches_hex(r, g, b, SLOT_GRAY_HEX, SLOT_GRAY_TOLERANCE)
        mask[i] = is_transparent or is_slot_gray
    return mask


def _build_color_cue_mask(pixels, w: int, h: int, hex_val: int, tolerance: float) -> list[bool]:
    count = w * h
    mask = [False] * count
    for i in range(count):
        o = i * 4
        r, g, b = float(pixels[o]), float(pixels[o + 1]), float(pixels[o + 2])
        mask[i] = _pixel_matches_hex(r, g, b, hex_val, tolerance)
    return mask


def _build_grey_mask(pixels, w: int, h: int, grey_range: dict[str, Any]) -> list[bool]:
    min_lum = float(grey_range.get("minLuminance", 160))
    max_lum = float(grey_range.get("maxLuminance", 230))
    max_spread = float(grey_range.get("maxChannelSpread", 18))
    count = w * h
    mask = [False] * count
    for i in range(count):
        o = i * 4
        r, g, b = float(pixels[o]), float(pixels[o + 1]), float(pixels[o + 2])
        lum = (r + g + b) / 3.0
        if lum < min_lum or lum > max_lum:
            continue
        spread = max(abs(r - g), abs(g - b), abs(r - b))
        mask[i] = spread <= max_spread
    return mask


def _build_chroma_mask(pixels, w: int, h: int) -> list[bool]:
    cr, cg, cb = (CHROMA_KEY_RGB[0] * 255, CHROMA_KEY_RGB[1] * 255, CHROMA_KEY_RGB[2] * 255)
    tol = CHROMA_TOLERANCE * 255
    count = w * h
    mask = [False] * count
    for i in range(count):
        o = i * 4
        r, g, b = float(pixels[o]), float(pixels[o + 1]), float(pixels[o + 2])
        mask[i] = abs(r - cr) <= tol and abs(g - cg) <= tol and abs(b - cb) <= tol
    return mask


@dataclass
class _Component:
    rect: tuple[int, int, int, int]  # x, y, w, h
    area: int
    indices: list[int]


def _all_components(
    mask: list[bool],
    w: int,
    h: int,
    *,
    ignore_edge_touching: bool,
    min_area: int,
) -> list[_Component]:
    count = w * h
    visited = [False] * count
    results: list[_Component] = []

    for start_y in range(h):
        for start_x in range(w):
            start_idx = start_y * w + start_x
            if not mask[start_idx] or visited[start_idx]:
                continue

            stack = [start_idx]
            min_x = max_x = start_x
            min_y = max_y = start_y
            area = 0
            touches_edge = False
            indices: list[int] = []

            while stack:
                cur = stack.pop()
                if visited[cur] or not mask[cur]:
                    continue
                visited[cur] = True
                area += 1
                indices.append(cur)
                cx = cur % w
                cy = cur // w
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                if cx == 0 or cy == 0 or cx == w - 1 or cy == h - 1:
                    touches_edge = True
                if cx > 0:
                    stack.append(cur - 1)
                if cx + 1 < w:
                    stack.append(cur + 1)
                if cy > 0:
                    stack.append(cur - w)
                if cy + 1 < h:
                    stack.append(cur + w)

            if ignore_edge_touching and touches_edge:
                continue
            if area < min_area:
                continue
            results.append(
                _Component(
                    rect=(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1),
                    area=area,
                    indices=indices,
                )
            )
    return results


def _expanded_rect(rect: tuple[int, int, int, int], pixels: int, w: int, h: int) -> tuple[int, int, int, int]:
    x, y, rw, rh = rect
    p = pixels
    nx = max(0, x - p)
    ny = max(0, y - p)
    max_x = min(w, x + rw + p)
    max_y = min(h, y + rh + p)
    return (nx, ny, max_x - nx, max_y - ny)


def _normalized_rect(rect: tuple[int, int, int, int], w: int, h: int) -> list[float]:
    x, y, rw, rh = rect
    nx = x / w
    ny = y / h
    nw = rw / w
    nh = rh / h
    ix = max(0.0, min(1.0, nx))
    iy = max(0.0, min(1.0, ny))
    iw = max(0.01, min(1.0 - ix, nw))
    ih = max(0.01, min(1.0 - iy, nh))
    return [round(ix, 6), round(iy, 6), round(iw, 6), round(ih, 6)]


def _sort_components(components: list[_Component], w: int, h: int) -> list[_Component]:
    def key_fn(c: _Component) -> tuple[float, float]:
        nr = _normalized_rect(c.rect, w, h)
        return (nr[1] + nr[3] / 2, nr[0] + nr[2] / 2)

    return sorted(components, key=key_fn)


def _trace_boundary(grid: list[bool], w: int, h: int, start: tuple[int, int]) -> list[tuple[int, int]]:
    dirs = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

    def filled(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and grid[y * w + x]

    path: list[tuple[int, int]] = []
    px, py = start
    back_dir = 4
    max_steps = w * h * 8
    steps = 0

    while True:
        path.append((px, py))
        found = False
        for k in range(8):
            d = (back_dir + 1 + k) % 8
            nx = px + dirs[d][0]
            ny = py + dirs[d][1]
            if filled(nx, ny):
                px, py = nx, ny
                back_dir = (d + 4) % 8
                found = True
                break
        if not found:
            break
        steps += 1
        if (px, py) == start and len(path) >= 3:
            break
        if steps >= max_steps:
            break
    return path


def _contour_path_data(indices: list[int], w: int, h: int) -> str | None:
    if not indices or w <= 0 or h <= 0:
        return None
    grid = [False] * (w * h)
    for idx in indices:
        grid[idx] = True

    start = None
    for y in range(h):
        for x in range(w):
            if grid[y * w + x]:
                start = (x, y)
                break
        if start:
            break
    if not start:
        return None

    boundary = _trace_boundary(grid, w, h, start)
    if len(boundary) < 3:
        return None

    max_points = 512
    step = max(1, len(boundary) // max_points)
    simplified = boundary[::step]
    if boundary[-1] != simplified[-1]:
        simplified.append(boundary[-1])
    if len(simplified) < 3:
        return None

    sx = DESIGN_SIZE / w
    sy = DESIGN_SIZE / h
    parts: list[str] = []
    for i, (px, py) in enumerate(simplified):
        x = (px + 0.5) * sx
        y = (py + 0.5) * sy
        parts.append(f"{'M' if i == 0 else 'L'} {x:.2f} {y:.2f}")
    parts.append("Z")
    return " ".join(parts)


def _components_to_manifest_slots(
    components: list[_Component],
    w: int,
    h: int,
    *,
    shape: str,
    expansion: int,
) -> list[dict[str, Any]]:
    total = w * h
    slots: list[dict[str, Any]] = []
    for idx, comp in enumerate(components):
        expanded = _expanded_rect(comp.rect, expansion, w, h)
        n_rect = _normalized_rect(expanded, w, h)
        path_data = None
        if shape == "organic":
            path_data = _contour_path_data(comp.indices, w, h)
        slot: dict[str, Any] = {
            "id": f"slot_{idx}",
            "n_rect": n_rect,
            "path_data": path_data,
        }
        slots.append(slot)
    _ = total
    return slots


def _normalized_cues(raw_cues: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not raw_cues:
        return list(DEFAULT_CUE_PALETTE)
    out: list[dict[str, Any]] = []
    for cue in raw_cues:
        hex_val = _parse_hex(cue.get("hex"))
        if hex_val is None:
            continue
        out.append({
            "hex": hex_val,
            "tolerance": float(cue.get("tolerance", 14)),
            "shape": str(cue.get("shape", "organic")).lower(),
        })
    return out or list(DEFAULT_CUE_PALETTE)


def _collect_components(pixels, w: int, h: int, config: AnalyzeConfig) -> list[_Component]:
    total_pixels = w * h
    min_area = max(1, int(total_pixels * config.min_area_fraction))
    mode = config.detection_mode.lower().replace("-", "_").replace(" ", "_")
    cues = _normalized_cues(config.slot_cues if config.slot_cues else None)
    grey = config.grey_range if config.grey_range else dict(DEFAULT_GREY_RANGE)

    def sorted_from_mask(mask: list[bool]) -> list[_Component]:
        comps = _all_components(
            mask,
            w,
            h,
            ignore_edge_touching=config.ignore_edge_touching_holes,
            min_area=min_area,
        )
        return _sort_components(comps, w, h)

    if mode in ("transparency", "alpha", "holes"):
        return sorted_from_mask(_build_transparency_mask(pixels, w, h))

    if mode in ("colorcue", "color_cue"):
        if not config.slot_cues and grey:
            grey_comps = sorted_from_mask(_build_grey_mask(pixels, w, h, grey))
            if grey_comps:
                return grey_comps
        all_comps: list[_Component] = []
        for cue in cues:
            mask = _build_color_cue_mask(pixels, w, h, int(cue["hex"]), float(cue["tolerance"]))
            all_comps.extend(sorted_from_mask(mask))
        return all_comps

    # auto
    transparent = sorted_from_mask(_build_transparency_mask(pixels, w, h))
    if transparent:
        return transparent
    if grey:
        grey_comps = sorted_from_mask(_build_grey_mask(pixels, w, h, grey))
        if grey_comps:
            return grey_comps
    color_all: list[_Component] = []
    for cue in cues:
        mask = _build_color_cue_mask(pixels, w, h, int(cue["hex"]), float(cue["tolerance"]))
        color_all.extend(sorted_from_mask(mask))
    if color_all:
        return color_all
    return sorted_from_mask(_build_chroma_mask(pixels, w, h))


def analyze_image_path(path: Path, config: AnalyzeConfig | None = None) -> list[dict[str, Any]]:
    """Return manifest ``slots`` entries for an on-disk template/frame image."""
    if config is None:
        config = AnalyzeConfig()
    loaded = _load_rgba(path, config.max_analysis_long_side)
    if loaded is None:
        return []
    img, w, h = loaded
    pixels = img.tobytes()
    components = _collect_components(pixels, w, h, config)
    if not components:
        return []

    # Use per-detection shape when color-cue; otherwise config default (rect for polaroid).
    shape = config.default_shape
    if config.detection_mode.lower() in ("colorcue", "color_cue") and config.slot_cues:
        shape = str(config.slot_cues[0].get("shape", shape)).lower()

    return _components_to_manifest_slots(
        components,
        w,
        h,
        shape=shape,
        expansion=config.hole_expansion_pixels,
    )


def png_template_config_from_sidecar(sidecar: dict[str, Any], file_ext: str) -> AnalyzeConfig:
    mode = sidecar.get("detectionMode") or sidecar.get("detection_mode")
    if not isinstance(mode, str) or not mode.strip():
        mode = "colorCue" if file_ext in ("jpg", "jpeg") else "auto"
    grey = sidecar.get("greyRange") or sidecar.get("grey_range")
    slot_cues = sidecar.get("slotCues") or sidecar.get("slot_cues")
    return AnalyzeConfig(
        detection_mode=str(mode).strip(),
        ignore_edge_touching_holes=False,
        default_shape="organic",
        slot_cues=slot_cues if isinstance(slot_cues, list) else [],
        grey_range=grey if isinstance(grey, dict) else dict(DEFAULT_GREY_RANGE),
    )


def polaroid_config() -> AnalyzeConfig:
    return AnalyzeConfig(
        detection_mode="transparency",
        ignore_edge_touching_holes=True,
        default_shape="rect",
        slot_cues=[],
        grey_range=None,
    )
