const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function installMock(relativePath, exports) {
  const modulePath = require.resolve(path.resolve(__dirname, '..', relativePath));
  require.cache[modulePath] = {
    id: modulePath,
    filename: modulePath,
    loaded: true,
    exports,
  };
}

function loadPage(relativePath) {
  let definition;
  global.Page = (config) => {
    definition = config;
  };
  const pagePath = require.resolve(path.resolve(__dirname, '..', relativePath));
  delete require.cache[pagePath];
  require(pagePath);
  return definition;
}

function instantiate(definition) {
  const page = {
    ...definition,
    data: JSON.parse(JSON.stringify(definition.data)),
    setData(patch) {
      this.data = { ...this.data, ...patch };
    },
  };
  return page;
}

function installMemoryStorage(wx) {
  const values = new Map();
  wx.getStorageSync = (key) => values.get(key);
  wx.setStorageSync = (key, value) => values.set(key, value);
  wx.removeStorageSync = (key) => values.delete(key);
  return values;
}

test('receiving captures once and uses OCR without scanning a QR code', async () => {
  const uploads = [];
  const previews = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      throw new Error('normal receiving must not call the legacy scan API');
    },
    upload: async (options) => {
      uploads.push(options);
      return {
        matched: true,
        needs_suffix: false,
        recognized: {
          ship_no: 'HUX-01',
          material_code: 'MAT-01',
          item_name: '船用支架',
          tag_suffix: '1-1',
        },
        mismatches: [],
        recognition: { recognition_id: 'recognition-1' },
        photo: { photo_id: 'photo-1', file_name: 'label.jpg' },
      };
    },
  });

  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/label.jpg' }],
    }),
    previewImage: (options) => previews.push(options),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };

  await page.captureLabel();

  assert.equal(uploads.length, 1);
  assert.equal(
    uploads[0].path,
    '/shipyard/inbound/tickets/ticket-1/label-recognition',
  );
  assert.equal(page.data.scans.length, 1);
  assert.equal(page.data.scans[0].recognition.recognition_id, 'recognition-1');
  assert.equal(page.data.scans[0].localPath, '/tmp/label.jpg');
  assert.equal(page.data.palletProgressText, '1 / 2 托');
  assert.equal(page.data.nextSuffix, '还差 1 托');

  page.previewScanPhoto({
    currentTarget: { dataset: { path: page.data.scans[0].localPath } },
  });
  assert.deepEqual(previews[0], {
    current: '/tmp/label.jpg',
    urls: ['/tmp/label.jpg'],
  });
});

test('receiving submits a short quantity with fewer pallets and its reason', async () => {
  const requests = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async (options) => {
      requests.push(options);
      return {
        ticket: {
          item_name: '船用支架',
          received_quantity: '2.000',
        },
        receipt: {
          location_path_snapshot: 'A 区 / A1',
        },
      };
    },
    upload: async () => ({ photo: { photo_id: 'photo-1' } }),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件，余货明天补到';

  await page.confirmReceipt();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/shipyard/inbound/tickets/ticket-1/receipts');
  assert.equal(requests[0].data.received_quantity, 2);
  assert.equal(requests[0].data.pallets.length, 1);
  assert.equal(requests[0].data.pallets[0].label_recognition_id, 'recognition-1');
  assert.equal(requests[0].data.difference_reason, '少 2 件，余货明天补到');
});

test('outbound asks only for the remaining suffix when OCR misses it', async () => {
  const previews = [];
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async () => ({
      matched: false,
      needs_suffix: true,
      recognized: {
        material_code: 'MAT-01',
        item_name: '船用支架',
      },
      mismatches: [{ field: 'tag_suffix', reason: 'REQUIRED' }],
      recognition: { recognition_id: 'recognition-2' },
      photo: { photo_id: 'photo-2', file_name: 'label.jpg' },
    }),
  });
  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/label.jpg' }],
    }),
    previewImage: (options) => previews.push(options),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.task = {
    task_id: 'task-1',
    status: 'PENDING',
    expected_tag_suffixes: ['1-1', '1-2'],
    expected_pallet_count: 2,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];

  await page.captureLabel();

  assert.equal(page.data.suffixPickerVisible, true);
  assert.deepEqual(page.data.suffixChoices, ['1-2']);
  assert.equal(page.data.scans.length, 1);

  page.selectSuffix({ currentTarget: { dataset: { suffix: '1-2' } } });

  assert.equal(page.data.suffixPickerVisible, false);
  assert.equal(page.data.scans.length, 2);
  assert.equal(page.data.scans[1].recognition.recognition_id, 'recognition-2');
  assert.equal(page.data.scans[1].localPath, '/tmp/label.jpg');
  assert.equal(page.data.can_confirm, true);

  page.previewScanPhoto({
    currentTarget: { dataset: { path: page.data.scans[1].localPath } },
  });
  assert.deepEqual(previews[0], {
    current: '/tmp/label.jpg',
    urls: ['/tmp/label.jpg'],
  });
});

test('outbound list refreshes whenever the task screen returns', () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({ orders: [] }),
    upload: async () => ({}),
  });
  global.wx = {};

  const page = instantiate(loadPage('pages/outbound/index.js'));
  let loads = 0;
  page.loadOrders = async () => {
    loads += 1;
  };

  page.onLoad();
  assert.equal(loads, 0);
  page.onShow();
  assert.equal(loads, 1);
  page.onShow();
  assert.equal(loads, 2);
});

test('receiving reuses the same idempotency key after an unknown network outcome', async () => {
  const requests = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async (options) => {
      requests.push(options);
      if (requests.length === 1) {
        const error = new Error('request:fail timeout');
        error.statusCode = 0;
        error.networkError = true;
        throw error;
      }
      return {
        ticket: { item_name: '船用支架', received_quantity: '2.000' },
        receipt: { location_path_snapshot: 'A 区 / A1' },
      };
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件';

  await page.confirmReceipt();
  assert.equal(page.data.submitUncertain, true);
  assert.ok(storage.size > 0);

  await page.confirmReceipt();
  assert.equal(requests.length, 2);
  assert.equal(
    requests[1].data.idempotency_key,
    requests[0].data.idempotency_key,
  );
  assert.equal(page.data.submitUncertain, false);
});

test('receiving restores an unsent draft after the page is reopened', async () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  const manifestId = 'manifest-1';
  const ticketId = 'ticket-1';
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async ({ path }) => {
      if (path.startsWith('/shipyard/inbound/manifests?')) {
        return {
          manifests: [{
            manifest_id: manifestId,
            manifest_no: 'IN-1',
            status: 'CONFIRMED',
            pending_ticket_count: 1,
          }],
        };
      }
      return {
        manifest: {
          manifest_id: manifestId,
          manifest_no: 'IN-1',
          status: 'CONFIRMED',
        },
        tickets: [{
          ticket_id: ticketId,
          ticket_no: 'T-1',
          status: 'EXPECTED',
          declared_quantity: '4.000',
          received_quantity: '0.000',
          expected_pallet_count: 2,
          pallet_count: 0,
          item_name: '船用支架',
          location_path_snapshot: 'A 区 / A1',
        }],
      };
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = `receipt:receiver:1`;
  const workflow = require('../utils/workflow');
  workflow.writeDraft(page.draftScope, {
    manifest_id: manifestId,
    ticket_id: ticketId,
    scans: [{
      suffix: '1-1',
      recognition: { recognition_id: 'recognition-1' },
      photo: { photo_id: 'photo-1' },
    }],
    quantity: '2',
    difference_reason: '少 2 件，余货补发',
    submit_uncertain: false,
  });
  page.pendingDraft = workflow.readDraft(page.draftScope);

  await page.loadManifests();

  assert.equal(page.data.selectedTicket.ticket_id, ticketId);
  assert.equal(page.data.scans.length, 1);
  assert.equal(page.data.quantity, '2');
  assert.equal(page.data.differenceReason, '少 2 件，余货补发');
});

test('starting a new receipt clears a stale successful submission key', () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    pageScrollTo: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = 'receipt:receiver:1';
  page.data.selectedManifest = {
    tickets: [{
      ticket_id: 'ticket-1',
      status: 'PARTIAL',
      declared_quantity: '4.000',
      received_quantity: '2.000',
      expected_pallet_count: 2,
      pallet_count: 1,
    }],
  };
  const workflow = require('../utils/workflow');
  workflow.getOrCreateSubmissionKey(
    'receipt-submit:receiver:ticket-1',
    'receipt',
  );

  page.startTicket({
    currentTarget: { dataset: { id: 'ticket-1' } },
  });

  assert.equal(
    workflow.hasSubmissionKey('receipt-submit:receiver:ticket-1'),
    false,
  );
});

test('receiving cannot switch tickets while a submission is in flight', () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async () => ({}),
  });
  const toasts = [];
  global.wx = {
    showToast: (options) => toasts.push(options.title),
    pageScrollTo: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = 'receipt:receiver:1';
  page.data.busy = true;
  page.data.selectedManifest = {
    tickets: [{
      ticket_id: 'ticket-1',
      status: 'EXPECTED',
      declared_quantity: '4.000',
      received_quantity: '0.000',
      expected_pallet_count: 2,
      pallet_count: 0,
    }],
  };
  const workflow = require('../utils/workflow');
  workflow.getOrCreateSubmissionKey(
    'receipt-submit:receiver:ticket-1',
    'receipt',
  );

  page.startTicket({
    currentTarget: { dataset: { id: 'ticket-1' } },
  });

  assert.equal(page.data.selectedTicket, null);
  assert.match(toasts.at(-1), /正在提交/);
  assert.equal(
    workflow.hasSubmissionKey('receipt-submit:receiver:ticket-1'),
    true,
  );
});

test('receiving blocks refresh and manifest switching while submitting', () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async () => ({}),
  });
  let stoppedRefreshes = 0;
  const toasts = [];
  global.wx = {
    stopPullDownRefresh: () => { stoppedRefreshes += 1; },
    showToast: (options) => toasts.push(options.title),
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.busy = true;
  let loads = 0;
  let opens = 0;
  page.loadManifests = async () => { loads += 1; };
  page.openManifest = async () => { opens += 1; };

  page.onPullDownRefresh();
  page.onManifestTap({ currentTarget: { dataset: { id: 'manifest-2' } } });

  assert.equal(stoppedRefreshes, 1);
  assert.equal(loads, 0);
  assert.equal(opens, 0);
  assert.equal(toasts.length, 2);
});

test('outbound restores photographed pallets before confirmation', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({
      order: {
        outbound_order_id: 'order-1',
        outbound_order_no: 'OUT-1',
        status: 'PENDING',
      },
      summary: {},
      tasks: [{
        task_id: 'task-1',
        task_no: 'TASK-1',
        status: 'PENDING',
        expected_tag_suffixes: ['1-1', '1-2'],
        expected_pallet_count: 2,
        planned_quantity: '4.000',
        pallets: [],
      }],
    }),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.taskId = 'task-1';
  page.data.orderId = 'order-1';
  page.draftScope = 'outbound:outbound:task-1';
  const workflow = require('../utils/workflow');
  workflow.writeDraft(page.draftScope, {
    task_id: 'task-1',
    order_id: 'order-1',
    scans: [
      {
        suffix: '1-1',
        recognition: { recognition_id: 'recognition-1' },
        photo: { photo_id: 'photo-1' },
      },
      {
        suffix: '1-2',
        recognition: { recognition_id: 'recognition-2' },
        photo: { photo_id: 'photo-2' },
      },
    ],
    submit_uncertain: false,
  });
  page.pendingDraft = workflow.readDraft(page.draftScope);

  await page.loadTask();

  assert.equal(page.data.scans.length, 2);
  assert.equal(page.data.can_confirm, true);
  assert.equal(page.data.nextSuffix, '');
});

test('loading confirmation reuses its key after an unknown network outcome', async () => {
  const requests = [];
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async (options) => {
      requests.push(options);
      if (requests.length === 1) {
        const error = new Error('request:fail timeout');
        error.statusCode = 0;
        error.networkError = true;
        throw error;
      }
      return {
        order: {
          outbound_order_id: 'order-1',
          outbound_order_no: 'OUT-1',
          status: 'LOADED',
        },
        summary: {},
        tasks: [],
      };
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: (options) => {
      if (options.success && options.title.includes('确认整单装车')) {
        options.success({ confirm: true, content: '沪A12345' });
      }
    },
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/index.js'));
  page.data.session = session;
  page.data.selectedOrder = {
    order: {
      outbound_order_id: 'order-1',
      outbound_order_no: 'OUT-1',
      status: 'OUTBOUND',
    },
    tasks: [],
  };
  page.loadOrders = async () => {};

  page.confirmLoading();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(requests.length, 1);
  assert.equal(page.data.loadingUncertain, true);

  page.confirmLoading();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(requests.length, 2);
  assert.equal(
    requests[1].data.idempotency_key,
    requests[0].data.idempotency_key,
  );
  assert.equal(page.data.loadingUncertain, false);
});

test('receiving success cannot recreate its draft when the page closes', async () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({
      ticket: { item_name: '船用支架', received_quantity: '2.000' },
      receipt: { location_path_snapshot: 'A 区 / A1' },
    }),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = 'receipt:receiver:1';
  page.data.selectedManifestId = 'manifest-1';
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件';

  await page.confirmReceipt();
  page.onHide();
  page.onUnload();

  const workflow = require('../utils/workflow');
  assert.equal(workflow.readDraft(page.draftScope), null);
  assert.equal(storage.size, 0);
});

test('receiving conflict clears the old draft and refreshes the task list', async () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      const error = new Error('receipt state changed');
      error.statusCode = 409;
      throw error;
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = 'receipt:receiver:1';
  page.data.selectedManifestId = 'manifest-1';
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件';
  let refreshes = 0;
  page.loadManifests = async () => {
    refreshes += 1;
  };

  await page.confirmReceipt();

  assert.equal(refreshes, 1);
  assert.equal(page.data.submitUncertain, false);
  assert.equal(storage.size, 0);
});

test('outbound success cannot recreate its draft when the page closes', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({
      confirmation: {
        quantity: '2.000',
        location_path_snapshot: 'A 区 / A1',
      },
      order: { status: 'OUTBOUND' },
    }),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
    navigateBack: () => {},
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.taskId = 'task-1';
  page.data.orderId = 'order-1';
  page.draftScope = 'outbound:outbound:task-1';
  page.data.task = {
    task_id: 'task-1',
    status: 'PENDING',
    expected_tag_suffixes: ['1-1'],
    expected_pallet_count: 1,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];

  await page.confirmOutbound();
  page.onHide();
  page.onUnload();

  const workflow = require('../utils/workflow');
  assert.equal(workflow.readDraft(page.draftScope), null);
  assert.equal(storage.size, 0);
});

test('outbound conflict clears the old draft and refreshes the task', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      const error = new Error('outbound state changed');
      error.statusCode = 409;
      throw error;
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.taskId = 'task-1';
  page.data.orderId = 'order-1';
  page.draftScope = 'outbound:outbound:task-1';
  page.data.task = {
    task_id: 'task-1',
    status: 'PENDING',
    expected_tag_suffixes: ['1-1'],
    expected_pallet_count: 1,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  let refreshes = 0;
  page.loadTask = async () => {
    refreshes += 1;
  };

  await page.confirmOutbound();

  assert.equal(refreshes, 1);
  assert.equal(page.data.submitUncertain, false);
  assert.equal(storage.size, 0);
});

test('loading conflict clears its key and refreshes the order list', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      const error = new Error('order state changed');
      error.statusCode = 409;
      throw error;
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: (options) => {
      if (options.success && options.title.includes('确认整单装车')) {
        options.success({ confirm: true, content: '' });
      }
    },
  };
  const storage = installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/index.js'));
  page.data.session = session;
  page.data.selectedOrder = {
    order: {
      outbound_order_id: 'order-1',
      outbound_order_no: 'OUT-1',
      status: 'OUTBOUND',
    },
    tasks: [],
  };
  let refreshes = 0;
  page.loadOrders = async () => {
    refreshes += 1;
  };

  page.confirmLoading();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(refreshes, 1);
  assert.equal(page.data.loadingUncertain, false);
  assert.equal(storage.size, 0);
});

test('receiving persists an in-flight marker before awaiting the response', async () => {
  let releaseRequest;
  let markStarted;
  const started = new Promise((resolve) => {
    markStarted = resolve;
  });
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      markStarted();
      return new Promise((resolve) => {
        releaseRequest = resolve;
      });
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.draftScope = 'receipt:receiver:1';
  page.data.selectedManifestId = 'manifest-1';
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件';

  const confirmation = page.confirmReceipt();
  await started;

  const workflow = require('../utils/workflow');
  assert.equal(
    workflow.readDraft(page.draftScope).submission_pending,
    true,
  );

  releaseRequest({
    ticket: { item_name: '船用支架', received_quantity: '2.000' },
    receipt: { location_path_snapshot: 'A 区 / A1' },
  });
  await confirmation;
  assert.equal(workflow.readDraft(page.draftScope), null);
});

test('outbound persists an in-flight marker before awaiting the response', async () => {
  let releaseRequest;
  let markStarted;
  const started = new Promise((resolve) => {
    markStarted = resolve;
  });
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      markStarted();
      return new Promise((resolve) => {
        releaseRequest = resolve;
      });
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
    navigateBack: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.taskId = 'task-1';
  page.data.orderId = 'order-1';
  page.draftScope = 'outbound:outbound:task-1';
  page.data.task = {
    task_id: 'task-1',
    status: 'PENDING',
    expected_tag_suffixes: ['1-1'],
    expected_pallet_count: 1,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];

  const confirmation = page.confirmOutbound();
  await started;

  const workflow = require('../utils/workflow');
  assert.equal(
    workflow.readDraft(page.draftScope).submission_pending,
    true,
  );

  releaseRequest({
    confirmation: {
      quantity: '2.000',
      location_path_snapshot: 'A 区 / A1',
    },
    order: { status: 'OUTBOUND' },
  });
  await confirmation;
  assert.equal(workflow.readDraft(page.draftScope), null);
});

test('loading restores the uncertain state from its persisted key', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({
      order: {
        outbound_order_id: 'order-1',
        outbound_order_no: 'OUT-1',
        status: 'OUTBOUND',
      },
      summary: {},
      tasks: [],
    }),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };
  installMemoryStorage(global.wx);

  const page = instantiate(loadPage('pages/outbound/index.js'));
  page.data.session = session;
  const workflow = require('../utils/workflow');
  workflow.getOrCreateSubmissionKey(
    'loading-submit:outbound:order-1',
    'loading',
  );

  await page.openOrder('order-1');

  assert.equal(page.data.loadingUncertain, true);
});
