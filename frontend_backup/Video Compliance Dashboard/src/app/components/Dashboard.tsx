import { Link } from "react-router";
import { FileVideo, Play, CheckCircle, AlertTriangle } from "lucide-react";
import { mockVideos, calculateGrade } from "../data/mockData";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

export function Dashboard() {
  return (
    <div className="min-h-screen bg-slate-900 text-slate-100">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-950/50 backdrop-blur">
        <div className="container mx-auto px-6 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-gradient-to-br from-cyan-500 to-violet-500 rounded-lg">
                <FileVideo className="w-6 h-6 text-white" />
              </div>
              <div>
                <h1 className="text-xl font-bold">AI Guardian</h1>
                <p className="text-sm text-slate-400">Brand Safety & Compliance Platform</p>
              </div>
            </div>
            <div className="flex items-center gap-4">
              <Button variant="outline" className="border-slate-700 hover:bg-slate-800">
                Upload Video
              </Button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="container mx-auto px-6 py-8">
        {/* Stats Overview */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
          <Card className="bg-slate-800/50 border-slate-700">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium text-slate-400">Total Videos</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-slate-100">{mockVideos.length}</div>
            </CardContent>
          </Card>
          
          <Card className="bg-slate-800/50 border-slate-700">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium text-slate-400">Violations Found</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-cyan-400">
                {mockVideos.reduce((acc, v) => acc + v.violations.length, 0)}
              </div>
            </CardContent>
          </Card>

          <Card className="bg-slate-800/50 border-slate-700">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium text-slate-400">Violations Fixed</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-green-400">
                {mockVideos.reduce(
                  (acc, v) => acc + v.violations.filter((vio) => vio.status === "fixed").length,
                  0
                )}
              </div>
            </CardContent>
          </Card>

          <Card className="bg-slate-800/50 border-slate-700">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-medium text-slate-400">Avg. Safety Grade</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-violet-400">B+</div>
            </CardContent>
          </Card>
        </div>

        {/* Videos List */}
        <div>
          <h2 className="text-2xl font-bold mb-4">Videos</h2>
          <div className="space-y-4">
            {mockVideos.map((video) => (
              <Card key={video.id} className="bg-slate-800/30 border-slate-700 hover:border-cyan-500/50 transition-all">
                <CardContent className="p-6">
                  <div className="flex items-start justify-between">
                    <div className="flex gap-4 flex-1">
                      <div className="w-32 h-20 bg-slate-700 rounded-lg flex items-center justify-center">
                        <Play className="w-8 h-8 text-slate-500" />
                      </div>
                      
                      <div className="flex-1">
                        <h3 className="font-semibold text-lg mb-1">{video.name}</h3>
                        <p className="text-sm text-slate-400 mb-3">
                          Duration: {video.duration}s • Uploaded {video.uploadedAt.toLocaleDateString()}
                        </p>
                        
                        <div className="flex items-center gap-3 flex-wrap">
                          <Badge
                            variant="outline"
                            className={
                              video.status === "complete"
                                ? "border-green-500/50 text-green-400"
                                : video.status === "scanning"
                                ? "border-cyan-500/50 text-cyan-400"
                                : "border-yellow-500/50 text-yellow-400"
                            }
                          >
                            {video.status === "complete" && <CheckCircle className="w-3 h-3 mr-1" />}
                            {video.status === "scanning" && <AlertTriangle className="w-3 h-3 mr-1" />}
                            {video.status.toUpperCase()}
                          </Badge>

                          <div className="text-sm text-slate-400">
                            {video.violations.length} violation{video.violations.length !== 1 ? "s" : ""} detected
                          </div>

                          {video.grade && (
                            <Badge
                              className={
                                video.grade === "A"
                                  ? "bg-green-500/20 text-green-400"
                                  : video.grade === "B"
                                  ? "bg-blue-500/20 text-blue-400"
                                  : "bg-yellow-500/20 text-yellow-400"
                              }
                            >
                              Grade: {video.grade}
                            </Badge>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex gap-2">
                      <Link to={`/audit/${video.id}`}>
                        <Button className="bg-gradient-to-r from-cyan-500 to-violet-500 hover:from-cyan-600 hover:to-violet-600">
                          Open Audit
                        </Button>
                      </Link>
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
