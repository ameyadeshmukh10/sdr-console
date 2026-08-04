import { useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner } from './ui.jsx'
import PlayContent from './PlayContent.jsx'

// The sequence, one row per step, with the CTA it carries.
//
// This is the link the whole campaign model exists to make explicit. Before, which
// offer a step carried lived only as prose in the generation prompt and was
// reverse-engineered afterwards from the finished copy. Here the step declares it:
// the offer picker on each row is an INPUT to generation, and every step shows the
// give and meeting ask it will produce.
//
// Copy per step is either:
//   generated — the persona agent writes per contact inside this step's frame
//   manual    — the subject/body below are used verbatim, merge variables and all
// "Suggest" drafts manual copy inside the step's assigned offer, so a suggestion
// cannot drift off the CTA the campaign assigned to that touch.

const LI_SLOT = { 1: 'Connection request', 2: 'First message', 3: 'Follow-up' }

export default function SequenceEditor({ campaignId, steps, ctas, planPrompt, onChanged }) {
  const [error, setError] = useState(null)
  const [showPrompt, setShowPrompt] = useState(false)
  const email = steps.filter((s) => s.channel === 'email').sort((a, b) => a.step_no - b.step_no)
  const li = steps.filter((s) => s.channel === 'linkedin').sort((a, b) => a.step_no - b.step_no)

  const unassigned = email.filter((s) => !s.cta_key && s.copy_mode !== 'manual').length

  return (
    <div>
      <ErrorBanner error={error} />

      {unassigned > 0 && (
        <div className="banner warn" style={{ marginBottom: 14 }}>
          {unassigned} email step{unassigned > 1 ? 's have' : ' has'} no offer assigned. Those
          steps fall back to whatever give the knowledge base suggests for that position, which
          is exactly the ambiguity a campaign is meant to remove.
        </div>
      )}

      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
        <div className="muted" style={{ fontSize: 12 }}>
          Every step names the offer its CTA must carry. The generator receives this as a
          constraint, per contact.
        </div>
        <button className="ghost sm" onClick={() => setShowPrompt(!showPrompt)}>
          {showPrompt ? 'Hide' : 'Show'} what the writer receives
        </button>
      </div>

      {showPrompt && (
        <pre className="panel" style={{ fontSize: 11, whiteSpace: 'pre-wrap', marginBottom: 16,
          maxHeight: 320, overflowY: 'auto' }}>{planPrompt || '(no steps)'}</pre>
      )}

      <h3 style={{ fontSize: 14, margin: '18px 0 8px' }}>Email — 4 touches</h3>
      {email.map((s) => (
        <StepRow key={`e${s.step_no}`} campaignId={campaignId} step={s} ctas={ctas}
          label={`Email ${s.step_no}`} onChanged={onChanged} onError={setError} />
      ))}

      <h3 style={{ fontSize: 14, margin: '22px 0 8px' }}>LinkedIn</h3>
      {li.map((s) => (
        <StepRow key={`l${s.step_no}`} campaignId={campaignId} step={s} ctas={ctas}
          label={LI_SLOT[s.step_no] || `LinkedIn ${s.step_no}`} onChanged={onChanged} onError={setError} />
      ))}
    </div>
  )
}

function StepRow({ campaignId, step, ctas, label, onChanged, onError }) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(null)
  const [angle, setAngle] = useState(step.angle || '')
  const [subject, setSubject] = useState(step.subject || '')
  const [body, setBody] = useState(step.body || '')
  const [instruction, setInstruction] = useState('')

  const cta = step.cta
  const manual = step.copy_mode === 'manual'
  const channelCtas = ctas.filter((c) => (c.channels || []).includes(step.channel))

  async function save(fields, kind = 'save') {
    setSaving(kind); onError(null)
    try {
      await api.upsertCampaignStep(campaignId, {
        step_no: step.step_no, channel: step.channel, ...fields,
      })
      onChanged()
    } catch (e) { onError(e.message) } finally { setSaving(null) }
  }

  async function suggest() {
    setSaving('suggest'); onError(null)
    try {
      const d = await api.suggestStepCopy(campaignId, {
        step_no: step.step_no, channel: step.channel,
        instruction: instruction.trim() || undefined,
      })
      setSubject(d.subject); setBody(d.body)
      // Not saved yet — the draft lands in the fields for review.
    } catch (e) {
      onError(e.status === 501
        ? 'Copy suggestions need ANTHROPIC_API_KEY set on the server.'
        : e.message)
    } finally { setSaving(null) }
  }

  return (
    <div className="panel" style={{ padding: '12px 16px', marginBottom: 8 }}>
      <div className="row" style={{ justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div className="row" style={{ gap: 10, minWidth: 0 }}>
          <span className={`badge channel-${step.channel}`}>{label}</span>
          {step.day_offset != null && (
            <span className="muted" style={{ fontSize: 12 }}>day {step.day_offset}</span>
          )}
          <span className={manual ? 'badge' : 'badge status-generated'}
            title={manual
              ? 'This exact copy is used for everyone'
              : 'The persona agent writes this per contact, inside the frame below'}>
            {manual ? 'manual copy' : 'generated'}
          </span>
        </div>

        <div className="row" style={{ gap: 8 }}>
          <select value={step.cta_key || ''} disabled={saving === 'cta'}
            onChange={(e) => save({ cta_key: e.target.value || null }, 'cta')}
            title="The offer this step's CTA must carry">
            <option value="">— no offer —</option>
            {channelCtas.map((c) => (
              <option key={c.cta_key} value={c.cta_key}>
                {c.tier ? `${c.tier}. ` : ''}{c.label}
              </option>
            ))}
          </select>
          <button className="ghost sm" onClick={() => setOpen(!open)}>{open ? 'Close' : 'Edit'}</button>
        </div>
      </div>

      {cta ? (
        <>
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            <b style={{ color: 'var(--violet)' }}>CTA:</b> anchor the meeting on {cta.give}.
            {' '}Ask: “{cta.ask}”.
          </div>
          {/* What evidence this play leans on, and how to change it. Sits with the
              offer because they are one decision: the promise and the proof. */}
          <PlayContent ctaKey={cta.key} content={cta.content} onChanged={onChanged} />
        </>
      ) : (
        <div className="muted" style={{ fontSize: 12, marginTop: 8, color: 'var(--amber)' }}>
          No offer assigned{step.channel === 'linkedin' && step.step_no === 1
            ? ' — correct for a connection request.' : '.'}
        </div>
      )}

      {open && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>This step's job</div>
          <textarea rows={3} value={angle} onChange={(e) => setAngle(e.target.value)}
            style={{ width: '100%', fontSize: 12 }}
            placeholder="What this touch is for. Passed to the writer verbatim." />
          <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="ghost sm" disabled={saving === 'angle'}
              onClick={() => save({ angle }, 'angle')}>
              {saving === 'angle' ? <Spinner /> : 'Save job'}
            </button>
            <label className="muted" style={{ fontSize: 12 }}>
              Day{' '}
              <input type="number" min="0" style={{ width: 60 }} defaultValue={step.day_offset ?? ''}
                onBlur={(e) => save({ day_offset: e.target.value === '' ? null : Number(e.target.value) }, 'day')} />
            </label>
            <label className="row muted" style={{ gap: 6, fontSize: 12, cursor: 'pointer' }}>
              <input type="checkbox" checked={manual} style={{ width: 'auto' }}
                onChange={(e) => save({ copy_mode: e.target.checked ? 'manual' : 'generated' }, 'mode')} />
              Write this step manually
            </label>
          </div>

          <div style={{ marginTop: 14 }}>
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>
                Copy {manual ? '(used verbatim)' : '(unused while this step is generated)'}
              </div>
              <button className="ghost sm" disabled={saving === 'suggest'} onClick={suggest}
                title="Draft this step inside its assigned offer">
                {saving === 'suggest' ? <Spinner /> : '✦ Suggest'}
              </button>
            </div>
            <input value={subject} onChange={(e) => setSubject(e.target.value)}
              placeholder="subject" style={{ width: '100%', marginBottom: 6, fontSize: 12 }} />
            <textarea rows={7} value={body} onChange={(e) => setBody(e.target.value)}
              placeholder="Body. Merge variables: {{first_name}}, {{company}}"
              style={{ width: '100%', fontSize: 12 }} />
            <input value={instruction} onChange={(e) => setInstruction(e.target.value)}
              placeholder="Optional direction for Suggest, e.g. “shorter, lead on the hiring angle”"
              style={{ width: '100%', marginTop: 6, fontSize: 12 }} />
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <button className="primary sm" disabled={saving === 'copy'}
                onClick={() => save({ subject, body }, 'copy')}>
                {saving === 'copy' ? <Spinner /> : 'Save copy'}
              </button>
              {!manual && (
                <span className="muted" style={{ fontSize: 11 }}>
                  Saved, but not sent while this step is set to generated.
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
