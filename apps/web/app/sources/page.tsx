"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, ErrorNote } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import type { Campaign, Source } from "@/lib/types";

export default function Sources() {
  const { data, error, reload } = useApi<Source[]>("/sources", 5000);
  const { data: camps } = useApi<Campaign[]>("/campaigns");
  const [file, setFile] = useState<File | null>(null);
  const [campaign, setCampaign] = useState("");
  const [rights, setRights] = useState("campaign_supplied");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function upload() {
    if (!file) return;
    setBusy(true);
    setErr(null);
    const fd = new FormData();
    fd.append("file", file);
    if (campaign) fd.append("campaign", campaign);
    fd.append("rights_basis", rights);
    try {
      await api<Source>("/sources", { method: "POST", body: fd });
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Sources</h1>
      <ErrorNote error={error} />
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Add authorized footage</h2>
        <div className="grid cols-3">
          <div><label>Video file</label><input type="file" accept="video/*,audio/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></div>
          <div><label>Campaign</label><select value={campaign} onChange={(e) => setCampaign(e.target.value)}>
            <option value="">(none)</option>{camps?.map((c) => <option key={c.slug} value={c.slug}>{c.name}</option>)}
          </select></div>
          <div><label>Rights basis</label><select value={rights} onChange={(e) => setRights(e.target.value)}>
            {["campaign_supplied", "owned", "licensed", "permission", "unknown"].map((r) => <option key={r}>{r}</option>)}
          </select></div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" disabled={!file || busy} onClick={upload}>{busy ? "Uploading…" : "Upload"}</button>
          <span className="muted small">Unknown rights make every clip from this source REVIEW_REQUIRED.</span>
        </div>
        <ErrorNote error={err} />
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>#</th><th>title</th><th>length</th><th>resolution</th><th>status</th><th>rights</th><th>candidates</th></tr></thead>
          <tbody>{data?.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td><td><Link href={`/sources/${s.id}`}>{s.title}</Link></td>
              <td>{(s.duration / 60).toFixed(1)} min</td><td>{s.width}×{s.height}</td>
              <td><Badge value={s.status} /></td><td>{s.rights_status} <span className="muted small">{s.rights_basis}</span></td>
              <td>{s.n_candidates}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}
