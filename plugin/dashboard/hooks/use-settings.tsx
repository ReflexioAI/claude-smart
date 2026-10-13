"use client";

import {
  createContext,
  Fragment,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { selectDataSource, type DataSource } from "@/lib/reflexio-client";

export const SETTINGS_CHANGED_EVENT = "claude-smart-settings-changed";
interface Connection {
  id: string;
  url: string;
  configured: boolean;
  learningModel?: string;
}
interface Settings {
  reflexioUrl: string;
  source: DataSource | null;
  sources: { local: Connection; managed: Connection } | null;
  error: string | null;
  changeSource: (source: DataSource) => void;
}
const SettingsContext = createContext<Settings | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [source, setSource] = useState<DataSource | null>(null);
  const [sources, setSources] = useState<Settings["sources"]>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const response = await fetch("/api/data-sources", {
          cache: "no-store",
        });
        if (!response.ok)
          throw new Error("Unable to load dashboard connections");
        const data = await response.json();
        if (cancelled) return;
        setSources({ local: data.local, managed: data.managed });
        const requested = new URLSearchParams(window.location.search).get(
          "source",
        );
        const selected =
          requested === "local" || requested === "managed"
            ? requested
            : data.defaultSource;
        selectDataSource(selected, data[selected].id);
        setSource(selected);
        setError(null);
      } catch (e) {
        if (!cancelled)
          setError(
            e instanceof Error ? e.message : "Unable to load connections",
          );
      }
    }
    void load();
    const reload = () => window.location.reload();
    window.addEventListener(SETTINGS_CHANGED_EVENT, reload);
    return () => {
      cancelled = true;
      window.removeEventListener(SETTINGS_CHANGED_EVENT, reload);
    };
  }, []);
  const changeSource = (next: DataSource) => {
    // A new document prevents detail IDs, in-flight actions, or cached records
    // from crossing backend boundaries. The runtime configuration is unchanged.
    window.location.assign(`/dashboard?source=${next}`);
  };
  return (
    <SettingsContext.Provider
      value={{
        source,
        sources,
        error,
        changeSource,
        reflexioUrl: source
          ? sources?.[source].url || "Not configured"
          : "Loading connection…",
      }}
    >
      {source ? (
        <Fragment key={source}>{children}</Fragment>
      ) : (
        <div role={error ? "alert" : "status"} className="p-6">
          {error || "Loading dashboard connection…"}
        </div>
      )}
    </SettingsContext.Provider>
  );
}
export function useSettings(): Settings {
  const value = useContext(SettingsContext);
  if (!value)
    throw new Error("useSettings must be used within SettingsProvider");
  return value;
}
