const { request } = require('../../utils/request');
const { requireSession } = require('../../utils/session');
const {
  clearSubmissionKey,
  getOrCreateSubmissionKey,
  isAmbiguousSubmissionError,
} = require('../../utils/workflow');

const STATUS_LABELS = {
  PENDING: '待出库',
  IN_PROGRESS: '出库中',
  PARTIAL: '部分出库',
  OUTBOUND: '已出库',
  LOADED: '已装车',
  EXCEPTION: '异常',
};

function statusClass(status) {
  if (status === 'LOADED') return 'chip-success';
  if (['PARTIAL', 'IN_PROGRESS', 'EXCEPTION'].includes(status)) return 'chip-warning';
  return '';
}

function numberText(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) ? String(number) : number.toFixed(3);
}

function loadingSubmissionScope(session, orderId) {
  return `loading-submit:${session.user.username}:${orderId}`;
}

Page({
  data: {
    session: null,
    warehouseId: 1,
    orders: [],
    selectedOrder: null,
    selectedOrderId: '',
    filter: 'pending',
    visibleTasks: [],
    busy: false,
    loadingUncertain: false,
    loading: false,
  },

  onLoad() {
    const session = requireSession();
    if (!session) return;
    this.setData({ session, warehouseId: session.warehouseId });
  },

  onShow() {
    if (!this.data.session) return;
    this.loadOrders();
  },

  onPullDownRefresh() {
    this.loadOrders().finally(() => wx.stopPullDownRefresh());
  },

  async loadOrders() {
    this.setData({ loading: true });
    try {
      const data = await request({
        path: `/shipyard/outbound/mobile/orders?warehouse_id=${this.data.warehouseId}`,
      });
      const orders = (data.orders || []).map((item) => ({
        ...item,
        status_label: STATUS_LABELS[item.status] || item.status,
        status_class: statusClass(item.status),
        progress_text: `${numberText(item.total_fulfilled_quantity)} / ${numberText(item.total_required_quantity)}`,
      }));
      this.setData({ orders });
      if (this.data.selectedOrder) {
        await this.openOrder(this.data.selectedOrder.order.outbound_order_id);
      } else {
        const actionable = orders.filter((item) => (
          Number(item.pending_task_count || 0) > 0
          || ['PENDING', 'IN_PROGRESS', 'PARTIAL'].includes(item.status)
        ));
        if (actionable.length === 1) {
          await this.openOrder(actionable[0].outbound_order_id);
        }
      }
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async openOrder(orderId) {
    this.setData({ busy: true });
    try {
      const data = await request({
        path: `/shipyard/outbound/mobile/orders/${orderId}`,
      });
      const tasks = (data.tasks || []).map((task) => ({
        ...task,
        status_label: STATUS_LABELS[task.status] || task.status,
        status_class: statusClass(task.status),
        suffix_text: (task.expected_tag_suffixes || []).join('、'),
        can_outbound: task.status === 'PENDING',
        batch_text: (task.pallets || [])[0]?.batch_no || '—',
      }));
      const selectedOrder = { ...data, tasks };
      const loadingUncertain = data.order.status === 'LOADED'
        ? false
        : this.data.loadingUncertain;
      if (data.order.status === 'LOADED') {
        clearSubmissionKey(
          loadingSubmissionScope(
            this.data.session,
            data.order.outbound_order_id,
          ),
        );
      }
      this.setData({
        selectedOrder,
        selectedOrderId: selectedOrder.order.outbound_order_id,
        loadingUncertain,
      });
      this.applyFilter(this.data.filter, selectedOrder);
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onOrderTap(event) {
    this.openOrder(event.currentTarget.dataset.id);
  },

  setFilter(event) {
    const filter = event.currentTarget.dataset.filter;
    this.setData({ filter });
    this.applyFilter(filter, this.data.selectedOrder);
  },

  applyFilter(filter, order) {
    if (!order) {
      this.setData({ visibleTasks: [] });
      return;
    }
    const visibleTasks = (order.tasks || []).filter((task) => {
      if (filter === 'pending') return task.status === 'PENDING';
      if (filter === 'outbound') return task.status !== 'PENDING';
      return true;
    });
    this.setData({ visibleTasks });
  },

  openTask(event) {
    const taskId = event.currentTarget.dataset.id;
    const orderId = this.data.selectedOrder.order.outbound_order_id;
    wx.navigateTo({
      url: `/pages/outbound/task?taskId=${taskId}&orderId=${orderId}`,
    });
  },

  confirmLoading() {
    const order = this.data.selectedOrder;
    if (!order || order.order.status !== 'OUTBOUND') return;
    const submissionScope = loadingSubmissionScope(
      this.data.session,
      order.order.outbound_order_id,
    );
    const submissionKey = getOrCreateSubmissionKey(
      submissionScope,
      'loading',
    );
    wx.showModal({
      title: this.data.loadingUncertain ? '重试确认整单装车' : '确认整单装车',
      content: '',
      editable: true,
      placeholderText: '可填写车辆、司机或交接备注',
      confirmText: '确认装车',
      success: async (result) => {
        if (!result.confirm) return;
        this.setData({ busy: true });
        try {
          const data = await request({
            path: `/shipyard/outbound/orders/${order.order.outbound_order_id}/loading`,
            method: 'POST',
            data: {
              idempotency_key: submissionKey,
              client_device_id: `wx-${this.data.session.user.username}`,
              notes: result.content || '',
            },
          });
          clearSubmissionKey(submissionScope);
          wx.showToast({ title: '装车已确认', icon: 'success' });
          this.setData({
            selectedOrder: {
              ...data,
              tasks: data.tasks.map((task) => ({
                ...task,
                status_label: STATUS_LABELS[task.status] || task.status,
                status_class: statusClass(task.status),
                suffix_text: (task.expected_tag_suffixes || []).join('、'),
                can_outbound: task.status === 'PENDING',
                batch_text: (task.pallets || [])[0]?.batch_no || '—',
              })),
            },
            selectedOrderId: data.order.outbound_order_id,
            loadingUncertain: false,
          });
          await this.loadOrders();
        } catch (error) {
          if (isAmbiguousSubmissionError(error)) {
            this.setData({ loadingUncertain: true });
            wx.showModal({
              title: '装车结果未确认',
              content: '网络中断或服务暂不可用，装车可能已经完成。请再次点击确认装车，系统会复用同一提交编号。',
              showCancel: false,
            });
          } else if (error.statusCode === 409) {
            clearSubmissionKey(submissionScope);
            this.setData({ loadingUncertain: false });
            await this.loadOrders();
            wx.showModal({
              title: '状态已更新',
              content: '出库单状态已经变化，已刷新最新装车信息。',
              showCancel: false,
            });
          } else {
            clearSubmissionKey(submissionScope);
            this.setData({ loadingUncertain: false });
            wx.showToast({ title: error.message, icon: 'none' });
          }
        } finally {
          this.setData({ busy: false });
        }
      },
    });
  },
});
