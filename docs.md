# 在线白板与批注协同服务

一个进程内（in-memory）的协同白板服务端，支持多客户端在同一画板上并发增删图形、
绘制批注、调整层级，处理离线重连与增量同步，提供历史压缩/快照、按用户与画板的访问
控制、以及作用于"自己操作序列"的撤销/重做。

- HTTP 框架：FastAPI（见 `whiteboard/api.py`）
- 纯逻辑内核：`whiteboard/service.py` 门面 + 引擎/历史/快照/权限/会话/撤销各模块
- 测试：`pytest`，共 39 个用例，覆盖并发归并、乱序/重复、离线重连、快照压缩、
  权限拒绝、撤销重做隔离。

## 1. 模块划分

| 模块 | 职责 |
| --- | --- |
| `whiteboard/models.py` | 领域类型：`OpId/Op/ShapeRecord/BoardState/Snapshot/Role/...` |
| `whiteboard/engine.py` | 确定性归并引擎：逐字段 LWW、活性（墓碑）、层级寄存器 |
| `whiteboard/history.py` | 只追加的提交日志与按版本区间查询（配合压缩偏移） |
| `whiteboard/snapshots.py` | 历史压缩：重放日志前缀生成快照，截断后仍可全量/增量同步 |
| `whiteboard/sessions.py` | 画板会话与连接管理；某客户端断/连不影响其他人 |
| `whiteboard/permissions.py` | 画板×用户角色访问控制，所有允许/拒绝写审计日志 |
| `whiteboard/undo.py` | 撤销/重做：只作用于发起者自己的操作序列，逆操作带保护 |
| `whiteboard/service.py` | 门面：每把画板锁串行化提交，串联上述所有子系统 |
| `whiteboard/serialization.py` | Op/OpId 的 JSON 编解码 |
| `whiteboard/api.py` | FastAPI 路由 |

## 2. 一致性语义（Convergence Contract）

### 2.1 全局顺序与版本

- 每个画板有一把锁和一条提交日志，操作在锁内**串行提交**；
- 提交时赋予从 1 开始的稠密全局版本 `version`（等于已提交操作条数，
  客户端 BATCH 整体占一个版本）；
- 客户端每个操作带 `(client_id, client_seq)` 与 `base_version`（本地序号与所基于的
  画板版本，用于可解释性/调试）。全局提交顺序由服务端到达顺序决定；而**最终内容**
  与到达顺序无关（见下）。

### 2.2 操作身份 OpId（全序、确定性）

```
OpId = (client_id, seq, tag)
  tag = 0 client   客户端原始操作，seq = 客户端本地序号
  tag = 1 undo     服务端撤销批次，seq = 该用户撤销计数器
  tag = 2 redo     服务端重做批次
```

- 客户端操作按 `(client_id, seq)` 去重：重连/重试/重复上报返回同一结果且不重复入库；
- `OpId` 是字典序全序，作为所有 LWW 冲突仲裁的依据，因此**同一批输入在任意到达
  顺序下最终内容一致**（引擎对操作集合可交换）。

### 2.3 CRDT 风格归并规则（与到达顺序无关）

每个实体（图形或批注）由若干独立寄存器组成：

- **逐字段 LWW**：`color/position/text/...` 各自是一个最后写入胜出寄存器，
  按 `OpId` 全序比较。不同字段的并发修改自然合并；同一字段的并发修改由 OpId
  决定唯一胜者，结果确定且可解释（可在状态中查到获胜 OpId）。
- **活性寄存器（liveness）**：`add` 写 True，`delete` 写 False，二者仅按 OpId
  竞争，删除以墓碑形式保留；更高 id 的删除不会被晚到的低 id 新增"复活"。
- **层级寄存器 z**：`zmove` 写一个 LWW 整数；渲染时按 `(z, 出生 OpId, 实体 id)`
  稳定排序，层级调整结果确定。
- **迟到写入不丢失**：更新/层级/墓碑即使先于该实体的 `add` 到达也会被暂存；
  渲染只显示"已出生且未被删除"的实体。集合重放在任意排列下结果相同。

> 冲突解释示例：两个编辑者同时改同一字段，`(client_id, seq)` 字典序更大者胜出，
> 与谁先到达服务端、与线程调度均无关；`GET state` 与内部 `ShapeRecord.fields`
> 可以直接给出获胜操作身份。

### 2.4 去重与乱序

- 服务端维护 `seen[(client_id, client_seq)] -> Op`；重复提交返回
  `duplicate=true`，版本不变，不产生重复图形；
- 乱序到达由上面的 LWW/墓碑/暂存规则天然吸收，无需客户端做 OT 变换。

## 3. 离线重连与增量同步

- `POST /connect`（或 `GET /sync?since=<version>`）给出 `DeltaSync`：
  - 若 `since >= 压缩边界`：只返回 `(since, version]` 的操作，纯增量；
  - 若 `since < 压缩边界`：返回快照全量内容 `full_state` + 压缩后操作尾，
    客户端据此可重建等价完整内容，不会漏掉中间编辑；
- 重连后重复上报的旧操作仍被 `seen` 索引识别（快照会携带该索引，见第 4 节），
  不会产生重复图形或丢失编辑；
- 会话与内容解耦：某客户端 `disconnect` 只翻转自己的连接标记，画板内容与其他
  客户端连接/编辑完全不受影响；同一 `client_id` 可建立新连接完成重连。

## 4. 历史压缩与快照

`POST /compact?through_version=k`（owner）把 `1..k` 折叠为快照：

- 快照内容通过**在空画板上重放日志前缀**得到，严格等于第 k 版本状态，绝不混入
  之后的操作；
- 快照保存：实体寄存器（含 LWW 属主、字段历史链、墓碑）、
  `(client_id, client_seq)` 去重索引、各用户撤销/重做栈；
- 压缩只截断内存日志前缀：
  - 压缩**之后**新加入的客户端拿到"快照 + 尾部"，等价于完整内容；
  - 压缩时仍离线、`last_version` 早于边界的客户端重连走全量快照 + 尾部，
    **增量同步不被破坏**；
  - 压缩前的旧操作被重复上报时，因快照保留去重索引，依旧被识别忽略；
  - 对压缩前操作的撤销仍有效（寄存器保留 `prev` 历史链）。

## 5. 访问控制

- 角色：`owner`（拥有者）/ `editor`（编辑者）可写；`viewer` 只读；未知用户无权。
- 写操作前对每个具体操作（含 BATCH 内的每个子操作）做画板级鉴权；
- 只读用户提交修改、未知用户访问、越权授权/压缩/查审计均返回 `403` 并写审计；
- HTTP 头 `X-User-Id` 标识当前用户（示例约定，生产应替换为已校验的会话/JWT）；
  请求体中的 `author` 必须与头一致，防止伪造他人身份提交；
- 多用户并发修改同一图形：由 §2 的逐字段 LWW 决定唯一、可复现、可解释的结果。

## 6. 撤销与重做

- 每个已提交的客户端操作（或客户端 BATCH）作为一个 `UndoUnit` 压入**作者本人**的
  撤销栈；新的编辑会清空该用户的重做栈（标准编辑器语义）；
- 撤销生成逆操作（服务端 BATCH），每个逆写带：
  - `anchor`：被回滚的原始客户端 OpId；
  - `guard`：当前期望持有该寄存器的属主；
  并遵循两条仲裁规则：
  1. **逆操作 vs 常规写**：只要存在比 `anchor` 更新的常规写就不生效——他人在中间
     的并发编辑一定被保留，撤销只回退"仍然归属于自己目标操作"的字段；
  2. **逆操作 vs 逆操作**：服务端在画板锁内串行生成逆操作，后生成者（撤销删除再
     撤销新增等链式场景）生效；
- 字段逆值取自寄存器 `prev` 历史链，可精确恢复"该操作覆盖前"的值，包括恢复为
  "字段原本不存在"；
- 重做按原始效果再次应用，并以撤销逆作为 guard；撤销/重做与新操作任意交错结果
  稳定；撤销空栈返回 `accepted=false`。

## 7. HTTP 接口约定

所有接口前缀 `/api`，写/读均需头 `X-User-Id`。

| 方法与路径 | 身份 | 说明 |
| --- | --- | --- |
| `POST /boards/{board}?owner={uid}` | - | 建板，owner 自动授权 |
| `POST /boards/{board}/roles` | owner | 授予角色 `{user_id, role}` |
| `GET  /boards/{board}/state` | 任意成员 | 当前完整内容（存活实体，按层级排序） |
| `POST /boards/{board}/ops` | editor/owner | 提交一个操作或 BATCH |
| `POST /boards/{board}/undo` | editor/owner | 撤销本人最近操作 |
| `POST /boards/{board}/redo` | editor/owner | 重做 |
| `POST /boards/{board}/connect` | 任意成员 | `{client_id, last_version}` 建连接并返回增量 |
| `POST /boards/{board}/disconnect?conn_id=` | - | 断开某连接 |
| `GET  /boards/{board}/sync?since=` | 任意成员 | 增量/跨快照同步 |
| `POST /boards/{board}/compact?through_version=` | owner | 压缩历史为快照 |
| `GET  /boards/{board}/audit` | owner | 访问审计（允许与拒绝） |

### 操作 JSON 示例

```json
{
  "id": {"client_id": "c1", "seq": 7, "tag": "client"},
  "type": "update_shape",
  "author": "alice",
  "base_version": 12,
  "shape_id": "shape-9",
  "props": {"color": "#0a0", "x": 120}
}
```

支持的 `type`：`add_shape / update_shape / delete_shape / zmove_shape /
add_annot / update_annot / delete_annot / batch`。

- 新增：`shape_id`（图形）或 `annot_id`（批注），`props` 为初始属性，
  `z_index` 为初始层级；
- 修改：`props` 为本次要写的字段（未提及字段不动）；
- 层级：`zmove_shape` 带 `z_index`；
- BATCH：`type=batch`，`ops` 为子操作数组，整体占一个全局版本并按头部
  `(client_id, seq)` 去重。

提交响应：

```json
{"accepted": true, "version": 13, "duplicate": false, "op": { ... }, "reason": ""}
```

同步响应（跨压缩边界时 `full_state` 非空）：

```json
{
  "board_id": "b", "from_version": 2, "version": 13,
  "ops": [ ... ],
  "full_state": {"board_id": "b", "version": 10, "entities": [ ... ]}
}
```

## 8. 运行与测试

```bash
uv sync
uv run uvicorn main:app --reload       # 启动服务
uv run pytest -q                            # 全量测试
```

## 9. 范围与说明

- 当前实现为**单进程内存存储**：重启数据不保留；分布式部署需把日志/快照/去重
  索引外置，并以等价的线性化提交替代进程内锁（接口与归并语义可直接复用）。
- LWW 按 `(client_id, seq)` 仲裁要求客户端序号在其 `client_id` 命名空间内严格
  单调，这也是去重所依赖的约束。
