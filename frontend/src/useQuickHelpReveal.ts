import { useEffect, useMemo, useState } from 'react'
import {
  QUICK_HELP_CARD_STAGGER_MS,
  QUICK_HELP_REVEAL_BUDGET_MS,
  QUICK_HELP_SUMMARY_HOLD_MS,
  QUICK_HELP_SUMMARY_TYPE_MS,
  fragmentText,
  typedText,
  type QuickHelpRevealStep,
} from './quickHelpReveal'

function readReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(readReducedMotion)
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(media.matches)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])
  return reduced
}

export function useQuickHelpReveal(enabled: boolean, summaryText: string): {
  step: QuickHelpRevealStep
  typedSummary: string
  summaryReady: boolean
  animate: boolean
} {
  const reducedMotion = usePrefersReducedMotion()
  const animate = enabled && !reducedMotion
  const fragments = useMemo(() => fragmentText(summaryText), [summaryText])
  const [step, setStep] = useState<QuickHelpRevealStep>(() => (animate ? 'summary' : 'done'))
  const [visibleCount, setVisibleCount] = useState(() => (animate ? Math.min(1, fragments.length) : fragments.length))
  const [summaryReady, setSummaryReady] = useState(() => !animate)

  useEffect(() => {
    if (!animate) {
      setStep('done')
      setVisibleCount(fragments.length)
      setSummaryReady(true)
      return
    }
    setStep('summary')
    setVisibleCount(Math.min(1, fragments.length))
    setSummaryReady(false)
    const timers: number[] = []
    const count = Math.max(fragments.length, 1)
    const interval = QUICK_HELP_SUMMARY_TYPE_MS / count
    fragments.forEach((_, index) => {
      if (index === 0) return
      timers.push(window.setTimeout(() => setVisibleCount(index + 1), interval * index))
    })
    const messageAt = QUICK_HELP_SUMMARY_TYPE_MS + QUICK_HELP_SUMMARY_HOLD_MS
    timers.push(window.setTimeout(() => setSummaryReady(true), QUICK_HELP_SUMMARY_TYPE_MS))
    timers.push(window.setTimeout(() => setStep('message'), messageAt))
    timers.push(window.setTimeout(() => setStep('secondary'), messageAt + QUICK_HELP_CARD_STAGGER_MS))
    timers.push(window.setTimeout(() => setStep('fallback'), messageAt + QUICK_HELP_CARD_STAGGER_MS * 2))
    timers.push(window.setTimeout(() => setStep('done'), QUICK_HELP_REVEAL_BUDGET_MS))
    return () => timers.forEach((timer) => window.clearTimeout(timer))
  }, [animate, fragments])

  return {
    step,
    typedSummary: animate ? typedText(fragments, visibleCount) : summaryText,
    summaryReady: animate ? summaryReady : true,
    animate,
  }
}
