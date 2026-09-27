// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CoordinateField, RadiusField, ZoneNameField } from "./fields";

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

const DAM = { lat: 52.3731, lon: 4.8926 };

function coordinateFields(center = DAM) {
  const onCommit = vi.fn();
  const fields = (at: { lat: number; lon: number }) => (
    <>
      <CoordinateField axis="lat" center={at} onCommit={onCommit} />
      <CoordinateField axis="lon" center={at} onCommit={onCommit} />
    </>
  );
  const view = render(fields(center));
  const lat = screen.getByRole("textbox", { name: "Centre latitude" }) as HTMLInputElement;
  const lon = screen.getByRole("textbox", { name: "Centre longitude" }) as HTMLInputElement;
  return { onCommit, lat, lon, rerender: (at: typeof DAM) => view.rerender(fields(at)) };
}

describe("centre fields", () => {
  it("shows the centre to six decimals and moves it by one coordinate on Enter", async () => {
    const user = userEvent.setup();
    const { onCommit, lat, lon } = coordinateFields();
    expect([lat.value, lon.value]).toEqual(["52.373100", "4.892600"]);
    await user.clear(lat);
    await user.type(lat, "52,38{Enter}");
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith({ lat: 52.38, lon: 4.8926 });
    expect(lat.value).toBe("52.380000");
    await user.tab();
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  it("reads hemispheres, and moves both coordinates when a pair is pasted", async () => {
    const user = userEvent.setup();
    const { onCommit, lat, lon } = coordinateFields();
    await user.clear(lon);
    await user.type(lon, "74.006 W{Enter}");
    expect(onCommit).toHaveBeenLastCalledWith({ lat: 52.3731, lon: -74.006 });
    await user.clear(lat);
    await user.click(lat);
    await user.paste("40.712800, -74.006000");
    await user.keyboard("{Enter}");
    expect(onCommit).toHaveBeenLastCalledWith({ lat: 40.7128, lon: -74.006 });
  });

  it("flags what it cannot read or what lies off the globe, and restores the centre when left", async () => {
    const user = userEvent.setup();
    const { onCommit, lat } = coordinateFields();
    for (const text of ["north", "91", "4.9 E"]) {
      await user.clear(lat);
      await user.type(lat, `${text}{Enter}`);
      expect(lat.getAttribute("aria-invalid")).toBe("true");
    }
    await user.tab();
    expect(lat.value).toBe("52.373100");
    expect(lat.getAttribute("aria-invalid")).toBeNull();
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("puts the centre back on Escape, and sends nothing for a value it already has", async () => {
    const user = userEvent.setup();
    const { onCommit, lat, lon } = coordinateFields();
    await user.clear(lat);
    await user.type(lat, "10{Escape}");
    expect(lat.value).toBe("52.373100");
    await user.clear(lon);
    await user.type(lon, "4.8926000{Enter}");
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("follows the zone while it holds no edit, and keeps a pending edit when it moves", async () => {
    const user = userEvent.setup();
    const { onCommit, lat, lon, rerender } = coordinateFields();
    await user.click(lat);
    rerender({ lat: 52.36, lon: 4.9 });
    expect([lat.value, lon.value]).toEqual(["52.360000", "4.900000"]);
    await user.clear(lat);
    await user.type(lat, "52.35");
    rerender({ lat: 52.37, lon: 4.91 });
    expect(lat.value).toBe("52.35");
    await user.tab();
    expect(onCommit).toHaveBeenCalledWith({ lat: 52.35, lon: 4.91 });
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
