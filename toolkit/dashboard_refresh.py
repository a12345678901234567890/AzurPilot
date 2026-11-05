"""Dashboard 资源按需刷新（本地扩展，挂载在 Reward 任务末尾）。

本文件位于 ``/toolkit`` 目录，已纳入本 fork 的版本管理，与上游仅差这一个提交。

设计取舍
========
上游每次启动都会 ``git reset --hard origin/master``。为了在"紧跟上游"和
"保留本地改动"之间取平衡，本扩展刻意满足两点：

1. **只改一个稳定文件**：挂载点是 ``module/reward/reward.py`` 的 ``run()``
   末尾——该文件 12 个月仅 13 次提交（对比 ``alas.py`` 91 次），冲突概率低。
2. **实现全部在独立文件**：真正的逻辑留在本文件。即使 reward.py 出现冲突，
   也只是末尾两行的小冲突，容易手工解决。

挂载语义
========
Reward 任务负责"收菜"（每日补给 + 任务奖励）。本扩展在它**收完菜之后**
顺带读一次 Dashboard 的各项资源余额：

    Reward.run()
        ├─ 领石油/金币/经验
        ├─ 领任务奖励
        ├─ maybe_refresh(...)   ← 本扩展：按需读取 12 项资源
        └─ task_delay(...)

因为 Reward 随服务器刷新每天自动跑多次，挂在这里无需额外调度。

节流
====
每次 Reward 都会调用 ``maybe_refresh``，但默认 **4 小时**内只真正扫一次，
避免 Reward 失败重试时反复进出商店。节流状态写进实例配置
``DashboardRefresh.LastRun``，重启后依然有效。

配置
====
写入 ``config/<实例>.json`` 的 ``DashboardRefresh`` 分组，全部可选::

    DashboardRefresh.Enable             总开关，默认 True
    DashboardRefresh.Resources          要读的资源，默认全部 12 项
    DashboardRefresh.MinIntervalHours   节流小时数，默认 4
"""
from __future__ import annotations

import datetime
from typing import Iterable

from module.logger import logger

GROUP = 'DashboardRefresh'

#: Dashboard 上定义的全部 12 个资源（见 module/config/argument/dashboard.yaml）
ALL_RESOURCES = [
    'Oil', 'Coin', 'Gem', 'Pt', 'Cube', 'ActionPoint',
    'YellowCoin', 'PurpleCoin', 'Core', 'Medal', 'Merit', 'GuildCoin',
]

#: 默认节流间隔（小时）。4 小时约等于每天最多 6 次。
DEFAULT_MIN_INTERVAL_HOURS = 4

#: 每个阶段的资源分组；顺序即导航顺序。
_STAGES = (
    ('campaign', ('Oil', 'Coin', 'Pt')),
    ('os', ('ActionPoint', 'YellowCoin', 'PurpleCoin')),
    ('shop', ('Gem', 'Medal', 'Merit', 'GuildCoin', 'Core')),
    ('build', ('Cube',)),
)

#: 海里数：不属于 Dashboard 的 12 项，走 cl1 数据库的 distance 序列，
#: 由 runtime_service.monthly_statistics_resources 渲染到主页 Dashboard。
#: 它在大世界总览页右栏常显，读取成本极低，因此不受 Resources 限制，
#: 只要进了大世界页就顺手记录（可用 RecordSeaMiles 单独关闭）。
SEA_MILES_DEFAULT = True

#: 「出海里程」数值区域（1280×720 坐标），按 2560×1440 实机截图标定。
#: 注意不是上游 MARITIME_SCHEDULE(162,505)——那个在左侧，是侵蚀1情报子页
#: 的旧位置，在大世界总览页会一直读到 0。
SEA_MILES_AREA = (1040, 414, 1096, 433)
SEA_MILES_COLOR = (242, 212, 56)
SEA_MILES_LETTER = (255, 207, 66)


# ==================== 配置读取 ====================

def _read(config, key, default):
    """读取扩展配置项。

    读取本身出异常时返回 default；但 ``cross_get`` 抛出异常说明配置对象
    不可用，此时应让异常继续上抛，由 ``maybe_refresh`` 统一兜底并中止，
    以免在配置损坏的情况下仍然去操作游戏。
    """
    try:
        value = config.cross_get(keys=[GROUP, key], default=default)
    except (AttributeError, TypeError, KeyError, IndexError):
        # 配置对象形态不符（例如测试替身），按默认值处理
        return default
    return default if value is None else value


def settings(config):
    """解析扩展配置。

    Args:
        config (AzurLaneConfig): 实例配置对象。

    Returns:
        tuple[bool, list[str], float]: (是否启用, 资源列表, 节流小时数)。
    """
    enabled = bool(_read(config, 'Enable', True))

    resources = _read(config, 'Resources', ALL_RESOURCES)
    if isinstance(resources, str):
        resources = [x.strip() for x in resources.replace('\n', ',').split(',')]
    if not isinstance(resources, (list, tuple)):
        resources = ALL_RESOURCES
    # 只保留已知资源，避免配置写错打乱导航
    resources = [x for x in resources if x in ALL_RESOURCES]

    hours = _read(config, 'MinIntervalHours', DEFAULT_MIN_INTERVAL_HOURS)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = float(DEFAULT_MIN_INTERVAL_HOURS)
    if hours < 0:
        hours = 0.0
    return enabled, resources, hours


def record_sea_miles_enabled(config):
    """是否记录海里数（默认开启，可用 RecordSeaMiles 关闭）。"""
    return bool(_read(config, 'RecordSeaMiles', SEA_MILES_DEFAULT))


# ==================== 节流 ====================

def _parse_time(value):
    """把配置里的时间值转成 datetime，失败返回 None。"""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def due(config, now=None):
    """判断本次是否需要真正扫描。

    Args:
        config (AzurLaneConfig): 实例配置对象。
        now (datetime.datetime | None): 基准时间，默认当前时间。

    Returns:
        tuple[bool, str]: (是否需要扫描, 原因说明)。
    """
    enabled, resources, hours = settings(config)
    if not enabled:
        return False, '已在配置中关闭'
    if not resources:
        return False, '未选择任何资源'
    if hours <= 0:
        return True, '未启用节流'

    now = now or datetime.datetime.now()
    last = _parse_time(_read(config, 'LastRun', None))
    if last is None:
        return True, '首次运行'
    elapsed = (now - last).total_seconds()
    # 时钟回拨（手工改时间、时区变化）时不认为已过期，避免误触发
    if elapsed < 0:
        return True, '上次运行时间在未来，重新扫描'
    if elapsed >= hours * 3600:
        return True, f'距上次 {elapsed / 3600:.1f}h 已超过 {hours:g}h'
    return False, f'距上次 {elapsed / 3600:.1f}h 未满 {hours:g}h'


def mark_done(config, now=None):
    """记录本次扫描时间，供节流判断。

    写配置失败不会中断扫描本身，但配置对象不可用（形态错误）时应上抛，
    由 ``maybe_refresh`` 中止整轮，避免无节流地反复操作游戏。
    """
    now = now or datetime.datetime.now()
    try:
        config.cross_set(f'{GROUP}.LastRun', now.replace(microsecond=0))
    except (AttributeError, TypeError, KeyError, IndexError):
        pass
    except OSError as error:
        logger.warning(f'[资源刷新] 记录时间失败: {type(error).__name__}')


# ==================== 读取实现 ====================

def _read_campaign(ui):
    """战役页读取 Oil / Coin。"""
    from module.campaign.campaign_status import CampaignStatus
    from module.ui.page import page_campaign

    status = CampaignStatus(ui.config, ui.device)
    status.ui_ensure(page_campaign)
    result = {}
    try:
        oil = status.get_oil(skip_first_screenshot=False)
        if isinstance(oil, int) and oil > 0:
            result['Oil'] = oil
    except Exception as error:
        logger.warning(f'[资源刷新] 读取石油失败: {type(error).__name__}')
    try:
        coin = status.get_coin(skip_first_screenshot=False)
        if isinstance(coin, int) and coin > 0:
            result['Coin'] = coin
    except Exception as error:
        logger.warning(f'[资源刷新] 读取物资失败: {type(error).__name__}')
    return result


def _read_pt(ui):
    """在活动页读取 PT。

    没有活动（战役页看不到活动入口按钮）时写入 0——此时 Panel 上显示 "-"
    没有意义，写 0 更直观。注意只在**明确确认**活动入口不存在时才写 0，
    读取过程出错（页面异常、OCR 失败等）则跳过，避免把真实 PT 误写成 0。
    """
    from module.campaign.campaign_status import CampaignStatus
    from module.ui.page import page_campaign
    from module.ui.assets import CAMPAIGN_GOTO_EVENT

    status = CampaignStatus(ui.config, ui.device)
    try:
        status.ui_ensure(page_campaign)
    except Exception as error:
        logger.info(f'[资源刷新] 无法进入战役页，跳过 PT: {type(error).__name__}')
        return {}

    # 先确认活动入口在不在：不在就是当前没有活动，写 0。
    # 注意：CAMPAIGN_GOTO_EVENT 是战役菜单里常驻的入口按钮，即使当前没有
    # 活动也会显示（点了进不去），所以这里只把它当作“可能存在活动”的信号，
    # 真正的判定交给下面 OCR 是否成功。
    has_event = bool(ui.appear(CAMPAIGN_GOTO_EVENT, offset=(20, 20)))
    if not has_event:
        logger.info('[资源刷新] 战役页无活动入口，PT 记为 0')
        return {'Pt': 0}

    try:
        if ui.appear_then_click(CAMPAIGN_GOTO_EVENT, offset=(20, 20), interval=2):
            ui.device.screenshot()
        # 直接读 PT，不用 get_event_pt(update=True)：那个方法在 OCR 失败时
        # 会把 Pt 写成 0，等我们拿到返回值时已经污染了配置。
        pt = status.get_event_pt(update=False)
    except Exception as error:
        logger.warning(f'[资源刷新] 活动 PT 读取异常，保留原值: {type(error).__name__}')
        return {}

    # get_event_pt 在解析失败时返回 0 或空串，这里据此判定“当前无活动”
    if pt is None or pt == '' or (isinstance(pt, int) and pt == 0):
        logger.info('[资源刷新] 活动页无 PT 数值（当前无活动），PT 记为 0')
        return {'Pt': 0}
        return {}
    return {'Pt': int(pt)}


def _read_os(ui):
    """大世界页读取 ActionPoint / YellowCoin / PurpleCoin，并顺带记录海里数。

    复用调度器已有实现，保证与任务内读数口径完全一致。
    海里数不属于 Dashboard 的 12 项：它来自 cl1 数据库的 distance 序列，
    由 ``module.api.runtime_service.monthly_statistics_resources`` 渲染到
    主页 Dashboard。只要在这里写进 cl1，就比原来只在侵蚀1练级时更新要准确
    得多——任何开新图（月度开荒、隐秘海域等）都会让海里数增长。
    """
    from module.scheduler.resources import refresh_resources
    from module.config.deep import deep_get

    wanted = ['ActionPoint', 'YellowCoin', 'PurpleCoin']
    refresh_resources(ui.config, ui.device, wanted)

    # 此刻已在大世界页，顺手读海里数
    _record_sea_miles(ui)

    result = {}
    for name in wanted:
        group = deep_get(ui.config.data, f'Dashboard.{name}') or {}
        if not isinstance(group, dict):
            continue
        value = group.get('Value')
        if isinstance(value, int) and value >= 0:
            result[name] = value
    return result


def _record_sea_miles(ui):
    """在当前画面读取「出海里程」并写入 cl1 数据库。

    **不要用** 上游的 ``OCR_SEA_MILES_DIGIT``：它的区域 ``MARITIME_SCHEDULE``
    是 ``(162, 505)``，位于屏幕**左侧**，那是侵蚀1情报子页里的旧位置，
    在大世界总览页读到的一直是 0。

    这里按 2560×1440 实机截图标定的位置读取：
    「出海里程」数值在大世界**大地图总览页右侧面板**，
    1280×720 下为 ``(1040, 414, 1096, 433)``，文字为亮黄色 RGB(242, 212, 56)。
    实测 OCR 结果为 12685，与画面一致。

    Args:
        ui: 具备 config/device 的模块对象（大世界总览页已就绪）。

    Returns:
        int | None: 读到的海里数，失败返回 None。
    """
    try:
        from module.base.button import Button
        from module.ocr.ocr import Digit

        button = Button(area=SEA_MILES_AREA, color=SEA_MILES_COLOR,
                        button=SEA_MILES_AREA, name='SEA_MILES_OVERVIEW')
        ocr = Digit(button, lang='azur_lane', letter=SEA_MILES_LETTER,
                    threshold=128, alphabet='0123456789',
                    name='SEA_MILES_OVERVIEW')

        value = ocr.ocr(ui.device.image)
        if not isinstance(value, int) or value <= 0:
            logger.info(f'[资源刷新] 海里数识别无效({value!r})，跳过')
            return None

        from module.statistics.opsi_runtime import record_ap_snapshot
        record_ap_snapshot(config=ui.config, ap_current=None,
                           source='dashboard_refresh', distance=value)
        logger.info(f'[资源刷新] 海里数已记录：{value}')
        return value
    except Exception as error:
        logger.warning(f'[资源刷新] 读取海里数失败: {type(error).__name__}: {error}')
        return None


def _enter_os_and_record_sea_miles(ui):
    """只为了记录海里数而进一次大世界页。

    仅在用户把 ActionPoint/YellowCoin/PurpleCoin 都从 Resources 移除、
    但仍保留 RecordSeaMiles 时走这条路径。

    Returns:
        int | None: 记录到的海里数。
    """
    from module.os.operation_siren import OperationSiren
    from module.ui.page import page_os

    os_ui = OperationSiren(ui.config, ui.device)
    os_ui.ui_ensure(page_os)
    return _record_sea_miles(os_ui)


def _read_shop(ui, wanted):
    """军需商店各分页读取 Gem / Medal / Merit / GuildCoin / Core。

    导航顺序严格对照 ``module/shop/shop_reward.py`` 的 ``run_once``：
    通用标签下有钻石/功勋/舰队，月度标签下有核心/勋章。
    """
    from module.shop.shop_general import GeneralShop_250814
    from module.shop.ui import ShopUI
    from module.shop.assets import (
        NAV_GENERAL, NAV_MONTHLY,
        TAB_GENERAL, TAB_MERIT, TAB_GUILD, TAB_CORE_MONTHLY, TAB_MEDAL,
    )

    shop_ui = ShopUI(ui.config, ui.device)
    shop_ui.ui_goto_shop()
    shop_ui.device.click_record_clear()

    reader = GeneralShop_250814(ui.config, ui.device)
    result = {}

    # (顶部导航, 分类标签, 资源名, 读取函数)
    plan = [
        (NAV_GENERAL, TAB_GENERAL, 'Gem', reader.status_get_gems),
        (NAV_GENERAL, TAB_MERIT, 'Merit', reader.status_get_merit),
        (NAV_GENERAL, TAB_GUILD, 'GuildCoin', reader.status_get_guild_coins),
        (NAV_MONTHLY, TAB_CORE_MONTHLY, 'Core', reader.status_get_core),
        (NAV_MONTHLY, TAB_MEDAL, 'Medal', reader.status_get_medal),
    ]
    for nav, tab, name, getter in plan:
        if name not in wanted:
            continue
        try:
            shop_ui.device.click_record_clear()
            shop_ui.shop_nav_250814.set(nav, main=shop_ui)
            shop_ui.shop_tab_250814.set(tab, main=shop_ui)
            value = getter()
            if isinstance(value, int) and value >= 0:
                result[name] = value
        except Exception as error:
            logger.warning(f'[资源刷新] 读取 {name} 失败: {type(error).__name__}')
    return result


def _read_build(ui):
    """建造页读取 Cube（心智魔方）。"""
    from module.ui.page import page_build
    from module.gacha.gacha_reward import OCR_BUILD_CUBE_COUNT

    try:
        ui.ui_ensure(page_build)
        value = OCR_BUILD_CUBE_COUNT.ocr(ui.device.image)
        if getattr(OCR_BUILD_CUBE_COUNT, 'last_valid', False) and isinstance(value, int):
            from module.log_res import LogRes
            LogRes(ui.config).record('Cube', value)
            return {'Cube': value}
    except Exception as error:
        logger.warning(f'[资源刷新] 读取魔方失败: {type(error).__name__}')
    return {}


def _publish(config, values):
    """把读到的数值写回 Dashboard 并记账。"""
    if not values:
        return
    from module.log_res import LogRes

    log = LogRes(config)
    for name, value in values.items():
        try:
            log.record(name, value)
        except Exception as error:
            logger.warning(f'[资源刷新] 写入 {name} 失败: {type(error).__name__}')
    try:
        config.update()
    except Exception as error:
        logger.warning(f'[资源刷新] 保存配置失败: {type(error).__name__}')


# ==================== 入口 ====================

def refresh(script, resources: Iterable[str]):
    """执行一次全量扫描并写回。尽力而为，绝不上抛异常。

    Args:
        script: 具备 ``config``/``device`` 的模块对象（此处传 Reward 实例）。
        resources (Iterable[str]): 要读取的资源名集合。

    Returns:
        dict[str, int]: 成功读到的资源与数值。
    """
    config = script.config
    wanted = set(resources)
    total = len(wanted)
    logger.hr(f'Dashboard 资源刷新（{total} 项）', level=1)

    collected = {}
    sea_miles = record_sea_miles_enabled(config)
    for stage, names in _STAGES:
        active = wanted & set(names)
        # 大世界阶段：即使没选 ActionPoint 等三项，只要要记海里数也要进一次
        if not active and not (stage == 'os' and sea_miles):
            continue
        try:
            if stage == 'campaign':
                collected.update(_read_campaign(script))
                if 'Pt' in active:
                    collected.update(_read_pt(script))
            elif stage == 'os':
                if active:
                    collected.update(_read_os(script))
                elif sea_miles:
                    # 只记海里数：仍需进大世界页，复用现成导航
                    _enter_os_and_record_sea_miles(script)
            elif stage == 'shop':
                collected.update(_read_shop(script, active))
            elif stage == 'build':
                collected.update(_read_build(script))
        except Exception as error:
            logger.warning(f'[资源刷新] 阶段 {stage} 失败: {type(error).__name__}: {error}')

    # 回到主界面，保持与 Reward.run 结尾一致的收尾状态
    try:
        script.ui_goto_main()
    except Exception:
        pass

    collected = {k: v for k, v in collected.items() if k in wanted}
    _publish(config, collected)

    logger.info(f'[资源刷新] 成功 {len(collected)}/{total}：{sorted(collected)}')
    missing = sorted(wanted - set(collected))
    if missing:
        logger.warning(f'[资源刷新] 未读到：{missing}')
    return collected


def maybe_refresh(script, now=None):
    """Reward.run() 末尾调用的入口：按节流判断是否需要刷新。

    任何异常都会被吞掉并记录，绝不打断 Reward 的收菜流程。

    Args:
        script: ``module.reward.reward.Reward`` 实例。
        now (datetime.datetime | None): 基准时间，测试用。

    Returns:
        bool: 本次是否实际执行了扫描。
    """
    try:
        ok, reason = due(script.config, now=now)
        if not ok:
            logger.info(f'[资源刷新] 跳过：{reason}')
            return False
        _, resources, _ = settings(script.config)
        # 先记录时间再扫描，避免中途崩溃导致下次又来一遍
        mark_done(script.config, now=now)
        refresh(script, resources)
        return True
    except Exception as error:
        logger.warning(f'[资源刷新] 执行失败，已忽略: {type(error).__name__}: {error}')
        return False