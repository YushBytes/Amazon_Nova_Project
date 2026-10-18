"use client";

import React from "react";
import { 
  Play, 
  Activity, 
  CheckCircle2,
  Eye,
  MoreVertical,
  Zap,
  Rocket,
  FileVideo,
  Bell,
  Search
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export default function HUDView() {
  const [goal, setGoal] = React.useState("");
  const [submissions, setSubmissions] = React.useState<any[]>([]);
  const [activeVideoId, setActiveVideoId] = React.useState<string | null>(null);
  const [detections, setDetections] = React.useState<any[]>([]);
  const [currentTime, setCurrentTime] = React.useState(0);
  const videoInputRef = React.useRef<HTMLInputElement>(null);
  const videoRef = React.useRef<HTMLVideoElement>(null);
  const pollingRef = React.useRef<any>(null);

  const [videoSrc, setVideoSrc] = React.useState<string | null>(null);
  const [selectedFile, setSelectedFile] = React.useState<File | null>(null);
  const [isUploading, setIsUploading] = React.useState(false);
  const [activeTab, setActiveTab] = React.useState("Overview");

  React.useEffect(() => {
    const handleStorage = () => {
      const saved = JSON.parse(localStorage.getItem("novaflow_submissions") || "[]");
      setSubmissions(saved);
    };
    handleStorage();
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  const steps = [
    { title: "Neural Link Status", status: "success", desc: "4096 clusters synced and ready.", time: "0.4s" },
    { title: "Spatial Analysis", status: "active", desc: "Parsing environment metadata...", time: "" },
    { title: "Optimization Loop", status: "pending", desc: "Awaiting cluster data confirmation.", time: "" },
  ];

  // Fetch violations polling
  React.useEffect(() => {
    if (!activeVideoId) return;

    const pollViolations = async () => {
      try {
        const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/violations/${activeVideoId}`);
        const data = await res.json();
        if (data.detections) {
          setDetections(data.detections);
        }
      } catch (err) {
        console.error("Polling error", err);
      }
    };

    pollingRef.current = setInterval(pollViolations, 3000);
    return () => clearInterval(pollingRef.current);
  }, [activeVideoId]);

  return (
    <motion.div 
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-12"
    >
      {/* Top Navigation / Tabs */}
      <div className="flex flex-wrap items-center gap-x-12 gap-y-4 border-b border-white/5">
        {["Overview", "Project Assets", "Telemetry"].map(tab => (
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
              <motion.div layoutId="tab-underline" className="absolute bottom-0 left-0 right-0 h-px bg-primary shadow-glow" />
            )}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-10 items-start">
        {/* Left Column: Video & Goal */}
        <div className="xl:col-span-8 space-y-10">
          {/* Video Feed Area */}
          <section className="relative overflow-hidden rounded-[2rem] border border-white/5 bg-black/20 shadow-2xl group/video">
            <div className="aspect-video relative">
              <video 
                ref={videoRef}
                onTimeUpdate={(e) => setCurrentTime((e.target as any).currentTime)}
                src={videoSrc || "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"}
                className="w-full h-full object-cover"
                controls
              />
              
              {/* Real-Time Bounding Box Overlay */}
              <div className="absolute inset-0 pointer-events-none">
                {detections
                  .filter(d => Math.abs(d.timestamp - currentTime) < 0.5)
                  .map((d, idx) => (
                  <motion.div
                    key={idx}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="absolute border-2 border-critical shadow-glow-critical rounded-md"
                    style={{
                      top: `${d.bounding_box[0] / 10}%`,
                      left: `${d.bounding_box[1] / 10}%`,
                      height: `${(d.bounding_box[2] - d.bounding_box[0]) / 10}%`,
                      width: `${(d.bounding_box[3] - d.bounding_box[1]) / 10}%`,
                    }}
                  >
                    <div className="absolute -top-7 left-0 bg-critical text-white text-[10px] font-bold px-3 py-1 rounded-t-md whitespace-nowrap">
                      {d.category} • {Math.round(d.confidence * 100)}%
                    </div>
                  </motion.div>
                ))}
              </div>
            </div>
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-10">
            {/* Submission Area */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-accent/5 rounded-2xl flex items-center justify-center border border-accent/20">
                  <Rocket className="w-6 h-6 text-accent" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">Video Ingest</h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">Ready for Neural Sync</p>
                </div>
              </div>

              <div 
                onClick={() => videoInputRef.current?.click()}
                className={cn(
                  "border-2 border-dashed rounded-3xl p-10 flex flex-col items-center justify-center space-y-4 transition-all cursor-pointer flex-1 min-h-[160px]",
                  selectedFile ? "border-accent/40 bg-accent/5" : "border-white/5 hover:border-white/10 hover:bg-white/[0.01]"
                )}
              >
                <input 
                  type="file" 
                  ref={videoInputRef}
                  accept="video/*" 
                  className="hidden" 
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) setSelectedFile(file);
                  }}
                />
                <FileVideo className={cn("w-10 h-10", selectedFile ? "text-accent" : "text-white/10")} />
                <div className="text-center">
                  <p className="text-xs font-bold text-white/80">
                    {selectedFile ? selectedFile.name : "Select Video Payload"}
                  </p>
                  <p className="text-[10px] text-white/20 uppercase mt-2">Max 60MB / 60S</p>
                </div>
              </div>

              <button 
                disabled={!selectedFile || isUploading || !!activeVideoId}
                onClick={async () => {
                  if (!selectedFile) return;
                  setIsUploading(true);
                  const video = document.createElement('video');
                  video.preload = 'metadata';
                  video.onloadedmetadata = async function() {
                    window.URL.revokeObjectURL(video.src);
                    const formData = new FormData();
                    formData.append('file', selectedFile);
                    try {
                      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/upload-reel`, {
                        method: 'POST',
                        body: formData,
                      });
                      const data = await response.json();
                      if (data.video_id) {
                        setActiveVideoId(data.video_id);
                        setVideoSrc(data.video_url);
                        setSubmissions(prev => [{
                          id: data.video_id,
                          title: selectedFile.name,
                          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                          status: "READY",
                          agent: "Nova-01"
                        }, ...prev]);
                      }
                    } catch (err) {
                      alert("Sync Error");
                    } finally {
                      setIsUploading(false);
                    }
                  }
                  video.src = URL.createObjectURL(selectedFile);
                }}
                className={cn(
                  "w-full h-14 rounded-2xl font-bold text-xs uppercase tracking-widest transition-all",
                  selectedFile && !activeVideoId ? "bg-accent text-white shadow-xl hover:translate-y-[-2px]" : "bg-white/5 text-white/10"
                )}
              >
                {isUploading ? "Syncing..." : "Launch Audit"}
              </button>
            </section>

            {/* Prompt Area */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
                  <Activity className="w-6 h-6 text-primary" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">Neural Command</h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">Task Definition Layer</p>
                </div>
              </div>

              <textarea
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="Enter audit parameters..."
                className="flex-1 w-full bg-white/[0.01] border border-white/5 rounded-2xl p-6 text-sm focus:outline-none focus:border-primary/20 transition-all placeholder:text-white/10 resize-none min-h-[160px]"
              />

              <button className="w-full h-14 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 text-white font-bold text-xs uppercase tracking-widest transition-all mt-6">
                Pulse Protocol
              </button>
            </section>
          </div>
        </div>

        {/* Right Column: Feeds */}
        <div className="xl:col-span-4 space-y-10">
          <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-10 flex flex-col h-full">
            <div>
              <h2 className="text-[10px] font-bold uppercase tracking-[0.3em] text-white/20 mb-8">System Telemetry</h2>
              <div className="space-y-6">
                {steps.map((step, i) => (
                  <div key={i} className="flex space-x-6 items-start">
                    <div className={cn(
                      "w-1.5 h-1.5 rounded-full mt-2 shrink-0",
                      step.status === 'success' ? "bg-primary shadow-glow" : 
                      step.status === 'active' ? "bg-accent animate-pulse" : "bg-white/10"
                    )} />
                    <div className="space-y-1">
                      <div className="flex items-center justify-between gap-4">
                        <h3 className="text-xs font-bold text-white/90">{step.title}</h3>
                        {step.time && <span className="text-[9px] font-bold text-primary">{step.time}</span>}
                      </div>
                      <p className="text-[10px] text-white/30 font-medium leading-relaxed">{step.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="pt-10 border-t border-white/5 space-y-8 flex-1">
              <h3 className="text-[10px] font-bold uppercase tracking-[0.3em] text-white/20">Audit Pipeline</h3>
              <div className="space-y-4">
                {submissions.slice(0, 4).map((sub: any) => (
                  <div key={sub.id} className="flex items-center justify-between p-5 rounded-2xl bg-white/[0.01] border border-white/5 hover:bg-white/[0.03] transition-colors group">
                    <div className="flex items-center space-x-4 min-w-0">
                      <div className="w-10 h-10 rounded-xl bg-white/5 flex items-center justify-center shrink-0">
                        <Play className="w-4 h-4 text-white/20 group-hover:text-primary transition-colors" />
                      </div>
                      <div className="min-w-0">
                        <p className="text-xs font-bold text-white truncate">{sub.title}</p>
                        <p className="text-[9px] text-white/20 font-medium uppercase tracking-tighter mt-1">{sub.agent} • {sub.time}</p>
                      </div>
                    </div>
                    <div className="flex items-center space-x-3 ml-4">
                      <span className="text-[8px] font-bold text-primary/60 border border-primary/20 px-2 py-0.5 rounded uppercase">{sub.status}</span>
                      <button className="p-2 transition-colors">
                        <Eye className="w-4 h-4 text-white/10 group-hover:text-white" />
                      </button>
                    </div>
                  </div>
                ))}
                {submissions.length === 0 && (
                  <div className="text-center py-10 opacity-20 italic text-[10px] uppercase font-medium tracking-widest p-6 border border-dashed border-white/5 rounded-3xl">
                    Awaiting Signals...
                  </div>
                )}
              </div>
            </div>

            <div className="pt-10 border-t border-white/5 grid grid-cols-2 gap-10">
              {[{ label: "Network", val: 82 }, { label: "Nodes", val: 54 }].map((item, i) => (
                <div key={i} className="space-y-3">
                  <p className="text-[9px] font-bold uppercase text-white/20 tracking-widest">{item.label}</p>
                  <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                    <motion.div initial={{ width: 0 }} animate={{ width: `${item.val}%` }} className="h-full bg-primary" />
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>
    </motion.div>
  );
}
