import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// Who we sell to, as configuration.
//
// This one ruleset drives four things that used to be four hardcoded lists:
// what the enrichment provider is asked to search for, which returned titles we
// keep, which persona writes the copy, and who is worth a rep's call.
//
// ORDER IS THE LOGIC — rules are evaluated top-down and the first match wins, so
// "Sales Operations Manager" only lands on RevOps because RevOps sits above the
// generic Sales rule. That is the main way to get this wrong, so the order is shown
// as a number you can edit rather than left implicit in the list position.
//
// Exclusion is not a special case: it is simply the top rule, mapping to not-ICP.

const SENIORITY = ['decision-maker', 'champion', 'influencer', 'excluded']
const PERSONAS = ['sales-leadership', 'revops', 'partnerships', 'sdr-bdr']

export default function BuyerGroup() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)
  const [testInput, setTestInput] = useState('VP of Sales\nAccount Executive\nSales Operations Manager')
  const [testOut, setTestOut] = useState(null)

  function load() {
    api.buyerGroup().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function patch(role_key, fields) {
    setBusy(role_key); setError(null)
    try { await api.updateBuyerGroup({ action: 'update', role_key, ...fields }); load() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function runTest() {
    setBusy('test'); setError(null)
    try {
      const titles = testInput.split('\n').map((t) => t.trim()).filter(Boolean)
      setTestOut(await api.updateBuyerGroup({ action: 'test', titles }))
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  const roles = data?.roles || []

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 2 }}>
        <Addon id="buyer-group" />
      </div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        Which titles count as buyers. This drives what enrichment searches for, which
        contacts survive the ICP gate, which persona writes their copy, and who is worth a
        call — from one definition instead of four.
      </p>

      <ErrorBanner error={error} />
      {data && data.available === false && (
        <div className="empty">
          Buyer group configuration isn’t present in this dataset yet.
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : roles.length > 0 && (
        <>
          <div className="banner info" style={{ marginBottom: 14 }}>
            Rules run <b>top to bottom, first match wins</b>. That ordering is the logic:
            RevOps sits above general Sales so “Sales Operations” resolves to RevOps, and
            the exclusion rule runs first so junior titles never reach a seniority rule.
          </div>

          <div className="panel" style={{ padding: 0, overflowX: 'auto', marginBottom: 20 }}>
            <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 1040 }}>
              <thead><tr>
                <th style={{ width: '6%' }} title="Match priority — lower runs first">Order</th>
                <th style={{ width: '18%' }}>Role</th>
                <th style={{ width: '12%' }}>Seniority</th>
                <th style={{ width: '13%' }}>Persona</th>
                <th style={{ width: '7%' }} title="Counts as a buyer">ICP</th>
                <th style={{ width: '7%' }} title="Senior enough for a rep's call">Call</th>
                <th style={{ width: '25%' }}>Title patterns</th>
                <th style={{ width: '12%' }}>Search terms</th>
              </tr></thead>
              <tbody>
                {roles.map((r) => (
                  <tr key={r.role_key} style={r.active ? undefined : { opacity: 0.5 }}>
                    <td>
                      <input type="number" className="f-num" defaultValue={r.sort_order}
                        style={{ width: 56 }} disabled={busy === r.role_key}
                        onBlur={(e) => {
                          const v = Number(e.target.value)
                          if (v !== r.sort_order) patch(r.role_key, { sort_order: v })
                        }} />
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{r.label}</div>
                      <div className="acct-dom mono">{r.role_key}</div>
                    </td>
                    <td>
                      <select value={r.seniority || ''} disabled={busy === r.role_key}
                        style={{ width: '100%', fontSize: 12 }}
                        onChange={(e) => patch(r.role_key, { seniority: e.target.value })}>
                        {SENIORITY.map((s) => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </td>
                    <td>
                      <select value={r.persona || ''} disabled={busy === r.role_key}
                        style={{ width: '100%', fontSize: 12 }}
                        onChange={(e) => patch(r.role_key, { persona: e.target.value })}>
                        <option value="">—</option>
                        {PERSONAS.map((s) => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </td>
                    <td>
                      <input type="checkbox" checked={!!r.is_icp} style={{ width: 'auto' }}
                        disabled={busy === r.role_key}
                        onChange={(e) => patch(r.role_key, { is_icp: e.target.checked })} />
                    </td>
                    <td>
                      <input type="checkbox" checked={!!r.worth_calling} style={{ width: 'auto' }}
                        disabled={busy === r.role_key || !r.is_icp}
                        onChange={(e) => patch(r.role_key, { worth_calling: e.target.checked })} />
                    </td>
                    <td>
                      <textarea rows={2} className="mono" style={{ width: '100%', fontSize: 11 }}
                        defaultValue={(r.match_patterns || []).join('\n')}
                        disabled={busy === r.role_key}
                        onBlur={(e) => {
                          const v = e.target.value.split('\n').map((x) => x.trim()).filter(Boolean)
                          if (v.join('\n') !== (r.match_patterns || []).join('\n')) {
                            patch(r.role_key, { match_patterns: v })
                          }
                        }} />
                    </td>
                    <td>
                      <textarea rows={2} style={{ width: '100%', fontSize: 11 }}
                        defaultValue={(r.clay_titles || []).join('\n')}
                        disabled={busy === r.role_key}
                        onBlur={(e) => {
                          const v = e.target.value.split('\n').map((x) => x.trim()).filter(Boolean)
                          if (v.join('\n') !== (r.clay_titles || []).join('\n')) {
                            patch(r.role_key, { clay_titles: v })
                          }
                        }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid" style={{ gap: 16, gridTemplateColumns: 'repeat(auto-fit,minmax(300px,1fr))' }}>
            <div className="panel">
              <div className="card-h"><h3>Try some titles</h3></div>
              <p className="card-note">
                Paste real titles and see how the ruleset classifies them. The fastest way
                to catch an ordering mistake before it decides who gets contacted.
              </p>
              <textarea rows={5} value={testInput} onChange={(e) => setTestInput(e.target.value)}
                style={{ width: '100%', fontSize: 12, marginTop: 10 }} />
              <div className="card-actions">
                <button className="ghost sm" disabled={busy === 'test'} onClick={runTest}>
                  {busy === 'test' ? <Spinner /> : 'Classify'}
                </button>
              </div>
              {testOut && (
                <table className="dense" style={{ width: '100%', marginTop: 12 }}>
                  <tbody>
                    {(testOut.results || []).map((r, i) => (
                      <tr key={i}>
                        <td style={{ fontSize: 12 }}>{r.title}</td>
                        <td>
                          {r.is_icp
                            ? <span className="badge status-enrolled">{r.label}</span>
                            : <span className="badge status-skipped">not a buyer</span>}
                        </td>
                        <td className="muted" style={{ fontSize: 11 }}>
                          {r.worth_calling ? 'worth a call' : r.seniority || ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="panel">
              <div className="card-h"><h3>What enrichment will search for</h3></div>
              <p className="card-note">
                Sent to the enrichment provider as the include/exclude keywords. Matching there
                is fuzzy, so every returned contact is still re-checked against the rules
                above — that gate is the actual guarantee.
              </p>
              <div className="section-h" style={{ marginBottom: 6 }}>
                Include ({num((data.clay_include || []).length)})
              </div>
              <div className="tags">
                {(data.clay_include || []).map((t) => <span key={t} className="tag">{t}</span>)}
              </div>
              <div className="section-h" style={{ marginBottom: 6 }}>
                Exclude ({num((data.clay_exclude || []).length)})
              </div>
              <div className="tags">
                {(data.clay_exclude || []).map((t) => (
                  <span key={t} className="tag" style={{ color: 'var(--amber)' }}>{t}</span>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
