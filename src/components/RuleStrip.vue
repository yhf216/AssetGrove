<script setup lang="ts">
import { computed } from 'vue'
import { Trash2, Pencil, GripVertical } from 'lucide-vue-next'

const props = defineProps<{ rule:any; stock?:any; name:string; basisLabel:string }>()
defineEmits<{ remove:[]; edit:[]; dragstart:[event:DragEvent]; dragend:[]; move:[step:number] }>()
const current = computed(() => Number(props.stock?.quote?.price) || null)
const dps = computed(() => props.stock?.dps?.[props.rule.basis])
const yieldNow = computed(() => props.stock?.yields?.[props.rule.basis])
const target = computed(() => {
  const byYield = Number(props.rule.target_yield) > 0 && dps.value > 0 ? dps.value / Number(props.rule.target_yield) * 100 : null
  if (props.rule.mode === 'yield') return byYield
  const price = Number(props.rule.target_price) || null
  return props.rule.mode === 'all' && byYield ? Math.min(price || byYield,byYield) : price
})
const difference = computed(() => current.value && target.value ? (target.value/current.value-1)*100 : null)
const money = (v:any) => v == null ? '—' : Number(v).toLocaleString('zh-CN',{minimumFractionDigits:props.stock?.instrument === 'etf' ? 3 : 2,maximumFractionDigits:3})
const percent = (v:any) => v == null ? '—' : Number(v).toFixed(2)+'%'
const position = (v:number) => {
  if (!current.value || !target.value) return 50
  const low = Math.min(current.value,target.value)*.88, high = Math.max(current.value,target.value)*1.12
  return (v-low)/(high-low)*100
}
const quoteTime = computed(() => props.stock?.quote?.as_of ? '报价时间 '+props.stock.quote.as_of.replace('T',' ').slice(0,19) : '报价时间未核实')
const fresh = computed(() => {
  const quote = props.stock?.quote
  const age = quote?.as_of ? Date.now() - Date.parse(quote.as_of) : Infinity
  return quote?.timestamp_verified && age >= -60000 && age <= 900000
})
</script>

<template>
  <article class="rule-strip">
    <div class="rule-identity"><strong>{{ name }}</strong><span>{{ rule.symbol }}</span><span class="tag">{{ basisLabel }}</span></div>
    <div class="rule-values">
      <div :title="quoteTime"><span>当前股价</span><strong>¥{{ money(current) }}</strong></div>
      <div><span>当前股息率</span><strong>{{ percent(yieldNow) }}</strong></div>
      <div><span>每股年股息</span><strong>{{ dps == null ? '—' : '¥'+Number(dps).toFixed(4) }}</strong></div>
    </div>
    <div class="rule-destination">
      <div class="rule-targets"><span v-if="rule.mode !== 'yield'">目标 ≤ ¥{{ money(rule.target_price) }}</span><span v-if="rule.mode !== 'price'">目标 ≥ {{ percent(rule.target_yield) }}</span><span>{{ rule.mode === 'all' ? '都满足' : rule.mode === 'yield' ? '股息满足' : '股价满足' }}</span></div>
      <div v-if="current && target" class="price-scale" role="img" :aria-label="`当前价格 ${money(current)}，目标价格 ${money(target)}，目标相对当前 ${percent(difference)}`">
        <div class="price-track"><span class="target-zone" :style="{width:position(target)+'%'}"></span></div>
        <span class="price-marker current-marker" :style="{left:position(current)+'%'}"><i></i><span>当前 {{ money(current) }}</span></span>
        <span class="price-marker target-marker" :style="{left:position(target)+'%'}"><i></i><span>目标 {{ money(target) }}</span></span>
      </div>
      <span class="rule-distance">{{ difference == null ? '等待价格与分红资料' : `目标较当前 ${difference > 0 ? '+' : ''}${percent(difference)}` }}</span>
    </div>
    <div class="rule-actions"><span class="tag">{{ rule.triggered ? '条件达标' : fresh ? '监控中' : '等待有效行情' }}</span><div class="rule-edit-actions"><button class="icon-button rule-drag" draggable="true" :aria-label="'拖动排序 '+rule.symbol" title="拖动排序；方向键调整位置" @dragstart="$emit('dragstart',$event)" @dragend="$emit('dragend')" @keydown.up.prevent="$emit('move',-1)" @keydown.down.prevent="$emit('move',1)"><GripVertical :size="17" /></button><button class="icon-button" :aria-label="'编辑关注条件 '+rule.symbol" @click="$emit('edit')"><Pencil :size="17" /></button><button class="icon-button" aria-label="删除关注条件" @click="$emit('remove')"><Trash2 :size="17" /></button></div></div>
  </article>
</template>
