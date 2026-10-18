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
    const savedToken = localStorage.getItem("novaflow_token");
    const savedUser = localStorage.getItem("novaflow_user");
    
    if (savedToken && savedUser) {
      setToken(savedToken);
      setUser(JSON.parse(savedUser));
    } else {
      // Redirect to login if not authenticated and not already on login page
      if (window.location.pathname !== "/login") {
        router.push("/login");
      }
    }
    setIsLoading(false);
  }, [router]);

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
