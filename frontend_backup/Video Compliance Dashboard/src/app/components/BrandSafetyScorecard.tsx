import { Violation } from "../types";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Download, Shield, AlertCircle, CheckCircle2, XCircle } from "lucide-react";
import { motion } from "motion/react";

interface BrandSafetyScorecardProps {
  violations: Violation[];
  grade: string;
  videoName: string;
}

export function BrandSafetyScorecard({ violations, grade, videoName }: BrandSafetyScorecardProps) {
  const logoCount = violations.filter((v) => v.type === "logo").length;
  const safetyCount = violations.filter((v) => v.type === "safety").length;
  const contentCount = violations.filter((v) => v.type === "content").length;

  const fixedCount = violations.filter((v) => v.status === "fixed").length;
  const pendingCount = violations.filter((v) => v.status === "pending_approval").length;
  const detectedCount = violations.filter((v) => v.status === "detected").length;

  const getGradeColor = (grade: string) => {
    switch (grade) {
      case "A":
        return "text-green-400 border-green-500";
      case "B":
        return "text-blue-400 border-blue-500";
      case "C":
        return "text-yellow-400 border-yellow-500";
      case "D":
        return "text-orange-400 border-orange-500";
      default:
        return "text-red-400 border-red-500";
    }
  };

  const handleExportPDF = () => {
    // Mock PDF export functionality
    alert("Compliance PDF would be generated and downloaded in production.");
  };

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Shield className="w-6 h-6 text-violet-400" />
          <h2 className="text-xl font-bold">Brand Safety Scorecard</h2>
        </div>
        <Button
          onClick={handleExportPDF}
          className="bg-gradient-to-r from-cyan-500 to-violet-500 hover:from-cyan-600 hover:to-violet-600"
        >
          <Download className="w-4 h-4 mr-2" />
          Export PDF
        </Button>
      </div>

      {/* Grade Display */}
      <div className="mb-6">
        <div className="flex items-center gap-4">
          <motion.div
            className={`w-24 h-24 rounded-full border-4 ${getGradeColor(
              grade
            )} flex items-center justify-center`}
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: "spring", duration: 0.6 }}
          >
            <span className={`text-4xl font-bold ${getGradeColor(grade)}`}>{grade}</span>
          </motion.div>
          <div>
            <h3 className="text-lg font-semibold text-slate-300">Overall Safety Grade</h3>
            <p className="text-sm text-slate-400 mt-1">
              {grade === "A" && "Excellent - Minimal violations"}
              {grade === "B" && "Good - Minor violations addressed"}
              {grade === "C" && "Fair - Several violations detected"}
              {grade === "D" && "Poor - Multiple serious violations"}
              {grade === "F" && "Failed - Critical violations present"}
            </p>
          </div>
        </div>
      </div>

      {/* Violation Counts */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <Card className="bg-slate-900/50 border-slate-700">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-slate-400">Logo Violations</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-cyan-400">{logoCount}</div>
          </CardContent>
        </Card>

        <Card className="bg-slate-900/50 border-slate-700">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-slate-400">Safety Hazards</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-orange-400">{safetyCount}</div>
          </CardContent>
        </Card>

        <Card className="bg-slate-900/50 border-slate-700">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium text-slate-400">Content Issues</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-violet-400">{contentCount}</div>
          </CardContent>
        </Card>
      </div>

      {/* Status Breakdown */}
      <div className="space-y-3 mb-6">
        <h4 className="text-sm font-semibold text-slate-400">Resolution Status</h4>
        
        <div className="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-green-400" />
            <span className="text-sm text-slate-300">Fixed</span>
          </div>
          <Badge className="bg-green-500/20 text-green-400">{fixedCount}</Badge>
        </div>

        <div className="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-yellow-400" />
            <span className="text-sm text-slate-300">Pending Approval</span>
          </div>
          <Badge className="bg-yellow-500/20 text-yellow-400">{pendingCount}</Badge>
        </div>

        <div className="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700">
          <div className="flex items-center gap-2">
            <XCircle className="w-4 h-4 text-cyan-400" />
            <span className="text-sm text-slate-300">Detected</span>
          </div>
          <Badge className="bg-cyan-500/20 text-cyan-400">{detectedCount}</Badge>
        </div>
      </div>

      {/* Summary */}
      <div className="pt-4 border-t border-slate-700">
        <p className="text-xs text-slate-400">
          <strong className="text-slate-300">{videoName}</strong> has been analyzed for brand
          safety compliance. {violations.length} total violation{violations.length !== 1 ? "s" : ""}{" "}
          detected.
        </p>
      </div>
    </div>
  );
}
