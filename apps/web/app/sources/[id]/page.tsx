"use client";

import { use, useState } from "react";

import { ErrorNote, JobBar } from "@/components/ui";
import { post, useApi, useJob } from "@/lib/api";
import type { Job, Source } from "@/lib/types";

type Page = { text: string; next_start: number | null; language: string | null };

export default function SourcePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: s, error, reload } = useApi<Source>(`/sources/${id}`);
  const [start, setStart] = useState(0);
  const { data: tr } = useApi<Page>(s?.status === "analyzed" ? `/sources/${id}/transcript?start=${start}&max_chars=12000` : null);
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJob(jobId, () => void reload());
  const [err, setErr] = useState<string | null>(null);

  async function start_(path: string, body: unknown = {}) {
    try {
      setJobId((await post<Job>(path, body)).id);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (!s) return <p className="muted">Loading…</p>;
  const products = s.analysis?.products ?? {};
  return (
    <>
      <div className="row"><h1>{s.title}</h1><span className="spacer" />
        <button onClick={() => start_(`/sources/${id}/analyze`)}>Analyze</button>
        <button className="primary" onClick={() => start_(`/sources/${id}/candidates`, {})}>Find candidates</button></div>
      <ErrorNote error={err} />
      <JobBar job={job} />
      <div className="grid cols-2">
        <div className="panel">
          {s.media_url && <video src={s.media_url} controls style={{ width: "100%", borderRadius: 8 }} />}
          <p className="small muted">rights: {s.rights_status} ({s.rights_basis}) · {(s.duration / 60).toFixed(1)} min · {s.width}×{s.height}</p>
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Analysis</h2>
          <table><tbody>{Object.entries(products).map(([k, v]) => (
            <tr key={k}><td><b>{k}</b></td><td className="small"><pre>{JSON.stringify(v, null, 1)}</pre></td></tr>
          ))}</tbody></table>
          {Object.keys(products).length === 0 && <p className="muted">Not analyzed yet.</p>}
        </div>
      </div>
      {tr && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Transcript</h2>
          <pre className="small">{tr.text}</pre>
          <div className="row" style={{ marginTop: 8 }}>
            {start > 0 && <button onClick={() => setStart(0)}>From start</button>}
            {tr.next_start !== null && <button onClick={() => setStart(tr.next_start ?? 0)}>More</button>}
          </div>
        </div>
      )}
    </>
  );
}
