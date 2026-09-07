# Codex sandbox inside the factory container

The pinned Codex CLI (0.153.4) uses bubblewrap to enforce `read-only` and
`workspace-write`. Docker's default seccomp policy rejects namespace creation;
its default AppArmor policy rejects bubblewrap's mounts. On this Ubuntu host,
using `apparmor=unconfined` also triggers the host's unprivileged-user-namespace
restriction. Keep that host restriction enabled.

The factory service selects the two profiles in this directory. It runs as its
existing non-root user, drops all container capabilities, and enables
`no-new-privileges`. The kernel grants bubblewrap capabilities inside its new
user namespace to build the inner sandbox. No capabilities are added to the
outer container. Codex's native permissions remain enabled.

## Host setup

Requires Docker with AppArmor, an AppArmor parser supporting ABI 4.0, and enabled
unprivileged user namespaces. From the repository root, install and load the
factory-specific profile:

```bash
sudo install -m 0644 deploy/security/software-factory.apparmor /etc/apparmor.d/software-factory
sudo apparmor_parser -r /etc/apparmor.d/software-factory
```

This registers a named profile; only containers selecting `software-factory`
use it. No Docker restart or host sysctl change is required. The installed file
is loaded by AppArmor on boot. Keep `deploy/security/` beside `compose.yaml` in
the deployment directory: Compose reads the seccomp JSON on the client host.

## Verification

Validated on 2026-09-06 with Docker 29.8.0, kernel 7.0.0-31-generic, and the
existing `software-factory:v0-prototype` image
(`sha256:2585820cbdde321a7359d4d23792d1bc7349697c3bac47f3f15d09fed0a0ee0b`).
The offline boundary checks and authenticated Codex subscription smoke passed.
The live transcript shows `cat smoke.txt` exiting 0 with `ORBIT`; the response
passed schema validation, the marker file was unchanged, and no additional
workspace files were created. Evidence is under
`.scratch/live/smoke/codex-subscription-a182ce89-8a59-4185-85f1-540abe0f8f32/`.
The installed AppArmor profile matches the repository copy; the host's
`kernel.apparmor_restrict_unprivileged_userns` remains `1`.

Run the offline regression probe with the existing image (or pass another image
reference as the first argument):

```bash
bash scripts/check-codex-sandbox software-factory:v0-prototype
```

It runs the real CLI without a model, credentials, host mounts, or network. It
requires successful file reads, denial of writes in read-only mode, and correct
workspace and `.git` write boundaries in workspace-write mode. Follow it with
`factory smoke --agent codex --auth subscription --output /evidence` in a
one-off container using the same security options and native auth/evidence
mounts. Require actual file access, schema-valid output containing the marker,
and unchanged input. Authentication success alone is insufficient.

## Policy changes and source

The profiles derive from
[moby/profiles revision 61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31](https://github.com/moby/profiles/tree/61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31).
The upstream Apache-2.0 license is included as `LICENSE.moby`.

- Seccomp retains the upstream default deny action and rules, adding `clone`
  with `CLONE_NEWUSER`, `unshare` limited to namespace flags excluding cgroups,
  and `mount`, `umount2`, and `pivot_root`. `clone3` retains the upstream ENOSYS
  fallback. These exceptions expose the namespace/mount kernel interfaces to
  this trusted-code container; kernel capability checks still apply.
- AppArmor uses the upstream container restrictions with the factory profile
  name, ABI 4.0 with explicit Unix socket and user-namespace permission, and
  mount/pivot-root permission in place of the mount denial. `/proc`, `/sys`,
  signal/ptrace peer, and prohibited network-family restrictions are retained.

References: [Docker seccomp](https://docs.docker.com/engine/security/seccomp/),
[Docker AppArmor](https://docs.docker.com/engine/security/apparmor/),
[OpenAI sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing).

## Rollback

Stop containers using the named profile before unloading it. From the host:

```bash
sudo apparmor_parser -R /etc/apparmor.d/software-factory
sudo rm /etc/apparmor.d/software-factory
```

Remove the corresponding `security_opt` entries from the factory service to
return to Docker defaults; that also restores the original Codex blocker.
