// Keep wheel scrolling while preventing native form controls from changing values.
export function installWheelGuard() {
  document.addEventListener('wheel', event => {
    const target = event.target instanceof Element ? event.target.closest('input,select') : null
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return
    if (target instanceof HTMLInputElement && !['number','range','date','datetime-local','time','month','week'].includes(target.type)) return
    if (!event.cancelable) return
    event.preventDefault()
    const scale = event.deltaMode === 1 ? 20 : event.deltaMode === 2 ? window.innerHeight : 1
    let parent = target.parentElement
    while (parent) {
      if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight) {
        const before = parent.scrollTop
        parent.scrollBy({ top:event.deltaY * scale, left:event.deltaX * scale, behavior:'instant' })
        if (parent.scrollTop !== before) return
      }
      parent = parent.parentElement
    }
    window.scrollBy({ top:event.deltaY * scale, left:event.deltaX * scale, behavior:'instant' })
  }, { capture:true, passive:false })
}
