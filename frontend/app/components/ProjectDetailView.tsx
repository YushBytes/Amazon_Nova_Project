"use client";

import React from "react";
import { 
  Play, 
  Share2, 
  ArrowLeft,
  CheckCircle2,
  Database,
  Clock,
  HardDrive,
  Cpu,
  ShieldCheck,
  Zap
} from "lucide-react";
import { motion } from "framer-motion";

interface Submission {
  id: string;
  filename: string;
  title: string;
  agent: string;
  time: string;
  status: string;
  url: string;
  size: string;
}

export default function ProjectDetailView({ 
  submission, 
  onBack 
}: { 
  submission: Submission; 
  onBack: () => void;
}) {
  return (
    <div className="space-y-8 animate-in fade-in zoom-in-95 duration-700">
      {/* Navigation */}
      <button 
        onClick={onBack}
        className="flex items-center space-x-3 text-white/40 hover:text-white transition-colors group"
      >
        <ArrowLeft className="w-4 h-4 transition-transform group-hover:-translate-x-1" />
        <span className="text-[10px] font-black uppercase tracking-[0.3em]">Return to Nexus</span>
      </button>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-10">
        {/* Left Column: Video & Post Action */}
        <div className="xl:col-span-8 space-y-8">
          <div className="glass-card p-2 rounded-3xl border-white/10 bg-surface/40 overflow-hidden shadow-3xl">
            <video 
              src={submission.url} 
              className="w-full rounded-2xl shadow-2xl"
              controls
              autoPlay
            />
          </div>

          <button className="w-full bg-accent hover:bg-accent/80 p-8 rounded-3xl flex flex-col items-center justify-center space-y-3 transition-all duration-500 shadow-glow-accent group relative overflow-hidden">
            <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/5 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-1000" />
            <Share2 className="w-8 h-8 text-white animate-bounce" />
            <div className="text-center">
              <h2 className="text-xl font-black text-white tracking-[0.2em] uppercase">Post to Social</h2>
              <p className="text-[10px] font-bold text-white/40 uppercase tracking-widest mt-1">Deploy Multi-Channel Network Broadcast</p>
            </div>
          </button>

          {/* Verification Status Card */}
          <div className="glass-card p-8 border-success/20 bg-success/[0.02] flex items-center space-x-8">
            <div className="w-16 h-16 rounded-2xl bg-success/10 border border-success/30 flex items-center justify-center shadow-glow-success">
              <ShieldCheck className="w-8 h-8 text-success" />
            </div>
            <div>
              <h3 className="text-lg font-black text-success tracking-widest uppercase">Audit Passed</h3>
              <p className="text-xs font-bold text-white/40 leading-relaxed max-w-xl">
                The content was verified by NovaFlow Neural Agent #{submission.id.slice(-3)}. 
                Compliance & quality standards met. Frame consistency scan validated.
              </p>
            </div>
          </div>
        </div>

        {/* Right Column: Telemetry & Metadata */}
        <div className="xl:col-span-4 space-y-6">
          <div className="glass-card p-8 border-white/5 bg-white/[0.01] space-y-8">
            <h3 className="text-xs font-black text-white/20 uppercase tracking-[0.4em] pb-4 border-b border-white/5">Metadata Logs</h3>
            
            {[
              { icon: Database, label: "Filename", value: submission.filename },
              { icon: Clock, label: "Last Modified", value: submission.time },
              { icon: HardDrive, label: "Filesize", value: submission.size },
              { icon: Cpu, label: "Encoding", value: "H.265 Neural" }
            ].map((item, i) => (
              <div key={i} className="flex items-center space-x-5 group">
                <div className="w-10 h-10 rounded-xl bg-white/5 flex items-center justify-center group-hover:bg-accent/10 transition-colors">
                  <item.icon className="w-5 h-5 text-white/20 group-hover:text-accent" />
                </div>
                <div>
                  <p className="text-[9px] font-black text-white/20 uppercase tracking-widest mb-1">{item.label}</p>
                  <p className="text-xs font-bold text-white uppercase tracking-wider">{item.value}</p>
                </div>
              </div>
            ))}

            <div className="pt-8 border-t border-white/5 space-y-6">
               <div className="flex justify-between items-center text-[9px] font-black uppercase tracking-widest">
                 <span className="text-white/20">Sync Integrity</span>
                 <span className="text-success">99.8%</span>
               </div>
               <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
                 <motion.div 
                   initial={{ width: 0 }}
                   animate={{ width: "99.8%" }}
                   className="h-full bg-success shadow-glow-success"
                 />
               </div>
            </div>
          </div>

          <div className="glass-card p-8 border-primary/20 bg-primary/[0.02] flex items-center justify-between">
            <div className="flex items-center space-x-4">
               <Zap className="w-5 h-5 text-primary" />
               <span className="text-[10px] font-black text-white tracking-widest uppercase">Turbo Boost Optimization</span>
            </div>
            <div className="w-10 h-6 bg-primary/20 rounded-full flex items-center px-1 border border-primary/30">
               <div className="w-4 h-4 bg-primary rounded-full" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
