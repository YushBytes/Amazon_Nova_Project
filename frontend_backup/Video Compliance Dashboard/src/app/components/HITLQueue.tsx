import { motion } from "motion/react";
import { Violation } from "../types";
import { AlertTriangle, CheckCircle, X } from "lucide-react";
import { Button } from "./ui/button";
import { Badge } from "./ui/badge";

interface HITLQueueProps {
  violations: Violation[];
  onApprove: (violationId: string) => void;
  onReject: (violationId: string) => void;
}

export function HITLQueue({ violations, onApprove, onReject }: HITLQueueProps) {
  const pendingViolations = violations.filter((v) => v.status === "pending_approval");

  if (pendingViolations.length === 0) {
    return null;
  }

  return (
    <div className="fixed bottom-6 right-6 z-50 max-w-md">
      <div className="space-y-3">
        {pendingViolations.map((violation, index) => (
          <motion.div
            key={violation.id}
            initial={{ opacity: 0, y: 20, scale: 0.9 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, x: 100, scale: 0.9 }}
            transition={{ delay: index * 0.1 }}
            className="bg-slate-900 border-2 border-yellow-500/50 rounded-lg shadow-2xl overflow-hidden"
          >
            {/* Alert Header */}
            <div className="bg-gradient-to-r from-yellow-500/20 to-orange-500/20 border-b border-yellow-500/30 px-4 py-3">
              <div className="flex items-center gap-2">
                <motion.div
                  animate={{
                    rotate: [0, 10, -10, 0],
                  }}
                  transition={{
                    duration: 0.5,
                    repeat: Infinity,
                    repeatDelay: 1,
                  }}
                >
                  <AlertTriangle className="w-5 h-5 text-yellow-400" />
                </motion.div>
                <h3 className="font-semibold text-yellow-400">Human Review Required</h3>
              </div>
            </div>

            {/* Content */}
            <div className="p-4">
              <div className="mb-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium text-slate-300">Violation Detected</span>
                  <Badge
                    variant="outline"
                    className={
                      violation.severity === "high"
                        ? "border-red-500/50 text-red-400"
                        : violation.severity === "medium"
                        ? "border-yellow-500/50 text-yellow-400"
                        : "border-blue-500/50 text-blue-400"
                    }
                  >
                    {violation.severity.toUpperCase()}
                  </Badge>
                </div>

                <p className="text-slate-100 font-semibold mb-1">{violation.description}</p>
                <p className="text-xs text-slate-400">
                  at {Math.floor(violation.timestamp / 60)}:
                  {String(violation.timestamp % 60).padStart(2, "0")}
                </p>
              </div>

              <div className="bg-slate-950 rounded p-3 mb-3 border border-slate-700">
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div>
                    <span className="text-slate-500">Confidence:</span>
                    <span className="ml-2 text-cyan-400 font-semibold">{violation.confidence}%</span>
                  </div>
                  <div>
                    <span className="text-slate-500">Type:</span>
                    <span className="ml-2 text-violet-400 font-semibold capitalize">
                      {violation.type}
                    </span>
                  </div>
                </div>
                {violation.strategy && (
                  <div className="mt-2 pt-2 border-t border-slate-800">
                    <span className="text-slate-500 text-xs">Proposed Fix:</span>
                    <p className="text-slate-300 text-xs mt-1">{violation.strategy}</p>
                  </div>
                )}
              </div>

              <p className="text-xs text-slate-400 mb-4">Should the AI proceed with this fix?</p>

              {/* Action Buttons */}
              <div className="flex gap-2">
                <Button
                  onClick={() => onApprove(violation.id)}
                  className="flex-1 bg-green-600 hover:bg-green-700 text-white"
                >
                  <CheckCircle className="w-4 h-4 mr-2" />
                  Approve Fix
                </Button>
                <Button
                  onClick={() => onReject(violation.id)}
                  variant="outline"
                  className="flex-1 border-red-500/50 text-red-400 hover:bg-red-500/10"
                >
                  <X className="w-4 h-4 mr-2" />
                  Reject
                </Button>
              </div>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}
