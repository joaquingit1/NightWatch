import type { Metadata } from "next";
import { Inter, Anton, IBM_Plex_Mono, Plus_Jakarta_Sans } from "next/font/google";
import "./globals.css";

import { LenisProvider } from "@/components/LenisProvider";

const inter = Inter({ subsets: ["latin"] });
const anton = Anton({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-display",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-mono",
});
const plusJakartaSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["600", "700", "800"],
  variable: "--font-heading",
});

export const metadata: Metadata = {
  title: "守夜犬 Night Watch",
  description: "Every AI makes you work more. This one makes you stop.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${inter.className} ${anton.variable} ${plexMono.variable} ${plusJakartaSans.variable}`}
    >
      <body suppressHydrationWarning className="bg-booth-bg text-booth-text antialiased">
        <LenisProvider>{children}</LenisProvider>
      </body>
    </html>
  );
}
