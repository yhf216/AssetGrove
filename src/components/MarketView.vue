<script setup lang="ts">
import { onMounted,onBeforeUnmount,reactive,ref } from 'vue'
import { RefreshCw, Square, Database, ChevronDown } from 'lucide-vue-next'
import { api } from '../api'
import MarketChart from './MarketChart.vue'

const state=ref<any>(null),history=ref<any>(null),error=ref(''),busy=ref(false),maText=ref('10,20,50,250'),backfill=ref(false)
const prefs=reactive<any>({symbols:['sh000001'],period:'d',chart_type:'candlestick',ma_enabled:false,ma_periods:[10,20,50,250],volume_enabled:true,trin_enabled:false,erp_enabled:false,normalize:true})
const today=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'})
const end=ref(today),start=ref((Number(today.slice(0,4))-1)+today.slice(4))
let timer:ReturnType<typeof setInterval>,lastRevision=-1,loaded=false,disposed=false,request=0,saveVersion=0
let saveQueue=Promise.resolve()
async function loadHistory(){
  const version=++request,result=await api('/market/history')
  if(!disposed && version===request){history.value=result;lastRevision=result.revision ?? lastRevision}
}
async function poll(){
  if(disposed)return
  try{
    const value=await api('/market/state');if(disposed)return
    state.value=value
    if(!loaded){Object.assign(prefs,value.preferences);maText.value=prefs.ma_periods.join(',');loaded=true;await loadHistory()}
    if(value.progress.revision>lastRevision){await loadHistory()}
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
  try{await api('/market/sync','POST',{kind,...(kind==='history'?{start:start.value,end:end.value}:{})});await poll()}
  catch(e:any){error.value=e.message}
  finally{busy.value=false}
}
async function cancel(){try{await api('/market/cancel','POST');await poll()}catch(e:any){error.value=e.message}}
async function background(enabled:boolean){try{await api('/market/background','POST',{enabled});await poll()}catch(e:any){error.value=e.message}}
onMounted(async()=>{await poll();if(disposed)return;timer=setInterval(poll,4000)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);request++})
</script>
<template>
  <section class="market-view">
    <div class="market-top"><div class="market-periods" role="group" aria-label="图表周期"><button v-for="[key,label] in [['d','日K'],['w','周K'],['m','月K']]" :key="key" :class="['button',prefs.period===key?'primary':'secondary']" @click="prefs.period=key;save()">{{ label }}</button></div><div class="actions"><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync()"><RefreshCw :size="16" />更新市场数据</button><button class="button secondary" @click="backfill=!backfill"><Database :size="16" />历史回补<ChevronDown :size="14" /></button><button v-if="state?.progress.running" class="button secondary" @click="cancel"><Square :size="14" />停止同步</button></div></div>
    <div class="market-data-row"><span>交易日 12:00、16:00 各更新一轮</span><span v-if="state?.background?.last_daily">最近同步 {{ state.background.last_daily }}</span></div>
    <div class="panel market-controls">
      <fieldset class="market-index-list"><legend>指数</legend><label v-for="index in state?.catalog || []" :key="index.symbol" class="checkbox-label"><input v-model="prefs.symbols" type="checkbox" :value="index.symbol" @change="save" />{{ index.name }}</label></fieldset>
      <div class="market-options"><label>图形<select v-model="prefs.chart_type" aria-label="图形" @change="save"><option value="candlestick" :disabled="prefs.symbols.length>1">K 线</option><option value="line">收盘折线</option></select></label><label class="checkbox-label"><input v-model="prefs.ma_enabled" type="checkbox" @change="save" />均线（主指数）</label><label v-if="prefs.ma_enabled">日均线周期<input v-model="maText" aria-label="日均线周期" placeholder="10,20,50,250" @change="save" /></label><label class="checkbox-label"><input v-model="prefs.volume_enabled" type="checkbox" @change="save" />主指数成交量</label><label class="checkbox-label"><input v-model="prefs.trin_enabled" type="checkbox" @change="save" />TRIN</label><label class="checkbox-label"><input v-model="prefs.erp_enabled" type="checkbox" @change="save" />ERP</label><label v-if="prefs.symbols.length>1" class="checkbox-label"><input v-model="prefs.normalize" type="checkbox" @change="save" />共同起点归一化</label></div>
    </div>
    <p v-if="error" class="market-error" role="alert">{{ error }}</p>
    <div v-if="state?.progress.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>{{ state.progress.phase }}</span><progress v-if="state.progress.total" :value="state.progress.done" :max="state.progress.total" /><span v-if="state.progress.total">{{ state.progress.done }} / {{ state.progress.total }}</span></div>
    <div v-if="state?.calculation?.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>正在用本地数据补算指标</span><span v-if="state.calculation.total">{{ state.calculation.done }} / {{ state.calculation.total }}</span></div>
    <div v-if="state?.calculation?.error" class="market-error" role="alert">{{ state.calculation.error }}</div>
    <div v-if="state?.progress.errors?.length" class="market-error" role="status"><div v-for="message in state.progress.errors.slice(-4)" :key="message">{{ message }}</div></div>
    <article class="panel market-plot"><MarketChart v-if="history" :history="history" :catalog="state?.catalog || []" /><div v-else class="empty-chart">正在读取市场缓存…</div></article>
    <div class="market-data-row"><span v-for="entry in history?.coverage?.filter((r:any)=>prefs.symbols.includes(r.symbol)) || []" :key="entry.symbol" :title="entry.source">{{ state?.catalog.find((r:any)=>r.symbol===entry.symbol)?.name }} {{ entry.first_date }} — {{ entry.last_date }}</span></div>
    <div v-if="prefs.trin_enabled" class="panel market-trin-info"><div><strong>TRIN · 沪深 A 股 <b v-if="state?.latest_trin">{{ Number(state.latest_trin.value).toFixed(3) }}</b></strong><span v-if="state?.latest_trin">最新日值 {{ state.latest_trin.date }} · {{ state.latest_trin.provisional ? '盘中值' : '收盘值' }} · 上涨 {{ state.latest_trin.advances }} 家 / 下跌 {{ state.latest_trin.declines }} 家</span><span>{{ state?.trin_coverage.count || 0 }} 个有效交易日<span v-if="state?.trin_coverage.first"> · {{ state.trin_coverage.first }} — {{ state.trin_coverage.last }}</span></span></div></div>
    <div v-if="prefs.erp_enabled" class="panel market-trin-info"><div><strong>ERP · 沪深300{{ history?.erp_frequency==='monthly' ? '（月度）' : '' }} <b v-if="state?.latest_erp">{{ Number(state.latest_erp.value).toFixed(2) }} 个百分点</b></strong><span>100 ÷ 滚动市盈率 − 中国10年期国债收益率</span><span v-if="state?.latest_erp">{{ state.latest_erp.date }} · PE {{ Number(state.latest_erp.pe).toFixed(2) }} · 国债 {{ Number(state.latest_erp.bond_yield).toFixed(2) }}%</span><span v-else>等待后台读取估值和国债历史</span><span>{{ state?.erp_coverage?.count || 0 }} 个观测值</span><span v-if="state?.erp_error" class="market-error">{{ state.erp_error }}</span></div></div>
    <div class="market-data-row"><label class="checkbox-label"><input type="checkbox" :checked="state?.background?.enabled ?? true" @change="background(($event.target as HTMLInputElement).checked)" />自动回补指标历史</label><span v-if="state?.background?.plan">{{ state.background.plan.start }} — {{ state.background.plan.end }}</span><span v-if="state?.history_pending?.count">待补齐 {{ state.history_pending.count }} 个交易日<span v-if="state.history_pending.latest"> · {{ state.history_pending.latest.date }} 样本完整度 {{ (state.history_pending.latest.coverage*100).toFixed(2) }}%</span></span></div>
    <div v-if="state?.network" class="market-data-row"><span>今日自动请求 {{ state.network.automatic_used }} / {{ state.network.automatic_limit }} · 历史回补 {{ state.network.history_used }} / {{ state.network.history_limit }} · 含手动 {{ state.network.used }} / {{ state.network.limit }}</span><span v-if="state.network.retry_at>Date.now()/1000">{{ state.network.message }} · {{ new Date(state.network.retry_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) }}</span></div>
    <form v-if="backfill" class="panel market-backfill" @submit.prevent="sync('history')"><div class="form-row"><label>开始日期<input v-model="start" type="date" min="1990-01-01" :max="today" required /></label><label>结束日期<input v-model="end" type="date" :min="start" :max="today" required /></label></div><p>指标历史默认后台续传，周末也会继续；按缺口选择逐股或全市场日线。请求间隔至少 30 秒，达到预算后自动等待，本地补算持续进行。</p><button class="button primary" :disabled="busy || state?.progress.running">开始 / 继续回补</button></form>
  </section>
</template>
