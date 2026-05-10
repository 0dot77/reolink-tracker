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

type MobileServerStatus = {
  running: boolean;
  bind: string;
  port: number | null;
  token: string;
  urls: string[];
  status_path: string;
  token_header: string;
  error: string | null;
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
  body_catch_points: number[][];
  relaxed_presence_points: number[][];
  relaxed_presence_uv: number[];
  relaxed_presence_margin_uv: number | null;
  relaxed_presence_min_confidence: number | null;
  relaxed_presence_v: number | null;
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
  uv: number[];
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

type UvPoint = { u: number; v: number };
type UvQuad = [UvPoint, UvPoint, UvPoint, UvPoint];
type WorkbenchMode = "inspect" | "canvas" | "fit" | "surface" | "warp";
type WorkbenchLayer = "projection" | "dispatch" | "zones" | "actors" | "surface" | "warp" | "links";
type WorkbenchHandleKind = "region" | "surface" | "warp";
type WorkbenchSelection = { kind: WorkbenchHandleKind; index: number; key?: string } | null;
type WorkbenchCanvasSize = { width: number; height: number; lockedAspect: boolean };

type CalibrationFrame = {
  camera: string;
  path: string;
  width: number;
  height: number;
};

type CalibrationTool = "floor" | "body" | "stair";
type CalibrationMappingDraft = {
  key: string;
  projectionUMin: string;
  projectionUMax: string;
  projectionVMin: string;
  projectionVMax: string;
  dispatchUMin: string;
  dispatchUMax: string;
  dispatchVMin: string;
  dispatchVMax: string;
  stairRelaxedUMin: string;
  stairRelaxedUMax: string;
  stairRelaxedVMin: string;
  stairRelaxedVMax: string;
  stairFixedV: string;
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
  mobile: MobileServerStatus | null;
  process: ProcessStatus;
  config: string;
  logs: TrackerLog[];
  events: TrackerEvent[];
  network: NetworkReport | null;
  fieldChecks: FieldCheckReport | null;
  projection: ProjectionSnapshot | null;
  videoPath: string;
  videoTestCamera: string;
  calibrationCamera: string;
  calibrationRegionId: string;
  calibrationTool: CalibrationTool;
  calibrationFrame: CalibrationFrame | null;
  calibrationPoints: number[][];
  calibrationDraftActive: boolean;
  calibrationMappingDraft: CalibrationMappingDraft | null;
  workbenchCanvasSize: WorkbenchCanvasSize;
  workbenchCanvasTouched: boolean;
  workbenchRegionKey: string;
  draftRegionProjection: Record<string, UvQuad>;
  draftUsableSurface: Record<string, UvQuad>;
  draftInteractionWarp: Record<string, UvQuad>;
  workbenchView: {
    mode: WorkbenchMode;
    selectedHandle: WorkbenchSelection;
    visibleLayers: Record<WorkbenchLayer, boolean>;
  };
  sectionScrollTop: Partial<Record<SectionId, number>>;
  busy: string | null;
  error: string | null;
  saved: boolean;
} = {
  section: "live",
  runtime: null,
  mobile: null,
  process: { running: false, exit_code: null },
  config: "",
  logs: [],
  events: [],
  network: null,
  fieldChecks: null,
  projection: null,
  videoPath: "/Users/taeyang/Desktop/VomReo01-01-211808-211942.mp4",
  videoTestCamera: "cam2",
  calibrationCamera: "cam0",
  calibrationRegionId: "",
  calibrationTool: "floor",
  calibrationFrame: null,
  calibrationPoints: [],
  calibrationDraftActive: false,
  calibrationMappingDraft: null,
  workbenchCanvasSize: { width: 9600, height: 1080, lockedAspect: true },
  workbenchCanvasTouched: false,
  workbenchRegionKey: "",
  draftRegionProjection: {},
  draftUsableSurface: {},
  draftInteractionWarp: {},
  workbenchView: {
    mode: "inspect",
    selectedHandle: null,
    visibleLayers: {
      projection: true,
      dispatch: true,
      zones: true,
      actors: true,
      surface: true,
      warp: true,
      links: true,
    },
  },
  sectionScrollTop: {},
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
let workbenchDrag: {
  kind: WorkbenchHandleKind;
  index: number;
  projectionId: string;
  startUv: UvPoint;
  originQuad: UvQuad;
  startClientX: number;
  startClientY: number;
  rect: DOMRect;
} | null = null;
let pendingWorkbenchRegionPersistKey: string | null = null;
let pendingWorkbenchRegionPersistRefresh = false;
let workbenchRegionPersistTimer: number | null = null;
let workbenchRegionPersistInFlight = false;
let workbenchCanvasEditStart: WorkbenchCanvasSize | null = null;

function isConfigEditorActive(): boolean {
  return state.section === "config" && document.activeElement?.id === "configEditor";
}

function isCalibrationMappingInputActive(): boolean {
  if (state.section !== "calibration") {
    return false;
  }
  return [
    "projectionUMin",
    "projectionUMax",
    "projectionVMin",
    "projectionVMax",
    "dispatchUMin",
    "dispatchUMax",
    "dispatchVMin",
    "dispatchVMax",
    "stairRelaxedUMin",
    "stairRelaxedUMax",
    "stairRelaxedVMin",
    "stairRelaxedVMax",
    "stairFixedV",
  ].includes(String(document.activeElement?.id ?? ""));
}

function isWorkbenchCanvasInputActive(): boolean {
  if (state.section !== "projection") {
    return false;
  }
  return [
    "workbenchWidth",
    "workbenchHeight",
    "workbenchPreset",
  ].includes(String(document.activeElement?.id ?? ""));
}

function requestRender(): void {
  if (isConfigEditorActive() || isCalibrationMappingInputActive() || isWorkbenchCanvasInputActive()) {
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
      const name = String(cam.name ?? "");
      const camClass = cameraClassForName(name, index);
      return `<tr>
        <td><span class="cam-swatch ${camClass}"></span>${escapeHtml(name)}</td>
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
  const previousMain = root.querySelector<HTMLElement>(".main");
  if (previousMain) {
    state.sectionScrollTop[state.section] = previousMain.scrollTop;
  }

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
          ${navButton("mobile", "Mobile", state.mobile?.port ? String(state.mobile.port) : "off")}
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

  const main = root.querySelector<HTMLElement>(".main");
  if (main) {
    main.scrollTop = state.sectionScrollTop[state.section] ?? 0;
    main.addEventListener("scroll", () => {
      state.sectionScrollTop[state.section] = main.scrollTop;
    }, { passive: true });
  }
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
  const videoTestCamera = root.querySelector<HTMLSelectElement>("#videoTestCamera");
  videoTestCamera?.addEventListener("change", () => {
    state.videoTestCamera = videoTestCamera.value;
  });
  const calibrationCamera = root.querySelector<HTMLSelectElement>("#calibrationCamera");
  calibrationCamera?.addEventListener("change", () => {
    state.calibrationCamera = calibrationCamera.value;
    state.calibrationRegionId = firstRegionIdForCamera(calibrationCamera.value);
    state.calibrationFrame = null;
    state.calibrationPoints = [];
    state.calibrationDraftActive = false;
    state.calibrationMappingDraft = null;
    render();
  });
  const calibrationRegion = root.querySelector<HTMLSelectElement>("#calibrationRegion");
  calibrationRegion?.addEventListener("change", () => {
    state.calibrationRegionId = calibrationRegion.value;
    state.calibrationPoints = [];
    state.calibrationDraftActive = false;
    state.calibrationMappingDraft = null;
    render();
  });
  root.querySelectorAll<HTMLButtonElement>("button[data-calibration-tool]").forEach((button) => {
    button.addEventListener("click", () => {
      const tool = button.dataset.calibrationTool;
      state.calibrationTool = tool === "stair" || tool === "body" ? tool : "floor";
      state.calibrationPoints = [];
      state.calibrationDraftActive = false;
      render();
    });
  });
  const calibrationFrame = root.querySelector<HTMLElement>("#calibrationFrame");
  calibrationFrame?.addEventListener("click", (event) => {
    if (!state.calibrationFrame) {
      return;
    }
    const rect = calibrationFrame.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * state.calibrationFrame.width;
    const y = ((event.clientY - rect.top) / rect.height) * state.calibrationFrame.height;
    state.calibrationPoints = nextCalibrationPoints([Math.round(x), Math.round(y)]);
    state.calibrationDraftActive = true;
    render();
  });
  root.querySelectorAll<HTMLInputElement>(".mapping-grid input").forEach((input) => {
    input.addEventListener("input", () => {
      updateCalibrationMappingDraft(input);
      state.saved = false;
    });
  });
  root.querySelectorAll<HTMLButtonElement>("button[data-workbench-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.workbenchMode as WorkbenchMode | undefined;
      if (mode === "inspect" || mode === "canvas" || mode === "fit" || mode === "surface" || mode === "warp") {
        state.workbenchView.mode = mode;
        const activeKind = mode === "fit" ? "region" : mode;
        if (mode === "inspect" || mode === "canvas" || state.workbenchView.selectedHandle?.kind !== activeKind) {
          state.workbenchView.selectedHandle = null;
        }
        render();
      }
    });
  });
  const workbenchRegion = root.querySelector<HTMLSelectElement>("#workbenchRegion");
  workbenchRegion?.addEventListener("change", () => {
    state.workbenchRegionKey = workbenchRegion.value;
    state.workbenchView.selectedHandle = null;
    render();
  });
  root.querySelectorAll<HTMLInputElement>("input[data-workbench-layer]").forEach((input) => {
    input.addEventListener("change", () => {
      const layer = input.dataset.workbenchLayer as WorkbenchLayer | undefined;
      if (layer && layer in state.workbenchView.visibleLayers) {
        state.workbenchView.visibleLayers[layer] = input.checked;
        render();
      }
    });
  });
  const workbenchWidth = root.querySelector<HTMLInputElement>("#workbenchWidth");
  workbenchWidth?.addEventListener("focus", beginWorkbenchCanvasEdit);
  workbenchWidth?.addEventListener("input", () => {
    updateWorkbenchCanvasInput("width", workbenchWidth.value);
    refreshWorkbenchCanvasDom();
  });
  workbenchWidth?.addEventListener("change", () => {
    updateWorkbenchCanvasInput("width", workbenchWidth.value);
    workbenchCanvasEditStart = null;
    render();
  });
  workbenchWidth?.addEventListener("blur", () => {
    workbenchCanvasEditStart = null;
    if (deferredRender) {
      window.setTimeout(requestRender, 0);
    }
  });
  const workbenchHeight = root.querySelector<HTMLInputElement>("#workbenchHeight");
  workbenchHeight?.addEventListener("focus", beginWorkbenchCanvasEdit);
  workbenchHeight?.addEventListener("input", () => {
    updateWorkbenchCanvasInput("height", workbenchHeight.value);
    refreshWorkbenchCanvasDom();
  });
  workbenchHeight?.addEventListener("change", () => {
    updateWorkbenchCanvasInput("height", workbenchHeight.value);
    workbenchCanvasEditStart = null;
    render();
  });
  workbenchHeight?.addEventListener("blur", () => {
    workbenchCanvasEditStart = null;
    if (deferredRender) {
      window.setTimeout(requestRender, 0);
    }
  });
  const workbenchAspectLock = root.querySelector<HTMLInputElement>("#workbenchAspectLock");
  workbenchAspectLock?.addEventListener("change", () => {
    state.workbenchCanvasSize.lockedAspect = workbenchAspectLock.checked;
    state.workbenchCanvasTouched = true;
    render();
  });
  const workbenchPreset = root.querySelector<HTMLSelectElement>("#workbenchPreset");
  workbenchPreset?.addEventListener("change", () => {
    applyWorkbenchPreset(workbenchPreset.value);
    render();
  });
  root.querySelectorAll<HTMLButtonElement>(".wb-handle").forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      beginWorkbenchDrag(event, handle);
    });
  });
}

function navButton(section: SectionId, label: string, badge: string): string {
  return `<button class="nav-item ${state.section === section ? "active" : ""}" data-section="${section}">
    <span class="nav-dot"></span>
    <span>${label}</span>
    <span class="badge">${escapeHtml(badge)}</span>
  </button>`;
}

function beginWorkbenchDrag(event: PointerEvent, handle: HTMLButtonElement): void {
  const kind = handle.dataset.handleKind as WorkbenchHandleKind | undefined;
  if (kind !== "region" && kind !== "surface" && kind !== "warp") {
    return;
  }
  const activeKind = state.workbenchView.mode === "fit" ? "region" : state.workbenchView.mode;
  if (activeKind !== kind) {
    state.workbenchView.selectedHandle = { kind, index: Number(handle.dataset.handleIndex ?? 0), key: handle.dataset.projectionId };
    render();
    return;
  }
  const index = Number(handle.dataset.handleIndex ?? 0);
  const projectionId = handle.dataset.projectionId ?? state.projection?.projections[0]?.id ?? "corridor";
  const canvas = root.querySelector<HTMLElement>("#workbenchCanvas");
  if (!canvas || !Number.isInteger(index) || index < 0 || index > 3) {
    return;
  }
  event.preventDefault();
  handle.setPointerCapture?.(event.pointerId);
  const rect = canvas.getBoundingClientRect();
  const originQuad = cloneQuad(draftQuad(kind, projectionId));
  workbenchDrag = {
    kind,
    index,
    projectionId,
    startUv: { ...originQuad[index] },
    originQuad,
    startClientX: event.clientX,
    startClientY: event.clientY,
    rect,
  };
  state.workbenchView.selectedHandle = { kind, index, key: projectionId };
  render();
}

function updateWorkbenchDrag(event: PointerEvent): void {
  if (!workbenchDrag) {
    return;
  }
  event.preventDefault();
  const drag = workbenchDrag;
  const fine = event.altKey ? 0.25 : 1;
  let du = ((event.clientX - drag.startClientX) / Math.max(1, drag.rect.width)) * fine;
  let dv = ((event.clientY - drag.startClientY) / Math.max(1, drag.rect.height)) * fine;
  if (event.shiftKey) {
    if (Math.abs(du) >= Math.abs(dv)) {
      dv = 0;
    } else {
      du = 0;
    }
  }
  const next = {
    u: drag.startUv.u + du,
    v: drag.startUv.v + dv,
  };
  setDraftHandle(drag.kind, drag.projectionId, drag.index, next);
  if (drag.kind === "region") {
    scheduleWorkbenchRegionPersist(drag.projectionId);
  }
  requestAnimationFrame(render);
}

function endWorkbenchDrag(): void {
  const ended = workbenchDrag;
  workbenchDrag = null;
  if (ended?.kind === "region") {
    pendingWorkbenchRegionPersistKey = ended.projectionId;
    void flushWorkbenchRegionPersist(true);
  }
}

function cancelWorkbenchDrag(): void {
  if (!workbenchDrag) {
    return;
  }
  setDraftQuad(workbenchDrag.kind, workbenchDrag.projectionId, cloneQuad(workbenchDrag.originQuad));
  if (workbenchDrag.kind === "region") {
    pendingWorkbenchRegionPersistKey = workbenchDrag.projectionId;
    void flushWorkbenchRegionPersist(true);
  }
  workbenchDrag = null;
  render();
}

function handleWorkbenchKeyDown(event: KeyboardEvent): void {
  if (state.section !== "projection") {
    return;
  }
  const tag = (document.activeElement?.tagName ?? "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") {
    return;
  }
  if (event.key === "Escape") {
    if (workbenchDrag) {
      event.preventDefault();
      cancelWorkbenchDrag();
    }
    return;
  }
  const selection = state.workbenchView.selectedHandle;
  if (!selection || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    return;
  }
  event.preventDefault();
  const projectionId = selection.key ?? state.projection?.projections[0]?.id ?? "corridor";
  const quad = draftQuad(selection.kind, projectionId);
  const point = quad[selection.index];
  const pxStep = event.altKey ? 0.2 : event.shiftKey ? 10 : 1;
  const du = pxStep / Math.max(1, state.workbenchCanvasSize.width);
  const dv = pxStep / Math.max(1, state.workbenchCanvasSize.height);
  const next = { ...point };
  if (event.key === "ArrowLeft") {
    next.u -= du;
  } else if (event.key === "ArrowRight") {
    next.u += du;
  } else if (event.key === "ArrowUp") {
    next.v -= dv;
  } else if (event.key === "ArrowDown") {
    next.v += dv;
  }
  setDraftHandle(selection.kind, projectionId, selection.index, next);
  if (selection.kind === "region") {
    scheduleWorkbenchRegionPersist(projectionId);
  }
  render();
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
  const videoCameras = configuredCameraNames();
  if (!videoCameras.includes(state.videoTestCamera)) {
    state.videoTestCamera = videoCameras.includes("cam2") ? "cam2" : videoCameras[0] ?? "cam0";
  }
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
            <label class="video-test-camera">Camera ${selectHtml("videoTestCamera", videoCameras, state.videoTestCamera)}</label>
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
  const projectionId = projection?.id ?? "corridor";
  const projectionRegions = regions.filter((region) => region.projection_id === projectionId || !projection);
  const selectedFitRegion = selectedWorkbenchRegion(projectionRegions);
  const zones = projection?.zones ?? [];
  const canvas = state.workbenchCanvasSize;
  const mode = state.workbenchView.mode;
  return `
    <section class="panel proj-wrap workbench-wrap">
      <div class="panel-head">
        <h3>projection workbench</h3>
        <span class="sub">${escapeHtml(projection?.id ?? "config projection")} · field pixel preview</span>
        <div class="actions">
          <button class="btn" data-action="projection-refresh" ${buttonDisabled()}>Reload</button>
          <span class="pill ok">draft only</span>
        </div>
      </div>
      <div class="workbench-toolbar">
        <div class="tool-switch" role="group" aria-label="Workbench mode">
          ${workbenchModeButton("inspect", "Inspect", mode)}
          ${workbenchModeButton("canvas", "Canvas", mode)}
          ${workbenchModeButton("fit", "Camera Fit", mode)}
          ${workbenchModeButton("surface", "Interactive Area", mode)}
          ${workbenchModeButton("warp", "Output Warp", mode)}
        </div>
        <div class="canvas-controls">
          <label>Width <input id="workbenchWidth" type="number" min="128" max="50000" step="1" value="${escapeAttr(String(canvas.width))}"></label>
          <label>Height <input id="workbenchHeight" type="number" min="128" max="50000" step="1" value="${escapeAttr(String(canvas.height))}"></label>
          <label class="check-control"><input id="workbenchAspectLock" type="checkbox" ${canvas.lockedAspect ? "checked" : ""}> Lock ratio</label>
          <label>Preset ${workbenchPresetSelect(projection)}</label>
          <button class="btn" data-action="workbench-reset-canvas">Reset to config</button>
        </div>
        ${mode === "fit" ? workbenchRegionControl(projectionRegions, selectedFitRegion) : ""}
        ${workbenchModeGuide(mode)}
        ${workbenchLayerControls()}
      </div>
      <div class="proj-meta">
        <span>projection <b>${escapeHtml(projection?.id ?? "-")}</b></span>
        <span>world <b>${formatVector(projection?.world_size_m, "m")}</b></span>
        <span>pixels <b id="workbenchPixelsValue">${canvas.width} x ${canvas.height} px</b></span>
        <span>config <b>${formatVector(projection?.pixel_size, "px")}</b></span>
        <span>cameras <b>${snapshot?.camera_count ?? cameraNames().length}</b></span>
        <span>regions <b>${regions.length}</b></span>
        <span>stairs <b>${regions.filter((region) => region.relaxed_presence_points?.length).length}</b></span>
      </div>
      <div class="workbench-shell">
        <div class="workbench-stage">
          <div
            id="workbenchCanvas"
            class="proj-canvas workbench-canvas mode-${escapeAttr(mode)}"
            data-projection-id="${escapeAttr(projectionId)}"
            style="--canvas-aspect:${canvasAspect(canvas)};aspect-ratio:${Math.max(1, canvas.width)} / ${Math.max(1, canvas.height)}"
          >
            ${projection ? workbenchOverlays(projection, projectionRegions, selectedFitRegion) : `<div class="empty-projection">No projection in config.yaml yet.</div>`}
          </div>
        </div>
        <aside class="workbench-readout">
          ${workbenchReadout(projection)}
        </aside>
      </div>
    </section>
    <section class="metric-grid">
      ${validationCard("projection config", projections.length ? `${projections.length}` : "none", projections.length ? "ok" : "warn", "read from config.yaml projections[]")}
      ${validationCard("calibrated regions", regions.length ? `${regions.length}` : "none", regions.length ? "ok" : "warn", "camera regions with projection_uv and dispatch_uv")}
      ${validationCard("interaction zones", zones.length ? `${zones.length}` : "none", zones.length ? "ok" : "warn", "read-only zone overlay from interaction_zones[]")}
      ${validationCard("stair relaxed masks", regions.some((region) => region.relaxed_presence_points?.length) ? `${regions.filter((region) => region.relaxed_presence_points?.length).length}` : "none", regions.some((region) => region.relaxed_presence_points?.length) ? "ok" : "warn", "camera-image polygons used only for relaxed seated-person detection")}
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
  const points = state.calibrationDraftActive
    ? state.calibrationPoints
    : calibrationToolPoints(selectedRegion);
  const toolLabel = calibrationToolLabel(state.calibrationTool);
  const pointsLabel = calibrationToolPointsLabel(state.calibrationTool);
  const saveDisabled = state.calibrationPoints.length !== 4 || (state.calibrationTool !== "floor" && !selectedRegion);
  const frame = state.calibrationFrame;
  const projectionUv = normalizedUv(selectedRegion?.projection_uv, [0, 0, 1, 1]);
  const dispatchUv = normalizedUv(selectedRegion?.dispatch_uv, projectionUv);
  const stairRelaxedUv = normalizedUv(selectedRegion?.relaxed_presence_uv, projectionUv);
  const stairV = selectedRegion?.relaxed_presence_v ?? null;
  const mappingKey = calibrationMappingKey(state.calibrationCamera, selectedRegion?.id ?? state.calibrationRegionId);
  const frameSrc = frame && hasTauriRuntime ? convertFileSrc(frame.path) : "";
  const frameStyle = frame
    ? `aspect-ratio:${Math.max(1, frame.width)} / ${Math.max(1, frame.height)};min-height:0`
    : "";
  return `
    <section class="calib-layout">
      <article class="panel">
        <div class="panel-head">
          <h3>4pt</h3>
          <span class="sub">${toolLabel} · click four camera-image points</span>
          <div class="actions">
            <button class="btn" data-action="capture-calibration-frame" ${buttonDisabled(!isSetupReady())}>Capture</button>
            <button class="btn" data-action="capture-video-calibration-frame" ${buttonDisabled(!isSetupReady())}>Use live video</button>
          </div>
        </div>
        <div class="calib-toolbar">
          <label>Camera ${selectHtml("calibrationCamera", cameras, state.calibrationCamera)}</label>
          <label>Region ${selectHtml("calibrationRegion", regions.map((region) => region.id), state.calibrationRegionId)}</label>
          <div class="tool-switch" role="group" aria-label="Calibration tool">
            <button class="btn ${state.calibrationTool === "floor" ? "active" : ""}" data-calibration-tool="floor">Floor UV</button>
            <button class="btn ${state.calibrationTool === "body" ? "active body" : ""}" data-calibration-tool="body">Body catch</button>
            <button class="btn ${state.calibrationTool === "stair" ? "active stair" : ""}" data-calibration-tool="stair">Stair relaxed</button>
          </div>
          <button class="btn" data-action="clear-calibration-points" ${buttonDisabled(!points.length)}>Clear points</button>
          <button class="btn primary" data-action="save-calibration-points" ${buttonDisabled(saveDisabled)}>Save ${toolLabel}</button>
        </div>
        <div id="calibrationFrame" class="calib-frame ${frameSrc ? "has-image" : ""}" style="${frameStyle}">
          ${frameSrc && frame ? `<img class="calib-image" src="${escapeAttr(frameSrc)}" alt="${escapeAttr(frame.camera)} calibration frame" draggable="false">` : `<div class="frame-grid"></div><div class="future-note">Capture a live camera frame, then click four projection corners in order.</div>`}
          ${frame ? calibrationRegionOverlay(selectedRegion, frame, state.calibrationTool) : ""}
          ${frame && points.length ? calibrationPointMarkers(points, frame, state.calibrationTool).join("") : ""}
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>mapping</h3><span class="sub">projection/dispatch ownership in UV space</span></div>
        <div class="panel-body">
          <div class="checks">
            <span class="${isSetupReady() ? "ok" : "missing"}">runtime: ${isSetupReady() ? "ready" : "missing"}</span>
            <span class="${points.length === 4 ? "ok" : "missing"}">${pointsLabel}: ${points.length}/4</span>
          </div>
          <div class="mapping-help">
            <b>Projection</b> observes and holds tracks for handoff. <b>Dispatch</b> creates gid/OSC actors.
            U splits left-to-right camera ownership; V controls depth rows.
          </div>
          <div class="mapping-grid">
            <label>Projection U min <small>left observe edge</small><input id="projectionUMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "projectionUMin", formatUvNumber(projectionUv[0])))}"></label>
            <label>Projection U max <small>right observe edge</small><input id="projectionUMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "projectionUMax", formatUvNumber(projectionUv[2])))}"></label>
            <label>Projection V min <small>far/top observe row</small><input id="projectionVMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "projectionVMin", formatUvNumber(projectionUv[1])))}"></label>
            <label>Projection V max <small>near/bottom observe row</small><input id="projectionVMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "projectionVMax", formatUvNumber(projectionUv[3])))}"></label>
            <label>Dispatch U min <small>left gid edge</small><input id="dispatchUMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "dispatchUMin", formatUvNumber(dispatchUv[0])))}"></label>
            <label>Dispatch U max <small>right gid edge</small><input id="dispatchUMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "dispatchUMax", formatUvNumber(dispatchUv[2])))}"></label>
            <label>Dispatch V min <small>far/top gid row</small><input id="dispatchVMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "dispatchVMin", formatUvNumber(dispatchUv[1])))}"></label>
            <label>Dispatch V max <small>near/bottom gid row</small><input id="dispatchVMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "dispatchVMax", formatUvNumber(dispatchUv[3])))}"></label>
            <label>Stair U min <small>left relaxed warp edge</small><input id="stairRelaxedUMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "stairRelaxedUMin", formatUvNumber(stairRelaxedUv[0])))}"></label>
            <label>Stair U max <small>right relaxed warp edge</small><input id="stairRelaxedUMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "stairRelaxedUMax", formatUvNumber(stairRelaxedUv[2])))}"></label>
            <label>Stair V min <small>far/top relaxed warp row</small><input id="stairRelaxedVMin" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "stairRelaxedVMin", formatUvNumber(stairRelaxedUv[1])))}"></label>
            <label>Stair V max <small>near/bottom relaxed warp row</small><input id="stairRelaxedVMax" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "stairRelaxedVMax", formatUvNumber(stairRelaxedUv[3])))}"></label>
            <label>Stair fixed v <input id="stairFixedV" type="number" min="0" max="1" step="0.01" value="${escapeAttr(mappingDraftValue(mappingKey, "stairFixedV", stairV == null ? "" : formatUvNumber(stairV)))}" placeholder="optional"></label>
            <button class="btn primary" data-action="save-calibration-mapping" ${buttonDisabled(!selectedRegion)}>Save mapping</button>
          </div>
          <div class="kv">
            <div class="row"><span class="k">camera</span><span class="v">${escapeHtml(state.calibrationCamera)}</span></div>
            <div class="row"><span class="k">region</span><span class="v">${escapeHtml(state.calibrationRegionId)}</span></div>
            <div class="row"><span class="k">tool</span><span class="v">${escapeHtml(toolLabel)}</span></div>
            <div class="row"><span class="k">points</span><span class="v">${escapeHtml(formatImagePoints(points))}</span></div>
            <div class="row"><span class="k">projection uv</span><span class="v">${escapeHtml(formatUvRange(selectedRegion?.projection_uv ?? []))}</span></div>
            <div class="row"><span class="k">dispatch uv</span><span class="v">${escapeHtml(formatUvRange(selectedRegion?.dispatch_uv ?? []))}</span></div>
            <div class="row"><span class="k">stair relaxed uv</span><span class="v">${escapeHtml(formatUvRange(stairRelaxedUv))}</span></div>
            <div class="row"><span class="k">body catch</span><span class="v">${escapeHtml(formatImagePoints(selectedRegion?.body_catch_points ?? []))}</span></div>
            <div class="row"><span class="k">stair mask</span><span class="v">${escapeHtml(formatImagePoints(selectedRegion?.relaxed_presence_points ?? []))}</span></div>
            <div class="row"><span class="k">stair margin/conf</span><span class="v">${escapeHtml(formatOptionalNumber(selectedRegion?.relaxed_presence_margin_uv))} / ${escapeHtml(formatOptionalNumber(selectedRegion?.relaxed_presence_min_confidence))}</span></div>
            <div class="row"><span class="k">stair fixed v</span><span class="v">${stairV == null ? "not set" : escapeHtml(formatUvNumber(stairV))}</span></div>
            <div class="row"><span class="k">save path</span><span class="v">${escapeHtml(shortPath(state.runtime?.config_path))}</span></div>
            <div class="row"><span class="k">frame</span><span class="v">${frame ? `${frame.width} x ${frame.height}` : "not captured"}</span></div>
          </div>
          <p class="muted block-copy setup-copy">${calibrationToolDescription(state.calibrationTool)}</p>
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
        ${statusTile("current stream", "active id list plus packed projection xy/uv triples", "live")}
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
  const mobile = state.mobile;
  const setupReady = isSetupReady();
  const status = mobile?.running ? "serving" : mobile?.error ? "error" : "starting";
  const urlRows = mobile?.urls.length
    ? mobile.urls.map((url) => `<div class="mobile-url">${escapeHtml(url)}</div>`).join("")
    : `<p class="muted">Mobile URL is pending. Refresh after the desktop app finishes starting.</p>`;
  return `
    <section class="mobile-ops">
      <article class="panel">
        <div class="panel-head">
          <h3>LAN mobile control</h3>
          <span class="sub">status plus PIN-protected Start/Stop</span>
          <div class="actions"><button class="btn" data-action="mobile-refresh" ${buttonDisabled()}>Refresh</button></div>
        </div>
        <div class="panel-body">
          <div class="mobile-status-grid">
            ${trafficTile("server", mobile?.running ? "ok" : "warn", status)}
            ${trafficTile("port", mobile?.port ? "ok" : "warn", mobile?.port ? String(mobile.port) : "-")}
            ${trafficTile("runtime", setupReady ? "ok" : "warn", setupReady ? "ready" : "setup")}
            ${trafficTile("tracker", state.process.running ? "ok" : "warn", state.process.running ? "running" : "stopped")}
          </div>
          ${mobile?.error ? `<div class="banner error mobile-error">${escapeHtml(mobile.error)}</div>` : ""}
          <div class="mobile-pin-box">
            <span>PIN</span>
            <b>${escapeHtml(mobile?.token ?? "-")}</b>
            <em>${escapeHtml(mobile?.token_header ?? "X-Reolink-Mobile-Token")} header is required for every API request.</em>
          </div>
          <div class="mobile-url-list">
            <span class="label">Open on phone</span>
            ${urlRows}
          </div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>connection checks</h3><span class="sub">when the phone cannot reach the page</span></div>
        <div class="panel-body mobile-checks">
          <div class="field-check-item ${mobile?.running ? "ok" : "warn"}">
            <span class="ck">${mobile?.running ? "OK" : "WAIT"}</span>
            <span class="body"><b>Desktop app running</b><em>The mobile server exists only while this Tauri app is open.</em></span>
            <span class="meta">${escapeHtml(mobile?.bind ?? "0.0.0.0")}</span>
          </div>
          <div class="field-check-item warn">
            <span class="ck">LAN</span>
            <span class="body"><b>Same Wi-Fi or wired LAN</b><em>The phone must be on the same local network as this Mac.</em></span>
            <span class="meta">local only</span>
          </div>
          <div class="field-check-item warn">
            <span class="ck">FW</span>
            <span class="body"><b>macOS firewall</b><em>Allow incoming connections for Reolink Tracker if macOS prompts.</em></span>
            <span class="meta">port ${escapeHtml(String(mobile?.port ?? "1421-1430"))}</span>
          </div>
          <div class="field-check-item warn">
            <span class="ck">PIN</span>
            <span class="body"><b>Status is private</b><em>The phone shows only the PIN entry screen until the correct PIN is entered.</em></span>
            <span class="meta">required</span>
          </div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><h3>mobile API</h3><span class="sub">v1 scope</span></div>
        <div class="panel-body future-grid mobile-api-grid">
          ${statusTile("GET /mobile", "Phone-first status and emergency control page.", "live")}
          ${statusTile("GET /api/status", "Runtime, process, camera, OSC, event, and log summary after PIN.", "live")}
          ${statusTile("GET /api/preview/<cam>.jpg", "Low-rate preview frame served only while mobile View is active.", "live")}
          ${statusTile("POST /api/start", "Starts tracker headless through the same desktop process supervisor.", "live")}
          ${statusTile("POST /api/stop", "Stops the shared tracker process. Preview and calibration stay desktop-only.", "live")}
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

function workbenchModeButton(mode: WorkbenchMode, label: string, active: WorkbenchMode): string {
  return `<button class="btn ${active === mode ? "active" : ""}" data-workbench-mode="${mode}">${escapeHtml(label)}</button>`;
}

function workbenchModeGuide(mode: WorkbenchMode): string {
  const copy: Record<WorkbenchMode, { title: string; body: string }> = {
    inspect: {
      title: "Inspect",
      body: "Check configured camera regions, interaction zones, and live actor points. Editing handles stay hidden.",
    },
    canvas: {
      title: "Canvas Size",
      body: "Set the field pixel size used by readouts. The drawing stays scaled to this panel.",
    },
    fit: {
      title: "Camera Fit",
      body: "Select any camera region and drag its projection rect. Changes are saved live while dragging.",
    },
    surface: {
      title: "Interactive Area",
      body: "Edit the usable interactive surface only. Output warp is hidden while this target is active.",
    },
    warp: {
      title: "Output Warp",
      body: "Edit the final draft warp for raw actor points. Interactive area is hidden to avoid overlapping handles.",
    },
  };
  return `<div class="mode-guide">
    <b>${escapeHtml(copy[mode].title)}</b>
    <span>${escapeHtml(copy[mode].body)}</span>
  </div>`;
}

function workbenchRegionControl(regions: RegionInfo[], selected: RegionInfo | undefined): string {
  if (!regions.length) {
    return `<div class="fit-controls"><span class="muted">No camera regions available.</span></div>`;
  }
  return `<div class="fit-controls">
    <label>Camera region ${selectHtml("workbenchRegion", regions.map(regionKey), selected ? regionKey(selected) : regionKey(regions[0]))}</label>
    <span>${selected ? `${escapeHtml(selected.camera)} · floor ${selected.image_points?.length ?? 0}pt · stair ${selected.relaxed_presence_points?.length ?? 0}pt` : ""}</span>
  </div>`;
}

function workbenchLayerControls(): string {
  return `<details class="layer-controls">
    <summary>Reference layers</summary>
    <div class="layer-control-grid" aria-label="Layer visibility">
      ${workbenchLayerToggle("projection", "projection")}
      ${workbenchLayerToggle("dispatch", "dispatch")}
      ${workbenchLayerToggle("zones", "zones")}
      ${workbenchLayerToggle("actors", "actors")}
      ${workbenchLayerToggle("surface", "surface")}
      ${workbenchLayerToggle("warp", "warp")}
      ${workbenchLayerToggle("links", "links")}
    </div>
  </details>`;
}

function workbenchLayerToggle(layer: WorkbenchLayer, label: string): string {
  const checked = state.workbenchView.visibleLayers[layer] ? "checked" : "";
  return `<label class="check-control"><input type="checkbox" data-workbench-layer="${layer}" ${checked}> ${escapeHtml(label)}</label>`;
}

function workbenchPresetSelect(projection: ProjectionInfo | undefined): string {
  const selected = workbenchPresetValue(projection);
  const config = projectionConfigCanvasSize(projection);
  const fixed = [
    { label: "9600 x 1080", width: 9600, height: 1080 },
    { label: "1920 x 1080", width: 1920, height: 1080 },
    { label: "3840 x 1080", width: 3840, height: 1080 },
  ];
  return `<select id="workbenchPreset">
    <option value="config" ${selected === "config" ? "selected" : ""}>current config (${config.width} x ${config.height})</option>
    ${fixed.map((preset) => `<option value="${preset.width}x${preset.height}" ${selected === `${preset.width}x${preset.height}` ? "selected" : ""}>${preset.label}</option>`).join("")}
    <option value="custom" ${selected === "custom" ? "selected" : ""}>custom</option>
  </select>`;
}

function workbenchPresetValue(projection: ProjectionInfo | undefined): string {
  const config = projectionConfigCanvasSize(projection);
  const current = state.workbenchCanvasSize;
  const fixed = [
    { width: 9600, height: 1080 },
    { width: 1920, height: 1080 },
    { width: 3840, height: 1080 },
  ];
  const selectedFixed = fixed.find((preset) => preset.width === current.width && preset.height === current.height);
  if (current.width === config.width && current.height === config.height) {
    return "config";
  }
  return selectedFixed ? `${selectedFixed.width}x${selectedFixed.height}` : "custom";
}

function workbenchOverlays(projection: ProjectionInfo, regions: RegionInfo[], selectedFitRegion: RegionInfo | undefined): string {
  const layers = effectiveWorkbenchLayers();
  const projectionId = projection.id;
  const surface = draftQuad("surface", projectionId);
  const warp = draftQuad("warp", projectionId);
  const actorPairs = workbenchActorPairs(projectionId, warp);
  const fitKey = selectedFitRegion ? regionKey(selectedFitRegion) : "";
  const fitQuad = selectedFitRegion ? draftQuad("region", fitKey, selectedFitRegion) : null;
  return `
    ${layers.projection ? regions.map((region, index) => workbenchRegionTile(region, index, "projection", selectedFitRegion)).join("") : ""}
    ${layers.dispatch ? regions.map((region, index) => workbenchRegionTile(region, index, "dispatch", selectedFitRegion)).join("") : ""}
    ${layers.dispatch ? workbenchSeams(regions) : ""}
    ${layers.zones ? projection.zones.map(zoneTile).join("") : ""}
    ${state.workbenchView.mode === "fit" && fitQuad ? workbenchQuadSvg("region", fitQuad) : ""}
    ${layers.links ? workbenchLinkSvg(actorPairs) : ""}
    ${layers.surface ? workbenchQuadSvg("surface", surface) : ""}
    ${layers.warp ? workbenchQuadSvg("warp", warp) : ""}
    ${layers.actors ? workbenchActorDots(actorPairs) : ""}
    ${state.workbenchView.mode === "fit" && fitQuad ? workbenchHandles("region", fitQuad, fitKey) : ""}
    ${layers.surface ? workbenchHandles("surface", surface, projectionId) : ""}
    ${layers.warp ? workbenchHandles("warp", warp, projectionId) : ""}
    ${!regions.length ? `<div class="empty-projection">No calibrated regions in config.yaml yet.</div>` : ""}
  `;
}

function effectiveWorkbenchLayers(): Record<WorkbenchLayer, boolean> {
  const requested = state.workbenchView.visibleLayers;
  const allowedByMode: Record<WorkbenchMode, Record<WorkbenchLayer, boolean>> = {
    inspect: {
      projection: true,
      dispatch: true,
      zones: true,
      actors: true,
      surface: false,
      warp: false,
      links: false,
    },
    canvas: {
      projection: true,
      dispatch: true,
      zones: true,
      actors: false,
      surface: false,
      warp: false,
      links: false,
    },
    fit: {
      projection: true,
      dispatch: true,
      zones: false,
      actors: true,
      surface: false,
      warp: false,
      links: false,
    },
    surface: {
      projection: true,
      dispatch: true,
      zones: true,
      actors: true,
      surface: true,
      warp: false,
      links: false,
    },
    warp: {
      projection: false,
      dispatch: false,
      zones: true,
      actors: true,
      surface: false,
      warp: true,
      links: true,
    },
  };
  const allowed = allowedByMode[state.workbenchView.mode];
  return {
    projection: requested.projection && allowed.projection,
    dispatch: requested.dispatch && allowed.dispatch,
    zones: requested.zones && allowed.zones,
    actors: requested.actors && allowed.actors,
    surface: requested.surface && allowed.surface,
    warp: requested.warp && allowed.warp,
    links: requested.links && allowed.links,
  };
}

function workbenchRegionTile(region: RegionInfo, index: number, kind: "projection" | "dispatch", selected?: RegionInfo): string {
  const values = kind === "projection" ? region.projection_uv : region.dispatch_uv;
  const rect = uvRect(values.length === 4 ? values : region.projection_uv);
  const camClass = cameraClassForName(region.camera, index);
  const isSelected = selected ? regionKey(selected) === regionKey(region) : false;
  return `<div class="dispatch wb-${kind} ${camClass} ${isSelected ? "selected-region" : ""}" style="${rectStyle(rect)}">
    <span>${escapeHtml(region.camera)} · ${escapeHtml(region.id)}</span>
    <b>${escapeHtml(kind)} ${formatUvRange(values)}</b>
  </div>`;
}

function workbenchSeams(regions: RegionInfo[]): string {
  const slices = regions
    .map((region) => {
      const rect = normalizedUv(region.dispatch_uv, region.projection_uv.length === 4 ? region.projection_uv : [0, 0, 1, 1]);
      return {
        label: region.camera,
        left: Math.min(rect[0], rect[2]),
        right: Math.max(rect[0], rect[2]),
      };
    })
    .filter((slice) => slice.right > slice.left)
    .sort((a, b) => a.left - b.left || a.right - b.right);
  if (slices.length < 2) {
    return "";
  }
  return slices
    .slice(0, -1)
    .map((slice, index) => {
      const next = slices[index + 1];
      const boundary = clamp01((slice.right + next.left) / 2);
      const width = Math.max(0.008, Math.min(0.04, Math.abs(next.left - slice.right) || 0.012));
      const left = clamp01(boundary - width / 2) * 100;
      const widthPct = Math.max(0.6, width * 100);
      const label = `${slice.label}->${next.label}`;
      return `<div class="seam" style="left:${left}%;width:${widthPct}%"><span>${escapeHtml(label)}</span></div>`;
    })
    .join("");
}

function workbenchQuadSvg(kind: WorkbenchHandleKind, quad: UvQuad): string {
  const points = quad.map((point) => `${clamp01(point.u) * 100},${clamp01(point.v) * 100}`).join(" ");
  return `<svg class="workbench-svg ${kind}" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
    <polygon points="${escapeAttr(points)}"></polygon>
  </svg>`;
}

function workbenchHandles(kind: WorkbenchHandleKind, quad: UvQuad, projectionId: string): string {
  return quad.map((point, index) => {
    const selected = state.workbenchView.selectedHandle?.kind === kind && state.workbenchView.selectedHandle.index === index;
    const activeKind = state.workbenchView.mode === "fit" ? "region" : state.workbenchView.mode;
    const editable = activeKind === kind;
    const position = handlePositionStyle(point);
    return `<button
      class="wb-handle ${kind} ${selected ? "selected" : ""} ${editable ? "editable" : ""}"
      data-handle-kind="${kind}"
      data-handle-index="${index}"
      data-projection-id="${escapeAttr(projectionId)}"
      style="${position}"
      aria-label="${kind} handle ${index + 1}"
    >${index + 1}</button>`;
  }).join("");
}

function handlePositionStyle(point: UvPoint): string {
  const left = clamp01(point.u) * 100;
  const top = clamp01(point.v) * 100;
  return `left:clamp(13px, ${left}%, calc(100% - 13px));top:clamp(13px, ${top}%, calc(100% - 13px))`;
}

function workbenchLinkSvg(actorPairs: Array<{ raw: UvPoint; warped: UvPoint; gid: number }>): string {
  if (!actorPairs.length) {
    return "";
  }
  const lines = actorPairs.slice(0, 24).map((pair) => {
    return `<line x1="${pair.raw.u * 100}" y1="${pair.raw.v * 100}" x2="${pair.warped.u * 100}" y2="${pair.warped.v * 100}"></line>`;
  }).join("");
  return `<svg class="workbench-links" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${lines}</svg>`;
}

function workbenchActorDots(actorPairs: Array<{ raw: UvPoint; warped: UvPoint; gid: number; state?: string }>): string {
  if (!actorPairs.length) {
    return `<div class="person-dot held" style="left:50%;top:50%"><span>idle</span></div>`;
  }
  return actorPairs.slice(0, 24).map((pair) => {
    return `<div class="person-dot raw-dot" style="left:${pair.raw.u * 100}%;top:${pair.raw.v * 100}%"><span>gid ${escapeHtml(String(pair.gid))} raw</span></div>
      <div class="person-dot warped-dot ${pair.state === "held" ? "held" : ""}" style="left:${pair.warped.u * 100}%;top:${pair.warped.v * 100}%"><span>warp</span></div>`;
  }).join("");
}

function workbenchReadout(projection: ProjectionInfo | undefined): string {
  const projectionId = projection?.id ?? "corridor";
  const selection = state.workbenchView.selectedHandle;
  const canvas = state.workbenchCanvasSize;
  const actorRows = workbenchActorPairs(projectionId, draftQuad("warp", projectionId)).slice(0, 5);
  const selectedKey = selection?.key ?? projectionId;
  const selectedPoint = selection ? draftQuad(selection.kind, selectedKey)[selection.index] : null;
  return `
    <div class="readout-block">
      <h4>canvas</h4>
      <div class="micro-row"><span>size</span><b id="workbenchReadoutSize">${canvas.width} x ${canvas.height}</b></div>
      <div class="micro-row"><span>aspect</span><b id="workbenchReadoutAspect">${formatAspect(canvas.width, canvas.height)}</b></div>
      <div class="micro-row"><span>mode</span><b>${escapeHtml(state.workbenchView.mode)}</b></div>
    </div>
    <div class="readout-block">
      <h4>selected handle</h4>
      ${selectedPoint && selection ? `
        <div class="micro-row"><span>target</span><b>${escapeHtml(selection.kind)} ${selection.index + 1}</b></div>
        <div class="micro-row"><span>uv</span><b>${formatUvPoint(selectedPoint)}</b></div>
        <div class="micro-row"><span>px</span><b>${formatPxPoint(selectedPoint)}</b></div>
      ` : `<p class="muted">${escapeHtml(workbenchSelectionHint())}</p>`}
      ${selection?.kind === "region" ? `<p class="muted readout-note">Camera Fit saves live while dragging; release finalizes the config refresh.</p>` : state.workbenchView.mode === "surface" || state.workbenchView.mode === "warp" ? `<div class="inline-actions tight">
        <button class="btn" data-action="workbench-reset-surface">Reset surface</button>
        <button class="btn" data-action="workbench-reset-warp">Reset warp</button>
      </div>` : ""}
    </div>
    <div class="readout-block">
      <h4>actors</h4>
      ${actorRows.length ? actorRows.map((pair) => workbenchActorReadout(pair)).join("") : `<p class="muted">No live actor payload yet.</p>`}
    </div>
  `;
}

function workbenchActorReadout(pair: { raw: UvPoint; warped: UvPoint; gid: number }): string {
  const du = pair.warped.u - pair.raw.u;
  const dv = pair.warped.v - pair.raw.v;
  const canvas = state.workbenchCanvasSize;
  return `<div class="actor-readout">
    <b>gid ${escapeHtml(String(pair.gid))}</b>
    <span>raw ${formatUvPoint(pair.raw)} / ${formatPxPoint(pair.raw)}</span>
    <span>warp ${formatUvPoint(pair.warped)} / ${formatPxPoint(pair.warped)}</span>
    <span>delta ${du.toFixed(3)}, ${dv.toFixed(3)} / ${Math.round(du * canvas.width)}, ${Math.round(dv * canvas.height)} px</span>
  </div>`;
}

function workbenchActorPairs(projectionId: string, warp: UvQuad): Array<{ raw: UvPoint; warped: UvPoint; gid: number; state?: string }> {
  const runtime = projectionRuntimeItems().find((item) => item.id === projectionId);
  return (runtime?.persons ?? [])
    .filter((person) => Number.isFinite(person.u) && Number.isFinite(person.v))
    .map((person) => {
      const raw = { u: clamp01(person.u), v: clamp01(person.v) };
      return {
        raw,
        warped: bilinearQuad(raw, warp),
        gid: person.gid,
        state: person.state,
      };
    });
}

function identityQuad(): UvQuad {
  return [
    { u: 0, v: 0 },
    { u: 1, v: 0 },
    { u: 1, v: 1 },
    { u: 0, v: 1 },
  ];
}

function rectToQuad(rect: number[]): UvQuad {
  const [x0 = 0, y0 = 0, x1 = 1, y1 = 1] = normalizedUv(rect, [0, 0, 1, 1]);
  const left = Math.min(x0, x1);
  const right = Math.max(x0, x1);
  const top = Math.min(y0, y1);
  const bottom = Math.max(y0, y1);
  return [
    { u: left, v: top },
    { u: right, v: top },
    { u: right, v: bottom },
    { u: left, v: bottom },
  ];
}

function quadToRect(quad: UvQuad): [number, number, number, number] {
  const xs = quad.map((point) => clamp01(point.u));
  const ys = quad.map((point) => clamp01(point.v));
  return [
    Math.min(...xs),
    Math.min(...ys),
    Math.max(...xs),
    Math.max(...ys),
  ];
}

function cloneQuad(quad: UvQuad): UvQuad {
  return quad.map((point) => ({ ...point })) as UvQuad;
}

function draftQuad(kind: WorkbenchHandleKind, projectionId: string, region?: RegionInfo): UvQuad {
  const store = kind === "region"
    ? state.draftRegionProjection
    : kind === "surface"
      ? state.draftUsableSurface
      : state.draftInteractionWarp;
  if (!store[projectionId]) {
    store[projectionId] = region ? rectToQuad(normalizedUv(region.projection_uv, [0, 0, 1, 1])) : identityQuad();
  }
  return store[projectionId];
}

function setDraftQuad(kind: WorkbenchHandleKind, projectionId: string, quad: UvQuad): void {
  if (kind === "region") {
    state.draftRegionProjection[projectionId] = quad;
  } else if (kind === "surface") {
    state.draftUsableSurface[projectionId] = quad;
  } else {
    state.draftInteractionWarp[projectionId] = quad;
  }
}

function setDraftHandle(kind: WorkbenchHandleKind, projectionId: string, index: number, point: UvPoint): void {
  const quad = cloneQuad(draftQuad(kind, projectionId));
  if (kind === "region") {
    setDraftQuad(kind, projectionId, rectCornerDragQuad(quad, index, point));
  } else {
    quad[index] = { u: clamp01(point.u), v: clamp01(point.v) };
    setDraftQuad(kind, projectionId, quad);
  }
}

function resetDraftQuad(kind: WorkbenchHandleKind, projectionId: string): void {
  setDraftQuad(kind, projectionId, identityQuad());
}

function regionKey(region: RegionInfo): string {
  return `${region.camera}:${region.id}`;
}

function selectedWorkbenchRegion(regions: RegionInfo[]): RegionInfo | undefined {
  if (!regions.length) {
    state.workbenchRegionKey = "";
    return undefined;
  }
  const selected = regions.find((region) => regionKey(region) === state.workbenchRegionKey);
  if (selected) {
    return selected;
  }
  state.workbenchRegionKey = regionKey(regions[0]);
  return regions[0];
}

function rectCornerDragQuad(quad: UvQuad, index: number, point: UvPoint): UvQuad {
  const [left, top, right, bottom] = quadToRect(quad);
  const minSize = 0.001;
  let nextLeft = left;
  let nextTop = top;
  let nextRight = right;
  let nextBottom = bottom;
  if (index === 0 || index === 3) {
    nextLeft = Math.min(clamp01(point.u), right - minSize);
  }
  if (index === 1 || index === 2) {
    nextRight = Math.max(clamp01(point.u), left + minSize);
  }
  if (index === 0 || index === 1) {
    nextTop = Math.min(clamp01(point.v), bottom - minSize);
  }
  if (index === 2 || index === 3) {
    nextBottom = Math.max(clamp01(point.v), top + minSize);
  }
  return rectToQuad([clamp01(nextLeft), clamp01(nextTop), clamp01(nextRight), clamp01(nextBottom)]);
}

function workbenchSelectionHint(): string {
  if (state.workbenchView.mode === "fit") {
    return "Select a Camera Fit corner.";
  }
  if (state.workbenchView.mode === "surface") {
    return "Select an interactive area handle.";
  }
  if (state.workbenchView.mode === "warp") {
    return "Select an output warp handle.";
  }
  return "Switch to an edit mode to select handles.";
}

function regionByKey(key: string): RegionInfo | undefined {
  return (state.projection?.regions ?? []).find((region) => regionKey(region) === key);
}

function clampRectInsideRect(rect: [number, number, number, number], bounds: [number, number, number, number]): [number, number, number, number] {
  const width = Math.max(0.001, Math.min(rect[2] - rect[0], bounds[2] - bounds[0]));
  const height = Math.max(0.001, Math.min(rect[3] - rect[1], bounds[3] - bounds[1]));
  const left = Math.max(bounds[0], Math.min(bounds[2] - width, rect[0]));
  const top = Math.max(bounds[1], Math.min(bounds[3] - height, rect[1]));
  return [left, top, left + width, top + height];
}

function scheduleWorkbenchRegionPersist(key: string): void {
  pendingWorkbenchRegionPersistKey = key;
  if (workbenchRegionPersistTimer !== null) {
    return;
  }
  workbenchRegionPersistTimer = window.setTimeout(() => {
    workbenchRegionPersistTimer = null;
    void flushWorkbenchRegionPersist(false);
  }, 180);
}

async function flushWorkbenchRegionPersist(refreshAfterSave: boolean): Promise<void> {
  pendingWorkbenchRegionPersistRefresh = pendingWorkbenchRegionPersistRefresh || refreshAfterSave;
  if (workbenchRegionPersistTimer !== null) {
    window.clearTimeout(workbenchRegionPersistTimer);
    workbenchRegionPersistTimer = null;
  }
  if (workbenchRegionPersistInFlight) {
    return;
  }
  const key = pendingWorkbenchRegionPersistKey;
  pendingWorkbenchRegionPersistKey = null;
  if (!key) {
    pendingWorkbenchRegionPersistRefresh = false;
    return;
  }
  const shouldRefresh = pendingWorkbenchRegionPersistRefresh;
  pendingWorkbenchRegionPersistRefresh = false;
  workbenchRegionPersistInFlight = true;
  try {
    await persistWorkbenchRegionFit(key, shouldRefresh);
  } finally {
    workbenchRegionPersistInFlight = false;
    if (pendingWorkbenchRegionPersistKey) {
      scheduleWorkbenchRegionPersist(pendingWorkbenchRegionPersistKey);
    }
  }
}

async function persistWorkbenchRegionFit(key: string, refreshAfterSave = true): Promise<void> {
  const region = regionByKey(key);
  if (!region) {
    return;
  }
  const projectionUv = quadToRect(draftQuad("region", key, region));
  const currentDispatch = normalizedUv(region.dispatch_uv, projectionUv) as [number, number, number, number];
  const dispatchUv = clampRectInsideRect(currentDispatch, projectionUv);
  try {
    await invoke("save_calibration_mapping", {
      request: {
        cameraName: region.camera,
        regionId: region.id,
        projectionUv,
        dispatchUv,
        relaxedPresenceV: region.relaxed_presence_v,
      },
    });
    state.saved = true;
    if (refreshAfterSave) {
      await refreshConfig();
      await refreshProjection();
      requestRender();
    }
  } catch (error) {
    state.error = String(error);
    requestRender();
  }
}

function bilinearQuad(point: UvPoint, quad: UvQuad): UvPoint {
  const u = clamp01(point.u);
  const v = clamp01(point.v);
  const topU = quad[0].u * (1 - u) + quad[1].u * u;
  const topV = quad[0].v * (1 - u) + quad[1].v * u;
  const bottomU = quad[3].u * (1 - u) + quad[2].u * u;
  const bottomV = quad[3].v * (1 - u) + quad[2].v * u;
  return {
    u: clamp01(topU * (1 - v) + bottomU * v),
    v: clamp01(topV * (1 - v) + bottomV * v),
  };
}

function projectionConfigCanvasSize(projection: ProjectionInfo | undefined): { width: number; height: number } {
  const width = Number(projection?.pixel_size?.[0]);
  const height = Number(projection?.pixel_size?.[1]);
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    return { width: Math.round(width), height: Math.round(height) };
  }
  return { width: 9600, height: 1080 };
}

function syncWorkbenchCanvasFromConfig(force = false): void {
  if (!force && state.workbenchCanvasTouched) {
    return;
  }
  const next = projectionConfigCanvasSize(state.projection?.projections[0]);
  state.workbenchCanvasSize = {
    ...state.workbenchCanvasSize,
    width: next.width,
    height: next.height,
  };
  state.workbenchCanvasTouched = false;
}

function beginWorkbenchCanvasEdit(): void {
  if (!workbenchCanvasEditStart) {
    workbenchCanvasEditStart = { ...state.workbenchCanvasSize };
  }
}

function refreshWorkbenchCanvasDom(): void {
  const canvas = root.querySelector<HTMLElement>("#workbenchCanvas");
  if (canvas) {
    canvas.style.setProperty("--canvas-aspect", canvasAspect(state.workbenchCanvasSize));
    canvas.style.aspectRatio = `${Math.max(1, state.workbenchCanvasSize.width)} / ${Math.max(1, state.workbenchCanvasSize.height)}`;
  }
  const activeId = String(document.activeElement?.id ?? "");
  const widthInput = root.querySelector<HTMLInputElement>("#workbenchWidth");
  const heightInput = root.querySelector<HTMLInputElement>("#workbenchHeight");
  if (widthInput && activeId !== "workbenchWidth") {
    widthInput.value = String(state.workbenchCanvasSize.width);
  }
  if (heightInput && activeId !== "workbenchHeight") {
    heightInput.value = String(state.workbenchCanvasSize.height);
  }
  const preset = root.querySelector<HTMLSelectElement>("#workbenchPreset");
  if (preset && activeId !== "workbenchPreset") {
    preset.value = workbenchPresetValue(state.projection?.projections[0]);
  }
  const pixels = root.querySelector<HTMLElement>("#workbenchPixelsValue");
  if (pixels) {
    pixels.textContent = `${state.workbenchCanvasSize.width} x ${state.workbenchCanvasSize.height} px`;
  }
  const readout = root.querySelector<HTMLElement>(".workbench-readout");
  if (readout) {
    readout.innerHTML = workbenchReadout(state.projection?.projections[0]);
  }
}

function applyWorkbenchCanvasSize(width: number, height: number, touched = true): void {
  const safeWidth = Math.max(128, Math.min(50000, Math.round(width)));
  const safeHeight = Math.max(128, Math.min(50000, Math.round(height)));
  state.workbenchCanvasSize = {
    ...state.workbenchCanvasSize,
    width: safeWidth,
    height: safeHeight,
  };
  state.workbenchCanvasTouched = touched;
}

function applyWorkbenchPreset(value: string): void {
  if (value === "config") {
    syncWorkbenchCanvasFromConfig(true);
    return;
  }
  const match = /^(\d+)x(\d+)$/.exec(value);
  if (match) {
    applyWorkbenchCanvasSize(Number(match[1]), Number(match[2]));
  }
}

function updateWorkbenchCanvasInput(axis: "width" | "height", value: string): void {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return;
  }
  const current = state.workbenchCanvasSize;
  const basis = workbenchCanvasEditStart ?? current;
  const ratio = basis.width / Math.max(1, basis.height);
  if (axis === "width") {
    applyWorkbenchCanvasSize(parsed, current.lockedAspect ? parsed / ratio : current.height);
  } else {
    applyWorkbenchCanvasSize(current.lockedAspect ? parsed * ratio : current.width, parsed);
  }
}

function formatUvPoint(point: UvPoint): string {
  return `${point.u.toFixed(3)}, ${point.v.toFixed(3)}`;
}

function formatPxPoint(point: UvPoint): string {
  const canvas = state.workbenchCanvasSize;
  return `${Math.round(point.u * canvas.width)}, ${Math.round(point.v * canvas.height)} px`;
}

function formatAspect(width: number, height: number): string {
  const ratio = width / Math.max(1, height);
  return `${ratio.toFixed(2)}:1`;
}

function canvasAspect(canvas: WorkbenchCanvasSize): string {
  const ratio = canvas.width / Math.max(1, canvas.height);
  return Number.isFinite(ratio) && ratio > 0 ? ratio.toFixed(6) : "1";
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
    .slice(0, 8)
    .map((cam, index) => {
      const count = Math.min(cameras.length, 8);
      const angle = -Math.PI / 2 + (index / Math.max(1, count)) * Math.PI * 2;
      const left = 50 + Math.cos(angle) * 30;
      const top = 50 + Math.sin(angle) * 25;
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
      uv: Array.isArray(projection.uv)
        ? projection.uv.map((value) => Number(value)).filter(Number.isFinite)
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
    return `<p class="muted">No projection runtime payload yet. Start tracker to see /active, /xy, and /uv state.</p>`;
  }
  return `<div class="td-runtime-list">${projections
    .map((projection) => {
      const active = projection.active.length ? projection.active.join(", ") : "-";
      const xyTriples = Math.floor(projection.xy.length / 3);
      const uvTriples = Math.floor(projection.uv.length / 3);
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
        <div class="micro-row"><span>/proj/${escapeHtml(projection.id)}/persons/count</span><b>${projection.active.length}</b></div>
        <div class="micro-row"><span>/proj/${escapeHtml(projection.id)}/xy</span><b>${xyTriples} triples / ${projection.xy.length} values</b></div>
        <div class="micro-row"><span>/proj/${escapeHtml(projection.id)}/uv</span><b>${uvTriples} triples / ${projection.uv.length} values</b></div>
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
      addressRate(`/proj/${pid}/uv`, 0, "uv"),
      addressRate(`/proj/${pid}/persons/count`, 0, "count"),
    ].join("");
  }
  return projections
    .map((projection) => {
      const rate = sumCameraNumber("osc_rate") / Math.max(1, projections.length);
      return [
        addressRate(`/proj/${projection.id}/active`, rate / 3, "ids"),
        addressRate(`/proj/${projection.id}/xy`, rate / 3, "xy"),
        addressRate(`/proj/${projection.id}/uv`, rate / 3, "uv"),
        addressRate(`/proj/${projection.id}/persons/count`, rate / 3, "count"),
      ].join("");
    })
    .join("");
}

function regionTile(region: RegionInfo, index: number): string {
  const rect = uvRect(region.dispatch_uv.length === 4 ? region.dispatch_uv : region.projection_uv);
  const camClass = cameraClassForName(region.camera, index);
  const hasRelaxed = Boolean(region.relaxed_presence_points?.length);
  const relaxedMeta = `stair m ${formatOptionalNumber(region.relaxed_presence_margin_uv)}`;
  return `<div class="dispatch ${camClass} ${hasRelaxed ? "has-relaxed" : ""}" style="${rectStyle(rect)}">
    <span>${escapeHtml(region.camera)} · ${escapeHtml(region.id)}</span>
    <b>${formatUvRange(region.dispatch_uv)}</b>
    ${hasRelaxed ? `<em>${escapeHtml(relaxedMeta)}</em>` : ""}
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
        <span class="v">${escapeHtml(region.projection_id)} · proj ${formatUvRange(region.projection_uv)} · dispatch ${formatUvRange(region.dispatch_uv)}${region.relaxed_presence_points?.length ? ` · stair ${region.relaxed_presence_points.length}pt margin ${formatOptionalNumber(region.relaxed_presence_margin_uv)} conf ${formatOptionalNumber(region.relaxed_presence_min_confidence)}` : ""}</span>
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

function cameraClassForName(cameraName: string, fallbackIndex = 0): string {
  const suffix = /(\d+)$/.exec(cameraName.trim());
  if (suffix) {
    return `cam${Number(suffix[1]) % 6}`;
  }
  let hash = Math.max(0, fallbackIndex);
  for (const ch of cameraName) {
    hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  }
  return `cam${hash % 6}`;
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

function selectedCalibrationRegion(): RegionInfo | undefined {
  return calibrationRegionsForCamera(state.calibrationCamera).find((region) => region.id === state.calibrationRegionId);
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

function calibrationPointMarkers(points: number[][], frame: CalibrationFrame, tool: CalibrationTool): string[] {
  return points.slice(0, 4).map((point, index) => {
    const left = clamp01(Number(point[0]) / Math.max(1, frame.width)) * 100;
    const top = clamp01(Number(point[1]) / Math.max(1, frame.height)) * 100;
    return `<span class="calib-point ${tool}" style="left:${left}%;top:${top}%">${index + 1}</span>`;
  });
}

function calibrationRegionOverlay(region: RegionInfo | undefined, frame: CalibrationFrame, activeTool: CalibrationTool): string {
  if (!region) {
    return "";
  }
  const floor = svgPolygonPoints(region.image_points);
  const body = svgPolygonPoints(region.body_catch_points);
  const relaxed = svgPolygonPoints(region.relaxed_presence_points);
  if (!floor && !body && !relaxed) {
    return "";
  }
  const stateClass = (tool: CalibrationTool) => tool === activeTool ? "active" : "dim";
  return `<svg class="calib-overlay" viewBox="0 0 ${Math.max(1, frame.width)} ${Math.max(1, frame.height)}" aria-hidden="true">
    ${floor ? `<polygon class="floor-poly ${stateClass("floor")}" points="${escapeAttr(floor)}"></polygon>` : ""}
    ${body ? `<polygon class="body-poly ${stateClass("body")}" points="${escapeAttr(body)}"></polygon>` : ""}
    ${relaxed ? `<polygon class="relaxed-poly ${stateClass("stair")}" points="${escapeAttr(relaxed)}"></polygon>` : ""}
  </svg>
  <div class="calib-legend">
    ${floor ? `<span class="floor">floor uv</span>` : ""}
    ${body ? `<span class="body">body catch</span>` : ""}
    ${relaxed ? `<span class="relaxed">stair relaxed</span>` : ""}
  </div>`;
}

function calibrationToolLabel(tool: CalibrationTool): string {
  if (tool === "body") {
    return "Body catch";
  }
  return tool === "stair" ? "Stair relaxed" : "Floor UV";
}

function calibrationToolPointsLabel(tool: CalibrationTool): string {
  if (tool === "body") {
    return "body points";
  }
  return tool === "stair" ? "stair points" : "image points";
}

function calibrationToolDescription(tool: CalibrationTool): string {
  if (tool === "body") {
    return "Body catch saves a wide bbox allowance polygon. It does not define the floor plane.";
  }
  if (tool === "stair") {
    return "Stair relaxed saves a seated-person mask. Use Stair fixed v when the stair should land on a stable projection row.";
  }
  return "Floor UV defines the precise floor plane used for homography. Keep it on the real walking surface.";
}

function calibrationMappingKey(camera: string, regionId: string): string {
  return `${camera}:${regionId}`;
}

function currentCalibrationMappingKey(): string {
  const regionId = selectedCalibrationRegion()?.id ?? state.calibrationRegionId;
  return calibrationMappingKey(state.calibrationCamera, regionId);
}

function defaultCalibrationMappingDraft(key = currentCalibrationMappingKey()): CalibrationMappingDraft {
  const region = selectedCalibrationRegion();
  const projectionUv = normalizedUv(region?.projection_uv, [0, 0, 1, 1]);
  const dispatchUv = normalizedUv(region?.dispatch_uv, projectionUv);
  const stairRelaxedUv = normalizedUv(region?.relaxed_presence_uv, projectionUv);
  return {
    key,
    projectionUMin: formatUvNumber(projectionUv[0]),
    projectionUMax: formatUvNumber(projectionUv[2]),
    projectionVMin: formatUvNumber(projectionUv[1]),
    projectionVMax: formatUvNumber(projectionUv[3]),
    dispatchUMin: formatUvNumber(dispatchUv[0]),
    dispatchUMax: formatUvNumber(dispatchUv[2]),
    dispatchVMin: formatUvNumber(dispatchUv[1]),
    dispatchVMax: formatUvNumber(dispatchUv[3]),
    stairRelaxedUMin: formatUvNumber(stairRelaxedUv[0]),
    stairRelaxedUMax: formatUvNumber(stairRelaxedUv[2]),
    stairRelaxedVMin: formatUvNumber(stairRelaxedUv[1]),
    stairRelaxedVMax: formatUvNumber(stairRelaxedUv[3]),
    stairFixedV: region?.relaxed_presence_v == null ? "" : formatUvNumber(region.relaxed_presence_v),
  };
}

function mappingDraftValue(
  key: string,
  field: keyof Omit<CalibrationMappingDraft, "key">,
  fallback: string,
): string {
  return state.calibrationMappingDraft?.key === key
    ? state.calibrationMappingDraft[field]
    : fallback;
}

function updateCalibrationMappingDraft(input: HTMLInputElement): void {
  const key = currentCalibrationMappingKey();
  const draft = state.calibrationMappingDraft?.key === key
    ? state.calibrationMappingDraft
    : defaultCalibrationMappingDraft(key);
  if (input.id in draft) {
    state.calibrationMappingDraft = {
      ...draft,
      [input.id]: input.value,
    };
  }
}

function normalizedUv(values: number[] | undefined, fallback: number[]): number[] {
  if (!values || values.length < 4) {
    return [...fallback];
  }
  return values.slice(0, 4).map((value, index) => {
    const n = Number(value);
    return Number.isFinite(n) ? clamp01(n) : fallback[index] ?? 0;
  });
}

function formatUvNumber(value: number): string {
  return Number(value).toFixed(2);
}

function svgPolygonPoints(points: number[][] | undefined): string {
  if (!points?.length) {
    return "";
  }
  return points
    .filter((point) => point.length >= 2)
    .map((point) => `${Number(point[0]) || 0},${Number(point[1]) || 0}`)
    .join(" ");
}

function currentCalibrationPoints(): number[][] {
  const source = state.calibrationDraftActive
    ? state.calibrationPoints
    : calibrationToolPoints(selectedCalibrationRegion());
  return source.slice(0, 4).map((point) => [Math.round(Number(point[0]) || 0), Math.round(Number(point[1]) || 0)]);
}

function calibrationToolPoints(region: RegionInfo | undefined): number[][] {
  if (!region) {
    return [];
  }
  if (state.calibrationTool === "stair") {
    return region.relaxed_presence_points ?? [];
  }
  if (state.calibrationTool === "body") {
    return region.body_catch_points ?? [];
  }
  return region.image_points ?? [];
}

function nextCalibrationPoints(clickedPoint: number[]): number[][] {
  const current = currentCalibrationPoints();
  if (current.length >= 4) {
    const next = current.map((point) => [...point]);
    next[nearestCalibrationPointIndex(next, clickedPoint)] = clickedPoint;
    return next;
  }
  return [...current, clickedPoint];
}

function nearestCalibrationPointIndex(points: number[][], target: number[]): number {
  let nearestIndex = 0;
  let nearestDistance = Number.POSITIVE_INFINITY;
  points.forEach((point, index) => {
    const dx = Number(point[0]) - Number(target[0]);
    const dy = Number(point[1]) - Number(target[1]);
    const distance = dx * dx + dy * dy;
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestIndex = index;
    }
  });
  return nearestIndex;
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
  if (command === "get_mobile_server_status") {
    return {
      running: true,
      bind: "0.0.0.0",
      port: 1421,
      token: "123456",
      urls: ["http://192.168.1.42:1421/mobile", "http://127.0.0.1:1421/mobile"],
      status_path: "/api/status",
      token_header: "X-Reolink-Mobile-Token",
      error: null,
    } as T;
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
  if (command === "save_calibration_mapping") {
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
  - name: cam2
    url: rtsp://admin:<urlencoded-password>@<center-camera-ip>:554/h264Preview_01_sub
    osc_prefix: /cam/2
    regions: []
  - name: cam1
    url: rtsp://admin:<urlencoded-password>@<right-camera-ip>:554/h264Preview_01_sub
    osc_prefix: /cam/1
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
      camera_count: 3,
      projections: [
        {
          id: "corridor",
          pixel_size: [9600, 1080],
          world_size_m: [40, 4.5],
          zones: [{ id: "center", uv_rect: [0.35, 0.15, 0.65, 0.85] }],
        },
      ],
      regions: [
        {
          camera: "cam0",
          id: "cam0_region_1",
          projection_id: "corridor",
          image_points: [[120, 120], [740, 120], [780, 520], [90, 520]],
          projection_uv: [0, 0.5, 0.44, 1],
          dispatch_uv: [0, 0.5, 0.2, 1],
          min_bbox_height_px: 24,
          body_catch_points: [],
          relaxed_presence_points: [],
          relaxed_presence_uv: [],
          relaxed_presence_margin_uv: 0,
          relaxed_presence_min_confidence: null,
          relaxed_presence_v: null,
        },
        {
          camera: "cam2",
          id: "center_band",
          projection_id: "corridor",
          image_points: [[180, 110], [1040, 120], [1080, 570], [150, 560]],
          projection_uv: [0.18, 0.5, 0.82, 1],
          dispatch_uv: [0.2, 0.5, 0.8, 1],
          min_bbox_height_px: 24,
          body_catch_points: [[150, 80], [1080, 90], [1100, 260], [140, 250]],
          relaxed_presence_points: [[405, 280], [1115, 292], [1133, 416], [338, 390]],
          relaxed_presence_uv: [0.24, 0.56, 0.74, 0.92],
          relaxed_presence_margin_uv: 0.12,
          relaxed_presence_min_confidence: 0.12,
          relaxed_presence_v: null,
        },
        {
          camera: "cam1",
          id: "cam1_region_1",
          projection_id: "corridor",
          image_points: [[1180, 120], [520, 120], [500, 530], [1210, 540]],
          projection_uv: [0.56, 0.5, 1, 1],
          dispatch_uv: [0.8, 0.5, 1, 1],
          min_bbox_height_px: 24,
          body_catch_points: [],
          relaxed_presence_points: [],
          relaxed_presence_uv: [],
          relaxed_presence_margin_uv: 0.1,
          relaxed_presence_min_confidence: 0.12,
          relaxed_presence_v: null,
        },
      ],
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

function readUnitInput(id: string, fallback: number): number {
  const input = document.getElementById(id) as HTMLInputElement | null;
  const parsed = Number(input?.value);
  return Number.isFinite(parsed) ? clamp01(parsed) : fallback;
}

function readOptionalUnitInput(id: string): number | null {
  const input = document.getElementById(id) as HTMLInputElement | null;
  const value = input?.value.trim() ?? "";
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? clamp01(parsed) : null;
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
    } else if (action === "mobile-refresh") {
      await refreshMobile();
    } else if (action === "start") {
      await refreshConfig();
      await refreshProjection();
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: false });
    } else if (action === "preview") {
      await refreshConfig();
      await refreshProjection();
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: true });
    } else if (action === "start-video-test" || action === "preview-video-test") {
      const cameraName = configuredCameraNames().includes(state.videoTestCamera)
        ? state.videoTestCamera
        : configuredCameraNames()[0];
      state.process = await invoke<ProcessStatus>("start_video_test", {
        request: {
          videoPath: state.videoPath.trim(),
          cameraName,
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
      state.calibrationDraftActive = false;
    } else if (action === "save-calibration-points") {
      await invoke("save_calibration_points", {
        request: {
          cameraName: state.calibrationCamera,
          regionId: state.calibrationRegionId || defaultCalibrationRegionId(state.calibrationCamera),
          imagePoints: state.calibrationPoints,
          pointKind: state.calibrationTool,
        },
      });
      state.saved = true;
      await refreshConfig();
      await refreshProjection();
      state.calibrationDraftActive = false;
    } else if (action === "save-calibration-mapping") {
      const region = selectedCalibrationRegion();
      if (!region) {
        throw new Error("Select a calibration region first.");
      }
      const projectionUv = normalizedUv(region.projection_uv, [0, 0, 1, 1]);
      const dispatchUv = normalizedUv(region.dispatch_uv, projectionUv);
      const stairRelaxedUv = normalizedUv(region.relaxed_presence_uv, projectionUv);
      projectionUv[0] = readUnitInput("projectionUMin", projectionUv[0]);
      projectionUv[2] = readUnitInput("projectionUMax", projectionUv[2]);
      projectionUv[1] = readUnitInput("projectionVMin", projectionUv[1]);
      projectionUv[3] = readUnitInput("projectionVMax", projectionUv[3]);
      dispatchUv[0] = readUnitInput("dispatchUMin", dispatchUv[0]);
      dispatchUv[2] = readUnitInput("dispatchUMax", dispatchUv[2]);
      dispatchUv[1] = readUnitInput("dispatchVMin", dispatchUv[1]);
      dispatchUv[3] = readUnitInput("dispatchVMax", dispatchUv[3]);
      if (projectionUv[0] >= projectionUv[2]) {
        throw new Error("u min must be lower than u max.");
      }
      if (projectionUv[1] >= projectionUv[3]) {
        throw new Error("v min must be lower than v max.");
      }
      dispatchUv[0] = Math.max(projectionUv[0], dispatchUv[0]);
      dispatchUv[2] = Math.min(projectionUv[2], dispatchUv[2]);
      dispatchUv[1] = Math.max(projectionUv[1], dispatchUv[1]);
      dispatchUv[3] = Math.min(projectionUv[3], dispatchUv[3]);
      if (dispatchUv[0] >= dispatchUv[2]) {
        dispatchUv[0] = projectionUv[0];
        dispatchUv[2] = projectionUv[2];
      }
      if (dispatchUv[1] >= dispatchUv[3]) {
        dispatchUv[1] = projectionUv[1];
        dispatchUv[3] = projectionUv[3];
      }
      stairRelaxedUv[0] = readUnitInput("stairRelaxedUMin", stairRelaxedUv[0]);
      stairRelaxedUv[2] = readUnitInput("stairRelaxedUMax", stairRelaxedUv[2]);
      stairRelaxedUv[1] = readUnitInput("stairRelaxedVMin", stairRelaxedUv[1]);
      stairRelaxedUv[3] = readUnitInput("stairRelaxedVMax", stairRelaxedUv[3]);
      if (stairRelaxedUv[0] >= stairRelaxedUv[2]) {
        throw new Error("stair relaxed u min must be lower than u max.");
      }
      if (stairRelaxedUv[1] >= stairRelaxedUv[3]) {
        throw new Error("stair relaxed v min must be lower than v max.");
      }
      await invoke("save_calibration_mapping", {
        request: {
          cameraName: state.calibrationCamera,
          regionId: region.id,
          projectionUv,
          dispatchUv,
          relaxedPresenceUv: region.relaxed_presence_points?.length ? stairRelaxedUv : undefined,
          relaxedPresenceV: readOptionalUnitInput("stairFixedV"),
        },
      });
      state.saved = true;
      await refreshConfig();
      await refreshProjection();
      state.calibrationMappingDraft = null;
    } else if (action === "clear-calibration-points") {
      state.calibrationPoints = [];
      state.calibrationDraftActive = true;
    } else if (action === "stop") {
      state.process = await invoke<ProcessStatus>("stop_tracker");
    } else if (action === "network") {
      state.network = await invoke<NetworkReport>("collect_network_report");
    } else if (action === "field-checks") {
      state.fieldChecks = await invoke<FieldCheckReport>("run_field_checks");
    } else if (action === "projection-refresh") {
      await refreshProjection();
    } else if (action === "workbench-reset-canvas") {
      syncWorkbenchCanvasFromConfig(true);
    } else if (action === "workbench-reset-surface") {
      resetDraftQuad("surface", state.projection?.projections[0]?.id ?? "corridor");
      state.workbenchView.selectedHandle = null;
    } else if (action === "workbench-reset-warp") {
      resetDraftQuad("warp", state.projection?.projections[0]?.id ?? "corridor");
      state.workbenchView.selectedHandle = null;
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
    syncWorkbenchCanvasFromConfig();
  } catch {
    state.projection = null;
  }
}

async function refreshMobile(): Promise<void> {
  try {
    state.mobile = await invoke<MobileServerStatus>("get_mobile_server_status");
  } catch {
    state.mobile = null;
  }
}

async function refreshAll(): Promise<void> {
  state.runtime = await invoke<RuntimeStatus>("get_runtime_status");
  state.process = await invoke<ProcessStatus>("tracker_status");
  await refreshMobile();
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

function formatOptionalNumber(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "-";
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

document.addEventListener("pointermove", updateWorkbenchDrag);
document.addEventListener("pointerup", endWorkbenchDrag);
document.addEventListener("pointercancel", endWorkbenchDrag);
window.addEventListener("keydown", handleWorkbenchKeyDown);

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
      { name: "cam2", fps: 0, osc_rate: 0, reconnects: 0, frame_age_s: null },
      { name: "cam1", fps: 0, osc_rate: 0, reconnects: 0, frame_age_s: null },
    ],
    projections: [
      {
        id: "corridor",
        active: [1, 2],
        xy: [1, 1728, 453.6, 2, 6912, 626.4],
        uv: [1, 0.18, 0.42, 2, 0.72, 0.58],
        persons: [
          { gid: 1, x: 0.18, y: 0.42, u: 0.18, v: 0.42, state: "fresh" },
          { gid: 2, x: 0.72, y: 0.58, u: 0.72, v: 0.58, state: "held" },
        ],
      },
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
