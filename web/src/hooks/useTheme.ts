/** Light/dark theme toggle, persisted per browser; index.html applies it pre-paint. */
import { useCallback, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "dsa-theme";

function current(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(current);
  const toggle = useCallback(() => {
    const next: Theme = current() === "dark" ? "light" : "dark";
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Storage blocked (private window): the toggle still works for this visit.
    }
    setTheme(next);
  }, []);
  return [theme, toggle];
}
