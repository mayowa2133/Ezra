"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import type { Compliance, Job } from "@/lib/types";

const LINKS: [string, string][] = [
  ["/", "Dashboard"], ["/campaigns", "Campaigns"], ["/sources", "Sources"], ["/clips", "Clips"],
  ["/review", "Review"], ["/publishing", "Publishing"], ["/analytics", "Analytics"], ["/earnings", "Earnings"],
  ["/integrations", "Integrations"], ["/settings", "Settings"],
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="nav">
      <div className="brand">Ezra</div>
      {LINKS.map(([href, label]) => (
        <Link key={href} href={href} className={(href === "/" ? path === "/" : path.startsWith(href)) ? "active" : ""}>
          {label}
        </Link>
      ))}
    </nav>
  );
}

export function Badge({ value }: { value: string | null | undefined }) {
  return <span className={`badge ${value ?? ""}`}>{value === "REVIEW_REQUIRED" ? "REVIEW" : value ?? "–"}</span>;
}

export function ComplianceBox({ c }: { c: Compliance }) {
  return (
    <div>
      <Badge value={c.status} />
      {c.reasons.length > 0 && (
        <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {c.reasons.map((r, i) => (
            <li key={i}>
              <b className={r.outcome === "fail" ? "" : "muted"}>{r.outcome}</b> {r.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ScoreBars({ scores, why }: { scores: Record<string, number | null>; why?: Record<string, string> }) {
  return (
    <div className="scores">
      {Object.entries(scores).map(([k, v]) => (
        <div key={k} style={{ display: "contents" }} title={why?.[k] ?? ""}>
          <span className="muted">{k.replace("_", " ")}</span>
          <div className="bar"><div style={{ width: `${v ?? 0}%` }} /></div>
          <span>{v === null ? "–" : v.toFixed(0)}</span>
        </div>
      ))}
    </div>
  );
}

export function JobBar({ job }: { job: Job | null }) {
  if (!job) return null;
  return (
    <div className="panel small">
      <div className="row">
        <b>job {job.id}</b> <span>{job.kind}</span> <Badge value={job.status} />
        <span className="muted">{job.message}</span>
      </div>
      <div className="bar" style={{ marginTop: 6 }}><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
      {job.error && <div className="error" style={{ marginTop: 8 }}>{job.error.type}: {job.error.message}</div>}
    </div>
  );
}

export function Stat({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="panel stat">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}
