import { QueryClientProvider } from "@tanstack/react-query";
import { LazyMotion, MotionConfig } from "motion/react";
import { Tooltip } from "radix-ui";
import { queryClient } from "./queryClient";
import { SessionGate } from "./SessionGate";
import { ThemeSync } from "./ThemeSync";

const loadMotionFeatures = () => import("./motionFeatures").then((module) => module.default);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <LazyMotion features={loadMotionFeatures} strict>
        <MotionConfig reducedMotion="user">
          <Tooltip.Provider delayDuration={350} skipDelayDuration={150}>
            <ThemeSync />
            <SessionGate />
          </Tooltip.Provider>
        </MotionConfig>
      </LazyMotion>
    </QueryClientProvider>
  );
}
