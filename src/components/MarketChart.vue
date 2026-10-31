<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { fontSize, resolvedTheme } from '../appearance'

echarts.use([CandlestickChart,LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps<{ history:any; catalog:any[] }>()
const host=ref<HTMLElement>(), range=ref('')
let chart:echarts.ECharts|undefined,observer:ResizeObserver,zoom:any=null,lastKey=''
const key=computed(()=>JSON.stringify([props.history?.preferences?.symbols,props.history?.preferences?.period,props.history?.preferences?.normalize]))
function update(){
  if(!chart || !props.history)return
  const prefs=props.history.preferences, all=props.history.series || {},symbols:string[]=prefs.symbols
  const primary=symbols[0], normalized=symbols.length>1 && prefs.normalize
  const available=symbols.map(s=>all[s]?.[0]?.date).filter(Boolean)
  const common=available.length===symbols.length ? available.sort().at(-1) : null
  const dates:string[]=Array.from(new Set<string>([...symbols.flatMap(s=>(all[s] || []).map((b:any)=>b.date)),...(prefs.trin_enabled?(props.history.trin || []).map((r:any)=>r.date):[]),...(prefs.erp_enabled?(props.history.erp || []).map((r:any)=>r.date):[])])).sort().filter(day=>!normalized || !common || day>=common)
  const dark=resolvedTheme.value==='dark',ink=dark?'#b9cabe':'#68766d',border=dark?'#39483e':'#e4eae3'
  const colors=dark?['#93c6a3','#8db6c5','#c4a8dc','#d68e8e','#8ec2bf']:['#36775d','#377d9d','#8862ad','#b36363','#36857f']
  const series:any[]=[],bySymbol:Record<string,Map<string,any>>={}
  for(const [index,symbol] of symbols.entries()){
    const bars=all[symbol] || [],byDate=new Map<string,any>(bars.map((b:any)=>[b.date,b]));bySymbol[symbol]=byDate
    const first=bars.find((b:any)=>!common || b.date>=common)?.close
    const value=(v:any)=>v==null?null:normalized && first ? v/first*100 : v
    const name=props.catalog.find(c=>c.symbol===symbol)?.name || symbol
    const candle=symbols.length===1 && prefs.chart_type==='candlestick'
    series.push({name,type:candle?'candlestick':'line',xAxisIndex:0,yAxisIndex:0,
      data:dates.map(day=>{const b=byDate.get(day);return candle?(b?[b.open,b.close,b.low,b.high]:['-','-','-','-']):b?value(b.close):null}),
      showSymbol:dates.length<10,symbolSize:5,connectNulls:false,progressive:1000,
      ...(candle?{itemStyle:{color:'#d96666',color0:'#4c9d7b',borderColor:'#d96666',borderColor0:'#4c9d7b'}}:{lineStyle:{width:1.5},color:colors[index%colors.length]})})
    if(prefs.ma_enabled && symbol===primary){
      for(const [i,n] of prefs.ma_periods.entries())series.push({name:`M${n}（日）`,type:'line',showSymbol:false,connectNulls:false,lineStyle:{width:1,type:'dashed',color:['#8a9385','#a58ab8','#6f9ba8','#ba9292'][i%4]},itemStyle:{color:['#8a9385','#a58ab8','#6f9ba8','#ba9292'][i%4]},xAxisIndex:0,yAxisIndex:0,data:dates.map(day=>value(byDate.get(day)?.ma?.[String(n)]))})
    }
  }
  const trin=new Map<string,any>((props.history.trin || []).map((r:any)=>[r.date,r]))
  if(prefs.trin_enabled)series.push({name:prefs.period==='d'?'TRIN':'TRIN（日值均值）',type:'line',xAxisIndex:0,yAxisIndex:1,connectNulls:false,showSymbol:true,symbolSize:6,lineStyle:{width:1.4,color:'#bd9147'},itemStyle:{color:'#bd9147'},data:dates.map(day=>trin.get(day)?.value ?? null),markLine:{silent:true,symbol:'none',label:{formatter:'TRIN = 1'},lineStyle:{color:'#bd9147',type:'dashed'},data:[{yAxis:1}]}})
  const erp=new Map<string,any>((props.history.erp || []).map((r:any)=>[r.date,r]))
  if(prefs.erp_enabled){
    const monthly=props.history.erp_frequency==='monthly',segments:any[][]=[[]]
    for(const row of props.history.erp || []){
      if(monthly && row.value==null){if(segments.at(-1)!.length)segments.push([])}
      else segments.at(-1)!.push(row)
    }
    for(const segment of (monthly?segments:[props.history.erp || []])){
      const values=new Map<string,any>(segment.map((r:any)=>[r.date,r]))
      series.push({name:monthly?'ERP（月度·沪深300）':prefs.period==='d'?'ERP（沪深300）':'ERP（日值均值）',type:'line',xAxisIndex:0,yAxisIndex:2,connectNulls:monthly,showSymbol:true,symbolSize:6,lineStyle:{width:1.5,color:'#7b72b4'},itemStyle:{color:'#7b72b4'},data:dates.map(day=>values.get(day)?.value ?? null),tooltip:{formatter:(p:any)=>`${p.seriesName}<br/>${erp.get(p.name)?.observed_date || p.name}<br/>${Number(p.value).toFixed(2)} 个百分点`}})
    }
  }
  if(prefs.volume_enabled)series.push({name:'主指数成交量',type:'bar',xAxisIndex:1,yAxisIndex:prefs.erp_enabled?3:2,progressive:1000,barMaxWidth:8,data:dates.map(day=>{const b=bySymbol[primary]?.get(day);return b?{value:b.volume,itemStyle:{color:b.close>=b.open?'#d96666':'#4c9d7b'}}:null})})
  if(lastKey!==key.value){zoom=null;lastKey=key.value}
  const axes=[0,...(prefs.volume_enabled?[1]:[])]
  const cutoff=new Date((dates.at(-1) || '2000-01-01')+'T00:00:00Z');cutoff.setUTCDate(cutoff.getUTCDate()-30)
  const monthStart=dates.findIndex(day=>day>=cutoff.toISOString().slice(0,10))
  const initialZoom={start:Math.max(0,monthStart)/Math.max(1,dates.length-1)*100,end:100}
  const right=Math.max(prefs.trin_enabled && prefs.erp_enabled?124:64,fontSize.value*3)
  chart.setOption({animation:false,color:colors,backgroundColor:'transparent',textStyle:{fontSize:fontSize.value*.8,color:ink},
    legend:{type:'scroll',top:5,left:12,right:12,textStyle:{color:ink,fontSize:fontSize.value*.8}},
    tooltip:{className:'market-value-tooltip',trigger:'item',triggerOn:'mousemove',showDelay:0,hideDelay:0,alwaysShowContent:false,enterable:false,confine:true,axisPointer:{type:'none'},backgroundColor:dark?'#202b25':'#fff',borderColor:border,textStyle:{color:ink,fontSize:fontSize.value*.8}},
    grid:[{left:Math.max(64,fontSize.value*3.5),right,top:62,height:prefs.volume_enabled?'57%':'76%'},...(prefs.volume_enabled?[{left:Math.max(64,fontSize.value*3.5),right,top:'76%',height:'12%'}]:[])],
    xAxis:axes.map(index=>({type:'category',gridIndex:index,data:dates,boundaryGap:true,axisLabel:{show:index===axes.at(-1),color:ink,fontSize:fontSize.value*.75,hideOverlap:true},axisLine:{lineStyle:{color:border}},axisTick:{show:false}})),
    yAxis:[{type:'value',gridIndex:0,scale:true,name:normalized?'基准 100':'指数点位',nameTextStyle:{color:ink},axisLabel:{color:ink},splitLine:{lineStyle:{color:border,type:'dashed'}}},
      {type:'value',gridIndex:0,scale:true,position:'right',show:prefs.trin_enabled,name:'TRIN',nameTextStyle:{color:'#bd9147'},axisLabel:{color:'#bd9147'},splitLine:{show:false}},
      ...(prefs.erp_enabled?[{type:'value',gridIndex:0,scale:true,position:'right',offset:prefs.trin_enabled?60:0,name:'ERP（百分点）',nameTextStyle:{color:'#7b72b4'},axisLabel:{color:'#7b72b4',formatter:(v:number)=>v.toFixed(1)},splitLine:{show:false}}]:[]),
      ...(prefs.volume_enabled?[{type:'value',gridIndex:1,scale:true,splitNumber:2,name:'成交量（股）',nameTextStyle:{color:ink},axisLabel:{color:ink,formatter:(v:number)=>Math.abs(v)>=1e8?(v/1e8).toFixed(1)+'亿':Math.round(v/1e4)+'万'},splitLine:{show:false}}]:[])],
    dataZoom:[{type:'inside',xAxisIndex:axes,filterMode:'filter',zoomOnMouseWheel:'ctrl',moveOnMouseMove:true,moveOnMouseWheel:false,...(zoom || initialZoom)},
      {type:'slider',xAxisIndex:axes,filterMode:'filter',bottom:9,height:22,borderColor:border,textStyle:{color:ink},...(zoom || initialZoom)}],series},true)
  chart.dispatchAction({type:'hideTip'})
  updateRange()
}
function updateRange(){
  const options=chart?.getOption() as any,dates=options?.xAxis?.[0]?.data || [],z=options?.dataZoom?.[0]
  if(!dates.length){range.value='暂无历史数据';return}
  const first=Math.max(0,Math.floor((z?.start || 0)/100*(dates.length-1))),last=Math.min(dates.length-1,Math.ceil((z?.end ?? 100)/100*(dates.length-1)))
  range.value=`显示区间 ${dates[first]} — ${dates[last]}`
}
function reset(){zoom=null;update()}
function handleWheel(event:WheelEvent){if(!event.ctrlKey)event.stopPropagation()}
onMounted(()=>{chart=echarts.init(host.value);chart.on('datazoom',()=>{const value=(chart!.getOption() as any).dataZoom?.[0];if(value)zoom={start:value.start,end:value.end};updateRange()});chart.getZr().on('mousemove',(event:any)=>{if(!event.target)chart?.dispatchAction({type:'hideTip'})});chart.getZr().on('globalout',()=>chart?.dispatchAction({type:'hideTip'}));observer=new ResizeObserver(()=>chart?.resize());observer.observe(host.value!);update()})
watch([()=>props.history,fontSize,resolvedTheme],update,{deep:false})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div class="market-chart-wrap"><div class="market-chart-meta"><span>{{ range }}</span><button class="text-button" @click="reset">重置视图</button></div><div ref="host" class="market-chart" role="img" aria-label="市场图表，可拖动平移，按住 Ctrl 滚轮缩放" @wheel.capture.passive="handleWheel" /></div></template>
