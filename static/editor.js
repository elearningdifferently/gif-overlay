/* Source images are browser previews; the server owns validation and GIF rendering. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const config = JSON.parse($("editor-config").textContent);
  const form = $("form");
  const singleLimit = config.maxUploadMb * 1024 * 1024;
  const fileLimit = config.maxFileMb * 1024 * 1024;
  const layers = {
    gif: { file: null, url: null, dimensions: null },
    overlay: { file: null, url: null, dimensions: null },
  };
  let info = null;
  let revision = 0;
  let analysisController = null;
  let exportController = null;
  let resultUrl = null;
  let busy = false;
  let exportProgress = "";
  let batchFiles = [];
  let batchStatus = null;
  let batchJobUrl = null;
  const batchMode = () => $("batch-mode").checked;
  const requestMb = () => batchMode() ? config.maxBatchUploadMb : config.maxUploadMb;
  const requestLimit = () => requestMb() * 1024 * 1024;
  const settings = ["position", "x", "y", "margin", "scale", "opacity", "stretch_overlay",
    "crop_x", "crop_y", "crop_width", "crop_height"];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  $("hide-previews").defaultChecked = reducedMotion.matches;
  function updateMotion() {
    document.body.classList.toggle("previews-paused", $("hide-previews").checked);
    $("motion-note").hidden = !$("hide-previews").checked;
  }
  $("hide-previews").addEventListener("change", updateMotion);
  updateMotion();
  const bytes = (size) => size < 1024 * 1024
    ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(2)} MiB`;
  const combinedSize = () => (batchMode()
    ? batchFiles.reduce((sum, file) => sum + file.size, 0) : layers.gif.file?.size || 0)
    + (layers.overlay.file?.size || 0);

  function clearResult() {
    $("result-fig").hidden = true;
    $("result").removeAttribute("src");
    $("download").removeAttribute("href");
    $("download").removeAttribute("download");
    $("download-name").textContent = "";
    if (resultUrl) URL.revokeObjectURL(resultUrl);
    resultUrl = null;
    batchStatus = null;
    batchJobUrl = null;
  }

  function renderBatch() {
    $("batch-panel").hidden = !batchMode() || !batchFiles.length;
    $("batch-progress").value = batchStatus?.percentage || 0;
    $("batch-summary").textContent = batchStatus
      ? `${batchStatus.completed} of ${batchStatus.total} completed · ${batchStatus.percentage}% · ${batchStatus.failed} errors`
      : `${batchFiles.length} GIFs selected · Preview shows the first file.`;
    $("batch-download").hidden = !(batchStatus?.state === "done" && batchStatus.result_available);
    if (batchJobUrl) {
      $("batch-download").href = `${batchJobUrl}/archive`;
      $("batch-download").download = "gifs-overlay.zip";
    } else {
      $("batch-download").removeAttribute("href");
    }
    const rows = batchFiles.map((file, index) => {
      const item = batchStatus?.files[index];
      const row = document.createElement("li");
      const name = document.createElement("span");
      name.className = "batch-name";
      name.textContent = file.name;
      const state = document.createElement("span");
      state.textContent = item?.state || "queued";
      row.append(name, state);
      if (item?.error) {
        const error = document.createElement("p");
        error.className = "field-error";
        error.textContent = item.error;
        row.append(error);
      }
      if (item?.result_available && batchJobUrl) {
        const link = document.createElement("a");
        link.textContent = "Download GIF";
        link.href = `${batchJobUrl}/files/${index}/result`;
        link.download = item.download_name;
        row.append(link);
      }
      return row;
    });
    $("batch-files").replaceChildren(...rows);
  }

  function compositionChanged() {
    revision += 1;
    exportController?.abort();
    exportController = null;
    busy = false;
    exportProgress = "";
    clearResult();
    $("error").textContent = "";
    update();
  }

  function update() {
    const total = combinedSize();
    const ready = Boolean(layers.gif.file && layers.overlay.file && (batchMode() ? batchFiles.length : info));
    $("submit").disabled = busy || !ready || total >= requestLimit() || !settingsValid();
    $("submit").textContent = batchMode()
      ? (busy ? "Creating GIFs…" : "Create batch ↗")
      : (busy ? "Creating GIF…" : "Create GIF ↗");
    $("form").setAttribute("aria-busy", String(busy));
    $("size-summary").textContent = total
      ? `${bytes(total)} combined · ${requestMb()} MiB ${batchMode() ? "batch" : "single"} request cap, including form data and multipart overhead. Server validation is authoritative.`
      : "Files stay in this session; uploads are processed on the server.";
    if (total >= requestLimit()) {
      $("status").textContent = "Files exceed the request budget. Choose smaller files; form data also needs space.";
    } else if (busy) {
      $("status").textContent = exportProgress;
    } else if (batchStatus && ["done", "error"].includes(batchStatus.state)) {
      $("status").textContent = `Batch finished: ${batchStatus.succeeded} successful, ${batchStatus.failed} failed. Download available GIFs below the preview.`;
    } else if (resultUrl) {
      $("status").textContent = "Export complete. Preview and download your GIF below.";
    } else if (!layers.gif.file || !layers.overlay.file) {
      $("status").textContent = "Add both files to get started.";
    } else if (!info && !batchMode()) {
      $("status").textContent = "A valid base GIF analysis is required before export.";
    } else {
      $("status").textContent = settingsValid() ? "Your layers are ready. Create your GIF when it looks right." : "Check the placement and crop values before exporting.";
    }
    $("custom-controls").hidden = $("position").value !== "custom";
    renderBatch();
    renderPreview();
  }

  function settingsValid() {
    return settings.every((name) => $(name).checkValidity());
  }

  // Match Python's round-to-even for scaled dimensions, including half pixels.
  function roundEven(value) {
    const floor = Math.floor(value);
    return value - floor === 0.5 ? floor + (floor % 2) : Math.round(value);
  }

  function renderPreview() {
    const base = info || layers.gif.dimensions;
    const overlay = layers.overlay.dimensions;
    const show = Boolean(base && layers.gif.url && layers.gif.dimensions);
    $("stage").hidden = !show;
    $("preview-empty").hidden = show;
    $("overlay-preview").hidden = !show || !overlay || !settingsValid();
    if (!show) {
      $("preview-detail").textContent = "Your original GIF dimensions and frame timing are preserved on export.";
      return;
    }
    const { width: bw, height: bh } = base;
    let cx = Math.max(0, Math.min(Number($("crop_x").value), bw));
    let cy = Math.max(0, Math.min(Number($("crop_y").value), bh));
    let cw = $("crop_width").value === "" ? bw - cx : Math.min(Number($("crop_width").value), bw - cx);
    let ch = $("crop_height").value === "" ? bh - cy : Math.min(Number($("crop_height").value), bh - cy);
    if (!settingsValid() || cw <= 0 || ch <= 0) {
      cx = cy = 0;
      cw = bw;
      ch = bh;
    }
    $("stage").style.aspectRatio = `${cw} / ${ch}`;
    // Keep tall images inside the workspace while retaining source aspect ratio.
    $("stage").style.width = `min(100%, ${440 * cw / ch}px)`;
    Object.assign($("base-preview").style, {
      width: `${bw / cw * 100}%`, height: `${bh / ch * 100}%`,
      left: `${-cx / cw * 100}%`, top: `${-cy / ch * 100}%`,
    });
    const detail = `${cw} × ${ch} px output · ${bw} × ${bh} px canvas · Crop origin (${cx}, ${cy})`;
    $("preview-detail").textContent = `${detail}${overlay ? "" : " · Add an overlay to compose."}`;
    if (!overlay || !settingsValid()) return;
    const scale = Number($("scale").value);
    const stretch = $("stretch_overlay").checked;
    const ow = stretch ? bw : scale > 0 ? Math.max(1, roundEven(bw * scale / 100)) : overlay.width;
    const oh = stretch ? bh : scale > 0 ? Math.max(1, roundEven(overlay.height * ow / overlay.width)) : overlay.height;
    const margin = Math.trunc(Number($("margin").value));
    const position = $("position").value;
    let x = Math.floor((bw - ow) / 2);
    let y = Math.floor((bh - oh) / 2);
    if (stretch) {
      x = 0;
      y = 0;
    } else if (position === "custom") {
      x = Math.trunc(Number($("x").value));
      y = Math.trunc(Number($("y").value));
    } else {
      if (position.includes("left")) x = margin;
      if (position.includes("right")) x = bw - ow - margin;
      if (position.includes("top")) y = margin;
      if (position.includes("bottom")) y = bh - oh - margin;
    }
    Object.assign($("overlay-preview").style, {
      width: `${ow / cw * 100}%`, height: `${oh / ch * 100}%`,
      left: `${(x - cx) / cw * 100}%`, top: `${(y - cy) / ch * 100}%`,
      opacity: String(Number($("opacity").value || "100") / 100),
    });
    $("preview-detail").textContent = `${detail} · Overlay ${ow} × ${oh} px at (${x}, ${y})`;
  }

  async function apiError(response, jobRequest = false, uploadMb = config.maxUploadMb) {
    let message = "";
    try {
      const body = await response.json();
      if (typeof body.error === "string") message = body.error;
    } catch { /* Proxies may return HTML or an empty body. Never display it. */ }
    if (response.status === 413) {
      return `${message || "Request too large."} Each file must be at most ${config.maxFileMb} MiB; this request must fit within ${uploadMb} MiB including form data and multipart overhead. The host may impose a smaller limit.`;
    }
    if (jobRequest && response.status === 404) {
      return `${message || "This export is no longer available."} The job may have expired or the server restarted. Click Create GIF to retry.`;
    }
    return message || `The server could not complete the request (HTTP ${response.status}). Please try again.`;
  }

  function pollDelay(signal) {
    return new Promise((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        reject(new DOMException("Export interrupted.", "AbortError"));
      };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", abort);
        resolve();
      }, 1000);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    });
  }

  async function readJob(response, uploadMb) {
    if (!response.ok) throw new Error(await apiError(response, true, uploadMb));
    try {
      return await response.json();
    } catch {
      throw new Error("The server returned unreadable job information. Click Create GIF to retry.");
    }
  }

  async function analyze(file) {
    analysisController?.abort();
    const controller = new AbortController();
    analysisController = controller;
    $("info").textContent = "Reading GIF properties…";
    const data = new FormData();
    data.append("gif", file);
    try {
      if (file.size >= singleLimit) {
        throw new Error(`GIF properties require an analysis request under ${config.maxUploadMb} MiB, including multipart overhead, even in batch mode. Choose a smaller GIF to analyze it.`);
      }
      const response = await fetch(config.analyzeUrl, { method: "POST", body: data, signal: controller.signal });
      if (!response.ok) throw new Error(await apiError(response));
      let result;
      try {
        result = await response.json();
      } catch {
        throw new Error("The server returned unreadable GIF properties. Please try again.");
      }
      if (!(result.width > 0 && result.height > 0 && result.frame_count > 0)) {
        throw new Error("The server returned invalid GIF properties. Please try again.");
      }
      if (controller.signal.aborted || layers.gif.file !== file) return;
      info = result;
      const loop = result.loop === null ? "plays once" : result.loop === 0 ? "loops forever" : `loops ${result.loop}×`;
      $("info").textContent = `${result.width} × ${result.height} px · ${result.frame_count} frames · ${(result.total_duration / 1000).toFixed(2)} s · Up to ${result.max_colors} colours/frame · ${result.has_transparency ? "Transparent" : "Opaque"} · ${loop}`;
    } catch (error) {
      if (controller.signal.aborted || layers.gif.file !== file) return;
      info = null;
      $("info").textContent = "GIF analysis failed.";
      $("gif-error").textContent = error instanceof TypeError
        ? "Could not reach the server. Check your connection and choose the GIF again to retry."
        : error.message;
    } finally {
      if (analysisController === controller) {
        analysisController = null;
        update();
      }
    }
  }

  function clearLayer(name) {
    const layer = layers[name];
    if (name === "gif") {
      batchFiles = [];
      analysisController?.abort();
      analysisController = null;
      info = null;
      $("info").textContent = "GIF properties will appear here.";
    }
    for (const image of [$(name + "-thumb"), $(name === "gif" ? "base-preview" : "overlay-preview")]) {
      image.onload = null;
      image.onerror = null;
      image.removeAttribute("src");
    }
    if (layer.url) URL.revokeObjectURL(layer.url);
    layer.file = null;
    layer.url = null;
    layer.dimensions = null;
    $(name).value = "";
    $(name + "-thumb").hidden = true;
    $(name + "-remove").hidden = true;
    $(name + "-detail").textContent = "No file selected";
    $(name + "-error").textContent = "";
  }

  function chooseFile(name, files) {
    if (!files.length) return;
    clearLayer(name);
    compositionChanged();
    const batch = name === "gif" && batchMode();
    if (batch && files.length > config.maxBatchFiles) {
      $(name + "-error").textContent = `Choose at most ${config.maxBatchFiles} GIFs per batch.`;
      return;
    }
    if (!batch && files.length !== 1) {
      $(name + "-error").textContent = "Choose one file for this layer.";
      return;
    }
    const file = files[0];
    if (files.some((item) => !item.size)) {
      $(name + "-error").textContent = "This file is empty. Choose another image.";
      return;
    }
    if (files.some((item) => item.size > fileLimit)) {
      $(name + "-error").textContent = `Each ${name === "gif" ? "GIF" : "overlay"} must be at most ${config.maxFileMb} MiB. Choose smaller files.`;
      return;
    }
    // A missing or misleading MIME is common: extensions are a fallback, not proof.
    if (name === "gif" && files.some((item) => item.type !== "image/gif" && !/\.gif$/i.test(item.name))) {
      $(name + "-error").textContent = "Choose a GIF for the base layer. The server checks its contents.";
      return;
    }
    if (name === "overlay" && !file.type.startsWith("image/") && !/\.(png|gif|jpe?g|webp|bmp|tiff?|ico|avif|apng)$/i.test(file.name)) {
      $(name + "-error").textContent = "Choose an image such as PNG, GIF, JPEG or WebP. The server checks format support.";
      return;
    }
    if (files.reduce((sum, item) => sum + item.size, 0) + combinedSize() >= requestLimit()) {
      $(name + "-error").textContent = `All files must fit within the ${requestMb()} MiB ${batchMode() ? "batch" : "single"} request cap, with room for form data and multipart overhead. Choose smaller files.`;
      update();
      return;
    }
    const layer = layers[name];
    if (batch) batchFiles = files;
    layer.file = file;
    layer.url = URL.createObjectURL(file);
    const url = layer.url;
    const image = $(name + "-thumb");
    image.onload = () => {
      if (layer.url !== url) return;
      layer.dimensions = { width: image.naturalWidth, height: image.naturalHeight };
      image.hidden = false;
      update();
    };
    image.onerror = () => {
      if (layer.url !== url) return;
      image.hidden = true;
      $(name + "-error").textContent = "Browser preview unavailable. The server will validate this image on upload.";
      update();
    };
    image.src = url;
    $(name === "gif" ? "base-preview" : "overlay-preview").src = url;
    $(name + "-detail").textContent = batch
      ? `${files.length} GIFs · First: ${file.name}` : `${file.name} · ${bytes(file.size)}`;
    $(name + "-remove").hidden = false;
    if (name === "gif") analyze(file);
    update();
  }

  for (const name of Object.keys(layers)) {
    $(name).addEventListener("change", (event) => chooseFile(name, Array.from(event.target.files)));
    $(name + "-remove").addEventListener("click", () => {
      clearLayer(name);
      compositionChanged();
      $(name).focus();
    });
    const zone = $(name + "-zone");
    let dragDepth = 0;
    zone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      dragDepth += 1;
      zone.classList.add("dragging");
    });
    zone.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) zone.classList.remove("dragging");
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      dragDepth = 0;
      zone.classList.remove("dragging");
      chooseFile(name, Array.from(event.dataTransfer.files));
    });
  }
  // Even a drop outside either card must not navigate away from this editor.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (event) => event.preventDefault());
  }
  for (const name of settings) {
    $(name).addEventListener("input", () => {
      if (name === "opacity") $("opacity-range").value = $("opacity").value || "100";
      compositionChanged();
    });
  }
  $("opacity-range").addEventListener("input", () => {
    $("opacity").value = $("opacity-range").value;
    compositionChanged();
  });
  $("reset-editor").addEventListener("click", () => {
    form.reset();
    $("gif").multiple = false;
    clearLayer("gif");
    clearLayer("overlay");
    updateMotion();
    compositionChanged();
    $("gif").focus();
  });
  $("batch-mode").addEventListener("change", () => {
    $("gif").multiple = batchMode();
    clearLayer("gif");
    compositionChanged();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || (!info && !batchMode()) || !layers.gif.file || !layers.overlay.file
        || combinedSize() >= requestLimit() || !form.reportValidity()) return;
    const batch = batchMode();
    const uploadMb = requestMb();
    const jobsUrl = batch ? config.batchesUrl : config.jobsUrl;
    const data = new FormData();
    if (batch) {
      for (const file of batchFiles) data.append("gifs", file);
    } else {
      data.append("gif", layers.gif.file);
    }
    data.append("overlay", layers.overlay.file);
    for (const name of settings) {
      data.append(name, $(name).type === "checkbox" ? $(name).checked : $(name).value);
    }
    busy = true;
    exportProgress = batch ? "Uploading files to queue your batch…" : "Uploading files to queue your GIF…";
    const currentRevision = revision;
    const controller = new AbortController();
    exportController = controller;
    const stale = () => controller.signal.aborted || currentRevision !== revision || exportController !== controller;
    const fallbackName = (layers.gif.file.name.replace(/\.[^.]*$/, "") || "image") + "-overlay.gif";
    clearResult();
    $("error").textContent = "";
    update();
    try {
      // Let the browser serialize FormData; the server checks multipart overhead.
      const created = await fetch(jobsUrl, { method: "POST", body: data, signal: controller.signal });
      const job = await readJob(created, uploadMb);
      if (stale()) return;
      if (created.status !== 202 || !job || typeof job.job_id !== "string"
          || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(job.job_id)
          || job.state !== "queued") {
        throw new Error("The server returned invalid job information. Click Create GIF to retry.");
      }
      const jobUrl = `${jobsUrl.replace(/\/$/, "")}/${encodeURIComponent(job.job_id)}`;
      if (batch) {
        batchStatus = job;
        batchJobUrl = jobUrl;
      }
      exportProgress = "Queued. Waiting for the server to create your GIF…";
      update();
      while (true) {
        await pollDelay(controller.signal);
        if (stale()) return;
        const status = await readJob(await fetch(jobUrl, { signal: controller.signal }), uploadMb);
        if (stale()) return;
        if (!status || status.job_id !== job.job_id
            || !["queued", "processing", "done", "error", "failed"].includes(status.state)) {
          throw new Error("The server returned invalid job information. Click Create GIF to retry.");
        }
        if (batch) {
          batchStatus = status;
          exportProgress = `${status.progress} · ${status.percentage}% · ${status.failed} errors`;
          update();
        }
        if (status.state === "error" || status.state === "failed") {
          throw new Error(typeof status.error === "string" && status.error
            ? status.error : "The server could not create your GIF. Click Create GIF to retry.");
        }
        if (status.state === "done") {
          if (!status.result_available) {
            throw new Error("The completed GIF is no longer available. It may have expired or the server restarted. Click Create GIF to retry.");
          }
          break;
        }
        if (batch) continue;
        const progress = typeof status.progress === "string" && status.progress ? ` ${status.progress}` : "";
        exportProgress = `${status.state === "queued" ? "Queued." : "Processing on the server."}${progress}`;
        update();
      }
      if (batch) return;
      const response = await fetch(`${jobUrl}/result`, { signal: controller.signal });
      if (!response.ok) throw new Error(await apiError(response, true));
      if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("image/gif")) {
        throw new Error("The server did not return a GIF. Please try again.");
      }
      const blob = await response.blob();
      if (stale()) return;
      if (!blob.size) throw new Error("The server returned an empty GIF. Please try again.");
      const disposition = response.headers.get("Content-Disposition") || "";
      const filename = disposition.match(/filename="([^"]+)"/)?.[1]
        || disposition.match(/filename=([^;]+)/)?.[1]?.trim() || fallbackName;
      resultUrl = URL.createObjectURL(blob);
      $("result").src = resultUrl;
      $("download").href = resultUrl;
      $("download").download = filename;
      $("download-name").textContent = filename;
      $("result-fig").hidden = false;
    } catch (error) {
      if (stale()) return;
      $("error").textContent = error instanceof TypeError
        ? "Could not reach the server. Check your connection and try again."
        : error.message;
    } finally {
      if (exportController === controller) {
        exportController = null;
        busy = false;
        exportProgress = "";
        update();
      }
    }
  });

  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    analysisController?.abort();
    exportController?.abort();
    clearResult();
    clearLayer("gif");
    clearLayer("overlay");
  });
  update();
})();
