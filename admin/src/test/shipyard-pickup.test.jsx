import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    postForm: vi.fn(),
    put: vi.fn(),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import { api } from '../api.js';
import ShipyardPickup from '../pages/ShipyardPickup.jsx';

function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  };
}

const requestDetail = {
  request: {
    request_id: 'pickup-1',
    warehouse_id: 1,
    request_no: 'PICK-20261028-001',
    source_type: 'DAILY_DEMAND',
    request_date: '2026-10-28',
    dispatch_no: null,
    source_file_name: '日配送需求.xlsx',
    status: 'PARSED',
    print_url: '/api/shipyard/pickup-requests/pickup-1/print',
    print_all_url: '/api/shipyard/pickup-requests/pickup-1/print?include_unavailable=1',
    file_url: '/api/shipyard/pickup-requests/pickup-1/file',
  },
  lines: [
    {
      line_id: 'line-ready',
      source_sheet: '日需求1',
      source_row_no: '2',
      request_date: '2026-10-28',
      engineering_no: 'H1615',
      section_no: '657',
      pallet_no: '657ME201EM',
      pallet_name: '657分段电舾件安装托盘表',
      material_name: null,
      item_spec: null,
      demand_quantity: null,
      demand_unit: '件',
      match_status: 'AVAILABLE',
      source_availability_status: 'AVAILABLE',
      delivery_status_raw: '前置仓',
      available_quantity: '6.000',
      match_details: {
        selected_ticket: {
          ticket_no: 'T-001',
          location_path_snapshot: 'A 区 / A1',
        },
      },
    },
    {
      line_id: 'line-missing',
      source_sheet: '日需求1',
      source_row_no: '3',
      request_date: '2026-10-28',
      engineering_no: 'H1617',
      section_no: '664',
      pallet_no: '664FE120121',
      pallet_name: '664分段电舾件安装托盘表',
      material_name: null,
      item_spec: null,
      demand_quantity: null,
      demand_unit: '件',
      match_status: 'WAREHOUSE_MISSING',
      source_availability_status: 'AVAILABLE',
      delivery_status_raw: '前置仓',
      available_quantity: '0',
      match_details: { candidates: [] },
    },
    {
      line_id: 'line-source-missing',
      source_sheet: '日需求1',
      source_row_no: '4',
      request_date: '2026-10-28',
      engineering_no: 'H1615',
      section_no: '658',
      pallet_no: '658ME201EM',
      pallet_name: '配送单标注前置仓未到',
      material_name: null,
      item_spec: null,
      demand_quantity: null,
      demand_unit: '件',
      match_status: 'SOURCE_UNAVAILABLE',
      source_availability_status: 'UNAVAILABLE',
      delivery_status_raw: '未到',
      available_quantity: '0',
      match_details: { candidates: [] },
    },
  ],
  summary: {
    line_count: 3,
    available_count: 1,
    warehouse_missing_count: 1,
    source_unavailable_count: 1,
    ambiguous_count: 0,
    date_from: '2026-10-28',
    date_to: '2026-10-28',
  },
};

describe('ShipyardPickup', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockImplementation((path) => {
      if (path === '/shipyard/pickup-requests/pickup-1') {
        return Promise.resolve(response(requestDetail));
      }
      if (path.includes('/shipyard/pickup-requests?')) {
        return Promise.resolve(response({
          requests: [
            {
              request_id: 'pickup-1',
              request_no: 'PICK-20261028-001',
              source_type: 'DAILY_DEMAND',
              request_date: '2026-10-28',
              dispatch_no: null,
              line_count: 3,
              available_count: 1,
              warehouse_missing_count: 1,
              source_unavailable_count: 1,
              ambiguous_count: 0,
              status: 'PARSED',
              created_at: '2026-10-28T08:00:00+08:00',
            },
          ],
        }));
      }
      return Promise.resolve(response({}));
    });
  });

  it('shows only actionable pickup lines by default', async () => {
    render(<ShipyardPickup />);
    expect(await screen.findByText('PICK-20261028-001')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看' }));

    expect(await screen.findByText('657ME201EM')).toBeInTheDocument();
    expect(screen.getByText('可提货')).toBeInTheDocument();
    expect(screen.getByText(/A 区 \/ A1/)).toBeInTheDocument();
    expect(screen.queryByText('664FE120121')).not.toBeInTheDocument();
    expect(screen.queryByText('658ME201EM')).not.toBeInTheDocument();
    expect(screen.queryByText('记录异常')).not.toBeInTheDocument();
  });

  it('shows retained unavailable and exception rows on demand', async () => {
    render(<ShipyardPickup />);
    await screen.findByText('PICK-20261028-001');
    fireEvent.click(screen.getByRole('button', { name: '查看' }));
    await screen.findByText('657ME201EM');

    fireEvent.click(screen.getByRole('button', { name: '查看本批无货/异常' }));

    expect(screen.getByText('664FE120121')).toBeInTheDocument();
    expect(screen.getByText('658ME201EM')).toBeInTheDocument();
    expect(screen.getByText('本批无货')).toBeInTheDocument();
    expect(screen.getByText('记录异常')).toBeInTheDocument();
    expect(screen.getByText('配送单：前置仓无货/未到')).toBeInTheDocument();
  });

  it('creates outbound tasks for all actionable lines without manual selection', async () => {
    vi.mocked(api.post).mockResolvedValue(response({
      ...requestDetail,
      created_task_links: [{ pickup_line_id: 'line-ready' }],
    }));
    render(<ShipyardPickup />);
    await screen.findByText('PICK-20261028-001');
    fireEvent.click(screen.getByRole('button', { name: '查看' }));
    await screen.findByText('657ME201EM');

    fireEvent.click(screen.getByRole('button', { name: '生成提货单（1）' }));
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/shipyard/pickup-requests/pickup-1/outbound',
        {},
      );
    });
    expect(await screen.findByText('已生成整票出库任务 1 张')).toBeInTheDocument();
  });
});
