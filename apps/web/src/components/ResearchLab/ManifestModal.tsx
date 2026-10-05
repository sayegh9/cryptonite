"use client";

import React from "react";
import { ShieldCheck } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  runId: string;
  runConfigHash: string;
  catalogManifest: any[];
  artifactManifest: Record<string, any>;
}

export const ManifestModal: React.FC<Props> = ({
  isOpen,
  onClose,
  runId,
  runConfigHash,
  catalogManifest,
  artifactManifest,
}) => {
  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="bg-zinc-950 border border-zinc-800 w-full max-w-3xl max-h-[85vh] flex flex-col font-mono text-2xs shadow-2xl p-0 gap-0 text-zinc-100">
        {/* Modal Header */}
        <DialogHeader className="flex flex-row items-center justify-between px-4 py-3 border-b border-zinc-800 bg-zinc-900/60 m-0">
          <div className="flex items-center space-x-2">
            <ShieldCheck className="w-4 h-4 text-cyan-400" />
            <DialogTitle className="font-bold text-sm text-zinc-100 m-0">REPRODUCIBILITY & DATA INTEGRITY MANIFEST</DialogTitle>
          </div>
          <DialogDescription className="sr-only">Detailed run configuration hashes and catalog files.</DialogDescription>
        </DialogHeader>

        {/* Modal Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Hashes */}
          <div className="grid grid-cols-1 gap-2 bg-zinc-900/40 border border-zinc-800/80 rounded p-3">
            <div>
              <span className="text-zinc-400 text-2xs block">RUN ID</span>
              <span className="text-zinc-200 font-bold select-all">{runId}</span>
            </div>
            <div>
              <span className="text-zinc-400 text-2xs block">RUN CONFIG HASH (Resolved Params + Intrabar Order + Profile)</span>
              <span className="text-cyan-400 font-bold select-all break-all">{runConfigHash}</span>
            </div>
          </div>

          {/* Input Data Catalog Manifest */}
          <div>
            <h3 className="font-bold text-zinc-300 mb-1.5 flex items-center space-x-1.5">
              <span>Input Data Catalog Files (Nautilus ParquetDataCatalog)</span>
              <span className="text-2xs text-zinc-500">({catalogManifest?.length || 0} files)</span>
            </h3>
            <div className="border border-zinc-800 rounded overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-zinc-900/60 border-b border-zinc-800 text-zinc-400 text-2xs">
                    <th className="py-1 px-2.5">Relative Path</th>
                    <th className="py-1 px-2 text-right">Size (Bytes)</th>
                    <th className="py-1 px-2.5">SHA-256 Checksum</th>
                  </tr>
                </thead>
                <tbody>
                  {catalogManifest && catalogManifest.length > 0 ? (
                    catalogManifest.map((f, i) => (
                      <tr key={i} className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                        <td className="py-1 px-2.5 text-zinc-300 font-mono select-all">{f.path}</td>
                        <td className="py-1 px-2 text-right text-zinc-400">{f.size_bytes?.toLocaleString()}</td>
                        <td className="py-1 px-2.5 text-cyan-400 font-mono text-2xs select-all break-all">
                          {f.sha256}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={3} className="py-3 text-center text-zinc-500">
                        No catalog files recorded.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* Generated Artifacts Manifest */}
          <div>
            <h3 className="font-bold text-zinc-300 mb-1.5">Immutable Output Artifacts (ZSTD Parquet / JSON)</h3>
            <div className="border border-zinc-800 rounded overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-zinc-900/60 border-b border-zinc-800 text-zinc-400 text-2xs">
                    <th className="py-1 px-2.5">Artifact File</th>
                    <th className="py-1 px-2 text-right">Size</th>
                    <th className="py-1 px-2.5">SHA-256 Checksum</th>
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    const filesMap = artifactManifest?.files || (artifactManifest && typeof artifactManifest === "object" ? artifactManifest : {});
                    const entries = Object.entries(filesMap).filter(([, v]) => typeof v === "object" && v !== null && ("sha256" in v || "size_bytes" in v));
                    return entries.length > 0 ? (
                      entries.map(([filename, meta]: [string, any]) => (
                        <tr key={filename} className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                          <td className="py-1 px-2.5 text-emerald-400 font-semibold">{filename}</td>
                          <td className="py-1 px-2 text-right text-zinc-400">
                            {meta?.size_bytes ? `${meta.size_bytes.toLocaleString()} B` : "-"}
                          </td>
                          <td className="py-1 px-2.5 text-cyan-400 font-mono text-2xs select-all break-all">
                            {meta?.sha256 || "-"}
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={3} className="py-3 text-center text-zinc-500">
                          No artifacts recorded.
                        </td>
                      </tr>
                    );
                  })()}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};
