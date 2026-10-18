/* Source images are browser previews; the server owns validation and GIF rendering. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const config = JSON.parse($("editor-config").textContent);
  const form = $("form");
  const limit = config.maxUploadMb * 1024 * 1024;
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
    ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(2)} MB`;
  const combinedSize = () => Object.values(layers).reduce((sum, layer) => sum + (layer.file?.size || 0), 0);

  function clearResult() {
    $("result-fig").hidden = true;
    $("result").removeAttribute("src");
    $("download").removeAttribute("href");
    $("download").removeAttribute("download");
    $("download-name").textContent = "";
    if (resultUrl) URL.revokeObjectURL(resultUrl);
    resultUrl = null;
  }

  function compositionChanged() {
    revision += 1;
    exportController?.abort();
    clearResult();
    $("error").textContent = "";
    update();
  }

  function update() {
    const total = combinedSize();
    const ready = Boolean(layers.gif.file && layers.overlay.file && info);
    $("submit").disabled = busy || !ready || total >= limit || !settingsValid();
    $("submit").textContent = busy ? "Creating GIF…" : "Create GIF ↗";
    $("form").setAttribute("aria-busy", String(busy));
    $("size-summary").textContent = total
      ? `${bytes(total)} combined · ${config.maxUploadMb} MB request limit (plus form data).`
      : "Files stay in this session; uploads are processed on the server.";
    if (total >= limit) {
      $("status").textContent = "Files exceed the request budget. Choose smaller files; form data also needs space.";
    } else if (busy) {
      $("status").textContent = "Processing on the server. This may take a moment.";
    } else if (resultUrl) {
      $("status").textContent = "Export complete. Preview and download your GIF below.";
    } else if (!layers.gif.file || !layers.overlay.file) {
      $("status").textContent = "Add both files to get started.";
    } else if (!info) {
      $("status").textContent = "A valid base GIF analysis is required before export.";
    } else {
      $("status").textContent = settingsValid() ? "Your layers are ready. Create your GIF when it looks right." : "Check the placement and crop values before exporting.";
    }
    $("custom-controls").hidden = $("position").value !== "custom";
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

  async function apiError(response) {
    let message = "";
    try {
      const body = await response.json();
      if (typeof body.error === "string") message = body.error;
    } catch { /* Proxies may return HTML or an empty body. Never display it. */ }
    if (response.status === 413) {
      return `Request too large. Both files and form data must fit within ${config.maxUploadMb} MB; the host may impose a smaller limit.`;
    }
    return message || `The server could not complete the request (HTTP ${response.status}). Please try again.`;
  }

  async function analyze(file) {
    analysisController?.abort();
    const controller = new AbortController();
    analysisController = controller;
    $("info").textContent = "Reading GIF properties…";
    const data = new FormData();
    data.append("gif", file);
    try {
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
    if (files.length !== 1) {
      $(name + "-error").textContent = "Choose one file for this layer.";
      return;
    }
    const file = files[0];
    if (!file.size) {
      $(name + "-error").textContent = "This file is empty. Choose another image.";
      return;
    }
    // A missing or misleading MIME is common: extensions are a fallback, not proof.
    if (name === "gif" && file.type !== "image/gif" && !/\.gif$/i.test(file.name)) {
      $(name + "-error").textContent = "Choose a GIF for the base layer. The server checks its contents.";
      return;
    }
    if (name === "overlay" && !file.type.startsWith("image/") && !/\.(png|gif|jpe?g|webp|bmp|tiff?|ico|avif|apng)$/i.test(file.name)) {
      $(name + "-error").textContent = "Choose an image such as PNG, GIF, JPEG or WebP. The server checks format support.";
      return;
    }
    if (file.size + combinedSize() >= limit) {
      $(name + "-error").textContent = `Both files must fit within ${config.maxUploadMb} MB per request, with room for form data. Choose a smaller file.`;
      update();
      return;
    }
    const layer = layers[name];
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
    $(name + "-detail").textContent = `${file.name} · ${bytes(file.size)}`;
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
    clearLayer("gif");
    clearLayer("overlay");
    updateMotion();
    compositionChanged();
    $("gif").focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !info || !layers.gif.file || !layers.overlay.file || !form.reportValidity()) return;
    const data = new FormData();
    data.append("gif", layers.gif.file);
    data.append("overlay", layers.overlay.file);
    for (const name of settings) {
      data.append(name, $(name).type === "checkbox" ? $(name).checked : $(name).value);
    }
    busy = true;
    const currentRevision = revision;
    const controller = new AbortController();
    exportController = controller;
    const fallbackName = (layers.gif.file.name.replace(/\.[^.]*$/, "") || "image") + "-overlay.gif";
    clearResult();
    $("error").textContent = "";
    update();
    try {
      // Measure the serialized multipart body, including headers, names and boundaries.
      const request = new Request(config.overlayUrl, { method: "POST", body: data });
      const body = await request.blob();
      if (controller.signal.aborted || currentRevision !== revision) return;
      if (body.size > limit) {
        throw new Error(`The complete request exceeds ${config.maxUploadMb} MB including form data. Choose smaller files.`);
      }
      const response = await fetch(config.overlayUrl, { method: "POST", body, headers: { "Content-Type": request.headers.get("Content-Type") }, signal: controller.signal });
      if (!response.ok) throw new Error(await apiError(response));
      if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("image/gif")) {
        throw new Error("The server did not return a GIF. Please try again.");
      }
      const blob = await response.blob();
      if (controller.signal.aborted || currentRevision !== revision) return;
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
      if (controller.signal.aborted || currentRevision !== revision) return;
      $("error").textContent = error instanceof TypeError
        ? "Could not reach the server. Check your connection and try again."
        : error.message;
    } finally {
      if (exportController === controller) {
        exportController = null;
        busy = false;
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
