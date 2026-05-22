"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  ReactNode,
} from "react";

interface Settings {
  reflexioUrl: string;
}

interface SettingsContextValue extends Settings {
  setReflexioUrl: (url: string) => void;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

const STORAGE_KEY = "claude-smart-dashboard-settings";
const DEFAULT_URL = "http://localhost:8071";
const DEFAULTS: Settings = { reflexioUrl: DEFAULT_URL };
const DEFAULT_JSON = JSON.stringify(DEFAULTS);

type Listener = () => void;
const listeners = new Set<Listener>();

function readStorage(): string {
  if (typeof window === "undefined") return DEFAULT_JSON;
  try {
    return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_JSON;
  } catch {
    return DEFAULT_JSON;
  }
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  const onStorage = (ev: StorageEvent) => {
    if (ev.key === STORAGE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function writeStorage(settings: Settings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // ignore
  }
  for (const l of listeners) l();
}

function parse(json: string): Settings {
  try {
    const parsed = JSON.parse(json);
    return { ...DEFAULTS, ...parsed };
  } catch {
    return DEFAULTS;
  }
}

function isLocalUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return ["localhost", "127.0.0.1", "0.0.0.0", "::1"].includes(url.hostname);
  } catch {
    return false;
  }
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const raw = useSyncExternalStore(subscribe, readStorage, () => DEFAULT_JSON);
  const settings = useMemo(() => parse(raw), [raw]);

  useEffect(() => {
    let cancelled = false;
    async function syncManagedUrl() {
      try {
        const res = await fetch("/api/config", { cache: "no-store" });
        if (!res.ok) return;
        const config = (await res.json()) as { REFLEXIO_URL?: string };
        const configuredUrl = config.REFLEXIO_URL;
        if (
          !cancelled &&
          configuredUrl &&
          !isLocalUrl(configuredUrl) &&
          isLocalUrl(settings.reflexioUrl)
        ) {
          writeStorage({ reflexioUrl: configuredUrl });
        }
      } catch {
        // Keep the dashboard on its local default when config cannot be read.
      }
    }
    void syncManagedUrl();
    return () => {
      cancelled = true;
    };
  }, [settings.reflexioUrl]);

  const setReflexioUrl = useCallback((url: string) => {
    writeStorage({ reflexioUrl: url });
  }, []);

  return (
    <SettingsContext.Provider value={{ ...settings, setReflexioUrl }}>
      {children}
    </SettingsContext.Provider>
  );
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within SettingsProvider");
  return ctx;
}
