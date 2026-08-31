"""Generate the brand icon: a stacked bar chart.

Home Assistant serves a custom integration's brand assets from
`<integration>/brand/` (see homeassistant/components/brands), so this
directory must stay inside the integration, not at the repository root.

Kept in the repo so the icon is reproducible rather than an opaque binary.
Run inside the project's container image:

    docker run --rm -v "$PWD:/w" -w /w ha-discrete-stats-test \
        python custom_components/discrete_statistics/brand/generate_icon.py
"""

import pathlib

from PIL import Image, ImageDraw

# Three stacked segments, echoing the component's own vocabulary:
# a recorded state, a second recorded state, and a no_data gap.
SEGMENT_COLOURS = [
    (56, 132, 255, 255),   # blue
    (34, 168, 108, 255),   # green
    (146, 96, 214, 255),   # purple
]

# (bottom segment, middle, top) heights per bar, in a 256px grid.
BARS = [
    (70, 54, 32),
    (112, 46, 22),
    (48, 84, 56),
]

SIZE = 256
PADDING = 26
BASELINE = SIZE - 28
BAR_WIDTH = 52
GAP = 25
RADIUS = 9


def draw(size: int) -> Image.Image:
    scale = size / SIZE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)

    def s(value: float) -> float:
        return value * scale

    x = PADDING
    for segments in BARS:
        y = BASELINE
        for index, height in enumerate(segments):
            top = y - height
            # Round only the topmost segment, so the stack reads as one bar.
            if index == len(segments) - 1:
                canvas.rounded_rectangle(
                    [s(x), s(top), s(x + BAR_WIDTH), s(y)],
                    radius=s(RADIUS),
                    fill=SEGMENT_COLOURS[index],
                )
                # Square off the bottom of the rounded cap so it meets the
                # segment below without a notch.
                canvas.rectangle(
                    [s(x), s(top + RADIUS), s(x + BAR_WIDTH), s(y)],
                    fill=SEGMENT_COLOURS[index],
                )
            else:
                canvas.rectangle(
                    [s(x), s(top), s(x + BAR_WIDTH), s(y)],
                    fill=SEGMENT_COLOURS[index],
                )
            y = top
        x += BAR_WIDTH + GAP

    return image


if __name__ == "__main__":
    here = pathlib.Path(__file__).parent
    draw(256).save(here / "icon.png")
    draw(512).save(here / "icon@2x.png")
    print(f"wrote {here}/icon.png (256) and {here}/icon@2x.png (512)")
