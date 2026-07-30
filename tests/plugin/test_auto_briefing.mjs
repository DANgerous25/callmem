#!/usr/bin/env node
// Behavioural harness for src/callmem/templates/opencode/plugins/auto-briefing.js
//
// Imports the plugin module with a mocked plugin API surface matching the
// shapes in @opencode-ai/plugin's Hooks / PluginInput types, fires the real
// session.created event shape (properties.info.id, per
// @opencode-ai/sdk EventSessionCreated), and asserts:
//   1. the briefing subprocess is invoked exactly once per new session
//   2. injection into experimental.chat.system.transform's output happens
//      exactly once
//   3. a second event/transform call for the same session does not
//      double-inject or re-run the subprocess
//   4. sub-sessions (parentID set) are never injected
//   5. a failing/timing-out briefing logs quietly and never throws
//
// Runs with plain node assertions (no test framework dependency) so it can
// be invoked directly or shelled out to from pytest.

import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync, chmodSync, readFileSync, existsSync } from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"

const PLUGIN_PATH = path.resolve(
  import.meta.dirname,
  "../../src/callmem/templates/opencode/plugins/auto-briefing.js",
)

let importCounter = 0
async function loadPlugin() {
  importCounter += 1
  return (await import(pathToFileURL(PLUGIN_PATH).href + "?case=" + importCounter)).default
}

function makeFakeCallmem(dir, { text, exitCode = 0, delayMs = 0, countFile }) {
  const script = path.join(dir, "callmem")
  const body = `#!/usr/bin/env bash
${countFile ? `echo x >> "${countFile}"` : ""}
${delayMs > 0 ? `sleep ${delayMs / 1000}` : ""}
if [ "${exitCode}" != "0" ]; then
  echo "boom" 1>&2
  exit ${exitCode}
fi
cat <<'EOF'
${text}
EOF
`
  writeFileSync(script, body)
  chmodSync(script, 0o755)
  return dir
}

function makeCtx(directory) {
  const logs = []
  return {
    ctx: {
      client: {
        app: {
          log: async (args) => {
            logs.push(args)
          },
        },
      },
      directory,
    },
    logs,
  }
}

function sessionCreatedEvent(id, parentID) {
  return {
    event: {
      type: "session.created",
      properties: { info: { id, parentID, projectID: "p1", directory: "/tmp", title: "t" } },
    },
  }
}

async function withPath(extraDir, fn) {
  const original = process.env.PATH
  process.env.PATH = `${extraDir}:${original}`
  try {
    return await fn()
  } finally {
    process.env.PATH = original
  }
}

let failures = 0
async function test(name, fn) {
  try {
    await fn()
    console.log(`ok - ${name}`)
  } catch (err) {
    failures += 1
    console.error(`not ok - ${name}`)
    console.error(err)
  }
}

await test("injects briefing text once into system.transform output", async () => {
  const binDir = mkdtempSync(path.join(tmpdir(), "callmem-bin-"))
  const countFile = path.join(binDir, "count")
  makeFakeCallmem(binDir, { text: "BRIEFING-CONTENT-1", countFile })

  await withPath(binDir, async () => {
    const factory = await loadPlugin()
    const { ctx } = makeCtx(mkdtempSync(path.join(tmpdir(), "callmem-proj-")))
    const hooks = await factory(ctx)

    await hooks.event(sessionCreatedEvent("sess-1"))

    const output1 = { system: [] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "sess-1" }, output1)

    assert.equal(output1.system.length, 1, "should inject exactly one system entry")
    assert.ok(output1.system[0].includes("BRIEFING-CONTENT-1"), "injected text should contain briefing content")

    // Second turn, same session: must not double-inject.
    const output2 = { system: [] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "sess-1" }, output2)
    assert.equal(output2.system.length, 0, "second turn must not re-inject")

    const invocations = existsSync(countFile) ? readFileSync(countFile, "utf8").trim().split("\n").length : 0
    assert.equal(invocations, 1, "briefing subprocess should run exactly once")
  })
})

await test("duplicate session.created events do not double-spawn the subprocess", async () => {
  const binDir = mkdtempSync(path.join(tmpdir(), "callmem-bin-"))
  const countFile = path.join(binDir, "count")
  makeFakeCallmem(binDir, { text: "BRIEFING-CONTENT-2", countFile })

  await withPath(binDir, async () => {
    const factory = await loadPlugin()
    const { ctx } = makeCtx(mkdtempSync(path.join(tmpdir(), "callmem-proj-")))
    const hooks = await factory(ctx)

    await hooks.event(sessionCreatedEvent("sess-2"))
    await hooks.event(sessionCreatedEvent("sess-2")) // duplicate fire

    const output = { system: [] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "sess-2" }, output)
    assert.equal(output.system.length, 1)

    const invocations = readFileSync(countFile, "utf8").trim().split("\n").length
    assert.equal(invocations, 1, "duplicate session.created must not spawn a second subprocess")
  })
})

await test("sub-sessions (parentID set) are never injected", async () => {
  const binDir = mkdtempSync(path.join(tmpdir(), "callmem-bin-"))
  const countFile = path.join(binDir, "count")
  makeFakeCallmem(binDir, { text: "SHOULD-NOT-APPEAR", countFile })

  await withPath(binDir, async () => {
    const factory = await loadPlugin()
    const { ctx } = makeCtx(mkdtempSync(path.join(tmpdir(), "callmem-proj-")))
    const hooks = await factory(ctx)

    await hooks.event(sessionCreatedEvent("sub-sess-1", "parent-sess-1"))

    const output = { system: [] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "sub-sess-1" }, output)
    assert.equal(output.system.length, 0, "sub-session must not get a briefing")
    assert.ok(!existsSync(countFile), "sub-session must not spawn the briefing subprocess at all")
  })
})

await test("briefing failure logs quietly and never throws or injects", async () => {
  const binDir = mkdtempSync(path.join(tmpdir(), "callmem-bin-"))
  makeFakeCallmem(binDir, { text: "unused", exitCode: 1 })

  await withPath(binDir, async () => {
    const factory = await loadPlugin()
    const { ctx, logs } = makeCtx(mkdtempSync(path.join(tmpdir(), "callmem-proj-")))
    const hooks = await factory(ctx)

    await hooks.event(sessionCreatedEvent("sess-4"))

    const output = { system: [] }
    await assert.doesNotReject(
      hooks["experimental.chat.system.transform"]({ sessionID: "sess-4" }, output),
    )
    assert.equal(output.system.length, 0, "failed briefing must not inject anything")
    assert.equal(logs.length, 1, "failure should log exactly one quiet line")
    assert.equal(logs[0].body.level, "info")
  })
})

await test("missing callmem binary on PATH is handled without throwing", async () => {
  // Deliberately do not create a `callmem` executable anywhere on PATH.
  const emptyBinDir = mkdtempSync(path.join(tmpdir(), "callmem-empty-"))
  const originalPath = process.env.PATH
  process.env.PATH = emptyBinDir // no fallback to the real PATH, forces ENOENT
  try {
    const factory = await loadPlugin()
    const { ctx, logs } = makeCtx(mkdtempSync(path.join(tmpdir(), "callmem-proj-")))
    const hooks = await factory(ctx)

    await hooks.event(sessionCreatedEvent("sess-5"))

    const output = { system: [] }
    await assert.doesNotReject(
      hooks["experimental.chat.system.transform"]({ sessionID: "sess-5" }, output),
    )
    assert.equal(output.system.length, 0)
    assert.equal(logs.length, 1)
  } finally {
    process.env.PATH = originalPath
  }
})

if (failures > 0) {
  console.error(`\n${failures} test(s) failed`)
  process.exit(1)
} else {
  console.log("\nall tests passed")
}
