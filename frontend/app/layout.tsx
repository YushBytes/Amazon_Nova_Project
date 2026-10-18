import type { Metadata } from "next";
import { Inter } from "next/font/google"; // Added this import
import "./globals.css";
import { AuthProvider } from "./context/AuthContext"; // Added this import

const inter = Inter({ subsets: ["latin"] }); // Added this line

export const metadata: Metadata = {
  title: "NovaFlow Command Center",
  description: "Next-gen AI Agent Orchestration Pipeline",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.className} dark bg-background text-foreground antialiased`}>
        <AuthProvider>
          {children}
        </AuthProvider>
      </body>
    </html>
  );
}
