import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import LoginPage from "./page";

/**
 * Phase 3.5: the login form must handle a throttled response correctly.
 *
 * Rate limiting was added to POST /auth/login and POST /auth/signup, so the
 * client now has to distinguish "your password was wrong" (401) from "you are
 * being throttled" (429). Showing a credential error for a 429 would push a
 * user into retrying and burning more budget.
 *
 * The router and auth context are stubbed; nothing contacts a backend.
 */

const pushMock = vi.fn();
const loginMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: pushMock, prefetch: vi.fn() }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ login: loginMock, user: null, token: null, logout: vi.fn(), isLoading: false }),
}));

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function submit(container: HTMLElement) {
  const inputs = container.querySelectorAll("input");
  fireEvent.change(inputs[0], { target: { value: "someone" } });
  fireEvent.change(inputs[1], { target: { value: "a-good-password" } });
  const form = container.querySelector("form")!;
  fireEvent.submit(form);
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  pushMock.mockClear();
  loginMock.mockClear();
});

describe("login throttling (429)", () => {
  it("reports the wait time from Retry-After instead of a credential error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "Too many requests." }, 429, { "Retry-After": "42" })
      )
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/too many attempts/i)).toBeTruthy();
    });
    expect(screen.getByText(/42 seconds/i)).toBeTruthy();
    // A throttled attempt must not be mistaken for a successful sign-in.
    expect(loginMock).not.toHaveBeenCalled();
  });

  it("falls back to a generic wait when Retry-After is absent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Too many requests." }, 429))
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/too many attempts/i)).toBeTruthy();
    });
    expect(screen.getByText(/a moment/i)).toBeTruthy();
  });

  it("handles a 429 with no JSON body at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("rate limited", { status: 429, headers: { "Retry-After": "7" } })
      )
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/too many attempts/i)).toBeTruthy();
    });
    expect(screen.getByText(/7 seconds/i)).toBeTruthy();
  });

  it("uses the singular form for a one-second wait", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({}, 429, { "Retry-After": "1" }))
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/1 second\b/i)).toBeTruthy();
    });
  });
});

describe("other authentication outcomes", () => {
  it("shows the backend detail for a rejected password", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "Invalid username or password" }, 401)
      )
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/invalid username or password/i)).toBeTruthy();
    });
    expect(loginMock).not.toHaveBeenCalled();
  });

  it("signs in on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ access_token: "a.b.c", role: "client" })
      )
    );

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(loginMock).toHaveBeenCalledWith("a.b.c", "client", "someone");
    });
  });

  it("does not send a role on signup", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ access_token: "a.b.c", role: "client" }));
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<LoginPage />);
    // Switch to the registration tab.
    fireEvent.click(screen.getByText(/PROTOCOL_SIGNUP/i));
    submit(container);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("role");
    expect(body.username).toBe("someone");
  });

  it("surfaces a network failure without leaking an object", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const { container } = render(<LoginPage />);
    submit(container);

    await waitFor(() => {
      expect(screen.getByText(/failed to fetch|could not reach the server/i)).toBeTruthy();
    });
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });
});
