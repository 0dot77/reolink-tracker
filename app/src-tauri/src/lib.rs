use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::{
    collections::VecDeque,
    fs,
    io::{BufRead, BufReader, Write},
    net::{TcpListener, TcpStream, UdpSocket},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter, Manager};

struct AppState {
    child: Mutex<Option<Child>>,
    active_config: Mutex<Option<PathBuf>>,
    logs: Mutex<VecDeque<LogEvent>>,
    events: Mutex<VecDeque<JsonValue>>,
    mobile_token: String,
    mobile_port: Mutex<Option<u16>>,
    mobile_error: Mutex<Option<String>>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
            active_config: Mutex::new(None),
            logs: Mutex::new(VecDeque::new()),
            events: Mutex::new(VecDeque::new()),
            mobile_token: generate_mobile_token(),
            mobile_port: Mutex::new(None),
            mobile_error: Mutex::new(None),
        }
    }
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
struct MobileServerStatus {
    running: bool,
    bind: String,
    port: Option<u16>,
    token: String,
    urls: Vec<String>,
    status_path: String,
    token_header: String,
    error: Option<String>,
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
    image_points: Vec<Vec<f64>>,
    projection_uv: Vec<f64>,
    dispatch_uv: Vec<f64>,
    min_bbox_height_px: Option<f64>,
    body_catch_points: Vec<Vec<f64>>,
    relaxed_presence_points: Vec<Vec<f64>>,
    relaxed_presence_uv: Vec<f64>,
    relaxed_presence_margin_uv: Option<f64>,
    relaxed_presence_min_confidence: Option<f64>,
    relaxed_presence_v: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
struct ProjectionSnapshot {
    projections: Vec<ProjectionInfo>,
    regions: Vec<RegionInfo>,
    camera_count: usize,
}

#[derive(Debug, Serialize)]
struct CalibrationFrame {
    camera: String,
    path: String,
    width: u32,
    height: u32,
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

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CaptureCalibrationFrameRequest {
    camera_name: Option<String>,
    video_path: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SaveCalibrationPointsRequest {
    camera_name: String,
    region_id: Option<String>,
    image_points: Vec<[f64; 2]>,
    point_kind: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SaveCalibrationMappingRequest {
    camera_name: String,
    region_id: String,
    projection_uv: Option<[f64; 4]>,
    dispatch_uv: Option<[f64; 4]>,
    relaxed_presence_uv: Option<[f64; 4]>,
    relaxed_presence_v: Option<f64>,
}

const MOBILE_TOKEN_HEADER: &str = "X-Reolink-Mobile-Token";
const MOBILE_BIND_ADDR: &str = "0.0.0.0";
const MOBILE_LOG_LIMIT: usize = 500;
const MOBILE_EVENT_LIMIT: usize = 200;
const MOBILE_API_LOG_LIMIT: usize = 80;
const MOBILE_API_EVENT_LIMIT: usize = 80;
const MOBILE_PREVIEW_REQUEST_FILE: &str = ".request";
const MOBILE_PREVIEW_MAX_WIDTH: &str = "640";
const MOBILE_PREVIEW_INTERVAL_S: &str = "0.75";
const MOBILE_PREVIEW_REQUEST_TTL_S: &str = "4.0";
const MOBILE_PREVIEW_JPEG_QUALITY: &str = "65";

fn generate_mobile_token() -> String {
    let seed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as u64)
        .unwrap_or_default()
        ^ ((std::process::id() as u64) << 17);
    let mixed = seed
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407);
    format!("{:06}", mixed % 1_000_000)
}

fn mobile_token_matches(expected: &str, provided: Option<&str>) -> bool {
    let Some(candidate) = provided.map(str::trim) else {
        return false;
    };
    if expected.is_empty() || candidate.len() != expected.len() {
        return false;
    }
    expected
        .bytes()
        .zip(candidate.bytes())
        .fold(0_u8, |acc, (left, right)| acc | (left ^ right))
        == 0
}

fn select_mobile_port<I, F>(ports: I, mut can_bind: F) -> Option<u16>
where
    I: IntoIterator<Item = u16>,
    F: FnMut(u16) -> bool,
{
    ports.into_iter().find(|port| can_bind(*port))
}

fn push_bounded<T>(items: &mut VecDeque<T>, item: T, limit: usize) {
    items.push_back(item);
    while items.len() > limit {
        let _ = items.pop_front();
    }
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

fn mobile_preview_dir(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(runtime_dir(app)?.join("preview"))
}

fn mobile_preview_request_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(mobile_preview_dir(app)?.join(MOBILE_PREVIEW_REQUEST_FILE))
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

fn write_text_atomically(path: &Path, content: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create {}: {err}", parent.display()))?;
    }
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("config.yaml");
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let tmp_path =
        path.with_file_name(format!(".{file_name}.tmp-{}-{}", std::process::id(), nonce));
    fs::write(&tmp_path, content)
        .map_err(|err| format!("failed to write {}: {err}", tmp_path.display()))?;
    fs::rename(&tmp_path, path).map_err(|err| {
        let _ = fs::remove_file(&tmp_path);
        format!(
            "failed to replace {} with {}: {err}",
            path.display(),
            tmp_path.display()
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
    let log = LogEvent {
        stream: stream.to_string(),
        line: line.clone(),
    };
    if let Ok(mut logs) = app.state::<AppState>().logs.lock() {
        push_bounded(&mut logs, log.clone(), MOBILE_LOG_LIMIT);
    }
    let _ = app.emit("tracker-log", log);
    if line.trim_start().starts_with('{') {
        if let Ok(value) = serde_json::from_str::<JsonValue>(&line) {
            if value.get("event").is_some() {
                if let Ok(mut events) = app.state::<AppState>().events.lock() {
                    push_bounded(&mut events, value.clone(), MOBILE_EVENT_LIMIT);
                }
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

fn parse_config_camera_names(config: &str) -> Vec<String> {
    let parsed: serde_yaml::Value = match serde_yaml::from_str(config) {
        Ok(value) => value,
        Err(_) => return Vec::new(),
    };
    parsed
        .get("cameras")
        .and_then(|v| v.as_sequence())
        .map(|cameras| {
            cameras
                .iter()
                .filter_map(|camera| camera.get("name").and_then(|v| v.as_str()))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
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

fn yaml_points(value: Option<&serde_yaml::Value>) -> Vec<Vec<f64>> {
    value
        .and_then(|v| v.as_sequence())
        .map(|points| {
            points
                .iter()
                .map(|point| yaml_number_vec(Some(point)))
                .collect()
        })
        .unwrap_or_default()
}

fn yaml_optional_f64(value: Option<&serde_yaml::Value>) -> Option<f64> {
    value.and_then(|v| v.as_f64().or_else(|| v.as_i64().map(|n| n as f64)))
}

fn validate_unit_rect(rect: [f64; 4], label: &str) -> Result<[f64; 4], String> {
    if rect.iter().any(|value| !(0.0..=1.0).contains(value)) {
        return Err(format!("{label} must stay inside 0..1"));
    }
    if rect[0] >= rect[2] || rect[1] >= rect[3] {
        return Err(format!("{label} must satisfy min < max"));
    }
    Ok(rect)
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
                        image_points: region
                            .get("image_points")
                            .map(|value| yaml_points(Some(value)))
                            .unwrap_or_default(),
                        projection_uv: yaml_number_vec(region.get("projection_uv")),
                        dispatch_uv: yaml_number_vec(region.get("dispatch_uv")),
                        min_bbox_height_px: yaml_optional_f64(region.get("min_bbox_height_px")),
                        body_catch_points: yaml_points(region.get("body_catch_points")),
                        relaxed_presence_points: yaml_points(
                            region
                                .get("relaxed_presence_points")
                                .or_else(|| region.get("stair_catch_points")),
                        ),
                        relaxed_presence_margin_uv: yaml_optional_f64(
                            region.get("relaxed_presence_margin_uv"),
                        ),
                        relaxed_presence_min_confidence: yaml_optional_f64(
                            region
                                .get("relaxed_presence_min_confidence")
                                .or_else(|| region.get("stair_catch_min_confidence")),
                        ),
                        relaxed_presence_uv: yaml_number_vec(region.get("relaxed_presence_uv")),
                        relaxed_presence_v: yaml_optional_f64(region.get("relaxed_presence_v")),
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

fn video_test_config_text(
    config_text: &str,
    video_path: &Path,
    camera_name: Option<&str>,
) -> Result<String, String> {
    let mut config_value: serde_yaml::Value = serde_yaml::from_str(config_text)
        .map_err(|err| format!("YAML validation failed: {err}"))?;

    let cameras = config_value
        .get_mut("cameras")
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "config is missing cameras[]".to_string())?;
    if cameras.is_empty() {
        return Err("config cameras[] is empty".to_string());
    }

    let requested_name = camera_name.map(str::trim).filter(|name| !name.is_empty());
    let index = if let Some(target_name) = requested_name {
        cameras
            .iter()
            .position(|camera| {
                camera
                    .get("name")
                    .and_then(|value| value.as_str())
                    .map(|name| name == target_name)
                    .unwrap_or(false)
            })
            .ok_or_else(|| format!("camera not found in config: {target_name}"))?
    } else {
        0
    };
    let target_name = cameras[index]
        .get("name")
        .and_then(|value| value.as_str())
        .or(requested_name)
        .unwrap_or("camera")
        .to_string();

    let camera = cameras[index]
        .as_mapping_mut()
        .ok_or_else(|| "target camera entry must be a YAML mapping".to_string())?;
    camera.insert(
        serde_yaml::Value::String("name".to_string()),
        serde_yaml::Value::String(target_name),
    );
    camera.insert(
        serde_yaml::Value::String("url".to_string()),
        serde_yaml::Value::String(video_path.display().to_string()),
    );

    if !config_value
        .get("osc")
        .map(|value| value.is_mapping())
        .unwrap_or(false)
    {
        if let Some(root) = config_value.as_mapping_mut() {
            root.insert(
                serde_yaml::Value::String("osc".to_string()),
                serde_yaml::Value::Mapping(Default::default()),
            );
        }
    }
    let osc = config_value
        .get_mut("osc")
        .and_then(|value| value.as_mapping_mut())
        .ok_or_else(|| "osc config must be a YAML mapping".to_string())?;
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

    serde_yaml::to_string(&config_value)
        .map_err(|err| format!("failed to serialize video test config: {err}"))
}

fn first_projection_id(config_value: &serde_yaml::Value) -> String {
    config_value
        .get("projections")
        .and_then(|value| value.as_sequence())
        .and_then(|items| items.first())
        .and_then(|projection| projection.get("id"))
        .and_then(|value| value.as_str())
        .unwrap_or("corridor")
        .to_string()
}

fn camera_url_from_config(
    config_text: &str,
    camera_name: Option<&str>,
) -> Result<(String, String), String> {
    let config_value: serde_yaml::Value = serde_yaml::from_str(config_text)
        .map_err(|err| format!("YAML validation failed: {err}"))?;
    let cameras = config_value
        .get("cameras")
        .and_then(|value| value.as_sequence())
        .ok_or_else(|| "config is missing cameras[]".to_string())?;
    if cameras.is_empty() {
        return Err("config cameras[] is empty".to_string());
    }
    let requested_name = camera_name.map(str::trim).filter(|name| !name.is_empty());
    let camera = if let Some(target_name) = requested_name {
        cameras
            .iter()
            .find(|camera| {
                camera
                    .get("name")
                    .and_then(|value| value.as_str())
                    .map(|name| name == target_name)
                    .unwrap_or(false)
            })
            .ok_or_else(|| format!("camera not found in config: {target_name}"))?
    } else {
        &cameras[0]
    };
    let name = camera
        .get("name")
        .and_then(|value| value.as_str())
        .or(requested_name)
        .unwrap_or("camera")
        .to_string();
    let url = camera
        .get("url")
        .and_then(|value| value.as_str())
        .ok_or_else(|| format!("{name}.url is missing"))?;
    Ok((name, url.to_string()))
}

fn calibration_config_text(
    config_text: &str,
    request: &SaveCalibrationPointsRequest,
) -> Result<String, String> {
    if request.image_points.len() != 4 {
        return Err("image_points must contain exactly 4 points".to_string());
    }
    let point_key = match request.point_kind.as_deref().unwrap_or("floor") {
        "floor" | "image_points" => "image_points",
        "body" | "body_catch" | "body_catch_points" => "body_catch_points",
        "stair" | "relaxed" | "relaxed_presence_points" | "stair_catch_points" => {
            "relaxed_presence_points"
        }
        other => return Err(format!("unsupported calibration point kind: {other}")),
    };
    let mut config_value: serde_yaml::Value = serde_yaml::from_str(config_text)
        .map_err(|err| format!("YAML validation failed: {err}"))?;
    let default_projection_id = first_projection_id(&config_value);
    let cameras = config_value
        .get_mut("cameras")
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "config is missing cameras[]".to_string())?;
    let camera = cameras
        .iter_mut()
        .find(|camera| {
            camera
                .get("name")
                .and_then(|value| value.as_str())
                .map(|name| name == request.camera_name)
                .unwrap_or(false)
        })
        .ok_or_else(|| format!("camera not found: {}", request.camera_name))?;
    let camera_mapping = camera
        .as_mapping_mut()
        .ok_or_else(|| "target camera entry must be a YAML mapping".to_string())?;
    let regions_key = serde_yaml::Value::String("regions".to_string());
    if !camera_mapping.contains_key(&regions_key) {
        camera_mapping.insert(regions_key.clone(), serde_yaml::Value::Sequence(Vec::new()));
    }
    let regions = camera_mapping
        .get_mut(&regions_key)
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "camera regions must be a YAML sequence".to_string())?;
    let target_region_id = request
        .region_id
        .as_deref()
        .filter(|id| !id.trim().is_empty())
        .unwrap_or("app_calibration");
    let existing_index = regions.iter().position(|region| {
        region
            .get("id")
            .and_then(|value| value.as_str())
            .map(|id| id == target_region_id)
            .unwrap_or(false)
    });
    let index = match existing_index {
        Some(index) => index,
        None if point_key == "image_points" && regions.is_empty() => {
            let mut region = serde_yaml::Mapping::new();
            region.insert(
                serde_yaml::Value::String("id".to_string()),
                serde_yaml::Value::String(target_region_id.to_string()),
            );
            region.insert(
                serde_yaml::Value::String("projection_id".to_string()),
                serde_yaml::Value::String(default_projection_id),
            );
            region.insert(
                serde_yaml::Value::String("projection_uv".to_string()),
                serde_yaml::to_value([0.0_f64, 0.0, 1.0, 1.0]).unwrap_or_default(),
            );
            region.insert(
                serde_yaml::Value::String("dispatch_uv".to_string()),
                serde_yaml::to_value([0.0_f64, 0.0, 1.0, 1.0]).unwrap_or_default(),
            );
            regions.push(serde_yaml::Value::Mapping(region));
            regions.len() - 1
        }
        None if point_key == "image_points" => 0,
        None => return Err("catch points must be attached to an existing floor region".to_string()),
    };
    let region = regions[index]
        .as_mapping_mut()
        .ok_or_else(|| "target region entry must be a YAML mapping".to_string())?;
    region.insert(
        serde_yaml::Value::String(point_key.to_string()),
        serde_yaml::to_value(&request.image_points)
            .map_err(|err| format!("failed to serialize image points: {err}"))?,
    );

    serde_yaml::to_string(&config_value)
        .map_err(|err| format!("failed to serialize calibration config: {err}"))
}

fn calibration_mapping_config_text(
    config_text: &str,
    request: &SaveCalibrationMappingRequest,
) -> Result<String, String> {
    let mut config_value: serde_yaml::Value = serde_yaml::from_str(config_text)
        .map_err(|err| format!("YAML validation failed: {err}"))?;
    let cameras = config_value
        .get_mut("cameras")
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "config is missing cameras[]".to_string())?;
    let camera = cameras
        .iter_mut()
        .find(|camera| {
            camera
                .get("name")
                .and_then(|value| value.as_str())
                .map(|name| name == request.camera_name)
                .unwrap_or(false)
        })
        .ok_or_else(|| format!("camera not found: {}", request.camera_name))?;
    let regions = camera
        .get_mut("regions")
        .and_then(|value| value.as_sequence_mut())
        .ok_or_else(|| "camera regions must be a YAML sequence".to_string())?;
    let region = regions
        .iter_mut()
        .find(|region| {
            region
                .get("id")
                .and_then(|value| value.as_str())
                .map(|id| id == request.region_id)
                .unwrap_or(false)
        })
        .ok_or_else(|| format!("region not found: {}", request.region_id))?;
    let region = region
        .as_mapping_mut()
        .ok_or_else(|| "target region entry must be a YAML mapping".to_string())?;
    let has_relaxed_presence_points = region
        .get("relaxed_presence_points")
        .and_then(|value| value.as_sequence())
        .map(|points| !points.is_empty())
        .unwrap_or(false);

    if let Some(projection_uv) = request.projection_uv {
        let projection_uv = validate_unit_rect(projection_uv, "projection_uv")?;
        region.insert(
            serde_yaml::Value::String("projection_uv".to_string()),
            serde_yaml::to_value(projection_uv)
                .map_err(|err| format!("failed to serialize projection_uv: {err}"))?,
        );
    }
    if let Some(dispatch_uv) = request.dispatch_uv {
        let dispatch_uv = validate_unit_rect(dispatch_uv, "dispatch_uv")?;
        if let Some(projection_uv) = request.projection_uv {
            if dispatch_uv[0] < projection_uv[0]
                || dispatch_uv[1] < projection_uv[1]
                || dispatch_uv[2] > projection_uv[2]
                || dispatch_uv[3] > projection_uv[3]
            {
                return Err("dispatch_uv must stay inside projection_uv".to_string());
            }
        }
        region.insert(
            serde_yaml::Value::String("dispatch_uv".to_string()),
            serde_yaml::to_value(dispatch_uv)
                .map_err(|err| format!("failed to serialize dispatch_uv: {err}"))?,
        );
    }
    if let Some(relaxed_presence_uv) = request.relaxed_presence_uv {
        if !has_relaxed_presence_points {
            return Err(
                "relaxed_presence_uv requires relaxed_presence_points on the target region"
                    .to_string(),
            );
        }
        let relaxed_presence_uv = validate_unit_rect(relaxed_presence_uv, "relaxed_presence_uv")?;
        region.insert(
            serde_yaml::Value::String("relaxed_presence_uv".to_string()),
            serde_yaml::to_value(relaxed_presence_uv)
                .map_err(|err| format!("failed to serialize relaxed_presence_uv: {err}"))?,
        );
    }
    if let Some(relaxed_presence_v) = request.relaxed_presence_v {
        let clamped = relaxed_presence_v.clamp(0.0, 1.0);
        region.insert(
            serde_yaml::Value::String("relaxed_presence_v".to_string()),
            serde_yaml::to_value(clamped)
                .map_err(|err| format!("failed to serialize relaxed_presence_v: {err}"))?,
        );
    }

    serde_yaml::to_string(&config_value)
        .map_err(|err| format!("failed to serialize calibration config: {err}"))
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
            if let Ok(mut active_config) = state.active_config.lock() {
                *active_config = None;
            }
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

fn summarize_checks(
    generated_at: String,
    checks: Vec<FieldCheck>,
    target_count: usize,
) -> FieldCheckReport {
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

fn parse_ifconfig_ipv4(output: &str) -> Vec<String> {
    output
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim_start();
            let rest = trimmed.strip_prefix("inet ")?;
            let ip = rest.split_whitespace().next()?;
            if ip == "127.0.0.1" || ip.starts_with("169.254.") || ip.contains(':') {
                None
            } else {
                Some(ip.to_string())
            }
        })
        .collect()
}

fn local_ipv4_candidates() -> Vec<String> {
    let mut ips = Vec::new();
    if let Ok(socket) = UdpSocket::bind("0.0.0.0:0") {
        let _ = socket.connect("8.8.8.8:80");
        if let Ok(addr) = socket.local_addr() {
            let ip = addr.ip().to_string();
            if ip != "0.0.0.0" && ip != "127.0.0.1" {
                ips.push(ip);
            }
        }
    }
    if let Ok(output) = Command::new("ifconfig").output() {
        let text = String::from_utf8_lossy(&output.stdout);
        ips.extend(parse_ifconfig_ipv4(&text));
    }
    ips.sort();
    ips.dedup();
    ips
}

fn mobile_urls(port: u16) -> Vec<String> {
    let mut urls = local_ipv4_candidates()
        .into_iter()
        .map(|ip| format!("http://{ip}:{port}/mobile"))
        .collect::<Vec<_>>();
    urls.push(format!("http://127.0.0.1:{port}/mobile"));
    urls.dedup();
    urls
}

fn mobile_server_status(app: &AppHandle) -> MobileServerStatus {
    let state = app.state::<AppState>();
    let port = state.mobile_port.lock().ok().and_then(|guard| *guard);
    let error = state
        .mobile_error
        .lock()
        .ok()
        .and_then(|guard| guard.clone());
    MobileServerStatus {
        running: port.is_some() && error.is_none(),
        bind: MOBILE_BIND_ADDR.to_string(),
        urls: port.map(mobile_urls).unwrap_or_default(),
        port,
        token: state.mobile_token.clone(),
        status_path: "/api/status".to_string(),
        token_header: MOBILE_TOKEN_HEADER.to_string(),
        error,
    }
}

fn snapshot_logs(state: &AppState, limit: usize) -> Vec<LogEvent> {
    state
        .logs
        .lock()
        .map(|logs| {
            let start = logs.len().saturating_sub(limit);
            logs.iter().skip(start).cloned().collect()
        })
        .unwrap_or_default()
}

fn snapshot_events(state: &AppState, limit: usize) -> Vec<JsonValue> {
    state
        .events
        .lock()
        .map(|events| {
            let start = events.len().saturating_sub(limit);
            events.iter().skip(start).cloned().collect()
        })
        .unwrap_or_default()
}

fn runtime_ready_status(runtime: &RuntimeStatus) -> bool {
    runtime.venv_exists && runtime.config_exists && runtime.model_exists && runtime.tracker_exists
}

fn mobile_status_json(
    runtime: RuntimeStatus,
    process: ProcessStatus,
    configured_cameras: Vec<String>,
    events: Vec<JsonValue>,
    logs: Vec<LogEvent>,
) -> JsonValue {
    let latest_fps = events
        .iter()
        .rev()
        .find(|event| event.get("event").and_then(|value| value.as_str()) == Some("fps_tick"));
    let cameras = latest_fps
        .and_then(|event| event.get("cameras"))
        .cloned()
        .unwrap_or_else(|| serde_json::json!([]));
    let projections = latest_fps
        .and_then(|event| event.get("projections"))
        .cloned()
        .unwrap_or_else(|| serde_json::json!([]));
    let camera_count = cameras
        .as_array()
        .map(Vec::len)
        .filter(|count| *count > 0)
        .unwrap_or(configured_cameras.len());
    let osc_rate = cameras
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|camera| camera.get("osc_rate").and_then(|value| value.as_f64()))
                .sum::<f64>()
        })
        .unwrap_or_default();
    let latest_event = events
        .last()
        .and_then(|event| event.get("event"))
        .and_then(|value| value.as_str())
        .unwrap_or("-");
    serde_json::json!({
        "ok": true,
        "generated_at": now_label(),
        "summary": {
            "runtime_ready": runtime_ready_status(&runtime),
            "camera_count": camera_count,
            "osc_rate": osc_rate,
            "latest_event": latest_event,
            "log_count": logs.len(),
            "event_count": events.len()
        },
        "runtime": runtime,
        "process": process,
        "configured_cameras": configured_cameras,
        "cameras": cameras,
        "projections": projections,
        "events": events,
        "logs": logs
    })
}

fn mobile_status_for_app(app: &AppHandle) -> Result<JsonValue, String> {
    let runtime = get_runtime_status(app.clone())?;
    let configured_cameras = fs::read_to_string(config_path(app)?)
        .map(|config| parse_config_camera_names(&config))
        .unwrap_or_default();
    let state = app.state::<AppState>();
    let process = inspect_tracker_state(&state)?;
    Ok(mobile_status_json(
        runtime,
        process,
        configured_cameras,
        snapshot_events(&state, MOBILE_API_EVENT_LIMIT),
        snapshot_logs(&state, MOBILE_API_LOG_LIMIT),
    ))
}

fn bind_mobile_listener() -> Result<(TcpListener, u16), String> {
    let mut listener = None;
    let mut last_error = None;
    let port = select_mobile_port(1421..=1430, |port| {
        match TcpListener::bind((MOBILE_BIND_ADDR, port)) {
            Ok(value) => {
                listener = Some(value);
                true
            }
            Err(err) => {
                last_error = Some(format!("{MOBILE_BIND_ADDR}:{port}: {err}"));
                false
            }
        }
    })
    .ok_or_else(|| {
        format!(
            "failed to bind mobile server on ports 1421..1430{}",
            last_error
                .map(|err| format!("; last error: {err}"))
                .unwrap_or_default()
        )
    })?;
    let listener = listener.ok_or_else(|| "selected mobile port without listener".to_string())?;
    Ok((listener, port))
}

fn set_mobile_server_state(app: &AppHandle, port: Option<u16>, error: Option<String>) {
    let state = app.state::<AppState>();
    if let Ok(mut mobile_port) = state.mobile_port.lock() {
        *mobile_port = port;
    }
    if let Ok(mut mobile_error) = state.mobile_error.lock() {
        *mobile_error = error;
    };
}

fn start_mobile_server(app: AppHandle) {
    thread::spawn(move || match bind_mobile_listener() {
        Ok((listener, port)) => {
            set_mobile_server_state(&app, Some(port), None);
            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => {
                        let app = app.clone();
                        thread::spawn(move || handle_mobile_connection(stream, app));
                    }
                    Err(err) => {
                        set_mobile_server_state(&app, Some(port), Some(err.to_string()));
                        break;
                    }
                }
            }
        }
        Err(err) => {
            set_mobile_server_state(&app, None, Some(err));
        }
    });
}

struct HttpRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
}

fn read_http_request(stream: &TcpStream) -> Result<HttpRequest, String> {
    let reader_stream = stream
        .try_clone()
        .map_err(|err| format!("failed to clone mobile HTTP stream: {err}"))?;
    let mut reader = BufReader::new(reader_stream);
    let mut first_line = String::new();
    reader
        .read_line(&mut first_line)
        .map_err(|err| format!("failed to read request line: {err}"))?;
    let mut parts = first_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path = parts.next().unwrap_or_default().to_string();
    if method.is_empty() || path.is_empty() {
        return Err("empty mobile HTTP request".to_string());
    }

    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        reader
            .read_line(&mut line)
            .map_err(|err| format!("failed to read request header: {err}"))?;
        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            headers.push((name.trim().to_ascii_lowercase(), value.trim().to_string()));
        }
    }

    Ok(HttpRequest {
        method,
        path,
        headers,
    })
}

fn request_header<'a>(request: &'a HttpRequest, name: &str) -> Option<&'a str> {
    let wanted = name.to_ascii_lowercase();
    request
        .headers
        .iter()
        .find(|(header, _)| header == &wanted)
        .map(|(_, value)| value.as_str())
}

fn write_http_response(stream: &mut TcpStream, status: &str, content_type: &str, body: &str) {
    let header = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.as_bytes().len()
    );
    let _ = stream.write_all(header.as_bytes());
    let _ = stream.write_all(body.as_bytes());
}

fn write_http_bytes(stream: &mut TcpStream, status: &str, content_type: &str, body: &[u8]) {
    let header = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(header.as_bytes());
    let _ = stream.write_all(body);
}

fn write_http_redirect(stream: &mut TcpStream, location: &str) {
    let header = format!(
        "HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
    );
    let _ = stream.write_all(header.as_bytes());
}

fn write_json_response(stream: &mut TcpStream, status: &str, value: JsonValue) {
    write_http_response(stream, status, "application/json", &value.to_string());
}

fn write_json_error(stream: &mut TcpStream, status: &str, message: &str) {
    write_json_response(
        stream,
        status,
        serde_json::json!({
            "ok": false,
            "error": message
        }),
    );
}

fn percent_decode_path_segment(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hi = bytes.get(i + 1).copied()?;
            let lo = bytes.get(i + 2).copied()?;
            let hex = [hi, lo];
            let decoded = u8::from_str_radix(std::str::from_utf8(&hex).ok()?, 16).ok()?;
            out.push(decoded);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

fn safe_preview_camera_name(value: &str) -> Option<String> {
    let decoded = percent_decode_path_segment(value)?;
    if decoded.is_empty()
        || decoded
            .chars()
            .any(|ch| !(ch.is_ascii_alphanumeric() || ch == '-' || ch == '_'))
    {
        None
    } else {
        Some(decoded)
    }
}

fn preview_camera_from_route(route: &str) -> Option<String> {
    let name = route
        .strip_prefix("/api/preview/")
        .and_then(|rest| rest.strip_suffix(".jpg"))?;
    safe_preview_camera_name(name)
}

fn touch_mobile_preview_request(app: &AppHandle) -> Result<(), String> {
    let request_path = mobile_preview_request_path(app)?;
    if let Some(parent) = request_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create {}: {err}", parent.display()))?;
    }
    fs::write(&request_path, now_label())
        .map_err(|err| format!("failed to request mobile preview: {err}"))
}

fn handle_mobile_preview_request(
    stream: &mut TcpStream,
    app: &AppHandle,
    camera_name: &str,
) -> Result<(), String> {
    touch_mobile_preview_request(app)?;
    let path = mobile_preview_dir(app)?.join(format!("{camera_name}.jpg"));
    match fs::read(&path) {
        Ok(bytes) => {
            write_http_bytes(stream, "200 OK", "image/jpeg", &bytes);
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            write_json_error(
                stream,
                "404 Not Found",
                "Preview frame is not ready yet. Keep View open for a moment.",
            );
            Ok(())
        }
        Err(err) => Err(format!("failed to read {}: {err}", path.display())),
    }
}

fn handle_mobile_connection(mut stream: TcpStream, app: AppHandle) {
    let request = match read_http_request(&stream) {
        Ok(request) => request,
        Err(err) => {
            write_json_error(&mut stream, "400 Bad Request", &err);
            return;
        }
    };
    let route = request.path.split('?').next().unwrap_or(&request.path);
    if let Some(camera_name) = preview_camera_from_route(route) {
        if request.method != "GET" {
            write_json_error(
                &mut stream,
                "405 Method Not Allowed",
                "Use GET for preview.",
            );
            return;
        }
        let state = app.state::<AppState>();
        if !mobile_token_matches(
            &state.mobile_token,
            request_header(&request, MOBILE_TOKEN_HEADER),
        ) {
            write_json_error(&mut stream, "401 Unauthorized", "Invalid mobile PIN.");
            return;
        }
        if let Err(err) = handle_mobile_preview_request(&mut stream, &app, &camera_name) {
            write_json_error(&mut stream, "500 Internal Server Error", &err);
        }
        return;
    }
    match (request.method.as_str(), route) {
        ("GET", "/") => write_http_redirect(&mut stream, "/mobile"),
        ("GET", "/mobile") => {
            write_http_response(&mut stream, "200 OK", "text/html", mobile_page_html());
        }
        ("GET", "/api/status") | ("POST", "/api/start") | ("POST", "/api/stop") => {
            let state = app.state::<AppState>();
            if !mobile_token_matches(
                &state.mobile_token,
                request_header(&request, MOBILE_TOKEN_HEADER),
            ) {
                write_json_error(&mut stream, "401 Unauthorized", "Invalid mobile PIN.");
                return;
            }
            match (request.method.as_str(), route) {
                ("GET", "/api/status") => match mobile_status_for_app(&app) {
                    Ok(value) => write_json_response(&mut stream, "200 OK", value),
                    Err(err) => write_json_error(&mut stream, "500 Internal Server Error", &err),
                },
                ("POST", "/api/start") => {
                    let result = spawn_tracker_with_config(
                        app.clone(),
                        &state,
                        match config_path(&app) {
                            Ok(path) => path,
                            Err(err) => {
                                write_json_error(&mut stream, "500 Internal Server Error", &err);
                                return;
                            }
                        },
                        false,
                    );
                    match result {
                        Ok(_) => match mobile_status_for_app(&app) {
                            Ok(value) => write_json_response(&mut stream, "200 OK", value),
                            Err(err) => {
                                write_json_error(&mut stream, "500 Internal Server Error", &err)
                            }
                        },
                        Err(err) => write_json_error(&mut stream, "409 Conflict", &err),
                    }
                }
                ("POST", "/api/stop") => match stop_tracker_state(&state) {
                    Ok(_) => match mobile_status_for_app(&app) {
                        Ok(value) => write_json_response(&mut stream, "200 OK", value),
                        Err(err) => {
                            write_json_error(&mut stream, "500 Internal Server Error", &err)
                        }
                    },
                    Err(err) => write_json_error(&mut stream, "500 Internal Server Error", &err),
                },
                _ => write_json_error(&mut stream, "404 Not Found", "Not found."),
            }
        }
        _ => write_json_error(&mut stream, "404 Not Found", "Not found."),
    }
}

fn mobile_page_html() -> &'static str {
    r#"<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Reolink Mobile Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #08090b;
      --panel: #12151a;
      --panel-2: #181c22;
      --line: #2a303a;
      --ink: #f0eadc;
      --muted: #918b7d;
      --amber: #ff8a3d;
      --green: #7bd88f;
      --red: #ff4757;
      --yellow: #ffd166;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100svh;
      background: var(--bg);
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
    }
    button, input { font: inherit; }
    .app {
      min-height: 100svh;
      padding: max(18px, env(safe-area-inset-top)) 14px calc(96px + env(safe-area-inset-bottom));
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      padding: 5px 9px;
    }
    .hero {
      margin-bottom: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #151820, #101217);
      padding: 18px;
    }
    .state {
      color: var(--yellow);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: clamp(36px, 12vw, 54px);
      font-weight: 300;
      line-height: 0.95;
      letter-spacing: 0;
    }
    .state.running { color: var(--green); }
    .meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
      margin-top: 16px;
    }
    .tile, .card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .tile { padding: 12px; }
    .tile span, .label {
      display: block;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .tile b {
      display: block;
      margin-top: 7px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 18px;
      font-weight: 500;
      overflow-wrap: anywhere;
    }
    .card {
      margin-top: 12px;
      overflow: hidden;
    }
    .card h2 {
      margin: 0;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.12em;
      padding: 11px 13px;
      text-transform: uppercase;
    }
    .card-body { padding: 12px 13px; }
    .row {
      display: grid;
      grid-template-columns: minmax(82px, .7fr) minmax(0, 1fr);
      gap: 10px;
      border-bottom: 1px solid rgba(42, 48, 58, .75);
      padding: 8px 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .row:last-child { border-bottom: 0; }
    .row span { color: var(--muted); }
    .row b { font-weight: 500; overflow-wrap: anywhere; text-align: right; }
    .feed {
      display: grid;
      gap: 8px;
      max-height: 240px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
    }
    .feed-item {
      border: 1px solid rgba(42, 48, 58, .75);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--muted);
      padding: 9px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .feed-item b {
      display: block;
      margin-bottom: 4px;
      color: var(--ink);
      font-weight: 500;
    }
    .preview-card { margin-top: 12px; }
    .preview-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
    }
    .preview-head h2 {
      border-bottom: 0;
      padding: 0;
    }
    .preview-controls {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .preview-controls select,
    .preview-controls button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--ink);
      min-height: 38px;
      padding: 8px 10px;
    }
    .preview-controls button.active {
      border-color: var(--amber);
      background: var(--amber);
      color: #1b1007;
      font-weight: 700;
    }
    .preview-frame {
      position: relative;
      display: grid;
      min-height: 190px;
      place-items: center;
      background: #040507;
    }
    .preview-frame img {
      display: block;
      width: 100%;
      max-height: min(52svh, 420px);
      object-fit: contain;
    }
    .preview-frame span {
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      padding: 18px;
      text-align: center;
    }
    .preview-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      padding: 8px 12px 12px;
    }
    .auth {
      display: grid;
      gap: 12px;
      min-height: calc(100svh - 56px);
      align-content: center;
    }
    .auth h1 {
      margin: 0;
      font-size: 30px;
      letter-spacing: 0;
    }
    .auth p { margin: 0; color: var(--muted); line-height: 1.5; }
    .auth input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 24px;
      letter-spacing: .14em;
      padding: 15px;
      text-align: center;
    }
    .auth button, .bar button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--ink);
      min-height: 48px;
      padding: 10px 12px;
    }
    .auth button.primary, .bar button.primary {
      border-color: var(--amber);
      background: var(--amber);
      color: #1b1007;
      font-weight: 700;
    }
    .bar button.danger {
      border-color: rgba(255, 71, 87, .65);
      color: var(--red);
    }
    .bar button:disabled { opacity: .45; }
    .bar {
      position: fixed;
      right: 0;
      bottom: 0;
      left: 0;
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
      border-top: 1px solid var(--line);
      background: rgba(8, 9, 11, .94);
      padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
      backdrop-filter: blur(14px);
    }
    .error {
      border: 1px solid rgba(255, 71, 87, .5);
      border-radius: 8px;
      background: rgba(255, 71, 87, .12);
      color: var(--red);
      padding: 10px 12px;
    }
    @media (orientation: landscape) and (max-height: 720px) {
      #dash {
        display: grid;
        grid-template-columns: minmax(360px, 1.25fr) minmax(300px, .75fr);
        grid-auto-rows: min-content;
        gap: 10px 12px;
        padding: max(10px, env(safe-area-inset-top)) 12px calc(68px + env(safe-area-inset-bottom));
      }
      #dash .top,
      #dash .preview-card {
        grid-column: 1;
      }
      #dash .hero,
      #dash .card:not(.preview-card),
      #dash #dashError {
        grid-column: 2;
      }
      #dash .top {
        margin-bottom: 0;
      }
      #dash .hero {
        margin-bottom: 0;
        padding: 12px;
      }
      #dash .state {
        font-size: clamp(30px, 9vw, 46px);
      }
      #dash .meta {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        margin-top: 12px;
      }
      .preview-card {
        margin-top: 0;
      }
      .preview-frame {
        min-height: calc(100svh - 178px);
      }
      .preview-frame img {
        max-height: calc(100svh - 178px);
      }
      #events,
      #logs {
        max-height: 100px;
      }
      .bar {
        grid-template-columns: repeat(3, minmax(0, 1fr));
        padding-top: 8px;
      }
    }
    .hidden { display: none; }
    #dash.hidden { display: none; }
  </style>
</head>
<body>
  <main id="auth" class="app auth">
    <div class="top"><span>vomlab/reolink</span><span class="badge">mobile</span></div>
    <h1>Enter PIN</h1>
    <p>Status and controls stay locked until the desktop app PIN is entered.</p>
    <input id="pin" inputmode="numeric" autocomplete="one-time-code" placeholder="000000">
    <button id="unlock" class="primary">Unlock</button>
    <div id="authError" class="error hidden"></div>
  </main>
  <main id="dash" class="app hidden">
    <div class="top"><span>vomlab/reolink</span><button id="lock" class="badge">Lock</button></div>
    <section class="hero">
      <div id="state" class="state">STOPPED</div>
      <div class="meta">
        <div class="tile"><span>runtime</span><b id="runtime">-</b></div>
        <div class="tile"><span>osc</span><b id="osc">-</b></div>
        <div class="tile"><span>cameras</span><b id="cameras">-</b></div>
        <div class="tile"><span>latest</span><b id="latest">-</b></div>
      </div>
    </section>
    <section class="card preview-card">
      <div class="preview-head">
        <h2>preview</h2>
        <div class="preview-controls">
          <select id="previewCamera" aria-label="Preview camera"></select>
          <button id="view">View</button>
        </div>
      </div>
      <div class="preview-frame">
        <img id="previewImage" alt="Mobile camera preview" class="hidden">
        <span id="previewMessage">Tap View to request low-rate preview frames.</span>
      </div>
      <div class="preview-meta">
        <span id="previewAge">on demand</span>
        <span>rotate phone for landscape</span>
      </div>
    </section>
    <section class="card"><h2>cameras</h2><div id="cameraRows" class="card-body"></div></section>
    <section class="card"><h2>events</h2><div id="events" class="card-body feed"></div></section>
    <section class="card"><h2>stdout/stderr</h2><div id="logs" class="card-body feed"></div></section>
    <div id="dashError" class="error hidden"></div>
  </main>
  <nav class="bar hidden" id="bar">
    <button id="start" class="primary">Start</button>
    <button id="stop" class="danger">Stop</button>
    <button id="refresh">Refresh</button>
  </nav>
  <script>
    const key = "reolink-mobile-token";
    const auth = document.getElementById("auth");
    const dash = document.getElementById("dash");
    const bar = document.getElementById("bar");
    const pin = document.getElementById("pin");
    const authError = document.getElementById("authError");
    const dashError = document.getElementById("dashError");
    const previewCamera = document.getElementById("previewCamera");
    const previewImage = document.getElementById("previewImage");
    const previewMessage = document.getElementById("previewMessage");
    const previewAge = document.getElementById("previewAge");
    const viewButton = document.getElementById("view");
    let token = localStorage.getItem(key) || "";
    let busy = false;
    let previewOn = false;
    let previewTimer = 0;
    let previewObjectUrl = "";

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[ch] || ch));
    }
    function num(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(1) : "-";
    }
    function showAuth(message = "") {
      setPreview(false);
      auth.classList.remove("hidden");
      dash.classList.add("hidden");
      bar.classList.add("hidden");
      authError.textContent = message;
      authError.classList.toggle("hidden", !message);
      pin.focus();
    }
    function showDash() {
      auth.classList.add("hidden");
      dash.classList.remove("hidden");
      bar.classList.remove("hidden");
    }
    function cameraNames(data) {
      const live = Array.isArray(data?.cameras)
        ? data.cameras.map(cam => String(cam.name || "")).filter(Boolean)
        : [];
      const configured = Array.isArray(data?.configured_cameras)
        ? data.configured_cameras.map(String).filter(Boolean)
        : [];
      return [...new Set([...live, ...configured])];
    }
    function syncPreviewCameras(data) {
      const names = cameraNames(data);
      const selected = previewCamera.value || names[0] || "";
      previewCamera.innerHTML = names.length
        ? names.map(name => `<option value="${esc(name)}" ${name === selected ? "selected" : ""}>${esc(name)}</option>`).join("")
        : "<option value=''>No camera</option>";
      if (names.includes(selected)) {
        previewCamera.value = selected;
      }
      viewButton.disabled = !names.length;
    }
    function previewUrl() {
      const camera = previewCamera.value;
      return camera ? `/api/preview/${encodeURIComponent(camera)}.jpg?ts=${Date.now()}` : "";
    }
    function setPreview(on) {
      previewOn = Boolean(on);
      viewButton.textContent = previewOn ? "Hide" : "View";
      viewButton.classList.toggle("active", previewOn);
      if (!previewOn) {
        if (previewTimer) window.clearTimeout(previewTimer);
        previewTimer = 0;
        previewImage.classList.add("hidden");
        previewMessage.classList.remove("hidden");
        previewMessage.textContent = "Tap View to request low-rate preview frames.";
        previewAge.textContent = "on demand";
      } else {
        previewMessage.textContent = "Waiting for preview frame...";
        previewMessage.classList.remove("hidden");
        fetchPreview();
      }
    }
    async function fetchPreview() {
      if (!previewOn || !token) return;
      const url = previewUrl();
      if (!url) {
        previewMessage.textContent = "No camera is available yet.";
        return;
      }
      try {
        const response = await fetch(url, {
          headers: { "X-Reolink-Mobile-Token": token },
          cache: "no-store"
        });
        if (response.status === 401) {
          localStorage.removeItem(key);
          token = "";
          showAuth("Wrong PIN.");
          return;
        }
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || response.statusText);
        }
        const blob = await response.blob();
        if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
        previewObjectUrl = URL.createObjectURL(blob);
        previewImage.src = previewObjectUrl;
        previewImage.classList.remove("hidden");
        previewMessage.classList.add("hidden");
        previewAge.textContent = new Date().toLocaleTimeString();
      } catch (error) {
        previewImage.classList.add("hidden");
        previewMessage.classList.remove("hidden");
        previewMessage.textContent = String(error.message || error);
      } finally {
        if (previewOn) {
          previewTimer = window.setTimeout(fetchPreview, 1000);
        }
      }
    }
    async function callApi(path, method = "GET") {
      const response = await fetch(path, {
        method,
        headers: { "X-Reolink-Mobile-Token": token },
        cache: "no-store"
      });
      const data = await response.json().catch(() => ({ ok: false, error: "Invalid response" }));
      if (response.status === 401) {
        localStorage.removeItem(key);
        token = "";
        showAuth("Wrong PIN.");
        throw new Error("Wrong PIN.");
      }
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || response.statusText);
      }
      return data;
    }
    function renderRows(cameras) {
      if (!Array.isArray(cameras) || !cameras.length) {
        return "<p class='label'>No camera events yet.</p>";
      }
      return cameras.map(cam => `<div class="row"><span>${esc(cam.name)}</span><b>${num(cam.fps)} fps / age ${num(cam.frame_age_s)}s / reconnects ${esc(cam.reconnects ?? 0)}</b></div>`).join("");
    }
    function renderFeed(items, kind) {
      if (!Array.isArray(items) || !items.length) {
        return "<div class='feed-item'>No entries yet.</div>";
      }
      return items.slice(-20).reverse().map(item => {
        if (kind === "log") {
          return `<div class="feed-item"><b>${esc(item.stream)}</b>${esc(item.line)}</div>`;
        }
        const name = item.event || "event";
        return `<div class="feed-item"><b>${esc(name)}</b>${esc(JSON.stringify(item))}</div>`;
      }).join("");
    }
    function renderStatus(data) {
      showDash();
      const running = Boolean(data.process && data.process.running);
      document.getElementById("state").textContent = running ? "RUNNING" : "STOPPED";
      document.getElementById("state").classList.toggle("running", running);
      document.getElementById("runtime").textContent = data.summary?.runtime_ready ? "ready" : "setup";
      document.getElementById("osc").textContent = `${num(data.summary?.osc_rate)}/s`;
      document.getElementById("cameras").textContent = String(data.summary?.camera_count ?? 0);
      document.getElementById("latest").textContent = data.summary?.latest_event || "-";
      document.getElementById("cameraRows").innerHTML = renderRows(data.cameras);
      document.getElementById("events").innerHTML = renderFeed(data.events, "event");
      document.getElementById("logs").innerHTML = renderFeed(data.logs, "log");
      syncPreviewCameras(data);
      document.getElementById("start").disabled = running || busy;
      document.getElementById("stop").disabled = !running || busy;
      dashError.classList.add("hidden");
    }
    async function refresh() {
      if (!token) {
        showAuth();
        return;
      }
      try {
        renderStatus(await callApi("/api/status"));
      } catch (error) {
        dashError.textContent = String(error.message || error);
        dashError.classList.remove("hidden");
      }
    }
    async function action(path) {
      if (busy) return;
      busy = true;
      try {
        renderStatus(await callApi(path, "POST"));
      } catch (error) {
        dashError.textContent = String(error.message || error);
        dashError.classList.remove("hidden");
      } finally {
        busy = false;
      }
    }
    document.getElementById("unlock").addEventListener("click", () => {
      token = pin.value.trim();
      localStorage.setItem(key, token);
      refresh();
    });
    pin.addEventListener("keydown", event => {
      if (event.key === "Enter") document.getElementById("unlock").click();
    });
    document.getElementById("lock").addEventListener("click", () => {
      localStorage.removeItem(key);
      token = "";
      showAuth();
    });
    viewButton.addEventListener("click", () => setPreview(!previewOn));
    previewCamera.addEventListener("change", () => {
      if (previewOn) {
        if (previewTimer) window.clearTimeout(previewTimer);
        previewTimer = 0;
        previewMessage.textContent = "Waiting for preview frame...";
        previewMessage.classList.remove("hidden");
        fetchPreview();
      }
    });
    document.getElementById("start").addEventListener("click", () => action("/api/start"));
    document.getElementById("stop").addEventListener("click", () => action("/api/stop"));
    document.getElementById("refresh").addEventListener("click", refresh);
    if (token) refresh(); else showAuth();
    setInterval(() => { if (token && !busy) refresh(); }, 2500);
  </script>
</body>
</html>"#
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
  - name: cam2
    url: rtsp://admin:%21pass@192.168.1.22:554/h264Preview_01_sub
"#;
        assert_eq!(
            parse_config_targets(config),
            vec![
                ("cam0".to_string(), "192.168.1.20".to_string()),
                ("cam1".to_string(), "192.168.1.21".to_string()),
                ("cam2".to_string(), "192.168.1.22".to_string())
            ]
        );
    }

    #[test]
    fn parse_config_camera_names_reads_configured_order() {
        let config = r#"
cameras:
  - name: cam0
    url: /tmp/a.mp4
  - name: cam2
    url: /tmp/b.mp4
"#;
        assert_eq!(
            parse_config_camera_names(config),
            vec!["cam0".to_string(), "cam2".to_string()]
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
    fn mobile_token_validation_requires_exact_header_value() {
        assert!(mobile_token_matches("123456", Some("123456")));
        assert!(mobile_token_matches("123456", Some(" 123456 ")));
        assert!(!mobile_token_matches("123456", None));
        assert!(!mobile_token_matches("123456", Some("12345")));
        assert!(!mobile_token_matches("123456", Some("1234567")));
        assert!(!mobile_token_matches("123456", Some("654321")));
    }

    #[test]
    fn select_mobile_port_uses_first_available_candidate() {
        let picked = select_mobile_port([1421, 1422, 1423], |port| port >= 1422);
        assert_eq!(picked, Some(1422));

        let none = select_mobile_port([1421, 1422], |_| false);
        assert_eq!(none, None);
    }

    #[test]
    fn preview_camera_route_accepts_safe_names_only() {
        assert_eq!(
            preview_camera_from_route("/api/preview/cam0.jpg"),
            Some("cam0".to_string())
        );
        assert_eq!(
            preview_camera_from_route("/api/preview/cam-2_aux.jpg"),
            Some("cam-2_aux".to_string())
        );
        assert_eq!(
            preview_camera_from_route("/api/preview/../secret.jpg"),
            None
        );
        assert_eq!(preview_camera_from_route("/api/preview/cam%200.jpg"), None);
    }

    #[test]
    fn mobile_status_json_includes_runtime_process_camera_event_and_log_shape() {
        let runtime = RuntimeStatus {
            app_data_dir: "/tmp/app".to_string(),
            runtime_dir: "/tmp/app/runtime".to_string(),
            engine_dir: "/tmp/app/runtime/engine".to_string(),
            config_path: "/tmp/app/runtime/config.yaml".to_string(),
            python_path: "/tmp/app/runtime/.venv/bin/python".to_string(),
            venv_exists: true,
            config_exists: true,
            model_exists: true,
            tracker_exists: true,
            uv_path: Some("/usr/local/bin/uv".to_string()),
        };
        let process = ProcessStatus {
            running: true,
            exit_code: None,
        };
        let events = vec![serde_json::json!({
            "event": "fps_tick",
            "ts": 1.0,
            "cameras": [
                {"name": "cam0", "fps": 12.0, "osc_rate": 9.5, "reconnects": 0, "frame_age_s": 0.1},
                {"name": "cam1", "fps": 11.0, "osc_rate": 8.5, "reconnects": 1, "frame_age_s": 0.2}
            ],
            "projections": [{"id": "corridor", "active": [1], "xy": [1, 10, 20], "uv": [1, 0.1, 0.2]}]
        })];
        let logs = vec![LogEvent {
            stream: "stdout".to_string(),
            line: "ready".to_string(),
        }];

        let value = mobile_status_json(
            runtime,
            process,
            vec!["cam0".to_string(), "cam1".to_string()],
            events,
            logs,
        );
        assert_eq!(value.get("ok").and_then(|item| item.as_bool()), Some(true));
        assert_eq!(
            value
                .pointer("/summary/runtime_ready")
                .and_then(|item| item.as_bool()),
            Some(true)
        );
        assert_eq!(
            value
                .pointer("/summary/camera_count")
                .and_then(|item| item.as_u64()),
            Some(2)
        );
        assert_eq!(
            value
                .get("configured_cameras")
                .and_then(|item| item.as_array())
                .map(Vec::len),
            Some(2)
        );
        assert_eq!(
            value
                .pointer("/process/running")
                .and_then(|item| item.as_bool()),
            Some(true)
        );
        assert!(value
            .get("cameras")
            .and_then(|item| item.as_array())
            .is_some());
        assert!(value
            .get("events")
            .and_then(|item| item.as_array())
            .is_some());
        assert!(value.get("logs").and_then(|item| item.as_array()).is_some());
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
        relaxed_presence_uv: [0.05, 0.2, 0.45, 0.9]
        min_bbox_height_px: 24
        relaxed_presence_points: [[100, 80], [320, 80], [300, 150], [120, 150]]
        relaxed_presence_margin_uv: 0.1
        relaxed_presence_min_confidence: 0.12
  - name: cam2
    regions:
      - id: center
        projection_id: corridor
        projection_uv: [0.32, 0.0, 0.68, 1.0]
        dispatch_uv: [0.4, 0.0, 0.6, 1.0]
        min_bbox_height_px: 24
"#;
        let snapshot = parse_projection_snapshot(config).expect("snapshot should parse");
        assert_eq!(snapshot.camera_count, 2);
        assert_eq!(snapshot.projections[0].id, "corridor");
        assert_eq!(snapshot.projections[0].zones[0].id, "center");
        assert_eq!(snapshot.regions[0].camera, "cam0");
        assert_eq!(snapshot.regions[0].dispatch_uv, vec![0.0, 0.0, 0.5, 1.0]);
        assert_eq!(snapshot.regions[1].camera, "cam2");
        assert_eq!(snapshot.regions[1].dispatch_uv, vec![0.4, 0.0, 0.6, 1.0]);
        assert_eq!(
            snapshot.regions[0].relaxed_presence_uv,
            vec![0.05, 0.2, 0.45, 0.9]
        );
        assert_eq!(snapshot.regions[0].relaxed_presence_points.len(), 4);
        assert_eq!(
            snapshot.regions[0].relaxed_presence_min_confidence,
            Some(0.12)
        );
    }

    #[test]
    fn video_test_config_retargets_one_camera_without_touching_saved_config() {
        let config = r#"
model: yolo26n.pt
osc:
  host: 127.0.0.1
  port: 7000
  raw_per_cam: true
  zone_level: true
cameras:
  - name: cam0
    url: rtsp://admin:%21pass@192.168.1.20:554/h264Preview_01_sub
    regions: []
  - name: cam1
    url: rtsp://admin:%21pass@192.168.1.21:554/h264Preview_01_sub
    regions: []
  - name: cam2
    url: rtsp://admin:%21pass@192.168.1.22:554/h264Preview_01_sub
    regions: []
"#;
        let text =
            video_test_config_text(config, Path::new("/tmp/test-camera-1.mp4"), Some("cam1"))
                .expect("video test config should serialize");
        let parsed: serde_yaml::Value =
            serde_yaml::from_str(&text).expect("generated YAML should parse");
        let cameras = parsed
            .get("cameras")
            .and_then(|value| value.as_sequence())
            .expect("cameras should remain a sequence");
        assert_eq!(
            cameras[0].get("url").and_then(|value| value.as_str()),
            Some("rtsp://admin:%21pass@192.168.1.20:554/h264Preview_01_sub")
        );
        assert_eq!(
            cameras[1].get("url").and_then(|value| value.as_str()),
            Some("/tmp/test-camera-1.mp4")
        );
        assert_eq!(
            cameras[2].get("url").and_then(|value| value.as_str()),
            Some("rtsp://admin:%21pass@192.168.1.22:554/h264Preview_01_sub")
        );
        let osc = parsed.get("osc").expect("osc should exist");
        assert_eq!(
            osc.get("td_minimal").and_then(|value| value.as_bool()),
            Some(true)
        );
        assert_eq!(
            osc.get("raw_per_cam").and_then(|value| value.as_bool()),
            Some(false)
        );
        assert_eq!(
            osc.get("zone_level").and_then(|value| value.as_bool()),
            Some(false)
        );
    }

    #[test]
    fn calibration_config_updates_existing_region_points() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam0
    url: /tmp/test.mp4
    regions:
      - id: near
        projection_id: corridor
        image_points: [[0, 0], [1, 0], [1, 1], [0, 1]]
        projection_uv: [0.0, 0.0, 0.5, 1.0]
        dispatch_uv: [0.0, 0.0, 0.5, 1.0]
"#;
        let request = SaveCalibrationPointsRequest {
            camera_name: "cam0".to_string(),
            region_id: Some("near".to_string()),
            image_points: vec![[10.0, 20.0], [30.0, 40.0], [50.0, 60.0], [70.0, 80.0]],
            point_kind: Some("floor".to_string()),
        };
        let text =
            calibration_config_text(config, &request).expect("calibration config should save");
        let parsed: serde_yaml::Value =
            serde_yaml::from_str(&text).expect("generated YAML should parse");
        let points = parsed
            .get("cameras")
            .and_then(|value| value.as_sequence())
            .and_then(|cameras| cameras.first())
            .and_then(|camera| camera.get("regions"))
            .and_then(|value| value.as_sequence())
            .and_then(|regions| regions.first())
            .and_then(|region| region.get("image_points"))
            .and_then(|value| value.as_sequence())
            .expect("image points should exist");
        assert_eq!(points.len(), 4);
        assert_eq!(yaml_number_vec(points.first()), vec![10.0, 20.0]);
    }

    #[test]
    fn calibration_config_updates_existing_stair_relaxed_points() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam0
    url: /tmp/test.mp4
    regions:
      - id: near
        projection_id: corridor
        image_points: [[0, 0], [1, 0], [1, 1], [0, 1]]
        projection_uv: [0.0, 0.0, 0.5, 1.0]
        dispatch_uv: [0.0, 0.0, 0.5, 1.0]
"#;
        let request = SaveCalibrationPointsRequest {
            camera_name: "cam0".to_string(),
            region_id: Some("near".to_string()),
            image_points: vec![[100.0, 80.0], [320.0, 80.0], [300.0, 150.0], [120.0, 150.0]],
            point_kind: Some("stair".to_string()),
        };
        let text = calibration_config_text(config, &request).expect("stair points should save");
        let parsed: serde_yaml::Value =
            serde_yaml::from_str(&text).expect("generated YAML should parse");
        let region = parsed
            .get("cameras")
            .and_then(|value| value.as_sequence())
            .and_then(|cameras| cameras.first())
            .and_then(|camera| camera.get("regions"))
            .and_then(|value| value.as_sequence())
            .and_then(|regions| regions.first())
            .expect("region should exist");
        assert!(region.get("image_points").is_some());
        let relaxed = region
            .get("relaxed_presence_points")
            .and_then(|value| value.as_sequence())
            .expect("relaxed points should exist");
        assert_eq!(relaxed.len(), 4);
        assert_eq!(yaml_number_vec(relaxed.first()), vec![100.0, 80.0]);
    }

    #[test]
    fn calibration_config_adds_cam2_floor_region() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam0
    url: /tmp/cam0.mp4
    regions: []
  - name: cam2
    url: /tmp/cam2.mp4
    regions: []
"#;
        let request = SaveCalibrationPointsRequest {
            camera_name: "cam2".to_string(),
            region_id: Some("center_band".to_string()),
            image_points: vec![
                [180.0, 120.0],
                [1040.0, 120.0],
                [1080.0, 560.0],
                [150.0, 560.0],
            ],
            point_kind: Some("floor".to_string()),
        };
        let text = calibration_config_text(config, &request).expect("cam2 floor should save");
        let parsed: serde_yaml::Value =
            serde_yaml::from_str(&text).expect("generated YAML should parse");
        let cameras = parsed
            .get("cameras")
            .and_then(|value| value.as_sequence())
            .expect("cameras should exist");
        let cam2 = cameras
            .iter()
            .find(|camera| camera.get("name").and_then(|value| value.as_str()) == Some("cam2"))
            .expect("cam2 should exist");
        let region = cam2
            .get("regions")
            .and_then(|value| value.as_sequence())
            .and_then(|regions| regions.first())
            .expect("cam2 region should exist");
        assert_eq!(
            region.get("id").and_then(|value| value.as_str()),
            Some("center_band")
        );
        assert_eq!(
            region.get("projection_id").and_then(|value| value.as_str()),
            Some("corridor")
        );
        assert_eq!(
            yaml_number_vec(
                region
                    .get("image_points")
                    .and_then(|value| value.as_sequence())
                    .and_then(|points| points.first())
            ),
            vec![180.0, 120.0]
        );
    }

    #[test]
    fn calibration_mapping_updates_cam2_without_touching_other_cameras() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam0
    url: /tmp/cam0.mp4
    regions:
      - id: near
        projection_id: corridor
        projection_uv: [0.0, 0.0, 0.48, 1.0]
        dispatch_uv: [0.0, 0.0, 0.4, 1.0]
  - name: cam2
    url: /tmp/cam2.mp4
    regions:
      - id: center_band
        projection_id: corridor
        projection_uv: [0.32, 0.0, 0.68, 1.0]
        dispatch_uv: [0.4, 0.0, 0.6, 1.0]
        relaxed_presence_points: [[400, 280], [1120, 280], [1120, 420], [400, 420]]
"#;
        let request = SaveCalibrationMappingRequest {
            camera_name: "cam2".to_string(),
            region_id: "center_band".to_string(),
            projection_uv: Some([0.30, 0.0, 0.70, 1.0]),
            dispatch_uv: Some([0.40, 0.0, 0.60, 1.0]),
            relaxed_presence_uv: Some([0.32, 0.2, 0.58, 0.82]),
            relaxed_presence_v: Some(0.35),
        };
        let text =
            calibration_mapping_config_text(config, &request).expect("cam2 mapping should save");
        let parsed: serde_yaml::Value =
            serde_yaml::from_str(&text).expect("generated YAML should parse");
        let cameras = parsed
            .get("cameras")
            .and_then(|value| value.as_sequence())
            .expect("cameras should exist");
        let cam0_region = cameras[0]
            .get("regions")
            .and_then(|value| value.as_sequence())
            .and_then(|regions| regions.first())
            .expect("cam0 region should exist");
        let cam2_region = cameras[1]
            .get("regions")
            .and_then(|value| value.as_sequence())
            .and_then(|regions| regions.first())
            .expect("cam2 region should exist");
        assert_eq!(
            yaml_number_vec(cam0_region.get("dispatch_uv")),
            vec![0.0, 0.0, 0.4, 1.0]
        );
        assert_eq!(
            yaml_number_vec(cam2_region.get("projection_uv")),
            vec![0.30, 0.0, 0.70, 1.0]
        );
        assert_eq!(
            yaml_number_vec(cam2_region.get("relaxed_presence_uv")),
            vec![0.32, 0.2, 0.58, 0.82]
        );
        assert_eq!(
            yaml_optional_f64(cam2_region.get("relaxed_presence_v")),
            Some(0.35)
        );
    }

    #[test]
    fn calibration_mapping_rejects_invalid_relaxed_presence_uv() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam2
    url: /tmp/cam2.mp4
    regions:
      - id: center_band
        projection_id: corridor
        projection_uv: [0.32, 0.0, 0.68, 1.0]
        dispatch_uv: [0.4, 0.0, 0.6, 1.0]
        relaxed_presence_points: [[400, 280], [1120, 280], [1120, 420], [400, 420]]
"#;
        let request = SaveCalibrationMappingRequest {
            camera_name: "cam2".to_string(),
            region_id: "center_band".to_string(),
            projection_uv: None,
            dispatch_uv: None,
            relaxed_presence_uv: Some([0.75, 0.2, 0.25, 0.8]),
            relaxed_presence_v: None,
        };
        let err = calibration_mapping_config_text(config, &request)
            .expect_err("invalid relaxed_presence_uv should fail");
        assert_eq!(err, "relaxed_presence_uv must satisfy min < max");
    }

    #[test]
    fn calibration_mapping_rejects_relaxed_presence_uv_without_stair_points() {
        let config = r#"
projections:
  - id: corridor
cameras:
  - name: cam2
    url: /tmp/cam2.mp4
    regions:
      - id: center_band
        projection_id: corridor
        projection_uv: [0.32, 0.0, 0.68, 1.0]
        dispatch_uv: [0.4, 0.0, 0.6, 1.0]
"#;
        let request = SaveCalibrationMappingRequest {
            camera_name: "cam2".to_string(),
            region_id: "center_band".to_string(),
            projection_uv: None,
            dispatch_uv: None,
            relaxed_presence_uv: Some([0.32, 0.2, 0.58, 0.82]),
            relaxed_presence_v: None,
        };
        let err = calibration_mapping_config_text(config, &request)
            .expect_err("relaxed_presence_uv without points should fail");
        assert_eq!(
            err,
            "relaxed_presence_uv requires relaxed_presence_points on the target region"
        );
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
    write_text_atomically(&path, &request.content)
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
        if let Ok(mut active_config) = state.active_config.lock() {
            *active_config = None;
        }
    }
    Ok(())
}

fn active_config_path(state: &AppState) -> Option<PathBuf> {
    state
        .active_config
        .lock()
        .ok()
        .and_then(|active_config| active_config.clone())
}

fn spawn_tracker_with_config(
    app: AppHandle,
    state: &AppState,
    config: PathBuf,
    show_preview: bool,
) -> Result<ProcessStatus, String> {
    ensure_tracker_not_running(state)?;
    copy_engine_files(&app)?;

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
    let preview_dir = mobile_preview_dir(&app)?;
    fs::create_dir_all(&preview_dir)
        .map_err(|err| format!("failed to create {}: {err}", preview_dir.display()))?;

    let mut cmd = Command::new(&python);
    cmd.current_dir(runtime_dir(&app)?)
        .env("REOLINK_MOBILE_PREVIEW_DIR", &preview_dir)
        .env(
            "REOLINK_MOBILE_PREVIEW_REQUEST_FILE",
            mobile_preview_request_path(&app)?,
        )
        .env("REOLINK_MOBILE_PREVIEW_MAX_WIDTH", MOBILE_PREVIEW_MAX_WIDTH)
        .env(
            "REOLINK_MOBILE_PREVIEW_INTERVAL_S",
            MOBILE_PREVIEW_INTERVAL_S,
        )
        .env(
            "REOLINK_MOBILE_PREVIEW_REQUEST_TTL_S",
            MOBILE_PREVIEW_REQUEST_TTL_S,
        )
        .env(
            "REOLINK_MOBILE_PREVIEW_JPEG_QUALITY",
            MOBILE_PREVIEW_JPEG_QUALITY,
        )
        .arg(&tracker)
        .arg("--config")
        .arg(&config)
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
    if let Ok(mut active_config) = state.active_config.lock() {
        *active_config = Some(config);
    }
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

    let test_config = runtime_dir(&app)?.join("video-test-config.yaml");
    let test_config_text =
        video_test_config_text(&config_text, &video_path, request.camera_name.as_deref())?;
    write_text_atomically(&test_config, &test_config_text)?;

    spawn_tracker_with_config(app, &state, test_config, request.show_preview)
}

#[tauri::command]
fn capture_calibration_frame(
    app: AppHandle,
    request: CaptureCalibrationFrameRequest,
) -> Result<CalibrationFrame, String> {
    let config_text = fs::read_to_string(config_path(&app)?)
        .map_err(|err| format!("failed to read runtime config: {err}"))?;
    let (camera, config_url) =
        camera_url_from_config(&config_text, request.camera_name.as_deref())?;
    let source = request
        .video_path
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|value| local_path_from_input(value).display().to_string())
        .unwrap_or(config_url);
    if source.contains('<') || source.contains('>') {
        return Err(format!("{camera}.url still contains placeholder values"));
    }

    let python = python_path(&app)?;
    if !python.exists() {
        return Err("Python runtime is not ready. Run setup first.".to_string());
    }
    let capture_dir = runtime_dir(&app)?.join("calibration");
    fs::create_dir_all(&capture_dir)
        .map_err(|err| format!("failed to create {}: {err}", capture_dir.display()))?;
    let safe_camera = camera
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>();
    let capture_ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|err| format!("failed to read system time: {err}"))?
        .as_millis();
    let out_path = capture_dir.join(format!("{safe_camera}-{capture_ts_ms}.jpg"));
    let script = r#"
import cv2
import sys

source = sys.argv[1]
out_path = sys.argv[2]
cap = cv2.VideoCapture(source)
ok, frame = cap.read()
cap.release()
if not ok or frame is None:
    raise SystemExit("failed to read frame")
height, width = frame.shape[:2]
if not cv2.imwrite(out_path, frame):
    raise SystemExit("failed to write frame")
print(f"{width} {height}")
"#;
    let mut cmd = Command::new(&python);
    cmd.arg("-c").arg(script).arg(&source).arg(&out_path);
    let out = run_capture(cmd)?;
    if !out.ok {
        return Err(format!(
            "failed to capture frame\nstdout:\n{}\nstderr:\n{}",
            out.stdout, out.stderr
        ));
    }
    let mut dims = out.stdout.split_whitespace();
    let width = dims
        .next()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or_default();
    let height = dims
        .next()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or_default();
    Ok(CalibrationFrame {
        camera,
        path: out_path.display().to_string(),
        width,
        height,
    })
}

#[tauri::command]
fn save_calibration_points(
    app: AppHandle,
    state: tauri::State<AppState>,
    request: SaveCalibrationPointsRequest,
) -> Result<(), String> {
    let config = config_path(&app)?;
    let config_text = fs::read_to_string(&config)
        .map_err(|err| format!("failed to read runtime config: {err}"))?;
    let next_config = calibration_config_text(&config_text, &request)?;
    write_text_atomically(&config, &next_config)?;

    if let Some(active_config) = active_config_path(&state) {
        if active_config != config && active_config.exists() {
            let active_text = fs::read_to_string(&active_config)
                .map_err(|err| format!("failed to read active tracker config: {err}"))?;
            let next_active_config = calibration_config_text(&active_text, &request)?;
            write_text_atomically(&active_config, &next_active_config)?;
        }
    }
    Ok(())
}

#[tauri::command]
fn save_calibration_mapping(
    app: AppHandle,
    state: tauri::State<AppState>,
    request: SaveCalibrationMappingRequest,
) -> Result<(), String> {
    let config = config_path(&app)?;
    let config_text = fs::read_to_string(&config)
        .map_err(|err| format!("failed to read runtime config: {err}"))?;
    let next_config = calibration_mapping_config_text(&config_text, &request)?;
    write_text_atomically(&config, &next_config)?;

    if let Some(active_config) = active_config_path(&state) {
        if active_config != config && active_config.exists() {
            let active_text = fs::read_to_string(&active_config)
                .map_err(|err| format!("failed to read active tracker config: {err}"))?;
            let next_active_config = calibration_mapping_config_text(&active_text, &request)?;
            write_text_atomically(&active_config, &next_active_config)?;
        }
    }
    Ok(())
}

fn stop_tracker_state(state: &AppState) -> Result<ProcessStatus, String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "tracker process lock poisoned".to_string())?;
    if let Some(mut child) = guard.take() {
        if let Ok(mut active_config) = state.active_config.lock() {
            *active_config = None;
        }
        let _ = child.kill();
        let status = child
            .wait()
            .map_err(|err| format!("failed to wait for tracker: {err}"))?;
        return Ok(ProcessStatus {
            running: false,
            exit_code: status.code(),
        });
    }
    if let Ok(mut active_config) = state.active_config.lock() {
        *active_config = None;
    }
    Ok(ProcessStatus {
        running: false,
        exit_code: None,
    })
}

#[tauri::command]
fn stop_tracker(state: tauri::State<AppState>) -> Result<ProcessStatus, String> {
    stop_tracker_state(&state)
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
            runtime.venv_exists,
            runtime.config_exists,
            runtime.model_exists,
            runtime.tracker_exists
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
            if route_failures.is_empty() {
                "ok"
            } else {
                "warn"
            },
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

        for (_, host) in &targets {
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
        "TD ack needs an OSC listener/sidecar contract; this launcher does not own it yet."
            .to_string(),
    ));
    checks.push(field_check(
        "walk_test",
        "Walk test",
        "warn",
        "manual check".to_string(),
        "Automatic seam walk-test needs structured fusion history from tracker/sidecar."
            .to_string(),
    ));

    Ok(summarize_checks(generated_at, checks, targets.len()))
}

#[tauri::command]
fn read_projection_snapshot(app: AppHandle) -> Result<ProjectionSnapshot, String> {
    let config = fs::read_to_string(config_path(&app)?)
        .map_err(|err| format!("failed to read config for projection snapshot: {err}"))?;
    parse_projection_snapshot(&config)
}

#[tauri::command]
fn get_mobile_server_status(app: AppHandle) -> MobileServerStatus {
    mobile_server_status(&app)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState::default())
        .setup(|app| {
            start_mobile_server(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_runtime_status,
            prepare_runtime,
            read_config,
            save_config,
            start_tracker,
            start_video_test,
            capture_calibration_frame,
            save_calibration_points,
            save_calibration_mapping,
            stop_tracker,
            tracker_status,
            collect_network_report,
            run_field_checks,
            read_projection_snapshot,
            get_mobile_server_status
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
