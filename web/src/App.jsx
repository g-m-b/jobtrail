import React, { useEffect, useMemo, useState } from "react";
import { BySource, Funnel, Legend, Monthly, OUTCOMES } from "./charts.jsx";

const STAGES = [
  "applied",
  "acknowledged",
  "recruiter_screen",
  "assessment",
  "interview",
  "final",
  "offer",
];
const STATUSES = ["active", "rejected", "offer", "withdrawn", "ghosted"];

const OUTCOME_BY_KEY = Object.fromEntries(OUTCOMES.map((o) => [o.key, o]));
const pillFor = (status) =>
  OUTCOME_BY_KEY[status] || { label: status, color: "var(--text-muted)" };

async function request(path, options) {
  const res = await fetch(`/api${path}`, options);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail.slice(0, 200)}` : ""}`);
  }
  // 204 and restarts-in-progress both yield an empty body; .json() would throw.
  const body = await res.text();
  return body ? JSON.parse(body) : null;
}

const api = {
  get: (p) => request(p),
  patch: (p, body) =>
    request(p, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  post: (p, body) =>
    request(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }),
  del: (p) => request(p, { method: "DELETE" }),
};

function useTheme() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "system"
  );
  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") el.removeAttribute("data-theme");
    else el.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);
  return [theme, setTheme];
}

function relative(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 90) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function Kpi({ label, value, note }) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

function Drawer({ id, onClose, onChanged }) {
  const [app, setApp] = useState(null);
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);

  const closeRef = React.useRef(null);
  const focused = React.useRef(false);
  // Remember what opened the drawer so focus can go back there on close.
  const opener = React.useRef(typeof document !== "undefined" ? document.activeElement : null);

  useEffect(() => {
    api.get(`/applications/${id}`).then((a) => {
      setApp(a);
      setDraft({ stage: a.stage, status: a.status, notes: a.notes || "" });
    });
  }, [id]);

  // Move focus INTO the dialog once it has rendered. Calling focus() inside the fetch
  // .then() runs before React commits the button, so it silently did nothing and left
  // the keyboard user stranded behind the scrim.
  useEffect(() => {
    if (app && !focused.current) {
      focused.current = true;
      closeRef.current?.focus();
    }
  }, [app]);

  // Return focus to the row that opened this, or the user lands back at the top of the page.
  useEffect(() => () => opener.current?.focus?.(), []);

  // Escape closes — a modal you can only dismiss with the mouse is a keyboard trap.
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!app) return null;
  const dirty =
    draft.stage !== app.stage ||
    draft.status !== app.status ||
    (draft.notes || "") !== (app.notes || "");

  const save = async () => {
    setSaving(true);
    const updated = await api.patch(`/applications/${id}`, draft);
    setApp(updated);
    setSaving(false);
    onChanged();
  };

  const remove = async () => {
    await api.del(`/applications/${id}`);
    onChanged();
    onClose();
  };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`${app.company} — ${app.role}`}
      >
        <button ref={closeRef} className="close" onClick={onClose} aria-label="Close details">
          ×
        </button>
        <h3>{app.company}</h3>
        <div style={{ color: "var(--text-secondary)", marginBottom: 4 }}>{app.role}</div>
        <div style={{ color: "var(--text-muted)", fontSize: 12 }}>
          {app.initiator === "inbound" ? "Recruiter reached out" : "I applied"} ·{" "}
          {app.source} · first activity {app.applied_on}
          {app.manual ? " · edited by you" : ""}
        </div>

        <div className="sec-title">Stage &amp; outcome</div>
        <div className="row-2">
          <div className="field">
            <label htmlFor="stage">Furthest stage</label>
            <select
              id="stage"
              value={draft.stage}
              onChange={(e) => setDraft({ ...draft, stage: e.target.value })}
            >
              {STAGES.map((s) => (
                <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="status">Outcome</label>
            <select
              id="status"
              value={draft.status}
              onChange={(e) => setDraft({ ...draft, status: e.target.value })}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="notes">Notes — rounds held by phone or in person go here</label>
          <textarea
            id="notes"
            value={draft.notes}
            placeholder="e.g. cleared L1 + L2 over call, awaiting manager round"
            onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
          />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={save} disabled={!dirty || saving}>
            {saving ? "Saving…" : dirty ? "Save" : "Saved"}
          </button>
          <button className="btn ghost" onClick={onClose}>Close</button>
          <button className="btn danger" style={{ marginLeft: "auto" }} onClick={remove}>
            Delete
          </button>
        </div>

        {app.contacts?.length > 0 && (
          <>
            <div className="sec-title">HR contacts</div>
            {app.contacts.map((c) => (
              <div key={c.email} style={{ marginBottom: 6 }}>
                <div style={{ fontSize: 13 }}>{c.name || c.email}</div>
                {c.email && (
                  <a href={`mailto:${c.email}`} style={{ fontSize: 12 }}>{c.email}</a>
                )}
              </div>
            ))}
          </>
        )}

        <div className="sec-title">Email timeline ({app.events.length})</div>
        <ul className="timeline">
          {app.events.map((e) => (
            <li key={e.id}>
              <span className="when">{e.date}</span>
              <span>
                <div className="what">{e.subject}</div>
                <div className="who">
                  {e.is_from_me ? <span className="me">you →</span> : null} {e.sender}
                  {" · "}
                  {e.stage.replace(/_/g, " ")}
                </div>
              </span>
            </li>
          ))}
        </ul>
      </aside>
    </>
  );
}

export default function App() {
  const [apps, setApps] = useState([]);
  const [stats, setStats] = useState(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState({ key: "applied_on", dir: -1 });
  const [openId, setOpenId] = useState(null);
  const [theme, setTheme] = useTheme();
  const [syncing, setSyncing] = useState(false);
  const [upcoming, setUpcoming] = useState([]);
  const [syncInfo, setSyncInfo] = useState(null);
  const [apiError, setApiError] = useState(null);

  const reload = () =>
    Promise.all([
      api.get("/applications"),
      api.get("/stats"),
      api.get("/upcoming"),
      api.get("/sync"),
    ])
      .then(([a, s, u, sy]) => {
        setApps(a);
        setStats(s);
        setUpcoming(u);
        setSyncInfo(sy);
        setApiError(null);
      })
      .catch((e) => setApiError(e.message));

  useEffect(() => { reload(); }, []);

  // The scheduler pulls in the background; poll so the page reflects it without a
  // manual refresh. Cheap: /sync is served from memory.
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const sy = await api.get("/sync");
        setApiError(null);
        setSyncInfo((prev) => {
          if (prev && sy.cache_version !== prev.cache_version) reload();
          return sy;
        });
      } catch (e) {
        // The API restarts whenever the scheduler process does. Show it, don't crash.
        setApiError(e.message);
      }
    }, 60000);
    return () => clearInterval(t);
  }, []);

  const syncNow = async () => {
    setSyncing(true);
    try {
      await api.post("/sync");
      await reload();
    } catch (e) {
      setApiError(e.message);
    } finally {
      setSyncing(false);
    }
  };

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const out = apps.filter(
      (a) =>
        (!status || a.status === status) &&
        (!needle ||
          a.company.toLowerCase().includes(needle) ||
          a.role.toLowerCase().includes(needle))
    );
    const { key, dir } = sort;
    return [...out].sort((x, y) => {
      const a = key === "stage" ? x.stage_rank : x[key] ?? "";
      const b = key === "stage" ? y.stage_rank : y[key] ?? "";
      return a < b ? -dir : a > b ? dir : 0;
    });
  }, [apps, q, status, sort]);

  const th = (key, label) => {
    const active = sort.key === key;
    const toggle = () => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
    return (
      <th
        scope="col"
        aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
        tabIndex={0}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        {label}{active ? (sort.dir === 1 ? " ↑" : " ↓") : ""}
      </th>
    );
  };

  if (!stats)
    return (
      <div className="wrap">
        {apiError ? (
          <div className="card" role="alert">
            <h2>Can&rsquo;t reach the API</h2>
            <p className="hint">{apiError}</p>
            <p className="hint">Is it running? <code>uvicorn jobtrail.api:app --port 8000</code></p>
          </div>
        ) : (
          "Loading…"
        )}
      </div>
    );

  return (
    <div className="wrap">
      <a className="skip-link" href="#applications">Skip to applications</a>
      <header className="top">
        <h1>Job Applications</h1>
        <span className="sub">
          {stats.total} tracked · {stats.by_month[0]?.month} → {stats.by_month.at(-1)?.month}
        </span>
        <span className="sync-state" title={apiError || syncInfo?.last_error || ""}>
          {apiError ? (
            <><i className="dot bad" /> API unreachable</>
          ) : syncInfo?.last_error ? (
            <><i className="dot bad" /> sync failed</>
          ) : (
            <>
              <i className="dot ok" /> synced {relative(syncInfo?.last_sync)}
              {syncInfo?.enabled ? ` · every ${syncInfo.interval_hours}h` : " · scheduler off"}
            </>
          )}
        </span>
        <button className="theme-toggle" onClick={syncNow} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync now"}
        </button>
        <button
          className="theme-toggle"
          style={{ marginLeft: 0 }}
          aria-label={`Theme: ${theme}. Click to change.`}
          onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")}
        >
          Theme: {theme}
        </button>
      </header>

      <div className="kpis">
        <Kpi label="Applications sent" value={stats.outbound} note="I applied" />
        <Kpi label="Recruiters approached me" value={stats.inbound} note="inbound" />
        <Kpi
          label="Response rate"
          value={`${stats.response_rate}%`}
          note={`${stats.responded} of ${stats.outbound} outbound`}
        />
        <Kpi
          label="Reached interview"
          value={stats.interviewed}
          note={`${stats.interview_rate}% of all`}
        />
        <Kpi label="Offers" value={stats.offers} note="incl. 1 declined" />
        <Kpi
          label="Median days to reply"
          value={stats.median_days_to_response ?? "—"}
          note="past auto-acknowledgement"
        />
        <Kpi label="Still active" value={stats.active} note={`${stats.ghosted} went quiet`} />
      </div>

      {upcoming.length > 0 && (
        <section className="card upcoming" style={{ marginBottom: 16 }}>
          <h2>Upcoming interviews</h2>
          <p className="hint">Scheduled and still in the future.</p>
          <ul className="upcoming-list">
            {upcoming.map((u) => (
              <li key={u.id}>
                <button className="linkish" onClick={() => setOpenId(u.id)}>
                  <strong>{u.company}</strong> <span className="role">{u.role}</span>
                </button>
                <time dateTime={u.interview_at}>
                  {new Date(u.interview_at).toLocaleString(undefined, {
                    weekday: "short", day: "numeric", month: "short",
                    hour: "numeric", minute: "2-digit",
                  })}
                </time>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="grid-2">
        <section className="card">
          <h2>Pipeline funnel</h2>
          <p className="hint">How far each application got — furthest stage reached, not current state.</p>
          <Funnel data={stats.funnel} total={stats.total} />
        </section>

        <section className="card">
          <h2>Activity by month</h2>
          <p className="hint">Applications started each month, coloured by where they ended up.</p>
          <Monthly data={stats.by_month} />
          <Legend items={OUTCOMES} />
        </section>
      </div>

      <div className="grid-side">
        <section className="card">
          <h2>Which sources actually reply</h2>
          <p className="hint">
            Outbound applications only — a recruiter emailing you first is not a response to
            anything, so those are excluded here. Bars are dimmed and the number in brackets is
            how many you sent; under 3, the rate is noise.
          </p>
          <BySource data={stats.by_source} />
          <div className="note-box">
            {(() => {
              const worst = [...stats.by_source].filter((s) => s.total >= 3).sort((a, b) => a.response_rate - b.response_rate)[0];
              const best = [...stats.by_source].filter((s) => s.total >= 3).sort((a, b) => b.response_rate - a.response_rate)[0];
              if (!worst || !best || worst.source === best.source) return "Not enough outbound volume per source to compare yet.";
              return `${best.source} replies to ${best.response_rate}% of what you send (${best.total} sent). ${worst.source} replies to ${worst.response_rate}% (${worst.total} sent).`;
            })()}
          </div>
        </section>

        <section className="card">
          <h2>Outcomes</h2>
          <p className="hint">Every tracked application by final state.</p>
          <table>
            <tbody>
              {OUTCOMES.map((o) => (
                <tr key={o.key} style={{ cursor: "default" }}>
                  <td>
                    <span className="pill">
                      <i className="dot" style={{ background: o.color }} />
                      {o.label}
                    </span>
                  </td>
                  <td className="num">{stats.by_status[o.key] || 0}</td>
                  <td className="num">
                    {Math.round((100 * (stats.by_status[o.key] || 0)) / stats.total)}%
                  </td>
                </tr>
              ))}
              {stats.by_status.withdrawn ? (
                <tr style={{ cursor: "default" }}>
                  <td><span className="pill"><i className="dot" style={{ background: "var(--text-muted)" }} />Withdrawn</span></td>
                  <td className="num">{stats.by_status.withdrawn}</td>
                  <td className="num">{Math.round((100 * stats.by_status.withdrawn) / stats.total)}%</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </section>
      </div>

      <section className="card" id="applications">
        <h2>All applications</h2>
        <p className="hint">
          Click a row to see its email timeline, HR contacts, and to correct anything email
          could not see.
        </p>
        <div className="controls">
          <input
            type="search"
            aria-label="Search company or role"
            placeholder="Search company or role…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            aria-label="Filter by outcome"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">All outcomes</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <span className="count">{rows.length} shown</span>
        </div>
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                {th("company", "Company")}
                {th("stage", "Stage reached")}
                {th("status", "Outcome")}
                {th("source", "Source")}
                {th("applied_on", "First activity")}
                {th("event_count", "Emails")}
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const p = pillFor(a.status);
                return (
                  <tr
                    key={a.id}
                    tabIndex={0}
                    role="button"
                    aria-label={`${a.company}, ${a.role}. Stage ${a.stage}, outcome ${a.status}. Open details.`}
                    onClick={() => setOpenId(a.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setOpenId(a.id);
                      }
                    }}
                  >
                    <td>
                      <div className="company">{a.company}</div>
                      <div className="role">{a.role}</div>
                    </td>
                    <td><span className="stage-chip">{a.stage.replace(/_/g, " ")}</span></td>
                    <td>
                      <span className="pill">
                        <i className="dot" style={{ background: p.color }} />
                        {p.label}
                      </span>
                    </td>
                    <td style={{ color: "var(--text-secondary)" }}>
                      {a.source}
                      {a.initiator === "inbound" ? " ·  inbound" : ""}
                    </td>
                    <td className="num">{a.applied_on}</td>
                    <td className="num">{a.event_count}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {openId && (
        <Drawer id={openId} onClose={() => setOpenId(null)} onChanged={reload} />
      )}
    </div>
  );
}
