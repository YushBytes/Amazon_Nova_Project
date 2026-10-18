"use client";

import React, { useState } from "react";
import { 
  Search, 
  Download, 
  Eye, 
  MoreVertical, 
  ChevronDown, 
  ShieldAlert, 
  LayoutDashboard, 
  Users, 
  History, 
  UserCircle 
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import Link from "next/link";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const incidents = [
  {
    id: 1,
    title: "Unauthorized Access Attempt",
    description: "Facial recognition mismatch. Alert triggered at primary vault entry.",
    status: "CRITICAL",
    badge: "LIVE AI",
    location: "Server Room A",
    time: "14:22:05 Today",
    image: "https://images.unsplash.com/photo-1558494949-ef0109159d5e?q=80&w=2000&auto=format&fit=crop"
  },
  {
    id: 2,
    title: "Tailgating Detected",
    description: "Secondary person entered without badge swipe behind Authorized User #442.",
    status: "MEDIUM",
    badge: "MOTION",
    location: "West Wing Elevator",
    time: "13:45:12 Today",
    image: "https://images.unsplash.com/photo-1577705998148-ebbd7a31ec75?q=80&w=2000&auto=format&fit=crop"
  },
  {
    id: 3,
    title: "After-hours Activity",
    description: "Maintenance crew scheduled cleaning. Identification verified by onsite security.",
    status: "RESOLVED",
    badge: null,
    location: "Main Lobby",
    time: "02:10:00 Today",
    image: "https://images.unsplash.com/photo-1497366216548-37526070297c?q=80&w=2000&auto=format&fit=crop"
  }
];

export default function LogsPage() {
  const [activeTab, setActiveTab] = useState("All Flagged");

  return (
    <div className="flex flex-col min-h-screen p-6 space-y-8 bg-background max-w-md mx-auto relative">
      {/* Header */}
      <header className="flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 bg-primary/20 rounded-xl flex items-center justify-center border border-primary/30">
            <ShieldAlert className="text-primary w-6 h-6" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Violation Log</h1>
            <p className="text-[10px] uppercase font-bold text-primary tracking-widest">v2.4.0</p>
          </div>
        </div>
        <button className="flex items-center space-x-2 bg-primary text-white px-4 py-2.5 rounded-xl text-xs font-bold shadow-glow hover:scale-105 transition-transform">
          <Download className="w-4 h-4" />
          <span>Export Report</span>
        </button>
      </header>

      {/* Tabs */}
      <div className="flex space-x-6 border-b border-white/5">
        {["All Flagged", "High Risk", "Resolved"].map(tab => (
          <button 
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              "pb-3 text-sm font-bold transition-all relative",
              activeTab === tab ? "text-primary" : "text-white/40"
            )}
          >
            {tab}
            {activeTab === tab && (
              <motion.div layoutId="tab-underline" className="absolute bottom-0 left-0 right-0 h-[2px] bg-primary shadow-glow" />
            )}
          </button>
        ))}
      </div>

      {/* Filters */}
      <div className="flex space-x-2 overflow-x-auto pb-2 scrollbar-hide">
        {[
          { label: "Last 24h", icon: ChevronDown },
          { label: "AI Detected", icon: ChevronDown },
          { label: "Camera 1", icon: ChevronDown },
        ].map((filter, i) => (
          <button key={i} className="flex items-center space-x-2 glass-card px-4 py-2 text-[11px] font-bold whitespace-nowrap border-white/10 hover:border-primary/50 transition-colors">
            <span>{filter.label}</span>
            <filter.icon className="w-3 h-3 opacity-40" />
          </button>
        ))}
      </div>

      {/* Incident Cards */}
      <section className="space-y-6 flex-1">
        <AnimatePresence mode="popLayout">
          {incidents.map((incident, i) => (
            <motion.div 
              key={incident.id}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.1 }}
              className="glass-card overflow-hidden border-white/5 hover:border-primary/20 transition-colors group"
            >
              <div className="aspect-video relative overflow-hidden">
                <img src={incident.image} alt={incident.title} className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-700" />
                <div className="absolute inset-0 bg-gradient-to-t from-surface via-transparent to-transparent opacity-60" />
                {incident.badge && (
                  <span className={cn(
                    "absolute top-3 left-3 px-2 py-1 text-[9px] font-extrabold rounded-sm tracking-tighter",
                    incident.badge === 'LIVE AI' ? "bg-critical text-white" : "bg-primary text-white"
                  )}>
                    {incident.badge}
                  </span>
                )}
                <div className="absolute top-3 right-3 w-4 h-4 bg-success rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                   <div className="w-2 h-2 rounded-full bg-white animate-pulse" />
                </div>
              </div>
              
              <div className="p-5 space-y-3">
                <div className="flex justify-between items-start">
                  <h3 className="font-bold text-base leading-tight pr-4">{incident.title}</h3>
                  <div className="flex items-center space-x-1.5 whitespace-nowrap">
                    <div className={cn(
                      "w-1.5 h-1.5 rounded-full",
                      incident.status === 'CRITICAL' ? "bg-critical" : 
                      incident.status === 'MEDIUM' ? "bg-accent" : "bg-success"
                    )} />
                    <span className={cn(
                      "text-[9px] font-black uppercase tracking-widest",
                      incident.status === 'CRITICAL' ? "text-critical" : 
                      incident.status === 'MEDIUM' ? "text-accent" : "text-success"
                    )}>
                      {incident.status}
                    </span>
                  </div>
                </div>
                
                <p className="text-xs text-white/50 leading-relaxed font-medium">
                  {incident.description}
                </p>
                
                <div className="flex items-center space-x-6 text-[10px] font-bold text-white/30 uppercase tracking-tight">
                  <div className="flex items-center space-x-1.5">
                    <span className="w-4 h-3 rounded-sm bg-white/5 flex items-center justify-center">🏢</span>
                    <span>{incident.location}</span>
                  </div>
                  <div className="flex items-center space-x-1.5">
                    <span className="w-4 h-3 rounded-sm bg-white/5 flex items-center justify-center">🕒</span>
                    <span>{incident.time}</span>
                  </div>
                </div>

                <div className="flex space-x-2 pt-2">
                  <button title="View Details" className="flex-1 bg-primary/20 border border-primary/30 hover:bg-primary transition-all rounded-xl py-3 flex items-center justify-center space-x-2 group/btn">
                    <Eye className="w-4 h-4 text-primary group-hover/btn:text-white" />
                  </button>
                  <button title="More Options" className="glass-card w-14 flex items-center justify-center hover:bg-white/10">
                    <MoreVertical className="w-4 h-4 opacity-40" />
                  </button>
                </div>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
        
        <button className="w-full py-6 flex items-center justify-center space-x-2 text-[10px] font-black uppercase tracking-[0.3em] text-primary hover:text-accent transition-colors group">
          <span>Load more incidents</span>
          <ChevronDown className="w-4 h-4 group-hover:translate-y-1 transition-transform" />
        </button>
      </section>

      {/* Bottom Navigation */}
      <nav className="fixed bottom-0 left-0 right-0 bg-background/80 backdrop-blur-2xl border-t border-white/5 p-4 z-50">
        <div className="flex justify-between items-center max-w-md mx-auto px-4">
          <Link href="/" className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <LayoutDashboard className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">HUD</span>
          </Link>
          <Link href="/agents" className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <Users className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Agents</span>
          </Link>
          <div className="flex flex-col items-center space-y-1 text-primary transition-all">
            <History className="w-6 h-6 drop-shadow-glow" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Logs</span>
            <motion.div layoutId="nav" className="w-1 h-1 bg-primary rounded-full" />
          </div>
          <button className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <UserCircle className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Profile</span>
          </button>
        </div>
      </nav>
    </div>
  );
}
