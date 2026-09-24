# iOS preparation status and recovery CLI

`reproof ios-mobile` operates on an already admitted iOS IPA preparation
journal. It has two offline commands:

```text
reproof ios-mobile status --config /absolute/reference.json --operation mobile-one
reproof ios-mobile recover --config /absolute/reference.json --operation mobile-one \
  --request-digest <original-request-sha256> --timeout-seconds 30
```

The reference is exported by the original `IOSMobileOperationStore`. It binds
the original run store root, owner root, environment, configuration digest,
scope, and `IOSMobileDefinition`. The private device reference is used only to
reconstruct that definition; it is omitted from `public()` and the
configuration object's representation. The reference contains no IPA
baseline, query executable, signing credential, or device service.

`status` is read-only and reports the journal state, preparation roles,
reservation, and historical native ownership. It never starts a tool or
device service. `recover` verifies the supplied request digest, refuses
terminal operations without consuming anything, and then performs only
preparation cleanup through `preparation_recovery` and
`RunStore.finish_ios_preparation_recovery`. The operation's reservation is
released only after the cleanup capability is consumed.

Journals with a native binding remain quarantined and reserved. They cannot be
handled by this preparation-only command. Use the authenticated
`protected-service ios-recover` path in [iOS protected service](IOS-PROTECTED-SERVICE.md)
with the original registered inputs and a fresh native recovery lease. Cancellation and the timeout are bounded; interrupted or rejected
operations return a redacted JSON error and do not expose paths or device
identifiers.
