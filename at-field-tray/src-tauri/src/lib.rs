// Prevents an extra console window from opening on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod autostart;
#[cfg(windows)]
mod caption_color;
#[cfg(windows)]
mod service_installer;

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Deserialize;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, WebviewWindowBuilder,
};
use tauri_plugin_notification::NotificationExt;

const API_BASE: &str = "http://127.0.0.1:8765";
const POLL_INTERVAL: Duration = Duration::from_secs(2);

// ─────────────────────────────────────────────────────────────────────
// Health snapshot subset -- we only deserialize the fields the tray
// reads to derive status. Adding more fields on the Python side won't
// break us; serde silently ignores them.
// ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize, Clone)]
struct HealthSnapshot {
    paused: bool,
    heartbeat_age_s: Option<f64>,
    collectors: Vec<CollectorView>,
    last_action: Option<LastAction>,
}

#[derive(Debug, Deserialize, Clone)]
struct CollectorView {
    health: String,
}

#[derive(Debug, Deserialize, Clone)]
struct LastAction {
    at: f64,
    kind: String,
    // Optional fields populated by the server for kill actions; absent
    // for older service versions which is fine -- serde fills with None.
    #[serde(default)]
    rule: Option<String>,
    #[serde(default)]
    signal: Option<String>,
    #[serde(default)]
    script: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TrayStatus {
    Healthy,
    Degraded,
    Alerting,
    Down,
}

impl TrayStatus {
    fn tooltip(self) -> &'static str {
        match self {
            TrayStatus::Healthy => "AT-Field — healthy",
            TrayStatus::Degraded => "AT-Field — degraded",
            TrayStatus::Alerting => "AT-Field — recent kill action",
            TrayStatus::Down => "AT-Field — service unreachable",
        }
    }
}

fn derive_status(maybe_health: Option<&HealthSnapshot>) -> TrayStatus {
    let Some(h) = maybe_health else {
        return TrayStatus::Down;
    };
    if let Some(la) = &h.last_action {
        if la.kind == "kill" {
            let now_unix = chrono_now();
            if (now_unix - la.at) < 5.0 * 60.0 {
                return TrayStatus::Alerting;
            }
        }
    }
    if h.paused {
        return TrayStatus::Degraded;
    }
    if h.collectors.iter().any(|c| c.health == "FAILED" || c.health == "DEGRADED") {
        return TrayStatus::Degraded;
    }
    if let Some(age) = h.heartbeat_age_s {
        if age > 30.0 {
            return TrayStatus::Degraded;
        }
    }
    TrayStatus::Healthy
}

/// Plain unix epoch seconds. We avoid pulling chrono just for this.
fn chrono_now() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ─────────────────────────────────────────────────────────────────────
// Background poll thread -- updates the tray tooltip + (eventually) icon
// based on the current TrayStatus. We don't yet swap icons (placeholder
// art is the same for all states); when real artwork lands we can call
// tray.set_icon() here.
// ─────────────────────────────────────────────────────────────────────

fn spawn_poller(app: AppHandle, last_status: Arc<Mutex<TrayStatus>>) {
    // We track the timestamp of the last kill we've ALREADY notified about
    // so we don't re-fire the toast on every poll for the same event. The
    // initial value is set on the first /health observation (rather than 0)
    // so we don't notify the user about a kill that happened BEFORE the
    // tray app started -- they didn't ask for archaeology, only live alerts.
    let last_notified_kill_at: Arc<Mutex<Option<f64>>> = Arc::new(Mutex::new(None));

    thread::spawn(move || loop {
        let snapshot: Option<HealthSnapshot> = ureq_get_json(&format!("{}/health", API_BASE));
        let status = derive_status(snapshot.as_ref());

        // Only push tray updates when status changes -- avoids touching
        // the OS API on every tick.
        let mut guard = last_status.lock().unwrap();
        if *guard != status {
            *guard = status;
            if let Some(tray) = app.tray_by_id("atfield-tray") {
                let _ = tray.set_tooltip(Some(status.tooltip()));
            }
        }
        drop(guard);

        // Detect a NEW kill since our last poll and fire a system
        // notification. We only consider `kind == "kill"` (log/throttle
        // actions get audited but don't deserve a popup), and we ignore
        // any kill_at older than the moment we first observed the API
        // (see init comment above).
        if let Some(h) = snapshot.as_ref() {
            let mut seen = last_notified_kill_at.lock().unwrap();
            if seen.is_none() {
                // First observation -- baseline at the current last_action
                // (or 0 if none) so we don't replay history.
                *seen = Some(h.last_action.as_ref().map(|la| la.at).unwrap_or(0.0));
            }
            if let Some(la) = h.last_action.as_ref() {
                if la.kind == "kill" && Some(la.at) != *seen && la.at > seen.unwrap_or(0.0) {
                    *seen = Some(la.at);
                    fire_kill_notification(&app, la);
                }
            }
        }

        thread::sleep(POLL_INTERVAL);
    });
}

/// Build + send a Windows toast notification announcing a kill action.
/// We keep the body short and structured: title is "AT-Field killed
/// <script>", body is "Rule: <rule_name>. Signal: <signal_name>."
/// System notifications can't render colored text -- the OS theme owns
/// that surface -- so we use a clear leading marker character + the
/// always-recognizable AT-Field branding to convey severity. An in-app
/// red banner (when the dashboard window is open) handles the "red text"
/// emphasis path separately, via an emit() event consumed by React.
fn fire_kill_notification(app: &AppHandle, la: &LastAction) {
    let target = la
        .script
        .clone()
        .unwrap_or_else(|| "a process".to_string());
    let title = format!("AT-Field killed {}", target);
    let mut body_parts: Vec<String> = Vec::new();
    if let Some(rule) = la.rule.as_ref() {
        body_parts.push(format!("Rule: {}", rule));
    }
    if let Some(sig) = la.signal.as_ref() {
        body_parts.push(format!("Signal: {}", sig));
    }
    let body = if body_parts.is_empty() {
        "A watchdog rule fired. Click the tray icon for details.".to_string()
    } else {
        body_parts.join("  ·  ")
    };

    // Best-effort -- if the notification system is unavailable (rare on
    // Windows, but possible in locked-down corp environments) we just
    // skip; the audit log + dashboard still capture the event.
    let _ = app
        .notification()
        .builder()
        .title(&title)
        .body(&body)
        .show();

    // Also broadcast to the React side so a dashboard that's open can
    // render a red toast banner ("the user wants RED text" path). Front
    // end listens to "atfield://kill" via the Tauri event bus.
    let payload = serde_json::json!({
        "at": la.at,
        "rule": la.rule,
        "signal": la.signal,
        "script": la.script,
    });
    let _ = app.emit("atfield://kill", payload);
}

// ─────────────────────────────────────────────────────────────────────
// Tiny stdlib HTTP GET (avoid pulling reqwest/ureq for one endpoint).
// Returns None on any error -- tray status will fall through to Down.
// ─────────────────────────────────────────────────────────────────────

fn ureq_get_json<T: for<'de> Deserialize<'de>>(url: &str) -> Option<T> {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

    let parsed = url.strip_prefix("http://")?.split_once('/')?;
    let (host_port, path) = parsed;
    let mut stream = TcpStream::connect_timeout(
        &host_port.parse().ok()?,
        Duration::from_millis(500),
    )
    .ok()?;
    stream.set_read_timeout(Some(Duration::from_millis(1500))).ok()?;
    let req = format!(
        "GET /{} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",
        path, host_port
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = String::new();
    stream.read_to_string(&mut buf).ok()?;

    // Strip headers; the body is after the first blank line.
    let body = buf.split_once("\r\n\r\n").map(|(_, b)| b)?;
    serde_json::from_str(body).ok()
}

// ─────────────────────────────────────────────────────────────────────
// Tauri setup
// ─────────────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init());

    // Service-installer + caption-color commands are only useful on
    // Windows; the rest of the tray builds (and runs in dev mode) on
    // Linux/macOS for editor ergonomics. On non-Windows we simply
    // don't expose the commands.
    #[cfg(windows)]
    let builder = builder.invoke_handler(tauri::generate_handler![
        service_installer::service_status,
        service_installer::install_service,
        service_installer::uninstall_service,
        caption_color::set_caption_color,
    ]);

    builder
        .setup(|app| {
            // Build the tray menu.
            let show_item = MenuItem::with_id(app, "show", "Show Dashboard", true, None::<&str>)?;
            let pause_30 = MenuItem::with_id(app, "pause_30m", "30 minutes", true, None::<&str>)?;
            let pause_1h = MenuItem::with_id(app, "pause_1h", "1 hour", true, None::<&str>)?;
            let pause_4h = MenuItem::with_id(app, "pause_4h", "4 hours", true, None::<&str>)?;
            let pause_until_reboot = MenuItem::with_id(
                app,
                "pause_reboot",
                "Until reboot",
                true,
                None::<&str>,
            )?;
            let pause_menu = Submenu::with_id_and_items(
                app,
                "pause_submenu",
                "Pause",
                true,
                &[&pause_30, &pause_1h, &pause_4h, &pause_until_reboot],
            )?;
            let unpause = MenuItem::with_id(app, "unpause", "Unpause", true, None::<&str>)?;
            let open_events =
                MenuItem::with_id(app, "open_events", "Open events.jsonl", true, None::<&str>)?;
            let open_log =
                MenuItem::with_id(app, "open_log", "Open watchdog.log", true, None::<&str>)?;
            let about = MenuItem::with_id(app, "about", "About AT-Field", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;

            let menu = Menu::with_items(
                app,
                &[
                    &show_item,
                    &sep,
                    &pause_menu,
                    &unpause,
                    &sep,
                    &open_events,
                    &open_log,
                    &sep,
                    &about,
                    &quit,
                ],
            )?;

            // Build the tray icon. Icon path lives in src-tauri/icons/tray.png
            // and is bundled at compile time.
            let _tray = TrayIconBuilder::with_id("atfield-tray")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .tooltip("AT-Field — connecting…")
                .icon(app.default_window_icon().unwrap().clone())
                .on_menu_event(|app, event| handle_menu_event(app, event.id().as_ref()))
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        toggle_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            spawn_poller(
                app.handle().clone(),
                Arc::new(Mutex::new(TrayStatus::Down)),
            );

            // Pre-stamp the main window with the brand caption colour
            // before the user ever sees it. The window starts visible=false
            // but Tauri creates it eagerly so we can grab the HWND now.
            #[cfg(windows)]
            if let Some(window) = app.get_webview_window("main") {
                apply_brand_caption(&window);
            }

            // User-mode autostart: register this exe in HKCU\Run so the
            // tray shows up automatically next login. Idempotent; safe to
            // call every launch. Failures are logged but don't block
            // startup -- the watchdog itself runs as a service so the
            // user is still protected even if their tray doesn't auto-launch.
            let just_registered = matches!(
                autostart::ensure_registered(),
                Ok(autostart::EnsureOutcome::Registered)
            );
            if just_registered {
                eprintln!("autostart: registered AT-Field Tray in HKCU\\Run");
            }

            // First-launch discoverability toast.
            //
            // Windows 11 hides every new tray icon in the overflow chevron
            // by default -- the user has to right-click the icon there and
            // pick "Show in taskbar" once. Without an active prompt that
            // looks identical to "the app didn't start" (which is what the
            // user reported after the v0.2 hard-reboot incident).
            //
            // We send one notification on the very first launch per
            // install (state file marker), which fires a system toast
            // the user can't miss and which is a clickable hint to
            // promote the icon out of the overflow.
            //
            // ``just_registered`` is the proxy for "first launch on this
            // user account" -- ensure_registered() returns Registered
            // exactly once per (exe path, user) pair. Subsequent launches
            // (and reboots) return AlreadyRegistered and stay silent.
            if just_registered {
                let _ = app
                    .notification()
                    .builder()
                    .title("AT-Field is on guard")
                    .body(
                        "Your GPU, memory, and temperatures are protected. \
                         The tray icon lives in the system tray -- click the \
                         ^ chevron in your taskbar if you don't see it, then \
                         open the dashboard from there.",
                    )
                    .show();
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the main window hides it instead of quitting -- the
            // tray is the persistent surface, the window is the lens. User
            // explicitly Quits from the tray menu.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running AT-Field tray");
}

fn handle_menu_event(app: &AppHandle, id: &str) {
    match id {
        "show" => toggle_main_window(app),
        "pause_30m" => post_pause(30 * 60),
        "pause_1h" => post_pause(60 * 60),
        "pause_4h" => post_pause(4 * 60 * 60),
        "pause_reboot" => post_pause(0), // 0 -> indefinite on the API side
        "unpause" => {
            let _ = ureq_post(&format!("{}/unpause", API_BASE), b"{}");
        }
        "open_events" => {
            let _ = open_state_file(app, "events.jsonl");
        }
        "open_log" => {
            let _ = open_state_file(app, "watchdog.log");
        }
        "about" => {
            toggle_main_window(app);
        }
        "quit" => {
            app.exit(0);
        }
        _ => {}
    }
}

fn post_pause(duration_s: u64) {
    let body = if duration_s == 0 {
        "{}".to_string()
    } else {
        format!("{{\"duration_s\": {}}}", duration_s)
    };
    let _ = ureq_post(&format!("{}/pause", API_BASE), body.as_bytes());
}

fn ureq_post(url: &str, body: &[u8]) -> Option<()> {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

    let parsed = url.strip_prefix("http://")?.split_once('/')?;
    let (host_port, path) = parsed;
    let mut stream = TcpStream::connect_timeout(
        &host_port.parse().ok()?,
        Duration::from_millis(500),
    )
    .ok()?;
    let req = format!(
        "POST /{} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        path,
        host_port,
        body.len()
    );
    stream.write_all(req.as_bytes()).ok()?;
    stream.write_all(body).ok()?;
    let mut buf = String::new();
    stream.read_to_string(&mut buf).ok()?;
    Some(())
}

fn toggle_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
            // Re-apply the brand caption colour every time we show the
            // window. Cheap (one DWM call) and bullet-proof against the
            // OS occasionally resetting attributes after focus changes
            // or DPI events.
            #[cfg(windows)]
            apply_brand_caption(&window);
        }
    } else {
        // First show after silent boot: build the window from config.
        if let Ok(window) = WebviewWindowBuilder::from_config(
            app,
            &app.config().app.windows[0],
        )
        .and_then(|b| b.build())
        {
            #[cfg(windows)]
            apply_brand_caption(&window);
        }
    }
}

/// Push the brand-purple title bar onto a freshly-shown window. No-op
/// on Windows 10 / pre-22H2 (the DWM call returns failure silently).
#[cfg(windows)]
fn apply_brand_caption(window: &tauri::WebviewWindow) {
    if let Ok(hwnd) = window.hwnd() {
        let (cr, cg, cb) = caption_color::BRAND_CAPTION;
        let (tr, tg, tb) = caption_color::BRAND_TEXT;
        let (br, bg, bb) = caption_color::BRAND_BORDER;
        caption_color::apply_to_hwnd(
            hwnd,
            caption_color::colorref_from_rgb(cr, cg, cb),
            caption_color::colorref_from_rgb(tr, tg, tb),
            caption_color::colorref_from_rgb(br, bg, bb),
        );
    }
}

fn open_state_file(app: &AppHandle, filename: &str) -> Result<(), Box<dyn std::error::Error>> {
    use tauri_plugin_opener::OpenerExt;
    let program_data = std::env::var("PROGRAMDATA").unwrap_or_else(|_| "C:\\ProgramData".into());
    let path = format!("{}\\ATField\\{}", program_data, filename);
    app.opener().open_path(path, None::<&str>)?;
    Ok(())
}
