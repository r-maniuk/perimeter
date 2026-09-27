/**
 * Browser APIs that jsdom leaves out, for component tests. Import it first: stores read the
 * colour scheme preference when their module loads.
 */
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

if (typeof window !== "undefined" && typeof window.ResizeObserver !== "function") {
  // Radix measures its sliders and popovers; jsdom lays nothing out, so nothing ever resizes.
  window.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}
