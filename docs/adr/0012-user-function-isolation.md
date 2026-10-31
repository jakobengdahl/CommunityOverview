# ADR 0012 — User-function isolation: a hardened-subprocess default runner

- **Status:** Proposed
- **Date:** 2026-10-14
- **Scope:** Open-source core
- **Related:** [`USER_FUNCTION_CONTRACT.md`](../USER_FUNCTION_CONTRACT.md) §3,
  §3.1 and §8 (the requirements a runner meets, amended by this ADR);
  [ADR 0011](0011-sandboxed-user-functions.md) (which left the isolation runtime
  open)

## Context

ADR 0011 adopted the user-function contract but left the isolation runtime
open: any runner meeting the contract's invariants and limits qualifies. The
open core needs a default runner before functions can run at all.

That default has to work where the open core is actually deployed. The
application image runs as a non-root user without added capabilities, usually
in a single container on a managed container platform or an orchestrator, and
often on a host the operator does not control. The runner therefore cannot
depend on root, on `CAP_SYS_ADMIN`, on user namespaces (often disabled for
unprivileged containers), on a container runtime inside the container, or on
`/dev/kvm`.

### Options considered

| Option | Boundary | Works unprivileged in a container | Notes |
|---|---|---|---|
| Plain subprocess with resource limits | None beyond Unix permissions | Yes | Reads everything the application user can; full network. |
| Subprocess + Landlock + seccomp + resource limits ("hardened subprocess") | Kernel LSM and syscall filter | Yes, where the kernel provides Landlock | Stdlib only; needs Landlock ABI 6 (Linux 6.12) for signal and abstract-socket scoping. |
| bubblewrap, nsjail | Namespaces (+ seccomp, cgroups) | Usually not: needs user namespaces or privileges | Strong where available. |
| Container per run (rootless runtimes, Docker-in-container) | Namespaces + seccomp | Usually not: needs privileges or a host socket | Mounting a host container socket is equivalent to host root. |
| gVisor per run | User-space kernel | Not per run without root or user namespaces | Platforms that already run the whole application under gVisor are a different case (below). |
| microVM per run | Hardware virtualization | No: needs KVM | Strongest; a hosted-layer option. |
| CPython on WASI | WebAssembly sandbox | Yes, anywhere | Pure Python only: no C extensions or threads; the Node.js runtime has no equivalent. |

### What the prototype showed

A stdlib-only prototype of the hardened subprocess was run against a suite of
escape attempts, on a Linux host and inside an unprivileged container
(non-root user, all capabilities dropped, `no-new-privileges`, the default
container seccomp profile):

- **Plain subprocess (resource limits only):** the function read system files
  and the application's source tree, wrote outside its directory, opened
  outbound TCP and UDP sockets and netlink sockets, and created a user
  namespace.
- **Hardened subprocess:** every one of those attempts was refused. So were
  `ptrace` of and signals to the application, starting subprocesses, forking,
  `setsid`, and reading `/proc`. Threads kept working. An allocation loop
  stopped with `MemoryError` below the 256 MiB address-space limit, and a busy
  loop was terminated by the host's wall-clock timer. Start-up overhead was
  about 70 ms on the host and 130–250 ms in the container.

Four findings shape the decision:

1. **`setsid` defeats process-group termination.** Without a syscall filter, a
   function forked a child that called `setsid`, survived the host's
   `killpg`, kept the output pipe open and stalled the host. Terminating "the
   run" requires either preventing new processes or collecting them.
2. **The host must bound what it reads.** A function writing to standard output
   in a loop made an unbounded reader buffer over 500 MB. The contract's size
   limits have to be enforced while reading, not after.
3. **A function runs as the application's own user.** An unprivileged
   application cannot switch users for its children. Without Landlock, nothing
   stops a function from reading what the application user can read: its data
   files, its configuration and the `/proc` entries of the application
   process, including its environment.
4. **Landlock does not cover connecting to a Unix socket file.** With Unix
   sockets allowed, the function connected to a socket file the host had
   created and sent it data. Any reachable local socket (a container runtime's
   control socket, the application's own) would be exposed. Creating sockets
   has to be denied outright; a connected pair from `socketpair` remains
   available.

The address-space limit suits CPython but not Node.js: Node.js 22 failed to
start under a 256 MiB address-space limit and started under 1 GiB, because its
JavaScript engine reserves address space up front. The Node.js runtime
therefore enforces the memory limit through the engine's heap limit under a
larger address-space ceiling (contract §8).

Two properties could not be verified here: whether Landlock is available on
platforms that run applications under a user-space kernel, and how the
address-space limit interacts with native extensions that reserve large
virtual ranges. The runner probes for the first at start-up; the second is
checked when dependencies are supported (contract §13).

## Decision

### 1. The default runner is a hardened subprocess

The open core's default runner starts each run as a separate process with an
empty environment, its own working directory and no inherited file descriptors
besides its standard streams, after applying, in this order:
resource limits (address space, CPU time as a backstop, file size, open files,
no core dumps); `no_new_privs`; a Landlock ruleset (read-only access to the
runtime, the helper library and the function's directory; full access to the
working directory; TCP bind and connect denied; signals and abstract Unix
sockets scoped to the run); and a seccomp filter. The filter denies creating sockets of
any family (`socketpair` remains), allows `clone` only for threads, makes `clone3` unavailable so the C
library falls back to `clone`, and denies `fork` and `vfork` (on architectures
that have them), `setsid`, `setpgid`,
`ptrace`, cross-process memory access, namespace and mount calls, `bpf`,
`userfaultfd`, `perf_event_open`, `io_uring` and module loading. It rejects
other architectures' system-call conventions outright. It needs no privileges
and no third-party dependency.

The per-run supervisor also traces the run's storage-changing system calls,
with defaults and platform maximums of 256 MiB and 10,000 entries. The runner's
`max_disk_bytes` and `max_disk_files` constructor settings accept lower quotas
and reject invalid values at startup. Storage-changing calls are serialized,
and usage is scanned around each one, at every process exit before descriptors
close, and once more after termination. Scans count entries recursively (files
and directories, excluding the root), sum the larger of apparent size and
allocated bytes per entry, and never follow symbolic links. Exceeding either
quota terminates the run and its descendants and returns `failed` with
`disk_exceeded`, even if the function sent a result; scan failures stop the run
with `runner_lost`. Scans also count unlinked regular files held open by the run
and its descendants, deduplicating descriptors by inode. The 16 MiB
`RLIMIT_FSIZE` per-file limit remains in place.

This is best-effort accounting, not a filesystem quota: a call that sleeps
waiting for another of the run's processes (a lock, a FIFO open) yields the
serialization so the run cannot stall, so usage may overshoot until the next
scan; data buffered in FIFOs or pipes is not counted. Operators should also
limit the container/host storage backing the work directory, using an
ephemeral-storage limit or size-capped tmpfs with an inode limit where supported (see the
[Deployment Guide](../DEPLOYMENT_GUIDE.md#user-function-temporary-storage)).

### 2. Runners report an isolation level, and instances set a floor

A runner probes the host when it starts and reports which of the contract's
controls it enforces (contract §3.1). With all four, its level is `full`;
otherwise `degraded`. For the default runner on x86-64 and arm64 Linux:

| Control | Enforced by | Requires |
|---|---|---|
| `filesystem` | Landlock filesystem rules | Landlock ABI 3 (Linux 6.2), for truncation |
| `network` | seccomp: `socket` denied | seccomp filter mode, a system-call table for the architecture |
| `processes` | seccomp: non-thread `clone`, `fork` and `vfork` where the architecture has them, `setsid`, `setpgid` denied | as `network` |
| `isolation` | seccomp: `ptrace`, cross-process memory access denied; Landlock: signals scoped, `/proc` outside the allowed paths | as `network`, plus Landlock ABI 6 (Linux 6.12) |

TCP bind and connect are also denied through Landlock (ABI 4) where available,
as a second layer under the seccomp rule.

By default an instance runs functions at either level. At `degraded`, the host
logs a warning naming the missing controls and every run record carries them.
An instance that sets `isolation_minimum: full` rejects runs below it. This
keeps functions usable on development machines and on hosts without Landlock,
while letting an operator require full confinement.

Whatever the level, a runner reads a run's output within the contract's
limits and terminates the run's process. Where it cannot prevent new processes,
a per-run supervisor process, registered as a child subreaper, adopts orphaned
descendants and kills them all before the run is recorded as finished. Without
the `isolation` control the function runs as the same user as that supervisor
and can kill it, so at `degraded` termination of descendants is best-effort; a
run whose supervisor dies is recorded as `failed` with `runner_lost`. At `degraded`
without the `processes` control, a run also shares the application user's
process limit, so a fork loop can exhaust it.

### 3. The runner is a replaceable seam

The host talks to a runner through a narrow seam, so stronger runners can be
added without changing the broker or the contract:

- **probe:** the isolation level, the controls enforced and the runtimes
  supported;
- **start:** a run specification (run id, runtime, entry point, the function's
  files as content or a digest rather than a host path, limits, an empty
  environment), returning the process's standard streams;
- **kill:** forced and idempotent;
- **wait:** the outcome (exited, timed out, memory exceeded, killed, failed to
  start) and the usage the runner could measure.

A runner never retries: a run is executed at most once (contract §11). Further
runners are additive: a WebAssembly runner for portable pure-Python functions,
a sidecar runner that executes in a separate container without network or
credentials, and, outside this repository, stronger boundaries for hosted
deployments that run code from mutually untrusted authors.

### 4. The contract text is amended, not versioned

Contract §3 now qualifies invariant 2, invariants 4, 6 and 7 insofar as they
rest on it, and the process-termination part of invariant 5 by isolation level and §3.1 defines the
levels; §8 allows the memory limit to be an address-space limit and defines
working-directory byte and entry quotas with best-effort accounting; §11 adds the
`isolation` field and the `isolation_below_minimum` and `disk_exceeded` codes;
invariant 5 now covers every process a run starts; §12 now states that text may be amended
until a host implements it. No host implements contract version 1 yet, so its
text is amended in place rather than bumped.

## Consequences

- Functions can run on any Linux host the application runs on, with no new
  dependency and no privileges, and with full confinement on x86-64 and arm64
  where the kernel provides seccomp and Landlock ABI 6 (Linux 6.12) or later.
- A function cannot start subprocesses or call native tools; it is plain code
  that talks to the broker. Threads work.
- At level `degraded`, which is the default floor, a function can read what the
  application user can read, including credentials in its environment or
  files. The warning and the run record make this visible; operators close it
  with `isolation_minimum: full`. This trade-off is deliberate: it keeps
  functions available on hosts without Landlock.
- The seccomp filter is architecture-specific. Each supported architecture
  needs its own system-call table, and the probe reports the `processes`,
  `network` and `isolation` controls as missing on an architecture without one.
- The kernel remains the boundary. A kernel bug reachable through the allowed
  system calls is not contained; that residual risk is acceptable for operator
  authored functions in a self-hosted instance and is why hosted, multi-author
  deployments need a stronger runner.

## What this ADR does not decide

- The runner for hosted, multi-tenant deployments.
- How third-party dependencies of a function are installed (contract §13).
- The WebAssembly and sidecar runners, beyond their place behind the seam.
