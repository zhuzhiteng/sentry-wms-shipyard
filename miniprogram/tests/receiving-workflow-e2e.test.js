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
  return {
    ...definition,
    data: JSON.parse(JSON.stringify(definition.data)),
    setData(patch) {
      this.data = { ...this.data, ...patch };
    },
  };
}

test('operator can continuously capture slips, receive one ticket, auto-advance and retain a partial ticket', async (t) => {
  const startedAt = performance.now();
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  const toasts = [];
  const scrolls = [];
  const requests = [];
  const uploads = [];
  const mediaPaths = [
    '/tmp/slip-1.png',
    '/tmp/slip-2.png',
    '/tmp/ticket-1.png',
    '/tmp/ticket-2.png',
  ];
  let arrivalSlipSequence = 0;
  let labelSequence = 0;
  const manifest = {
    manifest: {
      manifest_id: 'manifest-1',
      manifest_no: 'IN-E2E-1',
      metadata: { manufacturer_name: '测试供应商' },
    },
    arrival_slips: [],
    tickets: [
      {
        ticket_id: 'ticket-1',
        ticket_no: 'IN-1',
        item_name: '船用支架 A',
        item_spec: 'S-A',
        declared_quantity: '4.000',
        received_quantity: '0.000',
        expected_pallet_count: 1,
        status: 'EXPECTED',
        delivery_status: 'RECEIVED',
        location_path_snapshot: 'A 区 / A1',
      },
      {
        ticket_id: 'ticket-2',
        ticket_no: 'IN-2',
        item_name: '船用支架 B',
        item_spec: 'S-B',
        declared_quantity: '3.000',
        received_quantity: '0.000',
        expected_pallet_count: 2,
        status: 'EXPECTED',
        delivery_status: 'RECEIVED',
        location_path_snapshot: 'A 区 / A2',
      },
    ],
  };

  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async (options) => {
      requests.push(options);
      if (options.path === '/shipyard/inbound/manifests/manifest-1') {
        return JSON.parse(JSON.stringify(manifest));
      }
      if (options.path.endsWith('/receipts')) {
        const ticketId = options.path.split('/').at(-2);
        const ticket = manifest.tickets.find((item) => item.ticket_id === ticketId);
        if (ticketId === 'ticket-1') {
          ticket.status = 'CLOSED';
          ticket.received_quantity = '4.000';
        } else {
          ticket.status = 'PARTIAL';
          ticket.received_quantity = '1.000';
        }
        return {};
      }
      throw new Error(`unexpected request: ${options.path}`);
    },
    upload: async (options) => {
      uploads.push(options);
      if (options.path.endsWith('/arrival-slips/recognize')) {
        arrivalSlipSequence += 1;
        return {
          fields: {
            slip_no: `SLIP-${arrivalSlipSequence}`,
            delivery_note_no: `DN-${arrivalSlipSequence}`,
            supplier_name: '测试供应商',
            engineering_no: 'H-E2E',
            purchase_order_no: 'PO-E2E',
          },
          missing_labels: [],
          message: '已自动识别，请核对验收单信息。',
        };
      }
      if (options.path.endsWith('/arrival-slips')) {
        const sequence = manifest.arrival_slips.length + 1;
        manifest.arrival_slips.unshift({
          arrival_slip_id: `slip-${sequence}`,
          slip_no: options.formData.slip_no,
          delivery_note_no: options.formData.delivery_note_no,
          supplier_name: options.formData.supplier_name,
          engineering_no: options.formData.engineering_no,
          purchase_order_no: options.formData.purchase_order_no,
        });
        return { arrival_slips: manifest.arrival_slips };
      }
      if (options.path.endsWith('/label-recognition')) {
        labelSequence += 1;
        return {
          matched: true,
          needs_suffix: false,
          mismatches: [],
          recognized: {
            tag_suffix: labelSequence === 1 ? '1-1' : '2-1',
            item_name: labelSequence === 1 ? '船用支架 A' : '船用支架 B',
          },
          recognition: { recognition_id: `recognition-${labelSequence}` },
          photo: { photo_id: `photo-${labelSequence}` },
        };
      }
      throw new Error(`unexpected upload: ${options.path}`);
    },
  });

  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: mediaPaths.shift() }],
    }),
    showToast: (options) => toasts.push(options.title),
    showModal: () => {},
    previewImage: () => {},
    pageScrollTo: (options) => scrolls.push(options),
    nextTick: (callback) => callback(),
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedManifestId = 'manifest-1';

  await page.openManifest('manifest-1');
  await page.captureArrivalSlip();
  assert.equal(page.data.arrivalSlipSavedCount, 1);
  await page.captureArrivalSlip();
  assert.equal(page.data.arrivalSlipSavedCount, 2);
  assert.equal(page.data.arrivalSlipPhotoPath, '');

  page.beginTicket('ticket-1');
  assert.equal(page.data.quantity, '4');
  assert.equal(page.data.editQuantity, false);
  await page.captureLabel();
  await page.confirmReceipt();

  assert.equal(page.data.selectedTicket.ticket_id, 'ticket-2');
  assert.equal(page.data.quantity, '3');
  assert.equal(page.data.editQuantity, false);
  assert.ok(toasts.includes('已收货，继续下一票'));
  assert.ok(scrolls.some((item) => item.selector === '#ticket-workbench'));

  await page.captureLabel();
  page.toggleQuantityEdit();
  page.onQuantityInput({ detail: { value: '1' } });
  page.onDifferenceReasonInput({
    detail: { value: '少 2 件，下一批补到' },
  });
  await page.confirmReceipt();

  assert.equal(page.data.selectedTicket, null);
  assert.ok(toasts.includes('本次已记录，还需继续补收'));
  assert.equal(toasts.includes('本清单已全部收完'), false);
  assert.equal(
    requests.filter((item) => item.path.endsWith('/receipts')).length,
    2,
  );
  assert.equal(
    uploads.filter((item) => item.path.endsWith('/arrival-slips/recognize')).length,
    2,
  );
  const elapsedMs = performance.now() - startedAt;
  t.diagnostic(`page workflow logic: ${elapsedMs.toFixed(1)}ms`);
  assert.ok(elapsedMs < 1000, `page workflow took ${elapsedMs.toFixed(1)}ms`);
});
