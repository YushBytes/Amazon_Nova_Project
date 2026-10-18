"use client";

import React, { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Command, User, Shield, ArrowRight, Loader2, Sparkles } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import { cn } from "../lib/utils";

export default function LoginPage() {
  const [role, setRole] = useState<"client" | "agent">("client");
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

    const endpoint = isLogin ? "/auth/login" : "/auth/signup";
    const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    try {
      const response = await fetch(`${API_URL}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, role }),
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Authentication failed");

      login(data.access_token, data.role, username);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background text-foreground flex items-center justify-center p-6 relative overflow-hidden">
      {/* Dynamic Background Glows */}
      <div className="absolute top-0 left-0 w-full h-full pointer-events-none">
        <motion.div 
          animate={{ scale: [1, 1.2, 1], opacity: [0.1, 0.2, 0.1] }}
          transition={{ duration: 8, repeat: Infinity }}
          className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-primary/20 blur-[120px] rounded-full" 
        />
        <motion.div 
          animate={{ scale: [1, 1.3, 1], opacity: [0.05, 0.15, 0.05] }}
          transition={{ duration: 10, repeat: Infinity, delay: 1 }}
          className="absolute bottom-[-10%] right-[-10%] w-[60%] h-[60%] bg-accent/20 blur-[150px] rounded-full" 
        />
      </div>

      <motion.div 
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-xl relative z-10"
      >
        <div className="glass-card p-10 md:p-16 space-y-10 border-white/5 bg-white/[0.02] backdrop-blur-3xl shadow-2xl">
          {/* Brand Header */}
          <div className="flex flex-col items-center text-center space-y-4">
            <div className="w-16 h-16 bg-primary rounded-2xl flex items-center justify-center shadow-glow mb-2">
              <Command className="text-white w-10 h-10" />
            </div>
            <div>
              <h1 className="text-4xl font-black tracking-tighter text-white uppercase italic">NovaFlow</h1>
              <p className="text-[10px] uppercase font-bold text-primary tracking-[0.4em] mt-1">Command Center Access</p>
            </div>
          </div>

          {/* Role Selection */}
          <div className="grid grid-cols-2 gap-6">
            {[
              { id: "client", label: "Client", icon: User, desc: "Organization Owner" },
              { id: "agent", label: "Agent", icon: Shield, desc: "Operational Unit" }
            ].map((item) => (
              <button
                key={item.id}
                onClick={() => setRole(item.id as any)}
                className={cn(
                  "p-6 rounded-3xl border transition-all duration-500 text-left group relative overflow-hidden",
                  role === item.id 
                    ? "bg-primary border-primary shadow-2xl text-white" 
                    : "bg-white/[0.02] border-white/10 text-white/40 hover:border-white/20 hover:bg-white/[0.04]"
                )}
              >
                <item.icon className={cn("w-8 h-8 mb-3", role === item.id ? "text-white" : "text-primary/60 group-hover:scale-110 transition-transform")} />
                <p className="font-bold text-lg uppercase tracking-widest leading-none">{item.label}</p>
                <p className="text-xs font-semibold opacity-60 mt-1 uppercase tracking-widest leading-none">{item.desc}</p>
                {role === item.id && (
                  <motion.div layoutId="role-bg" className="absolute top-4 right-4 p-2">
                    <Sparkles className="w-4 h-4 text-white/40 animate-pulse" />
                  </motion.div>
                )}
              </button>
            ))}
          </div>

          {/* Auth Form */}
          <form onSubmit={handleAuth} className="space-y-6">
            <div className="space-y-4">
              <div className="space-y-2">
                <label className="text-[10px] font-black uppercase tracking-widest text-white/40 ml-4">Identifier</label>
                <input 
                  type="text" 
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="USERNAME_NODEx"
                  className="w-full bg-white/[0.03] border border-white/10 rounded-2xl p-5 text-sm focus:outline-none focus:border-primary/50 transition-all font-mono tracking-widest uppercase placeholder:text-white/5"
                  required
                />
              </div>
              <div className="space-y-2">
                <label className="text-[10px] font-black uppercase tracking-widest text-white/40 ml-4">Authorization Code</label>
                <input 
                  type="password" 
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••••••"
                  className="w-full bg-white/[0.03] border border-white/10 rounded-2xl p-5 text-sm focus:outline-none focus:border-primary/50 transition-all font-mono tracking-widest placeholder:text-white/5"
                  required
                />
              </div>
            </div>

            {error && (
              <motion.div 
                initial={{ opacity: 0, x: -10 }} 
                animate={{ opacity: 1, x: 0 }}
                className="bg-critical/10 border border-critical/30 p-4 rounded-xl text-[10px] font-black uppercase tracking-widest text-critical text-center"
              >
                ACCESS_DENIED: {error}
              </motion.div>
            )}

            <button 
              disabled={isLoading}
              type="submit"
              className="w-full bg-primary hover:bg-accent disabled:opacity-50 text-white p-6 rounded-3xl font-black text-xs uppercase tracking-[0.3em] shadow-glow hover:scale-[1.02] active:scale-[0.98] transition-all flex items-center justify-center space-x-3 group"
            >
              {isLoading ? (
                <Loader2 className="w-5 h-5 animate-spin" />
              ) : (
                <>
                  <span>{isLogin ? "INITIATE_HANDSHAKE" : "REGISTER_IDENTITY"}</span>
                  <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                </>
              )}
            </button>
          </form>

          {/* Footer Toggle */}
          <div className="text-center pt-2">
            <button 
              onClick={() => setIsLogin(!isLogin)}
              className="text-[10px] font-black uppercase tracking-widest text-white/20 hover:text-primary transition-colors italic"
            >
              {isLogin ? "> PROTOCOL_SIGNUP: NEW_USER" : "> PROTOCOL_LOGIN: EXISTING_USER"}
            </button>
          </div>
        </div>

        {/* Security Meta */}
        <div className="mt-8 flex justify-between items-center px-4 opacity-20 text-[9px] font-black uppercase tracking-[0.2em] font-mono">
           <div className="flex items-center space-x-2">
              <span className="w-1.5 h-1.5 rounded-full bg-success" />
              <span>Security Hash: SHA-256 (AUTO)</span>
           </div>
           <span>AES-256 Encryption Active</span>
        </div>
      </motion.div>
    </div>
  );
}
