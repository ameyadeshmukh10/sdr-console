import { useState } from 'react'

// EverWorker brand logos live in public/ and are served at the site root.
// `black` reads on light surfaces (the sign-in panel); `white` reads on the
// dark rail / ocean.
const SRC = {
  black: '/logo=primary_neg_mono_black.png',
  white: '/logo=secondary_mono_white.png',
}

// Renders the brand PNG. If the file is missing (e.g. not added to public/
// yet) it falls back to a styled monogram + wordmark so the UI never shows a
// broken image.
export function BrandLogo({ variant = 'black', alt = 'EverWorker', className = '' }) {
  const [broken, setBroken] = useState(false)
  const cls = (base) => base + (className ? ' ' + className : '')

  if (broken) {
    return (
      <span className={cls('ev-logo' + (variant === 'white' ? ' ev-logo--light' : ''))}>
        <span className="ev-mark" aria-hidden="true">e</span>
        <span className="ev-word">Ever<span className="accent">Worker</span></span>
      </span>
    )
  }
  return (
    <img
      className={cls('ev-logo-img')}
      src={SRC[variant]} alt={alt}
      onError={() => setBroken(true)}
    />
  )
}
