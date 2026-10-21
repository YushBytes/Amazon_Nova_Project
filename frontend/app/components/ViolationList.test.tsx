import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ViolationList from "./ViolationList";
import type { Detection } from "../lib/api";

/**
 * Phase 7: findings are rendered for the first time.
 *
 * Model output is not schema-validated server-side, so every optional field
 * can be absent. These tests pin that a missing field reads as missing rather
 * than as a fabricated value - the case that matters most being confidence,
 * where a rendered "0%" would assert certainty the model never expressed.
 */

function renderList(detections: Detection[], onSeek?: (s: number) => void) {
  return render(<ViolationList detections={detections} onSeek={onSeek} />);
}

describe("full detections", () => {
  it("renders every field the model supplied", () => {
    renderList([
      {
        category: "competitor_logo",
        description: "A competitor mark is visible on the cup.",
        confidence: 0.91,
        risk_level: "High",
        bounding_box: [100, 200, 300, 400],
        timestamp: 65,
        frame_number: 65,
      },
    ]);

    expect(screen.getByText("competitor_logo")).toBeTruthy();
    expect(screen.getByText(/a competitor mark is visible/i)).toBeTruthy();
    expect(screen.getByText("High risk")).toBeTruthy();
    expect(screen.getByText("91%")).toBeTruthy();
    expect(screen.getByText("1:05")).toBeTruthy();
    expect(screen.getByText("65")).toBeTruthy();
    expect(screen.getByText(/located on frame/i)).toBeTruthy();
  });
});

describe("missing optional fields stay missing", () => {
  it("reports an absent confidence rather than printing zero", () => {
    renderList([{ category: "unsafe_item", timestamp: 2 }]);

    expect(screen.getByText(/confidence/i)).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
    // Two fields are unreported here: confidence and severity.
    expect(screen.getAllByText("Not reported").length).toBeGreaterThanOrEqual(1);
  });

  it("distinguishes a reported zero confidence from an absent one", () => {
    renderList([{ category: "x", confidence: 0, timestamp: 1 }]);
    expect(screen.getByText("0%")).toBeTruthy();
  });

  it("says so when the model reported no severity", () => {
    renderList([{ category: "x", timestamp: 1 }]);
    expect(screen.getByText(/severity not reported/i)).toBeTruthy();
  });

  it("omits the on-frame marker when the box is absent or malformed", () => {
    renderList([
      { category: "no-box", timestamp: 1 },
      { category: "bad-box", bounding_box: [1, 2, 3], timestamp: 2 },
    ]);
    expect(screen.queryByText(/located on frame/i)).toBeNull();
  });

  it("falls back to a neutral title when the category is missing", () => {
    renderList([{ timestamp: 1 }]);
    expect(screen.getByText("Violation")).toBeTruthy();
  });

  it("renders no description paragraph for the empty string the backend normalises to", () => {
    // backend/app.py _normalise_detection sets description to "" when absent.
    const { container } = renderList([{ category: "x", description: "", timestamp: 1 }]);
    const item = container.querySelector("li")!;
    // The only <p> in a row is the description, so there must be none.
    expect(item.querySelectorAll("p")).toHaveLength(0);
  });

  it("omits the frame row when no frame number was recorded", () => {
    renderList([{ category: "x", timestamp: 1 }]);
    expect(screen.queryByText(/^frame$/i)).toBeNull();
  });

  it("reports an absent timestamp rather than rendering 0:00", () => {
    renderList([{ category: "x" }]);
    expect(screen.queryByText("0:00")).toBeNull();
  });
});

describe("ordering and seeking", () => {
  it("lists the most severe findings first", () => {
    const { container } = renderList([
      { category: "low-one", risk_level: "Low", timestamp: 1 },
      { category: "high-one", risk_level: "High", timestamp: 2 },
      { category: "unrated", timestamp: 3 },
      { category: "medium-one", risk_level: "Medium", timestamp: 4 },
    ]);

    const titles = Array.from(container.querySelectorAll("h4")).map((h) => h.textContent);
    expect(titles).toEqual(["high-one", "medium-one", "low-one", "unrated"]);
  });

  it("offers a keyboard-reachable jump to a finding's timestamp", () => {
    const onSeek = vi.fn();
    renderList([{ category: "x", timestamp: 12 }], onSeek);

    const button = screen.getByRole("button", { name: /jump to 0:12/i });
    fireEvent.click(button);
    expect(onSeek).toHaveBeenCalledWith(12);
  });

  it("offers no jump when there is no player to seek", () => {
    renderList([{ category: "x", timestamp: 12 }]);
    expect(screen.queryByRole("button", { name: /jump to/i })).toBeNull();
  });

  it("offers no jump for a finding with no timestamp", () => {
    renderList([{ category: "x" }], vi.fn());
    expect(screen.queryByRole("button", { name: /jump to/i })).toBeNull();
  });
});

describe("severity is never colour alone", () => {
  it("states each severity as a word", () => {
    renderList([
      { category: "a", risk_level: "High", timestamp: 1 },
      { category: "b", risk_level: "Medium", timestamp: 2 },
      { category: "c", risk_level: "Low", timestamp: 3 },
    ]);
    expect(screen.getByText("High risk")).toBeTruthy();
    expect(screen.getByText("Medium risk")).toBeTruthy();
    expect(screen.getByText("Low risk")).toBeTruthy();
  });

  it("shows an unrecognised severity verbatim instead of guessing one", () => {
    renderList([{ category: "a", risk_level: "SEVERE-9", timestamp: 1 }]);
    expect(screen.getByText("SEVERE-9")).toBeTruthy();
  });
});
