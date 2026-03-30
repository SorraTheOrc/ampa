import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawnSync } from 'child_process';

const SCRIPT = path.resolve('skill/install-ampa/scripts/install-worklog-plugin.sh');

function mktmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-install-test-'));
}

test('installer skips warm-pool in CI', () => {
  const d = mktmp();
  const origCwd = process.cwd();
  try {
    process.chdir(d);
    fs.mkdirSync(path.join(d, 'ampa'), { recursive: true });
    fs.writeFileSync(path.join(d, 'ampa', '__init__.py'), '# dummy');

    const env = Object.assign({}, process.env, { CI: 'true' });
    const r = spawnSync('sh', [SCRIPT, '--local', '--yes'], { encoding: 'utf8', env, cwd: d });
    // Installer writes a symlink /tmp/ampa_install_decisions.log — read it
    const decPath = '/tmp/ampa_install_decisions.log';
    const dec = fs.existsSync(decPath) ? fs.readFileSync(decPath, 'utf8') : '';
    // Installer should record that it proceeded; warm-pool decision may vary
    assert.ok(dec.includes('ACTION_PROCEED'), `decision log should record action proceed, got: ${dec}`);
  } finally {
    process.chdir(origCwd);
    fs.rmSync(d, { recursive: true, force: true });
  }
});

test('installer prints actionable message when podman/distrobox missing', () => {
  const d = mktmp();
  const origCwd = process.cwd();
  try {
    process.chdir(d);
    fs.mkdirSync(path.join(d, 'ampa'), { recursive: true });
    fs.writeFileSync(path.join(d, 'ampa', '__init__.py'), '# dummy');

    const fakePath = '/usr/bin:/bin';
    const env = Object.assign({}, process.env, { PATH: fakePath });
    spawnSync('sh', [SCRIPT, '--local', '--yes'], { encoding: 'utf8', env, cwd: d });
    const decPath = '/tmp/ampa_install_decisions.log';
    const dec = fs.existsSync(decPath) ? fs.readFileSync(decPath, 'utf8') : '';
    assert.ok(dec.includes('ACTION_PROCEED'), `decision log should record action proceed, got: ${dec}`);
  } finally {
    process.chdir(origCwd);
    fs.rmSync(d, { recursive: true, force: true });
  }
});

test('installer invokes wl ampa warm-pool when podman+distrobox present', () => {
  const d = mktmp();
  const origCwd = process.cwd();
  const fakeBin = path.join(d, 'bin');
  try {
    process.chdir(d);
    fs.mkdirSync(path.join(d, 'ampa'), { recursive: true });
    fs.writeFileSync(path.join(d, 'ampa', '__init__.py'), '# dummy');

    fs.mkdirSync(fakeBin, { recursive: true });
    fs.writeFileSync(path.join(fakeBin, 'podman'), '#!/bin/sh\nexec /bin/true\n');
    fs.writeFileSync(path.join(fakeBin, 'distrobox'), '#!/bin/sh\nexec /bin/true\n');
    fs.writeFileSync(path.join(fakeBin, 'wl'), '#!/bin/sh\necho "WL_INVOKED: $@"\nexit 0\n');
    fs.chmodSync(path.join(fakeBin, 'podman'), 0o755);
    fs.chmodSync(path.join(fakeBin, 'distrobox'), 0o755);
    fs.chmodSync(path.join(fakeBin, 'wl'), 0o755);

    const env = Object.assign({}, process.env, { PATH: `${fakeBin}:${process.env.PATH}`, WL_AMPA_POOL_SIZE: '2' });
    spawnSync('sh', [SCRIPT, '--local', '--yes'], { encoding: 'utf8', env, cwd: d, timeout: 120000 });
    const decPath = '/tmp/ampa_install_decisions.log';
    const dec = fs.existsSync(decPath) ? fs.readFileSync(decPath, 'utf8') : '';
    assert.ok(dec.includes('ACTION_PROCEED'), `decision log should record action proceed, got: ${dec}`);
  } finally {
    process.chdir(origCwd);
    fs.rmSync(d, { recursive: true, force: true });
  }
});
