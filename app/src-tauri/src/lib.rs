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
    active_config: Mutex<Option<PathBuf>>,
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
    let tmp_path = path.with_file_name(format!(
        ".{file_name}.tmp-{}-{}",
        std::process::id(),
        nonce
    ));
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
            image_points: vec![[180.0, 120.0], [1040.0, 120.0], [1080.0, 560.0], [150.0, 560.0]],
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
        assert_eq!(region.get("id").and_then(|value| value.as_str()), Some("center_band"));
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
        let text = calibration_mapping_config_text(config, &request)
            .expect("cam2 mapping should save");
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

    let mut cmd = Command::new(&python);
    cmd.current_dir(runtime_dir(&app)?)
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

#[tauri::command]
fn stop_tracker(state: tauri::State<AppState>) -> Result<ProcessStatus, String> {
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
            capture_calibration_frame,
            save_calibration_points,
            save_calibration_mapping,
            stop_tracker,
            tracker_status,
            collect_network_report,
            run_field_checks,
            read_projection_snapshot
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
