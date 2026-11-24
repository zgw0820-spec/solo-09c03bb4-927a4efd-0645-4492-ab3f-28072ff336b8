# Shared Helm Values Cleanup API

生产仍在用旧版 Helm Chart（v1），预发已切到新版（v2），而两边共用同一份 values
覆盖文件。本服务用 **FastAPI** 找出覆盖文件里那些只是重复 Chart 默认值、删掉后
**每个版本** 渲染结果都保持不变的映射键，并返回清理后的 YAML 和删键路径；其余覆盖
原样保留。

## 目录结构

```
mounted-charts/                 # 模拟“挂载目录”：两个渲染结果稳定的 Chart
├── orders-api/
│   ├── v1/                     # 旧版（生产，chart 1.4.0 / app 1.4.2）
│   └── v2/                     # 新版（预发，chart 2.1.0 / app 2.1.0，含 HPA 等）
└── payment-web/
    ├── v1/                     # 旧版（生产，chart 1.8.0）
    └── v2/                     # 新版（预发，chart 2.0.0，含 nodeSelector/ConfigMap/ServiceMonitor）
overrides/                      # 两版共用的 values 覆盖（待清理对象）
├── orders-api.values.yaml
└── payment-web.values.yaml
app/                            # FastAPI 服务
├── main.py                     # 路由、请求模型、Swagger 双样例
├── solver.py                   # 基线渲染 → 单键筛选 → 最多 8 键组合穷举 → 复核
├── helm.py                     # helm template 封装（固定 release/namespace）
├── normalize.py                # 清单归一化与语义比对
└── values_tools.py             # 叶键枚举、路径表示、删键
tests/                          # pytest 自测（24 个用例）
Dockerfile
docker-compose.yml
requirements.txt
examples/                       # Swagger 两个样例对应的原始请求体
```

## 清理规则与算法

1. **基线渲染**：用固定发布名 `platform-rel` 和命名空间 `platform` 对每个版本执行
   `helm template -f <覆盖文件>`。任一版本基线渲染失败 → 直接报错（HTTP 422）。
2. **叶键枚举**：只枚举映射键的叶路径（标量/列表终止），天然互不包含；
   形如 `image.pullPolicy`，带点的键用引号段表示（如
   `podAnnotations."checksum/legacy"`）。
3. **单键筛选**：逐个删键试渲染，只有单独删除后所有版本仍渲染成功且清单与基线
   语义一致的键才进入候选——避免把 8 个名额浪费在不可能删除的键上。
4. **候选上限 8 个**：可删键超过 8 个时按覆盖文件中的顺序取前 8（响应中
   `candidate_cap_applied=true`）。
5. **组合穷举**：从最大子集开始尝试，找到能同时删除的最多候选键即返回。
   某组合渲染失败不会中断流程，自动跳过并尝试其他组合（`render_failures` 计数）。
6. **最终复核**：用清理后的 YAML 对每个版本重新渲染并与基线比对，全部一致才置
   `verified=true`。

### 比对语义

- 忽略 YAML 注释（解析为对象后注释不存在）
- 忽略文档先后（按 `kind/namespace/name` 排序后比较）
- 忽略映射键顺序（字典按内容比较）
- **列表顺序必须相同**（列表逐元素有序比较）

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# Swagger UI（可直接试两个样例）： http://localhost:8000/docs
# OpenAPI JSON：                    http://localhost:8000/openapi.json
# 健康检查：                        http://localhost:8000/health
```

镜像内安装了 Helm 3，并把 `mounted-charts/`、`overrides/` 以只读卷挂到
`/workspace/...`，与示例请求里的绝对路径一致。

### 本地直跑（无 Docker 时）

```bash
# 依赖 Helm 3 在 PATH 上，Python 依赖见 requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `POST /api/v1/solve`

请求体：

| 字段 | 说明 |
| --- | --- |
| `values_override_path` | 共用覆盖 YAML 的绝对路径 |
| `versions[]` | 每个版本的 `label`（唯一）与 `chart_dir`（绝对路径） |
| `release_name` | 固定发布名，默认 `platform-rel` |
| `namespace` | 固定命名空间，默认 `platform` |

响应关键字段：`cleaned_yaml`（删键后的覆盖）、`removed_paths`（可删路径）、
`candidate_paths`（最多 8 个候选）、`kept_paths`（保留的其余叶键）、
`versions[].manifests_match_baseline`、`verified`。

两个可在 Swagger 直接试用的样例（请求体同时保存在 `examples/`）：

- `orders-api`：21 个叶键，**8 个可全部删除**；跨版默认值翻转的
  `podSecurityContext.*` 被正确保留。
- `payment-web`：18 个叶键，其中 10 个单独可删，受 8 键上限约束取前 8；
  v2 独有的 `nodeSelector.disktype`（v1 模板忽略它，看似可删）被正确保留。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/solve \
  -H 'Content-Type: application/json' \
  --data @examples/orders-api.request.json | jq .
# 想要 YAML 响应：加 -H 'Accept: application/yaml'
```

错误约定：覆盖文件不存在/无法解析 → 400；基线 `helm template` 失败 → 422（附 helm stderr）。

## 自测

```bash
PATH=/path/to/helm:$PATH PYTHONPATH=<deps> python3 -m pytest tests/ -q
# 当前实测：24 passed
```

覆盖：归一化比对四条规则、路径工具、两个 Chart 的端到端清理、8 键上限、
跨版陷阱键保留、基线失败报错、删键组合渲染失败自动跳过、API 200/400/422 与
Swagger 双样例。
