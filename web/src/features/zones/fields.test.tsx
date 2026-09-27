// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RadiusField, ZoneNameField } from "./fields";

afterEach(cleanup);

function radiusField(radius = 300) {
  const onCommit = vi.fn();
  const view = render(<RadiusField radius={radius} onCommit={onCommit} />);
  const input = screen.getByRole("textbox", { name: "Radius" }) as HTMLInputElement;
  const rerender = (next: number) =>
    view.rerender(<RadiusField radius={next} onCommit={onCommit} />);
  return { onCommit, input, rerender };
}

function nameField(name = "Dam Square") {
  const onCommit = vi.fn();
  const view = render(<ZoneNameField name={name} color="#6d5dfc" onCommit={onCommit} />);
  const input = screen.getByRole("textbox", { name: "Zone name" }) as HTMLInputElement;
  const rerender = (next: string) =>
    view.rerender(<ZoneNameField name={next} color="#6d5dfc" onCommit={onCommit} />);
  return { onCommit, input, rerender };
}

describe("radius field", () => {
  it("saves once on Enter; leaving the field afterwards sends nothing more", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = radiusField();
    await user.clear(input);
    await user.type(input, "400{Enter}");
    await user.tab();
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(400);
  });

  it("follows the zone while it holds no edit, and never re-sends on leave", async () => {
    const user = userEvent.setup();
    const { onCommit, input, rerender } = radiusField();
    await user.click(input);
    rerender(345);
    expect(input.value).toBe("345 m");
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("keeps a pending edit when the zone changes elsewhere, and saves it on leave", async () => {
    const user = userEvent.setup();
    const { onCommit, input, rerender } = radiusField();
    await user.clear(input);
    await user.type(input, "410");
    rerender(345);
    expect(input.value).toBe("410");
    await user.tab();
    expect(onCommit).toHaveBeenCalledWith(410);
  });

  it("puts the zone's radius back on Escape without saving", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = radiusField();
    await user.clear(input);
    await user.type(input, "999{Escape}");
    expect(input.value).toBe("300 m");
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("flags text it cannot read, and restores the radius when left", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = radiusField();
    await user.clear(input);
    await user.type(input, "wide{Enter}");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    await user.tab();
    expect(input.value).toBe("300 m");
    expect(input.getAttribute("aria-invalid")).toBeNull();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("clamps to the allowed range and reads kilometres", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = radiusField();
    await user.clear(input);
    await user.type(input, "1.5 km{Enter}");
    expect(onCommit).toHaveBeenLastCalledWith(1_500);
    await user.clear(input);
    await user.type(input, "2{Enter}");
    expect(onCommit).toHaveBeenLastCalledWith(10);
  });
});

describe("zone name field", () => {
  it("saves the trimmed name once on Enter", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = nameField();
    await user.clear(input);
    await user.type(input, "  Dam  {Enter}");
    await user.click(document.body);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith("Dam");
  });

  it("does not undo a rename from another session when left untouched", async () => {
    const user = userEvent.setup();
    const { onCommit, input, rerender } = nameField();
    await user.click(input);
    rerender("De Dam");
    expect(input.value).toBe("De Dam");
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("reverts on Escape without saving", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = nameField();
    await user.clear(input);
    await user.type(input, "Scratch{Escape}");
    expect(input.value).toBe("Dam Square");
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("refuses an empty name and shows the saved one again", async () => {
    const user = userEvent.setup();
    const { onCommit, input } = nameField();
    await user.clear(input);
    await user.type(input, "   {Enter}");
    expect(input.value).toBe("Dam Square");
    expect(onCommit).not.toHaveBeenCalled();
  });
});
