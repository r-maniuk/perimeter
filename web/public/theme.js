// Applies the saved (or system) theme before first paint, so dark-mode users never see a flash.
(() => {
  try {
    const saved = localStorage.getItem("perimeter.theme");
    const dark =
      saved === "dark" ||
      (saved !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
  } catch {
    // Storage blocked: the app applies the system theme once it starts.
  }
})();
