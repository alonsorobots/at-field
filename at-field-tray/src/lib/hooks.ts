import { useEffect, useRef, useState } from "react";

import { type ThemeId, getTheme, subscribeTheme } from "./theme";

/**
 * Poll an async function on an interval and expose its latest value.
 *
 * Tracks reachability separately from data: a fetch failure does NOT
 * blank the previously-known data (so transient network blips don't
 * cause the dashboard to flash empty). Instead, `reachable` flips
 * false and the UI can render a banner.
 *
 * Cancellation: each poll is fired-and-discarded -- if a fetch is still
 * in flight when the next interval ticks, it's left alone (rare given
 * sub-ms localhost latency). If the component unmounts mid-flight, the
 * `mounted` guard prevents a stale state write.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
): {
  data: T | null;
  reachable: boolean;
  error: Error | null;
  hasAttempted: boolean;
  refresh: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [reachable, setReachable] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);
  // Distinguishes "we haven't tried yet" from "we tried and got nothing".
  // Without this, the dashboard renders "Service down" for the ~1s between
  // mount and the first poll resolving -- which lies to the user.
  const [hasAttempted, setHasAttempted] = useState<boolean>(false);
  const mountedRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const tick = async () => {
    try {
      const v = await fetcherRef.current();
      if (!mountedRef.current) return;
      setData(v);
      setReachable(true);
      setError(null);
    } catch (e) {
      if (!mountedRef.current) return;
      setReachable(false);
      setError(e as Error);
    } finally {
      if (mountedRef.current) setHasAttempted(true);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => {
      mountedRef.current = false;
      window.clearInterval(id);
    };
    // tick() is stable via fetcherRef; intervalMs is the only real dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);

  return { data, reachable, error, hasAttempted, refresh: tick };
}

/** Returns the active theme id and re-renders the calling component
 *  whenever it changes. Use in screens that render sparklines or other
 *  JS-computed colors that won't re-render on a CSS variable flip alone.
 *
 *  Components that ONLY use CSS variables (`var(--color-accent)` etc.)
 *  don't need this -- the browser repaints them automatically. */
export function useTheme(): ThemeId {
  const [theme, setLocal] = useState<ThemeId>(() => getTheme());
  useEffect(() => subscribeTheme(setLocal), []);
  return theme;
}
