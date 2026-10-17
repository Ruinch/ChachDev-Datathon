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
  let nextId = 1;
  return {
    setTimeout(callback) { const id = nextId++; tasks.set(id, callback); return id; },
    clearTimeout(id) { tasks.delete(id); },
    async tick() { const batch = [...tasks.values()]; tasks.clear(); for (const callback of batch) await callback(); await settle(); },
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
    ...overrides,
  };
  const nav = navigation(url);
  const clock = scheduler();
  const session = new WorkspaceSession({ api, navigation: nav, scheduler: clock });
  t.after(() => session.dispose());
  return { session, calls, nav, clock };
}

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
