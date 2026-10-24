<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { LayoutDashboard, Wallet, ChartNoAxesCombined, Sprout, ArrowUpRight, Plus, RefreshCw, Bell, Download, Upload, Settings2, X, ArrowRight, ChevronLeft, ChevronRight, CalendarDays, Pencil, Trash2, Check, CircleHelp, ShieldCheck, Landmark, TrendingUp, Coins, Search, CheckCircle2, AlertCircle, Leaf } from 'lucide-vue-next'
import Chart from './components/Chart.vue'
import StockPicker from './components/StockPicker.vue'
import RuleStrip from './components/RuleStrip.vue'
import { fontSize, theme, resolvedTheme, applyAppearance } from './appearance'
import { api, connect, download, quitApplication } from './api'

type Page = 'overview' | 'assets' | 'stocks'
const page = ref<Page>('overview'), assetTab = ref('accounts'), stockTab = ref('holdings')
const data = ref<any>(null), connected = ref(false), loading = ref(false), notice = ref(''), error = ref('')
const modal = ref(''), saving = ref(false), search = ref(''), basis = ref('ttm'), selectedStock = ref(''), history = ref<any[]>([])
const selectedPreview = ref<any>(null), previewBusy = ref(false), draggedAccount = ref(''), nativeNotification = ref<any>(null)
const draggedRule = ref('')
let previewVersion = 0
const form = reactive<any>({}), importInput = ref<HTMLInputElement>(), importBackup = ref<any>(null)
const calendarMonth = ref(new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' }).slice(0, 7)), calendarScope = ref('held'), calendarType = ref('pay_date')
let timer: ReturnType<typeof setInterval>, messageTimer: ReturnType<typeof setTimeout>, fontTimer: ReturnType<typeof setTimeout>
const today = () => data.value?.today || new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
const currency = (v: any) => v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const compact = (v: any) => Number(v || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const percent = (v: any) => v == null ? '—' : `${Number(v).toFixed(2)}%`
const priceText = (v: any, symbol: string) => v == null ? '—' : Number(v).toLocaleString('zh-CN',{minimumFractionDigits:/^(5|159)/.test(symbol) ? 3 : 2,maximumFractionDigits:/^(5|159)/.test(symbol) ? 3 : 2})
const kindNames: Record<string, string> = { cash: '现金', broker: '证券账户', stock: '股票', fund: '基金', bond: '债券', deposit: '定期存款' }
const txNames: Record<string, string> = { opening: '初始余额', balance: '余额更新', opening_position: '初始持仓', income: '现金流增量', adjustment: '资产调整', transfer: '账户转账', buy: '买入', sell: '卖出', dividend: '分红到账', dividend_record: '分红记录' }
const basisNames: Record<string, string> = { ttm:'近 12 个月 TTM', last_year: '1 年股息率', avg3: '3 年平均股息率', avg5: '5 年平均股息率' }
const estimateNames: Record<string, string> = { last_year:'1 年', avg3:'3 年平均', avg5:'5 年平均' }
const estimateBasis = computed(() => data.value?.preferences?.dividend_basis || 'avg3')
const summary = computed(() => data.value?.summary || {})
const annualEstimateText = computed(() => summary.value.missing_dividend_positions === data.value?.holdings.length && data.value?.holdings.length ? '资料待同步' : currency(summary.value.annual_estimate))
const hasAccounts = computed(() => !!data.value?.accounts.length)
const accountName = (id: string) => data.value?.accounts.find((a: any) => a.id === id)?.name || '—'
const pending = computed(() => data.value?.entitlements.filter((e: any) => e.state === 'pending') || [])
const filteredWatch = computed(() => (data.value?.watchlist || []).filter((s: any) => `${s.name}${s.symbol}`.includes(search.value)))
const stockKinds = ['buy', 'sell', 'opening_position', 'dividend', 'dividend_record']
const records = computed(() => (data.value?.transactions || []).filter((t: any) => page.value === 'stocks' ? stockKinds.includes(t.kind) : !['buy', 'sell', 'opening_position', 'dividend_record'].includes(t.kind)))
const dividendYear = ref(String(new Date().getFullYear()))
const receivedDividends = computed(() => (data.value?.transactions || []).filter((t: any) => ['dividend', 'dividend_record'].includes(t.kind) && t.date.startsWith(dividendYear.value)))
const allocations = computed(() => Object.entries(summary.value.allocation || {}).map(([kind, value]) => ({ name: kindNames[kind], value: Number(value), kind })).filter(x => x.value > 0))
const palette = computed(() => resolvedTheme.value === 'dark' ? ['#80bfa1','#a5cbb2','#c1d4ad','#d3bd8a','#8db6c5'] : ['#2d6853', '#8ab89a', '#b4cfb0', '#d8c493', '#94b8c3'])
const lineOption = computed(() => ({ animation: false, color: ['#36775d'], tooltip: { trigger: 'axis', valueFormatter: (v: any) => '¥' + currency(v) }, grid: { left: 62, right: 22, top: 24, bottom: 30 }, xAxis: { type: 'category', boundaryGap: false, data: data.value?.timeline.map((x: any) => x.date.slice(5)), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#859089' } }, yAxis: { type: 'value', scale: true, splitNumber: 4, axisLabel: { color: '#859089', formatter: (v: number) => v >= 10000 ? `${compact(v / 10000)}万` : compact(v) }, splitLine: { lineStyle: { color: '#edf0eb', type: 'dashed' } } }, series: [{ type: 'line', smooth: .25, symbol: 'circle', symbolSize: 7, data: data.value?.timeline.map((x: any) => x.value), lineStyle: { width: 2.5 }, areaStyle: { color: '#e7f1e8', opacity: .65 } }] }))
const pieOption = computed(() => ({ animation: false, color: palette.value, tooltip: { trigger: 'item', formatter: (p: any) => `${p.name}<br/>¥${currency(p.value)} · ${p.percent}%` }, series: [{ type: 'pie', radius: ['65%', '86%'], center: ['50%', '50%'], avoidLabelOverlap: true, label: { show: false }, itemStyle: { borderColor: '#fff', borderWidth: 4, borderRadius: 4 }, data: allocations.value }] }))
const barOption = computed(() => ({ animation: false, color: ['#709c7a'], tooltip: { trigger: 'axis', valueFormatter: (v: any) => '¥' + currency(v) }, grid: { left: 52, right: 20, top: 20, bottom: 28 }, xAxis: { type: 'category', data: Array.from({ length: 12 }, (_, i) => `${i + 1}月`), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#859089' } }, yAxis: { type: 'value', splitNumber: 3, axisLabel: { color: '#859089' }, splitLine: { lineStyle: { color: '#edf0eb', type: 'dashed' } } }, series: [{ name: '已确认到账', type: 'bar', barMaxWidth: 24, itemStyle: { borderRadius: [5, 5, 0, 0] }, data: (page.value === 'stocks' ? data.value?.monthly_dividends_by_year?.[dividendYear.value] : data.value?.monthly_dividends) || [] }] }))
const stockHistoryOption = computed(() => ({ ...lineOption.value, xAxis: { ...lineOption.value.xAxis, data: history.value.map(x => x.date.slice(5)) }, series: [{ type: 'line', smooth: false, showSymbol: false, data: history.value.map(x => Number(x.close)), lineStyle: { width: 2 }, areaStyle: { color: '#e7f1e8', opacity: .5 } }] }))
const yearDividendOption = computed(() => {
  const values: Record<string, number> = {}
  for (const d of data.value?.dividends || []) if (d.symbol === selectedStock.value && d.report_year && d.status === 'implemented') values[d.report_year] = (values[d.report_year] || 0) + Number(d.cash_ps)
  const years = Object.keys(values).sort()
  return { ...barOption.value, xAxis: { ...barOption.value.xAxis, data: years }, tooltip: { trigger: 'axis', valueFormatter: (v: any) => `¥${v} / 股（原始股本口径）` }, series: [{ type: 'bar', barMaxWidth: 34, data: years.map(y => values[y]), itemStyle: { borderRadius: [5, 5, 0, 0] } }] }
})
const shownEvents = computed(() => {
  const receipts = calendarType.value === 'pay_date' ? (data.value?.transactions || []).filter((t: any) => ['dividend','dividend_record'].includes(t.kind) && !t.dividend_id).map((t: any) => ({ id:'receipt:'+t.id, symbol:t.symbol, pay_date:t.date, amount:t.amount, received:true })) : []
  return [...(data.value?.dividends || []),...receipts].filter((d: any) => calendarScope.value !== 'held' || data.value.holdings.some((h: any) => h.symbol === d.symbol))
})
const calendarDays = computed(() => {
  const [y, m] = calendarMonth.value.split('-').map(Number)
  const first = new Date(y, m - 1, 1), offset = (first.getDay() + 6) % 7
  return Array.from({ length: 42 }, (_, i) => {
    const day = new Date(y, m - 1, 1 - offset + i)
    const date = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
    return { date, day: day.getDate(), current: day.getMonth() === m - 1, events: shownEvents.value.filter((d: any) => d[calendarType.value] === date) }
  })
})
const title = computed(() => ({ overview: '总览', assets: '资产', stocks: '股票' })[page.value])
const quoteLabel = (q: any) => !q ? '暂无行情 · 使用成本参考' : `${q.source} · ${q.as_of ? q.as_of.replace('T', ' ').slice(0, 19) : '源时间未核实'}`
const stockName = (code: string) => data.value?.securities?.find((s: any) => s.symbol === code)?.name || code
function toast(text: string) { notice.value = text; clearTimeout(messageTimer); messageTimer = setTimeout(() => notice.value = '', 5000) }
async function refresh() {
  try {
    data.value = await api('/state'); connected.value = true; error.value = ''
    if (modal.value === 'rule' && /^\d{6}$/.test(form.symbol) && !data.value.watchlist.some((s: any) => s.symbol === form.symbol)) {
      const symbol = form.symbol, result = await api('/security/' + symbol)
      if (modal.value === 'rule' && symbol === form.symbol) { selectedPreview.value = result; fillRuleDefaults(false) }
    } else if (modal.value === 'rule') fillRuleDefaults(false)
    if (isTauri()) nativeNotification.value = await invoke('last_notification_status')
  }
  catch (e: any) { connected.value = false; error.value = e.message || '无法连接本地数据服务' }
}
async function sync() {
  loading.value = true
  try { const r = await api('/sync', 'POST'); toast(r.started ? '正在后台同步，账本可继续使用' : '同步仍在进行，请稍候'); await refresh() }
  catch (e: any) { toast(e.message) }
  finally { loading.value = false }
}
function open(type: string, defaults: any = {}) {
  for (const k of Object.keys(form)) delete form[k]
  Object.assign(form, { kind: type === 'account' ? 'cash' : type === 'stock' ? 'buy' : type === 'dividend' ? 'dividend_record' : 'balance', account_id: data.value?.accounts[0]?.id || '', date: today(), amount: '', fees: 0, price: '', quantity: '', symbol: '', name: '', balance: '', rate: '', basis: 'ttm', mode: 'price', day: 1, start_date: today(), credit_cash: false, ...defaults })
  if (['stock','dividend'].includes(type)) form.account_id = defaults.account_id || data.value?.accounts.find((a: any) => a.kind === 'broker')?.id || ''
  if (type === 'rule') { form.symbol = defaults.symbol || ''; selectedPreview.value = null }
  if (type === 'balance' && !defaults.id) form.amount = data.value?.accounts.find((a: any) => a.id === form.account_id)?.balance ?? ''
  modal.value = type
  if (type === 'rule') fillRuleDefaults()
  if (type === 'settings' && isTauri()) checkNotification(false)
}
function editRecord(record: any) {
  open(['buy','sell','opening_position'].includes(record.kind) ? 'stock' : ['dividend','dividend_record'].includes(record.kind) ? 'dividend' : 'balance', { ...record, name:stockName(record.symbol), credit_cash:record.kind === 'dividend' })
}
const ruleStock = computed(() => data.value?.watchlist.find((s: any) => s.symbol === form.symbol) || (selectedPreview.value?.symbol === form.symbol ? selectedPreview.value : null))
function fillRuleDefaults(reset = true) {
  if (modal.value !== 'rule' || form.id) return
  const stock = ruleStock.value
  if (reset || form.target_price === '' || form.target_price == null) form.target_price = stock?.quote?.price ? Number(stock.quote.price).toFixed(stock.instrument === 'etf' ? 3 : 2) : ''
  if (reset || form.target_yield === '' || form.target_yield == null) form.target_yield = stock?.yields?.[form.basis] > 0 ? Number(stock.yields[form.basis]).toFixed(3) : ''
}
function relateRule(source: string) {
  if (form.mode !== 'all') return
  const dps = ruleStock.value?.dps[form.basis]
  if (!(dps > 0)) return
  if (source === 'price' && Number(form.target_price) > 0) form.target_yield = (dps / Number(form.target_price) * 100).toFixed(3)
  if (source === 'yield' && Number(form.target_yield) > 0) form.target_price = (dps / Number(form.target_yield) * 100).toFixed(4)
}
watch(() => [form.symbol, form.basis], () => {
  if (modal.value === 'rule') fillRuleDefaults()
  if (modal.value === 'stock' && !form.id) {
    const stock = ruleStock.value
    if (stock?.name && !form.name) form.name = stock.name
    if (stock?.quote?.price && form.kind !== 'opening_position') form.price = stock.quote.price
  }
})
watch(() => form.symbol, async symbol => {
  const version = ++previewVersion
  selectedPreview.value = null
  if (modal.value !== 'rule' || !/^\d{6}$/.test(symbol)) { previewBusy.value = false; return }
  previewBusy.value = true
  try {
    const result = await api('/securities/prepare','POST',{symbol,name:form.name})
    if (version === previewVersion && modal.value === 'rule') { selectedPreview.value = result; fillRuleDefaults(false) }
  } catch (e: any) { if (version === previewVersion) toast(e.message) }
  finally { if (version === previewVersion) previewBusy.value = false }
})
watch(() => form.account_id, () => {
  if (modal.value === 'balance' && !form.id) form.amount = data.value?.accounts.find((a: any) => a.id === form.account_id)?.balance ?? ''
})
async function save() {
  saving.value = true
  try {
    let path = '', body: any = { ...form }, method = 'POST'
    if (modal.value === 'account') { path = '/accounts' + (form.id ? '/' + form.id : ''); method = form.id ? 'PUT' : 'POST'; body.balance = form.balance || 0; body.rate = form.rate || 0 }
    if (['stock','balance','dividend'].includes(modal.value)) {
      path = '/transactions' + (form.id ? '/' + form.id : ''); method = form.id ? 'PUT' : 'POST'
      body.quantity = Number(form.quantity || 0); body.price = form.price || 0; body.amount = form.amount || 0
      if (modal.value === 'balance' && !form.id) body.kind = 'balance'
      if (modal.value === 'dividend') body.kind = form.credit_cash ? 'dividend' : 'dividend_record'
    }
    if (modal.value === 'watch') path = '/watchlist'
    if (modal.value === 'rule') { path = '/rules' + (form.id ? '/' + form.id : ''); method = form.id ? 'PUT' : 'POST' }
    if (modal.value === 'flow') path = '/cashflows'
    if (modal.value === 'confirm') path = `/entitlements/${form.id}/confirm`
    if (modal.value === 'delete') { path = form.path; method = 'DELETE'; body = undefined }
    if (modal.value === 'import') { path = '/import'; body = importBackup.value }
    await api(path, method, body)
    if (modal.value === 'dividend') dividendYear.value = form.date.slice(0,4)
    modal.value = ''; await refresh(); toast('已保存到本地')
  } catch (e: any) { toast(e.message) }
  finally { saving.value = false }
}
async function changeFont(event: Event) {
  applyAppearance(Number((event.target as HTMLInputElement).value))
  clearTimeout(fontTimer)
  fontTimer = setTimeout(async () => {
    try { await api('/settings','POST',{font_size:fontSize.value}) } catch (e: any) { toast(e.message) }
  },250)
}
async function nativeTheme() {
  if (isTauri()) await getCurrentWindow().setTheme(theme.value === 'system' ? null : theme.value as 'light' | 'dark')
}
async function changeTheme(event: Event) {
  applyAppearance(fontSize.value,(event.target as HTMLSelectElement).value)
  try { await api('/settings','POST',{theme:theme.value}); await nativeTheme() } catch (e: any) { toast(e.message) }
}
const dataFolder = ref('')
async function chooseDataFolder() {
  if (!isTauri()) return
  try { const path = await invoke<string | null>('choose_data_directory'); if (path) dataFolder.value = path } catch (e: any) { toast(e.message) }
}
async function changeDataFolder() {
  saving.value = true
  try { await api('/data-directory','POST',{path:dataFolder.value}); await refresh(); toast('数据文件夹已更换，原文件保留') } catch (e: any) { toast(e.message) }
  finally { saving.value = false }
}
function remove(path: string, label: string) { open('delete', { path, label }) }
async function exportBackup() {
  try { const path = await download(`AssetGrove-${today()}.json`, JSON.stringify(await api('/export'), null, 2)); if (path) toast('备份已保存至 ' + path) }
  catch (e: any) { toast(e.message) }
}
async function exportCsv() {
  const escape = (v: any) => `"${String(v ?? '').replaceAll('"', '""')}"`
  const source = page.value === 'assets' && assetTab.value === 'records' || page.value === 'stocks' && stockTab.value === 'records' ? records.value : data.value.transactions
  const table = [['日期', '类型', '账户', '股票代码', '股数', '价格', '费用', '金额', '记录后余额', '备注'], ...source.map((t: any) => [t.date, txNames[t.kind], accountName(t.account_id), t.symbol, t.quantity, t.price, t.fees, t.amount, t.balance_after, t.note])]
  try { const path = await download(`AssetGrove-记录-${today()}.csv`, '\uFEFF' + table.map(row => row.map(escape).join(',')).join('\r\n'), 'text/csv'); if (path) toast('CSV 已保存至 ' + path) }
  catch (e: any) { toast(e.message) }
}
async function changePreference(key: string, value: string) {
  try { await api('/settings','POST',{[key]:value}); await refresh() } catch (e: any) { toast(e.message) }
}
function startAccountDrag(event: DragEvent, id: string) {
  draggedAccount.value = id
  event.dataTransfer?.setData('text/plain', id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
async function dropAccount(id: string) {
  const source = draggedAccount.value; draggedAccount.value = ''
  if (!source || source === id) return
  const ids = data.value.accounts.map((a: any) => a.id)
  const from = ids.indexOf(source), target = ids.indexOf(id)
  if (from < 0 || target < 0) return
  ids.splice(from,1); ids.splice(target,0,source)
  try { await api('/accounts/order','POST',{ids}); await refresh() } catch (e: any) { toast(e.message) }
}
async function moveAccount(id: string, step: number) {
  const ids = data.value.accounts.map((a: any) => a.id), index = ids.indexOf(id)
  if (index + step < 0 || index + step >= ids.length) return
  draggedAccount.value = id; await dropAccount(ids[index + step])
}
function startRuleDrag(event: DragEvent, id: string) {
  draggedRule.value = id
  event.dataTransfer?.setData('text/plain',id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
async function dropRule(id: string) {
  const source = draggedRule.value; draggedRule.value = ''
  if (!source || source === id) return
  const ids = data.value.rules.map((r: any) => r.id)
  const from = ids.indexOf(source), target = ids.indexOf(id)
  if (from < 0 || target < 0) return
  ids.splice(from,1); ids.splice(target,0,source)
  try { await api('/rules/order','POST',{ids}); await refresh() } catch (e: any) { toast(e.message) }
}
async function moveRule(id: string, step: number) {
  const ids = data.value.rules.map((r: any) => r.id), index = ids.indexOf(id)
  if (index + step < 0 || index + step >= ids.length) return
  draggedRule.value = id; await dropRule(ids[index + step])
}
async function checkNotification(sendTest: boolean) {
  if (!isTauri()) return
  try { nativeNotification.value = await invoke('notification_status',{sendTest}); if (sendTest) toast(nativeNotification.value.message) }
  catch (e: any) { toast(String(e)) }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  try {
    const file = input.files?.[0]; if (!file) return
    if (file.size > 20_000_000) throw new Error('备份文件不得超过 20 MB')
    importBackup.value = JSON.parse(await file.text()); open('import', { filename: file.name })
  } catch (e: any) { toast(e.message) }
  finally { input.value = '' }
}
function moveMonth(step: number) {
  const [y, m] = calendarMonth.value.split('-').map(Number), date = new Date(y, m - 1 + step, 1)
  calendarMonth.value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}
async function detail(symbol: string) {
  selectedStock.value = symbol; history.value = []
  try { history.value = await api('/history/' + symbol); modal.value = 'detail' } catch (e: any) { toast(e.message) }
}
async function toggleAutostart(event: Event) {
  try {
    const { enable, disable } = await import('@tauri-apps/plugin-autostart')
    if ((event.target as HTMLInputElement).checked) await enable(); else await disable()
    toast('启动设置已更新')
  } catch (e: any) { toast(e.message) }
}
const autostart = ref(false)
onMounted(async () => {
  try { await connect(); await refresh(); if (data.value?.settings) applyAppearance(data.value.settings.font_size, data.value.settings.theme); await nativeTheme(); if (isTauri()) { const { isEnabled } = await import('@tauri-apps/plugin-autostart'); autostart.value = await isEnabled() } }
  catch (e: any) { error.value = e.message }
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => { clearInterval(timer); clearTimeout(messageTimer); clearTimeout(fontTimer) })
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="page = 'overview'"><span class="brand-icon"><Sprout :size="27" /></span><span>AssetGrove</span></a>
      
      <nav aria-label="主导航"><button v-for="item in [{ id: 'overview', label: '总览', icon: LayoutDashboard }, { id: 'assets', label: '资产', icon: Wallet }, { id: 'stocks', label: '股票', icon: ChartNoAxesCombined }]" :key="item.id" :class="['nav-item', { active: page === item.id }]" @click="page = item.id as Page"><component :is="item.icon" :size="19" />{{ item.label }}<span v-if="item.id === 'stocks' && pending.length" class="nav-count">{{ pending.length }}</span></button></nav>

      <div class="sidebar-bottom"><button @click="open('settings')"><Settings2 :size="17" /> 数据与设置</button><div class="local-status"><span :class="['dot', { offline: !connected }]"></span>{{ connected ? '本地账本已连接' : '等待本地服务' }}<ShieldCheck :size="14" /></div></div>
    </aside>
    <main>

      <div class="content">
        <header class="page-header"><div><h1>{{ title }}</h1></div><div class="header-actions"><button class="icon-button" aria-label="通知记录" @click="open('notifications')"><Bell :size="19" /></button><button class="button secondary" :disabled="loading || !connected" @click="sync"><RefreshCw :size="15" :class="{ spin: loading || data?.sync.running === '1' }" />{{ data?.sync.running === '1' ? '同步中' : '同步数据' }}</button><button class="button primary" :disabled="!connected" @click="open(page === 'stocks' ? 'stock' : 'account', page === 'stocks' ? { kind: 'opening_position' } : {})"><Plus :size="17" />{{ page === 'stocks' ? '添加持仓' : '添加资产' }}</button></div></header>
        <div v-if="error" class="connection-error"><AlertCircle :size="18" /><div><strong>本地数据服务未连接</strong><p>{{ error }}。</p></div><button class="text-button" @click="refresh">重试</button></div>
        <template v-if="data">
          <div v-if="summary.estimated_positions" class="subtle-alert"><CircleHelp :size="15" />{{ summary.estimated_positions }} 项持仓行情待同步</div>
          <template v-if="page === 'overview'">
            <section class="metrics">
              <article class="metric featured"><div class="metric-label">存储性总资产 <Wallet :size="17" /></div><div class="metric-value"><span>¥</span>{{ currency(summary.total) }}</div><div class="metric-decor"><Sprout :size="100" :stroke-width=".8" /></div></article>
              <article class="metric"><div class="metric-label">证券账户总价值 <ChartNoAxesCombined :size="17" /></div><div class="metric-value"><span>¥</span>{{ currency(summary.broker_value) }}</div><div class="metric-bottom muted">含证券现金 ¥{{ currency(summary.broker_cash) }}</div></article>
              <article class="metric"><div class="metric-label">今年已收股息 <Coins :size="17" /></div><div class="metric-value"><span>¥</span>{{ currency(summary.received) }}</div></article>
              <article class="metric"><div class="metric-label">年股息预计 <select :value="estimateBasis" aria-label="年股息预计口径" @change="changePreference('dividend_basis',($event.target as HTMLSelectElement).value)"><option v-for="(label,key) in estimateNames" :value="key">{{ label }}</option></select></div><div class="metric-value"><span v-if="!summary.missing_dividend_positions">¥</span>{{ annualEstimateText }}</div></article>
            </section>
            <div v-if="!hasAccounts" class="welcome-banner"><div class="welcome-icon"><Sprout :size="27" /></div><div><h3>添加第一笔资产</h3></div><button class="button primary" @click="open('account')">开始记录 <ArrowRight :size="16" /></button></div>
            <section class="dashboard-grid"><article class="panel trend-panel"><div class="panel-heading"><div><h2>资产趋势</h2></div><span class="tag">自 {{ data.tracking_start }}</span></div><Chart v-if="data.timeline.some((x: any) => x.value > 0)" :option="lineOption" /><div v-else class="empty-chart"><TrendingUp :size="34" :stroke-width="1.2" /><p>暂无资产记录</p></div></article>
              <article class="panel allocation-panel"><div class="panel-heading"><div><h2>资产配置</h2></div><span class="tag">按资产类型</span></div><div class="donut-wrap"><Chart v-if="allocations.length" :option="pieOption" /><div v-else class="empty-donut"></div><div class="donut-center"><small>资产类别</small><strong>{{ allocations.length }}<span> 类</span></strong></div></div><div class="allocation-legend"><div v-for="(a, i) in allocations" :key="a.kind"><i :style="{ background: palette[i % palette.length] }"></i><span>{{ a.name }}</span><strong>{{ percent(a.value / summary.total * 100) }}</strong></div><p v-if="!allocations.length" class="muted center">暂时还没有资产记录</p></div></article></section>
            <section class="dashboard-grid lower"><article class="panel"><div class="panel-heading"><div><h2>每月收息</h2></div><span class="tag">{{ data.today.slice(0, 4) }} 年 · 已收</span></div><Chart :option="barOption" /></article><article class="panel upcoming-panel"><div class="panel-heading"><div><h2>派息安排</h2></div><button class="text-button" @click="page = 'stocks'; stockTab = 'dividends'">查看全部 <ArrowUpRight :size="15" /></button></div><template v-if="data.entitlements.some((e: any) => e.state !== 'confirmed')"><div v-for="e in data.entitlements.filter((e: any) => e.state !== 'confirmed').slice(0, 3)" :key="e.id" class="upcoming-item"><div class="date-tile"><small>{{ e.pay_date.slice(5, 7) }}月</small><strong>{{ e.pay_date.slice(8) }}</strong></div><div><strong>{{ stockName(e.symbol) }}</strong><small>{{ e.state === 'pending' ? '待确认到账' : '已公告派息' }} · {{ e.quantity }} 股</small></div><b>¥{{ currency(e.expected_amount) }}</b></div></template><div v-else class="empty-chart small"><CalendarDays :size="32" :stroke-width="1.2" /><p>静候派息安排</p></div></article></section>
          </template>

          <template v-if="page === 'assets'">
            <div class="tabs"><button v-for="t in [{ id: 'accounts', name: '资产账户' }, { id: 'records', name: '变动记录' }, { id: 'flows', name: '现金流增量' }]" :class="{ active: assetTab === t.id }" @click="assetTab = t.id">{{ t.name }}</button></div>
            <template v-if="assetTab === 'accounts'"><div class="section-title"><h2>我的存储性资产 <span>{{ data.accounts.length }} 个账户</span></h2><button class="button primary" @click="open('balance')"><Plus :size="15" />更新余额</button></div><div class="account-grid"><article v-for="a in data.accounts" :key="a.id" class="panel account-card" :class="{dragging:draggedAccount === a.id}" @dragover.prevent @drop.prevent="dropAccount(a.id)"><div class="account-top"><span class="account-icon"><component :is="a.kind === 'broker' ? ChartNoAxesCombined : a.kind === 'cash' ? Wallet : Landmark" :size="21" /></span><div class="actions"><span class="tag">{{ kindNames[a.kind] }}</span><button class="icon-button account-drag" draggable="true" :aria-label="'拖动排序 '+a.name" title="拖动排序；方向键调整位置" @dragstart="startAccountDrag($event,a.id)" @dragend="draggedAccount = ''" @keydown.left.prevent="moveAccount(a.id,-1)" @keydown.up.prevent="moveAccount(a.id,-1)" @keydown.right.prevent="moveAccount(a.id,1)" @keydown.down.prevent="moveAccount(a.id,1)">⠿</button><button class="icon-button" :aria-label="'编辑账户 '+a.name" @click="open('account',a)"><Pencil :size="16" /></button><button class="icon-button" :aria-label="'删除账户 ' + a.name" @click="remove('/accounts/' + a.id, '删除“' + a.name + '”及其持仓、分红和变动记录？关联转账也会删除。')"><Trash2 :size="16" /></button></div></div><h3>{{ a.name }}</h3><div class="account-amount">¥{{ currency(a.balance + (a.kind === 'broker' ? a.stock_value : 0)) }}</div><p v-if="a.kind === 'broker'">闲置现金 ¥{{ currency(a.balance) }} · 股权参考 ¥{{ currency(a.stock_value) }}</p><p v-else-if="Number(a.rate)">预期年利率 {{ percent(a.rate) }}{{ a.maturity ? ' · 到期 ' + a.maturity : '' }}</p><button class="button secondary full" @click="open('balance', { account_id: a.id })"><Plus :size="15" />更新余额</button></article><button class="add-account" @click="open('account')"><Plus :size="28" :stroke-width="1.2" /><strong>添加一个资产账户</strong></button></div></template>
            <template v-if="assetTab === 'flows'"><div class="section-title"><h2>固定现金流增量</h2><button class="button primary" @click="open('flow')"><Plus :size="15" />新增每月计划</button></div><div class="panel table-panel"><table><thead><tr><th>计划名称</th><th>进入账户</th><th>金额</th><th>每月日期</th><th>开始日期</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="f in data.cashflows" :key="f.id"><td>{{ f.note }}</td><td>{{ accountName(f.account_id) }}</td><td class="numeric">¥{{ currency(f.amount / 100) }}</td><td>{{ f.day }} 日</td><td>{{ f.start_date }}</td><td><span class="tag">{{ f.enabled ? '执行中' : '已停止' }}</span></td><td><button v-if="f.enabled" class="text-button" @click="remove('/cashflows/' + f.id, '停止这项每月现金流计划？已生成的记录将保留。')">停止</button></td></tr></tbody></table><div v-if="!data.cashflows.length" class="empty-chart small"><Coins :size="30" /><p>暂无现金流计划</p></div></div></template>
          </template>

          <template v-if="page === 'stocks'">
            <div class="tabs"><button v-for="t in [{ id: 'holdings', name: '我的持仓' }, { id: 'watchlist', name: '自选与股息率' }, { id: 'dividends', name: '分红统计' }, { id: 'calendar', name: '分红日历' }, { id: 'records', name: '交易记录' }, { id: 'rules', name: '提醒规则' }]" :class="{ active: stockTab === t.id }" @click="stockTab = t.id">{{ t.name }}<span v-if="t.id === 'dividends' && pending.length" class="tab-badge">{{ pending.length }}</span></button></div>
            <template v-if="stockTab === 'holdings'"><div class="section-title"><h2>股票与 ETF 持仓 <span>{{ data.holdings.length }} 项持仓</span></h2><div class="actions"><button class="button secondary" @click="open('stock', { kind: 'buy' })"><Plus :size="15" />买入</button><button class="button secondary" @click="open('stock', { kind: 'sell' })">卖出</button></div></div><div class="panel table-panel"><table><thead><tr><th>股票 / 账户</th><th>持有股权</th><th>平均成本</th><th>最新价</th><th>股权参考价值</th><th>TTM 股息率</th><th>年股息预计</th><th></th></tr></thead><tbody><tr v-for="h in data.holdings" :key="h.account_id + h.symbol"><td><button class="stock-link" @click="detail(h.symbol)">{{ h.name }}</button><small>{{ h.symbol }} · {{ accountName(h.account_id) }}</small></td><td class="numeric">{{ compact(h.quantity) }} {{ h.instrument === 'etf' ? '份' : '股' }}</td><td class="numeric">¥{{ priceText(h.average_cost,h.symbol) }}</td><td :title="quoteLabel(h.quote)" class="numeric">{{ h.quote ? '¥' + priceText(h.quote.price,h.symbol) : '暂无行情' }}</td><td class="numeric">¥{{ currency(h.reference_value) }}</td><td class="numeric green">{{ percent(h.yield_ttm) }}</td><td class="numeric">{{ h.annual_dividend_estimate == null ? '—' : '¥' + currency(h.annual_dividend_estimate) }}</td><td><div class="actions"><button class="icon-button" title="买入股票" @click="open('stock', { kind: 'buy', symbol: h.symbol, name: h.name, account_id: h.account_id })"><Plus :size="16" /></button><button class="icon-button" title="卖出股票" @click="open('stock', { kind: 'sell', symbol: h.symbol, name: h.name, account_id: h.account_id })"><ArrowUpRight :size="16" /></button><button class="icon-button" title="添加提醒" @click="open('rule', { symbol: h.symbol })"><Bell :size="16" /></button></div></td></tr></tbody></table><div v-if="!data.holdings.length" class="empty-chart"><Sprout :size="37" :stroke-width="1.2" /><p>暂无股票持仓</p><button class="button primary" @click="open('stock', { kind: 'opening_position' })">添加期初持仓</button></div></div></template>
            <template v-if="stockTab === 'watchlist'"><div class="section-title"><div class="actions"><label class="search"><Search :size="16" /><input v-model="search" placeholder="搜索代码或名称" /></label><select v-model="basis"><option v-for="(label, key) in basisNames" :value="key">{{ label }}</option></select></div><button class="button primary" @click="open('watch')"><Plus :size="15" />添加自选</button></div><div class="panel table-panel"><table><thead><tr><th>股票</th><th>最新价</th><th>每股现金分红</th><th>股息率</th><th></th></tr></thead><tbody><tr v-for="s in filteredWatch" :key="s.symbol"><td><button class="stock-link" @click="detail(s.symbol)">{{ s.name }}</button><small>{{ s.symbol }} {{ s.held ? '· 已持有' : '' }}</small></td><td class="numeric">{{ s.quote ? '¥' + priceText(s.quote.price,s.symbol) : '—' }}</td><td class="numeric">{{ s.dps[basis] == null ? '资料不足' : '¥' + Number(s.dps[basis]).toFixed(4) }}</td><td class="numeric green">{{ percent(s.yields[basis]) }}</td><td><div class="actions"><button class="icon-button" title="设置提醒" @click="open('rule', { symbol: s.symbol })"><Bell :size="16" /></button><button class="icon-button" title="移出自选" @click="remove('/watchlist/' + s.symbol, '移出自选？持仓仍会显示，已保存的分红和记录将保留。')"><X :size="16" /></button></div></td></tr></tbody></table><div v-if="!filteredWatch.length" class="empty-chart small"><Search :size="30" /><p>暂无自选股票</p></div></div></template>
            <template v-if="stockTab === 'dividends'"><div class="metrics three"><article class="metric featured"><div class="metric-label">今年已确认到账</div><div class="metric-value"><span>¥</span>{{ currency(summary.received) }}</div><p>实际到账口径</p></article><article class="metric"><div class="metric-label">待确认到账</div><div class="metric-value"><span>¥</span>{{ currency(pending.reduce((n: number, e: any) => n + e.expected_amount, 0)) }}</div><p>{{ pending.length }} 笔 · 预计税前</p></article><article class="metric"><div class="metric-label">年股息预计 <select :value="estimateBasis" aria-label="年股息预计口径" @change="changePreference('dividend_basis',($event.target as HTMLSelectElement).value)"><option v-for="(label,key) in estimateNames" :value="key">{{ label }}</option></select></div><div class="metric-value"><span v-if="!summary.missing_dividend_positions">¥</span>{{ annualEstimateText }}</div></article></div><article class="panel"><div class="panel-heading"><div><h2>年度收息</h2><select v-model="dividendYear" aria-label="统计年份"><option v-for="year in data.dividend_years" :value="year">{{ year }} 年</option></select></div><span class="tag">现金分红</span></div><Chart :option="barOption" /></article><div class="section-title"><h2>分红到账核对</h2><button class="button primary" @click="open('dividend')"><Plus :size="15" />添加分红记录</button></div><div class="panel table-panel"><table><thead><tr><th>股票 / 账户</th><th>登记持股</th><th>派息日</th><th>预计税前</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="e in data.entitlements" :key="e.id"><td><strong>{{ stockName(e.symbol) }}</strong><small>{{ e.account_name }}</small></td><td>{{ compact(e.quantity) }} 股</td><td>{{ e.pay_date }}</td><td class="numeric">¥{{ currency(e.expected_amount) }}</td><td><span :class="['status-pill', e.state]">{{ { pending: '待确认到账', scheduled: '等待派息', confirmed: '已确认到账' }[e.state as string] }}</span></td><td><button v-if="e.state === 'pending'" class="button secondary" @click="open('confirm', { id: e.id, amount: e.expected_amount, name: stockName(e.symbol) })"><Check :size="15" />确认到账</button></td></tr></tbody></table><div v-if="!data.entitlements.length" class="empty-chart small"><CalendarDays :size="30" /><p>还没有需要核对的分红</p></div></div><div class="section-title"><h2>{{ dividendYear }} 年已收分红</h2><strong>¥{{ currency(receivedDividends.reduce((sum: number, item: any) => sum + item.amount, 0)) }}</strong></div>
              <div class="panel table-panel"><table><thead><tr><th>到账日期</th><th>股票</th><th>证券账户</th><th>到账金额</th><th>操作</th></tr></thead><tbody><tr v-for="item in receivedDividends" :key="item.id"><td>{{ item.date }}</td><td>{{ stockName(item.symbol) }}</td><td>{{ accountName(item.account_id) }}</td><td>¥{{ currency(item.amount) }}</td><td><div class="actions"><button v-if="!item.dividend_id" class="icon-button" title="编辑分红" @click="editRecord(item)"><Pencil :size="16" /></button><button class="icon-button" title="删除分红" @click="remove('/transactions/' + item.id, '删除这笔分红记录？')"><Trash2 :size="16" /></button></div></td></tr></tbody></table><div v-if="!receivedDividends.length" class="empty-chart small"><p>暂无分红记录</p></div></div>
              </template>
            <template v-if="stockTab === 'calendar'"><div class="section-title"><div class="actions"><button class="icon-button outlined" aria-label="上个月" @click="moveMonth(-1)"><ChevronLeft :size="18" /></button><input v-model="calendarMonth" type="month" aria-label="日历月份" /><button class="icon-button outlined" aria-label="下个月" @click="moveMonth(1)"><ChevronRight :size="18" /></button></div><div class="actions"><select v-model="calendarScope"><option value="held">仅持仓标的</option><option value="all">全部标的</option></select><select v-model="calendarType"><option value="pay_date">派息日</option><option value="record_date">股权登记日</option><option value="ex_date">除权除息日</option></select></div></div><div class="panel calendar"><div class="calendar-week"><span v-for="d in ['一', '二', '三', '四', '五', '六', '日']">周{{ d }}</span></div><div class="calendar-grid"><div v-for="d in calendarDays" :key="d.date" :class="['calendar-day', { other: !d.current, today: d.date === today() }]"><span class="day-number">{{ d.day }}</span><div v-for="e in d.events" :key="e.id" :class="['calendar-event', { proposal: e.status === 'proposed' }]" :title="e.received ? `${stockName(e.symbol)} · 到账 ¥${currency(e.amount)}` : `${stockName(e.symbol)} · 每股 ¥${e.cash_ps}`">{{ stockName(e.symbol) }}<small>{{ e.received ? '到账 ¥' + currency(e.amount) : (e.status === 'proposed' ? '预案' : '已公告') + ' · ¥' + Number(e.cash_ps).toFixed(3) + '/股' }}</small></div></div></div></div></template>
            <template v-if="stockTab === 'rules'"><div class="section-title"><h2>我的关注条件</h2><button class="button primary" @click="open('rule')"><Plus :size="15" />新增提醒</button></div><div class="rule-list"><RuleStrip v-for="r in data.rules" :key="r.id" :rule="r" :stock="data.watchlist.find((s: any) => s.symbol === r.symbol)" :name="stockName(r.symbol)" :class="{dragging:draggedRule === r.id}" @dragstart="startRuleDrag($event,r.id)" @dragend="draggedRule = ''" @dragover.prevent @drop.prevent="dropRule(r.id)" @move="moveRule(r.id,$event)" @edit="open('rule',{...r,name:stockName(r.symbol)})" :basis-label="basisNames[r.basis] || '近12个月 TTM'" @remove="remove('/rules/' + r.id, '删除这条提醒规则？')" /><div v-if="!data.rules.length" class="panel empty-chart"><Bell :size="32" /><p>暂无关注条件</p></div></div></template>
          </template>

          <template v-if="(page === 'assets' && assetTab === 'records') || (page === 'stocks' && stockTab === 'records')">
            <div class="section-title"><h2>{{ page === 'stocks' ? '交易与分红记录' : '余额记录' }}</h2><div class="actions"><button class="button secondary" @click="exportCsv"><Download :size="15" />导出 CSV</button><button class="button primary" @click="open(page === 'stocks' ? 'stock' : 'balance')"><Plus :size="15" />{{ page === 'stocks' ? '记录交易' : '更新余额' }}</button></div></div>
            <div class="panel table-panel"><table>
              <thead><tr><th>日期</th><th>类型</th><th>{{ page === 'stocks' ? '股票 / 账户' : '账户' }}</th><template v-if="page === 'stocks'"><th>股数</th><th>成交价</th><th>费用</th><th>金额</th></template><th v-else>记录后余额</th><th>备注</th><th>操作</th></tr></thead>
              <tbody><tr v-for="record in records" :key="record.id"><td>{{ record.date }}</td><td>{{ txNames[record.kind] }}</td><td>{{ page === 'stocks' ? stockName(record.symbol) : accountName(record.account_id) }}<small v-if="page === 'stocks'">{{ accountName(record.account_id) }}</small></td><template v-if="page === 'stocks'"><td>{{ record.quantity ? compact(record.quantity) : '—' }}</td><td>{{ record.quantity ? '¥' + currency(record.price) : '—' }}</td><td>{{ record.quantity ? '¥' + currency(record.fees) : '—' }}</td><td>¥{{ currency(record.quantity ? Number(record.price) * record.quantity : record.amount) }}</td></template><td v-else class="numeric">¥{{ currency(record.balance_after) }}</td><td class="note-cell">{{ record.note || '—' }}</td><td><div class="actions"><button v-if="!record.dividend_id" class="icon-button" title="编辑记录" @click="editRecord(record)"><Pencil :size="16" /></button><button class="icon-button" :title="record.dividend_id ? '撤销到账确认' : '删除记录'" @click="remove('/transactions/' + record.id, record.dividend_id ? '撤销这笔到账确认？' : '删除这笔记录？')"><Trash2 :size="16" /></button></div></td></tr></tbody>
            </table><div v-if="!records.length" class="empty-chart small"><p>暂无记录</p></div></div>
          </template>


        </template>
      </div>
    </main>
    <input ref="importInput" type="file" accept=".json,application/json" hidden @change="importFile" />
    <Transition name="toast"><div v-if="notice" class="toast" role="status"><CheckCircle2 :size="18" />{{ notice }}<button aria-label="关闭提示" @click="notice = ''"><X :size="15" /></button></div></Transition>
    <div v-if="modal" class="modal-overlay" @mousedown.self="modal = ''" @keydown.esc="modal = ''"><section :class="['modal', { wide: ['settings', 'detail', 'notifications'].includes(modal) }]" role="dialog" aria-modal="true" :aria-label="modal === 'stock' ? '股票变动' : modal === 'balance' ? '余额记录' : '资产设置'"><div class="modal-header"><h2>{{ ({ account: form.id ? '编辑资产账户' : '添加资产账户', stock: form.id ? '编辑股票记录' : '记录股票变动', balance: form.id ? '编辑余额记录' : '更新账户余额', dividend: form.id ? '编辑分红记录' : '添加分红记录', watch: '添加自选股票', rule: form.id ? '编辑关注条件' : '设置关注条件', flow: '每月现金流增量', confirm: '确认分红到账', delete: '确认操作', import: '恢复本地备份', settings: '数据与设置', notifications: '通知与待办', detail: stockName(selectedStock) })[modal] }}</h2><button class="icon-button" aria-label="关闭弹窗" @click="modal = ''"><X :size="20" /></button></div>
      <form v-if="!['settings', 'detail', 'notifications'].includes(modal)" @submit.prevent="save">
        <template v-if="modal === 'account'"><label>账户名称<input v-model="form.name" placeholder="例如：证券账户" required maxlength="80" /></label><label>资产类型<select v-model="form.kind"><option v-for="k in ['cash', 'broker', 'fund', 'bond', 'deposit']" :value="k">{{ kindNames[k] }}</option></select></label><div v-if="!form.id" class="form-row"><label>{{ form.kind === 'broker' ? '当前闲置现金（不含股票）' : '当前金额' }}<input v-model="form.balance" type="number" min="0" step="0.01" placeholder="0.00" required /></label><label>记录日期<input v-model="form.date" type="date" :max="today()" required /></label></div><div v-if="['bond', 'deposit'].includes(form.kind)" class="form-row"><label>预期年利率 %（可选）<input v-model="form.rate" type="number" min="0" max="100" step="0.001" /></label><label>到期日期（可选）<input v-model="form.maturity" type="date" /></label></div></template>
        <template v-if="modal === 'stock'">
          <label>变动类型<select v-model="form.kind" :disabled="!!form.id"><option value="opening_position">初始持仓</option><option value="buy">买入</option><option value="sell">卖出</option></select></label>
          <StockPicker v-model="form.symbol" v-model:name="form.name" />
          <label>证券账户<select v-model="form.account_id" required><option value="" disabled>请选择账户</option><option v-for="account in data.accounts.filter((a: any) => a.kind === 'broker')" :value="account.id">{{ account.name }}</option></select></label>
          <div class="form-row"><label>变动股数<input v-model="form.quantity" type="number" min="1" step="1" required /></label><label>{{ form.kind === 'opening_position' ? '原买入价格' : '成交价格' }}<input v-model="form.price" type="number" min="0.0001" step="0.0001" required /></label></div>
          <div class="form-row"><label>日期<input v-model="form.date" type="date" :max="today()" required /></label><label>交易费用<input v-model="form.fees" type="number" min="0" step="0.01" required /></label></div>
          <p v-if="['buy','sell'].includes(form.kind)" class="form-hint">{{ form.id && !form.cash_settlement ? '这笔旧版交易保留原来的现金处理方式。' : (form.kind === 'buy' ? '买入扣除证券现金（含费用）' : '卖出增加证券现金（扣除费用）') + '：¥' + currency(Number(form.price || 0) * Number(form.quantity || 0) + (form.kind === 'buy' ? 1 : -1) * Number(form.fees || 0)) }}</p>
          <label>备注<input v-model="form.note" maxlength="500" /></label>
        </template>
        <template v-if="modal === 'balance'">
          <label>资产账户<select v-model="form.account_id" required><option value="" disabled>请选择账户</option><option v-for="account in data.accounts" :value="account.id">{{ account.name }}</option></select></label>
          <label>{{ form.id && !['opening','balance'].includes(form.kind) ? '原记录金额' : '记录后余额' }}<input v-model="form.amount" type="number" :min="form.kind === 'adjustment' ? undefined : 0" step="0.01" required /></label>
          <label>日期<input v-model="form.date" type="date" :max="today()" required /></label>
          <label>备注<input v-model="form.note" maxlength="500" /></label>
        </template>
        <template v-if="modal === 'dividend'">
          <StockPicker v-model="form.symbol" v-model:name="form.name" />
          <label>证券账户<select v-model="form.account_id" required><option value="" disabled>请选择账户</option><option v-for="account in data.accounts.filter((a: any) => a.kind === 'broker')" :value="account.id">{{ account.name }}</option></select></label>
          <div class="form-row"><label>到账金额<input v-model="form.amount" type="number" min="0.01" step="0.01" required /></label><label>到账日期<input v-model="form.date" type="date" :max="today()" required /></label></div>
          <label class="checkbox-label"><input v-model="form.credit_cash" type="checkbox" />同时增加证券现金</label>
          <label>备注<input v-model="form.note" maxlength="500" /></label>
        </template>
        <template v-if="modal === 'watch'"><StockPicker v-model="form.symbol" v-model:name="form.name" /></template>
        <template v-if="modal === 'rule'">
          <StockPicker v-model="form.symbol" v-model:name="form.name" />
          <label>组合条件<select v-model="form.mode"><option value="price">股价满足</option><option value="yield">股息满足</option><option value="all">都满足</option></select></label>
          <label v-if="form.mode !== 'yield'">目标价格 ≤<input v-model="form.target_price" type="number" min="0.0001" step="0.0001" required @input="relateRule('price')" /></label>
          <label v-if="form.mode !== 'price'">目标股息率 ≥ %<input v-model="form.target_yield" type="number" min="0.001" step="0.001" required @input="relateRule('yield')" /></label>
          <label>股息口径<select v-model="form.basis"><option v-for="(label, key) in basisNames" :value="key">{{ label }}</option></select></label>
          <div v-if="previewBusy" role="status">正在获取标的信息…</div><div class="rule-reference"><span>当前价格 {{ ruleStock?.quote ? '¥' + priceText(ruleStock.quote.price,form.symbol) : '—' }}</span><span>当前股息率 {{ percent(ruleStock?.yields[form.basis]) }}</span></div>
        </template>

        <template v-if="modal === 'flow'"><label>计划名称<input v-model="form.note" placeholder="例如：每月储蓄增量" required /></label><label>进入账户<select v-model="form.account_id" required><option v-for="a in data.accounts.filter((a: any) => ['cash', 'broker'].includes(a.kind))" :value="a.id">{{ a.name }}</option></select></label><div class="form-row"><label>每月增量<input v-model="form.amount" type="number" min="0.01" step="0.01" required /></label><label>每月几日<input v-model="form.day" type="number" min="1" max="31" required /></label></div><label>开始日期<input v-model="form.start_date" type="date" required /></label><p class="form-hint">设定开始日期后，到期的计划会自动生成现金流记录。</p></template>
        <template v-if="modal === 'confirm'"><p class="form-hint">{{ form.name }} · 实际到账确认后计入对应证券现金，差额保留在预计与实际金额中。</p><label>实际到账金额<input v-model="form.amount" type="number" min="0" step="0.01" required /></label><label>实际到账日期<input v-model="form.date" type="date" :max="today()" required /></label></template>
        <template v-if="modal === 'delete'"><p class="confirm-text">{{ form.label }}</p><p class="form-hint">如导致现金或持股不足，或影响已确认分红，操作会被拒绝，原记录保留。</p></template>
        <template v-if="modal === 'import'"><p class="confirm-text">使用 {{ form.filename }} 替换当前本地账本？</p><p class="form-hint">建议先导出当前备份。文件验证失败时原数据保留；恢复操作也会保留本地审计记录。</p><button type="button" class="button secondary" @click="exportBackup"><Download :size="15" />先导出当前备份</button></template>
        <div class="modal-actions"><button type="button" class="button secondary" @click="modal = ''">取消</button><button class="button primary" :disabled="saving">{{ saving ? '保存中…' : modal === 'delete' ? '确认操作' : modal === 'import' ? '恢复备份' : '保存到本地' }}</button></div>
      </form>
      <div v-if="modal === 'settings'" class="settings-body">
        <div class="setting-section"><h3>字体大小</h3><label>字号 {{ fontSize }}<input aria-label="字体大小" type="range" min="14" max="24" step="1" :value="fontSize" @input="changeFont" /></label><div class="font-preview">AssetGrove 资产与分红记录 123,456.78</div></div>
        <div class="setting-section"><h3>账户排序</h3><label>排序方式<select :value="data?.preferences?.account_sort || 'manual'" @change="changePreference('account_sort',($event.target as HTMLSelectElement).value)"><option value="manual">手动排序</option><option value="type">按类型</option><option value="amount">按金额从高到低</option></select></label></div><div class="setting-section"><h3>外观</h3><label>显示模式<select :value="theme" @change="changeTheme"><option value="light">浅色</option><option value="dark">深色</option><option value="system">跟随系统</option></select></label></div>
        <div class="setting-section"><h3>数据文件夹</h3><code>{{ data?.data_path }}</code><div class="actions folder-controls"><input v-model="dataFolder" aria-label="数据文件夹" placeholder="选择新的数据文件夹" /><button v-if="isTauri()" class="button secondary" @click="chooseDataFolder">选择文件夹</button><button class="button secondary" :disabled="!dataFolder || saving" @click="changeDataFolder">迁移数据</button></div><div class="actions"><button class="button primary" @click="exportBackup"><Download :size="15" />导出备份</button><button class="button secondary" @click="importInput?.click()"><Upload :size="15" />导入备份</button><button class="button secondary" @click="exportCsv">导出 CSV</button></div></div>
        <div class="setting-section"><h3>数据源状态</h3><div class="sync-list"><div v-for="(value, key) in Object.fromEntries(Object.entries(data?.sync || {}).filter(([key]) => !key.startsWith('trading_day:') && key !== 'scheduler_last_tick'))" :key="key"><span>{{ key }}</span><strong>{{ value }}</strong></div><p v-if="!Object.keys(data?.sync || {}).length">尚未同步</p></div></div>
        <div class="setting-section"><h3>系统通知</h3><p v-if="!isTauri()">系统通知请在桌面安装版中使用。</p><template v-else><p role="status">{{ nativeNotification?.message || '正在检查 Windows 通知状态…' }}</p><div class="actions"><button class="button secondary" type="button" @click="checkNotification(true)"><Bell :size="16" />发送测试通知</button><button class="button secondary" type="button" @click="checkNotification(false)">检查通知状态</button></div><p>若通知中心收到消息但没有横幅，请检查 Windows 的通知横幅及勿扰设置。</p></template></div><div class="setting-section"><h3>运行设置</h3><label v-if="isTauri()" class="checkbox-label"><input v-model="autostart" type="checkbox" @change="toggleAutostart" />登录系统后自动启动</label><button v-if="isTauri()" class="button secondary" @click="quitApplication">完全退出并停止监控</button></div>
      </div>

      <div v-if="modal === 'notifications'" class="notifications-body"><div v-for="e in pending" class="notification"><CalendarDays :size="19" /><div><strong>{{ stockName(e.symbol) }} 有一笔分红待确认</strong><p>{{ e.pay_date }} · 预计税前 ¥{{ currency(e.expected_amount) }}</p></div><button class="text-button" @click="open('confirm', { id: e.id, amount: e.expected_amount, name: stockName(e.symbol) })">核对</button></div><div v-for="n in data?.notifications" class="notification"><Bell :size="19" /><div><strong>{{ n.title }}</strong><p>{{ n.body }}</p><small>{{ n.date.slice(0, 19).replace('T', ' ') }}</small></div></div><div v-if="!pending.length && !data?.notifications.length" class="empty-chart"><Bell :size="32" /><p>暂时没有通知</p></div></div>
      <div v-if="modal === 'detail'" class="detail-body"><p class="muted">{{ selectedStock }} · 历史价格采用不复权口径</p><h3>股价趋势</h3><Chart v-if="history.length" :option="stockHistoryOption" /><div v-else class="empty-chart small">尚无历史行情，请同步后查看</div><h3>历年每股现金分红</h3><Chart :option="yearDividendOption" /><p class="form-hint">按财年归属汇总原始每股分红，未统一历史送转股基准，不作为总回报序列。</p></div>
    </section></div>
  </div>
</template>
