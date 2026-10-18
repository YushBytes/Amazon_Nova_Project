import { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "motion/react";
import { LogEntry } from "../types";
import { Terminal, Zap, CheckCircle, AlertCircle, Info } from "lucide-react";

interface ThoughtStreamProps {
  logs: LogEntry[];
}

export function ThoughtStream({ logs }: ThoughtStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  const getIcon = (type: LogEntry["type"]) => {
    switch (type) {
      case "detection":
        return <AlertCircle className="w-4 h-4 text-cyan-400" />;
      case "processing":
        return <Zap className="w-4 h-4 text-violet-400" />;
      case "success":
        return <CheckCircle className="w-4 h-4 text-green-400" />;
      case "error":
        return <AlertCircle className="w-4 h-4 text-red-400" />;
      default:
        return <Info className="w-4 h-4 text-slate-400" />;
    }
  };

  const getTextColor = (type: LogEntry["type"]) => {
    switch (type) {
      case "detection":
        return "text-cyan-300";
      case "processing":
        return "text-violet-300";
      case "success":
        return "text-green-300";
      case "error":
        return "text-red-300";
      default:
        return "text-slate-300";
    }
  };

  return (
    <div className="bg-slate-950 border border-slate-700 rounded-lg overflow-hidden">
      <div className="border-b border-slate-700 bg-slate-900/50 px-4 py-3 flex items-center gap-2">
        <Terminal className="w-4 h-4 text-cyan-400" />
        <h3 className="font-semibold text-sm">Thought Stream</h3>
        <div className="ml-auto flex items-center gap-2">
          <motion.div
            className="w-2 h-2 rounded-full bg-green-500"
            animate={{
              opacity: [1, 0.5, 1],
            }}
            transition={{
              duration: 2,
              repeat: Infinity,
              ease: "easeInOut",
            }}
          />
          <span className="text-xs text-slate-400">Live</span>
        </div>
      </div>

      <div
        ref={containerRef}
        className="h-96 overflow-y-auto p-4 font-mono text-sm space-y-2 custom-scrollbar"
      >
        <AnimatePresence initial={false}>
          {logs.map((log) => (
            <motion.div
              key={log.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3 }}
              className="flex items-start gap-3 group"
            >
              <div className="mt-0.5">{getIcon(log.type)}</div>
              <div className="flex-1">
                <span className="text-slate-500 text-xs">{log.timestamp}</span>
                <span className="mx-2 text-slate-600">—</span>
                <span className={getTextColor(log.type)}>{log.message}</span>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>

        {logs.length === 0 && (
          <div className="flex items-center justify-center h-full text-slate-600">
            <div className="text-center">
              <Terminal className="w-12 h-12 mx-auto mb-2 opacity-50" />
              <p>Waiting for processing to start...</p>
            </div>
          </div>
        )}
      </div>

      <style>{`
        .custom-scrollbar::-webkit-scrollbar {
          width: 8px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
          background: rgb(15 23 42);
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
          background: rgb(51 65 85);
          border-radius: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
          background: rgb(71 85 105);
        }
      `}</style>
    </div>
  );
}
