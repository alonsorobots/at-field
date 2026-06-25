import { motion, AnimatePresence } from "framer-motion";
import { openUrl } from "@tauri-apps/plugin-opener";

interface AboutModalProps {
  open: boolean;
  onClose: () => void;
  /** Service version (from /health). Falls back to a dash when offline. */
  version?: string | null;
}

const REPO_URL = "https://github.com/alonsorobots/at-field";
const ISSUES_URL = "https://github.com/alonsorobots/at-field/issues";
const COFFEE_URL = "https://buymeacoffee.com/alonsorobots";

// Third-party components AT-Field ships or links against. Kept short and
// honest -- the full ledger lives in LICENSE-third-party.md.
const LICENSE_ENTRIES = [
  { name: "LibreHardwareMonitor", license: "MPL-2.0", url: "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor" },
  { name: "NSSM", license: "Public domain", url: "https://nssm.cc/" },
  { name: "Tauri", license: "MIT / Apache-2.0", url: "https://github.com/tauri-apps/tauri" },
  { name: "psutil", license: "BSD-3-Clause", url: "https://github.com/giampaolo/psutil" },
  { name: "React", license: "MIT", url: "https://react.dev" },
];

function ExternalLink({
  url,
  className,
  children,
}: {
  url: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button type="button" onClick={() => openUrl(url)} className={className}>
      {children}
    </button>
  );
}

export default function AboutModal({ open, onClose, version }: AboutModalProps) {
  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="about-backdrop"
            className="fixed inset-0 z-50 bg-black/60"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
          />

          <motion.div
            key="about-card"
            className="fixed inset-0 z-50 flex items-center justify-center pointer-events-none p-4"
            initial={{ opacity: 0, scale: 0.92 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.92 }}
            transition={{ duration: 0.25, ease: [0.4, 0, 0.2, 1] }}
          >
            <div
              className="frosted pointer-events-auto bg-[var(--color-surface)] rounded-xl
                         border border-[var(--color-border-strong)] shadow-2xl
                         w-[400px] max-h-[88vh] overflow-y-auto"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Header */}
              <div className="flex flex-col items-center pt-6 pb-4 px-6">
                <img
                  src="/app-icon.png"
                  alt="AT-Field"
                  className="w-[120px] h-[120px] rounded-2xl mb-3"
                  draggable={false}
                />
                <h2 className="hud hud-glow text-xl font-semibold" style={{ color: "var(--color-accent)" }}>
                  AT-FIELD
                </h2>
                <span className="text-xs text-[var(--color-text-tertiary)] mt-1">
                  v{version ?? "—"}
                </span>
                <p className="text-[11px] text-[var(--color-text-secondary)] text-center mt-2 leading-relaxed">
                  An absolute thermal-and-memory field for your rig — kills runaway
                  jobs before they cook your GPU.
                </p>
              </div>

              <div className="mx-6 border-t border-[var(--color-border)]" />

              {/* Support */}
              <div className="px-6 py-4 flex flex-col gap-2">
                <span className="text-[11px] uppercase tracking-wider text-[var(--color-text-tertiary)] font-medium">
                  Support the project
                </span>

                <ExternalLink
                  url={REPO_URL}
                  className="flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg
                             bg-[var(--color-accent)] text-white text-sm font-medium
                             hover:bg-[var(--color-accent-hover)] transition-colors cursor-pointer"
                >
                  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <path d="M12 17.27l5.18 3.04-1.37-5.88 4.56-3.95-6.01-.52L12 4.5 9.64 9.96l-6.01.52 4.56 3.95-1.37 5.88L12 17.27z" />
                  </svg>
                  Star on GitHub
                </ExternalLink>

                <ExternalLink
                  url={COFFEE_URL}
                  className="flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg
                             border border-[var(--color-border-strong)] bg-[var(--color-surface-raised)]
                             text-[var(--color-text-primary)] text-sm font-medium
                             hover:bg-[var(--color-surface-hover)] transition-colors cursor-pointer"
                >
                  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M18 8h1a4 4 0 0 1 0 8h-1" />
                    <path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z" />
                    <line x1="6" y1="1" x2="6" y2="4" />
                    <line x1="10" y1="1" x2="10" y2="4" />
                    <line x1="14" y1="1" x2="14" y2="4" />
                  </svg>
                  Buy me a coffee
                </ExternalLink>

                <ExternalLink
                  url={ISSUES_URL}
                  className="flex items-center justify-center gap-2 px-4 py-1.5 rounded-lg
                             text-[var(--color-text-secondary)] text-xs font-medium
                             hover:text-[var(--color-text-primary)] transition-colors cursor-pointer"
                >
                  Report an issue or request a feature
                </ExternalLink>
              </div>

              <div className="mx-6 border-t border-[var(--color-border)]" />

              {/* Licenses */}
              <div className="px-6 py-4">
                <span className="text-[11px] uppercase tracking-wider text-[var(--color-text-tertiary)] font-medium">
                  Open-source components
                </span>
                <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1.5 leading-relaxed">
                  AT-Field is released under the{" "}
                  <span className="text-[var(--color-text-secondary)] font-medium">MIT</span>{" "}
                  license. It stands on the shoulders of:
                </p>

                <div className="mt-3 flex flex-col gap-1">
                  {LICENSE_ENTRIES.map((entry) => (
                    <ExternalLink
                      key={entry.name}
                      url={entry.url}
                      className="flex items-center justify-between py-1.5 px-2 -mx-2 rounded
                                 hover:bg-[var(--color-surface-hover)] transition-colors group cursor-pointer text-left"
                    >
                      <span className="text-xs text-[var(--color-text-secondary)] group-hover:text-[var(--color-text-primary)] transition-colors">
                        {entry.name}
                      </span>
                      <span className="text-[10px] text-[var(--color-text-tertiary)] font-mono">
                        {entry.license}
                      </span>
                    </ExternalLink>
                  ))}
                </div>
              </div>

              <div className="mx-6 border-t border-[var(--color-border)]" />

              <div className="px-6 py-3">
                <p className="text-[10px] text-[var(--color-text-tertiary)] text-center leading-relaxed italic">
                  An homage to the AT Fields of{" "}
                  <span className="not-italic">Neon Genesis Evangelion</span> — an absolute
                  barrier against catastrophic damage. An unaffiliated fan project.
                </p>
                <p className="text-[10px] text-[var(--color-text-tertiary)] text-center leading-relaxed italic mt-1.5">
                  Built by Alonso Martinez · vibe-coded with Claude.
                </p>
              </div>

              <div className="px-6 pb-5 pt-1">
                <button
                  type="button"
                  onClick={onClose}
                  className="w-full py-2 rounded-lg bg-[var(--color-surface-raised)]
                             text-[var(--color-text-secondary)] text-xs font-medium
                             hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text-primary)] transition-colors"
                >
                  Close
                </button>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
