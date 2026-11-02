<template>
  <details class="panel indicator-guide">
    <summary>指标说明</summary>
    <div class="indicator-guide-content">
      <article><h3>TRIN · 涨跌成交量的相对强弱</h3>
        <p>TRIN =（上涨家数 ÷ 下跌家数）÷（上涨股票成交量 ÷ 下跌股票成交量），也等于下跌股票的平均成交量除以上涨股票的平均成交量。反映成交量更集中于哪一组股票。</p>
        <table><thead><tr><th>取值</th><th>含义</th></tr></thead><tbody><tr><td>接近 1</td><td>两组股票的平均成交量接近。</td></tr><tr><td>小于 1</td><td>上涨组平均成交量更大；例如 0.5 表示约为下跌组的两倍。</td></tr><tr><td>大于 1</td><td>下跌组平均成交量更大；例如 2 表示约为上涨组的两倍。</td></tr></tbody></table>
        <p>强势上涨日常见较低值，集中抛压时常见较高值。低于 0.5 或高于 3 可作为相对极端的观察例子，不能单凭这些数值判断顶部或底部。本工具采用沪深 A 股全市场、成交股数口径，阈值不能直接照搬其他市场。</p>
        <a href="https://chartschool.stockcharts.com/table-of-contents/market-indicators/arms-index-trin" target="_blank" rel="noreferrer">TRIN 口径来源</a>
      </article>
      <article><h3>ERP · 股票相对国债的盈利收益率差</h3>
        <p>ERP = 100 ÷ 沪深300 PE-TTM − 中国10年期国债收益率，单位为百分点。采用滚动利润的盈利收益率，衡量相同估值口径下股票相对国债的回报补偿。</p>
        <table><thead><tr><th>情况</th><th>含义</th></tr></thead><tbody><tr><td>ERP 越高</td><td>股票盈利收益率相对国债越高，股债相对吸引力通常更强。</td></tr><tr><td>ERP 为 0 或负数</td><td>盈利收益率等于或低于国债收益率。</td></tr><tr><td>PE 20 倍、国债 2%</td><td>ERP = 5% − 2% = 3 个百分点；PE 降为 10 倍、国债不变时，ERP 为 8 个百分点。</td></tr></tbody></table>
        <p>应结合自身历史分位看高低，没有所有时期通用的固定阈值。80% 分位表示高于历史约 80% 的观测，不代表未来上涨概率。盈利下滑、利率变化都会改变 ERP。</p>
      </article>
      <article><h3>PE-TTM 估值通道</h3>
        <p>PE-TTM 比较指数价格与过去十二个月盈利。通道使用所选十年或全部历史的均值 μ 和样本标准差 σ：阴影为 μ ± 1σ，虚线补充 μ ± 2σ。</p>
        <p>低于 μ − 1σ 表示相对该历史样本偏低，高于 μ + 1σ 表示相对偏高；P10、P50、P90 分别表示样本的第 10、50、90 百分位。均值与分位均基于所选完整样本，是历史描述，不是当时已知的回测信号或必然回归的价格目标。</p>
        <p>长期指标默认按周显示，每周取最后记录日期的观测，也可切换日频。底层按实际日数据计算，缺少输入的观测保留空缺，不以月度插值补成周度。静态来源周度更新，源数据的截止日会单独显示。</p>
        <a href="https://github.com/Ron-Wang/InvestmentQuant" target="_blank" rel="noreferrer">参考仓库与静态数据来源</a>
      </article>
    </div>
  </details>
</template>
