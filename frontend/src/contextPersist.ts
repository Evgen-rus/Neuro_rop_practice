export async function copyTextToClipboard(
  text: string,
  clipboard: Pick<Clipboard, 'writeText'> | null | undefined = typeof navigator === 'undefined' ? undefined : navigator.clipboard,
) {
  try {
    if (!text || !clipboard?.writeText) return false
    await clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

export async function persistTextAndOpenUrl(
  text: string,
  url: string,
  deps: {
    copy: (value: string) => Promise<boolean>
    open: (target: string) => boolean
  },
) {
  // Сначала открываем URL: после await clipboard браузер может заблокировать popup.
  let opened = false
  try {
    opened = Boolean(url) && deps.open(url)
  } catch {
    opened = false
  }
  let copied = false
  try {
    copied = await deps.copy(text)
  } catch {
    copied = false
  }
  return { copied, opened }
}
