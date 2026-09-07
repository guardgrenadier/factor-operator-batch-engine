# 示例目录

正式链路示例见
[`日内异常成交量价格压力因子_新版.ipynb`](日内异常成交量价格压力因子_新版.ipynb)，
它使用任务级 `SmartQuantDataProvider` 直连真实日历、资产轴和分钟数据源。

`legacy/` 保存旧链路 notebook（含基于 Snapshot Store 的
`昼夜合成反转因子_新版.ipynb` 与旧研究层测试 notebook），仅供参考，其依赖的
`FeatureStoreDataProvider` 兼容层已移除，不能直接运行。新引擎的最小可运行示例见
根目录 [`README.md`](../README.md)。
