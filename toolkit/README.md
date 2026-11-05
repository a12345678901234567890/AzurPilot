# Dashboard 资源按需刷新（本地扩展）

把 Dashboard 上因对应任务未启用而长期显示 **“-”** 的 12 项资源，
在 Reward 任务收完菜之后读取一次真实数值，写回面板与资源账本。

---

## 1. 为什么需要它

`Dashboard.*` 的资源数值由各任务在运行时“顺手读取”。如果你没有开启
对应的任务（例如从不开商店任务），那些资源就永远不会被写入，
WebUI 上会一直显示 `-`。

## 2. 挂载方式：借用 Reward 任务

Reward 负责“收菜”（每日补给 + 任务奖励），随服务器刷新每天自动跑好几次。
本扩展在它**收完菜之后**顺带读一次资源：

```
Reward.run()
    ├─ 领石油/金币/经验
    ├─ 领任务奖励
    ├─ maybe_refresh(...)   ← 本扩展（带 4 小时节流）
    └─ task_delay(...)
```

**为什么挂在 Reward 上**：`module/reward/reward.py` 近 12 个月仅 13 次提交，
而 `alas.py` 有 91 次。只改这一个稳定文件，把 merge 冲突概率降到最低。

**代价**：Reward 被禁用时本扩展不生效。本项目假设 Reward 常开（核心功能）。

## 3. 覆盖的资源与读取位置

| 资源 | 读取位置 | 依赖的现成实现 |
| --- | --- | --- |
| `Oil`、`Coin` | 战役页 | `CampaignStatus.get_oil()` / `get_coin()` |
| `Pt` | 活动页 | `CampaignStatus.get_event_pt()`（无活动时安静跳过） |
| `ActionPoint`、`YellowCoin`、`PurpleCoin` | 大世界页 | `scheduler.resources.refresh_resources()` |
| `Gem`、`Merit`、`GuildCoin` | 军需商店 · 通用标签 | `ShopStatus.status_get_*()` |
| `Core`、`Medal` | 军需商店 · 月度标签 | `ShopStatus.status_get_*()` |
| `Cube` | 建造页 | `OCR_BUILD_CUBE_COUNT` |

> 全部复用上游已有的读取函数，**不新增任何 OCR 模板**，
> 因此与游戏内实际数值口径完全一致。

## 4. 文件构成

```
toolkit/
├── dashboard_refresh.py        # 全部实现都在这里
├── get_git.py                  # 查看上游更新差异的小工具
└── README.md                   # 本文件

module/reward/reward.py         # 只在 run() 末尾追加 5 行挂载
```

`toolkit/` 已纳入本 fork 的版本管理（`.gitignore` 中取消了 `/toolkit`），
因此 `git reset --hard` 不会删除它。

## 5. 节流

默认 **4 小时**内只真正扫描一次，避免 Reward 因失败重试时反复进出商店。
节流状态写在实例配置 `DashboardRefresh.LastRun`，重启后依然有效。

> 每次扫描约 3–6 分钟（要进出战役页/大世界页/军需商店/建造页），
> 因此不建议把节流设得太短。

## 6. 配置（可选）

写入 `config/<实例>.json` 的 `DashboardRefresh` 分组。**不配置就用默认值。**

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `Enable` | `true` | 总开关 |
| `Resources` | 全部 12 项 | 只刷哪些，如 `["Oil","Coin","Cube"]` |
| `MinIntervalHours` | `4` | 节流小时数；`0` 表示每次都刷 |

示例：

```json
{
  "DashboardRefresh": {
    "Resources": ["Oil", "Coin", "Gem", "Cube"],
    "MinIntervalHours": 6
  }
}
```

- 未知资源名自动过滤
- `MinIntervalHours` 非数字时回落到 4

## 7. 行为与风险

- **只读**：只读取余额，不购买、不刷新商品、不领取奖励
- **失败隔离**：任一阶段异常只记 warning，其余阶段继续；
  配置损坏时整轮中止（不会无节流地反复操作游戏）
- **不影响收菜**：挂在 `task_delay` 之前、`reward_mission` 之后，
  即使扫描崩溃也只影响这一次刷新

## 8. 与上游同步

本 fork 相对上游 `wess09/AzurPilot` 只领先一个 commit，改动集中在：

1. `toolkit/`（新增目录）
2. `module/reward/reward.py`（末尾 +5 行）
3. `.gitignore`（取消 `/toolkit` 忽略）

同步上游时：

```bash
git fetch upstream
git merge upstream/master     # 若 reward.py 有冲突，保留末尾那 5 行即可
git push origin master
```

## 9. 卸载

```bash
git checkout -- module/reward/reward.py   # 去掉挂载
rm -rf toolkit                            # 删除扩展
```

`DashboardRefresh` 会留在配置里但不再被调用，可手动删除该分组。
