"use client";

import React from "react";
import { 
  Bot, 
  Cpu, 
  Terminal, 
  Zap,
  AlertTriangle
} from "lucide-react";
import { motion } from "framer-motion";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export default function AgentsView() {
  const stats = [
    { label: "Logic Load", value: "42%", delta: "-5%", color: "primary" },
    { label: "Vision Latency", value: "68ms", delta: "+12%", color: "accent" },
  ];

  const agents = [
    { name: "Core-Act-01", status: "ONLINE", type: "Browser Automation Controller", image: "https://images.unsplash.com/photo-1620641788421-7a1c342ea42e?q=80&w=2000&auto=format&fit=crop" },
    { name: "Nova-Vision-7", status: "SYNCING", type: "Multi-modal Analysis Node", image: "https://images.unsplash.com/photo-1633167606207-d840b5070fc2?q=80&w=2000&auto=format&fit=crop" },
  ];

  return (
    <motion.div 
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-12"
    >
      {/* Header Info */}
      <div className="flex items-center space-x-4 border-b border-white/5 pb-10">
        <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
          <Bot className="text-primary w-6 h-6" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">Neural Cluster</h1>
          <p className="text-[11px] font-semibold text-primary/60 uppercase tracking-[0.3em] mt-1">Status: Cluster Optimal</p>
        </div>
      </div>

      {/* System Stats Row */}
      <section className="grid grid-cols-1 lg:grid-cols-12 gap-10">
        <div className="lg:col-span-8 grid grid-cols-1 md:grid-cols-2 gap-10">
          {stats.map((stat, i) => (
            <div key={i} className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-6">
              <div className="flex justify-between items-center text-white/20 text-[10px] font-bold uppercase tracking-widest">
                <span>{stat.label}</span>
                <Cpu className="w-5 h-5 text-primary/40" />
              </div>
              <div className="flex items-baseline space-x-4">
                <span className="text-5xl font-bold text-white tracking-tighter">{stat.value}</span>
                <span className={cn(
                  "text-[10px] font-bold px-3 py-1 rounded-lg tracking-widest uppercase",
                  stat.delta.startsWith('+') ? "text-success bg-success/5" : "text-critical bg-critical/5"
                )}>
                  {stat.delta}
                </span>
              </div>
              <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                <motion.div
                   initial={{ width: 0 }}
                   animate={{ width: parseInt(stat.value) + "%" }}
                   transition={{ duration: 1.5, ease: "easeOut" }}
                   className={cn("h-full", stat.color === 'primary' ? "bg-primary" : "bg-accent")}
                />
              </div>
            </div>
          ))}
        </div>

        <div className="lg:col-span-4 bg-primary/[0.02] border border-primary/10 rounded-[2rem] p-10 flex flex-col justify-center space-y-8">
           <div className="space-y-6">
              <p className="text-[10px] font-bold text-primary/60 uppercase tracking-widest text-center">Protocol Stability</p>
              <div className="grid grid-cols-2 gap-6">
                 <div className="bg-white/[0.02] border border-white/5 rounded-2xl py-6 flex flex-col items-center justify-center">
                    <span className="text-xl font-bold text-white">99.9%</span>
                    <span className="text-[8px] font-semibold text-white/20 uppercase mt-1">Uptime</span>
                 </div>
                 <div className="bg-white/[0.02] border border-white/5 rounded-2xl py-6 flex flex-col items-center justify-center">
                    <span className="text-xl font-bold text-primary">2.1ms</span>
                    <span className="text-[8px] font-semibold text-white/20 uppercase mt-1">Sync</span>
                 </div>
              </div>
           </div>
           <button className="w-full h-14 bg-primary text-white rounded-2xl font-bold text-xs uppercase tracking-widest transition-all hover:translate-y-[-2px] shadow-lg flex items-center justify-center space-x-3">
              <Zap className="w-4 h-4" />
              <span>Expand Cluster</span>
           </button>
        </div>
      </section>

      {/* Agents List */}
      <section className="space-y-8">
        <div className="flex items-center justify-between border-b border-white/5 pb-4">
          <h2 className="text-[11px] font-bold uppercase tracking-[0.3em] text-white/20">Operational Units</h2>
          <span className="text-[10px] font-bold text-primary/40 tracking-widest">2 ACTIVE NODES</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-2 gap-10">
          {agents.map((agent, i) => (
            <motion.div
              key={i}
              whileHover={{ y: -5 }}
              className="bg-white/[0.02] border border-white/5 rounded-[2.5rem] overflow-hidden group relative"
            >
              <div className="aspect-[21/9] relative">
                <img src={agent.image} alt={agent.name} className="w-full h-full object-cover opacity-40 grayscale group-hover:grayscale-0 group-hover:scale-105 transition-all duration-1000" />
                <div className="absolute inset-0 bg-gradient-to-t from-[#05010d] via-[#05010d]/40 to-transparent" />
                <div className="absolute inset-0 p-10 flex flex-col justify-end space-y-3">
                  <div className="flex items-center space-x-3">
                    <div className="w-2 h-2 rounded-full bg-success shadow-[0_0_10px_rgba(16,185,129,0.8)]" />
                    <span className="text-[10px] font-bold tracking-widest uppercase text-success">{agent.status}</span>
                  </div>
                  <h3 className="font-bold text-2xl text-white tracking-tight">{agent.name}</h3>
                  <p className="text-xs font-medium text-white/30">{agent.type}</p>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </section>

      {/* Emergency Overrides */}
      <footer className="bg-critical/[0.02] border border-critical/10 p-6 rounded-[2rem] flex items-center justify-between px-10">
         <div className="flex items-center space-x-4">
            <AlertTriangle className="w-5 h-5 text-critical" />
            <h3 className="text-[11px] font-bold text-critical/60 uppercase tracking-[0.3em]">Advanced Cluster Overrides</h3>
         </div>
         <button className="bg-critical text-white text-[10px] font-bold px-8 py-3 rounded-xl uppercase tracking-widest hover:bg-critical/80 transition-all">
           Purge Cache
         </button>
      </footer>
    </motion.div>
  );
}

