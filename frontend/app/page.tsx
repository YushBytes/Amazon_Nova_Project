"use client";

import React, { useState } from "react";
import { LogOut, LayoutDashboard, Users, History, Settings, Bell, Command, UserCircle } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useAuth } from "./context/AuthContext";
import { cn } from "./lib/utils";

import HUDView from "./components/HUDView";
import AgentsView from "./components/AgentsView";
import LogsView from "./components/LogsView";
import ClientPortal from "./components/ClientPortal";
import ProjectDetailView from "./components/ProjectDetailView";

type ViewType = "HUD" | "AGENTS" | "LOGS" | "PROFILE" | "CLIENT";

export default function UnifiedCommandCenter() {
  const [mounted, setMounted] = useState(false);
  const { user, logout, isLoading: authLoading } = useAuth();
  const [activeView, setActiveView] = useState<ViewType>("HUD");
  const [selectedSubmission, setSelectedSubmission] = useState<any>(null);

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
      <div className="h-screen bg-background flex items-center justify-center">
        <motion.div 
          animate={{ rotate: 360 }}
          transition={{ duration: 1, repeat: Infinity, ease: "linear" }}
          className="w-10 h-10 border-4 border-primary border-t-transparent rounded-full"
        />
      </div>
    );
  }

  if (!user) return null; // Logic in AuthContext handles redirect to /login


  return (
    <div className="flex h-screen bg-background overflow-hidden text-foreground">
      {/* Background Glows */}
      <div className="absolute top-0 left-0 w-full h-full pointer-events-none overflow-hidden">
        <motion.div 
          animate={{ scale: [1, 1.1, 1], opacity: [0.1, 0.2, 0.1] }}
          transition={{ duration: 10, repeat: Infinity }}
          className="absolute top-[-20%] left-[-10%] w-[60%] h-[60%] bg-primary/20 blur-[150px] rounded-full" 
        />
        <motion.div 
          animate={{ scale: [1, 1.2, 1], opacity: [0.05, 0.1, 0.05] }}
          transition={{ duration: 12, repeat: Infinity, delay: 2 }}
          className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-accent/20 blur-[150px] rounded-full" 
        />
      </div>

      {/* Sidebar Navigation */}
      <aside className="w-20 lg:w-72 border-r border-white/5 bg-[#0a0516]/50 backdrop-blur-2xl z-50 flex flex-col transition-all duration-500">
        <div className="p-8 flex items-center space-x-4">
          <div className="w-11 h-11 bg-primary/10 border border-primary/20 rounded-xl flex items-center justify-center shadow-sm shrink-0">
            <Command className="text-primary w-6 h-6" />
          </div>
          <div className="hidden lg:block overflow-hidden whitespace-nowrap">
            <h1 className="text-xl font-bold tracking-tight text-white">NovaFlow</h1>
            <p className="text-[10px] uppercase font-bold text-primary/60 tracking-widest">v2.4 Control</p>
          </div>
        </div>

        <nav className="flex-1 px-4 mt-6 space-y-1 w-full">
          {[
            { label: "Dashboard", icon: LayoutDashboard, view: "HUD", roles: ["agent"] },
            { label: "Client Portal", icon: LayoutDashboard, view: "CLIENT", roles: ["agent", "client"] },
            { label: "Agents", icon: Users, view: "AGENTS", roles: ["agent"] },
            { label: "Compliance Logs", icon: History, view: "LOGS", roles: ["agent"] },
            { label: "Settings", icon: Settings, view: "PROFILE", roles: ["agent", "client"] },
          ]
          .filter(item => item.roles.includes(user?.role || ""))
          .map((item, i) => (
            <button 
              key={i} 
              onClick={() => setActiveView(item.view as ViewType)}
              className={cn(
                "w-full flex items-center space-x-4 p-4 rounded-xl transition-all duration-200 group relative",
                activeView === item.view 
                  ? "bg-white/5 text-primary" 
                  : "text-white/40 hover:bg-white/[0.02] hover:text-white"
              )}
            >
              <item.icon className={cn("w-5 h-5 shrink-0 transition-transform group-hover:scale-110", activeView === item.view ? "text-primary" : "")} />
              <span className="hidden lg:block font-medium text-sm tracking-tight">{item.label}</span>
              {activeView === item.view && (
                <motion.div layoutId="nav-pill-desktop" className="absolute left-0 w-1 h-6 bg-primary rounded-r-full hidden lg:block" />
              )}
            </button>
          ))}
        </nav>

        <div className="p-8 w-full mt-auto border-t border-white/5">
          <div className="flex flex-col space-y-3">
            <button 
              onClick={() => setActiveView("PROFILE")}
              className={cn(
                "w-full flex items-center space-x-4 p-3 rounded-xl transition-all group",
                activeView === "PROFILE" ? "bg-white/5 text-white" : "text-white/40 hover:bg-white/[0.02] hover:text-white"
              )}
            >
              <UserCircle className="w-5 h-5 shrink-0 group-hover:text-primary transition-colors" />
              <div className="hidden lg:block text-left overflow-hidden">
                 <p className="text-xs font-semibold truncate text-white leading-tight">{user.username}</p>
                 <p className="text-[9px] uppercase font-bold text-primary/40 tracking-[0.2em] mt-0.5">Secure Node</p>
              </div>
            </button>

            <button 
              onClick={logout}
              className="w-full flex items-center space-x-4 p-3 rounded-xl text-critical/40 hover:bg-critical/5 hover:text-critical transition-all group"
            >
              <LogOut className="w-5 h-5 shrink-0 transition-transform group-hover:-translate-x-1" />
              <span className="hidden lg:block font-bold text-[10px] uppercase tracking-widest">Sign Out</span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-screen overflow-hidden relative z-10 bg-[#05010d]">
        <header className="h-24 border-b border-white/5 bg-background/40 backdrop-blur-xl flex items-center justify-between px-10 shrink-0">
          <div className="flex items-center space-x-6">
             <div className="flex flex-col">
               <h2 className="text-xs font-bold uppercase tracking-[0.4em] text-white/50 leading-none">{activeView}</h2>
               <div className="h-px w-8 bg-primary mt-3" />
             </div>
          </div>
          <div className="flex items-center space-x-8">
            <div className="hidden md:flex items-center space-x-3 bg-white/[0.02] border border-white/5 px-4 py-2 rounded-xl">
              <span className="w-1.5 h-1.5 rounded-full bg-success shadow-[0_0_8px_rgba(16,185,129,0.5)] animate-pulse" />
              <span className="text-[9px] font-bold tracking-widest uppercase text-white/40">Sync: US-EAST-1</span>
            </div>
            <button className="p-3 bg-white/[0.02] border border-white/5 rounded-xl hover:bg-white/5 transition-all relative group">
              <Bell className="w-5 h-5 text-white/40 group-hover:text-white transition-colors" />
              <span className="absolute top-2.5 right-2.5 w-1.5 h-1.5 bg-critical rounded-full" />
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-10 custom-scrollbar">
          <div className="max-w-7xl mx-auto">
            <AnimatePresence mode="wait">
              {activeView === "HUD" && <HUDView key="hud" />}
              {activeView === "CLIENT" && (
                selectedSubmission ? (
                  <ProjectDetailView 
                    key="detail" 
                    submission={selectedSubmission} 
                    onBack={() => setSelectedSubmission(null)} 
                  />
                ) : (
                  <ClientPortal 
                    key="client" 
                    onSelect={(sub) => setSelectedSubmission(sub)} 
                  />
                )
              )}
              {activeView === "AGENTS" && <AgentsView key="agents" />}
              {activeView === "LOGS" && <LogsView key="logs" />}
              {activeView === "PROFILE" && (
                 <motion.div 
                   initial={{ opacity: 0, scale: 0.95 }}
                   animate={{ opacity: 1, scale: 1 }}
                   className="grid grid-cols-1 lg:grid-cols-2 gap-8"
                 >
                    <div className="glass-card p-12 flex flex-col items-center justify-center text-center space-y-6">
                      <div className="w-32 h-32 rounded-full bg-primary/10 border-4 border-primary/30 p-2 shadow-glow">
                         <img src="https://api.dicebear.com/7.x/avataaars/svg?seed=Niru" className="w-full h-full rounded-full" alt="Avatar" />
                      </div>
                      <div>
                         <h2 className="text-3xl font-black text-white">Commander Niru</h2>
                         <p className="text-xs uppercase font-extrabold text-primary tracking-[0.4em] mt-2">Level 10 Overseer</p>
                      </div>
                      <div className="flex space-x-4 w-full pt-4">
                        <button className="flex-1 bg-primary text-white py-4 rounded-2xl font-black text-xs uppercase tracking-widest hover:scale-105 transition-transform shadow-glow">Identity Protocol</button>
                        <button className="flex-1 bg-white/5 border border-white/10 text-white/60 py-4 rounded-2xl font-black text-xs uppercase tracking-widest hover:bg-white/10 transition-all">Revoke Access</button>
                      </div>
                    </div>
                    <div className="glass-card p-8 space-y-6">
                       <h3 className="text-xs font-black uppercase tracking-[0.3em] text-white/40">Security Clearance</h3>
                       <div className="space-y-4">
                          {[
                            { label: "Bio-Sync", level: "98%", status: "VERIFIED" },
                            { label: "Grid Isolation", level: "ENABLED", status: "SAFE" },
                            { label: "Neural Bridge", level: "ACTIVE", status: "ACTIVE" }
                          ].map((item, i) => (
                            <div key={i} className="flex items-center justify-between p-4 bg-white/[0.02] border border-white/5 rounded-2xl">
                               <span className="text-xs font-bold text-white/80">{item.label}</span>
                               <span className="text-[10px] font-black text-primary">{item.level} // {item.status}</span>
                            </div>
                          ))}
                       </div>
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
