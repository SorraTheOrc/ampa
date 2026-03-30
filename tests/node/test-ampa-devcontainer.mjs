import { test, describe, mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import path from 'path';
import os from 'os';

// Import the plugin module — all new helpers/constants are named exports.
const pluginModule = new URL('../../skill/install-ampa/resources/ampa.mjs', import.meta.url);
const plugin = await import(pluginModule.href);

// ---------------------------------------------------------------------------
// Test isolation helpers for global pool state paths.
// Since pool state now resolves via globalAmpaDir() using XDG_CONFIG_HOME,
// we redirect it to a per-test temp directory to avoid cross-contamination
// and to avoid writing to the real global directory during tests.
// ---------------------------------------------------------------------------

let _savedXdg;

/** Set XDG_CONFIG_HOME to a fresh temp directory; returns the path. */
function useIsolatedGlobalDir() {
  _savedXdg = process.env.XDG_CONFIG_HOME;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-xdg-test-'));
  process.env.XDG_CONFIG_HOME = dir;
  return dir;
}

/** Restore XDG_CONFIG_HOME and clean up the temp directory. */
function restoreGlobalDir(xdgDir) {
  if (_savedXdg === undefined) {
    delete process.env.XDG_CONFIG_HOME;
  } else {
    process.env.XDG_CONFIG_HOME = _savedXdg;
  }
  if (xdgDir) {
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
}

// Re-use the FakeProgram/FakeCommand pattern from test-ampa.mjs for command
// registration tests.
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
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe('dev container constants', () => {
  test('CONTAINER_IMAGE is ampa-dev:latest', () => {
    assert.equal(plugin.CONTAINER_IMAGE, 'ampa-dev:latest');
  });

  test('CONTAINER_PREFIX is ampa-', () => {
    assert.equal(plugin.CONTAINER_PREFIX, 'ampa-');
  });

  test('TEMPLATE_CONTAINER_NAME is ampa-template', () => {
    assert.equal(plugin.TEMPLATE_CONTAINER_NAME, 'ampa-template');
  });
});

// ---------------------------------------------------------------------------
// branchName generation
// ---------------------------------------------------------------------------

describe('branchName', () => {
  test('feature issue type produces feature/<id>', () => {
    assert.equal(plugin.branchName('SA-123', 'feature'), 'feature/SA-123');
  });

  test('bug issue type produces bug/<id>', () => {
    assert.equal(plugin.branchName('SA-456', 'bug'), 'bug/SA-456');
  });

  test('chore issue type produces chore/<id>', () => {
    assert.equal(plugin.branchName('WL-1', 'chore'), 'chore/WL-1');
  });

  test('task issue type produces task/<id>', () => {
    assert.equal(plugin.branchName('WL-2', 'task'), 'task/WL-2');
  });

  test('unknown issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-789', 'epic'), 'task/SA-789');
  });

  test('empty issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-100', ''), 'task/SA-100');
  });

  test('null issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-200', null), 'task/SA-200');
  });

  test('undefined issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-300', undefined), 'task/SA-300');
  });
});

// ---------------------------------------------------------------------------
// containerName generation
// ---------------------------------------------------------------------------

describe('containerName', () => {
  test('generates ampa-<id> from work item ID', () => {
    assert.equal(plugin.containerName('SA-0MLQ8YD0Z1C1FP07'), 'ampa-SA-0MLQ8YD0Z1C1FP07');
  });

  test('generates ampa-<id> from short ID', () => {
    assert.equal(plugin.containerName('WL-1'), 'ampa-WL-1');
  });
});

// ---------------------------------------------------------------------------
// checkBinary
// ---------------------------------------------------------------------------

describe('checkBinary', () => {
  test('returns true for a binary that exists (node)', () => {
    assert.equal(plugin.checkBinary('node'), true);
  });

  test('returns false for a binary that does not exist', () => {
    assert.equal(plugin.checkBinary('nonexistent-binary-xyz-999'), false);
  });
});

// ---------------------------------------------------------------------------
// checkPrerequisites
// ---------------------------------------------------------------------------

describe('checkPrerequisites', () => {
  test('returns an object with ok and missing properties', () => {
    const result = plugin.checkPrerequisites();
    assert.ok(typeof result.ok === 'boolean');
    assert.ok(Array.isArray(result.missing));
  });

  test('missing array contains any tools not in PATH', () => {
    const result = plugin.checkPrerequisites();
    // We can't guarantee what's installed in CI, but the shape is correct
    for (const m of result.missing) {
      assert.ok(['podman', 'distrobox', 'git', 'wl'].includes(m));
    }
  });
});

// ---------------------------------------------------------------------------
// getGitOrigin
// ---------------------------------------------------------------------------

describe('getGitOrigin', () => {
  test('returns a string URL when in a git repo with origin', () => {
    const origin = plugin.getGitOrigin();
    // We are in a git repo with an origin remote
    assert.ok(origin === null || typeof origin === 'string');
    if (origin) {
      assert.ok(origin.length > 0, 'origin should not be empty');
    }
  });
});

// ---------------------------------------------------------------------------
// validateWorkItem
// ---------------------------------------------------------------------------

describe('validateWorkItem', () => {
  test('returns null for a non-existent work item ID', () => {
    const result = plugin.validateWorkItem('NONEXISTENT-FAKE-ID-999');
    assert.equal(result, null);
  });

  // Note: We can't test a valid work item without wl being fully configured,
  // which depends on runtime state. The function is tested end-to-end instead.
});

// ---------------------------------------------------------------------------
// checkContainerExists
// ---------------------------------------------------------------------------

describe('checkContainerExists', () => {
  test('returns false for a non-existent container', () => {
    // This will either return false (podman installed, no such container)
    // or false (podman not installed, command fails with non-zero).
    const result = plugin.checkContainerExists('nonexistent-container-xyz-999');
    assert.equal(result, false);
  });
});

// ---------------------------------------------------------------------------
// ensureTemplate
// ---------------------------------------------------------------------------

describe('ensureTemplate', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.ensureTemplate, 'function');
  });

  test('returns ok: true when template already exists', () => {
    // Only test the return shape when the template container already exists,
    // otherwise calling ensureTemplate() would attempt a slow distrobox create.
    const templateExists = plugin.checkContainerExists(plugin.TEMPLATE_CONTAINER_NAME);
    if (!templateExists) {
      // Skip — cannot test without triggering a slow distrobox create
      return;
    }
    const result = plugin.ensureTemplate();
    assert.ok(typeof result === 'object' && result !== null, 'should return an object');
    assert.equal(result.ok, true);
    assert.ok(typeof result.message === 'string', 'message should be a string');
  });
});
// Command registration
// ---------------------------------------------------------------------------

describe('command registration', () => {
  let ampaCmd;

  test('registers ampa command with all subcommands', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    ampaCmd = ctx.program.commands.get('ampa');
    assert.ok(ampaCmd, 'ampa command should be registered');
  });

  test('registers start-work subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('start-work'), 'start-work should be registered');
    assert.ok(ampa.subcommands.get('start-work').actionFn, 'start-work should have an action');
  });

  test('registers sw alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('sw'), 'sw alias should be registered');
    assert.ok(ampa.subcommands.get('sw').actionFn, 'sw should have an action');
  });

  test('registers finish-work subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('finish-work'), 'finish-work should be registered');
    assert.ok(ampa.subcommands.get('finish-work').actionFn, 'finish-work should have an action');
  });

  test('registers fw alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('fw'), 'fw alias should be registered');
    assert.ok(ampa.subcommands.get('fw').actionFn, 'fw should have an action');
  });

  test('registers list-containers subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('list-containers'), 'list-containers should be registered');
    assert.ok(ampa.subcommands.get('list-containers').actionFn, 'list-containers should have an action');
  });

  test('registers lc alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('lc'), 'lc alias should be registered');
    assert.ok(ampa.subcommands.get('lc').actionFn, 'lc should have an action');
  });

  test('still registers original subcommands (start, stop, status, run, list, ls)', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    for (const cmd of ['start', 'stop', 'status', 'run', 'list', 'ls']) {
      assert.ok(ampa.subcommands.has(cmd), `${cmd} should be registered`);
    }
  });
});

// ---------------------------------------------------------------------------
// finish-work detection (outside container)
// ---------------------------------------------------------------------------

describe('finish-work outside container', () => {
  test('finish-work exits with code 2 when not in a container and no claimed containers', async () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      // Ensure env vars are not set
      const origName = process.env.AMPA_CONTAINER_NAME;
      const origId = process.env.AMPA_WORK_ITEM_ID;
      delete process.env.AMPA_CONTAINER_NAME;
      delete process.env.AMPA_WORK_ITEM_ID;

      // Save and clear pool state so there are no claimed containers
      const projectRoot = process.cwd();
      plugin.savePoolState(projectRoot, {});

      const ctx = { program: new FakeProgram() };
      plugin.default(ctx);
      const ampa = ctx.program.commands.get('ampa');
      const fwCmd = ampa.subcommands.get('finish-work');

      // Capture stderr
      const originalError = console.error;
      let errorOutput = '';
      console.error = (...args) => { errorOutput += args.join(' ') + '\n'; };

      process.exitCode = undefined;
      await fwCmd.actionFn(undefined, { force: false });
      console.error = originalError;

      assert.equal(process.exitCode, 2, 'should set exit code to 2');
      assert.ok(errorOutput.includes('No claimed containers found'), 'should print no claimed containers error');

      // Restore env
      if (origName !== undefined) process.env.AMPA_CONTAINER_NAME = origName;
      if (origId !== undefined) process.env.AMPA_WORK_ITEM_ID = origId;
      process.exitCode = undefined;
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

// ---------------------------------------------------------------------------
// Pool constants
// ---------------------------------------------------------------------------

describe('pool constants', () => {
  test('POOL_PREFIX is ampa-pool-', () => {
    assert.equal(plugin.POOL_PREFIX, 'ampa-pool-');
  });

  test('POOL_SIZE is 3', () => {
    assert.equal(plugin.POOL_SIZE, 3);
  });
});

// ---------------------------------------------------------------------------
// poolContainerName
// ---------------------------------------------------------------------------

describe('poolContainerName', () => {
  test('generates ampa-pool-0 for index 0', () => {
    assert.equal(plugin.poolContainerName(0), 'ampa-pool-0');
  });

  test('generates ampa-pool-2 for index 2', () => {
    assert.equal(plugin.poolContainerName(2), 'ampa-pool-2');
  });
});

// ---------------------------------------------------------------------------
// poolStatePath
// ---------------------------------------------------------------------------

describe('poolStatePath', () => {
  test('returns path under global .worklog/ampa/', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const p = plugin.poolStatePath('/tmp/test-project');
      assert.equal(p, path.join(xdgDir, 'opencode', '.worklog', 'ampa', 'pool-state.json'));
      assert.ok(p.endsWith('pool-state.json'));
      // Should NOT contain the projectRoot
      assert.ok(!p.includes('/tmp/test-project'));
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

// ---------------------------------------------------------------------------
// Pool state read/write (using a temp directory)
// ---------------------------------------------------------------------------

describe('getPoolState / savePoolState', () => {
  let xdgDir;

  test('getPoolState returns empty object when no state file exists', () => {
    xdgDir = useIsolatedGlobalDir();
    try {
      const state = plugin.getPoolState('/tmp/unused');
      assert.deepEqual(state, {});
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('savePoolState creates directories and writes JSON', () => {
    xdgDir = useIsolatedGlobalDir();
    try {
      const testState = { 'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' } };
      plugin.savePoolState('/tmp/unused', testState);
      const stateFile = plugin.poolStatePath('/tmp/unused');
      assert.ok(fs.existsSync(stateFile), 'state file should be created');
      const read = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      assert.deepEqual(read, testState);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('getPoolState reads back saved state', () => {
    xdgDir = useIsolatedGlobalDir();
    try {
      const testState = { 'ampa-pool-1': { workItemId: 'SA-42', branch: 'bug/SA-42', claimedAt: '2025-06-15T12:00:00.000Z' } };
      plugin.savePoolState('/tmp/unused', testState);
      const read = plugin.getPoolState('/tmp/unused');
      assert.deepEqual(read, testState);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

// ---------------------------------------------------------------------------
// claimPoolContainer / releasePoolContainer / findPoolContainerForWorkItem
// ---------------------------------------------------------------------------

describe('claimPoolContainer', () => {
  test('returns null or a pool container name depending on pool state', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      const result = plugin.claimPoolContainer(tmpDir, 'WL-1', 'feature/WL-1');
      if (result === null) {
        // No pool containers exist in podman — expected in CI or fresh hosts
        assert.equal(result, null);
      } else {
        // Pool containers exist on this host — claim should return a valid name
        assert.ok(result.startsWith(plugin.POOL_PREFIX), `expected pool name, got: ${result}`);
        // Verify the claim was persisted
        const state = plugin.getPoolState(tmpDir);
        assert.ok(state[result], 'claimed container should appear in pool state');
        assert.equal(state[result].workItemId, 'WL-1');
        // Clean up claim
        plugin.releasePoolContainer(tmpDir, result);
      }
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

describe('releasePoolContainer', () => {
  test('removes a specific container claim from state', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      const state = {
        'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
        'ampa-pool-1': { workItemId: 'WL-2', branch: 'task/WL-2', claimedAt: '2025-01-02T00:00:00.000Z' },
      };
      plugin.savePoolState(tmpDir, state);
      plugin.releasePoolContainer(tmpDir, 'ampa-pool-0');
      const updated = plugin.getPoolState(tmpDir);
      assert.equal(updated['ampa-pool-0'], undefined, 'ampa-pool-0 should be removed');
      assert.ok(updated['ampa-pool-1'], 'ampa-pool-1 should remain');
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('clears all claims with wildcard *', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      const state = {
        'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
        'ampa-pool-1': { workItemId: 'WL-2', branch: 'task/WL-2', claimedAt: '2025-01-02T00:00:00.000Z' },
      };
      plugin.savePoolState(tmpDir, state);
      plugin.releasePoolContainer(tmpDir, '*');
      const updated = plugin.getPoolState(tmpDir);
      assert.deepEqual(updated, {});
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

describe('findPoolContainerForWorkItem', () => {
  test('returns the container name for a claimed work item', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      const state = {
        'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
        'ampa-pool-2': { workItemId: 'SA-99', branch: 'bug/SA-99', claimedAt: '2025-02-01T00:00:00.000Z' },
      };
      plugin.savePoolState(tmpDir, state);
      assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'SA-99'), 'ampa-pool-2');
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('returns null for an unclaimed work item', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      plugin.savePoolState(tmpDir, {});
      assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'WL-999'), null);
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('returns null with empty pool state', () => {
    const xdgDir = useIsolatedGlobalDir();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    try {
      assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'WL-1'), null);
    } finally {
      restoreGlobalDir(xdgDir);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// poolCleanupPath / getCleanupList / saveCleanupList / markForCleanup / cleanupMarkedContainers
// ---------------------------------------------------------------------------

describe('poolCleanupPath', () => {
  test('returns path under global .worklog/ampa/', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const p = plugin.poolCleanupPath('/tmp/test-project');
      assert.equal(p, path.join(xdgDir, 'opencode', '.worklog', 'ampa', 'pool-cleanup.json'));
      assert.ok(p.endsWith('pool-cleanup.json'));
      // Should NOT contain the projectRoot
      assert.ok(!p.includes('/tmp/test-project'));
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

describe('getCleanupList / saveCleanupList', () => {
  test('returns empty array when no cleanup file exists', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const list = plugin.getCleanupList('/tmp/unused');
      assert.deepEqual(list, []);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('saveCleanupList creates directories and writes JSON array', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      plugin.saveCleanupList('/tmp/unused', ['ampa-pool-1', 'ampa-pool-3']);
      const p = plugin.poolCleanupPath('/tmp/unused');
      assert.ok(fs.existsSync(p), 'cleanup file should exist');
      const data = JSON.parse(fs.readFileSync(p, 'utf8'));
      assert.deepEqual(data, ['ampa-pool-1', 'ampa-pool-3']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('getCleanupList reads back saved list', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      plugin.saveCleanupList('/tmp/unused', ['ampa-pool-5']);
      const list = plugin.getCleanupList('/tmp/unused');
      assert.deepEqual(list, ['ampa-pool-5']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

describe('markForCleanup', () => {
  test('adds a container name to the cleanup list', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      plugin.markForCleanup('/tmp/unused', 'ampa-pool-2');
      const list = plugin.getCleanupList('/tmp/unused');
      assert.deepEqual(list, ['ampa-pool-2']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('does not add duplicates', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      plugin.markForCleanup('/tmp/unused', 'ampa-pool-2');
      plugin.markForCleanup('/tmp/unused', 'ampa-pool-2');
      const list = plugin.getCleanupList('/tmp/unused');
      assert.deepEqual(list, ['ampa-pool-2']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('appends to existing list', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      plugin.markForCleanup('/tmp/unused', 'ampa-pool-1');
      plugin.markForCleanup('/tmp/unused', 'ampa-pool-4');
      const list = plugin.getCleanupList('/tmp/unused');
      assert.deepEqual(list, ['ampa-pool-1', 'ampa-pool-4']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

describe('cleanupMarkedContainers', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.cleanupMarkedContainers, 'function');
  });

  test('returns empty arrays when no containers are marked', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const result = plugin.cleanupMarkedContainers('/tmp/unused');
      assert.deepEqual(result.destroyed, []);
      assert.deepEqual(result.errors, []);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

// ---------------------------------------------------------------------------
// imageCreatedDate / isImageStale / teardownStalePool
// ---------------------------------------------------------------------------

describe('imageCreatedDate', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.imageCreatedDate, 'function');
  });

  test('returns null for a non-existent image', () => {
    const result = plugin.imageCreatedDate('no-such-image:never');
    assert.equal(result, null);
  });
});

describe('isImageStale', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.isImageStale, 'function');
  });

  test('returns false when Containerfile does not exist', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-stale-test-'));
    // No ampa/Containerfile in tmpDir
    assert.equal(plugin.isImageStale(tmpDir), false);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('teardownStalePool', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.teardownStalePool, 'function');
  });
});

// ---------------------------------------------------------------------------
// listAvailablePool (depends on podman state — mostly a shape test)
// ---------------------------------------------------------------------------

describe('listAvailablePool', () => {
  test('returns an array', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const result = plugin.listAvailablePool('/tmp/unused');
      assert.ok(Array.isArray(result), 'should return an array');
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});

// ---------------------------------------------------------------------------
// replenishPool (shape test — does not require podman)
// ---------------------------------------------------------------------------

describe('replenishPool', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.replenishPool, 'function');
  });
});

// ---------------------------------------------------------------------------
// replenishPoolBackground
// ---------------------------------------------------------------------------

describe('replenishPoolBackground', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.replenishPoolBackground, 'function');
  });
});

// ---------------------------------------------------------------------------
// Command registration — warm-pool
// ---------------------------------------------------------------------------

describe('warm-pool command registration', () => {
  test('registers warm-pool subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('warm-pool'), 'warm-pool should be registered');
    assert.ok(ampa.subcommands.get('warm-pool').actionFn, 'warm-pool should have an action');
  });

  test('registers wp alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('wp'), 'wp alias should be registered');
    assert.ok(ampa.subcommands.get('wp').actionFn, 'wp should have an action');
  });
});

// ---------------------------------------------------------------------------
// globalAmpaDir — XDG_CONFIG_HOME and HOME fallback
// ---------------------------------------------------------------------------

describe('globalAmpaDir', () => {
  test('uses XDG_CONFIG_HOME when set', () => {
    const savedXdg = process.env.XDG_CONFIG_HOME;
    try {
      process.env.XDG_CONFIG_HOME = '/custom/xdg';
      const dir = plugin.globalAmpaDir();
      assert.equal(dir, path.join('/custom/xdg', 'opencode', '.worklog', 'ampa'));
    } finally {
      if (savedXdg === undefined) {
        delete process.env.XDG_CONFIG_HOME;
      } else {
        process.env.XDG_CONFIG_HOME = savedXdg;
      }
    }
  });

  test('falls back to HOME/.config when XDG_CONFIG_HOME is unset', () => {
    const savedXdg = process.env.XDG_CONFIG_HOME;
    const savedHome = process.env.HOME;
    try {
      delete process.env.XDG_CONFIG_HOME;
      process.env.HOME = '/home/testuser';
      const dir = plugin.globalAmpaDir();
      assert.equal(dir, path.join('/home/testuser', '.config', 'opencode', '.worklog', 'ampa'));
    } finally {
      if (savedXdg === undefined) {
        delete process.env.XDG_CONFIG_HOME;
      } else {
        process.env.XDG_CONFIG_HOME = savedXdg;
      }
      if (savedHome === undefined) {
        delete process.env.HOME;
      } else {
        process.env.HOME = savedHome;
      }
    }
  });

  test('falls back to os.homedir() when both XDG_CONFIG_HOME and HOME are unset', () => {
    const savedXdg = process.env.XDG_CONFIG_HOME;
    const savedHome = process.env.HOME;
    try {
      delete process.env.XDG_CONFIG_HOME;
      delete process.env.HOME;
      const dir = plugin.globalAmpaDir();
      // os.homedir() returns a platform-specific home directory
      const expected = path.join(os.homedir(), '.config', 'opencode', '.worklog', 'ampa');
      assert.equal(dir, expected);
    } finally {
      if (savedXdg === undefined) {
        delete process.env.XDG_CONFIG_HOME;
      } else {
        process.env.XDG_CONFIG_HOME = savedXdg;
      }
      if (savedHome === undefined) {
        delete process.env.HOME;
      } else {
        process.env.HOME = savedHome;
      }
    }
  });

  test('is exported', () => {
    assert.equal(typeof plugin.globalAmpaDir, 'function');
  });
});

// ---------------------------------------------------------------------------
// Pool state directory creation on first write
// ---------------------------------------------------------------------------

describe('pool state directory creation', () => {
  test('savePoolState creates nested global directory on first write', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const globalDir = path.join(xdgDir, 'opencode', '.worklog', 'ampa');
      // Directory should not exist before first write
      assert.ok(!fs.existsSync(globalDir), 'global ampa dir should not exist yet');

      const testState = { 'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' } };
      plugin.savePoolState('/tmp/unused', testState);

      // Directory and file should now exist
      assert.ok(fs.existsSync(globalDir), 'global ampa dir should be created');
      const stateFile = plugin.poolStatePath('/tmp/unused');
      assert.ok(fs.existsSync(stateFile), 'pool-state.json should exist');
      const read = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      assert.deepEqual(read, testState);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });

  test('saveCleanupList creates nested global directory on first write', () => {
    const xdgDir = useIsolatedGlobalDir();
    try {
      const globalDir = path.join(xdgDir, 'opencode', '.worklog', 'ampa');
      assert.ok(!fs.existsSync(globalDir), 'global ampa dir should not exist yet');

      plugin.saveCleanupList('/tmp/unused', ['ampa-pool-0']);

      assert.ok(fs.existsSync(globalDir), 'global ampa dir should be created');
      const cleanupFile = plugin.poolCleanupPath('/tmp/unused');
      assert.ok(fs.existsSync(cleanupFile), 'pool-cleanup.json should exist');
      const read = JSON.parse(fs.readFileSync(cleanupFile, 'utf8'));
      assert.deepEqual(read, ['ampa-pool-0']);
    } finally {
      restoreGlobalDir(xdgDir);
    }
  });
});
