import type { ComponentType } from "react";
import { AlertsPanel } from "@/features/alerts/AlertsPanel";
import { FleetPanel } from "@/features/fleet/FleetPanel";
import { OpsPanel } from "@/features/ops/OpsPanel";
import { SessionsPanel } from "@/features/sessions/SessionsPanel";
import { ZonesPanel } from "@/features/zones/ZonesPanel";
import type { Panel } from "@/state/ui";

export interface PanelProps {
  onClose?: (() => void) | undefined;
  titleId?: string | undefined;
}

export const PANEL_VIEWS: Record<Panel, ComponentType<PanelProps>> = {
  zones: ZonesPanel,
  alerts: AlertsPanel,
  fleet: FleetPanel,
  sessions: SessionsPanel,
  ops: OpsPanel,
};
