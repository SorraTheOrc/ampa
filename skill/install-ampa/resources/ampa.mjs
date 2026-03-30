// Node ESM implementation of the wl 'ampa' plugin.
//
// CANONICAL SOURCE: skill/install-ampa/resources/ampa.mjs
//   This file is the single source of truth for the AMPA plugin. The installer
//   (skill/install-ampa/scripts/install-worklog-plugin.sh) copies it into
//   .worklog/plugins/ampa.mjs at install time. Do NOT create copies elsewhere
//   in the repo — edit this file directly and re-run the installer to deploy.
//   Tests import from this path (see tests/node/test-ampa*.mjs).
//
// Registers `wl ampa start|stop|status|run|list|ls|start-work|finish-work|list-containers`
// and manages pid/log files under `.worklog/ampa/<name>.(pid|log)`.

import { spawn, spawnSync } from 'child_process';
import fs from 'fs';
import fsPromises from 'fs/promises';
import path from 'path';
import os from 'os';

/**
 * Resolve the global OpenCode base directory.
 * Returns ${XDG_CONFIG_HOME}/opencode or $HOME/.config/opencode.
 * Throws if neither XDG_CONFIG_HOME nor HOME is available.
 */
function globalOpenCodeDir() {
  const xdg = process.env.XDG_CONFIG_HOME;
  if (xdg) {
    return path.join(xdg, 'opencode');
  }
  const home = process.env.HOME || os.homedir();
  if (!home) {
    throw new Error(
      'Cannot determine global directory: neither XDG_CONFIG_HOME nor HOME is set'
    );
  }
  return path.join(home, '.config', 'opencode');
}

/**
 * Resolve the global AMPA data directory.
 * Pool state files (pool-state.json, pool-cleanup.json, pool-replenish.log)
 * are stored here so they are shared across all projects.
 *
 * Respects XDG_CONFIG_HOME; falls back to $HOME/.config.
 * Throws if neither XDG_CONFIG_HOME nor HOME is set.
 */
function globalAmpaDir() {
  return path.join(globalOpenCodeDir(), '.worklog', 'ampa');
}

/**
 * Resolve the global plugins directory.
 * Returns ${XDG_CONFIG_HOME}/opencode/.worklog/plugins or
 * $HOME/.config/opencode/.worklog/plugins.
 */
function globalPluginsDir() {
  return path.join(globalOpenCodeDir(), '.worklog', 'plugins');
}

/**
 * Resolve the per-project AMPA config directory.
 * Per-project configuration (.env, scheduler_store.json) and daemon runtime
 * files (PID, log) are stored here.
 *
 * Path: <projectRoot>/.worklog/ampa/
 */
function projectAmpaDir(projectRoot) {
  return path.join(projectRoot, '.worklog', 'ampa');
}

/**
 * Locate the bundled AMPA Python package (ampa_py).
 *
 * Resolution order:
 *   1. <projectRoot>/.worklog/plugins/ampa_py/  (local override)
 *   2. <globalPluginsDir>/ampa_py/              (global install)
 *
 * Returns { pyPath, pythonBin } or null if the package is not found in
 * either location.
 */
function resolveAmpaPackage(projectRoot) {
  const locations = [
    path.join(projectRoot, '.worklog', 'plugins', 'ampa_py'),
    path.join(globalPluginsDir(), 'ampa_py'),
  ];
  for (const pyPath of locations) {
    try {
      if (fs.existsSync(path.join(pyPath, 'ampa', '__init__.py'))) {
        const venvPython = path.join(pyPath, 'venv', 'bin', 'python');
        const pythonBin = fs.existsSync(venvPython) ? venvPython : 'python3';
        return { pyPath, pythonBin };
      }
    } catch (e) {}
  }
  return null;
}

function findProjectRoot(start) {
  let cur = path.resolve(start);
  for (let i = 0; i < 100; i++) {
    if (
      fs.existsSync(path.join(cur, 'worklog.json')) ||
      fs.existsSync(path.join(cur, '.worklog')) ||
      fs.existsSync(path.join(cur, '.git'))
    ) {
      return cur;
    }
    const parent = path.dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  throw new Error('project root not found (worklog.json, .worklog or .git)');
}

function shellSplit(s) {
  if (!s) return [];
  const re = /((?:\\.|[^\s"'])+)|"((?:\\.|[^\\"])*)"|'((?:\\.|[^\\'])*)'/g;
  const out = [];
  let m;
  while ((m = re.exec(s)) !== null) {
    out.push(m[1] || m[2] || m[3] || '');
  }
  return out;
}

function readDotEnvFile(envPath) {
  if (!envPath || !fs.existsSync(envPath)) return {};
  let content = '';
  try {
    content = fs.readFileSync(envPath, 'utf8');
  } catch (e) {
    return {};
  }
  const env = {};
  const lines = content.split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const normalized = trimmed.startsWith('export ') ? trimmed.slice(7).trim() : trimmed;
    const idx = normalized.indexOf('=');
    if (idx === -1) continue;
    const key = normalized.slice(0, idx).trim();
    let val = normalized.slice(idx + 1).trim();
    if (!key) continue;
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    env[key] = val;
  }
  return env;
}

function readDotEnv(projectRoot, extraPaths = []) {
  const envPaths = [path.join(projectRoot, '.env'), ...extraPaths];
  return envPaths.reduce((acc, envPath) => Object.assign(acc, readDotEnvFile(envPath)), {});
}

async function resolveCommand(cliCmd, projectRoot) {
  if (cliCmd) return Array.isArray(cliCmd) ? cliCmd : shellSplit(cliCmd);
  if (process.env.WL_AMPA_CMD) return shellSplit(process.env.WL_AMPA_CMD);
  const wlJson = path.join(projectRoot, 'worklog.json');
  if (fs.existsSync(wlJson)) {
    try {
      const data = JSON.parse(await fsPromises.readFile(wlJson, 'utf8'));
      if (data && typeof data === 'object' && 'ampa' in data) {
        const val = data.ampa;
        if (typeof val === 'string') return shellSplit(val);
        if (Array.isArray(val)) return val;
      }
    } catch (e) {}
  }
  const pkgJson = path.join(projectRoot, 'package.json');
  if (fs.existsSync(pkgJson)) {
    try {
      const pj = JSON.parse(await fsPromises.readFile(pkgJson, 'utf8'));
      const scripts = pj.scripts || {};
      if (scripts.ampa) return shellSplit(scripts.ampa);
    } catch (e) {}
  }
  const candidates = [path.join(projectRoot, 'scripts', 'ampa'), path.join(projectRoot, 'scripts', 'daemon')];
  for (const c of candidates) {
    try {
      if (fs.existsSync(c) && fs.accessSync(c, fs.constants.X_OK) === undefined) return [c];
    } catch (e) {}
  }
  // Fallback: look for the bundled Python package 'ampa' in the local or global
  // plugins directory.  resolveAmpaPackage() checks project-local first, then
  // the global install path — there is no fallback to a development source tree.
  const pkg = resolveAmpaPackage(projectRoot);
  if (pkg) {
    const { pyPath, pythonBin } = pkg;
    const launcher = `import sys; sys.path.insert(0, ${JSON.stringify(pyPath)}); import ampa.daemon as d; d.main()`;
    // Run the daemon in long-running mode by default (start scheduler).
    // Users can override via --cmd or AMPA_RUN_SCHEDULER env var if desired.
    // use -u to force unbuffered stdout/stderr so logs show up promptly
    return {
      cmd: [pythonBin, '-u', '-c', launcher, '--start-scheduler'],
      env: { PYTHONPATH: pyPath, AMPA_RUN_SCHEDULER: '1' },
    };
  }
  return null;
}

async function resolveRunOnceCommand(projectRoot, commandId, extraArgs = []) {
  // Build base args: use 'run' subcommand (enhanced version).
  const subcommand = 'run';
  const baseArgs = ['-u', '-m', 'ampa.scheduler_cli', subcommand];
  if (commandId) baseArgs.push(commandId);
  if (extraArgs.length) baseArgs.push(...extraArgs);

  // Per-project config: .env is always loaded from <projectRoot>/.worklog/ampa/.env
  const envPath = path.join(projectAmpaDir(projectRoot), '.env');

  // Locate the bundled Python package (local override or global install).
  const pkg = resolveAmpaPackage(projectRoot);
  if (pkg) {
    const { pyPath, pythonBin } = pkg;
    return {
      cmd: [pythonBin, ...baseArgs],
      env: { PYTHONPATH: pyPath },
      envPaths: [envPath],
    };
  }
  // No bundled package found — report error instead of falling back to dev tree.
  throw new Error(
    'AMPA Python package not found. Install the plugin with:\n' +
    '  skill/install-ampa/scripts/install-worklog-plugin.sh --yes\n' +
    'Expected at: <projectRoot>/.worklog/plugins/ampa_py/ or ' +
    globalPluginsDir() + '/ampa_py/'
  );
}

async function resolveListCommand(projectRoot, useJson) {
  const args = ['-m', 'ampa.scheduler_cli', 'list'];
  if (useJson) args.push('--json');

  // Per-project config: .env is always loaded from <projectRoot>/.worklog/ampa/.env
  const envPath = path.join(projectAmpaDir(projectRoot), '.env');

  // Locate the bundled Python package (local override or global install).
  const pkg = resolveAmpaPackage(projectRoot);
  if (pkg) {
    const { pyPath, pythonBin } = pkg;
    return {
      cmd: [pythonBin, '-u', ...args],
      env: { PYTHONPATH: pyPath },
      envPaths: [envPath],
    };
  }
  // No bundled package found — report error instead of falling back to dev tree.
  throw new Error(
    'AMPA Python package not found. Install the plugin with:\n' +
    '  skill/install-ampa/scripts/install-worklog-plugin.sh --yes\n' +
    'Expected at: <projectRoot>/.worklog/plugins/ampa_py/ or ' +
    globalPluginsDir() + '/ampa_py/'
  );
}

const DAEMON_NOT_RUNNING_MESSAGE = 'Daemon is not running. Start it with: wl ampa start';

function readDaemonEnv(pid) {
  try {
    const envRaw = fs.readFileSync(`/proc/${pid}/environ`, 'utf8');
    const out = {};
    for (const entry of envRaw.split('\0')) {
      if (!entry) continue;
      const idx = entry.indexOf('=');
      if (idx === -1) continue;
      const key = entry.slice(0, idx);
      const val = entry.slice(idx + 1);
      out[key] = val;
    }
    return out;
  } catch (e) {
    return null;
  }
}

function resolveDaemonStore(projectRoot, name = 'default') {
  const ppath = pidPath(projectRoot, name);
  if (!fs.existsSync(ppath)) return { running: false };
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    return { running: false };
  }
  if (!isRunning(pid)) return { running: false };
  const owned = pidOwnedByProject(projectRoot, pid, logPath(projectRoot, name));
  if (!owned) return { running: false };

  let cwd = projectRoot;
  try {
    cwd = fs.readlinkSync(`/proc/${pid}/cwd`);
  } catch (e) {}
  const env = readDaemonEnv(pid) || {};

  // Resolution order (matches the Python daemon's SchedulerConfig.from_env):
  //   1. AMPA_SCHEDULER_STORE env var (explicit override)
  //   2. <projectRoot>/.worklog/ampa/scheduler_store.json (per-project)
  //   3. <packageDir>/ampa/scheduler_store.json (backward compat)
  let storePath = env.AMPA_SCHEDULER_STORE || '';
  if (!storePath) {
    // Per-project path (preferred)
    const projectStore = path.join(projectAmpaDir(projectRoot), 'scheduler_store.json');
    if (fs.existsSync(projectStore)) {
      storePath = projectStore;
    } else {
      // Backward compat: look inside the Python package directory. Prefer the
      // per-project package first, then any PYTHONPATH entries, then the
      // global plugins dir. If a package provides a scheduler_store.json use
      // that; otherwise continue searching. If no package store is found,
      // default to the per-project path.
      const candidates = [];
      // per-project package candidate first
      candidates.push(path.join(projectRoot, '.worklog', 'plugins', 'ampa_py'));
      // Do not consult the daemon's PYTHONPATH here — the test harness and
      // developer environments may set PYTHONPATH to include local copies of
      // the package, which would break test isolation. Only consider the
      // per-project package and the daemon's XDG_CONFIG_HOME-based global
      // plugins path.
      // Determine the global plugins dir based on the daemon's environment
      // (env) when possible. Tests spawn the daemon with XDG_CONFIG_HOME set
      // in its environment; prefer that so resolution matches what the daemon
      // will see. Fall back to the current process's globalPluginsDir()
      // only if the daemon didn't set XDG_CONFIG_HOME.
      try {
        // Only consult the daemon's XDG_CONFIG_HOME (from the daemon process
        // environment). Do NOT consult this process's XDG_CONFIG_HOME — that
        // risks picking up the developer's installed plugin and breaking test
        // isolation.
        if (env.XDG_CONFIG_HOME) {
          const gbase = path.join(env.XDG_CONFIG_HOME, 'opencode', '.worklog', 'plugins');
          candidates.push(path.join(gbase, 'ampa_py'));
        }
      } catch (e) {}

      const perProjectPkg = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py');
      for (const candidate of candidates) {
        const ampaPath = path.join(candidate, 'ampa');
        if (!fs.existsSync(path.join(ampaPath, 'scheduler.py'))) continue;
        const pkgStore = path.join(ampaPath, 'scheduler_store.json');
        if (fs.existsSync(pkgStore)) {
          storePath = pkgStore;
          break;
        }
        // If the per-project package exists but has no store file, prefer the
        // per-project scheduler_store.json path rather than falling back to
        // a global package store. This keeps per-project isolation intact.
        if (candidate === perProjectPkg) {
          storePath = projectStore;
          break;
        }
        // otherwise continue searching other candidates
      }
      // Default to per-project path even if file doesn't exist yet
      if (!storePath) {
        storePath = projectStore;
      }
    }
  } else if (!path.isAbsolute(storePath)) {
    storePath = path.resolve(cwd, storePath);
  }
  return { running: true, pid, cwd, env, storePath };
}

function ensureDirs(projectRoot, name) {
  const base = path.join(projectRoot, '.worklog', 'ampa', name);
  fs.mkdirSync(base, { recursive: true });
  return base;
}

function pidPath(projectRoot, name) {
  return path.join(ensureDirs(projectRoot, name), `${name}.pid`);
}

function logPath(projectRoot, name) {
  return path.join(ensureDirs(projectRoot, name), `${name}.log`);
}

function isRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    if (e && e.code === 'EPERM') return true;
    return false;
  }
}

function pidOwnedByProject(projectRoot, pid, lpath) {
  // Try /proc first (Linux). Fallback to ps if needed. Return true when a
  // substring that ties the process to this project is present in the cmdline.
  let cmdline = '';
  try {
    const p = `/proc/${pid}/cmdline`;
    if (fs.existsSync(p)) {
      cmdline = fs.readFileSync(p, 'utf8').replace(/\0/g, ' ').trim();
    }
  } catch (e) {}
  if (!cmdline) {
    try {
      const r = spawnSync('ps', ['-p', String(pid), '-o', 'args=']);
      if (r && r.status === 0 && r.stdout) cmdline = String(r.stdout).trim();
    } catch (e) {}
  }
  // Decide what patterns indicate ownership of the process by this project.
  const candidates = [
    projectRoot,
    path.join(projectRoot, '.worklog', 'plugins', 'ampa_py'),
    path.join(projectRoot, 'ampa'),
    'ampa.daemon',
    'ampa.scheduler',
  ];
  let matches = false;
  try {
    const lower = cmdline.toLowerCase();
    for (const c of candidates) {
      if (!c) continue;
      if (lower.includes(String(c).toLowerCase())) {
        matches = true;
        break;
      }
    }
  } catch (e) {}
  // Append a short diagnostic entry to the log if available.
  try {
    if (lpath) {
      fs.appendFileSync(lpath, `PID_VALIDATION pid=${pid} cmdline=${JSON.stringify(cmdline)} matches=${matches}\n`);
    }
  } catch (e) {}
  return matches;
}

function writePid(ppath, pid) {
  fs.writeFileSync(ppath, String(pid), 'utf8');
}

function readLogTail(lpath, maxBytes = 64 * 1024) {
  try {
    if (!fs.existsSync(lpath)) return '';
    const stat = fs.statSync(lpath);
    if (!stat || stat.size === 0) return '';
    const toRead = Math.min(stat.size, maxBytes);
    const fd = fs.openSync(lpath, 'r');
    const buf = Buffer.alloc(toRead);
    const pos = stat.size - toRead;
    fs.readSync(fd, buf, 0, toRead, pos);
    fs.closeSync(fd);
    return buf.toString('utf8');
  } catch (e) {
    return '';
  }
}

function extractErrorLines(text) {
  if (!text) return [];
  const lines = text.split(/\r?\n/);
  const re = /(ERROR|Traceback|Exception|AMPA_DISCORD_BOT_TOKEN|AMPA_DISCORD_CHANNEL_ID)/i;
  const out = [];
  for (const l of lines) {
    if (re.test(l)) out.push(l);
  }
  // return last 200 matching lines at most
  return out.slice(-200);
}

function printLogErrors(lpath) {
  try {
    const tail = readLogTail(lpath);
    const errs = extractErrorLines(tail);
    if (errs.length > 0) {
      console.log('Recent errors from log:');
      for (const line of errs) console.log(line);
      return true;
    }
  } catch (e) {}
  return false;
}

function findMostRecentLog(projectRoot) {
  try {
    const base = path.join(projectRoot, '.worklog', 'ampa');
    if (!fs.existsSync(base)) return null;
    let best = { p: null, m: 0 };
    const names = fs.readdirSync(base);
    for (const n of names) {
      const sub = path.join(base, n);
      try {
        const st = fs.statSync(sub);
        if (!st.isDirectory()) continue;
      } catch (e) { continue; }
      const files = fs.readdirSync(sub);
      for (const f of files) {
        if (!f.endsWith('.log')) continue;
        const fp = path.join(sub, f);
        try {
          const s = fs.statSync(fp);
          if (s && s.mtimeMs > best.m) {
            best.p = fp;
            best.m = s.mtimeMs;
          }
        } catch (e) {}
      }
    }
    return best.p;
  } catch (e) {
    return null;
  }
}

async function start(projectRoot, cmd, name = 'default', foreground = false) {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (fs.existsSync(ppath)) {
    try {
      const pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
      if (isRunning(pid)) {
        // Verify the pid actually belongs to this project's ampa daemon
        const owned = pidOwnedByProject(projectRoot, pid, lpath);
        if (owned) {
          console.log(`Already running (pid=${pid})`);
          return 0;
        } else {
          try { fs.unlinkSync(ppath); } catch (e) {}
          console.log(`Stale pid file removed (pid=${pid} did not match project)`);
        }
      }
    } catch (e) {}
  }
  // Diagnostic: record the resolved command and env to the log so failures to
  // persist can be investigated easily.
  try {
    fs.appendFileSync(lpath, `Resolved command: ${JSON.stringify(cmd)}\n`);
  } catch (e) {}

  if (foreground) {
    if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
      const env = Object.assign({}, process.env, cmd.env || {});
      const proc = spawn(cmd.cmd[0], cmd.cmd.slice(1), { cwd: projectRoot, stdio: 'inherit', env });
      return await new Promise((resolve) => {
        proc.on('exit', (code) => resolve(code || 0));
        proc.on('error', () => resolve(1));
      });
    }
    const proc = spawn(cmd[0], cmd.slice(1), { cwd: projectRoot, stdio: 'inherit' });
    return await new Promise((resolve) => {
      proc.on('exit', (code) => resolve(code || 0));
      proc.on('error', () => resolve(1));
    });
  }
  const out = fs.openSync(lpath, 'a');
  let proc;
  try {
    if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
      const env = Object.assign({}, process.env, cmd.env || {});
      proc = spawn(cmd.cmd[0], cmd.cmd.slice(1), { cwd: projectRoot, detached: true, stdio: ['ignore', out, out], env });
    } else {
      proc = spawn(cmd[0], cmd.slice(1), { cwd: projectRoot, detached: true, stdio: ['ignore', out, out] });
    }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.error('failed to start:', msg);
    // append the error message to the log file for easier diagnosis
    try { fs.appendFileSync(lpath, `Failed to spawn process: ${msg}\n`); } catch (ex) {}
    return 1;
  }
  if (!proc || !proc.pid) {
    console.error('failed to start: process did not spawn');
    return 1;
  }
  writePid(ppath, proc.pid);
  proc.unref();
  await new Promise((r) => setTimeout(r, 300));
  if (!isRunning(proc.pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.error('failed to start: process exited immediately');
    // Collect a helpful diagnostic snapshot: append the tail of the log to
    // the log itself with an explicit marker so operators can see what the
    // child process printed before exiting.
    let logText = '';
    try {
      const maxBytes = 32 * 1024; // read up to last 32KB of log
      const stat = fs.existsSync(lpath) && fs.statSync(lpath);
      if (stat && stat.size > 0) {
        const fd = fs.openSync(lpath, 'r');
        const toRead = Math.min(stat.size, maxBytes);
        const buf = Buffer.alloc(toRead);
        const pos = stat.size - toRead;
        fs.readSync(fd, buf, 0, toRead, pos);
        fs.closeSync(fd);
        logText = buf.toString('utf8');
        fs.appendFileSync(lpath, `\n----- CHILD PROCESS OUTPUT (last ${toRead} bytes) -----\n`);
        fs.appendFileSync(lpath, logText + '\n');
        fs.appendFileSync(lpath, `----- END CHILD OUTPUT -----\n`);
      }
    } catch (ex) {
      try { fs.appendFileSync(lpath, `Failed to capture child output: ${String(ex)}\n`); } catch (e) {}
    }
    // Surface relevant error lines from the daemon log so the user does not
    // need to open the log file manually.
    const errorLines = extractErrorLines(logText);
    if (errorLines.length > 0) {
      console.error('');
      console.error('Daemon log errors:');
      for (const line of errorLines) console.error(`  ${line}`);
    }
    // Detect missing Discord configuration and provide actionable instructions.
    if (errorLines.some((l) => l.includes('AMPA_DISCORD_BOT_TOKEN'))) {
      console.error('');
      console.error('Discord configuration is missing or incomplete.');
      console.error('Create .worklog/ampa/.env in your project with the required settings:');
      console.error('');
      console.error('  mkdir -p .worklog/ampa');
      console.error('  cat > .worklog/ampa/.env <<\'EOF\'');
      console.error('  AMPA_DISCORD_BOT_TOKEN="<your-bot-token>"');
      console.error('  AMPA_DISCORD_CHANNEL_ID="<your-channel-id>"');
      console.error('  EOF');
      console.error('');
      console.error('See ampa/.env.sample for all available options.');
    }
    console.error('');
    console.error(`Full log: ${lpath}`);
    return 1;
  }
  console.log(`Started ${name} pid=${proc.pid} log=${lpath}`);
  return 0;
}

async function stop(projectRoot, name = 'default', timeout = 10) {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (!fs.existsSync(ppath)) {
    console.log('Not running (no pid file)');
    return 0;
  }
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    fs.unlinkSync(ppath);
    console.log('Stale pid file removed');
    return 0;
  }
  if (!isRunning(pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log('Not running (stale pid file cleared)');
    return 0;
  }
  // Ensure the running pid is our process
  const owned = pidOwnedByProject(projectRoot, pid, lpath);
  if (!owned) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log('Not running (pid belonged to another process)');
    return 0;
  }
  try {
    try {
      process.kill(-pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(pid, 'SIGTERM'); } catch (e2) {}
    }
  } catch (e) {}
  const startTime = Date.now();
  while (isRunning(pid) && Date.now() - startTime < timeout * 1000) {
    await new Promise((r) => setTimeout(r, 100));
  }
  if (isRunning(pid)) {
    try {
      try { process.kill(-pid, 'SIGKILL'); } catch (e) { process.kill(pid, 'SIGKILL'); }
    } catch (e) {}
  }
  if (!isRunning(pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log(`Stopped pid=${pid}`);
    return 0;
  }
  console.log(`Failed to stop pid=${pid}`);
  return 1;
}

/**
 * Print pool container status as a headed, indented block.
 */
function printPoolStatus(projectRoot) {
  try {
    const existing = existingPoolContainers();
    const state = getPoolState(projectRoot);
    const cleanupList = getCleanupList(projectRoot);
    const cleanupSet = new Set(cleanupList);

    // Categorise containers
    const claimed = [];
    const pendingCleanup = [];
    const available = [];

    for (let i = 0; i < POOL_MAX_INDEX; i++) {
      const name = poolContainerName(i);
      if (!existing.has(name)) continue;
      if (cleanupSet.has(name)) {
        pendingCleanup.push(name);
      } else if (state[name]) {
        claimed.push({ name, ...state[name] });
      } else {
        available.push(name);
      }
    }

    // Image status
    const imgExists = imageExists(CONTAINER_IMAGE);
    const stale = imgExists ? isImageStale(projectRoot) : false;
    const templateExists = existing.has(TEMPLATE_CONTAINER_NAME) || checkContainerExists(TEMPLATE_CONTAINER_NAME);

    console.log('Sandbox pool:');
    console.log(`  Image:     ${imgExists ? CONTAINER_IMAGE : 'not built'}${stale ? ' (stale — run warm-pool to rebuild)' : ''}`);
    console.log(`  Template:  ${templateExists ? TEMPLATE_CONTAINER_NAME : 'not created'}`);
    console.log(`  Available: ${available.length} / ${POOL_SIZE} target`);
    if (available.length > 0) {
      for (const name of available) {
        console.log(`    - ${name}`);
      }
    }
    if (claimed.length > 0) {
      console.log(`  Claimed:   ${claimed.length}`);
      for (const c of claimed) {
        console.log(`    - ${c.name} -> ${c.workItemId} (${c.branch || 'no branch'})`);
      }
    } else {
      console.log('  Claimed:   0');
    }
    if (pendingCleanup.length > 0) {
      console.log(`  Cleanup:   ${pendingCleanup.length} pending destruction`);
      for (const name of pendingCleanup) {
        console.log(`    - ${name}`);
      }
    }
  } catch (e) {
    // Pool status is best-effort; don't fail status if pool helpers error
  }
}

/**
 * Inspect the scheduler daemon and return a structured status object.
 * Does NOT print anything — callers decide on formatting.
 *
 * Returned object shape:
 *   { name, state, pid, log, reason }
 *   state: 'running' | 'stopped' | 'error'
 */
function getSchedulerDaemonStatus(projectRoot, name = 'default') {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);

  if (!fs.existsSync(ppath)) {
    const alt = findMostRecentLog(projectRoot) || lpath;
    let errorLines = [];
    try { errorLines = extractErrorLines(readLogTail(alt)); } catch (e) {}
    return {
      name: 'scheduler',
      state: 'stopped',
      pid: null,
      log: lpath,
      reason: 'no pid file',
      recent_errors: errorLines.slice(-5),
    };
  }

  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    try { fs.unlinkSync(ppath); } catch (e2) {}
    const alt = findMostRecentLog(projectRoot) || lpath;
    let errorLines = [];
    try { errorLines = extractErrorLines(readLogTail(alt)); } catch (ex) {}
    return {
      name: 'scheduler',
      state: 'error',
      pid: null,
      log: lpath,
      reason: 'corrupt pid file (cleared)',
      recent_errors: errorLines.slice(-5),
    };
  }

  if (isRunning(pid)) {
    const owned = pidOwnedByProject(projectRoot, pid, lpath);
    if (owned) {
      return { name: 'scheduler', state: 'running', pid, log: lpath, reason: null, recent_errors: [] };
    } else {
      try { fs.unlinkSync(ppath); } catch (e) {}
      return {
        name: 'scheduler',
        state: 'stopped',
        pid: null,
        log: lpath,
        reason: 'stale pid file removed (pid belongs to unrelated process)',
        recent_errors: [],
      };
    }
  } else {
    try { fs.unlinkSync(ppath); } catch (e) {}
    const alt = findMostRecentLog(projectRoot) || lpath;
    let errorLines = [];
    try { errorLines = extractErrorLines(readLogTail(alt)); } catch (ex) {}
    return {
      name: 'scheduler',
      state: 'stopped',
      pid: null,
      log: lpath,
      reason: 'stale pid file removed (process not running)',
      recent_errors: errorLines.slice(-5),
    };
  }
}

/**
 * Scan /proc for a running discord bot process (ampa.discord_bot).
 * Returns the PID if found, null otherwise. Linux-only; no-op on other platforms.
 * /proc is an in-memory filesystem so directory enumeration is fast even with many PIDs.
 */
function findDiscordBotPid() {
  try {
    if (!fs.existsSync('/proc')) return null;
    const entries = fs.readdirSync('/proc');
    for (const entry of entries) {
      if (!/^\d+$/.test(entry)) continue;
      try {
        const cmdline = fs.readFileSync(`/proc/${entry}/cmdline`, 'utf8').replace(/\0/g, ' ');
        if (cmdline.includes('ampa.discord_bot')) {
          return parseInt(entry, 10);
        }
      } catch (e) {}
    }
  } catch (e) {}
  return null;
}

/**
 * Return a structured status object for the Discord bot daemon.
 * Checks whether AMPA_DISCORD_BOT_TOKEN is configured (in env or .env file)
 * and whether the discord_bot process is currently running.
 */
function getDiscordDaemonStatus(projectRoot) {
  // Check if token is configured: check process.env first, then .worklog/ampa/.env
  const envFilePath = path.join(projectAmpaDir(projectRoot), '.env');
  const fileEnv = readDotEnvFile(envFilePath);
  const token = process.env.AMPA_DISCORD_BOT_TOKEN || fileEnv.AMPA_DISCORD_BOT_TOKEN || '';
  const discordConfigured = !!token;

  if (!discordConfigured) {
    return {
      name: 'discord',
      state: 'not_configured',
      pid: null,
      reason: 'AMPA_DISCORD_BOT_TOKEN is not set',
      discord_configured: false,
    };
  }

  const discordPid = findDiscordBotPid();
  if (discordPid !== null) {
    return {
      name: 'discord',
      state: 'running',
      pid: discordPid,
      reason: null,
      discord_configured: true,
    };
  }

  return {
    name: 'discord',
    state: 'stopped',
    pid: null,
    reason: 'bot process not found',
    discord_configured: true,
  };
}

async function status(projectRoot, name = 'default', useJson = false) {
  const schedulerInfo = getSchedulerDaemonStatus(projectRoot, name);
  const discordInfo = getDiscordDaemonStatus(projectRoot);

  if (useJson) {
    const output = {
      daemons: [
        {
          name: schedulerInfo.name,
          state: schedulerInfo.state,
          pid: schedulerInfo.pid,
          log: schedulerInfo.log,
          reason: schedulerInfo.reason,
        },
        {
          name: discordInfo.name,
          state: discordInfo.state,
          pid: discordInfo.pid,
          reason: discordInfo.reason,
        },
      ],
      discord_configured: discordInfo.discord_configured,
    };
    console.log(JSON.stringify(output, null, 2));
  } else {
    // Human-readable output
    // Scheduler line
    if (schedulerInfo.state === 'running') {
      console.log(`scheduler: running pid=${schedulerInfo.pid} log=${schedulerInfo.log}`);
    } else {
      const reason = schedulerInfo.reason ? ` (${schedulerInfo.reason})` : '';
      console.log(`scheduler: ${schedulerInfo.state}${reason}`);
      if (schedulerInfo.recent_errors && schedulerInfo.recent_errors.length > 0) {
        console.log('Recent errors from log:');
        for (const line of schedulerInfo.recent_errors) console.log(line);
      }
    }

    // Discord line
    if (discordInfo.state === 'running') {
      console.log(`discord:   running pid=${discordInfo.pid}`);
    } else if (discordInfo.state === 'not_configured') {
      console.log(`discord:   not configured`);
      console.error('');
      console.error('WARNING: AMPA_DISCORD_BOT_TOKEN is not set — Discord notifications are disabled.');
      console.error('  Set it in .worklog/ampa/.env to enable Discord integration:');
      console.error('    AMPA_DISCORD_BOT_TOKEN="<your-bot-token>"');
      console.error('    AMPA_DISCORD_CHANNEL_ID="<your-channel-id>"');
    } else {
      const reason = discordInfo.reason ? ` (${discordInfo.reason})` : '';
      console.log(`discord:   ${discordInfo.state}${reason}`);
    }

    printPoolStatus(projectRoot);
  }

  // Determine exit code:
  // 0 = all configured daemons running
  // 1 = error state
  // 3 = stopped
  if (schedulerInfo.state === 'error') return 1;
  if (schedulerInfo.state !== 'running') return 3;
  // Scheduler is running; check Discord (only if configured)
  if (discordInfo.discord_configured && discordInfo.state !== 'running') return 3;
  return 0;
}

async function runOnce(projectRoot, cmdSpec) {
  const envPaths = cmdSpec && Array.isArray(cmdSpec.envPaths) ? cmdSpec.envPaths : [];
  const dotenvEnv = readDotEnv(projectRoot, envPaths);
  if (cmdSpec && cmdSpec.cmd && Array.isArray(cmdSpec.cmd)) {
    const env = Object.assign({}, process.env, dotenvEnv, cmdSpec.env || {});
    const proc = spawn(cmdSpec.cmd[0], cmdSpec.cmd.slice(1), { cwd: projectRoot, stdio: 'inherit', env });
    return await new Promise((resolve) => {
      proc.on('exit', (code) => resolve(code || 0));
      proc.on('error', () => resolve(1));
    });
  }
  return 1;
}

// ---------------------------------------------------------------------------
// Dev container helpers (start-work / finish-work / list-containers)
// ---------------------------------------------------------------------------

const CONTAINER_IMAGE = 'ampa-dev:latest';
const CONTAINER_PREFIX = 'ampa-';
const TEMPLATE_CONTAINER_NAME = 'ampa-template';
const POOL_PREFIX = 'ampa-pool-';
const POOL_SIZE = 3;

/**
 * Check if a binary exists in $PATH. Returns true if found, false otherwise.
 */
function checkBinary(name) {
  const whichCmd = process.platform === 'win32' ? 'where' : 'which';
  const result = spawnSync(whichCmd, [name], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Check that all required binaries (podman, distrobox, git, wl) are available.
 * Returns an object with { ok, missing } where missing is an array of names.
 */
function checkPrerequisites() {
  const required = ['podman', 'distrobox', 'git', 'wl'];
  const missing = required.filter((bin) => !checkBinary(bin));
  return { ok: missing.length === 0, missing };
}

/**
 * Validate a work item exists via `wl show <id> --json`.
 * Returns the work item data on success, or null on failure.
 */
function validateWorkItem(id) {
  const result = spawnSync('wl', ['show', id, '--json'], { stdio: 'pipe', encoding: 'utf8' });
  if (result.status !== 0) return null;
  try {
    const parsed = JSON.parse(result.stdout);
    if (parsed && parsed.success && parsed.workItem) return parsed.workItem;
    return null;
  } catch (e) {
    return null;
  }
}

/**
 * Check if a Podman container with the given name already exists.
 */
function checkContainerExists(name) {
  const result = spawnSync('podman', ['container', 'exists', name], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Get the git remote origin URL from the current directory.
 * Returns the URL string or null if not available.
 */
function getGitOrigin() {
  const result = spawnSync('git', ['remote', 'get-url', 'origin'], { stdio: 'pipe', encoding: 'utf8' });
  if (result.status !== 0 || !result.stdout) return null;
  return result.stdout.trim();
}

/**
 * Derive a container name from a work item ID.
 */
function containerName(workItemId) {
  return `${CONTAINER_PREFIX}${workItemId}`;
}

/**
 * Derive a branch name from a work item's issue type and ID.
 * Pattern: <issueType>/<work-item-id>
 * Falls back to task/ if issueType is unknown or empty.
 */
function branchName(workItemId, issueType) {
  const validTypes = ['feature', 'bug', 'chore', 'task'];
  const type = issueType && validTypes.includes(issueType) ? issueType : 'task';
  return `${type}/${workItemId}`;
}

/**
 * Check if the Podman image exists locally.
 */
function imageExists(imageName) {
  const result = spawnSync('podman', ['image', 'exists', imageName], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Get the creation timestamp of a Podman image.
 * Returns a Date, or null if the image does not exist or the date cannot be parsed.
 */
function imageCreatedDate(imageName) {
  const result = spawnSync('podman', [
    'image', 'inspect', imageName, '--format', '{{.Created}}',
  ], { encoding: 'utf8', stdio: 'pipe' });
  if (result.status !== 0) return null;
  const raw = (result.stdout || '').trim();
  if (!raw) return null;
  const d = new Date(raw);
  return isNaN(d.getTime()) ? null : d;
}

/**
 * Check whether the container image is older than the Containerfile.
 * Returns true if the image should be rebuilt (Containerfile is newer),
 * false if the image is up-to-date or if either date cannot be determined.
 */
function isImageStale(projectRoot) {
  const containerfilePath = path.join(projectRoot, 'ampa', 'Containerfile');
  if (!fs.existsSync(containerfilePath)) return false;
  if (!imageExists(CONTAINER_IMAGE)) return false; // no image to be stale

  const fileMtime = fs.statSync(containerfilePath).mtime;
  const imgDate = imageCreatedDate(CONTAINER_IMAGE);
  if (!imgDate) return false; // can't determine — assume up-to-date

  return fileMtime > imgDate;
}

/**
 * Tear down stale pool infrastructure so it can be rebuilt from a new image.
 * Destroys unclaimed pool containers and the template. Removes the image.
 * Claimed containers (active work) are preserved — they were built from
 * the old image but are still in use.
 * Returns { destroyed: string[], kept: string[], errors: string[] }.
 */
function teardownStalePool(projectRoot) {
  const destroyed = [];
  const kept = [];
  const errors = [];

  const state = getPoolState(projectRoot);
  const existing = existingPoolContainers();

  // Destroy unclaimed pool containers
  for (const name of existing) {
    if (state[name] && state[name].workItemId) {
      // Claimed — leave it alone
      kept.push(name);
      continue;
    }
    spawnSync('podman', ['stop', name], { stdio: 'pipe' });
    const rm = spawnSync('distrobox', ['rm', '--force', name], { encoding: 'utf8', stdio: 'pipe' });
    if (rm.status === 0) {
      destroyed.push(name);
    } else {
      const msg = (rm.stderr || rm.stdout || '').trim();
      errors.push(`Failed to remove ${name}: ${msg}`);
    }
  }

  // Destroy the template container
  if (checkContainerExists(TEMPLATE_CONTAINER_NAME)) {
    spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });
    const rm = spawnSync('distrobox', ['rm', '--force', TEMPLATE_CONTAINER_NAME], { encoding: 'utf8', stdio: 'pipe' });
    if (rm.status === 0) {
      destroyed.push(TEMPLATE_CONTAINER_NAME);
    } else {
      const msg = (rm.stderr || rm.stdout || '').trim();
      errors.push(`Failed to remove ${TEMPLATE_CONTAINER_NAME}: ${msg}`);
    }
  }

  // Remove the image
  if (imageExists(CONTAINER_IMAGE)) {
    const rm = spawnSync('podman', ['rmi', CONTAINER_IMAGE], { encoding: 'utf8', stdio: 'pipe' });
    if (rm.status !== 0) {
      const msg = (rm.stderr || rm.stdout || '').trim();
      errors.push(`Failed to remove image ${CONTAINER_IMAGE}: ${msg}`);
    }
  }

  // Clear cleanup list (stale entries no longer relevant)
  saveCleanupList(projectRoot, []);

  return { destroyed, kept, errors };
}

/**
 * Build the Podman image from the Containerfile.
 * Returns { ok, message }.
 */
function buildImage(projectRoot) {
  const containerfilePath = path.join(projectRoot, 'ampa', 'Containerfile');
  if (!fs.existsSync(containerfilePath)) {
    return { ok: false, message: `Containerfile not found at ${containerfilePath}` };
  }
  console.log(`Building image ${CONTAINER_IMAGE} from ${containerfilePath}...`);
  const result = spawnSync('podman', ['build', '-t', CONTAINER_IMAGE, '-f', containerfilePath, path.join(projectRoot, 'ampa')], {
    stdio: 'inherit',
  });
  if (result.status !== 0) {
    return { ok: false, message: `podman build failed with exit code ${result.status}` };
  }
  return { ok: true, message: 'Image built successfully' };
}

/**
 * Ensure the template container exists and is initialized.
 * On first run, creates a Distrobox container from the image and enters it
 * once to trigger full host-integration init (slow, one-off).
 * On subsequent runs, returns immediately because the template already exists.
 * Returns { ok, message }.
 */
function ensureTemplate() {
  if (checkContainerExists(TEMPLATE_CONTAINER_NAME)) {
    return { ok: true, message: 'Template container already exists' };
  }

  console.log('');
  console.log('='.repeat(72));
  console.log('  FIRST-TIME SETUP: Creating template container.');
  console.log('  This is a one-off step that takes several minutes while');
  console.log('  Distrobox integrates the host environment. Subsequent');
  console.log('  start-work runs will be much faster.');
  console.log('='.repeat(72));
  console.log('');

  console.log(`Creating template container "${TEMPLATE_CONTAINER_NAME}"...`);
  const createResult = spawnSync('distrobox', [
    'create',
    '--name', TEMPLATE_CONTAINER_NAME,
    '--image', CONTAINER_IMAGE,
    '--yes',
    '--no-entry',
  ], { encoding: 'utf8', stdio: 'inherit' });
  if (createResult.status !== 0) {
    return { ok: false, message: `Failed to create template (exit code ${createResult.status})` };
  }

  // Enter the template once to trigger Distrobox's full init.
  // Use stdio: inherit so the user sees real-time progress output.
  console.log('Initializing template (this is the slow part)...');
  const initResult = spawnSync('distrobox', [
    'enter', TEMPLATE_CONTAINER_NAME, '--', 'true',
  ], { encoding: 'utf8', stdio: 'inherit' });
  if (initResult.status !== 0) {
    // Clean up the broken template
    spawnSync('distrobox', ['rm', '--force', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });
    return { ok: false, message: `Template init failed (exit code ${initResult.status})` };
  }

  // Stop the template — distrobox enter leaves it running and
  // distrobox create --clone refuses to clone a running container.
  spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });

  console.log('Template container ready.');
  return { ok: true, message: 'Template created and initialized' };
}

// ---------------------------------------------------------------------------
// Container pool — pre-warmed containers for instant start-work
// ---------------------------------------------------------------------------

/**
 * Generate the name for pool container at the given index.
 */
function poolContainerName(index) {
  return `${POOL_PREFIX}${index}`;
}

/**
 * Path to the pool state JSON file.
 * Stores a mapping of pool container name -> { workItemId, branch, claimedAt }
 * for containers that have been claimed by start-work.
 *
 * Pool state is stored in the global AMPA directory so it is shared across
 * all projects. The projectRoot parameter is accepted for API compatibility
 * but is no longer used for path resolution.
 */
function poolStatePath(_projectRoot) {
  return path.join(globalAmpaDir(), 'pool-state.json');
}

/**
 * Read the pool state from disk. Returns an object keyed by container name.
 */
function getPoolState(projectRoot) {
  const p = poolStatePath(projectRoot);
  try {
    if (fs.existsSync(p)) {
      return JSON.parse(fs.readFileSync(p, 'utf8'));
    }
  } catch (e) {}
  return {};
}

/**
 * Persist pool state to disk.
 */
function savePoolState(projectRoot, state) {
  const p = poolStatePath(projectRoot);
  const dir = path.dirname(p);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(p, JSON.stringify(state, null, 2), 'utf8');
}

// Maximum pool index to scan.  This caps the total number of pool
// containers (claimed + unclaimed) to avoid runaway index growth.
const POOL_MAX_INDEX = POOL_SIZE * 3; // e.g. 9

/**
 * Return a Set of pool container names that currently exist in Podman.
 * Uses a single `podman ps -a` call instead of per-container checks.
 */
function existingPoolContainers() {
  const result = spawnSync('podman', [
    'ps', '-a', '--filter', `name=${POOL_PREFIX}`, '--format', '{{.Names}}',
  ], { encoding: 'utf8', stdio: 'pipe' });
  if (result.status !== 0) return new Set();
  return new Set(result.stdout.split('\n').filter(Boolean));
}

/**
 * List pool containers that exist in Podman and are NOT currently claimed.
 * Scans up to POOL_MAX_INDEX so we can find unclaimed containers even when
 * some lower-indexed slots are occupied by claimed (in-use) containers.
 * Returns an array of container names that are available for use.
 */
function listAvailablePool(projectRoot) {
  const state = getPoolState(projectRoot);
  const existing = existingPoolContainers();
  const available = [];
  for (let i = 0; i < POOL_MAX_INDEX; i++) {
    const name = poolContainerName(i);
    if (existing.has(name) && !state[name]) {
      available.push(name);
    }
  }
  return available;
}

/**
 * Claim a pool container for a work item. Updates the pool state file.
 * Returns the pool container name, or null if no pool containers are available.
 */
function claimPoolContainer(projectRoot, workItemId, branch) {
  const available = listAvailablePool(projectRoot);
  if (available.length === 0) return null;
  const name = available[0];
  const state = getPoolState(projectRoot);
  state[name] = {
    workItemId,
    branch,
    claimedAt: new Date().toISOString(),
  };
  savePoolState(projectRoot, state);
  return name;
}

/**
 * Release a pool container claim (after finish-work destroys it).
 */
function releasePoolContainer(projectRoot, containerNameOrAll) {
  const state = getPoolState(projectRoot);
  if (containerNameOrAll === '*') {
    // Clear all claims
    savePoolState(projectRoot, {});
    return;
  }
  delete state[containerNameOrAll];
  savePoolState(projectRoot, state);
}

/**
 * Path to the pool cleanup JSON file.
 * Stores an array of container names that should be destroyed from the host.
 *
 * Pool cleanup state is stored in the global AMPA directory so it is shared
 * across all projects. The projectRoot parameter is accepted for API
 * compatibility but is no longer used for path resolution.
 */
function poolCleanupPath(_projectRoot) {
  return path.join(globalAmpaDir(), 'pool-cleanup.json');
}

/**
 * Read the list of containers marked for cleanup.
 */
function getCleanupList(projectRoot) {
  const p = poolCleanupPath(projectRoot);
  try {
    if (fs.existsSync(p)) {
      const data = JSON.parse(fs.readFileSync(p, 'utf8'));
      return Array.isArray(data) ? data : [];
    }
  } catch (e) {}
  return [];
}

/**
 * Write the cleanup list to disk.
 */
function saveCleanupList(projectRoot, list) {
  const p = poolCleanupPath(projectRoot);
  const dir = path.dirname(p);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(p, JSON.stringify(list, null, 2), 'utf8');
}

/**
 * Mark a container for cleanup.  Called from inside the container when
 * finish-work cannot destroy itself.
 */
function markForCleanup(projectRoot, cName) {
  const list = getCleanupList(projectRoot);
  if (!list.includes(cName)) {
    list.push(cName);
    saveCleanupList(projectRoot, list);
  }
}

/**
 * Destroy containers that were marked for cleanup by finish-work running
 * inside a container.  This must be called from the host side.
 * Returns { destroyed: string[], errors: string[] }.
 */
function cleanupMarkedContainers(projectRoot) {
  const list = getCleanupList(projectRoot);
  if (list.length === 0) return { destroyed: [], errors: [] };

  const destroyed = [];
  const errors = [];
  const remaining = [];

  for (const cName of list) {
    const rmResult = spawnSync('distrobox', ['rm', '--force', cName], {
      encoding: 'utf8',
      stdio: 'pipe',
    });
    if (rmResult.status === 0) {
      destroyed.push(cName);
    } else {
      // Container may already be gone — check if it still exists
      if (!checkContainerExists(cName)) {
        destroyed.push(cName);
      } else {
        const msg = (rmResult.stderr || rmResult.stdout || '').trim();
        errors.push(`Failed to destroy ${cName}: ${msg}`);
        remaining.push(cName);
      }
    }
  }

  // Update cleanup list to only contain containers that failed to destroy
  saveCleanupList(projectRoot, remaining);

  return { destroyed, errors };
}

/**
 * Look up which pool container is assigned to a work item ID.
 * Returns the pool container name or null.
 */
function findPoolContainerForWorkItem(projectRoot, workItemId) {
  const state = getPoolState(projectRoot);
  for (const [name, info] of Object.entries(state)) {
    if (info && info.workItemId === workItemId) return name;
  }
  return null;
}

/**
 * Synchronously fill the pool so that at least POOL_SIZE unclaimed
 * containers are available.  Scans up to POOL_MAX_INDEX to find free
 * slot indices (no existing container), creates new clones there, and
 * enters each one to trigger Distrobox init.
 *
 * Returns { created, errors } — the count of newly created containers
 * and an array of error messages for any that failed.
 */
function replenishPool(projectRoot, sizeArg) {
  // Clean up any containers marked for destruction before counting slots
  cleanupMarkedContainers(projectRoot);

  const state = getPoolState(projectRoot);
  let created = 0;
  const errors = [];

  // Determine target size for this invocation (optional override)
  const targetSize = Number.isFinite(Number(sizeArg)) && Number(sizeArg) > 0 ? Number(sizeArg) : POOL_SIZE;
  const poolMaxIndex = targetSize * 3;

  // Count how many unclaimed containers already exist
  const existing = existingPoolContainers();
  let unclaimed = 0;
  for (let i = 0; i < poolMaxIndex; i++) {
    const name = poolContainerName(i);
    if (existing.has(name) && !state[name]) {
      unclaimed++;
    }
  }

  const deficit = targetSize - unclaimed;
  if (deficit <= 0) {
    return { created: 0, errors: [] };
  }

  // Collect free slot indices (where no container exists at all)
  const freeSlots = [];
  for (let i = 0; i < poolMaxIndex && freeSlots.length < deficit; i++) {
    const name = poolContainerName(i);
    if (!existing.has(name)) {
      freeSlots.push(name);
    }
  }

  if (freeSlots.length === 0) {
    return { created: 0, errors: [`No free pool slots available (all ${POOL_MAX_INDEX} indices occupied)`] };
  }

  // Ensure template exists
  const tmpl = ensureTemplate();
  if (!tmpl.ok) {
    return { created: 0, errors: [`Template not available: ${tmpl.message}`] };
  }

  // Stop the template — clone requires it to be stopped
  spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });

  for (const name of freeSlots) {
    const result = spawnSync('distrobox', [
      'create',
      '--clone', TEMPLATE_CONTAINER_NAME,
      '--name', name,
      '--yes',
      '--no-entry',
    ], { encoding: 'utf8', stdio: 'pipe' });
    if (result.status !== 0) {
      const msg = (result.stderr || result.stdout || '').trim();
      errors.push(`Failed to create ${name}: ${msg}`);
      continue;
    }

    // Enter the container once to trigger Distrobox's full init.
    // Without this step the first distrobox-enter at claim time would
    // run init and the bash --login shell would source profile files
    // before Distrobox finishes writing them, leaving host binaries
    // (git, wl, etc.) off the PATH.
    const initResult = spawnSync('distrobox', [
      'enter', name, '--', 'true',
    ], { encoding: 'utf8', stdio: 'pipe' });
    if (initResult.status !== 0) {
      const msg = (initResult.stderr || initResult.stdout || '').trim();
      errors.push(`Failed to init ${name}: ${msg}`);
      // Clean up the broken container
      spawnSync('distrobox', ['rm', '--force', name], { stdio: 'pipe' });
      continue;
    }

    // Stop the container — it must not be running when start-work
    // enters it later (and also so future --clone operations work).
    spawnSync('podman', ['stop', name], { stdio: 'pipe' });

    created++;
  }

  return { created, errors };
}

/**
 * Spawn a detached background process that replenishes the pool.
 * Returns immediately — the replenish happens asynchronously.
 */
function replenishPoolBackground(projectRoot) {
  // Build an inline Node script that does the replenish.
  // We import the plugin and call replenishPool directly.
  const pluginPath = path.resolve(projectRoot, 'skill', 'install-ampa', 'resources', 'ampa.mjs');
  // Fallback to installed copy if canonical source does not exist
  const actualPath = fs.existsSync(pluginPath)
    ? pluginPath
    : path.resolve(projectRoot, '.worklog', 'plugins', 'ampa.mjs');

  const script = [
    `import('file://${actualPath}')`,
    `.then(m => {`,
    `  const r = m.replenishPool('${projectRoot.replace(/'/g, "\\'")}');`,
    `  if (r.errors.length) r.errors.forEach(e => process.stderr.write(e + '\\n'));`,
    `})`,
    `.catch(e => { process.stderr.write(String(e) + '\\n'); process.exit(1); });`,
  ].join('');

  const logDir = globalAmpaDir();
  fs.mkdirSync(logDir, { recursive: true });
  const logFile = path.join(logDir, 'pool-replenish.log');
  const out = fs.openSync(logFile, 'a');
  try {
    fs.appendFileSync(logFile, `\n--- replenish started at ${new Date().toISOString()} ---\n`);
  } catch (e) {}

  const child = spawn(process.execPath, ['--input-type=module', '-e', script], {
    cwd: projectRoot,
    detached: true,
    stdio: ['ignore', out, out],
  });
  child.unref();
}

/**
 * Run a command synchronously, returning { status, stdout, stderr }.
 */
function runSync(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, { encoding: 'utf8', stdio: 'pipe', ...opts });
  return {
    status: result.status,
    stdout: (result.stdout || '').trim(),
    stderr: (result.stderr || '').trim(),
  };
}

/**
 * Create and enter a Distrobox container for a work item.
 */
async function startWork(projectRoot, workItemId, agentName, waitFlag, waitTimeoutSec) {
  console.log(`Creating sandbox container to work on ${workItemId}...`);

  // 1. Check prerequisites
  const prereqs = checkPrerequisites();
  if (!prereqs.ok) {
    const installHints = {
      podman: 'Install podman: https://podman.io/getting-started/installation',
      distrobox: 'Install distrobox: https://github.com/89luca89/distrobox#installation',
      git: 'Install git: apt install git / brew install git',
      wl: 'Install wl: see project README',
    };
    console.error(`Missing required tools: ${prereqs.missing.join(', ')}`);
    for (const m of prereqs.missing) {
      if (installHints[m]) console.error(`  ${installHints[m]}`);
    }
    return 2;
  }

  // 2. Sync worklog data so the container starts with the latest state
  console.log('Syncing worklog data...');
  const syncResult = runSync('wl', ['sync', '--json']);
  if (syncResult.status !== 0) {
    console.warn(`Warning: wl sync failed (exit ${syncResult.status}). Continuing with local data.`);
    if (syncResult.stderr) console.warn(`  ${syncResult.stderr}`);
  }

  // 3. Validate work item
  const workItem = validateWorkItem(workItemId);
  if (!workItem) {
    console.error(`Work item "${workItemId}" not found. Verify the ID with: wl show ${workItemId}`);
    return 2;
  }

  // 3b. Clean up any containers marked for destruction by finish-work
  const cleanup = cleanupMarkedContainers(projectRoot);
  if (cleanup.destroyed.length > 0) {
    console.log(`Cleaned up ${cleanup.destroyed.length} finished container(s): ${cleanup.destroyed.join(', ')}`);
  }

  // 4. Check if this work item already has a claimed container — enter it
  const existingPool = findPoolContainerForWorkItem(projectRoot, workItemId);
  if (existingPool) {
    console.log(`Work item "${workItemId}" already has container "${existingPool}". Entering...`);
    // Sync worklog on re-entry so the container picks up changes from other agents
    const reentryCmd = [
      'export PATH="$HOME/.npm-global/bin:$PATH"',
      'cd /workdir/project 2>/dev/null',
      'echo "Syncing worklog data..."',
      'wl sync 2>/dev/null || true',
      'exec bash --login',
    ].join('; ');
    const enterProc = spawn('distrobox', ['enter', existingPool, '--', 'bash', '--login', '-c', reentryCmd], {
      stdio: 'inherit',
    });
    return new Promise((resolve) => {
      enterProc.on('exit', (code) => resolve(code || 0));
      enterProc.on('error', (err) => {
        console.error(`Failed to enter container: ${err.message}`);
        resolve(1);
      });
    });
  }

  // Also check legacy container name (ampa-<id>) for backwards compat
  const legacyName = containerName(workItemId);
  if (checkContainerExists(legacyName)) {
    console.log(`Container "${legacyName}" already exists. Entering...`);
    // Sync worklog on re-entry so the container picks up changes from other agents
    const reentryCmd = [
      'export PATH="$HOME/.npm-global/bin:$PATH"',
      'cd /workdir/project 2>/dev/null',
      'echo "Syncing worklog data..."',
      'wl sync 2>/dev/null || true',
      'exec bash --login',
    ].join('; ');
    const enterProc = spawn('distrobox', ['enter', legacyName, '--', 'bash', '--login', '-c', reentryCmd], {
      stdio: 'inherit',
    });
    return new Promise((resolve) => {
      enterProc.on('exit', (code) => resolve(code || 0));
      enterProc.on('error', (err) => {
        console.error(`Failed to enter container: ${err.message}`);
        resolve(1);
      });
    });
  }

  // 5. Get git origin
  const origin = getGitOrigin();
  if (!origin) {
    console.error('Could not determine git remote origin. Ensure this is a git repo with a remote named "origin".');
    return 2;
  }
  // Extract project name from origin URL (e.g. "SorraAgents" from
  // "git@github.com:Org/SorraAgents.git" or "https://…/SorraAgents.git")
  const projectName = origin.replace(/\.git$/, '').split('/').pop().split(':').pop();

  // 6. Build image if needed
  if (!imageExists(CONTAINER_IMAGE)) {
    const build = buildImage(projectRoot);
    if (!build.ok) {
      console.error(`Failed to build container image: ${build.message}`);
      return 2;
    }
  }

  // 7. Ensure template container exists (one-off slow init)
  const tmpl = ensureTemplate();
  if (!tmpl.ok) {
    console.error(`Failed to prepare template container: ${tmpl.message}`);
    return 1;
  }

  // 8. Derive branch name
  const branch = branchName(workItemId, workItem.issueType);

  // 9. Claim a pre-warmed pool container, or fall back to direct clone
  let cName = claimPoolContainer(projectRoot, workItemId, branch);
  if (cName) {
    console.log(`Using pre-warmed container "${cName}".`);
  } else {
     // Pool is empty — fall back to cloning from template directly
    console.log('No pre-warmed containers available, cloning from template...');
    spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });
    // Use the first pool slot name so it integrates with the pool system
    cName = poolContainerName(0);
    const createResult = runSync('distrobox', [
      'create',
      '--clone', TEMPLATE_CONTAINER_NAME,
      '--name', cName,
      '--yes',
      '--no-entry',
    ]);
    if (createResult.status !== 0) {
      console.error(`Failed to create container: ${createResult.stderr || createResult.stdout}`);
      return 1;
    }
    // Enter once to trigger Distrobox init (sets up host PATH integration)
    console.log('Initializing container...');
    const initResult = spawnSync('distrobox', [
      'enter', cName, '--', 'true',
    ], { encoding: 'utf8', stdio: 'inherit' });
    if (initResult.status !== 0) {
      console.error('Container init failed');
      spawnSync('distrobox', ['rm', '--force', cName], { stdio: 'pipe' });
      return 1;
    }
    spawnSync('podman', ['stop', cName], { stdio: 'pipe' });
    // Record the claim
    const state = getPoolState(projectRoot);
    state[cName] = {
      workItemId,
      branch,
      claimedAt: new Date().toISOString(),
    };
    savePoolState(projectRoot, state);
  }

  // 9. Run setup inside the container:
  //    - Clone the project (shallow)
  //    - Create/checkout branch
  //    - Set env vars for container detection
  //    - Copy host worklog config and run wl init + wl sync
  //
  // Read the host's worklog config so we can inject it into the container.
  // The config.yaml may not be in the clone (it's gitignored on older branches).
  const hostConfigPath = path.join(projectRoot, '.worklog', 'config.yaml');
  let hostConfig = '';
  let wlProjectName = 'Project';
  let wlPrefix = 'WL';
  try {
    hostConfig = fs.readFileSync(hostConfigPath, 'utf8').trim();
    // Parse simple YAML key: value pairs
    const prefixMatch = hostConfig.match(/^prefix:\s*(.+)$/m);
    const nameMatch = hostConfig.match(/^projectName:\s*(.+)$/m);
    if (prefixMatch) wlPrefix = prefixMatch[1].trim();
    if (nameMatch) wlProjectName = nameMatch[1].trim();
  } catch {
    console.log('Warning: Could not read host .worklog/config.yaml — worklog init may prompt interactively.');
  }

  // Use /tmp so the sandbox lives on the container's writable tmpfs/overlay
  // rather than under /opt which may be root-owned or backed by a host bind.
  const containerProjectRoot = `/tmp/ampa_sandbox_${cName}/project`;

  const setupScript = [
    `set -e`,
    // Symlink host Node.js into the container so tools like wl work.
    // Node bundles its own OpenSSL so it is safe to use from /run/host
    // (unlike git/ssh which must be installed natively).
    `if [ -x /run/host/usr/bin/node ] && [ ! -e /usr/local/bin/node ]; then`,
    `  sudo ln -s /run/host/usr/bin/node /usr/local/bin/node`,
    `fi`,
    // Symlink host gh (GitHub CLI) into the container.  gh is a statically
    // linked Go binary so it has no shared-library dependencies and is safe
    // to use from /run/host.
    `if [ -x /run/host/usr/bin/gh ] && [ ! -e /usr/local/bin/gh ]; then`,
    `  sudo ln -s /run/host/usr/bin/gh /usr/local/bin/gh`,
    `fi`,
    // Ensure we use container-native Python instead of any legacy host-python
    // bridge symlinks from earlier AMPA versions.
    `if [ -L /usr/local/bin/python3 ] && [ "$(readlink /usr/local/bin/python3)" = "/run/host/usr/bin/python3" ]; then`,
    `  sudo rm -f /usr/local/bin/python3`,
    `fi`,
    `if [ -L /usr/local/bin/python ] && [ "$(readlink /usr/local/bin/python)" = "/usr/local/bin/python3" ]; then`,
    `  sudo rm -f /usr/local/bin/python`,
    `fi`,
    `if [ -x /usr/bin/python3 ] && [ ! -e /usr/local/bin/python ]; then`,
    `  sudo ln -s /usr/bin/python3 /usr/local/bin/python`,
    `fi`,
    `if [ -x /run/host/usr/lib/node_modules/npm/bin/npm-cli.js ] && [ ! -e /usr/local/bin/npm ]; then`,
    `  printf '#!/bin/sh\\nexec /usr/local/bin/node /run/host/usr/lib/node_modules/npm/bin/npm-cli.js "$@"\\n' | sudo tee /usr/local/bin/npm > /dev/null`,
    `  sudo chmod +x /usr/local/bin/npm`,
    `fi`,
    // Use /tmp so the sandbox lives on the container's writable tmpfs/overlay
    // rather than under /opt which may be root-owned or backed by a host bind.
    `CONTAINER_PROJECT_ROOT="/tmp/ampa_sandbox_${cName}/project"`,
    `rm -rf "${CONTAINER_PROJECT_ROOT}" || true`,
    `mkdir -p "$(dirname ${CONTAINER_PROJECT_ROOT})" || true`,
    `echo "Preparing container-local project at ${CONTAINER_PROJECT_ROOT}..."`,
    `if git clone --depth 1 "${origin}" "${CONTAINER_PROJECT_ROOT}" > /tmp/ampa_clone.log 2>&1; then`,
    `  echo "Clone succeeded into container-local path"`,
    // Prefer numeric uid/gid chown to avoid depending on a "dev" user existing
    `  if command -v id >/dev/null 2>&1; then CON_UID="$(id -u)"; CON_GID="$(id -g)"; else CON_UID=1000; CON_GID=1000; fi`,
    `  sudo chown -R "\${CON_UID}:\${CON_GID}" "${CONTAINER_PROJECT_ROOT}" || true`,
    `else`,
    `  echo "Clone to container-local path failed; showing diagnostic"`,
    `  cat /tmp/ampa_clone.log || true`,
    `  exit 1`,
    `fi`,
    `cd "${CONTAINER_PROJECT_ROOT}"`,
    // Create a symlink from the expected /workdir/project location to the actual clone location
    `if [ ! -e /workdir/project ]; then`,
    `  sudo ln -s "${CONTAINER_PROJECT_ROOT}" /workdir/project`,
    `fi`,
    // Check if branch exists on remote
    `if git ls-remote --heads origin "${branch}" | grep -q "${branch}"; then`,
    `  echo "Branch ${branch} exists on remote, checking out..."`,
    `  git fetch origin "${branch}:refs/remotes/origin/${branch}" --depth 1`,
    `  git checkout -b "${branch}" "origin/${branch}"`,
    `else`,
    `  echo "Creating new branch ${branch}..."`,
    `  git checkout -b "${branch}"`,
    `fi`,
    // Symlink host gh (GitHub CLI) into the container.  gh is a statically
    // linked Go binary so it has no shared-library dependencies and is safe
    // to use from /run/host.
    `if [ -x /run/host/usr/bin/gh ] && [ ! -e /usr/local/bin/gh ]; then`,
    `  sudo ln -s /run/host/usr/bin/gh /usr/local/bin/gh`,
    `fi`,
    // Ensure we use container-native Python instead of any legacy host-python
    // bridge symlinks from earlier AMPA versions.
    `if [ -L /usr/local/bin/python3 ] && [ "$(readlink /usr/local/bin/python3)" = "/run/host/usr/bin/python3" ]; then`,
    `  sudo rm -f /usr/local/bin/python3`,
    `fi`,
    `if [ -L /usr/local/bin/python ] && [ "$(readlink /usr/local/bin/python)" = "/usr/local/bin/python3" ]; then`,
    `  sudo rm -f /usr/local/bin/python`,
    `fi`,
    `if [ -x /usr/bin/python3 ] && [ ! -e /usr/local/bin/python ]; then`,
    `  sudo ln -s /usr/bin/python3 /usr/local/bin/python`,
    `fi`,
    // Create a wrapper for npm that delegates to the host's npm module tree
    // via the already-symlinked node.  npm is a Node.js script (not a native
    // binary) so a simple symlink won't work — the require() paths would
    // resolve against the container's filesystem where the npm module tree
    // doesn't exist.
    `if [ -f /run/host/usr/lib/node_modules/npm/bin/npm-cli.js ] && [ ! -e /usr/local/bin/npm ]; then`,
    `  printf '#!/bin/sh\\nexec /usr/local/bin/node /run/host/usr/lib/node_modules/npm/bin/npm-cli.js "$@"\\n' | sudo tee /usr/local/bin/npm > /dev/null`,
    `  sudo chmod +x /usr/local/bin/npm`,
    `fi`,
    // Write all AMPA container configuration to /etc/ampa_bashrc — a file on
    // the container's own overlay filesystem, invisible to the host and other
    // containers.  We overwrite (not append) so the file always has correct
    // values and duplication is impossible.  Uses sudo because /etc is
    // root-owned inside the container.
    `sudo tee /etc/ampa_bashrc > /dev/null << 'AMPA_BASHRC_EOF'`,
    `# AMPA container shell configuration`,
    `# Written by wl ampa start-work — do not edit manually.`,
    `export AMPA_CONTAINER_NAME=${cName}`,
    `export AMPA_WORK_ITEM_ID=${workItemId}`,
    `export AMPA_BRANCH=${branch}`,
    `export AMPA_PROJECT_ROOT=${projectRoot}`,
    `export PATH=/usr/local/bin:/usr/bin:/bin:$PATH`,
    `AMPA_BASHRC_EOF`,
    // Append the prompt and exit trap via separate heredocs so that the
    // quoted delimiters prevent shell expansion of bash escape sequences.
    // Green for project_sandbox, cyan for branch, reset before newline
    // PROMPT_COMMAND computes the path relative to /workdir/project each time
    `sudo tee -a /etc/ampa_bashrc > /dev/null << 'AMPA_PROMPT'`,
    `__ampa_prompt_cmd() { __ampa_rel="\${PWD#/workdir/project}"; __ampa_rel="\${__ampa_rel:-/}"; }`,
    `PROMPT_COMMAND=__ampa_prompt_cmd`,
    `PS1='\\[\\e[32m\\]${projectName}_sandbox\\[\\e[0m\\] - \\[\\e[36m\\]${branch}\\[\\e[0m\\]\\n\\[\\e[38;5;208m\\]\$__ampa_rel\\[\\e[0m\\] \\$ '`,
    `AMPA_PROMPT`,
    // Sync worklog data on shell exit so changes are not lost if the user
    // exits without running 'wl ampa finish-work'.  The trap runs on any
    // clean exit (exit, Ctrl+D, etc.).
    `sudo tee -a /etc/ampa_bashrc > /dev/null << 'AMPA_EXIT_TRAP'`,
    `__ampa_exit_sync() {`,
    `  if command -v wl >/dev/null 2>&1 && [ -d /workdir/project/.worklog ]; then`,
    `    echo ""`,
    `    echo "Syncing worklog data before exit..."`,
    `    ( cd /workdir/project && wl sync 2>/dev/null ) || true`,
    `  fi`,
    `}`,
    `trap __ampa_exit_sync EXIT`,
    `AMPA_EXIT_TRAP`,
    `echo 'cd /workdir/project' | sudo tee -a /etc/ampa_bashrc > /dev/null`,
    `echo '[ -f /workdir/project/.venv/bin/activate ] && . /workdir/project/.venv/bin/activate' | sudo tee -a /etc/ampa_bashrc > /dev/null`,
    // Add a one-line source guard to ~/.bashrc (idempotently) so that
    // /etc/ampa_bashrc is sourced only inside AMPA containers.  On the host
    // the file does not exist, so the guard is a no-op.
    `if ! grep -q '/etc/ampa_bashrc' ~/.bashrc 2>/dev/null; then`,
    `  echo '[ -f /etc/ampa_bashrc ] && . /etc/ampa_bashrc' >> ~/.bashrc`,
    `fi`,
    // Initialize worklog inside the cloned project.
    // .worklog/config.yaml may not be present in the clone (it's gitignored on
    // older branches / main).  Read the host's config and write it into the
    // container's project so wl init can bootstrap from it.
    // The setup script runs as a non-interactive login shell (bash --login -c)
    // which does NOT source .bashrc, so wl (~/.npm-global/bin) must be added
    // to PATH explicitly.
    `export PATH="$HOME/.npm-global/bin:$PATH"`,
    `if command -v wl >/dev/null 2>&1; then`,
    `  mkdir -p .worklog`,
    `  if [ ! -f .worklog/config.yaml ]; then`,
    `    cat > .worklog/config.yaml << 'WLCFG'`,
    `${hostConfig}`,
    `WLCFG`,
    `  fi`,
    `  if [ -f pyproject.toml ] || [ -f ampa/requirements.txt ] || [ -f requirements.txt ]; then`,
    `    echo "Preparing Python test environment..."`,
    `    if [ ! -d .venv ]; then`,
    `      python3 -m venv .venv || echo "Warning: failed to create .venv"`,
    `    fi`,
    `    if [ -f .venv/bin/activate ]; then`,
    `      . .venv/bin/activate`,
    `      python -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true`,
    `      if [ -f ampa/requirements.txt ]; then`,
    `        pip install -r ampa/requirements.txt >/dev/null 2>&1 || echo "Warning: pip install ampa/requirements.txt failed"`,
    `      fi`,
    `      if [ -f requirements.txt ]; then`,
    `        pip install -r requirements.txt >/dev/null 2>&1 || echo "Warning: pip install requirements.txt failed"`,
    `      fi`,
    `      if [ -f pyproject.toml ]; then`,
    `        pip install -e .[dev] >/dev/null 2>&1 || echo "Warning: pip install -e .[dev] failed"`,
    `      fi`,
    `      deactivate || true`,
    `    fi`,
    `  fi`,
    `  echo "Initializing worklog..."`,
    `  wl init --project-name "${wlProjectName}" --prefix "${wlPrefix}" --auto-export yes --auto-sync no --agents-template skip --workflow-inline no --stats-plugin-overwrite no --json || echo "wl init skipped (may already be initialized)"`,
    `  echo "Syncing worklog data..."`,
    `  wl sync --json || echo "wl sync skipped"`,
    // Copy the ampa plugin from the host project into the container's project.
    // The host home dir is mounted by Distrobox, so projectRoot is accessible.
    // Canonical source is skill/install-ampa/resources/ampa.mjs; fall back to
    // the installed copy at .worklog/plugins/ampa.mjs.
    `  echo "Installing wl ampa plugin..."`,
    `  mkdir -p .worklog/plugins`,
    `  if [ -f "${projectRoot}/skill/install-ampa/resources/ampa.mjs" ]; then`,
    `    cp "${projectRoot}/skill/install-ampa/resources/ampa.mjs" .worklog/plugins/ampa.mjs`,
    `  elif [ -f "${projectRoot}/.worklog/plugins/ampa.mjs" ]; then`,
    `    cp "${projectRoot}/.worklog/plugins/ampa.mjs" .worklog/plugins/ampa.mjs`,
    `  else`,
    `    echo "Warning: ampa plugin not found on host — wl ampa will not be available."`,
    `  fi`,
    `else`,
    `  echo "Warning: wl not found in PATH. Worklog will not be initialized."`,
    `fi`,
    `echo "Setup complete. Project cloned to ${containerProjectRoot} on branch ${branch}"`,
    `# Perform readiness checks: plugin copied and python/venv available. Only
    # write the readiness sentinel when checks pass so callers can wait on it.
    PLUGIN_OK=0`,
    `if [ -f .worklog/plugins/ampa.mjs ]; then PLUGIN_OK=1; fi`,
    `PY_OK=0`,
    `if [ -x .venv/bin/python ]; then PY_OK=1; elif command -v python3 >/dev/null 2>&1; then PY_OK=1; fi`,
    `if [ "$PLUGIN_OK" -eq 1 ] && [ "$PY_OK" -eq 1 ]; then`,
    `  echo "Readiness checks passed: plugin present and python/venv available."`,
    `  sudo sh -c 'echo READY > /tmp/ampa_ready' || true`,
    `  sudo chmod 644 /tmp/ampa_ready || true`,
    `else`,
    `  echo "Readiness checks failed: plugin_ok=${PLUGIN_OK} python_ok=${PY_OK}" > /tmp/ampa_install_start.log 2>/dev/null || true`,
    `  echo "--- Diagnostic: /workdir/project/.worklog/plugins listing ---" >> /tmp/ampa_install_start.log 2>/dev/null || true`,
    `  ls -la .worklog/plugins >> /tmp/ampa_install_start.log 2>/dev/null || true`,
    `  echo "--- Diagnostic: /tmp/ampa_clone.log ---" >> /tmp/ampa_install_start.log 2>/dev/null || true`,
    `  cat /tmp/ampa_clone.log >> /tmp/ampa_install_start.log 2>/dev/null || true`,
    `  echo "Readiness incomplete — leaving setup script with non-zero exit"`,
    `  exit 1`,
    `fi`,
  ].join('\n');

  console.log('Running setup inside container...');
  const setupResult = runSync('distrobox', [
    'enter', cName, '--', 'bash', '--login', '-c', setupScript,
  ]);
  if (setupResult.status !== 0) {
    console.error(`Container setup returned non-zero status: ${setupResult.status}`);
    // If the setup script failed but the repo was still cloned correctly
    // inside the container (some commands are best-effort), treat it as a
    // success. Check for an existing clone and a valid git repo.
    const verify = runSync('distrobox', [
      'enter', cName, '--', 'bash', '--login', '-c',
      `if [ -d "${containerProjectRoot}/.git" ]; then echo OK; else echo NO; fi`,
    ]);
    if (verify.status === 0 && (verify.stdout || '').includes('OK')) {
      console.log('Setup script failed but a valid clone exists — continuing.');
    } else {
      console.error(setupResult.stderr || setupResult.stdout);
      // Attempt cleanup
      releasePoolContainer(projectRoot, cName);
      spawnSync('distrobox', ['rm', '--force', cName], { stdio: 'pipe' });
      return 1;
    }
  }
  if (setupResult.stdout) console.log(setupResult.stdout);

  // Determine whether caller wants to wait for readiness and decide timeout.
  // Behavior: explicit waitFlag controls waiting. If undefined, default to
  // not waiting unless an agent name was provided (non-interactive callers),
  // in which case we block by default. The caller may override via CLI
  // flags (--wait / --timeout) which map into waitFlag / waitTimeoutSec.
  const effectiveWait = typeof waitFlag === 'boolean' ? waitFlag : !!agentName;
  const effectiveTimeout = Number(waitTimeoutSec !== undefined ? waitTimeoutSec : (process.env.WL_AMPA_WAIT_SECONDS || 30));

  if (effectiveWait && effectiveTimeout > 0) {
    const startTs = Date.now();
    let ready = false;
    while ((Date.now() - startTs) < effectiveTimeout * 1000) {
      const chk = runSync('distrobox', [
        'enter', cName, '--', 'bash', '-lc', `if [ -f /tmp/ampa_ready ]; then echo OK; else echo NO; fi`,
      ]);
      if (chk.status === 0 && (chk.stdout || '').includes('OK')) { ready = true; break; }
      // sleep 1 second
      spawnSync('bash', ['-lc', 'sleep 1']);
    }
    if (!ready) {
      console.error(`Container did not signal readiness within ${effectiveTimeout}s`);
      // Surface container-side diagnostics to help triage the failure.
      try {
        console.error('\n--- Container-side diagnostics (tail) ---');
        const diag = runSync('distrobox', [
          'enter', cName, '--', 'bash', '-lc', 'echo "--- /tmp/ampa_clone.log ---"; tail -n +1 /tmp/ampa_clone.log 2>/dev/null || true; echo "--- /tmp/ampa_install_start.log ---"; tail -n +1 /tmp/ampa_install_start.log 2>/dev/null || true; echo "--- /tmp listing ---"; ls -la /tmp || true',
        ]);
        if (diag.stdout) console.error(diag.stdout);
        if (diag.stderr) console.error(diag.stderr);
      } catch (e) {
        console.error('Failed to collect container diagnostics:', e && e.message ? e.message : String(e));
      }
      // Return non-zero so automated callers know start-work failed.
      return 1;
    }
  }

  // 10. Claim work item if agent name provided
  if (agentName) {
    spawnSync('wl', ['update', workItemId, '--status', 'in_progress', '--assignee', agentName, '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
  }

  // 11. Replenish the pool in the background (replace the container we just used)
  replenishPoolBackground(projectRoot);

  // 12. Enter the container interactively
  console.log(`\nEntering container "${cName}"...`);
  console.log(`Work directory: ${containerProjectRoot}`);
  console.log(`Branch: ${branch}`);
  console.log(`Work item: ${workItemId} - ${workItem.title}`);
  console.log(`\nRun 'wl ampa finish-work' when done.\n`);

  const enterProc = spawn('distrobox', ['enter', cName, '--', 'bash', '--login', '-c', `cd ${containerProjectRoot} && exec bash --login`], {
    stdio: 'inherit',
  });

  return new Promise((resolve) => {
    enterProc.on('exit', (code) => resolve(code || 0));
    enterProc.on('error', (err) => {
      console.error(`Failed to enter container: ${err.message}`);
      resolve(1);
    });
  });
}

/**
 * Finish work in a dev container: commit, push, update work item, destroy container.
 */
async function finishWork(opts = {}, workItemIdArg) {
  // 1. Detect context — running inside a container or from the host?
  // Backwards-compatible: callers may pass a boolean (old code) — treat that
  // boolean as discard. We intentionally drop the legacy `--force` CLI flag
  // entirely, but still accept code that used the old boolean parameter.
  if (typeof opts !== 'object' || opts === null) {
    opts = { discard: !!opts };
  }
  const discard = !!opts.discard;
  const noPush = !!opts.noPush; // --no-push
  const insideContainer = !!process.env.AMPA_CONTAINER_NAME;

  let cName, workItemId, branch, projectRoot;
  // commitHash: set when we push from inside the container
  let commitHash = null;
  // commitHashHost: set when we push via host-enter path
  let commitHashHost = null;

  if (insideContainer) {
    // Inside-container path: read env vars set by start-work
    cName = process.env.AMPA_CONTAINER_NAME;
    workItemId = process.env.AMPA_WORK_ITEM_ID;
    branch = process.env.AMPA_BRANCH;
    projectRoot = process.env.AMPA_PROJECT_ROOT;
  } else {
    // Host path: look up the container from pool state
    projectRoot = process.cwd();
    try { projectRoot = findProjectRoot(projectRoot); } catch (e) {
      console.error(e.message);
      return 2;
    }

    const state = getPoolState(projectRoot);
    const claimed = Object.entries(state).filter(([, v]) => v.workItemId);

    if (claimed.length === 0) {
      console.error('No claimed containers found. Nothing to finish.');
      return 2;
    }

    if (workItemIdArg) {
      // Find the container for the given work item
      const match = claimed.find(([, v]) => v.workItemId === workItemIdArg);
      if (!match) {
        console.error(`No container found for work item "${workItemIdArg}".`);
        console.error('Claimed containers:');
        for (const [name, v] of claimed) {
          console.error(`  ${name} → ${v.workItemId} (${v.branch})`);
        }
        return 2;
      }
      [cName, { workItemId, branch }] = [match[0], match[1]];
    } else if (claimed.length === 1) {
      // Only one claimed container — use it automatically
      [cName, { workItemId, branch }] = [claimed[0][0], claimed[0][1]];
      console.log(`Using container "${cName}" (${workItemId})`);
    } else {
      // Multiple claimed containers — require explicit ID
      console.error('Multiple claimed containers found. Specify a work item ID:');
      for (const [name, v] of claimed) {
        console.error(`  wl ampa finish-work ${v.workItemId}  (container: ${name}, branch: ${v.branch})`);
      }
      return 2;
    }
  }

  if (!cName || !workItemId) {
    console.error('Could not determine container or work item. Use "wl ampa finish-work <work-item-id>" from the host or run from inside a container.');
    return 2;
  }

  if (insideContainer) {
    // --- Inside-container path: commit, push, mark for cleanup ---

    // 2. Check for uncommitted changes
    const statusResult = runSync('git', ['status', '--porcelain']);
    const hasUncommitted = statusResult.stdout.length > 0;

    if (hasUncommitted && !discard) {
      console.log('Uncommitted changes detected. Committing...');
      const addResult = runSync('git', ['add', '-A']);
      if (addResult.status !== 0) {
        console.error(`git add failed: ${addResult.stderr}`);
        return 1;
      }

      const commitMsg = `${workItemId}: Work completed in dev container`;
      const commitResult = runSync('git', ['commit', '-m', commitMsg]);
      if (commitResult.status !== 0) {
        console.error(`git commit failed: ${commitResult.stderr}`);
        console.error('Uncommitted files:');
        console.error(statusResult.stdout);
        console.error('Use --discard to destroy the container without committing (changes will be lost).');
        return 1;
      }
      console.log(commitResult.stdout);
    } else if (hasUncommitted && discard) {
      console.log('Warning: Discarding uncommitted changes (--discard)');
      console.log(statusResult.stdout);
    }

    // 3. Push if there are commits to push (skip entirely when discarding)
    if (!discard) {
      if (!noPush) {
        const pushBranch = branch || 'HEAD';
        console.log(`Pushing ${pushBranch} to origin...`);
        const pushResult = runSync('git', ['push', '-u', 'origin', pushBranch]);
        if (pushResult.status !== 0) {
          console.error(`git push failed: ${pushResult.stderr}`);
          console.error('Use --discard to destroy the container without pushing.');
          return 1;
        }
        if (pushResult.stdout) console.log(pushResult.stdout);

        // Ensure worklog data is synced even if the push was a no-op (which
        // skips the pre-push hook) or if there were only worklog changes.
        console.log('Syncing worklog data...');
        runSync('wl', ['sync']);

        const hashResult = runSync('git', ['rev-parse', '--short', 'HEAD']);
        commitHash = hashResult.stdout || 'unknown';
      } else {
        // noPush: commit has been performed but do not push. Still sync worklog
        console.log('Skipping push (--no-push).');
        console.log('Syncing worklog data...');
        runSync('wl', ['sync']);
        const hashResult = runSync('git', ['rev-parse', '--short', 'HEAD']);
        commitHash = hashResult.stdout || 'unknown';
      }
    }

    // 4. Update work item (always update, even when --discard is used). When
    // discarding, there is no commit hash — note that in the comment.
    try {
      console.log(`Updating work item ${workItemId}...`);
      spawnSync('wl', ['update', workItemId, '--stage', 'in_review', '--status', 'completed', '--json'], {
        stdio: 'pipe',
        encoding: 'utf8',
      });
      const commentMsg = commitHash
        ? `Work completed in dev container ${cName}. Branch: ${branch || 'HEAD'}. Latest commit: ${commitHash}`
        : `Work completed in dev container ${cName}. Branch: ${branch || 'HEAD'}. No commit pushed (finished with --discard or no new commits).`;
      spawnSync('wl', ['comment', 'add', workItemId, '--comment', commentMsg, '--author', 'ampa', '--json'], {
        stdio: 'pipe',
        encoding: 'utf8',
      });
    } catch (e) {
      // Non-fatal — continue to cleanup even if wl update/comment fail
    }

    // 5. Release pool claim and attempt to destroy the container. If the
    // container cannot remove itself, mark it for host-side cleanup.
    if (projectRoot) {
      try {
        releasePoolContainer(projectRoot, cName);
      } catch (e) {
        // Non-fatal — pool state file may not be accessible from inside container
      }
      // Try to remove the container directly (may fail inside some container
      // environments). If direct removal fails, fall back to marking it for
      // host-side cleanup.
      try {
    const rmRes = runSync('distrobox', ['rm', '--force', cName]);
        if (rmRes.status === 0) {
          console.log(`Container "${cName}" destroyed.`);
        } else {
          // Couldn't remove from inside — mark for cleanup
          markForCleanup(projectRoot, cName);
          console.log(`Container "${cName}" marked for cleanup — it will be destroyed automatically on the next host-side pool operation.`);
        }
      } catch (e) {
        try {
          markForCleanup(projectRoot, cName);
          console.log(`Container "${cName}" marked for cleanup — it will be destroyed automatically on the next host-side pool operation.`);
        } catch (ee) {
          console.log(`Container "${cName}" marked for cleanup.`);
          console.log('Run the following from the host to destroy the container:');
          console.log(`  distrobox rm --force ${cName}`);
        }
      }
    } else {
      console.log(`Container "${cName}" marked for cleanup.`);
      console.log('Run the following from the host to destroy the container:');
      console.log(`  distrobox rm --force ${cName}`);
    }

    // 6. Exit the container shell so the user is returned to the host.
    console.log('Exiting container...');
    process.exit(0);
  }

  // --- Host path: enter container, commit/push, destroy, replenish ---

  console.log(`Finishing work in container "${cName}" (${workItemId}, branch: ${branch})...`);

  if (!discard) {
    // Build a script to commit, push, and sync worklog inside the container
    const commitPushScript = [
      `set -e`,
      `export PATH="$HOME/.npm-global/bin:$PATH"`,
      `cd /workdir/project 2>/dev/null || { echo "No project directory found in container."; exit 1; }`,
      // Check for uncommitted changes
      `if [ -n "$(git status --porcelain)" ]; then`,
      `  echo "Uncommitted changes detected. Committing..."`,
      `  git add -A`,
      `  git commit -m "${workItemId}: Work completed in dev container"`,
      `fi`,
      // Push
      `PUSH_BRANCH="${branch || 'HEAD'}"`,
      `echo "Pushing $PUSH_BRANCH to origin..."`,
      `git push -u origin "$PUSH_BRANCH"`,
      // Ensure worklog data is synced even if the push was a no-op
      `if command -v wl >/dev/null 2>&1; then`,
      `  echo "Syncing worklog data..."`,
      `  wl sync --json || echo "wl sync skipped"`,
      `fi`,
      `echo "AMPA_COMMIT_HASH=$(git rev-parse --short HEAD)"`,
    ].join('\n');

    console.log('Entering container to commit and push...');
    const commitResult = runSync('distrobox', [
      'enter', cName, '--', 'bash', '--login', '-c', commitPushScript,
    ]);

    if (commitResult.status !== 0) {
      console.error(`Commit/push inside container failed: ${commitResult.stderr || commitResult.stdout}`);
      console.error('Use --discard to destroy the container without committing (changes will be lost).');
      return 1;
    }
    if (commitResult.stdout) console.log(commitResult.stdout);

    // Extract commit hash from output
    const hashMatch = (commitResult.stdout || '').match(/AMPA_COMMIT_HASH=(\S+)/);
    commitHashHost = hashMatch ? hashMatch[1] : 'unknown';

    }

    // Update work item from the host (always update, even when --discard).
    try {
      console.log(`Updating work item ${workItemId}...`);
      spawnSync('wl', ['update', workItemId, '--stage', 'in_review', '--status', 'completed', '--json'], {
        stdio: 'pipe',
        encoding: 'utf8',
      });
      const commentMsgHost = commitHashHost || (commitHash || null)
        ? `Work completed in dev container ${cName}. Branch: ${branch || 'HEAD'}. Latest commit: ${commitHashHost || commitHash}`
        : `Work completed in dev container ${cName}. Branch: ${branch || 'HEAD'}. No commit pushed (finished with --discard or no new commits).`;
      spawnSync('wl', ['comment', 'add', workItemId, '--comment', commentMsgHost, '--author', 'ampa', '--json'], {
        stdio: 'pipe',
        encoding: 'utf8',
      });
    } catch (e) {
      // Non-fatal
    }
    if (discard) {
      console.log('Warning: Skipping commit/push (--discard). Uncommitted changes will be lost.');
    }

  // Release pool claim
  try {
    releasePoolContainer(projectRoot, cName);
    console.log(`Released pool claim for "${cName}".`);
  } catch (e) {
    console.error(`Warning: Could not release pool claim: ${e.message}`);
  }

  // Destroy the container
  console.log(`Destroying container "${cName}"...`);
  const rmResult = runSync('distrobox', ['rm', '--force', cName]);
  if (rmResult.status !== 0) {
    console.error(`Warning: Container removal failed: ${rmResult.stderr || rmResult.stdout}`);
    console.error(`You may need to run: distrobox rm --force ${cName}`);
  } else {
    console.log(`Container "${cName}" destroyed.`);
  }

  // Replenish pool in background
  replenishPoolBackground(projectRoot);
  console.log('Pool replenishment started in background.');

  return 0;
}

/**
 * List all dev containers created by start-work.
 * Shows claimed pool containers with their work item mapping.
 * Hides unclaimed pool containers and the template container.
 */
function listContainers(projectRoot, useJson = false) {
  // Clean up any containers marked for destruction before listing
  const cleanup = cleanupMarkedContainers(projectRoot);
  if (cleanup.destroyed.length > 0 && !useJson) {
    console.log(`Cleaned up ${cleanup.destroyed.length} finished container(s): ${cleanup.destroyed.join(', ')}`);
  }

  // Parse output of podman ps to find ampa-* containers
  const result = runSync('podman', ['ps', '-a', '--filter', `name=${CONTAINER_PREFIX}`, '--format', '{{.Names}}\\t{{.Status}}\\t{{.Created}}']);
  if (result.status !== 0) {
    // podman might not be installed
    if (!checkBinary('podman')) {
      console.error('podman is not installed. Install podman: https://podman.io/getting-started/installation');
      return 2;
    }
    console.error(`Failed to list containers: ${result.stderr}`);
    return 1;
  }

  const poolState = getPoolState(projectRoot);

  const lines = result.stdout.split('\n').filter(Boolean);
  const containers = lines.map((line) => {
    const parts = line.split('\t');
    const name = parts[0] || '';
    const status = parts[1] || 'unknown';
    const created = parts[2] || 'unknown';

    // Check if this is a pool container with a work item claim
    const claim = poolState[name];
    if (claim) {
      return { name, workItemId: claim.workItemId, branch: claim.branch, status, created };
    }

    // Legacy container name: ampa-<work-item-id> (not pool, not template)
    if (name.startsWith(CONTAINER_PREFIX) && !name.startsWith(POOL_PREFIX) && name !== TEMPLATE_CONTAINER_NAME) {
      const workItemId = name.slice(CONTAINER_PREFIX.length);
      return { name, workItemId, status, created };
    }

    // Unclaimed pool container or template — mark for filtering
    return null;
  }).filter(Boolean);

  if (useJson) {
    console.log(JSON.stringify({ containers }, null, 2));
  } else if (containers.length === 0) {
    console.log('No dev containers found.');
  } else {
    console.log('Dev containers:');
    console.log(`${'NAME'.padEnd(40)} ${'WORK ITEM'.padEnd(24)} ${'STATUS'.padEnd(20)} CREATED`);
    console.log('-'.repeat(100));
    for (const c of containers) {
      console.log(`${c.name.padEnd(40)} ${(c.workItemId || '-').padEnd(24)} ${c.status.padEnd(20)} ${c.created}`);
    }
  }

  return 0;
}

export default function register(ctx) {
  const { program } = ctx;
  const ampa = program.command('ampa').description('Manage project dev daemons and dev containers');

  ampa
    .command('start')
    .description('Start the project daemon')
    .option('--cmd <cmd>', 'Command to run (overrides config)')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--foreground', 'Run in foreground', false)
    .option('--verbose', 'Print resolved command and env', false)
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const cmd = await resolveCommand(opts.cmd, cwd);
      if (!cmd) {
        console.error('No command resolved. Set --cmd, WL_AMPA_CMD or configure worklog.json/package.json/scripts.');
        process.exitCode = 2;
        return;
      }
      if (opts.verbose) {
        try {
          if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
            console.log('Resolved command:', cmd.cmd.join(' '), 'env:', JSON.stringify(cmd.env || {}));
          } else if (Array.isArray(cmd)) {
            console.log('Resolved command:', cmd.join(' '));
          } else {
            console.log('Resolved command (unknown form):', JSON.stringify(cmd));
          }
        } catch (e) {}
      }
      const code = await start(cwd, cmd, opts.name, opts.foreground);
      process.exitCode = code;
    });

  ampa
    .command('stop')
    .description('Stop the project daemon')
    .option('--name <name>', 'Daemon name', 'default')
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await stop(cwd, opts.name);
      process.exitCode = code;
    });

  ampa
    .command('status')
    .description('Show daemon status')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--json', 'Output in JSON format')
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      const useJson = !!allOpts.json;
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await status(cwd, opts.name, useJson);
      process.exitCode = code;
    });

  ampa
    .command('run')
    .description('Run a scheduler command immediately by id, or list available commands')
    .arguments('[command-id]')
    .option('--json', 'Output in JSON format')
    .option('-F, --format <format>', 'Human display format (concise|normal|full|raw)')
    .option('-w, --watch [seconds]', 'Rerun the command every N seconds (default: 5)')
    .option('--verbose', 'Show verbose output including debug messages')
    .action(async (commandId, opts, cmd) => {
      // Use optsWithGlobals() because wl defines a global --json flag that
      // captures the option before it reaches the local opts object.
      const allOpts = cmd.optsWithGlobals();
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      // Build extra CLI args to pass through to the Python scheduler
      const extraArgs = [];
      if (allOpts.json) extraArgs.push('--json');
      if (allOpts.format) extraArgs.push('--format', allOpts.format);
      if (allOpts.watch !== undefined) {
        if (allOpts.watch === true) {
          extraArgs.push('--watch');
        } else {
          extraArgs.push('--watch', String(allOpts.watch));
        }
      }
      if (allOpts.verbose) extraArgs.push('--verbose');

      const cmdSpec = await resolveRunOnceCommand(cwd, commandId || null, extraArgs);
      if (!cmdSpec) {
        console.error('No run command resolved.');
        process.exitCode = 2;
        return;
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });

  ampa
    .command('list')
    .description('List scheduled commands')
    .option('--json', 'Output JSON')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--verbose', 'Print resolved store path', false)
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      const verbose = !!allOpts.verbose || process.argv.includes('--verbose');
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const daemon = resolveDaemonStore(cwd, allOpts.name);
      if (!daemon.running) {
        console.log(DAEMON_NOT_RUNNING_MESSAGE);
        process.exitCode = 3;
        return;
      }
      const cmdSpec = await resolveListCommand(cwd, !!allOpts.json);
      if (!cmdSpec) {
        console.error('No list command resolved.');
        process.exitCode = 2;
        return;
      }
      if (daemon.storePath) {
        if (verbose) {
          console.log(`Using scheduler store: ${daemon.storePath}`);
        }
        cmdSpec.env = Object.assign({}, cmdSpec.env || {}, { AMPA_SCHEDULER_STORE: daemon.storePath });
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });

  ampa
    .command('ls')
    .description('Alias for list')
    .option('--json', 'Output JSON')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--verbose', 'Print resolved store path', false)
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      const verbose = !!allOpts.verbose || process.argv.includes('--verbose');
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const daemon = resolveDaemonStore(cwd, allOpts.name);
      if (!daemon.running) {
        console.log(DAEMON_NOT_RUNNING_MESSAGE);
        process.exitCode = 3;
        return;
      }
      const cmdSpec = await resolveListCommand(cwd, !!allOpts.json);
      if (!cmdSpec) {
        console.error('No list command resolved.');
        process.exitCode = 2;
        return;
      }
      if (daemon.storePath) {
        if (verbose) {
          console.log(`Using scheduler store: ${daemon.storePath}`);
        }
        cmdSpec.env = Object.assign({}, cmdSpec.env || {}, { AMPA_SCHEDULER_STORE: daemon.storePath });
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });

  // ---- Dev container subcommands ----

  ampa
    .command('start-work')
    .description('Create an isolated dev container for a work item')
    .arguments('<work-item-id>')
    .option('--agent <name>', 'Agent name for work item assignment')
    .option('--wait', 'Block until container signals readiness', false)
    .option('--timeout <seconds>', 'Override readiness wait timeout seconds', parseInt)
    .action(async (workItemId, opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const waitFlag = opts.wait !== undefined ? !!opts.wait : undefined;
      const timeout = opts.timeout !== undefined ? Number(opts.timeout) : undefined;
      const code = await startWork(cwd, workItemId, opts.agent, waitFlag, timeout);
      process.exitCode = code;
    });

  ampa
    .command('sw')
    .description('Alias for start-work')
    .arguments('<work-item-id>')
    .option('--agent <name>', 'Agent name for work item assignment')
    .action(async (workItemId, opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await startWork(cwd, workItemId, opts.agent);
      process.exitCode = code;
    });

  ampa
    .command('finish-work')
    .description('Commit, push, and clean up a dev container')
    .arguments('[work-item-id]')
    .option('--discard', 'Discard uncommitted changes and destroy the container', false)
    .option('--no-push', 'Do not push commits to origin (commits are still created locally)', false)
    .action(async (workItemId, opts, cmd) => {
      // Support both invocation styles used by the real CLI (opts,cmd)
      // and the test harness (opts only). Prefer cmd.optsWithGlobals() when
      // available so global flags are honored.
      const allOpts = cmd && typeof cmd.optsWithGlobals === 'function' ? cmd.optsWithGlobals() : (opts || {});
      const code = await finishWork({ discard: allOpts.discard, noPush: allOpts.noPush }, workItemId);
      process.exitCode = code;
    });

  ampa
    .command('fw')
    .description('Alias for finish-work')
    .arguments('[work-item-id]')
    .option('--discard', 'Discard uncommitted changes and destroy the container', false)
    .option('--no-push', 'Do not push commits to origin (commits are still created locally)', false)
    .action(async (workItemId, opts, cmd) => {
      const allOpts = cmd && typeof cmd.optsWithGlobals === 'function' ? cmd.optsWithGlobals() : (opts || {});
      const code = await finishWork({ discard: allOpts.discard, noPush: allOpts.noPush }, workItemId);
      process.exitCode = code;
    });

  ampa
    .command('list-containers')
    .description('List dev containers created by start-work')
    .option('--json', 'Output JSON')
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = listContainers(cwd, !!allOpts.json);
      process.exitCode = code;
    });

  ampa
    .command('lc')
    .description('Alias for list-containers')
    .option('--json', 'Output JSON')
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = listContainers(cwd, !!allOpts.json);
      process.exitCode = code;
    });

  ampa
    .command('build-image')
    .description('Build the container image from the Containerfile')
    .action(async () => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const prereqs = checkPrerequisites();
      if (!prereqs.ok) { console.error(prereqs.message); process.exitCode = 1; return; }
      console.log('Building container image...');
      const build = buildImage(cwd);
      if (!build.ok) {
        console.error(`Failed to build container image: ${build.message}`);
        process.exitCode = 1;
        return;
      }
      console.log(build.message);
      process.exitCode = 0;
    });

  ampa
    .command('warm-pool')
    .description('Pre-warm the container pool (ensure template exists and fill empty pool slots)')
    .option('--size <n>', 'Target pool size (overrides WL_AMPA_POOL_SIZE env var)')
    .option('--non-interactive', 'Run without interactive prompts (suitable for installers)')
    .option('--rebuild', 'Force rebuild of the container image and recreate pool from it')
    .action(async (opts, cmd) => {
      const allOpts = cmd.optsWithGlobals();
      // Note: POOL_SIZE is a const; to allow per-invocation size we pass requestedSize
      // into replenishPool by wrapping or using a new helper. For simplicity, we
      // set a local variable here and pass it to replenishPool via an argument
      // in the future; for now replenishPool reads POOL_SIZE constant. If callers
      // rely on --size, they should set WL_AMPA_POOL_SIZE in the environment.
      // Continue with existing behaviour.
      
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const prereqs = checkPrerequisites();
      if (!prereqs.ok) {
        console.error(prereqs.message);
        process.exitCode = 1;
        return;
      }
      // Rebuild image if requested, otherwise check if the image is stale
      if (allOpts.rebuild) {
        console.log('Rebuild requested — tearing down pool/template and removing image...');
        const teardown = teardownStalePool(cwd);
        if (teardown.destroyed.length > 0) {
          console.log(`Removed containers/template: ${teardown.destroyed.join(', ')}`);
        }
        if (teardown.kept.length > 0) {
          console.log(`Kept claimed containers (still in use): ${teardown.kept.join(', ')}`);
        }
        if (teardown.errors.length > 0) {
          teardown.errors.forEach(e => console.error(e));
        }
      } else {
        // Check if the image is stale (Containerfile newer than image)
        if (isImageStale(cwd)) {
          console.log('Containerfile is newer than the current image — rebuilding...');
          const teardown = teardownStalePool(cwd);
          if (teardown.destroyed.length > 0) {
            console.log(`Removed stale containers: ${teardown.destroyed.join(', ')}`);
          }
          if (teardown.kept.length > 0) {
            console.log(`Kept claimed containers (still in use): ${teardown.kept.join(', ')}`);
          }
          if (teardown.errors.length > 0) {
            teardown.errors.forEach(e => console.error(e));
          }
        }
      }
      // Build image if needed
      if (!imageExists(CONTAINER_IMAGE)) {
        console.log('Building container image...');
        const build = buildImage(cwd);
        if (!build.ok) {
          console.error(`Failed to build container image: ${build.message}`);
          process.exitCode = 1;
          return;
        }
      }
      console.log('Ensuring template container exists...');
      const tmpl = ensureTemplate();
      if (!tmpl.ok) {
        console.error(`Failed to create template: ${tmpl.message}`);
        process.exitCode = 1;
        return;
      }
      console.log('Template ready. Filling pool slots...');
      // If caller supplied a size via env or flag, pass it through
      const requestedSize = parseInt(allOpts.size || process.env.WL_AMPA_POOL_SIZE || POOL_SIZE, 10) || POOL_SIZE;
      const result = replenishPool(cwd, requestedSize);
      if (result.errors.length) {
        result.errors.forEach(e => console.error(e));
      }
      if (result.created > 0) {
        console.log(`Created ${result.created} pool container(s). Pool is now warm.`);
      } else {
        console.log('Pool is already fully warm — no new containers needed.');
      }
      process.exitCode = result.errors.length > 0 ? 1 : 0;
    });

  ampa
    .command('wp')
    .description('Alias for warm-pool')
    .action(async () => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const prereqs = checkPrerequisites();
      if (!prereqs.ok) {
        console.error(prereqs.message);
        process.exitCode = 1;
        return;
      }
      // Check if the image is stale (Containerfile newer than image)
      if (isImageStale(cwd)) {
        console.log('Containerfile is newer than the current image — rebuilding...');
        const teardown = teardownStalePool(cwd);
        if (teardown.destroyed.length > 0) {
          console.log(`Removed stale containers: ${teardown.destroyed.join(', ')}`);
        }
        if (teardown.kept.length > 0) {
          console.log(`Kept claimed containers (still in use): ${teardown.kept.join(', ')}`);
        }
        if (teardown.errors.length > 0) {
          teardown.errors.forEach(e => console.error(e));
        }
      }
      // Build image if needed
      if (!imageExists(CONTAINER_IMAGE)) {
        console.log('Building container image...');
        const build = buildImage(cwd);
        if (!build.ok) {
          console.error(`Failed to build container image: ${build.message}`);
          process.exitCode = 1;
          return;
        }
      }
      console.log('Ensuring template container exists...');
      const tmpl = ensureTemplate();
      if (!tmpl.ok) {
        console.error(`Failed to create template: ${tmpl.message}`);
        process.exitCode = 1;
        return;
      }
      console.log('Template ready. Filling pool slots...');
      const result = replenishPool(cwd);
      if (result.errors.length) {
        result.errors.forEach(e => console.error(e));
      }
      if (result.created > 0) {
        console.log(`Created ${result.created} pool container(s). Pool is now warm.`);
      } else {
        console.log('Pool is already fully warm — no new containers needed.');
      }
      process.exitCode = result.errors.length > 0 ? 1 : 0;
    });
}

export {
  CONTAINER_IMAGE,
  CONTAINER_PREFIX,
  DAEMON_NOT_RUNNING_MESSAGE,
  extractErrorLines,
  POOL_PREFIX,
  POOL_SIZE,
  POOL_MAX_INDEX,
  TEMPLATE_CONTAINER_NAME,
  branchName,
  buildImage,
  checkBinary,
  checkContainerExists,
  checkPrerequisites,
  claimPoolContainer,
  cleanupMarkedContainers,
  containerName,
  ensureTemplate,
  existingPoolContainers,
  findDiscordBotPid,
  findPoolContainerForWorkItem,
  getCleanupList,
  getDiscordDaemonStatus,
  getGitOrigin,
  getPoolState,
  getSchedulerDaemonStatus,
  globalAmpaDir,
  globalPluginsDir,
  imageCreatedDate,
  imageExists,
  isImageStale,
  listAvailablePool,
  listContainers,
  markForCleanup,
  poolCleanupPath,
  poolContainerName,
  poolStatePath,
  projectAmpaDir,
  releasePoolContainer,
  replenishPool,
  replenishPoolBackground,
  resolveAmpaPackage,
  resolveDaemonStore,
  saveCleanupList,
  savePoolState,
  start,
  startWork,
  teardownStalePool,
  status,
  stop,
  validateWorkItem,
};
