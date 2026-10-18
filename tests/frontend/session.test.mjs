import { test } from "node:test";
import assert from "node:assert/strict";
import { WorkspaceSession } from "../../frontend/session.js";

const ID = "123e4567-e89b-12d3-a456-426614174000";
const OTHER = "123e4567-e89b-12d3-a456-426614174001";
const READY = { ready: true, busy: false, limits: { max_upload_bytes: 1000, max_video_duration_sec: 180 } };
const FILE = { name: "cash.mov", size: 10 };
const settle = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function navigation(initial = "http://local/#checks") {
  let url = initial;
  const listeners = new Set();
  const writes = [];
  return {
    read: () => url,
    write(value, replace) { url = value; writes.push({ url, replace }); },
    listen(callback) { listeners.add(callback); return () => listeners.delete(callback); },
    visit(value) { url = value; for (const callback of listeners) callback(); },
    writes,
  };
}

function scheduler() {
  const tasks = new Map();
  const delays = [];
  let nextId = 1;
  return {
    setTimeout(callback, delay) { const id = nextId++; tasks.set(id, callback); delays.push(delay); return id; },
    clearTimeout(id) { tasks.delete(id); },
    async tick() { const batch = [...tasks.values()]; tasks.clear(); for (const callback of batch) await callback(); await settle(); },
    get pending() { return tasks.size; },
    delays,
  };
}

function setup(t, overrides = {}, url) {
  const calls = { system: 0, jobs: [], starts: 0, history: [] };
  const api = {
    async getSystem() { calls.system++; return READY; },
    async getJob(id) { calls.jobs.push(id); return { job_id: id, status: "done", violations: [] }; },
    async startAnalysis() { calls.starts++; return { job_id: ID, status: "processing" }; },
    async getJobs(key, filters) { calls.history.push({ key, filters }); return { items: [], total: 0 }; },
    async getSource() { return new Blob(["video"]); },
    async getRules() { return { items: [] }; },
    async getLegalGuide() { return { version: "1", procedures: [] }; },
    ...overrides,
  };
  const nav = navigation(url);
  const clock = scheduler();
  const session = new WorkspaceSession({ api, navigation: nav, scheduler: clock });
  t.after(() => session.dispose());
  return { session, calls, nav, clock };
}

test("explicit experimental readiness uploads to review while strict readiness stays false", async t => {
  let reviews = 0;
  const { session, calls } = setup(t, {
    getSystem: async () => ({ ...READY, ready: false, review: { enabled: true, ready: true } }),
    startReview: async () => { reviews++; return { job_id: ID, status: "processing" }; },
  });
  session.setKey("secret");
  session.selectFile(FILE);
  assert.equal(await session.start(5), true);
  assert.equal(reviews, 1);
  assert.equal(calls.starts, 0);
  assert.equal(session.state.system.ready, false);
});

test("unavailable experimental mode cannot silently fall back to strict analysis", async t => {
  const { session, calls } = setup(t, {
    getSystem: async () => ({ ...READY, review: { enabled: true, ready: false } }),
    startReview: () => assert.fail("unavailable review submitted"),
  });
  session.setKey("secret");
  session.selectFile(FILE);
  await assert.rejects(session.start(5), /не готов/);
  assert.equal(calls.starts, 0);
});

test("a deep link restores once after connection, including duplicate route notifications", async t => {
  const job = deferred();
  let requests = 0;
  const { session, nav } = setup(t, { getJob: () => { requests++; return job.promise; } }, `http://local/?job=${ID}#review`);
  session.setKey("secret");
  await session.connect();
  nav.visit(nav.read());
  nav.visit(nav.read());
  assert.equal(requests, 1);
  assert.equal(session.state.pendingJob, null);
  job.resolve({ job_id: ID, status: "done" });
  await settle();
  await session.connect();
  assert.equal(requests, 1);
  assert.equal(session.state.jobId, ID);
  assert.equal(session.state.status, "done");
});

test("polling updates a job while the user remains in the history page", async t => {
  let status = "processing";
  const { session, nav, clock } = setup(t, { getJob: async id => ({ job_id: id, status }) });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  session.navigate("checks");
  const before = nav.writes.length;
  status = "done";
  await clock.tick();
  assert.equal(session.state.status, "done");
  assert.equal(session.state.page, "checks");
  assert.equal(nav.read(), "http://local/#checks");
  assert.equal(nav.writes.length, before);
});

test("malformed UUIDs never request a job or survive in the URL", async t => {
  const { session, nav, calls } = setup(t, {}, "http://local/?job=%3Cscript%3E#review");
  assert.equal(session.state.pendingJob, null);
  assert.equal(new URL(nav.read()).searchParams.has("job"), false);
  session.setKey("secret");
  await session.connect();
  assert.equal(await session.openJob("../etc/passwd"), false);
  assert.match(session.state.error.message, /UUID/);
  assert.deepEqual(calls.jobs, []);
});

test("a chosen file survives connection, key changes, and a trip through other pages", async t => {
  const { session } = setup(t);
  session.navigate("new");
  session.selectFile(FILE);
  session.navigate("connection");
  session.setKey("first");
  await session.connect();
  session.navigate("checks");
  session.navigate("new");
  assert.equal(session.state.selectedFile, FILE);
  session.forget();
  session.setKey("second");
  await session.connect();
  assert.equal(session.state.selectedFile, FILE);
});

test("processing blocks starting, reset, replacement, and another job, while history remains usable", async t => {
  const { session, calls } = setup(t, { getJob: async id => ({ job_id: id, status: "processing" }) });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  assert.equal(session.reset(), false);
  assert.equal(session.selectFile(FILE), false);
  assert.equal(await session.start(), false);
  assert.equal(await session.openJob(OTHER), false);
  assert.equal(session.navigate("new"), false);
  session.navigate("checks");
  await session.history({ q: "cash" });
  assert.equal(session.state.page, "checks");
  assert.equal(session.state.jobId, ID);
  assert.equal(calls.history.length, 1);
  assert.equal(calls.starts, 0);
});

test("double submit cannot start two uploads", async t => {
  const upload = deferred();
  let count = 0;
  const { session } = setup(t, {
    startAnalysis: () => { count++; return upload.promise; },
    getJob: async id => ({ job_id: id, status: "processing" }),
  });
  session.setKey("secret");
  session.selectFile(FILE);
  const first = session.start(2);
  assert.equal(await session.start(2), false);
  await settle();
  assert.equal(count, 1);
  upload.resolve({ job_id: ID, status: "processing" });
  await first;
  assert.equal(session.state.jobId, ID);
  assert.equal(session.state.busy, true);
});

test("an old connection response cannot reconnect after forgetting the key or restore its deep link", async t => {
  const pending = deferred();
  const { session, calls } = setup(t, { getSystem: () => pending.promise }, `http://local/?job=${ID}#review`);
  session.setKey("first");
  const connection = session.connect();
  const rejected = assert.rejects(connection, { name: "AbortError" });
  session.forget();
  pending.resolve(READY);
  await rejected;
  assert.equal(session.state.connected, false);
  assert.equal(session.state.keyPresent, false);
  assert.deepEqual(calls.jobs, []);
});

test("a stale failed request cannot invalidate a newer successful connection", async t => {
  const old = deferred();
  const { session } = setup(t, { getSystem: key => key === "first" ? old.promise : Promise.resolve(READY) });
  session.setKey("first");
  const rejected = assert.rejects(session.connect(), { name: "AbortError" });
  session.setKey("second");
  await session.connect();
  old.reject(Object.assign(new Error("unauthorized"), { status: 401 }));
  await rejected;
  assert.equal(session.state.connected, true);
  assert.equal(session.state.connecting, false);
  assert.equal(session.state.error, null);
});

test("responses for history and source become unusable after the corresponding session changes", async t => {
  const history = deferred();
  const source = deferred();
  const { session } = setup(t, { getJobs: () => history.promise, getSource: () => source.promise });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  const staleSource = assert.rejects(session.source(), { name: "AbortError" });
  session.reset();
  source.resolve(new Blob(["old video"]));
  await staleSource;
  const staleHistory = assert.rejects(session.history({}), { name: "AbortError" });
  session.forget();
  history.resolve({ items: [{ job_id: ID }] });
  await staleHistory;
});

test("a missing poll response keeps the processing lock until status is known", async t => {
  let failing = false;
  const { session, clock } = setup(t, { getJob: async id => {
    if (failing) throw new Error("offline");
    return { job_id: id, status: "processing" };
  } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  failing = true;
  await clock.tick();
  assert.equal(session.state.status, "connection");
  assert.equal(session.state.busy, true);
  assert.equal(session.reset(), false);
});

test("the public snapshot and share URL contain no credential", async t => {
  const { session } = setup(t);
  session.setKey("do-not-leak-me");
  await session.connect();
  await session.openJob(ID);
  assert.equal(JSON.stringify(session.state).includes("do-not-leak-me"), false);
  assert.equal(session.jobLink().includes("do-not-leak-me"), false);
  assert.equal(session.jobLink(), `http://local/?job=${ID}#review`);
});

test("browser navigation cleans a stale job query in place and cannot create a Back loop", async t => {
  const { session, nav } = setup(t);
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  nav.visit(`http://local/?job=${ID}#checks`);
  assert.equal(session.state.page, "checks");
  assert.deepEqual(nav.writes.at(-1), { url: "http://local/#checks", replace: true });
  const count = nav.writes.length;
  nav.visit(nav.read());
  assert.equal(nav.writes.length, count);
});

test("an unauthorized response clears protected data and cancels other requests in the same session", async t => {
  const history = deferred();
  const { session } = setup(t, {
    getJobs: () => history.promise,
    getRules: async () => { throw Object.assign(new Error("invalid key"), { status: 401 }); },
  });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  const staleHistory = assert.rejects(session.history({}), { name: "AbortError" });
  await assert.rejects(session.rules(), { status: 401 });
  history.resolve({ items: [{ job_id: ID }] });
  await staleHistory;
  assert.equal(session.state.connected, false);
  assert.equal(session.state.job, null);
  assert.equal(session.state.jobId, null);
  assert.equal(session.state.pendingJob, ID);
});

test("the reference guide remains available when the model and executable rules are not ready", async t => {
  const expected = { version: "1", procedures: [] };
  const { session } = setup(t, {
    getSystem: async () => ({ ...READY, ready: false }),
    getLegalGuide: async (key, signal) => { assert.equal(key, "secret"); assert.equal(signal.aborted, false); return expected; },
  });
  session.setKey("secret");
  await session.connect();
  assert.equal(session.state.system.ready, false);
  assert.deepEqual(await session.legalGuide(), expected);
});

test("a legal guide response from a forgotten session cannot be used after reconnecting", async t => {
  const old = deferred();
  const { session } = setup(t, { getLegalGuide: () => old.promise });
  session.setKey("first");
  await session.connect();
  const stale = assert.rejects(session.legalGuide(), { name: "AbortError" });
  session.forget();
  session.setKey("second");
  await session.connect();
  old.resolve({ version: "old", procedures: [] });
  await stale;
  assert.equal(session.state.connected, true);
});

test("an unauthorized guide request clears protected results through the shared session guard", async t => {
  const { session } = setup(t, { getLegalGuide: async () => { throw Object.assign(new Error("invalid key"), { status: 401 }); } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  await assert.rejects(session.legalGuide(), { status: 401 });
  assert.equal(session.state.connected, false);
  assert.equal(session.state.job, null);
  assert.equal(session.state.pendingJob, ID);
});

test("a transient poll failure recovers automatically without navigating or duplicating the request", async t => {
  let requests = 0;
  const { session, clock } = setup(t, { getJob: async id => {
    requests++;
    if (requests === 2) throw Object.assign(new Error("offline"), { code: "network_error", status: 0 });
    return { job_id: id, status: requests === 1 ? "processing" : "done" };
  } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  session.navigate("checks");
  await clock.tick();
  assert.equal(session.state.busy, true);
  assert.equal(session.state.pollRetry.attempt, 1);
  assert.equal(clock.delays.at(-1), 1000);
  await clock.tick();
  assert.equal(requests, 3);
  assert.equal(session.state.status, "done");
  assert.equal(session.state.busy, false);
  assert.equal(session.state.error, null);
  assert.equal(session.state.pollRetry, null);
  assert.equal(session.state.page, "checks");
  assert.equal(clock.pending, 0);
});

test("poll retries have a finite backoff budget and a manual refresh starts a fresh budget", async t => {
  let requests = 0;
  const { session, clock } = setup(t, { getJob: async id => {
    requests++;
    if (requests === 1) return { job_id: id, status: "processing" };
    throw Object.assign(new Error("gateway unavailable"), { status: 502 });
  } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  for (let i = 0; i < 5; i++) await clock.tick();
  assert.equal(requests, 6);
  assert.deepEqual(clock.delays.slice(1), [1000, 2000, 4000, 8000]);
  assert.equal(clock.pending, 0);
  assert.equal(session.state.pollRetry.exhausted, true);
  assert.equal(session.state.busy, true);
  await session.refreshJob();
  assert.equal(requests, 7);
  assert.equal(session.state.pollRetry.attempt, 1);
  assert.equal(session.state.pollRetry.exhausted, false);
  assert.equal(clock.pending, 1);
});

test("an active automatic retry shows request progress and cannot be duplicated or reset mid-flight", async t => {
  let requests = 0;
  const pending = deferred();
  const { session, clock } = setup(t, { getJob: async () => {
    requests++;
    if (requests === 1) throw Object.assign(new Error("offline"), { status: 0 });
    return pending.promise;
  } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  const automatic = clock.tick();
  await settle();
  assert.equal(session.state.pollRetry.delayMs, 0);
  assert.equal(session.state.pollRetry.attempt, 1);
  const manual = session.refreshJob();
  assert.equal(session.refreshJob(), manual);
  assert.equal(requests, 2);
  pending.reject(Object.assign(new Error("still offline"), { status: 0 }));
  await Promise.all([automatic, manual]);
  assert.equal(session.state.pollRetry.attempt, 2);
  assert.equal(session.state.pollRetry.delayMs, 2000);
});

test("an authoritative missing job is not retried", async t => {
  const { session, clock } = setup(t, { getJob: async () => { throw Object.assign(new Error("missing"), { status: 404 }); } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  assert.equal(clock.pending, 0);
  assert.equal(session.state.pollRetry, null);
  assert.equal(session.state.busy, false);
});

test("opening the same unavailable job retries its status without creating a new analysis", async t => {
  let requests = 0;
  const { session, calls, clock } = setup(t, { getJob: async id => {
    requests++;
    if (requests === 1) throw Object.assign(new Error("offline"), { status: 0 });
    return { job_id: id, status: "done", violations: [] };
  } });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  assert.equal(session.state.status, "connection");
  assert.equal(clock.pending, 1);
  session.navigate("checks");
  await session.openJob(ID);
  assert.equal(requests, 2);
  assert.equal(session.state.status, "done");
  assert.equal(session.state.page, "review");
  assert.equal(clock.pending, 0);
  assert.equal(calls.starts, 0);
});

test("forgetting, changing a job, or disposing cancels scheduled status retries", async t => {
  for (const cancel of [session => session.forget(), session => session.openJob(OTHER), session => session.dispose()]) {
    let requests = 0;
    const { session, clock } = setup(t, { getJob: async id => {
      requests++;
      if (id === OTHER) return { job_id: id, status: "done" };
      throw Object.assign(new Error("offline"), { status: 0 });
    } });
    session.setKey("secret");
    await session.connect();
    await session.openJob(ID);
    assert.equal(clock.pending, 1);
    await cancel(session);
    const before = requests;
    await clock.tick();
    assert.equal(requests, before);
    assert.equal(clock.pending, 0);
  }
});

test("an unauthorized parallel request cancels a pending poll retry and clears its notice", async t => {
  let requests = 0;
  const { session, clock } = setup(t, {
    getJob: async () => { requests++; throw Object.assign(new Error("offline"), { status: 0 }); },
    getRules: async () => { throw Object.assign(new Error("invalid key"), { status: 401 }); },
  });
  session.setKey("secret");
  await session.connect();
  await session.openJob(ID);
  await assert.rejects(session.rules(), { status: 401 });
  await clock.tick();
  assert.equal(requests, 1);
  assert.equal(session.state.pollRetry, null);
  assert.equal(session.state.connected, false);
});

test("uncertain upload outcomes keep the file and direct the user to history without resubmitting", async t => {
  for (const failure of [
    Object.assign(new Error("offline"), { code: "network_error", status: 0, outcomeUnknown: true }),
    Object.assign(new Error("invalid JSON"), { code: "invalid_response", status: 202, outcomeUnknown: true }),
    Object.assign(new Error("timeout"), { code: "request_timeout", status: 0, outcomeUnknown: true }),
    Object.assign(new Error("gateway failure"), { status: 502, outcomeUnknown: true }),
  ]) {
    let requests = 0;
    const { session } = setup(t, { startAnalysis: async () => { requests++; throw failure; } });
    session.setKey("secret");
    session.selectFile(FILE);
    await assert.rejects(session.start(2), error => /журнал/.test(error.message));
    assert.equal(session.state.uploadUncertain, true);
    assert.equal(session.state.selectedFile, FILE);
    assert.equal(session.state.jobId, null);
    assert.equal(session.state.busy, false);
    assert.equal(requests, 1);
  }
});

test("a malformed upload acceptance is uncertain while a definite rejection is not", async t => {
  for (const acceptance of [null, {}, { status: "processing", job_id: "wrong" }]) {
    const { session } = setup(t, { startAnalysis: async () => acceptance });
    session.setKey("secret");
    session.selectFile(FILE);
    await assert.rejects(session.start(2), error => /журнал/.test(error.message));
    assert.equal(session.state.uploadUncertain, true);
    session.selectFile(FILE);
    assert.equal(session.state.uploadUncertain, false);
  }
  const { session } = setup(t, { startAnalysis: async () => { throw Object.assign(new Error("invalid video"), { status: 422 }); } });
  session.setKey("secret");
  session.selectFile(FILE);
  await assert.rejects(session.start(2), { status: 422 });
  assert.equal(session.state.uploadUncertain, false);
});

test("a parallel unauthorized request preserves uncertain acceptance after cancelling an active upload", async t => {
  let uploads = 0;
  let uploadAborted = false;
  const { session } = setup(t, {
    startAnalysis: (_file, _key, signal) => {
      uploads++;
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => {
        uploadAborted = true;
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true }));
    },
    getRules: async () => { throw Object.assign(new Error("invalid key"), { status: 401 }); },
  });
  session.setKey("first");
  session.selectFile(FILE);
  const upload = session.start(2);
  await settle();
  assert.equal(session.state.status, "uploading");
  await assert.rejects(session.rules(), { status: 401 });
  assert.equal(await upload, false);
  assert.equal(uploadAborted, true);
  assert.equal(uploads, 1);
  assert.equal(session.state.uploadUncertain, true);
  assert.equal(session.state.selectedFile, FILE);
  assert.equal(session.state.connected, false);
  assert.equal(session.state.job, null);
  assert.equal(session.state.jobId, null);
  session.forget();
  assert.equal(session.state.uploadUncertain, true);
  session.setKey("second");
  assert.equal(session.state.uploadUncertain, true);
  await session.connect();
  assert.equal(session.state.uploadUncertain, true);
  assert.equal(session.state.job, null);
  session.selectFile(FILE);
  assert.equal(session.state.uploadUncertain, false);
});

test("an unauthorized response from the upload itself is a definite rejection", async t => {
  const { session } = setup(t, {
    startAnalysis: async () => { throw Object.assign(new Error("invalid key"), { status: 401 }); },
  });
  session.setKey("secret");
  session.selectFile(FILE);
  assert.equal(await session.start(2), false);
  assert.equal(session.state.uploadUncertain, false);
  assert.equal(session.state.connected, false);
  assert.equal(session.state.jobId, null);
  assert.equal(session.state.error.status, 401);
});
