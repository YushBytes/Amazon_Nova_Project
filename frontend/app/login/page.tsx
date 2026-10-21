"use client";

/**
 * Sign in and registration.
 *
 * Phase 7 changes:
 *   - the two inputs now have real <label for> associations, so a screen
 *     reader announces them and clicking the label focuses the field; the
 *     error is `role="alert"` so a failure is announced rather than only
 *     appearing;
 *   - the role selector is gone. It set local state that was never sent and
 *     never consulted, so choosing "Agent" changed nothing while implying
 *     the client could pick its own privilege. The backend derives the role
 *     from stored account data, and public signup always creates a Client;
 *   - the footer claiming "Security Hash: SHA-256 (AUTO)" and "AES-256
 *     Encryption Active" is gone. Passwords are hashed with Argon2id, the
 *     SHA-256 claim was a leftover from the superseded scheme, and nothing
 *     in this application performs AES encryption of anything.
 *
 * The 429 handling is unchanged and still load-bearing: /auth/login and
 * /auth/signup are rate limited, so a throttled response must not be shown
 * as a rejected password - that would push the user into retrying and burning
 * more of their budget.
 */

import React, { useState } from "react";
import { motion } from "framer-motion";
import { ArrowRight, Command, Loader2 } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import { cn } from "../lib/utils";
import { ApiError, login as loginRequest, signup as signupRequest } from "../lib/api";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isLogin, setIsLogin] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const { login } = useAuth();

  const handleAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError("");

    try {
      // No role is sent. Registration always creates a least-privileged
      // account server-side; the agent role is granted by an operator.
      const data = isLogin
        ? await loginRequest(username, password)
        : await signupRequest(username, password);
      // Only after the request succeeded. A success message or a redirect
      // before this point would claim an outcome that had not happened.
      login(data.access_token, data.role, username);
      return;
    } catch (err) {
      if (err instanceof ApiError && err.isThrottled) {
        // The credential endpoints are rate limited. Surface the wait time
        // from Retry-After so the user knows this is temporary rather than a
        // rejected password.
        const retryAfter = err.retryAfterSeconds;
        const wait =
          retryAfter !== null
            ? `${retryAfter} second${retryAfter === 1 ? "" : "s"}`
            : "a moment";
        setError(`Too many attempts. Please wait ${wait} and try again.`);
      } else if (err instanceof ApiError) {
        // The backend's own `detail`, which is written for a reader and
        // never carries exception text.
        setError(err.message || "Authentication failed");
      } else if (err instanceof Error && err.message) {
        // Network or unexpected failure. Never surface a raw object.
        setError(err.message);
      } else {
        setError("Could not reach the server. Please try again.");
      }
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background p-6 text-foreground">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 h-full w-full"
      >
        <motion.div
          animate={{ scale: [1, 1.2, 1], opacity: [0.1, 0.2, 0.1] }}
          transition={{ duration: 8, repeat: Infinity }}
          className="absolute left-[-10%] top-[-10%] h-[50%] w-[50%] rounded-full bg-primary/20 blur-[120px]"
        />
        <motion.div
          animate={{ scale: [1, 1.3, 1], opacity: [0.05, 0.15, 0.05] }}
          transition={{ duration: 10, repeat: Infinity, delay: 1 }}
          className="absolute bottom-[-10%] right-[-10%] h-[60%] w-[60%] rounded-full bg-accent/20 blur-[150px]"
        />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="relative z-10 w-full max-w-xl"
      >
        <div className="glass-card space-y-10 border-white/5 bg-white/[0.02] p-10 shadow-2xl backdrop-blur-3xl md:p-16">
          <div className="flex flex-col items-center space-y-4 text-center">
            <div className="mb-2 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary shadow-glow">
              <Command className="h-10 w-10 text-white" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-4xl font-black uppercase italic tracking-tighter text-white">
                NovaFlow
              </h1>
              <p className="mt-1 text-[10px] font-bold uppercase tracking-[0.4em] text-primary">
                {isLogin ? "Sign in" : "Create an account"}
              </p>
            </div>
          </div>

          {!isLogin && (
            <p className="text-[10px] font-semibold uppercase tracking-widest text-white/30">
              New accounts are created as Client. Agent access is granted by an
              administrator.
            </p>
          )}

          <form onSubmit={handleAuth} className="space-y-6">
            <div className="space-y-4">
              <div className="space-y-2">
                <label
                  htmlFor="username"
                  className="ml-4 block text-[10px] font-black uppercase tracking-widest text-white/40"
                >
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className={cn(
                    "w-full rounded-2xl border border-white/10 bg-white/[0.03] p-5 font-mono",
                    "text-sm tracking-widest transition-all placeholder:text-white/20",
                    "focus:border-primary/50 focus:outline-2 focus:outline-offset-2 focus:outline-primary"
                  )}
                  required
                />
              </div>
              <div className="space-y-2">
                <label
                  htmlFor="password"
                  className="ml-4 block text-[10px] font-black uppercase tracking-widest text-white/40"
                >
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete={isLogin ? "current-password" : "new-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••••••"
                  className={cn(
                    "w-full rounded-2xl border border-white/10 bg-white/[0.03] p-5 font-mono",
                    "text-sm tracking-widest transition-all placeholder:text-white/10",
                    "focus:border-primary/50 focus:outline-2 focus:outline-offset-2 focus:outline-primary"
                  )}
                  required
                />
              </div>
            </div>

            {error && (
              <motion.p
                role="alert"
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                className="rounded-xl border border-critical/30 bg-critical/10 p-4 text-center text-[11px] font-bold tracking-wide text-critical"
              >
                {error}
              </motion.p>
            )}

            <button
              disabled={isLoading}
              type="submit"
              className={cn(
                "group flex w-full items-center justify-center space-x-3 rounded-3xl bg-primary p-6",
                "text-xs font-black uppercase tracking-[0.3em] text-white shadow-glow transition-all",
                "hover:bg-accent active:scale-[0.98] disabled:opacity-50",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
              )}
            >
              {isLoading ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
                  <span>{isLogin ? "Signing in" : "Creating account"}</span>
                </>
              ) : (
                <>
                  <span>{isLogin ? "Sign in" : "Create account"}</span>
                  <ArrowRight
                    className="h-5 w-5 transition-transform group-hover:translate-x-1"
                    aria-hidden="true"
                  />
                </>
              )}
            </button>
          </form>

          <div className="pt-2 text-center">
            <button
              type="button"
              onClick={() => {
                setIsLogin(!isLogin);
                setError("");
              }}
              className={cn(
                "text-[10px] font-black uppercase italic tracking-widest text-white/30 transition-colors",
                "hover:text-primary focus-visible:outline-2 focus-visible:outline-offset-2",
                "focus-visible:outline-primary"
              )}
            >
              {isLogin ? "> PROTOCOL_SIGNUP: NEW_USER" : "> PROTOCOL_LOGIN: EXISTING_USER"}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}
