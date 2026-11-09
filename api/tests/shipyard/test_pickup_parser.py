from io import BytesIO

from openpyxl import Workbook

from services.shipyard.pickup_parser import parse_pickup_workbook


def _pickup_workbook():
    workbook = Workbook()
    daily = workbook.active
    daily.title = "日需求1"
    daily.append(
        [
            "提货时间",
            "工程号",
            "分段号",
            "托盘号",
            "托盘名称",
            "到货情况",
            "前置仓状态",
            "供应商",
            "需求时间",
        ]
    )
    daily.append(
        [
            "2026-10-27",
            "H1615",
            "657",
            "657ME201EM",
            "657分段电舾件安装托盘表",
            "前置仓",
            "29号发",
            "宝应帆洋",
            "2026-10-28 09:00:00~2026-10-28 09:30:00",
        ]
    )
    daily.append(
        [
            "2026-10-28",
            "H1616",
            "658",
            "658ME201EM",
            "658分段电舾件安装托盘表",
            "未到",
            "29号发",
            "宝应帆洋",
            "2026-10-28 09:30:00~2026-10-28 10:00:00",
        ]
    )
    on_demand = workbook.create_sheet("按需")
    on_demand.append(
        [
            "日期",
            "工程号",
            "分段号",
            "托盘号",
            "托盘名称",
            "状态",
            "工单状态",
            "申请人",
            "需求时间",
        ]
    )
    on_demand.append(
        [
            "2026-10-28",
            "H1617",
            "172",
            "172FE120121",
            "172分段电舾件安装托盘表",
            "未到",
            "镇江中环",
            "测试申请人",
            "2026-10-28 10:00:00~2026-10-28 10:30:00",
        ]
    )
    auxiliary = workbook.create_sheet("说明")
    auxiliary.append(["本页不导入"])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_pickup_workbook_imports_both_daily_and_on_demand_sheets():
    parsed = parse_pickup_workbook(_pickup_workbook(), source_file_name="需求.xlsx")

    assert parsed.metadata["line_count"] == 3
    assert parsed.metadata["daily_demand_count"] == 2
    assert parsed.metadata["on_demand_count"] == 1
    assert parsed.metadata["source_available_count"] == 1
    assert parsed.metadata["source_unavailable_count"] == 1
    assert parsed.metadata["source_availability_unknown_count"] == 1
    assert parsed.metadata["ignored_sheets"] == ["说明"]
    daily, unavailable, on_demand = parsed.lines
    assert daily.source_kind == "DAILY_DEMAND"
    assert daily.request_date.isoformat() == "2026-10-28"
    assert daily.delivery_status == "前置仓"
    assert daily.source_availability_status == "AVAILABLE"
    assert daily.prewarehouse_status == "29号发"
    assert daily.supplier_name == "宝应帆洋"
    assert daily.demand_start_at.isoformat() == "2026-10-28T09:00:00+08:00"
    assert unavailable.delivery_status == "未到"
    assert unavailable.source_availability_status == "UNAVAILABLE"
    assert on_demand.source_kind == "ON_DEMAND"
    assert on_demand.work_order_status_raw == "镇江中环"
    assert on_demand.source_availability_status == "UNKNOWN"
    assert on_demand.pallet_no == "172FE120121"
