import { motion } from "motion/react";
import { ProcessingStage } from "../types";

interface HealingStatusBarProps {
  currentStage: ProcessingStage;
  progress: number;
}

const stages: ProcessingStage[] = ["SCANNING", "DETECTED", "GENERATING FIX", "FINAL REVIEW"];

export function HealingStatusBar({ currentStage, progress }: HealingStatusBarProps) {
  const currentIndex = stages.indexOf(currentStage);

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-6">
      <h3 className="text-sm font-semibold text-slate-400 mb-4">Processing Status</h3>
      
      <div className="space-y-4">
        {/* Progress Bar */}
        <div className="relative h-2 bg-slate-700 rounded-full overflow-hidden">
          <motion.div
            className="absolute inset-y-0 left-0 bg-gradient-to-r from-cyan-500 to-violet-500"
            initial={{ width: 0 }}
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.5 }}
          />
        </div>

        {/* Stages */}
        <div className="grid grid-cols-4 gap-2">
          {stages.map((stage, index) => {
            const isActive = index === currentIndex;
            const isComplete = index < currentIndex;

            return (
              <div key={stage} className="relative">
                <motion.div
                  className={`p-3 rounded-lg border-2 text-center transition-all ${
                    isActive
                      ? "border-cyan-500 bg-cyan-500/10"
                      : isComplete
                      ? "border-green-500 bg-green-500/10"
                      : "border-slate-700 bg-slate-800/30"
                  }`}
                  animate={
                    isActive
                      ? {
                          boxShadow: [
                            "0 0 0px rgba(6, 182, 212, 0)",
                            "0 0 20px rgba(6, 182, 212, 0.5)",
                            "0 0 0px rgba(6, 182, 212, 0)",
                          ],
                        }
                      : {}
                  }
                  transition={{
                    duration: 2,
                    repeat: isActive ? Infinity : 0,
                    ease: "easeInOut",
                  }}
                >
                  <div className="flex items-center justify-center mb-1">
                    {isComplete ? (
                      <div className="w-5 h-5 rounded-full bg-green-500 flex items-center justify-center">
                        <svg
                          className="w-3 h-3 text-white"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={3}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                    ) : isActive ? (
                      <motion.div
                        className="w-5 h-5 rounded-full bg-cyan-500"
                        animate={{
                          scale: [1, 1.2, 1],
                        }}
                        transition={{
                          duration: 1.5,
                          repeat: Infinity,
                          ease: "easeInOut",
                        }}
                      />
                    ) : (
                      <div className="w-5 h-5 rounded-full bg-slate-600" />
                    )}
                  </div>
                  
                  <div
                    className={`text-xs font-medium ${
                      isActive
                        ? "text-cyan-400"
                        : isComplete
                        ? "text-green-400"
                        : "text-slate-500"
                    }`}
                  >
                    {stage}
                  </div>
                </motion.div>
              </div>
            );
          })}
        </div>

        {/* Current Stage Description */}
        <div className="text-center">
          <p className="text-sm text-slate-400">
            {currentStage === "SCANNING" && "Analyzing video frames for brand violations..."}
            {currentStage === "DETECTED" && "Violations identified, preparing fixes..."}
            {currentStage === "GENERATING FIX" && "AI is generating corrected content..."}
            {currentStage === "FINAL REVIEW" && "Finalizing and preparing for review..."}
          </p>
        </div>
      </div>
    </div>
  );
}
