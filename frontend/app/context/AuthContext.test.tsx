import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth } from "./AuthContext";
import { TOKEN_STORAGE_KEY, USER_STORAGE_KEY } from "../lib/api";

/**
 * Session handling.
 *
 * The properties pinned here are the ones that decide whether a signed-out or
 * expired session can still see private data:
 *
 *  * localStorage is never trusted. The stored role is re-read from the
 *    server on every restore, and a role the backend does not recognise is
 *    narrowed to the least-privileged one.
 *  * A 401 clears the stored session rather than leaving the user apparently
 *    signed in until their next request fails.
 *  * A network failure does NOT sign the user out - the backend being briefly
 *    unreachable is not proof the token is invalid - but every protected
 *    route still re-authorizes server-side.
 *  * Signing out clears in-memory state, not only storage.
 */

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: pushMock, prefetch: vi.fn() }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Renders the context's state so tests assert behaviour, not internals. */
function Probe() {
  const { user, token, isLoading, logout, login } = useAuth();
  return (
    <div>
      <span data-testid="loading">{String(isLoading)}</span>
      <span data-testid="username">{user?.username ?? "none"}</span>
      <span data-testid="role">{user?.role ?? "none"}</span>
      <span data-testid="has-token">{token ? "yes" : "no"}</span>
      <button onClick={logout}>sign out</button>
      <button onClick={() => login("new.token", "agent", "bob")}>sign in</button>
    </div>
  );
}

/**
 * The provider verifies the session asynchronously on mount, so the render is
 * wrapped in `act` and awaited: otherwise that state update lands outside the
 * test's act scope and React warns.
 */
async function renderAuth() {
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
  });
  return result;
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  pushMock.mockClear();
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe("no stored session", () => {
  it("redirects to the login page and renders no identity", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("username").textContent).toBe("none");
    expect(pushMock).toHaveBeenCalledWith("/login");
  });

  it("does not call the API when there is nothing to verify", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("treats a token with no cached user as signed out", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "a.b.c");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await renderAuth();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/login"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("a stored session is verified against the server", () => {
  beforeEach(() => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "a.b.c");
    window.localStorage.setItem(
      USER_STORAGE_KEY,
      JSON.stringify({ username: "alice", role: "client" })
    );
  });

  it("adopts the server's view of the identity", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "agent" }))
    );
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("role").textContent).toBe("agent"));
    expect(screen.getByTestId("username").textContent).toBe("alice");
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("overwrites a stale cached role with the server's", async () => {
    // The cache says client; the database says agent.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "agent" }))
    );
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("role").textContent).toBe("agent"));
    const cached = JSON.parse(window.localStorage.getItem(USER_STORAGE_KEY)!);
    expect(cached.role).toBe("agent");
  });

  it("does not trust a role the backend does not recognise", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "superuser" }))
    );
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("role").textContent).toBe("client");
  });

  it("sends the token as a bearer header, never in the URL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ username: "alice", role: "client" }));
    vi.stubGlobal("fetch", fetchMock);
    await renderAuth();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://backend.test/auth/me");
    expect(init.headers.Authorization).toBe("Bearer a.b.c");
    expect(String(url)).not.toContain("a.b.c");
  });
});

describe("authentication loss", () => {
  beforeEach(() => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "expired.token");
    window.localStorage.setItem(
      USER_STORAGE_KEY,
      JSON.stringify({ username: "alice", role: "agent" })
    );
  });

  it("clears the stored session on a 401 and redirects", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(jsonResponse({ detail: "Could not validate credentials" }, 401))
    );
    await renderAuth();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/login"));
    expect(screen.getByTestId("username").textContent).toBe("none");
    expect(screen.getByTestId("has-token").textContent).toBe("no");
    // The stale credential must not be left behind for the next page load.
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem(USER_STORAGE_KEY)).toBeNull();
  });

  it("keeps the cached session when the backend is merely unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("username").textContent).toBe("alice");
    expect(pushMock).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("expired.token");
  });

  it("keeps the session for a 500, which is not a statement about the token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Internal server error" }, 500))
    );
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("username").textContent).toBe("alice");
  });

  it("renders no identity when the cached record is corrupt and the API is down", async () => {
    window.localStorage.setItem(USER_STORAGE_KEY, "{not json");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("username").textContent).toBe("none");
  });

  it("narrows a tampered cached role when falling back to the cache", async () => {
    // localStorage is user-writable, so a cached "agent" must still be
    // checked. Here the value is not even a valid role.
    window.localStorage.setItem(
      USER_STORAGE_KEY,
      JSON.stringify({ username: "alice", role: "root" })
    );
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    expect(screen.getByTestId("role").textContent).toBe("client");
  });
});

describe("sign in and sign out", () => {
  it("stores the session and leaves the login page on sign in", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await renderAuth();
    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    pushMock.mockClear();

    fireEvent.click(screen.getByText("sign in"));

    expect(screen.getByTestId("username").textContent).toBe("bob");
    expect(screen.getByTestId("role").textContent).toBe("agent");
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("new.token");
    expect(pushMock).toHaveBeenCalledWith("/");
  });

  it("clears in-memory state as well as storage on sign out", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "a.b.c");
    window.localStorage.setItem(
      USER_STORAGE_KEY,
      JSON.stringify({ username: "alice", role: "agent" })
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "agent" }))
    );
    await renderAuth();

    await waitFor(() => expect(screen.getByTestId("username").textContent).toBe("alice"));
    fireEvent.click(screen.getByText("sign out"));

    // Both matter: clearing storage alone would leave the private view
    // rendered until the redirect completed.
    expect(screen.getByTestId("username").textContent).toBe("none");
    expect(screen.getByTestId("has-token").textContent).toBe("no");
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem(USER_STORAGE_KEY)).toBeNull();
    expect(pushMock).toHaveBeenCalledWith("/login");
  });
});

describe("useAuth outside a provider", () => {
  it("fails loudly rather than returning a signed-out-looking context", () => {
    // Returning a null user here would silently render protected screens as
    // if the user were simply signed out.
    const Orphan = () => {
      useAuth();
      return null;
    };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Orphan />)).toThrow(/must be used within an AuthProvider/);
    spy.mockRestore();
  });
});
