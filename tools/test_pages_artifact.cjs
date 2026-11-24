const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const workflow = fs.readFileSync(path.join(__dirname, '../.github/workflows/deploy.yml'), 'utf8');
const source = workflow.split('          script: |\n')[1].split('      - name: Deploy the validated artifact')[0]
  .split('\n').filter(line => line.trim()).map(line => line.slice(12)).join('\n');
const artifact = {id: 42, name: 'github-pages', expired: false, size_in_bytes: 123};

async function run(responses, expected='42') {
  let calls=0, sleeps=0;
  const context = {
    process: {env: {PAGES_ARTIFACT_ID: expected}},
    context: {repo: {owner:'test-owner',repo:'test-repo'}, runId: 1234},
    core: {info() {}},
    setTimeout(callback, ms) { assert.equal(ms,5000); sleeps++; callback(); },
    github: {rest: {actions: {async listWorkflowRunArtifacts(options) {
      assert.equal(options.run_id,1234);
      assert.equal(options.owner,'test-owner');
      assert.equal(options.repo,'test-repo');
      const items=responses[Math.min(calls,responses.length-1)];calls++;
      return {data:{artifacts:items}};
    }}}}
  };
  try {
    await vm.runInNewContext('(async () => {\n'+source+'\n})()',context);
    return {calls,sleeps};
  } catch (error) {
    error.calls=calls;error.sleeps=sleeps;throw error;
  }
}

(async()=>{
  assert.deepEqual(await run([[],[],[artifact],[artifact]]),{calls:4,sleeps:3});
  assert.deepEqual(await run([[artifact],[],[artifact],[artifact]]),{calls:4,sleeps:3});
  assert.deepEqual(await run([[artifact]]),{calls:2,sleeps:1});
  await assert.rejects(run([[]]),error=>error.calls===10 && error.sleeps===9 && /bounded wait/.test(error.message));
  await assert.rejects(run([[{...artifact,id:43}]]),/unexpected/);
  await assert.rejects(run([[{...artifact,expired:true}]]),/expired/);
  await assert.rejects(run([[{...artifact,size_in_bytes:0}]]),/empty/);
  await assert.rejects(run([[artifact,artifact]]),/Multiple/);
  await assert.rejects(run([[artifact]],''),error=>error.calls===0 && /valid Pages artifact ID/.test(error.message));
  await assert.rejects(run([[artifact]],'9007199254740993'),error=>error.calls===0);
  console.log('Artifact visibility guard: 10 regression checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
