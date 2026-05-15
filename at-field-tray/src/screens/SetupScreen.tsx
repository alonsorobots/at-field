import { useEffect, useState } from "react";
import {
  ServiceStatus,
  getServiceStatus,
  inTauri,
  installBundledService,
  uninstallBundledService,
} from "../lib/tauri";

interface SetupScreenProps {
  /** Re-poll the service /health after a successful install so the
      dashboard transitions out of the setup CTA into normal mode. */
  onInstalled: () => void;
}

/**
 * First-run / recovery surface shown when the watchdog service is not
 * installed or not running. Renders three states:
 *   1. Web preview (no Tauri runtime) -- explains why the buttons aren't here.
 *   2. Bundled-and-not-installed -- big "Set up the watchdog" CTA.
 *   3. Installed-but-not-reachable -- diagnostics + "Reinstall" / "Uninstall".
 *
 * The install button shells out to PowerShell with -Verb RunAs (UAC),
 * which is a one-time consent. Once the service is registered, this
 * screen hides itself and the user lands on the normal Signals view.
 */
export default function SetupScreen({ onInstalled }: SetupScreenProps) {
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    const s = await getServiceStatus();
    setStatus(s);
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
  }, []);

  const handleInstall = async () => {
    setBusy(true);
    setError(null);
    try {
      await installBundledService();
      await refresh();
      // Give the service a moment to come up before signaling the parent
      // to re-poll /health -- the CTA hides as soon as /health succeeds.
      setTimeout(() => onInstalled(), 1500);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  const handleUninstall = async () => {
    setBusy(true);
    setError(null);
    try {
      await uninstallBundledService();
      await refresh();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6 text-[var(--color-text-secondary)]">
        Probing watchdog service…
      </div>
    );
  }

  if (!inTauri()) {
    return (
      <div className="p-6 max-w-2xl mx-auto">
        <h2 className="text-lg font-semibold mb-3">Service unreachable</h2>
        <p className="text-sm text-[var(--color-text-secondary)] mb-4">
          The watchdog service isn't responding on localhost:8765, and this
          dashboard is running in a plain browser preview, so the one-click
          installer isn't available here.
        </p>
        <p className="text-sm text-[var(--color-text-secondary)]">
          Install via the AT-Field tray app (Start menu → AT-Field), or
          manually from an elevated PowerShell:
        </p>
        <pre className="mt-3 p-3 rounded bg-[var(--color-bg-elev)] text-xs text-[var(--color-text-primary)] overflow-x-auto">
          atf install
        </pre>
      </div>
    );
  }

  // Inside Tauri now; status came back from Rust.
  if (status?.installed && !status.running) {
    return (
      <div className="p-6 max-w-2xl mx-auto">
        <h2 className="text-lg font-semibold mb-3">Watchdog installed but stopped</h2>
        <p className="text-sm text-[var(--color-text-secondary)] mb-4">
          The Windows service <code>ATFieldWatchdog</code> is registered but
          not running right now. This usually means it crashed or was stopped
          manually. Reinstall to repair it, or run:
        </p>
        <pre className="mb-4 p-3 rounded bg-[var(--color-bg-elev)] text-xs text-[var(--color-text-primary)] overflow-x-auto">
          Start-Service ATFieldWatchdog
        </pre>
        <div className="flex gap-2">
          <button className="preset-pill" disabled={busy} onClick={handleInstall}>
            {busy ? "Working…" : "Reinstall service"}
          </button>
          <button className="preset-pill" disabled={busy} onClick={handleUninstall}>
            Uninstall
          </button>
        </div>
        {error && <pre className="mt-4 text-xs text-red-400 whitespace-pre-wrap">{error}</pre>}
      </div>
    );
  }

  if (!status?.bundled) {
    return (
      <div className="p-6 max-w-2xl mx-auto">
        <h2 className="text-lg font-semibold mb-3">Watchdog binaries missing</h2>
        <p className="text-sm text-[var(--color-text-secondary)] mb-4">
          This build of AT-Field shipped without the bundled service binaries
          (<code>resources/atfield/</code> is empty). You can still install
          the watchdog via the Python package:
        </p>
        <pre className="mb-4 p-3 rounded bg-[var(--color-bg-elev)] text-xs text-[var(--color-text-primary)] overflow-x-auto">
          pip install atfield{"\n"}
          atf install   # in an elevated PowerShell
        </pre>
        <button className="preset-pill" onClick={refresh} disabled={busy}>
          Re-check
        </button>
      </div>
    );
  }

  // bundled === true and installed === false: the happy first-run path.
  return (
    <div className="p-6 max-w-2xl mx-auto">
      <h2 className="text-lg font-semibold mb-1">Welcome to AT-Field</h2>
      <p className="text-xs text-[var(--color-text-tertiary)] mb-4">
        The watchdog runs as a Windows service so it keeps protecting your
        rig even when this dashboard is closed. Setting it up takes one
        UAC click.
      </p>
      <p className="text-sm text-[var(--color-text-secondary)] mb-3">
        Clicking the button below will:
      </p>
      <ul className="text-sm text-[var(--color-text-secondary)] list-disc ml-5 mb-4 space-y-1">
        <li>Open the standard Windows UAC consent prompt.</li>
        <li>Register the <code>ATFieldWatchdog</code> service to run as
          LocalSystem and start at boot.</li>
        <li>Drop a starter <code>config.toml</code> in
          <code> %ProgramData%\ATField\</code> if one isn't already there.</li>
        <li>Start the service and refresh this dashboard.</li>
      </ul>
      <div className="flex gap-2 items-center">
        <button
          className="preset-pill !bg-[var(--color-accent)] !text-black !border-[var(--color-accent)]"
          disabled={busy}
          onClick={handleInstall}
        >
          {busy ? "Waiting for UAC…" : "Set up watchdog service"}
        </button>
        <button className="preset-pill" onClick={refresh} disabled={busy}>
          Re-check
        </button>
      </div>
      {error && <pre className="mt-4 text-xs text-red-400 whitespace-pre-wrap">{error}</pre>}
      <p className="mt-6 text-[11px] text-[var(--color-text-tertiary)]">
        This will run <code>{status.install_script}</code> against{" "}
        <code>{status.service_exe}</code>.
      </p>
    </div>
  );
}
