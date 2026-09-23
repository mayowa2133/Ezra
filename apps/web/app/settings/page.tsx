"use client";

import { ErrorNote } from "@/components/ui";
import { useApi } from "@/lib/api";

type Audit = { id: number; at: string; actor: string; action: string; entity_type: string; entity_id: string | null };

export default function SettingsPage() {
  const { data, error } = useApi<Record<string, unknown>>("/settings");
  const { data: audit } = useApi<Audit[]>("/audit?limit=50");
  return (
    <>
      <h1>Settings</h1>
      <ErrorNote error={error} />
      <div className="panel">
        <p className="small muted">Configured through EZRA_* environment variables (see .env.example). Secrets are never shown.</p>
        <table><tbody>{data && Object.entries(data).map(([k, v]) => (
          <tr key={k}><td>{k}</td><td><code>{JSON.stringify(v)}</code></td></tr>
        ))}</tbody></table>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Audit log</h2>
        <table><tbody>{audit?.map((a) => (
          <tr key={a.id}><td className="small">{a.at}</td><td>{a.actor}</td><td>{a.action}</td><td>{a.entity_type} {a.entity_id}</td></tr>
        ))}</tbody></table>
      </div>
    </>
  );
}
