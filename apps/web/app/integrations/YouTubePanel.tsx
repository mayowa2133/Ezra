"use client";

import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { api, post, useApi } from "@/lib/api";

type Health = {
  account_id: number; handle: string; provider: string; status: string; channel_id: string | null;
  has_token: boolean; has_refresh_token: boolean; access_expires_at: string | null; scopes: string[];
  can_upload: boolean; can_read_analytics: boolean; can_manage: boolean; mode: string;
  live_check?: { ok: boolean; channel_title?: string; error?: string; error_code?: string };
};
type TestUpload = { dry_run?: boolean; video_id?: string; url?: string; privacy?: string; verified_on_youtube?: boolean;
  warnings?: { code: string; message: string }[] };

function Yes({ ok, label }: { ok: boolean; label: string }) {
  return <span style={{ marginRight: 14 }}>{ok ? "✓" : "✗"} {label}</span>;
}

function AccountCard({ id, onChange }: { id: number; onChange: () => void }) {
  const [live, setLive] = useState(false);
  const { data: h, error, reload } = useApi<Health>(`/accounts/${id}/health${live ? "?live=true" : ""}`);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<TestUpload | null>(null);

  async function run(fn: () => Promise<void>) {
    setErr(null);
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (!h) return <ErrorNote error={error} />;
  const reconnect = () => run(async () => {
    const out = await api<{ authorize_url: string }>("/integrations/youtube/connect");
    window.location.assign(out.authorize_url);
  });
  const disconnect = () => {
    if (!window.confirm(`Disconnect ${h.handle}? Ezra revokes its token at Google.`)) return;
    const purge = window.confirm("Also delete the YouTube metrics and API data Ezra stored for this account?");
    void run(async () => { await post(`/accounts/${id}/disconnect?purge=${purge}`); onChange(); });
  };
  const testUpload = () => {
    if (!window.confirm("Upload a 5-second generated test pattern to this channel as a PRIVATE video?")) return;
    void run(async () => setTest(await post<TestUpload>(`/accounts/${id}/test-upload`, { confirm: true })));
  };
  const expires = h.access_expires_at ? new Date(h.access_expires_at).toLocaleString() : "unknown";
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>{h.handle} <span className="muted small">{h.channel_id}</span></h3>
      <ErrorNote error={error ?? err} />
      <div className="small" style={{ lineHeight: 1.9 }}>
        <div><b>Status:</b> {h.status === "reconnect_required" ? <span style={{ color: "var(--bad, #c33)" }}>needs reconnecting</span> : h.status}</div>
        <div><b>OAuth:</b> <Yes ok={h.has_token} label="access token" /><Yes ok={h.has_refresh_token} label="refresh token" />
          <span className="muted">access token expires {expires} (refreshed automatically)</span></div>
        <div><b>Permissions:</b> <Yes ok={h.can_upload} label="upload" /><Yes ok={h.can_read_analytics} label="analytics" />
          <Yes ok={h.can_manage} label="edit/delete" />
          {!h.can_manage && <span className="muted">(edit/delete: set EZRA_YOUTUBE_MANAGE=1 and reconnect)</span>}</div>
        <div><b>Compliance:</b> {h.mode === "private only"
          ? <>development, <b>private uploads only</b> <span className="muted">until the Google project passes YouTube&apos;s API audit (then EZRA_YOUTUBE_PUBLIC_ALLOWED=1)</span></>
          : <>audited, public and scheduled uploads allowed</>}</div>
        {h.live_check && <div><b>Live check:</b> {h.live_check.ok ? `✓ ${h.live_check.channel_title ?? "reachable"}` : `✗ ${h.live_check.error_code}: ${h.live_check.error}`}</div>}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <button disabled={busy} onClick={() => { setLive(true); void reload(); }}>Check connection</button>
        <button disabled={busy || !h.can_upload} onClick={testUpload}>Test private upload</button>
        <button disabled={busy} onClick={reconnect}>Reconnect</button>
        <button disabled={busy} onClick={disconnect}>Disconnect</button>
      </div>
      {test && (
        <div className="small" style={{ marginTop: 10 }}>
          Uploaded <a href={test.url} target="_blank" rel="noreferrer">{test.video_id}</a> ({test.privacy}
          {test.verified_on_youtube ? ", confirmed on YouTube" : ""}).
          {test.warnings?.map((w) => <div key={w.code} className="muted">warning {w.code}: {w.message}</div>)}
        </div>
      )}
    </div>
  );
}

export function YouTubePanel({ accounts, configured, onChange }:
  { accounts: { id: number; provider: string; status: string }[]; configured: boolean; onChange: () => void }) {
  const yt = accounts.filter((a) => a.provider === "youtube" && a.status !== "disconnected");
  const [err, setErr] = useState<string | null>(null);
  async function connect() {
    setErr(null);
    try {
      const out = await api<{ authorize_url: string }>("/integrations/youtube/connect");
      window.location.assign(out.authorize_url);
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>YouTube</h2>
      <ErrorNote error={err} />
      {!configured && <p className="muted">Add the Google OAuth client first: set <code>YOUTUBE_CLIENT_SECRET_JSON</code> (see REMAINING_EXTERNAL_SETUP.md).</p>}
      {yt.length === 0 && configured && <p className="muted">No channel connected.</p>}
      {yt.map((a) => <AccountCard key={a.id} id={a.id} onChange={onChange} />)}
      <button disabled={!configured} onClick={() => void connect()}>{yt.length ? "Connect another channel" : "Connect YouTube"}</button>
      <p className="muted small">Ezra asks for upload, read-only and analytics access (plus edit/delete only when enabled). Nothing is posted without your approval. See docs/YOUTUBE-COMPLIANCE.md.</p>
    </div>
  );
}
