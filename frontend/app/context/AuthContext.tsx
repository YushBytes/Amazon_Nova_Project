"use client";

import React, { createContext, useContext, useState, useEffect } from "react";
import { useRouter } from "next/navigation";

interface User {
  username: string;
  role: "client" | "agent";
}

interface AuthContextType {
  user: User | null;
  token: string | null;
  login: (token: string, role: string, username: string) => void;
  logout: () => void;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();
  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  useEffect(() => {
    let cancelled = false;

    const restoreSession = async () => {
      const savedToken = localStorage.getItem("novaflow_token");
      const savedUser = localStorage.getItem("novaflow_user");

      if (!savedToken || !savedUser) {
        if (window.location.pathname !== "/login") {
          router.push("/login");
        }
        if (!cancelled) setIsLoading(false);
        return;
      }

      // Do not trust localStorage. The token may be expired, revoked, or
      // signed with a key this backend no longer accepts, and the stored role
      // may be stale. Ask the server, which reads the role from the database.
      try {
        const res = await fetch(`${API_URL}/auth/me`, {
          headers: { Authorization: `Bearer ${savedToken}` },
        });

        if (cancelled) return;

        if (!res.ok) {
          // 401 means the session is gone; clear it rather than leaving the
          // user apparently signed in until their next request fails.
          localStorage.removeItem("novaflow_token");
          localStorage.removeItem("novaflow_user");
          setToken(null);
          setUser(null);
          if (window.location.pathname !== "/login") {
            router.push("/login");
          }
          return;
        }

        const verified = await res.json();
        if (cancelled) return;

        const userData: User = {
          username: verified.username,
          role: verified.role,
        };
        setToken(savedToken);
        setUser(userData);
        // Keep the cached copy aligned with the server's view of the role.
        localStorage.setItem("novaflow_user", JSON.stringify(userData));
      } catch {
        // Network failure: keep the cached session rather than signing the
        // user out because the backend was briefly unreachable. Any protected
        // request will still be rejected server-side if the token is invalid.
        if (cancelled) return;
        setToken(savedToken);
        try {
          setUser(JSON.parse(savedUser));
        } catch {
          setUser(null);
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    void restoreSession();
    return () => {
      cancelled = true;
    };
  }, [router, API_URL]);

  const login = (newToken: string, role: string, username: string) => {
    const userData: User = { username, role: role as "client" | "agent" };
    setToken(newToken);
    setUser(userData);
    localStorage.setItem("novaflow_token", newToken);
    localStorage.setItem("novaflow_user", JSON.stringify(userData));
    router.push("/");
  };

  const logout = () => {
    setToken(null);
    setUser(null);
    localStorage.removeItem("novaflow_token");
    localStorage.removeItem("novaflow_user");
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
