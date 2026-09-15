export default function OperatorLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col h-screen w-screen bg-zinc-950 text-zinc-100 font-sans">
      {/* Operator Header */}
      <header className="h-14 flex-none bg-zinc-900 border-b border-zinc-800 flex items-center justify-between px-6 z-20 shadow-md">
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-2">
            <div className="w-8 h-8 rounded bg-red-600 flex items-center justify-center font-black text-white shadow-inner">
              OP
            </div>
            <span className="font-extrabold text-lg tracking-wider text-red-500">
              OPERATOR CONTROL
            </span>
          </div>
        </div>
      </header>

      <main className="flex-1 flex min-h-0 min-w-0 overflow-auto bg-zinc-950 p-6">
        {children}
      </main>
    </div>
  );
}
