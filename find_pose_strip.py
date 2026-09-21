# -*- coding: utf-8 -*-

from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image


IMAGE = Path("vrchat_capture.png")

# Cell sizes worth testing.
CELL_SIZES = [4, 3, 5, 2, 6, 7, 8, 10, 12, 16]

# Color thresholds.
MIN_CHANNEL = 120
MIN_DOMINANCE = 50

# Pose grid dimensions.
GRID_W = 34
GRID_H = 2

MAX_HITS_PER_SIZE = 30


def make_masks(img):
    """Create tolerant red/green masks from RGB image."""

    r = img[..., 0].astype(np.int16)
    g = img[..., 1].astype(np.int16)
    b = img[..., 2].astype(np.int16)

    red = (
        (r >= MIN_CHANNEL)
        & ((r - g) >= MIN_DOMINANCE)
        & ((r - b) >= MIN_DOMINANCE)
    )

    green = (
        (g >= MIN_CHANNEL)
        & ((g - r) >= MIN_DOMINANCE)
        & ((g - b) >= MIN_DOMINANCE)
    )

    return red, green


def find_pattern(red, green, cell, pattern):
    """
    Vectorized search for a 2x2 marker.

    pattern="GRRG":

        G R
        R G

    pattern="RGGR":

        R G
        G R

    Coordinates returned correspond to the top-left
    of the 2x2 marker, not the complete 34x2 grid.
    """

    h, w = red.shape

    half = cell // 2

    # Center positions of possible top-left cells.
    y0 = half
    y1 = h - cell - half

    x0 = half
    x1 = w - cell - half

    if y1 <= y0 or x1 <= x0:
        return []

    if pattern == "GRRG":

        a = green[
            y0:y1,
            x0:x1,
        ]

        b = red[
            y0:y1,
            x0 + cell:x1 + cell,
        ]

        c = red[
            y0 + cell:y1 + cell,
            x0:x1,
        ]

        d = green[
            y0 + cell:y1 + cell,
            x0 + cell:x1 + cell,
        ]

    elif pattern == "RGGR":

        a = red[
            y0:y1,
            x0:x1,
        ]

        b = green[
            y0:y1,
            x0 + cell:x1 + cell,
        ]

        c = green[
            y0 + cell:y1 + cell,
            x0:x1,
        ]

        d = red[
            y0 + cell:y1 + cell,
            x0 + cell:x1 + cell,
        ]

    else:
        raise ValueError(pattern)

    hits = a & b & c & d

    ys, xs = np.where(hits)

    if len(xs) == 0:
        return []

    # Convert sampled centers back to approximate cell top-left.
    xs = xs + x0 - half
    ys = ys + y0 - half

    return list(zip(xs.tolist(), ys.tolist()))


def validate_grid_origin(marker_x, marker_y, cell, width, height):
    """
    Marker occupies grid columns 32 and 33.

    Calculate full 34x2 grid origin.
    """

    left = marker_x - 32 * cell
    top = marker_y

    if left < 0:
        return None

    if top < 0:
        return None

    if left + GRID_W * cell > width:
        return None

    if top + GRID_H * cell > height:
        return None

    return left, top


def main():

    if not IMAGE.exists():
        print(f"ERROR: {IMAGE} not found.")
        return 1

    print()
    print("KYROSHIX BOT - Vectorized PoseStrip Finder")
    print("------------------------------------------")
    print()

    started = time.perf_counter()

    img = np.array(
        Image.open(IMAGE).convert("RGB"),
        dtype=np.uint8,
    )

    h, w, _ = img.shape

    print(f"Image: {w}x{h}")
    print("Creating RGB masks...")

    red, green = make_masks(img)

    print(f"Red pixels   : {int(red.sum())}")
    print(f"Green pixels : {int(green.sum())}")
    print()

    all_hits = set()

    # Try both vertical orientations.
    patterns = [
        ("GR/RG", "GRRG"),
        ("RG/GR", "RGGR"),
    ]

    for cell in CELL_SIZES:

        print(f"Testing cell={cell}px ...")

        for display_name, pattern in patterns:

            t0 = time.perf_counter()

            hits = find_pattern(
                red,
                green,
                cell,
                pattern,
            )

            elapsed = (
                time.perf_counter()
                - t0
            )

            valid = []

            for marker_x, marker_y in hits:

                origin = validate_grid_origin(
                    marker_x,
                    marker_y,
                    cell,
                    w,
                    h,
                )

                if origin is None:
                    continue

                left, top = origin

                key = (
                    left,
                    top,
                    cell,
                    display_name,
                )

                if key in all_hits:
                    continue

                all_hits.add(key)

                valid.append(
                    (
                        left,
                        top,
                        marker_x,
                        marker_y,
                    )
                )

                if len(valid) >= MAX_HITS_PER_SIZE:
                    break

            print(
                f"  {display_name}: "
                f"raw={len(hits)} "
                f"valid={len(valid)} "
                f"time={elapsed:.3f}s"
            )

            for (
                left,
                top,
                marker_x,
                marker_y,
            ) in valid[:10]:

                location = []

                if top > h - 300:
                    location.append("BOTTOM")

                if top < 300:
                    location.append("TOP")

                if left < 500:
                    location.append("LEFT")

                if left > w - 500:
                    location.append("RIGHT")

                loc = (
                    "+".join(location)
                    if location
                    else "CENTER"
                )

                print(
                    "    HIT"
                    f" pattern={display_name}"
                    f" cell={cell}"
                    f" grid=({left},{top})"
                    f" marker=({marker_x},{marker_y})"
                    f" region={loc}"
                )

        print()

    total_time = (
        time.perf_counter()
        - started
    )

    print("------------------------------------------")
    print(f"Unique candidates: {len(all_hits)}")
    print(f"Total time: {total_time:.3f}s")
    print()

    if not all_hits:

        print("No GR/RG or RG/GR marker candidate found.")
        print()
        print(
            "If the PoseStrip is visibly present in this image, "
            "the next step is to inspect its actual pixel geometry."
        )

        return 2

    # Rank useful candidates:
    #
    # 1. cell=4
    # 2. close to bottom
    # 3. close to left

    ranked = sorted(
        all_hits,
        key=lambda item: (
            abs(item[2] - 4),
            h - item[1],
            item[0],
        ),
    )

    print("Best candidates:")
    print()

    for (
        left,
        top,
        cell,
        pattern,
    ) in ranked[:20]:

        print(
            f"  grid=({left},{top}) "
            f"cell={cell} "
            f"pattern={pattern}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
