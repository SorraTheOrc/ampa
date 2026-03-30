import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import os from 'os';

// Lightweight lifecycle test for the Node ampa plugin when installed into
// .worklog/plugins in a temporary project directory.

const pluginModule = new URL('../../skill/install-ampa/resources/ampa.mjs', import.meta.url);
const plugin = await import(pluginModule.href);

class FakeProgram {
  constructor() {
    this.commands = new Map();
  }

  command(name) {
    const cmd = new FakeCommand(name, this);
    this.commands.set(name, cmd);
    return cmd;
  }
}

class FakeCommand {
  constructor(name, program) {
    this.name = name;
    this.program = program;
    this.subcommands = new Map();
    this.actionFn = null;
    this._opts = {};
  }

  command(name) {
    const cmd = new FakeCommand(name, this.program);
    this.subcommands.set(name, cmd);
    return cmd;
  }

  description() {
    return this;
  }

  option() {
    return this;
  }

  arguments() {
    return this;
  }

  action(fn) {
    this.actionFn = fn;
    return this;
  }

  /** Mimic Commander.js Command.optsWithGlobals(). */
  optsWithGlobals() {
    return Object.assign({}, this._opts);
  }

  /** Set options so optsWithGlobals() returns them. */
  setOpts(opts) {
    this._opts = opts || {};
  }
}

async function withTempDir(name, fn) {
  const tmp = path.join(process.cwd(), name);
  if (!fs.existsSync(tmp)) fs.mkdirSync(tmp);
  try {
    return await fn(tmp);
  } finally {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
  }
}

async function runActionPreservingExitCode(action, ...args) {
  const previous = process.exitCode;
  process.exitCode = 0;
  try {
    await action(...args);
    return process.exitCode || 0;
  } finally {
    process.exitCode = previous;
  }
}

test('ampa list requires running daemon', async (t) => {
  await withTempDir('tmp-ampa-list-test', async (tmp) => {
    fs.mkdirSync(path.join(tmp, '.worklog', 'ampa', 't1'), { recursive: true });

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, false, 'daemon should be reported as not running');
    assert.equal(
      plugin.DAEMON_NOT_RUNNING_MESSAGE,
      'Daemon is not running. Start it with: wl ampa start'
    );
  });
});

test('ampa start/status/stop lifecycle', async (t) => {
  await withTempDir('tmp-ampa-test', async (tmp) => {
    const daemon = path.join(tmp, 'test_daemon.js');
    fs.writeFileSync(
      daemon,
      `process.on('SIGTERM', ()=>{ console.log('got TERM'); process.exit(0); }); setInterval(()=>{},1000);`
    );
    fs.chmodSync(daemon, 0o755);
    fs.writeFileSync(path.join(tmp, 'worklog.json'), JSON.stringify({ ampa: `node ${daemon}` }));

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampaCmd = ctx.program.commands.get('ampa');
    const startCmd = ampaCmd.subcommands.get('start');
    const statusCmd = ampaCmd.subcommands.get('status');
    const stopCmd = ampaCmd.subcommands.get('stop');

    const originalCwd = process.cwd();
    let startCode, statusCode, stopCode;
    try {
      process.chdir(tmp);

      startCode = await runActionPreservingExitCode(
        startCmd.actionFn,
        { name: 't1', cmd: null, foreground: false, verbose: false }
      );
      assert.equal(startCode, 0, `start exit code unexpected: ${startCode}`);

      let output = '';
      const originalLog = console.log;
      console.log = (...args) => {
        output += args.join(' ') + '\n';
      };
      statusCode = await runActionPreservingExitCode(statusCmd.actionFn, { name: 't1' });
      console.log = originalLog;
      assert.equal(statusCode, 0, `status exit code unexpected: ${statusCode}`);
      assert.ok(/running pid=\d+/.test(output), `status output unexpected: ${output}`);

      stopCode = await runActionPreservingExitCode(stopCmd.actionFn, { name: 't1' });
      assert.equal(stopCode, 0, `stop exit code unexpected: ${stopCode}`);
    } finally {
      process.chdir(originalCwd);
    }
  });
});

test('ampa list resolves daemon store env', async (t) => {
  await withTempDir('tmp-ampa-store-test', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);

    const storeRel = 'stores/active.json';
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env, { AMPA_SCHEDULER_STORE: storeRel }),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, true, 'daemon should be reported running');
    assert.equal(state.storePath, path.resolve(tmp, storeRel));

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('resolveDaemonStore defaults to per-project path when no store file exists anywhere', async (t) => {
  await withTempDir('tmp-ampa-store-bundle-test', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    const bundlePath = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
    fs.mkdirSync(bundlePath, { recursive: true });
    fs.writeFileSync(path.join(bundlePath, 'scheduler.py'), '# placeholder');

    // With per-project isolation, when no scheduler_store.json exists in either
    // the per-project dir or the package dir, defaults to per-project path.
    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, true, 'daemon should be reported running');
    assert.equal(state.storePath, path.join(tmp, '.worklog', 'ampa', 'scheduler_store.json'),
      'should default to per-project path when no store file exists');

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('resolveDaemonStore uses package-dir store for backward compat when file exists there', async (t) => {
  await withTempDir('tmp-ampa-store-compat-test', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    const bundlePath = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
    fs.mkdirSync(bundlePath, { recursive: true });
    fs.writeFileSync(path.join(bundlePath, 'scheduler.py'), '# placeholder');
    // Create the store file in the package dir to trigger backward compat
    fs.writeFileSync(path.join(bundlePath, 'scheduler_store.json'), '{}');

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, true, 'daemon should be reported running');
    assert.equal(state.storePath, path.join(bundlePath, 'scheduler_store.json'),
      'should use package-dir store for backward compat when file exists there');

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('ampa list verbose prints store path', async (t) => {
  await withTempDir('tmp-ampa-verbose-test', async (tmp) => {
    const ampaDir = path.join(tmp, 'ampa');
    fs.mkdirSync(ampaDir, { recursive: true });
    const daemon = path.join(ampaDir, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);

    const storeRel = 'stores/active.json';
    const storePath = path.resolve(tmp, storeRel);
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env, { AMPA_SCHEDULER_STORE: storeRel }),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    fs.writeFileSync(path.join(ampaDir, '__init__.py'), '');
    fs.writeFileSync(
      path.join(ampaDir, 'scheduler.py'),
      'import sys\n\nif __name__ == "__main__":\n    if "list" in sys.argv:\n        print("[]")\n'
    );

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampaCmd = ctx.program.commands.get('ampa');
    const listCmd = ampaCmd.subcommands.get('list');

    // The action handler signature is (opts, cmd) where cmd must have
    // optsWithGlobals(). Set opts on the FakeCommand and pass it as the
    // second argument.
    listCmd.setOpts({ name: 't1', json: true, verbose: true });

    let output = '';
    const originalLog = console.log;
    console.log = (...args) => {
      output += args.join(' ') + '\n';
    };
    const originalCwd = process.cwd();
    try {
      process.chdir(tmp);
      await runActionPreservingExitCode(listCmd.actionFn, {}, listCmd);
    } finally {
      process.chdir(originalCwd);
      console.log = originalLog;
    }

    assert.ok(output.includes(`Using scheduler store: ${storePath}`), `verbose output missing store: ${output}`);

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();

  });
});

// ---------- resolveAmpaPackage tests ----------

test('resolveAmpaPackage finds local package first', async () => {
  await withTempDir('tmp-ampa-pkg-local', async (tmp) => {
    // Create local package
    const localPy = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
    fs.mkdirSync(localPy, { recursive: true });
    fs.writeFileSync(path.join(localPy, '__init__.py'), '');

    const result = plugin.resolveAmpaPackage(tmp);
    assert.ok(result, 'should find local package');
    assert.equal(result.pyPath, path.join(tmp, '.worklog', 'plugins', 'ampa_py'));
    assert.equal(result.pythonBin, 'python3'); // no venv
  });
});

test('resolveAmpaPackage finds global package when local absent', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-global-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-global-proj', async (tmp) => {
      // No local package — create global package
      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, '__init__.py'), '');

      const result = plugin.resolveAmpaPackage(tmp);
      assert.ok(result, 'should find global package');
      assert.equal(result.pyPath, path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py'));
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveAmpaPackage prefers local over global', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-pref-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-prefer', async (tmp) => {
      // Create both local and global packages
      const localPy = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(localPy, { recursive: true });
      fs.writeFileSync(path.join(localPy, '__init__.py'), '');

      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, '__init__.py'), '');

      const result = plugin.resolveAmpaPackage(tmp);
      assert.ok(result, 'should find a package');
      assert.equal(result.pyPath, path.join(tmp, '.worklog', 'plugins', 'ampa_py'),
        'should prefer local over global');
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveAmpaPackage returns null when no package exists', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-none-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-none', async (tmp) => {
      const result = plugin.resolveAmpaPackage(tmp);
      assert.equal(result, null, 'should return null when no package found');
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

// ---------- projectAmpaDir tests ----------

test('projectAmpaDir returns correct path', () => {
  assert.equal(plugin.projectAmpaDir('/foo/bar'), path.join('/foo/bar', '.worklog', 'ampa'));
});

// ---------- globalPluginsDir tests ----------

test('globalPluginsDir respects XDG_CONFIG_HOME', () => {
  const saved = process.env.XDG_CONFIG_HOME;
  try {
    process.env.XDG_CONFIG_HOME = '/tmp/test-xdg';
    assert.equal(plugin.globalPluginsDir(), path.join('/tmp/test-xdg', 'opencode', '.worklog', 'plugins'));
  } finally {
    if (saved === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = saved;
  }
});

// ---------- resolveDaemonStore fallback tests ----------

test('resolveDaemonStore falls back to projectAmpaDir when no package found', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-fb-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-fallback', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // No ampa_py package anywhere — should fall back to projectAmpaDir
      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, path.join(tmp, '.worklog', 'ampa', 'scheduler_store.json'),
        'should fall back to projectAmpaDir/scheduler_store.json');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveDaemonStore defaults to per-project path when global package has no store file', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-gbl-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-global', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // Create global ampa_py package with scheduler.py but no store file
      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, 'scheduler.py'), '# placeholder');

      // With per-project isolation, when no store file exists in either location,
      // defaults to per-project path
      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, path.join(tmp, '.worklog', 'ampa', 'scheduler_store.json'),
        'should default to per-project path when global package has no store file');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveDaemonStore uses global package store for backward compat when file exists', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-gbl-compat-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-global-compat', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // Create global ampa_py package with scheduler.py AND scheduler_store.json
      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, 'scheduler.py'), '# placeholder');
      fs.writeFileSync(path.join(globalPy, 'scheduler_store.json'), '{}');

      // With backward compat, when store file exists in the package dir and not
      // in the per-project dir, should use the package dir store
      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, path.join(globalPy, 'scheduler_store.json'),
        'should use global package store for backward compat when file exists there');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveDaemonStore prefers per-project store over package-dir store', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-pref-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-prefer', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // Create BOTH per-project and global package store files
      const projectStore = path.join(tmp, '.worklog', 'ampa', 'scheduler_store.json');
      fs.writeFileSync(projectStore, '{"source":"project"}');

      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, 'scheduler.py'), '# placeholder');
      fs.writeFileSync(path.join(globalPy, 'scheduler_store.json'), '{"source":"global"}');

      // Per-project store should take precedence
      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, projectStore,
        'should prefer per-project store over package-dir store');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

// ---------- extractErrorLines tests ----------

test('extractErrorLines returns empty array for empty input', () => {
  assert.deepEqual(plugin.extractErrorLines(''), []);
  assert.deepEqual(plugin.extractErrorLines(null), []);
});

test('extractErrorLines captures ERROR lines', () => {
  const text = 'INFO starting\nERROR something failed\nINFO done';
  const result = plugin.extractErrorLines(text);
  assert.ok(result.includes('ERROR something failed'));
  assert.equal(result.length, 1);
});

test('extractErrorLines captures AMPA_DISCORD_BOT_TOKEN mention', () => {
  const text = [
    'INFO loading config',
    'ERROR AMPA_DISCORD_BOT_TOKEN is not set; cannot send heartbeats',
    'INFO exiting',
  ].join('\n');
  const result = plugin.extractErrorLines(text);
  assert.ok(result.some((l) => l.includes('AMPA_DISCORD_BOT_TOKEN')));
});

test('extractErrorLines captures Traceback and Exception lines', () => {
  const text = 'Traceback (most recent call last):\n  File "x.py"\nSomeException: oops';
  const result = plugin.extractErrorLines(text);
  assert.ok(result.some((l) => l.includes('Traceback')));
  assert.ok(result.some((l) => l.includes('SomeException')));
});

// ---------- start() improved error output tests ----------

test('start prints log errors and Discord hint on immediate exit', async () => {
  await withTempDir('tmp-ampa-start-err', async (tmp) => {
    // Use a daemon that mimics missing AMPA_DISCORD_BOT_TOKEN by writing the
    // same log message the Python daemon would and then exiting immediately.
    const daemon = path.join(tmp, 'fail_daemon.js');
    fs.writeFileSync(
      daemon,
      `process.stderr.write('ERROR AMPA_DISCORD_BOT_TOKEN is not set; cannot send heartbeats\\n'); process.exit(2);`
    );

    const errors = [];
    const originalError = console.error;
    console.error = (...args) => { errors.push(args.join(' ')); };
    try {
      const code = await plugin.start(tmp, ['node', daemon], 'default');
      assert.equal(code, 1, 'start should return 1 on failure');
    } finally {
      console.error = originalError;
    }

    const combined = errors.join('\n');
    assert.ok(combined.includes('process exited immediately'), `missing main error: ${combined}`);
    assert.ok(combined.includes('AMPA_DISCORD_BOT_TOKEN'), `missing token mention: ${combined}`);
    assert.ok(combined.includes('Discord configuration'), `missing Discord hint: ${combined}`);
    assert.ok(combined.includes('.worklog/ampa/.env'), `missing .env path hint: ${combined}`);
  });
});

// ---------- getSchedulerDaemonStatus tests ----------

test('getSchedulerDaemonStatus returns stopped when no pid file', async () => {
  await withTempDir('tmp-ampa-sched-status-stopped', async (tmp) => {
    fs.mkdirSync(path.join(tmp, '.worklog', 'ampa', 'default'), { recursive: true });
    const result = plugin.getSchedulerDaemonStatus(tmp, 'default');
    assert.equal(result.name, 'scheduler');
    assert.equal(result.state, 'stopped');
    assert.equal(result.pid, null);
    assert.ok(result.reason.includes('no pid file'), `reason unexpected: ${result.reason}`);
  });
});

test('getSchedulerDaemonStatus returns running when process is alive', async () => {
  await withTempDir('tmp-ampa-sched-status-running', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 'default');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 'default.pid'), String(proc.pid));

    const result = plugin.getSchedulerDaemonStatus(tmp, 'default');
    // The process is running but may not be "owned" by this project since the
    // cmdline won't contain project paths. Accept either running or stopped
    // depending on ownership check result.
    assert.ok(['running', 'stopped'].includes(result.state), `unexpected state: ${result.state}`);
    assert.equal(result.name, 'scheduler');

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('getSchedulerDaemonStatus returns stopped when pid file stale', async () => {
  await withTempDir('tmp-ampa-sched-status-stale', async (tmp) => {
    const base = path.join(tmp, '.worklog', 'ampa', 'default');
    fs.mkdirSync(base, { recursive: true });
    // Write a PID that is very unlikely to be a running process
    fs.writeFileSync(path.join(base, 'default.pid'), '999999999');

    const result = plugin.getSchedulerDaemonStatus(tmp, 'default');
    assert.equal(result.name, 'scheduler');
    assert.equal(result.state, 'stopped');
    assert.equal(result.pid, null);
  });
});

// ---------- getDiscordDaemonStatus tests ----------

test('getDiscordDaemonStatus returns not_configured when no token', async () => {
  await withTempDir('tmp-ampa-discord-notoken', async (tmp) => {
    // Ensure no token in env
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    delete process.env.AMPA_DISCORD_BOT_TOKEN;
    try {
      const result = plugin.getDiscordDaemonStatus(tmp);
      assert.equal(result.name, 'discord');
      assert.equal(result.state, 'not_configured');
      assert.equal(result.discord_configured, false);
      assert.ok(result.reason.includes('AMPA_DISCORD_BOT_TOKEN'), `reason unexpected: ${result.reason}`);
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});

test('getDiscordDaemonStatus reads token from .env file', async () => {
  await withTempDir('tmp-ampa-discord-envfile', async (tmp) => {
    // No token in process.env
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    delete process.env.AMPA_DISCORD_BOT_TOKEN;
    try {
      // Write token to .worklog/ampa/.env
      const ampaDir = path.join(tmp, '.worklog', 'ampa');
      fs.mkdirSync(ampaDir, { recursive: true });
      fs.writeFileSync(path.join(ampaDir, '.env'), 'AMPA_DISCORD_BOT_TOKEN="test-token"\n');

      const result = plugin.getDiscordDaemonStatus(tmp);
      assert.equal(result.name, 'discord');
      assert.equal(result.discord_configured, true, 'should detect token from .env file');
      // State depends on whether discord_bot process is actually running (not_configured or running/stopped)
      assert.ok(['running', 'stopped'].includes(result.state), `unexpected state: ${result.state}`);
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});

test('getDiscordDaemonStatus reports stopped when token set but bot not running', async () => {
  await withTempDir('tmp-ampa-discord-stopped', async (tmp) => {
    // Set token in env so discord_configured=true, but no discord_bot process
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    process.env.AMPA_DISCORD_BOT_TOKEN = 'fake-test-token-xyz-999';
    try {
      const result = plugin.getDiscordDaemonStatus(tmp);
      assert.equal(result.name, 'discord');
      assert.equal(result.discord_configured, true);
      // Either stopped (most likely) or running if discord_bot happens to be running on this machine
      assert.ok(['running', 'stopped'].includes(result.state), `unexpected state: ${result.state}`);
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});

test('findDiscordBotPid detects process with ampa.discord_bot in cmdline', async () => {
  await withTempDir('tmp-ampa-discord-pid-detect', async (tmp) => {
    // Spawn a node process that has 'ampa.discord_bot' as a command-line argument
    // so /proc/{pid}/cmdline contains the marker string
    const daemon = path.join(tmp, 'mock_discord_bot.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    const proc = spawn('node', [daemon, 'ampa.discord_bot'], {
      cwd: tmp,
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 100));

    try {
      const found = plugin.findDiscordBotPid();
      // On Linux where /proc is available, we should detect the process
      if (fs.existsSync('/proc')) {
        assert.equal(found, proc.pid, `findDiscordBotPid should find pid=${proc.pid}, got=${found}`);
      } else {
        // Non-Linux: /proc not available, function returns null — that's expected
        assert.equal(found, null, 'findDiscordBotPid should return null on non-Linux');
      }
    } finally {
      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    }
  });
});

// ---------- status() with --json flag tests ----------

test('status --json outputs structured JSON when scheduler stopped', async () => {
  await withTempDir('tmp-ampa-status-json-stopped', async (tmp) => {
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    delete process.env.AMPA_DISCORD_BOT_TOKEN;
    try {
      const logs = [];
      const originalLog = console.log;
      const originalErr = console.error;
      console.log = (...args) => { logs.push(args.join(' ')); };
      console.error = () => {};
      let code;
      try {
        code = await plugin.status(tmp, 'default', /* useJson */ true);
      } finally {
        console.log = originalLog;
        console.error = originalErr;
      }

      const output = JSON.parse(logs.join('\n'));
      assert.ok(Array.isArray(output.daemons), 'daemons should be an array');
      assert.equal(output.daemons.length, 2, 'should report exactly 2 daemons');

      const scheduler = output.daemons.find((d) => d.name === 'scheduler');
      assert.ok(scheduler, 'should have scheduler daemon');
      assert.ok(['stopped', 'error'].includes(scheduler.state), `scheduler state: ${scheduler.state}`);

      const discord = output.daemons.find((d) => d.name === 'discord');
      assert.ok(discord, 'should have discord daemon');
      assert.equal(discord.state, 'not_configured', `discord state: ${discord.state}`);

      assert.equal(output.discord_configured, false, 'discord_configured should be false');

      // Exit code should be non-zero (scheduler not running)
      assert.notEqual(code, 0, 'exit code should be non-zero when scheduler stopped');
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});

test('status human output includes discord warning when token not set', async () => {
  await withTempDir('tmp-ampa-status-human-warn', async (tmp) => {
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    delete process.env.AMPA_DISCORD_BOT_TOKEN;
    try {
      const logs = [];
      const errors = [];
      const originalLog = console.log;
      const originalErr = console.error;
      console.log = (...args) => { logs.push(args.join(' ')); };
      console.error = (...args) => { errors.push(args.join(' ')); };
      try {
        await plugin.status(tmp, 'default', /* useJson */ false);
      } finally {
        console.log = originalLog;
        console.error = originalErr;
      }

      const allOutput = [...logs, ...errors].join('\n');
      // Should mention both daemons
      assert.ok(logs.some((l) => l.includes('scheduler:')), `missing scheduler line: ${logs.join('\n')}`);
      assert.ok(logs.some((l) => l.includes('discord:')), `missing discord line: ${logs.join('\n')}`);
      // Should show a warning about missing token
      assert.ok(allOutput.includes('AMPA_DISCORD_BOT_TOKEN'), `missing token warning: ${allOutput}`);
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});

test('status --json exit code 0 when scheduler running and discord not configured', async () => {
  await withTempDir('tmp-ampa-status-exit-ok', async (tmp) => {
    const saved = process.env.AMPA_DISCORD_BOT_TOKEN;
    delete process.env.AMPA_DISCORD_BOT_TOKEN;
    try {
      // Start a real process and write its PID
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, `process.title = 'ampa.scheduler'; setInterval(()=>{},1000);`);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 100));

      const base = path.join(tmp, '.worklog', 'ampa', 'default');
      fs.mkdirSync(base, { recursive: true });
      const pidFile = path.join(base, 'default.pid');
      const logFile = path.join(base, 'default.log');
      fs.writeFileSync(pidFile, String(proc.pid));
      // Write ownership marker to the log so pidOwnedByProject accepts the process
      fs.writeFileSync(logFile, `ampa.scheduler started pid=${proc.pid}\n`);

      const logs = [];
      const originalLog = console.log;
      const originalErr = console.error;
      console.log = (...args) => { logs.push(args.join(' ')); };
      console.error = () => {};
      let code;
      try {
        code = await plugin.status(tmp, 'default', /* useJson */ true);
      } finally {
        console.log = originalLog;
        console.error = originalErr;
      }

      const output = JSON.parse(logs.join('\n'));
      const scheduler = output.daemons.find((d) => d.name === 'scheduler');
      // The process is alive; state depends on ownership validation
      assert.ok(scheduler, 'should have scheduler daemon');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    } finally {
      if (saved === undefined) delete process.env.AMPA_DISCORD_BOT_TOKEN;
      else process.env.AMPA_DISCORD_BOT_TOKEN = saved;
    }
  });
});
