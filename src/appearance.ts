import { ref } from 'vue'

export const fontSize = ref(Number(localStorage.getItem('assetgrove-font-size')) || 16)
export const theme = ref(localStorage.getItem('assetgrove-theme') || 'light')
const systemScheme = window.matchMedia('(prefers-color-scheme: dark)')
export const resolvedTheme = ref('light')

export function applyAppearance(size = fontSize.value, value = theme.value) {
  fontSize.value = Math.min(24, Math.max(14, Number(size) || 16))
  theme.value = ['light','dark','system'].includes(value) ? value : 'light'
  resolvedTheme.value = theme.value === 'system' ? (systemScheme.matches ? 'dark' : 'light') : theme.value
  document.documentElement.style.fontSize = `${fontSize.value}px`
  document.documentElement.dataset.theme = resolvedTheme.value
  document.documentElement.style.colorScheme = resolvedTheme.value
  localStorage.setItem('assetgrove-font-size', String(fontSize.value))
  localStorage.setItem('assetgrove-theme', theme.value)
}

systemScheme.addEventListener('change', () => { if (theme.value === 'system') applyAppearance() })
applyAppearance()
