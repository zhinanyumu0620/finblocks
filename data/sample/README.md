# 内置真实历史样例

样例来自[Bokeh官方样例仓库](https://github.com/bokeh/bokeh_sampledata)，固定提交 `4195d5e5c3094ef578be168778c7e9501138f921`，原文件为 `_data/AAPL.csv`、`IBM.csv`、`MSFT.csv`。[stocks模块](https://github.com/bokeh/bokeh_sampledata/blob/4195d5e5c3094ef578be168778c7e9501138f921/src/bokeh_sampledata/stocks.py)标注来源为public news sources。

固定选择2012年，每股250条，实际范围2012-01-03至2012-12-31；共750条。只保留上游Open/High/Low/Close/Volume，按日期排序并添加原标的代码，不将Adj Close混入OHLC，不生成价格。上游2013年只有41条，因此选择足够预热的完整2012年；没有按策略收益挑选。

上游仓库分发许可为BSD-3-Clause，完整原文及版权说明保留在 [LICENSE_BOKEH.txt](LICENSE_BOKEH.txt)。上游原CSV的固定URL和SHA-256、最终market.csv哈希都在 [manifest.json](manifest.json) 中；此清单是来源记录，不是交易所认证或供应商授权证明。

价格币种USD、成交量股。企业行动/复权未独立核验；不是最新行情，不是沪深300，也不是完整市场模拟。样例用于可复现软件演示，不能用来证明投资收益、基本面PIT或任意市场适用性。

点击“载入真实案例”可运行 `examples/sample_case.json`：MA5严格高于MA20才入选；三股共享现金、最多3只、单股上限1/3，每日调仓，成本10基点，滞后1条，每年252收益周期，无风险率0。参数是演示假设，不是最优策略。实际回测结果见运行生成的报告，不预置收益数字冒充运行结果。
