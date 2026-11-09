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

test('receiving keeps the saved photo and asks again after an invalid pallet number', async () => {
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
  const answers = [
    { confirm: true, content: '一号托' },
    { confirm: true, content: '1-1' },
  ];
  const toasts = [];
  let promptCount = 0;
  global.wx = {
    showModal: (options) => {
      promptCount += 1;
      options.success(answers.shift());
    },
    showToast: (options) => toasts.push(options.title),
  };

  const page = instantiate(loadPage('pages/receive/index.js'));

  assert.equal(await page.askSuffix(), '1-1');
  assert.equal(promptCount, 2);
  assert.ok(toasts.includes('请按 1-1 的格式填写'));
});

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
  page.data.selectedManifestId = 'manifest-1';
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

test('receiving defaults to the remaining quantity and only asks for a shortage reason when reduced', async () => {
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

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifest = {
    tickets: [{
      ticket_id: 'ticket-1',
      declared_quantity: '4.000',
      received_quantity: '0.000',
      expected_pallet_count: 2,
      status: 'EXPECTED',
    }],
  };

  page.startTicket({
    currentTarget: { dataset: { id: 'ticket-1' } },
  });
  assert.equal(page.data.quantity, '4');
  assert.equal(page.data.showDifferenceReason, false);
  assert.equal(page.data.editQuantity, false);

  page.toggleQuantityEdit();
  assert.equal(page.data.editQuantity, true);
  page.onQuantityInput({ detail: { value: '2' } });
  assert.equal(page.data.showDifferenceReason, true);
  page.data.differenceReason = '少 2 件，余货明天补到';

  page.restoreFullQuantity();
  assert.equal(page.data.editQuantity, false);
  assert.equal(page.data.quantity, '4');
  assert.equal(page.data.showDifferenceReason, false);
  assert.equal(page.data.differenceReason, '');
});

test('receiving submits a short quantity with fewer pallets and its reason', async () => {
  const requests = [];
  const toasts = [];
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
    showToast: (options) => toasts.push(options.title),
    showModal: () => {},
    pageScrollTo: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
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
  page.data.differenceReason = '少 2 件，余货明天补到';
  page.openManifest = async () => {
    page.data.selectedManifest = {
      tickets: [
        {
          ticket_id: 'ticket-1',
          status: 'CLOSED',
          can_receive: false,
        },
        {
          ticket_id: 'ticket-2',
          status: 'EXPECTED',
          can_receive: true,
          declared_quantity: '6.000',
          received_quantity: '0.000',
          expected_pallet_count: 1,
        },
      ],
    };
    return true;
  };

  await page.confirmReceipt();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/shipyard/inbound/tickets/ticket-1/receipts');
  assert.equal(requests[0].data.received_quantity, 2);
  assert.equal(requests[0].data.pallets.length, 1);
  assert.equal(requests[0].data.pallets[0].label_recognition_id, 'recognition-1');
  assert.equal(requests[0].data.difference_reason, '少 2 件，余货明天补到');
  assert.equal(page.data.selectedTicket.ticket_id, 'ticket-2');
  assert.equal(page.data.quantity, '6');
  assert.ok(toasts.includes('已收货，继续下一票'));
});

test('a partial receipt does not claim the manifest is complete', async () => {
  const toasts = [];
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
    showToast: (options) => toasts.push(options.title),
    showModal: () => {},
    pageScrollTo: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
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
  page.data.differenceReason = '少 2 件，余货明天补到';
  page.openManifest = async () => {
    page.data.selectedManifest = {
      tickets: [{
        ticket_id: 'ticket-1',
        status: 'PARTIAL',
        can_receive: true,
        declared_quantity: '4.000',
        received_quantity: '2.000',
        expected_pallet_count: 2,
      }],
    };
    return true;
  };

  await page.confirmReceipt();

  assert.ok(toasts.includes('本次已记录，还需继续补收'));
  assert.equal(toasts.includes('本清单已全部收完'), false);
});

test('receiving can defer an undelivered ticket to the next batch', async () => {
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
          delivery_status: 'DEFERRED',
          deferred_to_date: '2026-10-30',
        },
      };
    },
    upload: async () => ({}),
  });
  const answers = [
    { confirm: true, content: '2026-10-30' },
    { confirm: true, content: '车辆装不下，随下一批送达' },
  ];
  global.wx = {
    showModal: (options) => options.success(answers.shift()),
    showToast: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';
  page.data.selectedManifest = {
    tickets: [{
      ticket_id: 'ticket-1',
      declared_quantity: '6.000',
      received_quantity: '0.000',
      status: 'EXPECTED',
      can_defer: true,
    }],
  };
  page.openManifest = async () => true;

  await page.deferTicket({
    currentTarget: { dataset: { id: 'ticket-1' } },
  });

  assert.equal(requests.length, 1);
  assert.equal(
    requests[0].path,
    '/shipyard/inbound/tickets/ticket-1/deferral',
  );
  assert.equal(requests[0].method, 'POST');
  assert.equal(requests[0].data.next_batch_date, '2026-10-30');
  assert.equal(requests[0].data.reason, '车辆装不下，随下一批送达');
});

test('receiving recognizes and retains a complete four-part arrival slip in one action', async () => {
  const uploads = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async (options) => {
      uploads.push(options);
      if (options.path.endsWith('/recognize')) {
        return {
          fields: {
            slip_no: '0920156',
            delivery_note_no: '920156',
            supplier_name: '宝应帆洋船舶电器配件制造有限公司',
            engineering_no: 'H1405',
            purchase_order_no: 'SWS202607080016',
          },
          missing_labels: [],
          message: '已自动识别，请核对验收单信息。',
        };
      }
      return { arrival_slips: [] };
    },
  });
  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/arrival-slip.jpg' }],
    }),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';
  page.data.arrivalSlipForm = {
    slip_no: '',
    delivery_note_no: '',
    supplier_name: '',
    engineering_no: '',
    purchase_order_no: '',
  };
  page.openManifest = async () => true;

  await page.captureArrivalSlip();
  assert.equal(uploads.length, 2);
  assert.equal(
    uploads[0].path,
    '/shipyard/inbound/manifests/manifest-1/arrival-slips/recognize',
  );
  assert.equal(page.data.arrivalSlipExpanded, false);
  assert.equal(page.data.arrivalSlipPhotoPath, '');

  assert.equal(
    uploads[1].path,
    '/shipyard/inbound/manifests/manifest-1/arrival-slips',
  );
  assert.equal(uploads[1].formData.slip_no, '0920156');
  assert.equal(uploads[1].formData.factory_copy_count, '2');
  assert.equal(uploads[1].formData.shipyard_copy_count, '2');
  assert.equal(uploads[1].formData.warehouse_copy_retained, 'true');
});

test('receiving keeps an incomplete arrival slip editable instead of blocking the user', async () => {
  const uploads = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async (options) => {
      uploads.push(options);
      return {
        fields: { slip_no: '0920156' },
        missing_labels: ['送货单号', '供应商'],
        message: '未能完整识别，请补充验收单信息。',
      };
    },
  });
  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/arrival-slip.jpg' }],
    }),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';
  page.data.arrivalSlipForm = {
    slip_no: '',
    delivery_note_no: '',
    supplier_name: '',
    engineering_no: '',
    purchase_order_no: '',
  };

  await page.captureArrivalSlip();

  assert.equal(uploads.length, 1);
  assert.equal(page.data.arrivalSlipPhotoPath, '/tmp/arrival-slip.jpg');
  assert.equal(page.data.arrivalSlipExpanded, true);
  assert.equal(page.data.arrivalSlipForm.slip_no, '0920156');
  assert.match(page.data.arrivalSlipMessage, /送货单号/);
  assert.match(page.data.arrivalSlipMessage, /供应商/);
});

test('switching manifests clears the previous arrival-slip fields', async () => {
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({
      manifest: {
        manifest_id: 'manifest-2',
        manifest_no: 'IN-2',
        metadata: { manufacturer_name: '新供应商' },
      },
      tickets: [],
      arrival_slips: [],
    }),
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';
  page.data.arrivalSlipForm = {
    slip_no: 'OLD-SLIP',
    delivery_note_no: 'OLD-DELIVERY',
    supplier_name: '旧供应商',
    engineering_no: 'H-OLD',
    purchase_order_no: 'PO-OLD',
  };

  await page.openManifest('manifest-2');

  assert.equal(page.data.arrivalSlipForm.slip_no, '');
  assert.equal(page.data.arrivalSlipForm.delivery_note_no, '');
  assert.equal(page.data.arrivalSlipForm.engineering_no, '');
  assert.equal(page.data.arrivalSlipForm.purchase_order_no, '');
  assert.equal(page.data.arrivalSlipForm.supplier_name, '新供应商');
});

test('a saved arrival slip can be corrected without replacing its photo', async () => {
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
      return { arrival_slips: [] };
    },
    upload: async () => ({}),
  });
  global.wx = {
    showToast: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';
  page.data.selectedManifest = {
    arrival_slips: [{
      arrival_slip_id: 'slip-1',
      slip_no: 'WRONG',
      delivery_note_no: '920156',
      supplier_name: '供应商',
      engineering_no: 'H1405',
      purchase_order_no: 'PO-1',
    }],
  };
  page.openManifest = async () => true;

  page.editSavedArrivalSlip({
    currentTarget: { dataset: { id: 'slip-1' } },
  });
  page.data.arrivalSlipForm.slip_no = 'CORRECTED';
  await page.saveArrivalSlip();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, 'PUT');
  assert.equal(
    requests[0].path,
    '/shipyard/inbound/arrival-slips/slip-1',
  );
  assert.equal(requests[0].data.slip_no, 'CORRECTED');
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
  page.openManifest = async () => true;
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
