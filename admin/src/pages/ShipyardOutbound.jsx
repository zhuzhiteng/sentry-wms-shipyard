import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';
import { useWarehouse } from '../warehouse.jsx';

const FORMAL_STATUS = {
  PARSED: '待确认',
  CONFIRMED: '已生成出库单',
  VOID: '已作废',
  PENDING: '待出库',
  IN_PROGRESS: '出库中',
  PARTIAL: '部分出库',
  OUTBOUND: '已出库',
  LOADED: '已装车',
  SHORTAGE: '库存不足',
  READY: '可生成',
  ALLOCATED: '已分配',
};

function formatTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false });
}

async function requireJson(response, fallback) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.error || fallback);
  }
  return data;
}

async function loadFormalWorkspace(warehouseId) {
  const responses = await Promise.all([
    api.get(`/shipyard/outbound/formal-manifests?warehouse_id=${warehouseId}`),
    api.get(`/shipyard/outbound/orders?warehouse_id=${warehouseId}`),
  ]);
  const [manifestData, orderData] = await Promise.all([
    requireJson(responses[0], '无法读取正式出库清单'),
    requireJson(responses[1], '无法读取正式出库单'),
  ]);
  return {
    formalManifests: manifestData.manifests || [],
    formalOrders: orderData.orders || [],
  };
}

function Status({ value }) {
  return (
    <span className={`shipyard-status shipyard-status-${String(value).toLowerCase()}`}>
      {FORMAL_STATUS[value] || value || '—'}
    </span>
  );
}

export default function ShipyardOutbound() {
  const { warehouseId } = useWarehouse();
  const formalFileRef = useRef(null);
  const formalPreviewRef = useRef(null);
  const formalOrderRef = useRef(null);
  const [formalManifests, setFormalManifests] = useState([]);
  const [formalOrders, setFormalOrders] = useState([]);
  const [selectedFormal, setSelectedFormal] = useState(null);
  const [selectedFormalOrder, setSelectedFormalOrder] = useState(null);
  const [selectedFormalFile, setSelectedFormalFile] = useState(null);
  const [formalDragging, setFormalDragging] = useState(false);
  const [factoryShipmentNo, setFactoryShipmentNo] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);

  const selectedFormalOrderId =
    selectedFormalOrder?.order?.outbound_order_id;
  const selectedFormalManifestId =
    selectedFormal?.manifest?.manifest_id;

  const refreshFormalWorkspace = useCallback(async () => {
    if (!warehouseId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const data = await loadFormalWorkspace(warehouseId);
      setFormalManifests(data.formalManifests);
      setFormalOrders(data.formalOrders);
      if (selectedFormalOrderId) {
        const response = await api.get(
          `/shipyard/outbound/orders/${selectedFormalOrderId}`,
        );
        setSelectedFormalOrder(
          await requireJson(response, '无法读取出库任务'),
        );
      }
      if (selectedFormalManifestId) {
        const response = await api.get(
          `/shipyard/outbound/formal-manifests/${selectedFormalManifestId}`,
        );
        setSelectedFormal(
          await requireJson(response, '无法读取正式出库清单'),
        );
      }
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setLoading(false);
    }
  }, [warehouseId, selectedFormalManifestId, selectedFormalOrderId]);

  useEffect(() => {
    if (!warehouseId) return undefined;
    let cancelled = false;
    loadFormalWorkspace(warehouseId)
      .then((data) => {
        if (cancelled) return;
        setFormalManifests(data.formalManifests);
        setFormalOrders(data.formalOrders);
      })
      .catch((error) => {
        if (!cancelled) setNotice({ type: 'error', message: error.message });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [warehouseId]);

  useEffect(() => {
    if (!selectedFormalManifestId) return;
    formalPreviewRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedFormalManifestId]);

  useEffect(() => {
    if (!selectedFormalOrderId) return;
    formalOrderRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedFormalOrderId]);

  function acceptFormalFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setNotice({
        type: 'error',
        message: '首期只接受船厂下发的 .xlsx 正式出库清单',
      });
      return;
    }
    setSelectedFormalFile(file);
    setNotice(null);
  }

  async function uploadFormalManifest() {
    if (!warehouseId || !selectedFormalFile) return;
    setBusy('formal-upload');
    setNotice(null);
    try {
      const form = new FormData();
      form.append('warehouse_id', String(warehouseId));
      form.append('file', selectedFormalFile);
      if (factoryShipmentNo.trim()) {
        form.append('factory_shipment_no', factoryShipmentNo.trim());
      }
      const response = await api.postForm(
        '/shipyard/outbound/formal-manifests/preview',
        form,
      );
      const data = await requireJson(response, '正式出库清单识别失败');
      setSelectedFormal(data);
      setSelectedFormalOrder(null);
      setSelectedFormalFile(null);
      setFactoryShipmentNo('');
      if (formalFileRef.current) formalFileRef.current.value = '';
      const summary = data.validation?.summary || {};
      setNotice({
        type: summary.shortage_line_count ? 'warning' : 'success',
        message: `已识别 ${summary.line_count || 0} 行，匹配 ${summary.task_count || 0} 张整票任务${summary.shortage_line_count ? `，${summary.shortage_line_count} 行库存不足` : ''}`,
      });
      await refreshFormalWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openFormalManifest(manifestId) {
    setBusy(`formal-${manifestId}`);
    setSelectedFormalOrder(null);
    try {
      const response = await api.get(
        `/shipyard/outbound/formal-manifests/${manifestId}`,
      );
      setSelectedFormal(
        await requireJson(response, '无法读取正式出库清单'),
      );
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function confirmFormalManifest() {
    if (!selectedFormal) return;
    if (selectedFormal.validation?.blocking_errors?.length) {
      setNotice({
        type: 'error',
        message: '仍有阻断项，请先处理清单中的绑定或字段错误',
      });
      return;
    }
    const manifestId = selectedFormal.manifest.manifest_id;
    setBusy('formal-confirm');
    try {
      const response = await api.post(
        `/shipyard/outbound/formal-manifests/${manifestId}/confirm`,
        {},
      );
      const order = await requireJson(response, '生成正式出库单失败');
      setSelectedFormalOrder(order);
      setNotice({
        type: order.summary?.shortage_line_count ? 'warning' : 'success',
        message: `正式出库单 ${order.order?.outbound_order_no || ''} 已生成，共 ${order.summary?.task_count || 0} 张整票任务`,
      });
      await refreshFormalWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openFormalOrder(orderId) {
    setBusy(`formal-order-${orderId}`);
    setSelectedFormal(null);
    try {
      const response = await api.get(`/shipyard/outbound/orders/${orderId}`);
      setSelectedFormalOrder(
        await requireJson(response, '无法读取出库任务'),
      );
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  if (!warehouseId) {
    return (
      <div>
        <PageHeader title="船厂正式出库" />
        <div className="settings-section">请先在顶部选择仓库。</div>
      </div>
    );
  }

  return (
    <div className="shipyard-page">
      <PageHeader title="船厂正式出库">
        <button
          className="btn"
          onClick={refreshFormalWorkspace}
          disabled={loading || !!busy}
        >
          {loading ? '读取中…' : '刷新'}
        </button>
      </PageHeader>

      {notice && (
        <div
          className={`shipyard-notice shipyard-notice-${notice.type}`}
          role="status"
        >
          <span>{notice.message}</span>
          <button className="btn btn-sm" onClick={() => setNotice(null)}>
            关闭
          </button>
        </div>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>导入正式出库清单</h3>
            <p className="settings-note">
              按物料编码、品名、规格、船号/工程号/制作图和整票托盘匹配库存。
              物料编码或品名不一致时只显示缺口，不按名称猜测扣库。
            </p>
          </div>
        </div>
        <div className="shipyard-actions shipyard-formal-meta">
          <label>
            <span>船厂出货单号（文件未提供时填写）</span>
            <input
              className="input"
              value={factoryShipmentNo}
              onChange={(event) => setFactoryShipmentNo(event.target.value)}
              placeholder="例如 CX-20261027-001"
            />
          </label>
        </div>
        <div
          className={`shipyard-dropzone${formalDragging ? ' is-dragging' : ''}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setFormalDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            event.preventDefault();
            setFormalDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setFormalDragging(false);
            acceptFormalFile(event.dataTransfer.files?.[0]);
          }}
        >
          <input
            ref={formalFileRef}
            id="shipyard-formal-outbound-file"
            type="file"
            accept=".xlsx"
            onChange={(event) => acceptFormalFile(event.target.files?.[0])}
          />
          <label htmlFor="shipyard-formal-outbound-file">
            <strong>
              {selectedFormalFile
                ? selectedFormalFile.name
                : '将船厂正式出库清单拖到这里，或点击选择文件'}
            </strong>
            <span>固定模板 .xlsx；物料编码和品名为强制字段</span>
          </label>
          <button
            className="btn btn-primary"
            onClick={uploadFormalManifest}
            disabled={!selectedFormalFile || busy === 'formal-upload'}
          >
            {busy === 'formal-upload' ? '识别中…' : '识别并匹配库存'}
          </button>
        </div>

        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>导入号</th>
                <th>船厂出货单号</th>
                <th>来源文件</th>
                <th>行数 / 数量</th>
                <th>状态</th>
                <th>出库单</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {formalManifests.map((item) => (
                <tr key={item.manifest_id}>
                  <td>{item.manifest_no}</td>
                  <td>{item.factory_shipment_no}</td>
                  <td>{item.source_file_name}</td>
                  <td>{item.line_count} / {item.total_quantity}</td>
                  <td>
                    <Status value={item.outbound_order_status || item.status} />
                  </td>
                  <td>{item.outbound_order_no || '—'}</td>
                  <td className="shipyard-actions">
                    <button
                      className="btn btn-sm"
                      onClick={() => openFormalManifest(item.manifest_id)}
                      disabled={busy === `formal-${item.manifest_id}`}
                    >
                      查看
                    </button>
                    {item.outbound_order_id && (
                      <button
                        className="btn btn-sm"
                        onClick={() => openFormalOrder(item.outbound_order_id)}
                      >
                        出库任务
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {!formalManifests.length && (
                <tr>
                  <td colSpan="7" className="empty-state">
                    还没有正式出库清单
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedFormal && (
        <section className="settings-section" ref={formalPreviewRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>
                正式出库清单预览 · {selectedFormal.manifest.manifest_no}
              </h3>
              <p className="settings-note">
                船厂出货单号 {selectedFormal.manifest.factory_shipment_no}
                {' · '}
                可匹配 {selectedFormal.validation.summary.task_count} 张整票任务
                {' · '}
                缺口 {selectedFormal.validation.summary.total_shortage_quantity}
              </p>
            </div>
            <div className="shipyard-actions">
              {selectedFormal.manifest.status === 'PARSED' && (
                <button
                  className="btn btn-primary"
                  onClick={confirmFormalManifest}
                  disabled={
                    busy === 'formal-confirm'
                    || !!selectedFormal.validation.blocking_errors?.length
                  }
                >
                  {busy === 'formal-confirm'
                    ? '生成中…'
                    : '确认并生成正式出库单'}
                </button>
              )}
              <a
                className="btn"
                href={`/api/shipyard/outbound/formal-manifests/${selectedFormal.manifest.manifest_id}/file`}
              >
                下载原文件
              </a>
            </div>
          </div>

          {!!selectedFormal.validation.blocking_errors?.length && (
            <div className="shipyard-warning-list">
              {selectedFormal.validation.blocking_errors.map((error) => (
                <div key={error.code}>
                  <strong>{error.code}</strong> {error.message}
                </div>
              ))}
              <a className="btn btn-sm" href="/shipyard-settings">
                去处理船号与区域绑定
              </a>
            </div>
          )}

          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>清单行</th>
                  <th>物料编码 / 品名</th>
                  <th>船号 / 制作图</th>
                  <th>需求</th>
                  <th>已分配</th>
                  <th>缺口</th>
                  <th>匹配票货</th>
                  <th>区域 / 托盘后缀</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {selectedFormal.lines.map((line) => (
                  <tr key={line.line_id}>
                    <td>{line.source_row_no}</td>
                    <td>
                      <strong>{line.material_code}</strong>
                      <div>{line.item_name}</div>
                      <div className="settings-note">
                        {line.item_spec || '—'}
                      </div>
                    </td>
                    <td>
                      {line.ship_no}
                      <div className="settings-note">
                        {line.production_drawing_no}
                      </div>
                    </td>
                    <td>{line.demand_quantity} {line.unit}</td>
                    <td>{line.allocated_quantity}</td>
                    <td
                      className={
                        Number(line.shortage_quantity) > 0
                          ? 'shipyard-error-text'
                          : ''
                      }
                    >
                      {line.shortage_quantity}
                    </td>
                    <td>
                      {line.tasks.length
                        ? line.tasks.map((task) => (
                          <div key={task.ticket_id}>
                            {task.ticket_no} · {task.available_quantity}
                          </div>
                        ))
                        : '—'}
                    </td>
                    <td>
                      {line.tasks.length
                        ? line.tasks.map((task) => (
                          <div key={task.ticket_id}>
                            {task.location_path_snapshot}
                            <div className="settings-note">
                              {task.tag_suffixes.join('、')}
                            </div>
                          </div>
                        ))
                        : '—'}
                    </td>
                    <td><Status value={line.match_status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>正式出库任务与装车</h3>
            <p className="settings-note">
              每张任务对应一张不可拆的整票托组；现场小程序拍齐全部吊牌后才扣库，
              全部出库且无缺口后才能装车。
            </p>
          </div>
        </div>
        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>出库单号</th>
                <th>船厂出货单号</th>
                <th>行数</th>
                <th>整票任务</th>
                <th>待出库</th>
                <th>应出 / 已出</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {formalOrders.map((item) => (
                <tr key={item.outbound_order_id}>
                  <td>{item.outbound_order_no}</td>
                  <td>{item.factory_shipment_no}</td>
                  <td>{item.line_count}</td>
                  <td>{item.task_count}</td>
                  <td>{item.pending_task_count}</td>
                  <td>
                    {item.total_required_quantity}
                    {' / '}
                    {item.total_fulfilled_quantity}
                  </td>
                  <td><Status value={item.status} /></td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => openFormalOrder(item.outbound_order_id)}
                      disabled={
                        busy === `formal-order-${item.outbound_order_id}`
                      }
                    >
                      查看任务
                    </button>
                  </td>
                </tr>
              ))}
              {!formalOrders.length && (
                <tr>
                  <td colSpan="8" className="empty-state">
                    还没有正式出库单
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedFormalOrder && (
        <section className="settings-section" ref={formalOrderRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>
                正式出库单 · {selectedFormalOrder.order.outbound_order_no}
              </h3>
              <p className="settings-note">
                {selectedFormalOrder.order.factory_shipment_no}
                {' · '}
                待出库 {selectedFormalOrder.summary.pending_task_count} 票
                {' · '}
                已出库 {selectedFormalOrder.summary.outbound_task_count} 票
                {' · '}
                <Status value={selectedFormalOrder.order.status} />
              </p>
            </div>
          </div>
          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>清单行</th>
                  <th>物料</th>
                  <th>票货 / 区域</th>
                  <th>整票数量</th>
                  <th>托盘后缀</th>
                  <th>出库人 / 时间</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {selectedFormalOrder.tasks.map((task) => (
                  <tr key={task.task_id}>
                    <td>{task.source_row_no}</td>
                    <td>
                      <strong>{task.material_code}</strong>
                      <div>{task.item_name}</div>
                      <div className="settings-note">
                        {task.item_spec || '—'}
                      </div>
                    </td>
                    <td>
                      {task.ticket_no}
                      <div className="settings-note">
                        {task.location_path_snapshot}
                      </div>
                    </td>
                    <td>{task.planned_quantity}</td>
                    <td>{task.expected_tag_suffixes.join('、')}</td>
                    <td>
                      {task.confirmation
                        ? `${task.confirmation.outbound_by} · ${formatTime(task.confirmation.outbound_at)}`
                        : '—'}
                    </td>
                    <td><Status value={task.status} /></td>
                  </tr>
                ))}
                {!selectedFormalOrder.tasks.length && (
                  <tr>
                    <td colSpan="7" className="empty-state">
                      该出库单没有可执行整票任务，请处理库存缺口
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
