# Publish the source-only workspace and documentation

For the repository owner on the source PC. No push has been performed by the
preparing agent. Use only an authorized private destination for the AMD content.
This checklist publishes the prepared main branch without force-pushing or
changing any child repository. GitHub and organization access/budget policies
still apply; private visibility alone is not redistribution authorization.

## Why there are several pushes

Docs contains 10,008 files, packaged as a 5,305,203,778-byte gzip archive split
into 57 parts of at most 90 MiB. These parts are ordinary Git blobs, not LFS.
Every file is below GitHub's 100 MiB ordinary-file limit, but GitHub separately
limits each push to 2 GiB. The archive was committed in six batches, at most
900 MiB each, to permit incremental publication.

The first checkpoint below also contains the original roughly 104 MB simulator
setup and the small Agent/tooling addition. Subsequent checkpoints add only the
next archive batch. Do not squash these into a single initial commit or try to
push the whole unpublished branch in one operation.

## Owner commands

Run from the NEW source-only checkout, not the original RDNA parent or a child:

```powershell
Set-Location C:/Users/shiny/Desktop/RDNA/SourceMigration/rdna
git remote -v
git branch --show-current
```

Verify origin is `git@github.com:avaimray-amd/rdna.git` and the branch is `main`.
Sign in interactively when prompted; never paste authentication secrets into
chat. Run each command separately, and stop immediately if one fails:

```powershell
git push origin 505b2fba393551572f8b6322bd9b1ee77e5c601e:refs/heads/main
git push origin 2389b3589ba60df3fa72d57f7964ea56a479889a:refs/heads/main
git push origin cbf506c6935dcec54ace8f897efad1df4c25ac2a:refs/heads/main
git push origin c378477971705610bdc40c3085cff2b83aaf9411:refs/heads/main
git push origin 631c5b2affcb5a223425de783a7545500520ec99:refs/heads/main
git push origin 0252cf85b940aea2f9335c5e39d8b9ef79a83d4c:refs/heads/main
git push --set-upstream origin main
```

The final push adds this publication checklist and any small follow-up guide
changes. No `--force`, `--mirror`, or LFS command is needed. These checkpoints
form a fast-forward chain, not separate branches. During the first five pushes
the archive is incomplete: wait for ALL steps before giving the laptop agent
the go-ahead to clone or restore Docs.

If the remote already contains a later checkpoint, skip already-published
ancestors only after verifying that state. If it contains unrelated history,
or a command is rejected, stop for review. Do not force, merge the old LFS
snapshot, or delete remote branches to resolve a rejection. Branch protection
may require an administrator-approved publication workflow.

## Laptop handoff

After publication succeeds, send WORKSPACE_MIGRATION.md (also copied to the
original GpuKernelLab/Docs/SIMULATOR_LAPTOP_SETUP.md). It covers cloning, source
builds, restoring Docs with per-file hashes, setting up Agent/MCP, and producing
the validated 128x128/eight-triangle Perfetto trace. The laptop must still have
access to the three private submodule repositories and AMD build dependencies.

Do not delete the source PC's originals until laptop validation succeeds.
Repacking Docs later may add another multi-gigabyte version to ordinary Git
history; this layout is a snapshot transfer, not an efficient document-update
mechanism.