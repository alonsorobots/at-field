//! Bridge between the tray UI and the bundled service binaries.
//!
//! When the user installs AT-Field via the NSIS installer they get the
//! tray app PLUS a `resources/atfield/` directory that ships:
//!
//!     atfield/
//!       atf.exe                <- frozen CLI (PyInstaller)
//!       atfield-service.exe    <- frozen service entry point
//!       _internal/             <- shared Python runtime + deps
//!       scripts/
//!         install_service.ps1
//!         uninstall_service.ps1
//!         config.example.toml
//!
//! Registering a Windows Service requires UAC, so we never run NSSM
//! directly from the user-mode tray. Instead we shell out to PowerShell
//! with `Start-Process -Verb RunAs` so Windows shows the standard UAC
//! consent prompt before the service is installed.
//!
//! This module exposes three Tauri commands:
//!   * `service_status`   -- probes Windows for the AT-Field service
//!   * `install_service`  -- triggers the elevated installer
//!   * `uninstall_service`-- triggers the elevated uninstaller
//!
//! All commands are synchronous-from-the-user-perspective (we wait for the
//! elevated process to exit) so the React side can refetch status as soon
//! as the promise resolves.

#![cfg(windows)]

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use tauri::{AppHandle, Manager};

/// Windows Service registration name used by scripts/install_service.ps1.
/// Must match the script's default; see PARAMETER ServiceName there.
const SERVICE_NAME: &str = "ATFieldWatchdog";

/// Snapshot describing what the tray knows about the service right now.
#[derive(Debug, Clone, Serialize)]
pub struct ServiceStatus {
    /// True if the bundled `atfield-service.exe` is shipped next to this
    /// install. Will be false during `cargo run` from a dev tree because
    /// the resource isn't staged yet.
    pub bundled: bool,

    /// True if `sc.exe query ATField` returns a known service.
    pub installed: bool,

    /// True if the installed service is in the RUNNING state. Implies
    /// `installed`.
    pub running: bool,

    /// Absolute path to the bundled service binary, when present. Used by
    /// the React UI to display "this build will install
    /// C:\Users\...\atfield-service.exe" so the user can see exactly what
    /// they're consenting to.
    pub service_exe: Option<String>,

    /// Absolute path to install_service.ps1 we'll hand to the elevated
    /// PowerShell, when present. Lets us surface a meaningful error if
    /// the resource didn't ship for some reason.
    pub install_script: Option<String>,
}

/// Resolve the bundled `resources/atfield/` directory regardless of
/// whether we're running from the installed location or from
/// `cargo run` in a dev tree.
fn resource_root(app: &AppHandle) -> Option<PathBuf> {
    // PathResolver resolves relative to the *resource* directory, which
    // for an installed Tauri app is `resources/`. We bundle the staged
    // PyInstaller output into `resources/atfield/`, so the lookup is
    // relative to that.
    let resolver = app.path();
    resolver.resource_dir().ok().map(|root| root.join("atfield"))
}

#[tauri::command]
pub fn service_status(app: AppHandle) -> ServiceStatus {
    let root = resource_root(&app);
    let service_exe = root
        .as_ref()
        .map(|r| r.join("atfield-service.exe"))
        .filter(|p| p.exists());
    let install_script = root
        .as_ref()
        .map(|r| r.join("scripts").join("install_service.ps1"))
        .filter(|p| p.exists());

    let bundled = service_exe.is_some() && install_script.is_some();

    let (installed, running) = query_sc(SERVICE_NAME);

    ServiceStatus {
        bundled,
        installed,
        running,
        service_exe: service_exe.map(path_to_string),
        install_script: install_script.map(path_to_string),
    }
}

/// Run install_service.ps1 with -Verb RunAs (UAC).
///
/// Returns Ok(()) on a successful (exit code 0) elevated run. The frontend
/// is responsible for re-querying `service_status` afterward to confirm the
/// service is actually registered + running.
#[tauri::command]
pub async fn install_service(app: AppHandle) -> Result<(), String> {
    let status = service_status(app.clone());
    let Some(script) = status.install_script else {
        return Err(
            "Service installer script not found in bundle. \
             This build of AT-Field is missing the bundled watchdog -- \
             try reinstalling from a release artifact."
                .into(),
        );
    };
    let Some(exe) = status.service_exe else {
        return Err(
            "atfield-service.exe not found in bundle. \
             This build of AT-Field is missing the bundled watchdog."
                .into(),
        );
    };
    elevated_powershell(&[
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", &path_to_string(PathBuf::from(&script)),
        "-BundledExe", &path_to_string(PathBuf::from(&exe)),
    ])
}

/// Run uninstall_service.ps1 with -Verb RunAs (UAC).
#[tauri::command]
pub async fn uninstall_service(app: AppHandle) -> Result<(), String> {
    let status = service_status(app.clone());
    let Some(script_path) = status.install_script.as_ref().map(|s| {
        // install_service.ps1 lives in scripts/, uninstall sits next to it.
        let p = PathBuf::from(s);
        p.with_file_name("uninstall_service.ps1")
    }) else {
        return Err("Service uninstaller script not found in bundle.".into());
    };
    if !script_path.exists() {
        return Err(format!(
            "Service uninstaller script missing on disk: {}",
            script_path.display(),
        ));
    }
    elevated_powershell(&[
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", &path_to_string(script_path),
    ])
}

// ─────────────────────────────────────────────────────────────────────
// Internals
// ─────────────────────────────────────────────────────────────────────

/// Run `sc.exe query <name>` and parse the STATE line. Returns
/// (installed, running). On any error we report (false, false) which is
/// safe -- the UI will then offer to install.
fn query_sc(name: &str) -> (bool, bool) {
    let output = match Command::new("sc.exe").args(["query", name]).output() {
        Ok(o) => o,
        Err(_) => return (false, false),
    };
    if !output.status.success() {
        // sc.exe returns 1060 when the service doesn't exist. We don't
        // care which error class; just report not-installed.
        return (false, false);
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let installed = true;
    let running = text
        .lines()
        .any(|l| l.trim_start().starts_with("STATE") && l.contains("RUNNING"));
    (installed, running)
}

/// Spawn an elevated PowerShell. Returns Ok if the elevated process
/// exits 0; otherwise returns the captured exit code as a string error.
///
/// We use the well-known `Start-Process -Verb RunAs -Wait` idiom, which
/// pops the standard Windows UAC dialog. If the user declines, the inner
/// Start-Process throws a "user cancelled" error and we surface that.
fn elevated_powershell(args: &[&str]) -> Result<(), String> {
    // Build an argument list for the inner Start-Process. Quoting matters
    // here because PowerShell parses the -ArgumentList string with its
    // own rules; we wrap each arg in single-quotes after escaping any
    // existing single-quotes inside it.
    let inner_args: String = args
        .iter()
        .map(|a| format!("'{}'", a.replace('\'', "''")))
        .collect::<Vec<_>>()
        .join(", ");

    // -PassThru | Wait-Process gives us the exit code of the elevated
    // child, which Start-Process otherwise swallows.
    let script = format!(
        "$p = Start-Process powershell.exe -Verb RunAs -ArgumentList @({}) -PassThru -Wait; \
         exit $p.ExitCode",
        inner_args
    );

    let output = Command::new("powershell.exe")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &script])
        .output()
        .map_err(|e| format!("Failed to spawn powershell.exe: {e}"))?;

    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let exit = output.status.code().unwrap_or(-1);
    Err(format!(
        "Elevated PowerShell exited with code {exit}. {}",
        stderr.trim()
    ))
}

fn path_to_string(p: PathBuf) -> String {
    p.to_string_lossy().into_owned()
}

/// Helper kept for `Path` arguments where converting via `.into()` is
/// awkward. Currently unused but exposed for symmetry with PathBuf.
#[allow(dead_code)]
fn path_ref_to_string(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}
