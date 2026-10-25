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
- Opt in to **Stretch overlay to fit full GIF** to fill every frame's canvas.
  This may distort the overlay and overrides width, position and margin;
  transparency and opacity still apply. Unchecked keeps the existing sizing.
- Animated overlays (e.g. another GIF) are synchronised with the base GIF's timing.
- Crop the final composition (base + overlay) using pixel X, Y, width and height
  in Composition preview. Blank dimensions keep the remaining canvas; bounds
  are clamped, and empty/out-of-bounds crops fall back to the full frame.
- Download the result as `<name>-overlay.gif`.
- Enable **Batch mode** to upload up to 100 GIFs with one overlay and shared
  settings (including crop). The preview shows the first GIF; placement and
  scaling are applied to each file's own dimensions. Track queued, processing,
  done and error statuses, completed-file counts and overall percentage.
- Download successful batch results individually or together as
  `gifs-overlay.zip`. Filenames are sanitized and duplicates receive numeric
  suffixes. A failed file does not stop the rest of the batch.
- Preview placement before rendering, compare the generated result, and reset
  the editor to start again. Processing states and inline errors guide uploads
  and downloads; keyboard-accessible controls also work on smaller screens.

## Quality preservation

The exporter preserves canvas size (unless cropped), frame count, per-frame durations and loop
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
gunicorn --workers 1 --bind 0.0.0.0:8080 app:app
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

### Anonymous, ephemeral background jobs

The editor uploads to `POST /jobs`, receives a UUID immediately (HTTP 202),
polls `GET /jobs/<job_id>`, then downloads `GET /jobs/<job_id>/result`.
One background rendering thread consumes a FIFO queue inside the existing web
service. Rendering uses the same implementation as the synchronous `/overlay`
endpoint, which remains available for compatibility. No database, SQL, Redis,
account, session, or separate App Platform Worker component is used.

Batch clients submit multipart `POST /batches` with repeated `gifs` fields,
one `overlay`, and the same settings fields as `/jobs`. Poll
`GET /batches/<job_id>` for `files` (zero-based `file_id`, output filename,
state, progress, error and availability), `total`, `completed`, `succeeded`,
`failed`, and `percentage`. Completed counts include failures. A batch ends
as `done` when at least one file succeeds, otherwise `error`.
Download each successful file from
`GET /batches/<job_id>/files/<file_id>/result`; after the batch finishes,
`GET /batches/<job_id>/archive` includes all successful outputs.
Unavailable results return HTTP 409, and unknown/expired IDs return HTTP 404.
ZIP creation copies files in chunks to an automatically deleted temporary
archive, then streams that archive to the client without loading it into RAM.
Each batch occupies one registry slot and processes its files sequentially
on the existing worker to bound rendering memory. The batch upload limit covers
the entire batch request; the per-file cap covers every GIF and the overlay,
and the pixel limit applies separately to each GIF.
Batch results expire together 15 minutes after the last file finishes.

**Keep `--workers 1` in the Procfile and exactly one App Platform instance.**
Each process/instance has its own memory and temporary files; multiple workers
or instances cannot reliably serve each other's jobs. Do not override the run
command with a multi-worker configuration or enable horizontal autoscaling.
The 120-second Gunicorn timeout remains a web-request safeguard, not a rendering
deadline: rendering no longer holds an HTTP request open. Uploads, analysis,
and result transfers still have hosting limits. Background work does not
increase rendering speed or eliminate CPU/memory limits.

Uploads are staged on disk in private temporary directories rather than retained
as whole-request byte buffers in application memory, then deleted as soon as
rendering succeeds or fails. Multipart parsing may still use bounded buffers.
Completed results and failed-job metadata expire
**15 minutes after processing finishes**; cleanup runs every 30 seconds and
on job requests, with best-effort shutdown cleanup. There is a limit of **16
retained jobs**, including queued, processing and recently finished jobs; new
submissions receive HTTP 503 when full. The configured upload and pixel limits
still apply. Numeric/settings validation happens on submission; image decoding
and pixel-budget validation happen in the background and can yield an error job.

**No persistence or completion guarantee:** jobs and downloads may disappear on
restart, redeploy, crash, or instance replacement. Hard kills cannot guarantee
immediate file cleanup; temporary container storage is ephemeral. Keep your
source files and resubmit if a job disappears. Changing files/settings stops
browser polling and ignores stale results, but does not cancel server rendering;
abandoned jobs still finish and expire. Job UUIDs are anonymous bearer links:
anyone with a UUID can retrieve its status/result until expiry; do not share it.
No upload paths are exposed in status responses.

**Capacity planning:** disk staging does not make large batches free. Queued
uploads, in-progress renders, retained outputs and temporary ZIP archives share
the instance's ephemeral disk; concurrent jobs can exhaust it before the job
count limit is reached. Provision and monitor sufficient free disk and leave
headroom for outputs and archives, or lower the caps. Decoded frames, compositing
and encoding still consume rendering memory; a 1 GiB upload cap is not a promise
that a 1 GiB-memory instance can render every accepted image. Reverse-proxy body
limits and upload/analysis/download timeouts may reject requests below the app
caps. The app spec is configuration guidance, not evidence of a deployment or
verification of provider limits.

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
| `MAX_UPLOAD_MB` | `100`       | Single-request cap in MiB for `/analyze`, `/jobs` and legacy `/overlay`, including multipart overhead and form fields. |
| `MAX_BATCH_UPLOAD_MB` | `1024` | Batch-request cap in MiB for `/batches` (1 GiB by default), including every GIF, the shared overlay, multipart overhead and form fields. |
| `MAX_FILE_MB`   | `100`       | Per-file cap in MiB for each GIF and overlay; a file exactly at the cap is allowed if its complete request fits. |
| `MAX_PIXELS`    | `950000000` | Max `width × height × frames` of a GIF (memory cap). |
| `PORT`          | `8080`      | Port to listen on (set by App Platform).             |

Request caps are **separate from the per-file cap**: `/jobs` (and legacy `/overlay`)
must fit the GIF, overlay, options and multipart headers/boundaries into the
single-request budget. `/batches` uses the larger batch budget. `/analyze` always
uses the single-request budget, even when analyzing the first GIF in batch mode.
Two individually acceptable files can exceed it together, and files whose
combined bytes equal the cap leave no room for multipart overhead. Over-limit
requests return HTTP 413 with a JSON error. The browser checks individual file
sizes and combined file bytes, then submits FormData directly without building a
full multipart Blob to measure it. These checks cannot account for exact
multipart overhead: server validation is authoritative. The pixel cap is a
separate image-processing memory safeguard.

## API

- `POST /analyze` — form field `gif`; returns GIF properties as JSON.
- `POST /jobs` — the same form fields as `/overlay`; returns HTTP 202 with
  `job_id`, initial `state: "queued"`, timestamps, progress text, `error`,
  and `result_available`.
- `GET /jobs/<job_id>` — status (`queued`, `processing`, `done`, or `error`).
  Unknown/expired jobs return HTTP 404 with JSON `error`; expired metadata is
  removed rather than retained as a tombstone.
- `GET /jobs/<job_id>/result` — GIF attachment when done, HTTP 409 JSON status
  when pending/failed, or HTTP 404 when unknown/expired.
- `POST /overlay` — form fields `gif`, `overlay`, and optional `position`
  (`top-left`, `top`, `top-right`, `left`, `center`, `right`, `bottom-left`,
  `bottom`, `bottom-right`, `custom`), `x`, `y`, `margin`, `scale`, `opacity`,
  `stretch_overlay` (`true` to stretch to the full canvas; omitted/`false` by default);
  optional `crop_x`, `crop_y` (default `0`), `crop_width`, `crop_height` (blank or
  omitted means the remaining canvas). Crop is applied after compositing on
  every frame, matching the live preview; empty rectangles keep the full frame.
  Returns the resulting `image/gif` used for both generated preview and download.

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
- **Upload budgets:** in batch mode, select several individually valid files
  whose combined size exceeds 100 MiB but stays below 1 GiB (default caps).
  Check that single mode still enforces 100 MiB, each GIF and the overlay reject
  sizes above 100 MiB but not exactly 100 MiB, and analysis always uses 100 MiB.
  Allow space for multipart overhead; verify server HTTP 413 errors remain
  helpful near request boundaries. Repeat after switching modes and resetting.

Native file-dialog interaction and real mobile/cross-browser behavior remain
manually unverified; browser automation and viewport emulation do not replace
those checks.
