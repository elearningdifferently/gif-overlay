# gif-overlay

Easy web app to upload an animated GIF and another image (PNG, GIF, JPEG, WebP, …),
overlay that image on **every frame** of the GIF and download the result as an
animated GIF — without losing quality.

## Features

- Upload a GIF and see its properties (size, frame count, duration, colours per
  frame, transparency, loop count).
- Place the overlay at any corner/edge/centre or custom X/Y, with margin,
  scaling (width as % of the GIF) and opacity.
- Animated overlays (e.g. another GIF) are synchronised with the base GIF's timing.
- Download the result as `<name>-overlay.gif`.

## Quality preservation

The exported GIF keeps the original canvas size, frame count, per-frame
durations, loop count and transparency. Each frame gets its own palette:

1. If the frame with the overlay still fits in 256 colours it is stored
   **exactly** (no quantization at all).
2. Otherwise all pixels of the original GIF outside the overlay are kept
   **bit-for-bit identical** and only the overlay area is fitted into the
   remaining palette slots (with dithering).
3. Only if the original frame itself leaves no room is the full frame
   quantized (median cut + Floyd–Steinberg dithering).

## Run locally

```bash
pip install -r requirements-dev.txt
python app.py               # http://localhost:8080
# or, like in production:
gunicorn --bind 0.0.0.0:8080 app:app
```

Run the tests:

```bash
python -m pytest
```

## Deploy to DigitalOcean App Platform

The app is detected as a Python app (`requirements.txt`, `.python-version`) and
started via the `Procfile` (gunicorn).

- **Control panel:** *Create App* → choose this GitHub repo → it is detected
  automatically → deploy.
- **CLI:** `doctl apps create --spec .do/app.yaml` (edit `github.repo`/`branch`
  in the spec if you deploy from a fork).

Health check endpoint: `/health`.

### Configuration (environment variables)

| Variable        | Default     | Description                                          |
|-----------------|-------------|------------------------------------------------------|
| `MAX_UPLOAD_MB` | `50`        | Maximum request size in MB.                          |
| `MAX_PIXELS`    | `100000000` | Max `width × height × frames` of a GIF (memory cap). |
| `PORT`          | `8080`      | Port to listen on (set by App Platform).             |

## API

- `POST /analyze` — form field `gif`; returns GIF properties as JSON.
- `POST /overlay` — form fields `gif`, `overlay`, and optional `position`
  (`top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`,
  `bottom`, `bottom-right`, `custom`), `x`, `y`, `margin`, `scale`, `opacity`;
  returns the resulting `image/gif`.
