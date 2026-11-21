export type Metric = {id:string;name:string;axis:string;unit:string;color:string;rows:any[];baseline?:number;statistics?:any;months:number}
export type WindowDates = {first:string;last:string;latestSpan?:number}
type Input = {metrics:Metric[];bars:any[];indexName:string;indexMode:string;period:string;viewKey:string;volume:boolean;ma:boolean;maPeriods:number[];dark:boolean;fontSize:number;windowDates:WindowDates|null;recent?:boolean;allVisible?:boolean}

// Align weekly/monthly observations by their real period, retaining original dates.
export function periodKey(day:string,period:string):string {
  if(period==='d')return day
  if(period==='m')return day.slice(0,7)
  const date=new Date(day+'T00:00:00Z'),weekday=(date.getUTCDay()+6)%7
  date.setUTCDate(date.getUTCDate()-weekday)
  return date.toISOString().slice(0,10)
}
const escape=(value:any)=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!))

function monthsBefore(day:string,months:number){
  const end=new Date(day+'T00:00:00Z')
  const first=new Date(Date.UTC(end.getUTCFullYear(),end.getUTCMonth()-months,1))
  const lastDay=new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0)).getUTCDate()
  first.setUTCDate(Math.min(end.getUTCDate(),lastDay))
  return first.toISOString().slice(0,10)
}

export function workspaceGutters(metrics:Metric[],overlay:boolean,fontSize:number){
  const titles=[...metrics.map(m=>m.axis),...(overlay?['指数点位']:[])]
  const halfTitle=Math.max(0,...titles.map(title=>Array.from(title).reduce((width,char)=>width+(/[\x00-\x7F]/.test(char) ? .6 : 1),0)))*fontSize*.36
  const ticks=fontSize*.72*4.8+12
  const margin=Math.ceil(Math.max(58,halfTitle+12,ticks))
  const step=Math.ceil(Math.max(margin+8,2*halfTitle+14))
  const left=margin+Math.max(0,Math.ceil(titles.length/2)-1)*step
  const right=titles.length>1?margin+Math.max(0,Math.floor(titles.length/2)-1)*step:28
  return {left,right,step,minimumWidth:Math.max(600,left+right+440)}
}

export function workspaceOption(input:Input){
  const {metrics,bars,indexName,indexMode,period,volume,ma,maPeriods,dark,fontSize}=input
  const overlay=indexMode!=='none',showVolume=overlay && volume
  const ink=dark?'#b9cabe':'#68766d',border=dark?'#39483e':'#e4eae3'
  const observations=new Map<string,string>()
  const remember=(row:any)=>{const key=periodKey(row.date,period),previous=observations.get(key);if(!previous || row.date>previous)observations.set(key,row.date)}
  const observedDays=metrics.flatMap(metric=>metric.rows.filter(row=>row.value!=null).map(row=>row.date)).sort()
  const firstObservation=observedDays[0],lastObservation=observedDays.at(-1)
  // Keep internal gaps and align them with price sessions when price is hidden.
  const insideMetrics=(r:any)=>firstObservation && lastObservation && r.date>=firstObservation && r.date<=lastObservation
  for(const metric of metrics)metric.rows.filter(r=>r.value!=null || insideMetrics(r)).forEach(remember)
  bars.filter(r=>overlay || insideMetrics(r)).forEach(remember)
  let keys=Array.from(observations.keys()).sort()
  const newest=observations.get(keys.at(-1)!) || '2000-01-01'
  if(input.recent && keys.length>24){
    const near=periodKey(monthsBefore(newest,3),period),index=keys.findIndex(key=>key>=near)
    keys=keys.slice(Math.min(index<0?keys.length:index,keys.length-24))
  }
  const dates=keys.map(k=>observations.get(k)!)
  const {left,right,step}=workspaceGutters(metrics,overlay,fontSize)
  const axes:any[]=[],series:any[]=[]
  const axis=(name:string,color:string,index:number,low?:number,high=low)=>({type:'value',gridIndex:0,scale:true,position:index%2?'right':'left',offset:Math.floor(index/2)*step,
    name,nameLocation:'end',nameGap:12,nameTextStyle:{color,fontSize:fontSize*.72,align:'center'},axisLabel:{color,fontSize:fontSize*.72,margin:8,hideOverlap:true,formatter:(v:number)=>Math.abs(v)>=1e5?(v/1e4).toFixed(1)+'万':Number(v.toFixed(2)).toString()},axisLine:{show:true,lineStyle:{color}},axisTick:{show:false},splitLine:{show:index===0,lineStyle:{color:border,type:'dashed'}},
    ...(low!=null && high!=null?{min:(v:any)=>Math.min(v.min,low),max:(v:any)=>Math.max(v.max,high)}:{})})
  for(const [index,metric] of metrics.entries()){
    const byPeriod=new Map(metric.rows.map(r=>[periodKey(r.date,period),r]))
    axes.push(axis(metric.axis,metric.color,index,metric.statistics?.lower2 ?? metric.baseline,metric.statistics?.upper2))
    const line:any={id:metric.id,name:metric.name,type:'line',yAxisIndex:index,z:4+index,data:keys.map(k=>byPeriod.get(k)?.value ?? null),connectNulls:false,showSymbol:true,symbolSize:5,lineStyle:{width:index===0?2:1.4,color:metric.color},itemStyle:{color:metric.color},
      tooltip:{formatter:(p:any)=>{const row=byPeriod.get(periodKey(p.name,period));return `${escape(metric.name)}<br/>${escape(row?.date || p.name)}<br/>${Number(p.value).toFixed(2)} ${escape(metric.unit)}${row?.state?'<br/>'+escape(row.state):''}${row?.ma200!=null?'<br/>MA50：'+row.ma50.toFixed(2)+'<br/>MA200：'+row.ma200.toFixed(2):''}${row?.vwma!=null?'<br/>VWMA20：'+row.vwma.toFixed(2)+'<br/>SMA20：'+row.sma.toFixed(2):''}`}}}
    if(metric.baseline!=null)line.markLine={silent:true,symbol:'none',label:{show:false},lineStyle:{color:metric.color,type:'dashed'},data:[{yAxis:metric.baseline}]}
    if(metric.statistics){
      const stats=metric.statistics
      line.markArea={silent:true,itemStyle:{color:dark?'rgba(120,160,190,.12)':'rgba(73,127,170,.10)'},data:[[{yAxis:stats.lower1},{yAxis:stats.upper1}]]}
      line.markLine={silent:true,symbol:'none',lineStyle:{color:metric.color,type:'dashed',width:1},label:{show:false},data:[['均值',stats.mean],['−1σ',stats.lower1],['+1σ',stats.upper1],['−2σ',stats.lower2],['+2σ',stats.upper2]].map(([name,yAxis])=>({name,yAxis}))}
    }
    series.push(line)
  }
  const byPeriod=new Map(bars.map(r=>[periodKey(r.date,period),r]))
  if(overlay){
    const index=axes.length,candle=indexMode==='candlestick'
    axes.push(axis('指数点位',ink,index))
    series.push({id:'index',name:indexName,type:candle?'candlestick':'line',yAxisIndex:index,z:2,
      data:keys.map(k=>{const b=byPeriod.get(k);return candle?(b?[b.open,b.close,b.low,b.high]:['-','-','-','-']):b?.close ?? null}),
      showSymbol:!candle,symbolSize:3,connectNulls:false,lineStyle:{width:1.2,color:'#7f9c8c'},itemStyle:candle?{color:'#d96666',color0:'#4c9d7b',borderColor:'#d96666',borderColor0:'#4c9d7b'}:{color:'#7f9c8c'},
      tooltip:{formatter:(p:any)=>{
        const b=byPeriod.get(periodKey(p.name,period));if(!b)return ''
        const change=typeof b.change_pct==='number'?`${b.change_pct>0?'+':''}${b.change_pct.toFixed(2)}%`:'暂无前收盘价'
        const label=period==='w'?'周涨跌幅':period==='m'?'月涨跌幅':'当日涨跌幅'
        return `${escape(indexName)}<br/>${escape(b.date)}<br/>开盘：${b.open.toFixed(2)}<br/>收盘：${b.close.toFixed(2)}<br/>最低：${b.low.toFixed(2)}<br/>最高：${b.high.toFixed(2)}<br/>${label}：${change}`
      }}})
    if(ma)for(const [i,n] of maPeriods.entries())series.push({id:'ma'+n,name:'M'+n+'（日）',type:'line',yAxisIndex:index,z:3,showSymbol:false,connectNulls:false,lineStyle:{width:1,type:'dashed',color:['#a58ab8','#6f9ba8','#ba9292','#8a9385'][i%4]},data:keys.map(k=>byPeriod.get(k)?.ma?.[String(n)] ?? null)})
  }
  if(showVolume){
    axes.push({type:'value',gridIndex:1,scale:true,splitNumber:2,name:'成交量（股）',axisLabel:{color:ink,formatter:(v:number)=>v>=1e8?(v/1e8).toFixed(1)+'亿':Math.round(v/1e4)+'万'},nameTextStyle:{color:ink},splitLine:{show:false}})
    series.push({id:'volume',name:'指数成交量',type:'bar',xAxisIndex:1,yAxisIndex:axes.length-1,barMaxWidth:8,z:1,data:keys.map(k=>{const b=byPeriod.get(k);return b?{value:b.volume,itemStyle:{color:b.close>=b.open?'#d96666':'#4c9d7b'}}:null})})
  }
  const defaultMonths=input.recent?Math.min(3,metrics[0]?.months || 1):(metrics[0]?.months || 1)
  const first=input.windowDates?.first || (input.allVisible?dates[0]:monthsBefore(newest,defaultMonths)) || newest
  const latestSpan=input.windowDates?.latestSpan,last=(latestSpan==null?input.windowDates?.last:newest) || newest
  const located=keys.findIndex(k=>k>=periodKey(first,period))
  const from=latestSpan!=null?Math.max(0,dates.length-latestSpan):located<0?Math.max(0,dates.length-1):located
  let to=dates.length-1
  while(to>from && keys[to]>periodKey(last,period))to--
  to=Math.max(from,to)
  const zoom=dates.length<2?{start:0,end:100}:{start:100*from/(dates.length-1),end:100*to/(dates.length-1)}
  const xAxes=[0,...(showVolume?[1]:[])]
  return {animation:false,textStyle:{fontSize:fontSize*.8,color:ink},legend:{type:'scroll',top:8,left:12,right:12,textStyle:{color:ink,fontSize:fontSize*.8}},
    tooltip:{className:'workspace-value-tooltip',trigger:'item',triggerOn:'mousemove',hideDelay:0,alwaysShowContent:false,enterable:false,confine:true,backgroundColor:dark?'#202b25':'#fff',borderColor:border,textStyle:{color:ink}},
    grid:[{left,right,top:70,bottom:showVolume?'28%':70},...(showVolume?[{left,right,top:'79%',height:'9%'}]:[])],
    xAxis:xAxes.map(i=>({type:'category',gridIndex:i,data:dates,boundaryGap:true,axisLabel:{show:i===xAxes.at(-1),color:ink,hideOverlap:true},axisLine:{lineStyle:{color:border}},axisTick:{show:false}})),yAxis:axes,
    dataZoom:[{type:'inside',xAxisIndex:xAxes,filterMode:'filter',zoomOnMouseWheel:false,moveOnMouseWheel:false,moveOnMouseMove:true,...zoom},{type:'slider',xAxisIndex:xAxes,show:dates.length>1,bottom:10,height:22,showDataShadow:false,...zoom}],series}
}
