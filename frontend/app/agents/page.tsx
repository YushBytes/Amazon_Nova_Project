"use client";

import React from "react";
import { 
  Bot, 
  Cpu, 
  Terminal, 
  Zap, 
  LayoutDashboard, 
  Users, 
  History, 
  UserCircle,
  AlertTriangle,
  ChevronRight
} from "lucide-react";
import { motion } from "framer-motion";
import Link from "next/link";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export default function AgentsPage() {
  const stats = [
    { label: "System CPU", value: "42%", delta: "-5%", color: "primary" },
    { label: "System GPU", value: "68%", delta: "+12%", color: "accent" },
  ];

  const agents = [
    { name: "Router-Agent", status: "ACTIVE", type: "Traffic Orchestration V1.2", image: "https://images.unsplash.com/photo-1620641788421-7a1c342ea42e?q=80&w=2000&auto=format&fit=crop" },
    { name: "Parser-Alpha", status: "ACTIVE", type: "Language Processing Core", image: "https://images.unsplash.com/photo-1639322537231-2f206e06af84?q=80&w=2000&auto=format&fit=crop" },
    { name: "Executor-7", status: "PROCESSING", type: "Task Execution Pipeline", image: "https://images.unsplash.com/photo-1633167606207-d840b5070fc2?q=80&w=2000&auto=format&fit=crop" },
  ];

  const logs = [
    { time: "14:22:01.34", source: "SYS", msg: "Initialization of sub-routine 'delta-s' successful", color: "text-primary" },
    { time: "14:22:01.88", source: "AGNT", msg: "Router-Agent: Receiving packet cluster 0823-X", color: "text-success" },
    { time: "14:22:02.05", source: "WARN", msg: "Latency spike detected in Remote-7 Bridge (L84ms)", color: "text-accent" },
    { time: "14:22:02.12", source: "SYS", msg: "Balancing load across 4 available compute shards...", color: "text-primary" },
    { time: "14:22:04.41", source: "AGNT", msg: "Parser-Alpha: Tokenization complete for payload zero", color: "text-success" },
  ];

  return (
    <div className="flex flex-col min-h-screen p-6 space-y-8 bg-background max-w-md mx-auto relative overflow-hidden pb-32">
       {/* Header */}
       <header className="flex items-center justify-between z-10">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 glass-card flex items-center justify-center border-primary/30">
            <Bot className="text-primary w-6 h-6 drop-shadow-glow" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">NovaFlow Orchestration</h1>
            <p className="text-[10px] uppercase font-bold text-primary tracking-[0.2em]">System Version 2.4.0 • STABLE</p>
          </div>
        </div>
        <button title="Dashboard" className="p-2 glass-card opacity-40">
           <LayoutDashboard className="w-5 h-5" />
        </button>
      </header>

      {/* System Stats */}
      <section className="grid grid-cols-1 gap-6">
        {stats.map((stat, i) => (
          <div key={i} className="glass-card p-5 space-y-4">
            <div className="flex justify-between items-center text-white/50 text-[10px] font-bold uppercase tracking-widest">
              <span>{stat.label}</span>
              <Cpu className="w-4 h-4 text-primary" />
            </div>
            <div className="flex items-baseline space-x-3">
              <span className="text-4xl font-black text-white">{stat.value}</span>
              <span className={cn(
                "text-[10px] font-bold px-2 py-0.5 rounded-full",
                stat.delta.startsWith('+') ? "bg-success/20 text-success" : "bg-critical/20 text-critical"
              )}>
                {stat.delta}
              </span>
            </div>
            <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
               <motion.div 
                 initial={{ width: 0 }}
                 animate={{ width: stat.value }}
                 transition={{ duration: 1, delay: i * 0.2 }}
                 className={cn("h-full shadow-glow", stat.color === 'primary' ? "bg-primary" : "bg-accent")} 
               />
            </div>
          </div>
        ))}

        <div className="glass-card p-5 space-y-4">
          <div className="flex justify-between items-center text-white/50 text-[10px] font-bold uppercase tracking-widest">
            <span>Memory Cluster</span>
            <Terminal className="w-4 h-4 text-primary" />
          </div>
          <div className="flex items-baseline space-x-3">
            <span className="text-3xl font-black text-white">12.4 <span className="text-xs font-medium opacity-40">TB</span></span>
          </div>
          <div className="flex space-x-1.5">
            {[80, 70, 60, 40, 30].map((v, i) => (
              <div key={i} className="flex-1 h-3 bg-white/5 rounded-sm overflow-hidden">
                 <div className="h-full bg-primary/40" style={{ ["--width" as any]: `${v}%`, width: 'var(--width)' }} />
              </div>
            ))}
          </div>
        </div>

        <div className="glass-card p-5 space-y-2">
            <div className="flex justify-between items-center text-white/50 text-[10px] font-bold uppercase tracking-widest">
              <span>Active Threads</span>
              <Zap className="w-4 h-4 text-primary" />
            </div>
            <div className="text-4xl font-black text-white">1,284</div>
            <p className="text-[9px] font-bold text-primary tracking-widest uppercase">765ms • AV. LATENCY: 2.1ms</p>
        </div>
      </section>

      {/* Active Agents */}
      <section className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <Users className="w-5 h-5 text-primary" />
            <h2 className="text-xs uppercase font-extrabold tracking-widest text-foreground">Active Agents</h2>
          </div>
          <span className="text-[8px] font-bold border border-primary/20 bg-primary/5 px-2 py-0.5 rounded-sm text-primary tracking-widest uppercase">3 ONLINE</span>
        </div>

        <div className="space-y-4">
          {agents.map((agent, i) => (
            <motion.div 
              key={i}
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: i * 0.1 }}
              className="glass-card overflow-hidden group border-white/5"
            >
              <div className="aspect-[21/9] relative">
                <img src={agent.image} alt={`Neural Agent: ${agent.name}`} className="w-full h-full object-cover opacity-60 grayscale group-hover:grayscale-0 group-hover:scale-105 transition-all duration-700" />
                <div className="absolute inset-0 bg-gradient-to-r from-surface to-transparent" />
                <div className="absolute inset-0 p-5 flex flex-col justify-center space-y-1">
                  <div className="flex items-center space-x-2">
                    <div className={cn("w-1.5 h-1.5 rounded-full animate-pulse", agent.status === 'ACTIVE' ? "bg-success" : "bg-primary")} />
                    <span className={cn("text-[9px] font-black tracking-widest uppercase", agent.status === 'ACTIVE' ? "text-success" : "text-primary")}>{agent.status}</span>
                  </div>
                  <h3 className="font-black text-lg text-white leading-tight">{agent.name}</h3>
                  <p className="text-[10px] font-bold text-white/40 uppercase tracking-tighter">{agent.type}</p>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </section>

      {/* Live Execution Feed */}
      <section className="glass-card p-5 space-y-6 border-white/10">
        <div className="flex items-center justify-between">
           <div className="flex items-center space-x-2">
             <Terminal className="w-4 h-4 text-primary" />
             <h2 className="text-[10px] uppercase font-extrabold tracking-widest">Live Execution Feed</h2>
           </div>
           <div className="flex space-x-1">
             {[1,2,3].map(i => <div key={i} className="w-1.5 h-1.5 rounded-full bg-white/10" />)}
           </div>
        </div>

        <div className="font-mono text-[9px] space-y-3 leading-relaxed">
          {logs.map((log, i) => (
            <div key={i} className="flex space-x-3 group">
              <span className="text-white/20 whitespace-nowrap">{log.time}</span>
              <span className={cn("font-bold whitespace-nowrap", log.color)}>[{log.source}]</span>
              <p className="text-white/60 group-hover:text-white transition-colors">{log.msg}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Node Overview */}
      <section className="glass-card p-6 space-y-6 bg-primary/5 border-primary/20">
         <div className="flex justify-between items-start">
            <div>
               <p className="text-[9px] font-black text-primary uppercase tracking-[0.2em] mb-1">Node Overview</p>
               <div className="grid grid-cols-2 gap-x-12 gap-y-3 text-[10px] font-bold uppercase tracking-tight">
                  <div className="space-y-1"><span className="text-white/30 block">Region</span><span>us-east-core-1</span></div>
                  <div className="space-y-1"><span className="text-white/30 block">IP (Virtual)</span><span>192.168.0.245</span></div>
               </div>
            </div>
            <div className="text-right">
               <span className="text-white/30 text-[9px] font-bold uppercase block mb-1">Cluster Load</span>
               <span className="text-success font-black text-xs">OPTIMAL</span>
            </div>
         </div>
         <button className="w-full bg-primary hover:bg-accent text-white py-4 rounded-xl text-[10px] font-black uppercase tracking-[0.2em] transition-all shadow-glow flex items-center justify-center space-x-3 group">
            <Zap className="w-4 h-4 group-hover:animate-pulse" />
            <span>Deploy New Instance</span>
         </button>
      </section>

      {/* Emergency Override */}
      <section className="glass-card p-4 border-critical/30 bg-critical/5 flex items-center justify-between">
         <div className="flex items-center space-x-4">
            <div className="bg-critical/20 p-2 rounded-lg"><AlertTriangle className="w-5 h-5 text-critical" /></div>
            <div>
              <h3 className="text-[10px] font-black text-critical uppercase tracking-widest">Emergency Override</h3>
              <p className="text-[9px] text-critical/60 font-bold max-w-[150px]">Shutdown all active agent pipelines immediately across all clusters.</p>
            </div>
         </div>
         <button className="bg-critical/10 border border-critical/30 text-critical text-[10px] font-black px-4 py-3 rounded-lg hover:bg-critical hover:text-white transition-all uppercase">
           Terminate All
         </button>
      </section>

      {/* Bottom Navigation */}
      <nav className="fixed bottom-0 left-0 right-0 bg-background/80 backdrop-blur-2xl border-t border-white/5 p-4 z-50">
        <div className="flex justify-between items-center max-w-md mx-auto px-4">
          <Link href="/" className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <LayoutDashboard className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">HUD</span>
          </Link>
          <div className="flex flex-col items-center space-y-1 text-primary transition-all">
            <Users className="w-6 h-6 drop-shadow-glow" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Agents</span>
            <motion.div layoutId="nav" className="w-1 h-1 bg-primary rounded-full" />
          </div>
          <Link href="/logs" className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <History className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Logs</span>
          </Link>
          <button className="flex flex-col items-center space-y-1 text-white/30 hover:text-white/60 transition-all">
            <UserCircle className="w-6 h-6" />
            <span className="text-[9px] font-bold uppercase tracking-widest">Profile</span>
          </button>
        </div>
      </nav>
    </div>
  );
}
