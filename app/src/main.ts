import { convertFileSrc, invoke as tauriInvoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";
import "./style.css";

type RuntimeStatus = {
  app_data_dir: string;
  runtime_dir: string;
  engine_dir: string;
  config_path: string;
  python_path: string;
  venv_exists: boolean;
  config_exists: boolean;
  model_exists: boolean;
  tracker_exists: boolean;
  uv_path: string | null;
};

type ProcessStatus = {
  running: boolean;
  exit_code: number | null;
};

type TrackerLog = {
  stream: string;
  line: string;
};

type TrackerEvent = {
  event: string;
  ts?: number;
  [key: string]: unknown;
};

type NetworkReport = {
  interfaces: string;
  default_route: string;
  arp: string;
  targets: Array<{ name: string; host: string; route: string }>;
};

type FieldCheck = {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail";
  meta: string;
  detail: string;
  ts: string;
};

type FieldCheckReport = {
  generated_at: string;
  checks: FieldCheck[];
  target_count: number;
  ok_count: number;
  warn_count: number;
  fail_count: number;
};

type ProjectionInfo = {
  id: string;
  pixel_size: number[];
  world_size_m: number[];
  zones: Array<{ id: string; uv_rect: number[] }>;
};

type RegionInfo = {
  camera: string;
  id: string;
  projection_id: string;
  image_points: number[][];
  projection_uv: number[];
  dispatch_uv: number[];
  min_bbox_height_px: number | null;
};

type ProjectionSnapshot = {
  projections: ProjectionInfo[];
  regions: RegionInfo[];
  camera_count: number;
};

type ProjectionRuntime = {
  id: string;
  active: number[];
  xy: number[];
  persons: Array<{
    gid: number;
    x: number;
    y: number;
    u: number;
    v: number;
    state?: string;
    source?: string;
  }>;
};

type CalibrationFrame = {
  camera: string;
  path: string;
  width: number;
  height: number;
};

type SectionId =
  | "live"
  | "projection"
  | "calibration"
  | "osc"
  | "network"
  | "showtime"
  | "replay"
  | "mobile"
  | "config"
  | "logs"
  | "setup";

const sectionLabels: Record<SectionId, string> = {
  live: "Live",
  projection: "Projection",
  calibration: "Calibration",
  osc: "OSC",
  network: "Network",
  showtime: "Showtime",
  replay: "Replay",
  mobile: "Mobile",
  config: "Config",
  logs: "Logs",
  setup: "Setup",
};

const state: {
  section: SectionId;
  runtime: RuntimeStatus | null;
  process: ProcessStatus;
  config: string;
  logs: TrackerLog[];
  events: TrackerEvent[];
  network: NetworkReport | null;
  fieldChecks: FieldCheckReport | null;
  projection: ProjectionSnapshot | null;
  videoPath: string;
  calibrationCamera: string;
  calibrationRegionId: string;
  calibrationFrame: CalibrationFrame | null;
  calibrationPoints: number[][];
  busy: string | null;
  error: string | null;
  saved: boolean;
} = {
  section: "live",
  runtime: null,
  process: { running: false, exit_code: null },
  config: "",
  logs: [],
  events: [],
  network: null,
  fieldChecks: null,
  projection: null,
  videoPath: "/Users/taeyang/Desktop/VomReo01-01-211808-211942.mp4",
  calibrationCamera: "cam0",
  calibrationRegionId: "",
  calibrationFrame: null,
  calibrationPoints: [],
  busy: null,
  error: null,
  saved: true,
};

const hasTauriRuntime =
  typeof (globalThis as { __TAURI_INTERNALS__?: { invoke?: unknown } }).__TAURI_INTERNALS__?.invoke ===
  "function";

const app = document.querySelector<HTMLDivElement>("#app");

if (!app) {
  throw new Error("missing #app");
}

const root = app;
let deferredRender = false;

function isConfigEditorActive(): boolean {
  return state.section === "config" && document.activeElement?.id === "configEditor";
}

function requestRender(): void {
  if (isConfigEditorActive()) {
    deferredRender = true;
    return;
  }
  deferredRender = false;
  render();
}

function latestEvent(name: string): TrackerEvent | undefined {
  return [...state.events].reverse().find((event) => event.event === name);
}

function latestFpsEvent(): TrackerEvent | undefined {
  return latestEvent("fps_tick");
}

function cameraItems(): Record<string, unknown>[] {
  const fps = latestFpsEvent();
  return Array.isArray(fps?.cameras) ? (fps.cameras as Record<string, unknown>[]) : [];
}

function isSetupReady(runtime = state.runtime): boolean {
  return Boolean(
    runtime?.venv_exists &&
      runtime.config_exists &&
      runtime.tracker_exists &&
      runtime.model_exists,
  );
}

function cameraRows(): string {
  const cameras = cameraItems();
  if (!cameras.length) {
    return `<tr><td colspan="5" class="empty-cell">No camera status events yet.</td></tr>`;
  }
  return cameras
    .map((cam, index) => {
      const camClass = index % 2 === 0 ? "cam0" : "cam1";
      return `<tr>
        <td><span class="cam-swatch ${camClass}"></span>${escapeHtml(String(cam.name ?? ""))}</td>
        <td>${formatNumber(cam.fps)}</td>
        <td>${formatNumber(cam.osc_rate)}</td>
        <td>${escapeHtml(String(cam.reconnects ?? 0))}</td>
        <td>${formatNumber(cam.frame_age_s)} s</td>
      </tr>`;
    })
    .join("");
}

function render(): void {
  const setupReady = isSetupReady();
  const activeCameras = cameraItems().length;
  const oscRate = sumCameraNumber("osc_rate");

  root.innerHTML = `
    <section class="app">
      <header class="topbar">
        <div class="brand">
          ${vomlabLogo()}
          <span class="name">/reolink</span>
          <span class="ver">operator</span>
        </div>
        <span class="pill ${state.process.running ? "live" : ""}">
          <span class="dot"></span>${state.process.running ? "Running" : "Stopped"}
        </span>
        <span class="pill ${setupReady ? "ok" : "warn"}">
          <span class="dot"></span>${setupReady ? "Runtime ready" : "Setup needed"}
        </span>
        <div class="topbar-meta">
          <div class="meta-cell">cams <b>${activeCameras}</b></div>
          <div class="meta-cell">osc <b>${formatNumber(oscRate)}</b><span class="unit">/s</span></div>
          <div class="meta-cell">logs <b>${state.logs.length}</b></div>
        </div>
        <div class="topbar-actions">
          <button class="btn" data-action="refresh" ${buttonDisabled()}>Refresh</button>
          <button class="btn" data-action="prepare" ${buttonDisabled()}>Setup</button>
          <button class="btn primary" data-action="start" ${buttonDisabled(!setupReady || state.process.running)}>Start</button>
          <button class="btn" data-action="preview" ${buttonDisabled(!setupReady || state.process.running)}>Preview</button>
          <button class="btn danger" data-action="stop" ${buttonDisabled(!state.process.running)}>Stop</button>
        </div>
      </header>

      <aside class="nav">
        <div class="nav-section">
          <div class="nav-label">Operate</div>
          ${navButton("live", "Live", activeCameras ? String(activeCameras) : "idle")}
          ${navButton("projection", "Projection", "uv")}
          ${navButton("osc", "OSC", `${formatNumber(oscRate)}/s`)}
          ${navButton("network", "Network", state.network ? String(state.network.targets.length) : "probe")}
          ${navButton("showtime", "Showtime", setupReady ? "ready" : "todo")}
          ${navButton("mobile", "Mobile", "health")}
        </div>
        <div class="nav-section">
          <div class="nav-label">Tools</div>
          ${navButton("calibration", "Calibration", "4pt")}
          ${navButton("replay", "Replay", "sidecar")}
          ${navButton("config", "Config", state.saved ? "saved" : "dirty")}
          ${navButton("logs", "Logs", String(state.logs.length))}
          ${navButton("setup", "Setup", setupReady ? "ready" : "todo")}
        </div>
        <div class="nav-footer">
          <div class="row"><span>runtime</span><b>${setupReady ? "ready" : "missing"}</b></div>
          <div class="row"><span>cfg</span><b>${state.runtime?.config_exists ? "config.yaml" : "none"}</b></div>
          <div class="row"><span>mode</span><b>${hasTauriRuntime ? "desktop" : "browser"}</b></div>
        </div>
      </aside>

      <main class="main">
        <div class="main-head">
          <h1>${sectionLabels[state.section]}</h1>
          <span class="crumb">reolink-tracker / ${sectionLabels[state.section].toLowerCase()}</span>
          <span class="spacer"></span>
          <span class="pill">${state.saved ? "config saved" : "config unsaved"}</span>
        </div>
        ${state.error ? `<div class="banner error">${escapeHtml(state.error)}</div>` : ""}
        ${state.busy ? `<div class="banner">${escapeHtml(state.busy)}</div>` : ""}
        ${mainSection()}
      </main>

      <aside class="right">
        ${rightRail(setupReady)}
      </aside>
    </section>
  `;

  root.querySelectorAll<HTMLButtonElement>("button[data-action]").forEach((button) => {
    button.addEventListener("click", () => void handleAction(button.dataset.action ?? ""));
  });
  root.querySelectorAll<HTMLButtonElement>("button[data-section]").forEach((button) => {
    button.addEventListener("click", () => {
      state.section = (button.dataset.section ?? "live") as SectionId;
      render();
    });
  });
  const editor = root.querySelector<HTMLTextAreaElement>("#configEditor");
  editor?.addEventListener("input", () => {
    state.config = editor.value;
    state.saved = false;
    root.querySelector<HTMLButtonElement>('button[data-action="save-config"]')?.removeAttribute("disabled");
  });
  editor?.addEventListener("blur", () => {
    if (deferredRender) {
      window.setTimeout(requestRender, 0);
    }
  });
  const videoPath = root.querySelector<HTMLInputElement>("#videoPath");
  videoPath?.addEventListener("input", () => {
    state.videoPath = videoPath.value;
  });
  const calibrationCamera = root.querySelector<HTMLSelectElement>("#calibrationCamera");
  calibrationCamera?.addEventListener("change", () => {
    state.calibrationCamera = calibrationCamera.value;
    state.calibrationRegionId = firstRegionIdForCamera(calibrationCamera.value);
    state.calibrationFrame = null;
    state.calibrationPoints = [];
    render();
  });
  const calibrationRegion = root.querySelector<HTMLSelectElement>("#calibrationRegion");
  calibrationRegion?.addEventListener("change", () => {
    state.calibrationRegionId = calibrationRegion.value;
    state.calibrationPoints = [];
    render();
  });
  const calibrationFrame = root.querySelector<HTMLElement>("#calibrationFrame");
  calibrationFrame?.addEventListener("click", (event) => {
    if (!state.calibrationFrame) {
      return;
    }
    const rect = calibrationFrame.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * state.calibrationFrame.width;
    const y = ((event.clientY - rect.top) / rect.height) * state.calibrationFrame.height;
    const next = state.calibrationPoints.length >= 4 ? [] : [...state.calibrationPoints];
    next.push([Math.round(x), Math.round(y)]);
    state.calibrationPoints = next;
    render();
  });
}

function navButton(section: SectionId, label: string, badge: string): string {
  return `<button class="nav-item ${state.section === section ? "active" : ""}" data-section="${section}">
    <span class="nav-dot"></span>
    <span>${label}</span>
    <span class="badge">${escapeHtml(badge)}</span>
  </button>`;
}

function vomlabLogo(): string {
  return `<svg class="brand-logo" viewBox="0 0 174 42" role="img" aria-label="VOMLab">
    <path class="brand-logo-mark" d="M14 3 C22 8 29 18 37 30 C42 37 46 39 50 39 C57 39 63 33 72 20 C76 14 80 8 84 4 L89 4 C82 15 74 28 65 41 L55 41 C45 27 34 12 23 3 Z" />
    <text class="brand-logo-text" x="0" y="34">VOMLab</text>
  </svg>`;
}

function mainSection(): string {
  if (state.section === "projection") {
    return projectionSection();
  }
  if (state.section === "calibration") {
    return calibrationSection();
  }
  if (state.section === "osc") {
    return oscSection();
  }
  if (state.section === "config") {
    return configSection();
  }
  if (state.section === "logs") {
    return logsSection();
  }
  if (state.section === "network") {
    return networkSection();
  }
  if (state.section === "showtime") {
    return showtimeSection();
  }
  if (state.section === "replay") {
    return replaySection();
  }
  if (state.section === "mobile") {
    return mobileSection();
  }
  if (state.section === "setup") {
    return setupSection();
  }
  return liveSection();
}

function liveSection(): string {
  return `
    <section class="metric-grid">
      <article class="panel">
        <div class="panel-head"><h3>runtime readiness</h3><span class="sub">local app data runtime</span></div>
        <div class="panel-body">${runtimePanel(state.runtime)}</div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>camera status</h3><span class="sub">tracker-status event stream</span></div>
        <div class="panel-body panel-body-flush">
          <table class="app-table">
            <thead><tr><th>Name</th><th>FPS</th><th>OSC/s</th><th>Reconnects</th><th>Age</th></tr></thead>
            <tbody>${cameraRows()}</tbody>
          </table>
        </div>
      </article>
    </section>

    <section class="metric-grid">
      <article class="panel">
        <div class="panel-head"><h3>td minimal stream</h3><span class="sub">active ids + projection x/y</span></div>
        <div class="panel-body">${tdRuntimePanel()}</div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>video test source</h3><span class="sub">temporary config, not saved</span></div>
        <div class="panel-body">
          <div class="video-test-row">
            <input id="videoPath" value="${escapeHtml(state.videoPath)}" placeholder="/path/to/test.mp4" ${state.process.running ? "disabled" : ""} />
            <button class="btn primary" data-action="start-video-test" ${buttonDisabled(!isSetupReady() || state.process.running || !state.videoPath.trim())}>Start Test</button>
            <button class="btn" data-action="preview-video-test" ${buttonDisabled(!isSetupReady() || state.process.running || !state.videoPath.trim())}>Preview</button>
          </div>
          <p class="muted block-copy">Runs tracker with a generated runtime video-test-config.yaml. The saved config.yaml is left untouched.</p>
        </div>
      </article>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>operator stream</h3>
        <span class="sub">latest stdout/stderr</span>
        <div class="actions"><button class="btn" data-action="clear-logs">Clear</button></div>
      </div>
      <div class="logs compact">${logRows(12)}</div>
    </section>
  `;
}

function projectionSection(): string {
  const snapshot = state.projection;
  const projections = snapshot?.projections ?? [];
  const regions = snapshot?.regions ?? [];
  const projection = projections[0];
  const zones = projection?.zones ?? [];
  return `
    <section class="panel proj-wrap">
      <div class="panel-head">
        <h3>projection layout</h3>
        <span class="sub">${escapeHtml(projection?.id ?? "config projection")} · dispatch_uv monitor</span>
        <div class="actions">
          <button class="btn" data-action="projection-refresh" ${buttonDisabled()}>Reload</button>
          <span class="pill ok">read-only</span>
        </div>
      </div>
      <div class="proj-meta">
        <span>projection <b>${escapeHtml(projection?.id ?? "-")}</b></span>
        <span>world <b>${formatVector(projection?.world_size_m, "m")}</b></span>
        <span>pixels <b>${formatVector(projection?.pixel_size, "px")}</b></span>
        <span>cameras <b>${snapshot?.camera_count ?? cameraNames().length}</b></span>
        <span>regions <b>${regions.length}</b></span>
      </div>
      <div class="proj-canvas">
        ${regions.length ? regions.map(regionTile).join("") : `<div class="empty-projection">No calibrated regions in config.yaml yet.</div>`}
        ${regions.length > 1 ? `<div class="seam" style="left:48%;width:4%"><span>hand-off</span></div>` : ""}
        ${zones.map(zoneTile).join("")}
        ${projectionPeople()}
      </div>
    </section>
    <section class="metric-grid">
      ${validationCard("projection config", projections.length ? `${projections.length}` : "none", projections.length ? "ok" : "warn", "read from config.yaml projections[]")}
      ${validationCard("calibrated regions", regions.length ? `${regions.length}` : "none", regions.length ? "ok" : "warn", "camera regions with projection_uv and dispatch_uv")}
      ${validationCard("interaction zones", zones.length ? `${zones.length}` : "none", zones.length ? "ok" : "warn", "read-only zone overlay from interaction_zones[]")}
      ${validationCard("live actors", String(projectionRuntimeItems().reduce((count, projection) => count + projection.active.length, 0)), "ok", "latest fps_tick projection active ids")}
    </section>
    <section class="panel">
      <div class="panel-head"><h3>region config</h3><span class="sub">authoritative read-only config snapshot</span></div>
      <div class="panel-body">${projectionRegionRows(regions)}</div>
    </section>
  `;
}

function calibrationSection(): string {
  const cameras = configuredCameraNames();
  if (!cameras.includes(state.calibrationCamera)) {
    state.calibrationCamera = cameras[0] ?? "cam0";
  }
  const regions = calibrationRegionsForCamera(state.calibrationCamera);
  let selectedRegion = regions.find((region) => region.id === state.calibrationRegionId);
  if (!state.calibrationRegionId || !selectedRegion) {
    state.calibrationRegionId = firstRegionIdForCamera(state.calibrationCamera);
    selectedRegion = regions.find((region) => region.id === state.calibrationRegionId);
  }
  const points = state.calibrationPoints.length ? state.calibrationPoints : selectedRegion?.image_points ?? [];
  const frame = state.calibrationFrame;
  const frameSrc = frame && hasTauriRuntime ? convertFileSrc(frame.path) : "";
  const frameStyle = frame
    ? `aspect-ratio:${Math.max(1, frame.width)} / ${Math.max(1, frame.height)};min-height:0`
    : "";
  return `
    <section class="calib-layout">
      <article class="panel">
        <div class="panel-head">
          <h3>4pt</h3>
          <span class="sub">camera corners · tl/tr/br/bl</span>
          <div class="actions">
            <button class="btn" data-action="capture-calibration-frame" ${buttonDisabled(!isSetupReady())}>Capture</button>
            <button class="btn" data-action="capture-video-calibration-frame" ${buttonDisabled(!isSetupReady())}>Use live video</button>
          </div>
        </div>
        <div class="calib-toolbar">
          <label>Camera ${selectHtml("calibrationCamera", cameras, state.calibrationCamera)}</label>
          <label>Region ${selectHtml("calibrationRegion", regions.map((region) => region.id), state.calibrationRegionId)}</label>
          <button class="btn" data-action="clear-calibration-points" ${buttonDisabled(!points.length)}>Clear points</button>
          <button class="btn primary" data-action="save-calibration-points" ${buttonDisabled(state.calibrationPoints.length !== 4)}>Save 4 points</button>
        </div>
        <div id="calibrationFrame" class="calib-frame ${frameSrc ? "has-image" : ""}" style="${frameStyle}">
          ${frameSrc && frame ? `<img class="calib-image" src="${escapeAttr(frameSrc)}" alt="${escapeAttr(frame.camera)} calibration frame" draggable="false">` : `<div class="frame-grid"></div><div class="future-note">Capture a live camera frame, then click four projection corners in order.</div>`}
          ${frame && points.length ? calibrationPointMarkers(points, frame).join("") : ""}
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>saved calibration</h3><span class="sub">runtime config.yaml snapshot</span></div>
        <div class="panel-body">
          <div class="checks">
            <span class="${isSetupReady() ? "ok" : "missing"}">runtime: ${isSetupReady() ? "ready" : "missing"}</span>
            <span class="${points.length === 4 ? "ok" : "missing"}">image points: ${points.length}/4</span>
          </div>
          <div class="kv">
            <div class="row"><span class="k">camera</span><span class="v">${escapeHtml(state.calibrationCamera)}</span></div>
            <div class="row"><span class="k">region</span><span class="v">${escapeHtml(state.calibrationRegionId)}</span></div>
            <div class="row"><span class="k">points</span><span class="v">${escapeHtml(formatImagePoints(points))}</span></div>
            <div class="row"><span class="k">projection uv</span><span class="v">${escapeHtml(formatUvRange(selectedRegion?.projection_uv ?? []))}</span></div>
            <div class="row"><span class="k">dispatch uv</span><span class="v">${escapeHtml(formatUvRange(selectedRegion?.dispatch_uv ?? []))}</span></div>
            <div class="row"><span class="k">save path</span><span class="v">${escapeHtml(shortPath(state.runtime?.config_path))}</span></div>
            <div class="row"><span class="k">frame</span><span class="v">${frame ? `${frame.width} x ${frame.height}` : "not captured"}</span></div>
          </div>
          <p class="muted block-copy setup-copy">This saves camera image_points. Projection/dispatch UV slices are still edited from Config or Show Preview in this pass.</p>
        </div>
      </article>
    </section>
  `;
}

function oscSection(): string {
  return `
    <section class="osc-grid">
      <article class="panel">
        <div class="panel-head"><h3>td minimal addresses</h3><span class="sub">runtime contract for TouchDesigner</span></div>
        <div class="panel-body address-list">
          ${minimalAddressRows()}
        </div>
      </article>
      <article class="panel">
        <div class="panel-head">
          <h3>message log</h3>
          <span class="sub">stdout/stderr proxy until OSC ring exists</span>
          <div class="actions"><button class="btn" data-action="clear-logs">Clear</button></div>
        </div>
        <div class="logs compact">${logRows(12)}</div>
      </article>
    </section>
    <section class="panel">
      <div class="panel-head"><h3>osc monitor requirements</h3><span class="sub">kept outside tracker runtime</span></div>
      <div class="panel-body future-grid">
        ${statusTile("current stream", "active id list and packed projection x/y triples", "live")}
        ${statusTile("log export", "requires sidecar ring buffer, not tracker process memory", "sidecar")}
        ${statusTile("TD ack", "optional TouchDesigner reply handshake", "manual")}
      </div>
    </section>
  `;
}

function showtimeSection(): string {
  const report = state.fieldChecks;
  const steps = report?.checks ?? fallbackShowtimeChecks();
  const criticalReady = Boolean(report && report.fail_count === 0 && isSetupReady());
  return `
    <section class="showtime-layout">
      <article class="panel">
        <div class="panel-head">
          <h3>pre-show checklist</h3>
          <span class="sub">${report ? `${report.ok_count} ok · ${report.warn_count} warn · ${report.fail_count} fail` : "run checks before opening"}</span>
          <div class="actions">
            <button class="btn" data-action="field-checks" ${buttonDisabled()}>Run checks</button>
            <button class="btn primary" data-action="prepare" ${buttonDisabled()}>Run setup</button>
          </div>
        </div>
        <div class="checklist">
          ${steps.map((check, index) => showtimeStep(index + 1, check)).join("")}
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>go live gate</h3><span class="sub">operator confirmation</span></div>
        <div class="panel-body">
          <div class="big-num">${criticalReady ? "READY" : "WAIT"}<span class="unit">checks</span></div>
          <p class="muted block-copy">Launcher checks cover runtime, config, routes, RTSP port probes, and process state. TD handshake and walk-test stay manual until sidecar instrumentation exists.</p>
          <div class="inline-actions">
            <button class="btn primary" data-action="start" ${buttonDisabled(!criticalReady || state.process.running)}>Start</button>
            <button class="btn" data-action="preview" ${buttonDisabled(!isSetupReady() || state.process.running)}>Preview</button>
          </div>
          ${report ? `<div class="field-summary">last run ${formatCheckTime(report.generated_at)} · targets ${report.target_count}</div>` : ""}
        </div>
      </article>
    </section>
  `;
}

function replaySection(): string {
  return `
    <section class="panel">
      <div class="panel-head">
        <h3>buffer replay</h3>
        <span class="sub">sidecar requirement map · current app is read-only</span>
        <div class="actions"><span class="pill warn">sidecar required</span></div>
      </div>
      <div class="timeline">
        <div class="timeline-track"></div>
        <span class="event-marker lost" style="left:18%">lost</span>
        <span class="event-marker warn" style="left:47%">reconnect</span>
        <span class="event-marker bookmark" style="left:72%">bookmark</span>
        <span class="playhead" style="left:55%"></span>
      </div>
      <div class="panel-body future-grid">
        ${statusTile("frame ring", "5 fps downsampled frames, isolated from tracker", "sidecar")}
        ${statusTile("OSC re-send", "replay selected time range to TouchDesigner", "sidecar")}
        ${statusTile("clip export", "bounded zip export with auto-delete policy", "sidecar")}
      </div>
    </section>
  `;
}

function mobileSection(): string {
  return `
    <section class="mobile-wrap">
      <div class="phone-shell">
        <div class="phone-screen">
          <div class="phone-top">vomlab/reolink</div>
          <div class="phone-status ${state.process.running ? "ok" : "warn"}">${state.process.running ? "RUNNING" : "STOPPED"}</div>
          <div class="traffic-grid">
            ${trafficTile("tracker", state.process.running ? "ok" : "warn", state.process.running ? "running" : "stopped")}
            ${trafficTile("runtime", isSetupReady() ? "ok" : "warn", isSetupReady() ? "ready" : "setup")}
            ${trafficTile("cameras", cameraItems().length ? "ok" : "warn", String(cameraItems().length))}
            ${trafficTile("osc", sumCameraNumber("osc_rate") > 0 ? "ok" : "warn", `${formatNumber(sumCameraNumber("osc_rate"))}/s`)}
          </div>
          <div class="phone-events">${eventRows()}</div>
        </div>
      </div>
      <article class="panel">
        <div class="panel-head"><h3>mobile health mirror</h3><span class="sub">desktop state, read-only</span></div>
        <div class="panel-body">
          <p class="muted block-copy">This preview mirrors the current desktop state. Real phone polling should live in the sidecar so tracker latency is untouched.</p>
          ${pathPanel(state.runtime)}
        </div>
      </article>
    </section>
  `;
}

function validationCard(title: string, value: string, tone: "ok" | "warn", note: string): string {
  return `<article class="panel">
    <div class="panel-head"><h3>${escapeHtml(title)}</h3><span class="pill ${tone}">${escapeHtml(value)}</span></div>
    <div class="panel-body"><p class="muted">${escapeHtml(note)}</p></div>
  </article>`;
}

function projectionPeople(): string {
  const runtimePersons = projectionRuntimeItems().flatMap((projection) => projection.persons);
  if (runtimePersons.length) {
    return runtimePersons
      .slice(0, 24)
      .map((person) => {
        const left = clamp01(person.u) * 100;
        const top = clamp01(person.v) * 100;
        return `<div class="person-dot" style="left:${left}%;top:${top}%"><span>gid ${escapeHtml(String(person.gid))}</span></div>`;
      })
      .join("");
  }
  const cameras = cameraItems();
  if (!cameras.length) {
    return `<div class="person-dot held" style="left:50%;top:50%"><span>idle</span></div>`;
  }
  return cameras
    .slice(0, 4)
    .map((cam, index) => {
      const left = 22 + index * 18;
      const top = 36 + (index % 2) * 22;
      return `<div class="person-dot" style="left:${left}%;top:${top}%"><span>${escapeHtml(String(cam.name ?? `cam${index}`))}</span></div>`;
    })
    .join("");
}

function projectionRuntimeItems(): ProjectionRuntime[] {
  const fps = latestFpsEvent();
  if (!Array.isArray(fps?.projections)) {
    return [];
  }
  return (fps.projections as Record<string, unknown>[])
    .map((projection) => ({
      id: String(projection.id ?? ""),
      active: Array.isArray(projection.active)
        ? projection.active.map((id) => Number(id)).filter(Number.isFinite)
        : [],
      xy: Array.isArray(projection.xy)
        ? projection.xy.map((value) => Number(value)).filter(Number.isFinite)
        : [],
      persons: Array.isArray(projection.persons)
        ? (projection.persons as Record<string, unknown>[]).map((person) => ({
            gid: Number(person.gid),
            x: Number(person.x),
            y: Number(person.y),
            u: Number(person.u),
            v: Number(person.v),
            state: person.state ? String(person.state) : undefined,
            source: person.source ? String(person.source) : undefined,
          })).filter((person) => Number.isFinite(person.gid) && Number.isFinite(person.x) && Number.isFinite(person.y))
        : [],
    }))
    .filter((projection) => projection.id);
}

function tdRuntimePanel(): string {
  const projections = projectionRuntimeItems();
  if (!projections.length) {
    return `<p class="muted">No projection runtime payload yet. Start tracker to see /active and /xy state.</p>`;
  }
  return `<div class="td-runtime-list">${projections
    .map((projection) => {
      const active = projection.active.length ? projection.active.join(", ") : "-";
      const rows = projection.persons.length
        ? projection.persons
            .slice(0, 8)
            .map(
              (person) => `<div class="micro-row">
                <span>gid ${escapeHtml(String(person.gid))}</span>
                <b>${formatCoord(person.x)}, ${formatCoord(person.y)}</b>
              </div>`,
            )
            .join("")
        : `<p class="muted">No active ids.</p>`;
      return `<div class="td-runtime-block">
        <div class="micro-row"><span>/proj/${escapeHtml(projection.id)}/active</span><b>${escapeHtml(active)}</b></div>
        <div class="micro-row"><span>/proj/${escapeHtml(projection.id)}/xy</span><b>${projection.xy.length} values</b></div>
        ${rows}
      </div>`;
    })
    .join("")}</div>`;
}

function minimalAddressRows(): string {
  const projections = projectionRuntimeItems();
  if (!projections.length) {
    const pid = state.projection?.projections[0]?.id ?? "corridor";
    return [
      addressRate(`/proj/${pid}/active`, 0, "ids"),
      addressRate(`/proj/${pid}/xy`, 0, "xy"),
    ].join("");
  }
  return projections
    .map((projection) => {
      const rate = sumCameraNumber("osc_rate") / Math.max(1, projections.length);
      return [
        addressRate(`/proj/${projection.id}/active`, rate / 2, "ids"),
        addressRate(`/proj/${projection.id}/xy`, rate / 2, "xy"),
      ].join("");
    })
    .join("");
}

function regionTile(region: RegionInfo, index: number): string {
  const rect = uvRect(region.dispatch_uv.length === 4 ? region.dispatch_uv : region.projection_uv);
  const camClass = index % 2 === 0 ? "cam0" : "cam1";
  return `<div class="dispatch ${camClass}" style="${rectStyle(rect)}">
    <span>${escapeHtml(region.camera)} · ${escapeHtml(region.id)}</span>
    <b>${formatUvRange(region.dispatch_uv)}</b>
  </div>`;
}

function zoneTile(zone: { id: string; uv_rect: number[] }): string {
  const rect = uvRect(zone.uv_rect);
  return `<div class="zone-tile" style="${rectStyle(rect)}">${escapeHtml(zone.id)}</div>`;
}

function projectionRegionRows(regions: RegionInfo[]): string {
  if (!regions.length) {
    return `<p class="muted">No camera regions are configured yet. Add regions in config.yaml or use the existing preview/calibration workflow.</p>`;
  }
  return `<div class="kv">${regions
    .map(
      (region) => `<div class="row">
        <span class="k">${escapeHtml(region.camera)} / ${escapeHtml(region.id)}</span>
        <span class="v">${escapeHtml(region.projection_id)} · proj ${formatUvRange(region.projection_uv)} · dispatch ${formatUvRange(region.dispatch_uv)}</span>
      </div>`,
    )
    .join("")}</div>`;
}

function uvRect(values: number[]): { left: number; top: number; width: number; height: number } {
  const [x0 = 0, y0 = 0, x1 = 1, y1 = 1] = values;
  const left = clamp01(Math.min(x0, x1));
  const top = clamp01(Math.min(y0, y1));
  const right = clamp01(Math.max(x0, x1));
  const bottom = clamp01(Math.max(y0, y1));
  return {
    left,
    top,
    width: Math.max(0.01, right - left),
    height: Math.max(0.01, bottom - top),
  };
}

function rectStyle(rect: { left: number; top: number; width: number; height: number }): string {
  return `left:${rect.left * 100}%;top:${rect.top * 100}%;width:${rect.width * 100}%;height:${rect.height * 100}%;bottom:auto`;
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
}

function formatUvRange(values: number[]): string {
  return values.length === 4 ? values.map((value) => Number(value).toFixed(2)).join(",") : "-";
}

function formatVector(values: number[] | undefined, unit: string): string {
  if (!values?.length) {
    return "-";
  }
  return `${values.map((value) => Number(value).toFixed(Number.isInteger(value) ? 0 : 1)).join(" x ")} ${unit}`;
}

function addressRate(address: string, rate: number, label: string): string {
  const width = Math.max(8, Math.min(100, rate * 12));
  return `<div class="address-row">
    <span class="addr">${escapeHtml(address)}</span>
    <span class="rate">${formatNumber(rate)}/s</span>
    <span class="meter"><span style="width:${width}%"></span></span>
    <span class="badge">${escapeHtml(label)}</span>
  </div>`;
}

function statusTile(title: string, body: string, status: string): string {
  const tone = status === "live" ? "ok" : status === "manual" ? "warn" : "";
  return `<div class="future-tile">
    <span class="pill ${tone}">${escapeHtml(status)}</span>
    <b>${escapeHtml(title)}</b>
    <p>${escapeHtml(body)}</p>
  </div>`;
}

function fallbackShowtimeChecks(): FieldCheck[] {
  return [
    {
      id: "runtime_prepared",
      label: "Runtime prepared",
      status: isSetupReady() ? "ok" : "warn",
      meta: isSetupReady() ? "ready" : "setup needed",
      detail: "Run checks for a full pre-show report.",
      ts: "",
    },
    {
      id: "config_valid",
      label: "Config YAML",
      status: state.config ? "ok" : "warn",
      meta: state.config ? "loaded" : "missing",
      detail: "Config is loaded through the current Tauri command surface.",
      ts: "",
    },
    {
      id: "tracker_process",
      label: "Tracker process",
      status: state.process.running ? "ok" : "warn",
      meta: state.process.running ? "running" : "stopped",
      detail: "Use Start or Preview after runtime setup.",
      ts: "",
    },
  ];
}

function showtimeStep(index: number, check: FieldCheck): string {
  const tone = check.status === "ok" ? "ok" : check.status === "fail" ? "err" : "warn";
  return `<div class="showtime-step ${escapeHtml(check.status)}">
    <span class="step-index">${index}</span>
    <span class="step-name">
      ${escapeHtml(check.label)}
      <small>${escapeHtml(check.meta)}</small>
    </span>
    <span class="pill ${tone}">${escapeHtml(check.status)}</span>
    ${check.detail ? `<p class="step-detail">${escapeHtml(check.detail)}</p>` : ""}
  </div>`;
}

function trafficTile(title: string, tone: "ok" | "warn", value: string): string {
  return `<div class="traffic-tile ${tone}">
    <span>${escapeHtml(title)}</span>
    <b>${escapeHtml(value)}</b>
  </div>`;
}

function configuredCameraNames(): string[] {
  const fromRegions = [...new Set((state.projection?.regions ?? []).map((region) => region.camera))].filter(Boolean);
  const fromConfig = cameraNames();
  const names = [...new Set([...fromRegions, ...fromConfig])].filter(Boolean);
  return names.length ? names : ["cam0"];
}

function calibrationRegionsForCamera(camera: string): RegionInfo[] {
  return (state.projection?.regions ?? []).filter((region) => region.camera === camera);
}

function firstRegionIdForCamera(camera: string): string {
  return calibrationRegionsForCamera(camera)[0]?.id ?? defaultCalibrationRegionId(camera);
}

function defaultCalibrationRegionId(camera: string): string {
  const name = camera.trim() || "camera";
  return `${name}_region_1`;
}

function selectHtml(id: string, options: string[], selected: string): string {
  const normalized = options.length ? options : [selected];
  const withSelected = normalized.includes(selected) ? normalized : [selected, ...normalized].filter(Boolean);
  return `<select id="${escapeAttr(id)}">${withSelected
    .map((option) => `<option value="${escapeAttr(option)}" ${option === selected ? "selected" : ""}>${escapeHtml(option)}</option>`)
    .join("")}</select>`;
}

function calibrationPointMarkers(points: number[][], frame: CalibrationFrame): string[] {
  return points.slice(0, 4).map((point, index) => {
    const left = clamp01(Number(point[0]) / Math.max(1, frame.width)) * 100;
    const top = clamp01(Number(point[1]) / Math.max(1, frame.height)) * 100;
    return `<span class="calib-point" style="left:${left}%;top:${top}%">${index + 1}</span>`;
  });
}

function formatImagePoints(points: number[][]): string {
  if (!points.length) {
    return "-";
  }
  return points
    .slice(0, 4)
    .map((point) => `[${Math.round(Number(point[0]) || 0)}, ${Math.round(Number(point[1]) || 0)}]`)
    .join(" ");
}

function cameraNames(): string[] {
  const liveNames = cameraItems()
    .map((cam) => String(cam.name ?? ""))
    .filter(Boolean);
  if (liveNames.length) {
    return liveNames;
  }
  const matches = [...state.config.matchAll(/^\s*-\s*name:\s*([^\n#]+)/gm)];
  return matches.map((match) => match[1].trim()).filter(Boolean);
}

function configSection(): string {
  return `
    <section class="panel config-panel">
      <div class="panel-head">
        <h3>config.yaml</h3>
        <span class="sub">${escapeHtml(state.runtime?.config_path ?? "runtime config path unavailable")}</span>
        <div class="actions">
          <button class="btn" data-action="refresh" ${buttonDisabled()}>Reload</button>
          <button class="btn primary" data-action="save-config" ${buttonDisabled(!state.config || state.saved)}>Save</button>
        </div>
      </div>
      <textarea id="configEditor" spellcheck="false">${escapeHtml(state.config)}</textarea>
    </section>
  `;
}

function logsSection(): string {
  return `
    <section class="split">
      <article class="panel logs-panel">
        <div class="panel-head">
          <h3>logs</h3>
          <span class="sub">last ${Math.min(state.logs.length, 300)} lines</span>
          <div class="actions"><button class="btn" data-action="clear-logs">Clear</button></div>
        </div>
        <div class="logs">${logRows(300)}</div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>tracker events</h3><span class="sub">parsed JSON payloads</span></div>
        <div class="panel-body events-list">${eventRows()}</div>
      </article>
    </section>
  `;
}

function networkSection(): string {
  return `
    <section class="panel">
      <div class="panel-head">
        <h3>network diagnostics</h3>
        <span class="sub">macOS route / ARP / camera targets</span>
        <div class="actions">
          <button class="btn" data-action="field-checks" ${buttonDisabled()}>Run field checks</button>
          <button class="btn primary" data-action="network" ${buttonDisabled()}>Collect</button>
        </div>
      </div>
      <div class="panel-body">${networkPanel()}</div>
    </section>
    <section class="panel field-check-panel">
      <div class="panel-head">
        <h3>field checks</h3>
        <span class="sub">${state.fieldChecks ? `${state.fieldChecks.ok_count} ok · ${state.fieldChecks.warn_count} warn · ${state.fieldChecks.fail_count} fail` : "not run yet"}</span>
      </div>
      <div class="panel-body">${fieldCheckPanel()}</div>
    </section>
  `;
}

function setupSection(): string {
  return `
    <section class="metric-grid">
      <article class="panel">
        <div class="panel-head">
          <h3>runtime setup</h3>
          <span class="sub">copy engine, prepare venv, warm model</span>
          <div class="actions"><button class="btn primary" data-action="prepare" ${buttonDisabled()}>Run setup</button></div>
        </div>
        <div class="panel-body">
          ${runtimePanel(state.runtime)}
          <p class="muted block-copy setup-copy">Setup refreshes tracker.py, fusion.py, region.py, viewer.py, requirements.txt, and config.example.yaml from this repo. Existing runtime config.yaml is preserved.</p>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>paths</h3><span class="sub">launcher-owned files</span></div>
        <div class="panel-body">${pathPanel(state.runtime)}</div>
      </article>
    </section>
  `;
}

async function invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (hasTauriRuntime) {
    return tauriInvoke<T>(command, args);
  }
  return mockInvoke<T>(command);
}

async function mockInvoke<T>(command: string): Promise<T> {
  if (command === "get_runtime_status") {
    return {
      app_data_dir: "Tauri runtime required",
      runtime_dir: "Preview mode",
      engine_dir: "../reolink-tracker",
      config_path: "app-data/runtime/config.yaml",
      python_path: "app-data/runtime/.venv/bin/python",
      venv_exists: false,
      config_exists: false,
      model_exists: false,
      tracker_exists: true,
      uv_path: null,
    } as T;
  }
  if (command === "tracker_status") {
    return { running: false, exit_code: null } as T;
  }
  if (command === "start_video_test") {
    return { running: true, exit_code: null } as T;
  }
  if (command === "capture_calibration_frame") {
    return {
      camera: "cam0",
      path: "",
      width: 1280,
      height: 720,
    } as T;
  }
  if (command === "save_calibration_points") {
    return undefined as T;
  }
  if (command === "read_config") {
    return `model: yolo26n.pt
device: mps
imgsz: 640
conf: 0.35
tracker: botsort.yaml

osc:
  host: 127.0.0.1
  port: 7000
  person_level: true
  raw_per_cam: true
  legacy_image_space: false

cameras:
  - name: cam0
    url: rtsp://admin:<urlencoded-password>@<camera-ip>:554/h264Preview_01_sub
    osc_prefix: /cam/0
    regions: []
` as T;
  }
  if (command === "collect_network_report") {
    return {
      interfaces: "Tauri runtime required for macOS network diagnostics.",
      default_route: "Preview mode",
      arp: "Preview mode",
      targets: [],
    } as T;
  }
  if (command === "run_field_checks") {
    return {
      generated_at: String(Math.floor(Date.now() / 1000)),
      target_count: 0,
      ok_count: 1,
      warn_count: 2,
      fail_count: 0,
      checks: [
        {
          id: "runtime_prepared",
          label: "Runtime prepared",
          status: "warn",
          meta: "preview mode",
          detail: "Open the Tauri desktop app to run real checks.",
          ts: "",
        },
        {
          id: "config_valid",
          label: "Config YAML",
          status: "ok",
          meta: "mock config",
          detail: "Preview config parsed.",
          ts: "",
        },
        {
          id: "td_handshake",
          label: "TouchDesigner handshake",
          status: "warn",
          meta: "sidecar required",
          detail: "TD ack is outside the current launcher command surface.",
          ts: "",
        },
      ],
    } as T;
  }
  if (command === "read_projection_snapshot") {
    return {
      camera_count: 1,
      projections: [
        {
          id: "corridor",
          pixel_size: [9600, 1080],
          world_size_m: [40, 4.5],
          zones: [{ id: "center", uv_rect: [0.35, 0.15, 0.65, 0.85] }],
        },
      ],
      regions: [],
    } as T;
  }
  throw new Error(`${command} requires the Tauri desktop runtime.`);
}

function runtimePanel(runtime: RuntimeStatus | null): string {
  if (!runtime) {
    return `<p class="muted">Runtime status has not loaded yet.</p>`;
  }
  return `
    <div class="checks">
      ${runtimeChecks(runtime)
        .map(
          ({ label, ok }) =>
            `<span class="${ok ? "ok" : "missing"}">${escapeHtml(label)}: ${ok ? "ready" : "missing"}</span>`,
        )
        .join("")}
    </div>
    ${pathPanel(runtime)}
  `;
}

function runtimeChecks(runtime: RuntimeStatus): Array<{ label: string; ok: boolean }> {
  return [
    { label: "Tracker", ok: runtime.tracker_exists },
    { label: "Python venv", ok: runtime.venv_exists },
    { label: "Config", ok: runtime.config_exists },
    { label: "Model", ok: runtime.model_exists },
  ];
}

function pathPanel(runtime: RuntimeStatus | null): string {
  if (!runtime) {
    return `<p class="muted">No runtime paths yet.</p>`;
  }
  const rows = [
    ["App data", runtime.app_data_dir],
    ["Runtime", runtime.runtime_dir],
    ["Engine", runtime.engine_dir],
    ["Config", runtime.config_path],
    ["Python", runtime.python_path],
    ["uv", runtime.uv_path ?? "not found"],
  ];
  return `<div class="kv">${rows
    .map(
      ([key, value]) =>
        `<div class="row"><span class="k">${escapeHtml(key)}</span><span class="v">${escapeHtml(value)}</span></div>`,
    )
    .join("")}</div>`;
}

function networkPanel(): string {
  if (!state.network) {
    return `<p class="muted">Collect diagnostics to read local route, ARP, and configured RTSP targets.</p>`;
  }
  const targetRows = state.network.targets.length
    ? state.network.targets
        .map(
          (target) => `<details><summary>${escapeHtml(target.name)} ${escapeHtml(target.host)}</summary><pre>${escapeHtml(
            target.route,
          )}</pre></details>`,
        )
        .join("")
    : `<p class="muted">No camera RTSP targets were found in config.yaml.</p>`;
  return `
    <div class="network-grid">
      <details open><summary>Default route</summary><pre>${escapeHtml(state.network.default_route)}</pre></details>
      <details><summary>ARP</summary><pre>${escapeHtml(state.network.arp)}</pre></details>
    </div>
    <details><summary>Interfaces</summary><pre>${escapeHtml(state.network.interfaces)}</pre></details>
    ${targetRows}
  `;
}

function fieldCheckPanel(): string {
  const report = state.fieldChecks;
  if (!report) {
    return `<p class="muted">Run field checks to validate runtime readiness, config, camera routes, RTSP ports, and current process state.</p>`;
  }
  return `<div class="field-check-list">
    ${report.checks
      .map(
        (check) => `<div class="field-check-item ${escapeHtml(check.status)}">
          <span class="ck">${check.status === "ok" ? "OK" : check.status === "fail" ? "FAIL" : "WARN"}</span>
          <span class="body"><b>${escapeHtml(check.label)}</b><em>${escapeHtml(check.detail || check.meta)}</em></span>
          <span class="meta">${escapeHtml(check.meta)}</span>
        </div>`,
      )
      .join("")}
  </div>`;
}

function logRows(limit: number): string {
  if (!state.logs.length) {
    return `<p class="muted">No logs yet.</p>`;
  }
  return state.logs
    .slice(-limit)
    .map((log) => `<div class="log ${escapeHtml(log.stream)}"><span>${escapeHtml(log.stream)}</span>${escapeHtml(log.line)}</div>`)
    .join("");
}

function eventRows(): string {
  if (!state.events.length) {
    return `<p class="muted">No structured tracker-status events yet.</p>`;
  }
  return state.events
    .slice(-20)
    .reverse()
    .map((event) => {
      const { event: name, ts, ...payload } = event;
      return `<div class="event-row">
        <span class="event-name">${escapeHtml(name)}</span>
        <span class="event-ts">${formatTimestamp(ts)}</span>
        <code>${escapeHtml(JSON.stringify(payload))}</code>
      </div>`;
    })
    .join("");
}

function rightRail(setupReady: boolean): string {
  const latest = state.events[state.events.length - 1];
  return `
    <div class="stat-block">
      <h4 class="${state.process.running ? "live" : ""}"><span class="marker"></span>process</h4>
      <div class="big-num">${state.process.running ? "ON" : "OFF"}<span class="unit">tracker</span></div>
      <div class="micro-row"><span>exit</span><b>${state.process.exit_code ?? "-"}</b></div>
      <div class="micro-row"><span>runtime</span><b>${setupReady ? "ready" : "incomplete"}</b></div>
    </div>
    <div class="stat-block">
      <h4 class="green"><span class="marker"></span>runtime checks</h4>
      ${state.runtime ? runtimeChecks(state.runtime)
        .map(({ label, ok }) => `<div class="micro-row"><span>${escapeHtml(label)}</span><b>${ok ? "ok" : "missing"}</b></div>`)
        .join("") : `<p class="muted">Status pending.</p>`}
    </div>
    <div class="stat-block">
      <h4><span class="marker"></span>event summary</h4>
      <div class="micro-row"><span>structured</span><b>${state.events.length}</b></div>
      <div class="micro-row"><span>latest</span><b>${escapeHtml(latest?.event ?? "-")}</b></div>
      <div class="micro-row"><span>cameras</span><b>${cameraItems().length}</b></div>
      <div class="micro-row"><span>osc rate</span><b>${formatNumber(sumCameraNumber("osc_rate"))}/s</b></div>
    </div>
    <div class="stat-block">
      <h4><span class="marker"></span>paths</h4>
      <div class="micro-row path-row"><span>app</span><b>${escapeHtml(shortPath(state.runtime?.app_data_dir))}</b></div>
      <div class="micro-row path-row"><span>runtime</span><b>${escapeHtml(shortPath(state.runtime?.runtime_dir))}</b></div>
      <div class="micro-row path-row"><span>config</span><b>${escapeHtml(shortPath(state.runtime?.config_path))}</b></div>
    </div>
  `;
}

function buttonDisabled(condition = false): string {
  return condition || Boolean(state.busy) ? "disabled" : "";
}

async function handleAction(action: string): Promise<void> {
  try {
    state.error = null;
    if (action === "prepare") {
      await withBusy("Preparing runtime. This can take several minutes on first run.", async () => {
        state.runtime = await invoke<RuntimeStatus>("prepare_runtime");
        await refreshConfig();
        await refreshProjection();
      });
    } else if (action === "refresh") {
      await refreshAll();
    } else if (action === "start") {
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: false });
    } else if (action === "preview") {
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: true });
    } else if (action === "start-video-test" || action === "preview-video-test") {
      state.process = await invoke<ProcessStatus>("start_video_test", {
        request: {
          videoPath: state.videoPath.trim(),
          cameraName: "cam1",
          showPreview: action === "preview-video-test",
        },
      });
    } else if (action === "capture-calibration-frame" || action === "capture-video-calibration-frame") {
      state.calibrationFrame = await invoke<CalibrationFrame>("capture_calibration_frame", {
        request: {
          cameraName: state.calibrationCamera,
        },
      });
      state.calibrationCamera = state.calibrationFrame.camera;
      state.calibrationPoints = [];
    } else if (action === "save-calibration-points") {
      await invoke("save_calibration_points", {
        request: {
          cameraName: state.calibrationCamera,
          regionId: state.calibrationRegionId || defaultCalibrationRegionId(state.calibrationCamera),
          imagePoints: state.calibrationPoints,
        },
      });
      state.saved = true;
      await refreshConfig();
      await refreshProjection();
    } else if (action === "clear-calibration-points") {
      state.calibrationPoints = [];
    } else if (action === "stop") {
      state.process = await invoke<ProcessStatus>("stop_tracker");
    } else if (action === "network") {
      state.network = await invoke<NetworkReport>("collect_network_report");
    } else if (action === "field-checks") {
      state.fieldChecks = await invoke<FieldCheckReport>("run_field_checks");
    } else if (action === "projection-refresh") {
      await refreshProjection();
    } else if (action === "save-config") {
      await invoke("save_config", { request: { content: state.config } });
      state.saved = true;
      state.runtime = await invoke<RuntimeStatus>("get_runtime_status");
      await refreshProjection();
    } else if (action === "clear-logs") {
      state.logs = [];
    }
  } catch (error) {
    state.error = String(error);
  } finally {
    render();
  }
}

async function withBusy<T>(message: string, fn: () => Promise<T>): Promise<T> {
  state.busy = message;
  render();
  try {
    return await fn();
  } finally {
    state.busy = null;
  }
}

async function refreshConfig(): Promise<void> {
  try {
    state.config = await invoke<string>("read_config");
    state.saved = true;
  } catch {
    state.config = "";
  }
}

async function refreshProjection(): Promise<void> {
  try {
    state.projection = await invoke<ProjectionSnapshot>("read_projection_snapshot");
  } catch {
    state.projection = null;
  }
}

async function refreshAll(): Promise<void> {
  state.runtime = await invoke<RuntimeStatus>("get_runtime_status");
  state.process = await invoke<ProcessStatus>("tracker_status");
  await refreshConfig();
  await refreshProjection();
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    const entities: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    };
    return entities[char] ?? char;
  });
}

function escapeAttr(value: string): string {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function formatNumber(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(1) : "-";
}

function formatCoord(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  return Math.abs(number) >= 10 ? number.toFixed(0) : number.toFixed(3);
}

function formatTimestamp(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  return new Date(number * 1000).toLocaleTimeString();
}

function formatCheckTime(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) {
    return "-";
  }
  return new Date(number * 1000).toLocaleTimeString();
}

function sumCameraNumber(key: string): number {
  return cameraItems().reduce((total, cam) => {
    const value = Number(cam[key]);
    return Number.isFinite(value) ? total + value : total;
  }, 0);
}

function shortPath(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const parts = value.split("/");
  return parts.length > 3 ? `.../${parts.slice(-3).join("/")}` : value;
}

if (hasTauriRuntime) {
  void tauriListen<TrackerLog>("tracker-log", (event) => {
    state.logs.push(event.payload);
    if (state.logs.length > 500) {
      state.logs.splice(0, state.logs.length - 500);
    }
    requestRender();
  });

  void tauriListen<TrackerEvent>("tracker-status", (event) => {
    state.events.push(event.payload);
    if (state.events.length > 200) {
      state.events.splice(0, state.events.length - 200);
    }
    requestRender();
  });
} else {
  state.logs.push({
    stream: "preview",
    line: "Browser preview mode: open the Tauri desktop window for setup/start commands.",
  });
  state.events.push({
    event: "fps_tick",
    cameras: [
      { name: "cam0", fps: 0, osc_rate: 0, reconnects: 0, frame_age_s: null },
    ],
  });
}

setInterval(() => {
  void invoke<ProcessStatus>("tracker_status")
    .then((status) => {
      state.process = status;
      requestRender();
    })
    .catch(() => undefined);
}, 2500);

void refreshAll()
  .catch((error) => {
    state.error = String(error);
  })
  .finally(render);
