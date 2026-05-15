// Prevents an extra console window from opening on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    at_field_tray_lib::run()
}
