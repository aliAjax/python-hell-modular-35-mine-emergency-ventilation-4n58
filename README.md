# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件、处置任务和交接班记录。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录，`source_id + record_id` 相同会幂等返回原记录。

## 交接班（责任转移）

跨班任务通过`handover`对象交接，状态机为`pending → confirmed / void`：

- 交班时必填`task_id`、`from_team`、`to_team`和`pending_items`（没做完的事）；`from_team`必须是任务当前的`responsible_team`，field角色只能交出本班组负责的任务。
- 同一任务只允许一条`pending`交接（规则校验加数据库部分唯一索引兜底），并发或重复提交时后到者收到409，不会顶掉先到者。
- 确认前责任仍在原班组：接班班组的field成员对任务`accept/complete`会被拒绝（未确认不得开工）；确认由接班班组（或调度）执行，确认后任务的`responsible_team`与`open_items`在同一事务内划转，原班组field成员再改任务即越权（403）。班组身份优先按人员档案（worker的name/id匹配用户）解析，缺失时退回请求内的`team`字段。
- 任务一旦发生任何状态流转，其未确认的交接自动作废；调度可用`reassign`动作（assigned/in_progress → assigned）或重新发起交接来重新派工。
- 断网时记录的交接通过离线记录合并回网：payload取`{"type":"handover",...}`或`{"type":"handover_confirm",...}`，按`source_id + record_id`幂等，重复上传只认一次；应用失败（如任务已有待确认交接）会在离线记录上留下`applied=false`和`apply_error`，由调度处理。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 任务`assign`/`reassign`必须指定`team`，写入`responsible_team`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务（且每条未完成任务都有负责班组），并且所有通风设备恢复运行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
