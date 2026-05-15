//! User-mode autostart for the AT-Field tray app.
//!
//! Registers the running tray executable in HKCU\Software\Microsoft\
//! Windows\CurrentVersion\Run so it launches automatically when the
//! current user logs in. Per-user (HKCU) instead of per-machine (HKLM)
//! by design:
//!
//!   * No UAC elevation required -- HKCU is writable by the current user.
//!   * The tray is a personal lens onto the watchdog. The watchdog
//!     itself is a LocalSystem service that's already auto-starting via
//!     NSSM on boot, regardless of whether anyone has logged in. The
//!     tray is purely a UI affordance for whoever's at the keyboard.
//!   * Other utilities (Slack, Discord, Spotify) use the same key for
//!     the same reason; users who want to disable it can do so from
//!     Task Manager → Startup or `msconfig`.
//!
//! This module is Windows-only. On other platforms its public functions
//! are no-ops so the rest of the codebase can call them unconditionally.

// `unregister` and `current_registration` are public for the future
// Settings tab "Launch on login" toggle. They're not wired into any
// menu yet, so the dead-code lint would fire -- silence it at the
// module boundary rather than peppering allow attributes everywhere.
#![allow(dead_code)]

#[cfg(windows)]
mod imp {
    use std::path::PathBuf;
    use winreg::enums::{HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE};
    use winreg::RegKey;

    const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    /// Value name we register under. Must match between install and
    /// uninstall paths so we can clean up our own entry without touching
    /// other Run-key tenants.
    const VALUE_NAME: &str = "AT-Field Tray";

    /// Idempotently register the current executable for user-mode autostart.
    ///
    /// Behavior:
    ///   * If the registry value already points at the current exe -> no-op.
    ///   * If it points at a stale path (old install location) -> overwrite.
    ///   * If the registry key isn't reachable -> log + return Err. Tray
    ///     keeps running; autostart just won't kick in next reboot.
    pub fn ensure_registered() -> Result<EnsureOutcome, String> {
        let exe = std::env::current_exe()
            .map_err(|e| format!("current_exe failed: {e}"))?;
        let exe_str = format_command(&exe);

        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        // KEY_READ first to check whether we'd be a no-op before asking
        // for a write handle. Saves an unnecessary write on every launch.
        if let Ok(run_key) = hkcu.open_subkey_with_flags(RUN_KEY, KEY_READ) {
            if let Ok(existing) = run_key.get_value::<String, _>(VALUE_NAME) {
                if existing == exe_str {
                    return Ok(EnsureOutcome::AlreadyRegistered);
                }
            }
        }

        let (run_key, _) = hkcu
            .create_subkey_with_flags(RUN_KEY, KEY_SET_VALUE)
            .map_err(|e| format!("open Run key for write: {e}"))?;
        run_key
            .set_value(VALUE_NAME, &exe_str)
            .map_err(|e| format!("set Run value: {e}"))?;
        Ok(EnsureOutcome::Registered)
    }

    /// Remove our autostart entry. Safe to call when not registered --
    /// missing values are reported as an Ok(NothingToRemove) so callers
    /// can treat the operation as idempotent.
    pub fn unregister() -> Result<UnregisterOutcome, String> {
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let run_key = match hkcu.open_subkey_with_flags(RUN_KEY, KEY_SET_VALUE) {
            Ok(k) => k,
            Err(_) => return Ok(UnregisterOutcome::NothingToRemove),
        };
        match run_key.delete_value(VALUE_NAME) {
            Ok(()) => Ok(UnregisterOutcome::Removed),
            // ERROR_FILE_NOT_FOUND is the windows-side "value didn't
            // exist" -- treat as success.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Ok(UnregisterOutcome::NothingToRemove)
            }
            Err(e) => Err(format!("delete Run value: {e}")),
        }
    }

    /// Returns Some(stored value) when our entry exists, None otherwise.
    /// Used by the About dialog / settings tab to show the user the
    /// current autostart state.
    pub fn current_registration() -> Option<String> {
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let run_key = hkcu.open_subkey_with_flags(RUN_KEY, KEY_READ).ok()?;
        run_key.get_value::<String, _>(VALUE_NAME).ok()
    }

    /// Format a path for the Run key. Quotes the executable so paths
    /// with spaces (e.g. "C:\Program Files\AT-Field\at-field-tray.exe")
    /// survive the shell-style parsing the Run key expects.
    fn format_command(exe: &PathBuf) -> String {
        let s = exe.to_string_lossy().to_string();
        if s.contains(' ') {
            format!("\"{s}\"")
        } else {
            s
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum EnsureOutcome {
        AlreadyRegistered,
        Registered,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum UnregisterOutcome {
        Removed,
        NothingToRemove,
    }
}

#[cfg(not(windows))]
mod imp {
    pub fn ensure_registered() -> Result<EnsureOutcome, String> {
        Ok(EnsureOutcome::AlreadyRegistered)
    }
    pub fn unregister() -> Result<UnregisterOutcome, String> {
        Ok(UnregisterOutcome::NothingToRemove)
    }
    pub fn current_registration() -> Option<String> { None }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum EnsureOutcome { AlreadyRegistered, Registered }
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum UnregisterOutcome { Removed, NothingToRemove }
}

#[allow(unused_imports)]
pub use imp::{
    current_registration,
    ensure_registered,
    unregister,
    EnsureOutcome,
    UnregisterOutcome,
};
