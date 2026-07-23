// debug-extract-probe.js — paste into DevTools console on the stuck page.
//
// Why this exists: the agent looped SCROLL on a GitHub repo page because the
// file-listing anchors (`backend`, `extension`, `README.md`, …) never appeared
// in the extracted accessibility tree, so the model had nothing to click. This
// probe reproduces content.js's EXACT extraction (selector + visibility filter)
// and then asks, for each real file row: was it matched? filtered? absent?
//
// It changes nothing — it only reports. Run it on the live repo page (NOT in
// the extension), ideally twice: once immediately, once after a second, to tell
// a hydration race apart from a selector/visibility miss.

(() => {
  // --- mirror content.js extractAccessibilityTree(), verbatim selector + filter
  const SELECTORS = 'button, a, input, select, textarea, [role], h1, h2, h3, label'
  const isVisible = (el) => {
    const r = el.getBoundingClientRect()
    return r.width > 0 && r.height > 0
  }
  const extracted = [...document.querySelectorAll(SELECTORS)].filter(isVisible)
  const extractedSet = new Set(extracted)

  // --- find the real repo file rows independent of the extraction selector.
  // GitHub's file table marks each row's name link with this test id; fall back
  // to any anchor whose href looks like /<owner>/<repo>/(tree|blob)/<ref>/...
  const path = location.pathname.replace(/\/+$/, '')
  const hrefLooksLikeFile = (a) =>
    /\/(tree|blob)\/[^/]+\//.test(a.getAttribute('href') || '')
  const fileLinks = [
    ...document.querySelectorAll('a[data-testid="file-row-link"], .react-directory-row a, a'),
  ].filter((a) => a.tagName === 'A' && hrefLooksLikeFile(a))

  const uniqByHref = new Map()
  for (const a of fileLinks) uniqByHref.set(a.getAttribute('href'), a)
  const rows = [...uniqByHref.values()]

  // --- classify each file row against extraction
  const classify = (a) => {
    const matchedBySelector = a.matches(SELECTORS)
    const passesVisible = isVisible(a)
    const inExtracted = extractedSet.has(a)
    let verdict
    if (inExtracted) verdict = 'OK (in extracted tree)'
    else if (!matchedBySelector) verdict = 'DROPPED: selector miss'
    else if (!passesVisible) verdict = 'DROPPED: visibility filter (0×0 rect)'
    else verdict = 'DROPPED: matched+visible but absent — re-check set logic'
    return {
      text: (a.innerText || '').trim().slice(0, 40),
      href: a.getAttribute('href'),
      matchedBySelector,
      passesVisible,
      rect: (() => { const r = a.getBoundingClientRect(); return `${Math.round(r.width)}×${Math.round(r.height)}` })(),
      ariaHidden: a.closest('[aria-hidden="true"]') ? 'inside aria-hidden' : 'no',
      verdict,
    }
  }

  console.log('%c=== a11y extraction probe ===', 'font-weight:bold')
  console.log('url:', location.href)
  console.log('total nodes matched by extraction selector (visible):', extracted.length)
  console.log('repo file rows found on page:', rows.length)
  if (rows.length === 0) {
    console.warn('No file rows found AT ALL right now → likely a hydration race: '
      + 'the file table had not rendered when this ran. Re-run after a second; '
      + 'if rows appear then, content.js is snapshotting before hydration.')
  }
  console.table(rows.map(classify))

  // --- mirror content.js extractHiddenMenuItems(): which CLOSED-menu items
  // would the agent now perceive? Paste on e.g. amazon.com and look for the
  // account flyout's links ("Returns & Orders" era failures).
  const HIDDEN_MENU_CAP = 40
  const MENUISH_ROLE = /^(menu|menubar|listbox)$/
  const MENUISH_NAME = /(menu|dropdown|drop-down|flyout|popover|submenu|popup)/i
  const controlledIds = new Set()
  document.querySelectorAll('[aria-haspopup][aria-controls], [aria-expanded][aria-controls], [aria-owns]').forEach(el => {
    const refs = (el.getAttribute('aria-controls') || '') + ' ' + (el.getAttribute('aria-owns') || '')
    refs.split(/\s+/).forEach(id => { if (id) controlledIds.add(id) })
  })
  const insideMenuContainer = (el) => {
    let n = el.parentElement
    for (let depth = 0; n && n !== document.body && depth < 8; n = n.parentElement, depth++) {
      const role = n.getAttribute('role')
      if (role && MENUISH_ROLE.test(role)) return true
      if (n.id && controlledIds.has(n.id)) return true
      if (MENUISH_NAME.test(n.id + ' ' + (n.getAttribute('class') || ''))) return true
    }
    return false
  }
  const hiddenItems = []
  for (const el of document.querySelectorAll('a, button, [role="menuitem"], [role="option"]')) {
    if (hiddenItems.length >= HIDDEN_MENU_CAP) break
    if (extractedSet.has(el) || isVisible(el)) continue
    const text = (el.textContent || '').trim().slice(0, 100)
    if (!text && !el.ariaLabel && !el.title) continue
    if (!insideMenuContainer(el)) continue
    hiddenItems.push({ tag: el.tagName.toLowerCase(), text, container: (el.closest('[id]')?.id || '').slice(0, 40) })
  }
  console.log('%c=== hidden menu items the agent would perceive ===', 'font-weight:bold')
  console.table(hiddenItems)

  // expose for poking around
  window.__a11yProbe = { extracted, rows, classify, hiddenItems }
  return `extracted=${extracted.length} fileRows=${rows.length} hiddenMenuItems=${hiddenItems.length} — see tables above; details in window.__a11yProbe`
})()
