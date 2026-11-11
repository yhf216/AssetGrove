<script setup lang="ts">
import { computed,onMounted,onBeforeUnmount,reactive,ref } from 'vue'
import { RefreshCw, Square } from 'lucide-vue-next'
import { api } from '../api'
// 独立指数视图暂时停用，下方保留完整模板。
// import MarketChart from './MarketChart.vue'
import IndicatorGuide from './IndicatorGuide.vue'
import MarketWorkspaceChart from './MarketWorkspaceChart.vue'
import type { Metric } from '../marketWorkspace'

const state=ref<any>(null),history=ref<any>(null),error=ref(''),busy=ref(false),refreshing=ref(false),maText=ref('10,20,50,250')
const prefs=reactive<any>({symbols:['sh000001'],period:'d',chart_type:'candlestick',ma_enabled:false,ma_periods:[10,20,50,250],volume_enabled:true,trin_enabled:false,erp_enabled:false,normalize:true,indicator_period:'w',valuation_enabled:true,valuation_symbol:'sh000300',valuation_years:10,wvf_enabled:false,trend_enabled:false,bond_enabled:false,market_mode:'short',short_indicator:'trin',long_indicator:'valuation',short_overlays:[],long_overlays:[],index_overlay:'line'})
// const priceHistory=computed(()=>history.value?{...history.value,preferences:{...history.value.preferences,trin_enabled:false,erp_enabled:false}}:null)
const primary=computed(()=>prefs.symbols[0])
const primaryName=computed(()=>state.value?.catalog.find((r:any)=>r.symbol===primary.value)?.name || primary.value)
const indicators=computed(()=>history.value?.indicators?.[primary.value] || {wvf:[],trend:[]})
const definitions:Record<string,any>={
  trin:{id:'trin',name:'TRIN · 沪深 A 股',label:'TRIN',axis:'TRIN',unit:'',color:'#bd9147',baseline:1,months:1},
  breadth:{id:'breadth',name:'宽广度 · 沪深 A 股',label:'宽广度',axis:'净上涨家数',unit:'家',color:'#638b49',baseline:0,months:1},
  wvf:{id:'wvf',name:'WVF 恐慌指标',label:'WVF 恐慌指标',axis:'WVF (%)',unit:'%',color:'#bb796b',months:1},
  trend:{id:'trend',name:'50／200日均线趋势',label:'均线趋势',axis:'偏离 (%)',unit:'%',color:'#4b8e80',baseline:0,months:36},
  erp:{id:'erp',name:'ERP · 沪深300',label:'ERP',axis:'ERP (百分点)',unit:'个百分点',color:'#7b72b4',months:120},
  bond:{id:'bond',name:'中国10年期国债收益率',label:'10年期国债',axis:'国债 (%)',unit:'%',color:'#497faa',months:120},
  valuation:{id:'valuation',name:'PE-TTM 估值通道',label:'PE-TTM 估值通道',axis:'PE (倍)',unit:'倍',color:'#5981a7',months:120},
}
const choices=computed(()=>prefs.market_mode==='short'?['trin','breadth','wvf']:['valuation','erp','trend','bond'])
const selected=computed<string>(()=>prefs[prefs.market_mode+'_indicator'])
const overlays=computed<string[]>(()=>prefs[prefs.market_mode+'_overlays'])
const hasValuation=computed(()=>[selected.value,...overlays.value].includes('valuation'))
const period=computed(()=>prefs.market_mode==='short'?(history.value?.preferences.period || prefs.period):(history.value?.preferences.indicator_period || prefs.indicator_period))
const bars=computed(()=>history.value?.[prefs.market_mode==='short'?'series':'long_series']?.[primary.value] || [])
const metrics=computed<Metric[]>(()=>[selected.value,...overlays.value].map(id=>{
  const definition=definitions[id]
  const rows=id==='trin'?history.value?.trin:id==='breadth'?history.value?.breadth:id==='wvf'?indicators.value.wvf:id==='trend'?indicators.value.trend:id==='erp'?history.value?.erp:id==='bond'?history.value?.bond:(history.value?.valuation?.series || []).map((r:any)=>({...r,value:r.pe}))
  const name=id==='wvf' || id==='trend'?definition.name+' · '+primaryName.value:id==='valuation'?definition.name+' · '+(history.value?.valuation?.name || ''):definition.name
  return {...definition,name:id==='breadth' && period.value!=='d'?name+'（日均）':name,rows:rows || [],...(id==='valuation'?{statistics:history.value?.valuation?.statistics}:{})}
}))
const viewKey=computed(()=>JSON.stringify([prefs.market_mode,selected.value,primary.value,period.value,prefs.valuation_symbol,prefs.valuation_years]))
async function chooseMode(mode:string){prefs.market_mode=mode;await save()}
async function chooseIndicator(id:string){prefs[prefs.market_mode+'_indicator']=id;prefs[prefs.market_mode+'_overlays']=overlays.value.filter(key=>key!==id);await save()}
async function toggleOverlay(id:string){prefs[prefs.market_mode+'_overlays']=overlays.value.includes(id)?overlays.value.filter(key=>key!==id):[...overlays.value,id];await save()}
async function chooseIndex(event:Event){prefs.symbols=[(event.target as HTMLSelectElement).value];await save()}

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
  if(!prefs.symbols.length){error.value='请至少选择一个指数';return false}
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
    return !disposed
  }catch(e:any){error.value=e.message;return false}
}
async function sync(kind='indices'){
  busy.value=true;error.value=''
  try{if(!await save())return;await api('/market/sync','POST',{kind});await poll()}
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
    <div class="market-top workspace-top">
      <div class="workspace-tabs" role="tablist" aria-label="指标分类"><button role="tab" :disabled="!history" :aria-selected="prefs.market_mode==='short'" :class="{active:prefs.market_mode==='short'}" @click="chooseMode('short')">短线指标</button><button role="tab" :disabled="!history" :aria-selected="prefs.market_mode==='long'" :class="{active:prefs.market_mode==='long'}" @click="chooseMode('long')">长线指标</button></div>
      <div class="actions"><button class="button secondary" :disabled="refreshing || state?.calculation?.running || (state?.calculation?.requested || 0)>(state?.calculation?.completed || 0)" title="仅用已缓存的数据重新计算，不联网" @click="refreshIndicators"><RefreshCw :size="16" />刷新指标</button><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync(prefs.market_mode==='long'?'erp':'indices')"><RefreshCw :size="16" />更新市场数据</button><button v-if="state?.progress.running" class="button secondary" @click="cancel"><Square :size="14" />停止同步</button></div>
    </div>
    <div v-if="state?.closing" class="market-data-row"><span>{{ state.closing.date }} 收盘数据{{ state.closing.complete ? '已完整' : '待补齐' }}</span><span v-if="!state.closing.complete && state.closing.error">{{ state.closing.error }}</span></div>
    <p v-if="error" class="market-error" role="alert">{{ error }}</p>
    <div v-if="state?.progress.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>{{ state.progress.phase }}</span><progress v-if="state.progress.total" :value="state.progress.done" :max="state.progress.total" /><span v-if="state.progress.total">{{ state.progress.done }} / {{ state.progress.total }}</span></div>
    <div v-if="state?.calculation?.running" class="market-progress" role="status"><RefreshCw :size="15" class="spin" /><span>正在用本地数据补算指标</span></div>
    <div v-if="state?.calculation?.error" class="market-error" role="alert">{{ state.calculation.error }}</div>
    <div v-if="state?.progress.errors?.length" class="market-error" role="status"><div v-for="message in state.progress.errors.slice(-4)" :key="message">{{ message }}</div></div>
    <section class="workspace-panel" role="tabpanel" :aria-label="prefs.market_mode==='short'?'短线指标':'长线指标'">
      <fieldset class="panel workspace-controls" :disabled="!history">
        <div class="workspace-indicators" role="group" aria-label="选择指标"><button v-for="id in choices" :key="id" :class="['button',selected===id?'primary':'secondary']" :aria-pressed="selected===id" @click="chooseIndicator(id)">{{ definitions[id].label }}</button>
          <details class="workspace-overlays"><summary>叠加指标{{ overlays.length?'（'+overlays.length+'）':'' }}</summary><div class="workspace-overlay-options"><button v-for="id in choices.filter(key=>key!==selected)" :key="id" :class="['button',overlays.includes(id)?'primary':'secondary']" :aria-pressed="overlays.includes(id)" @click="toggleOverlay(id)">叠加{{ definitions[id].label }}</button><button v-if="overlays.length" class="text-button" @click="prefs[prefs.market_mode+'_overlays']=[];save()">清除叠加</button></div></details>
        </div>
        <div class="workspace-chart-controls">
          <label>指数<select :value="primary" aria-label="叠加指数" @change="chooseIndex"><option v-for="index in state?.catalog || []" :key="index.symbol" :value="index.symbol">{{ index.name }}</option></select></label>
          <label>叠加形式<select v-model="prefs.index_overlay" aria-label="指数叠加形式" @change="save"><option value="none">不叠加</option><option value="line">指数折线</option><option value="candlestick">指数K线</option></select></label>
          <label v-if="prefs.market_mode==='short'">周期<select v-model="prefs.period" aria-label="行情周期" @change="save"><option value="d">日</option><option value="w">周</option><option value="m">月</option></select></label>
          <label v-else>频率<select v-model="prefs.indicator_period" aria-label="长期指标频率" @change="save"><option value="w">周频</option><option value="d">日频</option></select></label>
          <details class="workspace-options"><summary>图表设置</summary><div class="workspace-option-items"><label class="checkbox-label"><input v-model="prefs.ma_enabled" type="checkbox" @change="save" />指数均线</label><label v-if="prefs.ma_enabled">日均线周期<input v-model="maText" aria-label="日均线周期" placeholder="10,20,50,250" @change="save" /></label><label class="checkbox-label"><input v-model="prefs.volume_enabled" type="checkbox" @change="save" />指数成交量</label></div></details>
        </div>
        <div v-if="hasValuation" class="workspace-valuation">
          <div class="market-valuation-top"><label>估值指数<select v-model="prefs.valuation_symbol" aria-label="估值指数" @change="save"><option v-for="entry in state?.valuation_catalog || []" :key="entry.symbol" :value="entry.symbol">{{ entry.name }}</option></select></label><label>统计区间<select v-model.number="prefs.valuation_years" aria-label="估值统计区间" @change="save"><option :value="10">近十年</option><option :value="0">全部历史</option></select></label><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('valuation')">更新估值</button><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('valuation_history')">补齐估值历史</button></div>
          <div v-if="history?.valuation?.statistics" class="valuation-stats"><span>PE-TTM <strong>{{ Number(history.valuation.statistics.latest).toFixed(2) }} 倍</strong></span><span>历史分位 <strong>{{ Number(history.valuation.statistics.percentile).toFixed(1) }}%</strong></span><span>均值 {{ Number(history.valuation.statistics.mean).toFixed(2) }} 倍 · σ {{ Number(history.valuation.statistics.std).toFixed(2) }}</span></div>
          <p v-if="state?.valuation_error" class="market-error" role="status">{{ state.valuation_error }}</p><div v-if="history?.valuation?.coverage" class="market-data-row"><span>{{ history.valuation.coverage.first }} — {{ history.valuation.coverage.last }}</span><span v-if="!history.valuation.coverage.ten_years">历史尚不足十年，等待补齐</span><a :href="history.valuation.source_url" target="_blank" rel="noreferrer">数据来源</a></div>
        </div>
      </fieldset>
      <MarketWorkspaceChart v-if="history" :metrics="metrics" :bars="bars" :index-name="primaryName" :index-mode="prefs.index_overlay" :period="period" :view-key="viewKey" :volume="prefs.volume_enabled" :ma="prefs.ma_enabled" :ma-periods="prefs.ma_periods" />
      <div v-else class="panel empty-chart">正在读取市场缓存…</div>
      <p v-if="prefs.market_mode==='long' && state?.erp_error" class="market-error" role="status">{{ state.erp_error }}</p>
    </section>
    <!-- 独立指数模块暂时停用，保留原实现。
    <section class="market-section" aria-label="指数走势"><div class="market-section-heading"><h2>指数走势</h2></div>
    <div class="panel market-controls">
      <fieldset class="market-index-list"><legend>指数</legend><label v-for="index in state?.catalog || []" :key="index.symbol" class="checkbox-label"><input v-model="prefs.symbols" type="checkbox" :value="index.symbol" @change="save" />{{ index.name }}</label></fieldset>
      <div class="market-options"><label>图形<select v-model="prefs.chart_type" aria-label="图形" @change="save"><option value="candlestick" :disabled="prefs.symbols.length>1">K 线</option><option value="line">收盘折线</option></select></label><label class="checkbox-label"><input v-model="prefs.ma_enabled" type="checkbox" @change="save" />均线（主指数）</label><label v-if="prefs.ma_enabled">日均线周期<input v-model="maText" aria-label="日均线周期" placeholder="10,20,50,250" @change="save" /></label><label class="checkbox-label"><input v-model="prefs.volume_enabled" type="checkbox" @change="save" />主指数成交量</label><label v-if="prefs.symbols.length>1" class="checkbox-label"><input v-model="prefs.normalize" type="checkbox" @change="save" />共同起点归一化</label></div>
    </div>
    <article class="panel market-plot"><div class="market-section-heading chart-heading"><h3>行情</h3><label>行情周期<select v-model="prefs.period" aria-label="行情周期" @change="save"><option value="d">日K</option><option value="w">周K</option><option value="m">月K</option></select></label></div><MarketChart v-if="history" :history="priceHistory" :catalog="state?.catalog || []" /><div v-else class="empty-chart">正在读取市场缓存…</div></article>
    <div class="market-data-row"><span v-for="entry in history?.coverage?.filter((r:any)=>prefs.symbols.includes(r.symbol)) || []" :key="entry.symbol" :title="entry.source">{{ state?.catalog.find((r:any)=>r.symbol===entry.symbol)?.name }} {{ entry.first_date }} — {{ entry.last_date }}</span></div>
    </section>
    -->
    <div class="panel market-cache-status">
      <details class="market-static-history"><summary>个股历史补充数据</summary><div class="market-data-row"><span>沪深未复权静态快照 · 2010—2026-09-24 · 首次约 885 MB，可续传，之后复用本地文件</span><button class="button secondary" :disabled="busy || state?.progress.running" @click="sync('static_history')">{{ state?.static_history?.pending ? '继续下载并补齐' : '下载并补齐个股历史' }}</button><a href="https://github.com/noblechang/a-share-data/releases/tag/FREE_DATA_v0" target="_blank" rel="noreferrer">数据来源</a></div><div v-if="state?.static_history" class="market-data-row"><template v-if="state.static_history.completed"><span>最近导入 {{ state.static_history.checked }} · 新增 {{ state.static_history.imported }} 条</span></template><template v-else><span>{{ state.static_history.message }}</span><span>已下载 {{ ((state.static_history.downloaded || 0)/1048576).toFixed(1) }} / {{ ((state.static_history.size || 0)/1048576).toFixed(1) }} MB</span><span v-if="state.static_history.files">已读取 {{ state.static_history.files }} 个文件</span></template></div></details>
      <div class="market-data-row"><label class="checkbox-label"><input type="checkbox" :checked="state?.background?.enabled ?? true" @change="background(($event.target as HTMLInputElement).checked)" />自动回补指标历史</label><span v-if="state?.background?.plan">{{ state.background.plan.start }} — {{ state.background.plan.end }}</span><span v-if="state?.history_pending?.count">待补齐 {{ state.history_pending.count }} 个交易日<span v-if="state.history_pending.latest"> · {{ state.history_pending.latest.date }} 样本完整度 {{ (state.history_pending.latest.coverage*100).toFixed(2) }}%</span></span></div>
      <div v-if="state?.network?.retry_at>Date.now()/1000" class="market-data-row" role="status"><span>{{ state.network.message || '同步已暂缓' }} · 预计 {{ new Date(state.network.retry_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) }} 继续</span></div>
    </div>
    <IndicatorGuide />
  </section>
</template>
