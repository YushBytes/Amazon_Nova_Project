import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import UnifiedCommandCenter from "./page";

/**
 * The application shell.
 *
 * Phase 7 removed a fabricated account screen: a generated avatar for
 * "Commander Niru, Level 10 Overseer" - a name unrelated to whoever was
 * signed in - plus a "Security Clearance" panel reporting "Bio-Sync 98%",
 * "Grid Isolation ENABLED" and "Neural Bridge ACTIVE", and two buttons wired
 * to nothing. The header also carried a pulsing "Sync: US-EAST-1" indicator
 * and a notification bell with an unread dot, neither backed by anything.
 *
 * Role-based navigation is asserted here as PRESENTATION only. Hiding a nav
 * entry is not authorization: the backend authorizes every route and
 * re-checks ownership per request, which is what these tests must not be read
 * as replacing.
 */

const logoutMock = vi.fn();
let authState: {
  user: { username: string; role: "client" | "agent" } | null;
  isLoading: boolean;
} = { user: null, isLoading: false };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("./context/AuthContext", () => ({
  useAuth: () => ({
    user: authState.user,
    token: authState.user ? "a.b.c" : null,
    isLoading: authState.isLoading,
    login: vi.fn(),
    logout: logoutMock,
  }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The shell's child views fetch on mount; answer them with empty data. */
function stubBackend() {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes("/submissions")) return jsonResponse({ submissions: [] });
    if (url.includes("/health/worker")) {
      return jsonResponse({ status: "ok", execution_mode: "worker", counts: {}, active: 0, abandoned: 0 });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  logoutMock.mockClear();
  authState = { user: null, isLoading: false };
});

describe("signed out", () => {
  it("renders nothing rather than a protected shell", () => {
    stubBackend();
    const { container } = render(<UnifiedCommandCenter />);
    // AuthContext performs the redirect; this component must not paint
    // private chrome in the meantime.
    expect(container.textContent).toBe("");
  });

  it("announces that it is verifying the session while loading", () => {
    authState = { user: null, isLoading: true };
    stubBackend();
    render(<UnifiedCommandCenter />);
    expect(screen.getByRole("status").textContent).toMatch(/verifying your session/i);
  });
});

describe("the fabricated account screen is gone", () => {
  beforeEach(() => {
    authState = { user: { username: "alice", role: "agent" }, isLoading: false };
  });

  it("shows the signed-in identity, not an invented persona", async () => {
    stubBackend();
    render(<UnifiedCommandCenter />);

    fireEvent.click(screen.getByRole("button", { name: /^account$/i }));

    await waitFor(() => expect(screen.getByText(/signed in as/i)).toBeTruthy());
    expect(screen.getAllByText("alice").length).toBeGreaterThan(0);
    expect(screen.queryByText(/commander niru/i)).toBeNull();
    expect(screen.queryByText(/level 10 overseer/i)).toBeNull();
    expect(screen.queryByText(/bio-sync/i)).toBeNull();
    expect(screen.queryByText(/grid isolation/i)).toBeNull();
    expect(screen.queryByText(/neural bridge/i)).toBeNull();
    expect(screen.queryByText(/identity protocol/i)).toBeNull();
  });

  it("loads no avatar image from a third party", async () => {
    stubBackend();
    const { container } = render(<UnifiedCommandCenter />);
    fireEvent.click(screen.getByRole("button", { name: /^account$/i }));

    await waitFor(() => expect(screen.getByText(/signed in as/i)).toBeTruthy());
    expect(container.querySelectorAll("img")).toHaveLength(0);
  });

  it("says which account operations do not exist", async () => {
    stubBackend();
    render(<UnifiedCommandCenter />);
    fireEvent.click(screen.getByRole("button", { name: /^account$/i }));

    await waitFor(() => expect(screen.getByText(/account management/i)).toBeTruthy());
    // Including the honest limit of signing out: no server-side revocation.
    expect(screen.getByText(/does not revoke it server-side/i)).toBeTruthy();
  });

  it("carries no fabricated header indicators", () => {
    stubBackend();
    const { container } = render(<UnifiedCommandCenter />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/us-east-1/i);
    expect(text).not.toMatch(/secure node/i);
    expect(text).not.toMatch(/v2\.4 control/i);
  });

  it("signs out from the account screen", async () => {
    stubBackend();
    render(<UnifiedCommandCenter />);
    fireEvent.click(screen.getByRole("button", { name: /^account$/i }));

    await waitFor(() => expect(screen.getByText(/signed in as/i)).toBeTruthy());
    // Distinct from the sidebar control, and named for what it actually
    // does: there is no server-side revocation.
    fireEvent.click(screen.getByRole("button", { name: /sign out of this browser/i }));
    expect(logoutMock).toHaveBeenCalled();
  });
});

describe("role-based navigation is presentation only", () => {
  it("shows the agent-only entries to an agent", () => {
    authState = { user: { username: "alice", role: "agent" }, isLoading: false };
    stubBackend();
    render(<UnifiedCommandCenter />);

    expect(screen.getByRole("button", { name: /^dashboard$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^infrastructure$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^compliance log$/i })).toBeTruthy();
  });

  it("hides them from a client, and opens that client on their own videos", async () => {
    authState = { user: { username: "bob", role: "client" }, isLoading: false };
    stubBackend();
    render(<UnifiedCommandCenter />);

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^dashboard$/i })).toBeNull()
    );
    expect(screen.queryByRole("button", { name: /^infrastructure$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^your videos$/i })).toBeTruthy();
    // A client still gets their own audit history.
    expect(screen.getByRole("button", { name: /^compliance log$/i })).toBeTruthy();
  });

  it("marks the active entry for assistive technology", async () => {
    authState = { user: { username: "alice", role: "agent" }, isLoading: false };
    stubBackend();
    render(<UnifiedCommandCenter />);

    fireEvent.click(screen.getByRole("button", { name: /^compliance log$/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /^compliance log$/i }).getAttribute("aria-current")
      ).toBe("page")
    );
  });

  it("labels the navigation landmark", () => {
    authState = { user: { username: "alice", role: "agent" }, isLoading: false };
    stubBackend();
    render(<UnifiedCommandCenter />);
    expect(screen.getByRole("navigation", { name: /main/i })).toBeTruthy();
  });

  it("fetches only from the view that is open", async () => {
    authState = { user: { username: "alice", role: "agent" }, isLoading: false };
    const stub = stubBackend();
    render(<UnifiedCommandCenter />);

    // The dashboard is open and requests nothing until a file is submitted.
    await new Promise((r) => setTimeout(r, 30));
    expect(stub.calls).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: /^infrastructure$/i }));
    await waitFor(() =>
      expect(stub.calls.some((u) => u.includes("/health/worker"))).toBe(true)
    );
    expect(stub.calls.some((u) => u.includes("/submissions"))).toBe(false);
  });
});
