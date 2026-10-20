<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps<{ modelValue: string; name?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; 'update:name': [value: string] }>()
const query = ref(props.modelValue && props.name && props.modelValue !== props.name ? `${props.name} ${props.modelValue}` : props.modelValue || '')
const options = ref<{ symbol: string; name: string }[]>([])
const expanded = ref(false), active = ref(0), busy = ref(false)
let timer: ReturnType<typeof setTimeout>, requestId = 0
watch(() => props.modelValue, value => {
  if (value && !query.value.includes(value)) query.value = props.name && props.name !== value ? `${props.name} ${value}` : value
})
function search() {
  emit('update:modelValue', /^\d{6}$/.test(query.value.trim()) ? query.value.trim() : '')
  emit('update:name', '')
  expanded.value = true
  clearTimeout(timer)
  const version = ++requestId
  timer = setTimeout(async () => {
    busy.value = true
    try {
      const value = await api('/securities/search?q=' + encodeURIComponent(query.value.trim()))
      if (version === requestId) { options.value = value; active.value = 0 }
    } catch { if (version === requestId) options.value = [] }
    finally { if (version === requestId) busy.value = false }
  }, 150)
}
function choose(value: { symbol: string; name: string }) {
  ++requestId
  query.value = `${value.name} ${value.symbol}`
  emit('update:modelValue', value.symbol)
  emit('update:name', value.name)
  expanded.value = false
}
function keyboard(event: KeyboardEvent) {
  if (event.key === 'Escape') expanded.value = false
  if (event.key === 'ArrowDown') { event.preventDefault(); active.value = Math.min(active.value + 1, options.value.length - 1) }
  if (event.key === 'ArrowUp') { event.preventDefault(); active.value = Math.max(0, active.value - 1) }
  if (event.key === 'Enter' && expanded.value && options.value[active.value]) { event.preventDefault(); choose(options.value[active.value]) }
}
onUnmounted(() => { clearTimeout(timer); ++requestId })
</script>

<template>
  <div class="stock-picker" @focusout="(event: FocusEvent) => { if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node)) expanded = false }">
    <label>股票名称或代码
      <input v-model="query" role="combobox" aria-label="股票名称或代码" aria-autocomplete="list" aria-controls="stock-options" :aria-expanded="expanded" :aria-activedescendant="expanded && options[active] ? 'stock-option-' + active : undefined" placeholder="搜索名称或六位代码" autocomplete="off" required @input="search" @focus="expanded = true" @keydown="keyboard" />
    </label>
    <ul v-if="expanded && (options.length || busy)" id="stock-options" class="stock-options" role="listbox">
      <li v-if="busy" role="presentation">搜索中…</li>
      <li v-for="(option, index) in options" :id="'stock-option-' + index" :key="option.symbol" role="option" :aria-selected="active === index" @mousedown.prevent="choose(option)"><strong>{{ option.name }}</strong><span>{{ option.symbol }}</span></li>
    </ul>
  </div>
</template>
