const path = require('node:path');
const [sdk, folder, name, retention] = process.argv.slice(2);
const { DefaultArtifactClient } = require(path.join(sdk, 'node_modules/@actions/artifact'));

new DefaultArtifactClient().uploadArtifact(name, ['manifest.json', 'payload.json'].map(file => path.join(folder, file)), folder, { retentionDays: Number(retention), compressionLevel: 6 })
  .then(result => { process.stdout.write(JSON.stringify({ id: result.id, size: result.size, digest: result.digest })); })
  .catch(() => { process.stderr.write('Checkpoint artifact upload failed.\n'); process.exitCode = 1; });
