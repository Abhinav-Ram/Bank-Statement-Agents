import { useEffect, useState } from "react";

type Health = {
  status: string;
  chat_model: string;
  db: { ok: boolean; extensions?: string[]; error?: string };
  llm: { ok: boolean; models?: string[]; error?: string };
};

// Phase 0 placeholder: shows that web -> API -> (Postgres, oMLX) is wired up.
export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "40px auto", padding: "0 16px" }}>
      <h1>Bank Statement Agents</h1>
      <p>Phase 0 scaffold: service health.</p>
      {err && <p style={{ color: "crimson" }}>API unreachable: {err}</p>}
      {!health && !err && <p>Checking…</p>}
      {health && (
        <ul>
          <li>API: {health.status}</li>
          <li>Postgres: {health.db.ok ? `ok (${health.db.extensions?.join(", ")})` : health.db.error}</li>
          <li>LLM ({health.chat_model}): {health.llm.ok ? "ok" : health.llm.error ?? "model not listed"}</li>
        </ul>
      )}
    </main>
  );
}
