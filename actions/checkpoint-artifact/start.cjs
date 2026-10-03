const { spawn } = require('node:child_process');
const path = require('node:path');

const keys = ['ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY', 'GITHUB_SERVER_URL'];
const credentials = Object.fromEntries(keys.filter(key => process.env[key]).map(key => [key, process.env[key]]));
const kit = path.resolve(__dirname, '../..');
const child = spawn('/usr/bin/sudo', ['-n', 'env', `PYTHONPATH=${kit}`, 'python3', '-m', 'nexkit.checkpoints', 'monitor', '--context', process.env.INPUT_CONTEXT, '--role', process.env.INPUT_ROLE, '--source', process.env.INPUT_SOURCE, '--node', process.execPath, '--sdk', process.env.INPUT_SDK], { stdio: ['pipe', 'inherit', 'inherit'] });
child.stdin.end(JSON.stringify(credentials));
child.on('error', () => { process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code === 0 ? 0 : 1; });
