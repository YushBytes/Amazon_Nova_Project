"use client";

/**
 * Session state for the browser.
 *
 * Phase 7 changes:
 *   - requests go through the shared API client, so the base URL and the
 *     bearer-token handling live in one place rather than being re-derived
 *     here from `process.env`;
 *   - the role is narrowed through `coerceRole` on both paths (the server's
 *     answer and the cached copy), so an unexpected value renders as the
 *     least-privileged role instead of being passed through;
 *   - the cached user record is validated before use, because localStorage is
 *     user-writable.
 *
 * The security model is unchanged and deliberately so: the token is a bearer
 * credential in localStorage, it is never logged, and this context is NOT an
 * authorization boundary. Every protected route is authorized server-side and
 * re-checks ownership per request; what happens here only decides what is
 * worth rendering.
 */

import React, { createContext, useContext, useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  TOKEN_STORAGE_KEY,
  USER_STORAGE_KEY,
  coerceRole,
  fetchCurrentUser,
  type AuthenticatedUser,
  type UserRole,
} from "../lib/api";

interface AuthContextType {
  user: AuthenticatedUser | null;
  token: string | null;
  login: (token: string, role: string, username: string) => void;
  logout: () => void;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

/** Read the cached user record, or null when it is absent or unusable. */
function readCachedUser(raw: string | null): AuthenticatedUser | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const username = (parsed as { username?: unknown }).username;
    if (typeof username !== "string" || username === "") return null;
    return { username, role: coerceRole((parsed as { role?: unknown }).role) };
  } catch {
    return null;
  }
}

/** Storage can throw in private-browsing modes; never let that break a render. */
function safeSet(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Nothing to do: the session simply will not survive a reload.
  }
}

function safeRemove(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignored for the same reason.
  }
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthenticatedUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  // The restore below must run EXACTLY ONCE per mount, so the router is read
  // through a ref rather than being an effect dependency. Depending on the
  // router object means re-running the whole verification whenever its
  // identity changes - and since the effect's own setState triggers a
  // re-render, an unstable router turns that into a request loop. Nothing
  // about verifying a stored session should repeat on re-render.
  const routerRef = React.useRef(router);
  routerRef.current = router;

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const clearSession = () => {
      safeRemove(TOKEN_STORAGE_KEY);
      safeRemove(USER_STORAGE_KEY);
      setToken(null);
      setUser(null);
    };

    const restoreSession = async () => {
      let savedToken: string | null = null;
      let savedUser: string | null = null;
      try {
        savedToken = window.localStorage.getItem(TOKEN_STORAGE_KEY);
        savedUser = window.localStorage.getItem(USER_STORAGE_KEY);
      } catch {
        // Treated as signed out.
      }

      if (!savedToken || !savedUser) {
        if (window.location.pathname !== "/login") {
          routerRef.current.push("/login");
        }
        if (!cancelled) setIsLoading(false);
        return;
      }

      // Do not trust localStorage. The token may be expired, revoked, or
      // signed with a key this backend no longer accepts, and the stored role
      // may be stale. Ask the server, which reads the role from the database.
      try {
        const verified = await fetchCurrentUser({ signal: controller.signal });
        if (cancelled) return;

        setToken(savedToken);
        setUser(verified);
        // Keep the cached copy aligned with the server's view of the role.
        safeSet(USER_STORAGE_KEY, JSON.stringify(verified));
      } catch (err) {
        if (cancelled) return;

        const status =
          typeof err === "object" && err !== null && "status" in err
            ? (err as { status?: unknown }).status
            : undefined;

        // 401 means the session is gone; clear it rather than leaving the
        // user apparently signed in until their next request fails. Any other
        // 4xx/5xx is the server refusing this request, not proof the session
        // is invalid, so it is treated like a network failure below.
        if (status === 401) {
          clearSession();
          if (window.location.pathname !== "/login") {
            routerRef.current.push("/login");
          }
          return;
        }

        // Network or server failure: keep the cached session rather than
        // signing the user out because the backend was briefly unreachable.
        // Any protected request will still be rejected server-side if the
        // token is invalid.
        setToken(savedToken);
        setUser(readCachedUser(savedUser));
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    void restoreSession();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // Mount only: see routerRef above. The empty dependency list is correct
    // and complete - the effect closes over nothing that changes.
  }, []);

  const login = (newToken: string, role: string, username: string) => {
    // The role is narrowed here too: it arrives in the sign-in response and
    // drives navigation, so an unrecognised value must not widen anything.
    const userData: AuthenticatedUser = {
      username,
      role: coerceRole(role) satisfies UserRole,
    };
    setToken(newToken);
    setUser(userData);
    safeSet(TOKEN_STORAGE_KEY, newToken);
    safeSet(USER_STORAGE_KEY, JSON.stringify(userData));
    router.push("/");
  };

  const logout = () => {
    // In-memory state is cleared first, so a protected view stops rendering
    // private data in the same commit as the redirect rather than after it.
    setToken(null);
    setUser(null);
    safeRemove(TOKEN_STORAGE_KEY);
    safeRemove(USER_STORAGE_KEY);
    router.push("/login");
  };

  return (
    <AuthContext.Provider value={{ user, token, login, logout, isLoading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
};
