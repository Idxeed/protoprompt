"""Build the lightweight README GIF from the checked-in source frame."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def _view(
    source: Image.Image,
    *,
    size: tuple[int, int],
    zoom: float,
    focus: tuple[float, float],
) -> Image.Image:
    width, height = size
    base = source.resize(size, Image.Resampling.LANCZOS)
    scaled = base.resize(
        (round(width * zoom), round(height * zoom)),
        Image.Resampling.LANCZOS,
    )
    target_x = focus[0] * scaled.width
    target_y = focus[1] * scaled.height
    left = min(max(0, round(target_x - width / 2)), scaled.width - width)
    top = min(max(0, round(target_y - height / 2)), scaled.height - height)
    return scaled.crop((left, top, left + width, top + height))


def _ease(value: float) -> float:
    return value * value * (3.0 - 2.0 * value)


def build(source_path: Path, output_path: Path) -> None:
    source = Image.open(source_path).convert("RGB")
    size = (720, 409)
    keyframes = [
        (1.00, (0.50, 0.50)),
        (1.16, (0.56, 0.31)),
        (1.00, (0.50, 0.50)),
        (1.14, (0.56, 0.63)),
        (1.00, (0.50, 0.50)),
    ]
    frames: list[Image.Image] = []
    durations: list[int] = []
    for start, end in zip(keyframes, keyframes[1:]):
        for step in range(5):
            amount = _ease(step / 4)
            zoom = start[0] + (end[0] - start[0]) * amount
            focus = (
                start[1][0] + (end[1][0] - start[1][0]) * amount,
                start[1][1] + (end[1][1] - start[1][1]) * amount,
            )
            frame = _view(source, size=size, zoom=zoom, focus=focus)
            frames.append(frame.quantize(colors=96, method=Image.Quantize.MEDIANCUT))
            durations.append(650 if step == 4 else 80)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("docs/assets/telegram-memory.png"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/assets/telegram-memory.gif"),
    )
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
