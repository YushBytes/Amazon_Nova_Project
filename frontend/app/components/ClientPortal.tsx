"use client";

import React from "react";
import { 
  Play, 
  Share2, 
  Info, 
  CheckCircle2, 
  LayoutGrid,
  ShieldCheck,
  Search,
  Filter
} from "lucide-react";
import { motion } from "framer-motion";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

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

export default function ClientPortal({ onSelect }: { onSelect: (sub: Submission) => void }) {
  const [submissions, setSubmissions] = React.useState<Submission[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [activeTab, setActiveTab] = React.useState("ALL NODES");

  React.useEffect(() => {
    const fetchSubmissions = async () => {
      try {
        const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/submissions`);
        const data = await res.json();
        if (data && Array.isArray(data.submissions)) {
          setSubmissions(data.submissions);
        }
      } catch (err) {
        console.error("Fetch error", err);
      } finally {
        setLoading(false);
      }
    };
    fetchSubmissions();
  }, []);

  return (
    <div className="space-y-12 animate-in fade-in slide-in-from-bottom-4 duration-1000">
      {/* Header Area */}
      <header className="flex flex-col md:flex-row md:items-end justify-between gap-10 border-b border-white/5 pb-10">
        <div className="space-y-2">
          <h1 className="text-3xl font-bold tracking-tight text-white flex items-center space-x-4">
            <LayoutGrid className="w-8 h-8 text-accent" />
            <span>Digital Assets</span>
          </h1>
          <p className="text-[11px] font-semibold text-white/30 uppercase tracking-[0.3em]">
            Verified Neural Output Feed
          </p>
        </div>
        
        <div className="flex items-center space-x-6">
          <div className="bg-white/[0.02] border border-white/5 rounded-2xl px-5 py-3 flex items-center space-x-4 focus-within:border-accent/40 transition-all">
            <Search className="w-4 h-4 text-white/20" />
            <input 
              type="text" 
              placeholder="Search assets..." 
              className="bg-transparent border-none outline-none text-xs font-medium text-white placeholder:text-white/10 w-64"
            />
          </div>
          <button className="bg-white/[0.02] hover:bg-white/5 p-3 rounded-2xl border border-white/5 transition-all text-white/20 hover:text-white">
            <Filter className="w-5 h-5" />
          </button>
        </div>
      </header>

      {/* Filter Tabs */}
      <div className="flex items-center gap-4 overflow-x-auto pb-6 no-scrollbar shrink-0">
        {["ALL NODES", "LLAVA-CORE", "ACT-AGENT", "VERIFIED"].map((tab) => (
          <button 
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              "px-8 py-4 rounded-2xl text-[10px] font-bold uppercase tracking-widest transition-all whitespace-nowrap flex-shrink-0",
              activeTab === tab ? "bg-accent text-white shadow-xl shadow-accent/20" : "bg-white/[0.01] text-white/40 hover:bg-white/5 border border-white/5"
            )}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Submission Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10 pb-20">
        {loading ? (
          [1,2,3].map(i => (
            <div key={i} className="bg-white/[0.01] h-80 animate-pulse rounded-[2rem] border border-white/5" />
          ))
        ) : (
          (submissions || []).map((sub) => (
            <motion.div 
              key={sub.id}
              whileHover={{ y: -8 }}
              className="bg-white/[0.02] border border-white/5 rounded-[2rem] overflow-hidden flex flex-col group transition-all hover:border-accent/20 hover:shadow-2xl"
            >
              <div className="aspect-video relative overflow-hidden bg-black/40">
                <video 
                  src={sub.url} 
                  className="w-full h-full object-cover opacity-60 group-hover:opacity-100 transition-opacity"
                  muted
                />
                
                {sub.status === "VERIFIED" && (
                  <div className="absolute top-6 left-6 flex items-center space-x-2 bg-black/80 backdrop-blur-xl border border-success/30 px-3 py-1.5 rounded-full">
                    <ShieldCheck className="w-3.5 h-3.5 text-success" />
                    <span className="text-[10px] font-bold text-success tracking-widest uppercase">Verified</span>
                  </div>
                )}
              </div>

              <div className="p-8 space-y-6">
                <div>
                  <h3 className="text-base font-bold text-white tracking-wide truncate">{sub.title}</h3>
                  <div className="flex items-center space-x-2 mt-2">
                    <span className="text-[10px] font-semibold text-white/20 uppercase tracking-tighter">Node: {sub.agent}</span>
                    <span className="text-white/10 text-xs">•</span>
                    <span className="text-[10px] font-semibold text-white/20 uppercase tracking-tighter">{sub.time}</span>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <button 
                    onClick={() => onSelect(sub)}
                    className="flex items-center justify-center space-x-3 py-4 rounded-2xl bg-white/5 hover:bg-accent text-white/60 hover:text-white border border-white/5 transition-all text-xs font-bold uppercase tracking-widest"
                  >
                    <Play className="w-4 h-4" />
                    <span>Review</span>
                  </button>
                  <button className="flex items-center justify-center space-x-3 py-4 rounded-2xl bg-white/5 hover:bg-primary text-white/60 hover:text-white border border-white/5 transition-all text-xs font-bold uppercase tracking-widest">
                    <Share2 className="w-4 h-4" />
                    <span>Post</span>
                  </button>
                </div>
              </div>
            </motion.div>
          ))
        )}
      </div>
    </div>
  );
}
