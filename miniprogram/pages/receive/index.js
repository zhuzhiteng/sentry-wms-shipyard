const { buildReceiptConfirmation } = require('../../utils/payload');
const {
  remainingQuantity,
  scanProgress,
  validateReceiptDraft,
} = require('../../utils/receipt');
const { request, upload } = require('../../utils/request');
const { normalizeSuffix } = require('../../utils/pallet');
const { requireSession } = require('../../utils/session');
const {
  clearDraft,
  clearSubmissionKey,
  getOrCreateSubmissionKey,
  isAmbiguousSubmissionError,
  readDraft,
  writeDraft,
} = require('../../utils/workflow');

const STATUS_LABELS = {
  PARSED: '待确认',
  CONFIRMED: '待收货',
  PARTIAL: '部分到货',
  CLOSED: '已到齐',
  PENDING: '待收货',
  RECEIVED: '已到货',
  EXPECTED: '待收货',
  IN_STOCK: '已入库',
  OUTBOUND: '已出库',
  EXCEPTION: '异常',
};

const DELIVERY_LABELS = {
  PENDING: '本批未到',
  DEFERRED: '顺延下批',
  PARTIAL: '部分到货',
  RECEIVED: '已到齐',
};

function emptyArrivalSlipForm() {
  return {
    slip_no: '',
    delivery_note_no: '',
    supplier_name: '',
    engineering_no: '',
    purchase_order_no: '',
  };
}

function deliveryClass(status) {
  if (status === 'RECEIVED') return 'chip-success';
  if (status === 'PARTIAL' || status === 'DEFERRED') return 'chip-warning';
  return '';
}

function statusClass(status) {
  if (['CLOSED', 'RECEIVED', 'IN_STOCK'].includes(status)) return 'chip-success';
  if (['PARTIAL', 'EXCEPTION'].includes(status)) return 'chip-warning';
  return '';
}

function numberText(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) ? String(number) : number.toFixed(3);
}

function receiptDraftScope(session, warehouseId) {
  return `receipt:${session.user.username}:${warehouseId}`;
}

function receiptSubmissionScope(session, ticketId) {
  return `receipt-submit:${session.user.username}:${ticketId}`;
}

Page({
  data: {
    session: null,
    warehouseId: 1,
    manifests: [],
    selectedManifest: null,
    selectedManifestId: '',
    selectedTicket: null,
    scans: [],
    quantity: '',
    differenceReason: '',
    showDifferenceReason: false,
    editQuantity: false,
    busy: false,
    submitUncertain: false,
    loading: false,
    cameraVisible: false,
    pendingSuffix: '',
    nextSuffix: '',
    palletProgressText: '',
    remainingQuantityText: '',
    arrivalSlipPhotoPath: '',
    arrivalSlipExpanded: false,
    arrivalSlipMessage: '',
    arrivalSlipSavedCount: 0,
    editingArrivalSlipId: '',
    arrivalSlipForm: emptyArrivalSlipForm(),
  },

  onLoad() {
    const session = requireSession();
    if (!session) return;
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.setData({
      session,
      warehouseId: session.warehouseId,
    });
    this.draftScope = receiptDraftScope(session, session.warehouseId);
    this.pendingDraft = readDraft(this.draftScope);
    this.loadManifests();
  },

  onPullDownRefresh() {
    if (this.data.busy || this.submissionPending) {
      wx.stopPullDownRefresh();
      wx.showToast({ title: '正在提交，请等待结果', icon: 'none' });
      return;
    }
    this.loadManifests().finally(() => wx.stopPullDownRefresh());
  },

  onHide() {
    this.persistDraft();
  },

  onUnload() {
    this.persistDraft();
  },

  async loadManifests() {
    this.setData({ loading: true });
    try {
      const data = await request({
        path: `/shipyard/inbound/manifests?warehouse_id=${this.data.warehouseId}`,
      });
      const manifests = (data.manifests || []).map((item) => ({
        ...item,
        status_label: STATUS_LABELS[item.status] || '状态待确认',
        status_class: statusClass(item.status),
      }));
      this.setData({ manifests });
      if (this.pendingDraft) {
        const draft = this.pendingDraft;
        this.pendingDraft = null;
        await this.restoreDraft(draft);
        return;
      }
      if (this.data.selectedManifest) {
        await this.openManifest(this.data.selectedManifest.manifest.manifest_id);
      } else {
        const receivable = manifests.filter((item) => (
          ['CONFIRMED', 'PARTIAL'].includes(item.status)
          || Number(item.pending_ticket_count || 0) > 0
        ));
        if (receivable.length === 1) {
          await this.openManifest(receivable[0].manifest_id);
        }
      }
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async restoreDraft(draft) {
    if (!draft?.manifest_id || !draft?.ticket_id) {
      clearDraft(this.draftScope);
      return;
    }
    const opened = await this.openManifest(draft.manifest_id);
    if (!opened) {
      this.pendingDraft = draft;
      return;
    }
    const ticket = (this.data.selectedManifest?.tickets || []).find(
      (item) => item.ticket_id === draft.ticket_id,
    );
    if (!ticket || !['EXPECTED', 'PARTIAL'].includes(ticket.status)) {
      clearDraft(this.draftScope);
      clearSubmissionKey(
        receiptSubmissionScope(this.data.session, draft.ticket_id),
      );
      wx.showModal({
        title: '上次操作已处理',
        content: '货物状态已经变化，草稿已清理。请按最新货物状态操作。',
        showCancel: false,
      });
      return;
    }
    const scans = Array.isArray(draft.scans) ? draft.scans : [];
    const progress = scanProgress(ticket, scans);
    const remaining = numberText(remainingQuantity(ticket));
    const quantity = draft.quantity || remaining;
    const showDifferenceReason = Number(quantity) < Number(remaining);
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.setData({
      selectedTicket: ticket,
      scans,
      quantity,
      differenceReason: showDifferenceReason
        ? (draft.difference_reason || '')
        : '',
      showDifferenceReason,
      editQuantity: showDifferenceReason,
      submitUncertain: Boolean(
        draft.submit_uncertain || draft.submission_pending,
      ),
      palletProgressText: progress.text,
      remainingQuantityText: remaining,
      nextSuffix: progress.remaining === null
        ? '可继续拍摄下一托'
        : progress.remaining > 0
          ? `还差 ${progress.remaining} 托`
          : '',
    });
    wx.showToast({ title: '已恢复上次收货草稿', icon: 'none' });
  },

  async openManifest(manifestId) {
    this.setData({ busy: true });
    try {
      const manifestChanged = this.data.selectedManifestId !== manifestId;
      const data = await request({
        path: `/shipyard/inbound/manifests/${manifestId}`,
      });
      const tickets = (data.tickets || []).map((ticket) => ({
        ...ticket,
        status_label: STATUS_LABELS[ticket.status] || '状态待确认',
        status_class: statusClass(ticket.status),
        delivery_label: DELIVERY_LABELS[ticket.delivery_status] || '本批未到',
        delivery_class: deliveryClass(ticket.delivery_status),
        deferred_text: ticket.deferred_to_date
          ? `顺延至 ${ticket.deferred_to_date}：${ticket.deferred_reason || ''}`
          : '',
        short_quantity_text: numberText(ticket.short_quantity),
        remaining_quantity_text: numberText(remainingQuantity(ticket)),
        pallet_progress_text: scanProgress(ticket, []).text,
        can_receive: ['EXPECTED', 'PARTIAL'].includes(ticket.status),
        can_defer: ['EXPECTED', 'PARTIAL'].includes(ticket.status)
          && remainingQuantity(ticket) > 0,
      }));
      const selectedManifest = { ...data, tickets };
      const supplierName = data.manifest?.metadata?.manufacturer_name
        || (manifestChanged ? '' : this.data.arrivalSlipForm.supplier_name)
        || '';
      this.setData({
        selectedManifest,
        selectedManifestId: manifestId,
        selectedTicket: null,
        scans: [],
        arrivalSlipSavedCount: (data.arrival_slips || []).length,
        ...(manifestChanged
          ? {
            arrivalSlipPhotoPath: '',
            arrivalSlipExpanded: false,
            arrivalSlipMessage: '',
            editingArrivalSlipId: '',
          }
          : {}),
        arrivalSlipForm: manifestChanged
          ? { ...emptyArrivalSlipForm(), supplier_name: supplierName }
          : {
            ...this.data.arrivalSlipForm,
            supplier_name: supplierName || this.data.arrivalSlipForm.supplier_name,
          },
      });
      return true;
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
      return false;
    } finally {
      this.setData({ busy: false });
    }
  },

  onManifestTap(event) {
    if (this.data.busy || this.submissionPending) {
      wx.showToast({ title: '正在提交，请等待结果', icon: 'none' });
      return;
    }
    this.openManifest(event.currentTarget.dataset.id);
  },

  startTicket(event) {
    this.beginTicket(event.currentTarget.dataset.id);
  },

  beginTicket(ticketId) {
    if (this.data.busy || this.submissionPending) {
      wx.showToast({ title: '正在提交，请等待结果', icon: 'none' });
      return false;
    }
    if (this.data.submitUncertain) {
      wx.showToast({ title: '上次提交结果未确认，请先重试', icon: 'none' });
      return false;
    }
    const ticket = this.data.selectedManifest.tickets.find(
      (item) => item.ticket_id === ticketId,
    );
    if (!ticket) return false;
    if (!['EXPECTED', 'PARTIAL'].includes(ticket.status)) {
      wx.showToast({ title: '该票当前不能收货', icon: 'none' });
      return false;
    }
    const remaining = Math.max(
      remainingQuantity(ticket),
      0,
    );
    const progress = scanProgress(ticket, []);
    clearSubmissionKey(
      receiptSubmissionScope(this.data.session, ticket.ticket_id),
    );
    if (this.data.selectedTicket) {
      clearSubmissionKey(
        receiptSubmissionScope(this.data.session, this.data.selectedTicket.ticket_id),
      );
    }
    clearDraft(this.draftScope);
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.setData({
      selectedTicket: ticket,
      scans: [],
      quantity: numberText(remaining),
      differenceReason: '',
      showDifferenceReason: false,
      editQuantity: false,
      submitUncertain: false,
      palletProgressText: progress.text,
      remainingQuantityText: numberText(remaining),
    });
    this.persistDraft();
    const scrollToWorkbench = () => wx.pageScrollTo({
      selector: '#ticket-workbench',
      duration: 250,
    });
    if (typeof wx.nextTick === 'function') {
      wx.nextTick(scrollToWorkbench);
    } else {
      scrollToWorkbench();
    }
    return true;
  },

  nextReceivableTicketId(afterTicketId) {
    const tickets = this.data.selectedManifest?.tickets || [];
    const afterIndex = tickets.findIndex(
      (ticket) => ticket.ticket_id === afterTicketId,
    );
    const ordered = afterIndex >= 0
      ? [...tickets.slice(afterIndex + 1), ...tickets.slice(0, afterIndex)]
      : tickets;
    return ordered.find(
      (ticket) => ticket.can_receive && ticket.ticket_id !== afterTicketId,
    )?.ticket_id || '';
  },

  async deferTicket(event) {
    if (this.data.busy || this.submissionPending) return;
    const ticketId = event.currentTarget.dataset.id;
    const ticket = this.data.selectedManifest?.tickets?.find(
      (item) => item.ticket_id === ticketId,
    );
    if (!ticket?.can_defer) return;
    const dateResult = await new Promise((resolve) => {
      wx.showModal({
        title: '顺延到下一批',
        content: '请输入下一批预计到货日期。',
        editable: true,
        placeholderText: 'YYYY-MM-DD',
        success: resolve,
        fail: () => resolve({ confirm: false }),
      });
    });
    if (!dateResult.confirm) return;
    const nextBatchDate = String(dateResult.content || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(nextBatchDate)) {
      wx.showToast({ title: '日期格式应为 YYYY-MM-DD', icon: 'none' });
      return;
    }
    const reasonResult = await new Promise((resolve) => {
      wx.showModal({
        title: '顺延原因',
        content: '例如：车辆装不下，随下一批送达',
        editable: true,
        placeholderText: '本批未装原因',
        success: resolve,
        fail: () => resolve({ confirm: false }),
      });
    });
    if (!reasonResult.confirm) return;
    const reason = String(reasonResult.content || '').trim();
    if (!reason) {
      wx.showToast({ title: '请填写顺延原因', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await request({
        path: `/shipyard/inbound/tickets/${ticketId}/deferral`,
        method: 'POST',
        data: {
          next_batch_date: nextBatchDate,
          reason,
        },
      });
      wx.showToast({ title: '已标记顺延下批', icon: 'success' });
      await this.openManifest(this.data.selectedManifestId);
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onArrivalSlipInput(event) {
    const field = event.currentTarget.dataset.field;
    this.setData({
      arrivalSlipForm: {
        ...this.data.arrivalSlipForm,
        [field]: event.detail.value,
      },
    });
  },

  toggleArrivalSlipDetails() {
    if (this.data.busy) return;
    this.setData({
      arrivalSlipExpanded: !this.data.arrivalSlipExpanded,
    });
  },

  editSavedArrivalSlip(event) {
    const arrivalSlipId = event.currentTarget.dataset.id;
    const slip = (this.data.selectedManifest?.arrival_slips || []).find(
      (item) => item.arrival_slip_id === arrivalSlipId,
    );
    if (!slip) return;
    this.setData({
      editingArrivalSlipId: arrivalSlipId,
      arrivalSlipPhotoPath: '',
      arrivalSlipExpanded: true,
      arrivalSlipMessage: '请核对并保存修改。',
      arrivalSlipForm: {
        slip_no: slip.slip_no || '',
        delivery_note_no: slip.delivery_note_no || '',
        supplier_name: slip.supplier_name || '',
        engineering_no: slip.engineering_no || '',
        purchase_order_no: slip.purchase_order_no || '',
      },
    });
  },

  async persistArrivalSlip(photoPath, form) {
    return upload({
      path: `/shipyard/inbound/manifests/${this.data.selectedManifestId}/arrival-slips`,
      filePath: photoPath,
      timeout: 45000,
      formData: {
        slip_no: form.slip_no.trim(),
        delivery_note_no: form.delivery_note_no.trim(),
        supplier_name: form.supplier_name.trim(),
        engineering_no: String(form.engineering_no || '').trim(),
        purchase_order_no: String(form.purchase_order_no || '').trim(),
        factory_copy_count: '2',
        shipyard_copy_count: '2',
        warehouse_copy_retained: 'true',
      },
    });
  },

  clearArrivalSlipDraft(form) {
    this.setData({
      arrivalSlipPhotoPath: '',
      arrivalSlipExpanded: false,
      arrivalSlipMessage: '',
      editingArrivalSlipId: '',
      arrivalSlipForm: {
        ...form,
        slip_no: '',
        delivery_note_no: '',
      },
    });
  },

  async captureArrivalSlip() {
    if (!this.data.selectedManifestId || this.data.busy) return;
    if (this.data.selectedTicket) {
      wx.showToast({ title: '请先完成当前货物收货', icon: 'none' });
      return;
    }
    this.setData({ editingArrivalSlipId: '' });
    this.setData({ busy: true });
    try {
      const media = await wx.chooseMedia({
        count: 1,
        mediaType: ['image'],
        sourceType: ['camera'],
        sizeType: ['compressed'],
      });
      const photoPath = media.tempFiles[0].tempFilePath;
      this.setData({ arrivalSlipPhotoPath: photoPath });
      const recognized = await upload({
        path: `/shipyard/inbound/manifests/${this.data.selectedManifestId}/arrival-slips/recognize`,
        filePath: photoPath,
        timeout: 45000,
      });
      const fields = recognized.fields || {};
      const form = {
        slip_no: String(fields.slip_no || this.data.arrivalSlipForm.slip_no || ''),
        delivery_note_no: String(
          fields.delivery_note_no
          || this.data.arrivalSlipForm.delivery_note_no
          || '',
        ),
        supplier_name: String(
          fields.supplier_name
          || this.data.arrivalSlipForm.supplier_name
          || '',
        ),
        engineering_no: String(
          fields.engineering_no
          || this.data.arrivalSlipForm.engineering_no
          || '',
        ),
        purchase_order_no: String(
          fields.purchase_order_no
          || this.data.arrivalSlipForm.purchase_order_no
          || '',
        ),
      };
      const missingLabels = [
        ['slip_no', '验收单号'],
        ['delivery_note_no', '送货单号'],
        ['supplier_name', '供应商'],
      ]
        .filter(([field]) => !String(form[field] || '').trim())
        .map(([, label]) => label);
      this.setData({ arrivalSlipForm: form });
      if (!missingLabels.length) {
        await this.persistArrivalSlip(photoPath, form);
        this.clearArrivalSlipDraft(form);
        wx.showToast({ title: '四联验收单已保存', icon: 'success' });
        const refreshed = await this.openManifest(this.data.selectedManifestId);
        if (!refreshed) {
          wx.showToast({
            title: '已保存，刷新失败，请下拉刷新',
            icon: 'none',
          });
        }
        return;
      }
      this.setData({
        arrivalSlipPhotoPath: photoPath,
        arrivalSlipForm: form,
        arrivalSlipExpanded: missingLabels.length > 0,
        arrivalSlipMessage: missingLabels.length
          ? `还缺${missingLabels.join('、')}，请补充。`
          : '信息已自动读出，请核对验收单。',
      });
      wx.showToast({
        title: missingLabels.length ? '请补充验收单信息' : '验收单信息已读出',
        icon: 'none',
      });
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.setData({
        arrivalSlipExpanded: true,
        arrivalSlipMessage: '照片已保留，请核对或补充验收单信息。',
      });
      wx.showToast({ title: '请补充验收单信息', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  async saveArrivalSlip() {
    if (
      (!this.data.arrivalSlipPhotoPath && !this.data.editingArrivalSlipId)
      || this.data.busy
    ) return;
    const form = this.data.arrivalSlipForm;
    const editingArrivalSlipId = this.data.editingArrivalSlipId;
    const missing = [
      ['slip_no', '验收单号'],
      ['delivery_note_no', '送货单号'],
      ['supplier_name', '供应商'],
    ].filter(([field]) => !String(form[field] || '').trim());
    if (missing.length) {
      this.setData({ arrivalSlipExpanded: true });
      wx.showToast({
        title: `请填写${missing.map(([, label]) => label).join('、')}`,
        icon: 'none',
      });
      return;
    }
    this.setData({ busy: true });
    try {
      if (editingArrivalSlipId) {
        await request({
          path: `/shipyard/inbound/arrival-slips/${editingArrivalSlipId}`,
          method: 'PUT',
          data: {
            slip_no: form.slip_no.trim(),
            delivery_note_no: form.delivery_note_no.trim(),
            supplier_name: form.supplier_name.trim(),
            engineering_no: String(form.engineering_no || '').trim() || null,
            purchase_order_no: String(form.purchase_order_no || '').trim() || null,
          },
        });
      } else {
        await this.persistArrivalSlip(this.data.arrivalSlipPhotoPath, form);
      }
      this.clearArrivalSlipDraft(form);
      wx.showToast({
        title: editingArrivalSlipId ? '修改已保存' : '四联验收单已留存',
        icon: 'success',
      });
      const refreshed = await this.openManifest(this.data.selectedManifestId);
      if (!refreshed) {
        wx.showToast({
          title: '已保存，刷新失败，请下拉刷新',
          icon: 'none',
        });
      }
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  async captureLabel() {
    const ticket = this.data.selectedTicket;
    if (!ticket || this.data.busy || this.data.submitUncertain) return;
    this.setData({ busy: true });
    try {
      const media = await wx.chooseMedia({
        count: 1,
        mediaType: ['image'],
        sourceType: ['camera'],
        sizeType: ['compressed'],
      });
      const filePath = media.tempFiles[0].tempFilePath;
      const result = await upload({
        path: `/shipyard/inbound/tickets/${ticket.ticket_id}/label-recognition`,
        filePath,
        timeout: 45000,
      });
      const mismatches = result.mismatches || [];
      const blocking = mismatches.filter((item) => item.field !== 'tag_suffix');
      if (blocking.length) {
        this.showLabelError(blocking);
        return;
      }
      let suffix = normalizeSuffix(result.recognized?.tag_suffix);
      if (result.needs_suffix || !suffix) {
        suffix = await this.askSuffix();
      } else {
        const suffixMismatch = mismatches.find((item) => item.field === 'tag_suffix');
        if (suffixMismatch) {
          this.showLabelError([suffixMismatch]);
          return;
        }
      }
      if (!suffix) return;
      if (this.data.scans.some((item) => item.suffix === suffix)) {
        wx.showToast({ title: `托盘 ${suffix} 已拍摄，请检查是否拿错`, icon: 'none' });
        return;
      }
      const scans = [
        ...this.data.scans,
        {
          suffix,
          localPath: filePath,
          recognition: result.recognition,
          recognized: result.recognized || {},
          photo: result.photo,
          validation: result,
        },
      ];
      const progress = scanProgress(ticket, scans);
      const nextSuffix = progress.remaining === null
        ? '可继续拍摄下一托'
        : progress.remaining > 0
          ? `还差 ${progress.remaining} 托`
        : '';
      this.setData({
        scans,
        nextSuffix,
        palletProgressText: progress.text,
      });
      this.persistDraft();
      wx.showToast({ title: `托盘 ${suffix} 已核对`, icon: 'success' });
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.showLabelError(
        error.response?.data?.mismatches || [],
        error.message || '吊牌没有读全，请重拍',
      );
    } finally {
      this.setData({ busy: false });
    }
  },

  showLabelError(mismatches, fallback = '吊牌与当前货物不一致') {
    const labels = {
      ship_no: '船号',
      engineering_no: '工程号',
      production_drawing_no: '制作图号',
      material_code: '物料编码',
      item_name: '品名',
      item_spec: '规格',
      label_quantity: '件数',
      tag_suffix: '托盘编号',
    };
    const content = mismatches.length
      ? mismatches.slice(0, 4).map((item) => {
        const label = labels[item.field] || '吊牌内容';
        if (item.reason === 'REQUIRED') return `${label}没读出来，请重拍`;
        if (item.reason === 'DUPLICATE') return `${label} ${item.actual || ''} 已经用过`;
        if (item.reason === 'TOO_MANY') {
          return `这批货的托盘已经拍齐，应为 ${item.expected} 托`;
        }
        return `${label}不一致，应为 ${item.expected || '—'}，照片上是 ${item.actual || '—'}`;
      }).join('\n')
      : fallback;
    wx.showModal({
      title: '吊牌没有对上',
      content: `${content}\n\n照片已保存，这次没有算收货。`,
      showCancel: false,
      confirmText: '重拍',
    });
  },

  askSuffix() {
    const page = this;
    return new Promise((resolve) => {
      wx.showModal({
        title: '补填托盘编号',
        content: '照片已保存，但没读全托盘编号。请对照吊牌填写。',
        editable: true,
        placeholderText: '例如 1-1',
        success(result) {
          if (!result.confirm) {
            resolve('');
            return;
          }
          const suffix = normalizeSuffix(result.content);
          if (!suffix) {
            wx.showToast({ title: '请按 1-1 的格式填写', icon: 'none' });
            page.askSuffix().then(resolve);
            return;
          }
          resolve(suffix);
        },
        fail() {
          resolve('');
        },
      });
    });
  },

  onQuantityInput(event) {
    if (this.data.submitUncertain) return;
    const quantity = event.detail.value;
    const remaining = remainingQuantity(this.data.selectedTicket);
    const showDifferenceReason = (
      String(quantity).trim() !== ''
      && Number(quantity) < Number(remaining)
    );
    this.setData({
      quantity,
      showDifferenceReason,
      differenceReason: showDifferenceReason
        ? this.data.differenceReason
        : '',
    });
    this.persistDraft();
  },

  toggleQuantityEdit() {
    if (this.data.submitUncertain) return;
    this.setData({ editQuantity: true });
  },

  restoreFullQuantity() {
    if (this.data.submitUncertain) return;
    this.setData({
      quantity: this.data.remainingQuantityText,
      differenceReason: '',
      showDifferenceReason: false,
      editQuantity: false,
    });
    this.persistDraft();
  },

  onDifferenceReasonInput(event) {
    if (this.data.submitUncertain) return;
    this.setData({ differenceReason: event.detail.value });
    this.persistDraft();
  },

  removeScan(event) {
    if (this.data.submitUncertain) return;
    const suffix = event.currentTarget.dataset.suffix;
    const scans = this.data.scans.filter((item) => item.suffix !== suffix);
    const progress = scanProgress(this.data.selectedTicket, scans);
    this.setData({
      scans,
      palletProgressText: progress.text,
      nextSuffix: progress.remaining === null
        ? '可继续拍摄下一托'
        : progress.remaining > 0
          ? `还差 ${progress.remaining} 托`
          : '',
    });
    this.persistDraft();
  },

  previewScanPhoto(event) {
    const localPath = event.currentTarget.dataset.path;
    if (!localPath) return;
    wx.previewImage({
      current: localPath,
      urls: [localPath],
    });
  },

  persistDraft() {
    if (
      this.skipDraftPersist
      || !this.draftScope
      || !this.data.selectedTicket
    ) return;
    writeDraft(this.draftScope, {
      manifest_id: this.data.selectedManifestId,
      ticket_id: this.data.selectedTicket.ticket_id,
      scans: this.data.scans,
      quantity: this.data.quantity,
      difference_reason: this.data.differenceReason,
      submit_uncertain: this.data.submitUncertain,
      submission_pending: Boolean(this.submissionPending),
    });
  },

  async confirmReceipt() {
    const { selectedTicket, scans, quantity } = this.data;
    const differenceReason = this.data.differenceReason.trim();
    const draft = validateReceiptDraft({
      ticket: selectedTicket,
      scans,
      quantity,
      differenceReason,
    });
    if (!draft.ok) {
      wx.showModal({
        title: '还不能提交收货',
        content: draft.error,
        showCancel: false,
      });
      return;
    }
    const submissionScope = receiptSubmissionScope(
      this.data.session,
      selectedTicket.ticket_id,
    );
    const submissionKey = getOrCreateSubmissionKey(
      submissionScope,
      'receipt',
    );
    const body = buildReceiptConfirmation({
      idempotencyKey: submissionKey,
      quantity,
      evidence: scans,
      differenceReason,
      deviceId: `wx-${this.data.session.user.username}`,
    });
    this.skipDraftPersist = false;
    this.submissionPending = true;
    this.setData({ busy: true, submitUncertain: false });
    this.persistDraft();
    try {
      await request({
        path: `/shipyard/inbound/tickets/${selectedTicket.ticket_id}/receipts`,
        method: 'POST',
        data: body,
      });
      this.skipDraftPersist = true;
      this.submissionPending = false;
      clearDraft(this.draftScope);
      clearSubmissionKey(submissionScope);
      this.setData({
        busy: false,
        selectedTicket: null,
        scans: [],
        nextSuffix: '',
        palletProgressText: '',
        remainingQuantityText: '',
        showDifferenceReason: false,
        editQuantity: false,
      });
      if (this.data.selectedManifestId) {
        await this.openManifest(this.data.selectedManifestId);
      }
      const nextTicketId = this.nextReceivableTicketId(
        selectedTicket.ticket_id,
      );
      if (nextTicketId) {
        this.beginTicket(nextTicketId);
        wx.showToast({ title: '已收货，继续下一票', icon: 'success' });
      } else {
        const unfinishedTicket = (
          this.data.selectedManifest?.tickets || []
        ).find(
          (ticket) => (
            ticket.ticket_id === selectedTicket.ticket_id
            && ticket.can_receive
            && remainingQuantity(ticket) > 0
          ),
        );
        if (typeof wx.pageScrollTo === 'function') {
          wx.pageScrollTo({ scrollTop: 0, duration: 250 });
        }
        if (unfinishedTicket) {
          wx.showToast({ title: '本次已记录，还需继续补收', icon: 'none' });
        } else {
          wx.showToast({ title: '本清单已全部收完', icon: 'success' });
        }
      }
    } catch (error) {
      if (isAmbiguousSubmissionError(error)) {
        this.skipDraftPersist = false;
        this.submissionPending = false;
        this.setData({ submitUncertain: true });
        this.persistDraft();
        wx.showModal({
          title: '提交结果未确认',
          content: '网络中断或系统暂时不可用，操作可能已经完成。请再次确认收货，系统不会重复入库。',
          showCancel: false,
          confirmText: '知道了',
        });
      } else if (error.statusCode === 409) {
        this.skipDraftPersist = true;
        this.submissionPending = false;
        clearDraft(this.draftScope);
        clearSubmissionKey(submissionScope);
        this.setData({ submitUncertain: false });
        await this.loadManifests();
        wx.showModal({
          title: '状态已更新',
          content: '货物状态已经变化，已刷新最新内容。',
          showCancel: false,
        });
      } else {
        this.skipDraftPersist = false;
        this.submissionPending = false;
        clearSubmissionKey(submissionScope);
        this.setData({ submitUncertain: false });
        this.persistDraft();
        wx.showToast({ title: error.message, icon: 'none' });
      }
    } finally {
      this.setData({ busy: false });
    }
  },
});
