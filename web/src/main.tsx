import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./styles/app.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Development only: a handle for inspecting state from the browser console.
if (import.meta.env.DEV) {
  void Promise.all([
    import("./map/controller"),
    import("./state/ui"),
    import("./state/pointer"),
    import("./app/queryClient"),
    import("./app/runtime"),
  ]).then(([map, ui, pointer, query, runtime]) => {
    Object.assign(window, {
      __perimeter: {
        map: map.mapController,
        ui: ui.useUi,
        pointer: pointer.usePointer,
        query: query.queryClient,
        runtime: runtime.getRuntime,
      },
    });
  });
}
