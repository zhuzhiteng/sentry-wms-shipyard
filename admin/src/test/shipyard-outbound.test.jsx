import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    postForm: vi.fn(),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import { api } from '../api.js';
import ShipyardOutbound from '../pages/ShipyardOutbound.jsx';

function okJson(data) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
  });
}

const formalManifests = { manifests: [] };
const formalOrders = { orders: [] };

const formalDetail = {
  manifest: {
    manifest_id: 'formal-1',
    manifest_no: 'OB-20261027-001',
    factory_shipment_no: 'CX-20261027-001',
    source_file_name: '正式出库清单.xlsx',
    status: 'PARSED',
    metadata: { sheet_name: '出库清单' },
  },
  lines: [
    {
      line_id: 'formal-line-1',
      source_row_no: '1',
      ship_no: 'H9301',
      engineering_no: 'H9301',
      production_drawing_no: 'D-001',
      material_code: 'MAT-001',
      item_name: '船用支架',
      item_spec: 'S-100',
      demand_quantity: '4.000',
      allocated_quantity: '4.000',
      shortage_quantity: '0.000',
      unit: '件',
      match_status: 'READY',
      expected_tag_suffixes: ['1-1', '1-2'],
      tasks: [
        {
          ticket_id: 'ticket-1',
          ticket_no: 'T-001',
          available_quantity: '4.000',
          location_path_snapshot: 'A 区 / A1',
          tag_suffixes: ['1-1', '1-2'],
        },
      ],
    },
  ],
  order: null,
  validation: {
    blocking_errors: [],
    summary: {
      line_count: 1,
      task_count: 1,
      shortage_line_count: 0,
      total_shortage_quantity: '0.000',
    },
  },
};

describe('ShipyardOutbound', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockImplementation((path) => {
      if (path.endsWith('/formal-manifests/formal-1')) {
        return okJson(formalDetail);
      }
      if (path.includes('formal-manifests')) return okJson(formalManifests);
      if (path.includes('/outbound/orders')) return okJson(formalOrders);
      return okJson({});
    });
  });

  it('loads only the formal outbound queues', async () => {
    render(<ShipyardOutbound />);

    expect(await screen.findByText('还没有正式出库清单')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/formal-manifests?warehouse_id=1',
    );
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/orders?warehouse_id=1',
    );
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('uploads a formal outbound workbook and shows material identity', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve(formalDetail),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');

    fireEvent.change(container.querySelector('#shipyard-formal-outbound-file'), {
      target: {
        files: [
          new File(['xlsx'], 'formal.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));

    await waitFor(() => {
      expect(api.postForm).toHaveBeenCalledWith(
        '/shipyard/outbound/formal-manifests/preview',
        expect.any(FormData),
      );
    });
    expect(await screen.findByText('MAT-001')).toBeInTheDocument();
    expect(screen.getByText('A 区 / A1')).toBeInTheDocument();
  });

  it('keeps the generated order visible after confirmation', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve(formalDetail),
    });
    vi.mocked(api.post).mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        order: {
          outbound_order_id: 'formal-order-1',
          outbound_order_no: 'OUT-FORMAL-1',
          factory_shipment_no: 'CX-20261027-001',
          status: 'PENDING',
        },
        summary: {
          task_count: 1,
          pending_task_count: 1,
          outbound_task_count: 0,
          shortage_line_count: 0,
        },
        tasks: [],
      }),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');
    fireEvent.change(container.querySelector('#shipyard-formal-outbound-file'), {
      target: {
        files: [
          new File(['xlsx'], 'formal.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));
    fireEvent.click(await screen.findByRole('button', {
      name: '确认并生成正式出库单',
    }));

    expect(await screen.findByText('正式出库单 · OUT-FORMAL-1'))
      .toBeInTheDocument();
  });

  it('does not confirm a preview with blocking errors', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve({
        ...formalDetail,
        validation: {
          ...formalDetail.validation,
          blocking_errors: [{
            code: 'BINDING_CONFIGURATION_REQUIRED',
            message: '项目绑定配置缺失',
          }],
        },
      }),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');

    fireEvent.change(container.querySelector('#shipyard-formal-outbound-file'), {
      target: {
        files: [
          new File(['xlsx'], 'blocked.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));

    expect(await screen.findByText('BINDING_CONFIGURATION_REQUIRED'))
      .toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '确认并生成正式出库单' }),
    ).toBeDisabled();
    expect(screen.getByRole('link', { name: '去处理船号与区域绑定' }))
      .toHaveAttribute('href', '/shipyard-settings');
  });

  it('refreshes an open outbound order after field work changes state', async () => {
    let fieldWorkComplete = false;
    const pendingOrder = {
      outbound_order_id: 'order-live',
      outbound_order_no: 'OUT-LIVE',
      factory_shipment_no: 'CX-LIVE',
      line_count: 1,
      task_count: 1,
      pending_task_count: 1,
      total_required_quantity: '4.000',
      total_fulfilled_quantity: '0.000',
      status: 'PENDING',
    };
    const orderDetail = () => ({
      order: {
        outbound_order_id: 'order-live',
        outbound_order_no: 'OUT-LIVE',
        factory_shipment_no: 'CX-LIVE',
        status: fieldWorkComplete ? 'LOADED' : 'PENDING',
      },
      summary: {
        pending_task_count: fieldWorkComplete ? 0 : 1,
        outbound_task_count: fieldWorkComplete ? 1 : 0,
      },
      tasks: [{
        task_id: 'task-live',
        source_row_no: '1',
        material_code: 'MAT-LIVE',
        item_name: '现场物料',
        item_spec: 'S-1',
        ticket_no: 'TICKET-LIVE',
        location_path_snapshot: 'A 区 / A1',
        planned_quantity: '4.000',
        expected_tag_suffixes: ['1-1'],
        status: fieldWorkComplete ? 'OUTBOUND' : 'PENDING',
        confirmation: fieldWorkComplete
          ? {
            outbound_by: 'field-operator',
            outbound_at: '2026-10-27T12:00:00+08:00',
          }
          : null,
      }],
    });
    vi.mocked(api.get).mockImplementation((path) => {
      if (path.includes('/outbound/orders/order-live')) {
        return okJson(orderDetail());
      }
      if (path.includes('formal-manifests')) return okJson(formalManifests);
      if (path.includes('/outbound/orders')) {
        return okJson({ orders: [pendingOrder] });
      }
      return okJson({});
    });

    render(<ShipyardOutbound />);
    fireEvent.click(await screen.findByRole('button', { name: '查看任务' }));
    expect(await screen.findByText('正式出库单 · OUT-LIVE')).toBeInTheDocument();

    fieldWorkComplete = true;
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    expect(await screen.findByText(/field-operator/)).toBeInTheDocument();
  });
});
