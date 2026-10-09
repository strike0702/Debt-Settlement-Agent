/**
 * Light/dark theme toggle, persisted per browser; index.html applies it pre-paint.
 * The toggle also keeps `<meta name="theme-color">` on the page background, so
 * mobile browser chrome matches the theme (Phase 48).
 */
import { useCallback, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "dsa-theme";
/** `--bg` of each theme (src/index.css). */
export const THEME_COLOR: Record<Theme, string> = { light: "#f6f6f4", dark: "#121211" };

function current(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(current);
  const toggle = useCallback(() => {
    const next: Theme = current() === "dark" ? "light" : "dark";
    document.documentElement.classList.toggle("dark", next === "dark");
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLOR[next]);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Storage blocked (private window): the toggle still works for this visit.
    }
    setTheme(next);
  }, []);
  return [theme, toggle];
}
