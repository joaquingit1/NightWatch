export default function FormLayout({ children }: { children: React.ReactNode }) {
  return <div className="h-screen overflow-y-auto">{children}</div>;
}
