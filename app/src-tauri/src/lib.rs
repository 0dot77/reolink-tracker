use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::{
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
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

#[derive(Debug, Deserialize)]
struct SaveConfigRequest {
    content: String,
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

#[tauri::command]
fn start_tracker(
    app: AppHandle,
    state: tauri::State<AppState>,
    show_preview: bool,
) -> Result<ProcessStatus, String> {
    {
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
                return Ok(ProcessStatus {
                    running: true,
                    exit_code: None,
                });
            }
            *guard = None;
        }
    }

    let python = python_path(&app)?;
    let tracker = engine_dir(&app)?.join("tracker.py");
    let config = config_path(&app)?;
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
    Ok(ProcessStatus {
        running: true,
        exit_code: None,
    })
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
            stop_tracker,
            tracker_status,
            collect_network_report
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
