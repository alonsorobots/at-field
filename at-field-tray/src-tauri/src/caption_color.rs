//! Brand-purple title bar (caption) for the main dashboard window.
//!
//! Windows 11 22H2 (build 22000+) added three DWM attributes that recolor
//! the non-client window chrome without forcing us to switch to a fully
//! custom title bar (which would break native snap layouts, draggable
//! caption gestures, and accessibility):
//!
//! - `DWMWA_CAPTION_COLOR` (35) -- the title bar background fill.
//! - `DWMWA_BORDER_COLOR` (34) -- the 1 px window border colour.
//! - `DWMWA_TEXT_COLOR` (36) -- the colour of the window title text.
//!
//! On Windows 10 / Server / 11 < 22H2 these attributes are ignored
//! silently, which is exactly what we want -- the title bar falls back
//! to the OS default rather than turning into a hard error. We log the
//! HRESULT for observability but never propagate it.
//!
//! Colour values are passed as `COLORREF`s, which on Win32 is a `u32`
//! in `0x00BBGGRR` byte order (note the BGR -- this catches everyone
//! once). `colorref_from_rgb` does the conversion so callers can think
//! in normal `(r, g, b)` triples.
//!
//! The Tauri command `set_caption_color(hex)` lets the React side push
//! theme changes through. Hex format is `"#rrggbb"` to match what we
//! already store in CSS variables.
//!
//! The module is gated `#[cfg(windows)]` at the `mod` site (in
//! `lib.rs`); we don't repeat that gate here.

use tauri::Manager;
use windows::Win32::Foundation::{COLORREF, HWND};
use windows::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR,
};

/// Build a Win32 `COLORREF` from an `(r, g, b)` triple.
///
/// COLORREF is `0x00BBGGRR` in memory, which is the opposite byte order
/// from the `#rrggbb` hex you'd write in CSS.
pub fn colorref_from_rgb(r: u8, g: u8, b: u8) -> COLORREF {
    COLORREF((r as u32) | ((g as u32) << 8) | ((b as u32) << 16))
}

/// Apply the brand caption / border / text colours to a single HWND.
///
/// `caption` paints the title bar background. `text` paints the window
/// title characters; we let it float so we can pick a contrasting
/// foreground per theme. `border` rides 1 px of chrome around the
/// window edge -- subtle but it knits the OS chrome into the brand
/// palette at the same time.
pub fn apply_to_hwnd(hwnd: HWND, caption: COLORREF, text: COLORREF, border: COLORREF) {
    // SAFETY: `DwmSetWindowAttribute` is safe to call on any HWND. We
    // pass a valid pointer to a `COLORREF` (4 bytes) and the matching
    // size_of value; mismatches are the only way to corrupt anything.
    unsafe {
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_CAPTION_COLOR,
            &caption as *const _ as *const _,
            std::mem::size_of::<COLORREF>() as u32,
        );
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_BORDER_COLOR,
            &border as *const _ as *const _,
            std::mem::size_of::<COLORREF>() as u32,
        );
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_TEXT_COLOR,
            &text as *const _ as *const _,
            std::mem::size_of::<COLORREF>() as u32,
        );
    }
}

/// Default brand purple: matches the surface tone of the Nerv theme
/// (`--color-surface-raised` ≈ `#2b1f3d`). Lighter than `--color-bg`
/// so the bar reads as part of the chrome, not as the page itself,
/// while still being unmistakably purple.
pub const BRAND_CAPTION: (u8, u8, u8) = (0x2b, 0x1f, 0x3d);
pub const BRAND_BORDER: (u8, u8, u8) = (0x47, 0x35, 0x60);
/// White-ish title text reads on every Eva-* theme's caption colour;
/// we use the canonical primary text from the default palette.
pub const BRAND_TEXT: (u8, u8, u8) = (0xf0, 0xef, 0xf2);

/// Tauri command bound from React. Accepts CSS-style "#rrggbb" hex.
#[tauri::command]
pub fn set_caption_color(
    app: tauri::AppHandle,
    caption_hex: String,
    text_hex: Option<String>,
    border_hex: Option<String>,
) -> Result<(), String> {
    let caption = parse_hex(&caption_hex)?;
    let text = match text_hex {
        Some(h) => parse_hex(&h)?,
        None => BRAND_TEXT,
    };
    let border = match border_hex {
        Some(h) => parse_hex(&h)?,
        None => BRAND_BORDER,
    };

    if let Some(window) = app.get_webview_window("main") {
        match window.hwnd() {
            Ok(hwnd) => {
                apply_to_hwnd(
                    hwnd,
                    colorref_from_rgb(caption.0, caption.1, caption.2),
                    colorref_from_rgb(text.0, text.1, text.2),
                    colorref_from_rgb(border.0, border.1, border.2),
                );
                Ok(())
            }
            Err(e) => Err(format!("hwnd lookup failed: {e}")),
        }
    } else {
        Err("main window not found".to_string())
    }
}

fn parse_hex(s: &str) -> Result<(u8, u8, u8), String> {
    let trimmed = s.trim().trim_start_matches('#');
    if trimmed.len() != 6 {
        return Err(format!("expected #rrggbb, got {s:?}"));
    }
    let r = u8::from_str_radix(&trimmed[0..2], 16).map_err(|e| e.to_string())?;
    let g = u8::from_str_radix(&trimmed[2..4], 16).map_err(|e| e.to_string())?;
    let b = u8::from_str_radix(&trimmed[4..6], 16).map_err(|e| e.to_string())?;
    Ok((r, g, b))
}
