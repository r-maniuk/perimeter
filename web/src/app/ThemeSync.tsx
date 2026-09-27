import { useEffect } from "react";
import { useUi } from "@/state/ui";

/** Keeps <html class="dark"> and the browser chrome in step with the chosen theme. */
export function ThemeSync() {
  const dark = useUi((s) => s.dark);
  const syncSystemTheme = useUi((s) => s.syncSystemTheme);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.setAttribute("content", dark ? "#121216" : "#f3f2ee");
    }
  }, [dark]);

  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    query.addEventListener("change", syncSystemTheme);
    return () => query.removeEventListener("change", syncSystemTheme);
  }, [syncSystemTheme]);

  return null;
}
