# Factor Engine — Production

本分支只保留 Factor Engine 的生产运行代码、锁文件和连接配置。完整文档、示例、测试与基准位于 `main`，历史实现位于 `legacy`。

## 部署

```bash
uv sync --frozen
```

支持 Python 3.11–3.13。

## 数据库配置

`SmartQuantDataProvider` 从仓库根目录 `rawdata.json` 的 `oceanbase` 对象读取连接信息：

```json
{
  "oceanbase": {
    "host": "...",
    "port": "...",
    "database": "...",
    "user": "...",
    "password": "..."
  }
}
```

生产连接不读取 `.env` 或 `OB_*` 环境变量。

## 启动检查

```bash
uv run python -c "import factor_engine; print('factor_engine OK')"
```
