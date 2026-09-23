"use client";

import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { api, useApi } from "@/lib/api";

type Integrations = {
  publishers: { provider: string; platforms: string[]; oauth: boolean; configured: boolean; env_var: string | null }[];
  secrets_present: string[];
  accounts: { id: number; platform: string; provider: string; handle: string; has_credential: boolean }[];
  llm: string; transcriber: string; diarizer: string; face_detector: string;
};

export default function IntegrationsPage() {
  const { data, error } = useApi<Integrations>("/integrations");
  const [err, setErr] = useState<string | null>(null);

  async function connect(provider: string) {
    setErr(null);
    try {
      const out = await api<{ authorize_url: string }>(`/integrations/${provider}/connect`);
      window.location.assign(out.authorize_url);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <>
      <h1>Integrations</h1>
      <ErrorNote error={error ?? err} />
      {data && (
        <>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Publishing</h2>
            <table>
              <thead><tr><th>provider</th><th>platforms</th><th>app credentials</th><th></th></tr></thead>
              <tbody>{data.publishers.map((p) => (
                <tr key={p.provider}>
                  <td>{p.provider}</td><td>{p.platforms.join(", ")}</td>
                  <td>{p.configured ? "configured" : <span className="muted">missing: set <code>{p.env_var}</code> (REMAINING_EXTERNAL_SETUP.md)</span>}</td>
                  <td>{p.oauth && <button disabled={!p.configured} onClick={() => connect(p.provider)}>Connect account</button>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Connected accounts</h2>
            <table><tbody>{data.accounts.map((a) => (
              <tr key={a.id}><td>{a.platform}</td><td>@{a.handle}</td><td className="muted">{a.provider}</td>
                <td>{a.has_credential || a.provider === "local-export" ? "ready" : "needs credential"}</td></tr>
            ))}</tbody></table>
          </div>
          <div className="panel small">
            <b>Models &amp; analysis:</b> transcription {data.transcriber} · diarization {data.diarizer} · faces {data.face_detector} · judgment {data.llm}
            <div className="muted">Secrets present (names only): {data.secrets_present.join(", ") || "none"}</div>
          </div>
        </>
      )}
    </>
  );
}
