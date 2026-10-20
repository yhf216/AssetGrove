<script setup lang="ts">
import { onMounted, onBeforeUnmount, watch, ref } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, PieChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { fontSize, resolvedTheme } from '../appearance'
echarts.use([LineChart, PieChart, BarChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, CanvasRenderer])
const props = defineProps<{ option: any }>()
const el = ref<HTMLElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver
function update() {
  const dark = resolvedTheme.value === 'dark'
  const ink = dark ? '#b9cabe' : '#68766d', surface = dark ? '#202b25' : '#ffffff', border = dark ? '#39483e' : '#e4eae3'
  const value = { ...props.option, textStyle: { fontSize: fontSize.value * .875 } }
  value.color = dark ? (value.color?.length > 1 ? ['#80bfa1','#a5cbb2','#c1d4ad','#d3bd8a','#8db6c5'] : ['#93c6a3']) : props.option.color
  value.tooltip = { ...value.tooltip, backgroundColor:surface, borderColor:border, textStyle:{color:ink,fontSize:fontSize.value*.875} }
  if (value.xAxis) value.xAxis = { ...value.xAxis, axisLabel: { ...value.xAxis.axisLabel, color:ink, fontSize:fontSize.value * .875 } }
  if (value.yAxis) value.yAxis = { ...value.yAxis, axisLabel: { ...value.yAxis.axisLabel, color:ink, fontSize:fontSize.value * .875 }, splitLine:{lineStyle:{color:border,type:'dashed'}} }
  if (value.grid) value.grid = { ...value.grid, left:Math.max(value.grid.left || 0,fontSize.value*4), bottom:fontSize.value*2 }
  value.series = value.series?.map((series: any) => ({...series,
    ...(series.type === 'pie' ? {itemStyle:{...series.itemStyle,borderColor:surface}} : {}),
    ...(series.areaStyle && dark ? {areaStyle:{color:'#80bfa1',opacity:.12}} : {})
  }))
  chart?.setOption(value,true)
}
onMounted(() => { chart = echarts.init(el.value); update(); observer = new ResizeObserver(() => chart?.resize()); observer.observe(el.value!) })
watch([() => props.option, fontSize, resolvedTheme], update, { deep: true })
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>
<template><div ref="el" class="chart" role="img" aria-label="资产与分红统计图表"></div></template>
