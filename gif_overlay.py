"""Core image processing: overlay an image on every frame of an animated GIF.

Quality strategy
----------------
GIF frames are limited to a 256 colour palette. To avoid losing quality the
output keeps the original canvas size (unless cropped), frame count, per-frame durations, loop
count and transparency, and each frame is written with its own local palette:

1. If the composited frame fits in a palette (<= 256 colours, including a
   transparent slot when needed) it is stored *exactly* - no quantization.
2. Otherwise the colours of the original GIF outside of the overlay area are
   kept *exactly* and only the overlay area is quantized into the remaining
   palette slots (dithered against the full palette, so the overlay can also
   reuse the GIF's own colours).
3. Only when even that is impossible is the whole frame quantized (median cut
   with Floyd-Steinberg dithering).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageSequence

POSITIONS = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
    "custom",
)

# Alpha below this threshold becomes fully transparent (GIF has 1-bit alpha).
ALPHA_THRESHOLD = 128


class GifOverlayError(ValueError):
    """Raised for invalid user input (bad files, bad options, too large)."""


@dataclass
class GifInfo:
    width: int
    height: int
    frame_count: int
    durations: list[int]
    total_duration: int
    loop: int | None
    max_colors: int
    has_transparency: bool
    is_animated: bool

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "frame_count": self.frame_count,
            "durations": self.durations,
            "total_duration": self.total_duration,
            "loop": self.loop,
            "max_colors": self.max_colors,
            "has_transparency": self.has_transparency,
            "is_animated": self.is_animated,
        }


@dataclass
class OverlayOptions:
    position: str = "center"
    x: int = 0
    y: int = 0
    scale: float = 0.0  # overlay width as % of GIF width; 0 = keep original size
    opacity: float = 100.0  # 0 - 100
    margin: int = 0
    stretch_overlay: bool = False
    crop_x: int = 0
    crop_y: int = 0
    crop_width: int | None = None
    crop_height: int | None = None

    def validate(self) -> None:
        if self.position not in POSITIONS:
            raise GifOverlayError(f"Invalid position '{self.position}'.")
        if not 0 <= self.scale <= 1000:
            raise GifOverlayError("Scale must be between 0 and 1000 percent.")
        if not 0 <= self.opacity <= 100:
            raise GifOverlayError("Opacity must be between 0 and 100.")
        if self.margin < 0:
            raise GifOverlayError("Margin must be zero or positive.")


def _open_image(data: bytes, what: str) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:  # noqa: BLE001 - any decoding error is user error
        raise GifOverlayError(f"Could not read the {what} image.") from exc
    return im


def _frame_has_transparency(frame: Image.Image) -> bool:
    if "transparency" in frame.info:
        return True
    if frame.mode in ("RGBA", "LA", "PA"):
        extrema = frame.getchannel("A").getextrema()
        return extrema[0] < 255
    return False


def analyze_gif(data: bytes, max_pixels: int | None = None) -> GifInfo:
    """Inspect a GIF and return the properties needed to re-export it."""
    im = _open_image(data, "GIF")
    if im.format != "GIF":
        raise GifOverlayError("The base image must be a GIF.")

    width, height = im.size
    frame_count = getattr(im, "n_frames", 1)
    if max_pixels is not None and width * height * frame_count > max_pixels:
        raise GifOverlayError(
            "The GIF is too large to process "
            f"({width}x{height}, {frame_count} frames)."
        )

    durations: list[int] = []
    max_colors = 0
    has_transparency = False
    for frame in ImageSequence.Iterator(im):
        durations.append(int(frame.info.get("duration", 0) or 0))
        if frame.mode == "P":
            used = frame.getcolors(256)
            n = len(used) if used else 256
        else:
            used = frame.convert("RGB").getcolors(256)
            n = len(used) if used else 256
        max_colors = max(max_colors, n)
        has_transparency = has_transparency or _frame_has_transparency(frame)

    return GifInfo(
        width=width,
        height=height,
        frame_count=frame_count,
        durations=durations,
        total_duration=sum(durations),
        loop=im.info.get("loop"),
        max_colors=max_colors,
        has_transparency=has_transparency,
        is_animated=frame_count > 1,
    )


def _load_overlay_frames(
    data: bytes, max_pixels: int | None = None
) -> tuple[list[Image.Image], list[int]]:
    im = _open_image(data, "overlay")
    n_frames = getattr(im, "n_frames", 1)
    if max_pixels is not None and im.width * im.height * n_frames > max_pixels:
        raise GifOverlayError("The overlay image is too large to process.")
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(im):
        frames.append(frame.convert("RGBA"))
        durations.append(int(frame.info.get("duration", 0) or 0))
    if not frames:
        raise GifOverlayError("The overlay image has no frames.")
    return frames, durations


def _prepare_overlay(
    overlay: Image.Image, base_size: tuple[int, int], opts: OverlayOptions
) -> Image.Image:
    if opts.stretch_overlay:
        if overlay.size != base_size:
            overlay = overlay.resize(base_size, Image.Resampling.LANCZOS)
    elif opts.scale > 0:
        new_w = max(1, round(base_size[0] * opts.scale / 100))
        new_h = max(1, round(overlay.height * new_w / overlay.width))
        if (new_w, new_h) != overlay.size:
            overlay = overlay.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if opts.opacity < 100:
        alpha = overlay.getchannel("A").point(
            lambda a: round(a * opts.opacity / 100)
        )
        overlay = overlay.copy()
        overlay.putalpha(alpha)
    return overlay


def _overlay_position(
    base_size: tuple[int, int], ov_size: tuple[int, int], opts: OverlayOptions
) -> tuple[int, int]:
    if opts.stretch_overlay:
        return 0, 0
    bw, bh = base_size
    ow, oh = ov_size
    m = opts.margin
    if opts.position == "custom":
        return opts.x, opts.y
    vert, _, horiz = opts.position.partition("-")
    if opts.position in ("left", "right"):
        vert, horiz = "", opts.position
    elif opts.position in ("top", "bottom"):
        vert, horiz = opts.position, ""
    elif opts.position == "center":
        vert, horiz = "", ""
    x = {"left": m, "right": bw - ow - m}.get(horiz, (bw - ow) // 2)
    y = {"top": m, "bottom": bh - oh - m}.get(vert, (bh - oh) // 2)
    return x, y


def _palette_image(colors: list[tuple[int, int, int]]) -> Image.Image:
    """Build a P image palette; unused slots repeat the first colour."""
    padded = colors + [colors[0]] * (256 - len(colors))
    pal = Image.new("P", (1, 1))
    pal.putpalette([c for rgb in padded for c in rgb])
    return pal


def _composition_crop(
    size: tuple[int, int], opts: OverlayOptions
) -> tuple[int, int, int, int]:
    """Clamp the crop to the canvas; empty rectangles keep the full frame."""
    width, height = size
    x = max(0, min(opts.crop_x, width))
    y = max(0, min(opts.crop_y, height))
    right = width if opts.crop_width is None else min(width, x + opts.crop_width)
    bottom = height if opts.crop_height is None else min(height, y + opts.crop_height)
    if right <= x or bottom <= y:
        return 0, 0, width, height
    return x, y, right, bottom


def _keys_to_colors(keys: np.ndarray) -> list[tuple[int, int, int]]:
    return [(int(k) >> 16 & 255, int(k) >> 8 & 255, int(k) & 255) for k in keys]


def _exact_indices(keys: np.ndarray, palette_keys: np.ndarray) -> np.ndarray:
    """Map packed RGB keys to the index of the identical palette colour.

    Pillow's own palette mapping uses a reduced-precision colour cache, so it
    can confuse very similar colours; this lookup is exact.
    """
    order = np.argsort(palette_keys, kind="stable")
    sorted_keys = palette_keys[order]
    pos = np.searchsorted(sorted_keys, keys)
    pos = np.clip(pos, 0, len(sorted_keys) - 1)
    return order[pos]


def _to_paletted(
    rgba: Image.Image, overlay_box: tuple[int, int, int, int] | None
) -> Image.Image:
    """Convert a composited RGBA frame to a P frame, as losslessly as possible."""
    arr = np.asarray(rgba)
    keys = (
        (arr[..., 0].astype(np.uint32) << 16)
        | (arr[..., 1].astype(np.uint32) << 8)
        | arr[..., 2].astype(np.uint32)
    )
    transparent = arr[..., 3] < ALPHA_THRESHOLD
    has_transparency = bool(transparent.any())
    limit = 255 if has_transparency else 256

    unique = np.unique(keys[~transparent])
    if len(unique) == 0:
        unique = np.zeros(1, dtype=np.uint32)

    if len(unique) <= limit:
        # 1. Exact: no quantization needed at all.
        palette_keys = unique
        indices = _exact_indices(keys, palette_keys)
    else:
        indices = None
        if overlay_box is not None:
            # 2. Keep original GIF colours exact, quantize only the overlay area.
            left, top, right, bottom = overlay_box
            outside = ~transparent
            outside[top:bottom, left:right] = False
            base_keys = np.unique(keys[outside])
            slots = limit - len(base_keys)
            palette_keys = np.zeros(0, dtype=np.uint32)
            if slots >= 0:
                crop = rgba.convert("RGB").crop(overlay_box)
                extra = np.zeros(0, dtype=np.uint32)
                if slots > 0:
                    q = crop.quantize(
                        colors=slots,
                        method=Image.Quantize.MEDIANCUT,
                        dither=Image.Dither.NONE,
                    )
                    qpal = np.array(q.getpalette()[: 3 * slots], dtype=np.uint32)
                    qpal = qpal.reshape(-1, 3)
                    extra = (qpal[:, 0] << 16) | (qpal[:, 1] << 8) | qpal[:, 2]
                    extra = np.setdiff1d(extra, base_keys)
                palette_keys = np.concatenate([base_keys, extra])[:limit]
            if slots >= 0 and len(palette_keys) > 0:
                indices = _exact_indices(keys, palette_keys)
                # Dither the overlay area against the whole palette.
                colors = _keys_to_colors(palette_keys)
                p_crop = crop.quantize(
                    palette=_palette_image(colors),
                    dither=Image.Dither.FLOYDSTEINBERG,
                )
                crop_idx = np.asarray(p_crop).astype(np.intp)
                # Padding slots duplicate colour 0.
                crop_idx[crop_idx >= len(palette_keys)] = 0
                indices[top:bottom, left:right] = crop_idx
        if indices is None:
            # 3. Fallback: quantize the whole frame.
            q = rgba.convert("RGB").quantize(
                colors=limit,
                method=Image.Quantize.MEDIANCUT,
                dither=Image.Dither.FLOYDSTEINBERG,
            )
            qpal = np.array(q.getpalette(), dtype=np.uint32).reshape(-1, 3)[:limit]
            palette_keys = (qpal[:, 0] << 16) | (qpal[:, 1] << 8) | qpal[:, 2]
            indices = np.asarray(q).astype(np.intp)
            indices[indices >= len(palette_keys)] = 0

    n = len(palette_keys)
    flat = [c for rgb in _keys_to_colors(palette_keys) for c in rgb]
    if has_transparency:
        indices[transparent] = n
        flat += [0, 0, 0]
    p = Image.frombytes("P", rgba.size, indices.astype(np.uint8).tobytes())
    p.putpalette(flat)
    if has_transparency:
        p.info["transparency"] = n
    return p


def overlay_gif(
    gif_data: bytes,
    overlay_data: bytes,
    opts: OverlayOptions | None = None,
    max_pixels: int | None = None,
) -> tuple[bytes, GifInfo]:
    """Overlay `overlay_data` on every frame of `gif_data`.

    Returns the new GIF bytes and the analysed properties of the input GIF.
    """
    opts = opts or OverlayOptions()
    opts.validate()

    info = analyze_gif(gif_data, max_pixels=max_pixels)
    base = _open_image(gif_data, "GIF")
    base_size = base.size
    crop_box = _composition_crop(base_size, opts)

    ov_frames, ov_durations = _load_overlay_frames(overlay_data, max_pixels)
    if max_pixels is not None and (opts.stretch_overlay or opts.scale > 0):
        if opts.stretch_overlay:
            scaled_w, scaled_h = base_size
        else:
            scaled_w = base_size[0] * opts.scale / 100
            scaled_h = ov_frames[0].height * scaled_w / ov_frames[0].width
        if scaled_w * scaled_h * len(ov_frames) > max_pixels:
            raise GifOverlayError("The scaled overlay image is too large.")
    ov_frames = [_prepare_overlay(f, base_size, opts) for f in ov_frames]
    ov_total = sum(ov_durations)

    out_frames: list[Image.Image] = []
    elapsed = 0
    for index, frame in enumerate(ImageSequence.Iterator(base)):
        # Pick the overlay frame matching the current time (animated overlays).
        if len(ov_frames) == 1:
            ov = ov_frames[0]
        elif ov_total > 0:
            t = elapsed % ov_total
            k = 0
            while k < len(ov_durations) - 1 and t >= ov_durations[k]:
                t -= ov_durations[k]
                k += 1
            ov = ov_frames[k]
        else:
            ov = ov_frames[index % len(ov_frames)]
        elapsed += info.durations[index]

        canvas = frame.convert("RGBA")
        x, y = _overlay_position(base_size, ov.size, opts)
        box = (
            max(0, x),
            max(0, y),
            min(base_size[0], x + ov.width),
            min(base_size[1], y + ov.height),
        )
        overlay_box = box if box[0] < box[2] and box[1] < box[3] else None
        if overlay_box is not None:
            canvas.alpha_composite(
                ov,
                dest=overlay_box[:2],
                source=(overlay_box[0] - x, overlay_box[1] - y),
            )
        if crop_box != (0, 0, *base_size):
            canvas = canvas.crop(crop_box)
            if overlay_box is not None:
                left, top, right, bottom = crop_box
                box = (
                    max(0, overlay_box[0] - left),
                    max(0, overlay_box[1] - top),
                    min(right, overlay_box[2]) - left,
                    min(bottom, overlay_box[3]) - top,
                )
                overlay_box = box if box[0] < box[2] and box[1] < box[3] else None
        out_frames.append(_to_paletted(canvas, overlay_box))

    save_kwargs: dict = {
        "format": "GIF",
        "save_all": True,
        "append_images": out_frames[1:],
        "duration": info.durations if len(out_frames) > 1 else info.durations[0],
        "disposal": 2 if info.has_transparency else 1,
        "optimize": False,
    }
    if info.loop is not None:
        save_kwargs["loop"] = info.loop

    out = io.BytesIO()
    out_frames[0].save(out, **save_kwargs)
    return out.getvalue(), info
