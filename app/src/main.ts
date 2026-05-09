import { invoke as tauriInvoke } from "@tauri-apps/api/core";
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

const state: {
  runtime: RuntimeStatus | null;
  process: ProcessStatus;
  config: string;
  logs: TrackerLog[];
  events: TrackerEvent[];
  network: NetworkReport | null;
  busy: string | null;
  error: string | null;
  saved: boolean;
} = {
  runtime: null,
  process: { running: false, exit_code: null },
  config: "",
  logs: [],
  events: [],
  network: null,
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

function latestEvent(name: string): TrackerEvent | undefined {
  return [...state.events].reverse().find((event) => event.event === name);
}

function cameraRows(): string {
  const fps = latestEvent("fps_tick");
  const cameras = Array.isArray(fps?.cameras) ? fps.cameras : [];
  if (!cameras.length) {
    return `<tr><td colspan="5" class="muted">아직 카메라 상태 이벤트가 없습니다.</td></tr>`;
  }
  return cameras
    .map((item) => {
      const cam = item as Record<string, unknown>;
      return `<tr>
        <td>${escapeHtml(String(cam.name ?? ""))}</td>
        <td>${formatNumber(cam.fps)}</td>
        <td>${formatNumber(cam.osc_rate)}</td>
        <td>${escapeHtml(String(cam.reconnects ?? 0))}</td>
        <td>${formatNumber(cam.frame_age_s)}</td>
      </tr>`;
    })
    .join("");
}

function render(): void {
  const runtime = state.runtime;
  const setupReady = Boolean(
    runtime?.venv_exists &&
      runtime?.config_exists &&
      runtime?.tracker_exists &&
      runtime?.model_exists,
  );

  root.innerHTML = `
    <section class="shell">
      <aside class="sidebar">
        <div>
          <p class="eyebrow">Field Operator</p>
          <h1>Reolink Tracker</h1>
        </div>
        <nav>
          <button data-action="prepare" ${buttonDisabled()}>Setup</button>
          <button data-action="refresh" ${buttonDisabled()}>Refresh</button>
          <button data-action="start" ${buttonDisabled(!setupReady || state.process.running)}>Start</button>
          <button data-action="preview" ${buttonDisabled(!setupReady || state.process.running)}>Show Preview</button>
          <button data-action="stop" ${buttonDisabled(!state.process.running)}>Stop</button>
          <button data-action="network" ${buttonDisabled()}>Network</button>
        </nav>
        <div class="status-pill ${state.process.running ? "running" : ""}">
          ${state.process.running ? "Tracker running" : "Tracker stopped"}
        </div>
      </aside>

      <section class="content">
        ${state.error ? `<div class="banner error">${escapeHtml(state.error)}</div>` : ""}
        ${state.busy ? `<div class="banner">${escapeHtml(state.busy)}</div>` : ""}

        <section class="grid two">
          <article class="panel">
            <h2>Setup</h2>
            ${runtimePanel(runtime)}
          </article>
          <article class="panel">
            <h2>Camera Status</h2>
            <table>
              <thead><tr><th>Name</th><th>FPS</th><th>OSC/s</th><th>Reconnects</th><th>Age</th></tr></thead>
              <tbody>${cameraRows()}</tbody>
            </table>
          </article>
        </section>

        <section class="grid two main-grid">
          <article class="panel config-panel">
            <div class="panel-head">
              <h2>Config</h2>
              <button data-action="save-config" ${buttonDisabled(!state.config || state.saved)}>Save</button>
            </div>
            <textarea id="configEditor" spellcheck="false">${escapeHtml(state.config)}</textarea>
          </article>
          <article class="panel logs-panel">
            <div class="panel-head">
              <h2>Logs</h2>
              <button data-action="clear-logs">Clear</button>
            </div>
            <div class="logs">${logRows()}</div>
          </article>
        </section>

        <section class="panel">
          <h2>Network</h2>
          ${networkPanel()}
        </section>
      </section>
    </section>
  `;

  root.querySelectorAll<HTMLButtonElement>("button[data-action]").forEach((button) => {
    button.addEventListener("click", () => void handleAction(button.dataset.action ?? ""));
  });
  const editor = root.querySelector<HTMLTextAreaElement>("#configEditor");
  editor?.addEventListener("input", () => {
    state.config = editor.value;
    state.saved = false;
    root.querySelector<HTMLButtonElement>('button[data-action="save-config"]')?.removeAttribute("disabled");
  });
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
  throw new Error(`${command} requires the Tauri desktop runtime.`);
}

function runtimePanel(runtime: RuntimeStatus | null): string {
  if (!runtime) {
    return `<p class="muted">Runtime status를 불러오지 않았습니다.</p>`;
  }
  const rows = [
    ["App data", runtime.app_data_dir],
    ["Runtime", runtime.runtime_dir],
    ["Config", runtime.config_path],
    ["Python", runtime.python_path],
    ["uv", runtime.uv_path ?? "not found"],
  ];
  const checks = [
    ["Tracker", runtime.tracker_exists],
    ["Python venv", runtime.venv_exists],
    ["Config", runtime.config_exists],
    ["Model", runtime.model_exists],
  ];
  return `
    <div class="checks">
      ${checks
        .map(([label, ok]) => `<span class="${ok ? "ok" : "missing"}">${label}: ${ok ? "ready" : "missing"}</span>`)
        .join("")}
    </div>
    <dl>${rows.map(([key, value]) => `<dt>${escapeHtml(String(key))}</dt><dd>${escapeHtml(String(value))}</dd>`).join("")}</dl>
  `;
}

function networkPanel(): string {
  if (!state.network) {
    return `<p class="muted">Network 버튼을 누르면 macOS route/ARP 상태를 읽습니다.</p>`;
  }
  const targetRows = state.network.targets.length
    ? state.network.targets
        .map(
          (target) => `<details><summary>${escapeHtml(target.name)} ${escapeHtml(target.host)}</summary><pre>${escapeHtml(
            target.route,
          )}</pre></details>`,
        )
        .join("")
    : `<p class="muted">config.yaml에서 카메라 RTSP target을 찾지 못했습니다.</p>`;
  return `
    <div class="grid two">
      <details open><summary>Default route</summary><pre>${escapeHtml(state.network.default_route)}</pre></details>
      <details><summary>ARP</summary><pre>${escapeHtml(state.network.arp)}</pre></details>
    </div>
    <details><summary>Interfaces</summary><pre>${escapeHtml(state.network.interfaces)}</pre></details>
    ${targetRows}
  `;
}

function logRows(): string {
  if (!state.logs.length) {
    return `<p class="muted">로그가 없습니다.</p>`;
  }
  return state.logs
    .slice(-300)
    .map((log) => `<div class="log ${log.stream}"><span>${escapeHtml(log.stream)}</span>${escapeHtml(log.line)}</div>`)
    .join("");
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
      });
    } else if (action === "refresh") {
      await refreshAll();
    } else if (action === "start") {
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: false });
    } else if (action === "preview") {
      state.process = await invoke<ProcessStatus>("start_tracker", { showPreview: true });
    } else if (action === "stop") {
      state.process = await invoke<ProcessStatus>("stop_tracker");
    } else if (action === "network") {
      state.network = await invoke<NetworkReport>("collect_network_report");
    } else if (action === "save-config") {
      await invoke("save_config", { request: { content: state.config } });
      state.saved = true;
      state.runtime = await invoke<RuntimeStatus>("get_runtime_status");
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

async function refreshAll(): Promise<void> {
  state.runtime = await invoke<RuntimeStatus>("get_runtime_status");
  state.process = await invoke<ProcessStatus>("tracker_status");
  await refreshConfig();
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

function formatNumber(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(1) : "-";
}

if (hasTauriRuntime) {
  void tauriListen<TrackerLog>("tracker-log", (event) => {
    state.logs.push(event.payload);
    if (state.logs.length > 500) {
      state.logs.splice(0, state.logs.length - 500);
    }
    render();
  });

  void tauriListen<TrackerEvent>("tracker-status", (event) => {
    state.events.push(event.payload);
    if (state.events.length > 200) {
      state.events.splice(0, state.events.length - 200);
    }
    render();
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
      render();
    })
    .catch(() => undefined);
}, 2500);

void refreshAll()
  .catch((error) => {
    state.error = String(error);
  })
  .finally(render);
