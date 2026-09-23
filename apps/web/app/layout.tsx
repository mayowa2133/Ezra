import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/ui";

import "./globals.css";

export const metadata: Metadata = { title: "Ezra", description: "Clip, review, publish and learn" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Nav />
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
