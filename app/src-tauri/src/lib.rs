use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::{
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Default)]
struct AppState {
    child: Mutex<Option<Child>>,
}

#[derive(Debug, Serialize)]
struct RuntimeStatus {
    app_data_dir: String,
    runtime_dir: String,
    engine_dir: String,
    config_path: String,
    python_path: String,
    venv_exists: bool,
    config_exists: bool,
    model_exists: bool,
    tracker_exists: bool,
    uv_path: Option<String>,
}

#[derive(Debug, Serialize)]
struct CommandOutput {
    ok: bool,
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

#[derive(Debug, Clone, Serialize)]
struct LogEvent {
    stream: String,
    line: String,
}

#[derive(Debug, Serialize)]
struct ProcessStatus {
    running: bool,
    exit_code: Option<i32>,
}

#[derive(Debug, Serialize)]
struct NetworkReport {
    interfaces: String,
    default_route: String,
    arp: String,
    targets: Vec<TargetRouteReport>,
}

#[derive(Debug, Serialize)]
struct TargetRouteReport {
    name: String,
    host: String,
    route: String,
}

#[derive(Debug, Clone, Serialize)]
struct FieldCheck {
    id: String,
    label: String,
    status: String,
    meta: String,
    detail: String,
    ts: String,
}

#[derive(Debug, Serialize)]
struct FieldCheckReport {
    generated_at: String,
    checks: Vec<FieldCheck>,
    target_count: usize,
    ok_count: usize,
    warn_count: usize,
    fail_count: usize,
}

#[derive(Debug, Clone, Serialize)]
struct ProjectionInfo {
    id: String,
    pixel_size: Vec<f64>,
    world_size_m: Vec<f64>,
    zones: Vec<ZoneInfo>,
}

#[derive(Debug, Clone, Serialize)]
struct ZoneInfo {
    id: String,
    uv_rect: Vec<f64>,
}

#[derive(Debug, Clone, Serialize)]
struct RegionInfo {
    camera: String,
    id: String,
    projection_id: String,
    projection_uv: Vec<f64>,
    dispatch_uv: Vec<f64>,
    min_bbox_height_px: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
struct ProjectionSnapshot {
    projections: Vec<ProjectionInfo>,
    regions: Vec<RegionInfo>,
    camera_count: usize,
}

#[derive(Debug, Deserialize)]
struct SaveConfigRequest {
    content: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct VideoTestRequest {
    video_path: String,
    camera_name: Option<String>,
    show_preview: bool,
}

fn runtime_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|err| format!("failed to resolve app data dir: {err}"))?
        .join("runtime");
    Ok(dir)
}

fn engine_dir(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(runtime_dir(app)?.join("engine"))
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(runtime_dir(app)?.join("config.yaml"))
}

fn local_path_from_input(input: &str) -> PathBuf {
    let trimmed = input.trim();
    let without_file_scheme = trimmed.strip_prefix("file://").unwrap_or(trimmed);
    if let Some(rest) = without_file_scheme.strip_prefix("~/") {
        if let Some(home) = dirs::home_dir() {
            return home.join(rest);
        }
    }
    PathBuf::from(without_file_scheme)
}

fn python_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(runtime_dir(app)?.join(".venv/bin/python"))
}

fn engine_source_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("../.."))
}

const ENGINE_FILE_NAMES: [&str; 6] = [
    "tracker.py",
    "region.py",
    "viewer.py",
    "fusion.py",
    "requirements.txt",
    "config.example.yaml",
];

fn copy_file(src: &Path, dst: &Path) -> Result<(), String> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create {}: {err}", parent.display()))?;
    }
    fs::copy(src, dst).map(|_| ()).map_err(|err| {
        format!(
            "failed to copy {} to {}: {err}",
            src.display(),
            dst.display()
        )
    })
}

fn copy_engine_files_from(source: &Path, target: &Path) -> Result<(), String> {
    if !source.exists() {
        return Err(format!(
            "tracker source not found at {}. In bundled builds, add tracker-engine resources or run from the tools workspace.",
            source.display()
        ));
    }
    fs::create_dir_all(&target)
        .map_err(|err| format!("failed to create {}: {err}", target.display()))?;
    for name in ENGINE_FILE_NAMES {
        copy_file(&source.join(name), &target.join(name))?;
    }
    Ok(())
}

fn copy_engine_files(app: &AppHandle) -> Result<(), String> {
    copy_engine_files_from(&engine_source_dir(), &engine_dir(app)?)
}

fn run_capture(mut cmd: Command) -> Result<CommandOutput, String> {
    let output = cmd
        .output()
        .map_err(|err| format!("failed to run command: {err}"))?;
    Ok(CommandOutput {
        ok: output.status.success(),
        code: output.status.code(),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

fn which(program: &str) -> Option<PathBuf> {
    let output = Command::new("/usr/bin/which").arg(program).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(PathBuf::from(text))
    }
}

fn find_uv() -> Option<PathBuf> {
    if let Some(path) = which("uv") {
        return Some(path);
    }
    let home = dirs::home_dir()?;
    for candidate in [home.join(".local/bin/uv"), home.join(".cargo/bin/uv")] {
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

fn install_uv() -> Result<PathBuf, String> {
    if let Some(path) = find_uv() {
        return Ok(path);
    }
    let mut cmd = Command::new("/bin/sh");
    cmd.arg("-lc")
        .arg("curl -LsSf https://astral.sh/uv/install.sh | sh");
    let out = run_capture(cmd)?;
    if !out.ok {
        return Err(format!(
            "uv installer failed\nstdout:\n{}\nstderr:\n{}",
            out.stdout, out.stderr
        ));
    }
    find_uv().ok_or_else(|| {
        "uv installer finished, but uv was not found in ~/.local/bin or PATH".to_string()
    })
}

fn emit_log(app: &AppHandle, stream: &str, line: String) {
    let _ = app.emit(
        "tracker-log",
        LogEvent {
            stream: stream.to_string(),
            line: line.clone(),
        },
    );
    if line.trim_start().starts_with('{') {
        if let Ok(value) = serde_json::from_str::<JsonValue>(&line) {
            if value.get("event").is_some() {
                let _ = app.emit("tracker-status", value);
            }
        }
    }
}

fn spawn_reader(app: AppHandle, stream: &'static str, reader: impl std::io::Read + Send + 'static) {
    thread::spawn(move || {
        let buf = BufReader::new(reader);
        for line in buf.lines() {
            match line {
                Ok(line) => emit_log(&app, stream, line),
                Err(err) => {
                    emit_log(&app, stream, format!("failed to read {stream}: {err}"));
                    break;
                }
            }
        }
    });
}

fn parse_config_targets(config: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let parsed: serde_yaml::Value = match serde_yaml::from_str(config) {
        Ok(value) => value,
        Err(_) => return out,
    };
    if let Some(cameras) = parsed.get("cameras").and_then(|v| v.as_sequence()) {
        for cam in cameras {
            let name = cam
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("camera")
                .to_string();
            if let Some(url) = cam.get("url").and_then(|v| v.as_str()) {
                if let Some(host) = rtsp_host(url) {
                    out.push((name, host));
                }
            }
        }
    }
    out
}

fn yaml_number_vec(value: Option<&serde_yaml::Value>) -> Vec<f64> {
    value
        .and_then(|v| v.as_sequence())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_f64().or_else(|| item.as_i64().map(|v| v as f64)))
                .collect()
        })
        .unwrap_or_default()
}

fn parse_projection_snapshot(config: &str) -> Result<ProjectionSnapshot, String> {
    let parsed: serde_yaml::Value =
        serde_yaml::from_str(config).map_err(|err| format!("YAML validation failed: {err}"))?;
    let projections = parsed
        .get("projections")
        .and_then(|v| v.as_sequence())
        .map(|items| {
            items
                .iter()
                .map(|projection| {
                    let zones = projection
                        .get("interaction_zones")
                        .and_then(|v| v.as_sequence())
                        .map(|zones| {
                            zones
                                .iter()
                                .map(|zone| ZoneInfo {
                                    id: zone
                                        .get("id")
                                        .and_then(|v| v.as_str())
                                        .unwrap_or("zone")
                                        .to_string(),
                                    uv_rect: yaml_number_vec(zone.get("uv_rect")),
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    ProjectionInfo {
                        id: projection
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("projection")
                            .to_string(),
                        pixel_size: yaml_number_vec(projection.get("pixel_size")),
                        world_size_m: yaml_number_vec(projection.get("world_size_m")),
                        zones,
                    }
                })
                .collect()
        })
        .unwrap_or_default();

    let mut camera_count = 0;
    let mut regions = Vec::new();
    if let Some(cameras) = parsed.get("cameras").and_then(|v| v.as_sequence()) {
        camera_count = cameras.len();
        for camera in cameras {
            let camera_name = camera
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("camera")
                .to_string();
            if let Some(region_items) = camera.get("regions").and_then(|v| v.as_sequence()) {
                for region in region_items {
                    regions.push(RegionInfo {
                        camera: camera_name.clone(),
                        id: region
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("region")
                            .to_string(),
                        projection_id: region
                            .get("projection_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("projection")
                            .to_string(),
                        projection_uv: yaml_number_vec(region.get("projection_uv")),
                        dispatch_uv: yaml_number_vec(region.get("dispatch_uv")),
                        min_bbox_height_px: region
                            .get("min_bbox_height_px")
                            .and_then(|v| v.as_f64().or_else(|| v.as_i64().map(|n| n as f64))),
                    });
                }
            }
        }
    }

    Ok(ProjectionSnapshot {
        projections,
        regions,
        camera_count,
    })
}

fn rtsp_host(url: &str) -> Option<String> {
    let after_scheme = url.split_once("://")?.1;
    let authority = after_scheme.split('/').next().unwrap_or(after_scheme);
    let host_port = authority
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(authority);
    let host = host_port.split(':').next().unwrap_or(host_port).trim();
    if host.is_empty() || host.contains('<') {
        None
    } else {
        Some(host.to_string())
    }
}

fn inspect_tracker_state(state: &AppState) -> Result<ProcessStatus, String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "tracker process lock poisoned".to_string())?;
    if let Some(child) = guard.as_mut() {
        if let Some(status) = child
            .try_wait()
            .map_err(|err| format!("failed to inspect tracker: {err}"))?
        {
            *guard = None;
            return Ok(ProcessStatus {
                running: false,
                exit_code: status.code(),
            });
        }
        return Ok(ProcessStatus {
            running: true,
            exit_code: None,
        });
    }
    Ok(ProcessStatus {
        running: false,
        exit_code: None,
    })
}

fn now_label() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    seconds.to_string()
}

fn field_check(id: &str, label: &str, status: &str, meta: String, detail: String) -> FieldCheck {
    FieldCheck {
        id: id.to_string(),
        label: label.to_string(),
        status: status.to_string(),
        meta,
        detail,
        ts: now_label(),
    }
}

fn rtsp_port_probe(host: &str) -> FieldCheck {
    let mut cmd = Command::new("nc");
    cmd.arg("-vz").arg("-G").arg("2").arg(host).arg("554");
    match run_capture(cmd) {
        Ok(out) if out.ok => field_check(
            "rtsp_port",
            "RTSP port probe",
            "ok",
            format!("{host}:554 reachable"),
            out.stderr.trim().to_string(),
        ),
        Ok(out) => field_check(
            "rtsp_port",
            "RTSP port probe",
            "warn",
            format!("{host}:554 not confirmed"),
            [out.stdout.trim(), out.stderr.trim()]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join("\n"),
        ),
        Err(err) => field_check(
            "rtsp_port",
            "RTSP port probe",
            "warn",
            format!("{host}:554 skipped"),
            err,
        ),
    }
}

fn summarize_checks(generated_at: String, checks: Vec<FieldCheck>, target_count: usize) -> FieldCheckReport {
    let ok_count = checks.iter().filter(|check| check.status == "ok").count();
    let warn_count = checks.iter().filter(|check| check.status == "warn").count();
    let fail_count = checks.iter().filter(|check| check.status == "fail").count();
    FieldCheckReport {
        generated_at,
        checks,
        target_count,
        ok_count,
        warn_count,
        fail_count,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        env,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn engine_source_dir_resolves_to_repo_root() {
        let source = engine_source_dir();
        for name in ENGINE_FILE_NAMES {
            assert!(
                source.join(name).exists(),
                "expected {} below {}",
                name,
                source.display()
            );
        }
    }

    #[test]
    fn copy_engine_files_from_copies_expected_runtime_snapshot() {
        let source = engine_source_dir();
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before unix epoch")
            .as_nanos();
        let target = env::temp_dir().join(format!(
            "reolink-tracker-app-copy-test-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&target);

        copy_engine_files_from(&source, &target).expect("engine copy should succeed");

        for name in ENGINE_FILE_NAMES {
            let copied = target.join(name);
            assert!(copied.exists(), "expected copied file {}", copied.display());
            assert_eq!(
                fs::read(&source.join(name)).expect("source should be readable"),
                fs::read(&copied).expect("copy should be readable"),
                "copied file should match source: {name}"
            );
        }

        fs::remove_dir_all(&target).expect("temp copy should be removable");
    }

    #[test]
    fn rtsp_host_parses_credentials_and_rejects_placeholders() {
        assert_eq!(
            rtsp_host("rtsp://admin:%21pass@192.168.1.20:554/h264Preview_01_sub"),
            Some("192.168.1.20".to_string())
        );
        assert_eq!(
            rtsp_host("rtsp://10.0.0.8:554/h264Preview_01_sub"),
            Some("10.0.0.8".to_string())
        );
        assert_eq!(
            rtsp_host("rtsp://admin:<password>@<camera-ip>:554/h264Preview_01_sub"),
            None
        );
    }

    #[test]
    fn parse_config_targets_reads_camera_hosts() {
        let config = r#"
cameras:
  - name: cam0
    url: rtsp://admin:%21pass@192.168.1.20:554/h264Preview_01_sub
  - name: cam1
    url: rtsp://admin:%21pass@192.168.1.21:554/h264Preview_01_sub
"#;
        assert_eq!(
            parse_config_targets(config),
            vec![
                ("cam0".to_string(), "192.168.1.20".to_string()),
                ("cam1".to_string(), "192.168.1.21".to_string())
            ]
        );
    }

    #[test]
    fn summarize_checks_counts_statuses() {
        let checks = vec![
            field_check("a", "A", "ok", "ready".to_string(), String::new()),
            field_check("b", "B", "warn", "attention".to_string(), String::new()),
            field_check("c", "C", "fail", "broken".to_string(), String::new()),
        ];
        let report = summarize_checks("now".to_string(), checks, 2);
        assert_eq!(report.ok_count, 1);
        assert_eq!(report.warn_count, 1);
        assert_eq!(report.fail_count, 1);
        assert_eq!(report.target_count, 2);
    }

    #[test]
    fn parse_projection_snapshot_reads_projection_regions_and_zones() {
        let config = r#"
projections:
  - id: corridor
    pixel_size: [9600, 1080]
    world_size_m: [40.0, 4.5]
    interaction_zones:
      - id: center
        uv_rect: [0.35, 0.15, 0.65, 0.85]
cameras:
  - name: cam0
    regions:
      - id: near
        projection_id: corridor
        projection_uv: [0.0, 0.0, 0.55, 1.0]
        dispatch_uv: [0.0, 0.0, 0.5, 1.0]
        min_bbox_height_px: 24
"#;
        let snapshot = parse_projection_snapshot(config).expect("snapshot should parse");
        assert_eq!(snapshot.camera_count, 1);
        assert_eq!(snapshot.projections[0].id, "corridor");
        assert_eq!(snapshot.projections[0].zones[0].id, "center");
        assert_eq!(snapshot.regions[0].camera, "cam0");
        assert_eq!(snapshot.regions[0].dispatch_uv, vec![0.0, 0.0, 0.5, 1.0]);
    }
}

#[tauri::command]
fn get_runtime_status(app: AppHandle) -> Result<RuntimeStatus, String> {
    let runtime = runtime_dir(&app)?;
    let engine = engine_dir(&app)?;
    let config = config_path(&app)?;
    let python = python_path(&app)?;
    Ok(RuntimeStatus {
        app_data_dir: app
            .path()
            .app_data_dir()
            .map_err(|err| format!("failed to resolve app data dir: {err}"))?
            .display()
            .to_string(),
        runtime_dir: runtime.display().to_string(),
        engine_dir: engine.display().to_string(),
        config_path: config.display().to_string(),
        python_path: python.display().to_string(),
        venv_exists: python.exists(),
        config_exists: config.exists(),
        model_exists: runtime.join("yolo26n.pt").exists(),
        tracker_exists: engine.join("tracker.py").exists(),
        uv_path: find_uv().map(|path| path.display().to_string()),
    })
}

#[tauri::command]
fn prepare_runtime(app: AppHandle) -> Result<RuntimeStatus, String> {
    let runtime = runtime_dir(&app)?;
    fs::create_dir_all(&runtime)
        .map_err(|err| format!("failed to create {}: {err}", runtime.display()))?;
    copy_engine_files(&app)?;

    let config = config_path(&app)?;
    if !config.exists() {
        copy_file(&engine_dir(&app)?.join("config.example.yaml"), &config)?;
    }

    let uv = install_uv()?;
    let python = python_path(&app)?;
    if !python.exists() {
        let mut install_python = Command::new(&uv);
        install_python.arg("python").arg("install").arg("3.12");
        let out = run_capture(install_python)?;
        if !out.ok {
            return Err(format!("uv python install failed\n{}", out.stderr));
        }

        let mut venv = Command::new(&uv);
        venv.arg("venv")
            .arg("--python")
            .arg("3.12")
            .arg(runtime.join(".venv"));
        let out = run_capture(venv)?;
        if !out.ok {
            return Err(format!("uv venv failed\n{}", out.stderr));
        }
    }

    let mut pip = Command::new(&uv);
    pip.arg("pip")
        .arg("install")
        .arg("--python")
        .arg(&python)
        .arg("-r")
        .arg(engine_dir(&app)?.join("requirements.txt"));
    let out = run_capture(pip)?;
    if !out.ok {
        return Err(format!("dependency install failed\n{}", out.stderr));
    }

    let mut model = Command::new(&python);
    model
        .current_dir(&runtime)
        .arg("-c")
        .arg("from ultralytics import YOLO; YOLO('yolo26n.pt'); print('model ready')");
    let out = run_capture(model)?;
    if !out.ok {
        return Err(format!("model warmup/download failed\n{}", out.stderr));
    }

    get_runtime_status(app)
}

#[tauri::command]
fn read_config(app: AppHandle) -> Result<String, String> {
    let path = config_path(&app)?;
    fs::read_to_string(&path).map_err(|err| format!("failed to read {}: {err}", path.display()))
}

#[tauri::command]
fn save_config(app: AppHandle, request: SaveConfigRequest) -> Result<(), String> {
    serde_yaml::from_str::<serde_yaml::Value>(&request.content)
        .map_err(|err| format!("YAML validation failed: {err}"))?;
    let path = config_path(&app)?;
    fs::write(&path, request.content)
        .map_err(|err| format!("failed to write {}: {err}", path.display()))
}

fn ensure_tracker_not_running(state: &AppState) -> Result<(), String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "tracker process lock poisoned".to_string())?;
    if let Some(child) = guard.as_mut() {
        if child
            .try_wait()
            .map_err(|err| format!("failed to inspect tracker: {err}"))?
            .is_none()
        {
            return Err("tracker is already running".to_string());
        }
        *guard = None;
    }
    Ok(())
}

fn spawn_tracker_with_config(
    app: AppHandle,
    state: &AppState,
    config: PathBuf,
    show_preview: bool,
) -> Result<ProcessStatus, String> {
    ensure_tracker_not_running(state)?;

    let python = python_path(&app)?;
    let tracker = engine_dir(&app)?.join("tracker.py");
    if !python.exists() {
        return Err("Python runtime is not ready. Run setup first.".to_string());
    }
    if !tracker.exists() {
        return Err("Tracker engine is not ready. Run setup first.".to_string());
    }
    if !config.exists() {
        return Err("config.yaml is missing. Run setup first.".to_string());
    }

    let mut cmd = Command::new(&python);
    cmd.current_dir(runtime_dir(&app)?)
        .arg(&tracker)
        .arg("--config")
        .arg(config)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if show_preview {
        cmd.arg("--show");
    }

    let mut child = cmd
        .spawn()
        .map_err(|err| format!("failed to start tracker: {err}"))?;
    if let Some(stdout) = child.stdout.take() {
        spawn_reader(app.clone(), "stdout", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        spawn_reader(app.clone(), "stderr", stderr);
    }

    let mut guard = state
        .child
        .lock()
        .map_err(|_| "tracker process lock poisoned".to_string())?;
    *guard = Some(child);
    Ok(ProcessStatus {
        running: true,
        exit_code: None,
    })
}

#[tauri::command]
fn start_tracker(
    app: AppHandle,
    state: tauri::State<AppState>,
    show_preview: bool,
) -> Result<ProcessStatus, String> {
    spawn_tracker_with_config(app.clone(), &state, config_path(&app)?, show_preview)
}

#[tauri::command]
fn start_video_test(
    app: AppHandle,
    state: tauri::State<AppState>,
    request: VideoTestRequest,
) -> Result<ProcessStatus, String> {
    let video_path = local_path_from_input(&request.video_path);
    if !video_path.exists() {
        return Err(format!("video file not found: {}", video_path.display()));
    }

    let config_text = fs::read_to_string(config_path(&app)?)
        .map_err(|err| format!("failed to read runtime config: {err}"))?;
    let mut config_value: serde_yaml::Value = serde_yaml::from_str(&config_text)
        .map_err(|err| format!("YAML validation failed: {err}"))?;

    let cameras = config_value
        .get_mut("cameras")
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "config is missing cameras[]".to_string())?;
    if cameras.is_empty() {
        return Err("config cameras[] is empty".to_string());
    }

    let target_name = request.camera_name.as_deref().unwrap_or("cam1");
    let index = cameras
        .iter()
        .position(|camera| {
            camera
                .get("name")
                .and_then(|value| value.as_str())
                .map(|name| name == target_name)
                .unwrap_or(false)
        })
        .unwrap_or(0);

    if let Some(camera) = cameras[index].as_mapping_mut() {
        camera.insert(
            serde_yaml::Value::String("name".to_string()),
            serde_yaml::Value::String(target_name.to_string()),
        );
        camera.insert(
            serde_yaml::Value::String("url".to_string()),
            serde_yaml::Value::String(video_path.display().to_string()),
        );
    }

    if let Some(osc) = config_value.get_mut("osc").and_then(|value| value.as_mapping_mut()) {
        osc.insert(
            serde_yaml::Value::String("td_minimal".to_string()),
            serde_yaml::Value::Bool(true),
        );
        osc.insert(
            serde_yaml::Value::String("raw_per_cam".to_string()),
            serde_yaml::Value::Bool(false),
        );
        osc.insert(
            serde_yaml::Value::String("zone_level".to_string()),
            serde_yaml::Value::Bool(false),
        );
    }

    let test_config = runtime_dir(&app)?.join("video-test-config.yaml");
    let test_config_text = serde_yaml::to_string(&config_value)
        .map_err(|err| format!("failed to serialize video test config: {err}"))?;
    fs::write(&test_config, test_config_text)
        .map_err(|err| format!("failed to write {}: {err}", test_config.display()))?;

    spawn_tracker_with_config(app, &state, test_config, request.show_preview)
}

#[tauri::command]
fn stop_tracker(state: tauri::State<AppState>) -> Result<ProcessStatus, String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "tracker process lock poisoned".to_string())?;
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let status = child
            .wait()
            .map_err(|err| format!("failed to wait for tracker: {err}"))?;
        return Ok(ProcessStatus {
            running: false,
            exit_code: status.code(),
        });
    }
    Ok(ProcessStatus {
        running: false,
        exit_code: None,
    })
}

#[tauri::command]
fn tracker_status(state: tauri::State<AppState>) -> Result<ProcessStatus, String> {
    inspect_tracker_state(&state)
}

#[tauri::command]
fn collect_network_report(app: AppHandle) -> Result<NetworkReport, String> {
    let interfaces = run_capture(Command::new("ifconfig"))?.stdout;
    let default_route = run_capture({
        let mut cmd = Command::new("route");
        cmd.arg("-n").arg("get").arg("default");
        cmd
    })?
    .stdout;
    let arp = run_capture({
        let mut cmd = Command::new("arp");
        cmd.arg("-an");
        cmd
    })?
    .stdout;

    let config = fs::read_to_string(config_path(&app)?).unwrap_or_default();
    let mut targets = Vec::new();
    for (name, host) in parse_config_targets(&config) {
        let route = run_capture({
            let mut cmd = Command::new("route");
            cmd.arg("-n").arg("get").arg(&host);
            cmd
        })?
        .stdout;
        targets.push(TargetRouteReport { name, host, route });
    }

    Ok(NetworkReport {
        interfaces,
        default_route,
        arp,
        targets,
    })
}

#[tauri::command]
fn run_field_checks(
    app: AppHandle,
    state: tauri::State<AppState>,
) -> Result<FieldCheckReport, String> {
    let generated_at = now_label();
    let runtime = get_runtime_status(app.clone())?;
    let process = inspect_tracker_state(&state)?;
    let config_text = fs::read_to_string(config_path(&app)?).unwrap_or_default();
    let targets = parse_config_targets(&config_text);
    let mut checks = Vec::new();

    let runtime_ready = runtime.venv_exists
        && runtime.config_exists
        && runtime.model_exists
        && runtime.tracker_exists;
    checks.push(field_check(
        "runtime_prepared",
        "Runtime prepared",
        if runtime_ready { "ok" } else { "warn" },
        if runtime_ready {
            "all runtime files ready".to_string()
        } else {
            "setup incomplete".to_string()
        },
        format!(
            "venv={} config={} model={} tracker={}",
            runtime.venv_exists, runtime.config_exists, runtime.model_exists, runtime.tracker_exists
        ),
    ));

    match serde_yaml::from_str::<serde_yaml::Value>(&config_text) {
        Ok(_) if targets.is_empty() => checks.push(field_check(
            "config_valid",
            "Config YAML",
            "warn",
            "valid YAML, no usable RTSP targets".to_string(),
            "Camera URLs may still contain placeholders.".to_string(),
        )),
        Ok(_) => checks.push(field_check(
            "config_valid",
            "Config YAML",
            "ok",
            format!("{} camera target(s)", targets.len()),
            "YAML parsed and RTSP hosts were extracted.".to_string(),
        )),
        Err(err) => checks.push(field_check(
            "config_valid",
            "Config YAML",
            "fail",
            "invalid YAML".to_string(),
            err.to_string(),
        )),
    }

    if targets.is_empty() {
        checks.push(field_check(
            "camera_routes",
            "Camera routes",
            "warn",
            "no camera hosts".to_string(),
            "Save real RTSP URLs in config.yaml, then run checks again.".to_string(),
        ));
    } else {
        let mut route_failures = Vec::new();
        for (_, host) in &targets {
            let route = run_capture({
                let mut cmd = Command::new("route");
                cmd.arg("-n").arg("get").arg(host);
                cmd
            })?;
            if !route.ok {
                route_failures.push(host.clone());
            }
        }
        checks.push(field_check(
            "camera_routes",
            "Camera routes",
            if route_failures.is_empty() { "ok" } else { "warn" },
            if route_failures.is_empty() {
                format!("{} route(s) resolved", targets.len())
            } else {
                format!("{} unresolved route(s)", route_failures.len())
            },
            if route_failures.is_empty() {
                "macOS route lookup completed for every camera host.".to_string()
            } else {
                route_failures.join(", ")
            },
        ));

        for (_, host) in targets.iter().take(2) {
            checks.push(rtsp_port_probe(host));
        }
    }

    checks.push(field_check(
        "tracker_process",
        "Tracker process",
        if process.running { "ok" } else { "warn" },
        if process.running {
            "running".to_string()
        } else {
            "stopped".to_string()
        },
        "Start or Preview launches the current tracker workflow.".to_string(),
    ));

    checks.push(field_check(
        "td_handshake",
        "TouchDesigner handshake",
        "warn",
        "sidecar required".to_string(),
        "TD ack needs an OSC listener/sidecar contract; this launcher does not own it yet.".to_string(),
    ));
    checks.push(field_check(
        "walk_test",
        "Walk test",
        "warn",
        "manual check".to_string(),
        "Automatic seam walk-test needs structured fusion history from tracker/sidecar.".to_string(),
    ));

    Ok(summarize_checks(generated_at, checks, targets.len()))
}

#[tauri::command]
fn read_projection_snapshot(app: AppHandle) -> Result<ProjectionSnapshot, String> {
    let config = fs::read_to_string(config_path(&app)?)
        .map_err(|err| format!("failed to read config for projection snapshot: {err}"))?;
    parse_projection_snapshot(&config)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            get_runtime_status,
            prepare_runtime,
            read_config,
            save_config,
            start_tracker,
            start_video_test,
            stop_tracker,
            tracker_status,
            collect_network_report,
            run_field_checks,
            read_projection_snapshot
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
