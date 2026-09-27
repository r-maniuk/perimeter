import { ArrowRightFromLine, ArrowRightToLine, Hourglass } from "lucide-react";
import type { ReactNode } from "react";
import type { AlertKind } from "@/api/schemas";

export const KIND_ICON: Record<AlertKind, ReactNode> = {
  enter: <ArrowRightToLine className="size-4" />,
  exit: <ArrowRightFromLine className="size-4" />,
  dwell: <Hourglass className="size-4" />,
};

export const KIND_LABEL: Record<AlertKind, string> = {
  enter: "Enter",
  exit: "Exit",
  dwell: "Dwell",
};

export const KIND_TEXT: Record<AlertKind, string> = {
  enter: "text-enter",
  exit: "text-exit",
  dwell: "text-dwell",
};
