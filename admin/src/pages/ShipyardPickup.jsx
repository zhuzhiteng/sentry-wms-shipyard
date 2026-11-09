import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';
import { useWarehouse } from '../warehouse.jsx';

const STATUS = {
  PARSED: '已导入',
  CONFIRMED: '已导入',
  VOID: '已作废',
  PENDING: '后台处理中',
  AVAILABLE: '可提货',
  AMBIGUOUS: '需核对',
  SOURCE_UNAVAILABLE: '本批无货',
  WAREHOUSE_MISSING: '记录异常',
  QUANTITY_MISMATCH: '数量需核对',
  OUTBOUND_PENDING: '已生成出库任务',
  OUTBOUND: '已出库',
  DUPLICATE_DEMAND: '重复需求',
};

const SOURCE = {
  DAILY_DEMAND: '日配送需求',
  ON_DEMAND: '按需需求',
  SPECIAL_DISPATCH: '急单/改单派工',
};

const SOURCE_AVAILABILITY = {
  AVAILABLE: '配送单：前置仓有货',
  UNAVAILABLE: '配送单：前置仓无货/未到',
  UNKNOWN: '配送单：未标注',
};

async function readJson(response, fallback) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.error || fallback);
  }
  return data;
}

function formatTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false });
}

function Status({ value }) {
  return (
    <span className={`shipyard-status shipyard-status-${String(value).toLowerCase()}`}>
      {STATUS[value] || value || '—'}
    </span>
  );
}

async function loadRequests(warehouseId) {
  return readJson(
    await api.get(`/shipyard/pickup-requests?warehouse_id=${warehouseId}`),
    '无法读取配送/提货需求',
  );
}

async function loadRequest(requestId) {
  return readJson(
    await api.get(`/shipyard/pickup-requests/${requestId}`),
    '无法读取配送/提货需求详情',
  );
}

export default function ShipyardPickup() {
  const { warehouseId } = useWarehouse();
  const fileRef = useRef(null);
  const specialFileRef = useRef(null);
  const detailRef = useRef(null);
  const [requests, setRequests] = useState([]);
  const [selected, setSelected] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [specialEvidence, setSpecialEvidence] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const [showAllLines, setShowAllLines] = useState(false);
  const [special, setSpecial] = useState({
    request_no: '',
    dispatch_no: '',
    request_date: new Date().toISOString().slice(0, 10),
    engineering_no: '',
    section_no: '',
    pallet_no: '',
    pallet_name: '',
    item_spec: '',
    quantity: '',
    unit: '件',
    keeper_name: '',
    remarks: '',
  });

  const selectedId = selected?.request?.request_id;

  const refreshList = useCallback(async () => {
    if (!warehouseId) return;
    const data = await loadRequests(warehouseId);
    setRequests(data.requests || []);
    if (selectedId) {
      const fresh = await loadRequest(selectedId);
      setSelected(fresh);
    }
  }, [selectedId, warehouseId]);

  useEffect(() => {
    if (!warehouseId) {
      return undefined;
    }
    let cancelled = false;
    loadRequests(warehouseId)
      .then((data) => {
        if (!cancelled) setRequests(data.requests || []);
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
    if (!selectedId) return;
    detailRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, [selectedId]);

  async function openRequest(requestId) {
    setBusy(`open-${requestId}`);
    setNotice(null);
    try {
      const data = await loadRequest(requestId);
      setSelected(data);
      setShowAllLines(false);
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function uploadDaily() {
    if (!warehouseId || !selectedFile) return;
    setBusy('upload');
    setNotice(null);
    try {
      const form = new FormData();
      form.append('warehouse_id', String(warehouseId));
      form.append('file', selectedFile);
      const data = await readJson(
        await api.postForm('/shipyard/pickup-requests/daily/preview', form),
        '配送需求识别失败',
      );
      setSelected(data);
      setShowAllLines(false);
      setSelectedFile(null);
      if (fileRef.current) fileRef.current.value = '';
      setNotice({
        type: 'success',
        message: `已导入 ${data.summary.line_count} 行：前置仓有货 ${data.summary.source_available_count || 0} 行，未到/无货 ${data.summary.source_unavailable_count || 0} 行；当前可生成提货任务 ${data.summary.available_count} 行`,
      });
      await refreshList();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function createOutbound() {
    if (!selectedId) return;
    setBusy('outbound');
    try {
      const data = await readJson(
        await api.post(`/shipyard/pickup-requests/${selectedId}/outbound`, {}),
        '生成出库任务失败',
      );
      setSelected(data);
      setNotice({
        type: 'success',
        message: `已生成整票出库任务 ${data.created_task_links?.length || 0} 张`,
      });
      await refreshList();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function createSpecial(event) {
    event.preventDefault();
    if (!warehouseId) return;
    setBusy('special');
    try {
      let data = await readJson(
        await api.post('/shipyard/pickup-requests/special', {
          warehouse_id: warehouseId,
          request_no: special.request_no.trim(),
          dispatch_no: special.dispatch_no.trim(),
          request_date: special.request_date,
          lines: [
            {
              line_no: '1',
              engineering_no: special.engineering_no.trim(),
              section_no: special.section_no.trim() || null,
              pallet_no: special.pallet_no.trim(),
              pallet_name: special.pallet_name.trim(),
              item_spec: special.item_spec.trim() || null,
              quantity: special.quantity,
              unit: special.unit.trim() || '件',
              keeper_name: special.keeper_name.trim() || null,
              remarks: special.remarks.trim() || null,
            },
          ],
        }),
        '登记急单/改单失败',
      );
      if (specialEvidence) {
        const evidenceForm = new FormData();
        evidenceForm.append('file', specialEvidence);
        data = await readJson(
          await api.postForm(
            `/shipyard/pickup-requests/${data.request.request_id}/evidence`,
            evidenceForm,
          ),
          '派工单原图上传失败',
        );
      }
      setSelected(data);
      setShowAllLines(false);
      setNotice({
        type: data.summary.warehouse_missing_count ? 'warning' : 'success',
        message: '急单/改单已登记，可提货行已整理好',
      });
      setSpecialEvidence(null);
      if (specialFileRef.current) specialFileRef.current.value = '';
      await refreshList();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  const actionableLines =
    selected?.lines?.filter(
      (line) => ['AVAILABLE', 'OUTBOUND_PENDING'].includes(line.match_status),
    ) || [];
  const visibleLines = showAllLines ? selected?.lines || [] : actionableLines;
  const exceptionCount =
    (selected?.summary.warehouse_missing_count || 0)
    + (selected?.summary.ambiguous_count || 0)
    + (selected?.summary.quantity_mismatch_count || 0);

  if (!warehouseId) {
    return (
      <div>
        <PageHeader title="前置仓提货" />
        <div className="settings-section">请先在顶部选择仓库。</div>
      </div>
    );
  }

  return (
    <div className="shipyard-page">
      <PageHeader title="前置仓提货">
        <button
          className="btn"
          onClick={() => refreshList().catch((error) => (
            setNotice({ type: 'error', message: error.message })
          ))}
          disabled={loading || !!busy}
        >
          {loading ? '读取中…' : '刷新'}
        </button>
      </PageHeader>

      {notice && (
        <div className={`shipyard-notice shipyard-notice-${notice.type}`} role="status">
          <span>{notice.message}</span>
          <button className="btn btn-sm" onClick={() => setNotice(null)}>关闭</button>
        </div>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>导入船厂需求</h3>
            <p className="settings-note">
              导入后，系统只把“到货情况=前置仓”的行列入本批可提货清单，
              并自动定位库存托位；未到或无货的行原样留存。
            </p>
          </div>
        </div>
        <div className="shipyard-actions">
          <input
            ref={fileRef}
            id="shipyard-pickup-file"
            type="file"
            accept=".xlsx"
            onChange={(event) => setSelectedFile(event.target.files?.[0] || null)}
          />
          <label className="btn" htmlFor="shipyard-pickup-file">
            {selectedFile ? selectedFile.name : '选择需求 Excel'}
          </label>
          <button
            className="btn btn-primary"
            onClick={uploadDaily}
            disabled={!selectedFile || busy === 'upload'}
          >
            {busy === 'upload' ? '导入中…' : '导入需求'}
          </button>
        </div>
      </section>

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>登记急单 / 改单派工单</h3>
            <p className="settings-note">
              用于船厂临时派工、改单或前置仓提货。登记时至少填写工程号与托盘号，
              系统会在后台自动定位库存，整票数量不一致时保留异常提醒。
            </p>
          </div>
        </div>
        <form className="shipyard-actions" onSubmit={createSpecial}>
          <input className="input" placeholder="需求号" value={special.request_no}
            onChange={(e) => setSpecial({ ...special, request_no: e.target.value })} required />
          <input className="input" placeholder="派工单号" value={special.dispatch_no}
            onChange={(e) => setSpecial({ ...special, dispatch_no: e.target.value })} required />
          <input className="input" type="date" value={special.request_date}
            onChange={(e) => setSpecial({ ...special, request_date: e.target.value })} required />
          <input className="input" placeholder="工程号 / 船号" value={special.engineering_no}
            onChange={(e) => setSpecial({ ...special, engineering_no: e.target.value })} required />
          <input className="input" placeholder="分段号" value={special.section_no}
            onChange={(e) => setSpecial({ ...special, section_no: e.target.value })} />
          <input className="input" placeholder="托盘号 / 制作图号" value={special.pallet_no}
            onChange={(e) => setSpecial({ ...special, pallet_no: e.target.value })} required />
          <input className="input" placeholder="托盘名称 / 品名" value={special.pallet_name}
            onChange={(e) => setSpecial({ ...special, pallet_name: e.target.value })} required />
          <input className="input" placeholder="物资规格" value={special.item_spec}
            onChange={(e) => setSpecial({ ...special, item_spec: e.target.value })} />
          <input className="input" type="number" min="0.001" step="0.001"
            placeholder="数量" value={special.quantity}
            onChange={(e) => setSpecial({ ...special, quantity: e.target.value })} required />
          <input className="input" placeholder="单位" value={special.unit}
            onChange={(e) => setSpecial({ ...special, unit: e.target.value })} />
          <input className="input" placeholder="保管员" value={special.keeper_name}
            onChange={(e) => setSpecial({ ...special, keeper_name: e.target.value })} />
          <input className="input" placeholder="备注 / 平台" value={special.remarks}
            onChange={(e) => setSpecial({ ...special, remarks: e.target.value })} />
          <input
            ref={specialFileRef}
            id="shipyard-special-evidence"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(event) => setSpecialEvidence(event.target.files?.[0] || null)}
          />
          <label className="btn" htmlFor="shipyard-special-evidence">
            {specialEvidence ? specialEvidence.name : '选择派工单原图'}
          </label>
          <button className="btn btn-primary" disabled={busy === 'special'}>
            {busy === 'special' ? '登记中…' : '登记提货需求'}
          </button>
        </form>
      </section>

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>提货需求记录</h3>
            <p className="settings-note">导入记录、待提货行和本批无货行都会保留，方便后续核对。</p>
          </div>
        </div>
        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>需求号</th>
                <th>来源</th>
                <th>需求日期</th>
                <th>派工单号</th>
                <th>行数</th>
                <th>可提 / 本批无货 / 异常</th>
                <th>状态</th>
                <th>创建</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {requests.map((item) => (
                <tr key={item.request_id}>
                  <td>{item.request_no}</td>
                  <td>{SOURCE[item.source_type] || item.source_type}</td>
                  <td>{item.request_date || '—'}</td>
                  <td>{item.dispatch_no || '—'}</td>
                  <td>{item.line_count}</td>
                  <td>
                    <span className="shipyard-success-text">{item.available_count}</span>
                    {' / '}
                    <span className={item.source_unavailable_count ? 'shipyard-error-text' : ''}>
                      {item.source_unavailable_count}
                    </span>
                    {' / '}
                    {(item.warehouse_missing_count || 0) + (item.ambiguous_count || 0)}
                  </td>
                  <td><Status value={item.status} /></td>
                  <td>{formatTime(item.created_at)}</td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => openRequest(item.request_id)}
                      disabled={busy === `open-${item.request_id}`}
                    >
                      查看
                    </button>
                  </td>
                </tr>
              ))}
              {!requests.length && (
                <tr><td colSpan="9" className="empty-state">还没有配送或提货需求</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selected && (
        <section className="settings-section" ref={detailRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>{selected.request.request_no} · {SOURCE[selected.request.source_type]}</h3>
              <p className="settings-note">
                {selected.request.source_file_name || '手工录入'}
                {' · '}
                需求日期 {selected.summary.date_from || '—'}
                {selected.summary.date_from !== selected.summary.date_to
                  ? ` 至 ${selected.summary.date_to}`
                  : ''}
                {' · '}
                待提货 {selected.summary.available_count}
                {' · '}
                本批无货 {selected.summary.source_unavailable_count || 0}
                {' · '}
                异常{' '}
                {exceptionCount}
              </p>
            </div>
            <div className="shipyard-actions">
              <button className="btn btn-primary" onClick={createOutbound}
                disabled={busy === 'outbound' || !selected.summary.available_count}>
                {busy === 'outbound'
                  ? '生成中…'
                  : `生成提货单（${selected.summary.available_count}）`}
              </button>
              <a className="btn" href={selected.request.print_url} target="_blank" rel="noreferrer">
                打印待提货单
              </a>
              <button
                className="btn"
                onClick={() => setShowAllLines((value) => !value)}
              >
                {showAllLines ? '只看可提货' : '查看本批无货/异常'}
              </button>
              <a className="btn" href={selected.request.print_all_url} target="_blank" rel="noreferrer">
                打印完整留存
              </a>
              {selected.request.file_url && (
                <a className="btn" href={selected.request.file_url}>下载原文件</a>
              )}
            </div>
          </div>

          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>日期 / 行</th>
                  <th>工程号 / 托盘</th>
                  <th>托盘名称</th>
                  <th>物料 / 规格</th>
                  <th>需求</th>
                  <th>库存托位 / 票号</th>
                  <th>配送单状态</th>
                  <th>处理结果</th>
                </tr>
              </thead>
              <tbody>
                {visibleLines.map((line) => (
                    <tr key={line.line_id}>
                      <td>
                        {line.request_date || '—'}
                        <div className="settings-note">
                          {line.source_sheet} · 行 {line.source_row_no}
                        </div>
                      </td>
                      <td>
                        <strong>{line.engineering_no}</strong>
                        <div>{line.pallet_no}</div>
                        <div className="settings-note">{line.section_no || '—'}</div>
                      </td>
                      <td>{line.pallet_name}</td>
                      <td>
                        {line.material_name || '—'}
                        <div className="settings-note">{line.item_spec || '—'}</div>
                      </td>
                      <td>
                        {line.demand_quantity
                          ? `${line.demand_quantity} ${line.demand_unit}`
                          : '整托'}
                      </td>
                      <td>
                        {line.match_details?.selected_ticket ? (
                          <>
                            {line.match_details.selected_ticket.ticket_no}
                            <div className="settings-note">
                              {line.match_details.selected_ticket.location_path_snapshot}
                              {' · '}
                              {line.available_quantity}
                            </div>
                          </>
                        ) : '—'}
                      </td>
                      <td>
                        {line.delivery_status_raw || line.prewarehouse_status_raw || line.pickup_status_raw || '—'}
                        <div className="settings-note">
                          {SOURCE_AVAILABILITY[line.source_availability_status] || '配送单：未标注'}
                        </div>
                        <div className="settings-note">{line.supplier_name || ''}</div>
                      </td>
                      <td>
                        <Status value={line.match_status} />
                        {line.outbound_order_id && (
                          <div className="settings-note">{line.outbound_order_id}</div>
                        )}
                      </td>
                    </tr>
                  ))}
                {!visibleLines.length && (
                  <tr>
                    <td colSpan="8" className="empty-state">
                      {showAllLines ? '没有需求记录' : '本批没有前置仓有货的可提行'}
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
