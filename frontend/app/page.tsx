"use client";

/**
 * The application shell: navigation, and whichever view is active.
 *
 * Phase 7 removed the fabricated account screen. The "Settings" view showed a
 * randomly generated avatar for "Commander Niru, Level 10 Overseer" — a name
 * unrelated to the signed-in user — plus a "Security Clearance" panel
 * reporting "Bio-Sync 98% VERIFIED", "Grid Isolation ENABLED" and "Neural
 * Bridge ACTIVE", and buttons labelled "Identity Protocol" and "Revoke
 * Access" that were wired to nothing. None of it came from any endpoint.
 * It now shows the real signed-in identity from GET /auth/me and states
 * plainly which account operations do not exist.
 *
 * Also removed from the header: a pulsing green "Sync: US-EAST-1" indicator
 * (nothing reports a region or a sync state) and a notification bell with an
 * unread dot for a notification system that does not exist.
 *
 * Navigation visibility follows the user's role, but that is presentation
 * only: hiding a nav entry is not authorization. Every route the views call
 * is authorized server-side, and ownership is re-checked per request.
 */

import React, { useState } from "react";
import {
  Command,
  History,
  LayoutDashboard,
  LogOut,
  ServerCog,
  Settings,
  UserCircle,
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useAuth } from "./context/AuthContext";
import { cn } from "./lib/utils";

import HUDView from "./components/HUDView";
import AgentsView from "./components/AgentsView";
import LogsView from "./components/LogsView";
import ClientPortal from "./components/ClientPortal";
import ProjectDetailView, { type Submission } from "./components/ProjectDetailView";
import { UnavailablePanel } from "./components/ui/Feedback";

type ViewType = "HUD" | "AGENTS" | "LOGS" | "PROFILE" | "CLIENT";

const NAV_ITEMS: {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  view: ViewType;
  roles: string[];
}[] = [
  { label: "Dashboard", icon: LayoutDashboard, view: "HUD", roles: ["agent"] },
  { label: "Your Videos", icon: LayoutDashboard, view: "CLIENT", roles: ["agent", "client"] },
  { label: "Infrastructure", icon: ServerCog, view: "AGENTS", roles: ["agent"] },
  { label: "Compliance Log", icon: History, view: "LOGS", roles: ["agent", "client"] },
  { label: "Account", icon: Settings, view: "PROFILE", roles: ["agent", "client"] },
];

const VIEW_TITLES: Record<ViewType, string> = {
  HUD: "Dashboard",
  CLIENT: "Your Videos",
  AGENTS: "Infrastructure",
  LOGS: "Compliance Log",
  PROFILE: "Account",
};

export default function UnifiedCommandCenter() {
  const [mounted, setMounted] = useState(false);
  const { user, logout, isLoading: authLoading } = useAuth();
  const [activeView, setActiveView] = useState<ViewType>("HUD");
  const [selectedSubmission, setSelectedSubmission] = useState<Submission | null>(null);

  React.useEffect(() => {
    setMounted(true);
  }, []);

  React.useEffect(() => {
    if (user?.role === "client") {
      setActiveView("CLIENT");
    }
  }, [user]);

  const isLoading = authLoading || !mounted;

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div role="status" aria-live="polite" className="flex flex-col items-center gap-4">
          <motion.div
            animate={{ rotate: 360 }}
            transition={{ duration: 1, repeat: Infinity, ease: "linear" }}
            className="h-10 w-10 rounded-full border-4 border-primary border-t-transparent"
            aria-hidden="true"
          />
          <span className="text-[10px] font-bold uppercase tracking-widest text-white/40">
            Verifying your session
          </span>
        </div>
      </div>
    );
  }

  // AuthContext redirects to /login; rendering nothing here is what keeps a
  // signed-out or expired session from briefly showing private data.
  if (!user) return null;

  const visibleNav = NAV_ITEMS.filter((item) => item.roles.includes(user.role));

  const selectSubmission = (submission: Submission) => {
    setSelectedSubmission(submission);
  };

  return (
    <div className="flex h-screen overflow-hidden bg-background text-foreground">
      {/* Background glows, decorative only. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 h-full w-full overflow-hidden"
      >
        <motion.div
          animate={{ scale: [1, 1.1, 1], opacity: [0.1, 0.2, 0.1] }}
          transition={{ duration: 10, repeat: Infinity }}
          className="absolute left-[-10%] top-[-20%] h-[60%] w-[60%] rounded-full bg-primary/20 blur-[150px]"
        />
        <motion.div
          animate={{ scale: [1, 1.2, 1], opacity: [0.05, 0.1, 0.05] }}
          transition={{ duration: 12, repeat: Infinity, delay: 2 }}
          className="absolute bottom-[-10%] right-[-10%] h-[50%] w-[50%] rounded-full bg-accent/20 blur-[150px]"
        />
      </div>

      <aside className="z-50 flex w-20 flex-col border-r border-white/5 bg-[#0a0516]/50 backdrop-blur-2xl transition-all duration-500 lg:w-72">
        <div className="flex items-center space-x-4 p-8">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 shadow-sm">
            <Command className="h-6 w-6 text-primary" aria-hidden="true" />
          </div>
          <div className="hidden overflow-hidden whitespace-nowrap lg:block">
            <h1 className="text-xl font-bold tracking-tight text-white">NovaFlow</h1>
            <p className="text-[10px] font-bold uppercase tracking-widest text-primary/60">
              Brand Guardian
            </p>
          </div>
        </div>

        <nav aria-label="Main" className="mt-6 w-full flex-1 space-y-1 px-4">
          {visibleNav.map((item) => {
            const isActive = activeView === item.view;
            return (
              <button
                key={item.view}
                type="button"
                // An explicit name, because the visible label is hidden at
                // narrow widths where the icon is the only visual affordance.
                aria-label={item.label}
                aria-current={isActive ? "page" : undefined}
                onClick={() => {
                  setActiveView(item.view);
                  // Leaving a detail view clears its selection, so returning
                  // to a list does not reopen the last item.
                  if (item.view !== "CLIENT" && item.view !== "LOGS") {
                    setSelectedSubmission(null);
                  }
                }}
                className={cn(
                  "group relative flex w-full items-center space-x-4 rounded-xl p-4 transition-all duration-200",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
                  isActive
                    ? "bg-white/5 text-primary"
                    : "text-white/40 hover:bg-white/[0.02] hover:text-white"
                )}
              >
                <item.icon
                  className={cn(
                    "h-5 w-5 shrink-0 transition-transform group-hover:scale-110",
                    isActive ? "text-primary" : ""
                  )}
                  aria-hidden="true"
                />
                <span className="hidden text-sm font-medium tracking-tight lg:block">
                  {item.label}
                </span>
                {isActive && (
                  <motion.div
                    layoutId="nav-pill-desktop"
                    className="absolute left-0 hidden h-6 w-1 rounded-r-full bg-primary lg:block"
                  />
                )}
              </button>
            );
          })}
        </nav>

        <div className="mt-auto w-full border-t border-white/5 p-8">
          <div className="flex flex-col space-y-3">
            <button
              type="button"
              aria-label={`Account: ${user.username}, role ${user.role}`}
              onClick={() => setActiveView("PROFILE")}
              className={cn(
                "group flex w-full items-center space-x-4 rounded-xl p-3 transition-all",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
                activeView === "PROFILE"
                  ? "bg-white/5 text-white"
                  : "text-white/40 hover:bg-white/[0.02] hover:text-white"
              )}
            >
              <UserCircle
                className="h-5 w-5 shrink-0 transition-colors group-hover:text-primary"
                aria-hidden="true"
              />
              <span className="hidden overflow-hidden text-left lg:block">
                <span className="block truncate text-xs font-semibold leading-tight text-white">
                  {user.username}
                </span>
                <span className="mt-0.5 block text-[9px] font-bold uppercase tracking-[0.2em] text-primary/50">
                  {user.role}
                </span>
              </span>
            </button>

            <button
              type="button"
              aria-label="Sign out"
              onClick={logout}
              className={cn(
                "group flex w-full items-center space-x-4 rounded-xl p-3 text-critical/50 transition-all",
                "hover:bg-critical/5 hover:text-critical",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-critical"
              )}
            >
              <LogOut
                className="h-5 w-5 shrink-0 transition-transform group-hover:-translate-x-1"
                aria-hidden="true"
              />
              <span className="hidden text-[10px] font-bold uppercase tracking-widest lg:block">
                Sign Out
              </span>
            </button>
          </div>
        </div>
      </aside>

      <div className="relative z-10 flex h-screen flex-1 flex-col overflow-hidden bg-[#05010d]">
        <header className="flex h-24 shrink-0 items-center justify-between border-b border-white/5 bg-background/40 px-10 backdrop-blur-xl">
          <div className="flex flex-col">
            <h2 className="text-xs font-bold uppercase leading-none tracking-[0.4em] text-white/50">
              {VIEW_TITLES[activeView]}
            </h2>
            <div className="mt-3 h-px w-8 bg-primary" aria-hidden="true" />
          </div>
          {/* Previously a fake "Sync: US-EAST-1" status light and a
              notification bell with an unread badge. Neither had a data
              source, and a notification system does not exist. */}
        </header>

        <main className="custom-scrollbar flex-1 overflow-y-auto p-6 md:p-10">
          <div className="mx-auto max-w-7xl">
            <AnimatePresence mode="wait">
              {activeView === "HUD" && <HUDView key="hud" />}

              {activeView === "CLIENT" &&
                (selectedSubmission ? (
                  <ProjectDetailView
                    key="detail"
                    submission={selectedSubmission}
                    onBack={() => setSelectedSubmission(null)}
                  />
                ) : (
                  <ClientPortal key="client" onSelect={selectSubmission} />
                ))}

              {activeView === "LOGS" &&
                (selectedSubmission ? (
                  <ProjectDetailView
                    key="log-detail"
                    submission={selectedSubmission}
                    onBack={() => setSelectedSubmission(null)}
                  />
                ) : (
                  <LogsView key="logs" onSelect={selectSubmission} />
                ))}

              {activeView === "AGENTS" && <AgentsView key="agents" />}

              {activeView === "PROFILE" && (
                <motion.div
                  key="profile"
                  initial={{ opacity: 0, scale: 0.98 }}
                  animate={{ opacity: 1, scale: 1 }}
                  className="grid grid-cols-1 gap-8 lg:grid-cols-2"
                >
                  <div className="glass-card space-y-8 p-10">
                    <h2 className="text-xs font-black uppercase tracking-[0.3em] text-white/40">
                      Signed in as
                    </h2>
                    <dl className="space-y-6">
                      <div>
                        <dt className="text-[9px] font-black uppercase tracking-widest text-white/25">
                          Username
                        </dt>
                        <dd className="mt-1 break-all text-2xl font-bold text-white">
                          {user.username}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-[9px] font-black uppercase tracking-widest text-white/25">
                          Role
                        </dt>
                        <dd className="mt-1 text-sm font-bold uppercase tracking-widest text-primary">
                          {user.role}
                        </dd>
                      </div>
                    </dl>
                    <p className="text-[11px] leading-relaxed text-white/40">
                      Your role is read from your stored account by the server
                      on every request. It cannot be changed from this
                      interface: the agent role is granted only by an operator.
                    </p>
                    <button
                      type="button"
                      onClick={logout}
                      className={cn(
                        "w-full rounded-2xl border border-critical/30 bg-critical/10 py-4",
                        "text-xs font-black uppercase tracking-widest text-critical transition-colors",
                        "hover:bg-critical/20 focus-visible:outline-2 focus-visible:outline-offset-2",
                        "focus-visible:outline-critical"
                      )}
                    >
                      Sign out of this browser
                    </button>
                  </div>

                  <div className="space-y-8">
                    <UnavailablePanel
                      title="Account management"
                      explanation="There is no endpoint for changing a password, updating a profile, managing sessions, or deleting an account. Signing out clears this browser's stored token; it does not revoke it server-side, and the token stays valid until it expires."
                    />
                    <UnavailablePanel
                      title="Brand rule configuration"
                      explanation="Audits run against one fixed prompt. Per-client brand policies, custom rule sets and legal or regulatory evaluation are not implemented."
                    />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </main>
      </div>
    </div>
  );
}
