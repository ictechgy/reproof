# Bounded native-language sample logic repair

This repository-owned exercise compiles the actual Swift sample business logic
and runs its existing XCTest oracle on a macOS host. It is not iOS UI execution,
SDK capture, scenario replay, an AI integration, an arbitrary-code sandbox, or
company-app acceptance. The intentionally buggy sample files remain unchanged in
the checkout. A scripted candidate changes only a temporary frozen source copy.

## The causal check

`ios/Sample/CounterViewController.swift` calls `SubmissionLogic.shouldAccept` in
the duplicate-submission flow. The original `SubmissionLogic.swift` returns
`true`. The unchanged `ios/LogicTests/LogicTests.swift` contains exactly **one
selected testcase with two assertions**:

- Line 12: accept a first submission (`submitted: false`).
- Line 13: reject a repeated submission (`submitted: true`).

The supervisor freezes the exact checked-in source bytes, full original test
file, callsite, independent SwiftPM manifest, and recipe before proposing any
candidate. The manifest constructs a host module named `ReproSample` from the
three existing sample logic files, so the original test file and its testable
import need no edits. The counter and reset tests are compiled but are not run
by this selected duplicate-submission check. This is not the full sample suite.

Every native stage gets a fresh package and build directory. Compilation must
succeed before a test result is inspected. The same exact XCTest filter is used
for all native observations:

| Source expression | Required observed result |
| --- | --- |
| Baseline `true` | One selected testcase fails specifically at line 13, `XCTAssertFalse`. |
| Wrong patch `false` | That same testcase fails specifically at line 12, `XCTAssertTrue`. |
| Fixed patch `!submitted` | That same testcase passes both assertions with no skips. |
| No-op `true` | `RepairSource` rejects before candidate compilation. |

`RepairSource` enforces original-source binding and editable-path limits. The
existing `validate_swift_expression` permits only this finite Boolean expression
change while preserving every surrounding byte. Independent supervisor-side
hash pins prevent replacing the source, test, or executable SwiftPM manifest by
editing a descriptor to agree with the replacement. Recipe bytes must match the
fixed recipe. Oracle, manifest, recipe, callsite, project descriptor, evaluator,
and undeclared paths are not candidate-editable.

## Ticket correlation, without invented authority

A separate, explicitly synthetic duplicate-submission ticket, sealed capsule,
log, and authored specification exercise the real `intake_ticket` adapter. The
specification's expected predicate includes both Boolean requirements, and its
provenance names the frozen XCTest file digest. This is not the earlier shipping
threshold fixture. Project/source/specification/capsule/log references must all
match before the compiler is invoked.

The synthetic recording's schema-required launch action is never dispatched.
It is not SDK capture or evidence of an installed iOS artifact. The project
schema's original artifact digest identifies the pinned Swift source, not an
app binary; the report says so separately. Real host test executable hashes are
measured after compilation and included in each observation.

The intake result remains `review-required`, `approved=false`,
`executionAuthority=none`, and `aiTransfer=false`. A bounded explicit CLI/CI
invocation selects this local exercise. A ticket does not authorize execution,
register a company project, approve a specification, or grant repair authority.

## Executable entrypoints

On a Mac with the existing official Xcode 16.4 installation:

```sh
python scripts/qa-native-logic-repair.py --output /new/local/fixed-proof
python scripts/qa-native-logic-repair.py --candidate wrong --output /new/local/wrong-proof
python scripts/qa-native-logic-repair.py --candidate noop --output /new/local/noop-proof
python scripts/release-check.py --goal NATIVE_LOGIC_RUNTIME --effects filesystem,process,native-compile
```

The fixed case exits 0 only after the full native result checks. The wrong patch
exits 1 only after its expected native failure is verified. No-op and all
unavailable/invalid conditions exit 2. A new output directory is required when
`--output` is supplied; it retains logs, xUnit XML, package sources, and binaries.
No source path, executable, provider, patch text, or arbitrary command can be
supplied to the CLI. No tools are installed and no credentials are inherited.
The dependency-free manifest needs no package downloads. This does not claim OS
network isolation or protection against a hostile host/toolchain.

Portable contract tests do **not** execute Swift:

```sh
python scripts/release-check.py --goal NATIVE_LOGIC_CONTRACTS --effects filesystem,process
```

The portable workflow invokes this separate goal for each supported Python
version. Its fabricated parser fixtures and mocked control-flow tests are
explicitly not native execution evidence. The existing Mac workflow invokes the
native goal with its verified preinstalled Xcode and Python. The fixed, wrong, and no-op candidates have separate native release checks.
Each has a 360-second outer budget: fixed/wrong command stages total at most
270 seconds (two 15-second version queries, two 90-second builds, and two
30-second tests); no-op totals 150 seconds. The remaining budget covers bounded
cleanup, hashing, and serialization. Native tests contain
no mocks and cannot skip to green: unavailable tools fail the goal. Both stages
must pass independently. Actual Mac runtime compatibility must be established
by that run, not inferred from portable tests or source inspection.

## Result and process integrity

SwiftPM 6.1.2 generates XCTest xUnit only in its parallel-runner path, and its XML
failure message is generic. The fixed invocation therefore requests `--parallel
--num-workers 1 --verbose --enable-xctest --disable-swift-testing`, as documented
by the [official SwiftPM implementation](https://github.com/swiftlang/swift-package-manager/blob/swift-6.1.2-RELEASE/Sources/Commands/SwiftTestCommand.swift).
The evaluator checks both independently produced xUnit and native XCTest output:

- Exactly one expected testcase, one suite, and consistent exit/failure counts.
- Ordered native suite/case start and finish records and complete summaries.
- The exact frozen assertion path, line, and assertion type for each expected failure.
- No skipped, mixed Swift Testing, duplicate, malformed, truncated, unknown-case,
  setup/compile, crash, or contradictory results.

Compiler/runtime version output, exact invocations, source and build-input
hashes, protected-input hashes, native executable hashes, and result/log hashes
are retained. Source and binary bytes are checked again after execution. Logs
are bounded; wall-clock deadlines are enforced even if the process exits just
as the deadline expires.

Each command has an isolated environment and an owned process group. Active
unreaped groups receive bounded termination before the leader is reaped. A
leader already reaped by polling is never signaled again by its numeric PGID;
only bounded absence checks follow. SIGTERM/SIGINT cancellation flags leads through
owned-group cleanup, including on repeated termination signals. Unconfirmed
cleanup fails closed. There is
no claim of containment against descendants deliberately escaping their group,
PID namespace attacks, or arbitrary candidate code.
