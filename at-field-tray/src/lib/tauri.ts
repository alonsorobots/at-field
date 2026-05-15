/*
 * Bridge to Tauri-only Rust commands.
 *
 * The dashboard runs in two contexts:
 *   1. Inside the Tauri webview (production / `npm run tauri dev`),
 *      where window.__TAURI__ is defined and we can invoke Rust commands.
 *   2. In a plain browser (`npm run dev`), where there is no host bridge.
 *
 * Module 2 is useful during pure UI work (no need to spin Rust on every
 * code change), so every helper here gracefully degrades to "feature
 * unavailable" rather than throwing on a missing __TAURI__.
 */

export interface ServiceStatus {
  /** True if the bundled atfield-service.exe is shipped next to this install. */
  bundled: boolean;
  /** True if the Windows Service is registered (sc.exe query succeeded). */
  installed: boolean;
  /** True if the registered service is in the RUNNING state. */
  running: boolean;
  /** Absolute path to the bundled service exe (display-only). */
  service_exe: string | null;
  /** Absolute path to install_service.ps1 (display-only). */
  install_script: string | null;
}

interface TauriBridge {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { __TAURI__?: { core?: TauriBridge } };
  // Tauri 2 exposes invoke under window.__TAURI__.core.invoke.
  return w.__TAURI__?.core ?? null;
}

/** Returns true iff we're running inside the Tauri webview. */
export function inTauri(): boolean {
  return getTauri() !== null;
}

/** Probe the bundled watchdog service. Returns null when not in Tauri. */
export async function getServiceStatus(): Promise<ServiceStatus | null> {
  const t = getTauri();
  if (t == null) return null;
  try {
    return await t.invoke<ServiceStatus>("service_status");
  } catch {
    return null;
  }
}

/**
 * Trigger UAC + run install_service.ps1 against the bundled service exe.
 * Resolves on success; rejects with the elevated process's stderr on failure.
 */
export async function installBundledService(): Promise<void> {
  const t = getTauri();
  if (t == null) {
    throw new Error(
      "Service installation requires the Tauri runtime " +
      "(open the AT-Field tray app from your Start menu).",
    );
  }
  await t.invoke("install_service");
}

/** Trigger UAC + run uninstall_service.ps1. */
export async function uninstallBundledService(): Promise<void> {
  const t = getTauri();
  if (t == null) {
    throw new Error("Tauri runtime required.");
  }
  await t.invoke("uninstall_service");
}
