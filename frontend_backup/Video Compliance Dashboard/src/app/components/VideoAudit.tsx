import { useState, useEffect } from "react";
import { useParams, Link } from "react-router";
import { ArrowLeft } from "lucide-react";
import { mockVideos, calculateGrade } from "../data/mockData";
import { Violation, LogEntry, ProcessingStage } from "../types";
import { AuditMirror } from "./AuditMirror";
import { ThoughtStream } from "./ThoughtStream";
import { HITLQueue } from "./HITLQueue";
import { HealingStatusBar } from "./HealingStatusBar";
import { BrandSafetyScorecard } from "./BrandSafetyScorecard";
import { Button } from "./ui/button";

export function VideoAudit() {
  const { videoId } = useParams();
  const video = mockVideos.find((v) => v.id === videoId);

  const [violations, setViolations] = useState<Violation[]>(video?.violations || []);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [currentTime, setCurrentTime] = useState(0);
  const [currentStage, setCurrentStage] = useState<ProcessingStage>("SCANNING");
  const [progress, setProgress] = useState(0);

  // Simulate AI processing
  useEffect(() => {
    if (!video) return;

    let logId = 0;
    const addLog = (message: string, type: LogEntry["type"]) => {
      const now = new Date();
      const timestamp = `${String(now.getMinutes()).padStart(2, "0")}:${String(
        now.getSeconds()
      ).padStart(2, "0")}`;
      setLogs((prev) => [...prev, { id: `log-${logId++}`, timestamp, message, type }]);
    };

    // Initial scanning
    setTimeout(() => {
      addLog("Initializing video analysis pipeline...", "info");
      setProgress(5);
    }, 500);

    setTimeout(() => {
      addLog("Loading AI models (Nova Canvas, Nova Reel)...", "info");
      setProgress(10);
    }, 1500);

    setTimeout(() => {
      addLog("Extracting frames at 30fps...", "info");
      setProgress(20);
    }, 2500);

    // Detection phase
    setTimeout(() => {
      setCurrentStage("DETECTED");
      addLog("Frame extraction complete. Beginning violation detection...", "processing");
      setProgress(35);
    }, 4000);

    violations.forEach((violation, index) => {
      setTimeout(() => {
        addLog(`Ingesting Frame ${violation.timestamp * 30}...`, "info");
        setProgress(35 + index * 5);
      }, 5000 + index * 2000);

      setTimeout(() => {
        addLog(
          `Detected: "${violation.description}" (Confidence ${violation.confidence}%)`,
          "detection"
        );
      }, 5500 + index * 2000);

      setTimeout(() => {
        addLog(`Severity Assessment: ${violation.severity.toUpperCase()}`, "info");
      }, 6000 + index * 2000);

      setTimeout(() => {
        addLog(`Strategy: ${violation.strategy}`, "processing");
        // Set to pending approval for high severity
        if (violation.severity === "high") {
          setViolations((prev) =>
            prev.map((v) => (v.id === violation.id ? { ...v, status: "pending_approval" } : v))
          );
        }
      }, 6500 + index * 2000);
    });

    // Processing phase
    const processingDelay = 5000 + violations.length * 2000;
    setTimeout(() => {
      setCurrentStage("GENERATING FIX");
      addLog("All violations catalogued. Initiating correction process...", "processing");
      setProgress(60);
    }, processingDelay);

    setTimeout(() => {
      addLog("Generating inpainted frames via Nova Canvas...", "processing");
      setProgress(75);
    }, processingDelay + 2000);

    setTimeout(() => {
      addLog("Stitching corrected frames into video timeline...", "processing");
      setProgress(85);
    }, processingDelay + 4000);

    // Final review
    setTimeout(() => {
      setCurrentStage("FINAL REVIEW");
      addLog("Running quality assurance checks...", "info");
      setProgress(95);
    }, processingDelay + 6000);

    setTimeout(() => {
      addLog("✓ All systems nominal. Video ready for review.", "success");
      setProgress(100);
    }, processingDelay + 8000);
  }, [video, violations.length]);

  const handleApprove = (violationId: string) => {
    setViolations((prev) =>
      prev.map((v) => (v.id === violationId ? { ...v, status: "approved" } : v))
    );
    
    const violation = violations.find((v) => v.id === violationId);
    const now = new Date();
    const timestamp = `${String(now.getMinutes()).padStart(2, "0")}:${String(
      now.getSeconds()
    ).padStart(2, "0")}`;
    
    setLogs((prev) => [
      ...prev,
      {
        id: `log-${prev.length}`,
        timestamp,
        message: `✓ Human approved fix for "${violation?.description}"`,
        type: "success",
      },
    ]);

    // Simulate fixing
    setTimeout(() => {
      setViolations((prev) =>
        prev.map((v) => (v.id === violationId ? { ...v, status: "fixed" } : v))
      );
    }, 2000);
  };

  const handleReject = (violationId: string) => {
    setViolations((prev) =>
      prev.map((v) => (v.id === violationId ? { ...v, status: "rejected" } : v))
    );
    
    const violation = violations.find((v) => v.id === violationId);
    const now = new Date();
    const timestamp = `${String(now.getMinutes()).padStart(2, "0")}:${String(
      now.getSeconds()
    ).padStart(2, "0")}`;
    
    setLogs((prev) => [
      ...prev,
      {
        id: `log-${prev.length}`,
        timestamp,
        message: `✗ Human rejected fix for "${violation?.description}"`,
        type: "error",
      },
    ]);
  };

  const handleManualFlag = (x: number, y: number) => {
    const newViolation: Violation = {
      id: `v-manual-${Date.now()}`,
      timestamp: currentTime,
      type: "content",
      description: "Manual Flag",
      confidence: 100,
      severity: "medium",
      boundingBox: { x: x * 6.4, y: y * 3.6, width: 80, height: 60 },
      status: "pending_approval",
      strategy: "Manual Review Required",
    };

    setViolations((prev) => [...prev, newViolation]);

    const now = new Date();
    const timestamp = `${String(now.getMinutes()).padStart(2, "0")}:${String(
      now.getSeconds()
    ).padStart(2, "0")}`;
    
    setLogs((prev) => [
      ...prev,
      {
        id: `log-${prev.length}`,
        timestamp,
        message: `👤 Human flagged violation at [${Math.floor(x)}, ${Math.floor(y)}]`,
        type: "detection",
      },
    ]);
  };

  if (!video) {
    return (
      <div className="min-h-screen bg-slate-900 text-slate-100 flex items-center justify-center">
        <div className="text-center">
          <h1 className="text-2xl font-bold mb-4">Video not found</h1>
          <Link to="/">
            <Button>Back to Dashboard</Button>
          </Link>
        </div>
      </div>
    );
  }

  const grade = calculateGrade(violations);

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-950/50 backdrop-blur sticky top-0 z-40">
        <div className="container mx-auto px-6 py-4">
          <div className="flex items-center gap-4">
            <Link to="/">
              <Button variant="ghost" size="sm" className="text-slate-400 hover:text-slate-100">
                <ArrowLeft className="w-4 h-4 mr-2" />
                Back
              </Button>
            </Link>
            <div className="flex-1">
              <h1 className="text-xl font-bold">{video.name}</h1>
              <p className="text-sm text-slate-400">Real-time AI Compliance Audit</p>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="container mx-auto px-6 py-6">
        {/* Healing Status Bar */}
        <div className="mb-6">
          <HealingStatusBar currentStage={currentStage} progress={progress} />
        </div>

        {/* Main Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left Column - Video Player */}
          <div className="lg:col-span-2 space-y-6">
            <AuditMirror
              originalUrl={video.originalUrl}
              correctedUrl={video.correctedUrl}
              violations={violations}
              currentTime={currentTime}
              onTimeUpdate={setCurrentTime}
              onManualFlag={handleManualFlag}
            />

            <ThoughtStream logs={logs} />
          </div>

          {/* Right Column - Scorecard */}
          <div className="lg:col-span-1">
            <BrandSafetyScorecard violations={violations} grade={grade} videoName={video.name} />
          </div>
        </div>
      </main>

      {/* HITL Queue */}
      <HITLQueue violations={violations} onApprove={handleApprove} onReject={handleReject} />
    </div>
  );
}
