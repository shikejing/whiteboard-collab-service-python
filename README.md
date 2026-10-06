# Whiteboard Collaboration Service

面向在线白板与批注协同的服务端。多个客户端在同一块画板上并发增删图形、
绘制批注、调整层级；服务端把所有操作归并成**唯一的全局顺序**，保证所有
客户端最终看到一致的内容，且历史操作可追溯、可压缩、可撤销。

## 运行

```bash
uv sync                 # 安装依赖
python main.py          # 启动服务 (127.0.0.1:8000)
python -m pytest tests/ # 运行全部测试
```

## 架构

```
whiteboard/
  models.py    数据类型：Operation / SequencedOp / Shape / Snapshot / Role
  document.py  画板状态机：确定性应用操作、计算逆操作（供 undo）
  history.py   OpLog：全序操作日志 + 快照压缩 + 增量同步
  undo.py      按用户隔离的 undo/redo 栈
  auth.py      按 (用户, 画板) 的角色访问控制 + 越权审计日志
  session.py   BoardSession：归并点（锁内定序）、去重、撤销解析、压缩
  server.py    FastAPI HTTP + WebSocket 接口
```

## 一致性语义

1. **全局全序（归并）**：同一画板的所有操作在 `BoardSession.submit` 内
   串行化（锁），按到达顺序分配单调递增的 `server_seq`。这个顺序是唯一
   权威顺序；任何客户端按该顺序应用操作流，都会收敛到相同状态。
   客户端发送顺序、网络乱序不影响最终一致性——只影响哪条操作"先到"。

2. **冲突解决**：并发修改同一图形采用 **last-writer-wins（按 server_seq）**。
   结果对所有人一致且可解释：赢家就是日志中最后一条修改该字段的操作的
   提交者。`base_version` 仅作信息携带，服务端不做乐观锁拒绝。

3. **幂等去重**：操作以 `(client_id, client_seq)` 为幂等键。重连后重发、
   网络重传都会被识别为重复（返回 `duplicate: true` 与原始 `server_seq`），
   不会产生重复图形，也不会因去重丢掉中间的编辑。

4. **离线重连（增量同步）**：客户端记录最后见到的 `version`，重连后调用
   `sync?since=V`，服务端只返回 `server_seq > V` 的操作。若 `V` 早于压缩点，
   先返回快照再返回日志尾部，二者叠加与完整重放严格等价。

5. **快照压缩**：`compact(upto_version=N)` 把 `server_seq <= N` 的操作合并
   成快照（从上一快照重放重建，保证等价）。压缩不重新编号，日志尾部
   原样保留；客户端水位线随快照保存，压缩后重发旧操作仍被去重。
   压缩点之前的 undo 历史随之失效（undo 会跳过已压缩的操作）。

6. **撤销/重做**：undo 只作用于**发起者自己**的操作序列。服务端在操作
   定序时为其计算逆操作并存入日志；undo/redo 本身也是日志中的普通操作
   （携带解析后的 effect），因此所有副本以相同顺序应用，结果稳定。
   语义：undo 恢复该操作定序时捕获的旧值；若他人之后改动了同一元素，
   undo 仍恢复捕获值（last-writer-wins）。新编辑会清空该用户的 redo 栈。

7. **访问控制**：角色 `owner > editor > viewer`，按 (画板, 用户) 授权。
   viewer 只读；未授权用户读写均拒绝。所有越权尝试写入审计日志
   （`GET /boards/{board}/audit`）。任一客户端断开不影响其他客户端。

## 接口约定

所有请求/响应均为 JSON。操作对象：

```json
{
  "client_id": "c1",            // 客户端标识（连接级唯一）
  "client_seq": 7,              // 客户端本地单调序号（幂等键的一半）
  "kind": "add_shape",          // add_shape|update_shape|remove_shape|
                                // add_annotation|update_annotation|remove_annotation|
                                // reorder|undo|redo
  "user_id": "alice",
  "base_version": 12,           // 客户端基于的画板版本（信息性）
  "target_id": "shape-9",       // add_* 时为新建元素 id
  "payload": {"x": 10, "z": 3}  // 种类相关数据；reorder 用 {"z": N}
}
```

### HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/boards/{board}/grants` | 授权 `{by, user_id, role}`；首个授权可自助（bootstrap），之后仅 owner |
| POST | `/boards/{board}/ops` | 提交操作 → `{accepted, duplicate, server_seq, error, version}` |
| GET  | `/boards/{board}/sync?user_id=&client_id=&since=` | 增量同步 → `{version, snapshot|null, ops[]}` |
| POST | `/boards/{board}/compact` | `{by, upto_version}`，仅 owner |
| GET  | `/boards/{board}/audit` | 该画板的越权审计记录 |

错误：越权返回 403（并记录审计）；参数非法返回 400。

### WebSocket

`WS /boards/{board}/ws?user_id=&client_id=`

- 客户端发送操作 JSON；服务端定序后把 `{ops: [...], version}` 广播给
  该画板所有连接（含发送者）。
- 越权操作返回 `{error, client_seq}`，连接保持。
- 断开重连后应先用 HTTP `sync` 补齐缺口，再继续接收广播。

## 测试覆盖（34 项）

- `test_concurrent_merge.py`：多线程并发归并、可交换操作任意顺序收敛、
  冲突编辑/层级调整的全副本一致性
- `test_out_of_order_and_duplicates.py`：乱序到达、重传风暴、交错去重
- `test_reconnect.py`：断开不影响他人、重连恰好补齐缺失操作、
  重连重发不产生重复
- `test_compaction.py`：快照与完整重放等价、压缩后新客户端内容等价、
  离线客户端跨压缩点增量同步、去重跨压缩存活、压缩单调性
- `test_acl.py`：只读拒绝、未授权拒绝、审计记录、角色变更即时生效、
  同图形并发修改确定性
- `test_undo_redo.py`：undo 仅作用自身、交错编辑稳定、remove 可恢复、
  redo 失效语义、逐用户栈
- `test_api.py`：HTTP 全流程、越权 403 + 审计、WebSocket 广播与只读拒绝
