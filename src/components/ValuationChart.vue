<script setup lang="ts">
import { onMounted,onBeforeUnmount,ref,watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent,TooltipComponent,DataZoomComponent,MarkAreaComponent,MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { fontSize,resolvedTheme } from '../appearance'

echarts.use([LineChart,GridComponent,TooltipComponent,DataZoomComponent,MarkAreaComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps<{ valuation:any }>(),host=ref<HTMLElement>()
let chart:echarts.ECharts|undefined,observer:ResizeObserver,zoom:any=null,lastKey=''
function update(){
  if(!chart)return
  const value=props.valuation,stats=value?.statistics,series=value?.series || []
  if(!stats || !series.length){chart.clear();return}
  const key=JSON.stringify([value.symbol,value.period,value.years]);if(key!==lastKey){zoom=null;lastKey=key}
  const dark=resolvedTheme.value==='dark',ink=dark?'#b9cabe':'#68766d',border=dark?'#39483e':'#e4eae3',blue=dark?'#91b9df':'#497faa'
  chart.setOption({animation:false,textStyle:{fontSize:fontSize.value*.8,color:ink},
    tooltip:{className:'valuation-value-tooltip',trigger:'item',triggerOn:'mousemove',hideDelay:0,alwaysShowContent:false,enterable:false,confine:true,
      formatter:(p:any)=>`${p.name}<br/>PE-TTM：${Number(p.value).toFixed(2)} 倍`,backgroundColor:dark?'#202b25':'#fff',borderColor:border,textStyle:{color:ink}},
    grid:{left:64,right:96,top:30,bottom:65},
    xAxis:{type:'category',data:series.map((r:any)=>r.date),boundaryGap:false,axisLabel:{color:ink,hideOverlap:true},axisLine:{lineStyle:{color:border}}},
    yAxis:{type:'value',scale:true,name:'PE-TTM（倍）',nameTextStyle:{color:ink},axisLabel:{color:ink},splitLine:{lineStyle:{color:border,type:'dashed'}}},
    dataZoom:[{type:'inside',filterMode:'filter',zoomOnMouseWheel:false,moveOnMouseMove:true,moveOnMouseWheel:false,...(zoom || {start:0,end:100})},{type:'slider',bottom:10,height:22,...(zoom || {start:0,end:100})}],
    series:[{name:'PE-TTM',type:'line',data:series.map((r:any)=>r.pe),showSymbol:true,symbolSize:3,connectNulls:false,lineStyle:{color:blue,width:1.8},itemStyle:{color:blue},
      markArea:{silent:true,itemStyle:{color:dark?'rgba(120,160,190,.12)':'rgba(73,127,170,.10)'},data:[[{yAxis:stats.lower1},{yAxis:stats.upper1}]]},
      markLine:{silent:true,symbol:'none',lineStyle:{color:ink,type:'dashed',width:1},label:{color:ink,formatter:(p:any)=>p.name},data:[{name:'均值',yAxis:stats.mean},{name:'−1σ',yAxis:stats.lower1},{name:'+1σ',yAxis:stats.upper1},{name:'−2σ',yAxis:stats.lower2},{name:'+2σ',yAxis:stats.upper2}]}}]},true)
  chart.dispatchAction({type:'hideTip'})
}
onMounted(()=>{chart=echarts.init(host.value);chart.on('datazoom',()=>{const z=(chart!.getOption() as any).dataZoom?.[0];if(z)zoom={start:z.start,end:z.end}});chart.getZr().on('mousemove',(e:any)=>{if(!e.target)chart?.dispatchAction({type:'hideTip'})});chart.getZr().on('globalout',()=>chart?.dispatchAction({type:'hideTip'}));observer=new ResizeObserver(()=>chart?.resize());observer.observe(host.value!);update()})
watch([()=>props.valuation,fontSize,resolvedTheme],update)
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="host" class="valuation-chart" role="img" aria-label="PE-TTM 估值通道，可拖动和缩放" @wheel.capture.passive="$event.stopPropagation()" /></template>
