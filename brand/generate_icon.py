"""Generate the brand icon: a stacked bar chart.

Kept in the repo so the icon is reproducible rather than an opaque binary.
Run inside the project's container image:

    docker run --rm -v "$PWD:/w" -w /w ha-discrete-stats-test \
        python brand/generate_icon.py
"""

from PIL import Image, ImageDraw

# Three stacked segments, echoing the component's own vocabulary:
# a recorded state, a second recorded state, and a no_data gap.
SEGMENT_COLOURS = [
    (56, 132, 255, 255),   # blue
    (245, 166, 35, 255),   # amber
    (148, 163, 184, 255),  # slate — the gap band
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
    draw(256).save("brand/icon.png")
    draw(512).save("brand/icon@2x.png")
    print("wrote brand/icon.png (256) and brand/icon@2x.png (512)")
