import io

import pytest
from PIL import Image, ImageDraw, ImageSequence

from app import app
from gif_overlay import GifOverlayError, OverlayOptions, analyze_gif, overlay_gif

DURATIONS = [50, 60, 70, 80, 90]


def make_gif(transparent=False, size=(80, 60), loop=0):
    frames = []
    for i in range(len(DURATIONS)):
        im = Image.new("RGB", size, (10 * i, 100, 200))
        ImageDraw.Draw(im).rectangle([i * 5, 10, i * 5 + 20, 30], fill=(255, 0, 0))
        p = im.convert("P", palette=Image.Palette.ADAPTIVE, colors=8)
        if transparent:
            p.info["transparency"] = 0
        frames.append(p)
    kwargs = {}
    if loop is not None:
        kwargs["loop"] = loop
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=DURATIONS,
        disposal=2 if transparent else 1,
        **kwargs,
    )
    return buf.getvalue()


def make_png(size=(20, 20), color=(0, 255, 0, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def make_gradient_png(size=(64, 64)):
    w, h = size
    r = Image.linear_gradient("L").resize(size)
    g = Image.linear_gradient("L").rotate(90).resize(size)
    b = Image.new("L", size, 128)
    a = Image.new("L", size, 255)
    buf = io.BytesIO()
    Image.merge("RGBA", [r, g, b, a]).save(buf, format="PNG")
    return buf.getvalue()


def frames_of(data):
    return [f.convert("RGBA") for f in ImageSequence.Iterator(Image.open(io.BytesIO(data)))]


def pixels(im):
    return list(im.get_flattened_data()) if hasattr(im, "get_flattened_data") else list(im.getdata())


def test_analyze_gif():
    info = analyze_gif(make_gif())
    assert (info.width, info.height) == (80, 60)
    assert info.frame_count == 5
    assert info.durations == DURATIONS
    assert info.loop == 0
    assert info.is_animated
    assert not info.has_transparency
    assert 1 <= info.max_colors <= 8


def test_analyze_rejects_non_gif():
    with pytest.raises(GifOverlayError):
        analyze_gif(make_png())


def test_analyze_rejects_too_large():
    with pytest.raises(GifOverlayError):
        analyze_gif(make_gif(), max_pixels=100)


def test_overlay_is_lossless_and_keeps_timing():
    gif = make_gif()
    out, _ = overlay_gif(gif, make_png(), OverlayOptions(position="bottom-right", margin=2))
    result = Image.open(io.BytesIO(out))
    assert result.size == (80, 60)
    assert result.n_frames == 5
    assert result.info.get("loop") == 0
    assert [f.info["duration"] for f in ImageSequence.Iterator(result)] == DURATIONS

    green = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    for src, dst in zip(frames_of(gif), frames_of(out)):
        src.alpha_composite(green, (58, 38))
        assert pixels(src) == pixels(dst)


def test_play_once_gif_stays_play_once():
    out, info = overlay_gif(make_gif(loop=None), make_png())
    assert info.loop is None
    assert "loop" not in Image.open(io.BytesIO(out)).info


def test_many_color_overlay_keeps_original_pixels_exact():
    gif = make_gif()
    out, _ = overlay_gif(gif, make_gradient_png(), OverlayOptions(position="center"))
    for src, dst in zip(frames_of(gif), frames_of(out)):
        # The 64px overlay is centred on an 80px wide GIF: x 8..72 are covered.
        for x in list(range(0, 8)) + list(range(72, 80)):
            for y in range(60):
                assert src.getpixel((x, y)) == dst.getpixel((x, y))


def test_transparency_is_preserved():
    gif = make_gif(transparent=True)
    out, info = overlay_gif(gif, make_png())
    assert info.has_transparency
    green = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    for src, dst in zip(frames_of(gif), frames_of(out)):
        src.alpha_composite(green, (30, 20))
        assert [p[3] == 0 for p in pixels(src)] == [p[3] == 0 for p in pixels(dst)]
        assert [p for p in pixels(src) if p[3]] == [p for p in pixels(dst) if p[3]]


def test_scale_opacity_and_custom_position():
    gif = make_gif()
    opts = OverlayOptions(position="custom", x=4, y=6, scale=25, opacity=50)
    out, _ = overlay_gif(gif, make_png((10, 10), (255, 255, 255, 255)), opts)
    frame = frames_of(out)[0]
    src = frames_of(gif)[0]
    # 25% of 80px = 20px wide overlay at (4, 6), half transparent white.
    inside = frame.getpixel((4 + 19, 6 + 19))
    assert inside != src.getpixel((23, 25))
    assert frame.getpixel((4 + 21, 6 + 21)) == src.getpixel((25, 27))


def test_animated_overlay_follows_time():
    frames = [Image.new("RGBA", (10, 10), c) for c in [(255, 0, 0, 255), (0, 0, 255, 255)]]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=110, loop=0)
    out, _ = overlay_gif(make_gif(), buf.getvalue(), OverlayOptions(position="top-left"))
    # Base frames start at 0, 50, 110, 180, 260ms; overlay switches every 110ms.
    colors = [f.getpixel((0, 0))[:3] for f in frames_of(out)]
    assert colors == [(255, 0, 0), (255, 0, 0), (0, 0, 255), (0, 0, 255), (255, 0, 0)]


def test_invalid_options():
    with pytest.raises(GifOverlayError):
        overlay_gif(make_gif(), make_png(), OverlayOptions(position="nowhere"))
    with pytest.raises(GifOverlayError):
        overlay_gif(make_gif(), make_png(), OverlayOptions(opacity=150))


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def test_index(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"GIF Overlay" in res.data


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_analyze_endpoint(client):
    res = client.post("/analyze", data={"gif": (io.BytesIO(make_gif()), "a.gif")})
    assert res.status_code == 200
    assert res.get_json()["frame_count"] == 5


def test_overlay_endpoint(client):
    res = client.post(
        "/overlay",
        data={
            "gif": (io.BytesIO(make_gif()), "my anim.gif"),
            "overlay": (io.BytesIO(make_png()), "logo.png"),
            "position": "bottom-left",
            "margin": "3",
        },
    )
    assert res.status_code == 200
    assert res.mimetype == "image/gif"
    assert "my_anim-overlay.gif" in res.headers["Content-Disposition"]
    assert Image.open(io.BytesIO(res.data)).n_frames == 5


def test_overlay_endpoint_errors(client):
    res = client.post("/overlay", data={"gif": (io.BytesIO(make_gif()), "a.gif")})
    assert res.status_code == 400
    assert "overlay" in res.get_json()["error"]

    res = client.post(
        "/overlay",
        data={
            "gif": (io.BytesIO(b"not an image"), "a.gif"),
            "overlay": (io.BytesIO(make_png()), "logo.png"),
        },
    )
    assert res.status_code == 400

    res = client.post(
        "/overlay",
        data={
            "gif": (io.BytesIO(make_gif()), "a.gif"),
            "overlay": (io.BytesIO(make_png()), "logo.png"),
            "scale": "abc",
        },
    )
    assert res.status_code == 400


def test_full_palette_gif_keeps_original_pixels_exact():
    # A GIF already using 250 colours leaves only a few slots for the overlay.
    frames = []
    for i in range(3):
        im = Image.new("P", (130, 130))
        im.putpalette([v for c in range(250) for v in ((c + i) % 256, c, 255 - c)] + [0] * 18)
        im.putdata([(x + y) % 250 for y in range(130) for x in range(130)])
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=40, loop=0)
    gif = buf.getvalue()
    assert analyze_gif(gif).max_colors == 250

    out, _ = overlay_gif(gif, make_gradient_png((20, 20)), OverlayOptions(position="top-left"))
    for src, dst in zip(frames_of(gif), frames_of(out)):
        for x in range(20, 130):
            for y in range(130):
                assert src.getpixel((x, y)) == dst.getpixel((x, y))


def test_overlay_too_large():
    with pytest.raises(GifOverlayError):
        overlay_gif(make_gif(), make_png((300, 300)), max_pixels=50_000)
    with pytest.raises(GifOverlayError):
        overlay_gif(make_gif(), make_png(), OverlayOptions(scale=1000), max_pixels=50_000)
