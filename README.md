# Helm Values Cleanup & Rollout Planning API

一个 **FastAPI** 服务，包含两个互不依赖、都不连接 Kubernetes 集群的能力：

1. **`POST /api/v1/solve`** —— 清理多个 Chart 版本共用的 values 覆盖文件。
2. **`POST /api/v1/rollout/plan`** —— 为 Helm 滚动更新做分批排程：先渲染新版
   Chart，再把当前 Deployment 和新版配对，在 CPU / 内存请求配额内求出**最少批次**、
   每批**峰值用量**的完整排程。

两个接口都通过 `helm template` 在本地渲染（预排不连集群），也不写回源文件。

---

## 一、滚动更新分批排程（`/api/v1/rollout/plan`）

### 输入

| 字段 | 说明 |
| --- | --- |
| `current_deployments_yaml` | 现行 Deployment 的 YAML 文本（可含多个文档、其他 kind） |
| `chart_dir` | 已挂载的新版 Chart 目录绝对路径 |
| `values_path` | 渲染新版使用的 values 文件绝对路径 |
| `release_name` / `namespace` | `helm template` 使用的发布名与命名空间 |
| `quota` | 命名空间配额，`{cpu, memory}`，**两项都强制** |
| `other_used` | 排程范围之外已占用的请求量，**需自行包含被 `ignored` 的 Deployment**；省略按 0 计 |

`cpu` 必须是**整数毫核**（如 `"250m"`），`memory` 必须是**整数 MiB**（如 `"256Mi"`）。
`"0.25"`、`"1"`、`"1Gi"` 等一律 422，不做隐式换算。

### 配对与筛选

- 用 `helm template` 渲染新版 Chart，取出其中的 `Deployment`。
- 按 **`(metadata.namespace, metadata.name)`** 与现行 Deployment 配对；缺 namespace
  时回退到请求里的 `namespace`。
- 计入排程的必须是：**`apps/v1` Deployment、副本数不变（缺省为 1）、Pod 模板有改动**，
  且**最多 8 对**（超过即 422）。
- 以下配对不参与排程，进入 `ignored` 并说明原因：Pod 模板未变化、单边缺失、
  **任一侧声明了 init 容器**（init 容器不在本功能范围内，不猜测其开销）。
  `ignored` 的 Deployment 仍在命名空间里占资源，所以它们的占用要由调用方放进
  `other_used`，否则算出来的峰值会偏低。
- 每个**普通容器**都必须同时声明 `resources.requests.cpu` 与 `resources.requests.memory`，
  且符合上述单位；任何一边缺失或格式不对 → 422。

### 配额模型（关键）

每批的新 Pod 会在**旧 Pod 尚未退出时**创建，所以批次峰值按“同批新旧全部重叠”计算。
设 `D` 为已换版完成的 Deployment 集合，本批为 `S`：

```
usage_before = other_used + Σ_{D} new_total + Σ_{未换版} old_total
peak         = usage_before + Σ_{S} new_total
usage_after  = usage_before + Σ_{S} (new_total - old_total)
```

- 每一项的 `total = 每副本请求量（普通容器求和） × replicas`。
- **CPU 和内存的 `peak` 都不能超过 `quota`**，否则该批不可行。
- 一批新 Pod 就绪、旧 Pod 全部退出后才进入下一批；下一批的起点就是上一批的
  `usage_after`。
- 副本数不变，所以换版完成后的稳定占用是 `other_used + Σ new_total`。

### 求解

在至多 8 个 Deployment 上做**精确子集搜索**（记忆化状态压缩），返回批次最少的排程；
同样少批次时，优先“首批最大”，再按请求中 `current_deployments_yaml` 的出现顺序确定性
选择。 `feasible=false` 时**不返回任何批次**（`batches=[]`），只给 `reason`；HTTP 仍是 200。

### 响应

`feasible`、`reason`、`min_batches`、`batches[]`（每批的 Deployment、`usage_before`、
`new_pods_added`、`peak`、`usage_after`）、`deployments[]`（每对的旧/新单副本与总量）、
`ignored[]`、`quota`、`other_used`、`initial_usage`、`final_usage`。

### Swagger 样例：顺序排就会卡住

`/docs` 里的 **rollout-demo** 样例（同时保存在 `examples/rollout-demo.request.json`）
刻意构造了一个顺序陷阱：

- 配额 500m/640Mi。排程外的 `report` 已占 50m/64Mi（通过 `other_used` 传入），
  三对计划内 Deployment 的旧 Pod 合计 200m/256Mi，所以起点是 250m/320Mi。
- 按 YAML 顺序逐个升级 `api → worker → search`：前两步（300m、400m）都通过，
  第三步 `search` 会到 **550m/704Mi**，超配额卡死。
- 真正的最少排程是 **2 批**：`api + search` 一批（峰值正好 500m/640Mi），
  `worker` 一批（峰值也正好 500m/640Mi）。
- 第四个 Deployment `report` 带 init 容器，被明确列入 `ignored`，不参与排程。

---

## 二、共用 values 清理（`/api/v1/solve`，保留的原有接口）

生产仍在用旧版 Helm Chart（v1），预发已切到新版（v2），两边共用同一份 values
覆盖文件。该接口找出覆盖文件里那些只是重复 Chart 默认值、删掉后**每个版本**渲染结果
都保持不变的映射键，并返回清理后的 YAML 和删键路径；其余覆盖原样保留。

### 目录结构

```
mounted-charts/                 # 模拟“挂载目录”
├── orders-api/{v1,v2}/
├── payment-web/{v1,v2}/
└── rollout-demo/v2/            # 滚动排程样例用的新版 Chart
overrides/                      # 两版共用的 values 覆盖（待清理对象）
app/
├── main.py                     # 路由、请求/响应模型、Swagger 样例注入
├── solver.py                   # 基线渲染 → 单键筛选 → 最多 8 键组合穷举 → 复核
├── helm.py                     # helm template 封装
├── normalize.py                # 清单归一化与语义比对
├── values_tools.py             # 叶键枚举、路径表示、删键
├── quantities.py               # 严格的 m / Mi 数量解析
├── rollout.py                  # 配对、配额模型、最少批次搜索
└── examples.py                 # rollout Swagger 样例（与 examples/ 同步）
tests/                          # pytest 自测
Dockerfile
docker-compose.yml
requirements.txt
examples/
```

### 清理规则与算法

1. **基线渲染**：用固定发布名和命名空间对每个版本执行 `helm template -f <覆盖文件>`。
   任一版本基线渲染失败 → HTTP 422。
2. **叶键枚举**：只枚举映射键的叶路径（标量/列表终止），天然互不包含；带点的键用
   引号段表示（如 `podAnnotations."checksum/legacy"`）。
3. **单键筛选**：逐个删键试渲染，只有单独删除后所有版本仍渲染成功且清单与基线语义
   一致的键才进入候选。
4. **候选上限 8 个**：可删键超过 8 个时按覆盖文件顺序取前 8（`candidate_cap_applied=true`）。
5. **组合穷举**：从最大子集开始尝试，找到能同时删除的最多候选键；渲染失败的组合跳过
   并计入 `render_failures`。
6. **最终复核**：用清理后的 YAML 对每个版本重新渲染比对，全部一致才 `verified=true`。

### 比对语义

- 忽略 YAML 注释、文档顺序、映射键顺序；**列表顺序必须相同**。

### 两个 Swagger 样例

- `orders-api`：21 个叶键，**8 个可全部删除**；跨版默认值翻转的
  `podSecurityContext.*` 被正确保留。
- `payment-web`：18 个叶键，其中 10 个单独可删，受 8 键上限约束取前 8；v2 独有的
  `nodeSelector.disktype` 被正确保留。

---

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# Swagger UI（两个接口的样例都可直接 Try it out）： http://localhost:8000/docs
# OpenAPI JSON：                                     http://localhost:8000/openapi.json
# 健康检查：                                         http://localhost:8000/health
```

镜像内安装了 Helm 3，并把 `mounted-charts/`、`overrides/` 以只读卷挂到
`/workspace/...`，与样例请求里的绝对路径一致。

### 本地直跑（无 Docker 时）

```bash
# Helm 3 需在 PATH 上，Python 依赖见 requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### curl

```bash
# 共用 values 清理
curl -s -X POST http://localhost:8000/api/v1/solve \
  -H 'Content-Type: application/json' \
  --data @examples/orders-api.request.json | jq .
# 想要 YAML 响应：加 -H 'Accept: application/yaml'

# 滚动更新排程
curl -s -X POST http://localhost:8000/api/v1/rollout/plan \
  -H 'Content-Type: application/json' \
  --data @examples/rollout-demo.request.json | jq .
```

### 错误约定

- 400：文件不存在 / YAML 无法解析。
- 422：`helm template` 失败；Deployment 不是 `apps/v1`、副本数变化、超过 8 对、
  缺 requests、单位非 `m`/`Mi` 等规则违例。

## 自测

```bash
PATH=/path/to/helm:$PATH PYTHONPATH=. python3 -m pytest tests/ -q
# 当前实测：78 passed
```

覆盖：归一化比对规则、路径工具、两个 Chart 的端到端清理、8 键上限、跨版陷阱键保留、
基线失败报错、删键组合渲染失败跳过；数量解析的合法/非法形式、配对与筛选（幂等模板、
副本变化、非 apps/v1、init 容器、缺 requests、超过 8 对、replicas 放大）、
峰值/分段占用的算术、CPU 与内存分别超配、无解时不出批次、最少批次与首批最大
的确定性、起始占用已超配额、以及 API 200/400/422 与全部 Swagger 样例。
