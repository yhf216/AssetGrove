<script setup lang="ts">
import { computed,onMounted,onBeforeUnmount,reactive,ref } from 'vue'
import { RefreshCw, Square } from 'lucide-vue-next'
import { api } from '../api'
import MarketChart from './MarketChart.vue'
import IndicatorGuide from './IndicatorGuide.vue'
import ValuationChart from './ValuationChart.vue'
import IndicatorChart from './IndicatorChart.vue'

const state=ref<any>(null),history=ref<any>(null),error=ref(''),busy=ref(false),refreshing=ref(false),maText=ref('10,20,50,250')
const prefs=reactive<any>({symbols:['sh000001'],period:'d',chart_type:'candlestick',ma_enabled:false,ma_periods:[10,20,50,250],volume_enabled:true,trin_enabled:false,erp_enabled:false,normalize:true,indicator_period:'w',valuation_enabled:true,valuation_symbol:'sh000300',valuation_years:10,wvf_enabled:false,trend_enabled:false,bond_enabled:false})
const priceHistory=computed(()=>history.value?{...history.value,preferences:{...history.value.preferences,trin_enabled:false,erp_enabled:false}}:null)
const primary=computed(()=>prefs.symbols[0])
const primaryName=computed(()=>state.value?.catalog.find((r:any)=>r.symbol===primary.value)?.name || primary.value)
const reference=computed(()=>history.value?.reference_series?.[primary.value] || [])
const indicators=computed(()=>history.value?.indicators?.[primary.value] || {wvf:[],trend:[]})
let timer:ReturnType<typeof setInterval>,lastRevision=-1,lastCalculation=0,loaded=false,disposed=false,request=0,saveVersion=0
let polling:Promise<void>|null=null
let saveQueue=Promise.resolve()
async function loadHistory(){
  const version=++request,result=await api('/market/history')
  if(!disposed && version===request){history.value=result;lastRevision=result.revision ?? lastRevision}
}
async function poll(){
  if(polling)return polling
  polling=pollState().finally(()=>{polling=null})
  return polling
}
async function pollState(){
  if(disposed)return
  try{
    const value=await api('/market/state');if(disposed)return
    state.value=value
    if(!loaded){Object.assign(prefs,value.preferences);maText.value=prefs.ma_periods.join(',');loaded=true;await loadHistory()}
    else if(value.progress.revision!==lastRevision || (value.calculation?.completed || 0)!==lastCalculation){await loadHistory()}
    lastCalculation=value.calculation?.completed || 0
  }catch(e:any){if(!disposed)error.value=e.message}
}
async function save(){
  if(!prefs.symbols.length){error.value='请至少选择一个指数';return}
  if(prefs.symbols.length>1)prefs.chart_type='line'
  try{
    const periods=maText.value.split(/[,，\s]+/).filter(Boolean).map(Number)
    const body=JSON.parse(JSON.stringify({...prefs,ma_periods:periods})),version=++saveVersion
    saveQueue=saveQueue.catch(()=>{}).then(async()=>{
      const saved=await api('/market/preferences','POST',body)
      if(disposed || version!==saveVersion)return
      Object.assign(prefs,saved);error.value='';await loadHistory()
    })
    await saveQueue
  }catch(e:any){error.value=e.message}
}
async function sync(kind='indices'){
  busy.value=true;error.value=''
  try{await api('/market/sync','POST',{kind});await poll()}
  catch(e:any){error.value=e.message}
  finally{busy.value=false}
}
async function refreshIndicators(){
  refreshing.value=true;error.value=''
  try{await api('/market/recalculate','POST');await poll();await loadHistory()}
  catch(e:any){error.value=e.message}
  finally{refreshing.value=false}
}
async function cancel(){try{await api('/market/cancel','POST');await poll()}catch(e:any){error.value=e.message}}
async function background(enabled:boolean){try{await api('/market/background','POST',{enabled});await poll()}catch(e:any){error.value=e.message}}
function visible(){if(!document.hidden)poll()}
onMounted(async()=>{document.addEventListener('visibilitychange',visible);await poll();if(disposed)return;timer=setInterval(poll,4000)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);document.removeEventListener('visibilitychange',visible);request++})
</script>
<template>
  <section class="market-view">
    <div class="market-top"><div class="actions"><button class="button secondary" :disabled="refreshing || state?.calculation?.running || (state?.calculation?.requested || 0)>(state?.calculation?.completed || 0)" title="仅用已缓存的数据重新计算，不联网" @click="refreshIndicators"><RefreshCw :size="16" />刷新指标</button><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync()"><RefreshCw :size="16" />更新市场数据</button><button v-if="state?.progress.running" class="button secondary" @click="cancel"><Square :size="14" />停止同步</button></div></div>
    <div class="market-data-row"><span>交易日 12:00、16:00 各更新一轮</span><span v-if="state?.background?.last_daily">最近同步 {{ state.background.last_daily }}</span></div>
    <div v-if="state?.closing" class="market-data-row"><span>{{ state.closing.date }} 收盘数据{{ state.closing.complete ? '已完整' : '待补齐' }}</span><span v-if="!state.closing.complete && state.closing.error">{{ state.closing.error }}</span></div>
    <p v-if="error" class="market-error" role="alert">{{ error }}</p>
    <div v-if="state?.progress.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>{{ state.progress.phase }}</span><progress v-if="state.progress.total" :value="state.progress.done" :max="state.progress.total" /><span v-if="state.progress.total">{{ state.progress.done }} / {{ state.progress.total }}</span></div>
    <div v-if="state?.calculation?.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>正在用本地数据补算指标</span><span v-if="state.calculation.total">{{ state.calculation.done }} / {{ state.calculation.total }}</span></div>
    <div v-if="state?.calculation?.error" class="market-error" role="alert">{{ state.calculation.error }}</div>
    <div v-if="state?.progress.errors?.length" class="market-error" role="status"><div v-for="message in state.progress.errors.slice(-4)" :key="message">{{ message }}</div></div>
    <section class="market-section" aria-label="指数走势"><div class="market-section-heading"><h2>指数走势</h2></div>
    <div class="panel market-controls">
      <fieldset class="market-index-list"><legend>指数</legend><label v-for="index in state?.catalog || []" :key="index.symbol" class="checkbox-label"><input v-model="prefs.symbols" type="checkbox" :value="index.symbol" @change="save" />{{ index.name }}</label></fieldset>
      <div class="market-options"><label>图形<select v-model="prefs.chart_type" aria-label="图形" @change="save"><option value="candlestick" :disabled="prefs.symbols.length>1">K 线</option><option value="line">收盘折线</option></select></label><label class="checkbox-label"><input v-model="prefs.ma_enabled" type="checkbox" @change="save" />均线（主指数）</label><label v-if="prefs.ma_enabled">日均线周期<input v-model="maText" aria-label="日均线周期" placeholder="10,20,50,250" @change="save" /></label><label class="checkbox-label"><input v-model="prefs.volume_enabled" type="checkbox" @change="save" />主指数成交量</label><label v-if="prefs.symbols.length>1" class="checkbox-label"><input v-model="prefs.normalize" type="checkbox" @change="save" />共同起点归一化</label></div>
    </div>
    <article class="panel market-plot"><div class="market-section-heading chart-heading"><h3>行情</h3><label>行情周期<select v-model="prefs.period" aria-label="行情周期" @change="save"><option value="d">日K</option><option value="w">周K</option><option value="m">月K</option></select></label></div><MarketChart v-if="history" :history="priceHistory" :catalog="state?.catalog || []" /><div v-else class="empty-chart">正在读取市场缓存…</div></article>
    <div class="market-data-row"><span v-for="entry in history?.coverage?.filter((r:any)=>prefs.symbols.includes(r.symbol)) || []" :key="entry.symbol" :title="entry.source">{{ state?.catalog.find((r:any)=>r.symbol===entry.symbol)?.name }} {{ entry.first_date }} — {{ entry.last_date }}</span></div>
    </section>
    <section class="market-section market-shortterm" aria-label="短线情绪">
      <div class="market-section-heading"><h2>短线情绪</h2><label class="checkbox-label"><input v-model="prefs.trin_enabled" type="checkbox" @change="save" />TRIN</label><label class="checkbox-label"><input v-model="prefs.wvf_enabled" type="checkbox" @change="save" />WVF 恐慌指标</label></div>
      <p v-if="!prefs.trin_enabled && !prefs.wvf_enabled" class="market-muted">选择要显示的短线指标</p>
      <IndicatorChart v-if="prefs.trin_enabled" title="TRIN · 沪深 A 股" :rows="history?.trin || []" unit="" :baseline="1" :reference="reference" :reference-name="primaryName" :view-key="'trin:'+primary+':'+prefs.period" />
      <IndicatorChart v-if="prefs.wvf_enabled" :title="'WVF 恐慌指标 · '+primaryName" :rows="indicators.wvf" unit="%" color="#bb796b" :reference="reference" :reference-name="primaryName" :view-key="'wvf:'+primary+':'+prefs.period" />
    </section>
    <section class="market-section market-longterm" aria-label="长线趋势与估值">
      <div class="market-section-heading"><h2>长线趋势与估值</h2><label>长期指标频率<select v-model="prefs.indicator_period" aria-label="长期指标频率" @change="save"><option value="w">周频</option><option value="d">日频</option></select></label><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('erp')">更新长期数据</button></div>
      <div class="market-indicator-options"><label class="checkbox-label"><input v-model="prefs.trend_enabled" type="checkbox" @change="save" />50／200日均线趋势</label><label class="checkbox-label"><input v-model="prefs.erp_enabled" type="checkbox" @change="save" />ERP</label><label class="checkbox-label"><input v-model="prefs.bond_enabled" type="checkbox" @change="save" />10年期国债收益率</label></div>
      <IndicatorChart v-if="prefs.trend_enabled" :title="'50／200日均线趋势 · '+primaryName" :rows="indicators.trend" unit="%" color="#4b8e80" :baseline="0" :months="36" :reference="reference" :reference-name="primaryName" :view-key="'trend:'+primary+':'+prefs.indicator_period" />
      <IndicatorChart v-if="prefs.erp_enabled" title="ERP · 沪深300" :rows="history?.erp || []" unit="个百分点" color="#7b72b4" :months="120" :reference="reference" :reference-name="primaryName" :view-key="'erp:'+primary+':'+prefs.indicator_period" />
      <p v-if="prefs.erp_enabled && state?.erp_error" class="market-error" role="status">{{ state.erp_error }}</p>
      <IndicatorChart v-if="prefs.bond_enabled" title="中国10年期国债收益率" :rows="history?.bond || []" unit="%" color="#497faa" :months="120" :view-key="'bond:'+prefs.indicator_period" />
    <article class="panel market-valuation">
      <div class="market-valuation-top"><h3>PE-TTM 估值通道</h3><label>估值指数<select v-model="prefs.valuation_symbol" aria-label="估值指数" @change="save"><option v-for="entry in state?.valuation_catalog || []" :key="entry.symbol" :value="entry.symbol">{{ entry.name }}</option></select></label><label>统计区间<select v-model.number="prefs.valuation_years" aria-label="估值统计区间" @change="save"><option :value="10">近十年</option><option :value="0">全部历史</option></select></label><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('valuation')">更新估值</button><button class="button secondary" :disabled="busy || state?.progress.running" title="重新下载所选指数的完整估值历史，修复本地缺漏" @click="sync('valuation_history')">补齐估值历史</button></div>
      <div v-if="history?.valuation?.statistics" class="valuation-stats"><span>PE-TTM <strong>{{ Number(history.valuation.statistics.latest).toFixed(2) }} 倍</strong></span><span>历史分位 <strong>{{ Number(history.valuation.statistics.percentile).toFixed(1) }}%</strong></span><span>均值 {{ Number(history.valuation.statistics.mean).toFixed(2) }} 倍 · σ {{ Number(history.valuation.statistics.std).toFixed(2) }}</span><span>P10 {{ Number(history.valuation.statistics.p10).toFixed(2) }} · P50 {{ Number(history.valuation.statistics.p50).toFixed(2) }} · P90 {{ Number(history.valuation.statistics.p90).toFixed(2) }}</span></div>
      <ValuationChart v-if="history?.valuation?.statistics" :valuation="history.valuation" />
      <p v-else>暂无估值历史，点击“补齐估值历史”获取。</p>
      <p v-if="state?.valuation_error" class="market-error" role="status">{{ state.valuation_error }}</p>
      <div v-if="state?.valuation_status?.checked" class="market-data-row"><span>最近检查 {{ state.valuation_status.checked }}</span><span v-if="state.valuation_status.asof">源数据截至 {{ state.valuation_status.asof }}</span></div>
      <div v-if="history?.valuation?.coverage" class="market-data-row"><span>{{ history.valuation.name }} · {{ history.valuation.coverage.first }} — {{ history.valuation.coverage.last }}</span><span>{{ history.valuation.coverage.count }} 个日度观测 · {{ prefs.indicator_period==='w'?'周频显示':'日频显示' }}</span><span v-if="!history.valuation.coverage.ten_years">历史尚不足十年，等待补齐</span><a :href="history.valuation.source_url" target="_blank" rel="noreferrer">数据来源</a></div>
    </article>
    </section>
    <div class="panel market-cache-status">
      <details class="market-static-history"><summary>个股历史补充数据</summary><div class="market-data-row"><span>沪深未复权静态快照 · 2010—2026-09-24 · 首次约 885 MB，可续传，之后复用本地文件</span><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('static_history')">{{ state?.static_history?.pending ? '继续下载并补齐' : '下载并补齐个股历史' }}</button><a href="https://github.com/noblechang/a-share-data/releases/tag/FREE_DATA_v0" target="_blank" rel="noreferrer">数据来源</a></div><div v-if="state?.static_history" class="market-data-row"><template v-if="state.static_history.completed"><span>最近导入 {{ state.static_history.checked }} · 新增 {{ state.static_history.imported }} 条</span></template><template v-else><span>{{ state.static_history.message }}</span><span>已下载 {{ ((state.static_history.downloaded || 0)/1048576).toFixed(1) }} / {{ ((state.static_history.size || 0)/1048576).toFixed(1) }} MB</span><span v-if="state.static_history.files">已读取 {{ state.static_history.files }} 个文件</span></template></div></details>
      <div class="market-data-row"><label class="checkbox-label"><input type="checkbox" :checked="state?.background?.enabled ?? true" @change="background(($event.target as HTMLInputElement).checked)" />自动回补指标历史</label><span v-if="state?.background?.plan">{{ state.background.plan.start }} — {{ state.background.plan.end }}</span><span v-if="state?.history_pending?.count">待补齐 {{ state.history_pending.count }} 个交易日<span v-if="state.history_pending.latest"> · {{ state.history_pending.latest.date }} 样本完整度 {{ (state.history_pending.latest.coverage*100).toFixed(2) }}%</span></span></div>
      <div v-if="state?.network?.retry_at>Date.now()/1000" class="market-data-row" role="status"><span>{{ state.network.message || '同步已暂缓' }} · 预计 {{ new Date(state.network.retry_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) }} 继续</span></div>
    </div>
    <IndicatorGuide />
  </section>
</template>
