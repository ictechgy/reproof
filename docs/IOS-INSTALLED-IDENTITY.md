# iOS installed identity observation

`IOSMobileInstaller.observe_installed()` performs a read-only identity check
after a successful fixed install command. It accepts only the exact
`IOSInstallObservation` object issued by the same native owner. The command
intent and `tool-succeeded` state are reread under the owner directory, and
the complete intent digest must match the issuing owner's retained digest.
The saved successful result remains bound to the observation evidence digest.

The observer reparses the prepared source app for the command role and checks
its bundle identifier, short version, and build. It then issues a fresh
`apps` query through the installer’s original pinned query client and compares
the CoreDevice row fields `bundleIdentifier`, `version`, and `bundleVersion`
to those source values. The source app digest comes from the parser-issued
prepared capability; it is never inferred from CoreDevice metadata.

The returned immutable observation reports the native binding, operation
context, source role and app digest, installed identity fields, and query
evidence digest. `identityConfirmed` is `true`; `installedArtifactVerified`,
`deviceCleanupConfirmed`, and `executionAuthority` remain `false`, `false`,
and `none`. The observation does not write operation state, issue authority,
confirm a receipt, or perform cleanup. Cancellation, expiry, closed owners,
foreign/copied observations, stale command state, and mismatched app identity
are rejected with a redacted tool error.
