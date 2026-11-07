<template>
  <details class="panel indicator-guide">
    <summary>指标说明</summary>
    <div class="indicator-guide-content">
      <article><h3>TRIN · 抛压与买盘</h3><p>（上涨家数 ÷ 下跌家数）÷（上涨成交股数 ÷ 下跌成交股数）。小于 1 时成交量偏向上涨组，大于 1 时偏向下跌组；0.5 表示上涨组平均成交量约为下跌组两倍，2 则相反。</p><a href="https://chartschool.stockcharts.com/table-of-contents/market-indicators/arms-index-trin" target="_blank" rel="noreferrer">口径</a></article>
      <article><h3>WVF · 恐慌指标</h3><p>（近22个交易日最高收盘价 − 当日最低价）÷ 最高收盘价 × 100。越高表示从近期高点回落越深、抛压越强；接近0表示靠近近期高点。它是价格型恐慌代理，非期权隐含波动率。</p><a href="https://www.marketcalls.in/wp-content/uploads/2014/12/VIXFix.pdf" target="_blank" rel="noreferrer">Larry Williams 原文</a></article>
      <article><h3>50／200日均线趋势</h3><p>曲线 =（收盘价 ÷ MA200 − 1）× 100，正值在200日均线之上，负值在其下。价格与MA50均在MA200之上为牛市趋势，均在其下为熊市趋势；价格先上穿为修复，先下穿为回调，重合时为震荡。</p></article>
      <article><h3>ERP · 股债收益率差</h3><p>100 ÷ 沪深300 PE-TTM − 中国10年期国债收益率。越高表示股票相对国债的盈利收益率补偿越高；例如PE为20倍、国债为2%，ERP为3个百分点。</p></article>
      <article><h3>10年期国债收益率</h3><p>单独展示ERP所用的中国10年期国债到期收益率，单位为%。收益率上升时，其他条件不变，ERP下降。</p></article>
      <article><h3>PE-TTM 估值通道</h3><p>比较当前估值与近十年或全部历史。低于均值−1σ为相对偏低，高于均值+1σ为相对偏高；历史分位越高，估值在所选区间越靠上。</p></article>
    </div>
  </details>
</template>
