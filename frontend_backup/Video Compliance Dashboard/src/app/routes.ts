import { createBrowserRouter } from "react-router";
import { Dashboard } from "./components/Dashboard";
import { VideoAudit } from "./components/VideoAudit";

export const router = createBrowserRouter([
  {
    path: "/",
    Component: Dashboard,
  },
  {
    path: "/audit/:videoId",
    Component: VideoAudit,
  },
]);
