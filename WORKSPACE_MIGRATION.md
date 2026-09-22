# Source-only Magnus simulator setup for the laptop agent

Prepared 2026-09-22. Objective: build the required tools from source, run the
saved rasteriser kernel on the Magnus software simulator at **128x128, eight
triangles**, and generate and open a Perfetto instruction trace. Do not port
the kernel to the laptop GPU. The laptop's RDNA 3.5 GPU is not used by this path;
the CPU runs the simulator, and the kernel target remains **gfx1310**.

This replaces the large LFS workspace snapshot for this specific workflow.
It includes the Agent documentation toolkit and the complete hardware Docs
snapshot. Other profiling tools, historical captures, temporary parsed-document
caches, and unrelated experiments' runtime dependencies are NOT included.
Do not modify the kernel algorithm, silently change pinned versions, discard
local work, push to remote, bypass organization policies, or collect secrets.

## 1. Repository and publication

Remote: `git@github.com:avaimray-amd/rdna.git`
HTTPS: `https://github.com/avaimray-amd/rdna.git`
The source-only branch is **main**. The old full-workspace LFS snapshot was on
**master** in a DIFFERENT local checkout. Do not merge it into this history.

On the source PC this small checkout is at
`C:/Users/shiny/Desktop/RDNA/SourceMigration/rdna`. Its remote is configured,
but no push is performed by the preparing agent. The owner must publish its
main branch before the laptop clone below can work. Follow PUBLISH_DOCS.md on
the source PC: Docs is split across several commits so it can be published in
separate fast-forward pushes below GitHub's 2 GiB-per-push limit. Do not push
the entire new history in one operation. No prebuilt executable or
LFS object is included in this source-only repository. If publication is
rejected, stop; do not force-push or assume the server is empty.

The repository consists of three Git submodules, a 90,725,780-byte original
Magnus source archive, a roughly 13 MB GpuKernelLab Git bundle, saved source
edits, build scripts, a Conan dependency lock, and trace-converter Python source.
Agent adds about 150 KB of source/configuration/tests, with no .cache, .scratch,
.venv, __pycache__, or bytecode. Sources/Docs adds 57 compressed archive parts
totaling 5,305,203,778 bytes, each at most 90 MiB. Restoring them produces 10,008
files totaling 6,901,542,191 bytes. Docs is now the majority of the download;
this is no longer a roughly 104 MB parent checkout. These archives are ordinary
Git files, not LFS pointers. Future re-packing can substantially grow history.
The bundle preserves GpuKernelLab commits not yet available on its remote.
Submodule repositories require their own authorization, independent of access
to this parent repository. All content must remain in approved private storage.

## 2. Prerequisites: check before building

- Windows x64 and 64-bit PowerShell. This recipe is not for Windows ARM/Linux.
- Git for Windows, including `tar.exe` and `grep.exe`. If Git is installed
  somewhere other than `C:/Program Files/Git`, add its `usr/bin` directory to PATH.
- Python **3.12 x64**, available as `py -3.12`.
- Visual Studio **2022 Build Tools**, the **Desktop development with C++**
  workload, MSVC x64/x86 compiler tools, and a Windows SDK. Ask the user/IT to
  install these if absent. VS Code itself is not a C++ compiler.
- Authenticated access to GitHub's three private repositories and AMD's
  Atlanta Artifactory Conan repositories. Use the laptop's own credentials.
  Never copy the desktop SSH private key, token store, or virtual environments.
- Network access to the approved Python package indexes and AMD dependency
  servers. Do not disable TLS validation or change VPN/security policies.

Use a short checkout path such as **C:/RDNA**. The desktop's deeply nested
test path produced CMake object-path warnings. Avoid OneDrive/network folders
for builds. Allow tens of GB of disk for LLVM/model sources, objects and Conan
dependencies; the small Git download is not the eventual build size. Start
with **two build jobs**, or one on a memory-constrained laptop. The test is tiny,
but building LLVM and the simulator is substantial and may take hours.

## 3. Clone and recover the pinned sources

Use a NEW destination. Run commands individually and stop on any failure.
An interactive SSH/login prompt must be answered by the user directly; do not
ask them to paste credentials into the agent chat. These are PowerShell commands.

```powershell
Set-Location C:/
git clone --branch main git@github.com:avaimray-amd/rdna.git RDNA
Set-Location C:/RDNA
git config core.longpaths true
```

Do NOT use `--recurse-submodules` initially: the saved GpuKernelLab HEAD may
not have been published to its own server. Check the bundled history first:

```powershell
$snapshot = Get-Content ./snapshot.json -Raw | ConvertFrom-Json
if ((Get-FileHash ./Sources/GpuKernelLab.bundle -Algorithm SHA256).Hash -ne $snapshot.sourceBundle.sha256) { throw 'Bundle hash mismatch' }
git bundle verify ./Sources/GpuKernelLab.bundle
git clone --no-checkout ./Sources/GpuKernelLab.bundle GpuKernelLab
git -C GpuKernelLab config core.autocrlf false
git -C GpuKernelLab config core.longpaths true
git -C GpuKernelLab remote set-url origin git@github.com:AMD-CG-AIM/GpuKernelLab.git
git -C GpuKernelLab switch --detach 6d75f6fa9fa35323ae6c3c921f6fec1ee89860e9
git submodule init
git submodule update --init llvm-project PyGpuDirect
```

Pins (also recorded in snapshot.json and the gitlinks):

| Project | Source | Commit |
| --- | --- | --- |
| GpuKernelLab | AMD-CG-AIM/GpuKernelLab | 6d75f6fa9fa35323ae6c3c921f6fec1ee89860e9 |
| llvm-project | AMD-Lightning-Internal/llvm-project, amd-gfx13 source | f8dbab6a14a9b81f277f327c647ba1451b275b20 |
| PyGpuDirect | AMD-CG-AIM/PyGpuDirect | 75af085ee8ea4b1ffce40a62d0a3ebc8142fa3f5 |

Do not replace a missing commit with latest. A detached child checkout is
intentional. No LFS pull is needed for this parent. Inspect any future child
LFS requirements separately; parent attributes do not apply inside submodules.

## 4. Restore source and create Python environments

```powershell
./Scripts/Check-Prerequisites.ps1
./Scripts/Initialize-Source.ps1
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r ./GpuKernelLab/requirements.txt -r ./requirements-setup.txt
py -3.12 -m venv .conanenv
./.conanenv/Scripts/python.exe -m pip install -r ./requirements-conan.txt
./.conanenv/Scripts/conan.exe --version
```

The last command must report Conan **1.66.0**, not Conan 2. Keep Conan separate
from the experiment environment. The model README mentions an older Conan;
1.66.0 is the version used for this verified dependency/configuration path.
If policy prevents PowerShell script execution, ask IT for the approved method;
do not bypass system policy. If a pinned package cannot be downloaded, stop and
report its name instead of silently substituting another version.

Initialize-Source checks commit pins, archive hash and saved file hashes,
extracts Magnus into its expected sibling folder, and restores four saved
GpuKernelLab files without staging/committing them. It refuses conflicting
local edits. These files contain the current pipeline fixes and the portable
`--model-package` runner argument. Rerunning on an unchanged setup is safe.
The archive is verified by SHA-256:
`8FE8FDBB6B8E574A263D04089F81F577FE7084E572A099F0DBDC0A25958FCABE`.

### Restore Docs and set up Agent

From C:/RDNA, restore the documentation snapshot and verify every file:

```powershell
py -3.12 ./Scripts/docs_archive.py restore --archive ./Sources/Docs --destination ./Docs
py -3.12 ./Scripts/docs_archive.py verify --archive ./Sources/Docs --destination ./Docs
```

The archive tool uses only Python's standard library. It checks each archive
part and every restored document against SHA-256 hashes in
Sources/Docs/manifest.json. It restores into a new folder and refuses to
overwrite a conflicting existing Docs tree. Rerunning against a matching tree
verifies it instead. Allow approximately 12.3 GB EXTRA free space during restore
for the temporary combined archive and extracted files, on top of the cloned
Git checkout; Git history and working archive parts also take disk space.

This is a read-only snapshot, not a newly configured Perforce workspace. Never
edit the hardware documents, including to fix parser failures. The extracted
Docs directory is ignored because Git stores its archive parts instead. The
original desktop documents were not modified. Parsed caches will be rebuilt
locally in Agent/.cache and stay ignored.

Create Agent's separate environment explicitly using Python 3.12:

```powershell
py -3.12 -m venv ./Agent/.venv
./Agent/.venv/Scripts/python.exe -m pip install -r ./Agent/requirements.txt
Push-Location ./Agent
./.venv/Scripts/python.exe -m docparse parsers
./.venv/Scripts/python.exe ./tests/smoke_mcp.py
./.venv/Scripts/python.exe ./tests/smoke_mcp.py --docs-root ../Docs
Pop-Location
```

Stop on failures before proceeding. The first MCP test is self-contained; the
second tests the restored Word/Visio SX documents. Alternatively Agent/setup.ps1
creates its environment and tests parsers, a broader corpus sample, and MCP;
its default Docs path is the sibling Docs folder. Use `-DocsRoot` for a different
document location. The short commands above avoid a broad corpus scan on the
slower laptop.

Open RDNA-Simulator.code-workspace: Agent and Docs are included as roots.
Agent/.vscode/mcp.json uses `${workspaceFolder:Agent}` for both the Python
interpreter and server script, so it does not depend on the desktop username.
Review/trust the workspace as appropriate, then use VS Code's **MCP: List
Servers** command to start **rdna-docparse** if it is not already started.
Read Agent/AGENTS.md before document research. Its .github instructions, prompt,
skill and read-only researcher definition are included; any C:/RDNA examples
mean the chosen laptop checkout root. This Agent toolkit is optional for running
the simulator and does not synchronize Copilot conversations or credentials.

## 5. Configure Conan access to AMD dependencies

Inspect existing configuration first:

```powershell
./.conanenv/Scripts/conan.exe remote list
```

Add only missing remotes, using these exact names/URLs. If a name already has
a different URL, stop for review rather than overwrite the laptop configuration.

```powershell
./.conanenv/Scripts/conan.exe remote add gfxip_conan_gfx12 https://atlartifactory.amd.com/artifactory/api/conan/gfxip_conan_gfx12
./.conanenv/Scripts/conan.exe remote add gfxip_conan_gfx11 https://atlartifactory.amd.com/artifactory/api/conan/gfxip_conan_gfx11
./.conanenv/Scripts/conan.exe remote add gfxip_conan_dk https://atlartifactory.amd.com/artifactory/api/conan/gfxip_conan_dk
./.conanenv/Scripts/conan.exe remote add gfxip_conan_local https://atlartifactory.amd.com/artifactory/api/conan/gfxip_conan_local
```

Authenticate using the organization's approved Conan 1 flow. For an interactive
login, the USER can run `./.conanenv/Scripts/conan.exe user <AMD_NTID> -r
<remote-name> -p` and enter the required password/token directly in its prompt,
for each required remote. Never put the secret in command arguments or chat.
An existing `conancenter` remote may supply public recipes; don't remove existing
remotes. The lock preserves dependency/package identities, not authentication
or guaranteed server availability. The desktop dependency check used its cache;
an uncached laptop requires permission to download those historical packages.

## 6. Build the internal GPU compiler and native bridge

Open a fresh 64-bit PowerShell at C:/RDNA. Do not reuse a shell whose PATH was
changed by a previous Conan activation without understanding that environment.

```powershell
./Scripts/Build-LLVM.ps1 -Jobs 2
./Scripts/Build-Bridge.ps1 -Jobs 2
```

Build-LLVM selects Visual Studio 2022, configures Release with only AMDGPU/X86
targets and clang/lld projects, then builds clang, lld and llvm-objdump.
Outputs go to `llvm-project/build-laptop/bin`. Do not use the standard ROCm
compiler as a substitute: this kernel uses gfx1310-specific compiler support.

Build-Bridge builds `tcore_backend.dll` from PyGpuDirect and installs it under
`PyGpuDirect/install/bin`. Its build tree also contains a simulator **link stub**.
That stub is NOT the Magnus model and must never replace csimulate_shared.dll.
The installed Python source is loaded directly from `PyGpuDirect/src`; there
is no need to download the private pygpudirect wheel for this workflow.

## 7. Build Magnus from the included source archive

```powershell
./Scripts/Build-Magnus.ps1 -Jobs 2 -ConfigureOnly
./Scripts/Build-Magnus.ps1 -Jobs 2
```

The configure-only pass checks Conan access and generators before starting the
long compilation. The script uses the captured dependency lock, adjusting only
the jobs limit in a generated local lock. It explicitly selects the .venv Python
with PyYAML/Jinja2 for model generation; otherwise the model can pick another
Python and attempt a failing package download. Conan provides the model's MSVC
19.28 toolchain and SDK dependencies, independently of the VS2022 LLVM build.

The resulting runnable package is:
`one_model-magnus-13.0.8982267.21415-src/build-laptop/package`.
It must contain `bin/csimulate_shared.dll` and `runtime/`. Do not substitute
the separate desktop DTIF package, a different Magnus version, or a link stub.
Use the Visual C++ runtime supported by the laptop's IT if a native dependency
is missing; don't copy arbitrary system DLLs or disable security software.

## 8. Run the tiny kernel test AND create its Perfetto trace

From C:/RDNA, preferably in a fresh PowerShell:

```powershell
./Scripts/Run-Smoke.ps1
```

This sets the correct compiler, source Python and trace-converter paths, verifies
the built files, and runs ONE test: **128x128, 8 triangles, seed 42, two simulated
SIMDs**. Do not increase resolution, repetitions, or triangle count. It records
instruction tracing and converts it to Perfetto automatically, without a GPU
on the laptop participating. Each invocation uses a fresh output directory.

Expected outputs under `output/smoke-<timestamp>/`:

- `result.json`: all six validation booleans true; 24 covered pixels, 16,360
  uncovered pixels, zero overflow, drained queues, 24 shades/input indices.
- `HIP_RasteriserPipeline.pftrace`: nonempty instruction/stall timeline.
- Kernel snapshot, code object and raw `se*sa*itrace*.mon` files for debugging.
- Image at `GpuKernelLab/Experiments/HIP/Rasteriser/Images/rasteriser_pipeline.png`:
  green coverage on black. Image SHA-256 for this fixture:
  `69ae0322e9f8feda71bdad08640793bf8b5b4968ddea34822589eab22e2609c3`.

Run-Smoke checks the result and presence of the trace, not only process exit.
The raw/model/conversion work may take several minutes or longer on the laptop.
Do not terminate a healthy run just because no new text appears briefly. Do not
launch a second run while one is active. No hardware performance comparison is
implied: trace timestamps are model ticks, not verified physical nanoseconds.

## 9. Open and inspect the trace

Use **Google Chrome** and the approved Perfetto UI at https://ui.perfetto.dev.
Choose **Open trace file** and select the generated .pftrace locally. Do not
upload it to a public hosting service or use a share/upload action. Follow the
organization's policy for using local traces in a web UI; use an approved local
Perfetto UI deployment if required.

Confirm there is a populated GPU wave/instruction timeline and inspect the
reuse, indirectly dispatched vertex, bin, raster and fragment work. In the SQL
query view, check:

```sql
SELECT COUNT(*) AS slice_count FROM slice;
SELECT name, value FROM stats
WHERE severity = 'error' AND value != 0;
```

The first result must be nonzero. The second should have no nonzero import
errors; report any rather than claiming a clean trace. Warnings from our trace
converter about unsupported kmcnt/loadcnt/storecnt/dscnt dependency arrows are
known: actual instructions/stalls are retained but those dependency arrows are
incomplete. A successful PNG alone does not validate trace import.

## 10. Report success and preserve state

Report exact repository commits, compiler version, model package path, test
result values, trace path/size, and Perfetto import status. Do not report success
before the laptop itself runs the test. Keep the saved source edits, generated
builds and traces local; do not commit build artifacts. Open
`RDNA-Simulator.code-workspace` for continued development. Change kernel files
only after this baseline is working and the user requests it.

## What was tested on the desktop

- Original pinned compiler/model: 128x128/eight-triangle test passed.
- A fresh native bridge built from the pinned PyGpuDirect source.
- Conan 1.66 dependency resolution using the desktop cache and fresh Magnus
  CMake configuration passed after selecting the correct Python interpreter.
- The provided Run-Smoke launcher passed with that fresh bridge/source Python
  and existing pinned compiler/model binaries: 150 seconds, 30,627,616-byte
  trace, exact expected image. Build directories/path debug data can change
  code-object hashes; compare semantic validation and image, not only that hash.
- Agent's self-contained MCP smoke test passed without desktop document paths.
- All 10,008 documents were restored from the 57 archive parts and each file
  matched its recorded SHA-256 hash. Multipart round-trip, conflict refusal
  and path-traversal checks also passed on a small synthetic test.

A complete fresh LLVM/Magnus rebuild and uncached dependency downloads were
NOT repeated as part of this setup validation. Those remain laptop acceptance
steps. No remote publication/access or laptop runtime success is assumed.
The five-stage HIP pipeline is NOT a D3D12 work graph, and this small smoke does
not certify larger workloads or resolve any separately observed larger-case
coverage failures. `Reference/desktop-smoke.json` records the original small-run
baseline; historical raw captures are intentionally omitted from Git.