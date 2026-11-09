const { buildOutboundConfirmation } = require('../../utils/payload');
const { request, upload } = require('../../utils/request');
const {
  chooseNextSuffix,
  normalizeSuffix,
} = require('../../utils/pallet');
const { requireSession } = require('../../utils/session');
const {
  clearDraft,
  clearSubmissionKey,
  getOrCreateSubmissionKey,
  isAmbiguousSubmissionError,
  readDraft,
  writeDraft,
} = require('../../utils/workflow');

function outboundDraftScope(session, taskId) {
  return `outbound:${session.user.username}:${taskId}`;
}

function outboundSubmissionScope(session, taskId) {
  return `outbound-submit:${session.user.username}:${taskId}`;
}

Page({
  data: {
    session: null,
    taskId: '',
    orderId: '',
    task: null,
    order: null,
    scans: [],
    nextSuffix: '',
    busy: false,
    submitUncertain: false,
    complete: false,
    can_confirm: false,
    suffixPickerVisible: false,
    suffixChoices: [],
    pendingCapture: null,
  },

  onLoad(options) {
    const session = requireSession();
    if (!session) return;
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.setData({
      session,
      taskId: options.taskId,
      orderId: options.orderId,
    });
    this.draftScope = outboundDraftScope(session, options.taskId);
    this.pendingDraft = readDraft(this.draftScope);
    this.loadTask();
  },

  onHide() {
    this.persistDraft();
  },

  onUnload() {
    this.persistDraft();
  },

  async loadTask() {
    this.setData({ busy: true });
    try {
      const data = await request({
        path: `/shipyard/outbound/mobile/orders/${this.data.orderId}`,
      });
      const task = (data.tasks || []).find(
        (item) => item.task_id === this.data.taskId,
      );
      if (!task) throw new Error('没有找到这批出库货物');
      const draft = this.pendingDraft;
      this.pendingDraft = null;
      const hadDraft = Boolean(draft && draft.task_id === task.task_id);
      let scans = hadDraft && Array.isArray(draft.scans)
        ? draft.scans
        : this.data.scans;
      let submitUncertain = hadDraft
        ? Boolean(draft.submit_uncertain || draft.submission_pending)
        : this.data.submitUncertain;
      this.submissionPending = false;
      let pendingCapture = hadDraft ? (draft.pending_capture || null) : null;
      let suffixChoices = hadDraft && Array.isArray(draft.suffix_choices)
        ? draft.suffix_choices
        : [];
      if (task.status === 'OUTBOUND') {
        if (hadDraft) {
          wx.showToast({ title: '该票已完成出库，草稿已清理', icon: 'none' });
        }
        this.skipDraftPersist = true;
        this.submissionPending = false;
        clearDraft(this.draftScope);
        clearSubmissionKey(
          outboundSubmissionScope(this.data.session, task.task_id),
        );
        scans = [];
        submitUncertain = false;
        pendingCapture = null;
        suffixChoices = [];
      } else if (pendingCapture) {
        const completed = new Set(scans.map((item) => item.suffix));
        suffixChoices = suffixChoices.filter((suffix) => !completed.has(suffix));
        if (!suffixChoices.length) pendingCapture = null;
      }
      const nextSuffix = chooseNextSuffix(
        task.expected_tag_suffixes,
        scans.map((item) => item.suffix),
      );
      const decoratedTask = {
        ...task,
        suffix_text: (task.expected_tag_suffixes || []).join('、'),
      };
      this.setData({
        task: decoratedTask,
        order: data,
        nextSuffix,
        scans,
        submitUncertain,
        pendingCapture,
        suffixChoices,
        suffixPickerVisible: Boolean(pendingCapture && suffixChoices.length),
        can_confirm: scans.length === (task.expected_tag_suffixes || []).length,
        complete: (data.tasks || []).every((item) => item.status === 'OUTBOUND'),
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  async captureLabel() {
    const { task, busy, submitUncertain } = this.data;
    if (!task || busy || submitUncertain) return;
    if (task.status !== 'PENDING') {
      wx.showToast({ title: '该票已经完成出库', icon: 'none' });
      return;
    }
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
        path: `/shipyard/outbound/tasks/${task.task_id}/label-recognition`,
        filePath,
        timeout: 45000,
      });
      result.localPath = filePath;
      const mismatches = result.mismatches || [];
      const blocking = mismatches.filter((item) => item.field !== 'tag_suffix');
      if (blocking.length) {
        this.showLabelError(result, blocking);
        return;
      }
      const suffix = normalizeSuffix(result.recognized?.tag_suffix);
      if (result.needs_suffix || !suffix) {
        this.openSuffixPicker(result);
        return;
      } else {
        const suffixMismatch = mismatches.find((item) => item.field === 'tag_suffix');
        if (suffixMismatch) {
          this.showLabelError(result, [suffixMismatch]);
          return;
        }
      }
      this.addCapture(result, suffix);
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.showLabelError(error.response?.data || {}, [], error.message);
    } finally {
      this.setData({ busy: false });
    }
  },

  openSuffixPicker(result) {
    const completed = new Set(this.data.scans.map((item) => item.suffix));
    const suffixChoices = (this.data.task.expected_tag_suffixes || [])
      .filter((suffix) => !completed.has(suffix));
    if (!suffixChoices.length) {
      this.showLabelError(result, [{
        field: 'pallet_count',
        expected: this.data.task.expected_pallet_count,
        actual: this.data.scans.length,
      }]);
      return;
    }
    this.setData({
      suffixPickerVisible: true,
      suffixChoices,
      pendingCapture: result,
    });
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.persistDraft();
  },

  selectSuffix(event) {
    const suffix = normalizeSuffix(event.currentTarget.dataset.suffix);
    const pendingCapture = this.data.pendingCapture;
    if (!suffix || !pendingCapture) return;
    if (!(this.data.task.expected_tag_suffixes || []).includes(suffix)) {
      wx.showToast({ title: '这个托盘不属于当前货物', icon: 'none' });
      return;
    }
    if (this.data.scans.some((item) => item.suffix === suffix)) {
      wx.showToast({ title: `托盘 ${suffix} 已经拍过`, icon: 'none' });
      return;
    }
    this.setData({
      suffixPickerVisible: false,
      suffixChoices: [],
      pendingCapture: null,
    });
    this.addCapture(pendingCapture, suffix);
  },

  cancelSuffixPicker() {
    this.setData({
      suffixPickerVisible: false,
      suffixChoices: [],
      pendingCapture: null,
    });
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.persistDraft();
    wx.showToast({ title: '照片已保存，这次没有算出库', icon: 'none' });
  },

  addCapture(result, suffix) {
    const task = this.data.task;
    const scans = [
      ...this.data.scans,
      {
        suffix,
        localPath: result.localPath || '',
        recognition: result.recognition,
        recognized: result.recognized || {},
        photo: result.photo,
        validation: result,
      },
    ];
    const nextSuffix = chooseNextSuffix(
      task.expected_tag_suffixes,
      scans.map((item) => item.suffix),
    );
    this.setData({
      scans,
      nextSuffix,
      can_confirm: scans.length === task.expected_tag_suffixes.length,
    });
    this.skipDraftPersist = false;
    this.submissionPending = false;
    this.persistDraft();
    wx.showToast({ title: `托盘 ${suffix} 核对通过`, icon: 'success' });
  },

  showLabelError(result, mismatches = [], fallback = '') {
    const candidates = result.candidate_tasks || [];
    if (candidates.length) {
      const goToCandidate = (candidate) => {
        wx.redirectTo({
          url: `/pages/outbound/task?taskId=${candidate.task_id}&orderId=${candidate.outbound_order_id}`,
        });
      };
      if (candidates.length === 1) {
        wx.showModal({
          title: '拍到了另一批货',
          content: candidates[0].task_no
            ? `这张吊牌对应出库单 ${candidates[0].task_no}，当前页面不能继续。`
            : '这张吊牌对应另一张出库单，当前页面不能继续。',
          confirmText: '去正确出库单',
          cancelText: '留在当前票',
          success: (choice) => {
            if (choice.confirm) goToCandidate(candidates[0]);
          },
        });
        return;
      }
      wx.showActionSheet({
        itemList: candidates.map(
          (item) => `${item.outbound_order_no} · ${item.task_no}`,
        ),
        success: (choice) => {
          const candidate = candidates[choice.tapIndex];
          if (candidate) goToCandidate(candidate);
        },
      });
      return;
    }
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
    const details = mismatches.slice(0, 4).map((item) => {
      const label = labels[item.field] || item.field;
      if (item.reason === 'REQUIRED') return `${label}没读出来`;
      return `${label}不一致，应为 ${item.expected || '—'}，照片上是 ${item.actual || '—'}`;
    }).join('\n');
    if (result.mismatches?.length && !mismatches.length) {
      return this.showLabelError(result, result.mismatches, fallback);
    }
    if (result.matched_task_id && result.matched_outbound_order_id) {
      wx.showModal({
        title: '拍到了另一批货',
        content: result.matched_task_no
          ? `这张吊牌对应出库单 ${result.matched_task_no}，当前页面不能继续。`
          : '这张吊牌对应另一张出库单，当前页面不能继续。',
        confirmText: '去正确出库单',
        cancelText: '留在当前票',
        success: (choice) => {
          if (!choice.confirm) return;
          wx.redirectTo({
            url: `/pages/outbound/task?taskId=${result.matched_task_id}&orderId=${result.matched_outbound_order_id}`,
          });
        },
      });
      return;
    }
    wx.showModal({
      title: '吊牌未通过',
      content: `${details || fallback || '请调整角度后重拍'}\n\n照片已保存，这次没有算出库。`,
      showCancel: false,
      confirmText: '重拍',
    });
  },

  removeScan(event) {
    if (this.data.submitUncertain) return;
    const suffix = event.currentTarget.dataset.suffix;
    const scans = this.data.scans.filter((item) => item.suffix !== suffix);
    this.setData({
      scans,
      nextSuffix: chooseNextSuffix(
        this.data.task.expected_tag_suffixes,
        scans.map((item) => item.suffix),
      ),
      can_confirm: scans.length === this.data.task.expected_tag_suffixes.length,
    });
    this.skipDraftPersist = false;
    this.submissionPending = false;
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
    if (this.skipDraftPersist || !this.draftScope || !this.data.task) return;
    writeDraft(this.draftScope, {
      task_id: this.data.task.task_id,
      order_id: this.data.orderId,
      scans: this.data.scans,
      pending_capture: this.data.pendingCapture,
      suffix_choices: this.data.suffixChoices,
      submit_uncertain: this.data.submitUncertain,
      submission_pending: Boolean(this.submissionPending),
    });
  },

  async confirmOutbound() {
    const { task, scans } = this.data;
    if (!task) return;
    const expected = task.expected_tag_suffixes || [];
    if (scans.length !== expected.length) {
      wx.showToast({
        title: `托盘还没拍齐，还缺 ${expected.length - scans.length} 托`,
        icon: 'none',
      });
      return;
    }
    const submissionScope = outboundSubmissionScope(
      this.data.session,
      task.task_id,
    );
    const submissionKey = getOrCreateSubmissionKey(
      submissionScope,
      'outbound',
    );
    const body = buildOutboundConfirmation(
      submissionKey,
      scans,
      `wx-${this.data.session.user.username}`,
    );
    this.skipDraftPersist = false;
    this.submissionPending = true;
    this.setData({ busy: true, submitUncertain: false });
    this.persistDraft();
    try {
      const result = await request({
        path: `/shipyard/outbound/tasks/${task.task_id}/confirm`,
        method: 'POST',
        data: body,
      });
      this.skipDraftPersist = true;
      this.submissionPending = false;
      clearDraft(this.draftScope);
      clearSubmissionKey(submissionScope);
      wx.showModal({
        title: '这批货已出库',
        content: `数量 ${result.confirmation.quantity} 件\n存放区域 ${result.confirmation.location_path_snapshot}`,
        showCancel: false,
        success: () => wx.navigateBack(),
      });
    } catch (error) {
      if (isAmbiguousSubmissionError(error)) {
        this.skipDraftPersist = false;
        this.submissionPending = false;
        this.setData({ submitUncertain: true });
        this.persistDraft();
        wx.showModal({
          title: '提交结果未确认',
          content: '网络中断或系统暂时不可用，这批货可能已经出库。请再次确认出库，系统不会重复扣库。',
          showCancel: false,
          confirmText: '知道了',
        });
      } else if (error.statusCode === 409) {
        this.skipDraftPersist = true;
        this.submissionPending = false;
        clearDraft(this.draftScope);
        clearSubmissionKey(submissionScope);
        this.setData({ submitUncertain: false });
        await this.loadTask();
        wx.showModal({
          title: '状态已更新',
          content: '货物状态已经变化，已刷新最新出库信息。',
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
