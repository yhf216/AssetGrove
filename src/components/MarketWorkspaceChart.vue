<script setup lang="ts">
import { computed,onMounted,onBeforeUnmount,ref,watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart,CandlestickChart,BarChart } from 'echarts/charts'
import { GridComponent,TooltipComponent,DataZoomComponent,LegendComponent,MarkLineComponent,MarkAreaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { fontSize,resolvedTheme } from '../appearance'
import { workspaceOption, type Metric } from '../marketWorkspace'

echarts.use([LineChart,CandlestickChart,BarChart,GridComponent,TooltipComponent,DataZoomComponent,LegendComponent,MarkLineComponent,MarkAreaComponent,CanvasRenderer])
const props=defineProps<{metrics:Metric[];bars:any[];indexName:string;indexMode:string;period:string;viewKey:string;volume:boolean;ma:boolean;maPeriods:number[]}>()
const host=ref<HTMLElement>(),range=ref('')
const current=computed(()=>props.metrics[0]?.rows.filter(r=>r.value!=null).at(-1))
const missing=computed(()=>props.metrics.filter(m=>!m.rows.some(r=>r.value!=null)).map(m=>m.name))
const minimumWidth=computed(()=>Math.max(600,props.metrics.length*90+(props.indexMode==='none'?400:490)))
let chart:echarts.ECharts|undefined,observer:ResizeObserver,lastKey='',windowDates:{first:string;last:string}|null=null
function update(){
  if(!chart)return
  if(lastKey!==props.viewKey){windowDates=null;lastKey=props.viewKey}
  const option=workspaceOption({...props,dark:resolvedTheme.value==='dark',fontSize:fontSize.value,windowDates})
  chart.setOption(option as any,true)
  chart.dispatchAction({type:'hideTip'});updateRange()
}
function updateRange(remember=false){
  const option=chart?.getOption() as any,dates=option?.xAxis?.[0]?.data || [],z=option?.dataZoom?.[0]
  if(!dates.length){range.value='暂无历史数据';return}
  const first=dates[Math.max(0,Math.floor((z?.start || 0)/100*(dates.length-1)+1e-7))]
  const last=dates[Math.min(dates.length-1,Math.ceil((z?.end ?? 100)/100*(dates.length-1)-1e-7))]
  range.value=`${first} — ${last}`
  if(remember)windowDates={first,last}
}
function reset(){windowDates=null;update()}
onMounted(()=>{
  chart=echarts.init(host.value)
  chart.on('datazoom',()=>updateRange(true))
  chart.getZr().on('mousemove',(e:any)=>{if(!e.target)chart?.dispatchAction({type:'hideTip'})})
  chart.getZr().on('globalout',()=>chart?.dispatchAction({type:'hideTip'}))
  observer=new ResizeObserver(()=>chart?.resize());observer.observe(host.value!);update()
})
watch([()=>props.metrics,()=>props.bars,()=>props.indexMode,()=>props.period,()=>props.viewKey,()=>props.volume,()=>props.ma,()=>props.maPeriods,fontSize,resolvedTheme],update)
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template>
  <article class="panel workspace-plot">
    <div class="indicator-panel-top"><h3>{{ metrics[0]?.name }}</h3><span v-if="current" class="indicator-current"><strong>{{ Number(current.value).toFixed(2) }} {{ metrics[0]?.unit }}</strong><b v-if="current.state" class="trend-state">{{ current.state }}</b><span>{{ current.date }}</span></span></div>
    <div class="market-chart-meta"><span>{{ range }}</span><button class="text-button" @click="reset">重置视图</button></div>
    <p v-if="missing.length" class="market-muted workspace-missing" role="status">{{ missing.join('、') }} 暂无完整数据，等待补齐。</p>
    <div class="workspace-chart-scroll"><div ref="host" class="workspace-chart" :style="{minWidth:minimumWidth+'px'}" role="img" aria-label="市场指标图表，可拖动平移，使用底部滑块缩放" @wheel.capture.passive="$event.stopPropagation()" /></div>
  </article>
</template>
