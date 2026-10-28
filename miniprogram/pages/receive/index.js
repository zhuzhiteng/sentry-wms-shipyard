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
    busy: false,
    submitUncertain: false,
    loading: false,
    cameraVisible: false,
    pendingSuffix: '',
    nextSuffix: '',
    palletProgressText: '',
    remainingQuantityText: '',
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
        status_label: STATUS_LABELS[item.status] || item.status,
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
        content: '任务状态已经变化，草稿已清理。请按最新票货状态操作。',
        showCancel: false,
      });
      return;
    }
    const scans = Array.isArray(draft.scans) ? draft.scans : [];
    const progress = scanProgress(ticket, scans);
    const remaining = numberText(remainingQuantity(ticket));
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.setData({
      selectedTicket: ticket,
      scans,
      quantity: draft.quantity || remaining,
      differenceReason: draft.difference_reason || '',
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
      const data = await request({
        path: `/shipyard/inbound/manifests/${manifestId}`,
      });
      const tickets = (data.tickets || []).map((ticket) => ({
        ...ticket,
        status_label: STATUS_LABELS[ticket.status] || ticket.status,
        status_class: statusClass(ticket.status),
        short_quantity_text: numberText(ticket.short_quantity),
        remaining_quantity_text: numberText(remainingQuantity(ticket)),
        pallet_progress_text: scanProgress(ticket, []).text,
        can_receive: ['EXPECTED', 'PARTIAL'].includes(ticket.status),
      }));
      const selectedManifest = { ...data, tickets };
      this.setData({
        selectedManifest,
        selectedManifestId: manifestId,
        selectedTicket: null,
        scans: [],
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
    if (this.data.busy || this.submissionPending) {
      wx.showToast({ title: '正在提交，请等待结果', icon: 'none' });
      return;
    }
    if (this.data.submitUncertain) {
      wx.showToast({ title: '上次提交结果未确认，请先重试', icon: 'none' });
      return;
    }
    const ticketId = event.currentTarget.dataset.id;
    const ticket = this.data.selectedManifest.tickets.find(
      (item) => item.ticket_id === ticketId,
    );
    if (!ticket) return;
    if (!['EXPECTED', 'PARTIAL'].includes(ticket.status)) {
      wx.showToast({ title: '该票当前不能收货', icon: 'none' });
      return;
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
      submitUncertain: false,
      palletProgressText: progress.text,
      remainingQuantityText: numberText(remaining),
    });
    this.persistDraft();
    wx.pageScrollTo({ selector: '#ticket-workbench', duration: 250 });
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
      wx.showToast({ title: `托盘 ${suffix} 已识别`, icon: 'success' });
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.showLabelError(
        error.response?.data?.mismatches || [],
        error.message || '吊牌识别失败，请重拍',
      );
    } finally {
      this.setData({ busy: false });
    }
  },

  showLabelError(mismatches, fallback = '吊牌字段与当前票货不一致') {
    const labels = {
      ship_no: '船号',
      engineering_no: '工程号',
      production_drawing_no: '制作图号',
      material_code: '物料编码',
      item_name: '品名',
      item_spec: '规格',
      label_quantity: '件数',
      tag_suffix: '托盘后缀',
    };
    const content = mismatches.length
      ? mismatches.slice(0, 4).map((item) => {
        const label = labels[item.field] || item.field;
        if (item.reason === 'REQUIRED') return `${label}未识别，请重拍`;
        if (item.reason === 'DUPLICATE') return `${label} ${item.actual || ''} 已使用`;
        if (item.reason === 'TOO_MANY') {
          return `本票托盘数量已满，预计 ${item.expected} 托`;
        }
        return `${label}不一致，应为 ${item.expected || '—'}，识别为 ${item.actual || '—'}`;
      }).join('\n')
      : fallback;
    wx.showModal({
      title: '吊牌未通过',
      content: `${content}\n\n照片已留证，本次未计入收货。`,
      showCancel: false,
      confirmText: '重拍',
    });
  },

  askSuffix() {
    return new Promise((resolve) => {
      wx.showModal({
        title: '补录托盘后缀',
        content: 'OCR 未读到后缀，请按吊牌核对后输入。',
        editable: true,
        placeholderText: '例如 1-1',
        success(result) {
          if (!result.confirm) {
            resolve('');
            return;
          }
          const suffix = normalizeSuffix(result.content);
          if (!suffix) {
            wx.showToast({ title: '后缀格式应为 1-1', icon: 'none' });
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
    this.setData({ quantity: event.detail.value });
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
      const result = await request({
        path: `/shipyard/inbound/tickets/${selectedTicket.ticket_id}/receipts`,
        method: 'POST',
        data: body,
      });
      this.skipDraftPersist = true;
      this.submissionPending = false;
      clearDraft(this.draftScope);
      clearSubmissionKey(submissionScope);
      wx.showModal({
        title: '收货已完成',
        content: `${result.ticket.item_name}\n实收 ${result.ticket.received_quantity} 件\n区域 ${result.receipt.location_path_snapshot}`,
        showCancel: false,
        success: () => {
          this.setData({
            selectedTicket: null,
            scans: [],
            nextSuffix: '',
            palletProgressText: '',
            remainingQuantityText: '',
          });
          this.loadManifests();
        },
      });
    } catch (error) {
      if (isAmbiguousSubmissionError(error)) {
        this.skipDraftPersist = false;
        this.submissionPending = false;
        this.setData({ submitUncertain: true });
        this.persistDraft();
        wx.showModal({
          title: '提交结果未确认',
          content: '网络中断或服务暂不可用，操作可能已经完成。请重试上次提交，系统不会重复入库。',
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
          content: '该票货状态已经变化，已刷新最新任务。',
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
