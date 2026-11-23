# 涂层厚度 P95 服务

FastAPI + SQLite。按卷报送产线、生产日与分档计数（互不重叠的整数微米闭区间、
正整数计数）；可用更窄子档替换当前某档；按产线与日期范围查询 P95 的最小/最大
可能值，并列出值得补细分数据的卷与档。

## 数据模型与约定

- 日汇总由 `(line_id, production_day, coil_id)` 唯一标识；同一卷号可出现在
  不同生产日，各自独立。
- 一次报送 = 该卷当日的**日汇总**：若干 `[lo, hi]` 闭区间（整数微米）与
  `count`（正整数）。同一卷的档之间不得重叠。
- 服务**只存分档计数，从不展开测点**，一天上千卷、每卷上百万测点也只占一条
  日汇总。
- 厚度界限为 ±10⁹ μm，单档计数上限 10¹²，保证 SQLite 64 位聚合与秩运算不溢出；
  越界返回 422。
- 重复报送同一键会整体替换该卷当日的分档。`POST /reports/bulk` 支持一条产线
  一天上千卷整批上报，整批原子生效。
- 细化：`target` 按闭区间 `lo/hi` 定位当前档，可选带 `count`（带上时必须与
  当前报送一致，防误改）；`children` 须都在原档内、彼此不重叠，计数之和等于
  原档计数。新档位与日汇总在同一事务提交；任一规则失败即回滚，沿用原数据。

## P95 的定义与算法

- 秩 `k = ceil(0.95 × N)`，`N` = 各档计数之和，`k` 从 1 开始。
- 每个档只知计数，不知档内分布，因此：
  - **最小可能值** = 把每档所有测点放在档下界 `lo` 后的加权第 `k` 小；
  - **最大可能值** = 把每档所有测点放在档上界 `hi` 后的加权第 `k` 小。
- 两者都是加权次序统计量，用 SQL 窗口函数按 `lo` / `hi` 求累计计数得到，
  不为每档构造 Python 对象，也不展开测点。
- 两端不同时，列出与 `[p95_min, p95_max]` 交集**至少包含两个整数微米值**的
  分档及卷号。两端相同时该列表为空。
- 下界、上界、秩、候选分档都在**同一个 SQLite 只读事务快照**中读取，因此
  一定来自同一次提交；并发细化不会造成两端与列表错配。
- 日期范围无任何测点时返回 `has_data=false`，各数值为 `null`。
- `domain.py` 中的 `p95_range` 是等价的纯 Python 参考实现，测试用它做随机对照。

## API（`/docs` 可交互调试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 报送一卷的分档计数 |
| POST | `/reports/bulk` | 整批原子报送多卷日汇总 |
| POST | `/reports/{line_id}/{coil_id}/refinements` | 用更窄子档替换当前某档 |
| GET | `/reports/{line_id}/{coil_id}?production_day=` | 查看某卷某日当前分档与版本 |
| GET | `/p95?line_id=&start=&end=` | 查询 P95 最小/最大可能值与候选档 |
| GET | `/health` | 健康检查（容器健康探针使用） |
| GET | `/docs` | Swagger UI |

示例：

```bash
curl -X POST localhost:8000/reports -H 'content-type: application/json' -d '{
  "line_id": "L1", "production_day": "2026-11-20", "coil_id": "C1",
  "buckets": [{"lo": 10, "hi": 20, "count": 1000}, {"lo": 21, "hi": 30, "count": 500}]
}'

curl -X POST localhost:8000/reports/L1/C1/refinements -H 'content-type: application/json' -d '{
  "production_day": "2026-11-20",
  "target": {"lo": 10, "hi": 20, "count": 1000},
  "children": [{"lo": 10, "hi": 14, "count": 400}, {"lo": 15, "hi": 20, "count": 600}]
}'

curl 'localhost:8000/p95?line_id=L1&start=2026-11-20&end=2026-11-20'
```

`/p95` 响应要点：

```json
{
  "has_data": true,
  "measurements": 1500,
  "rank": 1425,
  "p95_min": 21,
  "p95_max": 30,
  "candidate_buckets": [
    {"coil_id": "C1", "production_day": "2026-11-20", "lo": 21, "hi": 30, "count": 500}
  ],
  "coil_ids": ["C1"]
}
```


## 运行

### Docker Compose

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

数据库存放在命名卷 `coating-data` 的 `/data/coating.db`，容器重启不丢。

### 本地

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

`DATABASE_PATH` 可覆盖默认路径（默认 `./data/coating.db`）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：秩公式、档位合法性与互不重叠、子档规则、报送/批量报送/细化成功路径、
各类失败后的回滚与版本不变、P95 两端与候选档过滤（SQL 聚合与纯 Python
参考实现的随机对照）、空数据、日期范围、以及“同一次快照”的一致性。
