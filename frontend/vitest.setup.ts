import "@testing-library/react";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

/**
 * Global test setup.
 *
 * Every test starts with a clean DOM and no leaked fetch stub, so tests cannot
 * pass because of another test's mock.
 */

beforeEach(() => {
  // Point the API client at a deterministic origin rather than whatever the
  // developer's environment happens to define.
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// jsdom does not implement these; HUDView touches them for object URLs.
if (!globalThis.URL.createObjectURL) {
  globalThis.URL.createObjectURL = vi.fn(() => "blob:mock");
}
if (!globalThis.URL.revokeObjectURL) {
  globalThis.URL.revokeObjectURL = vi.fn();
}
