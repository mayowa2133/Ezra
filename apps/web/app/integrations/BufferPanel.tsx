"use client";

import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { api, post } from "@/lib/api";

type Channel = {
  channel_id: string; platform: string; handle: string; organization: string;
  connected: boolean; locked: boolean; queue_paused: boolean;
};

export function BufferPanel({ configured, cloudinaryConfigured, onChange }: {
  configured: boolean; cloudinaryConfigured: boolean; onChange: () => void;
}) {
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [cloud, setCloud] = useState({ cloud_name: "", api_key: "", api_secret: "" });

  async function saveKey() {
    setBusy(true); setError(null); setMessage(null);
    try {
      await post("/integrations/buffer/credentials", { api_key: key });
      setKey(""); setMessage("Buffer API key saved in Ezra’s encrypted secret store."); onChange();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function discover() {
    setBusy(true); setError(null); setMessage(null);
    try {
      setChannels(await api<Channel[]>("/integrations/buffer/channels"));
      setSelected([]);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function saveHosting() {
    setBusy(true); setError(null); setMessage(null);
    try {
      await post("/integrations/buffer/media-credentials", cloud);
      setCloud({ cloud_name: "", api_key: "", api_secret: "" });
      setMessage("Cloudinary credentials saved in Ezra’s encrypted secret store."); onChange();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function connect() {
    setBusy(true); setError(null);
    try {
      const accounts = await post<unknown[]>("/integrations/buffer/connect", {
        channel_ids: selected, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      setMessage(`${accounts.length} Buffer account(s) connected. Approve a clip, then preview its publishing plan.`);
      onChange();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Buffer</h2>
      <p>Connect your social accounts in <a href="https://publish.buffer.com" target="_blank" rel="noreferrer">Buffer</a>,
        then import them here. Buffer handles the platform permissions and scheduled delivery.</p>
      <p>{configured ? "Buffer API key is configured. You can replace it below." : "Create an API key in Buffer’s settings and paste it below."}
        {" "}Ezra encrypts it locally and never displays it again.</p>
      <label>Buffer API key <input type="password" autoComplete="off" value={key}
        onChange={(e) => setKey(e.target.value)} /></label>{" "}
      <button disabled={busy || !key.trim()} onClick={() => void saveKey()}>Save API key</button>
      <p className="muted">Video publishing also needs Cloudinary or public S3 media hosting.
        Ezra checks this before queuing a post. Your clips still require approval.</p>
      <ErrorNote error={error} />
      <button disabled={!configured || busy} onClick={() => void discover()}>Find Buffer accounts</button>
      {channels && <>
        {channels.length === 0 && <p>No supported accounts found. Connect them in Buffer, then try again.</p>}
        <table><thead><tr><th></th><th>platform</th><th>account</th><th>workspace</th><th>status</th></tr></thead>
          <tbody>{channels.map((c) => <tr key={c.channel_id}>
            <td><input type="checkbox" aria-label={`Select ${c.platform} ${c.handle}`}
              disabled={busy || !c.connected || c.locked || c.queue_paused}
              checked={selected.includes(c.channel_id)} onChange={(e) => setSelected(e.target.checked
                ? [...selected, c.channel_id] : selected.filter((id) => id !== c.channel_id))} /></td>
            <td>{c.platform}</td><td>{c.handle}</td><td>{c.organization}</td>
            <td>{!c.connected ? "Reconnect in Buffer" : c.locked ? "Locked" : c.queue_paused ? "Queue paused" : "Ready"}</td>
          </tr>)}</tbody></table>
        <button disabled={busy || selected.length === 0} onClick={() => void connect()}>Connect selected accounts</button>
      </>}
      <details style={{ marginTop: 16 }}>
        <summary>Video hosting {cloudinaryConfigured ? "— Cloudinary configured" : "— set up Cloudinary"}</summary>
        <p>Buffer fetches videos from a public link. Connect <a href="https://cloudinary.com/pricing" target="_blank"
          rel="noreferrer">Cloudinary</a> to host approved clips when you publish. Copy these credentials from its dashboard.
          Ezra encrypts them locally. Existing public S3 hosting can also be configured in Ezra’s environment.</p>
        <div><label>Cloudinary cloud name <input value={cloud.cloud_name} autoComplete="off"
          onChange={(e) => setCloud({ ...cloud, cloud_name: e.target.value })} /></label></div>
        <div><label>Cloudinary API key <input type="password" value={cloud.api_key} autoComplete="off"
          onChange={(e) => setCloud({ ...cloud, api_key: e.target.value })} /></label></div>
        <div><label>Cloudinary API secret <input type="password" value={cloud.api_secret} autoComplete="off"
          onChange={(e) => setCloud({ ...cloud, api_secret: e.target.value })} /></label></div>
        <button disabled={busy || Object.values(cloud).some((value) => !value.trim())}
          onClick={() => void saveHosting()}>Save video hosting</button>
      </details>
      {message && <p role="status">{message}</p>}
    </div>
  );
}
