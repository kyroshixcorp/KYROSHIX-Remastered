# -*- coding: utf-8 -*-

from PIL import Image
import numpy as np

from src.pose_decoder import GRID_W, GRID_H, decode_strip

IMAGE = "vrchat_capture.png"

CANDIDATES = [
    (1444, 1829, 4),
    (1444, 1609, 4),
    (1444, 1411, 4),

    (1567, 1834, 3),
    (1567, 1614, 3),
    (1567, 1416, 3),
    (1427, 1306, 3),
    (1427, 1240, 3),
]


def sample(arr, left, top, cell, flip_rows):
    h, w = arr.shape[:2]

    if left < 0 or top < 0:
        return None

    if left + GRID_W * cell > w:
        return None

    if top + GRID_H * cell > h:
        return None

    data = bytearray()

    for row in range(GRID_H):

        if flip_rows:
            visual_row = GRID_H - 1 - row
        else:
            visual_row = row

        cy = top + visual_row * cell + cell // 2

        for col in range(GRID_W):

            cx = left + col * cell + cell // 2

            r, g, b = map(int, arr[cy, cx])

            data.extend((r, g, b))

    return bytes(data)


def marker(pixels):
    if pixels is None:
        return "NONE"

    result = []

    for row in range(2):
        for col in (32, 33):

            i = (row * GRID_W + col) * 3

            r = pixels[i]
            g = pixels[i + 1]
            b = pixels[i + 2]

            if r > 127 and g < 127:
                result.append("R")

            elif g > 127 and r < 127:
                result.append("G")

            else:
                result.append(".")

    return "".join(result)


img = np.array(
    Image.open(IMAGE).convert("RGB"),
    dtype=np.uint8,
)

print()
print("KYROSHIX BOT - Pose candidate decoder")
print("=" * 55)

for left, top, cell in CANDIDATES:

    print()
    print(
        f"Candidate grid=({left},{top}) "
        f"cell={cell}"
    )

    for flip in (True, False):

        pixels = sample(
            img,
            left,
            top,
            cell,
            flip,
        )

        m = marker(pixels)

        try:
            pose = decode_strip(pixels) if pixels else None
        except Exception as exc:
            pose = None
            print(
                f"  flip={flip:<5} "
                f"marker={m} ERROR={exc}"
            )
            continue

        if pose:

            print(
                f"  >>> VALID <<< "
                f"flip={flip} marker={m}"
            )

            print(
                f"      x={pose.x:+.3f} "
                f"y={pose.y:+.3f} "
                f"z={pose.z:+.3f} "
                f"yaw={pose.yaw:.2f}"
            )

        else:

            print(
                f"  flip={str(flip):<5} "
                f"marker={m} decode=FAIL"
            )

print()
print("=" * 55)
