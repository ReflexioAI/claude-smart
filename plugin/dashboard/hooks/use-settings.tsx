"use client";

import {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
} from "react";

interface Settings {
  reflexioUrl: string;
}

type SettingsContextValue = Settings;

const SettingsContext = createContext<SettingsContextValue | null>(null);

const DEFAULT_URL = "http://localhost:8071";
export const SETTINGS_CHANGED_EVENT = "claude-smart-settings-changed";

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>({ reflexioUrl: DEFAULT_URL });

  useEffect(() => {
    let cancelled = false;
    async function loadSettings() {
      try {
        const res = await fetch("/api/config", { cache: "no-store" });
        if (!res.ok) return;
        const config = (await res.json()) as { REFLEXIO_URL?: string };
        if (!cancelled) {
          setSettings({ reflexioUrl: config.REFLEXIO_URL || DEFAULT_URL });
        }
      } catch {
        // Keep the dashboard on its local default when config cannot be read.
      }
    }
    void loadSettings();
    const onSettingsChanged = () => {
      void loadSettings();
    };
    window.addEventListener(SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      cancelled = true;
      window.removeEventListener(SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, []);

  return <SettingsContext.Provider value={settings}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within SettingsProvider");
  return ctx;
}
