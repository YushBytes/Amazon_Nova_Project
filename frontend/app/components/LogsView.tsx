"use client";

import React, { useState } from "react";
import { 
  Download, 
  Eye, 
  MoreVertical, 
  ChevronDown, 
  ShieldAlert
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const incidents = [
  { id: 1, title: "Unauthorized Access Attempt", description: "Facial recognition mismatch. Alert triggered at primary vault entry.", status: "CRITICAL", badge: "LIVE AI", location: "Server Room A", time: "14:22:05 Today", image: "https://images.unsplash.com/photo-1558494949-ef0109159d5e?q=80&w=2000&auto=format&fit=crop" },
  { id: 2, title: "Tailgating Detected", description: "Secondary person entered without badge swipe behind Authorized User #442.", status: "MEDIUM", badge: "MOTION", location: "West Wing Elevator", time: "13:45:12 Today", image: "https://images.unsplash.com/photo-1577705998148-ebbd7a31ec75?q=80&w=2000&auto=format&fit=crop" },
  { id: 3, title: "After-hours Activity", description: "Maintenance crew scheduled cleaning. Identification verified by onsite security.", status: "RESOLVED", badge: null, location: "Main Lobby", time: "02:10:00 Today", image: "https://images.unsplash.com/photo-1497366216548-37526070297c?q=80&w=2000&auto=format&fit=crop" }
];

export default function LogsView() {
  const [activeTab, setActiveTab] = useState("All Events");

  return (
    <motion.div 
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-12"
    >
      {/* Header Info */}
      <div className="flex items-center justify-between border-b border-white/5 pb-10">
        <div className="flex items-center space-x-4">
          <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
            <ShieldAlert className="text-primary w-6 h-6" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">Security Intelligence</h1>
            <p className="text-[11px] font-semibold text-primary/60 uppercase tracking-[0.3em] mt-1">Live Anomaly Detection</p>
          </div>
        </div>
        <button className="bg-white/[0.02] border border-white/5 px-6 py-3 rounded-xl text-[10px] font-bold text-white/40 uppercase tracking-widest hover:text-white transition-all">
          Generate Audit
        </button>
      </div>

      {/* Tabs */}
      <div className="flex flex-wrap items-center gap-x-12 gap-y-4 border-b border-white/5">
        {["All Events", "Severe", "Resolved"].map(tab => (
          <button 
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              "pb-6 text-xs font-bold transition-all relative uppercase tracking-widest whitespace-nowrap",
              activeTab === tab ? "text-primary" : "text-white/20 hover:text-white/40"
            )}
          >
            {tab}
            {activeTab === tab && (
              <motion.div layoutId="tab-underline-logs" className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary shadow-glow" />
            )}
          </button>
        ))}
      </div>

      {/* Incident Grid */}
      <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10">
        <AnimatePresence mode="popLayout">
          {incidents.map((incident) => (
            <motion.div 
              key={incident.id}
              whileHover={{ y: -5 }}
              className="bg-white/[0.02] border border-white/5 rounded-[2rem] overflow-hidden flex flex-col h-full hover:border-primary/20 transition-all"
            >
              <div className="aspect-video relative overflow-hidden bg-black/40">
                <img src={incident.image} alt={incident.title} className="w-full h-full object-cover opacity-50 group-hover:scale-105 transition-transform" />
                <div className="absolute inset-0 bg-gradient-to-t from-[#05010d] via-transparent to-transparent" />
                {incident.badge && (
                  <span className={cn(
                    "absolute top-6 left-6 px-3 py-1 text-[9px] font-bold rounded-lg tracking-widest uppercase border border-white/10 backdrop-blur-xl",
                    incident.badge === 'LIVE AI' ? "bg-critical/80 text-white" : "bg-primary/80 text-white"
                  )}>
                    {incident.badge}
                  </span>
                )}
              </div>
              
              <div className="p-10 space-y-6 flex flex-col flex-1">
                <div className="space-y-2">
                  <div className="flex justify-between items-center">
                    <span className={cn(
                      "text-[9px] font-bold uppercase tracking-widest",
                      incident.status === 'CRITICAL' ? "text-critical" : "text-primary"
                    )}>
                      {incident.status}
                    </span>
                    <span className="text-[9px] font-bold text-white/10 uppercase">{incident.time}</span>
                  </div>
                  <h3 className="font-bold text-lg text-white leading-snug">{incident.title}</h3>
                </div>
                
                <p className="text-xs text-white/30 leading-relaxed font-medium flex-1">{incident.description}</p>
                
                <div className="flex items-center space-x-3 pt-6 border-t border-white/5">
                  <button className="flex-1 bg-white/5 border border-white/5 hover:bg-primary text-white text-[10px] font-bold px-4 py-3 rounded-xl uppercase tracking-widest transition-all">
                    View Frame
                  </button>
                  <button className="p-3 bg-white/5 border border-white/5 rounded-xl text-white/20 hover:text-white transition-all">
                    <MoreVertical className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </section>
    </motion.div>
  );
}

