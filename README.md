# gif-overlay

Easy web app to upload an animated GIF and another image (PNG, GIF, JPEG, WebP, …),
overlay that image on **every frame** of the GIF and download the result as an
animated GIF, with palette-aware quality preservation.

## Features

- A responsive editor with file selection/drop zones, local image previews,
  and GIF properties (size, frame count, duration, colours per frame,
  transparency, loop count).
- Place the overlay at any corner/edge/centre or custom X/Y, with margin,
  scaling (width as % of the GIF) and opacity.
- Animated overlays (e.g. another GIF) are synchronised with the base GIF's timing.
- Download the result as `<name>-overlay.gif`.
- Preview placement before rendering, compare the generated result, and reset
  the editor to start again. Processing states and inline errors guide uploads
  and downloads; keyboard-accessible controls also work on smaller screens.

## Quality preservation

The exporter preserves canvas size, frame count, per-frame durations and loop
metadata. GIF supports at most 256 colours per frame and only binary
transparency, so **not every overlay can be exported losslessly**. Partial
alpha must become either transparent or opaque. Each frame gets its own palette:

1. When the composited colours fit, including a transparency slot if needed,
   no colour quantization is necessary.
2. Otherwise the exporter tries to retain original colours outside the overlay
   and fit the overlay into the remaining palette slots, with dithering.
3. If the available palette cannot support that strategy, full-frame
   quantization may change colours outside the overlay too.

Resizing, opacity blending, palette reduction and GIF's transparency limit can
affect appearance. Review the generated GIF rather than treating the editor's
local preview as a pixel-exact export.

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

### Troubleshooting an App Platform hostname that does not resolve

An `NXDOMAIN` response for `super-giff-app-32zqy.ondigitalocean.app` means DNS
cannot find that name. This happens **before HTTP**: requests never reach
Flask, Gunicorn, the app route, or `/health`. Changing application code cannot
repair a missing provider-assigned DNS name.

The October 18 logs show Gunicorn listening on `0.0.0.0:8080` with two workers
and no crash shown. That is evidence of a running server, not proof that the
public hostname exists or is routed correctly. Preserve the existing
`Procfile`; changing its bind address is not a DNS fix.

1. Open the DigitalOcean App Platform dashboard for the correct app. Confirm
   there is a successful **active deployment**, then copy the exact assigned
   **Live App URL** from the dashboard. Do not guess or invent a replacement
   hostname. DigitalOcean assigns the starter domain after deployment; see
   [Manage domains](https://docs.digitalocean.com/products/app-platform/how-to/manage-domains/).
2. Compare that assigned hostname with the failing name, including spelling.
   Test DNS from your computer and, if possible, another network:

   ```bash
   nslookup super-giff-app-32zqy.ondigitalocean.app
   nslookup super-giff-app-32zqy.ondigitalocean.app 1.1.1.1
   nslookup super-giff-app-32zqy.ondigitalocean.app 8.8.8.8
   ```

   If the dashboard assigns a different name, repeat these checks using that
   **exact** hostname instead. A timeout contacting a resolver is not the same
   as an authoritative `NXDOMAIN` result.
3. If public resolvers resolve the assigned name but only your device or
   network fails, investigate local/negative DNS caching, the configured
   resolver, browser secure DNS, VPN/proxy settings, and network filtering.
   Retry after clearing the relevant cache or from a different network.
4. If the exact assigned domain still fails across resolvers after a successful
   deployment, contact **DigitalOcean support** with the app/deployment IDs,
   assigned URL, timestamps, resolver outputs and deployment logs. App dashboard
   or provider access is required to establish or repair that assignment.
   Do not create DNS records for DigitalOcean's `ondigitalocean.app` domain.
5. The app spec's web service `http_port: 8080` and `/health` health check were
   reviewed and preserved. Verify the assigned deployment's public `/` ingress
   in the dashboard; no spec routing change is needed without evidence that
   routing is missing. Once DNS resolves, verify ingress forwards to HTTP port
   **8080** and uses `/health` for health
   checks. Visit the assigned Live App URL and its `/health` path; the latter
   should return `{"status":"ok"}`. HTTP routing, health-check or server errors
   can only be investigated after DNS succeeds.

### Configuration (environment variables)

| Variable        | Default     | Description                                          |
|-----------------|-------------|------------------------------------------------------|
| `MAX_UPLOAD_MB` | `50`        | Per-request combined upload cap in MiB, including multipart overhead and form fields. |
| `MAX_PIXELS`    | `100000000` | Max `width × height × frames` of a GIF (memory cap). |
| `PORT`          | `8080`      | Port to listen on (set by App Platform).             |

The upload cap is **not per file**: `/overlay` must fit the GIF, overlay,
options and multipart headers/boundaries into the same request budget.
Two individually acceptable files can exceed it together, and files whose
combined bytes equal the cap leave no room for multipart overhead. Over-limit
requests return HTTP 413 with a JSON error. The pixel cap is a separate
image-processing memory safeguard.

## API

- `POST /analyze` — form field `gif`; returns GIF properties as JSON.
- `POST /overlay` — form fields `gif`, `overlay`, and optional `position`
  (`top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`,
  `bottom`, `bottom-right`, `custom`), `x`, `y`, `margin`, `scale`, `opacity`;
  returns the resulting `image/gif`.

## Manual browser checklist

- Upload a valid animated GIF and a PNG/animated overlay; inspect metadata,
  previews, placement, opacity and scale, then render and download. Confirm the
  downloaded GIF's timing, dimensions and filename, not just the local preview.
- **Races:** rapidly replace/remove uploads while analysis or rendering is in
  flight. Change options during a render. Late responses must not restore stale
  metadata/results, enable an obsolete download or overwrite newer errors.
- **Reset:** reset with files selected, after rendering, and while a request is
  pending. Check previews, filenames, metadata, options, status and download
  state clear consistently; selecting the same file again should work.
- **Keyboard:** tab through upload controls, positioning/options, render,
  download and reset. Check visible focus, Enter/Space activation, associated
  labels, and accessible progress/error announcements without a mouse.
- **Mobile:** use a narrow viewport and touch input; check readable controls,
  usable file pickers, previews and downloads without horizontal overflow.
- **Reduced motion:** enable the operating system's reduced-motion preference
  and check that nonessential transitions/animations are suppressed.
- **Errors:** try empty/corrupt files, a non-GIF base, an invalid overlay,
  excessive combined uploads and invalid numeric options. Simulate an offline
  connection, a server failure and a non-JSON error response; show an actionable
  error, clear busy state and allow retry without stale downloads.

Native file-dialog interaction and real mobile/cross-browser behavior remain
manually unverified; browser automation and viewport emulation do not replace
those checks.
