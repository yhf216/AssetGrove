<script setup lang="ts">
import { computed,onMounted,onBeforeUnmount,ref,watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent,TooltipComponent,DataZoomComponent,LegendComponent,MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { fontSize,resolvedTheme } from '../appearance'

echarts.use([LineChart,GridComponent,TooltipComponent,DataZoomComponent,LegendComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps<{rows:any[];title:string;unit:string;color?:string;reference?:any[];referenceName?:string;baseline?:number;months?:number;viewKey?:string}>()
const host=ref<HTMLElement>(),overlay=ref(false),range=ref('')
const points=computed(()=>props.rows.filter(r=>r.value!=null))
const latest=computed(()=>points.value.at(-1))
let chart:echarts.ECharts|undefined,observer:ResizeObserver,zoom:any=null,lastKey=''
function update(){
  if(!chart)return
  const key=props.viewKey || props.title
  if(key!==lastKey){zoom=null;lastKey=key}
  const values=new Map(props.rows.map(r=>[r.date,r]))
  const reference=new Map((props.reference || []).map(r=>[r.date,r]))
  const dates=Array.from(values.keys()).sort()
  const ink=resolvedTheme.value==='dark'?'#b9cabe':'#68766d',border=resolvedTheme.value==='dark'?'#39483e':'#e4eae3',color=props.color || '#bd9147'
  const cut=new Date((dates.at(-1) || '2000-01-01')+'T00:00:00Z');cut.setUTCMonth(cut.getUTCMonth()-(props.months || 1))
  const start=Math.max(0,dates.findIndex(day=>day>=cut.toISOString().slice(0,10)))/Math.max(1,dates.length-1)*100
  const escape=(v:any)=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!))
  const series:any[]=[{name:props.title,type:'line',data:dates.map(day=>values.get(day)?.value ?? null),connectNulls:false,showSymbol:true,symbolSize:5,lineStyle:{width:1.6,color},itemStyle:{color},
    tooltip:{formatter:(p:any)=>{
      const row=values.get(p.name)
      return `${escape(props.title)}<br/>${escape(p.name)}<br/>${Number(p.value).toFixed(2)} ${escape(props.unit)}${row?.state?'<br/>'+escape(row.state):''}${row?.ma200!=null?'<br/>MA50：'+row.ma50.toFixed(2)+'<br/>MA200：'+row.ma200.toFixed(2):''}`
    }},...(props.baseline!=null?{markLine:{silent:true,symbol:'none',lineStyle:{color,type:'dashed'},label:{formatter:String(props.baseline)},data:[{yAxis:props.baseline}]}}:{})}]
  if(overlay.value)series.push({name:props.referenceName || '主指数',type:'line',yAxisIndex:1,data:dates.map(day=>reference.get(day)?.close ?? null),connectNulls:false,showSymbol:false,lineStyle:{width:1,color:'#65937b',opacity:.75},itemStyle:{color:'#65937b'}})
  chart.setOption({animation:false,textStyle:{fontSize:fontSize.value*.8,color:ink},legend:{top:8,textStyle:{color:ink}},
    tooltip:{className:'indicator-value-tooltip',trigger:'item',triggerOn:'mousemove',hideDelay:0,alwaysShowContent:false,enterable:false,confine:true,backgroundColor:resolvedTheme.value==='dark'?'#202b25':'#fff',borderColor:border,textStyle:{color:ink}},
    grid:{left:72,right:overlay.value?85:30,top:56,bottom:65},
    xAxis:{type:'category',data:dates,boundaryGap:false,axisLabel:{color:ink,hideOverlap:true},axisLine:{lineStyle:{color:border}}},
    yAxis:[{type:'value',scale:true,name:props.unit,...(props.baseline!=null?{min:(v:any)=>Math.min(v.min,props.baseline!),max:(v:any)=>Math.max(v.max,props.baseline!)}:{}),nameTextStyle:{color:ink},axisLabel:{color:ink},splitLine:{lineStyle:{color:border,type:'dashed'}}},{type:'value',show:overlay.value,scale:true,name:'指数点位',axisLabel:{color:ink},splitLine:{show:false}}],
    dataZoom:[{type:'inside',filterMode:'filter',zoomOnMouseWheel:false,moveOnMouseWheel:false,moveOnMouseMove:true,...(zoom || {start,end:100})},{type:'slider',bottom:10,height:22,...(zoom || {start,end:100})}],series},true)
  chart.dispatchAction({type:'hideTip'});updateRange()
}
function updateRange(){
  const option=chart?.getOption() as any,dates=option?.xAxis?.[0]?.data || [],z=option?.dataZoom?.[0]
  range.value=dates.length?`${dates[Math.max(0,Math.floor((z?.start || 0)/100*(dates.length-1)))]} — ${dates[Math.min(dates.length-1,Math.ceil((z?.end ?? 100)/100*(dates.length-1))) ]}`:''
}
function reset(){zoom=null;update()}
onMounted(()=>{chart=echarts.init(host.value);chart.on('datazoom',()=>{const z=(chart!.getOption() as any).dataZoom?.[0];if(z)zoom={start:z.start,end:z.end};updateRange()});chart.getZr().on('mousemove',(e:any)=>{if(!e.target)chart?.dispatchAction({type:'hideTip'})});chart.getZr().on('globalout',()=>chart?.dispatchAction({type:'hideTip'}));observer=new ResizeObserver(()=>chart?.resize());observer.observe(host.value!);update()})
watch([()=>props.rows,()=>props.reference,()=>props.viewKey,overlay,fontSize,resolvedTheme],update)
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template>
  <article class="panel indicator-panel" :data-indicator="title">
    <div class="indicator-panel-top"><h3>{{ title }}</h3><span v-if="latest" class="indicator-current"><strong>{{ Number(latest.value).toFixed(2) }} {{ unit }}</strong><b v-if="latest.state" class="trend-state">{{ latest.state }}</b><span>{{ latest.date }}</span></span><label v-if="reference?.length" class="checkbox-label"><input v-model="overlay" type="checkbox" />叠加{{ referenceName || '主指数' }}</label></div>
    <div v-if="points.length" class="market-chart-meta"><span>{{ range }}</span><button class="text-button" @click="reset">重置视图</button></div>
    <p v-else class="indicator-empty">暂无完整数据，等待指数历史或指标数据补齐。</p>
    <div v-show="points.length" ref="host" class="indicator-chart" role="img" :aria-label="title+'图表，可拖动平移，使用底部滑块缩放'" @wheel.capture.passive="$event.stopPropagation()" />
  </article>
</template>
