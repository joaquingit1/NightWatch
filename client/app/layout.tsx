import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

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
    <html lang="zh-CN" suppressHydrationWarning className={inter.className}>
      <body suppressHydrationWarning className="bg-booth-bg text-booth-text antialiased">
        {children}
      </body>
    </html>
  );
}
