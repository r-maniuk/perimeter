import { Compass, House, Maximize2, Minus, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useZones } from "@/features/zones/useZones";
import { HOME, mapController } from "@/map/controller";
import { usePointer } from "@/state/pointer";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";

function useBearing(): number {
  const ready = usePointer((s) => s.mapReady);
  const [bearing, setBearing] = useState(0);
  useEffect(() => {
    const map = mapController.map;
    if (!ready || !map) return;
    const update = () => setBearing(map.getBearing());
    map.on("rotate", update);
    return () => {
      map.off("rotate", update);
    };
  }, [ready]);
  return bearing;
}

export function MapControls() {
  const bearing = useBearing();
  const { data: zones } = useZones();
  // Step aside for the inspector, which takes the right-hand column while something is selected.
  const inspecting = useUi((s) => s.selection !== null);

  return (
    <div
      className={cx(
        "fixed bottom-10 z-20 flex flex-col gap-2 transition-[right] duration-300 ease-out-quint",
        inspecting ? "right-[384px]" : "right-4",
      )}
    >
      <div className="glass flex flex-col rounded-2xl p-1">
        <IconButton label="Zoom in" side="left" onClick={() => mapController.zoomBy(1)}>
          <Plus className="size-4" />
        </IconButton>
        <IconButton label="Zoom out" side="left" onClick={() => mapController.zoomBy(-1)}>
          <Minus className="size-4" />
        </IconButton>
      </div>
      <div className="glass flex flex-col rounded-2xl p-1">
        <IconButton label="Reset north" side="left" onClick={() => mapController.resetNorth()}>
          <Compass
            className="size-4 transition-transform duration-150"
            style={{ transform: `rotate(${-bearing - 45}deg)` }}
          />
        </IconButton>
        <IconButton
          label="Back to the city"
          side="left"
          onClick={() => useUi.getState().flyTo(HOME.lat, HOME.lon, HOME.zoom)}
        >
          <House className="size-4" />
        </IconButton>
        <IconButton
          label="Show all zones"
          side="left"
          disabled={!zones || zones.length === 0}
          onClick={() => zones && mapController.fitZones(zones)}
        >
          <Maximize2 className="size-4" />
        </IconButton>
      </div>
    </div>
  );
}
